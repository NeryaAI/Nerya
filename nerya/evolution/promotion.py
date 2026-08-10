"""Apply an approved proposal. Only allowed for non-protected scopes."""

from __future__ import annotations

import json
import hashlib
import os
import stat
import shutil
import tempfile
from contextlib import contextmanager
from pathlib import Path, PurePosixPath
from typing import Any, Iterator

from ..core import jsonl, yaml_io
from ..core.atomic_write import atomic_write_bytes, atomic_write_text
from ..core.errors import ProtectedScopeViolation
from ..core.paths import WorkspacePaths
from .patch_proposal import list_proposals, set_state, is_protected
from .candidate_bundle import (
    digest_staged_entries,
    proposal_deleted_paths,
    verify_candidate_bundle,
)


MATERIALIZED_PROPOSAL_KINDS = {
    "strategy_tuning_proposal",
    "strategy_package_proposal",
    "strategy_config_patch",
    "script_proposal",
    "skill_proposal",
    "trigger_route_patch",
}

PASSED_VALIDATION_STATES = {"passed", "safe", "ready", "ok"}
_MUTATION_MANIFEST_VERSION = 1
_ISOLATION_IGNORED_ROOTS = frozenset({
    ".git", ".pytest_cache", ".ruff_cache", "__pycache__",
    ".venv", "venv", "node_modules", "dist", "build",
    "backtests", "evolution", "history", "inbox", "journals", "memory",
    "outbox", "runs", "sessions", "state", "vault", "versions", "reviews",
})


@contextmanager
def isolated_candidate_workspaces(
    paths: WorkspacePaths,
    prop,
) -> Iterator[tuple[WorkspacePaths, WorkspacePaths, dict[str, Any]]]:
    """Yield stable baseline and challenger workspaces for candidate checks.

    The proposal staging tree is never used as a subprocess cwd.  Both roots
    start from the same stable snapshot, then only the challenger receives the
    mutation plan that promotion would apply.  Volatile journals/evolution
    state are intentionally omitted so validation cannot feed its own writes
    back into the snapshot or active-profile resolution.
    """

    plan = build_mutation_plan(paths, prop)
    source_raw = Path(paths.root)
    if source_raw.is_symlink() or not source_raw.is_dir():
        raise ProtectedScopeViolation("workspace root is not a regular directory")
    source_root = source_raw.resolve()
    with tempfile.TemporaryDirectory(prefix="nerya-candidate-") as temp_root:
        temp = Path(temp_root)
        baseline_root = temp / "baseline"
        challenger_root = temp / "challenger"
        _copy_stable_workspace(source_root, baseline_root)
        shutil.copytree(baseline_root, challenger_root, symlinks=False)
        _materialize_mutation_plan(WorkspacePaths(challenger_root), plan)
        yield (
            WorkspacePaths(baseline_root),
            WorkspacePaths(challenger_root),
            plan,
        )


def _copy_stable_workspace(source: Path, destination: Path) -> None:
    """Copy regular stable workspace files without following links."""

    destination.mkdir(parents=True, exist_ok=True)
    for path in sorted(source.rglob("*")):
        relative = path.relative_to(source)
        if any(part in _ISOLATION_IGNORED_ROOTS for part in relative.parts):
            continue
        target = destination / relative
        if path.is_symlink():
            raise ProtectedScopeViolation(
                f"candidate isolation refuses workspace symlink: {relative.as_posix()}"
            )
        if path.is_dir():
            target.mkdir(parents=True, exist_ok=True)
            continue
        if not path.is_file():
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, target)


def _materialize_mutation_plan(
    paths: WorkspacePaths,
    plan: dict[str, Any],
) -> None:
    """Apply a prevalidated plan to an isolated workspace only."""

    for rel_posix, data, mode in plan.get("after_entries") or []:
        dst = _workspace_path(paths, rel_posix)
        if _path_exists(dst) and dst.is_dir():
            raise IsADirectoryError(f"candidate target is a directory: {rel_posix}")
        dst.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_bytes(dst, data)
        os.chmod(dst, mode)
    for rel_posix in plan.get("deleted") or []:
        dst = _workspace_path(paths, rel_posix)
        if _path_exists(dst):
            if dst.is_dir():
                raise IsADirectoryError(f"candidate delete target is a directory: {rel_posix}")
            dst.unlink()


def build_mutation_plan(paths: WorkspacePaths, prop) -> dict[str, Any]:
    """Build the exact writes/deletes that promotion would apply."""

    after_dir = prop.path / "after"
    after_entries: list[tuple[str, bytes, int]] = []
    manifest: dict[str, Any] = {
        "version": _MUTATION_MANIFEST_VERSION,
        "created": [],
        "modified": [],
        "deleted": [],
        "deleted_missing": [],
        "applied_digests": {},
    }
    after_files = _after_files(after_dir)
    seen_after: set[str] = set()
    for src in after_files:
        rel_posix = _normalize_rel_path(src.relative_to(after_dir))
        if rel_posix in seen_after:
            continue
        seen_after.add(rel_posix)
        _validate_mutation_path(rel_posix, pid=prop.id, action="write")
        raw_dst = paths.root / rel_posix
        if src.is_symlink() or raw_dst.is_symlink():
            raise ProtectedScopeViolation(f"proposal path is a symlink: {rel_posix}")
        dst = _workspace_path(paths, rel_posix)
        if dst.is_symlink():
            raise ProtectedScopeViolation(f"proposal target is a symlink: {rel_posix}")
        if _path_exists(dst) and dst.is_dir():
            raise IsADirectoryError(f"proposal target is a directory: {rel_posix}")
        data, mode = _read_staged_file(src)
        (manifest["modified"] if _path_exists(dst) else manifest["created"]).append(rel_posix)
        after_entries.append((rel_posix, data, mode))

    deleted_declarations = _declared_deleted_files(prop)
    seen_deleted: set[str] = set()
    for raw in deleted_declarations:
        rel_posix = _normalize_rel_path(raw)
        if rel_posix in seen_deleted:
            continue
        seen_deleted.add(rel_posix)
        _validate_mutation_path(rel_posix, pid=prop.id, action="delete")
        if rel_posix in seen_after:
            raise ValueError(f"proposal declares both write and delete: {rel_posix}")
        raw_dst = paths.root / rel_posix
        if raw_dst.is_symlink():
            raise ProtectedScopeViolation(f"proposal delete target is a symlink: {rel_posix}")
        dst = _workspace_path(paths, rel_posix)
        if dst.is_symlink():
            raise ProtectedScopeViolation(f"proposal delete target is a symlink: {rel_posix}")
        if _path_exists(dst) and dst.is_dir():
            raise IsADirectoryError(f"proposal delete target is a directory: {rel_posix}")
        (manifest["deleted"] if _path_exists(dst) else manifest["deleted_missing"]).append(rel_posix)

    for key in ("created", "modified", "deleted", "deleted_missing"):
        manifest[key] = sorted(_unique_strings(manifest[key]))
    return {
        "after_entries": after_entries,
        "declared_deleted": sorted(seen_deleted),
        "deleted": list(manifest["deleted"]),
        "deleted_missing": list(manifest["deleted_missing"]),
        "manifest": manifest,
    }


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

    artifacts = paths.evolution / "artifacts" / pid
    gates = proposal_action_gates(paths, prop)
    if not gates.get("can_apply"):
        gate_blockers = [str(blocker) for blocker in (gates.get("blockers") or [])]
        blockers = [
            blocker
            for blocker in gate_blockers
            if blocker not in {
                "candidate_bundle_conflict",
                "validation_candidate_bundle_mismatch",
            }
        ]
        if blockers:
            return {
                "ok": False,
                "proposal_id": pid,
                "reason": blockers[0],
                "kind": prop.kind,
                "state": prop.state,
                "action_gates": gates,
            }
        # Keep the evidence-specific reason visible when the candidate bytes
        # changed after validation. A stale strategy package is a more useful
        # domain reason than the generic CAS conflict, so it wins above.
        if "validation_candidate_bundle_mismatch" in gate_blockers:
            return {
                "ok": False,
                "proposal_id": pid,
                "reason": "validation_candidate_bundle_mismatch",
                "kind": prop.kind,
                "state": prop.state,
                "action_gates": gates,
            }
    candidate_bundle = _load_candidate_bundle(prop)
    cas = verify_candidate_bundle(paths.root, prop.path, candidate_bundle)
    if not cas.get("ok"):
        return {
            "ok": False,
            "proposal_id": pid,
            "reason": "candidate_bundle_conflict",
            "candidate_bundle": cas,
        }
    try:
        mutation_plan = build_mutation_plan(paths, prop)
    except (OSError, ValueError, ProtectedScopeViolation) as exc:
        return {
            "ok": False,
            "proposal_id": pid,
            "reason": "invalid_mutation_plan",
            "detail": str(exc),
        }
    if not gates.get("can_apply"):
        blockers = [str(blocker) for blocker in (gates.get("blockers") or [])]
        reason = "proposal_gate_blocked"
        if "validation_candidate_bundle_mismatch" in blockers:
            reason = "validation_candidate_bundle_mismatch"
        elif "candidate_bundle_conflict" in blockers:
            reason = "candidate_bundle_conflict"
        elif blockers:
            reason = blockers[0]
        return {
            "ok": False,
            "proposal_id": pid,
            "reason": reason,
            "kind": prop.kind,
            "state": prop.state,
            "action_gates": gates,
        }
    expected_after_digest = str(candidate_bundle.get("after_digest") or "")
    actual_after_digest = digest_staged_entries(
        mutation_plan["after_entries"],
        deleted_paths=mutation_plan["declared_deleted"],
    )
    if actual_after_digest != expected_after_digest:
        return {
            "ok": False,
            "proposal_id": pid,
            "reason": "candidate_bundle_conflict",
            "candidate_bundle": {
                "ok": False,
                "reason": "candidate_bundle_changed",
                "mismatches": {
                    "after_digest": {
                        "expected": expected_after_digest,
                        "actual": actual_after_digest,
                    },
                },
            },
        }

    after_entries = mutation_plan["after_entries"]
    manifest = mutation_plan["manifest"]

    before_dir = artifacts / "before"
    after_artifact_dir = artifacts / "after"
    before_dir.mkdir(parents=True, exist_ok=True)
    after_artifact_dir.mkdir(parents=True, exist_ok=True)

    # Snapshot every pre-existing path before applying any write/delete.
    for rel_posix in [*manifest["modified"], *manifest["deleted"]]:
        src = _workspace_path(paths, rel_posix)
        if not _path_exists(src):
            continue
        if (paths.root / rel_posix).is_symlink() or src.is_symlink():
            raise ProtectedScopeViolation(
                f"workspace backup source is a symlink: {rel_posix}"
            )
        before_dst = before_dir / rel_posix
        before_dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, before_dst)

    applied_files: list[str] = []
    for rel_posix, data, mode in after_entries:
        dst = _workspace_path(paths, rel_posix)
        dst.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_bytes(dst, data)
        os.chmod(dst, mode)
        after_dst = after_artifact_dir / rel_posix
        after_dst.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_bytes(after_dst, data)
        os.chmod(after_dst, mode)
        manifest["applied_digests"][rel_posix] = hashlib.sha256(data).hexdigest()
        applied_files.append(rel_posix)

    deleted_files: list[str] = []
    for rel_posix in manifest["deleted"]:
        dst = _workspace_path(paths, rel_posix)
        if _path_exists(dst):
            dst.unlink()
            deleted_files.append(rel_posix)
    for rel_posix in manifest["deleted"]:
        manifest["applied_digests"].setdefault(rel_posix, None)

    manifest_path = artifacts / "manifest.json"
    atomic_write_text(
        manifest_path,
        json.dumps(manifest, indent=2, ensure_ascii=False) + "\n",
    )

    set_state(paths, pid, "applied", note="applied by operator")
    version_records = _record_strategy_versions(paths, prop, applied_files)
    jsonl.append(paths.journal("evolution"), {
        "kind": "proposal.applied",
        "proposal_id": pid,
        "artifacts": str(artifacts),
        "manifest": manifest,
        "strategy_versions": version_records,
    })
    try:
        from .assets import record_capsule_from_proposal
        from .event_store import record_event

        capsule = record_capsule_from_proposal(
            paths,
            pid,
            outcome_score=0.0,
            outcome_status="observing",
        )
        record_event(
            paths,
            parent_id=prop.source_event_id,
            proposal_id=pid,
            mutation_scope=[prop.target] if prop.target else [],
            validation_status="passed" if capsule else "not_run",
            outcome="applied",
            # Applying a proposal proves only that the operator gate allowed
            # the mutation. Paper/shadow/live observations decide reward.
            outcome_score=0.0,
            summary=f"Proposal {pid} applied.",
            evidence_refs=list(prop.evidence_refs or []),
            metadata={
                "artifacts": str(artifacts),
                "manifest": manifest,
                "capsule": capsule,
                "applied_files": applied_files,
                "deleted_files": deleted_files,
                "strategy_versions": version_records,
                "observation_status": "pending",
                "reward_status": "unevaluated",
            },
        )
    except Exception:
        pass
    return {
        "ok": True,
        "proposal_id": pid,
        "artifacts": str(artifacts),
        "manifest": manifest,
        "candidate_bundle": candidate_bundle,
        "manifest_path": str(manifest_path),
        "applied_files": applied_files,
        "deleted_files": deleted_files,
        "created_files": list(manifest["created"]),
        "modified_files": list(manifest["modified"]),
        "strategy_versions": version_records,
        "action_gates": gates,
    }


def _load_candidate_bundle(prop) -> dict[str, Any] | None:
    path = prop.path / "candidate_bundle.json"
    if path.exists():
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
            return raw if isinstance(raw, dict) else None
        except (OSError, json.JSONDecodeError):
            return None
    meta = yaml_io.load(prop.path / "proposal.yml", default={}) or {}
    raw = meta.get("candidate_bundle")
    return raw if isinstance(raw, dict) else None


def proposal_action_gates(paths: WorkspacePaths, proposal_or_id: Any) -> dict[str, Any]:
    """Return the hard action gates for applying an evolution proposal."""

    prop = proposal_or_id
    if isinstance(proposal_or_id, str):
        prop = next((p for p in list_proposals(paths) if p.id == proposal_or_id), None)
    if prop is None:
        return {
            "version": "proposal_action_gates_v1",
            "can_apply": False,
            "blockers": ["proposal_not_found"],
            "warnings": [],
        }
    metadata = prop.metadata or {}
    after_files = _after_files(prop.path / "after")
    deletion_error: str | None = None
    try:
        deleted_files = _declared_deleted_files(prop)
    except (OSError, ValueError) as exc:
        deleted_files = []
        deletion_error = str(exc)
    candidate_check = verify_candidate_bundle(
        paths.root,
        prop.path,
        _load_candidate_bundle(prop),
    )
    materialized_required = prop.kind in MATERIALIZED_PROPOSAL_KINDS
    blockers: list[str] = []
    warnings: list[str] = []
    if prop.state != "approved":
        blockers.append(f"state_{prop.state}")
    if not candidate_check.get("ok"):
        blockers.append("candidate_bundle_conflict")
    if deletion_error:
        blockers.append("invalid_deletion_declaration")
    validation = _proposal_validation_gate(
        paths,
        prop,
        required=materialized_required,
        candidate_check=candidate_check,
    )
    evidence_refs = _unique_strings([
        *list(prop.evidence_refs or []),
        *[
            str(ref)
            for ref in (validation.get("evidence_refs") or [])
            if ref
        ],
    ])
    if materialized_required:
        if metadata.get("advisory_only"):
            blockers.append("advisory_only")
        if not after_files and not deleted_files:
            blockers.append("no_materialized_changes")
        if not evidence_refs:
            blockers.append("missing_evidence_refs")
        if not validation.get("ok"):
            reason = str(validation.get("reason") or "validation_not_passed")
            blockers.append(reason)
    if prop.kind == "strategy_tuning_proposal":
        strategy_id = str(metadata.get("strategy_id") or "").strip()
        expected_hash = str(metadata.get("package_hash") or "").strip()
        if not strategy_id:
            blockers.append("missing_strategy_id")
        if not expected_hash:
            blockers.append("missing_strategy_package_hash")
        if strategy_id and expected_hash:
            try:
                from ..strategies.package import load_package

                current_hash = load_package(paths, strategy_id).content_hash
            except Exception:
                current_hash = ""
            if current_hash != expected_hash:
                blockers.append("strategy_package_changed")
    return {
        "version": "proposal_action_gates_v1",
        "can_apply": not blockers,
        "blockers": _unique_strings(blockers),
        "warnings": _unique_strings(warnings),
        "state": prop.state,
        "kind": prop.kind,
        "materialization": {
            "required": materialized_required,
            "after_file_count": len(after_files),
            "deleted_file_count": len(deleted_files),
            "paths": [
                src.relative_to(prop.path / "after").as_posix()
                for src in after_files[:20]
            ],
            "deleted_paths": [_normalize_rel_path(path) for path in deleted_files[:20]],
            "advisory_only": bool(metadata.get("advisory_only")),
        },
        "evidence": {
            "required": materialized_required,
            "count": len(evidence_refs),
            "refs": evidence_refs[:20],
        },
        "candidate_bundle": candidate_check,
        "validation": validation,
    }


def _after_files(after_dir: Path) -> list[Path]:
    if after_dir.is_symlink() or not after_dir.exists() or not after_dir.is_dir():
        return []
    return [src for src in sorted(after_dir.rglob("*")) if src.is_file()]


def _read_staged_file(path: Path) -> tuple[bytes, int]:
    """Read one staged file through a no-follow descriptor."""

    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(path, flags)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            raise ProtectedScopeViolation(f"staged path is not a regular file: {path}")
        with os.fdopen(fd, "rb") as handle:
            return handle.read(), stat.S_IMODE(info.st_mode)
    except Exception:
        try:
            os.close(fd)
        except OSError:
            pass
        raise


def _digest_staged_entries(
    entries: list[tuple[str, bytes, int]],
    *,
    deleted_paths: list[str] | tuple[str, ...] = (),
) -> str:
    """Compatibility wrapper for the canonical candidate digest."""

    return digest_staged_entries(entries, deleted_paths=deleted_paths)


def _declared_deleted_files(prop) -> list[str]:
    """Read optional deletion declarations without breaking old proposals."""

    return proposal_deleted_paths(prop.path)


def _normalize_rel_path(value: Any) -> str:
    text = str(value or "").strip().replace("\\", "/")
    path = PurePosixPath(text)
    if not text or path.is_absolute() or any(part == ".." for part in path.parts):
        raise ProtectedScopeViolation(f"invalid proposal path: {value!r}")
    normalized = path.as_posix()
    if normalized in {"", "."}:
        raise ProtectedScopeViolation(f"invalid proposal path: {value!r}")
    return normalized


def _validate_mutation_path(rel_posix: str, *, pid: str, action: str) -> None:
    if is_protected(rel_posix):
        raise ProtectedScopeViolation(
            f"proposal {pid} tries to {action} protected path: {rel_posix}"
        )


def _workspace_path(paths: WorkspacePaths, rel_posix: str) -> Path:
    root = paths.root.resolve()
    raw = root / rel_posix
    # A symlink in any parent would make a seemingly safe relative write
    # target another location.  Candidate staging is untrusted, so reject it
    # before resolving the path.
    current = root
    for part in PurePosixPath(rel_posix).parts[:-1]:
        current = current / part
        if current.is_symlink():
            raise ProtectedScopeViolation(
                f"proposal target parent is a symlink: {rel_posix}"
            )
    target = raw.resolve(strict=False)
    if target != root and root not in target.parents:
        raise ProtectedScopeViolation(f"proposal path escapes workspace: {rel_posix}")
    return raw


def _path_exists(path: Path) -> bool:
    # ``exists`` is false for a broken symlink; it is still a mutation target.
    return path.exists() or path.is_symlink()


def _proposal_validation_gate(
    paths: WorkspacePaths,
    prop,
    *,
    required: bool = True,
    candidate_check: dict[str, Any] | None = None,
) -> dict[str, Any]:
    plan_id = str(prop.validation_plan_id or "").strip()
    if plan_id:
        plan = _load_validation_plan(paths, plan_id)
        if not isinstance(plan, dict):
            return {
                "ok": False,
                "required": required,
                "source": "validation_plan",
                "plan_id": plan_id,
                "reason": "validation_plan_not_found",
            }
        if candidate_check is None:
            candidate_check = verify_candidate_bundle(
                paths.root,
                prop.path,
                _load_candidate_bundle(prop),
            )
        return _validation_plan_gate(
            plan,
            required=required,
            proposal=prop,
            candidate_check=candidate_check,
        )
    report = _load_validation_report(prop.path)
    if isinstance(report, dict):
        ok = bool(report.get("ok")) and not _validation_report_blockers(report)
        return {
            "ok": ok,
            "required": required,
            "source": "validation_report",
            "status": "passed" if ok else "failed",
            "reason": None if ok else "validation_report_failed",
            "evidence_refs": [f"file:{prop.path / 'validation_report.json'}"],
            "report": {
                "ok": report.get("ok"),
                "blockers": _validation_report_blockers(report)[:12],
                "warnings": _validation_report_warnings(report)[:12],
            },
        }
    return {
        "ok": not required,
        "required": required,
        "source": "none",
        "status": "missing",
        "reason": "missing_validation_evidence" if required else None,
    }


def _load_validation_plan(paths: WorkspacePaths, plan_id: str) -> dict[str, Any] | None:
    text = str(plan_id or "").strip()
    if (
        not text
        or "/" in text
        or "\\" in text
        or Path(text).name != text
        or text in {".", ".."}
    ):
        return None
    path = paths.evolution_validation_plans / f"{text}.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None
    return data if isinstance(data, dict) else None


def _validation_plan_gate(
    plan: dict[str, Any],
    *,
    required: bool,
    proposal: Any | None = None,
    candidate_check: dict[str, Any] | None = None,
) -> dict[str, Any]:
    status = str(plan.get("status") or "not_run").lower()
    blocked_reasons = [
        str(reason)
        for reason in (plan.get("blocked_reasons") or [])
        if reason
    ]
    required_steps = [
        step for step in (plan.get("steps") or [])
        if isinstance(step, dict) and bool(step.get("required", True))
    ]
    failed_required = [
        f"step_{idx}:{step.get('status') or 'not_run'}"
        for idx, step in enumerate(required_steps)
        if str(step.get("status") or "not_run").lower() not in PASSED_VALIDATION_STATES
    ]
    missing_evidence = [
        f"step_{idx}:{step.get('type') or 'unknown'}"
        for idx, step in enumerate(required_steps)
        if not str(step.get("evidence_ref") or "").strip()
    ]
    binding = _validation_candidate_binding(
        plan,
        proposal=proposal,
        candidate_check=candidate_check,
        required=required,
    )
    ok = (
        not blocked_reasons
        and status in PASSED_VALIDATION_STATES
        and not failed_required
        and not missing_evidence
        and binding.get("ok", True)
    )
    reason = None
    if blocked_reasons:
        reason = "validation_plan_blocked"
    elif failed_required:
        reason = "validation_required_steps_not_passed"
    elif missing_evidence:
        reason = "validation_required_steps_missing_evidence"
    elif status not in PASSED_VALIDATION_STATES:
        reason = f"validation_not_passed:{status}"
    elif not binding.get("ok", True):
        reason = str(binding.get("reason") or "validation_candidate_bundle_mismatch")
    return {
        "ok": ok or not required,
        "required": required,
        "source": "validation_plan",
        "plan_id": plan.get("id"),
        "status": status,
        "reason": reason if required else None,
        "blocked_reasons": blocked_reasons,
        "failed_required_steps": failed_required,
        "missing_evidence_steps": missing_evidence,
        "evidence_refs": [
            str(step.get("evidence_ref"))
            for step in required_steps
            if str(step.get("evidence_ref") or "").strip()
        ],
        "candidate_bundle_digest": str(plan.get("candidate_bundle_digest") or "") or None,
        "candidate_bundle_binding": binding,
    }


def _validation_candidate_binding(
    plan: dict[str, Any],
    *,
    proposal: Any | None,
    candidate_check: dict[str, Any] | None,
    required: bool,
) -> dict[str, Any]:
    """Keep proposal validation evidence tied to the frozen candidate bytes."""

    plan_digest = str(plan.get("candidate_bundle_digest") or "").strip()
    # A plan without a proposal/digest is the legacy hand-authored form.
    if proposal is None and not plan_digest:
        return {"ok": True, "bound": False}
    if not required and proposal is None:
        return {"ok": True, "bound": False}
    if not plan_digest:
        return {
            "ok": not required,
            "bound": True,
            "reason": "validation_candidate_bundle_unbound",
        }
    check = candidate_check or {}
    bundle = check.get("bundle") if isinstance(check.get("bundle"), dict) else {}
    bundle_digest = str(bundle.get("digest") or "").strip()
    current_digest = str(check.get("current_digest") or "").strip()
    if plan_digest != bundle_digest or (
        current_digest and current_digest != plan_digest
    ):
        return {
            "ok": not required,
            "bound": True,
            "reason": "validation_candidate_bundle_mismatch",
            "expected_digest": plan_digest,
            "bundle_digest": bundle_digest or None,
            "current_digest": current_digest or None,
            "candidate_bundle": check,
        }
    if not check.get("ok"):
        return {
            "ok": not required,
            "bound": True,
            "reason": "validation_candidate_bundle_mismatch",
            "expected_digest": plan_digest,
            "bundle_digest": bundle_digest or None,
            "current_digest": current_digest or None,
            "candidate_bundle": check,
        }
    return {
        "ok": True,
        "bound": True,
        "expected_digest": plan_digest,
        "bundle_digest": bundle_digest,
        "current_digest": current_digest or bundle_digest,
    }


def _load_validation_report(proposal_path: Path) -> dict[str, Any] | None:
    path = proposal_path / "validation_report.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None
    return data if isinstance(data, dict) else None


def _validation_report_blockers(report: dict[str, Any]) -> list[str]:
    blockers: list[str] = []
    for issue in report.get("issues") or []:
        if not isinstance(issue, dict):
            continue
        severity = str(issue.get("severity") or issue.get("level") or "").lower()
        code = str(issue.get("code") or issue.get("message") or "issue")
        if severity in {"error", "blocker", "critical"}:
            blockers.append(code)
    for value in report.get("blockers") or []:
        if value:
            blockers.append(str(value))
    return _unique_strings(blockers)


def _validation_report_warnings(report: dict[str, Any]) -> list[str]:
    warnings: list[str] = []
    for issue in report.get("issues") or []:
        if not isinstance(issue, dict):
            continue
        severity = str(issue.get("severity") or issue.get("level") or "").lower()
        code = str(issue.get("code") or issue.get("message") or "issue")
        if severity in {"warning", "warn"}:
            warnings.append(code)
    for value in report.get("warnings") or []:
        if value:
            warnings.append(str(value))
    return _unique_strings(warnings)


def _unique_strings(values: list[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for raw in values:
        text = str(raw or "").strip()
        if not text or text in seen:
            continue
        seen.add(text)
        out.append(text)
    return out


def _record_strategy_versions(
    paths: WorkspacePaths,
    prop,
    applied_files: list[str],
) -> list[dict[str, Any]]:
    strategy_ids = _strategy_ids_from_applied_files(applied_files)
    if not strategy_ids:
        return []
    records: list[dict[str, Any]] = []
    for strategy_id in strategy_ids:
        try:
            from ..strategies.package import load_package
            from ..strategies.state import StrategyVersionRegistry

            package = load_package(paths, strategy_id)
            record = StrategyVersionRegistry(paths, strategy_id).record(
                package,
                promoted_by="proposal_apply",
                proposal_id=prop.id,
                notes=f"Applied {prop.kind} via evolution proposal.",
            )
            records.append(record.asdict())
        except Exception:
            continue
    return records


def _strategy_ids_from_applied_files(applied_files: list[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for raw in applied_files:
        parts = str(raw or "").replace("\\", "/").split("/")
        if len(parts) < 3 or parts[0] != "strategies":
            continue
        strategy_id = parts[1].strip()
        if not strategy_id or strategy_id in seen:
            continue
        seen.add(strategy_id)
        out.append(strategy_id)
    return out
