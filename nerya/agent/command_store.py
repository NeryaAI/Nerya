"""Durable conversation commands. An ingress receipt is not a tool receipt.

SQLite owns admission, queue revisions and active command uniqueness. Expired
workers become unconfirmed; neither reads nor retries replay external effects.
"""
from __future__ import annotations

from contextlib import contextmanager
import copy
import hashlib
import json
import re
import time
from typing import Any

from ..db.sqlite import connect
from .history_mutations import _session_id, is_session_deleted

ACTIVE = ("running", "stopping")
PENDING = ("queued", "delivering", "delivered", "running", "stopping", "unconfirmed")
TERMINAL = ("succeeded", "failed", "interrupted", "awaiting_approval", "awaiting_input", "blocked", "not_consumed", "injected", "removed", "unconfirmed")
LEASE_SECONDS = 30
MAX_QUEUE = 32
DECISION_PAUSES = ("awaiting_approval", "awaiting_input", "approval_continued", "plan_accepted", "plan_revised")
EDIT_SETTINGS = frozenset({
    "work_mode", "permission_mode", "model_provider", "model_id", "model_tier",
    "model_context_window", "reasoning_effort", "reasoning_summary",
    "max_iterations", "max_total_tool_calls", "max_wall_seconds",
})


def queue_editable(request):
    return not (request.get("interaction_id") or request.get("plan_id") or
                request.get("source") in ("approval_continue", "interaction_continue") or
                request.get("kind") == "approval.continue")


def validate_command_request(kind, request):
    """Shared admission/edit validation; identity and decision fields stay immutable."""
    if type(request.get("run_only", False)) is not bool or (request.get("run_only") and kind != "send"):
        raise CommandError("invalid_command_request", 400)
    if request.get("work_mode", "execute") not in ("execute", "plan", "goal"):
        raise CommandError("invalid_work_mode", 400)
    message = request.setdefault("payload", {})
    if not isinstance(message, dict):
        raise CommandError("invalid_command_request", 400)
    text, attachments = message.get("text", ""), message.get("attachments", [])
    if not isinstance(text, str):
        raise CommandError("invalid_message", 400)
    if not isinstance(attachments, list) or len(attachments) > 8 or any(not isinstance(file, dict) for file in attachments):
        raise CommandError("invalid_attachments", 400)
    if kind == "guide" and (not text.strip() or attachments):
        raise CommandError("guide_text_only", 400)
    if kind != "resume" and not text.strip() and not attachments:
        raise CommandError("message_required", 400)
    if kind == "resume":
        from .loop_state import validate_turn_checkpoint_resume_request
        validate_turn_checkpoint_resume_request(resume_turn_id=request.get("resume_turn_id"),
            continuation_feedback=request.get("continuation_feedback"), session_id=request["session_id"],
            turn_id=None, has_attachments=bool(attachments))
    elif request.get("resume_turn_id"):
        raise CommandError("invalid_command_type", 400)
    for key in ("model_provider", "model_id", "model_tier"):
        if request.get(key) is not None and (not isinstance(request[key], str) or len(request[key]) > 160):
            raise CommandError("invalid_command_request", 400)
    for key, values in {
        "permission_mode": (None, "", "default", "auto", "yolo"),
        "reasoning_effort": (None, "", "inherit", "off", "none", "minimal", "low", "medium", "high", "xhigh", "max"),
        "reasoning_summary": (None, "", "auto", "concise", "detailed"),
    }.items():
        if request.get(key) not in values:
            raise CommandError("invalid_command_request", 400)
    for key in ("model_context_window", "max_iterations", "max_total_tool_calls", "max_wall_seconds"):
        value = request.get(key)
        if value is not None and (type(value) is not int or value < 0):
            raise CommandError("invalid_command_request", 400)
    if len(encode(request).encode()) > 256_000:
        raise CommandError("command_too_large", 413)


def input_context(request):
    message = request.get("payload", {})
    attachments = message.get("attachments", [])
    return {
        "captured_at": time.time(), "work_mode": request.get("work_mode", "execute"),
        "input_text": str(request.get("continuation_feedback") or message.get("text", "")),
        "requested_model": {key: request.get(key) for key in
                            ("model_provider", "model_id", "model_tier", "model_context_window", "reasoning_effort")},
        "run_settings": {key: request[key] for key in EDIT_SETTINGS if key in request},
        "references": [file["reference"] for file in attachments if file.get("reference")],
        "attachments": [{key: file.get(key) for key in ("id", "name", "artifact_uri", "reference")} for file in attachments],
    }


class CommandError(ValueError):
    def __init__(self, code: str, status: int = 409):
        self.code, self.status = code, status
        super().__init__(code)


def encode(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), default=str, allow_nan=False)


def command_id(value: Any) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_-]{8,160}", value):
        raise CommandError("invalid_command_id", 400)
    return value


def fingerprint(kind: str, request: dict) -> str:
    return hashlib.sha256(json.dumps([kind, request], sort_keys=True, ensure_ascii=False,
                                     separators=(",", ":"), allow_nan=False).encode()).hexdigest()


class CommandStore:
    def __init__(self, paths, epoch: str):
        self.paths, self.epoch = paths, epoch

    @contextmanager
    def transaction(self):
        con = connect(self.paths.db)
        try:
            con.execute("BEGIN IMMEDIATE")
            yield con
            con.commit()
        except BaseException:
            if con.in_transaction:
                con.rollback()
            raise
        finally:
            con.close()

    def _queue(self, con, sid: str):
        if is_session_deleted(con, sid):
            raise CommandError("session_deleted", 410)
        now = time.time()
        con.execute("INSERT OR IGNORE INTO agent_command_queues(session_id,runtime_epoch,updated_at) VALUES (?,?,?)",
                    (sid, self.epoch, now))
        queue = dict(con.execute("SELECT * FROM agent_command_queues WHERE session_id=?", (sid,)).fetchone())
        expired = bool(queue["worker_owner"] and queue["lease_until"] < now)
        restarted = queue["runtime_epoch"] != self.epoch and not queue["worker_owner"]
        if expired:
            con.execute("UPDATE agent_commands SET state='unconfirmed',revision=revision+1,updated_at=? "
                        "WHERE session_id=? AND state IN ('running','stopping')", (now, sid))
            con.execute("UPDATE agent_commands SET state='not_consumed',revision=revision+1,updated_at=? "
                        "WHERE session_id=? AND kind='guide' AND state IN ('queued','delivering','delivered')", (now, sid))
        if expired or restarted:
            pending = con.execute("SELECT 1 FROM agent_commands WHERE session_id=? AND state IN ('queued','unconfirmed') LIMIT 1", (sid,)).fetchone()
            paused = bool(pending or queue["paused"])
            reason = ("operator" if queue["pause_reason"] == "operator" else "restarted") if paused else ""
            con.execute("UPDATE agent_command_queues SET runtime_epoch=?,worker_owner='',lease_until=0,"
                        "paused=?,pause_reason=?,revision=revision+1,updated_at=? WHERE session_id=?",
                        (self.epoch, int(paused), reason, now, sid))
            queue = dict(con.execute("SELECT * FROM agent_command_queues WHERE session_id=?", (sid,)).fetchone())
        return queue

    @staticmethod
    def _decision_pending(con, sid):
        if con.execute("SELECT 1 FROM agent_interactions WHERE session_id=? AND state IN ('pending','deferred','answered') LIMIT 1",(sid,)).fetchone():
            return "awaiting_input"
        # Pause reasons are presentation; a new pause click must not erase the
        # authoritative pending decision of the latest execution.
        last=con.execute("SELECT state FROM agent_commands WHERE session_id=? AND kind!='guide' AND state NOT IN ('queued','removed') ORDER BY created_at DESC LIMIT 1",(sid,)).fetchone()
        return last[0] if last and last[0] in ("awaiting_approval","awaiting_input") else None

    @staticmethod
    def public(row, *, full=False):
        item = dict(row)
        request = json.loads(item.pop("request_json"))
        item["context"] = json.loads(item.pop("context_json"))
        result = item.pop("result_json", None)
        error = item.pop("error_json", None)
        item.pop("fingerprint", None)
        item.pop("actor_id", None)
        item["input"] = str(request.get("payload", {}).get("text") or request.get("continuation_feedback") or "")
        item["attachments"] = request.get("payload", {}).get("attachments", [])
        item["editable_settings"] = {key: request[key] for key in EDIT_SETTINGS if key in request}
        item["queue_editable"] = queue_editable(request)
        item["show_user"] = request.get("source") not in ("approval_continue", "interaction_continue")
        item["has_result"] = bool(result)
        item["error"] = json.loads(error) if error else None
        if full:
            item["result"] = json.loads(result) if result else None
        return item

    def existing_receipt(self, cid: str, sid: str, actor: str, kind: str, request: dict):
        cid, sid = command_id(cid), _session_id(sid)
        con = connect(self.paths.db)
        try:
            if is_session_deleted(con,sid):
                raise CommandError("session_deleted",410)
            row = con.execute("SELECT * FROM agent_commands WHERE command_id=?",(cid,)).fetchone()
            if not row:
                return None
            if row["session_id"] != sid or row["actor_id"] != actor or row["fingerprint"] != fingerprint(kind,request):
                raise CommandError("command_conflict")
            return {"ok":True,"duplicate":True,"command":self.public(row)}
        finally:
            con.close()

    def accept(self, *, cid: str, sid: str, actor: str, kind: str, request: dict,
               context: dict, turn_id: str, continuation=False, identity_request=None):
        cid, sid = command_id(cid), _session_id(sid)
        digest = fingerprint(kind, identity_request if identity_request is not None else request)
        if len(encode(request).encode()) > 256_000:
            raise CommandError("command_too_large", 413)
        with self.transaction() as con:
            queue = self._queue(con, sid)
            old = con.execute("SELECT * FROM agent_commands WHERE command_id=?", (cid,)).fetchone()
            if old:
                if old["session_id"] != sid or old["actor_id"] != actor or old["fingerprint"] != digest:
                    raise CommandError("command_conflict")
                return {"ok": True, "duplicate": True, "command": self.public(old)}
            pending = con.execute("SELECT interaction_id,state FROM agent_interactions WHERE session_id=? AND state IN ('pending','deferred','answered')", (sid,)).fetchall()
            iid = request.get("interaction_id")
            if iid:
                interaction = con.execute("SELECT state,session_id,actor_id FROM agent_interactions WHERE interaction_id=?", (iid,)).fetchone()
                if not interaction or interaction["session_id"] != sid or interaction["actor_id"] != actor or interaction["state"] not in ("answered","resolved") or cid != "response_"+iid:
                    raise CommandError("interaction_response_required")
            if pending and (len(pending) != 1 or pending[0]["interaction_id"] != iid or pending[0]["state"] != "answered"):
                raise CommandError("interaction_response_required")
            from ..db.repositories import AgentSessionRepository
            repo = AgentSessionRepository(con)
            session = repo.get_session(sid)
            if session and session.get("strategy_id") != request.get("strategy_id"):
                raise CommandError("strategy_binding_conflict")
            if session and session.get("source") in ("mcp", "tunnel"):
                raise CommandError("external_session_read_only")
            if not session:
                repo.upsert_session(session_id=sid, source="user_chat", strategy_id=request.get("strategy_id"),
                                    title=str(request.get("payload", {}).get("text") or "Conversation")[:80],
                                    meta={"strategy_proposal_id":request.get("strategy_proposal_id")})
            count = con.execute("SELECT count(*) FROM agent_commands WHERE session_id=? AND state='queued'", (sid,)).fetchone()[0]
            if count >= MAX_QUEUE:
                raise CommandError("queue_full", 429)
            position = con.execute("SELECT COALESCE(MAX(position),0)+1 FROM agent_commands WHERE session_id=?", (sid,)).fetchone()[0]
            state = "queued"
            if kind == "guide":
                active = con.execute("SELECT turn_id FROM agent_commands WHERE session_id=? AND kind!='guide' AND state='running'", (sid,)).fetchone()
                if not active:
                    raise CommandError("no_running_turn")
                turn_id = active["turn_id"]
            elif continuation or request.get("run_only"):
                if con.execute("SELECT 1 FROM agent_commands WHERE session_id=? AND state='unconfirmed'", (sid,)).fetchone():
                    raise CommandError("execution_unconfirmed")
                if queue["worker_owner"]:
                    raise CommandError("session_turn_in_progress")
                if request.get("run_only") and self._decision_pending(con,sid):
                    raise CommandError("decision_pending")
                if request.get("run_only") and not queue["paused"] and count:
                    raise CommandError("queue_pause_required")
                # Continuing one turn is not permission to drain unrelated work.
                context={**context,"run_once":True,"run_once_epoch":self.epoch}
                position = con.execute("SELECT COALESCE(MIN(position),0)-1 FROM agent_commands WHERE session_id=?", (sid,)).fetchone()[0]
            now = time.time()
            con.execute("INSERT INTO agent_commands(command_id,session_id,actor_id,kind,fingerprint,request_json,context_json,state,turn_id,position,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                        (cid,sid,actor,kind,digest,encode(request),encode(context),state,turn_id,position,now,now))
            con.execute("UPDATE agent_command_queues SET revision=revision+1,updated_at=? WHERE session_id=?", (now,sid))
            row = con.execute("SELECT * FROM agent_commands WHERE command_id=?", (cid,)).fetchone()
            return {"ok": True, "duplicate": False, "command": self.public(row)}

    def _projection(self, con, row, *, full=False):
        item = self.public(row,full=full)
        user = con.execute("SELECT content,deleted FROM agent_messages WHERE session_id=? AND message_id=?",
                           (row["session_id"],row["turn_id"]+":user")).fetchone()
        if user is not None:
            if user["deleted"]:
                item["show_user"] = False
            elif row["kind"] == "send":
                item["input"] = user["content"]
        return item

    def snapshot(self, sid: str, cid: str | None = None):
        sid = _session_id(sid)
        # Polling a healthy queue is a read, not a heartbeat or admission.
        # Only missing/expired queues need the serialized recovery path.
        con = connect(self.paths.db)
        try:
            con.execute("BEGIN")
            if is_session_deleted(con, sid):
                raise CommandError("session_deleted", 410)
            row = con.execute("SELECT * FROM agent_command_queues WHERE session_id=?", (sid,)).fetchone()
            if row is not None:
                queue = dict(row)
                expired = bool(queue["worker_owner"] and queue["lease_until"] < time.time())
                restarted = queue["runtime_epoch"] != self.epoch and not queue["worker_owner"]
                if not expired and not restarted:
                    return self._snapshot(con, sid, cid, queue)
        finally:
            con.close()
        with self.transaction() as con:
            queue = self._queue(con, sid)
            return self._snapshot(con, sid, cid, queue)

    def _snapshot(self, con, sid, cid, queue):
        if cid:
            row = con.execute("SELECT * FROM agent_commands WHERE command_id=? AND session_id=?", (command_id(cid),sid)).fetchone()
            if not row:
                raise CommandError("command_not_found", 404)
            return {"ok": True, "command": self._projection(con,row, full=True)}
        rows = con.execute("SELECT * FROM agent_commands WHERE session_id=? AND "
                           "(state IN ('queued','delivering','delivered','running','stopping','unconfirmed') OR command_id IN "
                           "(SELECT command_id FROM agent_commands WHERE session_id=? ORDER BY created_at DESC LIMIT 30)) ORDER BY position", (sid,sid)).fetchall()
        from ..db.repositories import AgentSessionRepository
        checkpoint = AgentSessionRepository(con).peek_turn_checkpoint(sid)
        safe_checkpoint = None
        if checkpoint and not checkpoint.get("claim_id"):
            cp = checkpoint.get("checkpoint") or {}
            if isinstance(cp, dict) and cp.get("resumable"):
                safe_checkpoint = {"turn_id": checkpoint["turn_id"], "resumable": True,
                                   "resume_count": cp.get("resume_count",0)}
        session = AgentSessionRepository(con).get_session(sid)
        branch = json.loads(session.get("meta_json") or "{}").get("branch_of") if session else None
        return {"ok": True, "branch_of":branch, "commands": [self._projection(con,row) for row in rows],
                "queue": {k: queue[k] for k in ("paused","pause_reason","revision")},
                "checkpoint": safe_checkpoint, "server_time": time.time()}

    def claim(self, sid: str, owner: str):
        with self.transaction() as con:
            queue = self._queue(con, sid)
            if queue["worker_owner"]:
                return None
            if con.execute("SELECT 1 FROM agent_commands WHERE session_id=? AND state='unconfirmed'", (sid,)).fetchone():
                return None
            row = con.execute("SELECT * FROM agent_commands WHERE session_id=? AND kind!='guide' AND state='queued' ORDER BY position LIMIT 1", (sid,)).fetchone()
            if not row:
                return None
            request=json.loads(row["request_json"])
            if request.get("interaction_id"):
                decision = con.execute("SELECT state FROM agent_interactions WHERE interaction_id=? AND session_id=?",
                                       (request["interaction_id"], sid)).fetchone()
                if not decision or decision[0] != "resolved":
                    return None  # Response finalization precedes execution of a new plan/question.
            run_context=json.loads(row["context_json"])
            run_once=bool(run_context.get("run_once") and run_context.get("run_once_epoch")==self.epoch)
            pending_decision=self._decision_pending(con,sid)
            resolution=request.get("source")=="approval_continue" if pending_decision=="awaiting_approval" else bool(request.get("interaction_id"))
            if pending_decision and not resolution:
                return None
            if queue["paused"] and not run_once:
                return None
            now = time.time()
            con.execute("UPDATE agent_command_queues SET worker_owner=?,lease_until=?,runtime_epoch=? WHERE session_id=?", (owner,now+LEASE_SECONDS,self.epoch,sid))
            context = json.loads(row["context_json"])
            context["execution_started_at"] = now
            con.execute("UPDATE agent_commands SET state='running',context_json=?,revision=revision+1,updated_at=? WHERE command_id=?", (encode(context),now,row["command_id"]))
            request = json.loads(row["request_json"])
            if row["kind"] == "send" and request.get("source") != "approval_continue":
                from ..db.repositories import AgentSessionRepository
                AgentSessionRepository(con).record_message(message_id=row["turn_id"]+":user", session_id=sid,
                    turn_id=row["turn_id"], role="user", content=str(request.get("payload",{}).get("text") or ""),
                    meta={"source_command_id":row["command_id"], "attachments":request.get("payload",{}).get("attachments",[])})
            return dict(con.execute("SELECT * FROM agent_commands WHERE command_id=?", (row["command_id"],)).fetchone())

    def heartbeat(self, sid: str, owner: str):
        with self.transaction() as con:
            return bool(con.execute("UPDATE agent_command_queues SET lease_until=? WHERE session_id=? AND worker_owner=?",
                                    (time.time()+LEASE_SECONDS,sid,owner)).rowcount)

    def finish(self, cid: str, sid: str, owner: str, state: str, result=None, error=None):
        with self.transaction() as con:
            queue = con.execute("SELECT * FROM agent_command_queues WHERE session_id=?", (sid,)).fetchone()
            if not queue or queue["worker_owner"] != owner:
                return False  # A lost lease cannot manufacture a new authority state.
            now = time.time()
            current=con.execute("SELECT state FROM agent_commands WHERE command_id=?",(cid,)).fetchone()
            if current and current[0]=="stopping" and state=="succeeded":
                # An in-flight reply can finish after stop admission. Preserve its
                # evidence while reporting the operator's accepted stop outcome.
                state="interrupted"
                if result is not None: result={**result,"stopped_reason":"cancelled","stop_requested":True}
            con.execute("UPDATE agent_commands SET state=?,result_json=?,error_json=?,revision=revision+1,updated_at=? WHERE command_id=?",
                        (state,encode(result) if result is not None else None,encode(error) if error else None,now,cid))
            # Preserve a terminal message even when a gate failed before Kernel
            # wrote its answer; older failures must not disappear after 30 commands.
            command = con.execute("SELECT turn_id,context_json FROM agent_commands WHERE command_id=?", (cid,)).fetchone()
            from ..db.repositories import AgentSessionRepository
            if not con.execute("SELECT 1 FROM agent_messages WHERE session_id=? AND message_id=?",(sid,command["turn_id"]+":assistant")).fetchone():
                AgentSessionRepository(con).record_message(message_id=command["turn_id"]+":assistant",session_id=sid,
                    role="assistant",content="",turn_id=command["turn_id"],meta={})
            # Enrich the canonical answer without replacing its tool/approval metadata.
            answer = con.execute("SELECT meta_json FROM agent_messages WHERE session_id=? AND message_id=?",
                                 (sid, command["turn_id"]+":assistant")).fetchone()
            if answer:
                meta = json.loads(answer[0] or "{}")
                meta["source_command_id"] = cid
                meta["execution_status"] = state
                meta["command_revision"] = con.execute("SELECT revision FROM agent_commands WHERE command_id=?",(cid,)).fetchone()[0]
                saved_turn = dict(result or meta.get("turn") or {})
                saved_turn.update({"command_id":cid,"context_snapshot":json.loads(command["context_json"]),"execution_status":state})
                if error:
                    meta["error"] = encode(error)
                    saved_turn["error"] = encode(error)
                else:
                    meta.pop("error",None)
                    saved_turn.pop("error",None)
                if not saved_turn.get("stopped_reason"):
                    saved_turn["stopped_reason"] = state
                meta["turn"] = saved_turn
                con.execute("UPDATE agent_messages SET meta_json=? WHERE session_id=? AND message_id=?",
                            (encode(meta),sid,command["turn_id"]+":assistant"))
            con.execute("UPDATE agent_commands SET state='not_consumed',revision=revision+1,updated_at=? "
                        "WHERE session_id=? AND kind='guide' AND state IN ('queued','delivering','delivered')", (now,sid))
            if state == "succeeded" and queue["paused"] and queue["pause_reason"] in DECISION_PAUSES:
                # Completing a decision releases only its empty system wait. Old
                # queued work, unresolved effects and operator holds need consent.
                pending = con.execute("SELECT 1 FROM agent_commands WHERE session_id=? AND state IN "
                                      "('queued','running','stopping','delivering','delivered','unconfirmed') LIMIT 1", (sid,)).fetchone()
                if not pending and not self._decision_pending(con, sid):
                    con.execute("UPDATE agent_command_queues SET paused=0,pause_reason='',revision=revision+1 WHERE session_id=?", (sid,))
            if state != "succeeded":
                reason = state
                if queue["paused"] and (queue["pause_reason"] == "operator" or
                        (state in DECISION_PAUSES and queue["pause_reason"] not in DECISION_PAUSES)):
                    reason = queue["pause_reason"]
                con.execute("UPDATE agent_command_queues SET paused=1,pause_reason=?,revision=revision+1 WHERE session_id=?", (reason,sid))
            con.execute("UPDATE agent_command_queues SET worker_owner='',lease_until=0,updated_at=? WHERE session_id=? AND worker_owner=?", (now,sid,owner))
            return True

    def save_outcome(self, cid, sid, tid, state, result, error):
        # Separate from queue/lease finalization: a failed finish transaction
        # must not erase the exact command's already observed outcome.
        with self.transaction() as con:
            current = con.execute(
                "SELECT state FROM agent_commands WHERE command_id=? AND session_id=? AND turn_id=?",
                (cid, sid, tid),
            ).fetchone()
            if not current:
                raise CommandError("command_not_found", 404)
            if current[0] == "stopping" and state == "succeeded":
                state = "interrupted"
                result = {**(result or {}), "stopped_reason": "cancelled", "stop_requested": True}
            con.execute(
                "INSERT OR IGNORE INTO agent_command_events(command_id,event_id,payload_json) VALUES (?,?,?)",
                (cid, "outcome_" + cid, encode({"kind": "command.outcome",
                    "command_id": cid, "session_id": sid, "turn_id": tid,
                    "execution_status": state, "result": result, "error": error})),
            )

    @staticmethod
    def end_decision(con, row, reason, *, state="interrupted"):
        """Close the exact waiting command; unrelated queue items stay paused."""
        result = json.loads(row["result_json"] or "{}")
        result.update(command_id=row["command_id"], turn_id=row["turn_id"],
                      session_id=row["session_id"], stopped_reason=reason, execution_status=state)
        changed = con.execute(
            "UPDATE agent_commands SET state=?,result_json=?,revision=revision+1,updated_at=? "
            "WHERE command_id=? AND state IN ('awaiting_approval','awaiting_input') AND revision=?",
            (state, encode(result), time.time(), row["command_id"], row["revision"]),
        ).rowcount
        if changed:
            answer = con.execute("SELECT meta_json FROM agent_messages WHERE session_id=? AND message_id=?",
                                 (row["session_id"], row["turn_id"] + ":assistant")).fetchone()
            if answer:
                meta = json.loads(answer[0] or "{}")
                if meta.get("source_command_id") == row["command_id"]:
                    meta.update(execution_status=state, turn=result, command_revision=row["revision"] + 1)
                    con.execute("UPDATE agent_messages SET meta_json=? WHERE session_id=? AND message_id=?",
                                (encode(meta), row["session_id"], row["turn_id"] + ":assistant"))
            queue = con.execute("SELECT paused,pause_reason FROM agent_command_queues WHERE session_id=?", (row["session_id"],)).fetchone()
            # Closing a decision must not relabel an existing explicit/safety hold.
            pause_reason = queue["pause_reason"] if queue and queue["paused"] and queue["pause_reason"] not in DECISION_PAUSES else reason
            con.execute("UPDATE agent_command_queues SET paused=1,pause_reason=?,revision=revision+1 WHERE session_id=?",
                        (pause_reason, row["session_id"]))
            con.execute("DELETE FROM agent_turn_checkpoints WHERE session_id=? AND turn_id=? AND claim_id IS NULL",
                        (row["session_id"], row["turn_id"]))
        return bool(changed)

    def record_event(self, cid: str, event: dict):
        self.record_events(cid, [event])

    def record_events(self, cid: str, events: list[dict]):
        # One transaction per small replay batch, never one per model token.
        # Serialize outside the lock and preserve order for pagination/replay.
        rows = [(cid, str(event["event_id"]), encode(event)) for event in events]
        if not rows:
            return
        with self.transaction() as con:
            con.executemany("INSERT OR IGNORE INTO agent_command_events(command_id,event_id,payload_json) VALUES (?,?,?)", rows)

    def events(self, sid: str, cid: str, after: int = 0, limit: int = 500):
        sid, cid = _session_id(sid), command_id(cid)
        con = connect(self.paths.db)
        try:
            if is_session_deleted(con,sid):
                raise CommandError("session_deleted",410)
            if not con.execute("SELECT 1 FROM agent_commands WHERE session_id=? AND command_id=?",(sid,cid)).fetchone():
                raise CommandError("command_not_found",404)
            limit = max(1,min(1000,int(limit)))
            rows = con.execute("SELECT id,payload_json FROM agent_command_events WHERE command_id=? AND id>? ORDER BY id LIMIT ?", (cid,max(0,int(after)),limit+1)).fetchall()
            more = len(rows)>limit
            rows = rows[:limit]
            events = [{**json.loads(row["payload_json"]), "seq": row["id"]} for row in rows]
            cursor = rows[-1]["id"] if rows else max(0,int(after))
            return {"ok": True,"events":events,"cursor":cursor,"next_cursor":cursor,
                    "epoch":cid,"has_more":more,"reset_required":False}
        finally:
            con.close()

    def control(self, sid: str, action: str, *, cid=None, revision=None, text=None, before=None, edit=None, prepare_context=None):
        sid = _session_id(sid)
        with self.transaction() as con:
            queue = self._queue(con,sid)
            row = None
            if cid:
                row = con.execute("SELECT * FROM agent_commands WHERE command_id=? AND session_id=?", (command_id(cid),sid)).fetchone()
                if not row:
                    raise CommandError("command_not_found",404)
            expected = row["revision"] if row else queue["revision"]
            if revision is None or revision != expected:
                raise CommandError("command_revision_conflict")
            now=time.time()
            if action in ("pause","resume"):
                if action=="resume" and self._decision_pending(con,sid):
                    raise CommandError("decision_pending")
                if action=="resume" and con.execute("SELECT 1 FROM agent_commands WHERE session_id=? AND state='unconfirmed'", (sid,)).fetchone():
                    raise CommandError("execution_unconfirmed")
                con.execute("UPDATE agent_command_queues SET paused=?,pause_reason=?,runtime_epoch=?,revision=revision+1,updated_at=? WHERE session_id=?",
                            (int(action=="pause"),"operator" if action=="pause" else "",self.epoch,now,sid))
            elif action=="stop":
                if not row or row["state"] not in ACTIVE or row["kind"]=="guide":
                    raise CommandError("command_not_running")
                context = {**json.loads(row["context_json"]), "stop_requested": True}
                con.execute("UPDATE agent_commands SET state='stopping',context_json=?,revision=revision+1,updated_at=? WHERE command_id=?", (encode(context),now,cid))
                con.execute("UPDATE agent_command_queues SET paused=1,pause_reason='stopping',revision=revision+1 WHERE session_id=?", (sid,))
            elif action in ("edit","remove","move"):
                if not row or row["state"]!="queued" or row["kind"]=="guide":
                    raise CommandError("command_already_claimed")
                if action=="edit":
                    request=json.loads(row["request_json"])
                    if not queue_editable(request):
                        raise CommandError("decision_command_not_editable")
                    if edit is None:
                        if not isinstance(text, str) or not text.strip():
                            raise CommandError("invalid_message", 400)
                        edit = {"payload": {"text": text.strip()}}
                    if not isinstance(edit, dict) or not edit or set(edit) - (EDIT_SETTINGS | {"payload"}):
                        raise CommandError("invalid_command_edit", 400)
                    message = edit.get("payload", {})
                    if not isinstance(message, dict) or set(message) - {"text", "attachments"}:
                        raise CommandError("invalid_command_edit", 400)
                    if "text" in message and not isinstance(message["text"], str):
                        raise CommandError("invalid_message", 400)
                    if row["kind"] == "resume" and edit.get("work_mode", request.get("work_mode", "execute")) != request.get("work_mode", "execute"):
                        raise CommandError("invalid_command_edit", 400)
                    request.update({key: copy.deepcopy(value) for key, value in edit.items() if key != "payload"})
                    request.setdefault("payload", {}).update(copy.deepcopy(message))
                    if row["kind"] == "resume" and "text" in message:
                        request["continuation_feedback"] = message["text"]
                        request["payload"]["text"] = ""
                    validate_command_request(row["kind"], request)
                    if "attachments" in message:
                        # Editing carries saved upload references, never inline bytes or arbitrary URLs.
                        from pathlib import PurePosixPath
                        for file in message["attachments"]:
                            uri = file.get("artifact_uri")
                            prefix = "nerya://artifact/attachments/"
                            if set(file) - {"id", "name", "mime_type", "size", "kind", "artifact_uri", "reference", "uploaded", "reason", "model_sent"} or not isinstance(uri, str) or not uri.startswith(prefix):
                                raise CommandError("attachment_upload_required", 400)
                            rel = uri[len(prefix):]
                            parts = PurePosixPath(rel).parts
                            root = self.paths.artifacts / "attachments"
                            target = root.joinpath(*parts)
                            if not parts or any(p in (".", "..", "") for p in rel.split("/")) or "\\" in rel or not target.resolve().is_relative_to(root.resolve()) or not target.is_file() or any(path.is_symlink() for path in (target, *target.parents) if path != root and root in path.parents):
                                raise CommandError("attachment_upload_required", 400)
                    context = {**json.loads(row["context_json"]), **input_context(request)}
                    if prepare_context:
                        context["accepted_model"] = prepare_context(request)
                    elif set(edit) & {"model_provider", "model_id", "model_tier", "model_context_window"}:
                        context["accepted_model"] = None
                    con.execute("UPDATE agent_commands SET request_json=?,context_json=?,revision=revision+1,updated_at=? WHERE command_id=? AND state='queued' AND revision=?", (encode(request),encode(context),now,cid,revision))
                    # Original fingerprint is immutable: an ACK retry cannot undo an operator edit.
                elif action=="remove":
                    con.execute("UPDATE agent_commands SET state='removed',revision=revision+1,updated_at=? WHERE command_id=?", (now,cid))
                else:
                    ids=[r[0] for r in con.execute("SELECT command_id FROM agent_commands WHERE session_id=? AND kind!='guide' AND state='queued' ORDER BY position", (sid,))]
                    if before is not None and before not in ids:
                        raise CommandError("queue_anchor_missing")
                    if before!=cid:
                        ids.remove(cid)
                        ids.insert(ids.index(before) if before else len(ids),cid)
                        base=con.execute("SELECT COALESCE(MAX(position),0)+1 FROM agent_commands WHERE session_id=? AND state!='queued'", (sid,)).fetchone()[0]
                        for index,item in enumerate(ids):
                            con.execute("UPDATE agent_commands SET position=?,revision=revision+1,updated_at=? WHERE command_id=?", (base+index,now,item))
                con.execute("UPDATE agent_command_queues SET revision=revision+1,updated_at=? WHERE session_id=?", (now,sid))
            else:
                raise CommandError("invalid_command_action",400)
        return self.snapshot(sid)
