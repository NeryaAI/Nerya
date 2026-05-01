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
