"""Typed access to the sqlite tables."""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from typing import Any


class SessionStrategyMismatch(RuntimeError):
    """A session cannot be rebound to a different strategy."""

    def __init__(self, session_id: str, existing: str, requested: str) -> None:
        self.session_id = session_id
        self.existing = existing
        self.requested = requested
        super().__init__(
            f"session {session_id!r} is bound to strategy {existing!r}; "
            f"cannot rebind to {requested!r}"
        )


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
            (
                id,
                kind,
                "pending",
                now,
                now + expires_s,
                json.dumps(payload, default=str),
            ),
        )

    def set_state(self, id: str, state: str) -> None:
        self.con.execute("UPDATE approvals SET state=? WHERE id=?", (state, id))

    def claim_resume(self, id: str, *, lease_s: float = 300.0) -> bool:
        """Atomically claim one approved record for execution.

        A process can die after changing the row to ``resuming``.  Treat a
        claim without a recent lease as recoverable, while retaining the
        compare-and-swap on the exact payload so two workers cannot reclaim
        the same stale row concurrently.
        """

        now = time.time()
        row = self.con.execute(
            "SELECT state, payload FROM approvals WHERE id=?", (id,),
        ).fetchone()
        if row is None:
            return False
        state = str(row["state"] or "")
        raw_payload = row["payload"]
        try:
            payload = json.loads(raw_payload) if isinstance(raw_payload, str) else dict(raw_payload or {})
        except (TypeError, ValueError):
            payload = {}
        claimed_at = float(payload.get("resume_claimed_at") or 0.0)
        stale = state == "resuming" and (
            claimed_at <= 0 or now - claimed_at >= max(1.0, float(lease_s))
        )
        if state != "approved" and not stale:
            return False
        payload["resume_claimed_at"] = now
        payload["resume_attempts"] = int(payload.get("resume_attempts") or 0) + 1
        encoded = json.dumps(payload, default=str)
        if state == "approved":
            cursor = self.con.execute(
                "UPDATE approvals SET state='resuming', payload=? "
                "WHERE id=? AND state='approved'",
                (encoded, id),
            )
        else:
            cursor = self.con.execute(
                "UPDATE approvals SET payload=? "
                "WHERE id=? AND state='resuming' AND payload=?",
                (encoded, id, raw_payload),
            )
        return cursor.rowcount == 1

    def finish_resume(
        self,
        id: str,
        *,
        state: str,
        intent_id: str | None,
        response_status: str | None = None,
        error: str | None = None,
    ) -> bool:
        """Persist the terminal result of a claimed approval resume."""
        if state not in {"resumed", "resume_failed"}:
            raise ValueError(f"invalid approval resume state: {state}")
        row = self.get(id)
        if row is None:
            return False
        raw_payload = row.get("payload")
        try:
            payload = json.loads(raw_payload) if isinstance(raw_payload, str) else dict(raw_payload or {})
        except (TypeError, ValueError):
            payload = {}
        payload.update({
            "resumed_intent_id": intent_id,
            "resume_status": response_status,
            "resume_error": error,
        })
        cursor = self.con.execute(
            "UPDATE approvals SET state=?, payload=? WHERE id=? AND state='resuming'",
            (state, json.dumps(payload, default=str), id),
        )
        return cursor.rowcount == 1

    def list_pending(self) -> list[dict]:
        rows = self.con.execute(
            "SELECT id,kind,state,created_at,expires_at,payload FROM approvals WHERE state='pending' ORDER BY created_at"
        ).fetchall()
        return [dict(r) for r in rows]

    def get(self, id: str) -> dict | None:
        r = self.con.execute(
            "SELECT id,kind,state,created_at,expires_at,payload FROM approvals WHERE id=?",
            (id,),
        ).fetchone()
        return dict(r) if r else None


@dataclass
class LLMUsageRepository:
    con: Any

    def record(
        self, *, tier: str, task: str, caller: str, tokens: int, usd: float
    ) -> None:
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
        strategy_id = str(strategy_id or "").strip() or None
        meta_json = json.dumps(meta or {}, default=str, ensure_ascii=False)
        cur = self.con.execute(
            """
            INSERT INTO agent_sessions(
                session_id, strategy_id, title, source,
                created_at, updated_at, meta_json
            )
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(session_id) DO UPDATE SET
                strategy_id=CASE
                    WHEN agent_sessions.strategy_id IS NULL
                         OR agent_sessions.strategy_id = ''
                    THEN excluded.strategy_id
                    ELSE agent_sessions.strategy_id
                END,
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
                    WHEN excluded.meta_json <> '{}' THEN json_patch(
                        agent_sessions.meta_json,
                        excluded.meta_json
                    )
                    ELSE agent_sessions.meta_json
                END
            WHERE excluded.strategy_id IS NULL
               OR excluded.strategy_id = ''
               OR agent_sessions.strategy_id IS NULL
               OR agent_sessions.strategy_id = ''
               OR agent_sessions.strategy_id = excluded.strategy_id
            """,
            (session_id, strategy_id, title, source, now, now, meta_json),
        )
        if not (cur.rowcount or 0) and strategy_id:
            row = self.con.execute(
                "SELECT strategy_id FROM agent_sessions WHERE session_id=?",
                (session_id,),
            ).fetchone()
            existing = str(row["strategy_id"] or "") if row is not None else ""
            if existing and existing != strategy_id:
                raise SessionStrategyMismatch(session_id, existing, strategy_id)

    def set_title(
        self, session_id: str, title: str, *, ts: float | None = None
    ) -> None:
        self.con.execute(
            "UPDATE agent_sessions SET title=?, updated_at=? WHERE session_id=?",
            (title, float(ts or time.time()), session_id),
        )

    def get_session(self, session_id: str) -> dict[str, Any] | None:
        row = self.con.execute(
            """
            SELECT session_id, strategy_id, title, source,
                   created_at, updated_at, meta_json, compaction_epoch
            FROM agent_sessions
            WHERE session_id=?
            """,
            (session_id,),
        ).fetchone()
        return dict(row) if row is not None else None

    def update_session_meta(
        self,
        session_id: str,
        meta: dict[str, Any],
        *,
        ts: float | None = None,
    ) -> bool:
        now = float(ts or time.time())
        cur = self.con.execute(
            """
            UPDATE agent_sessions
            SET meta_json=?, updated_at=?
            WHERE session_id=?
            """,
            (
                json.dumps(meta or {}, default=str, ensure_ascii=False),
                now,
                session_id,
            ),
        )
        return bool(cur.rowcount or 0)

    def save_turn_checkpoint(
        self,
        session_id: str,
        *,
        turn_id: str,
        checkpoint: dict[str, Any],
        expected_claim_id: str | None = None,
        max_bytes: int = 2 * 1024 * 1024,
        ts: float | None = None,
    ) -> bool:
        """Persist one private continuation checkpoint for ``session_id``.

        Fresh turns may replace only an unclaimed row. A resumed turn must
        provide the exact claim id obtained from :meth:`claim_turn_checkpoint`;
        this prevents a stale worker from overwriting a newer checkpoint.
        """

        sid = str(session_id or "").strip()
        tid = str(turn_id or "").strip()
        if not sid or not tid:
            raise ValueError("session_id and turn_id are required")
        payload = json.dumps(
            checkpoint or {},
            default=str,
            ensure_ascii=False,
            separators=(",", ":"),
        )
        payload_bytes = len(payload.encode("utf-8"))
        byte_limit = max(1, int(max_bytes or 0))
        if payload_bytes > byte_limit:
            raise ValueError(
                "turn checkpoint exceeds size limit: "
                f"{payload_bytes} > {byte_limit} bytes"
            )
        saved_at = float(ts or time.time())
        claim_id = str(expected_claim_id or "").strip()
        if claim_id:
            cur = self.con.execute(
                """
                UPDATE agent_turn_checkpoints
                SET checkpoint_json=?, checkpoint_bytes=?, saved_at=?,
                    claim_id=NULL, claimed_at=NULL
                WHERE session_id=? AND turn_id=? AND claim_id=?
                """,
                (payload, payload_bytes, saved_at, sid, tid, claim_id),
            )
            return bool(cur.rowcount or 0)

        cur = self.con.execute(
            """
            INSERT INTO agent_turn_checkpoints(
                session_id, turn_id, checkpoint_json, checkpoint_bytes,
                saved_at, claim_id, claimed_at
            ) VALUES (?, ?, ?, ?, ?, NULL, NULL)
            ON CONFLICT(session_id) DO UPDATE SET
                turn_id=excluded.turn_id,
                checkpoint_json=excluded.checkpoint_json,
                checkpoint_bytes=excluded.checkpoint_bytes,
                saved_at=excluded.saved_at,
                claim_id=NULL,
                claimed_at=NULL
            WHERE agent_turn_checkpoints.claim_id IS NULL
            """,
            (sid, tid, payload, payload_bytes, saved_at),
        )
        return bool(cur.rowcount or 0)

    def peek_turn_checkpoint(
        self,
        session_id: str,
    ) -> dict[str, Any] | None:
        """Read the private checkpoint row without claiming it."""

        row = self.con.execute(
            """
            SELECT session_id, turn_id, checkpoint_json, checkpoint_bytes,
                   saved_at, claim_id, claimed_at
            FROM agent_turn_checkpoints
            WHERE session_id=?
            """,
            (str(session_id or "").strip(),),
        ).fetchone()
        if row is None:
            return None
        out = dict(row)
        try:
            checkpoint = json.loads(str(out.pop("checkpoint_json") or "{}"))
        except (TypeError, ValueError, json.JSONDecodeError):
            checkpoint = None
        out["checkpoint"] = checkpoint if isinstance(checkpoint, dict) else None
        return out

    def claim_turn_checkpoint(
        self,
        session_id: str,
        *,
        turn_id: str,
        claim_id: str,
        ts: float | None = None,
    ) -> dict[str, Any] | None:
        """Atomically make one checkpoint unavailable to other resumptions."""

        sid = str(session_id or "").strip()
        tid = str(turn_id or "").strip()
        claim = str(claim_id or "").strip()
        if not sid or not tid or not claim:
            raise ValueError("session_id, turn_id and claim_id are required")
        cur = self.con.execute(
            """
            UPDATE agent_turn_checkpoints
            SET claim_id=?, claimed_at=?
            WHERE session_id=? AND turn_id=? AND claim_id IS NULL
            """,
            (claim, float(ts or time.time()), sid, tid),
        )
        if not (cur.rowcount or 0):
            return None
        return self.peek_turn_checkpoint(sid)

    def begin_turn_checkpoint_lease(
        self,
        session_id: str,
        *,
        turn_id: str,
        claim_id: str,
        checkpoint: dict[str, Any],
        stale_before: float | None = None,
        max_bytes: int = 64 * 1024,
        ts: float | None = None,
    ) -> dict[str, Any] | None:
        """Atomically create/replace a session checkpoint with a live lease.

        The row exists for the entire turn, closing the cross-process gap
        between abandoning an old checkpoint and saving the new one. A live
        claim wins; only an unclaimed or explicitly stale row may be replaced.
        """

        sid = str(session_id or "").strip()
        tid = str(turn_id or "").strip()
        claim = str(claim_id or "").strip()
        if not sid or not tid or not claim:
            raise ValueError("session_id, turn_id and claim_id are required")
        payload = json.dumps(
            checkpoint or {},
            default=str,
            ensure_ascii=False,
            separators=(",", ":"),
        )
        payload_bytes = len(payload.encode("utf-8"))
        byte_limit = max(1, int(max_bytes or 0))
        if payload_bytes > byte_limit:
            raise ValueError(
                "turn lease checkpoint exceeds size limit: "
                f"{payload_bytes} > {byte_limit} bytes"
            )
        started_at = float(ts or time.time())
        availability = "agent_turn_checkpoints.claim_id IS NULL"
        params: list[Any] = [
            sid,
            tid,
            payload,
            payload_bytes,
            started_at,
            claim,
            started_at,
        ]
        if stale_before is not None:
            availability = (
                "(agent_turn_checkpoints.claim_id IS NULL OR "
                "(agent_turn_checkpoints.claimed_at IS NOT NULL AND "
                "agent_turn_checkpoints.claimed_at <= ?))"
            )
            params.append(float(stale_before))
        cur = self.con.execute(
            """
            INSERT INTO agent_turn_checkpoints(
                session_id, turn_id, checkpoint_json, checkpoint_bytes,
                saved_at, claim_id, claimed_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(session_id) DO UPDATE SET
                turn_id=excluded.turn_id,
                checkpoint_json=excluded.checkpoint_json,
                checkpoint_bytes=excluded.checkpoint_bytes,
                saved_at=excluded.saved_at,
                claim_id=excluded.claim_id,
                claimed_at=excluded.claimed_at
            WHERE """ + availability,
            tuple(params),
        )
        if not (cur.rowcount or 0):
            return None
        return self.peek_turn_checkpoint(sid)

    def clear_turn_checkpoint(
        self,
        session_id: str,
        *,
        turn_id: str | None = None,
        claim_id: str | None = None,
    ) -> bool:
        """Delete one checkpoint, optionally guarded by turn/claim identity."""

        clauses = ["session_id=?"]
        params: list[Any] = [str(session_id or "").strip()]
        tid = str(turn_id or "").strip()
        claim = str(claim_id or "").strip()
        if tid:
            clauses.append("turn_id=?")
            params.append(tid)
        if claim:
            clauses.append("claim_id=?")
            params.append(claim)
        cur = self.con.execute(
            "DELETE FROM agent_turn_checkpoints WHERE " + " AND ".join(clauses),
            tuple(params),
        )
        return bool(cur.rowcount or 0)

    def update_context_checkpoint(
        self,
        session_id: str,
        checkpoint: dict[str, Any],
        *,
        expected_epoch: int,
    ) -> bool:
        """CAS a checkpoint without making the session look user-active."""

        next_epoch = int(expected_epoch) + 1
        next_checkpoint = dict(checkpoint)
        next_checkpoint["compaction_epoch"] = next_epoch
        payload = json.dumps(next_checkpoint, default=str, ensure_ascii=False)
        cur = self.con.execute(
            """
            UPDATE agent_sessions
            SET meta_json = json_set(
                CASE WHEN json_valid(meta_json) THEN meta_json ELSE '{}' END,
                '$.context_compaction',
                json(?)
            ),
                compaction_epoch = compaction_epoch + 1
            WHERE session_id=? AND compaction_epoch=?
            """,
            (payload, session_id, int(expected_epoch)),
        )
        return bool(cur.rowcount or 0)

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
                   created_at, updated_at, meta_json, compaction_epoch,
                   (
                       SELECT COUNT(*)
                       FROM agent_messages m
                       WHERE m.session_id = agent_sessions.session_id
                         AND m.deleted = 0
                   ) AS message_count
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

    def compaction_transcript(
        self,
        session_id: str,
        *,
        after_seq: int = 0,
    ) -> list[dict[str, Any]]:
        """Return the append-only transcript window after a checkpoint cursor."""

        rows = self.con.execute(
            """
            SELECT rowid AS message_seq, message_id, session_id, turn_id,
                   role, content, ts, meta_json
            FROM agent_messages
            WHERE session_id=? AND deleted=0 AND rowid>?
            ORDER BY rowid ASC
            """,
            (session_id, max(0, int(after_seq))),
        ).fetchall()
        return [dict(row) for row in rows]

    def compaction_cursor_matches(
        self,
        session_id: str,
        *,
        message_seq: int,
        message_id: str,
    ) -> bool:
        row = self.con.execute(
            """
            SELECT message_id FROM agent_messages
            WHERE session_id=? AND rowid=? AND deleted=0
            """,
            (session_id, int(message_seq)),
        ).fetchone()
        return row is not None and str(row["message_id"] or "") == message_id

    def tool_events(
        self,
        session_id: str,
        *,
        turn_ids: list[str] | tuple[str, ...] | set[str] | None = None,
    ) -> list[dict[str, Any]]:
        params: list[Any] = [session_id]
        where = "session_id=?"
        if turn_ids:
            clean_turn_ids = [str(t) for t in turn_ids if str(t or "").strip()]
            if not clean_turn_ids:
                return []
            placeholders = ",".join("?" for _ in clean_turn_ids)
            where += f" AND turn_id IN ({placeholders})"
            params.extend(clean_turn_ids)
        rows = self.con.execute(
            f"""
            SELECT rowid, event_id, session_id, turn_id, call_id,
                   tool, phase, ok, ts, payload_json
            FROM agent_tool_events
            WHERE {where}
            ORDER BY ts ASC, rowid ASC
            """,
            params,
        ).fetchall()
        return [dict(r) for r in rows]
