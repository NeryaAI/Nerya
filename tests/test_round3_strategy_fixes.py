"""Round-3 audit regression tests (R3S1..R3S4).

Each case pins one Round-3 finding:

* R3S1 — the backtest ``MockTrading`` answers with a terminal
  paper-equivalent ``filled`` envelope (order summary included), the
  only status the compat adapters treat as executed, so imported
  Freqtrade/VNpy strategies register positions and manage exits in
  replay instead of degenerating into engine forced_close.
* R3S2 — ``_summarise_trades`` reads the D6 fill mirror's
  ``realized_pnl_usd`` key (falling back to ``realized_usd``).
* R3S3 — ``_closed_trade_pairs`` keeps the unconsumed residual of a
  partially consumed open queued FIFO instead of discarding it.
* R3S4 — the freqtrade adapter adopts the envelope's real fill into its
  tracked position, clamps exits to the held qty (never a notional
  fallback), and reconciles partial closes; the vnpy adapter adopts the
  real fill size/price symmetrically.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from nerya.skills.builtin.backtest.scripts.mock_ctx import MockState, MockTrading
from nerya.strategies.result import ResultBuilder, StrategyResultStatus

pytestmark = pytest.mark.smoke

MARKET = "PAPER:BTCUSDT"


def _candles(closes: list[float], *, start_ts: int = 1_700_000_000, step_s: int = 60) -> list[dict]:
    rows = []
    for i, close in enumerate(closes):
        open_ = closes[i - 1] if i else close
        rows.append(
            {
                "ts": start_ts + i * step_s,
                "open": open_,
                "high": max(open_, close) * 1.001,
                "low": min(open_, close) * 0.999,
                "close": close,
                "volume": 10.0 + i,
            }
        )
    return rows


class EnvelopeTrading:
    """submit_intent stub returning scripted envelopes with order summaries."""

    def __init__(self, *envelopes: dict[str, Any]) -> None:
        self.intents: list[dict] = []
        self.envelopes = list(envelopes) or [{"ok": True, "status": "filled", "order": {}}]
        self.n = 0

    def submit_intent(self, **payload):
        self.intents.append(dict(payload))
        env = self.envelopes[min(self.n, len(self.envelopes) - 1)]
        self.n += 1
        out = {"intent_id": f"env_{self.n}", "intent": dict(payload), **env}
        out.setdefault("order", {})
        return out


def _fake_ctx(candles: list[dict], trading: EnvelopeTrading) -> SimpleNamespace:
    return SimpleNamespace(
        market=SimpleNamespace(
            candles=lambda market, *a, timeframe="1m", limit=990, **kw: list(candles)[-limit:]
        ),
        state=MockState(),
        trading=trading,
        policy=SimpleNamespace(default_order_usd=100.0),
        result=ResultBuilder(),
        audit=None,
        config=SimpleNamespace(mode="paper", markets=(MARKET,)),
    )


# ---------------------------------------------------------------------------
# R3S1 — MockTrading terminal filled envelope
# ---------------------------------------------------------------------------


def test_mock_trading_submit_returns_terminal_filled_envelope() -> None:
    trading = MockTrading([], "s1", state=MockState(), mark_price=50.0)
    env = trading.submit_intent(market=MARKET, side="buy", size=100.0, size_unit="usd", order_type="market")
    assert env["status"] == "filled"
    order = env["order"]
    assert order["status"] == "filled"
    assert order["order_id"]
    assert order["filled_size"] == pytest.approx(100.0 / 50.0)
    assert order["avg_price"] == pytest.approx(50.0)
    assert order["notional_usd"] == pytest.approx(100.0)
    assert env["intent_id"]


def test_mock_trading_base_size_passes_through_and_close_uses_book() -> None:
    state = MockState()
    state.set(f"position:{MARKET}", {"qty": 1.5, "avg_price": 100.0})
    trading = MockTrading([], "s1", state=state, mark_price=110.0)

    env = trading.submit_intent(market=MARKET, side="buy", size=2.0, size_unit="base", order_type="market")
    assert env["order"]["filled_size"] == pytest.approx(2.0)

    close_env = trading.close_position(market=MARKET, side="long")
    assert close_env["status"] == "filled"
    assert close_env["order"]["filled_size"] == pytest.approx(1.5)

    reduce_env = trading.reduce_position(market=MARKET, side="long", reduce_pct=0.5)
    assert reduce_env["status"] == "filled"
    assert reduce_env["order"]["filled_size"] == pytest.approx(0.75)


def test_mock_trading_legacy_bare_exit_estimates_book_qty() -> None:
    state = MockState()
    state.set(f"position:{MARKET}", {"qty": 2.0, "avg_price": 100.0})
    trading = MockTrading([], "s1", state=state, mark_price=105.0)
    env = trading.submit_intent(market=MARKET, side="sell", size=0, size_unit="usd", order_type="market")
    assert env["order"]["filled_size"] == pytest.approx(2.0)


# ---------------------------------------------------------------------------
# R3S2 — performance summary reads the D6 pnl key
# ---------------------------------------------------------------------------


def test_summarise_trades_reads_realized_pnl_usd() -> None:
    from nerya.strategies.performance import _summarise_trades

    pnls = [
        {"pnl": {"realized_pnl_usd": 12.5, "fee_usd": 0.5}},  # D6 mirror key
        {"pnl": {"realized_usd": -3.0}},  # legacy key still honoured
    ]
    out = _summarise_trades([], [], [], pnls)
    assert out["pnl_total_usd"] == pytest.approx(9.5)
    assert out["wins"] == 1
    assert out["losses"] == 1
    assert out["closed"] == 2
    assert out["max_drawdown_usd"] == pytest.approx(-3.0)


# ---------------------------------------------------------------------------
# R3S3 — partial closes keep the open residual queued FIFO
# ---------------------------------------------------------------------------


def test_closed_trade_pairs_keep_partial_open_residual() -> None:
    from nerya.skills.builtin.backtest.scripts.metrics import _closed_trade_pairs

    trades = [
        # Enter 2.0 @ 100 (fee 1.0), reduce 50% @ 110, exit the rest @ 120.
        {"ts": 0, "market": "M", "side": "buy", "qty": 2.0, "price": 100.0, "fee": 1.0},
        {"ts": 3600, "market": "M", "side": "sell", "qty": 1.0, "price": 110.0, "fee": 1.0},
        {"ts": 7200, "market": "M", "side": "sell", "qty": 1.0, "price": 120.0, "fee": 1.0},
    ]
    pairs = _closed_trade_pairs(trades)

    assert len(pairs) == 2, "the residual 1.0 must pair with the final exit"
    # Pro-rata fees: each pair carries half the entry fee + its exit share.
    assert pairs[0]["pnl_usd"] == pytest.approx(10.0 - 1.5)
    assert pairs[1]["pnl_usd"] == pytest.approx(20.0 - 1.5)
    assert sum(p["pnl_usd"] for p in pairs) == pytest.approx(27.0)
    assert trades[1]["pnl"] == pytest.approx(8.5)
    assert trades[2]["pnl"] == pytest.approx(18.5)


def test_closed_trade_pairs_fifo_multi_open_full_close() -> None:
    from nerya.skills.builtin.backtest.scripts.metrics import _closed_trade_pairs

    trades = [
        {"ts": 0, "market": "M", "side": "buy", "qty": 1.0, "price": 100.0, "fee": 0.0},
        {"ts": 600, "market": "M", "side": "buy", "qty": 1.0, "price": 105.0, "fee": 0.0},
        # One exit fill closing both opens — FIFO order preserved.
        {"ts": 1200, "market": "M", "side": "sell", "qty": 2.0, "price": 110.0, "fee": 0.0},
    ]
    pairs = _closed_trade_pairs(trades)
    assert [p["pnl_usd"] for p in pairs] == pytest.approx([10.0, 5.0])
    assert trades[2]["pnl"] == pytest.approx(15.0)


# ---------------------------------------------------------------------------
# R3S4 — adapters adopt the envelope's real fill; exits clamp to holdings
# ---------------------------------------------------------------------------


FT_ROI_SOURCE = '''
"""freqtrade strategy with a plain ROI exit."""
from freqtrade.strategy import IStrategy


class RoiExitStrategy(IStrategy):
    timeframe = "1m"
    stoploss = -0.99
    minimal_roi = {"0": 0.05}

    def populate_indicators(self, dataframe, metadata):
        return dataframe

    def populate_entry_trend(self, dataframe, metadata):
        dataframe.loc[dataframe["close"] > 0, "enter_long"] = 1
        return dataframe

    def populate_exit_trend(self, dataframe, metadata):
        return dataframe
'''


def _load_ft(tmp_path: Path, source: str, name: str, cls: str) -> Any:
    from nerya.strategies.compat.entrypoint import load_framework_strategy

    (tmp_path / name).write_text(source, encoding="utf-8")
    return load_framework_strategy(
        tmp_path, framework="freqtrade", source_file=name, class_name=cls, settings={}
    )


def test_freqtrade_entry_adopts_envelope_real_fill(tmp_path: Path) -> None:
    from nerya.strategies.compat.freqtrade_adapter import run_freqtrade_tick

    strategy = _load_ft(tmp_path, FT_ROI_SOURCE, "r3s4_ft.py", "RoiExitStrategy")
    trading = EnvelopeTrading(
        {"ok": True, "status": "filled", "order": {"filled_size": 0.5, "avg_price": 101.0}}
    )
    ctx = _fake_ctx(_candles([100.0, 100.5, 101.0]), trading)
    run_freqtrade_tick(ctx, strategy, market=MARKET, settings={"stake_amount": 100.0})

    pos = ctx.state.get("_compat_pos")[MARKET]
    assert pos["side"] == "long"
    # The venue's real fill (0.5 base @ 101) — not stake/signal-price.
    assert float(pos["qty"]) == pytest.approx(0.5)
    assert float(pos["entry_price"]) == pytest.approx(101.0)


def _seed_long(ctx: Any, *, qty: float, entry: float = 100.0) -> None:
    ctx.state.set(
        "_compat_pos",
        {
            MARKET: {
                "side": "long",
                "qty": qty,
                "entry_price": entry,
                "entry_ts_ms": 1_700_000_000_000,
                "stake_amount": 100.0,
                "high_water_profit": 0.0,
                "stop_price": None,
            }
        },
    )


def test_freqtrade_exit_clamps_to_holdings_and_reconciles_residual(tmp_path: Path) -> None:
    from nerya.strategies.compat.freqtrade_adapter import run_freqtrade_tick

    strategy = _load_ft(tmp_path, FT_ROI_SOURCE, "r3s4_ft_b.py", "RoiExitStrategy")

    # Unknown holdings: the exit must hold instead of submitting the
    # entry notional (an over-close flips the PositionBook).
    trading = EnvelopeTrading({"ok": True, "status": "filled", "order": {}})
    ctx = _fake_ctx(_candles([100.0, 112.0]), trading)
    _seed_long(ctx, qty=0.0)
    result = run_freqtrade_tick(ctx, strategy, market=MARKET, settings={})
    assert result.reason == "freqtrade_compat_exit_no_holding"
    assert trading.intents == []

    # Partial venue fill: submit exactly the held qty in base units, then
    # keep the un-closed residual tracked (side stays long, qty reduced).
    trading2 = EnvelopeTrading(
        {"ok": True, "status": "filled", "order": {"filled_size": 1.25, "avg_price": 112.0}}
    )
    ctx2 = _fake_ctx(_candles([100.0, 112.0]), trading2)
    _seed_long(ctx2, qty=2.0)
    run_freqtrade_tick(ctx2, strategy, market=MARKET, settings={})
    assert trading2.intents, "expected the stop/ROI exit to submit"
    assert trading2.intents[0]["size_unit"] == "base"
    assert trading2.intents[0]["size"] == pytest.approx(2.0)
    pos2 = ctx2.state.get("_compat_pos")[MARKET]
    assert pos2["side"] == "long"
    assert float(pos2["qty"]) == pytest.approx(0.75)

    # Full fill reported by the executor → flat.
    trading3 = EnvelopeTrading(
        {"ok": True, "status": "filled", "order": {"filled_size": 2.0, "avg_price": 112.0}}
    )
    ctx3 = _fake_ctx(_candles([100.0, 112.0]), trading3)
    _seed_long(ctx3, qty=2.0)
    run_freqtrade_tick(ctx3, strategy, market=MARKET, settings={})
    pos3 = ctx3.state.get("_compat_pos")[MARKET]
    assert pos3["side"] == "flat"
    assert float(pos3["qty"]) == 0.0


def test_vnpy_adopts_envelope_real_fill() -> None:
    from nerya.strategies.compat.shims import install_vnpy_shims

    install_vnpy_shims()
    import vnpy_ctastrategy

    from nerya.strategies.compat.vnpy_adapter import run_vnpy_tick

    class FracBuyStrategy(vnpy_ctastrategy.CtaTemplate):
        seen_price = 0.0
        seen_volume = 0.0

        def on_bar(self, bar):
            if self.pos == 0:
                self.buy(bar.close_price, 0.5)

        def on_trade(self, trade):
            FracBuyStrategy.seen_price = trade.price
            FracBuyStrategy.seen_volume = trade.volume

    base = [100.0 + i for i in range(6)]
    trading = EnvelopeTrading(
        {"ok": True, "status": "filled", "order": {"filled_size": 0.25, "avg_price": 123.0}}
    )
    ctx = _fake_ctx(_candles(base), trading)
    strategy = FracBuyStrategy(None, "r3s4_vnpy", "ADV.LOCAL")
    run_vnpy_tick(ctx, strategy, market=MARKET, settings={"bar_timeframe": "1m"})  # init

    ctx2 = _fake_ctx(_candles(base + [106.0]), trading)
    ctx2.state = ctx.state
    run_vnpy_tick(ctx2, strategy, market=MARKET, settings={"bar_timeframe": "1m"})

    assert len(trading.intents) == 1
    # The synthesized fill adopted the envelope's real size and price.
    assert float(strategy.pos) == pytest.approx(0.25)
    assert FracBuyStrategy.seen_volume == pytest.approx(0.25)
    assert FracBuyStrategy.seen_price == pytest.approx(123.0)
    assert float(ctx2.state.get("_compat_vnpy")["pos"]) == pytest.approx(0.25)


# ---------------------------------------------------------------------------
# Keep the strategy-managed exit result status contract visible
# ---------------------------------------------------------------------------


def test_freqtrade_managed_exit_still_reports_filled(tmp_path: Path) -> None:
    from nerya.strategies.compat.freqtrade_adapter import run_freqtrade_tick

    strategy = _load_ft(tmp_path, FT_ROI_SOURCE, "r3s4_ft_c.py", "RoiExitStrategy")
    trading = EnvelopeTrading(
        {"ok": True, "status": "filled", "order": {"filled_size": 1.0, "avg_price": 100.0}},
        {"ok": True, "status": "filled", "order": {"filled_size": 1.0, "avg_price": 112.0}},
    )
    ctx = _fake_ctx(_candles([100.0, 100.5, 101.0]), trading)
    run_freqtrade_tick(ctx, strategy, market=MARKET, settings={"stake_amount": 100.0})

    ctx2 = _fake_ctx(_candles([100.0, 100.5, 101.0, 112.0]), trading)
    ctx2.state = ctx.state
    result = run_freqtrade_tick(ctx2, strategy, market=MARKET, settings={"stake_amount": 100.0})
    assert result.status is StrategyResultStatus.FILLED
    assert any(i["plan_action"] == "close_position" for i in trading.intents)
    pos = ctx2.state.get("_compat_pos")[MARKET]
    assert pos["side"] == "flat"
