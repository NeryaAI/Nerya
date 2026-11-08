"""Generated scaffold behavior only: synthetic candles/verdicts, no historical AI evidence."""
from types import ModuleType, SimpleNamespace as NS
from unittest.mock import create_autospec
import sys

import pytest

from nerya.core import yaml_io
from nerya.core.config import Config
from nerya.core.paths import WorkspacePaths
from nerya.evolution.strategy_code_generator import StrategyCodeGenerator, StrategyGenerationRequest
from nerya.strategies import ResultBuilder, StrategyResult
from nerya.strategies.context import (
    StrategyClock, StrategyConfig, StrategyContext, StrategyPosition,
    StrategyState, StrategySubAgents, StrategyTrading, StrategyTriggerContext,
)
from nerya.workspace.state_store import StateStore

pytestmark = pytest.mark.smoke


@pytest.fixture(autouse=True)
def no_external_effects(monkeypatch):
    def blocked(*args, **kwargs):
        pytest.fail('scaffold tests must not access network or DB')
    monkeypatch.setattr('socket.socket.connect', blocked)
    monkeypatch.setattr('sqlite3.connect', blocked)


@pytest.fixture
def scaffold(tmp_path, monkeypatch):
    request = StrategyGenerationRequest(
        strategy_id='eth_ema_agent_confirm', strategy_class='trend', execution_mode='script',
        markets=('BINANCE:ETHUSDT',), accounts=('paper_main',), timeframe='1h',
        subagents=('market_analyst',), create_tuning=False,
    )
    package = StrategyCodeGenerator(WorkspacePaths(tmp_path)).generate(
        request, validate=False, create_proposal_record=False)
    for name in ('strategy_execution', 'ema_cross'):
        if name + '.py' in package.files:
            helper = ModuleType(name)
            monkeypatch.setitem(sys.modules, name, helper)
            exec(compile(package.files[name + '.py'], name + '.py', 'exec'), helper.__dict__)
    module = ModuleType('scaffold_main')
    exec(compile(package.files['main.py'], 'main.py', 'exec'), module.__dict__)
    manifest = yaml_io.loads(package.files['strategy.yml'])
    return NS(module=module, manifest=manifest, files=package.files)


def sdk_fixture(tmp_path, scaffold, *, last=100., quantity=0., bars=160):
    manifest = scaffold.manifest
    params = dict(manifest['params'])
    rows = [dict(ts=1700006400 + i * 3600, open=price, high=price, low=price,
                 close=price, volume=100.)
            for i, price in enumerate([100.] * (bars - 1) + [last])]
    calls = []
    fixture = NS(rows=rows, calls=calls, quantity=quantity,
                 verdict={'ok': True, 'output': {'decision': 'ENTRY', 'confidence': .8}},
                 receipt={'status': 'submitted', 'order_id': 'synthetic-order'})
    def candles(market, *, timeframe, limit):
        calls.append(('candles', market, timeframe, limit))
        return rows[-limit:]
    def features(market, *, timeframe, lookback):
        return {}
    def positions(market):
        return [StrategyPosition(market=market, size=fixture.quantity)] if fixture.quantity else []
    def dispatch(target, *, payload, trigger_event_id, strategy_id, session_id):
        calls.append(('agent', target, payload))
        return fixture.verdict
    subagents = StrategySubAgents(Config(WorkspacePaths(tmp_path), {}), None, manifest['strategy_id'])
    subagents._dispatcher = NS(dispatch=dispatch)
    trading = create_autospec(StrategyTrading, instance=True)
    trading.open_position.side_effect = lambda **kwargs: fixture.receipt
    trading.close_position.side_effect = lambda **kwargs: fixture.receipt
    trading.execution_status.return_value = {'status': 'unknown', 'terminal': False,
                                            'reconciled': False, 'has_open_orders': None}
    ctx = StrategyContext(
        strategy_id=manifest['strategy_id'], run_id='behavior-only', session_id=None,
        strategy_root=tmp_path,
        config=StrategyConfig(strategy_id=manifest['strategy_id'], markets=manifest['markets'],
                              accounts=manifest['accounts'], extras={**manifest, 'params': params}),
        policy=NS(min_confidence=manifest['policy']['min_confidence']),
        trigger=StrategyTriggerContext(payload={'market': manifest['markets'][0]}), prompt=None,
        market=NS(candles=candles, features=features), news=None, dedupe=None, llm=None,
        subagents=subagents, trading=trading,
        portfolio=NS(positions=positions, position=lambda market: next(iter(positions(market)), None)),
        pnl=None, messages=None, state=StrategyState(StateStore(tmp_path / 'fixture-state.json')),
        clock=StrategyClock(_now_ms_fn=lambda: (rows[-1]['ts'] + 3600) * 1000), audit=None,
        result=ResultBuilder(),
    )
    fixture.ctx = ctx
    return fixture


@pytest.mark.parametrize('last,quantity,bars', [
    (100., 0., 160), (100., 2., 160), (120., 2., 160), (80., 0., 160), (120., 0., 20),
])
def test_no_signal_has_zero_agent_calls(scaffold, tmp_path, last, quantity, bars):
    fixture = sdk_fixture(tmp_path, scaffold, last=last, quantity=quantity, bars=bars)
    result = scaffold.module.run(fixture.ctx)
    assert [call for call in fixture.calls if call[0] == 'agent'] == []
    assert isinstance(result, StrategyResult) and result.status.value == 'hold'
    fixture.ctx.trading.open_position.assert_not_called()
    fixture.ctx.trading.close_position.assert_not_called()


def test_down_cross_exits_settled_long_without_agent(scaffold, tmp_path):
    fixture = sdk_fixture(tmp_path, scaffold, last=80., quantity=2.)
    flags = scaffold.module.ema_cross(fixture.rows, **fixture.ctx.config.params['indicator'])
    assert flags['fast'][-2] >= flags['slow'][-2] and flags['fast'][-1] < flags['slow'][-1]
    result = scaffold.module.run(fixture.ctx)
    assert [call for call in fixture.calls if call[0] == 'agent'] == []
    fixture.ctx.trading.close_position.assert_called_once()
    assert fixture.ctx.trading.close_position.call_args.kwargs['side'] == 'long'
    assert isinstance(result, StrategyResult) and result.status.value == 'submitted'
    assert result.metadata['execution'] == fixture.receipt
    fixture.ctx.trading.open_position.assert_not_called()


@pytest.mark.parametrize('status', ['submitted', 'pending_approval', 'rejected', 'partial', 'failed'])
def test_actual_up_cross_confirms_once_and_preserves_receipt(scaffold, tmp_path, status):
    fixture = sdk_fixture(tmp_path, scaffold, last=120.)
    flags = scaffold.module.ema_cross(fixture.rows, **fixture.ctx.config.params['indicator'])
    assert flags['fast'][-2] <= flags['slow'][-2] and flags['fast'][-1] > flags['slow'][-1]
    fixture.receipt = {'status': status, 'order_id': 'synthetic-order', 'approval_id': 'synthetic-approval'}
    result = scaffold.module.run(fixture.ctx)
    assert isinstance(result, StrategyResult)
    assert result.status.value == ('error' if status == 'failed' else status)
    assert result.metadata['execution'] == fixture.receipt
    assert result.approval_id == 'synthetic-approval'
    fixture.ctx.trading.open_position.assert_called_once_with(
        market='BINANCE:ETHUSDT', side='long', sizing=fixture.ctx.config.params['sizing'],
        protection=fixture.ctx.config.params.get('protection'), confidence=.8,
        reasoning_ref=f"ema_cross:buy:{fixture.rows[-1]['ts']}")
    fixture.ctx.trading.close_position.assert_not_called()
    agent_calls = [c for c in fixture.calls if c[0] == 'agent']
    assert len(agent_calls) == 1 and agent_calls[0][1] == 'subagent:market_analyst'
    payload = agent_calls[0][2]
    assert payload['signals'] == flags and payload['timeframe'] == '1h'
    assert payload['market'] == 'BINANCE:ETHUSDT'
    assert [row['ts'] for row in payload['candles']] == [row['ts'] for row in fixture.rows[-60:]]
    assert all((row['ts'] + 3600) * 1000 <= fixture.ctx.clock.now_ms()
               for row in payload['candles'])
    # Pending/unknown execution blocks duplicate submissions and confirmations.
    scaffold.module.run(fixture.ctx)
    assert len([c for c in fixture.calls if c[0] == 'agent']) == 1
    fixture.ctx.trading.open_position.assert_called_once()


@pytest.mark.parametrize('envelope,expected', [
    ({'ok': True, 'output': {'decision': 'HOLD', 'confidence': .8}}, 'hold'),
    ({'ok': True, 'output': {'decision': 'ENTRY', 'confidence': .5}}, 'hold'),
    ({'ok': True, 'output': {'decision': 'ENTRY', 'confidence': True}}, 'error'),
    ({'ok': True, 'output': {'decision': 'ENTRY', 'confidence': float('nan')}}, 'error'),
    ({'ok': True, 'output': {'decision': 'ENTRY', 'confidence': 1.1}}, 'error'),
    ({'ok': True, 'output': {'decision': 'SELL', 'confidence': .8}}, 'error'),
    ({'ok': True}, 'error'), ({'ok': False, 'output': {}}, 'error'),
])
def test_confirmation_veto_or_failure_never_enters(scaffold, tmp_path, envelope, expected):
    fixture = sdk_fixture(tmp_path, scaffold, last=120.)
    fixture.verdict = envelope
    assert scaffold.module.run(fixture.ctx).status.value == expected
    fixture.ctx.trading.open_position.assert_not_called()
    fixture.ctx.trading.close_position.assert_not_called()
    fixture.verdict = {'ok': True, 'output': {'decision': 'ENTRY', 'confidence': .8}}
    retried = scaffold.module.run(fixture.ctx)
    if expected == 'hold':
        assert retried.reason == 'duplicate_closed_candles'
        assert len([c for c in fixture.calls if c[0] == 'agent']) == 1
    else:
        assert retried.status.value == 'submitted'
        assert len([c for c in fixture.calls if c[0] == 'agent']) == 2


def test_open_candle_and_foreign_market_cannot_awaken_agent(scaffold, tmp_path):
    fixture = sdk_fixture(tmp_path, scaffold, last=120.)
    fixture.ctx.trigger = StrategyTriggerContext(payload={'market': 'OTHER:ETHUSDT'})
    assert scaffold.module.run(fixture.ctx).status.value == 'error'
    assert fixture.calls == []
    fixture.ctx.trigger = StrategyTriggerContext(payload={'market': 'BINANCE:ETHUSDT', 'timeframe': '5m'})
    fixture.ctx.clock = StrategyClock(_now_ms_fn=lambda: fixture.rows[-1]['ts'] * 1000 + 60000)
    assert scaffold.module.run(fixture.ctx).status.value == 'hold'
    assert len([c for c in fixture.calls if c[0] == 'agent']) == 0
    assert fixture.calls == [('candles', 'BINANCE:ETHUSDT', '1h', 160)]
    fixture.ctx.clock = StrategyClock(_now_ms_fn=lambda: (fixture.rows[-1]['ts'] + 3600) * 1000)
    assert scaffold.module.run(fixture.ctx).status.value == 'submitted'


def test_scaffold_saves_complete_closed_gate_config(scaffold):
    from nerya.strategies.validator import validate_proposal_files
    manifest = scaffold.manifest
    assert manifest['execution_mode'] == 'script' and manifest['mode'] == 'paper'
    assert manifest['agent_task']['enabled'] is False
    assert manifest['schedule']['enabled'] is False and manifest['tuning']['enabled'] is False
    assert manifest['timeframe'] == '1h'
    assert manifest['params']['confirmation_agent'] == 'market_analyst'
    assert {'main.py', 'ema_cross.py', 'strategy_execution.py'} <= scaffold.files.keys()
    assert 'decision ENTRY or HOLD' in scaffold.files['subagents/market_analyst.agent.md']
    checked = validate_proposal_files(strategy_id=manifest['strategy_id'], files=scaffold.files,
                                      smoke_test=False, run_tests=False)
    assert checked.ok, checked.asdict()


def test_authored_config_and_custom_entrypoint_are_preserved(tmp_path):
    generator = StrategyCodeGenerator(WorkspacePaths(tmp_path))
    saved = {'timeframe': '4h', 'params': {
        'indicator': {'fast_period': 7, 'slow_period': 21}, 'lookback': 240,
        'sizing': {'method': 'pct_nav', 'pct_nav': .17},
        'protection': {'stop_loss': {'type': 'pct', 'value': .03}},
        'confirmation_min_confidence': .91}, 'policy': {'max_single_order_usd': 40}}
    request = StrategyGenerationRequest(strategy_id='authored_settings', execution_mode='script',
        strategy_class='trend', markets=('BINANCE:ETHUSDT',), accounts=('paper_main',),
        subagents=('risk_reviewer',), files={'strategy.yml': yaml_io.dumps(saved)})
    generated = generator.generate(request, validate=False, create_proposal_record=False).files
    manifest = yaml_io.loads(generated['strategy.yml'])
    assert manifest['timeframe'] == '4h'
    assert manifest['policy']['max_single_order_usd'] == 40
    assert all(manifest['params'][key] == value for key, value in saved['params'].items())
    assert manifest['params']['confirmation_agent'] == 'risk_reviewer'
    assert manifest['schedule']['enabled'] is manifest['tuning']['schedule']['enabled'] is False
    request.files['main.py'] = '''def run(ctx):
    return ctx.result.hold(reason="custom")
'''
    custom = generator.generate(request, validate=False, create_proposal_record=False).files
    assert custom['main.py'] == request.files['main.py']
    assert 'strategy_execution.py' not in custom and 'ema_cross.py' not in custom
    assert 'confirmation_agent' not in yaml_io.loads(custom['strategy.yml'])['params']


def _candidate_contract(scaffold, monkeypatch):
    monkeypatch.setitem(sys.modules, 'main', scaffold.module)
    namespace = {}
    source = scaffold.files['tests/test_contract.py']
    exec(compile(source, 'tests/test_contract.py', 'exec'), namespace)
    names = {name for name in namespace if name.startswith('test_')}
    assert names == {
        'test_no_signal_zero_agent_calls', 'test_confirmed_entry', 'test_entry_veto',
        'test_deterministic_exit', 'test_pending_no_resubmit', 'test_confirmation_failure_retry',
    }
    return namespace


def test_generated_candidate_contract_executes_actual_signal_branches(scaffold, monkeypatch):
    namespace = _candidate_contract(scaffold, monkeypatch)
    for name in sorted(namespace):
        if name.startswith('test_'):
            namespace[name]()


@pytest.mark.parametrize('defect,test_name', [
    ('unconditional_agent', 'test_no_signal_zero_agent_calls'),
    ('ignore_veto', 'test_entry_veto'),
    ('agent_on_exit', 'test_deterministic_exit'),
    ('clear_pending', 'test_pending_no_resubmit'),
])
def test_generated_contract_catches_behavior_regressions(scaffold, monkeypatch, defect, test_name):
    namespace = _candidate_contract(scaffold, monkeypatch)
    original = scaffold.module.run
    def broken(ctx):
        if defect in {'unconditional_agent', 'agent_on_exit'}:
            ctx.subagents.run(ctx.config.params['confirmation_agent'], payload={
                'market': 'MOCK:PAIR', 'timeframe': '1h', 'candles': []})
        elif defect == 'ignore_veto':
            ctx.verdict = {'ok': True, 'output': {'decision': 'ENTRY', 'confidence': .8}}
        elif defect == 'clear_pending':
            ctx.saved.clear()
        return original(ctx)
    namespace['run'] = broken
    with pytest.raises(AssertionError):
        namespace[test_name]()


def test_generated_contract_is_collected_and_runs_as_candidate_pytest(scaffold, tmp_path):
    import os
    from pathlib import Path
    import subprocess
    for name in ('main.py', 'ema_cross.py', 'strategy_execution.py', 'tests/test_contract.py'):
        destination = tmp_path / name
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(scaffold.files[name], encoding='utf-8')
    config = tmp_path / 'pytest.ini'
    config.write_text('[pytest]\n')
    # Only trusted generated assets execute here, not a running/live candidate.
    # The child blocks network/DB and does not inherit repository pytest options.
    code = '''import socket, sqlite3, sys
def denied(*args, **kwargs):
    raise AssertionError("candidate contract must not access network or DB")
socket.socket.connect = socket.socket.connect_ex = denied
socket.create_connection = sqlite3.connect = denied
import pytest
raise SystemExit(pytest.main(["-c", "pytest.ini", "-o", "addopts=", "-p", "no:cacheprovider",
                             "--confcutdir=.", "-q", "tests/test_contract.py"]))
'''
    environment = dict(os.environ, PYTHONPATH=str(Path(__file__).parents[1]),
                       PYTEST_DISABLE_PLUGIN_AUTOLOAD='1', PYTEST_ADDOPTS='', PYTHONDONTWRITEBYTECODE='1')
    completed = subprocess.run([sys.executable, '-B', '-c', code], cwd=tmp_path,
                               env=environment, text=True, capture_output=True, timeout=20)
    assert completed.returncode == 0, completed.stdout + completed.stderr
    assert '6 passed' in completed.stdout, completed.stdout


def test_explicit_candidate_tests_are_preserved(tmp_path):
    authored = 'def test_custom_contract():\n    assert True\n'
    request = StrategyGenerationRequest(strategy_id='custom_contract', strategy_class='trend',
        execution_mode='script', markets=('MOCK:PAIR',), accounts=('paper_fixture',),
        subagents=('analyst',), files={'tests/test_contract.py': authored,
                                      'tests/test_other.py': '# keep user test file\n'})
    files = StrategyCodeGenerator(WorkspacePaths(tmp_path)).generate(
        request, validate=False, create_proposal_record=False).files
    assert files['tests/test_contract.py'] == authored
    assert files['tests/test_other.py'] == request.files['tests/test_other.py']
