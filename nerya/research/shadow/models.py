"""Shadow runtime data models."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal, Optional


ShadowStatus = Literal["pending", "running", "completed", "failed", "cancelled"]


@dataclass(frozen=True, slots=True)
class ShadowEvent:
    """A single market/signal/decision event in a shadow run."""

    ts: str
    kind: Literal["signal", "intent", "risk_decision", "virtual_fill", "log"]
    symbol: str
    detail: dict[str, Any] = field(default_factory=dict)

    def asdict(self) -> dict[str, Any]:
        return {
            "ts": self.ts,
            "kind": self.kind,
            "symbol": self.symbol,
            "detail": dict(self.detail),
        }


@dataclass(frozen=True, slots=True)
class ShadowFill:
    """A virtual fill — not a paper or live order."""

    ts: str
    symbol: str
    side: Literal["buy", "sell"]
    price: float
    quantity: float
    notional_usd: float

    def asdict(self) -> dict[str, Any]:
        return {
            "ts": self.ts,
            "symbol": self.symbol,
            "side": self.side,
            "price": self.price,
            "quantity": self.quantity,
            "notional_usd": self.notional_usd,
        }


@dataclass(slots=True)
class ShadowReport:
    """Final structured result of a shadow run."""

    run_id: str
    strategy_id: str
    candidate_id: str
    started_at: str
    finished_at: Optional[str]
    status: ShadowStatus
    metrics: dict[str, Any]
    risk_summary: dict[str, Any]
    artifacts: dict[str, str]

    def asdict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "strategy_id": self.strategy_id,
            "candidate_id": self.candidate_id,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "status": self.status,
            "metrics": dict(self.metrics),
            "risk_summary": dict(self.risk_summary),
            "artifacts": dict(self.artifacts),
        }


@dataclass(slots=True)
class ShadowRun:
    """A run record kept in the shadow store while/after the run executes."""

    run_id: str
    strategy_id: str
    candidate_id: str
    started_at: str
    status: ShadowStatus = "pending"
    config: dict[str, Any] = field(default_factory=dict)
    finished_at: Optional[str] = None
    events_path: Optional[str] = None
    fills_path: Optional[str] = None
    report_path: Optional[str] = None
    error: Optional[str] = None

    def asdict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "strategy_id": self.strategy_id,
            "candidate_id": self.candidate_id,
            "started_at": self.started_at,
            "status": self.status,
            "config": dict(self.config),
            "finished_at": self.finished_at,
            "events_path": self.events_path,
            "fills_path": self.fills_path,
            "report_path": self.report_path,
            "error": self.error,
        }


__all__ = [
    "ShadowEvent",
    "ShadowFill",
    "ShadowReport",
    "ShadowRun",
    "ShadowStatus",
]
