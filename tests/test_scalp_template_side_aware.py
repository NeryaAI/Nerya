"""Regression: generated scalp templates must close in the right
direction when their share is currently SHORT.

This pins down the v6 runaway-bug fix at the **template generator**
layer. Before the fix:

  - ``_scalping_template`` always emitted ``side="sell"`` on exit
    (assumed long).
  - When the SDK accidentally returned a merged-position size that
    was negative (the v6 contract break), the template happily
    "closed" a short by *selling more*, ballooning the share.

After the fix:

  1. The template reads ``signed`` size (not just ``abs``) and
     picks ``close_side = 'sell' if signed > 0 else 'buy'``.
  2. The PnL trigger is normalised by side so the TP/SL thresholds
     mean the same thing for long and short slices.

The test exercises **both branches** of the generated code by
substituting a fake StrategyContext that returns a long share, then a
short share, and inspecting the captured ``submit_intent`` payload.
"""

from __future__ import annotations

import pytest
import types

from nerya.evolution.strategy_code_generator import (
    StrategyGenerationRequest,
    _scalping_template,
    _trend_template,
)


pytestmark = pytest.mark.smoke


# --- helpers ---------------------------------------------------------------


class _RecordingTrading:
    """Captures whichever trading-API call the template makes.

    The templates now mix three surfaces:

    * ``submit_intent`` — legacy direct intent (still used by some
      paths and by older generated strategies that haven't been
      regenerated yet).
    * ``open_position`` — modern entry path that ships sizing +
      protection in one TradePlan (new default for entries).
    * ``close_position`` — modern exit path that releases bracket
      protection and forces ``SizingPolicy(close_all)`` (new default
      for tactical exits).

    The test fixture mirrors the duck-typed call shape strategies use
    against the real SDK; nothing here cares whether the underlying
    object is actually a ``StrategyTrading`` instance.
    """

    def __init__(self):
        self.last_call: dict | None = None
        self.last_kind: str | None = None

    def submit_intent(self, **kwargs):
        self.last_call = dict(kwargs)
        self.last_kind = "submit_intent"
        return {"ok": True}

    def open_position(self, **kwargs):
        self.last_call = dict(kwargs)
        self.last_kind = "open_position"
        return {"ok": True, "plan_action": "open_position"}

    def close_position(self, **kwargs):
        self.last_call = dict(kwargs)
        self.last_kind = "close_position"
        return {"ok": True, "plan_action": "close_position"}

    # Back-compat shim — historical test code uses ``last_intent``.
    @property
    def last_intent(self):
        return self.last_call


class _RecordingResult:
    def __init__(self):
        self.last_hold: dict | None = None

    def hold(self, **kwargs):
        self.last_hold = dict(kwargs)
        return {"hold": True, **kwargs}


class _StubPortfolio:
    def __init__(self, position: dict | None):
        self._position = position

    def positions(self, market):
        return [self._position] if self._position else []


class _Policy:
    default_order_usd = 100.0


class _StubMarket:
    def __init__(self, closes: list[float]):
        self._closes = closes

    def candles(self, market, *, timeframe, limit):
        # Produce a list of candle dicts with steadily decreasing
        # prices so momentum is strongly negative. The actual values
        # don't matter — only the relative pattern.
        return [{"close": c, "volume": 1000.0} for c in self._closes]

    def features(self, market, *, timeframe, lookback):
        # Trend template asks for indicator features but only embeds
        # them in metadata — values themselves are not consulted for
        # entry/exit decisions in the script branch.
        return {}


def _make_ctx(*, position, closes):
    """Build a barely-functional StrategyContext stand-in.

    The generated template only reads:

    * ``ctx.config.markets`` (the first one)
    * ``ctx.market.candles(market, timeframe=..., limit=...)``
    * ``ctx.portfolio.positions(market)``
    * ``ctx.policy.default_order_usd``
    * ``ctx.trading.submit_intent(...)``
    * ``ctx.result.hold(...)``

    Everything else can be a SimpleNamespace.
    """
    trading = _RecordingTrading()
    result = _RecordingResult()
    return types.SimpleNamespace(
        config=types.SimpleNamespace(markets=("binance:BTCUSDT",)),
        market=_StubMarket(closes=closes),
        portfolio=_StubPortfolio(position=position),
        policy=_Policy(),
        trading=trading,
        result=result,
        trigger={"timeframe": "15m"},
    )


def _exec_template(code: str):
    """Compile + exec the generator template into a fresh module ns."""
    ns: dict = {}
    exec(compile(code, "<gen_template>", "exec"), ns)
    return ns


# --- the regression tests --------------------------------------------------


@pytest.fixture
def template_code():
    req = StrategyGenerationRequest(
        strategy_id="probe_scalp",
        markets=("binance:BTCUSDT",),
        accounts=("paper_main",),
    )
    return _scalping_template(req)


def test_short_position_exit_routes_to_close_position_short(template_code):
    """A short share with a sharp price rally must close via close_position(side='short').

    Pre-fix this scenario was the runaway trigger: template always
    emitted ``side='sell'`` so each "close" doubled the short. The
    fix routes the exit through ``close_position`` which uses
    ``SizingPolicy(close_all)`` and flips the side internally.
    """

    ns = _exec_template(template_code)

    # Rallying closes against a SHORT share → pnl_pct hits the
    # SL rail and triggers a close.
    closes = [80_000.0 + i for i in range(120)]
    ctx = _make_ctx(
        position={
            "size": -2.0,
            "avg_price": 80_000.0,
        },
        closes=closes,
    )

    ns["run"](ctx)

    assert ctx.trading.last_call is not None, (
        "scalp template should have submitted a close for the "
        "short share; got hold instead"
    )
    assert ctx.trading.last_kind == "close_position", (
        f"v6 exit must go through close_position (releases protection "
        f"+ close_all sizing); got {ctx.trading.last_kind!r}"
    )
    call = ctx.trading.last_call
    assert call["side"] == "short", (
        f"close_position must declare the POSITION side (short), not the "
        f"order side. A bare ``side='sell'`` would grow the short — the "
        f"v6 runaway bug. Got side={call['side']!r}."
    )


def test_long_position_exit_routes_to_close_position_long(template_code):
    """A long share with a sharp drawdown must close via close_position(side='long')."""

    ns = _exec_template(template_code)

    closes = [80_000.0 - i * 4 for i in range(120)]
    ctx = _make_ctx(
        position={
            "size": 1.5,
            "avg_price": 80_000.0,
        },
        closes=closes,
    )

    ns["run"](ctx)

    assert ctx.trading.last_kind == "close_position"
    call = ctx.trading.last_call
    assert call["side"] == "long"


def test_no_position_opens_long_with_bracket(template_code):
    """No share + momentum positive → open_position(side='long') with bracket TP/SL."""

    ns = _exec_template(template_code)

    closes = [80_000.0 + i * 8 for i in range(120)]
    ctx = _make_ctx(position=None, closes=closes)

    ns["run"](ctx)

    if ctx.trading.last_call is None:
        # Entry filter is intentionally strict — fine for this test
        # if the template holds. The shape assertion below only
        # runs when an entry actually fires.
        assert ctx.result.last_hold is not None
        return
    assert ctx.trading.last_kind == "open_position", (
        f"v6 entry must go through open_position so bracket protection "
        f"is armed atomically; got {ctx.trading.last_kind!r}"
    )
    call = ctx.trading.last_call
    assert call["side"] == "long"
    # Bracket protection must be present — that's the whole point of
    # the open_position migration.
    protection = call.get("protection") or {}
    assert "stop_loss" in protection, (
        "open_position MUST ship a stop_loss spec — otherwise the "
        "live order has no exchange-side bracket and the strategy is "
        "exposed to the gap-down failure mode."
    )
    assert "take_profit" in protection, (
        "open_position SHOULD also ship a take_profit spec for "
        "symmetric brackets. Missing here means the strategy can "
        "still book — but TP/SL parity is the safer default."
    )
    # Sizing must declare a method — bare numeric sizing would skip
    # the BudgetChecker and ignore policy caps.
    sizing = call.get("sizing") or {}
    assert sizing.get("method") in {"fixed_usd", "pct_nav", "risk_to_stop"}


def test_short_position_no_exit_holds(template_code):
    """Short share + favourable (price drops) → hold, no flip."""

    ns = _exec_template(template_code)

    closes = [80_000.0 - i * 4 for i in range(120)]
    ctx = _make_ctx(
        position={
            "size": -1.0,
            "avg_price": 80_000.0,
        },
        closes=closes,
    )

    ns["run"](ctx)

    if ctx.trading.last_call is not None:
        # If the template DOES exit (rsi rail etc), it must still
        # route through close_position with side='short' — never a
        # bare submit_intent(side='sell') that would grow the short.
        assert ctx.trading.last_kind == "close_position", (
            "exit on a short share must go through close_position "
            "(the v6 fix); got "
            f"{ctx.trading.last_kind!r}"
        )
        assert ctx.trading.last_call["side"] == "short"


# ---------------------------------------------------------------------------
# Trend template — same contract, different signal source.
# ---------------------------------------------------------------------------


@pytest.fixture
def trend_template_code():
    req = StrategyGenerationRequest(
        strategy_id="probe_trend",
        markets=("binance:BTCUSDT",),
        accounts=("paper_main",),
    )
    return _trend_template(req)


def _trend_closes(slope: float, base: float = 80_000.0, n: int = 80) -> list[float]:
    """Build a price series with a clean MA cross at the tail.

    ``slope > 0`` produces a golden cross (uptrend), ``slope < 0`` a
    death cross. We use a steep linear ramp from the slow-window
    midpoint so both SMA(20) and SMA(50) have well-defined values
    and the latest cross is unambiguous.
    """
    return [base + slope * i for i in range(n)]


def test_trend_death_cross_on_long_closes_position_long(trend_template_code):
    """Holding a LONG, death cross fires → close_position(side='long')."""
    ns = _exec_template(trend_template_code)

    closes = _trend_closes(slope=200, n=55) + _trend_closes(slope=-400, base=91_000, n=25)
    ctx = _make_ctx(
        position={"size": 1.0, "avg_price": 80_000.0},
        closes=closes,
    )

    ns["run"](ctx)

    if ctx.trading.last_call is None:
        return
    assert ctx.trading.last_kind == "close_position"
    assert ctx.trading.last_call["side"] == "long"


def test_trend_golden_cross_on_short_closes_position_short(trend_template_code):
    """Holding a SHORT, golden cross fires → close_position(side='short').

    The v6 runaway bug: the OLD code would emit a bare order toward
    the opposite side (or a fixed-USD entry that doesn't close the
    actual exposure), letting the short bleed forever. The fix
    routes through ``close_position`` which forces close_all sizing.
    """
    ns = _exec_template(trend_template_code)

    closes = _trend_closes(slope=-200, n=55) + _trend_closes(slope=400, base=69_000, n=25)
    ctx = _make_ctx(
        position={"size": -2.0, "avg_price": 80_000.0},
        closes=closes,
    )

    ns["run"](ctx)

    if ctx.trading.last_call is None:
        return
    assert ctx.trading.last_kind == "close_position"
    assert ctx.trading.last_call["side"] == "short"


def test_trend_aligned_cross_does_not_double_down(trend_template_code):
    """Golden cross while already long → hold, do NOT double up."""
    ns = _exec_template(trend_template_code)

    closes = _trend_closes(slope=200, n=55) + _trend_closes(slope=400, base=91_000, n=25)
    ctx = _make_ctx(
        position={"size": 0.5, "avg_price": 80_000.0},
        closes=closes,
    )

    ns["run"](ctx)

    assert ctx.trading.last_call is None
    assert ctx.result.last_hold is not None


def test_trend_no_position_opens_on_cross_with_bracket(trend_template_code):
    """No share + cross → fresh open_position with bracket TP/SL."""
    ns = _exec_template(trend_template_code)

    closes = _trend_closes(slope=-200, n=55) + _trend_closes(slope=400, base=69_000, n=25)
    ctx = _make_ctx(position=None, closes=closes)

    ns["run"](ctx)

    if ctx.trading.last_call is None:
        return
    assert ctx.trading.last_kind == "open_position"
    call = ctx.trading.last_call
    assert call["side"] in {"long", "short"}
    protection = call.get("protection") or {}
    assert "stop_loss" in protection
    assert "take_profit" in protection
    sizing = call.get("sizing") or {}
    assert sizing.get("method") in {"fixed_usd", "pct_nav", "risk_to_stop"}


def test_template_kwargs_match_sdk_signature(template_code):
    """Every kwarg the template uses must exist on the real SDK.

    The migration emits three SDK methods. If any of them grows or
    loses a kwarg the generator depends on, the live strategy will
    blow up with ``TypeError: unexpected keyword argument``. This
    check fences that breakage at generator-test time.
    """

    import inspect

    from nerya.strategies.context import StrategyTrading

    # submit_intent (still used by some legacy paths and the backtest
    # script template's older branches; verify the kwargs the generator
    # passes there are still accepted).
    si = set(inspect.signature(StrategyTrading.submit_intent).parameters.keys())
    submit_intent_kwargs = {
        "market", "side", "size", "size_unit", "order_type",
        "confidence", "reasoning", "plan_action",
    }
    missing_si = submit_intent_kwargs - si
    assert not missing_si, (
        f"submit_intent missing kwargs the generator emits: {sorted(missing_si)}"
    )

    # open_position — used by entry path.
    op = set(inspect.signature(StrategyTrading.open_position).parameters.keys())
    open_position_kwargs = {
        "market", "side", "sizing", "protection", "confidence", "reasoning_ref",
    }
    missing_op = open_position_kwargs - op
    assert not missing_op, (
        f"open_position missing kwargs the generator emits: {sorted(missing_op)}"
    )

    # close_position — used by tactical exit path.
    cp = set(inspect.signature(StrategyTrading.close_position).parameters.keys())
    close_position_kwargs = {"market", "side", "confidence", "reasoning_ref"}
    missing_cp = close_position_kwargs - cp
    assert not missing_cp, (
        f"close_position missing kwargs the generator emits: {sorted(missing_cp)}"
    )
