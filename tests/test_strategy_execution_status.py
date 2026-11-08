"""Receipt status uses settlement evidence, never strategy-provided status."""
import json

import pytest

from nerya.core.config import Config
from nerya.core.paths import WorkspacePaths
from nerya.skills.builtin.backtest.scripts.config import load_config
from nerya.skills.builtin.backtest.scripts.engine import run_backtest
from nerya.strategies.context import StrategyTrading
from nerya.skills.builtin.backtest.scripts.mock_ctx import MockTrading

pytestmark = pytest.mark.smoke
MARKET = "MOCK:BTCUSDT"


def test_engine_receipt_unlocks_death_cross_exit():
    prices = [100, 110, 120, 110, 90, 80, 70]
    rows = [dict(ts=1700000000 + i * 3600, open=p, high=p + 1, low=p - 1,
                 close=p, volume=1) for i, p in enumerate(prices)]
    observed = []

    def strategy(ctx):
        receipt = ctx.state.get("pending")
        if receipt:
            status = ctx.trading.execution_status(receipt)
            observed.append(status)
            if not status["terminal"]:
                return ctx.result.hold(reason="await settlement")
            assert status["status"] == "filled"
            ctx.state.set("pending", None)
        candles = ctx.bars_by_market[MARKET]
        closes = [bar["close"] for bar in candles]
        position = ctx.portfolio.position(MARKET)
        if len(closes) == 2:
            receipt = ctx.trading.open_position(market=MARKET, side="long",
                sizing={"method": "fixed_usd", "fixed_usd": 100})
        elif (position and len(closes) >= 4
              and sum(closes[-3:-1]) / 2 >= sum(closes[-4:-1]) / 3
              and sum(closes[-2:]) / 2 < sum(closes[-3:]) / 3):
            receipt = ctx.trading.close_position(market=MARKET, side="long", reasoning_ref="death_cross")
        else:
            return ctx.result.hold(reason="no signal")
        assert receipt["order_id"] is None
        assert ctx.trading.execution_status(receipt) == {
            "status": "submitted", "terminal": False, "reconciled": True,
            "has_open_orders": True, "recovery": "wait"}
        # A model-authored status or portfolio delta must not settle this intent.
        receipt["status"] = "filled"
        assert not ctx.trading.execution_status(receipt)["terminal"]
        ctx.state.set("pending", receipt)
        return ctx.result.hold(reason="queued")

    result = run_backtest(None, load_config(markets=[MARKET], overrides={"warmup_bars": 0, "tf": "1h"}),
                          candles_by_market={MARKET: rows}, run_fn=strategy)
    assert len(result.trades) == 2
    assert [fill["side"] for fill in result.trades] == ["buy", "sell"]
    assert result.trades[-1]["reason"] == "death_cross"
    assert all(not fill["forced_close"] and fill["intent_id"] and fill["order_id"] for fill in result.trades)
    assert len(observed) == 2 and all(s["terminal"] and s["reconciled"] for s in observed)


def test_runtime_missing_evidence_is_unknown_without_creating_db(tmp_path):
    config = Config(paths=WorkspacePaths(root=tmp_path), data={})
    trading = StrategyTrading(config=config, strategy_id="mine", policy=None, accounts=("paper",))
    for receipt in ({"order_id": "invented", "status": "filled"}, {}, None, "order"):
        status = trading.execution_status(receipt)
        assert status == {"status": "unknown", "terminal": False, "reconciled": False,
                          "has_open_orders": None, "recovery": "verify_execution"}
        json.dumps(status, allow_nan=False)
    assert not config.paths.db.exists()


@pytest.mark.parametrize("extra", [
    {"strategy_id": "foreign"}, {"account_id": "foreign"},
    {"order_id": "invented"}, {"intent": {"intent_id": "different"}},
    {"approval_id": "pending"}, {"executor_id": "unverified"},
    {"reservation_id": "unverified"},
])
def test_replay_rejects_foreign_or_unbound_receipt_ids(extra):
    trading = MockTrading([], "mine", accounts=("paper",), execution_records={
        "intent-1": {"strategy_id": "mine", "status": "filled", "order_ids": ["order-1"]},
    })
    result = trading.execution_status({"intent_id": "intent-1", **extra})
    assert result["status"] == "unknown" and not result["terminal"]


def test_replay_receipt_status_never_overrides_settlement():
    records = {"intent-1": {"strategy_id": "mine", "status": "submitted", "order_ids": []}}
    trading = MockTrading([], "mine", accounts=("paper",), execution_records=records)
    receipt = {"intent_id": "intent-1", "status": "filled"}
    assert trading.execution_status(receipt)["status"] == "submitted"
    assert trading.execution_status({"intent_id": "invented", "status": "filled"})["status"] == "unknown"
    records["intent-1"].update(status="rejected")
    status = trading.execution_status(receipt)
    assert status["status"] == "rejected" and status["terminal"] and status["reconciled"]
    assert status["has_open_orders"] is False
