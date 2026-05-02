"""Pure indicator helpers used by the backtest engine."""

from __future__ import annotations

from typing import Any


Series = list[float | None]


def sma(candles: list[dict[str, Any]], period: int, *, warmup_bars: int = 0) -> Series:
    closes = _closes(candles)
    out: Series = []
    for i in range(len(closes)):
        if i + 1 < period:
            out.append(None)
        else:
            out.append(sum(closes[i + 1 - period:i + 1]) / period)
    return _mask(out, warmup_bars)


def ema(candles: list[dict[str, Any]], period: int, *, warmup_bars: int = 0) -> Series:
    closes = _closes(candles)
    if not closes:
        return []
    k = 2.0 / (period + 1.0)
    out: Series = []
    value: float | None = None
    for i, close in enumerate(closes):
        value = close if value is None else close * k + value * (1.0 - k)
        out.append(None if i + 1 < period else value)
    return _mask(out, warmup_bars)


def rsi(candles: list[dict[str, Any]], period: int, *, warmup_bars: int = 0) -> Series:
    closes = _closes(candles)
    out: Series = [None]
    gains: list[float] = []
    losses: list[float] = []
    for i in range(1, len(closes)):
        delta = closes[i] - closes[i - 1]
        gains.append(max(delta, 0.0))
        losses.append(abs(min(delta, 0.0)))
        if len(gains) < period:
            out.append(None)
            continue
        avg_gain = sum(gains[-period:]) / period
        avg_loss = sum(losses[-period:]) / period
        if avg_loss == 0:
            out.append(100.0)
        else:
            rs = avg_gain / avg_loss
            out.append(100.0 - (100.0 / (1.0 + rs)))
    return _mask(out, warmup_bars)


def atr(candles: list[dict[str, Any]], period: int, *, warmup_bars: int = 0) -> Series:
    trs: list[float] = []
    prev_close: float | None = None
    for row in candles:
        high = float(row.get("high", 0.0))
        low = float(row.get("low", 0.0))
        close = float(row.get("close", 0.0))
        if prev_close is None:
            tr = high - low
        else:
            tr = max(high - low, abs(high - prev_close), abs(low - prev_close))
        trs.append(tr)
        prev_close = close
    out: Series = []
    for i in range(len(trs)):
        if i + 1 < period:
            out.append(None)
        else:
            out.append(sum(trs[i + 1 - period:i + 1]) / period)
    return _mask(out, warmup_bars)


def compute_indicators(
    candles: list[dict[str, Any]],
    spec: dict[str, list[int]],
    *,
    warmup_bars: int = 0,
) -> dict[str, Series]:
    out: dict[str, Series] = {}
    for period in spec.get("sma", []):
        out[f"sma_{period}"] = sma(candles, period, warmup_bars=warmup_bars)
    for period in spec.get("ema", []):
        out[f"ema_{period}"] = ema(candles, period, warmup_bars=warmup_bars)
    for period in spec.get("rsi", []):
        out[f"rsi_{period}"] = rsi(candles, period, warmup_bars=warmup_bars)
    for period in spec.get("atr", []):
        out[f"atr_{period}"] = atr(candles, period, warmup_bars=warmup_bars)
    return out


def _closes(candles: list[dict[str, Any]]) -> list[float]:
    return [float(row.get("close", 0.0)) for row in candles]


def _mask(values: Series, warmup_bars: int) -> Series:
    for i in range(min(int(warmup_bars), len(values))):
        values[i] = None
    return values

