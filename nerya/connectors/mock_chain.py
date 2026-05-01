"""Mock chain connector — deterministic token balances for tests."""

from __future__ import annotations

import time

from .base import Connector, Ticker


class MockChain(Connector):
    venue = "MOCK_CHAIN"
    kind = "chain"

    def __init__(self):
        self._balances = {"0xmock_wallet": {"ETH": 10.0, "USDC": 50000.0}}

    def get_mark_price(self, market: str) -> float:
        return 1.0

    def get_ticker(self, market: str) -> Ticker:
        return Ticker(
            market=market, bid=1.0, ask=1.0, mid=1.0, last=1.0,
            spread_bps=0.0, ts_ms=int(time.time() * 1000), venue=self.venue,
        )

    def get_balance(self, address: str, token: str) -> float:
        return float(self._balances.get(address, {}).get(token, 0.0))

    def simulate_swap(self, **kwargs) -> dict:
        return {"ok": True, "expected_out": kwargs.get("amount_in", 0) * 0.995,
                "slippage_bps": 25}
