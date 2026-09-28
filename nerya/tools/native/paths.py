"""Workspace-rooted path resolution for native tools.

The native tools must never let the model escape the workspace root,
even when the ``run_shell`` cwd argument is set. This module is
deliberately small and self-contained so it can be imported by:

* native file ops
* native shell
* native skill index/view
* permission engine (for path-scope policy)

Behaviour mirrors :func:`nerya.skills.builtin.operator_skill.scripts.handlers._safe_path`
but exposes a stable name (``resolve_workspace_path``) we can keep when
the operator skill is eventually decomposed.
"""

from __future__ import annotations

import os
from pathlib import Path, PureWindowsPath
from typing import Optional


class WorkspaceEscapeError(ValueError):
    """Raised when a resolved path falls outside the workspace root."""

    def __init__(self, raw: str, resolved: Path, root: Path, *, reason: str = "") -> None:
        self.raw = raw
        self.resolved = resolved
        self.root = root
        super().__init__(f"invalid workspace path {raw!r}: {reason}" if reason else (
            f"permission denied: path {raw!r} resolves to {resolved} which is "
            f"outside the workspace sandbox root {root}; access refused. Only "
            "workspace-relative paths are allowed."
        ))


def resolve_workspace_path(
    raw: Optional[str],
    *,
    root: Path,
    must_exist: bool = False,
    default: str = ".",
) -> Path:
    """Resolve ``raw`` under ``root``, refusing escape.

    Accepts:

    * relative paths (``strategies/foo``) — resolved under ``root``;
    * absolute paths inside ``root`` (``C:\\...\\.nerya\\foo``);
    * ``~`` prefix — expanded then validated;
    * empty / ``None`` — falls back to ``default``.

    Strips one extra layer of indirection that LLMs frequently emit:
    if the expanded path coincides with (or sits below) ``root``,
    we re-anchor to root rather than appending. This prevents the
    "double-rooted" path bug.
    """

    root = Path(root).resolve()
    candidate = (raw or default).strip()
    if not candidate:
        candidate = default

    windows_path = PureWindowsPath(candidate)
    # C:foo is drive-relative, NOT C:\foo. Never let the process's remembered
    # per-drive cwd decide where a tool writes. On POSIX reject foreign absolute
    # paths rather than silently creating a literal 'C:\...' workspace filename.
    if ((windows_path.drive and not windows_path.root)
            or (os.name != "nt" and windows_path.drive)
            or "\x00" in candidate):
        raise WorkspaceEscapeError(candidate, Path(candidate), root,
                                   reason="use a native absolute path or a workspace-relative path without NUL bytes")
    if os.name == "nt":
        components = windows_path.parts[1:] if windows_path.anchor else windows_path.parts
        if any(part not in {".", ".."} and (
            PureWindowsPath(part).is_reserved() or part.endswith((" ", "."))
            or any(ch in part for ch in ':<>"|?*')
        ) for part in components):
            raise WorkspaceEscapeError(candidate, Path(candidate), root,
                                       reason="Windows device names, alternate data streams and ambiguous filename suffixes are not allowed")

    p = Path(candidate)
    try:
        if str(p).startswith("~"):
            p = p.expanduser()
    except (RuntimeError, OSError):
        pass

    if not p.is_absolute():
        p = root / p
    try:
        p = p.resolve(strict=False)
        p.relative_to(root)
    except (OSError, RuntimeError, ValueError) as exc:
        # Fail closed for unresolved symlink/junction loops and different drives.
        raise WorkspaceEscapeError(str(raw or ""), p, root) from exc

    if must_exist and not p.exists():
        raise FileNotFoundError(f"path does not exist: {p}")
    return p


def to_workspace_relative(path: Path, root: Path) -> str:
    """Render ``path`` as a workspace-relative POSIX string for the LLM."""

    try:
        rel = path.resolve().relative_to(root.resolve())
    except (OSError, ValueError):
        return str(path)
    return rel.as_posix() or "."


__all__ = [
    "WorkspaceEscapeError",
    "resolve_workspace_path",
    "to_workspace_relative",
]
