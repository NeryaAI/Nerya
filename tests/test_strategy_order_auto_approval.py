from __future__ import annotations

from copy import deepcopy

import pytest

from nerya.agent.kernel import _strategy_triggered_order_turn
from nerya.core import jsonl, yaml_io
from nerya.core.config import Config, DEFAULT_CONFIG
from nerya.core.paths import WorkspacePaths
from nerya.tools.native.trading import trade_intent_submit_handler
from nerya.tools.types import ToolCall
from nerya.trading.order_intents import SizingPolicy, TradeEntry, TradePlan
from nerya.trading.submit import _plan_to_intent, submit_trade_intent


pytestmark = pytest.mark.smoke


def _config(tmp_path) -> Config:
    data = deepcopy(DEFAULT_CONFIG)
    data["runtime"]["mock_mode"] = False
    cfg = Config(paths=WorkspacePaths(root=tmp_path), data=data)
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
    yaml_io.dump(
        cfg.paths.strategy("s1") / "strategy.yml",
        {
            "id": "s1",
            "status": "paper",
            "account_id": "paper_main",
            "markets": ["mock:BTC/USDT"],
            "paper_trading_enabled": True,
            "live_trading_enabled": False,
        },
    )
    yaml_io.dump(
        cfg.paths.strategy("s1") / "limits.yml",
        {
            "allowed_markets": ["mock:BTC/USDT"],
            "min_confidence": 0,
            "max_stale_seconds": 60,
            "approval_threshold_usd": 1,
        },
    )
    return cfg


def _intent_spec(source: str = "strategy_runtime") -> dict:
    return {
        "strategy_id": "s1",
        "account_id": "paper_main",
        "market": "mock:BTC/USDT",
        "side": "buy",
        "size": 100,
        "size_unit": "usd",
        "order_type": "market",
        "confidence": 1.0,
        "source": source,
    }


def test_strategy_runtime_threshold_escalation_is_auto_approved(tmp_path):
    cfg = _config(tmp_path)

    out = submit_trade_intent(
        cfg,
        spec=_intent_spec("strategy_runtime"),
        market_snapshot={"price": 50_000, "age_s": 0, "source": "test"},
    )

    assert out["status"] == "filled"
    assert out["approval"]["state"] == "auto_approved"
    assert jsonl.read_all(cfg.paths.approvals_pending) == []
    approved = jsonl.read_all(cfg.paths.approvals_approved)
    assert approved[-1]["auto"] is True
    assert approved[-1]["intent"]["source"] == "strategy_runtime"


def test_manual_agent_order_still_waits_for_approval(tmp_path):
    cfg = _config(tmp_path)

    out = submit_trade_intent(
        cfg,
        spec=_intent_spec("agent:native"),
        market_snapshot={"price": 50_000, "age_s": 0, "source": "test"},
    )

    assert out["status"] == "pending_approval"
    assert out["approval_id"]
    assert jsonl.read_all(cfg.paths.approvals_approved) == []
    pending = jsonl.read_all(cfg.paths.approvals_pending)
    assert pending[-1]["intent"]["source"] == "agent:native"


def test_strategy_agent_native_tool_defaults_auto_approve(tmp_path):
    cfg = _config(tmp_path)
    spec = _intent_spec()
    spec.pop("strategy_id")
    spec.pop("source")
    spec["market_snapshot"] = {"price": 50_000, "age_s": 0, "source": "test"}
    call = ToolCall(name="trade_intent_submit", arguments=spec, id="toolu_test")

    result = trade_intent_submit_handler(
        call,
        config=cfg,
        default_strategy="s1",
        default_source="strategy_agent",
    )

    assert result.is_error is False
    out = result.content[0].data
    assert out["status"] == "filled"
    assert out["approval"]["state"] == "auto_approved"
    assert out["order"]["intent_id"]
    assert jsonl.read_all(cfg.paths.approvals_pending) == []


def test_trade_plan_preserves_strategy_runtime_source():
    plan = TradePlan(
        action="open_position",
        strategy_id="s1",
        account_id="paper_main",
        market="mock:BTC/USDT",
        side="long",
        sizing=SizingPolicy(method="fixed_usd", fixed_usd=100),
        entry=TradeEntry(order_type="market"),
        confidence=1.0,
        source="strategy_runtime",
    )

    intent = _plan_to_intent(plan)

    assert intent.source == "strategy_runtime"


def test_only_strategy_triggered_turns_get_order_permission_bypass():
    assert _strategy_triggered_order_turn(
        "s1",
        {"source": "scheduled_session", "kind": "strategy.tick"},
    )
    assert not _strategy_triggered_order_turn(
        "s1",
        {"source": "dashboard", "kind": "user.chat"},
    )
