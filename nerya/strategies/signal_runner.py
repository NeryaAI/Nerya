"""Finite, market-scoped signal execution, shared by authored script strategies.

The callable provides signals only; this component owns timestamp handling,
per-market dedupe, settled-position reads and SDK submissions. It is not an
Agent Loop, optimizer, scheduler or alternate trading/risk implementation.
"""
from __future__ import annotations
from collections.abc import Callable, Mapping
from typing import Any
from inspect import signature

from .candle_view import candle_snapshots
from .result import StrategyResult, StrategyResultStatus


def _last_signal(value: Any, name: str) -> bool:
    if isinstance(value, (list, tuple)):
        if not value:
            return False
        value = value[-1]
    if type(value) is not bool:
        raise ValueError(f"signal {name} must be a boolean or a boolean series")
    return value


def indicator_parameters(signal, parameters):
    """Validate callable settings before reading candles or submitting orders.

    A matching optional name is configuration metadata, not a numerical
    argument. A different name or unknown parameter is rejected, never ignored.
    """
    if not isinstance(parameters, dict):
        raise ValueError("params.indicator must be a mapping")
    values = dict(parameters)
    declared = values.pop("name", None)
    actual = getattr(signal, "__name__", "")
    if declared is not None and (not isinstance(declared, str) or declared.casefold() != actual.casefold()):
        raise ValueError(f"indicator name {declared!r} does not match the configured callable {actual!r}")
    try:
        signature(signal).bind([], **values)
    except TypeError as exc:
        raise ValueError(f"invalid parameters for {actual}: {exc}") from exc
    return values


def minimum_closed_history(parameters: dict[str, Any]) -> int | None:
    """Return the minimum closed-bar window for a fast/slow crossover."""
    if not isinstance(parameters, dict) or not {"fast_period", "slow_period"} <= parameters.keys():
        return None
    periods = [parameters[name] for name in ("fast_period", "slow_period")]
    if any(type(period) is not int or period < 1 for period in periods):
        raise ValueError("EMA periods must be positive integers")
    return max(periods) + 1


def run_signal_strategy(ctx, signal: Callable[..., Mapping[str, Any]], *,
                        parameters: dict[str, Any] | None = None,
                        sizing: dict[str, Any] | None = None,
                        protection: dict[str, Any] | None = None,
                        lookback: int | None = None,
                        confidence: float | None = None) -> StrategyResult:
    """Long/flat spot execution; configuration is read from ``ctx.config.params``.

    params.indicator goes unchanged to the signal callable. params.sizing and
    params.protection go unchanged to the public trading SDK. This routine never
    chooses markets, alters risk parameters, launches a model or runs a backtest.
    Candle callbacks process exactly their market; timers process the universe.
    It raises on bad state/data/SDK contracts, instead of returning fake no-signal.
    """
    config = ctx.config.params
    parameters = parameters if parameters is not None else config.get("indicator", {})
    parameters = indicator_parameters(signal, parameters)
    sizing = sizing if sizing is not None else config.get("sizing", {"method":"fixed_usd", "fixed_usd":ctx.policy.default_order_usd})
    protection = protection if protection is not None else config.get("protection")
    lookback = lookback if lookback is not None else config.get("lookback", 200)
    if type(lookback) is not int or lookback < 2:
        raise ValueError("lookback must be an integer of at least two candles")
    confidence = confidence if confidence is not None else config.get("confidence", 0.8)
    timeframe = ctx.config.timeframe or ctx.trigger.get("timeframe")
    if not timeframe:
        raise ValueError("strategy timeframe is required")
    routed = ctx.trigger.get("market")
    if routed and routed not in ctx.config.markets:
        return ctx.result.error(message=f"unexpected event market {routed}", kind="data_error")
    markets = (routed,) if routed else ctx.config.markets
    results = []
    evaluated = duplicates = 0
    for market in markets:
        rows = candle_snapshots(ctx.market.candles(market, timeframe=timeframe, limit=lookback), timeframe)
        now = ctx.clock.now_ms()
        if any("close_time_ms" not in row or "ts" not in row for row in rows):
            return ctx.result.error(message=f"missing candle time for {market}", kind="data_error")
        candles = [row for row in rows if row["close_time_ms"] <= now]
        if not candles:
            return ctx.result.error(message=f"no closed candles for {market}", kind="data_error")
        stamps = [row["ts"] for row in candles]
        if any(a >= b for a, b in zip(stamps, stamps[1:])):
            return ctx.result.error(message=f"unordered or duplicate candles for {market}", kind="data_error")
        key = f"signal_runner:{getattr(signal, '__name__', 'signal')}:{market}:{timeframe}"
        previous = ctx.state.get(key)
        if previous is not None and previous >= stamps[-1]:
            duplicates += 1
            continue
        signals = signal(candles, **parameters)
        if not isinstance(signals, Mapping) or not {"buy", "sell"}.issubset(signals):
            raise ValueError("signal callable must return buy and sell flags/series")
        buy, sell = _last_signal(signals["buy"], "buy"), _last_signal(signals["sell"], "sell")
        if buy and sell:
            raise ValueError("conflicting buy and sell signals on the same candle")
        evaluated += 1
        # Consume the signal before any uncertain submission; the SDK owns
        # rejection/approval receipts. No implicit duplicate order retries.
        ctx.state.set(key, stamps[-1])
        position = ctx.portfolio.position(market)
        quantity = position.size if position is not None else 0.0
        if quantity < 0:
            return ctx.result.error(message=f"long-only strategy encountered a short position for {market}", kind="position_error")
        receipt = None
        if sell and quantity > 0:
            receipt = ctx.trading.close_position(market=market, side="long", confidence=confidence,
                reasoning_ref=f"{getattr(signal, '__name__', 'signal')}:sell:{stamps[-1]}")
        elif buy and quantity == 0:
            receipt = ctx.trading.open_position(market=market, side="long", sizing=sizing,
                protection=protection, confidence=confidence,
                reasoning_ref=f"{getattr(signal, '__name__', 'signal')}:buy:{stamps[-1]}")
        if receipt is not None:
            result = receipt if isinstance(receipt, StrategyResult) else StrategyResult.from_trade_envelope(receipt)
            results.append(result)
    metadata = {"evaluated_markets":evaluated, "duplicate_markets":duplicates, "submitted_calls":len(results)}
    if not results:
        return ctx.result.hold(reason="duplicate_closed_candles" if duplicates and not evaluated else "no_new_trade_signal", metadata=metadata)
    priority = {StrategyResultStatus.ERROR:0, StrategyResultStatus.PENDING_APPROVAL:1, StrategyResultStatus.REJECTED:2}
    result = min(results, key=lambda item: priority.get(item.status, 3))
    result.metadata = {**result.metadata, **metadata}
    return result
