"""Budgets, permission boundaries and generic observation replay contracts."""
from copy import deepcopy
from pathlib import Path
import pytest
from nerya.core import yaml_io
from nerya.core.config import Config, DEFAULT_CONFIG
from nerya.core.paths import WorkspacePaths
from nerya.agent.loop_contracts import LoopConfig
from nerya.api.routes_agent import _with_turn_limit_overrides
from nerya.tools.permissions import PermissionContext, PermissionEngine, PermissionMode, PermissionRequest, PermissionRule, PermissionDecisionKind
from nerya.tools.types import ToolDescriptor, RiskLevel, PermissionScope
from nerya.skills.builtin.backtest.scripts.config import BacktestConfig, BacktestConfigError
from nerya.skills.builtin.backtest.scripts.engine import BacktestResult
from nerya.skills.builtin.backtest.scripts.metrics import assemble_metrics
from nerya.skills.builtin.backtest.scripts.backtest_run import _discover_strategy_timeframes

pytestmark = pytest.mark.smoke

def test_api_defaults_match_chat_and_allow_bounded_authoring(tmp_path):
    cfg = Config(paths=WorkspacePaths(tmp_path), data=deepcopy(DEFAULT_CONFIG))
    loop = LoopConfig.from_config(cfg)
    assert (loop.max_iterations, loop.max_total_tool_calls, loop.max_wall_seconds) == (120, 400, 1800)
    assert loop.max_tokens == 16384
    assert loop.wall_time_final_synthesis_seconds == 30
    assert loop.action_tool_wall_reserve_seconds == 15
    assert cfg.get('runtime.permission_mode') == 'auto'
    assert cfg.live_trading_enabled() is False
    assert cfg.get('runtime.mock_mode') is False

def test_explicit_lower_operator_budgets_are_not_overridden(tmp_path):
    cfg = Config(paths=WorkspacePaths(tmp_path), data=deepcopy(DEFAULT_CONFIG))
    limited = _with_turn_limit_overrides(cfg, {'max_iterations': 5, 'max_total_tool_calls': 8, 'max_wall_seconds': 90})
    loop = LoopConfig.from_config(limited)
    assert (loop.max_iterations, loop.max_total_tool_calls, loop.max_wall_seconds) == (5, 8, 90)
    assert cfg.get('agent.native.max_total_tool_calls') == 400

@pytest.mark.parametrize('risk,expected', [(RiskLevel.READ,'allow'), (RiskLevel.WRITE,'allow'), (RiskLevel.EXEC,'allow'), (RiskLevel.DANGEROUS,'ask')])
def test_autonomy_preserves_dangerous_approval(risk, expected):
    descriptor = ToolDescriptor(name='fixture_action', description='fixture', input_schema={'type':'object'}, handler=lambda call: None, risk=risk, permission_scope=PermissionScope.WORKSPACE)
    request = PermissionRequest(descriptor=descriptor)
    ctx = PermissionContext(mode=PermissionMode.AUTO)
    assert PermissionEngine().evaluate(request, ctx).kind.value == expected
    ctx.deny_rules.append(PermissionRule(tool='fixture_action', decision=PermissionDecisionKind.DENY))
    assert PermissionEngine().evaluate(request, ctx).kind.value == 'deny'

@pytest.mark.parametrize('mapping', [False, True])
def test_card_timeframes_precede_legacy_code_literals(tmp_path, mapping):
    sources = [{'id':'bars', 'capability':'candles', 'timeframe':'15m'}, {'id':'day', 'capability':'candles', 'timeframe':'1d'}]
    yaml_io.dump(tmp_path / 'strategy.yml', {'data_sources': {v['id']:v for v in sources} if mapping else sources})
    (tmp_path / 'main.py').write_text('TIMEFRAME="1h"\n')
    assert _discover_strategy_timeframes(tmp_path) == ['15m','1d','1h']

def replay(statuses, mode='observation', orders=0):
    cfg = BacktestConfig(evaluation_mode=mode)
    return BacktestResult(strategy_id='fixture', strategy_root=None, config=cfg, decisions=[{'status':s} for s in statuses], order_attempts=orders)

@pytest.mark.parametrize('statuses,orders,verdict', [(['ok'],0,'PASS'), (['dispatch'],0,'PASS'), (['hold'],0,'WARN'), ([],0,'WARN'), (['error'],0,'FAIL'), (['dispatch'],1,'FAIL'), (['ok','error'],0,'FAIL')])
def test_observation_replay_is_not_profit_scoring(statuses, orders, verdict):
    metrics = assemble_metrics(replay(statuses, orders=orders))
    assert metrics['verdict'] == verdict
    assert metrics['replay']['agent_execution'] == 'not_run'
    assert metrics['replay']['schedule_execution'] == 'not_run'
    assert metrics['replay']['order_attempts'] == orders
    assert 'observation_replay_not_profit_evidence' in metrics['flags']

def test_typed_manifest_replay_preserves_card_sources(tmp_path):
    from test_strategy_author_workflows import manifest, source
    from nerya.strategies.package import load_package
    from nerya.skills.builtin.backtest.scripts.engine import _views_from_strategy_config
    raw = manifest('macd_agent')
    raw['evaluation'] = {'mode':'observation'}
    folder = tmp_path / 'strategies' / raw['strategy_id']
    yaml_io.dump(folder / 'strategy.yml', raw)
    (folder / 'main.py').write_text(source('macd_agent'))
    package = load_package(WorkspacePaths(tmp_path), raw['strategy_id'])
    assert package.manifest.extras['evaluation']['mode'] == 'observation'
    cfg = BacktestConfig(markets=raw['markets'])
    typed, _ = _views_from_strategy_config(raw['strategy_id'], cfg, package.manifest.asdict())
    plain, _ = _views_from_strategy_config(raw['strategy_id'], cfg, raw)
    assert typed.extras['data_sources'] == plain.extras['data_sources'] == raw['data_sources']
    assert 'extras' not in typed.extras
    assert typed.markets == tuple(raw['markets'])


def test_trading_default_retains_its_profit_gate_and_rejects_returned_errors():
    assert assemble_metrics(replay(['ok'], mode='trading'))['verdict'] == 'FAIL'
    metrics = assemble_metrics(replay(['error'], mode='trading'))
    assert metrics['verdict'] == 'FAIL'
    assert 'strategy_returned_errors' in metrics['flags']
    with pytest.raises(BacktestConfigError, match='evaluation_mode'):
        BacktestConfig.from_raw({'evaluation_mode':'pretend_pass'})
