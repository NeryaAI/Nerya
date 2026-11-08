from __future__ import annotations

import copy

import pytest

from nerya.core.config import Config, DEFAULT_CONFIG
from nerya.core import yaml_io
from nerya.core.paths import WorkspacePaths
from nerya.trading.live_trigger import admit_live_trigger


pytestmark = pytest.mark.smoke


def _cfg(tmp_path, *, live=False):
    data = copy.deepcopy(DEFAULT_CONFIG)
    data.setdefault("runtime", {})["live_trading_enabled"] = live
    cfg = Config(paths=WorkspacePaths(tmp_path), data=data)
    yaml_io.dump(cfg.paths.accounts_file, {"accounts": [{"id": "live_main", "venue": "fake", "mode": "live",
        "status": "active", "live_trading_enabled": True, "initial_balance_usd": 1000,
        "permissions": {"read_balances": True, "place_order": True}}]})
    yaml_io.dump(cfg.paths.strategy("live_one") / "strategy.yml", {"version": 1, "strategy_id": "live_one",
        "title": "Live one", "description": "fixture", "mode": "live", "entrypoint": "main.py:run",
        "markets": ["FAKE:BTCUSDT"], "accounts": ["live_main"], "schedule": {"type": "none", "enabled": False},
        "policy": {"max_single_order_usd": 100, "max_daily_notional_usd": 1000, "max_open_positions": 1, "min_confidence": 0.5, "max_run_seconds": 10},
        "llm_policy": {"default_tier": "light", "allowed_tiers": ["light"]},
        "agent_session": {}, "agent_profile": {}, "subagents": [], "news_sources": [], "tuning": {"enabled": False}})
    (cfg.paths.strategy("live_one") / "main.py").write_text("def run(ctx): return ctx.result.hold(reason='fixture')\n")
    return cfg


def test_live_trigger_is_blocked_when_runtime_switch_is_off(tmp_path):
    result = admit_live_trigger(_cfg(tmp_path, live=False), "live_one")
    assert result["allowed"] is False and "runtime_live_trading_disabled" in result["blockers"]


def test_live_trigger_requires_real_snapshot_and_reconciliation_clear(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path, live=True)
    from nerya.trading import live_trigger
    monkeypatch.setattr(live_trigger, "fresh_snapshot", lambda *a, **k: type("Snap", (), {"source": "live", "health": "ok", "snapshot_id": "s1"})())
    monkeypatch.setattr(live_trigger.ReconciliationStore, "worst_recent", lambda *a, **k: None)
    result = admit_live_trigger(cfg, "live_one")
    assert result["allowed"] is True and result["approval"] == "trade_intent_approval_gate"


@pytest.mark.parametrize('payload', [{}, {'mode': 'live'}, {'mode_override': 'LIVE'}])
def test_live_tick_without_admission_is_not_executed(tmp_path, monkeypatch, payload):
    from nerya.triggers.runtime import TriggerRuntime, STRATEGY_RUN_TICK_TARGET
    from nerya.triggers.event import TriggerEvent
    from nerya.triggers.router import RouterResult
    from nerya.skills.kernel import SkillKernel
    from nerya.strategies.runner import StrategyRunner
    cfg = _cfg(tmp_path)
    monkeypatch.setattr(SkillKernel, 'boot', lambda *_: None)
    monkeypatch.setattr(StrategyRunner, 'run_tick', lambda *a, **kw: pytest.fail('runner invoked after denial'))
    event = TriggerEvent.new(source='schedule', kind='tick', target=STRATEGY_RUN_TICK_TARGET, strategy_id='live_one', payload=payload)
    route = RouterResult(event.event_id, 'routed', event.target, None, 'live_one')
    out = TriggerRuntime.boot(cfg)._execute_strategy_target(event, route)
    assert out.status == 'blocked' and out.result['trigger_event_id'] == event.event_id
    assert 'runtime_live_trading_disabled' in out.result['admission']['blockers']


def test_payload_cannot_narrow_multiaccount_execution_authority(tmp_path):
    cfg = _cfg(tmp_path, live=True)
    path = cfg.paths.strategy('live_one') / 'strategy.yml'
    manifest = yaml_io.load(path); manifest['accounts'].append('unchecked'); yaml_io.dump(path, manifest)
    result = admit_live_trigger(cfg, 'live_one', {'account_id': 'live_main'})
    assert not result['allowed'] and 'live_trigger_requires_one_declared_account' in result['blockers']


def test_live_agent_task_is_not_queued_with_paper_payload(tmp_path, monkeypatch):
    from types import SimpleNamespace
    from nerya.sdk import task_dispatch
    from nerya.triggers.event import TriggerEvent
    cfg = _cfg(tmp_path)
    event = TriggerEvent.new(source='schedule', kind='tick', target='skill:strategy.agent_task',
                             strategy_id='live_one', payload={'mode': 'paper'})
    monkeypatch.setattr(task_dispatch, 'strategy_spec', lambda *a, **kw: SimpleNamespace(
        strategy_id='live_one', snapshot={'mode': 'live'}))
    monkeypatch.setattr(task_dispatch, 'task_runs', lambda *_: pytest.fail('queued a blocked live task'))
    out = task_dispatch.admit_strategy(cfg, event, SimpleNamespace(route_id=None))
    assert out.status == 'blocked'
    assert 'runtime_live_trading_disabled' in out.result['admission']['blockers']
