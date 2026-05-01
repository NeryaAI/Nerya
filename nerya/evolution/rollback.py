"""Rollback an applied proposal using the artifacts/before snapshot."""

from __future__ import annotations

import shutil
from typing import Any

from ..core import jsonl
from ..core.paths import WorkspacePaths
from .patch_proposal import list_proposals, set_state


def rollback_proposal(paths: WorkspacePaths, pid: str) -> dict[str, Any]:
    prop = next((p for p in list_proposals(paths) if p.id == pid), None)
    if prop is None:
        return {"ok": False, "reason": "not_found"}
    if prop.state != "applied":
        return {"ok": False, "reason": f"state_{prop.state}"}

    before = paths.evolution / "artifacts" / pid / "before"
    if not before.exists():
        return {"ok": False, "reason": "no_backup"}

    for src in before.rglob("*"):
        if src.is_file():
            rel = src.relative_to(before)
            dst = paths.root / rel
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dst)

    set_state(paths, pid, "rolled_back", note="restored from artifacts/before")
    jsonl.append(paths.journal("evolution"), {
        "kind": "proposal.rolled_back", "proposal_id": pid,
    })
    try:
        from .event_store import record_event

        record_event(
            paths,
            parent_id=prop.source_event_id,
            proposal_id=pid,
            mutation_scope=[prop.target] if prop.target else [],
            validation_status="failed",
            outcome="rolled_back",
            outcome_score=-1.0,
            summary=f"Proposal {pid} rolled back.",
            evidence_refs=list(prop.evidence_refs or []),
        )
    except Exception:
        pass
    return {"ok": True, "proposal_id": pid}
