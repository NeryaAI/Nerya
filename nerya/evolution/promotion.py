"""Apply an approved proposal. Only allowed for non-protected scopes."""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import Any

from ..core import jsonl, yaml_io
from ..core.errors import ProtectedScopeViolation
from ..core.paths import WorkspacePaths
from .patch_proposal import list_proposals, set_state, is_protected


def apply_proposal(paths: WorkspacePaths, pid: str) -> dict[str, Any]:
    prop = next((p for p in list_proposals(paths) if p.id == pid), None)
    if prop is None:
        return {"ok": False, "reason": "not_found"}
    if prop.state != "approved":
        return {"ok": False, "reason": f"state_{prop.state}"}

    target = prop.path / "target.yml"   # optional declared target
    meta_target = None
    if target.exists():
        decl = yaml_io.load(target, default={}) or {}
        meta_target = decl.get("target")

    if meta_target and is_protected(meta_target):
        raise ProtectedScopeViolation(f"cannot apply to protected scope: {meta_target}")

    # Back up any file the proposal brings as `after/<path>` into evolution/artifacts/<pid>/before/
    after_dir = prop.path / "after"
    artifacts = paths.evolution / "artifacts" / pid
    if after_dir.exists():
        (artifacts / "before").mkdir(parents=True, exist_ok=True)
        (artifacts / "after").mkdir(parents=True, exist_ok=True)
        for src in after_dir.rglob("*"):
            if src.is_file():
                rel = src.relative_to(after_dir)
                rel_posix = rel.as_posix()
                if is_protected(rel_posix):
                    raise ProtectedScopeViolation(
                        f"proposal {pid} tries to write protected path: {rel_posix}"
                    )
                dst = paths.root / rel
                if dst.exists():
                    before_dst = artifacts / "before" / rel
                    before_dst.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(dst, before_dst)
                dst.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(src, dst)
                after_dst = artifacts / "after" / rel
                after_dst.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(src, after_dst)

    set_state(paths, pid, "applied", note="applied by operator")
    jsonl.append(paths.journal("evolution"), {
        "kind": "proposal.applied",
        "proposal_id": pid,
        "artifacts": str(artifacts),
    })
    try:
        from .assets import record_capsule_from_proposal
        from .event_store import record_event

        capsule = record_capsule_from_proposal(paths, pid, outcome_score=1.0)
        record_event(
            paths,
            parent_id=prop.source_event_id,
            proposal_id=pid,
            mutation_scope=[prop.target] if prop.target else [],
            validation_status="passed" if capsule else "not_run",
            outcome="applied",
            outcome_score=1.0,
            summary=f"Proposal {pid} applied.",
            evidence_refs=list(prop.evidence_refs or []),
            metadata={"artifacts": str(artifacts), "capsule": capsule},
        )
    except Exception:
        pass
    return {"ok": True, "proposal_id": pid, "artifacts": str(artifacts)}
