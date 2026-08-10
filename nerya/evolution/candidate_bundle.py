"""Immutable proposal inputs and apply-time compare-and-swap checks.

The proposal directory is an untrusted staging area.  A small, content-addressed
record makes the reviewed candidate explicit and prevents applying a proposal
against a workspace or runtime contract that changed after review.
"""

from __future__ import annotations

import hashlib
import json
import os
import stat
from pathlib import Path
from pathlib import PurePosixPath
from typing import Any, Iterable

from ..core import yaml_io


BUNDLE_VERSION = 2
_DELETE_KEYS = ("deleted_files", "delete_files", "deletions", "deleted", "delete")

# Volatile runtime state is deliberately excluded from the base revision.  A
# proposal must conflict with user-owned source/config edits, not with journals
# written by the act of reviewing it.
_VOLATILE_ROOTS = frozenset({
    ".git", ".pytest_cache", ".ruff_cache", "__pycache__",
    "backtests", "evolution", "history", "inbox", "journals", "memory",
    "outbox", "runs", "sessions", "state", "vault", "versions", "reviews",
})
_RUNTIME_FINGERPRINTS = {
    "model_schema": ("llm/model_registry.py",),
    "tool_schema": ("tools/registry.py", "tools/native/bootstrap.py"),
    "eval_suite": ("evals/scenario.py", "evals/scenarios/__init__.py"),
}


def build_candidate_bundle(
    workspace_root: Path,
    proposal_root: Path,
    *,
    context: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Create a deterministic bundle record for a staged proposal."""

    supplied = dict(context or {})
    deleted_paths = proposal_deleted_paths(proposal_root)
    runtime = {
        key: _runtime_digest(paths)
        for key, paths in _RUNTIME_FINGERPRINTS.items()
    }
    values: dict[str, Any] = {
        "base_revision": str(
            supplied.get("base_revision") or workspace_revision(workspace_root)
        ),
        "after_digest": digest_after(
            proposal_root / "after",
            deleted_paths=deleted_paths,
        ),
        "deleted_paths": deleted_paths,
    }
    report_digest = _file_digest(proposal_root / "validation_report.json")
    if report_digest:
        values["validation_report_digest"] = report_digest
    frozen_context = {
        key: supplied[key]
        for key in (
            "model_schema", "model_schema_digest", "tool_schema",
            "tool_schema_digest", "eval_suite", "eval_suite_digest",
        )
        if key in supplied
    }
    for key in _RUNTIME_FINGERPRINTS:
        explicit_digest = supplied.get(f"{key}_digest")
        explicit = supplied.get(key)
        if explicit_digest is not None:
            values[f"{key}_digest"] = str(explicit_digest)
        else:
            values[f"{key}_digest"] = _value_digest(
                explicit if explicit is not None else runtime[key]
            )
    bundle = {"version": BUNDLE_VERSION, **values}
    if frozen_context:
        bundle["context"] = frozen_context
    bundle["digest"] = _bundle_digest(bundle)
    return bundle


def verify_candidate_bundle(
    workspace_root: Path,
    proposal_root: Path,
    bundle: dict[str, Any] | None,
    *,
    context: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Recompute the candidate and return a fail-closed CAS verdict."""

    symlinks = _symlink_paths(Path(proposal_root) / "after")
    if symlinks:
        return {
            "ok": False,
            "reason": "candidate_bundle_symlink",
            "paths": symlinks[:32],
        }
    if not isinstance(bundle, dict):
        return {"ok": False, "reason": "candidate_bundle_missing_or_invalid"}
    try:
        version = int(bundle.get("version") or 0)
    except (TypeError, ValueError):
        return {"ok": False, "reason": "candidate_bundle_missing_or_invalid"}
    if version != BUNDLE_VERSION:
        return {"ok": False, "reason": "candidate_bundle_missing_or_invalid"}

    expected_digest = str(bundle.get("digest") or "")
    signed_fields = {
        key: bundle.get(key)
        for key in (
            "version", "base_revision", "after_digest", "deleted_paths",
            "model_schema_digest", "tool_schema_digest", "eval_suite_digest",
            "context",
        )
    }
    if "validation_report_digest" in bundle:
        signed_fields["validation_report_digest"] = bundle.get(
            "validation_report_digest"
        )
    if not expected_digest or _bundle_digest(signed_fields) != expected_digest:
        return {"ok": False, "reason": "candidate_bundle_digest_invalid"}

    actual_base = workspace_revision(workspace_root)
    try:
        actual_deleted = proposal_deleted_paths(proposal_root)
    except ValueError as exc:
        return {
            "ok": False,
            "reason": "candidate_bundle_deleted_paths_invalid",
            "detail": str(exc),
        }
    declared_deleted = bundle.get("deleted_paths")
    if not isinstance(declared_deleted, list):
        return {"ok": False, "reason": "candidate_bundle_missing_or_invalid"}
    if [str(value) for value in declared_deleted] != actual_deleted:
        return {
            "ok": False,
            "reason": "candidate_bundle_changed",
            "mismatches": {
                "deleted_paths": {
                    "expected": declared_deleted,
                    "actual": actual_deleted,
                },
            },
        }
    actual_after = digest_after(proposal_root / "after", deleted_paths=actual_deleted)
    mismatches: dict[str, dict[str, str]] = {}
    if actual_base != str(bundle.get("base_revision") or ""):
        mismatches["base_revision"] = {
            "expected": str(bundle.get("base_revision") or ""),
            "actual": actual_base,
        }
    if actual_after != str(bundle.get("after_digest") or ""):
        mismatches["after_digest"] = {
            "expected": str(bundle.get("after_digest") or ""),
            "actual": actual_after,
        }
    if "validation_report_digest" in bundle:
        actual_report = _file_digest(proposal_root / "validation_report.json")
        if actual_report != str(bundle.get("validation_report_digest") or ""):
            mismatches["validation_report_digest"] = {
                "expected": str(bundle.get("validation_report_digest") or ""),
                "actual": actual_report,
            }

    # Runtime fingerprints are source-backed by default.  Callers may provide
    # explicit schema snapshots for deterministic test or provider contracts.
    supplied = dict(context or {})
    current_runtime_digests: dict[str, str] = {}
    for key, paths in _RUNTIME_FINGERPRINTS.items():
        if f"{key}_digest" in supplied:
            current = str(supplied[f"{key}_digest"])
        else:
            frozen = bundle.get("context") if isinstance(bundle.get("context"), dict) else {}
            if f"{key}_digest" in frozen:
                current = str(frozen[f"{key}_digest"])
            elif key in supplied:
                current = _value_digest(supplied[key])
            elif key in frozen:
                current = _value_digest(frozen[key])
            else:
                current = _value_digest(_runtime_digest(paths))
        field = f"{key}_digest"
        current_runtime_digests[field] = current
        if current != str(bundle.get(field) or ""):
            mismatches[field] = {
                "expected": str(bundle.get(field) or ""),
                "actual": current,
            }
    current_fields = {
        "version": bundle.get("version"),
        "base_revision": actual_base,
        "after_digest": actual_after,
        "deleted_paths": actual_deleted,
        **current_runtime_digests,
        "context": bundle.get("context"),
    }
    if "validation_report_digest" in bundle:
        current_fields["validation_report_digest"] = _file_digest(
            proposal_root / "validation_report.json"
        )
    return {
        "ok": not mismatches,
        "reason": None if not mismatches else "candidate_bundle_changed",
        "mismatches": mismatches,
        "bundle": dict(bundle),
        "expected_digest": expected_digest,
        "current_digest": _bundle_digest(current_fields),
    }


def workspace_revision(root: Path) -> str:
    """Hash stable workspace files, excluding Nerya's volatile state."""

    return "snapshot:" + _tree_digest(Path(root), exclude_roots=_VOLATILE_ROOTS)


def digest_after(
    after_root: Path,
    *,
    deleted_paths: Iterable[str] = (),
) -> str:
    """Digest staged bytes, file modes, and declared deletions."""

    root = Path(after_root)
    entries: list[tuple[str, bytes, int]] = []
    if root.exists() and root.is_dir() and not root.is_symlink():
        for path in root.rglob("*"):
            if not path.is_file() or path.is_symlink():
                continue
            try:
                relative = path.relative_to(root).as_posix()
                info = path.stat()
                entries.append((relative, path.read_bytes(), stat.S_IMODE(info.st_mode)))
            except OSError:
                continue
    return digest_staged_entries(entries, deleted_paths=deleted_paths)


def digest_staged_entries(
    entries: Iterable[tuple[str, bytes, int]],
    *,
    deleted_paths: Iterable[str] = (),
) -> str:
    """Canonical digest shared by bundle verification and promotion."""

    digest = hashlib.sha256()
    for relative, data, mode in sorted(entries, key=lambda item: str(item[0])):
        digest.update(b"file\0")
        digest.update(str(relative).encode("utf-8"))
        digest.update(b"\0")
        digest.update(str(int(mode)).encode("ascii"))
        digest.update(b"\0")
        digest.update(hashlib.sha256(bytes(data)).digest())
        digest.update(b"\0")
    for relative in sorted({str(value) for value in deleted_paths}):
        digest.update(b"delete\0")
        digest.update(relative.encode("utf-8"))
        digest.update(b"\0")
    return digest.hexdigest()


def proposal_deleted_paths(proposal_root: Path) -> list[str]:
    """Return canonical deletion declarations from a proposal metadata file."""

    path = Path(proposal_root) / "proposal.yml"
    if not path.exists():
        return []
    try:
        meta = yaml_io.load(path, default={}) or {}
    except Exception as exc:
        raise ValueError(f"cannot read proposal metadata: {exc}") from exc
    if not isinstance(meta, dict):
        raise ValueError("proposal metadata must be a mapping")
    sources = [meta]
    metadata = meta.get("metadata")
    if metadata is not None:
        if not isinstance(metadata, dict):
            raise ValueError("proposal metadata.metadata must be a mapping")
        sources.append(metadata)
    values: list[Any] = []
    for source in sources:
        for key in _DELETE_KEYS:
            if key in source:
                value = source[key]
                if isinstance(value, str):
                    values.append(value)
                elif isinstance(value, (list, tuple, set)):
                    values.extend(value)
                else:
                    raise ValueError(f"deletion declaration must be a string/list: {key}")
    out: list[str] = []
    seen: set[str] = set()
    for value in values:
        if not isinstance(value, (str, Path)):
            raise ValueError("deletion path must be a string")
        text = str(value).strip().replace("\\", "/")
        relative = PurePosixPath(text)
        if (
            not text
            or relative.is_absolute()
            or any(part == ".." for part in relative.parts)
            or relative.as_posix() in {"", "."}
        ):
            raise ValueError(f"invalid deletion path: {value!r}")
        canonical = relative.as_posix()
        if canonical not in seen:
            seen.add(canonical)
            out.append(canonical)
    return sorted(out)


def _runtime_digest(relative_paths: tuple[str, ...]) -> str:
    package_root = Path(__file__).resolve().parent.parent
    h = hashlib.sha256()
    for relative in relative_paths:
        path = package_root / relative
        h.update(relative.encode("utf-8"))
        h.update(b"\0")
        try:
            h.update(hashlib.sha256(path.read_bytes()).digest())
        except OSError:
            h.update(b"missing")
        h.update(b"\0")
    return h.hexdigest()


def _tree_digest(root: Path, *, exclude_roots: frozenset[str]) -> str:
    h = hashlib.sha256()
    if root.is_symlink() or not root.exists() or not root.is_dir():
        return h.hexdigest()
    resolved = root.resolve()
    files: list[tuple[str, Path]] = []
    links: list[tuple[str, str]] = []
    for path in root.rglob("*"):
        if path.is_symlink():
            try:
                relative = path.relative_to(root)
                if any(part in exclude_roots for part in relative.parts):
                    continue
                links.append((relative.as_posix(), os.readlink(path)))
            except (OSError, ValueError):
                continue
            continue
        elif not path.is_file():
            continue
        try:
            relative = path.relative_to(root)
        except ValueError:
            continue
        if any(part in exclude_roots for part in relative.parts):
            continue
        try:
            path.resolve().relative_to(resolved)
        except (OSError, ValueError):
            continue
        files.append((relative.as_posix(), path))
    for relative, target in sorted(links):
        h.update(b"symlink\0")
        h.update(relative.encode("utf-8"))
        h.update(b"\0")
        h.update(target.encode("utf-8"))
        h.update(b"\0")
    for relative, path in sorted(files):
        h.update(b"file\0")
        h.update(relative.encode("utf-8"))
        h.update(b"\0")
        try:
            h.update(str(stat.S_IMODE(path.stat().st_mode)).encode("ascii"))
            h.update(b"\0")
            h.update(hashlib.sha256(path.read_bytes()).digest())
        except OSError:
            continue
        h.update(b"\0")
    return h.hexdigest()


def _symlink_paths(root: Path) -> list[str]:
    """List staged links without resolving or reading their targets."""

    if root.is_symlink():
        return ["."]
    if not root.exists() or not root.is_dir():
        return []
    return sorted(
        path.relative_to(root).as_posix()
        for path in root.rglob("*")
        if path.is_symlink()
    )


def _value_digest(value: Any) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _bundle_digest(bundle: dict[str, Any]) -> str:
    keys = (
        "version", "base_revision", "after_digest", "deleted_paths",
        "model_schema_digest",
        "tool_schema_digest", "eval_suite_digest", "context",
    )
    payload = {key: bundle.get(key) for key in keys}
    if "validation_report_digest" in bundle:
        payload["validation_report_digest"] = bundle.get(
            "validation_report_digest"
        )
    return _value_digest(payload)


def _file_digest(path: Path) -> str:
    if path.is_symlink() or not path.is_file():
        return ""
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
        return ""


def _canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)


__all__ = [
    "BUNDLE_VERSION",
    "build_candidate_bundle",
    "digest_after",
    "digest_staged_entries",
    "proposal_deleted_paths",
    "verify_candidate_bundle",
    "workspace_revision",
]
