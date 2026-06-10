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
from pathlib import Path
from typing import Optional


class WorkspaceEscapeError(ValueError):
    """Raised when a resolved path falls outside the workspace root."""

    def __init__(self, raw: str, resolved: Path, root: Path) -> None:
        self.raw = raw
        self.resolved = resolved
        self.root = root
        super().__init__(
            f"permission denied: path {raw!r} resolves to {resolved} which is "
            f"outside the workspace sandbox root {root}; access refused. Only "
            "workspace-relative paths are allowed."
        )


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
    except OSError:
        p = p.absolute()

    try:
        is_inside = p == root or root in p.parents
    except OSError:
        is_inside = False
    if not is_inside:
        try:
            rel = os.path.relpath(p, root)
            is_inside = not rel.startswith("..")
        except ValueError:
            is_inside = False
    if not is_inside:
        raise WorkspaceEscapeError(str(raw or ""), p, root)

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
