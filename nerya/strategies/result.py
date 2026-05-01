"""Strategy run result envelope.

Generated strategy code returns a :class:`StrategyResult`; the
:class:`~nerya.strategies.runner.StrategyRunner` consumes it, journals
the outcome, and decides whether to escalate, retry, or hand off to
the trading kernel. The shape is intentionally narrow so generated
code can't smuggle in side effects through the return value.

Three primary lifecycles map onto the same dataclass:

* **HOLD**         — ``ctx.result.hold(reason=...)`` — strategy looked
  but found no setup; runner journals and exits.
* **SUBMITTED**    — strategy called ``ctx.trading.submit_intent(...)``;
  the trading facade returns the canonical envelope, the runner wraps
  it as ``StrategyResult.from_trade_envelope(envelope)``.
* **OK / ERROR**   — generic terminal states for strategies that did
  bookkeeping work but didn't trade (``ctx.result.ok()``,
  ``ctx.result.error(message, kind=...)``).

The facade in :mod:`nerya.strategies.context` exposes a
:class:`ResultBuilder` so authors write ``ctx.result.hold(...)`` /
``ctx.result.ok(...)`` instead of constructing the dataclass directly;
this keeps the public contract tight even when we add new fields.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass, field
from typing import Any, Optional


class StrategyResultStatus(str, enum.Enum):
    """Closed taxonomy of strategy run outcomes.

    The runner uses these for journaling + dashboard rendering. We
    deliberately mirror the trading-kernel statuses (``rejected``,
    ``pending_approval``, ``filled``, ``partial``) so the operator
    sees one consistent vocabulary across both layers.
    """

    HOLD = "hold"
    OK = "ok"
    SUBMITTED = "submitted"
    REJECTED = "rejected"
    PENDING_APPROVAL = "pending_approval"
    FILLED = "filled"
    PARTIAL = "partial"
    ERROR = "error"

    @property
    def is_terminal(self) -> bool:
        """Whether the run is final (no follow-up tick expected)."""

        return self != StrategyResultStatus.PENDING_APPROVAL


@dataclass
class StrategyResult:
    """Single-tick strategy outcome.

    ``intent`` carries the redacted trade-intent payload when the
    strategy submitted one (``status`` in {SUBMITTED, REJECTED,
    PENDING_APPROVAL, FILLED, PARTIAL}). ``risk_decision`` is the
    Risk Gate envelope; both come straight from
    :func:`nerya.trading.submit.submit_trade_intent` so dashboards
    can render the full pipeline without a translation layer.

    ``metadata`` is the free-form bag for strategy-specific facts
    (signal strength, evidence anchors, indicator readings). The
    runner journals it verbatim; nothing else parses it.
    """

    status: StrategyResultStatus
    reason: str = ""
    intent: dict[str, Any] = field(default_factory=dict)
    order: dict[str, Any] = field(default_factory=dict)
    risk_decision: dict[str, Any] = field(default_factory=dict)
    approval_id: Optional[str] = None
    order_id: Optional[str] = None
    session_id: Optional[str] = None
    metadata: dict[str, Any] = field(default_factory=dict)
    error_kind: Optional[str] = None

    @classmethod
    def hold(cls, *, reason: str = "", metadata: Optional[dict[str, Any]] = None) -> "StrategyResult":
        """Strategy chose not to trade this tick."""

        return cls(
            status=StrategyResultStatus.HOLD,
            reason=str(reason or "").strip(),
            metadata=dict(metadata or {}),
        )

    @classmethod
    def ok(cls, *, reason: str = "", metadata: Optional[dict[str, Any]] = None) -> "StrategyResult":
        """Strategy completed bookkeeping work without trading.

        Used by news-following strategies that send messages, update
        state, or annotate evidence but don't submit an intent on this
        particular tick.
        """

        return cls(
            status=StrategyResultStatus.OK,
            reason=str(reason or "").strip(),
            metadata=dict(metadata or {}),
        )

    @classmethod
    def error(
        cls,
        *,
        message: str,
        kind: str = "strategy_error",
        metadata: Optional[dict[str, Any]] = None,
    ) -> "StrategyResult":
        """Strategy hit an unrecoverable internal error.

        The runner catches exceptions at the entrypoint boundary and
        wraps them into this shape automatically; strategy code that
        wants to attribute a *recoverable* failure (e.g. "data
        provider returned 503") should call ``ctx.result.error(...)``
        explicitly so the journal records the strategy's own framing.
        """

        return cls(
            status=StrategyResultStatus.ERROR,
            reason=str(message or "").strip(),
            metadata=dict(metadata or {}),
            error_kind=kind or "strategy_error",
        )

    @classmethod
    def from_trade_envelope(
        cls,
        envelope: dict[str, Any],
        *,
        reason: str = "",
        metadata: Optional[dict[str, Any]] = None,
    ) -> "StrategyResult":
        """Map a :func:`submit_trade_intent` envelope onto a result.

        The trade envelope status is one of ``rejected /
        pending_approval / filled / partial / submitted``. We pass
        them through verbatim; the operator-facing taxonomy already
        matches.
        """

        env = envelope or {}
        status_str = str(env.get("status") or "submitted").strip().lower()
        try:
            status = StrategyResultStatus(status_str)
        except ValueError:
            status = StrategyResultStatus.SUBMITTED
        return cls(
            status=status,
            reason=str(reason or "").strip(),
            intent=dict(env.get("intent") or {}),
            order=dict(env.get("order") or {}),
            risk_decision=dict(env.get("risk_decision") or {}),
            approval_id=env.get("approval_id"),
            order_id=env.get("order_id"),
            session_id=env.get("session_id"),
            metadata=dict(metadata or {}),
        )

    def asdict(self) -> dict[str, Any]:
        return {
            "status": self.status.value,
            "reason": self.reason,
            "intent": dict(self.intent),
            "order": dict(self.order),
            "risk_decision": dict(self.risk_decision),
            "approval_id": self.approval_id,
            "order_id": self.order_id,
            "session_id": self.session_id,
            "metadata": dict(self.metadata),
            "error_kind": self.error_kind,
            "is_terminal": self.status.is_terminal,
        }


# ---------------------------------------------------------------------------
# Public builder — what ``ctx.result`` returns
# ---------------------------------------------------------------------------


@dataclass
class ResultBuilder:
    """Convenience wrapper exposed as ``ctx.result``.

    Forwards to the :class:`StrategyResult` factory methods. We could
    have authors call the factories directly, but ``ctx.result.hold(...)``
    reads more naturally inside a strategy entrypoint and means the
    facade owns the public surface (so we can swap the implementation
    later without breaking generated code).
    """

    def hold(self, *, reason: str = "", metadata: Optional[dict[str, Any]] = None) -> StrategyResult:
        return StrategyResult.hold(reason=reason, metadata=metadata)

    def ok(self, *, reason: str = "", metadata: Optional[dict[str, Any]] = None) -> StrategyResult:
        return StrategyResult.ok(reason=reason, metadata=metadata)

    def error(
        self,
        *,
        message: str,
        kind: str = "strategy_error",
        metadata: Optional[dict[str, Any]] = None,
    ) -> StrategyResult:
        return StrategyResult.error(message=message, kind=kind, metadata=metadata)


__all__ = [
    "ResultBuilder",
    "StrategyResult",
    "StrategyResultStatus",
]
