"""Rollback an applied proposal using the artifacts/before snapshot."""

from __future__ import annotations

import json
import hashlib
import os
import shutil
from pathlib import Path, PurePosixPath
from typing import Any

from ..core import jsonl
from ..core.errors import ProtectedScopeViolation
from ..core.paths import WorkspacePaths
from .patch_proposal import is_protected, list_proposals, set_state


_MUTATION_MANIFEST_VERSION = 1


def rollback_proposal(paths: WorkspacePaths, pid: str) -> dict[str, Any]:
    prop = next((p for p in list_proposals(paths) if p.id == pid), None)
    if prop is None:
        return {"ok": False, "reason": "not_found"}
    if prop.state != "applied":
        return {"ok": False, "reason": f"state_{prop.state}"}

    artifacts = paths.evolution / "artifacts" / pid
    before = artifacts / "before"
    manifest_path = artifacts / "manifest.json"
    manifest: dict[str, Any] | None = None
    if manifest_path.exists():
        try:
            raw = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {"ok": False, "proposal_id": pid, "reason": "invalid_manifest"}
        if not isinstance(raw, dict) or int(raw.get("version") or 0) != _MUTATION_MANIFEST_VERSION:
            return {"ok": False, "proposal_id": pid, "reason": "invalid_manifest"}
        manifest = raw

    if manifest is not None:
        conflicts = _rollback_conflicts(paths, manifest)
        if conflicts:
            return {
                "ok": False,
                "proposal_id": pid,
                "reason": "rollback_conflict",
                "paths": conflicts,
            }

    if before.is_symlink() or (not before.exists() and manifest is None):
        return {"ok": False, "reason": "no_backup"}

    if manifest is None:
        # Legacy artifacts predate the manifest and can still be restored.
        before_files = _artifact_rel_files(before)
        after_files = _artifact_rel_files(artifacts / "after")
        removed_created: list[str] = []
        for rel in sorted(after_files - before_files):
            dst = _workspace_path(paths, rel)
            if dst.is_symlink():
                return {"ok": False, "proposal_id": pid,
                        "reason": "rollback_conflict", "paths": [rel]}
            if dst.is_file():
                dst.unlink()
                removed_created.append(rel)
        restored_files: list[str] = []
        for rel in sorted(before_files):
            src = before / rel
            _assert_artifact_file(src, before)
            dst = _workspace_path(paths, rel)
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dst)
            restored_files.append(rel)
        manifest_for_journal: dict[str, Any] = {
            "version": 0,
            "created": sorted(after_files - before_files),
            "modified": sorted(before_files),
            "deleted": [],
        }
    else:
        created = _manifest_paths(manifest, "created")
        modified = _manifest_paths(manifest, "modified")
        deleted = _manifest_paths(manifest, "deleted")
        overlap = (set(created) & set(modified)) | (set(created) & set(deleted)) | (set(modified) & set(deleted))
        if overlap:
            return {
                "ok": False,
                "proposal_id": pid,
                "reason": "invalid_manifest_overlap",
                "paths": sorted(overlap),
            }

        restore = [*modified, *deleted]
        missing_backups = [
            rel for rel in restore
            if not _is_safe_artifact_file(before / rel, before)
        ]
        if missing_backups:
            return {
                "ok": False,
                "proposal_id": pid,
                "reason": "manifest_backup_missing",
                "paths": missing_backups,
            }

        removed_created = []
        for rel in created:
            dst = _workspace_path(paths, rel)
            if dst.is_symlink():
                return {
                    "ok": False,
                    "proposal_id": pid,
                    "reason": "rollback_conflict",
                    "paths": [rel],
                }
            if dst.is_file():
                dst.unlink()
                removed_created.append(rel)
            elif dst.exists():
                return {
                    "ok": False,
                    "proposal_id": pid,
                    "reason": "created_path_not_file",
                    "path": rel,
                }

        restored_files = []
        for rel in restore:
            src = before / rel
            _assert_artifact_file(src, before)
            dst = _workspace_path(paths, rel)
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dst)
            restored_files.append(rel)
        manifest_for_journal = manifest

    set_state(paths, pid, "rolled_back", note="restored from artifacts/before")
    jsonl.append(paths.journal("evolution"), {
        "kind": "proposal.rolled_back",
        "proposal_id": pid,
        "manifest": manifest_for_journal,
        "removed_created_files": removed_created,
        "restored_files": restored_files,
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
    return {
        "ok": True,
        "proposal_id": pid,
        "manifest": manifest_for_journal,
        "manifest_path": str(manifest_path) if manifest is not None else None,
        "removed_created_files": removed_created,
        "restored_files": restored_files,
    }


def _manifest_paths(manifest: dict[str, Any], key: str) -> list[str]:
    value = manifest.get(key)
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, (list, tuple, set)):
        return []
    out: list[str] = []
    for raw in value:
        rel = _normalize_rel_path(raw)
        if rel not in out:
            out.append(rel)
    return sorted(out)


def _artifact_rel_files(root: Path) -> set[str]:
    if root.is_symlink() or not root.exists() or not root.is_dir():
        return set()
    return {
        path.relative_to(root).as_posix()
        for path in root.rglob("*")
        if path.is_file() and not path.is_symlink()
    }


def _is_safe_artifact_file(path: Path, root: Path) -> bool:
    if path.is_symlink() or not path.is_file():
        return False
    try:
        path.resolve().relative_to(root.resolve())
    except (OSError, ValueError):
        return False
    return True


def _assert_artifact_file(path: Path, root: Path) -> None:
    if not _is_safe_artifact_file(path, root):
        raise ProtectedScopeViolation(f"invalid rollback artifact: {path}")


def _file_digest(path: Path) -> str | None:
    if path.is_symlink() or not path.is_file():
        return None
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(path, flags)
    except OSError:
        return None
    try:
        digest = hashlib.sha256()
        with os.fdopen(fd, "rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
        return digest.hexdigest()
    except OSError:
        return None


def _rollback_conflicts(paths: WorkspacePaths, manifest: dict[str, Any]) -> list[str]:
    """Refuse to overwrite user edits made after the proposal was applied."""

    digests = manifest.get("applied_digests")
    if not isinstance(digests, dict):
        return []  # legacy manifest: preserve the old recovery path
    conflicts: list[str] = []
    created = _manifest_paths(manifest, "created")
    modified = _manifest_paths(manifest, "modified")
    deleted = _manifest_paths(manifest, "deleted")
    deleted_missing = _manifest_paths(manifest, "deleted_missing")
    for rel in [*created, *modified]:
        expected = str(digests.get(rel) or "")
        current = paths.root / rel
        if not expected or _file_digest(current) != expected:
            conflicts.append(rel)
    for rel in [*deleted, *deleted_missing]:
        current = paths.root / rel
        if current.exists() or current.is_symlink():
            conflicts.append(rel)
    return sorted(set(conflicts))


def _normalize_rel_path(value: Any) -> str:
    text = str(value or "").strip().replace("\\", "/")
    path = PurePosixPath(text)
    if not text or path.is_absolute() or any(part == ".." for part in path.parts):
        raise ProtectedScopeViolation(f"invalid rollback path: {value!r}")
    normalized = path.as_posix()
    if normalized in {"", "."}:
        raise ProtectedScopeViolation(f"invalid rollback path: {value!r}")
    return normalized


def _workspace_path(paths: WorkspacePaths, rel_posix: str) -> Path:
    if is_protected(rel_posix):
        raise ProtectedScopeViolation(f"rollback path is protected: {rel_posix}")
    root = paths.root.resolve()
    raw = root / rel_posix
    if raw.is_symlink():
        raise ProtectedScopeViolation(f"rollback path is a symlink: {rel_posix}")
    target = raw.resolve(strict=False)
    if target != root and root not in target.parents:
        raise ProtectedScopeViolation(f"rollback path escapes workspace: {rel_posix}")
    return target
