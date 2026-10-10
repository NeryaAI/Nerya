"""Assistant delegation: separate command-backed conversations and durable results."""
from __future__ import annotations

import hashlib
import json
import time
from contextlib import closing

from .assistants import AssistantError, AssistantStore, DEFAULT_ID
from .session import SessionStore
from ..db.repositories import AgentSessionRepository

TERMINAL = {"succeeded", "failed", "interrupted", "removed"}


class AssistantWork:
    def __init__(self, paths):
        self.paths = paths
        with closing(self._connect()) as con:
            con.executescript("""
                CREATE TABLE IF NOT EXISTS assistant_work (
                    task_id TEXT PRIMARY KEY, assistant_id TEXT NOT NULL,
                    parent_session_id TEXT NOT NULL, task_session_id TEXT NOT NULL UNIQUE,
                    title TEXT NOT NULL, task_kind TEXT NOT NULL, fingerprint TEXT NOT NULL,
                    command_id TEXT NOT NULL, request_json TEXT NOT NULL, actor_id TEXT NOT NULL,
                    created_at REAL NOT NULL);
                CREATE TABLE IF NOT EXISTS assistant_results (
                    command_id TEXT PRIMARY KEY, task_id TEXT NOT NULL, state TEXT NOT NULL,
                    content TEXT NOT NULL, summary TEXT NOT NULL, result_json TEXT NOT NULL,
                    created_at REAL NOT NULL);
            """)

    def _connect(self):
        return AssistantStore(self.paths)._connect()

    def session(self, sid):
        with closing(self._connect()) as con:
            row = AgentSessionRepository(con).get_session(sid)
        return json.loads(row["meta_json"] or "{}") if row else {}

    def create(self, config, call, sid, actor, *, manager=None):
        from .command_runtime import runtime
        from .command_store import CommandError
        arguments = call.arguments or {}
        title, prompt = arguments.get("title"), arguments.get("prompt")
        kind = arguments.get("kind", "general")
        if (not isinstance(title, str) or not title.strip() or len(title) > 160
                or not isinstance(prompt, str) or not prompt.strip() or len(prompt) > 12000
                or not isinstance(kind, str) or kind not in {"research", "strategy", "general"}):
            raise AssistantError("invalid_assistant_task")
        if self.session(sid).get("conversation_kind") == "task" or config.get("runtime.task_run_id"):
            raise AssistantError("assistant_task_nested")
        assistants = AssistantStore(self.paths)
        profile = assistants.for_session(sid) or assistants.bind(sid, DEFAULT_ID)
        key = hashlib.sha256(json.dumps([sid, call.turn_id, call.id]).encode()).hexdigest()[:24]
        tid, child, cid = "awork_" + key, "taskchat_" + key, "acmd_" + key
        fingerprint = json.dumps([title.strip(), prompt.strip(), kind, actor], ensure_ascii=False)
        manager = manager or runtime(config)
        with closing(self._connect()) as con:
            if con.execute("SELECT 1 FROM agent_deleted_sessions WHERE session_id=?", (sid,)).fetchone():
                raise AssistantError("session_deleted", 409)
            parent = con.execute("SELECT request_json,context_json FROM agent_commands WHERE session_id=? AND turn_id=? AND kind!='guide' ORDER BY created_at DESC,position DESC LIMIT 1", (sid, call.turn_id)).fetchone()
            parent_request = json.loads(parent["request_json"]) if parent else {}
            parent_context = json.loads(parent["context_json"]) if parent else {}
            if parent_request.get("work_mode") == "plan":
                raise AssistantError("assistant_task_plan_only")
            prior = con.execute("SELECT * FROM assistant_work WHERE task_id=?", (tid,)).fetchone()
            if prior and prior["fingerprint"] != fingerprint:
                raise AssistantError("assistant_task_idempotency_conflict", 409)
            if not prior:
                count = con.execute("""SELECT COUNT(*) FROM assistant_work w JOIN agent_commands c
                    ON c.command_id=w.command_id WHERE w.parent_session_id=?
                    AND c.state IN ('queued','running','stopping','awaiting_input','awaiting_approval')""", (sid,)).fetchone()[0]
                if count >= 8:
                    raise AssistantError("assistant_task_limit", 409)
        assistants.bind(child, profile["id"], source="assistant_task", title=title.strip())
        meta = {"conversation_kind": "task", "parent_session_id": sid, "assistant_task_id": tid, "task_kind": kind}
        with closing(self._connect()) as con:
            AgentSessionRepository(con).upsert_session(session_id=child, title=title.strip(), source="assistant_task", meta=meta)
            request = {"source": "assistant_task", "assistant_id": profile["id"], "payload": {"text": prompt.strip()},
                       "permission_mode": config.get("runtime.permission_mode", "default")}
            for field in ("permission_mode", "work_mode", "reasoning_effort", "reasoning_summary", "max_iterations", "max_total_tool_calls", "max_wall_seconds", "model_tier"):
                if field in parent_request: request[field] = parent_request[field]
            attachments = (parent_request.get("payload") or {}).get("attachments")
            if attachments: request["payload"]["attachments"] = attachments
            for key_name in ("model_provider", "model_id", "model_tier"):
                value = profile[key_name] or config.get("agent.native." + key_name)
                if value: request[key_name] = value
            accepted_model = parent_context.get("accepted_model") or {}
            if accepted_model.get("provider") and accepted_model.get("model"):
                request.update(model_provider=accepted_model["provider"], model_id=accepted_model["model"])
            con.execute("INSERT OR IGNORE INTO assistant_work VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                        (tid, profile["id"], sid, child, title.strip(), kind, fingerprint, cid,
                         json.dumps(request), actor, time.time()))
            saved = dict(con.execute("SELECT * FROM assistant_work WHERE task_id=?", (tid,)).fetchone())
            con.commit()
        if saved["fingerprint"] != fingerprint:
            raise AssistantError("assistant_task_idempotency_conflict", 409)
        SessionStore(self.paths.root).update_meta(child, meta)
        try:
            manager.submit({"session_id": child, "command_id": cid, "command_type": "send",
                            "request": json.loads(saved["request_json"]), "_auth_actor_id": actor}, start=False)
        except CommandError as exc:
            raise AssistantError(exc.code, exc.status) from exc
        manager.kick(child)
        return {"ok": True, "protocol": "nerya.assistant_task.v2", "task_id": tid,
                "title": title.strip(), "assistant_id": profile["id"], "session_id": sid,
                "parent_session_id": sid, "task_session_id": child, "command_id": cid, "state": "queued"}

    def sync(self, sid=None):
        """Repair missing completion delivery without replaying task execution."""
        with closing(self._connect()) as con:
            con.execute("BEGIN IMMEDIATE")
            rows = con.execute("SELECT * FROM assistant_work" + (" WHERE task_session_id=? OR parent_session_id=?" if sid else ""),
                               (sid, sid) if sid else ()).fetchall()
            repo = AgentSessionRepository(con)
            for work in rows:
                commands = con.execute("""SELECT c.* FROM agent_commands c WHERE c.session_id=? AND c.kind!='guide'
                    AND c.state IN ('succeeded','failed','interrupted','removed')
                    AND NOT EXISTS (SELECT 1 FROM assistant_results r WHERE r.command_id=c.command_id)
                    ORDER BY c.created_at,c.position""", (work["task_session_id"],)).fetchall()
                for command in commands:
                    result = json.loads(command["result_json"] or "{}")
                    if not result:
                        answer = con.execute("SELECT meta_json FROM agent_messages WHERE session_id=? AND message_id=?", (work["task_session_id"], command["turn_id"]+":assistant")).fetchone()
                        result = (json.loads(answer[0] or "{}").get("turn") or {}) if answer else {}
                    error = json.loads(command["error_json"] or "null")
                    content = str(result.get("final_text") or (error or {}).get("message") or (error or {}).get("code") or command["state"])
                    summary = content[:1200] + ("…" if len(content) > 1200 else "")
                    con.execute("INSERT INTO assistant_results VALUES (?,?,?,?,?,?,?)", (command["command_id"], work["task_id"],
                                command["state"], content, summary, json.dumps(result, ensure_ascii=False), time.time()))
                    deleted = con.execute("SELECT 1 FROM agent_deleted_sessions WHERE session_id=?", (work["parent_session_id"],)).fetchone()
                    if not deleted:
                        status = {"succeeded": "已完成 / Completed", "failed": "执行失败 / Failed", "interrupted": "已停止 / Stopped", "removed": "已取消 / Cancelled"}[command["state"]]
                        text = f"**{work['title']}** · {status}\n\n{summary}\n\n[查看任务对话 / Open task conversation](/chat/{work['task_session_id']})"
                        repo.record_message(message_id="assistant_result:" + command["command_id"], session_id=work["parent_session_id"],
                                            role="assistant", content=text, meta={"assistant_task_id": work["task_id"],
                                            "task_session_id": work["task_session_id"], "execution_status": command["state"]})
                        con.execute("UPDATE agent_sessions SET updated_at=? WHERE session_id=?", (time.time(), work["parent_session_id"]))
            con.commit()

    def tasks(self, *, sid=None, aid=None, task_id=None):
        self.sync(sid)
        with closing(self._connect()) as con:
            rows = con.execute("SELECT * FROM assistant_work WHERE (? IS NULL OR parent_session_id=?) AND (? IS NULL OR assistant_id=?) AND (? IS NULL OR task_id=?) ORDER BY created_at DESC LIMIT 100",
                               (sid, sid, aid, aid, task_id, task_id)).fetchall()
            projected = []
            for work in rows:
                command = con.execute("SELECT * FROM agent_commands WHERE session_id=? AND kind!='guide' ORDER BY created_at DESC,position DESC LIMIT 1", (work["task_session_id"],)).fetchone()
                result = con.execute("SELECT * FROM assistant_results WHERE command_id=?", (command["command_id"],)).fetchone() if command else None
                state = command["state"] if command else "queued"
                projected.append({**dict(work), "kind": "background", "state": "cancelled" if state in {"interrupted", "removed"} else state,
                    "command_id": command["command_id"] if command else work["command_id"], "command_revision": command["revision"] if command else None,
                    "output": {"output": result["content"]} if result else None,
                    "error": ((json.loads(command["error_json"] or "null") or {}).get("message") or (json.loads(command["error_json"] or "null") or {}).get("code")) if command else None})
            for item in projected:
                for key in ("fingerprint", "request_json", "actor_id"):
                    item.pop(key, None)
            return projected

    def results(self, aid):
        self.sync()
        with closing(self._connect()) as con:
            rows = con.execute("""SELECT r.command_id,r.state,r.content,r.summary,r.created_at,
                w.task_id,w.title,w.task_kind,w.task_session_id,w.parent_session_id,w.assistant_id
                FROM assistant_results r JOIN assistant_work w USING(task_id)
                WHERE w.assistant_id=? ORDER BY r.created_at DESC LIMIT 100""", (aid,)).fetchall()
        return [{key: value for key, value in dict(row).items() if key != "content"} for row in rows]

    def context(self, sid):
        rows = [row for row in self.results(self.session(sid).get("assistant_id", DEFAULT_ID)) if row["parent_session_id"] == sid][:5]
        return json.dumps([{key: row[key] for key in ("title", "state", "summary", "task_session_id")} for row in rows], ensure_ascii=False)

    def stop(self, config, tid, sid):
        from .command_runtime import runtime
        task = next(iter(self.tasks(task_id=tid)), None)
        if not task or sid not in {task["parent_session_id"], task["task_session_id"]}:
            raise AssistantError("assistant_task_owner_mismatch", 403)
        manager = runtime(config)
        from .command_store import CommandError
        try:
            manager.control({"session_id": task["task_session_id"], "action": "remove" if task["state"] == "queued" else "stop", "command_id": task["command_id"], "expected_revision": task["command_revision"]})
        except CommandError as exc:
            if exc.code == "command_not_running": return False
            raise AssistantError(exc.code, exc.status) from exc
        return True

    def recover(self, manager):
        """Start only the admitted initial command; never replay uncertain work."""
        with closing(self._connect()) as con:
            rows = con.execute("SELECT * FROM assistant_work").fetchall()
        for work in rows:
            with manager.store.transaction() as con:
                command = con.execute("SELECT state FROM agent_commands WHERE command_id=?", (work["command_id"],)).fetchone()
                queue = manager.store._queue(con, work["task_session_id"])
                if command and command[0] != "queued": continue
                if queue["pause_reason"] == "operator" or queue["worker_owner"]: continue
                foreign = con.execute("SELECT 1 FROM agent_commands WHERE session_id=? AND command_id!=? AND state IN ('queued','running','stopping','unconfirmed')", (work["task_session_id"], work["command_id"])).fetchone()
                if foreign or manager.store._decision_pending(con, work["task_session_id"]): continue
                if queue["pause_reason"] == "restarted":
                    con.execute("UPDATE agent_command_queues SET paused=0,pause_reason='',revision=revision+1 WHERE session_id=?", (work["task_session_id"],))
                elif queue["paused"]: continue
            if not command:
                manager.submit({"session_id": work["task_session_id"], "command_id": work["command_id"], "request": json.loads(work["request_json"]), "_auth_actor_id": work["actor_id"]}, start=False)
            manager.kick(work["task_session_id"])
        self.sync()
