"""Current proposal validation evidence shared by SDK and native tools."""
from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

from ..core.atomic_write import atomic_write_text
from ..core.errors import NeryaError
from ..core.paths import WorkspacePaths
from ..core.redaction import redact_public_record
from ..evolution.patch_proposal import list_proposals
from .validator import validate_proposal_files
from .verification import source_revision
from .workflow_service import _safe_root, source_files


def _proposal_strategy_id(paths: WorkspacePaths, proposal_id: str) -> str:
    """Infer identity without reading the recursive proposal payload."""
    root = _safe_root(paths.proposals, proposal_id)
    proposal = next((p for p in list_proposals(paths) if p.id == proposal_id), None)
    if proposal is None:
        raise NeryaError("Strategy proposal not found")
    sid = (proposal.metadata or {}).get("strategy_id")
    if sid:
        return sid
    target = (proposal.target or "").split("/")
    if len(target) == 2 and target[0] == "strategies":
        return target[1]
    after = _safe_root(_safe_root(root, "after"), "strategies")
    sid = None
    if after.is_dir():
        for child in after.iterdir():
            if child.is_symlink():
                raise NeryaError("Symlink proposal payload is not allowed")
            if child.is_dir():
                if sid is not None:
                    raise NeryaError("Ambiguous strategy proposal; strategy_id is required")
                sid = child.name
    if not sid:
        raise NeryaError(f"proposal {proposal_id!r} has no after/strategies/* tree")
    return sid


def validate_proposal_receipt(
    paths: WorkspacePaths, proposal_id: str, *, strategy_id: str | None = None,
) -> dict[str, Any]:
    sid = strategy_id or _proposal_strategy_id(paths, proposal_id)
    files, source = source_files(paths, sid, proposal_id)
    if source["omitted_files"]:
        raise NeryaError("incomplete_source: source files were omitted; validation is unavailable")
    if not files:
        raise NeryaError(f"proposal {proposal_id!r} has no after/strategies/* tree")
    validation = validate_proposal_files(strategy_id=sid, files=files)
    latest, latest_source = source_files(paths, sid, proposal_id)
    if latest_source["omitted_files"] or latest != files:
        raise NeryaError("revision_conflict: package changed during validation; revalidate current source")
    report = redact_public_record(
        {**validation.asdict(), "source_revision": source_revision(files),
         "checked_at": datetime.now(timezone.utc).isoformat(),
         "scope": "Strategy structure and isolated candidate tests; not replay, activation or live execution."},
        identity_fields=frozenset({"package_hash", "source_revision"}),
    )
    # The evidence reference is producer-owned, derived only from the package digest.
    for check in report.get("checks", []):
        if check.get("check_id") == "strategy_behavior" and validation.package_hash:
            check["evidence_ref"] = f"source:{report['package_hash']}#behavioral"
    target = next(p for p in list_proposals(paths) if p.id == proposal_id)
    # Submitted review authority includes the original report bytes. Never reseal it.
    name = "validation_report.json" if target.state == "draft" else "latest_validation_receipt.json"
    atomic_write_text(target.path / name, json.dumps(report, indent=2, ensure_ascii=False) + "\n")
    return report
