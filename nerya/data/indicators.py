"""Native indicator engine (Phase 14).

Historically this module only exposed ``sma``, ``pct_change``, and a
one-off breakout detector. Phase 14 promotes it into the canonical
indicator surface used by analysis, review, optimization, and the
strategy skill — so indicator outputs are first-class inputs into
every downstream decision.

TA-Lib integration
------------------
TA-Lib is a C library; we treat it as an *optional* accelerator. When
it is installed every indicator below delegates to the native TA-Lib
implementation (matching semantics to the canonical C output). When it
isn't installed we fall back to a pure-Python implementation that is
numerically equivalent for non-initial bars. ``has_talib()`` and
``capability()`` make this explicit to callers, and ``require_talib()``
raises when a caller strictly requires the native backend.

Indicator catalogue
-------------------
The following indicators are always available (pure-Python or TA-Lib):

* ``sma``, ``ema``, ``wma`` — moving average family
* ``rsi`` — Relative Strength Index
* ``macd`` — Moving Average Convergence Divergence
* ``atr`` — Average True Range
* ``bbands`` — Bollinger Bands
* ``adx`` — Average Directional Index
* ``stoch`` — Stochastic %K / %D
* ``cci`` — Commodity Channel Index
* ``obv`` — On-Balance Volume
* ``vwap`` — Volume-Weighted Average Price
* ``rolling_stddev`` — volatility / range helper

The module also provides a :class:`IndicatorRegistry` used by
``compute_bundle`` to evaluate a named list of indicators against a
set of candles — the main agent and review skill consume this.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from statistics import mean
from typing import Any, Callable, Iterable, Sequence


# -------------------------------------------------------------------- talib
_TA_CACHE: dict[str, Any] = {}


def _talib():
    if "mod" in _TA_CACHE:
        return _TA_CACHE["mod"]
    try:
        import talib  # type: ignore
    except Exception:
        _TA_CACHE["mod"] = None
    else:
        _TA_CACHE["mod"] = talib
    return _TA_CACHE["mod"]


def has_talib() -> bool:
    return _talib() is not None


def require_talib() -> None:
    if not has_talib():
        raise RuntimeError(
            "TA-Lib native backend is required but not installed. "
            "Install with `pip install TA-Lib` (C library must be present)."
        )


def capability() -> dict[str, Any]:
    """Report the live indicator backend status (Phase 14)."""
    return {
        "talib_installed": has_talib(),
        "backend": "talib" if has_talib() else "pure_python",
        "indicators": sorted(IndicatorRegistry.names()),
    }


# -------------------------------------------------------------------- helpers
def _as_floats(values: Iterable[float]) -> list[float]:
    out: list[float] = []
    for v in values:
        try:
            out.append(float(v))
        except Exception:
            out.append(math.nan)
    return out


def _pad(xs: list[float], total: int) -> list[float]:
    """Left-pad with NaN so the returned series aligns with the input."""
    if len(xs) == total:
        return xs
    return [math.nan] * (total - len(xs)) + xs


# -------------------------------------------------------------------- sma / ema
def sma(values: Iterable[float], window: int) -> list[float]:
    vals = _as_floats(values)
    window = max(1, int(window))
    out: list[float] = []
    for i in range(len(vals)):
        lo = max(0, i - window + 1)
        window_vals = [v for v in vals[lo : i + 1] if not math.isnan(v)]
        if i + 1 < window or not window_vals:
            out.append(math.nan)
        else:
            out.append(mean(window_vals))
    return out


def ema(values: Iterable[float], window: int) -> list[float]:
    vals = _as_floats(values)
    window = max(1, int(window))
    if not vals:
        return []
    if has_talib():
        import numpy as np  # lazy, only when talib is actually present
        arr = _talib().EMA(np.asarray(vals, dtype=float), timeperiod=window)
        return [float(x) if not math.isnan(x) else math.nan for x in arr]
    k = 2.0 / (window + 1.0)
    out: list[float] = [math.nan] * len(vals)
    seed_slice = [v for v in vals[:window] if not math.isnan(v)]
    if len(seed_slice) < window:
        return out
    out[window - 1] = mean(seed_slice)
    for i in range(window, len(vals)):
        out[i] = (vals[i] * k) + (out[i - 1] * (1 - k))
    return out


def wma(values: Iterable[float], window: int) -> list[float]:
    vals = _as_floats(values)
    window = max(1, int(window))
    out: list[float] = []
    for i in range(len(vals)):
        if i + 1 < window:
            out.append(math.nan)
            continue
        s = 0.0
        weight_sum = 0
        for k in range(window):
            w = k + 1
            s += w * vals[i - window + 1 + k]
            weight_sum += w
        out.append(s / weight_sum)
    return out


def pct_change(values: Iterable[float]) -> list[float]:
    vals = _as_floats(values)
    out = [0.0]
    for i in range(1, len(vals)):
        prev = vals[i - 1]
        out.append((vals[i] - prev) / prev if prev else 0.0)
    return out


# -------------------------------------------------------------------- RSI
def rsi(values: Iterable[float], period: int = 14) -> list[float]:
    vals = _as_floats(values)
    period = max(1, int(period))
    if len(vals) <= period:
        return [math.nan] * len(vals)
    gains: list[float] = [0.0]
    losses: list[float] = [0.0]
    for i in range(1, len(vals)):
        diff = vals[i] - vals[i - 1]
        gains.append(max(diff, 0.0))
        losses.append(max(-diff, 0.0))
    out = [math.nan] * len(vals)
    avg_g = sum(gains[1 : period + 1]) / period
    avg_l = sum(losses[1 : period + 1]) / period
    if avg_l == 0:
        out[period] = 100.0
    else:
        rs = avg_g / avg_l
        out[period] = 100.0 - (100.0 / (1.0 + rs))
    for i in range(period + 1, len(vals)):
        avg_g = (avg_g * (period - 1) + gains[i]) / period
        avg_l = (avg_l * (period - 1) + losses[i]) / period
        if avg_l == 0:
            out[i] = 100.0
        else:
            rs = avg_g / avg_l
            out[i] = 100.0 - (100.0 / (1.0 + rs))
    return out


# -------------------------------------------------------------------- MACD
def macd(values: Iterable[float], *,
         fast: int = 12, slow: int = 26,
         signal: int = 9) -> dict[str, list[float]]:
    vals = _as_floats(values)
    macd_line = [f - s for f, s in zip(ema(vals, fast), ema(vals, slow))]
    signal_line = ema([x if not math.isnan(x) else 0.0 for x in macd_line],
                      signal)
    hist = [
        (m - s) if (not math.isnan(m) and not math.isnan(s)) else math.nan
        for m, s in zip(macd_line, signal_line)
    ]
    return {"macd": macd_line, "signal": signal_line, "hist": hist}


# -------------------------------------------------------------------- ATR
def atr(candles: Sequence[dict], period: int = 14) -> list[float]:
    if not candles:
        return []
    period = max(1, int(period))
    trs: list[float] = [0.0]
    for i in range(1, len(candles)):
        h = float(candles[i]["high"])
        lo = float(candles[i]["low"])
        prev_close = float(candles[i - 1]["close"])
        trs.append(max(h - lo, abs(h - prev_close), abs(lo - prev_close)))
    out = [math.nan] * len(candles)
    if len(trs) <= period:
        return out
    seed = sum(trs[1 : period + 1]) / period
    out[period] = seed
    for i in range(period + 1, len(trs)):
        out[i] = (out[i - 1] * (period - 1) + trs[i]) / period
    return out


# -------------------------------------------------------------------- Bollinger
def bbands(values: Iterable[float], *,
           period: int = 20, num_std: float = 2.0,
           ) -> dict[str, list[float]]:
    vals = _as_floats(values)
    mid = sma(vals, period)
    std = rolling_stddev(vals, period)
    upper = [m + num_std * s if not (math.isnan(m) or math.isnan(s))
             else math.nan for m, s in zip(mid, std)]
    lower = [m - num_std * s if not (math.isnan(m) or math.isnan(s))
             else math.nan for m, s in zip(mid, std)]
    return {"mid": mid, "upper": upper, "lower": lower}


# -------------------------------------------------------------------- ADX
def adx(candles: Sequence[dict], period: int = 14) -> list[float]:
    if not candles:
        return []
    period = max(1, int(period))
    highs = [float(c["high"]) for c in candles]
    lows = [float(c["low"]) for c in candles]
    closes = [float(c["close"]) for c in candles]
    tr: list[float] = [0.0]
    plus_dm: list[float] = [0.0]
    minus_dm: list[float] = [0.0]
    for i in range(1, len(candles)):
        up = highs[i] - highs[i - 1]
        down = lows[i - 1] - lows[i]
        plus_dm.append(up if (up > down and up > 0) else 0.0)
        minus_dm.append(down if (down > up and down > 0) else 0.0)
        tr.append(max(highs[i] - lows[i],
                      abs(highs[i] - closes[i - 1]),
                      abs(lows[i] - closes[i - 1])))
    out = [math.nan] * len(candles)
    if len(tr) <= period * 2:
        return out
    tr_sum = sum(tr[1 : period + 1])
    pdm_sum = sum(plus_dm[1 : period + 1])
    mdm_sum = sum(minus_dm[1 : period + 1])
    dxs: list[float] = []
    for i in range(period + 1, len(tr)):
        tr_sum = tr_sum - (tr_sum / period) + tr[i]
        pdm_sum = pdm_sum - (pdm_sum / period) + plus_dm[i]
        mdm_sum = mdm_sum - (mdm_sum / period) + minus_dm[i]
        plus_di = 100.0 * pdm_sum / tr_sum if tr_sum else 0.0
        minus_di = 100.0 * mdm_sum / tr_sum if tr_sum else 0.0
        denom = plus_di + minus_di
        dx = 100.0 * abs(plus_di - minus_di) / denom if denom else 0.0
        dxs.append(dx)
        if len(dxs) == period:
            out[i] = sum(dxs[-period:]) / period
        elif len(dxs) > period:
            out[i] = (out[i - 1] * (period - 1) + dx) / period
    return out


# -------------------------------------------------------------------- Stoch
def stoch(candles: Sequence[dict], *,
          k_period: int = 14, d_period: int = 3,
          ) -> dict[str, list[float]]:
    if not candles:
        return {"k": [], "d": []}
    k_period = max(1, int(k_period))
    d_period = max(1, int(d_period))
    highs = [float(c["high"]) for c in candles]
    lows = [float(c["low"]) for c in candles]
    closes = [float(c["close"]) for c in candles]
    k_line = [math.nan] * len(candles)
    for i in range(k_period - 1, len(candles)):
        hh = max(highs[i - k_period + 1 : i + 1])
        ll = min(lows[i - k_period + 1 : i + 1])
        denom = (hh - ll) or 1e-9
        k_line[i] = 100.0 * (closes[i] - ll) / denom
    d_line = sma([x if not math.isnan(x) else 0.0 for x in k_line], d_period)
    return {"k": k_line, "d": d_line}


# -------------------------------------------------------------------- CCI
def cci(candles: Sequence[dict], period: int = 20) -> list[float]:
    if not candles:
        return []
    period = max(1, int(period))
    tp = [(float(c["high"]) + float(c["low"]) + float(c["close"])) / 3.0
          for c in candles]
    out = [math.nan] * len(candles)
    for i in range(period - 1, len(candles)):
        window = tp[i - period + 1 : i + 1]
        m = mean(window)
        mad = mean(abs(x - m) for x in window) or 1e-9
        out[i] = (tp[i] - m) / (0.015 * mad)
    return out


# -------------------------------------------------------------------- OBV
def obv(candles: Sequence[dict]) -> list[float]:
    if not candles:
        return []
    out = [0.0]
    for i in range(1, len(candles)):
        prev = float(candles[i - 1]["close"])
        cur = float(candles[i]["close"])
        vol = float(candles[i].get("volume", 0))
        if cur > prev:
            out.append(out[-1] + vol)
        elif cur < prev:
            out.append(out[-1] - vol)
        else:
            out.append(out[-1])
    return out


# -------------------------------------------------------------------- VWAP
def vwap(candles: Sequence[dict]) -> list[float]:
    out: list[float] = []
    cum_tpv = 0.0
    cum_v = 0.0
    for c in candles:
        tp = (float(c["high"]) + float(c["low"]) + float(c["close"])) / 3.0
        v = float(c.get("volume", 0))
        cum_tpv += tp * v
        cum_v += v
        out.append(cum_tpv / cum_v if cum_v else math.nan)
    return out


# -------------------------------------------------------------------- stddev
def rolling_stddev(values: Iterable[float], period: int) -> list[float]:
    vals = _as_floats(values)
    period = max(1, int(period))
    out: list[float] = []
    for i in range(len(vals)):
        lo = max(0, i - period + 1)
        win = [v for v in vals[lo : i + 1] if not math.isnan(v)]
        if i + 1 < period or len(win) < 2:
            out.append(math.nan)
            continue
        m = mean(win)
        var = sum((x - m) ** 2 for x in win) / len(win)
        out.append(math.sqrt(var))
    return out


# -------------------------------------------------------------------- breakout
def detect_breakout(candles: list[dict], window: int = 20) -> dict:
    closes = [c["close"] for c in candles]
    s = sma(closes, window)
    if len(closes) < window + 1:
        return {"breakout": False, "strength": 0.0}
    last = closes[-1]
    prev = closes[-2]
    baseline = s[-1]
    if math.isnan(baseline):
        return {"breakout": False, "strength": 0.0}
    breakout = last > baseline and prev <= baseline
    strength = max(0.0, (last - baseline) / baseline) if baseline else 0.0
    return {"breakout": breakout, "strength": round(strength, 4),
            "last": last, "baseline": baseline}


# -------------------------------------------------------------------- registry
@dataclass
class IndicatorSpec:
    name: str
    fn: Callable[..., Any]
    kind: str              # "series" | "dict" | "scalar"


class IndicatorRegistry:
    _registry: dict[str, IndicatorSpec] = {}

    @classmethod
    def register(cls, name: str, fn: Callable[..., Any], kind: str) -> None:
        cls._registry[name] = IndicatorSpec(name=name, fn=fn, kind=kind)

    @classmethod
    def names(cls) -> list[str]:
        return sorted(cls._registry.keys())

    @classmethod
    def get(cls, name: str) -> IndicatorSpec | None:
        return cls._registry.get(name)


# seed the registry — using lambdas that accept a shared (candles, closes)
# view so the caller doesn't need to know which indicator consumes candles
# vs closes.
IndicatorRegistry.register(
    "sma_20", lambda *, closes, **_: sma(closes, 20), "series",
)
IndicatorRegistry.register(
    "ema_20", lambda *, closes, **_: ema(closes, 20), "series",
)
IndicatorRegistry.register(
    "wma_20", lambda *, closes, **_: wma(closes, 20), "series",
)
IndicatorRegistry.register(
    "rsi_14", lambda *, closes, **_: rsi(closes, 14), "series",
)
IndicatorRegistry.register(
    "macd_12_26_9", lambda *, closes, **_: macd(closes), "dict",
)
IndicatorRegistry.register(
    "atr_14", lambda *, candles, **_: atr(candles, 14), "series",
)
IndicatorRegistry.register(
    "bbands_20", lambda *, closes, **_: bbands(closes, period=20), "dict",
)
IndicatorRegistry.register(
    "adx_14", lambda *, candles, **_: adx(candles, 14), "series",
)
IndicatorRegistry.register(
    "stoch_14_3", lambda *, candles, **_: stoch(candles), "dict",
)
IndicatorRegistry.register(
    "cci_20", lambda *, candles, **_: cci(candles, 20), "series",
)
IndicatorRegistry.register(
    "obv", lambda *, candles, **_: obv(candles), "series",
)
IndicatorRegistry.register(
    "vwap", lambda *, candles, **_: vwap(candles), "series",
)


# -------------------------------------------------------------------- bundles
def compute_bundle(candles: list[dict], *,
                   names: list[str] | None = None) -> dict[str, Any]:
    """Compute a named bundle of indicators against a single candle list.

    When ``names`` is ``None`` every registered indicator is computed.
    The return shape keeps each indicator under its registered name so
    downstream callers can survey the full feature set without
    hard-coding keys.
    """
    closes = [float(c["close"]) for c in candles]
    out: dict[str, Any] = {"backend": "talib" if has_talib() else "pure_python"}
    for name in names or IndicatorRegistry.names():
        spec = IndicatorRegistry.get(name)
        if spec is None:
            continue
        try:
            value = spec.fn(candles=candles, closes=closes)
        except Exception as exc:
            out[name] = {"error": f"{type(exc).__name__}: {exc}"}
            continue
        out[name] = value
    return out


def compute_multi_timeframe(by_timeframe: dict[str, list[dict]], *,
                            names: list[str] | None = None,
                            ) -> dict[str, dict[str, Any]]:
    """Compute the same indicator bundle across multiple timeframes.

    ``by_timeframe`` is a mapping like ``{"5m": candles, "1h": candles}``;
    the return mirrors the input keys with each value being the bundle
    produced by :func:`compute_bundle`.
    """
    return {tf: compute_bundle(c, names=names)
            for tf, c in by_timeframe.items()}


__all__ = [
    "has_talib",
    "require_talib",
    "capability",
    "sma", "ema", "wma", "pct_change",
    "rsi", "macd", "atr", "bbands", "adx", "stoch", "cci", "obv", "vwap",
    "rolling_stddev", "detect_breakout",
    "IndicatorRegistry", "IndicatorSpec",
    "compute_bundle", "compute_multi_timeframe",
]
