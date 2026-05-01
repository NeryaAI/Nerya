"""Derived features from raw candles / orderbook / funding (Phase 14).

``compute_features`` historically returned only a handful of scalars
(close, sma_20, ret_1, vol_last, breakout). Phase 14 extends it to a
full collaborative feature set — every indicator in
:mod:`nerya.data.indicators` is represented as a last-bar scalar so
downstream analysis / review / optimization agents can consume a
single dict without having to evaluate indicators separately.

For multi-timeframe analysis callers should use
:func:`nerya.data.indicators.compute_multi_timeframe` directly and
feed the result into ``review`` / ``explain`` artifacts.
"""

from __future__ import annotations

import math
from typing import Any

from .indicators import (
    atr,
    adx,
    bbands,
    cci,
    compute_bundle,
    detect_breakout,
    ema,
    has_talib,
    macd,
    obv,
    pct_change,
    rsi,
    sma,
    stoch,
    vwap,
)


def _last(series: list[float]) -> float | None:
    if not series:
        return None
    v = series[-1]
    if isinstance(v, float) and math.isnan(v):
        return None
    return v


def compute_features(candles: list[dict]) -> dict:
    closes = [float(c["close"]) for c in candles] if candles else []
    vols = [float(c.get("volume", 0)) for c in candles] if candles else []
    features: dict[str, Any] = {
        "close": closes[-1] if closes else None,
        "sma_20": _last(sma(closes, 20)) if closes else None,
        "ema_20": _last(ema(closes, 20)) if closes else None,
        "ret_1": pct_change(closes)[-1] if closes else 0.0,
        "vol_last": vols[-1] if vols else 0.0,
        "breakout": detect_breakout(candles) if candles else None,
        "rsi_14": _last(rsi(closes, 14)) if closes else None,
        "atr_14": _last(atr(candles, 14)) if candles else None,
        "adx_14": _last(adx(candles, 14)) if candles else None,
        "cci_20": _last(cci(candles, 20)) if candles else None,
        "obv":    _last(obv(candles)) if candles else None,
        "vwap":   _last(vwap(candles)) if candles else None,
        "indicator_backend": "talib" if has_talib() else "pure_python",
    }
    if closes:
        m = macd(closes)
        features["macd"] = {
            "macd": _last(m["macd"]),
            "signal": _last(m["signal"]),
            "hist": _last(m["hist"]),
        }
        bb = bbands(closes)
        features["bbands"] = {
            "upper": _last(bb["upper"]),
            "mid":   _last(bb["mid"]),
            "lower": _last(bb["lower"]),
        }
        st = stoch(candles)
        features["stoch"] = {"k": _last(st["k"]), "d": _last(st["d"])}
    return features


__all__ = ["compute_features", "compute_bundle"]
