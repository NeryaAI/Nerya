"""Adversarial regression tests for the framework-compat layer.

Each case pins a specific audit finding:

1. A1 — fractional VNpy volumes keep ``strategy.pos`` a float, so
   ``pos == 0`` gates do not re-enter every bar.
2. A2 — VNpy ``stop=True`` orders park locally and only fill when a
   bar's high/low crosses the stop; they cancel cleanly.
3. A3 — freqtrade ``custom_stoploss`` ratchets a persisted stop price
   (profit locks never loosen).
4. A4 — trailing ``positive/offset`` only switches to the positive
   distance above the offset (before it trails at ``abs(stoploss)``).
5. C5/E5 — a ``pending_approval`` envelope never creates a phantom
   position in either adapter.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from nerya.strategies.result import ResultBuilder, StrategyResultStatus

pytestmark = pytest.mark.smoke


# ---------------------------------------------------------------------------
# fixtures
# ---------------------------------------------------------------------------


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


class FakeState:
    def __init__(self) -> None:
        self._data: dict = {}

    def get(self, key, default=None):
        return self._data.get(key, default)

    def set(self, key, value) -> None:
        self._data[key] = value


class ScriptedTrading:
    """submit_intent stub returning a scripted envelope status per order."""

    def __init__(self, *statuses: str) -> None:
        self.intents: list[dict] = []
        self.statuses = list(statuses) or ["filled"]
        self.n = 0

    def submit_intent(self, **payload):
        self.intents.append(dict(payload))
        status = self.statuses[min(self.n, len(self.statuses) - 1)]
        self.n += 1
        envelope = {
            "ok": status not in {"rejected", "failed"},
            "status": status,
            "intent_id": f"scripted_{self.n}",
            "intent": dict(payload),
            "order": {},
            "risk_decision": {"decision": "allow"},
        }
        if status == "pending_approval":
            envelope["approval_id"] = f"appr_{self.n}"
        return envelope


class FakeAudit:
    """Duck-typed StrategyAudit capturing journal entries."""

    def __init__(self) -> None:
        self.events: list[dict] = []

    def log(self, kind, payload=None, *, level="info") -> None:
        # Mirrors StrategyAudit.log: kinds are prefixed "strategy.".
        self.events.append({"kind": f"strategy.{kind}", "payload": payload, "level": level})


def _fake_ctx(candles: list[dict], trading: ScriptedTrading | None = None) -> SimpleNamespace:
    return SimpleNamespace(
        market=SimpleNamespace(
            candles=lambda market, *a, timeframe="1m", limit=990, **kw: list(candles)[-limit:]
        ),
        state=FakeState(),
        trading=trading or ScriptedTrading(),
        policy=SimpleNamespace(default_order_usd=100.0),
        result=ResultBuilder(),
        audit=FakeAudit(),
        config=SimpleNamespace(mode="paper", markets=("PAPER:BTCUSDT",)),
    )


def _cta_template() -> type:
    """Install the vnpy shims first, then fetch the (shimmed) base class."""

    from nerya.strategies.compat.shims import install_vnpy_shims

    install_vnpy_shims()
    import vnpy_ctastrategy

    return vnpy_ctastrategy.CtaTemplate


def _vnpy_strategy(cls: type, name: str) -> Any:
    return cls(None, name, "ADV.LOCAL")


# ---------------------------------------------------------------------------
# 1. A1 — fractional vnpy volumes stay fractional
# ---------------------------------------------------------------------------


def test_vnpy_fractional_volume_no_reentry() -> None:
    CtaTemplate = _cta_template()

    from nerya.strategies.compat.vnpy_adapter import run_vnpy_tick

    class FracStrategy(CtaTemplate):
        """Buys 0.5 base whenever it believes it is flat."""

        def on_bar(self, bar):
            if self.pos == 0:
                self.buy(bar.close_price, 0.5)

    strategy = _vnpy_strategy(FracStrategy, "frac")
    base = [100.0 + i for i in range(6)]

    ctx = _fake_ctx(_candles(base))
    run_vnpy_tick(ctx, strategy, market="PAPER:BTCUSDT", settings={"bar_timeframe": "1m"})  # init
    assert ctx.trading.intents == []

    # First trading tick (new bar 106): entry fills at 0.5 base.
    ctx2 = _fake_ctx(_candles(base + [106.0]))
    ctx2.state = ctx.state
    run_vnpy_tick(ctx2, strategy, market="PAPER:BTCUSDT", settings={"bar_timeframe": "1m"})
    assert len(ctx2.trading.intents) == 1
    assert float(strategy.pos) == pytest.approx(0.5)
    assert float(ctx2.state.get("_compat_vnpy")["pos"]) == pytest.approx(0.5)

    # Next bar (107): pos is 0.5 — not truncated to 0 — so no re-entry.
    ctx3 = _fake_ctx(_candles(base + [106.0, 107.0]))
    ctx3.state = ctx.state
    run_vnpy_tick(ctx3, strategy, market="PAPER:BTCUSDT", settings={"bar_timeframe": "1m"})
    assert ctx3.trading.intents == []
    assert float(strategy.pos) == pytest.approx(0.5)


# ---------------------------------------------------------------------------
# 2. A2 — vnpy stop orders park until the bar range crosses them
# ---------------------------------------------------------------------------


def test_vnpy_stop_order_triggers_only_on_cross() -> None:
    CtaTemplate = _cta_template()

    from nerya.strategies.compat.vnpy_adapter import run_vnpy_tick

    class StopBuyStrategy(CtaTemplate):
        placed = False

        def on_bar(self, bar):
            if not self.placed:
                self.buy(bar.close_price * 1.10, 1.0, stop=True)
                self.placed = True

    strategy = _vnpy_strategy(StopBuyStrategy, "stop_buy")
    base = [100.0, 100.5, 101.0, 101.5, 102.0]

    ctx = _fake_ctx(_candles(base))
    run_vnpy_tick(ctx, strategy, market="PAPER:BTCUSDT", settings={"bar_timeframe": "1m"})  # init

    # First trading tick (bar 103) places the stop 10% above market —
    # nothing is submitted.
    ctx2 = _fake_ctx(_candles(base + [103.0]))
    ctx2.state = ctx.state
    run_vnpy_tick(ctx2, strategy, market="PAPER:BTCUSDT", settings={"bar_timeframe": "1m"})
    assert ctx2.trading.intents == []
    assert float(strategy.pos) == 0.0
    pending = ctx2.state.get("_compat_vnpy")["pending_stops"]
    assert len(pending) == 1 and pending[0]["price"] == pytest.approx(103.0 * 1.10)

    # A bar below the trigger must not fill it.
    ctx3 = _fake_ctx(_candles(base + [103.0, 104.0]))
    ctx3.state = ctx.state
    run_vnpy_tick(ctx3, strategy, market="PAPER:BTCUSDT", settings={"bar_timeframe": "1m"})
    assert ctx3.trading.intents == []
    assert float(strategy.pos) == 0.0

    # A bar whose high crosses the stop fills it (buy/open_position).
    ctx4 = _fake_ctx(_candles(base + [103.0, 104.0, 115.0]))
    ctx4.state = ctx.state
    run_vnpy_tick(ctx4, strategy, market="PAPER:BTCUSDT", settings={"bar_timeframe": "1m"})
    assert len(ctx4.trading.intents) == 1
    assert ctx4.trading.intents[0]["side"] == "buy"
    assert ctx4.trading.intents[0]["plan_action"] == "open_position"
    assert float(strategy.pos) == pytest.approx(1.0)
    assert ctx4.state.get("_compat_vnpy")["pending_stops"] == []


def test_vnpy_stop_order_cancels_cleanly() -> None:
    CtaTemplate = _cta_template()

    from nerya.strategies.compat.vnpy_adapter import run_vnpy_tick

    class StopCancelStrategy(CtaTemplate):
        placed = False
        stop_id: str | None = None
        cancelled = False

        def on_bar(self, bar):
            if not self.placed:
                ids = self.buy(bar.close_price * 1.10, 1.0, stop=True)
                self.stop_id = ids[0] if ids else None
                self.placed = True
            elif not self.cancelled and self.stop_id:
                self.cancel_order(self.stop_id)
                self.cancelled = True

    strategy = _vnpy_strategy(StopCancelStrategy, "stop_cancel")
    base = [100.0, 100.5, 101.0, 101.5, 102.0]

    ctx = _fake_ctx(_candles(base))
    run_vnpy_tick(ctx, strategy, market="PAPER:BTCUSDT", settings={"bar_timeframe": "1m"})  # init
    ctx2 = _fake_ctx(_candles(base + [103.0]))
    ctx2.state = ctx.state
    run_vnpy_tick(ctx2, strategy, market="PAPER:BTCUSDT", settings={"bar_timeframe": "1m"})
    assert ctx2.state.get("_compat_vnpy")["pending_stops"]

    # Next bar (104, below the trigger) cancels the parked stop
    # strategy-side, like real VNpy.
    ctx3 = _fake_ctx(_candles(base + [103.0, 104.0]))
    ctx3.state = ctx.state
    run_vnpy_tick(ctx3, strategy, market="PAPER:BTCUSDT", settings={"bar_timeframe": "1m"})
    assert ctx3.trading.intents == []
    assert ctx3.state.get("_compat_vnpy")["pending_stops"] == []

    # A later bar crossing the original trigger fills nothing.
    ctx4 = _fake_ctx(_candles(base + [103.0, 104.0, 115.0]))
    ctx4.state = ctx.state
    run_vnpy_tick(ctx4, strategy, market="PAPER:BTCUSDT", settings={"bar_timeframe": "1m"})
    assert ctx4.trading.intents == []
    assert float(strategy.pos) == 0.0


# ---------------------------------------------------------------------------
# 3. A3 — custom_stoploss ratchets a persisted profit lock
# ---------------------------------------------------------------------------


LOCK_SOURCE = '''
"""custom_stoploss profit-lock strategy."""
from freqtrade.strategy import IStrategy


class LockStrategy(IStrategy):
    timeframe = "1m"
    stoploss = -0.10
    minimal_roi = {}
    trailing_stop = False

    def populate_indicators(self, dataframe, metadata):
        return dataframe

    def populate_entry_trend(self, dataframe, metadata):
        dataframe.loc[dataframe["close"] > 0, "enter_long"] = 1
        return dataframe

    def populate_exit_trend(self, dataframe, metadata):
        return dataframe

    def custom_stoploss(self, pair, trade, current_time, current_rate,
                        current_profit, after_fill=False, **kwargs):
        if current_profit >= 0.10:
            return -0.02  # lock in profit 2% below the current rate
        return None
'''


def _load_ft(tmp_path: Path, source: str, name: str, cls: str) -> Any:
    from nerya.strategies.compat.entrypoint import load_framework_strategy

    (tmp_path / name).write_text(source, encoding="utf-8")
    return load_framework_strategy(
        tmp_path, framework="freqtrade", source_file=name, class_name=cls, settings={}
    )


def _seed_long(ctx: Any, market: str, entry: float, *, qty: float = 1.0) -> None:
    # ``_compat_pos`` is the adapter's persisted position store.
    ctx.state.set(
        "_compat_pos",
        {
            market: {
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


def test_freqtrade_custom_stoploss_ratchets(tmp_path: Path) -> None:
    from nerya.strategies.compat.freqtrade_adapter import run_freqtrade_tick

    strategy = _load_ft(tmp_path, LOCK_SOURCE, "lock_ft.py", "LockStrategy")

    # Seeded long from 100; +12% candle arms the hook (2% trail).
    ctx = _fake_ctx(_candles([100.0, 105.0, 112.0]))
    _seed_long(ctx, "PAPER:BTCUSDT", 100.0)
    result = run_freqtrade_tick(ctx, strategy, market="PAPER:BTCUSDT", settings={})
    assert result.status is StrategyResultStatus.HOLD
    pos = ctx.state.get("_compat_pos")["PAPER:BTCUSDT"]
    # Stop ratcheted from the static 90.0 to 112 * 0.98.
    assert float(pos["stop_price"]) == pytest.approx(112.0 * 0.98)

    # Pullback to +9.2%: below the ratcheted stop → exit, even though a
    # fresh (non-ratcheted) hook stop would sit at 109.2*0.98 ≈ 107.0.
    ctx2 = _fake_ctx(_candles([100.0, 105.0, 112.0, 109.2]))
    ctx2.state = ctx.state
    result2 = run_freqtrade_tick(ctx2, strategy, market="PAPER:BTCUSDT", settings={})
    assert ctx2.trading.intents, "ratcheted profit lock must fire"
    assert ctx2.trading.intents[0]["plan_action"] == "close_position"
    assert result2.reason == "freqtrade_compat_exit_stoploss"
    assert ctx2.state.get("_compat_pos")["PAPER:BTCUSDT"]["side"] == "flat"


# ---------------------------------------------------------------------------
# 4. A4 — trailing positive/offset semantics
# ---------------------------------------------------------------------------


TRAIL_SOURCE = '''
"""trailing_stop_positive_offset strategy."""
from freqtrade.strategy import IStrategy


class TrailStrategy(IStrategy):
    timeframe = "1m"
    stoploss = -0.10
    minimal_roi = {}
    trailing_stop = True
    trailing_stop_positive = 0.02
    trailing_stop_positive_offset = 0.10
    trailing_only_offset_is_reached = False

    def populate_indicators(self, dataframe, metadata):
        return dataframe

    def populate_entry_trend(self, dataframe, metadata):
        dataframe.loc[dataframe["close"] > 0, "enter_long"] = 1
        return dataframe

    def populate_exit_trend(self, dataframe, metadata):
        return dataframe
'''


def test_freqtrade_trailing_offset_gates_positive_distance(tmp_path: Path) -> None:
    from nerya.strategies.compat.freqtrade_adapter import run_freqtrade_tick

    strategy = _load_ft(tmp_path, TRAIL_SOURCE, "trail_ft.py", "TrailStrategy")

    # +5% then retrace to +3%: below the 10% offset the trail distance is
    # abs(stoploss)=10%, so a 2% retracement of the peak must NOT exit
    # (the old code started trailing at 2% as soon as profit >= 2%).
    ctx = _fake_ctx(_candles([100.0, 105.0]))
    _seed_long(ctx, "PAPER:BTCUSDT", 100.0)
    assert run_freqtrade_tick(ctx, strategy, market="PAPER:BTCUSDT", settings={}).status is (
        StrategyResultStatus.HOLD
    )
    ctx2 = _fake_ctx(_candles([100.0, 105.0, 103.0]))
    ctx2.state = ctx.state
    run_freqtrade_tick(ctx2, strategy, market="PAPER:BTCUSDT", settings={})
    assert ctx2.trading.intents == [], "positive trail must not arm below the offset"

    # Above the offset (+13%) the positive distance (2%) takes over.
    ctx3 = _fake_ctx(_candles([100.0, 105.0, 103.0, 113.0]))
    ctx3.state = ctx.state
    run_freqtrade_tick(ctx3, strategy, market="PAPER:BTCUSDT", settings={})
    assert ctx3.trading.intents == []

    # Retrace to +11% = peak 13% - 2% → trailing_stop exit.
    ctx4 = _fake_ctx(_candles([100.0, 105.0, 103.0, 113.0, 111.0]))
    ctx4.state = ctx.state
    result4 = run_freqtrade_tick(ctx4, strategy, market="PAPER:BTCUSDT", settings={})
    assert ctx4.trading.intents, "expected trailing stop above the offset"
    assert ctx4.trading.intents[0]["plan_action"] == "close_position"
    assert result4.reason == "freqtrade_compat_exit_trailing_stop"


# ---------------------------------------------------------------------------
# 5. C5/E5 — pending_approval never creates a phantom position
# ---------------------------------------------------------------------------


def test_freqtrade_pending_approval_leaves_position_unchanged(tmp_path: Path) -> None:
    from nerya.strategies.compat.freqtrade_adapter import run_freqtrade_tick

    strategy = _load_ft(tmp_path, LOCK_SOURCE, "pending_ft.py", "LockStrategy")
    candles = _candles([100.0, 100.5])

    # First order is held by the Approval Gate.
    trading = ScriptedTrading("pending_approval", "filled")
    ctx = _fake_ctx(candles, trading)
    result = run_freqtrade_tick(ctx, strategy, market="PAPER:BTCUSDT", settings={})
    assert result.status is StrategyResultStatus.PENDING_APPROVAL
    pos = ctx.state.get("_compat_pos")["PAPER:BTCUSDT"]
    assert pos["side"] == "flat" and float(pos["qty"]) == 0.0
    assert pos["pending_intent"]["kind"] == "entry"

    # Next tick: the unresolvable marker is dropped with a journal
    # warning and the position is still flat; the strategy re-submits
    # and this time the fill records a real position.
    ctx2 = _fake_ctx(candles, trading)
    ctx2.state = ctx.state
    result2 = run_freqtrade_tick(ctx2, strategy, market="PAPER:BTCUSDT", settings={})
    # The marker was resolved (dropped) and the re-submitted entry
    # filled into a real position.
    assert ctx2.state.get("_compat_pos")["PAPER:BTCUSDT"].get("pending_intent") is None
    assert result2.status is StrategyResultStatus.FILLED
    pos2 = ctx2.state.get("_compat_pos")["PAPER:BTCUSDT"]
    assert pos2["side"] == "long" and float(pos2["qty"]) > 0.0
    dropped = [e for e in ctx2.audit.events if e["kind"] == "strategy.compat_pending_dropped"]
    assert dropped and dropped[0]["level"] == "warn"


def test_vnpy_pending_approval_does_not_synthesize_fill() -> None:
    CtaTemplate = _cta_template()

    from nerya.strategies.compat.vnpy_adapter import run_vnpy_tick

    class AlwaysBuyStrategy(CtaTemplate):
        def on_bar(self, bar):
            if self.pos == 0:
                self.buy(bar.close_price, 1.0)

    strategy = _vnpy_strategy(AlwaysBuyStrategy, "pending_buy")
    base = [100.0 + i for i in range(6)]

    trading = ScriptedTrading("pending_approval", "filled")
    ctx = _fake_ctx(_candles(base), trading)
    run_vnpy_tick(ctx, strategy, market="PAPER:BTCUSDT", settings={"bar_timeframe": "1m"})  # init

    # The buy (new bar 106) is held by the Approval Gate: no fill, pos
    # untouched, marker persisted.
    ctx2 = _fake_ctx(_candles(base + [106.0]), trading)
    ctx2.state = ctx.state
    run_vnpy_tick(ctx2, strategy, market="PAPER:BTCUSDT", settings={"bar_timeframe": "1m"})
    assert len(ctx2.trading.intents) == 1
    assert float(strategy.pos) == 0.0
    state2 = ctx2.state.get("_compat_vnpy")
    assert state2["pos"] == 0.0
    assert state2["pending_intent"]["vt_orderid"]

    # Next tick resolves (drops) the marker; the re-evaluated signal
    # fills and only then does pos move.
    ctx3 = _fake_ctx(_candles(base + [106.0, 107.0]), trading)
    ctx3.state = ctx.state
    run_vnpy_tick(ctx3, strategy, market="PAPER:BTCUSDT", settings={"bar_timeframe": "1m"})
    assert float(strategy.pos) == pytest.approx(1.0)
    dropped = [e for e in ctx3.audit.events if e["kind"] == "strategy.compat_pending_dropped"]
    assert dropped and dropped[0]["level"] == "warn"
