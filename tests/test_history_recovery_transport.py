"""Offline native dispatch/checkpoint transport; no live runtime or model calls."""
from copy import deepcopy
from dataclasses import replace
import json
import socket

import pytest

from nerya.agent.loop_state import LoopRunState
from nerya.agent.tool_projection import project_tool_results
from nerya.core import yaml_io
from nerya.strategies.tool_verification import source_binding
from nerya.tools.executor import NativeToolExecutor
from nerya.tools.native.bootstrap import build_native_tool_deps, register_native_tools
from nerya.tools.orchestrator import ToolOrchestrator
from nerya.tools.permissions import PermissionContext, PermissionEngine, PermissionMode
from nerya.tools.registry import ToolRegistry
from nerya.tools.result_contracts import tool_json_data
from test_backtest_history_gap_recovery import candidate as candidate


pytestmark = pytest.mark.smoke


def dispatch(config, call):
    registry = ToolRegistry()
    deps = build_native_tool_deps(workspace_root=config.paths.root, skill_roots=[],
                                  config=config, paths=config.paths)
    register_native_tools(registry, deps)
    descriptor = registry.get(call.name)
    invoked = []

    def observed(actual):
        result = descriptor.handler(actual)
        invoked.append(result)
        return result

    registry.register(replace(descriptor, handler=observed), replace=True)
    executor = NativeToolExecutor(registry=registry, permission_engine=PermissionEngine(),
        permission_context=PermissionContext(mode=PermissionMode.YOLO))
    batch = ToolOrchestrator(registry=registry, executor=executor).run_batch([call])
    assert len(invoked) == 1 and batch.auto_retries == 0
    assert len(batch.results) == 1 and batch.error_count == 1
    # The native executor returns the producer object, with no error remapping.
    assert batch.results[0] is invoked[0]
    return batch.results[0]


def checkpoint_roundtrip(result):
    state = LoopRunState(turn_id='transport-turn', message_id='transport-message',
        completed_tool_results=[result], tool_result_by_fingerprint={'fixture': result})
    wire = json.loads(json.dumps(state.to_checkpoint(resumable=True).asdict(), allow_nan=False))
    restored = LoopRunState.from_checkpoint(wire)
    return restored.completed_tool_results[0], restored.tool_result_by_fingerprint['fixture']


def assert_recovery(result, reason, retryable, action):
    assert result.is_error and result.semantic_success is False
    assert result.error is not None and result.error.kind.value == 'execution_error'
    assert result.error.retryable is retryable
    assert result.error.recovery_hint['reason'] == reason
    body = tool_json_data(result)
    assert body['ok'] is False and body.get('coverage_ok') is not True
    assert body['reason'] == reason
    assert body['next_required_action']['type'] == action
    projection = project_tool_results([result],
        render_tool_result=lambda item: {'type': 'tool_result', 'content': item.text()},
        rendered_tool_result_text=lambda item: item.get('content'))
    event = projection.event_blocks[0]
    assert event['ok'] is False and event['result'] is None
    assert event.get('display_result') is None
    assert event['recovery'] == result.error.recovery_hint
    assert event['recovery']['reason'] == reason
    assert event['recovery']['preserve_candidate'] is True
    if reason != 'protocol_replay_model_required':
        assert event['recovery']['preserve_requested_constraints'] is True
        assert event['recovery']['safe_to_retry'] is retryable
    assert event['recovery']['next_required_action'] == body['next_required_action']
    json.dumps(event, allow_nan=False)


@pytest.mark.parametrize('scenario,reason,retryable,action', [
    ('unsupported', 'historical_provider_unsupported', False, 'provide_historical_capability'),
    ('empty_cache', 'backtest_data_incomplete', True, 'prepare_historical_data'),
    ('protocol', 'protocol_replay_model_required', False, 'provide_protocol_replay'),
])
def test_registered_native_dispatch_and_checkpoint_keep_typed_recovery(
        candidate, monkeypatch, scenario, reason, retryable, action):
    config, call = candidate
    # Refuse every socket, including loopback; this is not live UI acceptance.
    monkeypatch.setattr(socket.socket, 'connect_ex', lambda *a, **kw: pytest.fail('No network'))
    if scenario != 'unsupported':
        path = (config.paths.root / 'evolution/proposals' / call.arguments['proposal_id']
                / 'after/strategies/history_fixture/strategy.yml')
        manifest = yaml_io.load(path)
        manifest['markets'] = ['BYBIT:SOLUSDC' if scenario == 'empty_cache' else 'AAVE_V3:ethereum-USDC']
        yaml_io.dump(path, manifest)
    before_args, before_source = deepcopy(call.arguments), source_binding(config, call)
    result = dispatch(config, call)
    expected_hint = deepcopy(result.error.recovery_hint)
    assert_recovery(result, reason, retryable, action)
    restored = checkpoint_roundtrip(result)
    for item in restored:
        assert item.tool_use_id == call.id and item.name == call.name
        assert item is not result and item.error is not result.error
        assert item.error.recovery_hint == expected_hint
        assert item.metadata['verification'] == result.metadata['verification']
        assert_recovery(item, reason, retryable, action)
    restored[0].error.recovery_hint['reason'] = 'changed-copy'
    assert restored[1].error.recovery_hint == result.error.recovery_hint == expected_hint
    assert call.arguments == before_args and source_binding(config, call) == before_source


@pytest.mark.parametrize('scenario,reason', [
    ('history', 'historical_provider_unsupported'),
    ('protocol', 'protocol_replay_model_required'),
])
def test_kernel_bridge_persists_recovery_for_running_command(candidate, monkeypatch, scenario, reason):
    """Real producer, command lease, kernel event bridge and SQLite event replay."""
    from nerya.agent import kernel as kernel_module
    from nerya.agent.command_runtime import CommandRuntime
    from nerya.agent.streaming import StreamingEventBus
    from nerya.core.config import DEFAULT_CONFIG
    from nerya.core.errors import LLMError
    from nerya.llm.gateway import LLMGateway
    from nerya.llm.messages import MessagesResponse
    from nerya.skills.builtin.backtest.scripts import backtest_run, data_cache
    from nerya.strategies import request_scope
    from test_request_scope import FakeGateway

    config, call = candidate
    config.data = deepcopy(DEFAULT_CONFIG)
    config.data['llm']['tiers'] = {'medium': {'provider': 'openai', 'model': 'fixture-model',
                                            'allowed_tasks': ['agent.loop']}}
    config.data['agent']['native'].update(tier='medium', llm_retry_attempts=1)
    config.data['plugins'] = {'enabled': False}
    monkeypatch.setenv('NERYA_SQLITE_JOURNAL_MODE', 'delete')
    monkeypatch.setattr(socket.socket, 'connect_ex', lambda *a, **kw: pytest.fail('No network'))
    market = 'JUPITER:SOL/USDC' if scenario == 'history' else 'AAVE_V3:ethereum-USDC'
    text = ('Jupiter SOL/USDC strategy, 1h candles; no trading.' if scenario == 'history'
            else 'Aave v3 ethereum USDC strategy, 1h candles; no trading.')
    path = (config.paths.root / 'evolution/proposals' / call.arguments['proposal_id']
            / 'after/strategies/history_fixture/strategy.yml')
    manifest = yaml_io.load(path)
    manifest['markets'] = [market]
    yaml_io.dump(path, manifest)

    def extracted(docs):
        doc = docs[0]
        values = ({'venue': 'Jupiter', 'base': 'SOL', 'quote': 'USDC', 'timeframe': '1h'}
                  if scenario == 'history' else
                  {'protocol': 'Aave v3', 'chain': 'ethereum', 'asset': 'USDC', 'timeframe': '1h'})
        fields = dict.fromkeys(request_scope.FIELDS)
        for key, value in values.items():
            start = doc['text'].index(value)
            fields[key] = {'command_id': doc['command_id'], 'start': start,
                           'end': start + len(value), 'text': value}
        return {'status': 'resolved', 'fields': fields, 'additional_resources': [], 'amendments': []}

    scope_gateway = FakeGateway(extracted)
    monkeypatch.setattr(LLMGateway, 'call_messages',
        lambda self, **kwargs: scope_gateway.call_messages(**kwargs))
    produced, thrown, model_calls, contexts = [], [], [], []
    original_backtest = backtest_run.run_strategy_backtest

    def observed_backtest(**kwargs):
        try:
            return original_backtest(**kwargs)
        except BaseException as exc:
            thrown.append(exc)
            raise

    monkeypatch.setattr(backtest_run, 'run_strategy_backtest', observed_backtest)

    class Gateway:
        def __init__(self, *_args, **_kwargs):
            pass

        def effective_model_metadata(self, *_args, **_kwargs):
            return 'openai', 'fixture-model', {}

        def call_messages(self, **kwargs):
            model_calls.append(kwargs)
            if len(model_calls) > 1:
                failure = LLMError('Fixture stops after one tool; no model recovery execution.')
                failure.retryable = False
                raise failure
            return MessagesResponse(content=[{'type': 'tool_use', 'id': call.id,
                'name': call.name, 'input': deepcopy(call.arguments)}], stop_reason='tool_use',
                provider='openai', model='fixture-model')

    monkeypatch.setattr(kernel_module, 'LLMGateway', Gateway)
    bus = StreamingEventBus()
    monkeypatch.setattr(kernel_module, 'get_default_bus', lambda: bus)
    monkeypatch.setattr('nerya.agent.command_runtime.get_default_bus', lambda: bus)

    def execute(bound_config, request):
        contexts.append(bound_config)
        kernel = kernel_module.AgentKernel(config=bound_config, skills=None,
                                           permission_mode=PermissionMode.YOLO)
        descriptor = kernel.tool_registry.get(call.name)

        def observed(actual):
            result = descriptor.handler(actual)
            produced.append(result)
            return result

        kernel.tool_registry.register(replace(descriptor, handler=observed), replace=True)
        return kernel.run_turn(trigger={'source': 'dashboard', 'payload': request['payload']},
            session_id=request['session_id'], turn_id=request['turn_id'],
            cancel_token=request['_task_cancel_token']).asdict()

    runtime = CommandRuntime(config, execute, epoch='transport-fixture')
    cid, sid, tid = 'history-transport-command', 'history-transport-session', 'history-transport-turn'
    request = {'payload': {'text': text}, 'session_id': sid, '_auth_actor_id': 'owner',
               '_auth_scopes': ['execute:strategy'], 'model_provider': 'openai',
               'model_id': 'fixture-model', 'assistant_intent': {'disposition': 'conversation'}}
    runtime.store.accept(cid=cid, sid=sid, actor='owner', kind='send', request=request,
                         context={}, turn_id=tid)
    row = runtime.store.claim(sid, 'transport-owner')
    assert row['state'] == 'running'
    runtime._run(row, 'transport-owner')

    assert len(contexts) == 1 and contexts[0].get('runtime.command_id') == cid
    assert len(scope_gateway.calls) == 1
    assert request_scope.current_contract(contexts[0]) is not None
    assert len(produced) == len(thrown) == 1
    if scenario == 'history':
        assert type(thrown[0]) is data_cache.NoHistoricalDataError
        assert backtest_run.NoHistoricalDataError is data_cache.NoHistoricalDataError
        assert thrown[0].reason == reason and thrown[0].retryable is False
    assert thrown[0].replay_phase == 'preflight'
    assert thrown[0].failure_path and thrown[0].run_receipt
    assert_recovery(produced[0], reason, False,
                    'provide_historical_capability' if scenario == 'history' else 'provide_protocol_replay')
    assert 'request_scope' in tool_json_data(produced[0])
    assert any(check['check_id'] == 'request_scope_match'
               for check in produced[0].metadata['verification']['checks'])
    events = runtime.store.events(sid, cid)['events']
    completed = [event for event in events if event['kind'] == 'tool.complete']
    assert len(completed) == 1
    event = completed[0]
    assert event['session_id'] == sid and event['turn_id'] == tid and event['call_id'] == call.id
    assert event['ok'] is False and event['error_kind'] == 'execution_error'
    assert event['result'] is None and event['display_result'] is None
    assert event.get('recovery') == produced[0].error.recovery_hint
    live = [event for event in bus.recent() if event['kind'] == 'tool.complete']
    assert len(live) == 1 and live[0]['recovery'] == event['recovery']
    assert event['verification'] == live[0]['verification']
