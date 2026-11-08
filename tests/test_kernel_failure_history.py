"""Native loop exceptions retain the completed public transcript in history."""
from copy import deepcopy
import json

import pytest

from nerya.agent import kernel as kernel_module
from nerya.agent.kernel import AgentKernel
from nerya.core.config import Config, DEFAULT_CONFIG
from nerya.core.errors import LLMError
from nerya.core.paths import WorkspacePaths
from nerya.db.repositories import AgentSessionRepository
from nerya.db.sqlite import connect
from nerya.llm.messages import MessagesResponse
from nerya.tools.registry import ToolDescriptor
from nerya.tools.types import RiskLevel, ToolResult
from nerya.tools.verification import attach_verification, verification_receipt


pytestmark = pytest.mark.smoke


def test_approval_resume_keeps_pending_write_before_completed_discovery():
    completed = [{'skill_id': 'native', 'action': 'Skill', 'ok': True,
                  'payload': {'index': i}, 'result': 'observed discovery ' * 100}
                 for i in range(35)]
    payload = {'path': 'evolution/proposals/fixture/strategy.yml',
               'contents': 'description: ' + 'saved strategy ' * 220
                           + '\nparams:\n  price_simulation:\n    dataset_id: pool\n'}
    pending = {'call_id': 'pending-write', 'skill_id': 'native', 'action': 'write_file',
               'payload': payload, 'ok': False, 'error_kind': 'permission_pending'}
    context = kernel_module._approval_resume_context_from_trace([*completed, pending])
    assert 'pending-write' in context and 'write_file' in context
    assert json.dumps(payload, ensure_ascii=False) in context
    assert context.index('write_file') < context.index('Observed tool results:')
    assert 'Approval is not execution' in context
    assert 'reissue the exact pending call' in context
    assert len(context) <= 6000
    secret = 'sk-' + 'sensitive123' * 4
    sensitive = {**pending, 'payload': {**payload, 'api_key': secret,
                                      'headers': {'Authorization': 'Bearer ' + secret}}}
    safe_context = kernel_module._approval_resume_context_from_trace([*completed, sensitive])
    assert secret not in safe_context and '__redacted__' in safe_context
    assert json.dumps(payload['contents'], ensure_ascii=False) in safe_context
    assert payload['path'] in safe_context
    assert sensitive['payload']['api_key'] == secret


def test_user_continuation_block_is_not_assistant_stream(tmp_path, monkeypatch):
    from nerya.agent.transcript_blocks import BlockEnvelope
    from nerya.agent.streaming import StreamingEventBus

    bus = StreamingEventBus()
    events = []
    bus.subscribe(events.append)
    monkeypatch.setattr(kernel_module, 'get_default_bus', lambda: bus)

    def run(loop, **kwargs):
        loop.event_sink(BlockEnvelope(seq=1, turn_id='turn-role', message_id='feedback',
            role='user', block={'kind': 'text', 'text': 'internal continuation feedback'}))
        raise LLMError('fixture stopped')

    monkeypatch.setattr(kernel_module.WorkspaceNativeAgentLoop, 'run', run)
    cfg = Config(paths=WorkspacePaths(tmp_path), data=deepcopy(DEFAULT_CONFIG))
    with pytest.raises(LLMError):
        AgentKernel(config=cfg, skills=None).run_turn(
            trigger={'payload': {'text': 'Continue'}},
            session_id='session-role', turn_id='turn-role')
    assert not any(e['kind'] == 'message.delta' and
                   e.get('text') == 'internal continuation feedback' for e in events)


@pytest.mark.parametrize('user_text', ['完成这项任务', 'Finish this task'])
@pytest.mark.parametrize('tool_count', [1, 42])
def test_gateway_failure_keeps_completed_native_history(tmp_path, monkeypatch, user_text, tool_count):
    secret = 'sk-' + 'abc123' * 8
    failure = LLMError('provider-network-config diagnostic api_key=' + secret)
    failure.code = 'model_network_config'
    failure.retryable = False
    calls, writes = [], []
    call_ids = [f'call-{i}-' + 'provider123' * 5 for i in range(tool_count)]
    receipt = verification_receipt(artifact_id='fixture', revision='a' * 64, scope='fixture-only',
        checks=[{'check_id': 'observed', 'status': 'passed'}], unverified=['Task completion'])

    class Gateway:
        def __init__(self, *_args, **_kwargs):
            pass

        def effective_model_metadata(self, *_args, **_kwargs):
            return 'fixture', 'fixture-model', {}

        def call_messages(self, **kwargs):
            calls.append(deepcopy(kwargs['messages']))
            if len(calls) > 1:
                raise failure
            kwargs['on_event']({'type': 'text_delta', 'text': 'Checking fixture.'})
            return MessagesResponse(content=[
                {'type': 'thinking', 'thinking': 'Completed reasoning', 'summary': 'Provider summary'},
                {'type': 'text', 'text': 'Checking fixture.'},
                *[{'type': 'tool_use', 'id': call_id, 'name': 'fixture_receipt', 'input': {'value': i}}
                  for i, call_id in enumerate(call_ids)],
            ], stop_reason='tool_use', stream_mode='stream',
                provider='fixture', model='fixture-model',
                usage={'input_tokens': 10, 'output_tokens': 4})

    monkeypatch.setattr(kernel_module, 'LLMGateway', Gateway)
    cfg = Config(paths=WorkspacePaths(tmp_path), data=deepcopy(DEFAULT_CONFIG))
    cfg.data['agent']['native']['llm_retry_attempts'] = 1
    kernel = AgentKernel(config=cfg, skills=None)

    def execute(call):
        writes.append(call.arguments)
        return attach_verification(ToolResult.from_json(tool_use_id=call.id, name=call.name,
            data={'receipt': 'observed-only', 'value': call.arguments['value']}), receipt)

    kernel.tool_registry.register(ToolDescriptor(name='fixture_receipt', description='Fixture receipt',
        input_schema={'type': 'object', 'properties': {'value': {'type': 'integer'}}},
        handler=execute, risk=RiskLevel.READ, read_only=True))
    with pytest.raises(LLMError) as caught:
        kernel.run_turn(trigger={'source': 'dashboard', 'payload': {'text': user_text}},
            session_id='session-failure', turn_id='turn-failure')
    assert caught.value is failure
    assert sorted(item['value'] for item in writes) == list(range(tool_count))
    assert len(calls) == 2

    con = connect(cfg.paths.db)
    try:
        repo = AgentSessionRepository(con)
        rows = repo.transcript('session-failure', limit=0)
        events = repo.tool_events('session-failure')
        assert repo.peek_turn_checkpoint('session-failure') is None
    finally:
        con.close()
    assert [row['role'] for row in rows] == ['user', 'assistant']
    saved = json.loads(rows[-1]['meta_json'])['turn']
    blocks = [env['block'] for env in saved['blocks'] if env['block']['kind'] != 'tool_batch_summary']
    assert [b['kind'] for b in blocks] == ['thinking', 'text',
        *['tool_use'] * tool_count, *['tool_result'] * tool_count, 'text']
    assert blocks[0]['summary'] == 'Provider summary'
    assert blocks[1]['text'] == 'Checking fixture.'
    uses = [b for b in blocks if b['kind'] == 'tool_use']
    results = [b for b in blocks if b['kind'] == 'tool_result']
    assert [b['call_id'] for b in uses] == [b['call_id'] for b in results] == call_ids
    assert all(b['ok'] is True and b['verification'] == receipt for b in results)
    assert all('observed-only' in str(b['result']) for b in results)
    assert [event['phase'] for event in events] == ['tool_use'] * tool_count + ['tool_result'] * tool_count
    assert [env['seq'] for env in saved['blocks']] == sorted({env['seq'] for env in saved['blocks']})
    assert len({b['block_id'] for b in blocks}) == len(blocks)
    result = caught.value.turn_result
    assert result['blocks'] == saved['blocks'] == result['steps']
    assert result['tool_trace'] == saved['tool_trace']
    assert [item['call_id'] for item in result['tool_trace']] == call_ids
    assert result['budget']['aborted'] is True
    assert result['budget']['tool_calls'] == saved['budget']['tool_calls'] == tool_count
    assert result['budget']['iterations'] == saved['budget']['iterations'] == 2
    assert result['budget']['llm_attempts'] == 2
    assert result['budget']['llm_calls'] == 1
    assert result['budget']['input_tokens_total'] == 10
    assert result['budget']['output_tokens_total'] == 4
    assert result['budget']['reported_provider'] == 'fixture'
    assert saved['execution_state']['model_usage'] == result['execution_state']['model_usage']
    assert result['stopped_reason'] == 'error'
    assert result['transition_reason'] == 'llm_error'
    assert result['actions'][-1]['ok'] is False
    final = result['final_text']
    assert final == saved['final_text'] == rows[-1]['content'] == blocks[-1]['text']
    assert ('本轮未能完成' in final) if user_text.startswith('完成') else ('could not finish' in final)
    assert 'provider-network-config' not in final
    assert 'Checking fixture.' not in final
    assert secret not in json.dumps(result, ensure_ascii=False)
    assert 'provider-network-config' in result['budget']['abort_reason']


def test_real_gateway_discard_then_failure_does_not_promote_preview_or_replay(tmp_path, monkeypatch):
    from test_managed_io_polish import StreamTransport, backend, delta, gateway, tool_chunks

    first = StreamTransport([delta(reasoning_content='Completed thinking'),
        delta(content='Completed commentary.'), *tool_chunks(),
        {'usage': {'prompt_tokens': 9, 'completion_tokens': 4}}])
    preview = StreamTransport([delta(content='DISCARDED_PREVIEW'),
        delta(reasoning_content='DISCARDED_THINKING'), *tool_chunks()[:-1]], fail=True)
    denied = StreamTransport([], status=401)
    gw = gateway(tmp_path, monkeypatch, [first])
    transports = iter([first, preview, denied, denied])
    monkeypatch.setattr(gw, '_resolve_messages_backend', lambda *args, **kwargs: backend(next(transports)))
    monkeypatch.setattr(kernel_module, 'LLMGateway', lambda *_args: gw)
    gw.config.data['agent']['native'].update(llm_retry_attempts=2,
        llm_retry_base_delay=0, llm_retry_max_delay=0)
    kernel = AgentKernel(config=gw.config, skills=None)
    writes = []

    def execute(call):
        writes.append(call.arguments)
        return ToolResult.from_text(tool_use_id=call.id, name=call.name, text='fixture write succeeded')

    kernel.tool_registry.register(ToolDescriptor(name='fixture_write', description='Fixture',
        input_schema={'type': 'object', 'properties': {'value': {'type': 'integer'}}},
        handler=execute, risk=RiskLevel.READ, read_only=True))
    for turn_id in ['retry-failure', 'fresh-failure']:
        with pytest.raises(LLMError) as caught:
            kernel.run_turn(trigger={'payload': {'text': 'Finish the task'}},
                turn_id=turn_id, session_id=turn_id)
        result = caught.value.turn_result
        con = connect(gw.config.paths.db)
        try:
            rows = AgentSessionRepository(con).transcript(turn_id, limit=0)
            saved = json.loads(rows[-1]['meta_json'])['turn']
        finally:
            con.close()
        assert saved['blocks'] == result['blocks']
        assert result['budget']['aborted'] is True
        assert result['stopped_reason'] == 'error'
        assert 'DISCARDED_' not in json.dumps(result)
        assert writes == [{'value': 1}]
        if turn_id == 'retry-failure':
            assert result['budget']['iterations'] == 2
            assert result['budget']['llm_attempts'] == 3
            assert result['budget']['input_tokens_total'] == 9
            assert result['budget']['output_tokens_total'] == 4
            assert result['budget']['tool_calls'] == 1
            blocks = [env['block'] for env in result['blocks']]
            assert any(b.get('text') == 'Completed commentary.' for b in blocks)
            assert any(b.get('text') == 'Completed thinking' for b in blocks)
            assert sum(b.get('kind') == 'tool_result' for b in blocks) == 1
            assert sum(b.get('status') == 'discarded' for b in blocks) == 1
            assert preview.calls[0]['messages'] == denied.calls[0]['messages']
        else:
            assert result['budget']['iterations'] == 1
            assert result['budget']['llm_attempts'] == 1
            assert result['budget']['tool_calls'] == 0
            assert result['budget']['input_tokens_total'] == 0
            assert result['tool_trace'] == []
            assert [env['block']['kind'] for env in result['blocks']] == ['text']
            assert 'Completed commentary.' not in json.dumps(result)
