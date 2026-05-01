"""Append-only event and signal store for self-evolution."""

from __future__ import annotations

from typing import Any

from ..core import jsonl
from ..core.paths import WorkspacePaths
from .events import EvolutionEvent, EvolutionSignal


def append_signal(
    paths: WorkspacePaths,
    signal: EvolutionSignal,
    *,
    dedupe: bool = True,
    window: int = 500,
) -> tuple[EvolutionSignal, bool]:
    """Persist a signal unless the recent window already has its key."""

    if dedupe:
        for row in jsonl.tail(paths.evolution_signals, n=window):
            if str(row.get("dedupe_key") or "") == signal.dedupe_key:
                return EvolutionSignal.from_record(row), False
    jsonl.append(paths.evolution_signals, signal.asdict(), stamp=False)
    return signal, True


def append_signals(
    paths: WorkspacePaths,
    signals: list[EvolutionSignal],
    *,
    dedupe: bool = True,
) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for sig in signals:
        stored, created = append_signal(paths, sig, dedupe=dedupe)
        row = stored.asdict()
        row["created"] = created
        out.append(row)
    return out


def list_signals(
    paths: WorkspacePaths,
    *,
    source: str | None = None,
    strategy_id: str | None = None,
    severity: str | None = None,
    kind: str | None = None,
    limit: int = 100,
) -> list[dict[str, Any]]:
    rows = jsonl.read_all(paths.evolution_signals)
    out: list[dict[str, Any]] = []
    for row in rows:
        if source and str(row.get("source") or "") != source:
            continue
        if strategy_id and str(row.get("strategy_id") or "") != strategy_id:
            continue
        if severity and str(row.get("severity") or "") != severity:
            continue
        if kind and str(row.get("kind") or "") != kind:
            continue
        out.append(row)
    return out[-max(1, int(limit)) :]


def append_event(paths: WorkspacePaths, event: EvolutionEvent) -> EvolutionEvent:
    jsonl.append(paths.evolution_events, event.asdict(), stamp=False)
    return event


def list_events(
    paths: WorkspacePaths,
    *,
    strategy_id: str | None = None,
    proposal_id: str | None = None,
    outcome: str | None = None,
    limit: int = 100,
) -> list[dict[str, Any]]:
    rows = jsonl.read_all(paths.evolution_events)
    out: list[dict[str, Any]] = []
    for row in rows:
        if strategy_id and str(row.get("strategy_id") or "") != strategy_id:
            continue
        if proposal_id and str(row.get("proposal_id") or "") != proposal_id:
            continue
        if outcome and str(row.get("outcome") or "") != outcome:
            continue
        out.append(row)
    return out[-max(1, int(limit)) :]


def record_event(
    paths: WorkspacePaths,
    *,
    parent_id: str | None = None,
    signals: list[str] | None = None,
    genes_used: list[str] | None = None,
    proposal_id: str | None = None,
    mutation_scope: list[str] | None = None,
    validation_status: str = "not_run",
    outcome: str = "candidate",
    outcome_score: float = 0.0,
    strategy_id: str | None = None,
    summary: str = "",
    evidence_refs: list[str] | None = None,
    metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    event = EvolutionEvent.create(
        parent_id=parent_id,
        signals=signals,
        genes_used=genes_used,
        proposal_id=proposal_id,
        mutation_scope=mutation_scope,
        validation_status=validation_status,  # type: ignore[arg-type]
        outcome=outcome,  # type: ignore[arg-type]
        outcome_score=outcome_score,
        strategy_id=strategy_id,
        summary=summary,
        evidence_refs=evidence_refs,
        metadata=metadata,
    )
    return append_event(paths, event).asdict()


__all__ = [
    "append_event",
    "append_signal",
    "append_signals",
    "list_events",
    "list_signals",
    "record_event",
]
