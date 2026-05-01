"""Turns long raw context into a short string the agent can afford."""

from __future__ import annotations

from typing import Any

from .features import compute_features


def summarize(market: str, candles: list[dict], orderbook: dict[str, Any] | None = None) -> str:
    feats = compute_features(candles)
    ob = orderbook or {}
    lines = [
        f"Market: {market}",
        f"Last close: {feats['close']}",
        f"SMA20: {feats['sma_20']}",
        f"Return (1 bar): {feats['ret_1']:+.4%}",
        f"Breakout: {feats['breakout']}",
        f"Spread bps: {ob.get('spread_bps', '-')}",
    ]
    return "\n".join(lines)
