"""Strategy session directory helpers.

this is the canonical place to open, close, and write artifacts
to a **session**. A session is a persisted record of a single *unit of
work*: a main-agent turn, a subagent analysis, a strategy review, or an
evolution run.

Session IDs are allocated here and are stable across surfaces:

* Strategy-scoped sessions land under
  ``strategies/<strategy_id>/sessions/<session_id>/``.
* Ambient ("global") sessions — main turns without an owning strategy,
  reviews, evolution runs — land under ``state/sessions/<session_id>/``.

The directory layout within a session is uniform regardless of origin so
operators can grep / replay / explain sessions the same way.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from ..core.atomic_write import atomic_write_text
from ..core.ids import session_id as new_session_id
from ..core.paths import WorkspacePaths
from ..core.time import now_iso


# ----------------------------------------------------------------- paths

def session_dir(paths: WorkspacePaths, strategy_id: str | None,
                sid: str) -> Path:
    """Return the on-disk directory for a session.

    ``strategy_id=None`` selects the ambient session tree under
    ``state/sessions`` — used by main turns that don't belong to a
    specific strategy, by review runs, and by evolution passes.
    """
    if strategy_id:
        return paths.strategy_sessions(strategy_id) / sid
    return paths.state / "sessions" / sid


# ----------------------------------------------------------------- open/close

def open_session(paths: WorkspacePaths, strategy_id: str | None, *,
                 trigger: dict[str, Any],
                 kind: str = "strategy",
                 parent_session_id: str | None = None,
                 owner: str | None = None) -> str:
    """Open a new session and return its id.

    ``kind`` describes the session type — ``"strategy"``, ``"main_turn"``,
    ``"subagent"``, ``"review"``, or ``"evolution"``. The value is
    recorded in ``meta.json`` so downstream tooling can filter sessions
    by origin without having to re-derive it from paths.

    ``parent_session_id`` records hierarchical relationships — e.g. a
    subagent session inside a main turn, or a review session spawned
    by a turn. The link is persisted so the explain / replay surfaces
    can walk the tree.
    """
    sid = new_session_id()
    target = session_dir(paths, strategy_id, sid)
    target.mkdir(parents=True, exist_ok=True)
    atomic_write_text(target / "trigger.json",
                      json.dumps(trigger, indent=2, default=str))
    meta = {
        "session_id": sid,
        "kind": kind,
        "strategy_id": strategy_id,
        "parent_session_id": parent_session_id,
        "owner": owner,
        "opened_at": now_iso(),
    }
    atomic_write_text(target / "meta.json",
                      json.dumps(meta, indent=2, default=str))
    atomic_write_text(
        target / "context_summary.md",
        f"# Session {sid}\n\nKind: {kind}\nOpened at {now_iso()}.\n",
    )
    return sid


def close_session(paths: WorkspacePaths, strategy_id: str | None, sid: str,
                  *, outcome: dict[str, Any]) -> Path:
    target = session_dir(paths, strategy_id, sid)
    target.mkdir(parents=True, exist_ok=True)
    atomic_write_text(target / "outcome.json",
                      json.dumps(outcome, indent=2, default=str))
    # Stamp the close time on meta.json if present so readers can tell
    # at a glance whether a session is still in flight.
    meta_path = target / "meta.json"
    if meta_path.exists():
        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
        except Exception:
            meta = {}
        meta["closed_at"] = now_iso()
        atomic_write_text(meta_path, json.dumps(meta, indent=2, default=str))
    return target


def latest_session_id(paths: WorkspacePaths,
                      strategy_id: str | None) -> str | None:
    root = (paths.strategy_sessions(strategy_id)
            if strategy_id else paths.state / "sessions")
    if not root.exists():
        return None
    candidates = [p for p in root.iterdir() if p.is_dir()]
    if not candidates:
        return None
    candidates.sort(key=lambda p: p.stat().st_mtime)
    return candidates[-1].name


def write_artifact(paths: WorkspacePaths, strategy_id: str | None, sid: str,
                   name: str, content: str | dict | list) -> Path:
    target = session_dir(paths, strategy_id, sid) / name
    target.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(content, (dict, list)):
        atomic_write_text(target, json.dumps(content, indent=2, default=str))
    else:
        atomic_write_text(target, str(content))
    return target


# ----------------------------------------------------------------- explain

def read_meta(paths: WorkspacePaths, strategy_id: str | None,
              sid: str) -> dict[str, Any] | None:
    """Return the ``meta.json`` for a session, or ``None`` if missing."""
    p = session_dir(paths, strategy_id, sid) / "meta.json"
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return None
