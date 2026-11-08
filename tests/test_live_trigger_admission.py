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
