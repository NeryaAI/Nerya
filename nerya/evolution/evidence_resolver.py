"""Resolve evolution evidence refs into operator-facing artifacts."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from ..core import jsonl
from ..core.paths import WorkspacePaths
from ..core.redaction import redact_display_dict, redact_text
from .patch_proposal import list_proposals


_JOURNAL_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
_PREVIEW_LIMIT = 12000


def resolve_evidence_refs(
    paths: WorkspacePaths,
    refs: list[str] | tuple[str, ...],
) -> dict[str, Any]:
    items = [_resolve_one(paths, str(ref)) for ref in refs if str(ref).strip()]
    return {"ok": True, "count": len(items), "items": items}


def _resolve_one(paths: WorkspacePaths, ref: str) -> dict[str, Any]:
    ref = ref.strip()
    if ref.startswith("proposal:"):
        return _proposal_ref(paths, ref, ref.split(":", 1)[1])
    if ref.startswith("strategy_tuning:"):
        return _strategy_tuning_ref(paths, ref, ref.split(":", 1)[1])
    if ref.startswith("validation:"):
        return _validation_ref(paths, ref, ref.split(":", 1)[1])
    if ref.startswith("journal:"):
        return _journal_ref(paths, ref)
    if ref.startswith("file:"):
        return _file_ref(paths, ref, ref.split(":", 1)[1])
    if ref.startswith("turn:"):
        return _journal_lookup_ref(paths, ref, "turn", "turn_id", ref.split(":", 1)[1])
    if ref.startswith("session:"):
        return _journal_lookup_ref(paths, ref, "session", "session_id", ref.split(":", 1)[1])
    return _unresolved(ref, "unsupported_ref")


def _file_ref(paths: WorkspacePaths, ref: str, raw_path: str) -> dict[str, Any]:
    if not raw_path:
        return _unresolved(ref, "file_path_missing")
    path = Path(raw_path).expanduser()
    if not path.is_absolute():
        path = paths.root / path
    try:
        resolved = path.resolve()
        root = paths.root.resolve()
    except OSError:
        return _unresolved(ref, "file_not_found")
    if not resolved.is_relative_to(root):
        return _unresolved(ref, "file_outside_workspace")
    artifact = _file_artifact(resolved)
    if not artifact:
        return _unresolved(ref, "file_not_found")
    return {
        "ref": ref,
        "type": "file",
        "resolved": True,
        "title": resolved.name,
        "summary": str(resolved.relative_to(root)),
        "path": str(resolved),
        "artifacts": [artifact],
    }


def _proposal_ref(paths: WorkspacePaths, ref: str, proposal_id: str) -> dict[str, Any]:
    proposal = next((p for p in list_proposals(paths) if p.id == proposal_id), None)
    if proposal is None:
        return _unresolved(ref, "proposal_not_found")
    artifacts = _proposal_artifacts(proposal.path)
    return {
        "ref": ref,
        "type": "proposal",
        "resolved": True,
        "title": f"Proposal {proposal.id}",
        "summary": proposal.summary,
        "path": str(proposal.path),
        "record": redact_display_dict(proposal.asdict()),
        "artifacts": artifacts,
    }


def _strategy_tuning_ref(paths: WorkspacePaths, ref: str, run_id: str) -> dict[str, Any]:
    rows = [
        row for row in jsonl.read_all(paths.journal("strategy_evolution"))
        if str(row.get("run_id") or "") == run_id
    ]
    if not rows:
        return _unresolved(ref, "strategy_tuning_run_not_found")
    row = rows[-1]
    sid = str(row.get("strategy_id") or "")
    review_path = Path(str(row.get("review_path") or "")) if row.get("review_path") else None
    audit_path = Path(str(row.get("audit_path") or "")) if row.get("audit_path") else None
    if sid:
        review_path = review_path or paths.strategy(sid) / "reviews" / f"tuning_{run_id}.md"
        audit_path = audit_path or paths.strategy(sid) / "reviews" / f"tuning_{run_id}_audit.json"
    artifacts = []
    for path in (review_path, audit_path):
        artifact = _file_artifact(path) if path else None
        if artifact:
            artifacts.append(artifact)
    return {
        "ref": ref,
        "type": "strategy_tuning",
        "resolved": True,
        "title": f"Strategy tuning {run_id}",
        "summary": str(row.get("reason") or row.get("status") or "strategy tuning run"),
        "path": str(review_path or audit_path or ""),
        "record": redact_display_dict(row),
        "artifacts": artifacts,
    }


def _validation_ref(paths: WorkspacePaths, ref: str, value: str) -> dict[str, Any]:
    if ":step:" in value:
        run_id, raw_step = value.split(":step:", 1)
        run = _read_validation_run(paths, run_id)
        if not isinstance(run, dict):
            return _unresolved(ref, "validation_run_not_found")
        try:
            step_index = int(raw_step)
        except ValueError:
            return _unresolved(ref, "validation_step_index_invalid")
        step = next(
            (s for s in run.get("steps") or [] if int(s.get("index", -1)) == step_index),
            None,
        )
        if not isinstance(step, dict):
            return _unresolved(ref, "validation_step_not_found")
        return {
            "ref": ref,
            "type": "validation_step",
            "resolved": True,
            "title": f"Validation {run_id} step {step_index}",
            "summary": str(step.get("status") or ""),
            "path": str(paths.evolution / "validation_runs" / f"{run_id}.json"),
            "record": redact_display_dict(step),
            "artifacts": _validation_step_artifacts(paths, step),
        }
    plan = _read_validation_plan(paths, value)
    if isinstance(plan, dict):
        return {
            "ref": ref,
            "type": "validation_plan",
            "resolved": True,
            "title": f"Validation plan {value}",
            "summary": str(plan.get("status") or ""),
            "path": str(paths.evolution_validation_plans / f"{value}.json"),
            "record": redact_display_dict(plan),
        }
    run = _read_validation_run(paths, value)
    if isinstance(run, dict):
        return {
            "ref": ref,
            "type": "validation_run",
            "resolved": True,
            "title": f"Validation run {value}",
            "summary": str(run.get("status") or ""),
            "path": str(paths.evolution / "validation_runs" / f"{value}.json"),
            "record": redact_display_dict(run),
        }
    return _unresolved(ref, "validation_not_found")


def _journal_ref(paths: WorkspacePaths, ref: str) -> dict[str, Any]:
    parts = ref.split(":")
    if len(parts) != 3:
        return _unresolved(ref, "journal_ref_invalid")
    _, name, raw_index = parts
    if not _JOURNAL_RE.match(name):
        return _unresolved(ref, "journal_name_invalid")
    try:
        index = int(raw_index)
    except ValueError:
        return _unresolved(ref, "journal_index_invalid")
    rows = jsonl.read_all(paths.journal(name))
    if index < 0 or index >= len(rows):
        return _unresolved(ref, "journal_row_not_found")
    row = rows[index]
    return {
        "ref": ref,
        "type": "journal",
        "resolved": True,
        "title": f"{name} journal row {index}",
        "summary": str(row.get("kind") or row.get("summary") or ""),
        "path": str(paths.journal(name)),
        "record": redact_display_dict(row),
        "metadata": {"journal": name, "index": index},
    }


def _journal_lookup_ref(
    paths: WorkspacePaths,
    ref: str,
    ref_type: str,
    key: str,
    value: str,
) -> dict[str, Any]:
    matches: list[dict[str, Any]] = []
    for name in ("agent", "evolution", "strategy_evolution"):
        for index, row in enumerate(jsonl.read_all(paths.journal(name))):
            if str(row.get(key) or "") == value:
                matches.append({"journal": name, "index": index, "row": row})
    if not matches:
        return _unresolved(ref, f"{ref_type}_not_found")
    return {
        "ref": ref,
        "type": ref_type,
        "resolved": True,
        "title": f"{ref_type.title()} {value}",
        "summary": f"{len(matches)} matching journal row(s)",
        "record": redact_display_dict(matches[:20]),
        "metadata": {"count": len(matches)},
    }


def _proposal_artifacts(root: Path) -> list[dict[str, Any]]:
    artifacts: list[dict[str, Any]] = []
    if not root.exists():
        return artifacts
    wanted = {
        "proposal.yml", "target.yml", "rationale.md", "test_plan.md",
        "rollback.md", "diff.patch", "tuning_run.json", "tuning_review.md",
        "tuning_audit.json", "materialization.json",
    }
    for path in sorted(root.iterdir()):
        if path.is_file() and path.name in wanted:
            artifact = _file_artifact(path)
            if artifact:
                artifacts.append(artifact)
    after = root / "after"
    if after.exists():
        for path in sorted(p for p in after.rglob("*") if p.is_file())[:40]:
            artifact = _file_artifact(path)
            if artifact:
                artifact["metadata"] = {
                    "scope": "after",
                    "workspace_path": path.relative_to(after).as_posix(),
                }
                artifacts.append(artifact)
    return artifacts[:80]


def _file_artifact(path: Path | None) -> dict[str, Any] | None:
    if path is None or not path.exists() or not path.is_file():
        return None
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    preview = redact_text(text)
    truncated = len(preview) > _PREVIEW_LIMIT
    if truncated:
        preview = preview[:_PREVIEW_LIMIT] + "\n... [truncated]"
    return {
        "title": path.name,
        "path": str(path),
        "size": path.stat().st_size,
        "preview": preview,
        "truncated": truncated,
    }


def _validation_step_artifacts(paths: WorkspacePaths, step: dict[str, Any]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    root = paths.root.resolve()
    for raw in step.get("artifacts") or []:
        if not isinstance(raw, dict):
            continue
        value = raw.get("path")
        if not value:
            continue
        path = Path(str(value)).expanduser()
        if not path.is_absolute():
            path = paths.root / path
        try:
            resolved = path.resolve()
        except OSError:
            continue
        if not resolved.is_relative_to(root):
            continue
        artifact = _file_artifact(resolved)
        if not artifact:
            continue
        artifact["metadata"] = {
            "kind": raw.get("kind"),
            "evidence_ref": raw.get("evidence_ref"),
        }
        out.append(artifact)
    return out


def _read_validation_plan(paths: WorkspacePaths, plan_id: str) -> Any:
    return _read_json(paths.evolution_validation_plans / f"{plan_id}.json")


def _read_validation_run(paths: WorkspacePaths, run_id: str) -> Any:
    return _read_json(paths.evolution / "validation_runs" / f"{run_id}.json")


def _read_json(path: Path) -> Any:
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


def _unresolved(ref: str, reason: str) -> dict[str, Any]:
    return {
        "ref": ref,
        "type": "unknown",
        "resolved": False,
        "title": ref,
        "summary": reason,
        "reason": reason,
    }


__all__ = ["resolve_evidence_refs"]
