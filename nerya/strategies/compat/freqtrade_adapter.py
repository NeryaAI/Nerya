"""Freqtrade adapter — run an ``IStrategy`` subclass from Nerya ticks.

Mapping
-------
Freqtrade's engine owns candles → dataframe → signals → orders. On
Nerya the adapter plays that role *per tick* against a single market:

1. ``ctx.market.candles(market, timeframe=strategy.timeframe, ...)``
   becomes a pandas DataFrame with Freqtrade's column conventions
   (``date`` tz-aware UTC, ``open``/``high``/``low``/``close``/``volume``).
   In backtest replay the mock context serves candles *up to the
   current bar*, so the dataframe pipeline is naturally leak-free.
2. ``populate_indicators`` → ``populate_entry_trend`` →
   ``populate_exit_trend`` (legacy ``populate_buy_trend`` /
   ``populate_sell_trend`` accepted) run exactly as authored.
3. The last dataframe row's ``enter_long`` / ``exit_long`` (and
   ``enter_short`` / ``exit_short`` when ``can_short``) are the
   signals.
4. Signals map onto intents through a Nerya-side tracked position
   (``ctx.state``): flat + ``enter_long`` → buy; long + ``exit_long``
   → sell (close); short likewise when ``can_short``.
5. Engine-managed exits are emulated in priority order: minimal_roi →
   stoploss → trailing stop → exit signal → ``custom_exit``.
   ``custom_stoploss`` follows real Freqtrade semantics: the hook
   returns a stoploss relative to the *current* rate and the adapter
   ratchets the effective stop price up (long) / down (short) — never
   the other way. Trailing stops trail at ``abs(stoploss)`` distance
   from the high-water rate until
   ``trailing_stop_positive_offset`` is reached, then switch to
   ``trailing_stop_positive``. ``custom_exit`` /
   ``confirm_trade_entry`` / ``confirm_trade_exit`` hooks are invoked
   with the duck-typed :class:`Trade` shim.
6. Only **filled** envelopes update the tracked position.
   ``pending_approval`` (the normal case when the Approval Gate holds
   an order on canary/live) records a pending marker and leaves the
   previous position untouched; the marker is resolved (dropped with
   a warning journal entry — the strategy context has no execution
   status API) before new signals are evaluated on the next tick.
   ``rejected`` / ``failed`` / ``canceled`` / ``shadow`` are no-ops.

Position state lives under ``ctx.state`` keys ``_compat_pos`` /
``_compat_meta`` so the same package runs identically in live paper
runs and backtest replay, and survives process restarts (paper/live).

Unsupported engine features (informative pairs, futures funding, pair
locks, order timeouts) are out of scope; the importer warns when the
uploaded source references them.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

import pandas as pd

from .detect import FREQTRADE
from .shims.freqtrade_shim import Trade

_LOG = logging.getLogger(__name__)

_POS_KEY = "_compat_pos"
_META_KEY = "_compat_ft_meta"

_TICK_MS = 60 * 1000


class FreqtradeAdapterError(Exception):
    """Raised when the foreign strategy cannot be executed as authored."""


def run_freqtrade_tick(
    ctx: Any,
    strategy: Any,
    *,
    market: str,
    settings: dict[str, Any],
) -> Any:
    """Run one tick of a Freqtrade ``IStrategy``; returns a StrategyResult.

    ``settings`` comes from the package manifest's ``extras.framework``
    block: ``stake_amount`` (USD notional per entry, default from
    policy), ``order_type`` (``market``|``limit``) and ``tag``.
    """


    stake_amount = float(settings.get("stake_amount") or 0.0)
    if stake_amount <= 0:
        stake_amount = float(getattr(ctx.policy, "default_order_usd", 0.0) or 100.0)
    order_type = str(settings.get("order_type") or "market").strip().lower()
    tag = str(settings.get("tag") or "freqtrade_compat")

    # Explicit manifest timeframe (extras.framework.timeframe, merged
    # into settings by the entrypoint) overrides the class attribute.
    timeframe = str(settings.get("timeframe") or getattr(strategy, "timeframe", "") or "5m")
    startup = max(int(getattr(strategy, "startup_candle_count", 30) or 30), 30)
    limit = min(startup * 2 + 60, 990)

    rows = _fetch_candles(ctx, market, timeframe=timeframe, limit=limit)
    if not rows:
        return ctx.result.hold(reason="freqtrade_compat_no_candles")
    dataframe = _rows_to_dataframe(rows)

    pos = _read_position(ctx, market)
    # Resolve a pending_approval marker from the previous tick before
    # evaluating exits or new signals (the marker itself is dropped —
    # the position it refers to was never written).
    pos, pending_cleared = _resolve_pending(ctx, market, pos)
    if pending_cleared:
        _write_position(ctx, market, pos)
    meta = _read_meta(ctx)
    last_ts = int(dataframe["date"].iloc[-1].timestamp() * 1000)

    # Engine-managed exit checks run every tick (intra-candle price may
    # have moved even when the candle timestamp has not).
    close_price = float(dataframe["close"].iloc[-1])
    now = dataframe["date"].iloc[-1].to_pydatetime()
    exit_result = _engine_exit_checks(
        ctx, strategy, market, settings, pos, close_price, now, stake_amount, order_type, tag
    )
    if exit_result is not None:
        return exit_result

    # Signals only recompute when we are flat or a fresh candle arrived —
    # same trade-off Freqtrade makes with ``process_only_new_candles``.
    signals = meta.get("last_signals") or {}
    if (
        pos.get("side") == "flat"
        or int(signals.get("ts") or 0) != last_ts
    ):
        dataframe = _populate(strategy, dataframe, market)
        last_row = dataframe.iloc[-1]
        signals = {
            "ts": last_ts,
            "enter_long": _signal_flag(last_row.get("enter_long", last_row.get("buy", 0))),
            "exit_long": _signal_flag(last_row.get("exit_long", last_row.get("sell", 0))),
            "enter_short": _signal_flag(last_row.get("enter_short", 0)),
            "exit_short": _signal_flag(last_row.get("exit_short", 0)),
            "enter_tag": str(last_row.get("enter_tag") or "") or None,
        }
        meta["last_ts"] = last_ts
        meta["last_signals"] = signals
        _write_meta(ctx, meta)

    return _apply_signals(
        ctx,
        strategy,
        market,
        settings=settings,
        signals=signals,
        close_price=close_price,
        candle_time=now,
        stake_amount=stake_amount,
        order_type=order_type,
        tag=tag,
    )


# ---------------------------------------------------------------------------
# Dataframe pipeline
# ---------------------------------------------------------------------------


def _populate(strategy: Any, dataframe: pd.DataFrame, market: str) -> pd.DataFrame:
    metadata = {"pair": market}
    dataframe = strategy.populate_indicators(dataframe, metadata)
    entry_method = _entry_method(strategy)
    exit_method = _exit_method(strategy)
    dataframe = entry_method(dataframe, metadata)
    dataframe = exit_method(dataframe, metadata)
    return dataframe


def _entry_method(strategy: Any) -> Any:
    if "populate_entry_trend" in type(strategy).__dict__:
        return strategy.populate_entry_trend
    if "populate_buy_trend" in type(strategy).__dict__:
        return strategy.populate_buy_trend
    return strategy.populate_entry_trend


def _exit_method(strategy: Any) -> Any:
    if "populate_exit_trend" in type(strategy).__dict__:
        return strategy.populate_exit_trend
    if "populate_sell_trend" in type(strategy).__dict__:
        return strategy.populate_sell_trend
    return strategy.populate_exit_trend


def _signal_flag(value: Any) -> int:
    try:
        if value is None or pd.isna(value):
            return 0
        return 1 if float(value) != 0.0 else 0
    except (TypeError, ValueError):
        return 0


def _rows_to_dataframe(rows: list[dict[str, Any]]) -> pd.DataFrame:
    records = []
    for row in rows:
        ts = _ts_ms(row)
        if ts <= 0:
            continue
        records.append(
            {
                "date": datetime.fromtimestamp(ts / 1000.0, tz=timezone.utc),
                "open": float(row.get("open") or 0.0),
                "high": float(row.get("high") or 0.0),
                "low": float(row.get("low") or 0.0),
                "close": float(row.get("close") or 0.0),
                "volume": float(row.get("volume") or 0.0),
            }
        )
    if not records:
        raise FreqtradeAdapterError("candle rows produced an empty dataframe")
    return pd.DataFrame(records)


def _ts_ms(row: dict[str, Any]) -> int:
    """Normalise a candle row timestamp to epoch milliseconds.

    Nerya candle rows carry ``ts`` in seconds (``normalize_klines`` and
    ``mock_candles``), but some providers and stored artifacts emit
    milliseconds — accept both.
    """

    try:
        ts = int(row.get("ts") or row.get("time") or 0)
    except (TypeError, ValueError):
        return 0
    if ts <= 0:
        return 0
    return ts if ts > 1_000_000_000_000 else ts * 1000


def _fetch_candles(ctx: Any, market: str, *, timeframe: str, limit: int) -> list[dict[str, Any]]:
    try:
        return list(ctx.market.candles(market, timeframe=timeframe, limit=limit) or [])
    except Exception as exc:  # connector failures → hold, not crash
        _LOG.warning("freqtrade adapter candles failed: %s", exc)
        return []


# ---------------------------------------------------------------------------
# Position state
# ---------------------------------------------------------------------------


def _read_position(ctx: Any, market: str) -> dict[str, Any]:
    positions = dict(ctx.state.get(_POS_KEY, {}) or {})
    pos = dict(positions.get(market) or {})
    pos.setdefault("side", "flat")
    pos.setdefault("qty", 0.0)
    pos.setdefault("entry_price", 0.0)
    pos.setdefault("entry_ts_ms", 0)
    pos.setdefault("stake_amount", 0.0)
    pos.setdefault("high_water_profit", 0.0)
    pos.setdefault("stop_price", None)
    return pos


def _write_position(ctx: Any, market: str, pos: dict[str, Any]) -> None:
    positions = dict(ctx.state.get(_POS_KEY, {}) or {})
    positions[market] = pos
    ctx.state.set(_POS_KEY, positions)


def _drop_pending_marker(ctx: Any, market: str, marker: dict[str, Any]) -> None:
    """Journal the drop of an unresolved ``pending_approval`` marker.

    The strategy context exposes no execution-status polling API, so a
    marker that survives into the next tick cannot be resolved here —
    leave a warning trail and move on with the position untouched.
    """

    audit = getattr(ctx, "audit", None)
    payload = {"market": market, "marker": marker}
    if audit is None:
        _LOG.warning("freqtrade compat pending_approval marker dropped: %s", payload)
        return
    try:
        audit.log(
            "compat_pending_dropped",
            {
                **payload,
                "note": "no execution-status API on ctx; position left untouched",
            },
            level="warn",
        )
    except Exception:
        _LOG.exception("failed to journal dropped compat pending marker")


def _resolve_pending(ctx: Any, market: str, pos: dict[str, Any]) -> "tuple[dict[str, Any], bool]":
    """Resolve a pending_approval marker carried over from last tick."""

    marker = pos.get("pending_intent")
    if not marker:
        return pos, False
    _drop_pending_marker(ctx, market, marker)
    return {**pos, "pending_intent": None}, True


def _read_meta(ctx: Any) -> dict[str, Any]:
    return dict(ctx.state.get(_META_KEY, {}) or {})


def _write_meta(ctx: Any, meta: dict[str, Any]) -> None:
    ctx.state.set(_META_KEY, meta)


# ---------------------------------------------------------------------------
# Signal → intent application
# ---------------------------------------------------------------------------


def _apply_signals(
    ctx: Any,
    strategy: Any,
    market: str,
    *,
    settings: dict[str, Any],
    signals: dict[str, Any],
    close_price: float,
    candle_time: datetime,
    stake_amount: float,
    order_type: str,
    tag: str,
) -> Any:
    pos = _read_position(ctx, market)
    side = pos.get("side") or "flat"

    # Exits take priority over entries in the same candle.
    if side == "long" and signals.get("exit_long"):
        return _close_position(
            ctx, strategy, market, pos=pos, price=close_price, when=candle_time,
            reason="exit_signal", order_type=order_type, tag=tag,
        )
    if side == "short" and signals.get("exit_short"):
        return _close_position(
            ctx, strategy, market, pos=pos, price=close_price, when=candle_time,
            reason="exit_signal", order_type=order_type, tag=tag,
        )

    if side == "flat":
        want_short = bool(getattr(strategy, "can_short", False)) and signals.get("enter_short")
        want_long = signals.get("enter_long")
        if want_long and want_short:
            # Conflicting signals in one candle → treat as no-setup.
            return ctx.result.hold(reason="freqtrade_compat_conflicting_signals")
        if want_long or want_short:
            return _open_position(
                ctx, strategy, market,
                direction="short" if want_short else "long",
                price=close_price, when=candle_time,
                stake_amount=stake_amount, order_type=order_type, tag=tag,
                enter_tag=signals.get("enter_tag"),
            )

    reason = "freqtrade_compat_no_signal" if side == "flat" else "freqtrade_compat_holding"
    return ctx.result.hold(
        reason=reason,
        metadata={
            "compat": FREQTRADE,
            "signals": {k: v for k, v in signals.items() if k != "ts"},
            "position": {"side": side},
        },
    )


def _open_position(
    ctx: Any,
    strategy: Any,
    market: str,
    *,
    direction: str,
    price: float,
    when: datetime,
    stake_amount: float,
    order_type: str,
    tag: str,
    enter_tag: str | None = None,
) -> Any:
    from ..result import StrategyResult

    intent_side = "buy" if direction == "long" else "sell"
    trade_shim = Trade(
        pair=market,
        open_rate=price,
        amount=stake_amount / price if price else 0.0,
        stake_amount=stake_amount,
        is_short=direction == "short",
        open_date=when,
    )
    if hasattr(strategy, "confirm_trade_entry"):
        allowed = strategy.confirm_trade_entry(
            pair=market,
            order_type=order_type,
            amount=trade_shim.amount,
            rate=price,
            time_in_force="gtc",
            current_time=when,
            entry_tag=enter_tag,
            side="long" if direction == "long" else "short",
        )
        if not allowed:
            return ctx.result.hold(reason="freqtrade_compat_entry_rejected_by_hook")

    envelope = _submit(
        ctx,
        market=market,
        side=intent_side,
        size=stake_amount,
        size_unit="usd",
        order_type=order_type,
        price=price if order_type == "limit" else None,
        when=when,
        tag=tag,
        plan_action="open_position",
        reasoning=f"freqtrade entry signal ({direction})",
        enter_tag=enter_tag,
    )
    status = str(envelope.get("status") or "rejected")
    if status == "rejected":
        return StrategyResult.from_trade_envelope(
            envelope,
            reason="freqtrade_compat_entry_rejected",
            metadata={"compat": FREQTRADE, "direction": direction},
        )
    if status == "filled":
        # Adopt the executor's real fill (order summary carries the
        # authoritative filled size / average price — partial fills and
        # fill-price drift make the signal-price estimate wrong, and the
        # tracked qty must match the PositionBook or the eventual exit
        # over/under-closes). Fall back to the computed estimate when the
        # envelope carries no order summary (scripted tests, legacy
        # bridges).
        order = envelope.get("order") or {}
        filled_size = _positive_float(order.get("filled_size"))
        avg_price = _positive_float(order.get("avg_price"))
        pos = {
            "side": direction,
            "qty": filled_size if filled_size is not None else trade_shim.amount,
            "entry_price": avg_price if avg_price is not None else price,
            "entry_ts_ms": int(when.timestamp() * 1000),
            "stake_amount": stake_amount,
            "high_water_profit": 0.0,
            "stop_price": None,
        }
        _write_position(ctx, market, pos)
        return StrategyResult.from_trade_envelope(
            envelope,
            reason=f"freqtrade_compat_{direction}_entry",
            metadata={"compat": FREQTRADE, "direction": direction, "enter_tag": enter_tag},
        )
    if status == "pending_approval":
        # Approval Gate holds the order — the normal case on
        # canary/live. Record a pending marker and leave the previous
        # position untouched; the next tick resolves the marker before
        # evaluating new signals (no phantom position).
        current = _read_position(ctx, market)
        current["pending_intent"] = {
            "kind": "entry",
            "direction": direction,
            "size": stake_amount,
            "price": price,
            "approval_id": envelope.get("approval_id"),
            "submitted_at": when.isoformat(),
        }
        _write_position(ctx, market, current)
        return StrategyResult.from_trade_envelope(
            envelope,
            reason=f"freqtrade_compat_{direction}_entry_pending_approval",
            metadata={"compat": FREQTRADE, "direction": direction},
        )
    # shadow / failed / canceled → journalled by the kernel, no position.
    return StrategyResult.from_trade_envelope(
        envelope,
        reason=f"freqtrade_compat_entry_{status}",
        metadata={"compat": FREQTRADE, "direction": direction},
    )


def _close_position(
    ctx: Any,
    strategy: Any,
    market: str,
    *,
    pos: dict[str, Any],
    price: float,
    when: datetime,
    reason: str,
    order_type: str,
    tag: str,
) -> Any:
    from ..result import StrategyResult

    direction = str(pos.get("side") or "long")
    intent_side = "sell" if direction == "long" else "buy"
    entry_price = float(pos.get("entry_price") or 0.0)
    stake = float(pos.get("stake_amount") or 0.0)

    profit = _profit_ratio(entry_price, price, direction)
    trade_shim = Trade(
        pair=market,
        open_rate=entry_price,
        amount=float(pos.get("qty") or 0.0),
        stake_amount=stake,
        is_short=direction == "short",
        open_date=datetime.fromtimestamp(float(pos.get("entry_ts_ms") or 0) / 1000.0, tz=timezone.utc),
    )
    if hasattr(strategy, "confirm_trade_exit"):
        allowed = strategy.confirm_trade_exit(
            pair=market,
            trade=trade_shim,
            order_type=order_type,
            amount=trade_shim.amount,
            rate=price,
            time_in_force="gtc",
            current_time=when,
            exit_reason=reason,
        )
        if not allowed:
            return ctx.result.hold(reason="freqtrade_compat_exit_rejected_by_hook")

    # Exits must match the *held base amount*: submitting the
    # entry-time stake (USD) under/overshoots after price moves, and an
    # overshoot gets the reduce-only order rejected — the stop would
    # never execute. Worse, PositionBook reads an over-close as
    # reduce+flip, so an oversized exit would open a phantom reverse
    # share. Clamp the submitted size to the qty we are about to
    # decrement, and never fall back to notional: with unknown holdings
    # a notional sell can exceed the book (or open a short from flat),
    # so hold instead.
    held_qty = abs(float(pos.get("qty") or 0.0))
    if held_qty <= 0:
        return ctx.result.hold(
            reason="freqtrade_compat_exit_no_holding",
            metadata={"compat": FREQTRADE, "exit_reason": reason},
        )
    size, size_unit = held_qty, "base"

    envelope = _submit(
        ctx,
        market=market,
        side=intent_side,
        size=size,
        size_unit=size_unit,
        order_type=order_type,
        price=price if order_type == "limit" else None,
        when=when,
        tag=tag,
        plan_action="close_position",
        reasoning=f"freqtrade exit: {reason} (profit={profit:.4f})",
        enter_tag=None,
    )
    status = str(envelope.get("status") or "rejected")
    if status == "rejected":
        return StrategyResult.from_trade_envelope(
            envelope,
            reason="freqtrade_compat_exit_rejected",
            metadata={"compat": FREQTRADE, "exit_reason": reason},
        )
    if status == "filled":
        # Prefer the authoritative PositionBook share the executor
        # reports: a full close flattens, but a partial close (venue
        # filled less than requested) leaves a tracked residual so the
        # exit re-fires for the remainder instead of assuming flat.
        order = envelope.get("order") or {}
        filled = _positive_float(order.get("filled_size"))
        residual = held_qty - filled if filled is not None else 0.0
        if filled is not None and residual > max(1e-12, held_qty * 1e-6):
            _write_position(
                ctx,
                market,
                {**pos, "qty": residual, "high_water_profit": 0.0, "stop_price": None},
            )
        else:
            _write_position(
                ctx,
                market,
                {**pos, "side": "flat", "qty": 0.0, "high_water_profit": 0.0, "stop_price": None},
            )
        return StrategyResult.from_trade_envelope(
            envelope,
            reason=f"freqtrade_compat_exit_{reason}",
            metadata={"compat": FREQTRADE, "exit_reason": reason, "profit_ratio": round(profit, 6)},
        )
    if status == "pending_approval":
        # Approval Gate holds the exit — keep the position so the exit
        # checks re-fire next tick; only mark the pending request.
        current = _read_position(ctx, market)
        current["pending_intent"] = {
            "kind": "exit",
            "exit_reason": reason,
            "qty": held_qty,
            "price": price,
            "approval_id": envelope.get("approval_id"),
            "submitted_at": when.isoformat(),
        }
        _write_position(ctx, market, current)
        return StrategyResult.from_trade_envelope(
            envelope,
            reason=f"freqtrade_compat_exit_{reason}_pending_approval",
            metadata={"compat": FREQTRADE, "exit_reason": reason},
        )
    # shadow / failed / canceled → no position change.
    return StrategyResult.from_trade_envelope(
        envelope,
        reason=f"freqtrade_compat_exit_{reason}_{status}",
        metadata={"compat": FREQTRADE, "exit_reason": reason},
    )


def _submit(
    ctx: Any,
    *,
    market: str,
    side: str,
    size: float,
    size_unit: str,
    order_type: str,
    price: float | None,
    when: datetime,
    tag: str,
    plan_action: str | None,
    reasoning: str,
    enter_tag: str | None,
) -> dict[str, Any]:
    kwargs: dict[str, Any] = {
        "market": market,
        "side": side,
        "size": float(size),
        "size_unit": size_unit,
        "order_type": order_type,
        "confidence": None,
        "reasoning": reasoning,
        "metadata": {
            "compat": FREQTRADE,
            "compat_signal_time": when.isoformat(),
        },
        "plan_action": plan_action,
    }
    if price is not None:
        kwargs["limit_price"] = float(price)
    if enter_tag:
        kwargs["metadata"]["enter_tag"] = enter_tag
    return dict(ctx.trading.submit_intent(**kwargs) or {})


# ---------------------------------------------------------------------------
# Engine-managed exits: ROI → stoploss → trailing → custom_exit
# ---------------------------------------------------------------------------


def _engine_exit_checks(
    ctx: Any,
    strategy: Any,
    market: str,
    settings: dict[str, Any],
    pos: dict[str, Any],
    price: float,
    when: datetime,
    stake_amount: float,
    order_type: str,
    tag: str,
) -> Any:
    side = str(pos.get("side") or "flat")
    if side == "flat" or not price:
        return None

    entry_price = float(pos.get("entry_price") or 0.0)
    entry_ts_ms = int(pos.get("entry_ts_ms") or 0)
    profit = _profit_ratio(entry_price, price, side)

    # 1. minimal_roi — keys are elapsed minutes, values are min profit.
    roi_reason = _minimal_roi_hit(strategy, profit, entry_ts_ms, when)
    if roi_reason:
        return _close_position(
            ctx, strategy, market, pos=pos, price=price, when=when,
            reason=roi_reason, order_type=order_type, tag=tag,
        )

    # 2. stoploss — the effective stop is a persisted *price level*
    # (``_compat_pos["stop_price"]``) ratcheted in the trade's favour:
    # never down for longs, never up for shorts. It is seeded from the
    # static ``stoploss`` ratio, and a ``custom_stoploss`` hook moves
    # it relative to the *current* rate — real Freqtrade semantics
    # (the engine ratchets ``trade.stop_loss`` up as price rises).
    stoploss = float(getattr(strategy, "stoploss", -0.99) or -0.99)
    stop_price = pos.get("stop_price")
    if not stop_price and entry_price > 0:
        stop_price = _initial_stop_price(entry_price, stoploss, side)
    if hasattr(type(strategy), "custom_stoploss") and "custom_stoploss" in type(strategy).__dict__:
        try:
            hooked = strategy.custom_stoploss(
                pair=market,
                trade=_trade_from_pos(pos, market),
                current_time=when,
                current_rate=price,
                current_profit=profit,
                after_fill=False,
            )
        except Exception as exc:
            _LOG.warning("custom_stoploss failed: %s", exc)
            hooked = None
        if hooked is not None and price > 0:
            hooked = float(hooked)
            if side == "short":
                candidate = price * (1.0 - hooked)
                stop_price = min(stop_price, candidate) if stop_price else candidate
            else:
                candidate = price * (1.0 + hooked)
                stop_price = max(stop_price, candidate) if stop_price else candidate
    if stop_price is not None:
        pos["stop_price"] = stop_price
        stop_hit = price >= stop_price if side == "short" else price <= stop_price
    else:
        # No seedable stop (zero entry price) — legacy ratio check.
        stop_hit = profit <= stoploss
    if stop_hit:
        return _close_position(
            ctx, strategy, market, pos=pos, price=price, when=when,
            reason="stoploss", order_type=order_type, tag=tag,
        )

    # 3. trailing stop — trails the high-water *rate*: at the static
    # ``abs(stoploss)`` distance until the positive offset is reached,
    # then at ``trailing_stop_positive`` distance.
    trailing = _trailing_stop_hit(strategy, pos, profit, stoploss)
    if trailing:
        return _close_position(
            ctx, strategy, market, pos=pos, price=price, when=when,
            reason="trailing_stop", order_type=order_type, tag=tag,
        )

    # 4. custom_exit hook.
    if "custom_exit" in type(strategy).__dict__:
        try:
            verdict = strategy.custom_exit(
                pair=market,
                trade=_trade_from_pos(pos, market),
                current_time=when,
                current_rate=price,
                current_profit=profit,
            )
        except Exception as exc:
            _LOG.warning("custom_exit failed: %s", exc)
            verdict = None
        if verdict:
            return _close_position(
                ctx, strategy, market, pos=pos, price=price, when=when,
                reason=str(verdict), order_type=order_type, tag=tag,
            )

    # Track the high-water profit for trailing-stop evaluation.
    pos["high_water_profit"] = max(float(pos.get("high_water_profit") or 0.0), profit)
    _write_position(ctx, market, pos)
    return None


def _minimal_roi_hit(strategy: Any, profit: float, entry_ts_ms: int, when: datetime) -> str | None:
    table = dict(getattr(strategy, "minimal_roi", {}) or {})
    if not table:
        return None
    if entry_ts_ms <= 0:
        elapsed_minutes = 10_000.0  # unknown entry age → apply the loosest tier
    else:
        elapsed_minutes = max((when.timestamp() * 1000.0 - entry_ts_ms) / _TICK_MS, 0.0)
    thresholds = sorted(
        ((float(minutes), float(ratio)) for minutes, ratio in table.items()),
        key=lambda kv: kv[0],
    )
    for minutes, ratio in thresholds:
        if elapsed_minutes >= minutes and profit >= ratio:
            return "roi" if minutes == 0 else f"roi_{int(minutes)}m"
    return None


def _trailing_stop_hit(strategy: Any, pos: dict[str, Any], profit: float, stoploss: float) -> bool:
    """Real Freqtrade trailing semantics, evaluated per tick.

    * With ``trailing_stop_positive=X`` + ``trailing_stop_positive_offset=Y``:
      below the offset the stop trails at the static ``abs(stoploss)``
      distance from the high-water rate (unless
      ``trailing_only_offset_is_reached`` — then it does not trail at
      all); at or above the offset it switches to distance X.
    * With only ``trailing_stop=True``: trail at ``abs(stoploss)``
      distance from the high-water rate.
    """

    if not getattr(strategy, "trailing_stop", False):
        return False
    positive = getattr(strategy, "trailing_stop_positive", None)
    offset = getattr(strategy, "trailing_stop_positive_offset", None)
    only_offset = bool(getattr(strategy, "trailing_only_offset_is_reached", False))
    high_water = float(pos.get("high_water_profit") or 0.0)
    high_water = max(high_water, profit)

    if positive is not None and offset is not None:
        if high_water >= float(offset):
            distance = float(positive)
        elif only_offset:
            return False
        else:
            distance = abs(stoploss)
    elif positive is not None:
        # No offset configured: trailing arms once profit reaches `positive`.
        if high_water < float(positive):
            return False
        distance = float(positive)
    else:
        distance = abs(stoploss)

    return high_water > 0 and profit <= high_water - distance


def _initial_stop_price(entry_price: float, stoploss: float, side: str) -> float | None:
    """Seed the persisted stop level from the static ratio at entry."""

    if entry_price <= 0:
        return None
    if side == "short":
        return entry_price * (1.0 - stoploss)
    return entry_price * (1.0 + stoploss)


def _trade_from_pos(pos: dict[str, Any], market: str) -> Trade:
    return Trade(
        pair=market,
        open_rate=float(pos.get("entry_price") or 0.0),
        amount=float(pos.get("qty") or 0.0),
        stake_amount=float(pos.get("stake_amount") or 0.0),
        is_short=str(pos.get("side")) == "short",
        open_date=datetime.fromtimestamp(
            float(pos.get("entry_ts_ms") or 0) / 1000.0 or 0, tz=timezone.utc
        ),
    )


def _positive_float(value: Any) -> float | None:
    """Coerce to a strictly positive float, else ``None`` (absent field)."""

    try:
        num = float(value)
    except (TypeError, ValueError):
        return None
    return num if num > 0.0 else None


def _profit_ratio(entry_price: float, price: float, side: str) -> float:
    if not entry_price or not price:
        return 0.0
    if side == "short":
        return (entry_price - price) / entry_price
    return (price - entry_price) / entry_price


__all__ = ["FreqtradeAdapterError", "run_freqtrade_tick"]
