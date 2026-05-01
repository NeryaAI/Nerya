"""Order request/result/fill dataclasses."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any

from ..core.ids import fill_id, order_id
from ..core.time import now_iso


@dataclass
class OrderRequest:
    order_id: str = field(default_factory=order_id)
    intent_id: str = ""
    strategy_id: str = ""
    account_id: str = ""
    market: str = ""
    side: str = "buy"
    size: float = 0.0
    size_unit: str = "usd"
    order_type: str = "market"
    limit_price: float | None = None
    stop_price: float | None = None
    time_in_force: str = "gtc"
    meta: dict[str, Any] = field(default_factory=dict)
    created_at: str = field(default_factory=now_iso)

    def asdict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class Fill:
    fill_id: str
    order_id: str
    intent_id: str
    market: str
    price: float
    size: float
    fee_usd: float
    ts: str


@dataclass
class OrderResult:
    order_id: str
    intent_id: str
    status: str            # "filled", "partial", "rejected", "cancelled", "accepted"
    fills: list[Fill] = field(default_factory=list)
    avg_price: float | None = None
    filled_size: float = 0.0
    notional_usd: float = 0.0
    fee_usd: float = 0.0
    reason: str | None = None

    def asdict(self) -> dict[str, Any]:
        return {
            "order_id": self.order_id,
            "intent_id": self.intent_id,
            "status": self.status,
            "avg_price": self.avg_price,
            "filled_size": self.filled_size,
            "notional_usd": self.notional_usd,
            "fee_usd": self.fee_usd,
            "reason": self.reason,
            "fills": [asdict(f) for f in self.fills],
        }


def new_fill(*, order_id: str, intent_id: str, market: str,
             price: float, size: float, fee_usd: float) -> Fill:
    return Fill(
        fill_id=fill_id(),
        order_id=order_id,
        intent_id=intent_id,
        market=market,
        price=price,
        size=size,
        fee_usd=fee_usd,
        ts=now_iso(),
    )
