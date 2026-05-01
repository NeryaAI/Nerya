"""Native file ops — read_file / list_dir / edit_file / write_file / apply_patch.

Lifted out of :mod:`nerya.skills.builtin.operator_skill.scripts.handlers`
into a *first-class* tool layer so the workspace-native agent loop can
drive them directly, without the SKILL.md-action -> ToolRunner detour.

Invariants kept from the operator-skill version:

* path safety via :func:`resolve_workspace_path`;
* :class:`FileStateCache` integration (fresh-read before edit);
* unified diff in the result so the dashboard can render edits;
* large files are truncated with explicit ``truncated=True`` markers.

Drop / change vs operator skill:

* No more dispatch through :class:`ToolRunner`. Each handler returns a
  :class:`ToolResult` directly, including provider-shaped error
  results for stale-read / not-found / multi-match conditions.
* Diffs come back as a :class:`ToolResultPart` of type ``"diff"``
  rather than a free-form text blob, so the dashboard can render them
  side-by-side without reparsing.
* ``edit_file`` requires *fresh read*. The legacy operator handler
  enforced the same; we surface it on the descriptor as
  ``requires_fresh_read=True`` and have the executor enforce it
  before calling the handler.
"""

from __future__ import annotations

import difflib
import fnmatch
import os
import time
from pathlib import Path
from typing import Any, Optional

from ...agent.file_state import (
    FileStateCache,
    StaleFileReadError,
    compute_file_hash,
)
from ..types import (
    ContextModifier,
    RiskLevel,
    ToolCall,
    ToolError,
    ToolErrorKind,
    ToolResult,
    ToolResultPart,
)
from .paths import (
    WorkspaceEscapeError,
    resolve_workspace_path,
    to_workspace_relative,
)


_DEFAULT_FILE_BYTES = 64 * 1024
_HARD_FILE_BYTES = 1 * 1024 * 1024
_DEFAULT_LIST_ENTRIES = 200
_HARD_LIST_ENTRIES = 1000


_SENSITIVE_MUTATION_GLOBS = (
    ".env",
    ".env.*",
    "nerya.yml",
    "nerya.yaml",
    "accounts/*",
    "exchanges.yml",
    "exchanges.yaml",
    "triggers/schedules.yml",
    "vault/*",
    "vault/**/*",
    "secrets/*",
    "secrets/**/*",
    "strategies/*/strategy.yml",
    "strategies/*/strategy.yaml",
    "strategies/*/limits.yml",
    "strategies/*/limits.yaml",
    "providers/*",
    "providers/**/*",
)


def _normalise_mutation_path(value: Any) -> str:
    raw = str(value or "").strip().replace("\\", "/")
    while raw.startswith("./"):
        raw = raw[2:]
    return raw.lower()


def is_sensitive_mutation_path(path: Any) -> bool:
    """Return true for files whose mutation changes runtime safety posture."""

    p = _normalise_mutation_path(path)
    if not p:
        return False
    return any(fnmatch.fnmatchcase(p, pattern) for pattern in _SENSITIVE_MUTATION_GLOBS)


def classify_file_mutation_risk(arguments: dict[str, Any]) -> RiskLevel:
    """Escalate safety-critical config writes while keeping code edits fluid."""

    if is_sensitive_mutation_path((arguments or {}).get("path")):
        return RiskLevel.DANGEROUS
    return RiskLevel.WRITE


def _read_text(path: Path) -> tuple[str, bool, int]:
    """Read text content, falling back to errors='replace' on bad UTF-8."""

    raw = path.read_bytes()
    try:
        text = raw.decode("utf-8")
        was_replaced = False
    except UnicodeDecodeError:
        text = raw.decode("utf-8", errors="replace")
        was_replaced = True
    return text, was_replaced, len(raw)


def _short_path(path: Path, root: Path) -> str:
    return to_workspace_relative(path, root)


# ---------------------------------------------------------------------------
# read_file
# ---------------------------------------------------------------------------


def read_file_handler(
    call: ToolCall,
    *,
    root: Path,
    file_state: Optional[FileStateCache],
) -> ToolResult:
    """Read a workspace file. Updates the FileStateCache for fresh-read."""

    args = call.arguments or {}
    raw_path = args.get("path")
    offset = int(args.get("offset", 0) or 0)
    limit = args.get("limit")
    max_bytes = int(args.get("max_bytes") or _DEFAULT_FILE_BYTES)
    max_bytes = max(1024, min(max_bytes, _HARD_FILE_BYTES))

    if not raw_path:
        return ToolResult.from_error(
            tool_use_id=call.id,
            name=call.name,
            error=ToolError(
                kind=ToolErrorKind.SCHEMA_VALIDATION,
                message="read_file requires a 'path' argument",
            ),
        )

    try:
        p = resolve_workspace_path(str(raw_path), root=root, must_exist=True)
    except WorkspaceEscapeError as exc:
        return ToolResult.from_error(
            tool_use_id=call.id,
            name=call.name,
            error=ToolError(
                kind=ToolErrorKind.PERMISSION_DENIED,
                message=str(exc),
                detail={"path": str(raw_path)},
            ),
        )
    except FileNotFoundError as exc:
        return ToolResult.from_error(
            tool_use_id=call.id,
            name=call.name,
            error=ToolError(
                kind=ToolErrorKind.NOT_FOUND,
                message=str(exc),
                detail={"path": str(raw_path)},
                recovery_hint={"action": "list_dir", "path": str(Path(raw_path).parent or '.')},
            ),
        )

    if p.is_dir():
        return ToolResult.from_error(
            tool_use_id=call.id,
            name=call.name,
            error=ToolError(
                kind=ToolErrorKind.SCHEMA_VALIDATION,
                message=f"{_short_path(p, root)} is a directory; use list_dir",
                recovery_hint={"action": "list_dir", "path": _short_path(p, root)},
            ),
        )

    try:
        text, replaced, raw_size = _read_text(p)
    except OSError as exc:
        return ToolResult.from_error(
            tool_use_id=call.id,
            name=call.name,
            error=ToolError(
                kind=ToolErrorKind.EXECUTION_ERROR,
                message=f"failed to read {_short_path(p, root)}: {exc}",
            ),
        )

    lines = text.splitlines()
    total_lines = len(lines)
    if offset < 0:
        offset = max(0, total_lines + offset)
    if limit is None:
        end = total_lines
    else:
        try:
            limit_int = int(limit)
        except Exception:
            limit_int = total_lines
        end = min(total_lines, offset + max(0, limit_int))
    sliced = lines[offset:end]
    truncated_lines = end < total_lines or offset > 0

    rendered = "\n".join(sliced)
    truncated_bytes = False
    if len(rendered.encode("utf-8")) > max_bytes:
        rendered = rendered.encode("utf-8")[:max_bytes].decode("utf-8", errors="ignore")
        truncated_bytes = True

    if file_state is not None:
        file_state.record_read(
            p,
            content=text,
            bytes_seen=raw_size,
            line_count=total_lines,
            truncated=truncated_lines or truncated_bytes,
        )

    payload: dict[str, Any] = {
        "path": _short_path(p, root),
        "absolute_path": str(p),
        "content": rendered,
        "offset": offset,
        "limit": (end - offset),
        "total_lines": total_lines,
        "bytes": raw_size,
        "truncated_lines": truncated_lines,
        "truncated_bytes": truncated_bytes,
        "encoding_replaced": replaced,
        "content_hash": compute_file_hash(text),
    }

    res = ToolResult(
        tool_use_id=call.id,
        name=call.name,
        content=[
            ToolResultPart.text_part(
                f"# {payload['path']} ({total_lines} lines, {raw_size} bytes)\n\n"
                + rendered
            ),
            ToolResultPart.json_part(
                {k: v for k, v in payload.items() if k != "content"}
            ),
        ],
        metadata={"path": payload["path"], "bytes": raw_size},
        context_modifiers=[
            ContextModifier(kind="file_read", path=str(p), payload={"hash": payload["content_hash"]})
        ],
    )
    return res


# ---------------------------------------------------------------------------
# list_dir
# ---------------------------------------------------------------------------


def list_dir_handler(call: ToolCall, *, root: Path) -> ToolResult:
    args = call.arguments or {}
    raw_path = args.get("path") or "."
    show_hidden = bool(args.get("show_hidden", False))
    limit = int(args.get("limit") or _DEFAULT_LIST_ENTRIES)
    limit = max(1, min(limit, _HARD_LIST_ENTRIES))
    pattern = args.get("pattern")

    try:
        p = resolve_workspace_path(str(raw_path), root=root, default=".")
    except WorkspaceEscapeError as exc:
        return ToolResult.from_error(
            tool_use_id=call.id,
            name=call.name,
            error=ToolError(kind=ToolErrorKind.PERMISSION_DENIED, message=str(exc)),
        )

    if not p.exists():
        return ToolResult.from_error(
            tool_use_id=call.id,
            name=call.name,
            error=ToolError(
                kind=ToolErrorKind.NOT_FOUND,
                message=f"directory not found: {_short_path(p, root)}",
            ),
        )
    if not p.is_dir():
        return ToolResult.from_error(
            tool_use_id=call.id,
            name=call.name,
            error=ToolError(
                kind=ToolErrorKind.SCHEMA_VALIDATION,
                message=f"{_short_path(p, root)} is not a directory; use read_file",
            ),
        )

    items: list[dict[str, Any]] = []
    truncated = False
    try:
        entries = sorted(p.iterdir(), key=lambda x: (x.is_file(), x.name.lower()))
    except OSError as exc:
        return ToolResult.from_error(
            tool_use_id=call.id,
            name=call.name,
            error=ToolError(kind=ToolErrorKind.EXECUTION_ERROR, message=str(exc)),
        )
    for entry in entries:
        if not show_hidden and entry.name.startswith("."):
            continue
        if pattern and not fnmatch.fnmatch(entry.name, str(pattern)):
            continue
        if len(items) >= limit:
            truncated = True
            break
        try:
            stat = entry.stat()
        except OSError:
            continue
        items.append(
            {
                "name": entry.name,
                "path": _short_path(entry, root),
                "type": "dir" if entry.is_dir() else "file",
                "bytes": stat.st_size if entry.is_file() else 0,
                "mtime": stat.st_mtime,
            }
        )

    summary_lines = [f"# {_short_path(p, root)} ({len(items)} entries)"]
    for it in items[:60]:
        suffix = "/" if it["type"] == "dir" else ""
        summary_lines.append(f"- {it['name']}{suffix}")
    if truncated:
        summary_lines.append(f"... truncated at {limit}")

    return ToolResult(
        tool_use_id=call.id,
        name=call.name,
        content=[
            ToolResultPart.text_part("\n".join(summary_lines)),
            ToolResultPart.json_part(
                {
                    "path": _short_path(p, root),
                    "items": items,
                    "truncated": truncated,
                    "count": len(items),
                }
            ),
        ],
        metadata={"path": _short_path(p, root), "count": len(items)},
    )


# ---------------------------------------------------------------------------
# edit_file (single-block string replace)
# ---------------------------------------------------------------------------


def _make_diff(before: str, after: str, path: str) -> str:
    return "".join(
        difflib.unified_diff(
            before.splitlines(keepends=True),
            after.splitlines(keepends=True),
            fromfile=f"a/{path}",
            tofile=f"b/{path}",
            n=3,
        )
    )


def edit_file_handler(
    call: ToolCall,
    *,
    root: Path,
    file_state: Optional[FileStateCache],
) -> ToolResult:
    args = call.arguments or {}
    raw_path = args.get("path")
    old_string = args.get("old_string")
    new_string = args.get("new_string")
    replace_all = bool(args.get("replace_all", False))

    if not raw_path or old_string is None or new_string is None:
        return ToolResult.from_error(
            tool_use_id=call.id,
            name=call.name,
            error=ToolError(
                kind=ToolErrorKind.SCHEMA_VALIDATION,
                message="edit_file requires path, old_string, new_string",
            ),
        )

    try:
        p = resolve_workspace_path(str(raw_path), root=root, must_exist=True)
    except WorkspaceEscapeError as exc:
        return ToolResult.from_error(
            tool_use_id=call.id,
            name=call.name,
            error=ToolError(kind=ToolErrorKind.PERMISSION_DENIED, message=str(exc)),
        )
    except FileNotFoundError as exc:
        return ToolResult.from_error(
            tool_use_id=call.id,
            name=call.name,
            error=ToolError(
                kind=ToolErrorKind.NOT_FOUND,
                message=str(exc),
                recovery_hint={"action": "write_file", "path": str(raw_path)},
            ),
        )

    if p.is_dir():
        return ToolResult.from_error(
            tool_use_id=call.id,
            name=call.name,
            error=ToolError(
                kind=ToolErrorKind.SCHEMA_VALIDATION,
                message="cannot edit a directory",
            ),
        )

    try:
        before, _, _ = _read_text(p)
    except OSError as exc:
        return ToolResult.from_error(
            tool_use_id=call.id,
            name=call.name,
            error=ToolError(kind=ToolErrorKind.EXECUTION_ERROR, message=str(exc)),
        )

    if file_state is not None:
        try:
            file_state.assert_fresh_for_edit(p, on_disk=before, require_read=True)
        except StaleFileReadError as exc:
            return ToolResult.from_error(
                tool_use_id=call.id,
                name=call.name,
                error=ToolError(
                    kind=ToolErrorKind.STALE_FILE,
                    message=str(exc),
                    detail=exc.as_dict(),
                    retryable=True,
                    recovery_hint={"action": "read_file", "path": _short_path(p, root)},
                ),
            )

    if old_string == new_string:
        return ToolResult.from_error(
            tool_use_id=call.id,
            name=call.name,
            error=ToolError(
                kind=ToolErrorKind.SCHEMA_VALIDATION,
                message="old_string equals new_string; no-op edit",
            ),
        )

    occurrences = before.count(old_string)
    if occurrences == 0:
        return ToolResult.from_error(
            tool_use_id=call.id,
            name=call.name,
            error=ToolError(
                kind=ToolErrorKind.NOT_FOUND,
                message="old_string not found in file",
                detail={"path": _short_path(p, root)},
                recovery_hint={
                    "advice": (
                        "Re-read the file and provide an old_string that matches "
                        "the current content exactly (whitespace included)."
                    )
                },
            ),
        )
    if occurrences > 1 and not replace_all:
        return ToolResult.from_error(
            tool_use_id=call.id,
            name=call.name,
            error=ToolError(
                kind=ToolErrorKind.DIFF_CONFLICT,
                message=(
                    f"old_string appears {occurrences} times; provide more context "
                    "or set replace_all=true"
                ),
                detail={"occurrences": occurrences},
            ),
        )

    if replace_all:
        after = before.replace(old_string, new_string)
    else:
        after = before.replace(old_string, new_string, 1)

    try:
        p.write_text(after, encoding="utf-8")
    except OSError as exc:
        return ToolResult.from_error(
            tool_use_id=call.id,
            name=call.name,
            error=ToolError(
                kind=ToolErrorKind.EXECUTION_ERROR,
                message=f"write failed: {exc}",
            ),
        )

    if file_state is not None:
        file_state.record_write(p, new_content=after, line_count=after.count("\n") + 1)

    diff = _make_diff(before, after, _short_path(p, root))
    return ToolResult(
        tool_use_id=call.id,
        name=call.name,
        content=[
            ToolResultPart.diff_part(diff=diff, path=_short_path(p, root)),
            ToolResultPart.json_part(
                {
                    "path": _short_path(p, root),
                    "occurrences_replaced": occurrences if replace_all else 1,
                    "bytes_after": len(after.encode("utf-8")),
                    "lines_after": after.count("\n") + 1,
                    "content_hash": compute_file_hash(after),
                }
            ),
        ],
        metadata={"path": _short_path(p, root), "kind": "edit"},
        context_modifiers=[
            ContextModifier(kind="file_mutate", path=str(p), payload={"diff_lines": diff.count("\n")})
        ],
    )


# ---------------------------------------------------------------------------
# write_file (full replacement)
# ---------------------------------------------------------------------------


def write_file_handler(
    call: ToolCall,
    *,
    root: Path,
    file_state: Optional[FileStateCache],
) -> ToolResult:
    args = call.arguments or {}
    raw_path = args.get("path")
    # Accept either ``content`` (legacy) or ``contents`` (current
    # bootstrap-declared schema). Different parts of the codebase used
    # different names; the handler is permissive so the model never has
    # to guess which spelling to use.
    if "content" in args:
        content = args.get("content")
    else:
        content = args.get("contents")
    require_existing_read = bool(args.get("require_existing_read", True))

    if not raw_path or content is None:
        return ToolResult.from_error(
            tool_use_id=call.id,
            name=call.name,
            error=ToolError(
                kind=ToolErrorKind.SCHEMA_VALIDATION,
                message="write_file requires path and contents (string body)",
            ),
        )

    if not isinstance(content, str):
        return ToolResult.from_error(
            tool_use_id=call.id,
            name=call.name,
            error=ToolError(
                kind=ToolErrorKind.SCHEMA_VALIDATION,
                message="content must be a string (write binary via separate handler)",
            ),
        )

    try:
        p = resolve_workspace_path(str(raw_path), root=root)
    except WorkspaceEscapeError as exc:
        return ToolResult.from_error(
            tool_use_id=call.id,
            name=call.name,
            error=ToolError(kind=ToolErrorKind.PERMISSION_DENIED, message=str(exc)),
        )

    existed = p.exists()
    before = ""
    if existed:
        try:
            before, _, _ = _read_text(p)
        except OSError as exc:
            return ToolResult.from_error(
                tool_use_id=call.id,
                name=call.name,
                error=ToolError(kind=ToolErrorKind.EXECUTION_ERROR, message=str(exc)),
            )
        if file_state is not None and require_existing_read:
            try:
                file_state.assert_fresh_for_edit(p, on_disk=before, require_read=True)
            except StaleFileReadError as exc:
                return ToolResult.from_error(
                    tool_use_id=call.id,
                    name=call.name,
                    error=ToolError(
                        kind=ToolErrorKind.STALE_FILE,
                        message=str(exc),
                        detail=exc.as_dict(),
                        retryable=True,
                        recovery_hint={
                            "action": "read_file",
                            "path": _short_path(p, root),
                        },
                    ),
                )
    else:
        try:
            p.parent.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            return ToolResult.from_error(
                tool_use_id=call.id,
                name=call.name,
                error=ToolError(
                    kind=ToolErrorKind.EXECUTION_ERROR,
                    message=f"failed to create parent dir: {exc}",
                ),
            )

    try:
        p.write_text(content, encoding="utf-8")
    except OSError as exc:
        return ToolResult.from_error(
            tool_use_id=call.id,
            name=call.name,
            error=ToolError(
                kind=ToolErrorKind.EXECUTION_ERROR,
                message=f"write failed: {exc}",
            ),
        )

    if file_state is not None:
        file_state.record_write(
            p, new_content=content, line_count=content.count("\n") + 1
        )

    diff = _make_diff(before, content, _short_path(p, root)) if existed else (
        f"--- /dev/null\n+++ b/{_short_path(p, root)}\n"
        + "".join(f"+{line}\n" for line in content.splitlines())
    )
    return ToolResult(
        tool_use_id=call.id,
        name=call.name,
        content=[
            ToolResultPart.diff_part(diff=diff, path=_short_path(p, root)),
            ToolResultPart.json_part(
                {
                    "path": _short_path(p, root),
                    "kind": "create" if not existed else "overwrite",
                    "bytes": len(content.encode("utf-8")),
                    "lines": content.count("\n") + 1,
                    "content_hash": compute_file_hash(content),
                }
            ),
        ],
        metadata={"path": _short_path(p, root), "kind": "write"},
        context_modifiers=[
            ContextModifier(kind="file_mutate", path=str(p), payload={"created": not existed})
        ],
    )


__all__ = [
    "edit_file_handler",
    "list_dir_handler",
    "read_file_handler",
    "write_file_handler",
]
