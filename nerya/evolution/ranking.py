"""Proposal review readiness from explicit evidence, not invented quality scores."""
from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import PurePosixPath
from typing import Any

from ..core.atomic_write import atomic_write_text
from ..core.paths import WorkspacePaths
from ..core.time import now_iso
from .evidence_resolver import resolve_evidence_refs
from .patch_proposal import Proposal, list_proposals
from .reflection_engine import collect_strategy_observations


@dataclass(frozen=True)
class EvidenceBundle:
    strategy_id: str
    observations: dict[str, Any]

    def asdict(self) -> dict[str, Any]:
        return {"strategy_id": self.strategy_id, **self.observations}


def build_evidence(paths: WorkspacePaths, strategy_id: str) -> EvidenceBundle:
    return EvidenceBundle(strategy_id, collect_strategy_observations(paths, strategy_id))


@dataclass(frozen=True)
class RankedProposal:
    proposal: Proposal
    strategy_id: str | None
    resolved_refs: tuple[str, ...]
    unresolved_refs: tuple[str, ...]
    evidence_coverage: float | None
    rationale: str

    def asdict(self) -> dict[str, Any]:
        return {
            "proposal_id": self.proposal.id, "kind": self.proposal.kind,
            "state": self.proposal.state, "summary": self.proposal.summary,
            "strategy_id": self.strategy_id, "evidence_coverage": self.evidence_coverage,
            "resolved_refs": list(self.resolved_refs), "unresolved_refs": list(self.unresolved_refs),
            "validation_plan_id": self.proposal.validation_plan_id,
            "rationale": self.rationale,
        }


def _derive_strategy_id(proposal: Proposal) -> str | None:
    """Only structured ownership and actual mutation targets establish scope."""
    declared = str((proposal.metadata or {}).get("strategy_id") or "").strip()
    parts = PurePosixPath(str(proposal.target or "")).parts
    target = parts[1] if len(parts) >= 3 and parts[0] == "strategies" and ".." not in parts else ""
    if declared and (PurePosixPath(declared).name != declared or declared in {".", ".."}):
        return None
    if declared and target and declared != target:
        return None
    return declared or target or None


def rank_proposal(proposal: Proposal, *, paths: WorkspacePaths) -> RankedProposal:
    refs = tuple(dict.fromkeys(str(ref).strip() for ref in proposal.evidence_refs or () if str(ref).strip()))
    items = resolve_evidence_refs(paths, refs)["items"]
    resolved = tuple(item["ref"] for item in items if item.get("resolved") is True)
    unresolved = tuple(item["ref"] for item in items if item.get("resolved") is not True)
    coverage = len(resolved) / len(refs) if refs else None
    return RankedProposal(
        proposal=proposal, strategy_id=_derive_strategy_id(proposal),
        resolved_refs=resolved, unresolved_refs=unresolved, evidence_coverage=coverage,
        rationale=(f"{len(resolved)}/{len(refs)} explicit evidence references are resolvable. "
                   "Reference availability is not causal support, verified correctness, or investment quality; "
                   "review the source contents and run the declared validation before promotion."),
    )


def _created_at(proposal: Proposal) -> float:
    try:
        timestamp = datetime.fromisoformat(proposal.ts.replace("Z", "+00:00"))
        if timestamp.tzinfo is None:
            timestamp = timestamp.replace(tzinfo=timezone.utc)
        return timestamp.timestamp()
    except (TypeError, ValueError, OverflowError):
        return 0.0


def rank_proposals(paths: WorkspacePaths, *, strategy_id: str | None = None,
                   states: tuple[str, ...] | None = ("draft", "pending_review")) -> list[RankedProposal]:
    """Order by evidence availability, then real creation time; no semantic scoring."""
    ranked = []
    for proposal in list_proposals(paths):
        if states is not None and proposal.state not in states:
            continue
        if strategy_id is not None and _derive_strategy_id(proposal) != strategy_id:
            continue
        ranked.append(rank_proposal(proposal, paths=paths))
    ranked.sort(key=lambda row: (
        -(row.evidence_coverage if row.evidence_coverage is not None else -1.0),
        -_created_at(row.proposal), row.proposal.id,
    ))
    return ranked


def write_ranking_snapshot(paths: WorkspacePaths, ranked: list[RankedProposal]) -> dict[str, Any]:
    snapshot = {"generated_at": now_iso(), "count": len(ranked),
                "ranked": [row.asdict() for row in ranked]}
    path = paths.evolution / "ranking.json"
    atomic_write_text(path, json.dumps(snapshot, ensure_ascii=False, indent=2))
    return {"path": str(path), "count": len(ranked)}
