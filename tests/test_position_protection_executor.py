from __future__ import annotations

from copy import deepcopy
import time

import pytest

from nerya.core import yaml_io
from nerya.core.config import Config, DEFAULT_CONFIG
from nerya.core.paths import WorkspacePaths
from nerya.trading.executors.market_order import MarketOrderExecutor
from nerya.trading.executors.orchestrator import ExecutorOrchestrator
from nerya.trading.executors.position_protection import PositionProtectionExecutor
from nerya.trading.order_intents import ProtectionRule, StopLossSpec
from nerya.trading.position_book import PositionBook
from nerya.trading.protection_store import ProtectionStore


pytestmark = pytest.mark.smoke


def _config(tmp_path) -> Config:
    data = deepcopy(DEFAULT_CONFIG)
    data["runtime"]["mock_mode"] = False
    cfg = Config(paths=WorkspacePaths(root=tmp_path), data=data)
    yaml_io.dump(tmp_path / "nerya.yml", data)
    yaml_io.dump(
        cfg.paths.accounts_file,
        {
            "accounts": [
                {
                    "id": "paper_main",
                    "exchange": "mock",
                    "venue": "mock",
                    "mode": "paper",
                    "status": "active",
                    "initial_balance_usd": 10_000,
                    "permissions": {
                        "read_balances": True,
                        "place_order": True,
                        "cancel_order": True,
                    },
                }
            ]
        },
    )
    return cfg


def _protection(cfg: Config):
    position = PositionBook(cfg.paths).apply_fill(
        account_id="paper_main",
        strategy_id="protect_s1",
        market="mock:BTC/USDT",
        side="buy",
        price=100.0,
        size_base=1.0,
        source="paper",
    )
    rule = ProtectionRule(
        position_id=position.position_id,
        strategy_id="protect_s1",
        account_id="paper_main",
        market="mock:BTC/USDT",
        side="long",
        stop_loss=StopLossSpec(type="price", value=95.0),
        status="armed",
    )
    ProtectionStore(cfg.paths).upsert(rule)
    orchestrator = ExecutorOrchestrator(cfg)
    executor = orchestrator.create_position_protection(
        rule=rule,
        position_id=position.position_id,
    )
    return orchestrator, executor, rule


def test_triggered_protection_persists_child_until_fill(tmp_path, monkeypatch):
    cfg = _config(tmp_path)
    orchestrator, executor, rule = _protection(cfg)
    monkeypatch.setattr(
        PositionProtectionExecutor,
        "_live_mark_price",
        lambda _self, _rule: 90.0,
    )

    def leave_child_open(self):
        self.transition("submitted")
        return False

    monkeypatch.setattr(MarketOrderExecutor, "step", leave_child_open)

    terminal = orchestrator.step_executor(executor)

    assert terminal is False
    assert executor.run.state == "working"
    child_id = executor.run.result_json["flatten_executor_id"]
    child = orchestrator.get(child_id)
    assert child is not None
    assert child.state == "submitted"
    assert child.position_id == executor.run.position_id
    assert ProtectionStore(cfg.paths).get(rule.protection_id).status == "armed"

    child.state = "done"
    child.close_type = "filled"
    child.updated_at = time.time()
    child.terminal_at = child.updated_at
    orchestrator._persist(child)

    terminal = orchestrator.step_executor(executor)

    assert terminal is True
    assert executor.run.state == "done"
    assert executor.run.close_type == "stop_loss"
    stored = ProtectionStore(cfg.paths).get(rule.protection_id)
    assert stored is not None
    assert stored.status == "triggered"
    assert stored.triggered_kind == "stop_loss"
    orchestrator.close()


def test_failed_protection_child_is_visible_and_rule_fails(tmp_path, monkeypatch):
    cfg = _config(tmp_path)
    orchestrator, executor, rule = _protection(cfg)
    monkeypatch.setattr(
        PositionProtectionExecutor,
        "_live_mark_price",
        lambda _self, _rule: 90.0,
    )

    def fail_child(self):
        self.store_result({"reason": "venue_rejected"})
        self.transition("failed", close_type="failed")
        return True

    monkeypatch.setattr(MarketOrderExecutor, "step", fail_child)

    terminal = orchestrator.step_executor(executor)

    assert terminal is True
    assert executor.run.state == "failed"
    child_id = executor.run.result_json["flatten_executor_id"]
    child = orchestrator.get(child_id)
    assert child is not None
    assert child.state == "failed"
    stored = ProtectionStore(cfg.paths).get(rule.protection_id)
    assert stored is not None
    assert stored.status == "failed"
    assert executor.run.result_json["flatten_result"]["reason"] == "venue_rejected"
    orchestrator.close()
