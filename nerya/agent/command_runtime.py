"""Conversation execution outlives the HTTP request, not the runtime lease.

All work goes through the existing run_turn handler. Queue restoration is
explicit; ambiguous execution is reconciled, never automatically replayed.
"""
from __future__ import annotations

import copy
import hashlib
import json
import logging
import sqlite3
import threading
import time
import uuid
from typing import Callable
from urllib.parse import urlencode

from ..core.config import Config
from ..harness.cancellation import signal_cancel, signal_steer
from .command_store import LEASE_SECONDS, CommandError, CommandStore, command_id, encode, input_context, validate_command_request
from .command_event_buffer import CommandEventBuffer
from .history_mutations import _session_id
from .streaming import get_default_bus

_LOG = logging.getLogger(__name__)
EPOCH = uuid.uuid4().hex


def _control_busy_can_retry(exc: BaseException, last_success: float, now: float) -> bool:
    """A brief SQLite writer collision is not a lost worker or operator stop."""
    if not isinstance(exc, sqlite3.OperationalError):
        return False
    code = getattr(exc, "sqlite_errorcode", None)
    busy = (code & 255) in {sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED} if isinstance(code, int) else str(exc).lower() in {"database is locked", "database table is locked"}
    return busy and now - last_success < min(10.0, LEASE_SECONDS / 2)


_FIELDS = ("source", "kind", "payload", "assistant_id", "conversation_kind", "strategy_id", "strategy_proposal_id", "reasoning_effort",
           "reasoning_summary", "permission_mode", "model_tier", "model_provider", "model_id",
           "model_context_window", "max_iterations", "max_total_tool_calls", "max_wall_seconds",
           "evidence_contract", "resume_turn_id", "continuation_feedback", "work_mode", "interaction_id", "plan_id", "run_only")


def outcome_state(result: dict) -> str:
    if result.get("execution_status") == "unconfirmed":
        return "unconfirmed"
    if result.get("ok") is False or result.get("status") == "error":
        return "failed"
    stop = str(result.get("stopped_reason") or "").lower()
    if stop == "user_input_pending":
        return "awaiting_input"
    if "approval" in stop or result.get("awaiting_approval"):
        return "awaiting_approval"
    if any(word in stop for word in ("cancel", "interrupt", "operator_stop", "assistant_permissions_changed")):
        return "interrupted"
    if result.get("aborted") or stop not in ("", "end_turn", "completed", "succeeded", "stop"):
        return "blocked"
    return "succeeded"


class CommandRuntime:
    def __init__(self, config: Config, execute: Callable[[Config, dict], dict], *, epoch=EPOCH):
        self.config, self.execute = config, execute
        self.store = CommandStore(config.paths, epoch)
        self._lock = threading.Lock()
        self._workers: dict[str, threading.Thread] = {}

    def submit(self, payload: dict, *, start=True):
        raw = payload.get("request")
        if not isinstance(raw, dict):
            raise CommandError("invalid_command_request", 400)
        if raw.get("source") == "approval_continue" or raw.get("kind") == "approval.continue":
            # Old clients may still post after the callback. Resolve the durable
            # decision instead of admitting their second continuation.
            from ..approval_service import ApprovalService
            service = ApprovalService(self.config)
            message = raw.get("payload") or {}
            approved = service.resolved(str(message.get("approval_id") or "")) if isinstance(message, dict) else None
            if not approved or approved.get("state") != "approved":
                raise CommandError("approval_not_resolved")
            actor = str(payload.get("_auth_actor_id") or "local")
            if not service.can_resolve(approved, actor, operator_authorized=service.trusted_operator(payload, actor, approved)):
                raise CommandError("approval_owner_mismatch", 403)
            if (approved.get("requester_session_id") or approved.get("session_id")) != payload.get("session_id"):
                raise CommandError("approval_session_mismatch", 403)
            receipt = self.resolve_approval(approved, start=start)
            if not receipt or not receipt.get("command"):
                raise CommandError("approval_continuation_unavailable")
            return receipt
        kind = payload.get("command_type", "send")
        if kind not in ("send", "resume", "guide"):
            raise CommandError("invalid_command_type", 400)
        request = {key: copy.deepcopy(raw[key]) for key in _FIELDS if key in raw}
        # Dispatcher-authenticated identity only; nested/body assertions are ignored.
        for key in ("_auth_actor_id", "_auth_scope", "_auth_scopes"):
            request[key] = copy.deepcopy(payload.get(key, "" if key != "_auth_scopes" else []))
        request["session_id"] = payload.get("session_id")
        request["target"] = "main"
        validate_command_request(kind, request)
        identity_request = copy.deepcopy(request)
        existing = self.store.existing_receipt(payload.get("command_id"),payload.get("session_id"),
            str(payload.get("_auth_actor_id") or "local"),kind,identity_request)
        if existing is not None:
            return existing
        from .assistants import AssistantError, prepare_request
        try:
            request = prepare_request(self.config, request)
        except AssistantError as exc:
            raise CommandError(exc.code, exc.status) from exc
        context = {"version": 1, **input_context(request), "plan_id": request.get("plan_id"),
                   "strategy_id": request.get("strategy_id"), "proposal_id": request.get("strategy_proposal_id"),
                   "accepted_model": self.accepted_model(request), "assistant_id": request.get("assistant_id")}
        if request.get("interaction_id"):
            # UI text comes from the durable answer, never the generated execution prompt.
            with self.store.transaction() as con:
                answer = con.execute("SELECT kind,payload_json,response_json FROM agent_interactions WHERE interaction_id=? AND session_id=?", (request["interaction_id"], request["session_id"])).fetchone()
                if answer:
                    detail, response = json.loads(answer["payload_json"]), json.loads(answer["response_json"] or "{}")
                    context["interaction_response"] = {"kind": answer["kind"], "title": detail.get("title", ""), "action": response.get("action", "answer"), "text": response.get("text", ""), "answers": response.get("answers", {})}
        if request.get("strategy_id"):
            from ..strategies.workflow_service import source_files
            from ..strategies.workflow_graph import package_revision
            files, source = source_files(self.config.paths, str(request["strategy_id"]), request.get("strategy_proposal_id"))
            context["strategy_source"] = {**source, "revision":package_revision(files)}
        receipt = self.store.accept(cid=payload.get("command_id"),sid=payload.get("session_id"),
            actor=str(payload.get("_auth_actor_id") or "local"),kind=kind,request=request,context=context,
            turn_id=str(request.get("resume_turn_id") or "turn_"+uuid.uuid4().hex),
            continuation=kind=="resume" or bool(request.get("interaction_id")) or request.get("source")=="approval_continue", identity_request=identity_request)
        if start and not receipt["duplicate"]:
            self.kick(receipt["command"]["session_id"])
        return receipt

    def accepted_model(self, request):
        from ..llm.gateway import LLMGateway
        try:
            provider, model, metadata = LLMGateway(self.config).effective_model_metadata(
                request.get("model_tier") or self.config.get("agent.native.tier"), provider_override=request.get("model_provider"),
                model_override=request.get("model_id"))
            if provider and model:
                return {"provider": provider, "model": model,
                    "context_limit": getattr(metadata, "context_window", None),
                    "limit_source": "registry" if getattr(metadata, "context_window", None) else "unconfirmed"}
        except Exception:
            # Original runtime resolves configuration errors; admission does not
            # substitute a model or silently enable mock execution.
            pass
        return None

    def kick(self, sid: str):
        with self._lock:
            if sid in self._workers:
                return
            thread=threading.Thread(target=self._drain,args=(sid,),name="nerya-command-"+sid[-12:],daemon=True)
            self._workers[sid]=thread
            thread.start()

    def _drain(self, sid: str):
        try:
            while True:
                waiting = self.check_approvals(sid)
                # Serialize idle handoff with kick so an accepted item cannot be
                # stranded between the worker's final query and its exit.
                with self._lock:
                    owner=uuid.uuid4().hex
                    row=self.store.claim(sid,owner)
                    if row is None:
                        if not waiting:
                            self._workers.pop(sid,None)
                            return
                if row is None:
                    # Approval expiry and callbacks are runtime work even with
                    # every browser closed. The lease is free while waiting.
                    time.sleep(1)
                else:
                    self._run(row,owner)
        except Exception:
            _LOG.exception("conversation command worker failed; lease recovery will not replay work")
            with self._lock:
                self._workers.pop(sid,None)

    def _run(self, row: dict, owner: str):
        sid,cid,tid=row["session_id"],row["command_id"],row["turn_id"]
        request=json.loads(row["request_json"])
        request["turn_id"]=tid
        config=Config(paths=self.config.paths,data=copy.deepcopy(self.config.data))
        done=threading.Event()
        event_failure=threading.Event()
        bus=get_default_bus()
        head=bus.latest_seq()
        event_buffer=CommandEventBuffer(self.store,cid)
        def capture(event):
            if event.get("session_id")!=sid or event.get("turn_id")!=tid or int(event.get("seq") or 0)<=head:
                return
            try:
                event_buffer.append(event)
            except Exception:
                event_failure.set()
                signal_cancel(tid,reason="event_persistence_failed")
                _LOG.exception("command event persistence failed")
        unsubscribe=bus.subscribe(capture)
        def watch():
            renewed=0.0
            last_control_success=time.monotonic()
            while not done.wait(0.4):
                try:
                    if time.monotonic()-renewed>3:
                        if not self.store.heartbeat(sid,owner):
                            signal_cancel(tid,reason="command_lease_lost")
                            return
                        renewed=time.monotonic()
                    current=self.store.snapshot(sid,cid)["command"]
                    if current["state"]=="stopping":
                        signal_cancel(tid,reason="operator_cancel")
                    self._guides(sid,tid)
                    last_control_success=time.monotonic()
                except Exception as exc:
                    if _control_busy_can_retry(exc,last_control_success,time.monotonic()):
                        _LOG.warning("command control briefly busy; retrying within the worker lease")
                        continue
                    signal_cancel(tid,reason="command_control_unavailable")
                    _LOG.exception("command control failed")
                    return
        watcher=threading.Thread(target=watch,name="nerya-command-control",daemon=True)
        watcher.start()
        result,error,state=None,None,"failed"
        started = time.monotonic()
        try:
            context = json.loads(row["context_json"])
            if context.get("strategy_source"):
                from ..strategies.workflow_service import source_files
                from ..strategies.workflow_graph import package_revision
                files, source = source_files(config.paths, str(request["strategy_id"]), request.get("strategy_proposal_id"))
                expected = context["strategy_source"]
                if package_revision(files) != expected["revision"] or source.get("state") != expected.get("state"):
                    raise CommandError("strategy_version_changed")
            if self.store.snapshot(sid,cid)["command"]["state"]=="stopping":
                result={"turn_id":tid,"session_id":sid,"stopped_reason":"cancelled","final_text":""}
            else:
                result=self.execute(config,request)
            if not isinstance(result,dict):
                raise TypeError("invalid turn result")
            state=outcome_state(result)
            # The accepted snapshot remains requested input; resolved provider
            # usage in the result is authoritative about what actually ran.
            result["command_id"]=cid
            result["execution_elapsed_ms"] = round((time.monotonic()-started)*1000)
            result["context_snapshot"]=json.loads(row["context_json"])
            from ..api.local_server import _json_safe
            result = _json_safe(result)
            if result.get("ok") is False:
                error={"code":str(result.get("error") or "turn_rejected"),"retrying":False}
            reason=str(result.get("stopped_reason") or (result.get("budget") or {}).get("abort_reason") or "")
            code = {"stream_interrupted":"model_stream_interrupted",
                    "cancelled:command_control_unavailable":"command_control_unavailable",
                    "cancelled:command_lease_lost":"command_lease_lost",
                    "cancelled:event_persistence_failed":"event_persistence_failed"}.get(reason)
            if code:
                error={"code":code,"retrying":False,"reason":reason}
            if event_failure.is_set():
                state="blocked"
                error={"code":"event_persistence_failed","retrying":False}
        except Exception as exc:
            status=getattr(exc,"status_code",None)
            error={"code":getattr(exc,"code",None) or ("rate_limited" if status==429 else "turn_failed"),
                   "status_code":status,"type":type(exc).__name__,"retrying":False,
                   "request_id":str(getattr(exc,"request_id","") or "")[:160]}
            if getattr(exc,"stream_interrupted",False):
                error.update(stream_output_committed=bool(getattr(exc,"stream_output_committed",True)),
                             retry_exhausted=str(getattr(exc,"retry_exhausted","not_retryable")))
            _LOG.warning("conversation command %s failed: type=%s code=%s", cid, type(exc).__name__, error["code"])
        finally:
            done.set()
            watcher.join(timeout=5)
            unsubscribe()
            try:
                event_buffer.close()  # Commit final deltas before the command is terminal.
            except Exception:
                event_failure.set()
                _LOG.exception("command final event flush failed")
        if event_failure.is_set():
            state="blocked"
            error={"code":"event_persistence_failed","retrying":False}
        self.store.save_outcome(cid,sid,tid,state,result,error)
        self.store.finish(cid,sid,owner,state,result,error)
        try:
            from .assistant_work import AssistantWork
            AssistantWork(self.config.paths).sync(sid)
        except Exception:
            _LOG.exception("assistant result delivery failed; durable command outcome retained")

    def recover_tasks(self):
        from .assistant_work import AssistantWork
        AssistantWork(self.config.paths).recover(self)

    def check_approvals(self, sid):
        from ..approval_service import ApprovalService
        return ApprovalService(self.config).reconcile_commands(self, sid)

    def resolve_approval(self, record, *, start=True):
        from ..approval_service import ApprovalService
        if str(record.get("kind") or "") in ("trade_intent", "wallet_swap"):
            return None  # Frozen financial actions keep their domain resume path.
        sid = record.get("requester_session_id") or record.get("session_id")
        tid = record.get("turn_id")
        aid = str(record.get("approval_id") or record.get("id") or "")
        state = record.get("state")
        if not sid or not tid or not aid or state not in ("approved", "rejected", "expired", "cancelled"):
            return None
        cid = "approval_" + hashlib.sha256(aid.encode()).hexdigest()[:40]
        with self.store.transaction() as con:
            old = con.execute("SELECT * FROM agent_commands WHERE command_id=? AND session_id=?", (cid, sid)).fetchone()
            original = con.execute("SELECT * FROM agent_commands WHERE session_id=? AND turn_id=? AND kind!='guide' ORDER BY created_at DESC LIMIT 1", (sid, tid)).fetchone()
            if old:
                if original and original["state"] == "awaiting_approval":
                    self.store.end_decision(con, original, "approval_continued", state="succeeded")
                return {"ok": True, "duplicate": True, "command": self.store.public(old)}
            if not original or original["state"] != "awaiting_approval":
                return None
            owner = ApprovalService.owner_actor_id(record)
            # Kernel tool scopes use the existing local-owner identity domain
            # (memory_actor); command admission retains the authenticated wire
            # principal. Compare with that same projection, never a new alias
            # table or an unconditional approval bypass. Remote principals stay
            # distinct and strategy/session/turn checks below still apply.
            from ..memory.scope import memory_actor
            requester_matches = original["actor_id"] == owner or (
                ApprovalService.is_native_tool(record) and memory_actor(original["actor_id"]) == owner)
            if owner and not requester_matches:
                return None
            strategy = record.get("requester_strategy_id") or record.get("strategy_id") or ""
            if str(strategy) != str(json.loads(original["request_json"]).get("strategy_id") or ""):
                return None
            if state != "approved":
                self.store.end_decision(con, original, "approval_" + state)
                return {"ok": True, "state": state, "command_id": original["command_id"]}
            original = dict(original)
        request = json.loads(original["request_json"])
        for key in ("resume_turn_id", "continuation_feedback", "interaction_id", "run_only"):
            request.pop(key, None)
        request.update(source="approval_continue", kind="approval.continue", payload={
            "text": "The requested permission was approved. Continue the original task within existing permissions.",
            "approval_id": aid, "approval_state": "approved"})
        context = {**json.loads(original["context_json"]), "approval_id": aid,
                   "captured_at": time.time(), "input_text": request["payload"]["text"]}
        receipt = self.store.accept(cid=cid, sid=sid, actor=original["actor_id"], kind="send",
            request=request, context=context, turn_id="turn_"+uuid.uuid4().hex, continuation=True)
        # Admission and closing the original decision are retryable with the
        # same ID. A duplicate callback cannot release any other queued work.
        with self.store.transaction() as con:
            self.store.end_decision(con, original, "approval_continued", state="succeeded")
        if start:
            self.kick(sid)
        return receipt

    def _guides(self,sid: str,tid: str):
        # Most control ticks have no steering input. Do not acquire a writer
        # reservation twice a second just to discover an empty inbox.
        from ..db.sqlite import connect_readonly
        con = connect_readonly(self.config.paths.db)
        try:
            if not con.execute("SELECT 1 FROM agent_commands WHERE session_id=? AND turn_id=? AND kind='guide' AND state='queued' LIMIT 1", (sid,tid)).fetchone():
                return
        finally:
            con.close()
        with self.store.transaction() as con:
            rows=con.execute("SELECT command_id,request_json FROM agent_commands WHERE session_id=? AND turn_id=? AND kind='guide' AND state='queued' ORDER BY position",(sid,tid)).fetchall()
            for row in rows:
                con.execute("UPDATE agent_commands SET state='delivering',revision=revision+1,updated_at=? WHERE command_id=?",(time.time(),row["command_id"]))
        for row in rows:
            cid=row["command_id"]
            text=json.loads(row["request_json"])["payload"]["text"]
            def consumed(command=cid):
                with self.store.transaction() as con:
                    con.execute("UPDATE agent_commands SET state='injected',revision=revision+1,updated_at=? WHERE command_id=? AND state IN ('delivering','delivered')",(time.time(),command))
            accepted=signal_steer(tid,text,on_consumed=consumed)
            with self.store.transaction() as con:
                con.execute("UPDATE agent_commands SET state=?,revision=revision+1,updated_at=? WHERE command_id=? AND state='delivering'",
                            ("delivered" if accepted else "queued",time.time(),cid))

    def control(self,payload: dict):
        sid=payload.get("session_id")
        if payload.get("action")=="reconcile":
            return self.reconcile(sid,payload.get("command_id"))
        if payload.get("action") == "edit":
            allowed = {"session_id", "action", "command_id", "expected_revision", "request", "text",
                       "_auth_actor_id", "_auth_scope", "_auth_scopes"}
            if set(payload) - allowed or ("request" in payload and (not isinstance(payload["request"], dict) or "text" in payload)):
                raise CommandError("invalid_command_edit", 400)
        response=self.store.control(sid,payload.get("action"),cid=payload.get("command_id"),
            revision=payload.get("expected_revision"),text=payload.get("text"),before=payload.get("before_command_id"),
            edit=payload.get("request"),prepare_context=self.accepted_model)
        if payload.get("action")=="stop":
            command=next(c for c in response["commands"] if c["command_id"]==payload["command_id"])
            response["signal_delivered"]=signal_cancel(command["turn_id"],reason="operator_cancel")
            # Never equate this signal receipt with actual execution termination.
        elif payload.get("action")=="resume":
            self.kick(sid)
        return response

    def reconcile(self,sid: str,cid: str):
        sid, cid = _session_id(sid), command_id(cid)
        diagnostic = {"status": "matched", "command_id": cid, "session_id": sid,
                      "turn_id": None, "source": "command",
                      "evidence_path": "/agent/commands/events?" + urlencode({"session_id": sid, "command_id": cid})}
        try:
            command=self.store.snapshot(sid,cid)["command"]
        except (sqlite3.Error, OSError):
            return {"ok": True, "reconciliation": {**diagnostic, "status": "lookup_failed", "source": None}}
        diagnostic["turn_id"] = command["turn_id"]
        if command["state"] == "awaiting_approval":
            # Explicit reconciliation may recover a decision saved before a
            # server restart/callback failure. Reuse the same approval-derived
            # command id; never replay unconfirmed work or drain unrelated jobs.
            self.check_approvals(sid)
            command = self.store.snapshot(sid, cid)["command"]
            if command["state"] != "awaiting_approval":
                diagnostic.update(status="matched", source="durable_approval")
                self.kick(sid)
            else:
                diagnostic.update(status="decision_pending", source="durable_approval")
        if command["state"] == "unconfirmed":
            diagnostic.update(status="insufficient_evidence", source=None)
            try:
                with self.store.transaction() as con:
                    saved = con.execute("SELECT payload_json FROM agent_command_events WHERE command_id=? AND event_id=?", (cid, "outcome_"+cid)).fetchone()
                    evidence = json.loads(saved[0]) if saved else {}
                    result, state, error = evidence.get("result"), evidence.get("execution_status"), evidence.get("error")
                    source = "command.outcome"
                    exact = evidence.get("command_id") == cid and evidence.get("session_id") == sid and evidence.get("turn_id") == command["turn_id"]
                    if not exact:
                        row=con.execute("SELECT meta_json FROM agent_messages WHERE session_id=? AND turn_id=? AND role='assistant' AND deleted=0 ORDER BY ts DESC LIMIT 1", (sid,command["turn_id"])).fetchone()
                        meta=json.loads(row[0]) if row else {}
                        result=meta.get("turn")
                        state=meta.get("execution_status")
                        exact = isinstance(result,dict) and bool(result.get("stopped_reason")) and meta.get("source_command_id") == cid
                        source = "assistant_message"
                        if exact and not state:
                            state = outcome_state(result)
                    if exact and state in ("succeeded","failed","blocked","awaiting_approval","awaiting_input","interrupted"):
                        if state == "succeeded" and command["context"].get("stop_requested"):
                            state = "interrupted"
                            result = {**(result or {}), "stopped_reason": "cancelled", "stop_requested": True}
                        con.execute("UPDATE agent_commands SET state=?,result_json=?,error_json=?,revision=revision+1,updated_at=? WHERE command_id=? AND state='unconfirmed'",
                            (state,encode(result) if result is not None else None,encode(error) if error else None,time.time(),cid))
                        diagnostic.update(status="matched", source=source)
                command = self.store.snapshot(sid,cid)["command"]
            except Exception:
                diagnostic.update(status="lookup_failed", source=None)
                _LOG.warning("command outcome lookup failed for %s", cid)
        return {"ok": True, "command": command, "reconciliation": diagnostic}


_RUNTIMES: dict[str,CommandRuntime]={}
_LOCK=threading.Lock()

def runtime(config: Config,execute: Callable[[Config,dict],dict] | None = None) -> CommandRuntime:
    key=str(config.paths.db.resolve())
    with _LOCK:
        instance=_RUNTIMES.get(key)
        if instance is None:
            instance=CommandRuntime(config,execute or _execute_internal)
            _RUNTIMES[key]=instance
        else:
            instance.config=config
        return instance


def _execute_internal(config, request):
    from ..api.routes_agent import routes
    from ..sdk.internal_client import InternalClient
    handler = next(handler for method, path, handler in routes() if path == "/agent/run_turn_internal")
    return handler(InternalClient.from_config(config), request)
