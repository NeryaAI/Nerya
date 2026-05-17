"""Deterministic mock exchange used by paper trading + tests."""

from __future__ import annotations

import time

from .base import Connector, Ticker


class MockExchange(Connector):
    venue = "MOCK"

    _BASELINES = {
        "MOCK:BTCUSDT": 80000.0,
        "MOCK:ETHUSDT": 3500.0,
        "MOCK:SOLUSDT": 180.0,
        "PAPER:BTCUSDT": 80000.0,
        "PAPER:ETHUSDT": 3500.0,
        "PAPER:SOLUSDT": 180.0,
    }

    def __init__(self, baselines: dict[str, float] | None = None):
        self._prices = dict(self._BASELINES)
        if baselines:
            self._prices.update(baselines)

    def set_price(self, market: str, price: float) -> None:
        self._prices[market] = float(price)

    def get_mark_price(self, market: str) -> float:
        return float(self._prices.get(market, 100.0))

    def get_ticker(self, market: str) -> Ticker:
        mid = self.get_mark_price(market)
        bid = mid * 0.9995
        ask = mid * 1.0005
        return Ticker(
            market=market, bid=bid, ask=ask, mid=mid, last=mid,
            spread_bps=10.0, ts_ms=int(time.time() * 1000),
            venue=self.venue,
        )

    def get_klines(
        self,
        market: str,
        *,
        interval: str = "1m",
        limit: int = 100,
    ) -> list[list[float]]:
        from ..data.candles import mock_candles

        interval_s = _interval_seconds(interval)
        rows = mock_candles(
            market,
            count=max(1, int(limit or 100)),
            interval_s=interval_s,
            seed_price=self.get_mark_price(market),
        )
        return [
            [
                int(row["ts"]) * 1000,
                float(row["open"]),
                float(row["high"]),
                float(row["low"]),
                float(row["close"]),
                float(row["volume"]),
            ]
            for row in rows
        ]


def _interval_seconds(interval: str) -> int:
    raw = str(interval or "1m").strip().lower()
    if len(raw) < 2:
        return 60
    try:
        n = max(1, int(raw[:-1]))
    except ValueError:
        return 60
    unit = raw[-1]
    if unit == "s":
        return n
    if unit == "m":
        return n * 60
    if unit == "h":
        return n * 3600
    if unit == "d":
        return n * 86400
    return 60
