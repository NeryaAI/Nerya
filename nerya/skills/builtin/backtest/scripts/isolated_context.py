"""Host-owned historical SDK dispatch; never imports candidate source."""

from __future__ import annotations

from dataclasses import asdict, is_dataclass
from enum import Enum
import json
from typing import Any

_METHODS = frozenset(
    {
        "market.candles",
        "market.get_candles",
        "market.ohlcv",
        "market.get_ohlcv",
        "market.klines",
        "market.features",
        "market.ticker",
        "market.get_ticker",
        "market.mark_price",
        "state.get",
        "state.set",
        "state.delete",
        "state.compare_and_set",
        "state.update",
        "portfolio.position",
        "portfolio.positions",
        "portfolio.summary",
        "portfolio.equity_usd",
        "portfolio.cash_usd",
        "portfolio.nav_usd",
        "trading.open_position",
        "trading.close_position",
        "trading.reduce_position",
        "trading.execution_status",
        "trading.signal",
        "audit.log",
        "audit.record",
        "subagents.run",
    }
)
_RESERVED = ("position:", "__")


def json_value(value: Any) -> Any:
    if isinstance(value, Enum):
        return value.value
    if hasattr(value, "asdict"):
        return json_value(value.asdict())
    if is_dataclass(value):
        return json_value(asdict(value))
    if isinstance(value, dict):
        return {str(k): json_value(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [json_value(v) for v in value]
    if value is None or type(value) in (str, int, float, bool):
        return value
    raise ValueError("historical SDK returned a non-JSON value")


def context_snapshot(ctx) -> dict:
    """No account credentials, host paths, complete tape or future bars."""
    return {
        "strategy_id": ctx.strategy_id,
        "run_id": ctx.run_id,
        "session_id": ctx.session_id,
        "mode": "backtest",
        "runmode": "backtest",
        "timeframe": ctx.config_obj.tf,
        "symbol": ctx.market_name,
        "config": json_value(ctx.config),
        "policy": json_value(ctx.policy),
        "clock": {
            "ts": ctx.clock.ts,
            "now_ms": ctx.clock.now_ms(),
            "now_iso": ctx.clock.now_iso(),
        },
        "trigger": {**json_value(ctx.trigger), **json_value(ctx.trigger.payload)},
    }


def dispatch_context(ctx, method: str, args: list, kwargs: dict, *, decision):
    """Only explicit public facades; accounting state is host-owned."""
    if (
        method not in _METHODS
        or not isinstance(args, list)
        or not isinstance(kwargs, dict)
    ):
        raise ValueError("historical SDK method is not allowed")
    encoded = json.dumps([args, kwargs], allow_nan=False, ensure_ascii=False).encode()
    if len(encoded) > 256_000:
        raise ValueError("historical SDK request exceeds limit")
    if method in {"state.set", "state.delete", "state.compare_and_set", "state.update"}:
        keys = (
            kwargs
            if method == "state.update"
            else [args[0] if args else kwargs.get("key")]
        )
        if any(not isinstance(key, str) or key.startswith(_RESERVED) for key in keys):
            raise ValueError("historical accounting state is read-only")
    if method == "subagents.run":
        return json_value(decision(ctx, args, kwargs))
    surface, operation = method.split(".")
    if surface in {"market", "portfolio", "trading"}:
        market = kwargs.get("market") or (
            args[0] if args and isinstance(args[0], str) else None
        )
        if market is not None and market not in ctx.config.markets:
            raise ValueError("historical SDK market is outside the candidate")
        if kwargs.get("account") not in (None, *ctx.config.accounts):
            raise ValueError("historical SDK account is outside the candidate")
    value = getattr(getattr(ctx, surface), operation)(*args, **kwargs)
    return json_value(value)
