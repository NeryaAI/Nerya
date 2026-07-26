"""Native ``glob`` + ``grep`` tools.

Both prefer ``rg`` (ripgrep) when available because it is dramatically
faster on large workspaces; we fall back to a pure-Python implementation
so the agent never ``not_found``s for a missing binary.

The tool descriptors mark these as ``read_only=True`` and
``is_concurrency_safe=True`` so :class:`ToolOrchestrator` can fan them
out in parallel with file reads.
"""

from __future__ import annotations

import fnmatch
import os
import re
import shutil
import subprocess
import time
from pathlib import Path
from typing import Any, Optional

from ...core.sandbox import sandbox_exec
from ..tool_errors import schema_validation_result
from ..types import (
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


_DEFAULT_RESULTS = 80
_HARD_RESULTS = 500
_DEFAULT_GLOB_RESULTS = 200
_HARD_GLOB_RESULTS = 1000
_GLOB_MAGIC = set("*?[")


# ---------------------------------------------------------------------------
# glob
# ---------------------------------------------------------------------------


def _has_glob_magic(part: str) -> bool:
    return any(ch in part for ch in _GLOB_MAGIC)


def _absolute_glob_parts(pattern: str) -> tuple[Path, str] | None:
    pattern_path = Path(pattern)
    if not pattern_path.is_absolute():
        return None

    parts = pattern_path.parts
    first_magic = next(
        (idx for idx, part in enumerate(parts) if _has_glob_magic(part)),
        None,
    )
    if first_magic is None:
        return pattern_path.parent, pattern_path.name
    if first_magic == 0:
        return None
    base = Path(*parts[:first_magic])
    rel_pattern = os.path.join(*parts[first_magic:])
    return base, rel_pattern


def _glob_base_and_pattern(
    *,
    root: Path,
    base: str,
    pattern: str,
) -> tuple[Path, str] | ToolResult:
    absolute = _absolute_glob_parts(pattern)
    if absolute is None:
        try:
            base_p = resolve_workspace_path(base, root=root, default=".")
        except WorkspaceEscapeError as exc:
            return ToolResult.from_error(
                tool_use_id="",
                name="glob",
                error=ToolError(kind=ToolErrorKind.PERMISSION_DENIED, message=str(exc)),
            )
        return base_p, pattern

    abs_base, rel_pattern = absolute
    try:
        base_p = resolve_workspace_path(str(abs_base), root=root, default=".")
    except WorkspaceEscapeError as exc:
        return ToolResult.from_error(
            tool_use_id="",
            name="glob",
            error=ToolError(kind=ToolErrorKind.PERMISSION_DENIED, message=str(exc)),
        )
    return base_p, rel_pattern.replace("\\", "/")


def glob_handler(call: ToolCall, *, root: Path) -> ToolResult:
    args = call.arguments or {}
    pattern = args.get("pattern")
    base = args.get("path") or "."
    limit = int(args.get("limit") or _DEFAULT_GLOB_RESULTS)
    limit = max(1, min(limit, _HARD_GLOB_RESULTS))

    if not pattern:
        return schema_validation_result(call, "glob requires a 'pattern' argument")

    resolved = _glob_base_and_pattern(root=root, base=str(base), pattern=str(pattern))
    if isinstance(resolved, ToolResult):
        resolved.tool_use_id = call.id
        resolved.name = call.name
        return resolved
    base_p, pattern = resolved

    if not base_p.exists():
        return ToolResult.from_error(
            tool_use_id=call.id,
            name=call.name,
            error=ToolError(
                kind=ToolErrorKind.NOT_FOUND,
                message=f"path does not exist: {to_workspace_relative(base_p, root)}",
            ),
        )

    matches: list[dict[str, Any]] = []
    truncated = False
    try:
        for path in base_p.rglob(str(pattern)):
            if not path.is_file():
                continue
            if len(matches) >= limit:
                truncated = True
                break
            try:
                stat = path.stat()
            except OSError:
                continue
            matches.append(
                {
                    "path": to_workspace_relative(path, root),
                    "bytes": stat.st_size,
                    "mtime": stat.st_mtime,
                }
            )
    except (NotImplementedError, ValueError, OSError) as exc:
        return ToolResult.from_error(
            tool_use_id=call.id,
            name=call.name,
            error=ToolError(kind=ToolErrorKind.EXECUTION_ERROR, message=str(exc)),
        )

    matches.sort(key=lambda m: -m["mtime"])

    head = "\n".join(f"- {m['path']}" for m in matches[:60])
    if truncated:
        head += f"\n... truncated at {limit}"
    return ToolResult(
        tool_use_id=call.id,
        name=call.name,
        content=[
            ToolResultPart.text_part(
                f"# glob {pattern!r} → {len(matches)} matches\n\n{head}"
            ),
            ToolResultPart.json_part(
                {
                    "pattern": pattern,
                    "base": to_workspace_relative(base_p, root),
                    "matches": matches,
                    "truncated": truncated,
                }
            ),
        ],
        metadata={"count": len(matches), "truncated": truncated},
    )


# ---------------------------------------------------------------------------
# grep
# ---------------------------------------------------------------------------


def _have_rg() -> bool:
    return shutil.which("rg") is not None


def _grep_with_rg(
    *,
    root: Path,
    base: Path,
    pattern: str,
    glob: Optional[str],
    case_insensitive: bool,
    limit: int,
    timeout_s: float,
    file_type: Optional[str],
) -> tuple[list[dict[str, Any]], bool]:
    cmd = ["rg", "--json", "--no-heading", "--color=never", "-n"]
    if case_insensitive:
        cmd.append("-i")
    if glob:
        cmd.extend(["--glob", glob])
    if file_type:
        cmd.extend(["--type", file_type])
    cmd.append("--max-count=2000")
    cmd.append(pattern)
    cmd.append(str(base))
    try:
        proc = sandbox_exec(
            cmd,
            cwd=root,
            root=root,
            capture_output=True,
            text=True,
            timeout=timeout_s,
            check=False,
        )
    except subprocess.TimeoutExpired:
        raise TimeoutError("rg timed out")

    out: list[dict[str, Any]] = []
    truncated = False
    for line in proc.stdout.splitlines():
        if len(out) >= limit:
            truncated = True
            break
        try:
            import json as _json

            row = _json.loads(line)
        except Exception:
            continue
        if row.get("type") != "match":
            continue
        data = row.get("data") or {}
        path = (data.get("path") or {}).get("text") or ""
        line_no = data.get("line_number")
        text = (data.get("lines") or {}).get("text") or ""
        out.append(
            {
                "path": to_workspace_relative(Path(path), root) if path else "",
                "line": line_no,
                "match": text.rstrip("\n"),
            }
        )
    return out, truncated


def _grep_python(
    *,
    root: Path,
    base: Path,
    pattern: str,
    glob: Optional[str],
    case_insensitive: bool,
    limit: int,
) -> tuple[list[dict[str, Any]], bool]:
    flags = re.IGNORECASE if case_insensitive else 0
    rx = re.compile(pattern, flags)
    out: list[dict[str, Any]] = []
    truncated = False
    iterator: Any
    if base.is_file():
        iterator = [base]
    else:
        iterator = base.rglob("*")
    for path in iterator:
        if len(out) >= limit:
            truncated = True
            break
        if not path.is_file():
            continue
        rel = to_workspace_relative(path, root)
        if glob and not fnmatch.fnmatch(path.name, glob):
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for i, line in enumerate(text.splitlines(), start=1):
            if rx.search(line):
                out.append({"path": rel, "line": i, "match": line[:400]})
                if len(out) >= limit:
                    truncated = True
                    break
    return out, truncated


def grep_handler(call: ToolCall, *, root: Path) -> ToolResult:
    args = call.arguments or {}
    pattern = args.get("pattern")
    base_arg = args.get("path") or "."
    glob = args.get("glob")
    case_insensitive = bool(args.get("case_insensitive", False))
    file_type = args.get("type")
    limit = int(args.get("max_results") or _DEFAULT_RESULTS)
    limit = max(1, min(limit, _HARD_RESULTS))
    timeout_s = 15.0

    if not pattern:
        return schema_validation_result(call, "grep requires a 'pattern' argument")

    try:
        base = resolve_workspace_path(str(base_arg), root=root, default=".")
    except WorkspaceEscapeError as exc:
        return ToolResult.from_error(
            tool_use_id=call.id,
            name=call.name,
            error=ToolError(kind=ToolErrorKind.PERMISSION_DENIED, message=str(exc)),
        )

    started = time.monotonic()
    try:
        if _have_rg():
            try:
                matches, truncated = _grep_with_rg(
                    root=root,
                    base=base,
                    pattern=pattern,
                    glob=str(glob) if glob else None,
                    case_insensitive=case_insensitive,
                    limit=limit,
                    timeout_s=timeout_s,
                    file_type=str(file_type) if file_type else None,
                )
            except TimeoutError:
                return ToolResult.from_error(
                    tool_use_id=call.id,
                    name=call.name,
                    error=ToolError(
                        kind=ToolErrorKind.TIMEOUT,
                        message=f"rg search exceeded {timeout_s}s",
                    ),
                )
        else:
            matches, truncated = _grep_python(
                root=root,
                base=base,
                pattern=pattern,
                glob=str(glob) if glob else None,
                case_insensitive=case_insensitive,
                limit=limit,
            )
    except re.error as exc:
        return schema_validation_result(call, f"invalid regex: {exc}")
    elapsed_ms = int((time.monotonic() - started) * 1000)

    head_lines = []
    last_path = ""
    for m in matches[:60]:
        if m["path"] != last_path:
            head_lines.append(f"## {m['path']}")
            last_path = m["path"]
        head_lines.append(f"  {m['line']}: {m['match']}")
    if truncated:
        head_lines.append(f"... truncated at {limit}")

    return ToolResult(
        tool_use_id=call.id,
        name=call.name,
        elapsed_ms=elapsed_ms,
        content=[
            ToolResultPart.text_part(
                f"# grep {pattern!r} ({len(matches)} matches in {elapsed_ms}ms)\n\n"
                + "\n".join(head_lines)
            ),
            ToolResultPart.json_part(
                {
                    "pattern": pattern,
                    "base": to_workspace_relative(base, root),
                    "matches": matches,
                    "truncated": truncated,
                    "engine": "rg" if _have_rg() else "python",
                }
            ),
        ],
        metadata={"count": len(matches), "engine": "rg" if _have_rg() else "python"},
    )


__all__ = ["glob_handler", "grep_handler"]
