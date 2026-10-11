"""Managed IO contracts: fixture subprocesses and local/in-memory HTTP only."""
from copy import deepcopy
import io
import json
import os
import socket
import ssl
import subprocess
import sys
import threading
import time
from types import SimpleNamespace
import pytest

from nerya.agent.loop import LoopConfig, WorkspaceNativeAgentLoop
from nerya.core.config import Config, DEFAULT_CONFIG
from nerya.core.errors import LLMError
from nerya.core.paths import WorkspacePaths
from nerya.core.sandbox import sandbox_exec
from nerya.harness.cancellation import CancelToken, CancelledError
from nerya.llm.adapters._base import UrllibTransport
from nerya.llm.gateway import LLMGateway
from nerya.llm.messages import MessagesRequest, OpenAIMessagesBackend
from nerya.llm.retry import is_retryable_stream_transport_error
from nerya.subagents.tasks import TaskStore
from nerya.tools.native.shell import run_shell_handler
from nerya.tools.native.tasks import task_output_handler, task_stop_handler
from nerya.tools.executor import NativeToolExecutor
from nerya.tools.orchestrator import ToolOrchestrator
from nerya.tools.permissions import PermissionContext, PermissionEngine, PermissionMode
from nerya.tools.registry import ToolRegistry
from nerya.tools.types import ToolCall, ToolDescriptor, ToolResult, RiskLevel

pytestmark = pytest.mark.smoke

@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setenv('NERYA_HOME', str(tmp_path))
    monkeypatch.setenv('NERYA_WORKSPACE', str(tmp_path))
    monkeypatch.setenv('NERYA_PROFILE', '')
    monkeypatch.setenv('NO_PROXY', '*')
    original = socket.socket.connect
    def local_only(sock, address):
        assert isinstance(address, tuple) and address[0] in {'127.0.0.1', '::1'}, address
        return original(sock, address)
    monkeypatch.setattr(socket.socket, 'connect', local_only)

def eventually(predicate, timeout=4):
    until = time.monotonic() + timeout
    while time.monotonic() < until:
        result = predicate()
        if result:
            return result
        time.sleep(.02)
    raise AssertionError('fixture condition timed out')

def test_long_output_drains_both_pipes_and_is_bounded(tmp_path):
    result = sandbox_exec([sys.executable, '-c',
        "import os; [(os.write(1,b'x'*65536),os.write(2,b'y'*65536)) for _ in range(32)]"],
        cwd=tmp_path, root=tmp_path, timeout=3, output_limit=4096)
    assert result.returncode == 0 and result.process_exited and result.process_group_stopped
    assert result.stdout == 'x' * 4096 and result.stderr == 'y' * 4096 and result.truncated

@pytest.mark.parametrize('mode', ['cancel', 'deadline', 'timeout'])
def test_cancel_timeout_reap_process_group_and_do_not_repeat(tmp_path, mode):
    token = CancelToken()
    script = ("import os,signal,time; signal.signal(signal.SIGTERM,signal.SIG_IGN); "
              "open('starts','a').write('once\\n'); print(os.getpid(),flush=True); time.sleep(30)")
    if mode == 'deadline':
        token.deadline_s = time.time() + .2
    timer = threading.Timer(.2, token.cancel) if mode == 'cancel' else None
    if timer:
        timer.start()
    started = time.monotonic()
    try:
        with pytest.raises(subprocess.TimeoutExpired if mode == 'timeout' else CancelledError) as caught:
            sandbox_exec([sys.executable, '-u', '-c', script], cwd=tmp_path, root=tmp_path,
                         cancel_token=token, timeout=.2 if mode == 'timeout' else 3)
    finally:
        if timer:
            timer.join()
    result = caught.value.result
    assert result.process_exited and result.process_group_stopped
    assert time.monotonic() - started < 2
    assert (tmp_path / 'starts').read_text() == 'once\n'
    with pytest.raises(ProcessLookupError):
        os.killpg(result.pid, 0)

def test_shell_group_descendant_is_stopped(tmp_path):
    process = sandbox_exec('sleep 30 & wait', cwd=tmp_path, root=tmp_path, shell=True,
                           background=True, timeout=4).process
    process.stop()
    result = process.wait()
    assert result.cancelled and result.process_exited and result.process_group_stopped

def test_background_shell_has_task_live_output_exit_and_stop(tmp_path):
    store = TaskStore(WorkspacePaths(tmp_path))
    result = run_shell_handler(ToolCall(name='run_shell', arguments={
        'command': 'echo ready; sleep 30', 'background': True, 'timeout_s': 3,
    }), root=tmp_path, store=store)
    task_id = result.metadata['task_id']
    store = TaskStore(WorkspacePaths(tmp_path))  # the next turn/API creates a new instance
    eventually(lambda: 'ready' in store.load(task_id).output.get('stdout', ''))
    output = task_output_handler(ToolCall(name='task_output', arguments={'task_id': task_id}), store=store)
    assert 'ready' in json.dumps(output.asdict())
    stopped = task_stop_handler(ToolCall(name='task_stop', arguments={'task_id': task_id}), store=store)
    assert 'live_worker_found' in json.dumps(stopped.asdict())
    record = eventually(lambda: (r if (r := store.load(task_id)).is_terminal() else None))
    assert record.state == 'cancelled'
    assert record.output['process_exited'] and record.output['process_group_stopped']
    assert record.output['exit_code'] is not None
    assert 'ready' in TaskStore(WorkspacePaths(tmp_path)).load(task_id).output['stdout']

def test_missing_worker_stop_is_not_false_confirmation(tmp_path):
    store = TaskStore(WorkspacePaths(tmp_path))
    record = store.create(name='run_shell', payload={})
    store._cancel_events.clear()  # emulate a lost process-local owner after restart
    reopened = TaskStore(WorkspacePaths(tmp_path))
    assert reopened.request_stop(record.task_id) is False
    assert reopened.load(record.task_id).state == 'queued'
    assert reopened.load(record.task_id).progress[-1]['payload']['stop_confirmed'] is False

def test_background_without_store_does_not_launch(tmp_path):
    result = run_shell_handler(ToolCall(name='run_shell', arguments={
        'command': 'echo x', 'background': True}), root=tmp_path)
    assert result.is_error and not list(tmp_path.glob('agent_tasks/*'))

def delta(**values):
    return {'choices': [{'index': 0, 'delta': values}]}

def finish(reason='stop'):
    return {'choices': [{'index': 0, 'delta': {}, 'finish_reason': reason}]}

class StreamTransport:
    def __init__(self, chunks, *, fail=False, check=None, status=200):
        self.chunks, self.fail, self.check, self.status = chunks, fail, check, status
        self.calls = []
    def stream_json(self, url, *, headers, body, timeout, on_event):
        self.calls.append(deepcopy(body))
        if self.status >= 400:
            return self.status, {'error': {'message': 'rejected before stream'}}, {}
        for chunk in self.chunks:
            on_event(chunk)
            if self.check:
                self.check()
        if self.fail:
            raise OSError('connection lost after submitted request')
        return 200, {'stream_complete': True}, {'content-type': 'text/event-stream'}


class PreOutputDisconnectOnce(StreamTransport):
    def stream_json(self, url, *, headers, body, timeout, on_event):
        if not self.calls:
            self.calls.append(deepcopy(body))
            raise ConnectionResetError('upstream disconnected before the first model delta')
        return super().stream_json(url, headers=headers, body=body,
                                   timeout=timeout, on_event=on_event)

def backend(transport):
    return OpenAIMessagesBackend(api_key='fixture', model='gpt-4o', transport=transport)

def request(**kwargs):
    return MessagesRequest(system='fixture', messages=[{'role': 'user', 'content': 'fixture'}], stream=True, **kwargs)

def tool_chunks(arguments=(' {"value":', '1}')):
    return [delta(tool_calls=[{'index': 0, 'id': 'call-fixture',
        'function': {'name': 'fixture_write', 'arguments': arguments[0]}}]),
        delta(tool_calls=[{'index': 0, 'function': {'arguments': arguments[1]}}]), finish('tool_calls')]

def test_real_deltas_thinking_tool_input_usage_before_response():
    events = []
    def check():
        assert events, 'event must reach consumer during transport'
    transport = StreamTransport([delta(reasoning_content='reason'), delta(content='Hello '),
        delta(content='world'), *tool_chunks(), {'usage': {'prompt_tokens': 9, 'completion_tokens': 4}}], check=check)
    response = backend(transport)(request(on_event=events.append))
    assert response.stream_mode == 'stream' and response.text() == 'Hello world'
    assert [e['text'] for e in events if e['type'] == 'text_delta'] == ['Hello ', 'world']
    assert [e['text'] for e in events if e['type'] == 'thinking_delta'] == ['reason']
    assert ''.join(e['partial_json'] for e in events if e['type'] == 'tool_input_delta') == ' {"value":1}'
    assert response.tool_uses()[0]['input'] == {'value': 1}
    assert events[-1]['usage'] == response.usage == {'input_tokens': 9, 'output_tokens': 4}

def test_split_thinking_tags_never_leak_to_public_deltas():
    events = []
    chunks = [delta(content=part) for part in [' <thi', 'nk>secret', '</thi', 'nk>public', ' answer']]
    response = backend(StreamTransport([*chunks, finish()]))(request(on_event=events.append))
    assert ''.join(e['text'] for e in events if e['type'] == 'text_delta') == 'public answer'
    assert response.text() == 'public answer'

@pytest.mark.parametrize('chunks,fail', [(tool_chunks()[:1], True),
    (tool_chunks(arguments=('{"value":', '')), False),
    ([*tool_chunks()[:-1], finish('length')], False), ([delta(content='partial')], False)])
def test_incomplete_stream_never_returns_tool_and_is_not_retryable(chunks, fail):
    transport = StreamTransport(chunks, fail=fail)
    with pytest.raises(Exception) as caught:
        backend(transport)(request())
    assert caught.value.stream_interrupted and caught.value.retryable is False
    assert len(transport.calls) == 1


def test_stream_disconnect_before_output_is_classified_for_bounded_retry():
    transport = StreamTransport([], fail=True)
    with pytest.raises(LLMError) as caught:
        backend(transport)(request())
    assert caught.value.stream_interrupted
    assert caught.value.stream_output_committed is False
    assert caught.value.retryable is True
    assert caught.value.code == 'model_stream_connection_failed'


@pytest.mark.parametrize('error,retryable', [
    (ConnectionResetError('connection reset'), True),
    (TimeoutError('read timed out'), True),
    (ssl.SSLCertVerificationError('certificate verify failed'), False),
    (PermissionError('access denied'), False),
    (socket.gaierror(socket.EAI_AGAIN, 'DNS temporarily unavailable'), True),
    (socket.gaierror(socket.EAI_NONAME, 'invalid DNS name'), False),
])
def test_stream_transport_failure_classifier_rejects_permanent_errors(error, retryable):
    assert is_retryable_stream_transport_error(error) is retryable


def test_tls_stream_failure_surfaces_without_replaying(tmp_path, monkeypatch):
    class BadTlsTransport(StreamTransport):
        def stream_json(self, url, *, headers, body, timeout, on_event):
            self.calls.append(deepcopy(body))
            raise ssl.SSLCertVerificationError('certificate verify failed')

    transport = BadTlsTransport([])
    loop = make_loop(gateway(tmp_path, monkeypatch, [transport]), [], [])
    with pytest.raises(LLMError) as caught:
        loop.run(system='fixture', user_message='Please answer.')
    assert caught.value.retryable is False
    assert caught.value.code == 'model_stream_tls_error'
    assert len(transport.calls) == 1


def test_stream_missing_finish_before_any_output_is_retryable():
    transport = StreamTransport([])
    with pytest.raises(LLMError) as caught:
        backend(transport)(request())
    assert caught.value.stream_interrupted and caught.value.retryable is True
    assert caught.value.stream_output_committed is False


@pytest.mark.parametrize('error,status,retryable', [
    ({'status': 429, 'code': 'rate_limit_exceeded', 'message': 'RPM exhausted',
      'request_id': 'req-fixture'}, 429, True),
    ({'status': 429, 'code': 'insufficient_quota', 'message': 'credits exhausted'}, 429, False),
    ({'status': 401, 'code': 'invalid_api_key', 'message': 'bad credentials'}, 401, False),
    ({'code': 'invalid_model', 'message': 'unknown model'}, None, False),
])
def test_provider_sse_error_retains_reason_and_retry_policy(error, status, retryable):
    transport = StreamTransport([{'error': error}])
    with pytest.raises(LLMError) as caught:
        backend(transport)(request())
    assert caught.value.stream_output_committed is False
    assert caught.value.retryable is retryable
    if status:
        assert caught.value.status_code == status
    if 'request_id' in error:
        assert caught.value.request_id == error['request_id']


def test_sse_rate_limit_keeps_retry_after_and_explicit_no_retry():
    for disabled in (False, True):
        details = {'status': 429, 'code': 'rate_limit_exceeded',
                   'message': 'temporarily busy', 'retry_after_ms': 1250}
        if disabled:
            details['retryable'] = False
        with pytest.raises(LLMError) as caught:
            backend(StreamTransport([{'error': details}]))(request())
        assert caught.value.retry_after_s == 1.25
        assert caught.value.retryable is not disabled


def test_invalid_provider_sse_error_does_not_retry(tmp_path, monkeypatch):
    transport = StreamTransport([{'error': {'status': 401, 'code': 'invalid_api_key',
                                            'message': 'authentication rejected'}}])
    loop = make_loop(gateway(tmp_path, monkeypatch, [transport]), [], [])
    with pytest.raises(LLMError) as caught:
        loop.run(system='fixture', user_message='Please answer.')
    assert caught.value.status_code == 401 and caught.value.retryable is False
    assert len(transport.calls) == 1

def gateway(tmp_path, monkeypatch, transports):
    data = deepcopy(DEFAULT_CONFIG)
    data['llm']['tiers'] = {'medium': {'routes': [{'provider': f'fixture{i}', 'model': 'gpt-4o'}
        for i in range(len(transports))], 'allowed_tasks': ['agent.loop']}}
    cfg = Config(paths=WorkspacePaths(tmp_path), data=data)
    gw = LLMGateway(cfg)
    monkeypatch.setattr(gw, '_resolve_messages_backend',
                        lambda *args, **kw: backend(transports[int(kw['route_cfg']['provider'][7:])]))
    return gw

def call(gw, **kwargs):
    return gw.call_messages(task='agent.loop', caller='fixture', tier='medium', system='fixture',
                           messages=[{'role': 'user', 'content': 'fixture'}], stream=True, **kwargs)

def test_gateway_does_not_fallback_after_submitted_stream(tmp_path, monkeypatch):
    first = StreamTransport([delta(content='partial'), tool_chunks()[0]], fail=True)
    backup = StreamTransport([delta(content='backup'), finish()])
    with pytest.raises(LLMError) as caught:
        call(gateway(tmp_path, monkeypatch, [first, backup]))
    assert isinstance(caught.value.__cause__, OSError)
    assert caught.value.stream_output_committed is True
    assert len(first.calls) == 1 and backup.calls == []

def test_gateway_can_fallback_after_explicit_pre_stream_rejection(tmp_path, monkeypatch):
    first, backup = StreamTransport([], status=429), StreamTransport([delta(content='backup'), finish()])
    assert call(gateway(tmp_path, monkeypatch, [first, backup])).text() == 'backup'
    assert len(first.calls) == len(backup.calls) == 1

def test_sync_transport_is_labelled_and_has_no_fake_deltas():
    events = []
    transport = SimpleNamespace(post_json=lambda *a, **kw: (200, {
        'choices': [{'message': {'content': 'whole answer'}, 'finish_reason': 'stop'}]}))
    response = backend(transport)(request(on_event=events.append))
    assert response.stream_mode == 'synchronous' and events == []

def make_loop(gw, events, writes):
    registry = ToolRegistry()
    def execute(call):
        writes.append(call.arguments)
        return ToolResult.from_text(tool_use_id=call.id, name=call.name, text='fixture write succeeded')
    registry.register(ToolDescriptor(name='fixture_write', description='fixture',
        input_schema={'type': 'object', 'properties': {'value': {'type': 'integer'}}, 'required': ['value']},
        handler=execute, risk=RiskLevel.READ, read_only=True))
    executor = NativeToolExecutor(registry=registry, permission_engine=PermissionEngine(),
                                  permission_context=PermissionContext(mode=PermissionMode.AUTO))
    return WorkspaceNativeAgentLoop(gateway=gw, registry=registry,
        orchestrator=ToolOrchestrator(registry=registry, executor=executor),
        config=LoopConfig(max_iterations=4, tier='medium'), event_sink=events.append)

def test_loop_executes_complete_tool_once_and_text_reconciles(tmp_path, monkeypatch):
    writes, events = [], []
    def check():
        assert not writes, 'tool executed during stream'
    first = StreamTransport(tool_chunks(), check=check)
    second = StreamTransport([delta(content='Done '), delta(content='from evidence.'), finish()])
    gw = gateway(tmp_path, monkeypatch, [first])
    calls = []
    def resolve(*args, **kwargs):
        transport = [first, second][len(calls)]
        calls.append(transport)
        return backend(transport)
    monkeypatch.setattr(gw, '_resolve_messages_backend', resolve)
    outcome = make_loop(gw, events, writes).run(system='fixture', user_message='execute fixture', turn_id='managed')
    assert writes == [{'value': 1}] and outcome.tool_calls == 1
    deltas = [e.block for e in events if e.block['kind'] == 'text_delta']
    final = [e.block for e in events if e.block['kind'] == 'text' and e.block.get('stream_id')]
    assert ''.join(e['text'] for e in deltas) == final[-1]['text'] == 'Done from evidence.'
    assert {e['stream_id'] for e in deltas} == {final[-1]['stream_id']}
    assert final[-1]['stream_id'] == 'managed:2:1'

def test_loop_partial_tool_is_not_executed_or_retried(tmp_path, monkeypatch):
    writes, events = [], []
    transport = StreamTransport([delta(content='partial'), tool_chunks()[0]], fail=True)
    outcome = make_loop(gateway(tmp_path, monkeypatch, [transport]), events, writes).run(
        system='fixture', user_message='execute fixture', turn_id='interrupted')
    assert not writes and len(transport.calls) == 1 and outcome.aborted
    assert outcome.abort_reason == 'stream_interrupted'
    assert '模型流式响应中断' in outcome.final_text
    assert '1 步，0 次工具调用' in outcome.final_text
    assert '错误详情' in outcome.final_text


def test_pre_output_disconnect_retries_in_same_loop_round(tmp_path, monkeypatch):
    writes, events = [], []
    transport = PreOutputDisconnectOnce([delta(content='Verified answer.'), finish()])
    loop = make_loop(gateway(tmp_path, monkeypatch, [transport]), events, writes)
    loop.config.llm_retry_base_delay = 0
    loop.config.llm_retry_max_delay = 0
    result = loop.run(system='fixture', user_message='Please answer.', turn_id='pre-output-retry')
    assert not result.aborted and result.final_text == 'Verified answer.'
    assert result.iterations == 1 and result.extra_llm_attempts == 1
    assert result.tool_calls == 0 and not writes and len(transport.calls) == 2
    assert [event.block['text'] for event in events if event.block['kind'] == 'text_delta'] == ['Verified answer.']
    assert any(event.block.get('retry', {}).get('state') == 'waiting' for event in events)


def test_real_local_http_stream_disconnect_then_recover(tmp_path, monkeypatch):
    """Integration: a 200 SSE socket closes before the first frame, then recovers."""
    from http.server import BaseHTTPRequestHandler, HTTPServer

    seen = []
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_POST(self):
            self.rfile.read(int(self.headers['Content-Length']))
            seen.append(1)
            self.send_response(200)
            self.send_header('Content-Type', 'text/event-stream')
            self.end_headers()
            if len(seen) == 2:
                frames = [delta(content='Recovered from local SSE.'), finish()]
                for frame in frames:
                    self.wfile.write(('data: ' + json.dumps(frame) + '\n\n').encode())
                self.wfile.write(b'data: [DONE]\n\n')
                self.wfile.flush()

    server = HTTPServer(('127.0.0.1', 0), Handler)
    thread = threading.Thread(target=lambda: (server.handle_request(), server.handle_request()), daemon=True)
    thread.start()
    try:
        gw = gateway(tmp_path, monkeypatch, [StreamTransport([])])
        local_backend = OpenAIMessagesBackend(api_key='fixture', model='gpt-4o',
            base_url=f'http://127.0.0.1:{server.server_port}/v1', timeout=3)
        monkeypatch.setattr(gw, '_resolve_messages_backend', lambda *a, **k: local_backend)
        writes, events = [], []
        loop = make_loop(gw, events, writes)
        loop.config.llm_retry_base_delay = 0
        loop.config.llm_retry_max_delay = 0
        result = loop.run(system='fixture', user_message='Please answer.')
        assert not result.aborted and result.final_text == 'Recovered from local SSE.'
        assert result.iterations == 1 and result.extra_llm_attempts == 1
        assert len(seen) == 2 and not writes
    finally:
        server.server_close()
        thread.join(timeout=2)
    assert not thread.is_alive()


def test_pre_output_disconnect_exhaustion_surfaces_real_failure(tmp_path, monkeypatch):
    writes, events = [], []
    transport = StreamTransport([], fail=True)
    loop = make_loop(gateway(tmp_path, monkeypatch, [transport]), events, writes)
    loop.config.llm_retry_attempts = 2
    loop.config.llm_retry_base_delay = 0
    loop.config.llm_retry_max_delay = 0
    with pytest.raises(LLMError) as caught:
        loop.run(system='fixture', user_message='Please answer.')
    assert caught.value.code == 'model_stream_connection_failed'
    assert caught.value.retry_exhausted == 'attempt_limit'
    assert len(transport.calls) == 2 and not writes


def test_after_completed_tool_pre_output_retry_never_repeats_write(tmp_path, monkeypatch):
    writes, events = [], []
    first = StreamTransport(tool_chunks())
    second = PreOutputDisconnectOnce([delta(content='Done.'), finish()])
    gw = gateway(tmp_path, monkeypatch, [first])
    routed = []
    def resolve(*args, **kwargs):
        routed.append(1)
        return backend(first if len(routed) == 1 else second)
    monkeypatch.setattr(gw, '_resolve_messages_backend', resolve)
    loop = make_loop(gw, events, writes)
    loop.config.llm_retry_base_delay = 0
    loop.config.llm_retry_max_delay = 0
    result = loop.run(system='fixture', user_message='Complete the fixture.')
    assert not result.aborted and result.final_text == 'Done.'
    assert result.tool_calls == 1 and writes == [{'value': 1}]
    assert len(first.calls) == 1 and len(second.calls) == 2

def test_urllib_sse_parser_consumes_frames_and_done(monkeypatch):
    import urllib.request
    chunks = [delta(content='one'), delta(content=' two'), finish()]
    wire = b': heartbeat\r\n\r\n' + b''.join(
        ('data: ' + json.dumps(c) + '\r\n\r\n').encode() for c in chunks) + b'data: [DONE]\n\n'
    response = io.BytesIO(wire)
    response.status = 200
    response.headers = {'Content-Type': 'text/event-stream'}
    monkeypatch.setattr(urllib.request, 'urlopen', lambda *a, **kw: response)
    events = []
    result = backend(UrllibTransport())(request(on_event=events.append))
    assert result.text() == 'one two' and response.closed
    assert [e['text'] for e in events if e['type'] == 'text_delta'] == ['one', ' two']

@pytest.mark.parametrize('stream', [False, True])
@pytest.mark.parametrize('mode', ['cancel', 'deadline'])
def test_local_http_cancel_closes_connection_and_joins_caller(stream, mode):
    from http.server import BaseHTTPRequestHandler, HTTPServer
    connected, closed = threading.Event(), threading.Event()
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass
        def do_POST(self):
            self.rfile.read(int(self.headers['Content-Length']))
            self.send_response(200)
            self.send_header('Content-Type', 'text/event-stream' if stream else 'application/json')
            self.end_headers()
            self.wfile.write(b'data: {"choices": [{"delta": {"content": "first"}}]}\n\n' if stream else b'{')
            self.wfile.flush()
            connected.set()
            self.connection.settimeout(3)
            try:
                if self.connection.recv(1) == b'':
                    closed.set()
            except ConnectionResetError:
                closed.set()
    server = HTTPServer(('127.0.0.1', 0), Handler)
    serving = threading.Thread(target=server.handle_request)
    serving.start()
    token = CancelToken()
    errors = []
    url = f'http://127.0.0.1:{server.server_port}/v1'
    def run():
        try:
            backend_ = OpenAIMessagesBackend(api_key='fixture', model='gpt-4o', base_url=url)
            backend_(MessagesRequest(system='', messages=[], stream=stream, cancel_token=token,
                                      deadline=time.time() + .3 if mode == 'deadline' else None))
        except BaseException as exc:
            errors.append(exc)
    caller = threading.Thread(target=run)
    caller.start()
    try:
        assert connected.wait(2)
        if mode == 'cancel':
            token.cancel('fixture stop')
        caller.join(2)
        assert not caller.is_alive()
        assert len(errors) == 1 and isinstance(errors[0], CancelledError)
        assert closed.wait(1), 'server must observe socket EOF, not only a cancelled wrapper'
    finally:
        token.cancel()
        caller.join(4)
        serving.join(4)
        server.server_close()

def test_foreground_shell_deadline_consumed_and_exit_confirmed(tmp_path):
    result = run_shell_handler(ToolCall(name='run_shell', arguments={'command': 'sleep 30'},
        metadata={'turn_deadline_epoch': time.time() + .15}), root=tmp_path)
    assert result.is_error and result.error.kind.value == 'timeout'
    assert result.error.retryable is False
    assert result.error.detail['process_exited'] and result.error.detail['process_group_stopped']

def test_pre_cancelled_shell_never_starts(tmp_path):
    token = CancelToken()
    token.cancel()
    result = run_shell_handler(ToolCall(name='run_shell', arguments={'command': 'echo unused'},
        metadata={'cancel_token': token}), root=tmp_path)
    assert result.is_error and result.error.kind.value == 'aborted'
    assert result.error.retryable is False

def test_loop_cancel_during_stream_does_not_execute_partial_tool(tmp_path, monkeypatch):
    writes, events = [], []
    token = CancelToken()
    transport = StreamTransport([delta(content='partial'), tool_chunks()[0]], check=token.cancel)
    outcome = make_loop(gateway(tmp_path, monkeypatch, [transport]), events, writes).run(
        system='fixture', user_message='fixture', cancel_token=token, turn_id='cancel')
    assert not writes and len(transport.calls) == 1
    assert outcome.stop_reason == 'cancelled'
    final = [e.block for e in events if e.block['kind'] == 'text' and e.block.get('stream_id')][-1]
    assert final['stream_id'] == 'cancel:1:1' and final['text'].startswith('partial')

def test_after_tool_commits_stream_failure_never_replays_tool(tmp_path, monkeypatch):
    writes, events = [], []
    transports = [StreamTransport(tool_chunks()), StreamTransport([delta(content='partial')], fail=True)]
    gw = gateway(tmp_path, monkeypatch, [transports[0]])
    pending = iter(transports)
    monkeypatch.setattr(gw, '_resolve_messages_backend', lambda *a, **k: backend(next(pending)))
    outcome = make_loop(gw, events, writes).run(system='fixture', user_message='fixture')
    assert writes == [{'value': 1}] and outcome.tool_calls == 1
    assert outcome.aborted and outcome.abort_reason == 'stream_interrupted'
    assert [len(t.calls) for t in transports] == [1, 1]
    assert outcome.checkpoint.resumable is False

def test_retry_before_stream_uses_new_identity(tmp_path, monkeypatch):
    from nerya.core.errors import LLMError
    import nerya.agent.loop as loop_module
    writes, events = [], []
    transport = StreamTransport([delta(content='verified'), finish()])
    gw = gateway(tmp_path, monkeypatch, [transport])
    calls = []
    def resolve(*args, **kwargs):
        calls.append(1)
        if len(calls) == 1:
            def reject(request):
                exc = LLMError('provider temporarily unavailable (503)')
                exc.status_code = 503
                raise exc
            return reject
        return backend(transport)
    monkeypatch.setattr(gw, '_resolve_messages_backend', resolve)
    # Existing retry policy may have a backoff; set it to zero in the fixture.
    monkeypatch.setattr(loop_module, 'retry_delay', lambda *a, **k: 0, raising=False)
    outcome = make_loop(gw, events, writes).run(system='fixture', user_message='fixture', turn_id='retry')
    deltas = [e.block for e in events if e.block['kind'] == 'text_delta']
    assert len(calls) == 2 and outcome.final_text == 'verified'
    assert {e['stream_id'] for e in deltas} == {'retry:1:2'}

def test_json_response_to_stream_request_remains_synchronous(monkeypatch):
    import urllib.request
    response = io.BytesIO(json.dumps({'choices': [{'message': {'content': 'whole'}, 'finish_reason': 'stop'}]}).encode())
    response.status, response.headers = 200, {'Content-Type': 'application/json'}
    monkeypatch.setattr(urllib.request, 'urlopen', lambda *a, **k: response)
    events = []
    result = backend(UrllibTransport())(request(on_event=events.append))
    assert result.stream_mode == 'synchronous' and result.text() == 'whole' and events == []

def test_background_large_output_completes_and_persists_in_taskstore(tmp_path):
    store = TaskStore(WorkspacePaths(tmp_path))
    task = store.create(name='run_shell', payload={})
    process = sandbox_exec([sys.executable, '-c', "import os; os.write(1,b'x'*2000000); os.write(2,b'y'*2000000)"],
        cwd=tmp_path, root=tmp_path, background=True, timeout=3, output_limit=2048).process
    store.attach_process(task.task_id, process)
    reopened = TaskStore(WorkspacePaths(tmp_path))
    result = eventually(lambda: (r if (r := reopened.load(task.task_id)).is_terminal() else None))
    assert result.state == 'succeeded' and result.output['exit_code'] == 0
    assert result.output['stdout'] == 'x'*2048 and result.output['stderr'] == 'y'*2048
    assert result.output['truncated'] and result.output['process_group_stopped']

def test_cancelled_gateway_never_falls_back_even_after_backend_returns(tmp_path, monkeypatch):
    from nerya.llm.messages import MessagesResponse
    token = CancelToken()
    attempts = []
    gw = gateway(tmp_path, monkeypatch, [StreamTransport([]), StreamTransport([])])
    def resolve(*a, **k):
        def run(req):
            attempts.append(1)
            token.cancel()
            return MessagesResponse(content=[{'type': 'text', 'text': 'too late'}])
        return run
    monkeypatch.setattr(gw, '_resolve_messages_backend', resolve)
    with pytest.raises(CancelledError):
        call(gw, cancel_token=token)
    assert attempts == [1]

def test_transport_observer_failure_cannot_replay_completed_request(tmp_path, monkeypatch):
    first = StreamTransport([delta(content='done'), finish()])
    backup = StreamTransport([delta(content='duplicate'), finish()])
    def event(event):
        if event['type'] == 'transport':
            raise ValueError('observer failed')
    assert call(gateway(tmp_path, monkeypatch, [first, backup]), on_event=event).text() == 'done'
    assert len(first.calls) == 1 and not backup.calls

def test_urllib_http_error_body_is_read_once_and_closed(monkeypatch):
    import urllib.error
    import urllib.request
    payload = io.BytesIO(b'{"error":{"message":"fixture denial"}}')
    error = urllib.error.HTTPError('https://fixture.invalid', 429, 'rate limited', {}, payload)
    def reject(*a, **k):
        raise error
    monkeypatch.setattr(urllib.request, 'urlopen', reject)
    status, doc = UrllibTransport().post_json('https://fixture.invalid', headers={}, body={}, timeout=1)
    assert status == 429 and doc['error']['message'] == 'fixture denial' and payload.closed

def test_local_http_increment_reaches_consumer_before_server_finishes():
    from http.server import BaseHTTPRequestHandler, HTTPServer
    observed = threading.Event()
    handshake = []
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass
        def do_POST(self):
            self.rfile.read(int(self.headers['Content-Length']))
            self.send_response(200)
            self.send_header('Content-Type', 'text/event-stream')
            self.end_headers()
            separator = chr(10) * 2
            self.wfile.write(('data: ' + json.dumps(delta(content='early')) + separator).encode())
            self.wfile.flush()
            handshake.append(observed.wait(2))
            self.wfile.write(('data: ' + json.dumps(finish()) + separator + 'data: [DONE]' + separator).encode())
            self.wfile.flush()
    server = HTTPServer(('127.0.0.1', 0), Handler)
    thread = threading.Thread(target=server.handle_request)
    thread.start()
    events = []
    def consume(event):
        events.append(event)
        if event.get('text') == 'early':
            observed.set()
    try:
        result = OpenAIMessagesBackend(api_key='fixture', model='gpt-4o',
            base_url=f'http://127.0.0.1:{server.server_port}/v1')(request(on_event=consume, cancel_token=CancelToken()))
        assert result.text() == 'early' and handshake == [True]
        assert events[0]['type'] == 'text_delta'
    finally:
        thread.join(3)
        server.server_close()
