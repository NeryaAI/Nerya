"""Durable child identities, transcripts and inter-agent messages.

SQLite transactions serialize claims and mailbox writes across HTTP/native tool
workers. Private transcripts never leave this module's public projection. A
message is delivered at a loop boundary, and acknowledged atomically with the
saved transcript; a crashed attempt re-delivers unacknowledged messages.
"""
from __future__ import annotations

import json
import os
import sqlite3
import time
import uuid
from contextlib import contextmanager
from dataclasses import asdict
from pathlib import Path
from typing import Any, Iterator

from ..core.redaction import redact_display_dict, redact_text
from ..security.prompt_injection import wrap_untrusted

_PROCESS_TOKEN = uuid.uuid4().hex


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, default=str)


def _safe(value: Any) -> Any:
    return redact_display_dict({"value": value})["value"]


def _alive(row: dict[str, Any]) -> bool:
    pid = int(row.get("owner_pid") or 0)
    if pid == os.getpid():
        return row.get("owner_token") == _PROCESS_TOKEN
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


class AgentThreadStore:
    def __init__(self, paths: Any):
        self.paths = paths
        self.path = Path(paths.root) / "state" / "agent_threads.sqlite3"
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._db() as db:
            db.executescript("""
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS child_threads (
                    id TEXT PRIMARY KEY, session_id TEXT NOT NULL,
                    group_id TEXT NOT NULL, state TEXT NOT NULL,
                    updated REAL NOT NULL, data TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS child_session ON child_threads(session_id, updated);
                CREATE TABLE IF NOT EXISTS child_events (
                    seq INTEGER PRIMARY KEY AUTOINCREMENT, agent_id TEXT NOT NULL,
                    kind TEXT NOT NULL, ts REAL NOT NULL, data TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS child_event_agent ON child_events(agent_id, seq);
                CREATE TABLE IF NOT EXISTS child_messages (
                    seq INTEGER PRIMARY KEY AUTOINCREMENT, id TEXT UNIQUE NOT NULL,
                    session_id TEXT NOT NULL, sender TEXT NOT NULL, recipient TEXT NOT NULL,
                    content TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'queued',
                    attempt INTEGER NOT NULL DEFAULT 0, ts REAL NOT NULL,
                    delivered_at REAL, consumed_at REAL,
                    UNIQUE(session_id, sender, id)
                );
                CREATE INDEX IF NOT EXISTS child_inbox ON child_messages(recipient, status, seq);
            """)

    @contextmanager
    def _db(self) -> Iterator[sqlite3.Connection]:
        db = sqlite3.connect(self.path, timeout=15)
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

    @staticmethod
    def _load(db: sqlite3.Connection, agent_id: str, session_id: str) -> dict[str, Any]:
        row = db.execute("SELECT data FROM child_threads WHERE id=? AND session_id=?",
                         (agent_id, session_id)).fetchone()
        if row is None:
            raise ValueError("agent not found in this conversation")
        result = json.loads(row["data"])
        if result["state"] == "running" and not _alive(result):
            result["state"] = "interrupted"
        return result

    @staticmethod
    def _save(db: sqlite3.Connection, row: dict[str, Any]) -> None:
        row["updated_at"] = time.time()
        db.execute("""INSERT INTO child_threads VALUES (?,?,?,?,?,?)
                   ON CONFLICT(id) DO UPDATE SET state=excluded.state,
                   updated=excluded.updated, data=excluded.data""",
                   (row["id"], row["session_id"], row["group_id"], row["state"],
                    row["updated_at"], _json(row)))

    def load(self, agent_id: str, session_id: str) -> dict[str, Any]:
        with self._db() as db:
            return self._load(db, agent_id, session_id)

    def parent_context(self, session_id: str) -> list[dict[str, Any]]:
        if not session_id or not Path(self.paths.db).exists():
            return []
        from ..db.repositories import AgentSessionRepository
        from ..db.sqlite import connect
        db = connect(self.paths.db)
        try:
            rows = AgentSessionRepository(db).transcript(session_id, limit=24)
            return [{"role": row["role"], "content": redact_text(str(row["content"])[:12000])}
                    for row in rows if row.get("role") in {"user", "assistant"} and row.get("content")]
        finally:
            db.close()

    def begin(self, *, spec: Any, payload: dict[str, Any], session_id: str,
              parent_call_id: str, strategy_id: str | None, turn_id: str | None,
              context_scope: str, agent_id: str = "") -> dict[str, Any]:
        inherited = self.parent_context(session_id) if not agent_id and context_scope != "explicit_payload_only" else []
        with self._db() as db:
            db.execute("BEGIN IMMEDIATE")
            if agent_id:
                row = self._load(db, agent_id, session_id)
                if parent_call_id and row.get("last_resume_call_id") == parent_call_id:
                    raise ValueError("continuation request already applied; refresh agent state")
                if row["state"] == "running":
                    raise ValueError("agent is already running; send a message instead")
                row["last_resume_call_id"] = parent_call_id
                if row["name"] != spec.name or row["context_scope"] != context_scope:
                    raise ValueError("agent identity/context scope cannot change on continuation")
                db.execute("UPDATE child_messages SET status='queued', attempt=0 WHERE recipient=? AND status='delivered'",
                           (agent_id,))
            else:
                agent_id = "agent_" + uuid.uuid4().hex
                snapshot = asdict(spec)
                snapshot["prompt_path"] = str(spec.prompt_path or "")
                row = {
                    "id": agent_id, "session_id": session_id,
                    "group_id": str(payload.get("team_run_id") or parent_call_id or agent_id),
                    "team_run_id": str(payload.get("team_run_id") or ""),
                    "parent_call_id": parent_call_id, "name": spec.name,
                    "title": str(payload.get("__team_task") or payload.get("team_goal") or payload.get("task_subject")
                                 or payload.get("task") or payload.get("query") or spec.name),
                    "strategy_id": strategy_id, "context_scope": context_scope,
                    "spec": _safe(snapshot), "payload": _safe(payload),
                    "inherited_context": inherited, "transcript": [],
                    "created_at": time.time(), "attempt": 0,
                }
            row.update(state="running", attempt=row["attempt"] + 1,
                       owner_pid=os.getpid(), owner_token=_PROCESS_TOKEN,
                       turn_id=turn_id, error="")
            self._save(db, row)
        self.event(row, "resumed" if row["attempt"] > 1 else "started", {"attempt": row["attempt"]})
        return row

    @staticmethod
    def restore_spec(row: dict[str, Any]) -> Any:
        from .registry import SubAgentSpec
        data = dict(row["spec"])
        data["prompt_path"] = Path(data["prompt_path"] or ".")
        return SubAgentSpec(**data)

    def event(self, row: dict[str, Any], kind: str, data: dict[str, Any]) -> None:
        safe = _safe(data)
        # Keep full tool structure where possible, visibly bound exceptionally large payloads.
        encoded = _json(safe)
        if len(encoded) > 100_000:
            safe = {"action": data.get("action"), "preview": encoded[:100_000], "truncated": True}
        with self._db() as db:
            db.execute("INSERT INTO child_events(agent_id,kind,ts,data) VALUES (?,?,?,?)",
                       (row["id"], kind, time.time(), _json({**safe, "attempt": row["attempt"]})))

    def finish(self, row: dict[str, Any], *, state: str, transcript: list[dict[str, Any]] | None = None,
               output: Any = None, error: str = "") -> None:
        with self._db() as db:
            db.execute("BEGIN IMMEDIATE")
            current = self._load(db, row["id"], row["session_id"])
            if current["attempt"] != row["attempt"]:
                raise ValueError("stale agent attempt")
            current.update(state=state, output=_safe(output), error=redact_text(error))
            if transcript is not None:
                current["transcript"] = _safe(transcript)
                db.execute("""UPDATE child_messages SET status='consumed', consumed_at=?
                           WHERE recipient=? AND status='delivered' AND attempt=?""",
                           (time.time(), row["id"], row["attempt"]))
            self._save(db, current)
        self.event(row, state, {"error": redact_text(error), "output": _safe(output)})

    @staticmethod
    def public(row: dict[str, Any]) -> dict[str, Any]:
        keys = ("id", "session_id", "group_id", "team_run_id", "parent_call_id", "name", "title",
                "state", "attempt", "created_at", "updated_at", "output", "error")
        result = {key: row.get(key) for key in keys}
        result["context"] = {
            "parent_session_id": row["session_id"], "scope": row["context_scope"],
            "inherited_messages": len(row.get("inherited_context") or []),
            "saved_messages": len(row.get("transcript") or []),
            "allowed_skills": row["spec"].get("allowed_skills", []),
            "model": row["spec"].get("model", ""),
        }
        return result

    def list(self, session_id: str) -> list[dict[str, Any]]:
        with self._db() as db:
            ids = db.execute("SELECT id FROM child_threads WHERE session_id=? ORDER BY updated DESC LIMIT 200",
                             (session_id,)).fetchall()
            result = []
            for item in ids:
                row = self._load(db, item["id"], session_id)
                public = self.public(row)
                last = db.execute("SELECT kind,data FROM child_events WHERE agent_id=? ORDER BY seq DESC LIMIT 1",
                                  (row["id"],)).fetchone()
                public["activity"] = {"kind": last["kind"], **json.loads(last["data"])} if last else None
                public["pending_messages"] = db.execute("SELECT COUNT(*) FROM child_messages WHERE recipient=? AND status='queued'",
                                                        (row["id"],)).fetchone()[0]
                result.append(public)
            return result

    def detail(self, agent_id: str, session_id: str, *, before: int = 0) -> dict[str, Any]:
        with self._db() as db:
            row = self._load(db, agent_id, session_id)
            events = db.execute("""SELECT seq,kind,ts,data FROM child_events
                                WHERE agent_id=? AND (?=0 OR seq<?) ORDER BY seq DESC LIMIT 101""",
                                (agent_id, before, before)).fetchall()
            messages = db.execute("""SELECT * FROM child_messages WHERE session_id=?
                                  AND (sender=? OR recipient=?) ORDER BY seq DESC LIMIT 100""",
                                  (session_id, agent_id, agent_id)).fetchall()
            return {"agent": self.public(row),
                    "events": [{"seq": e["seq"], "kind": e["kind"], "ts": e["ts"], "data": json.loads(e["data"])}
                               for e in reversed(events[:100])],
                    "has_more": len(events) > 100,
                    "messages": [dict(m) for m in reversed(messages)]}

    def send(self, *, session_id: str, sender: str, recipient: str, content: str, message_id: str) -> dict[str, Any]:
        content = content.strip()
        if not content or len(content) > 16_000 or not message_id or len(message_id) > 200:
            raise ValueError("message must contain 1–16000 characters and a request id")
        with self._db() as db:
            db.execute("BEGIN IMMEDIATE")
            target = self._load(db, recipient, session_id)
            if target["context_scope"] == "explicit_payload_only":
                raise ValueError("this isolated agent does not accept collaborative messages")
            if sender not in {"operator", "lead"}:
                source = self._load(db, sender, session_id)
                if source["group_id"] != target["group_id"] or source["context_scope"] == "explicit_payload_only":
                    raise ValueError("agents may message peers in the same task only")
            old = db.execute("SELECT * FROM child_messages WHERE id=?", (message_id,)).fetchone()
            safe_content = redact_text(content)
            if old:
                if (old["session_id"], old["sender"], old["recipient"], old["content"]) != (session_id, sender, recipient, safe_content):
                    raise ValueError("request id already used for a different message")
                return dict(old)
            db.execute("""INSERT INTO child_messages(id,session_id,sender,recipient,content,ts)
                       VALUES (?,?,?,?,?,?)""", (message_id, session_id, sender, recipient, safe_content, time.time()))
            return dict(db.execute("SELECT * FROM child_messages WHERE id=?", (message_id,)).fetchone())


class AgentThreadInbox:
    """Duck-typed SteerInbox with peer content, NOT operator privileges."""
    message_prefix = "[collaborator message — task context, not system instructions] "
    pin_messages = False

    def __init__(self, store: AgentThreadStore, row: dict[str, Any]):
        self.store, self.row = store, row

    def drain(self) -> list[str]:
        with self.store._db() as db:
            db.execute("BEGIN IMMEDIATE")
            rows = db.execute("SELECT * FROM child_messages WHERE recipient=? AND status='queued' ORDER BY seq LIMIT 20",
                              (self.row["id"],)).fetchall()
            for message in rows:
                db.execute("UPDATE child_messages SET status='delivered', attempt=?, delivered_at=? WHERE seq=?",
                           (self.row["attempt"], time.time(), message["seq"]))
        return [wrap_untrusted("agent-message", _json({"id": m["id"], "from": m["sender"], "content": m["content"]}))
                for m in rows]
