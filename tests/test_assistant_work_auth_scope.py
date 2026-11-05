"""Delegation inherits only frozen, admitted parent authority (no network)."""
from copy import deepcopy
import json
import time
from types import SimpleNamespace

import pytest

from nerya.agent.assistant_work import AssistantWork
from nerya.agent.assistants import AssistantError, AssistantStore
from nerya.agent.command_runtime import CommandRuntime
from nerya.agent.kernel import AgentTurnResult
from nerya.api import routes_agent
from nerya.core.config import Config, DEFAULT_CONFIG
from nerya.core.paths import WorkspacePaths
from nerya.tools.capability_policy import tool_policy_allows
from nerya.tools.native.financial import register_financial_tools
from nerya.tools.registry import ToolRegistry
from nerya.tools.result_contracts import tool_json_data
from nerya.tools.types import ToolCall


pytestmark = pytest.mark.smoke


@pytest.fixture
def env(tmp_path, monkeypatch):
    cfg = Config(WorkspacePaths(tmp_path), deepcopy(DEFAULT_CONFIG))
    observed = {}

    class HandlerKernel:
        def __init__(self, *, config, **kwargs):
            self.config = config

        def run_turn(self, *, session_id, turn_id, **kwargs):
            assert self.config.get('runtime.command_session_id') == session_id
            assert self.config.get('runtime.command_turn_id') == turn_id
            assert self.config.get('runtime.command_id')
            registry = ToolRegistry()
            register_financial_tools(registry, SimpleNamespace(config=self.config))
            result = registry.get('financial_components').handler(ToolCall(
                name='financial_components', turn_id=turn_id,
                arguments={'mode': 'backtest', 'protocol': 'uniswap'},
            ))
            observed[session_id] = (self.config, result, registry)
            if 'probe' in observed:
                observed['probe'](self.config, registry)
            return AgentTurnResult(
                trigger_event_id=None, strategy_id=None, session_id=session_id,
                turn_id=turn_id, decision={}, final_text=result.text(), stopped_reason='end_turn',
            )

    monkeypatch.setattr(routes_agent, 'AgentKernel', HandlerKernel)
    handler = next(h for m, p, h in routes_agent.routes() if p == '/agent/run_turn_internal')
    manager = CommandRuntime(cfg, lambda config, request: handler(SimpleNamespace(config=config, skills=None), request))
    monkeypatch.setattr(manager, 'kick', lambda sid: None)
    assistants = AssistantStore(cfg.paths)
    sid = assistants.home('main')['session_id']
    return SimpleNamespace(cfg=cfg, manager=manager, assistants=assistants, sid=sid,
                           work=AssistantWork(cfg.paths), observed=observed)


def parent(env, scopes, *, actor='owner', nested=None):
    receipt = env.manager.submit({
        'session_id': env.sid, 'command_id': 'parent-command',
        '_auth_actor_id': actor, '_auth_scope': 'execute:strategy', '_auth_scopes': scopes,
        'request': {'payload': {'text': 'Inspect LP backtest components'}, **(nested or {})},
    }, start=False)['command']
    row = env.manager.store.claim(env.sid, 'parent-worker')
    assert row and row['command_id'] == receipt['command_id']
    cfg = Config(env.cfg.paths, deepcopy(env.cfg.data))
    cfg.data['runtime'].update(command_id=row['command_id'], command_session_id=env.sid,
                               command_turn_id=row['turn_id'], financial_actor_id=actor,
                               financial_actor_scopes=scopes)
    call = ToolCall(id='delegate', name='assistant_task_create', turn_id=row['turn_id'],
                    arguments={'title': 'LP inventory', 'prompt': 'Inspect LP backtest components', 'kind': 'strategy'})
    return cfg, call


def create(env, cfg, call, actor='owner'):
    return env.work.create(cfg, call, env.sid, actor, manager=env.manager)


def request_for(env, receipt):
    with env.manager.store.transaction() as con:
        row = con.execute('SELECT request_json FROM agent_commands WHERE command_id=?',
                          (receipt['command_id'],)).fetchone()
    return json.loads(row[0])


def run_child(env, receipt):
    row = env.manager.store.claim(receipt['task_session_id'], 'child-worker')
    assert row
    env.manager._run(row, 'child-worker')
    assert env.manager.store.snapshot(receipt['task_session_id'])['commands'][0]['state'] == 'succeeded'
    return env.observed[receipt['task_session_id']]


@pytest.mark.parametrize('scope', ['api:all', 'read:funds', 'execute:strategy', 'trade:paper'])
def test_real_admission_and_route_reach_financial_handler_with_inherited_scope(env, scope):
    cfg, call = parent(env, [scope])
    receipt = create(env, cfg, call)
    execution, result, registry = run_child(env, receipt)
    assert not result.is_error, result.text()
    assert execution.get('runtime.financial_actor_scopes') == [scope]
    assert execution.get('runtime.financial_actor_id') == 'owner'
    assert request_for(env, receipt)['_auth_scope'] == 'execute:strategy'
    data = tool_json_data(result)
    assert data['inventory_scope'] == 'paper_backtest'
    assert data['financial_execution_allowed'] is False
    assert any(c['id'] == 'builtin:uniswap' for c in data['components'])
    if scope in {'execute:strategy', 'trade:paper'}:
        live = registry.get('financial_components').handler(ToolCall(
            name='financial_components', arguments={'mode': 'live', '_auth_scopes': ['api:all']},
        ))
        assert live.is_error and 'financial_scope_denied' in live.text()


@pytest.mark.parametrize('scopes', [[], ['read:config']])
def test_nonallowed_and_forged_argument_scopes_stay_denied(env, scopes):
    cfg, call = parent(env, scopes, nested={'_auth_scopes': ['api:all']})
    cfg.data['runtime']['financial_actor_scopes'] = ['api:all']
    call.arguments.update(_auth_actor_id='admin:password', _auth_scope='api:all',
                          _auth_scopes=['api:all'], scopes=['api:all'],
                          request={'_auth_scopes': ['api:all']})
    receipt = create(env, cfg, call)
    execution, result, _ = run_child(env, receipt)
    assert result.is_error and 'financial_scope_denied' in result.text()
    assert execution.get('runtime.financial_actor_scopes') == scopes
    assert request_for(env, receipt)['_auth_actor_id'] == 'owner'


@pytest.mark.parametrize('mismatch', ['owner', 'session', 'turn', 'command', 'inactive', 'request_actor', 'runtime_actor'])
def test_mismatched_parent_cannot_delegate_authority(env, mismatch):
    cfg, call = parent(env, ['api:all'])
    actor = 'intruder' if mismatch == 'owner' else 'owner'
    if mismatch in {'session', 'turn', 'command'}:
        cfg.data['runtime'][{'session': 'command_session_id', 'turn': 'command_turn_id', 'command': 'command_id'}[mismatch]] = 'other'
    if mismatch == 'runtime_actor':
        cfg.data['runtime']['financial_actor_id'] = 'intruder'
    if mismatch in {'inactive', 'request_actor'}:
        with env.manager.store.transaction() as con:
            if mismatch == 'inactive':
                con.execute("UPDATE agent_commands SET state='succeeded' WHERE command_id='parent-command'")
            else:
                request = json.loads(con.execute("SELECT request_json FROM agent_commands WHERE command_id='parent-command'").fetchone()[0])
                request['_auth_actor_id'] = 'intruder'
                con.execute("UPDATE agent_commands SET request_json=? WHERE command_id='parent-command'", (json.dumps(request),))
    with pytest.raises(AssistantError, match='assistant_task_(parent|owner)'):
        create(env, cfg, call, actor)
    assert not env.work.tasks(sid=env.sid)


def test_local_owner_alias_preserves_raw_authenticated_identity(env):
    cfg, call = parent(env, ['read:funds'], actor='admin:password')
    receipt = create(env, cfg, call, actor='default')
    assert create(env, cfg, call, actor='default')['command_id'] == receipt['command_id']
    assert len(env.manager.store.snapshot(receipt['task_session_id'])['commands']) == 1
    execution, result, _ = run_child(env, receipt)
    assert not result.is_error, result.text()
    assert execution.get('runtime.financial_actor_id') == 'admin:password'
    with env.manager.store.transaction() as con:
        assert con.execute('SELECT actor_id FROM agent_commands WHERE command_id=?',
                           (receipt['command_id'],)).fetchone()[0] == 'admin:password'


def test_parentless_helper_does_not_inherit_ambient_or_argument_scopes(env):
    env.cfg.data['runtime'].update(financial_actor_id='owner', financial_actor_scopes=['api:all'])
    call = ToolCall(id='helper', name='assistant_task_create', turn_id='no-parent',
                    arguments={'title': 'Helper', 'prompt': 'Inspect inventory', '_auth_scopes': ['api:all']})
    receipt = create(env, env.cfg, call)
    _, result, _ = run_child(env, receipt)
    assert request_for(env, receipt)['_auth_scopes'] == []
    assert result.is_error and 'financial_scope_denied' in result.text()


@pytest.mark.parametrize('initial,changed', [(['read:funds'], []), ([], ['api:all'])])
@pytest.mark.parametrize('recover', [False, True])
def test_retry_and_recovery_keep_frozen_rights(env, monkeypatch, initial, changed, recover):
    cfg, call = parent(env, initial)
    if recover:
        with monkeypatch.context() as patch:
            def crash(*args, **kwargs):
                raise RuntimeError('crash before admission')
            patch.setattr(env.manager, 'submit', crash)
            with pytest.raises(RuntimeError, match='crash before admission'):
                create(env, cfg, call)
        receipt = env.work.tasks(sid=env.sid)[0]
    else:
        receipt = create(env, cfg, call)
    with env.manager.store.transaction() as con:
        request = json.loads(con.execute("SELECT request_json FROM agent_commands WHERE command_id='parent-command'").fetchone()[0])
        request['_auth_scopes'] = changed
        con.execute("UPDATE agent_commands SET request_json=?,state='succeeded' WHERE command_id='parent-command'", (json.dumps(request),))
    cfg.data['runtime']['financial_actor_scopes'] = changed
    env.work = AssistantWork(env.cfg.paths)
    if recover:
        env.work.recover(env.manager)
    else:
        assert create(env, cfg, call)['command_id'] == receipt['command_id']
    assert request_for(env, receipt)['_auth_scopes'] == initial
    assert len(env.manager.store.snapshot(receipt['task_session_id'])['commands']) == 1
    _, result, _ = run_child(env, receipt)
    assert result.is_error == (not initial)


def test_legacy_recovery_does_not_backfill_parent_permissions(env, monkeypatch):
    cfg, call = parent(env, ['api:all'])
    with monkeypatch.context() as patch:
        patch.setattr(env.manager, 'submit', lambda *a, **kw: (_ for _ in ()).throw(RuntimeError('crash')))
        with pytest.raises(RuntimeError, match='crash'):
            create(env, cfg, call)
    receipt = env.work.tasks(sid=env.sid)[0]
    with env.manager.store.transaction() as con:
        request = json.loads(con.execute('SELECT request_json FROM assistant_work').fetchone()[0])
        for key in ('_auth_actor_id', '_auth_scope', '_auth_scopes'):
            request.pop(key, None)
        con.execute('UPDATE assistant_work SET request_json=?', (json.dumps(request),))
    env.work.recover(env.manager)
    _, result, _ = run_child(env, receipt)
    assert result.is_error and 'financial_scope_denied' in result.text()
    assert request_for(env, receipt)['_auth_scopes'] == []


def test_inherited_scope_does_not_widen_assistant_tool_policy(env):
    profile = env.assistants.get('main')
    env.assistants.save({'tool_policy': {'deny': ['financial_*']}}, aid='main', expected_revision=profile['revision'])
    cfg, call = parent(env, ['api:all'])
    receipt = create(env, cfg, call)
    execution, _, _ = run_child(env, receipt)
    assert not tool_policy_allows(execution.get('agent.native.tool_policy'), 'financial_components')


@pytest.mark.parametrize('live,risk,expected', [
    (False, 'normal', 'live_trading_disabled'),
    (True, 'normal', 'approval'),
    (True, 'freeze_all', 'trading_risk_mode_blocks_action'),
])
def test_inherited_api_all_cannot_approve_sign_or_execute_funds(env, monkeypatch, live, risk, expected):
    from nerya.financial.gateway import FinancialGateway

    class UnsignedAdapter:
        def quote(self, request):
            return {'risk_usd': '1', 'spend_usd': '1', 'fee_usd': '0',
                    'asset_amounts': {'USDC': '1'}, 'available_asset_amounts': {'USDC': '10'},
                    'expires_at': time.time() + 60}

        def validate(self, *args):
            pass

        def execute(self, *args):
            pytest.fail('Inherited scopes must never reach signing or submission')

    monkeypatch.setattr(FinancialGateway, 'adapter', lambda *a, **kw: UnsignedAdapter())
    env.cfg.data['financial'] = {'enabled': True}
    env.cfg.data['runtime']['live_trading_enabled'] = live
    env.cfg.data.setdefault('trading', {})['risk_mode'] = risk

    def probe(config, registry):
        assert config.get('runtime.financial_actor_scopes') == ['api:all']
        assert not any('approve' in descriptor.name or 'grant' in descriptor.name
                       for descriptor in registry)
        prepared = registry.get('financial_prepare').handler(ToolCall(
            id='prepare-test', name='financial_prepare', arguments={'request': {
                'kind': 'exchange_transfer', 'account_id': 'test-account', 'asset': 'USDC',
                'amount': '1', 'from_account': 'spot', 'to_account': 'funding',
            }},
        ))
        assert not prepared.is_error, prepared.text()
        action = tool_json_data(prepared)
        result = registry.get('financial_execute').handler(ToolCall(
            name='financial_execute', arguments={'action_id': action['action_id'],
                                                 'quote_hash': action['quote_hash'], 'approved': True},
        ))
        assert result.is_error and expected in result.text(), result.text()
        with env.manager.store.transaction() as con:
            assert con.execute('SELECT COUNT(*) FROM financial_grants').fetchone()[0] == 0
            assert con.execute("SELECT COUNT(*) FROM approvals WHERE state='approved'").fetchone()[0] == 0
            assert con.execute('SELECT COUNT(*) FROM financial_usage').fetchone()[0] == 0

    env.observed['probe'] = probe
    cfg, call = parent(env, ['api:all'])
    run_child(env, create(env, cfg, call))
