"""Strategy promotion gate.

Plan 2026-04-29 §11 P5 — Agent-generated strategies do not get to skip
straight from ``draft`` to ``live``. Every forward step on the lifecycle
graph requires concrete evidence:

| target          | required evidence                                  |
|-----------------|----------------------------------------------------|
| ``static_review`` | static-analysis pass (no connector / vault / net) |
| ``backtested``    | accepted backtest report artifact                |
| ``paper``         | paper account binding + protection rule presence |
| ``shadow``        | non-trivial paper PnL window (configurable)      |
| ``canary``        | non-trivial shadow window + protection rule      |
| ``live``          | clean canary window + operator approval          |

The gate is *cooperative*: ``request_promotion`` returns a typed
:class:`PromotionDecision`. Callers (CLI / dashboard / scheduler)
choose how to act on it — the only side effects done here are
persisting evidence + the promotion request audit trail. The actual
status mutation goes through :mod:`nerya.trading.strategy_crud`
(``set_status``) so the lifecycle table stays the single owner of
state changes.

Operators always retain a manual override path through
``strategy_crud.set_status`` — the gate is the safe default for
automation, not a hard wall.
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import asdict, dataclass, field
from typing import Any, Literal

from ..core.config import Config
from ..core.errors import TradingError
from ..core.ids import evidence_id as _new_evidence_id
from ..core.ids import promotion_id as _new_promotion_id
from ..core.paths import WorkspacePaths
from ..db.sqlite import connect
from .strategies import Strategy, load_strategy
from .strategy_lifecycle import (
    PROMOTION_TARGETS,
    InvalidTransition,
    promotion_target,
    validate_transition,
)

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------


EvidenceKind = Literal[
    "static_review",      # static analyzer verdict
    "backtest",           # backtest report
    "paper_window",       # paper-mode performance window
    "shadow_window",      # shadow-mode performance window
    "canary_window",      # canary-mode performance window
    "protection_check",   # protection rule presence on the strategy
    "subagent_review",    # subagent / human review verdict
    "operator_signoff",   # explicit operator approval (esp. for live)
]

PromotionState = Literal["pending", "approved", "rejected", "expired", "applied"]


@dataclass
class StrategyEvidence:
    evidence_id: str
    strategy_id: str
    kind: EvidenceKind
    ts: float
    passed: bool
    payload: dict[str, Any] = field(default_factory=dict)
    artifact_ref: str | None = None
    operator: str | None = None
    expires_at: float | None = None

    def asdict(self) -> dict[str, Any]:
        return {
            "evidence_id": self.evidence_id,
            "strategy_id": self.strategy_id,
            "kind": self.kind,
            "ts": self.ts,
            "passed": self.passed,
            "payload": self.payload,
            "artifact_ref": self.artifact_ref,
            "operator": self.operator,
            "expires_at": self.expires_at,
        }


@dataclass
class PromotionRecord:
    promotion_id: str
    strategy_id: str
    from_state: str
    to_state: str
    state: PromotionState
    ts_requested: float
    evidence: list[dict[str, Any]] = field(default_factory=list)
    reason_blocked: str | None = None
    approval_id: str | None = None
    ts_resolved: float | None = None
    ts_applied: float | None = None
    operator: str | None = None
    notes: str | None = None

    def asdict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class PromotionDecision:
    """Outcome of :func:`evaluate_promotion`.

    ``allow``        — every required evidence is present and fresh.
    ``needs_evidence`` — at least one required evidence is missing or expired.
    ``reject``       — illegal transition or strategy in a terminal state.
    ``escalate``     — gate would allow but operator approval is also
    required (currently only for the final ``canary -> live`` step).
    """

    verdict: Literal["allow", "needs_evidence", "reject", "escalate"]
    target: str
    reasons: list[str] = field(default_factory=list)
    missing_evidence: list[str] = field(default_factory=list)
    evidence_seen: list[str] = field(default_factory=list)

    def asdict(self) -> dict[str, Any]:
        return asdict(self)


# ---------------------------------------------------------------------------
# Evidence store
# ---------------------------------------------------------------------------


class EvidenceStore:
    """Persistence for :class:`StrategyEvidence`."""

    def __init__(self, paths: WorkspacePaths):
        self.paths = paths
        self._con = None

    def _con_lazy(self):
        if self._con is None:
            self._con = connect(self.paths.db)
        return self._con

    def record(
        self,
        *,
        strategy_id: str,
        kind: EvidenceKind,
        passed: bool,
        payload: dict[str, Any] | None = None,
        artifact_ref: str | None = None,
        operator: str | None = None,
        ttl_seconds: float | None = None,
    ) -> StrategyEvidence:
        ts = time.time()
        ev = StrategyEvidence(
            evidence_id=_new_evidence_id(),
            strategy_id=strategy_id,
            kind=kind,
            ts=ts,
            passed=passed,
            payload=dict(payload or {}),
            artifact_ref=artifact_ref,
            operator=operator,
            expires_at=(ts + ttl_seconds) if ttl_seconds else None,
        )
        self._con_lazy().execute(
            """
            INSERT INTO strategy_evidence (
                evidence_id, strategy_id, kind, ts, pass,
                payload_json, artifact_ref, operator, expires_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                ev.evidence_id, ev.strategy_id, ev.kind, ev.ts,
                1 if ev.passed else 0,
                json.dumps(ev.payload),
                ev.artifact_ref, ev.operator, ev.expires_at,
            ),
        )
        return ev

    def latest(self, strategy_id: str, kind: EvidenceKind) -> StrategyEvidence | None:
        row = self._con_lazy().execute(
            """
            SELECT * FROM strategy_evidence
             WHERE strategy_id = ? AND kind = ?
             ORDER BY ts DESC
             LIMIT 1
            """,
            (strategy_id, kind),
        ).fetchone()
        if row is None:
            return None
        return _row_to_evidence(row)

    def list_for(self, strategy_id: str) -> list[StrategyEvidence]:
        rows = self._con_lazy().execute(
            "SELECT * FROM strategy_evidence WHERE strategy_id = ? ORDER BY ts DESC",
            (strategy_id,),
        ).fetchall()
        return [_row_to_evidence(r) for r in rows]


def _row_to_evidence(row: Any) -> StrategyEvidence:
    return StrategyEvidence(
        evidence_id=str(row["evidence_id"]),
        strategy_id=str(row["strategy_id"]),
        kind=str(row["kind"]),  # type: ignore[arg-type]
        ts=float(row["ts"] or 0.0),
        passed=bool(row["pass"]),
        payload=json.loads(str(row["payload_json"] or "{}")),
        artifact_ref=row["artifact_ref"] or None,
        operator=row["operator"] or None,
        expires_at=(float(row["expires_at"]) if row["expires_at"] is not None else None),
    )


# ---------------------------------------------------------------------------
# Promotion store
# ---------------------------------------------------------------------------


class PromotionStore:
    """Persistence for :class:`PromotionRecord`."""

    def __init__(self, paths: WorkspacePaths):
        self.paths = paths
        self._con = None

    def _con_lazy(self):
        if self._con is None:
            self._con = connect(self.paths.db)
        return self._con

    def record(self, rec: PromotionRecord) -> None:
        self._con_lazy().execute(
            """
            INSERT OR REPLACE INTO strategy_promotions (
                promotion_id, strategy_id, from_state, to_state,
                state, reason_blocked, evidence_json, approval_id,
                ts_requested, ts_resolved, ts_applied, operator, notes
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                rec.promotion_id, rec.strategy_id, rec.from_state, rec.to_state,
                rec.state, rec.reason_blocked,
                json.dumps(rec.evidence),
                rec.approval_id,
                rec.ts_requested, rec.ts_resolved, rec.ts_applied,
                rec.operator, rec.notes,
            ),
        )

    def list_for(self, strategy_id: str, *, limit: int = 50) -> list[PromotionRecord]:
        rows = self._con_lazy().execute(
            "SELECT * FROM strategy_promotions WHERE strategy_id = ? ORDER BY ts_requested DESC LIMIT ?",
            (strategy_id, int(limit)),
        ).fetchall()
        return [_row_to_promotion(r) for r in rows]

    def latest(self, strategy_id: str) -> PromotionRecord | None:
        row = self._con_lazy().execute(
            "SELECT * FROM strategy_promotions WHERE strategy_id = ? ORDER BY ts_requested DESC LIMIT 1",
            (strategy_id,),
        ).fetchone()
        if row is None:
            return None
        return _row_to_promotion(row)


def _row_to_promotion(row: Any) -> PromotionRecord:
    return PromotionRecord(
        promotion_id=str(row["promotion_id"]),
        strategy_id=str(row["strategy_id"]),
        from_state=str(row["from_state"]),
        to_state=str(row["to_state"]),
        state=str(row["state"]),  # type: ignore[arg-type]
        reason_blocked=row["reason_blocked"] or None,
        evidence=list(json.loads(str(row["evidence_json"] or "[]"))),
        approval_id=row["approval_id"] or None,
        ts_requested=float(row["ts_requested"] or 0.0),
        ts_resolved=(float(row["ts_resolved"]) if row["ts_resolved"] is not None else None),
        ts_applied=(float(row["ts_applied"]) if row["ts_applied"] is not None else None),
        operator=row["operator"] or None,
        notes=row["notes"] or None,
    )


# ---------------------------------------------------------------------------
# Required-evidence matrix
# ---------------------------------------------------------------------------


# Required evidence per target state. A bare ``EvidenceKind`` means the
# latest matching evidence row must be ``passed=True`` and unexpired.
REQUIRED_EVIDENCE: dict[str, tuple[EvidenceKind, ...]] = {
    "static_review": ("static_review",),
    "backtested":    ("static_review", "backtest"),
    "paper":         ("static_review", "backtest"),
    "shadow":        ("static_review", "backtest", "paper_window", "protection_check"),
    "canary":        ("static_review", "backtest", "paper_window", "shadow_window", "protection_check"),
    "live":          ("static_review", "backtest", "paper_window", "shadow_window", "canary_window", "protection_check", "operator_signoff"),
}


def _evidence_passes(ev: StrategyEvidence | None) -> bool:
    if ev is None:
        return False
    if not ev.passed:
        return False
    if ev.expires_at is not None and ev.expires_at < time.time():
        return False
    return True


# ---------------------------------------------------------------------------
# Gate
# ---------------------------------------------------------------------------


def evaluate_promotion(
    paths: WorkspacePaths,
    *,
    strategy_id: str,
    target: str | None = None,
) -> PromotionDecision:
    """Decide whether ``strategy_id`` may move to ``target``.

    If ``target`` is None, the next forward state from
    :data:`PROMOTION_TARGETS` is used (``draft -> static_review`` etc.);
    if there is no forward state, we return ``reject`` with reason
    ``terminal``.
    """
    try:
        strategy: Strategy = load_strategy(paths, strategy_id)
    except Exception as exc:
        return PromotionDecision(
            verdict="reject",
            target=target or "",
            reasons=[f"strategy_unknown:{exc}"],
        )

    target_state = target or promotion_target(strategy.status)
    if target_state is None:
        return PromotionDecision(
            verdict="reject",
            target=strategy.status,
            reasons=["no_forward_state"],
        )

    try:
        validate_transition(strategy.status, target_state)
    except InvalidTransition as exc:
        return PromotionDecision(
            verdict="reject",
            target=target_state,
            reasons=[f"illegal_transition:{exc}"],
        )

    required = REQUIRED_EVIDENCE.get(target_state, ())
    store = EvidenceStore(paths)
    seen: list[str] = []
    missing: list[str] = []
    for kind in required:
        ev = store.latest(strategy_id, kind)
        if _evidence_passes(ev):
            seen.append(kind)
        else:
            missing.append(kind)

    reasons: list[str] = []
    if missing:
        reasons.append(f"missing_evidence:{','.join(missing)}")
        return PromotionDecision(
            verdict="needs_evidence",
            target=target_state,
            reasons=reasons,
            missing_evidence=missing,
            evidence_seen=seen,
        )

    # Promotion to ``live`` always requires an explicit, fresh
    # operator sign-off — even if the evidence catalogue is full.
    # The risk-gate hook for canary->live is the safety net; this is
    # the planning-side check so the dashboard doesn't pretend the
    # transition is automatic.
    if target_state == "live":
        op = store.latest(strategy_id, "operator_signoff")
        # ``operator_signoff`` already in REQUIRED_EVIDENCE so we
        # got it; flag the verdict as ``escalate`` to keep the call
        # site honest about needing a final manual click.
        reasons.append("live_promotion_requires_operator_click")
        return PromotionDecision(
            verdict="escalate",
            target=target_state,
            reasons=reasons,
            evidence_seen=seen,
        )

    return PromotionDecision(
        verdict="allow",
        target=target_state,
        reasons=reasons or ["ok"],
        evidence_seen=seen,
    )


def request_promotion(
    config: Config,
    *,
    strategy_id: str,
    target: str | None = None,
    operator: str | None = None,
    notes: str | None = None,
) -> PromotionRecord:
    """Persist a promotion request and return the record.

    The record is recorded as ``pending``; if the gate already returns
    ``allow`` the caller can immediately call :func:`apply_promotion`
    to flip the strategy state. The two-step shape keeps the audit
    trail clean even when a request is denied.
    """
    decision = evaluate_promotion(config.paths, strategy_id=strategy_id, target=target)
    rec = PromotionRecord(
        promotion_id=_new_promotion_id(),
        strategy_id=strategy_id,
        from_state=load_strategy(config.paths, strategy_id).status,
        to_state=decision.target,
        state="pending",
        ts_requested=time.time(),
        evidence=[{"kind": k} for k in decision.evidence_seen],
        operator=operator,
        notes=notes,
    )
    if decision.verdict == "reject":
        rec.state = "rejected"
        rec.reason_blocked = ";".join(decision.reasons)
        rec.ts_resolved = time.time()
    elif decision.verdict == "needs_evidence":
        rec.state = "pending"
        rec.reason_blocked = ";".join(decision.reasons)
    elif decision.verdict == "escalate":
        rec.state = "pending"
        rec.reason_blocked = ";".join(decision.reasons)
    else:
        rec.state = "approved"
        rec.ts_resolved = time.time()
    PromotionStore(config.paths).record(rec)
    return rec


def apply_promotion(
    config: Config,
    promotion_id_value: str,
) -> PromotionRecord:
    """Flip the strategy to its promoted state.

    Caller should only invoke this after observing a ``state==approved``
    record from :func:`request_promotion`. Re-running on an already
    applied record is a no-op (returns the existing row); attempting to
    apply a rejected/expired record raises :class:`TradingError`.
    """
    from .strategy_crud import set_status as _strategy_set_status

    store = PromotionStore(config.paths)
    rows = store._con_lazy().execute(  # type: ignore[attr-defined]
        "SELECT * FROM strategy_promotions WHERE promotion_id = ?",
        (promotion_id_value,),
    ).fetchall()
    if not rows:
        raise TradingError(f"unknown promotion_id: {promotion_id_value}")
    rec = _row_to_promotion(rows[0])
    if rec.state == "applied":
        return rec
    if rec.state in ("rejected", "expired"):
        raise TradingError(f"promotion {promotion_id_value} cannot be applied: state={rec.state}")
    if rec.state != "approved":
        raise TradingError(f"promotion {promotion_id_value} not approved (state={rec.state})")

    _strategy_set_status(
        config.paths,
        rec.strategy_id,
        rec.to_state,
        reason=f"promotion:{rec.promotion_id}",
    )
    rec.state = "applied"
    rec.ts_applied = time.time()
    store.record(rec)
    log.info(
        "promotion %s applied: %s %s -> %s",
        rec.promotion_id, rec.strategy_id, rec.from_state, rec.to_state,
    )
    return rec


__all__ = [
    "EvidenceKind",
    "EvidenceStore",
    "PromotionDecision",
    "PromotionRecord",
    "PromotionState",
    "PromotionStore",
    "REQUIRED_EVIDENCE",
    "StrategyEvidence",
    "apply_promotion",
    "evaluate_promotion",
    "request_promotion",
]
