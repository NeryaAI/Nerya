"""Rich session persistence + invoked-skill state preservation.

A *session* collects a set of related turns (same user/strategy context)
so they can be listed, resumed, inspected, and replayed without
re-reading the raw turn-step journal. It is deliberately a thin layer
over the existing turn journals: the journals remain the source of
truth, the session file is a *compact, indexed summary*.

What a session persists:

* ``session_id``, ``strategy_id``, ``created_at``, ``updated_at``.
* ``turn_ids`` — ordered list of turns that ran under the session.
* ``invoked_skills`` — deduped set of every skill that has been called.
* ``skill_state`` — optional per-skill payload captured by hooks. This
  lets a skill (e.g. a running script, an open script loop) persist
  the handles it needs to resume across turns.
* ``last_action`` — the top-level action of the most recent turn, so
  the dashboard can show "last: noop / submit_trade / ...".
* ``meta`` — free-form, operator-visible dict.

On-disk layout::

    workspace/sessions/<session_id>.json

Writes are atomic (tmp+rename) so readers never see a partial file. A
missing session file is not an error — callers simply create one.
"""

from __future__ import annotations

import json
import os
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping

from ..core.time import now_iso


def new_session_id() -> str:
    return "sess_" + uuid.uuid4().hex[:16]


@dataclass
class SessionState:
    session_id: str
    strategy_id: str | None = None
    created_at: str = ""
    updated_at: str = ""
    turn_ids: list[str] = field(default_factory=list)
    invoked_skills: list[str] = field(default_factory=list)
    skill_state: dict[str, Any] = field(default_factory=dict)
    last_action: str | None = None
    meta: dict[str, Any] = field(default_factory=dict)

    def asdict(self) -> dict[str, Any]:
        return asdict(self)


def _nonnegative_int(value: Any) -> int:
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError):
        return 0


def _iso_from_db_ts(value: Any) -> str:
    try:
        ts = float(value)
    except (TypeError, ValueError):
        return ""
    if ts <= 0:
        return ""
    return datetime.fromtimestamp(ts, timezone.utc).isoformat().replace("+00:00", "Z")


def _parse_meta_json(raw: Any) -> dict[str, Any]:
    if isinstance(raw, Mapping):
        return dict(raw)
    try:
        meta = json.loads(str(raw or "{}"))
    except (TypeError, ValueError, json.JSONDecodeError):
        return {}
    return meta if isinstance(meta, dict) else {}


def file_session_asdict(row: SessionState | Mapping[str, Any]) -> dict[str, Any]:
    """Normalize a file-backed session for API and gateway consumers."""

    data = row.asdict() if isinstance(row, SessionState) else dict(row)
    turn_ids = data.get("turn_ids") if isinstance(data.get("turn_ids"), list) else []
    turn_count = max(_nonnegative_int(data.get("turn_count")), len(turn_ids))
    data["turn_count"] = turn_count
    data["message_count"] = max(
        _nonnegative_int(data.get("message_count")),
        turn_count * 2,
    )
    if not data.get("source"):
        data["source"] = "session_file"
    return data


def db_session_asdict(row: Mapping[str, Any]) -> dict[str, Any]:
    """Normalize an ``agent_sessions`` repository row."""

    title = str(row.get("title") or "").strip()
    meta = _parse_meta_json(row.get("meta_json"))
    if title and not meta.get("title"):
        meta["title"] = title
        meta.setdefault("title_source", "db")
    return {
        "session_id": str(row.get("session_id") or ""),
        "strategy_id": row.get("strategy_id"),
        "created_at": _iso_from_db_ts(row.get("created_at")),
        "updated_at": _iso_from_db_ts(row.get("updated_at")),
        "turn_ids": [],
        "invoked_skills": [],
        "skill_state": {},
        "last_action": None,
        "meta": meta,
        "source": row.get("source") or "",
        "message_count": _nonnegative_int(row.get("message_count")),
        "turn_count": _nonnegative_int(row.get("turn_count")),
    }


def hydrate_db_session_counts(
    paths: Any,
    rows: Iterable[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    """Attach exact live message and turn counts to repository rows."""

    records = [dict(row) for row in rows]
    session_ids = tuple(
        dict.fromkeys(
            str(row.get("session_id") or "")
            for row in records
            if row.get("session_id")
        )
    )
    if not session_ids:
        return records

    from ..db.sqlite import connect

    placeholders = ",".join("?" for _ in session_ids)
    con = connect(paths.db)
    try:
        count_rows = con.execute(
            f"""
            SELECT
                session_id,
                COUNT(*) AS message_count,
                COUNT(DISTINCT COALESCE(turn_id, message_id)) AS turn_count
            FROM agent_messages
            WHERE deleted=0 AND session_id IN ({placeholders})
            GROUP BY session_id
            """,
            session_ids,
        ).fetchall()
    finally:
        con.close()
    counts = {
        str(row["session_id"]): {
            "message_count": _nonnegative_int(row["message_count"]),
            "turn_count": _nonnegative_int(row["turn_count"]),
        }
        for row in count_rows
    }
    for row in records:
        row.update(
            counts.get(
                str(row.get("session_id") or ""),
                {"message_count": 0, "turn_count": 0},
            )
        )
    return records


def session_updated_ts(session: Mapping[str, Any]) -> float:
    raw = session.get("updated_at")
    if isinstance(raw, (int, float)):
        return float(raw)
    text = str(raw or "").strip()
    if not text:
        return 0.0
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return 0.0


def merge_session_dict(
    file_state: SessionState | Mapping[str, Any],
    db_row: Mapping[str, Any] | None,
) -> dict[str, Any]:
    """Merge file and DB session records without losing richer counts."""

    file_data = file_session_asdict(file_state)
    if not db_row:
        return file_data
    db_state = db_session_asdict(db_row)
    merged = {**db_state, **file_data}
    file_meta = file_data.get("meta") if isinstance(file_data.get("meta"), dict) else {}
    db_meta = db_state.get("meta") if isinstance(db_state.get("meta"), dict) else {}
    meta = {**db_meta, **file_meta}
    if not meta.get("title") and db_meta.get("title"):
        meta["title"] = db_meta["title"]
    merged["meta"] = meta
    if not merged.get("strategy_id"):
        merged["strategy_id"] = db_state.get("strategy_id")
    if not merged.get("created_at"):
        merged["created_at"] = db_state.get("created_at") or ""
    if session_updated_ts(db_state) > session_updated_ts(file_data):
        merged["updated_at"] = db_state.get("updated_at") or merged.get("updated_at")
    if not merged.get("source") or merged.get("source") == "session_file":
        merged["source"] = db_state.get("source") or merged.get("source") or ""
    merged["message_count"] = max(
        _nonnegative_int(file_data.get("message_count")),
        _nonnegative_int(db_state.get("message_count")),
    )
    merged["turn_count"] = max(
        _nonnegative_int(file_data.get("turn_count")),
        _nonnegative_int(db_state.get("turn_count")),
    )
    return merged


class SessionStore:
    """Workspace-backed store for ``SessionState``.

    The store is safe to construct cheaply; every call reads from disk
    so there's no cache to invalidate after an out-of-process writer
    (the CLI, another runtime instance, an operator editing the file
    directly) mutates the file.
    """

    def __init__(self, workspace: Path) -> None:
        self.workspace = Path(workspace)
        self.dir = self.workspace / "sessions"

    def _path(self, session_id: str) -> Path:
        return self.dir / f"{session_id}.json"

    def exists(self, session_id: str) -> bool:
        return self._path(session_id).is_file()

    def load(self, session_id: str) -> SessionState | None:
        path = self._path(session_id)
        if not path.is_file():
            return None
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            return None
        if not isinstance(raw, dict):
            return None
        return SessionState(
            session_id=str(raw.get("session_id") or session_id),
            strategy_id=raw.get("strategy_id"),
            created_at=str(raw.get("created_at") or ""),
            updated_at=str(raw.get("updated_at") or ""),
            turn_ids=list(raw.get("turn_ids") or []),
            invoked_skills=list(raw.get("invoked_skills") or []),
            skill_state=dict(raw.get("skill_state") or {}),
            last_action=raw.get("last_action"),
            meta=dict(raw.get("meta") or {}),
        )

    def save(self, state: SessionState) -> Path:
        self.dir.mkdir(parents=True, exist_ok=True)
        path = self._path(state.session_id)
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(
            json.dumps(state.asdict(), indent=2, ensure_ascii=False,
                       default=str),
            encoding="utf-8",
        )
        os.replace(tmp, path)
        return path

    def ensure(
        self, session_id: str | None,
        *, strategy_id: str | None = None,
    ) -> SessionState:
        sid = session_id or new_session_id()
        existing = self.load(sid)
        if existing is not None:
            if self._bind_strategy(existing, strategy_id):
                self.save(existing)
            return existing
        now = now_iso()
        state = SessionState(
            session_id=sid,
            strategy_id=strategy_id,
            created_at=now,
            updated_at=now,
        )
        self.save(state)
        return state

    def append_turn(
        self, session_id: str, turn_id: str,
        *,
        invoked_skills: list[str] | None = None,
        last_action: str | None = None,
        strategy_id: str | None = None,
    ) -> SessionState:
        state = self.load(session_id) or SessionState(
            session_id=session_id,
            created_at=now_iso(),
        )
        self._bind_strategy(state, strategy_id)
        if turn_id not in state.turn_ids:
            state.turn_ids.append(turn_id)
        for skill in invoked_skills or []:
            if skill not in state.invoked_skills:
                state.invoked_skills.append(skill)
        if last_action is not None:
            state.last_action = last_action
        state.updated_at = now_iso()
        self.save(state)
        return state

    def update_meta(
        self,
        session_id: str,
        patch: dict[str, Any],
        *,
        strategy_id: str | None = None,
    ) -> SessionState:
        state = self.load(session_id) or SessionState(
            session_id=session_id,
            created_at=now_iso(),
        )
        self._bind_strategy(state, strategy_id)
        state.meta.update(dict(patch or {}))
        state.updated_at = now_iso()
        self.save(state)
        return state

    @staticmethod
    def _bind_strategy(state: SessionState, requested: str | None) -> bool:
        requested_id = str(requested or "").strip() or None
        existing_id = str(state.strategy_id or "").strip() or None
        if requested_id and existing_id and requested_id != existing_id:
            from ..db.repositories import SessionStrategyMismatch

            raise SessionStrategyMismatch(
                state.session_id,
                existing_id,
                requested_id,
            )
        if requested_id and not existing_id:
            state.strategy_id = requested_id
            return True
        return False

    def record_skill_state(
        self, session_id: str, skill_id: str, state_payload: Any,
    ) -> SessionState:
        """Persist a per-skill state blob so a running skill (e.g. a
        long-lived script) can reload its handles on next turn."""
        state = self.load(session_id) or SessionState(
            session_id=session_id,
            created_at=now_iso(),
        )
        state.skill_state[skill_id] = state_payload
        state.updated_at = now_iso()
        if skill_id not in state.invoked_skills:
            state.invoked_skills.append(skill_id)
        self.save(state)
        return state

    def list(self, *, strategy_id: str | None = None,
             limit: int = 50) -> list[SessionState]:
        """List every persisted session, newest-first.

        Historically this used a ``sess_*.json`` glob which silently
        hid every session whose id was not auto-generated by the
        kernel — for example a chat thread keyed by a UUID, a gateway
        run keyed by ``gw_*``, or a curl operator who supplied their
        own ``session_id``. The dashboard's chat sidebar relies on
        this listing to merge backend-only conversations into the UI,
        so we now accept *any* ``*.json`` file in the sessions
        directory and let the per-file ``load`` validate that it's a
        well-formed :class:`SessionState`.
        """
        if not self.dir.is_dir():
            return []
        out: list[SessionState] = []
        for p in sorted(self.dir.glob("*.json"),
                        key=lambda f: f.stat().st_mtime, reverse=True):
            # Skip half-written staging files that ``save`` writes via
            # ``foo.json.tmp`` -> ``os.replace``. The glob above will
            # not match ``.tmp`` but defend in depth in case a crash
            # left one behind.
            if p.name.endswith(".tmp"):
                continue
            s = self.load(p.stem)
            if s is None:
                continue
            if strategy_id and s.strategy_id != strategy_id:
                continue
            out.append(s)
            if len(out) >= limit:
                break
        return out

    def delete(self, session_id: str) -> bool:
        path = self._path(session_id)
        if not path.is_file():
            return False
        try:
            path.unlink()
        except OSError:
            return False
        return True


__all__ = [
    "SessionState",
    "SessionStore",
    "db_session_asdict",
    "file_session_asdict",
    "hydrate_db_session_counts",
    "merge_session_dict",
    "new_session_id",
    "session_updated_ts",
]
