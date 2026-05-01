"""Candle fetching — real connector-backed.

Mock fallback is *opt-in only*. Production runtime paths that call
:func:`fetch_candles` without authorising mock mode get an empty list and a
degraded envelope rather than fabricated OHLCV data. See
:mod:`nerya.core.truth` for the opt-in mechanics.
"""

from __future__ import annotations

import math
import time
from typing import Any

from ..core.truth import (
    RuntimeEnvelope,
    degraded_envelope,
    live_envelope,
    mock_envelope,
    resolve_allow_mock,
    tag_list_envelope,
)


def mock_candles(market: str, *, count: int = 60, interval_s: int = 60,
                 seed_price: float | None = None) -> list[dict[str, Any]]:
    """Generate a synthetic OHLCV series biased bullish to trigger breakouts."""
    base = seed_price if seed_price is not None else _default_price(market)
    now = int(time.time())
    out = []
    price = base
    for i in range(count):
        ts = now - (count - i) * interval_s
        drift = 0.0005 * math.sin(i / 5)
        price = price * (1 + drift + 0.0002)
        high = price * 1.001
        low = price * 0.999
        open_ = price * 0.9998
        close = price
        vol = 10 + (i % 7)
        out.append({"ts": ts, "open": open_, "high": high, "low": low,
                    "close": close, "volume": vol})
    last = out[-1]
    last["close"] = last["close"] * 1.015
    last["high"] = last["close"] * 1.002
    return out


def _default_price(market: str) -> float:
    return {
        "MOCK:BTCUSDT": 80000.0,
        "MOCK:ETHUSDT": 3500.0,
        "MOCK:SOLUSDT": 180.0,
        "PAPER:BTCUSDT": 80000.0,
        "PAPER:ETHUSDT": 3500.0,
        "PAPER:SOLUSDT": 180.0,
    }.get(market, 100.0)


# ---------------------------------------------------------------- normalization

def normalize_klines(venue: str, rows: list[Any]) -> list[dict[str, Any]]:
    """Normalize exchange-native kline arrays into ``{ts, open, high, low, close, volume}``.

    Supports Binance, Bybit v5, OKX, Hyperliquid shapes. Unknown shapes
    return an empty list so callers fall back to the mock.
    """
    if not rows:
        return []
    v = (venue or "").upper()
    out: list[dict[str, Any]] = []

    if v == "BINANCE":
        for r in rows:
            if len(r) < 6:
                continue
            out.append({
                "ts": int(r[0]) // 1000,
                "open": float(r[1]), "high": float(r[2]),
                "low": float(r[3]), "close": float(r[4]),
                "volume": float(r[5]),
            })
        return out

    if v == "BYBIT":
        # Bybit returns newest-first; reverse for chronological order.
        for r in reversed(rows):
            if len(r) < 6:
                continue
            out.append({
                "ts": int(r[0]) // 1000,
                "open": float(r[1]), "high": float(r[2]),
                "low": float(r[3]), "close": float(r[4]),
                "volume": float(r[5]),
            })
        return out

    if v == "OKX":
        for r in reversed(rows):
            if len(r) < 6:
                continue
            out.append({
                "ts": int(r[0]) // 1000,
                "open": float(r[1]), "high": float(r[2]),
                "low": float(r[3]), "close": float(r[4]),
                "volume": float(r[5]),
            })
        return out

    if v in ("HYPERLIQUID", "HL"):
        for r in rows:
            if isinstance(r, dict) and "t" in r:
                out.append({
                    "ts": int(r["t"]) // 1000,
                    "open": float(r.get("o", 0)), "high": float(r.get("h", 0)),
                    "low": float(r.get("l", 0)), "close": float(r.get("c", 0)),
                    "volume": float(r.get("v", 0)),
                })
        return out

    # Best-effort generic array of six numbers
    for r in rows:
        if isinstance(r, list | tuple) and len(r) >= 6:
            try:
                out.append({
                    "ts": int(r[0]) // 1000 if int(r[0]) > 1e12 else int(r[0]),
                    "open": float(r[1]), "high": float(r[2]),
                    "low": float(r[3]), "close": float(r[4]),
                    "volume": float(r[5]),
                })
            except (TypeError, ValueError):
                continue
    return out


# ---------------------------------------------------------------- public fetch

def fetch_candles(market: str, *, count: int = 60, interval: str = "1m",
                   connector: Any | None = None,
                   registry: Any | None = None,
                   account_cfg: dict[str, Any] | None = None,
                   allow_mock: bool | None = None,
                   config_like: Any | None = None) -> list[dict[str, Any]]:
    """Fetch candles for ``market``.

    Resolution order:

    1. If a ``connector`` is supplied, use it directly.
    2. Else if ``registry`` + ``account_cfg`` are supplied, build one from
       the account and use it.
    3. Else parse the venue from ``VENUE:SYMBOL`` and try
       ``registry.build_connector`` with a minimal config.
    4. On error, return mock candles only when mock mode is authorised;
       otherwise return ``[]`` tagged with a degraded envelope.
    """
    conn = connector
    venue = _venue_of(market)
    err = ""
    if conn is None and registry is not None:
        try:
            if account_cfg is not None:
                conn = registry.get_or_build(account_cfg)
            else:
                from ..connectors.registry import build_connector
                conn = build_connector({"venue": venue, "kind": "cex"})
        except Exception as exc:
            err = f"{type(exc).__name__}: {exc}"
            conn = None

    if conn is not None:
        try:
            rows = conn.get_klines(market, interval=interval, limit=count)
            norm = normalize_klines(getattr(conn, "venue", venue), rows)
            if norm:
                env = live_envelope(
                    source=(getattr(conn, "venue", venue) or venue).lower(),
                    venue=(getattr(conn, "venue", venue) or venue).lower(),
                    connector_id=getattr(conn, "connector_id", ""),
                )
                return tag_list_envelope(norm[:count], env)
        except NotImplementedError as exc:
            err = f"NotImplementedError: {exc}"
        except Exception as exc:
            err = f"{type(exc).__name__}: {exc}"

    explicit_mock_prefix = venue in ("MOCK", "PAPER")
    if explicit_mock_prefix or resolve_allow_mock(allow_mock, config_like):
        if explicit_mock_prefix or resolve_allow_mock(allow_mock, config_like):
            rows = mock_candles(market, count=count)
            return tag_list_envelope(
                rows,
                mock_envelope(venue=(venue.lower() or "mock")),
            )

    env = degraded_envelope(
        "candles", error=err or "no_rows",
        venue=(venue.lower() or "unknown"),
    )
    return tag_list_envelope([], env)


def _venue_of(market: str) -> str:
    """Parse ``VENUE:SYMBOL`` — no silent MOCK default.

    Returns ``""`` for unprefixed markets so downstream code can emit a
    degraded envelope instead of fabricating synthetic candles for every
    caller who forgot to prefix their symbol.
    """
    if ":" in market:
        return market.split(":", 1)[0].upper()
    return ""


__all__ = ["mock_candles", "fetch_candles", "normalize_klines"]
