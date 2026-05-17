from __future__ import annotations

from copy import deepcopy
import json

import pytest

from nerya.core import jsonl, yaml_io
from nerya.core.config import Config, DEFAULT_CONFIG
from nerya.core.paths import WorkspacePaths
from nerya.trading.order_tracker import OrderTracker
from nerya.trading.submit import submit_trade_intent


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
    yaml_io.dump(
        cfg.paths.messages_channels,
        {
            "channels": {
                "telegram_ops": {
                    "kind": "telegram",
                    "bot_token_ref": "vault://telegram_bot_token",
                    "chat_id": "123456",
                },
                "discord_ops": {
                    "kind": "discord",
                    "webhook_url_ref": "vault://discord_webhook_url",
                },
                "discord_silent": {
                    "kind": "discord",
                    "webhook_url_ref": "vault://discord_silent_webhook_url",
                    "trade_notifications": False,
                },
            }
        },
    )
    return cfg


def test_trade_execution_fans_out_to_configured_gateway_channels(tmp_path):
    cfg = _config(tmp_path)

    out = submit_trade_intent(
        cfg,
        spec={
            "strategy_id": "s1",
            "account_id": "paper_main",
            "market": "mock:BTC/USDT",
            "side": "buy",
            "size": 100,
            "size_unit": "usd",
            "order_type": "market",
            "confidence": 1.0,
            "source": "strategy_runtime",
        },
        market_snapshot={"price": 50_000, "age_s": 0, "source": "test"},
    )

    assert out["status"] == "filled"
    assert set(out["notifications"]["channels"]) == {"telegram_ops", "discord_ops"}
    assert "discord_silent" not in out["notifications"]["channels"]

    message_rows = jsonl.read_all(cfg.paths.journal("messages"))
    sent_rows = [r for r in message_rows if r.get("kind") == "message.sent"]
    assert len(sent_rows) == 2
    assert all("Nerya trade filled" in r.get("text", "") for r in sent_rows)
    assert all("mock:BTC/USDT" in r.get("text", "") for r in sent_rows)
    assert "vault://telegram_bot_token" not in json.dumps(message_rows)
    assert "vault://discord_webhook_url" not in json.dumps(message_rows)

    trading_rows = jsonl.read_all(cfg.paths.journal("trading"))
    assert any(r.get("kind") == "trade.notification" for r in trading_rows)


def test_executor_fill_fans_out_when_recorded_after_submit_turn(tmp_path):
    cfg = _config(tmp_path)
    tracker = OrderTracker(cfg.paths)
    order = tracker.register(
        client_order_id="client-1",
        account_id="paper_main",
        strategy_id="s1",
        market="mock:BTC/USDT",
        side="buy",
        order_type="market",
        size_base=0.01,
        intent_id="intent-1",
        plan_id="plan-1",
        executor_id="exec-1",
    )

    tracker.record_fill(
        order_id=order.order_id,
        price=50_000,
        size_base=0.01,
        fee_usd=0.5,
        source="live",
    )

    message_rows = jsonl.read_all(cfg.paths.journal("messages"))
    sent_rows = [r for r in message_rows if r.get("kind") == "message.sent"]
    assert len(sent_rows) == 2
    assert all("Nerya trade filled" in r.get("text", "") for r in sent_rows)
    assert all("Executor: exec-1" in r.get("text", "") for r in sent_rows)
    assert all("Plan: plan-1" in r.get("text", "") for r in sent_rows)
