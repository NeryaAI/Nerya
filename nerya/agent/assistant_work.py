"""Assistant delegation: separate command-backed conversations and durable results."""
from __future__ import annotations

import hashlib
import json
import time
from contextlib import closing

from .assistants import AssistantError, AssistantStore, DEFAULT_ID
from .session import SessionStore
from ..db.repositories import AgentSessionRepository
from .assistant_coordination import delivery_projection, unresolved_deliveries, reported_result
from .task_control import assignment_for, coordination_instruction, create_mandate, ensure_schema, input_receipt, public_control, role_plan_for

TERMINAL = {"succeeded", "failed", "interrupted", "removed", "blocked"}


class AssistantWork:
    def __init__(self, paths):
        self.paths = paths
        ensure_schema(paths)
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
                CREATE TABLE IF NOT EXISTS assistant_task_messages (
                    message_id TEXT PRIMARY KEY, parent_session_id TEXT NOT NULL,
                    sender_session_id TEXT NOT NULL, target_task_id TEXT NOT NULL,
                    target_session_id TEXT NOT NULL, message TEXT NOT NULL,
                    command_id TEXT NOT NULL, kind TEXT NOT NULL, state TEXT NOT NULL,
                    created_at REAL NOT NULL);
                CREATE TABLE IF NOT EXISTS assistant_task_access (
                    requester_session_id TEXT NOT NULL, task_id TEXT NOT NULL,
                    actor_id TEXT NOT NULL, action TEXT NOT NULL, created_at REAL NOT NULL);
            """)

    def _connect(self):
        return AssistantStore(self.paths)._connect()

    def session(self, sid):
        with closing(self._connect()) as con:
            row = AgentSessionRepository(con).get_session(sid)
        return json.loads(row["meta_json"] or "{}") if row else {}

    @staticmethod
    def _submit_initial(manager, work):
        # This record is producer-owned. submit deliberately ignores nested auth
        # claims, so promote only the frozen snapshot, never current/tool grants.
        request = json.loads(work["request_json"])
        return manager.submit({
            "session_id": work["task_session_id"], "command_id": work["command_id"],
            "request": request, "_auth_actor_id": work["actor_id"],
            "_auth_scope": request.get("_auth_scope", ""),
            "_auth_scopes": request.get("_auth_scopes", []),
        }, start=False)

    def create(self, config, call, sid, actor, *, manager=None):
        from .command_runtime import runtime
        from .command_store import CommandError
        from ..memory.scope import memory_actor
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
        prompt = prompt if config.get("runtime.assistant_request") is not None else prompt.strip()
        fingerprint = json.dumps([title.strip(), prompt, kind, actor], ensure_ascii=False)
        manager = manager or runtime(config)
        with closing(self._connect()) as con:
            if con.execute("SELECT 1 FROM agent_deleted_sessions WHERE session_id=?", (sid,)).fetchone():
                raise AssistantError("session_deleted", 409)
            parent_id = config.get("runtime.command_id")
            bound_session = config.get("runtime.command_session_id")
            bound_turn = config.get("runtime.command_turn_id")
            bound = any((parent_id, bound_session, bound_turn))
            if bound and (not parent_id or bound_session != sid or bound_turn != call.turn_id):
                raise AssistantError("assistant_task_parent_mismatch", 403)
            parent = con.execute("SELECT * FROM agent_commands WHERE session_id=? AND turn_id=? AND kind!='guide' AND (? IS NULL OR command_id=?) ORDER BY created_at DESC,position DESC LIMIT 1", (sid, call.turn_id, parent_id, parent_id)).fetchone()
            if bound and not parent:
                raise AssistantError("assistant_task_parent_mismatch", 403)
            parent_request = json.loads(parent["request_json"]) if parent else {}
            parent_context = json.loads(parent["context_json"]) if parent else {}
            if parent and memory_actor(parent["actor_id"]) != memory_actor(actor):
                raise AssistantError("assistant_task_owner_mismatch", 403)
            if parent_request.get("work_mode") == "plan":
                raise AssistantError("assistant_task_plan_only")
            request_binding = None
            if config.get("runtime.assistant_request") is not None:
                from .assistant_receipts import assistant_request_binding
                request_binding = assistant_request_binding(config, con, operation="task_create")
                if (request_binding["session_id"] != sid or request_binding["turn_id"] != call.turn_id
                        or call.id != "assistant_request_" + request_binding["command_id"]
                        or memory_actor(request_binding["actor_id"]) != memory_actor(actor)):
                    raise AssistantError("assistant_request_binding_mismatch", 403)
                if prompt != (parent_request.get("payload") or {}).get("text"):
                    raise AssistantError("assistant_request_input_mismatch", 403)
            prior = con.execute("SELECT * FROM assistant_work WHERE task_id=?", (tid,)).fetchone()
            if prior and prior["fingerprint"] != fingerprint:
                raise AssistantError("assistant_task_idempotency_conflict", 409)
            if prior and json.loads(prior["request_json"]).get("assistant_request_binding") != request_binding:
                raise AssistantError("assistant_request_binding_mismatch", 403)
            auth = {"_auth_scope": "", "_auth_scopes": []}
            if bound and not prior:
                if parent["state"] != "running":
                    raise AssistantError("assistant_task_parent_inactive", 403)
                authenticated_actor = parent_request.get("_auth_actor_id")
                if (authenticated_actor and authenticated_actor != parent["actor_id"]
                        or config.get("runtime.financial_actor_id") not in (None, parent["actor_id"])):
                    raise AssistantError("assistant_task_owner_mismatch", 403)
                # Kernel actor aliases are for memory ownership. Keep the raw
                # authenticated principal for financial command ownership.
                actor = parent["actor_id"]
                scopes = parent_request.get("_auth_scopes")
                if authenticated_actor and isinstance(scopes, list) and all(isinstance(s, str) for s in scopes):
                    auth = {"_auth_scope": parent_request.get("_auth_scope", ""), "_auth_scopes": scopes}
            if not prior:
                count = con.execute("""SELECT COUNT(*) FROM assistant_work w WHERE w.parent_session_id=?
                    AND EXISTS (SELECT 1 FROM agent_commands c WHERE c.session_id=w.task_session_id AND c.kind!='guide'
                    AND (c.state IN ('queued','running','stopping','unconfirmed') OR (c.state IN ('awaiting_input','awaiting_approval')
                    AND c.command_id=(SELECT command_id FROM agent_commands WHERE session_id=w.task_session_id AND kind!='guide' ORDER BY created_at DESC,position DESC LIMIT 1))))""", (sid,)).fetchone()[0]
                if count >= 8:
                    raise AssistantError("assistant_task_limit", 409)
        assistants.bind(child, profile["id"], source="assistant_task", title=title.strip())
        meta = {"conversation_kind": "task", "parent_session_id": sid, "assistant_task_id": tid, "task_kind": kind,
                "title": title.strip(), "title_source": "operator"}
        with closing(self._connect()) as con:
            AgentSessionRepository(con).upsert_session(session_id=child, title=title.strip(), source="assistant_task", meta=meta)
            request = {"source": "assistant_task", "assistant_id": profile["id"], "payload": {"text": prompt + coordination_instruction(kind)},
                       "permission_mode": config.get("runtime.permission_mode", "default"), **auth}
            if request_binding is not None:
                request["assistant_request_binding"] = request_binding
                # submit's allowlist retains payload, including on recovery.
                request["payload"]["assistant_request_binding"] = request_binding
            for field in ("permission_mode", "work_mode", "goal_token_budget", "reasoning_effort", "reasoning_summary", "max_iterations", "max_total_tool_calls", "max_wall_seconds", "model_tier"):
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
            receipt = self._submit_initial(manager, saved)
        except CommandError as exc:
            raise AssistantError(exc.code, exc.status) from exc
        role_plan = role_plan_for(kind)
        create_mandate(self.paths, task_id=tid, command_id=cid, session_id=child, actor_id=actor,
                       task_kind=kind, objective=prompt, scope={"paper_only": True, "live_trading": False, "role_plan": role_plan},
                       allowed_tools=["research", "strategy_author", "backtest", "strategy_validate", "team_run"],
                       execution_mode="paper")
        assignment = assignment_for(self.paths, task_id=tid, session_id=child, command_id=cid)
        input_receipt(self.paths, task_id=tid, assignment_id=assignment["assignment_id"], command_id=cid,
                      sender_session_id=sid, body=prompt, state="delivered")
        manager.kick(child)
        return {"ok": True, "protocol": "nerya.assistant_task.v2", "task_id": tid,
                "title": title.strip(), "assistant_id": profile["id"], "session_id": sid,
                "parent_session_id": sid, "task_session_id": child, "command_id": cid, "state": receipt["command"]["state"]}

    def sync(self, sid=None):
        """Repair missing completion delivery without replaying task execution."""
        with closing(self._connect()) as con:
            con.execute("BEGIN IMMEDIATE")
            rows = con.execute("SELECT * FROM assistant_work" + (" WHERE task_session_id=? OR parent_session_id=?" if sid else ""),
                               (sid, sid) if sid else ()).fetchall()
            repo = AgentSessionRepository(con)
            for work in rows:
                commands = con.execute("""SELECT c.* FROM agent_commands c WHERE c.session_id=? AND c.kind!='guide'
                    AND c.state IN ('succeeded','failed','interrupted','removed','blocked')
                    AND NOT EXISTS (SELECT 1 FROM assistant_results r WHERE r.command_id=c.command_id)
                    ORDER BY c.created_at,c.position""", (work["task_session_id"],)).fetchall()
                for command in commands:
                    result = json.loads(command["result_json"] or "{}")
                    if not result:
                        answer = con.execute("SELECT meta_json FROM agent_messages WHERE session_id=? AND message_id=?", (work["task_session_id"], command["turn_id"]+":assistant")).fetchone()
                        result = (json.loads(answer[0] or "{}").get("turn") or {}) if answer else {}
                    # 用户正文只来自 agent；状态与跳转交给 UI 的结构化回执和 i18n。
                    content = result.get("reply_text") or result.get("final_text") or ""
                    content = content if isinstance(content, str) else ""
                    publication = delivery_projection(con, work, command)
                    publication['reported_result'] = reported_result(command, content)
                    result = {**result, **publication}
                    summary = content[:1200] + ("…" if len(content) > 1200 else "")
                    con.execute("INSERT INTO assistant_results VALUES (?,?,?,?,?,?,?)", (command["command_id"], work["task_id"],
                                command["state"], content, summary, json.dumps(result, ensure_ascii=False), time.time()))
                    deleted = con.execute("SELECT 1 FROM agent_deleted_sessions WHERE session_id=?", (work["parent_session_id"],)).fetchone()
                    if not deleted:
                        repo.record_message(message_id="assistant_result:" + command["command_id"], session_id=work["parent_session_id"],
                                            role="assistant", content=content, meta={"source": "assistant_task_result",
                                            "task_title": work["title"], "assistant_task_id": work["task_id"],
                                            "task_session_id": work["task_session_id"], "execution_status": command["state"],
                                            **publication})
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
                if con.execute("SELECT 1 FROM agent_deleted_sessions WHERE session_id=?",(work["task_session_id"],)).fetchone(): continue
                active = con.execute("SELECT * FROM agent_commands WHERE session_id=? AND state IN ('running','stopping','unconfirmed') AND kind!='guide' ORDER BY created_at DESC LIMIT 1",(work["task_session_id"],)).fetchone()
                if active: command=active
                result = con.execute("SELECT * FROM assistant_results WHERE command_id=?", (command["command_id"],)).fetchone() if command else None
                state = command["state"] if command else "queued"
                projected.append({**dict(work), **delivery_projection(con, work, command),
                    "control": public_control(self.paths, work["task_id"]),
                    "reported_result": reported_result(command, result['content']) if result else None,
                    "kind": "background", "state": "cancelled" if state in {"interrupted", "removed"} else state,
                    "command_id": command["command_id"] if command else work["command_id"], "command_revision": command["revision"] if command else None,
                    "output": {"output": result["content"]} if result else None,
                    "error": ((json.loads(command["error_json"] or "null") or {}).get("message") or (json.loads(command["error_json"] or "null") or {}).get("code")) if command else None})
            for item in projected:
                for key in ("fingerprint", "request_json", "actor_id"):
                    item.pop(key, None)
            return projected

    def results(self, aid):
        from .assistant_coordination import _delivery
        from ..task_delivery import public_delivery

        self.sync()
        with closing(self._connect()) as con:
            rows = con.execute("""SELECT r.command_id,r.state,r.content,r.summary,r.created_at,
                w.task_id,w.title,w.task_kind,w.task_session_id,w.parent_session_id,w.assistant_id
                FROM assistant_results r JOIN assistant_work w USING(task_id)
                WHERE w.assistant_id=? ORDER BY r.created_at DESC LIMIT 100""", (aid,)).fetchall()
            projected = []
            for row in rows:
                work = con.execute('SELECT * FROM assistant_work WHERE task_id=?', (row['task_id'],)).fetchone()
                command = con.execute('SELECT * FROM agent_commands WHERE command_id=?', (row['command_id'],)).fetchone()
                projected.append({**{key: value for key, value in dict(row).items() if key != 'content'},
                    **delivery_projection(con, work, command),
                    # Keep this result's delivery distinct from the through-command
                    # task rollup; a later successful turn cannot relabel this report.
                    'delivery': public_delivery(_delivery(command)) if command is not None
                        and command['session_id'] == work['task_session_id']
                        and command['actor_id'] == work['actor_id'] else None,
                    'reported_result': reported_result(command, row['content']) if command else None})
            return projected

    def context(self, sid):
        from .assistant_coordination import coordination_context
        return coordination_context(self.paths, sid)

    def _visible_scope(self, con, requester_session_id, actor, task_id=""):
        from ..memory.scope import memory_actor
        profile = AssistantStore(self.paths).for_session(requester_session_id)
        if not profile or not actor:
            raise AssistantError("assistant_task_owner_mismatch", 403)
        source = con.execute("SELECT * FROM assistant_work WHERE task_session_id=?", (requester_session_id,)).fetchone()
        parent = source["parent_session_id"] if source else requester_session_id
        if source and (source["assistant_id"] != profile["id"] or memory_actor(source["actor_id"]) != memory_actor(actor)):
            raise AssistantError("assistant_task_owner_mismatch", 403)
        if con.execute("SELECT 1 FROM agent_deleted_sessions WHERE session_id IN (?,?)", (requester_session_id, parent)).fetchone():
            raise AssistantError("session_deleted", 410)
        target = con.execute("SELECT * FROM assistant_work WHERE task_id=?", (task_id,)).fetchone() if task_id else None
        if task_id:
            if (not target or target["parent_session_id"] != parent or target["assistant_id"] != profile["id"]
                    or memory_actor(target["actor_id"]) != memory_actor(actor)):
                raise AssistantError("assistant_task_owner_mismatch", 403)
            if con.execute("SELECT 1 FROM agent_deleted_sessions WHERE session_id=?", (target["task_session_id"],)).fetchone():
                raise AssistantError("session_deleted", 410)
        return target, parent, profile

    def visible_tasks(self, requester_session_id, actor):
        with closing(self._connect()) as con:
            _, parent, profile = self._visible_scope(con, requester_session_id, actor)
        from ..memory.scope import memory_actor
        with closing(self._connect()) as con:
            owned = {r["task_id"] for r in con.execute("SELECT task_id,actor_id FROM assistant_work WHERE parent_session_id=? AND assistant_id=?", (parent, profile["id"]))
                     if memory_actor(r["actor_id"]) == memory_actor(actor)}
            con.execute("INSERT INTO assistant_task_access VALUES (?,?,?,?,?)",(requester_session_id,"*",actor,"list",time.time()))
        return [{k: row.get(k) for k in ("task_id", "title", "state", "task_kind", "task_session_id", "command_id", "command_revision",
                    "task_complete", "delivery_completion", "unresolved_deliveries", "publication", "control")}
                for row in self.tasks(sid=parent) if row["task_id"] in owned]

    def read_task(self, requester_session_id, task_id, actor, *, offset=0, limit=8000, command_id=None):
        if type(offset) is not int or offset < 0 or type(limit) is not int or not 1 <= limit <= 16000:
            raise AssistantError("invalid_task_read_range")
        with closing(self._connect()) as con:
            work, _, _ = self._visible_scope(con, requester_session_id, actor, task_id)
            child = work["task_session_id"]
        self.sync(child)
        from ..core.redaction import redact_text
        with closing(self._connect()) as con:
            work, _, _ = self._visible_scope(con, requester_session_id, actor, task_id)
            command = con.execute("SELECT * FROM agent_commands WHERE session_id=? AND kind!='guide' ORDER BY created_at DESC,position DESC LIMIT 1", (child,)).fetchone()
            active = con.execute("SELECT * FROM agent_commands WHERE session_id=? AND state IN ('running','stopping','unconfirmed') AND kind!='guide' ORDER BY created_at DESC LIMIT 1",(child,)).fetchone()
            if active: command=active
            if command_id is not None:
                command = con.execute("SELECT * FROM agent_commands WHERE command_id=? AND session_id=? AND actor_id=? AND kind!='guide'",
                                      (command_id, child, work['actor_id'])).fetchone()
                if command is None:
                    raise AssistantError('assistant_task_command_not_found', 404)
            result = (con.execute("SELECT * FROM assistant_results WHERE task_id=? AND command_id=?",
                                  (task_id, command_id)).fetchone() if command_id is not None else
                      con.execute("SELECT * FROM assistant_results WHERE task_id=? ORDER BY created_at DESC LIMIT 1", (task_id,)).fetchone())
            from .assistant_coordination import command_verification, task_command_projection
            result_command = con.execute("SELECT * FROM agent_commands WHERE command_id=? AND session_id=?",
                                         (result["command_id"], child)).fetchone() if result else None
            content = redact_text(str(result["content"] if result else ""))
            # Public answer only: no provider prompt, hidden reasoning or credentials.
            data = {**delivery_projection(con, work, command),
                    "control": public_control(self.paths, task_id),
                    **task_command_projection(con, work, command),
                    "reported_result": reported_result(result_command, content) if result_command else None,
                    "task_id":task_id, "title":work["title"], "task_session_id":child,
                    "state":command["state"] if command else "unconfirmed", "command_id":command["command_id"] if command else None,
                    "admission_confirmed": command is not None,
                    "verification": command_verification(command),
                    "result_verification": command_verification(result_command) if result else None,
                    "content_authority": "untrusted_model_output",
                    "result_command_id":result["command_id"] if result else None,
                    "result_state":result["state"] if result else None, "content":content[offset:offset+limit],
                    "next_offset":offset+limit if offset+limit<len(content) else None,
                    "content_truncated":offset+limit<len(content), "total_chars":len(content),
                    "error_code":(json.loads(command["error_json"] or "null") or {}).get("code") if command else None}
            con.execute("INSERT INTO assistant_task_access VALUES (?,?,?,?,?)", (requester_session_id,task_id,actor,"read",time.time()))
            return data

    def message_task(self, config, requester_session_id, task_id, content, actor, *, request_id, turn_id, mode="follow_up", manager=None):
        from .command_runtime import runtime
        from .command_store import input_context, validate_command_request
        from ..memory.scope import memory_actor
        import re
        if (not isinstance(content,str) or not content.strip() or len(content)>12000
                or not isinstance(request_id,str) or not re.fullmatch(r"[A-Za-z0-9_-]{8,128}", request_id)
                or mode not in {"follow_up","guide"}):
            raise AssistantError("invalid_assistant_task_message")
        content = content if config.get("runtime.assistant_request") is not None else content.strip()
        manager = manager or runtime(config)
        digest = hashlib.sha256(json.dumps([requester_session_id,task_id,request_id]).encode()).hexdigest()[:32]
        cid = "atmsg_" + digest
        # Admission and message audit share a transaction; no untracked send window.
        with manager.store.transaction() as con:
            work, parent, profile = self._visible_scope(con, requester_session_id, actor, task_id)
            child = work["task_session_id"]
            if child == requester_session_id:
                raise AssistantError("assistant_task_self_message")
            if mode == "guide" and requester_session_id != parent:
                raise AssistantError("assistant_task_peer_guide_denied",403)
            request_binding = None
            if config.get("runtime.assistant_request") is not None:
                from .assistant_receipts import assistant_request_binding
                request_binding = assistant_request_binding(config, con, operation="task_followup")
                contract = config.get("runtime.assistant_request")
                if (request_binding["session_id"] != requester_session_id
                        or request_binding["turn_id"] != turn_id
                        or memory_actor(request_binding["actor_id"]) != memory_actor(actor)
                        or contract.get("target_task_id") != task_id
                        or request_id != "assistant_followup_" + request_binding["command_id"]
                        or mode != "follow_up"):
                    raise AssistantError("assistant_request_binding_mismatch", 403)
            prior = con.execute("SELECT * FROM assistant_task_messages WHERE message_id=?", (request_id+":"+digest,)).fetchone()
            if prior:
                if prior["message"] != content or prior["kind"] != mode:
                    raise AssistantError("assistant_task_message_conflict",409)
                command = con.execute("SELECT * FROM agent_commands WHERE command_id=?", (prior["command_id"],)).fetchone()
                if not command: raise AssistantError("assistant_task_receipt_missing",409)
                if (command['session_id'] != child or command['actor_id'] != work['actor_id']
                        or prior['target_task_id'] != task_id or prior['target_session_id'] != child
                        or prior['sender_session_id'] != requester_session_id or prior['parent_session_id'] != parent):
                    raise AssistantError('assistant_task_receipt_mismatch', 403)
                if json.loads(command["request_json"]).get("assistant_request_binding") != request_binding:
                    raise AssistantError("assistant_request_binding_mismatch", 403)
                return {"ok":True,"duplicate":True,"task_id":task_id,"task_session_id":child,"command_id":command["command_id"],"state":command["state"],"delivery":mode}
            source = con.execute("SELECT * FROM agent_commands WHERE session_id=? AND turn_id=? AND kind!='guide' ORDER BY created_at DESC LIMIT 1", (requester_session_id,turn_id)).fetchone()
            if not source or source["state"] != "running" or memory_actor(source["actor_id"]) != memory_actor(actor):
                raise AssistantError("assistant_task_sender_inactive",409)
            if request_binding is not None and source["command_id"] != request_binding["command_id"]:
                raise AssistantError("assistant_request_binding_mismatch", 403)
            source_request = json.loads(source["request_json"])
            if (source_request.get("source") == "assistant_task_message"
                    and source_request.get("assistant_request_binding")
                    and content.strip() == str((source_request.get("payload") or {}).get("text") or "").strip()):
                raise AssistantError("assistant_task_instruction_already_delivered", 409)
            if request_binding is not None and content != (source_request.get("payload") or {}).get("text"):
                raise AssistantError("assistant_request_input_mismatch", 403)
            if source_request.get("work_mode") == "plan":
                raise AssistantError("assistant_task_plan_only")
            source_message = (source_request.get("payload") or {}).get("task_message") or {}
            if source_request.get("source") == "assistant_task_message" and source_message.get("sender_session_id") != parent:
                raise AssistantError("assistant_task_message_relay_blocked",409)
            queue = manager.store._queue(con,child)
            if manager.store._decision_pending(con,child):
                raise AssistantError("assistant_task_decision_pending",409)
            if con.execute("SELECT 1 FROM agent_commands WHERE session_id=? AND state='unconfirmed'",(child,)).fetchone():
                raise AssistantError("execution_unconfirmed",409)
            current = con.execute("SELECT * FROM agent_commands WHERE session_id=? AND kind!='guide' ORDER BY created_at DESC,position DESC LIMIT 1",(child,)).fetchone()
            run_only = False
            if queue["paused"]:
                # A bound parent request authorizes one new send, not recovery
                # of an old turn or release of the destination's held queue.
                run_only = (request_binding is not None and requester_session_id == parent and mode == "follow_up"
                    and queue["pause_reason"] in {"restarted", "blocked", "failed"} and not queue["worker_owner"]
                    and current is not None and current["state"] in {"succeeded", "blocked", "failed"}
                    and not json.loads(current["context_json"]).get("stop_requested")
                    and not con.execute("SELECT 1 FROM agent_commands WHERE session_id=? AND state IN "
                        "('queued','running','stopping','delivering','delivered','unconfirmed') LIMIT 1", (child,)).fetchone())
                if not run_only:
                    raise AssistantError("assistant_task_paused",409)
            # Keep the destination's own settings and permission ceiling. A peer
            # never changes its model, bypasses a decision or releases its queue.
            previous = json.loads(current["request_json"]) if current else json.loads(work["request_json"])
            source_scopes, ceiling = source_request.get("_auth_scopes", []), previous.get("_auth_scopes", [])
            source_scopes = source_scopes if isinstance(source_scopes, list) and all(isinstance(s, str) for s in source_scopes) else []
            ceiling = ceiling if isinstance(ceiling, list) and all(isinstance(s, str) for s in ceiling) else []
            scopes = ceiling if "api:all" in source_scopes else [s for s in source_scopes if s in ceiling or "api:all" in ceiling]
            request = {key:previous[key] for key in ("model_provider","model_id","model_tier","reasoning_effort","reasoning_summary","max_iterations","max_total_tool_calls","max_wall_seconds","permission_mode") if key in previous}
            request.update(source="assistant_task_message",kind="task.follow_up",assistant_id=profile["id"],conversation_kind="task",session_id=child,target="main",work_mode="execute",
                payload={"text":content,"task_message":{"sender_session_id":requester_session_id,"target_task_id":task_id,"request_id":request_id}},
                _auth_actor_id=work["actor_id"],_auth_scopes=list(dict.fromkeys(scopes)))
            if request_binding is not None:
                request["assistant_request_binding"] = request_binding
                request["payload"]["assistant_request_binding"] = request_binding
            if requester_session_id != parent:
                request["permission_mode"] = "default"
            if run_only:
                request["run_only"] = True
            kind = "guide" if mode == "guide" else "send"
            validate_command_request(kind,request)
            identity_request = dict(request)
            # The destination is already bound and checked above; calling
            # bind again here would acquire a second SQLite writer transaction.
            context = {"version":1,**input_context(request),"assistant_id":profile["id"],"task_message":request["payload"]["task_message"]}
            if mode == 'follow_up':
                # Capture under the admission writer transaction. Never inherit old context
                # references or producer checks; retries return the frozen admission above.
                unresolved = unresolved_deliveries(con, work)
                context['unresolved_deliveries'] = unresolved[:16]
                context['unresolved_deliveries_truncated'] = len(unresolved) > 16
                context['publication_language'] = (config.get('runtime.assistant_request') or {}).get('language', 'und')
            receipt = manager.store.accept(cid=cid,sid=child,actor=work["actor_id"],kind=kind,request=request,context=context,
                turn_id="turn_"+hashlib.sha256(cid.encode()).hexdigest()[:32],identity_request=identity_request,connection=con)
            con.execute("INSERT INTO assistant_task_messages VALUES (?,?,?,?,?,?,?,?,?,?)",(request_id+":"+digest,parent,requester_session_id,task_id,child,content,cid,mode,receipt["command"]["state"],time.time()))
        assignment = assignment_for(self.paths, task_id=task_id, session_id=child, command_id=cid, attempt=2)
        input_receipt(self.paths, task_id=task_id, assignment_id=assignment["assignment_id"], command_id=cid,
                      sender_session_id=requester_session_id, body=content, state="delivered")
        manager.kick(child)
        return {"ok":True,"duplicate":False,"task_id":task_id,"task_session_id":child,"command_id":cid,"state":receipt["command"]["state"],"delivery":mode}

    def enqueue_trigger(self, config, task_id: str, event: dict, *, manager=None):
        """Admit one idempotent Trigger into an owned task's queue.

        Trigger input inherits the task's frozen actor and tool ceiling. It
        cannot approve a decision, widen the mandate, or execute live funds.
        """
        from .command_runtime import runtime
        from .command_store import input_context, validate_command_request
        import hashlib

        with closing(self._connect()) as con:
            work = con.execute("SELECT * FROM assistant_work WHERE task_id=?", (task_id,)).fetchone()
            if not work:
                raise AssistantError("assistant_task_not_found", 404)
            current = con.execute("SELECT * FROM agent_commands WHERE session_id=? AND kind!='guide' ORDER BY created_at DESC,position DESC LIMIT 1",
                                  (work["task_session_id"],)).fetchone()
            if current is not None and current["state"] in {"awaiting_input", "awaiting_approval", "unconfirmed", "running", "stopping"}:
                raise AssistantError("assistant_task_not_ready", 409)
            previous = json.loads(current["request_json"]) if current else json.loads(work["request_json"])
        event_id = str(event.get("event_id") or "")
        if not event_id:
            raise AssistantError("task_trigger_event_id_required", 400)
        text = "Trigger event " + event_id + "\n" + json.dumps(event.get("payload") or {}, ensure_ascii=False, sort_keys=True)
        cid = "tcmd_" + hashlib.sha256((task_id + ":" + event_id).encode()).hexdigest()[:40]
        request = {key: previous[key] for key in ("model_provider", "model_id", "model_tier", "reasoning_effort", "reasoning_summary",
                                                    "max_iterations", "max_total_tool_calls", "max_wall_seconds", "permission_mode") if key in previous}
        request.update(source="task_trigger", kind="task.trigger", assistant_id=work["assistant_id"], conversation_kind="task",
                       session_id=work["task_session_id"], target="main", work_mode="execute",
                       payload={"text": text, "task_trigger": {"event_id": event_id, "source": event.get("source"), "kind": event.get("kind")}},
                       _auth_actor_id=work["actor_id"], _auth_scope=previous.get("_auth_scope", ""),
                       _auth_scopes=previous.get("_auth_scopes", []), run_only=True)
        validate_command_request("send", request)
        manager = manager or runtime(config)
        with manager.store.transaction() as con:
            existing = con.execute("SELECT * FROM agent_commands WHERE command_id=?", (cid,)).fetchone()
            if existing is not None:
                assignment = assignment_for(self.paths, cid)
                with closing(self._connect()) as read_con:
                    input_row = read_con.execute("SELECT * FROM task_inputs WHERE command_id=? ORDER BY delivered_at DESC LIMIT 1", (cid,)).fetchone()
                return {"ok": True, "duplicate": True, "task_id": task_id, "task_session_id": work["task_session_id"],
                        "command_id": cid, "state": existing["state"], "input_id": input_row["input_id"] if input_row else ""}
            context = {"version": 1, **input_context(request), "task_trigger": event, "assistant_id": work["assistant_id"]}
            receipt = manager.store.accept(cid=cid, sid=work["task_session_id"], actor=work["actor_id"], kind="send",
                                          request=request, context=context, turn_id="turn_" + hashlib.sha256(cid.encode()).hexdigest()[:32],
                                          identity_request=request, connection=con)
        assignment = assignment_for(self.paths, task_id=task_id, session_id=work["task_session_id"], command_id=cid, attempt=2)
        input_row = input_receipt(self.paths, task_id=task_id, assignment_id=assignment["assignment_id"], command_id=cid,
                                  sender_session_id="trigger:" + str(event.get("source") or "unknown"), body=text, state="delivered")
        manager.kick(work["task_session_id"])
        return {"ok": True, "duplicate": False, "task_id": task_id, "task_session_id": work["task_session_id"],
                "command_id": cid, "state": receipt["command"]["state"], "input_id": input_row["input_id"]}

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
            rows = con.execute("""SELECT w.* FROM assistant_work w
                WHERE NOT EXISTS (SELECT 1 FROM agent_deleted_sessions d
                    WHERE d.session_id IN (w.task_session_id,w.parent_session_id))""").fetchall()
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
                self._submit_initial(manager, work)
            manager.kick(work["task_session_id"])
        # Recover admitted, never-started collaboration commands as well. A
        # restart must not discard them, release an operator pause or replay
        # an operation whose outcome is unknown.
        with closing(self._connect()) as con:
            pending = con.execute("""SELECT DISTINCT m.target_session_id FROM assistant_task_messages m
                JOIN agent_commands c ON c.command_id=m.command_id WHERE c.state='queued'
                AND NOT EXISTS (SELECT 1 FROM agent_deleted_sessions d WHERE d.session_id=m.target_session_id)""").fetchall()
        for row in pending:
            sid = row[0]
            with manager.store.transaction() as con:
                if con.execute("SELECT 1 FROM agent_deleted_sessions WHERE session_id=?",(sid,)).fetchone(): continue
                queue=manager.store._queue(con,sid)
                if queue["worker_owner"] or queue["pause_reason"] == "operator": continue
                if manager.store._decision_pending(con,sid): continue
                if con.execute("SELECT 1 FROM agent_commands WHERE session_id=? AND state='unconfirmed'",(sid,)).fetchone(): continue
                foreign=con.execute("SELECT 1 FROM agent_commands c WHERE c.session_id=? AND c.state='queued' AND NOT EXISTS(SELECT 1 FROM assistant_task_messages m WHERE m.command_id=c.command_id)",(sid,)).fetchone()
                if queue["paused"] and (queue["pause_reason"] != "restarted" or foreign): continue
                if queue["pause_reason"] == "restarted":
                    con.execute("UPDATE agent_command_queues SET paused=0,pause_reason='',revision=revision+1 WHERE session_id=?",(sid,))
            manager.kick(sid)
        self.sync()
