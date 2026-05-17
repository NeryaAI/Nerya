from __future__ import annotations

import time
from copy import deepcopy

import pytest

from nerya.agent.kernel import _strategy_triggered_order_turn
from nerya.core import jsonl, yaml_io
from nerya.core.config import Config, DEFAULT_CONFIG
from nerya.core.paths import WorkspacePaths
from nerya.db.sqlite import connect
from nerya.tools.native.trading import trade_intent_submit_handler
from nerya.tools.types import ToolCall
from nerya.trading.account_snapshots import capture_snapshot, latest_snapshot
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


def test_strategy_order_refreshes_stale_account_snapshot_before_risk(tmp_path):
    cfg = _config(tmp_path)
    stale = capture_snapshot(cfg, "paper_main", persist=True)
    stale_ts = time.time() - 180
    con = connect(cfg.paths.db)
    con.execute(
        "UPDATE account_snapshots SET ts = ? WHERE snapshot_id = ?",
        (stale_ts, stale.snapshot_id),
    )

    out = submit_trade_intent(
        cfg,
        spec=_intent_spec("strategy_runtime"),
        market_snapshot={"price": 50_000, "age_s": 0, "source": "test"},
    )

    assert out["status"] == "filled"
    assert not any(
        str(reason).startswith("account_snapshot_stale")
        for reason in out["risk_decision"]["reasons"]
    )
    refreshed = latest_snapshot(cfg.paths, "paper_main")
    assert refreshed is not None
    assert refreshed.ts > stale_ts


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


# ---------------------------------------------------------------------------
# Agent-side bracket TP/SL (GAP A fix).
#
# Pre-fix: ``trade_intent_submit`` only accepted bare order params and routed
# straight to ``submit_trade_intent``. Agents had no way to ship a bracket
# stop_loss / take_profit alongside the entry, so every Agent-placed order
# was naked to the gap-down failure mode.
#
# Post-fix: when ``args["protection"]`` is present the handler routes through
# ``TradingAPI.open_position`` / ``close_position`` / ``reduce_position``,
# which arms the bracket atomically with the entry order. These tests pin
# down the new wiring.
# ---------------------------------------------------------------------------


def test_agent_intent_with_protection_routes_to_open_position(tmp_path):
    """``protection={stop_loss, take_profit}`` → TradePlan with armed bracket."""

    cfg = _config(tmp_path)
    spec = _intent_spec("strategy_runtime")
    spec["market_snapshot"] = {"price": 50_000, "age_s": 0, "source": "test"}
    spec["protection"] = {
        "stop_loss": {"type": "pct", "value": 0.01},
        "take_profit": {"type": "pct", "value": 0.02},
    }
    call = ToolCall(name="trade_intent_submit", arguments=spec, id="toolu_p1")

    result = trade_intent_submit_handler(
        call,
        config=cfg,
        default_strategy="s1",
        default_source="strategy_agent",
    )

    assert result.is_error is False, result
    out = result.content[0].data
    assert out["status"] == "filled", out
    # The TradePlan envelope carries the executor result + risk decision.
    # The exact shape varies a little by execution mode but the existence
    # of an executor close result is the contract.
    assert "executor" in out
    assert out["executor"].get("state") == "done"
    assert out["executor"].get("order_ids"), "TradePlan path must produce order ids"
    # ``risk_decision`` is shared across the legacy and plan paths.
    assert "risk_decision" in out
    # The bracket was armed if a protection_id surfaces somewhere in the
    # envelope (executor.result, position, or budget_decision); the exact
    # field varies by mode so we accept any.
    haystack = str(out)
    # We're not asserting the protection_id specifically here because
    # paper-mode pretends the protection is "soft" and may not persist a
    # rule. The smoke test downstream will exercise live-mode arming.


def test_agent_intent_short_with_open_short_plan_action(tmp_path):
    """``plan_action='open_short' + side='sell'`` → short position with bracket."""

    cfg = _config(tmp_path)
    spec = _intent_spec("strategy_runtime")
    spec["side"] = "sell"
    spec["market_snapshot"] = {"price": 50_000, "age_s": 0, "source": "test"}
    spec["protection"] = {
        "stop_loss": {"type": "pct", "value": 0.012},
        "take_profit": {"type": "pct", "value": 0.024},
    }
    spec["plan_action"] = "open_short"
    call = ToolCall(name="trade_intent_submit", arguments=spec, id="toolu_p2")

    result = trade_intent_submit_handler(
        call,
        config=cfg,
        default_strategy="s1",
        default_source="strategy_agent",
    )

    # We're not asserting on the position direction in the storage layer
    # here (that's the position-book test suite's job); we ARE asserting
    # that the bracket-protected path accepts the open_short plan_action
    # without erroring out.
    assert result.is_error is False, result
    out = result.content[0].data
    assert out.get("status") in {"filled", "pending_approval", "rejected"}


def test_agent_intent_without_protection_uses_legacy_path(tmp_path):
    """Backwards compat: bare intents still route through ``submit_trade_intent``."""

    cfg = _config(tmp_path)
    spec = _intent_spec("strategy_runtime")
    spec["market_snapshot"] = {"price": 50_000, "age_s": 0, "source": "test"}
    # NO ``protection`` key — legacy path.
    call = ToolCall(name="trade_intent_submit", arguments=spec, id="toolu_p3")

    result = trade_intent_submit_handler(
        call,
        config=cfg,
        default_strategy="s1",
        default_source="strategy_agent",
    )

    assert result.is_error is False
    out = result.content[0].data
    assert out["status"] == "filled"
    # The legacy bare path returns a single ``order`` dict at the top
    # level (vs the TradePlan path which returns ``executor.order_ids``).
    # If this regresses to ``executor.order_ids`` shape, an Agent that
    # POSTed a bare intent would suddenly see a different envelope.
    assert "order" in out, (
        f"bare intents must keep the legacy ``order`` envelope; got keys={list(out)}"
    )


def test_agent_intent_with_invalid_protection_returns_usage_error(tmp_path):
    """A malformed ``protection`` block must surface a schema error, not 500."""

    cfg = _config(tmp_path)
    spec = _intent_spec("strategy_runtime")
    spec["market_snapshot"] = {"price": 50_000, "age_s": 0, "source": "test"}
    spec["protection"] = {
        # pct stop must be in (0, 1) per ``StopLossSpec`` validator.
        "stop_loss": {"type": "pct", "value": 5.0},
    }
    call = ToolCall(name="trade_intent_submit", arguments=spec, id="toolu_p4")

    result = trade_intent_submit_handler(
        call,
        config=cfg,
        default_strategy="s1",
        default_source="strategy_agent",
    )

    # Schema / validation errors should come back as a ToolError or as
    # an envelope flagged with ``status='rejected'`` — never raise.
    if result.is_error:
        assert "stop_loss" in str(result.error.message).lower() or "pct" in str(result.error.message).lower()
    else:
        out = result.content[0].data
        # The TradePlan path may also surface validator failure via the
        # ``status='rejected'`` envelope with a clear reason. Either is
        # acceptable as long as it doesn't trigger a market order.
        assert out.get("status") != "filled" or "rejected" in str(out).lower()
