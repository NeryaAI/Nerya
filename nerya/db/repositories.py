"""Typed access to the sqlite tables."""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from typing import Any


@dataclass
class DedupeRepository:
    con: Any

    def seen(self, scope: str, key: str, window_s: float) -> bool:
        now = time.time()
        row = self.con.execute(
            "SELECT ts FROM dedupe WHERE scope=? AND key=?", (scope, key)
        ).fetchone()
        if row and now - row["ts"] < window_s:
            return True
        self.con.execute(
            "INSERT OR REPLACE INTO dedupe(scope,key,ts) VALUES(?,?,?)",
            (scope, key, now),
        )
        return False

    def purge(self, older_than_s: float) -> int:
        cutoff = time.time() - older_than_s
        cur = self.con.execute("DELETE FROM dedupe WHERE ts < ?", (cutoff,))
        return cur.rowcount or 0


@dataclass
class CooldownRepository:
    con: Any

    def hit_and_check(self, scope: str, key: str, cooldown_s: float) -> bool:
        """Return True if we're still within cooldown (i.e. block)."""
        now = time.time()
        row = self.con.execute(
            "SELECT until FROM cooldown WHERE scope=? AND key=?", (scope, key)
        ).fetchone()
        if row and row["until"] > now:
            return True
        self.con.execute(
            "INSERT OR REPLACE INTO cooldown(scope,key,until) VALUES(?,?,?)",
            (scope, key, now + cooldown_s),
        )
        return False


@dataclass
class ProposalRepository:
    con: Any

    def upsert(self, *, id: str, kind: str, state: str, path: str) -> None:
        self.con.execute(
            """
            INSERT INTO proposals(id,kind,state,path,created_at)
            VALUES(?,?,?,?,?)
            ON CONFLICT(id) DO UPDATE SET state=excluded.state
            """,
            (id, kind, state, path, time.time()),
        )

    def list(self, state: str | None = None) -> list[dict]:
        if state:
            rows = self.con.execute(
                "SELECT id,kind,state,path,created_at FROM proposals WHERE state=? ORDER BY created_at DESC",
                (state,),
            ).fetchall()
        else:
            rows = self.con.execute(
                "SELECT id,kind,state,path,created_at FROM proposals ORDER BY created_at DESC"
            ).fetchall()
        return [dict(r) for r in rows]

    def get(self, id: str) -> dict | None:
        r = self.con.execute(
            "SELECT id,kind,state,path,created_at FROM proposals WHERE id=?", (id,)
        ).fetchone()
        return dict(r) if r else None


@dataclass
class ApprovalRepository:
    con: Any

    def insert(self, *, id: str, kind: str, expires_s: float, payload: dict) -> None:
        now = time.time()
        self.con.execute(
            "INSERT INTO approvals(id,kind,state,created_at,expires_at,payload) VALUES(?,?,?,?,?,?)",
            (id, kind, "pending", now, now + expires_s, json.dumps(payload, default=str)),
        )

    def set_state(self, id: str, state: str) -> None:
        self.con.execute("UPDATE approvals SET state=? WHERE id=?", (state, id))

    def list_pending(self) -> list[dict]:
        rows = self.con.execute(
            "SELECT id,kind,state,created_at,expires_at,payload FROM approvals WHERE state='pending' ORDER BY created_at"
        ).fetchall()
        return [dict(r) for r in rows]

    def get(self, id: str) -> dict | None:
        r = self.con.execute(
            "SELECT id,kind,state,created_at,expires_at,payload FROM approvals WHERE id=?", (id,)
        ).fetchone()
        return dict(r) if r else None


@dataclass
class LLMUsageRepository:
    con: Any

    def record(self, *, tier: str, task: str, caller: str, tokens: int, usd: float) -> None:
        self.con.execute(
            "INSERT INTO llm_usage(ts,tier,task,caller,tokens,usd) VALUES(?,?,?,?,?,?)",
            (time.time(), tier, task, caller, int(tokens), float(usd)),
        )

    def daily_spend(self, tier: str) -> float:
        day = time.time() - 86400
        row = self.con.execute(
            "SELECT COALESCE(SUM(usd),0) AS s FROM llm_usage WHERE tier=? AND ts>=?",
            (tier, day),
        ).fetchone()
        return float(row["s"] if row else 0.0)


@dataclass
class AgentSessionRepository:
    con: Any

    def upsert_session(
        self,
        *,
        session_id: str,
        strategy_id: str | None = None,
        title: str = "",
        source: str = "",
        meta: dict[str, Any] | None = None,
        ts: float | None = None,
    ) -> None:
        now = float(ts or time.time())
        meta_json = json.dumps(meta or {}, default=str, ensure_ascii=False)
        self.con.execute(
            """
            INSERT INTO agent_sessions(
                session_id, strategy_id, title, source,
                created_at, updated_at, meta_json
            )
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(session_id) DO UPDATE SET
                strategy_id=COALESCE(excluded.strategy_id, agent_sessions.strategy_id),
                title=CASE
                    WHEN excluded.title <> '' THEN excluded.title
                    ELSE agent_sessions.title
                END,
                source=CASE
                    WHEN excluded.source <> '' THEN excluded.source
                    ELSE agent_sessions.source
                END,
                updated_at=excluded.updated_at,
                meta_json=CASE
                    WHEN excluded.meta_json <> '{}' THEN excluded.meta_json
                    ELSE agent_sessions.meta_json
                END
            """,
            (session_id, strategy_id, title, source, now, now, meta_json),
        )

    def set_title(self, session_id: str, title: str, *, ts: float | None = None) -> None:
        self.con.execute(
            "UPDATE agent_sessions SET title=?, updated_at=? WHERE session_id=?",
            (title, float(ts or time.time()), session_id),
        )

    def get_session(self, session_id: str) -> dict[str, Any] | None:
        row = self.con.execute(
            """
            SELECT session_id, strategy_id, title, source,
                   created_at, updated_at, meta_json
            FROM agent_sessions
            WHERE session_id=?
            """,
            (session_id,),
        ).fetchone()
        return dict(row) if row is not None else None

    def record_message(
        self,
        *,
        message_id: str,
        session_id: str,
        role: str,
        content: str,
        turn_id: str | None = None,
        ts: float | None = None,
        meta: dict[str, Any] | None = None,
    ) -> None:
        self.con.execute(
            """
            INSERT INTO agent_messages(
                message_id, session_id, turn_id, role, content, ts, meta_json
            )
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(message_id) DO UPDATE SET
                content=excluded.content,
                meta_json=excluded.meta_json,
                deleted=0
            """,
            (
                message_id,
                session_id,
                turn_id,
                role,
                content,
                float(ts or time.time()),
                json.dumps(meta or {}, default=str, ensure_ascii=False),
            ),
        )

    def delete_message(self, message_id: str) -> None:
        self.con.execute(
            "UPDATE agent_messages SET deleted=1 WHERE message_id=?",
            (message_id,),
        )

    def update_message_content(
        self,
        *,
        session_id: str,
        message_id: str,
        content: str,
        ts: float | None = None,
    ) -> bool:
        now = float(ts or time.time())
        row = self.con.execute(
            """
            SELECT meta_json
            FROM agent_messages
            WHERE session_id=? AND message_id=? AND deleted=0
            """,
            (session_id, message_id),
        ).fetchone()
        if row is None:
            return False
        try:
            meta = json.loads(row["meta_json"] or "{}")
        except Exception:
            meta = {}
        if not isinstance(meta, dict):
            meta = {}
        meta["edited_at"] = now
        cur = self.con.execute(
            """
            UPDATE agent_messages
            SET content=?, meta_json=?
            WHERE session_id=? AND message_id=? AND deleted=0
            """,
            (
                content,
                json.dumps(meta, default=str, ensure_ascii=False),
                session_id,
                message_id,
            ),
        )
        self.con.execute(
            "UPDATE agent_sessions SET updated_at=? WHERE session_id=?",
            (now, session_id),
        )
        return bool(cur.rowcount or 0)

    def delete_session_message(
        self,
        *,
        session_id: str,
        message_id: str,
        ts: float | None = None,
    ) -> bool:
        now = float(ts or time.time())
        cur = self.con.execute(
            """
            UPDATE agent_messages
            SET deleted=1
            WHERE session_id=? AND message_id=? AND deleted=0
            """,
            (session_id, message_id),
        )
        self.con.execute(
            "UPDATE agent_sessions SET updated_at=? WHERE session_id=?",
            (now, session_id),
        )
        return bool(cur.rowcount or 0)

    def record_tool_event(
        self,
        *,
        event_id: str,
        session_id: str,
        tool: str,
        phase: str,
        turn_id: str | None = None,
        call_id: str | None = None,
        ok: bool | None = None,
        payload: dict[str, Any] | None = None,
        ts: float | None = None,
    ) -> None:
        self.con.execute(
            """
            INSERT OR REPLACE INTO agent_tool_events(
                event_id, session_id, turn_id, call_id,
                tool, phase, ok, ts, payload_json
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                event_id,
                session_id,
                turn_id,
                call_id,
                tool,
                phase,
                None if ok is None else (1 if ok else 0),
                float(ts or time.time()),
                json.dumps(payload or {}, default=str, ensure_ascii=False),
            ),
        )

    def list_sessions(self, *, limit: int = 50) -> list[dict[str, Any]]:
        rows = self.con.execute(
            """
            SELECT session_id, strategy_id, title, source,
                   created_at, updated_at, meta_json
            FROM agent_sessions
            ORDER BY updated_at DESC
            LIMIT ?
            """,
            (int(limit),),
        ).fetchall()
        return [dict(r) for r in rows]

    def transcript(self, session_id: str, *, limit: int = 200) -> list[dict[str, Any]]:
        limit = int(limit)
        if limit > 0:
            rows = self.con.execute(
                """
                SELECT message_id, session_id, turn_id, role, content, ts, meta_json
                FROM (
                    SELECT message_id, session_id, turn_id, role, content, ts, meta_json
                    FROM agent_messages
                    WHERE session_id=? AND deleted=0
                    ORDER BY
                        ts DESC,
                        CASE role
                            WHEN 'assistant' THEN 0
                            WHEN 'user' THEN 1
                            ELSE 2
                        END,
                        message_id DESC
                    LIMIT ?
                )
                ORDER BY
                    ts ASC,
                    CASE role
                        WHEN 'user' THEN 0
                        WHEN 'assistant' THEN 1
                        ELSE 2
                    END,
                    message_id ASC
                """,
                (session_id, limit),
            ).fetchall()
        else:
            rows = self.con.execute(
                """
                SELECT message_id, session_id, turn_id, role, content, ts, meta_json
                FROM agent_messages
                WHERE session_id=? AND deleted=0
                ORDER BY
                    ts ASC,
                    CASE role
                        WHEN 'user' THEN 0
                        WHEN 'assistant' THEN 1
                        ELSE 2
                    END,
                    message_id ASC
                """,
                (session_id,),
            ).fetchall()
        return [dict(r) for r in rows]
