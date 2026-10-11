"""Persistent task occurrences. CommandStore remains the execution authority."""
from __future__ import annotations

import copy
import hashlib
import json
import time
import uuid
from dataclasses import asdict, dataclass, field
from typing import Any, Literal

from ..core.redaction import redact_display_dict, redact_public_record
from ..db.sqlite import connect
from .command_store import CommandError, encode, validate_command_request

RunOrigin = Literal["strategy_agent", "scheduled_agent"]
WAITING = frozenset({"queued", "running", "stopping", "awaiting_input", "awaiting_approval", "unconfirmed", "blocked"})


def revision(value: Any) -> str:
    return hashlib.sha256(encode(value).encode()).hexdigest()


@dataclass(frozen=True)
class TaskSpec:
    task_kind: RunOrigin
    task_id: str
    session_id: str
    source_revision: str
    title: str
    request: dict[str, Any]
    snapshot: dict[str, Any] = field(default_factory=dict)
    overlap_policy: str = "skip"
    strategy_id: str | None = None


def business_result(result: dict) -> dict:
    """Narrative is not a receipt. Only structured action outcomes prove effects."""
    effects = []
    for action in result.get("actions", []) or []:
        if not isinstance(action, dict):
            continue
        output = action.get("result", action.get("data", {}))
        if isinstance(output, dict):
            status = output.get("status", output.get("state"))
            if status in {"filled", "confirmed", "submitted", "pending", "rejected", "failed", "unconfirmed"}:
                effects.append(redact_display_dict(output))
    states = {row.get("status", row.get("state")) for row in effects}
    status = ("unconfirmed" if "unconfirmed" in states else "submitted" if states & {"submitted", "pending"}
              else "confirmed" if states & {"filled", "confirmed"} else "failed" if states & {"failed", "rejected"}
              else "no_action" if result.get("no_action") else "reported")
    return {"status": status, "effects": effects, "final_text": result.get("final_text", "")}


class TaskRuns:
    def __init__(self, manager):
        self.manager = manager
        self.config = manager.config
        self.store = manager.store

    def admit(self, spec: TaskSpec, *, actor: str, trigger: dict, trigger_kind="schedule",
              trigger_id: str, scheduled_at: float | None = None, client_request_id=None,
              retry_of_run_id=None, start=True, skip_reason=None, operator=False) -> dict:
        if spec.task_kind not in {"strategy_agent", "scheduled_agent"} or spec.overlap_policy not in {"skip", "coalesce", "queue"}:
            raise CommandError("invalid_task_spec", 400)
        identity = {"kind": spec.task_kind, "task": spec.task_id, "revision": spec.source_revision,
                    "trigger_kind": trigger_kind, "trigger_id": trigger_id, "session": spec.session_id}
        digest = revision({**identity, "trigger": trigger, "actor": actor,'request':spec.request,'retry_of':retry_of_run_id})
        # Occurrence identity is independent of the time a worker happened to poll.
        key = revision(identity if trigger_kind != "manual" else {
            "kind": spec.task_kind, "task": spec.task_id, "actor": actor,
            "client_request_id": client_request_id or trigger_id, "session": spec.session_id})
        rid = "run_" + uuid.uuid4().hex
        tid, cid = "turn_" + uuid.uuid4().hex, "task_" + rid[4:]
        request = copy.deepcopy(spec.request)
        request.update(source=spec.task_kind, session_id=spec.session_id, strategy_id=spec.strategy_id,
                       _task_run_id=rid, _auth_actor_id=actor)
        request.setdefault("payload", {}).setdefault("text", spec.title)
        request.setdefault('permission_mode',self.config.get('runtime.permission_mode','default'))
        for name in ('max_iterations','max_total_tool_calls','max_wall_seconds'):
            value=self.config.get('agent.native.'+name)
            if isinstance(value,float) and value.is_integer():value=int(value)
            if name not in request and value is not None:request[name]=value
        validate_command_request("send", request)
        now = time.time()
        snapshot = {**copy.deepcopy(spec.snapshot), "title": spec.title,
                    'work_mode':request.get('work_mode','execute'),
                    "accepted_model": self.manager.accepted_model(request),'permission_mode':request['permission_mode'],
                    'budget':{key:request[key] for key in ('max_iterations','max_total_tool_calls','max_wall_seconds') if key in request}}
        with self.store.transaction() as con:
            alias=con.execute("SELECT * FROM agent_run_requests WHERE request_key=?",(key,)).fetchone()
            if alias:
                if alias["fingerprint"]!=digest:raise CommandError("run_idempotency_conflict")
                return self._receipt(con,con.execute("SELECT * FROM agent_runs WHERE run_id=?",(alias["run_id"],)).fetchone(),duplicate=True)
            old = con.execute("SELECT * FROM agent_runs WHERE dedupe_key=?", (key,)).fetchone()
            if old:
                if old["fingerprint"] != digest:
                    raise CommandError("run_idempotency_conflict")
                return self._receipt(con, old, duplicate=True)
            queue = self.store._queue(con, spec.session_id)
            if retry_of_run_id:
                original=con.execute('SELECT * FROM agent_runs WHERE run_id=?',(retry_of_run_id,)).fetchone()
                if not original or original['session_id']!=spec.session_id or original['task_id']!=spec.task_id or original['task_kind']!=spec.task_kind:
                    raise CommandError('retry_run_binding_mismatch')
                if original['actor_id']!=actor and not operator:raise CommandError('run_owner_mismatch',403)
                previous=con.execute("""SELECT c.* FROM agent_commands c JOIN agent_run_commands l ON c.command_id=l.command_id
                    WHERE l.run_id=? AND c.kind!='guide' ORDER BY l.created_at DESC LIMIT 1""",(retry_of_run_id,)).fetchone()
                pending_funds=con.execute("SELECT 1 FROM financial_actions WHERE run_id=? AND state IN ('awaiting_approval','awaiting_prerequisite','submitting','submitted','confirming','unconfirmed','needs_recovery') LIMIT 1",(retry_of_run_id,)).fetchone()
                if pending_funds:raise CommandError('financial_receipts_require_reconciliation')
                if previous and previous['state'] not in {'succeeded','failed','interrupted','blocked','removed'}:raise CommandError('run_not_ready_for_retry')
                if previous and previous['state']=='blocked':
                    con.execute("UPDATE agent_commands SET state='interrupted',revision=revision+1,updated_at=? WHERE command_id=?",(now,previous['command_id']))
                    if queue['pause_reason']=='blocked':
                        con.execute("UPDATE agent_command_queues SET paused=0,pause_reason='',revision=revision+1 WHERE session_id=?",(spec.session_id,))
                        queue={**queue,'paused':0,'pause_reason':''}
            binding=con.execute('SELECT 1 FROM agent_runs WHERE session_id=? AND (task_kind!=? OR task_id!=?) LIMIT 1',
                (spec.session_id,spec.task_kind,spec.task_id)).fetchone()
            if binding:raise CommandError('task_session_binding_conflict')
            active = con.execute("""SELECT c.*,r.run_id FROM agent_commands c
                LEFT JOIN agent_run_commands l ON l.command_id=c.command_id
                LEFT JOIN agent_runs r ON r.run_id=l.run_id
                WHERE c.session_id=? AND c.kind!='guide' AND c.state IN
                ('queued','running','stopping','awaiting_input','awaiting_approval','unconfirmed','blocked')
                ORDER BY c.created_at DESC LIMIT 1""", (spec.session_id,)).fetchone()
            funds=con.execute("""SELECT r.run_id,r.actor_id FROM financial_actions f JOIN agent_runs r ON f.run_id=r.run_id
                WHERE r.task_kind=? AND r.task_id=? AND f.state IN
                ('awaiting_approval','awaiting_prerequisite','submitting','submitted','confirming','unconfirmed','needs_recovery')
                ORDER BY f.created_at DESC LIMIT 1""",(spec.task_kind,spec.task_id)).fetchone()
            if funds and not active:active=funds
            if trigger_kind == "manual" and active and active["run_id"]:
                old = con.execute("SELECT * FROM agent_runs WHERE run_id=?", (active["run_id"],)).fetchone()
                if old["task_id"]!=spec.task_id or old["task_kind"]!=spec.task_kind:
                    raise CommandError("session_busy_with_other_task")
                if old["actor_id"]!=actor and not operator:raise CommandError("run_owner_mismatch",403)
                con.execute("INSERT INTO agent_run_requests VALUES (?,?,?,?,?)",(key,old["run_id"],digest,actor,client_request_id or trigger_id))
                return self._receipt(con, old, duplicate=True)
            reason, admission = (skip_reason,"skipped") if skip_reason else ("","admitted")
            pending_decision = self.store._decision_pending(con, spec.session_id)
            if not skip_reason and (active or pending_decision or queue["paused"]):
                if spec.overlap_policy == "coalesce" and not pending_decision and (not queue["paused"] or queue['pause_reason']=='funds_pending'):
                    pending = con.execute("""SELECT c.command_id,l.run_id FROM agent_commands c
                        JOIN agent_run_commands l ON l.command_id=c.command_id
                        JOIN agent_runs r ON r.run_id=l.run_id
                        WHERE c.session_id=? AND c.state='queued' AND r.task_kind=? AND r.task_id=?""",
                        (spec.session_id, spec.task_kind, spec.task_id)).fetchall()
                    for row in pending:
                        con.execute("UPDATE agent_commands SET state='removed',revision=revision+1,updated_at=? WHERE command_id=?", (now,row["command_id"]))
                        con.execute("UPDATE agent_runs SET admission_status='skipped',reason='coalesced',revision=revision+1,updated_at=? WHERE run_id=?", (now,row["run_id"]))
                    if funds:
                        con.execute("UPDATE agent_command_queues SET paused=1,pause_reason='funds_pending',revision=revision+1 WHERE session_id=?",(spec.session_id,))
                elif spec.overlap_policy != "queue" or pending_decision or queue["paused"]:
                    admission, reason = "skipped", 'pending_financial_receipt' if funds else "decision_pending" if pending_decision else "paused" if queue["paused"] else "busy"
            con.execute("""INSERT INTO agent_runs(run_id,task_kind,task_id,actor_id,session_id,source_revision,
                trigger_kind,trigger_id,scheduled_at,client_request_id,dedupe_key,fingerprint,admission_status,
                reason,snapshot_json,trigger_json,retry_of_run_id,created_at,updated_at)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (rid,spec.task_kind,spec.task_id,actor,spec.session_id,spec.source_revision,trigger_kind,
                 trigger_id,scheduled_at,client_request_id,key,digest,admission,reason,encode(snapshot),
                 encode(trigger),retry_of_run_id,now,now))
            if trigger_kind=="manual":
                con.execute("INSERT INTO agent_run_requests VALUES (?,?,?,?,?)",(key,rid,digest,actor,client_request_id or trigger_id))
            if admission == "admitted":
                self.store.accept(cid=cid,sid=spec.session_id,actor=actor,kind="send",request=request,
                    context={"version":1,"run_id":rid,"task":snapshot,
                             "accepted_model":snapshot["accepted_model"]},turn_id=tid,connection=con)
                con.execute("INSERT OR IGNORE INTO agent_run_commands VALUES (?,?,?)", (rid,cid,now))
                con.execute("UPDATE agent_sessions SET source=?,title=? WHERE session_id=? AND source='user_chat'",
                            (spec.task_kind,spec.title,spec.session_id))
            occurrence=snapshot.get("occurrence")
            if trigger_kind=="schedule" and occurrence:
                con.execute("""UPDATE task_schedule_state SET last_due=?,next_due=?,updated_at=?
                    WHERE task_id=? AND next_due=?""",(scheduled_at,occurrence.get("next_due"),now,spec.task_id,scheduled_at))
            receipt = self._receipt(con, con.execute("SELECT * FROM agent_runs WHERE run_id=?", (rid,)).fetchone())
        if start and admission == "admitted":
            self.manager.kick(spec.session_id)
        return receipt

    def _receipt(self, con, row, *, duplicate=False):
        item = self._project(con, row)
        return {"ok":True,"duplicate":duplicate,"run_id":item["run_id"],
                "session_id":item["session_id"],"command_id":item.get("command_id"),
                "turn_id":item.get("turn_id"),"admission_status":item["admission_status"],
                "execution_status":item["execution_status"],"run":item}

    @staticmethod
    def _project(con, row):
        data = dict(row)
        for key in ("fingerprint", "dedupe_key", "actor_id"):
            data.pop(key,None)
        for key in ("snapshot", "trigger", "delivery", "result"):
            data[key] = json.loads(data.pop(key+"_json"))
        command = con.execute("""SELECT c.* FROM agent_commands c JOIN agent_run_commands l
            ON l.command_id=c.command_id WHERE l.run_id=? AND c.kind!='guide' ORDER BY l.created_at DESC,c.position DESC LIMIT 1""",
            (data["run_id"],)).fetchone()
        data["execution_status"] = 'historical_unverified' if data["admission_status"]=='legacy' else data["admission_status"]
        if command:
            data.update(command_id=command["command_id"],turn_id=command["turn_id"],execution_status=data["admission_status"] if data["admission_status"]=="skipped" else command["state"],
                        command_revision=command["revision"])
            result = json.loads(command["result_json"] or "{}")
            data["result"] = {**business_result(result),"turn":result,**data["result"]}
            effects=con.execute("SELECT action_id,kind,state,request_json,submission_json,receipt_json,updated_at FROM financial_actions WHERE run_id=? ORDER BY created_at",(data["run_id"],)).fetchall()
            if effects:
                observed=[{"action_id":r["action_id"],"kind":r["kind"],"state":r["state"],"request":json.loads(r['request_json']),"submission":json.loads(r["submission_json"]),"receipt":json.loads(r["receipt_json"])} for r in effects]
                states={r["state"] for r in effects}
                status="unconfirmed" if "unconfirmed" in states else "needs_recovery" if "needs_recovery" in states else "submitted" if states & {"reserved","submitting","submitted","confirming"} else "confirmed" if states=={"confirmed"} else "awaiting_approval" if "awaiting_approval" in states else "reported"
                if states & {'failed_before_submission','rejected'}:status='partial' if 'confirmed' in states else 'failed'
                if any(effect['receipt'].get('partial') for effect in observed):status='partial'
                if 'awaiting_prerequisite' in states:status='needs_recovery'
                data["result"].update(effects=observed,status=status)
                data["updated_at"]=max(data["updated_at"],max(r["updated_at"] for r in effects))
            data["error"] = json.loads(command["error_json"] or "null")
            data["updated_at"] = max(data["updated_at"],command["updated_at"])
            data["elapsed_ms"] = result.get("execution_elapsed_ms")
        queue=con.execute('SELECT paused,pause_reason FROM agent_command_queues WHERE session_id=?',(data['session_id'],)).fetchone()
        if queue:data['queue']={'paused':bool(queue['paused']),'pause_reason':queue['pause_reason']}
        data["business_status"] = data["result"].get("status","not_started")
        data["delivery_status"] = data["delivery"].get("status","not_requested")
        from ..financial.store import FinancialStore
        return redact_public_record(data,identity_fields=FinancialStore.PUBLIC_IDENTITIES|{'source_revision','revision'})

    def get(self, rid, *, actor=None, operator=False):
        con=connect(self.config.paths.db)
        try:
            row=con.execute("SELECT * FROM agent_runs WHERE run_id=?",(rid,)).fetchone()
            if not row:
                raise CommandError("run_not_found",404)
            if actor and row["actor_id"]!=actor and not operator:
                raise CommandError("run_owner_mismatch",403)
            return self._project(con,row)
        finally:
            con.close()

    def list(self, *, actor=None, operator=False, task_kind=None, task_id=None, session_id=None,
             client_request_id=None, cursor=None, limit=50, state=None, since=None, until=None):
        limit=max(1,min(int(limit),100))
        clauses,args=[],[]
        for name,value in (("task_kind",task_kind),("task_id",task_id),("session_id",session_id)):
            if value:
                clauses.append(name+"=?");args.append(value)
        if client_request_id:
            clauses.append("""(client_request_id=? OR EXISTS(SELECT 1 FROM agent_run_requests q
                WHERE q.run_id=agent_runs.run_id AND q.client_request_id=? AND q.actor_id=?))""")
            args.extend([client_request_id,client_request_id,actor or ""])
        if actor and not operator:
            clauses.append("actor_id=?");args.append(actor)
        if since is not None:
            clauses.append("created_at>=?");args.append(float(since))
        if until is not None:
            clauses.append("created_at<?");args.append(float(until))
        if state:
            clauses.append("""CASE WHEN admission_status='skipped' THEN 'skipped' ELSE COALESCE((SELECT c.state FROM agent_commands c JOIN agent_run_commands l
                ON c.command_id=l.command_id WHERE l.run_id=agent_runs.run_id AND c.kind!='guide'
                ORDER BY l.created_at DESC,c.position DESC LIMIT 1),admission_status) END=?""")
            args.append(state)
        if cursor:
            # Stable keyset paging is unaffected by subsequent arrivals.
            try:
                stamp,rid=str(cursor).split(":",1)
                clauses.append("(created_at<? OR (created_at=? AND run_id<?))")
                args.extend([float(stamp),float(stamp),rid])
            except (ValueError,TypeError):
                raise CommandError("invalid_cursor",400)
        where=" WHERE "+" AND ".join(clauses) if clauses else ""
        con=connect(self.config.paths.db)
        try:
            rows=con.execute("SELECT * FROM agent_runs"+where+" ORDER BY created_at DESC,run_id DESC LIMIT ?",(*args,limit+1)).fetchall()
            items=[self._project(con,row) for row in rows[:limit]]
            for item in items:
                item.pop("trigger",None)
                item["snapshot"]={key:item["snapshot"][key] for key in ("title","mode","accepted_model") if key in item["snapshot"]}
                item["result"]={"status":item["business_status"],"final_text":str(item["result"].get("final_text") or "")[:300]}
            last=rows[limit-1] if len(rows)>limit else None
            return {"ok":True,"runs":items,"next_cursor":f"{last['created_at']}:{last['run_id']}" if last else None}
        finally:
            con.close()

    def events(self,rid,*,after=0,limit=500):
        con=connect(self.config.paths.db)
        try:
            limit=max(1,min(int(limit),500))
            rows=con.execute("""SELECT e.id,e.payload_json FROM agent_command_events e JOIN agent_run_commands l
                ON e.command_id=l.command_id WHERE l.run_id=? AND e.id>? ORDER BY e.id LIMIT ?""",
                (rid,int(after),max(1,min(int(limit),500))+1)).fetchall()
            page=rows[:min(int(limit),500)]
            return {"ok":True,"events":[{**redact_public_record(json.loads(r["payload_json"]),identity_fields=frozenset({'transaction_hash','source_revision','security_revision'})),"seq":r['id']} for r in page],
                "next_seq":page[-1]["id"] if page else int(after),"has_more":len(rows)>len(page)}
        finally:con.close()

    def update(self, rid, *, result=None, delivery=None):
        fields,args=[],[]
        for name,value in (("result_json",result),("delivery_json",delivery)):
            if value is not None:
                fields.append(name+"=?");args.append(encode(redact_display_dict(value)))
        if not fields:return
        with self.store.transaction() as con:
            con.execute("UPDATE agent_runs SET "+",".join(fields)+",revision=revision+1,updated_at=? WHERE run_id=?",
                        (*args,time.time(),rid))

    def stop(self,rid):
        run=self.get(rid)
        if not run.get('command_id'):return run
        state=run['execution_status'];cid=run['command_id'];sid=run['session_id']
        if state in {'queued','running'}:
            return self.manager.control({'session_id':sid,'command_id':cid,'expected_revision':run['command_revision'],
                'action':'remove' if state=='queued' else 'stop'})
        if state in {'awaiting_approval','awaiting_input'}:
            with self.store.transaction() as con:
                con.execute("UPDATE agent_commands SET state='interrupted',revision=revision+1,updated_at=? WHERE command_id=? AND state IN ('awaiting_approval','awaiting_input')",(time.time(),cid))
                con.execute("UPDATE agent_interactions SET state='cancelled' WHERE session_id=? AND turn_id=? AND state IN ('pending','deferred','answered')",(sid,run['turn_id']))
                con.execute("UPDATE approvals SET state='cancelled' WHERE state='pending' AND id IN (SELECT approval_id FROM financial_actions WHERE run_id=?)",(rid,))
        return self.get(rid)

    @staticmethod
    def link_continuation(cid, request, *, connection, context=None):
        rid=(context or {}).get("run_id")
        if rid:
            owned=connection.execute("SELECT 1 FROM agent_runs WHERE run_id=? AND session_id=?",(rid,request.get("session_id"))).fetchone()
            if owned:
                connection.execute("INSERT OR IGNORE INTO agent_run_commands VALUES (?,?,?)",(rid,cid,time.time()))
                return
        payload=request.get("payload") or {}
        tid=request.get("resume_turn_id") or payload.get("source_turn_id")
        row=connection.execute("""SELECT l.run_id FROM agent_run_commands l JOIN agent_commands c
            ON c.command_id=l.command_id WHERE c.session_id=? AND c.turn_id=? LIMIT 1""",
            (request.get("session_id"),tid)).fetchone() if tid else None
        if row is None:
            row=connection.execute("""SELECT r.run_id FROM agent_runs r JOIN agent_commands c ON c.session_id=r.session_id
                WHERE c.command_id=? AND r.actor_id=c.actor_id AND r.admission_status='admitted'
                ORDER BY r.created_at DESC LIMIT 1""",(cid,)).fetchone()
        if row:
            connection.execute("INSERT OR IGNORE INTO agent_run_commands VALUES (?,?,?)",(row[0],cid,time.time()))
            root=connection.execute('SELECT snapshot_json FROM agent_runs WHERE run_id=?',(row[0],)).fetchone()
            snapshot=json.loads(root['snapshot_json'])
            command=connection.execute('SELECT request_json,context_json FROM agent_commands WHERE command_id=?',(cid,)).fetchone()
            saved_request=json.loads(command['request_json']);saved_context=json.loads(command['context_json'])
            saved_context.update(run_id=row[0],task=snapshot,accepted_model=snapshot.get('accepted_model'))
            saved_request.update(snapshot.get('budget',{}));saved_request['permission_mode']=snapshot.get('permission_mode','default')
            if snapshot.get('accepted_model'):
                saved_request.update(model_provider=snapshot['accepted_model']['provider'],model_id=snapshot['accepted_model']['model'])
            connection.execute('UPDATE agent_commands SET request_json=?,context_json=? WHERE command_id=?',(encode(saved_request),encode(saved_context),cid))


def task_runs(config):
    from .command_runtime import runtime
    return TaskRuns(runtime(config))
