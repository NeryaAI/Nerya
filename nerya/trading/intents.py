"""TradeIntent model. Immutable, validated at construction."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Literal

from ..core.errors import IntentValidationError
from ..core.ids import intent_id
from ..core.time import now_iso


@dataclass
class TradeIntent:
    intent_id: str
    strategy_id: str
    account_id: str
    market: str
    side: Literal["buy", "sell"]
    size: float
    size_unit: Literal["base", "quote", "usd"]
    order_type: Literal["market", "limit", "stop", "stop_limit"]
    limit_price: float | None = None
    stop_price: float | None = None
    time_in_force: Literal["gtc", "ioc", "fok", "post_only"] = "gtc"
    confidence: float = 0.0
    reasoning: str = ""
    source: Literal[
        "agent",
        "agent:native",
        "subagent",
        "script",
        "cron",
        "strategy_runtime",
        "strategy_agent",
        "strategy_triggered_agent",
    ] = "agent"
    trigger_event_id: str | None = None
    created_at: str = field(default_factory=now_iso)
    meta: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self):
        if self.side not in ("buy", "sell"):
            raise IntentValidationError(f"bad side: {self.side!r}")
        if self.size_unit not in ("base", "quote", "usd"):
            raise IntentValidationError(f"bad size_unit: {self.size_unit!r}")
        if self.order_type not in ("market", "limit", "stop", "stop_limit"):
            raise IntentValidationError(f"bad order_type: {self.order_type!r}")
        if self.size <= 0:
            raise IntentValidationError("size must be positive")
        if self.order_type in ("limit", "stop_limit") and self.limit_price is None:
            raise IntentValidationError(f"{self.order_type} requires limit_price")
        if not 0 <= self.confidence <= 1:
            raise IntentValidationError("confidence must be in [0,1]")
        if ":" not in self.market:
            raise IntentValidationError("market must be '<venue>:<symbol>'")

    @classmethod
    def new(cls, **kwargs) -> "TradeIntent":
        kwargs.setdefault("intent_id", intent_id())
        return cls(**kwargs)

    def asdict(self) -> dict[str, Any]:
        return asdict(self)

    @property
    def notional_usd_estimate(self) -> float:
        """Rough notional for pre-risk caps. For `usd` it's exact; for
        `base`/`quote` the Risk Gate converts via market snapshot."""
        if self.size_unit == "usd":
            return float(self.size)
        if self.size_unit == "quote" and self.limit_price:
            return float(self.size)
        if self.size_unit == "base" and self.limit_price:
            return float(self.size) * float(self.limit_price)
        return float(self.size)
