"""Typed envelopes for Nerya-native self-evolution telemetry."""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, field
from typing import Any, Literal

from ..core.ids import new_id
from ..core.time import now_iso


SignalSource = Literal[
    "turn",
    "session",
    "tool",
    "memory",
    "proposal",
    "strategy",
    "trading",
    "skill",
    "script",
    "operator",
]

SignalSeverity = Literal["info", "warn", "critical"]
ValidationStatus = Literal["not_run", "passed", "failed", "skipped"]
EvolutionOutcome = Literal[
    "candidate",
    "proposed",
    "approved",
    "applied",
    "rejected",
    "rolled_back",
]


def stable_id(prefix: str, payload: Any) -> str:
    """Return a short deterministic id for deduped evolution records."""

    body = json.dumps(payload, sort_keys=True, ensure_ascii=False, default=str)
    digest = hashlib.sha256(body.encode("utf-8")).hexdigest()[:16]
    return f"{prefix}_{digest}"


@dataclass(frozen=True)
class EvolutionSignal:
    id: str
    ts: str
    source: SignalSource
    kind: str
    severity: SignalSeverity
    strategy_id: str | None
    evidence_refs: list[str]
    summary: str
    dedupe_key: str
    confidence: float = 1.0
    metadata: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def create(
        cls,
        *,
        source: SignalSource,
        kind: str,
        severity: SignalSeverity = "info",
        summary: str,
        evidence_refs: list[str] | None = None,
        strategy_id: str | None = None,
        dedupe_key: str | None = None,
        confidence: float = 1.0,
        metadata: dict[str, Any] | None = None,
    ) -> "EvolutionSignal":
        evidence = list(evidence_refs or [])
        key = dedupe_key or f"{source}:{kind}:{strategy_id or '*'}:{summary}"
        sid = stable_id("sig", {"source": source, "kind": kind, "key": key})
        return cls(
            id=sid,
            ts=now_iso(),
            source=source,
            kind=kind,
            severity=severity,
            strategy_id=strategy_id,
            evidence_refs=evidence,
            summary=summary,
            dedupe_key=key,
            confidence=max(0.0, min(1.0, float(confidence))),
            metadata=dict(metadata or {}),
        )

    @classmethod
    def from_record(cls, row: dict[str, Any]) -> "EvolutionSignal":
        return cls(
            id=str(row.get("id") or new_id("sig")),
            ts=str(row.get("ts") or now_iso()),
            source=str(row.get("source") or "turn"),  # type: ignore[arg-type]
            kind=str(row.get("kind") or "unknown"),
            severity=str(row.get("severity") or "info"),  # type: ignore[arg-type]
            strategy_id=(
                str(row.get("strategy_id")) if row.get("strategy_id") else None
            ),
            evidence_refs=[str(x) for x in (row.get("evidence_refs") or [])],
            summary=str(row.get("summary") or ""),
            dedupe_key=str(row.get("dedupe_key") or row.get("id") or ""),
            confidence=float(row.get("confidence") or 0.0),
            metadata=dict(row.get("metadata") or {}),
        )

    def asdict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class EvolutionEvent:
    id: str
    ts: str
    parent_id: str | None = None
    signals: list[str] = field(default_factory=list)
    genes_used: list[str] = field(default_factory=list)
    proposal_id: str | None = None
    mutation_scope: list[str] = field(default_factory=list)
    validation_status: ValidationStatus = "not_run"
    outcome: EvolutionOutcome = "candidate"
    outcome_score: float = 0.0
    strategy_id: str | None = None
    summary: str = ""
    evidence_refs: list[str] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def create(
        cls,
        *,
        parent_id: str | None = None,
        signals: list[str] | None = None,
        genes_used: list[str] | None = None,
        proposal_id: str | None = None,
        mutation_scope: list[str] | None = None,
        validation_status: ValidationStatus = "not_run",
        outcome: EvolutionOutcome = "candidate",
        outcome_score: float = 0.0,
        strategy_id: str | None = None,
        summary: str = "",
        evidence_refs: list[str] | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> "EvolutionEvent":
        return cls(
            id=new_id("evo"),
            ts=now_iso(),
            parent_id=parent_id,
            signals=list(signals or []),
            genes_used=list(genes_used or []),
            proposal_id=proposal_id,
            mutation_scope=list(mutation_scope or []),
            validation_status=validation_status,
            outcome=outcome,
            outcome_score=float(outcome_score),
            strategy_id=strategy_id,
            summary=summary,
            evidence_refs=list(evidence_refs or []),
            metadata=dict(metadata or {}),
        )

    @classmethod
    def from_record(cls, row: dict[str, Any]) -> "EvolutionEvent":
        return cls(
            id=str(row.get("id") or new_id("evo")),
            ts=str(row.get("ts") or now_iso()),
            parent_id=str(row.get("parent_id")) if row.get("parent_id") else None,
            signals=[str(x) for x in (row.get("signals") or [])],
            genes_used=[str(x) for x in (row.get("genes_used") or [])],
            proposal_id=(
                str(row.get("proposal_id")) if row.get("proposal_id") else None
            ),
            mutation_scope=[str(x) for x in (row.get("mutation_scope") or [])],
            validation_status=str(row.get("validation_status") or "not_run"),  # type: ignore[arg-type]
            outcome=str(row.get("outcome") or "candidate"),  # type: ignore[arg-type]
            outcome_score=float(row.get("outcome_score") or 0.0),
            strategy_id=(
                str(row.get("strategy_id")) if row.get("strategy_id") else None
            ),
            summary=str(row.get("summary") or ""),
            evidence_refs=[str(x) for x in (row.get("evidence_refs") or [])],
            metadata=dict(row.get("metadata") or {}),
        )

    def asdict(self) -> dict[str, Any]:
        return asdict(self)


__all__ = [
    "EvolutionEvent",
    "EvolutionOutcome",
    "EvolutionSignal",
    "SignalSeverity",
    "SignalSource",
    "ValidationStatus",
    "stable_id",
]
