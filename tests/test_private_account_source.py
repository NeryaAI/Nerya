from __future__ import annotations

import copy

import pytest

from nerya.agent.task_control import create_mandate, subscribe_task
from nerya.core.config import Config, DEFAULT_CONFIG
from nerya.core.paths import WorkspacePaths
from nerya.triggers.private_account_source import poll


pytestmark = pytest.mark.smoke


def _cfg(tmp_path):
    data = copy.deepcopy(DEFAULT_CONFIG)
    cfg = Config(paths=WorkspacePaths(tmp_path), data=data)
    cfg.paths.accounts_file.parent.mkdir(parents=True, exist_ok=True)
    cfg.paths.accounts_file.write_text("accounts:\n  - id: live_main\n    venue: fake\n    mode: live\n    status: active\n    live_trading_enabled: true\n    permissions:\n      read_balances: true\n")
    return cfg


def test_private_account_source_baseline_then_snapshot_and_order_events(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path)
    create_mandate(cfg.paths, task_id="task", command_id="cmd", session_id="s", actor_id="owner", task_kind="research", objective="observe")
    subscribe_task(cfg.paths, task_id="task", source="private_account", kind="account.snapshot_changed", match={"payload.account_id": "live_main"})
    subscribe_task(cfg.paths, task_id="task", source="private_account", kind="order.updated", match={"payload.account_id": "live_main"})
    class Snapshot:
        snapshot_id = "snap-1"; ts = 100.0; health = "ok"; nav_usd = 1000.0; free_by_asset = {"USDT": 1000.0}; meta = {"positions": [], "open_orders": []}
    monkeypatch.setattr("nerya.triggers.private_account_source.load_account_profiles", lambda paths: {"live_main": type("P", (), {"mode": "live", "permissions": type("Perm", (), {"read_balances": True})()})()})
    snapshots = [Snapshot()]
    monkeypatch.setattr("nerya.triggers.private_account_source.latest_snapshot", lambda *a, **k: snapshots[-1])
    monkeypatch.setattr("nerya.triggers.private_account_source.OrderTracker", lambda paths: type("T", (), {"active_orders": lambda self, account_id=None: [], "cached_orders": lambda self, account_id=None, now=None: [], "close": lambda self: None})())
    emitted = []
    monkeypatch.setattr("nerya.triggers.private_account_source.dispatch_trigger", lambda paths, event, config: emitted.append(event) or {"state": "delivered"})
    first = poll(cfg, now=1000)
    changed = Snapshot(); changed.snapshot_id = "snap-2"; changed.ts = 200.0; snapshots.append(changed)
    second = poll(cfg, now=1100)
    assert first["emitted"] == []
    assert len(emitted) == 1 and emitted[0]["kind"] == "account.snapshot_changed"
    assert len(second["emitted"]) == 1
    assert poll(cfg, now=1200)["emitted"] == []


def test_private_account_source_records_degraded_health_on_failure(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path)
    monkeypatch.setattr("nerya.triggers.private_account_source.load_account_profiles", lambda paths: {"live_main": type("P", (), {"mode": "live", "permissions": type("Perm", (), {"read_balances": True})()})()})
    monkeypatch.setattr("nerya.triggers.private_account_source.latest_snapshot", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("snapshot unavailable")))
    result = poll(cfg, now=1000)
    assert result["ok"] is False and result["errors"]
