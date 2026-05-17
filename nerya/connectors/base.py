"""Connector base class — rich, connector-oriented interface.

Connectors expose read (public market data) and write (signed trading)
methods. Writes are only ever called by the ExecutionEngine when the
account is live AND `live_trading_enabled` is true in nerya.yml.

Scripts and agents must NEVER import or use these directly; the skill
runtime's permission system blocks that path.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any


@dataclass
class Ticker:
    market: str
    bid: float
    ask: float
    mid: float
    last: float
    spread_bps: float
    ts_ms: int
    venue: str = ""

    def asdict(self) -> dict[str, Any]:
        return {
            "market": self.market, "bid": self.bid, "ask": self.ask,
            "mid": self.mid, "last": self.last, "spread_bps": self.spread_bps,
            "ts_ms": self.ts_ms, "venue": self.venue,
        }


@dataclass
class Balance:
    asset: str
    free: float
    locked: float = 0.0
    total: float = 0.0

    def asdict(self) -> dict[str, Any]:
        return {"asset": self.asset, "free": self.free,
                "locked": self.locked, "total": self.total or (self.free + self.locked)}


@dataclass
class OrderAck:
    order_id: str
    client_order_id: str
    status: str  # new | filled | partial | rejected | canceled
    market: str
    side: str
    price: float | None = None
    size: float | None = None
    filled: float | None = None
    avg_price: float | None = None
    # USD-equivalent fee for the filled portion (sum across multi-asset
    # fees). ``None`` distinguishes "fee field unavailable from broker"
    # from "fee == 0".
    fee_usd: float | None = None
    # Fee breakdown keyed by asset code, preserved verbatim from the
    # broker for audit. ``{"BNB": 0.001}`` etc.
    fee_breakdown: dict[str, float] = field(default_factory=dict)
    raw: dict[str, Any] = field(default_factory=dict)

    def asdict(self) -> dict[str, Any]:
        return {
            "order_id": self.order_id,
            "client_order_id": self.client_order_id,
            "status": self.status,
            "market": self.market,
            "side": self.side,
            "price": self.price,
            "size": self.size,
            "filled": self.filled,
            "avg_price": self.avg_price,
            "fee_usd": self.fee_usd,
            "fee_breakdown": dict(self.fee_breakdown),
        }


class Connector(ABC):
    venue: str = ""
    kind: str = "cex"  # cex | dex | chain

    # ------------------------------------------------------------ public
    @abstractmethod
    def get_ticker(self, market: str) -> Ticker: ...

    def get_mark_price(self, market: str) -> float:
        return self.get_ticker(market).mid

    def get_order_book(self, market: str) -> dict[str, Any]:
        t = self.get_ticker(market)
        return {
            "market": t.market, "bid": t.bid, "ask": t.ask, "mid": t.mid,
            "spread_bps": t.spread_bps, "ts_ms": t.ts_ms, "venue": t.venue,
        }

    def get_klines(self, market: str, *, interval: str = "1m", limit: int = 100) -> list[list[Any]]:
        # default: empty; overridden by native connectors
        return []

    # ------------------------------------------------------------ private
    def get_balances(self) -> list[Balance]:
        raise NotImplementedError(f"{self.venue} connector does not support balances in this build")

    def place_order(self, *, market: str, side: str, order_type: str,
                     size: float, price: float | None = None,
                     client_order_id: str | None = None,
                     time_in_force: str = "GTC") -> OrderAck:
        raise NotImplementedError(
            f"live place_order disabled for {self.venue}; use paper executor or enable live_trading"
        )

    def cancel_order(self, *, market: str, order_id: str) -> OrderAck:
        raise NotImplementedError(f"cancel_order disabled for {self.venue}")

    def get_order(self, *, market: str, order_id: str) -> OrderAck:
        raise NotImplementedError(f"get_order disabled for {self.venue}")


class CEXConnectorBase(Connector):
    kind = "cex"


class DEXConnectorBase(Connector):
    kind = "dex"


__all__ = ["Connector", "CEXConnectorBase", "DEXConnectorBase",
           "Ticker", "Balance", "OrderAck"]
