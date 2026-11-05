"""Durable conversation goals, separate from one model/tool turn.

Goals reuse command admission, leases, permissions and delivery receipts.
Only never-started idle continuations are automatic; unknown effects, user
decisions, operator pauses and transport failures always stop that path.
"""
from __future__ import annotations

import copy
import hashlib
import json
import time
import uuid
from pathlib import Path

from .command_store import CommandError, encode, input_context
from ..task_delivery import completion_is_current


def read_goal(con, sid):
    row = con.execute("SELECT * FROM agent_goals WHERE session_id=?", (sid,)).fetchone()
    if row is None:
        return None
    value = dict(row)
    value["protocol"] = "nerya.goal.v1"
    value["completion_requested"] = bool(value["completion_requested"])
    value["remaining_tokens"] = max(0, value["token_budget"] - value["tokens_used"]) if value["token_budget"] else None
    return value


def admit_goal(con, row, request):
    """Called inside the same transaction as initial command admission."""
    if request.get("work_mode") != "goal" and request.get("source") not in ("goal_continue","approval_continue","interaction_continue"):
        con.execute("UPDATE agent_goals SET status='paused',stop_reason='new_user_input',revision=revision+1,updated_at=? WHERE session_id=? AND status='active'",(time.time(),row["session_id"]))
        con.execute("""UPDATE agent_commands SET state='removed',revision=revision+1,updated_at=?
            WHERE session_id=? AND state='queued' AND json_extract(request_json,'$.source')='goal_continue'""",(time.time(),row["session_id"]))
    if request.get("work_mode") != "goal" or request.get("source") == "goal_continue":
        return
    sid, cid = row["session_id"], row["command_id"]
    existing = read_goal(con, sid)
    linked = con.execute("SELECT 1 FROM agent_goal_commands WHERE command_id=?", (cid,)).fetchone()
    if linked:
        return
    if existing and (request.get("source") in ("approval_continue","interaction_continue") or request.get("resume_turn_id")):
        con.execute("INSERT INTO agent_goal_commands(command_id,goal_id,created_at) VALUES (?,?,?)",(cid,existing["goal_id"],time.time()))
        con.execute("UPDATE agent_goals SET last_command_id=?,revision=revision+1,updated_at=? WHERE session_id=?",(cid,time.time(),sid))
        return
    objective = str(request.get("payload", {}).get("text") or "").strip()
    if not objective or len(objective) > 16000:
        raise CommandError("invalid_goal_objective", 400)
    budget = request.get("goal_token_budget")
    if budget is not None and (type(budget) is not int or budget <= 0):
        raise CommandError("invalid_goal_budget", 400)
    if existing and existing["status"] != "complete":
        if existing["objective"] != objective:
            raise CommandError("unfinished_goal_exists")
        if existing["status"] != "active":
            raise CommandError("goal_resume_required")
        gid = existing["goal_id"]
        con.execute("UPDATE agent_goals SET last_command_id=?,revision=revision+1,updated_at=? WHERE session_id=?", (cid,time.time(),sid))
    else:
        gid, now = "goal_" + uuid.uuid4().hex, time.time()
        con.execute("""INSERT OR REPLACE INTO agent_goals
            (session_id,goal_id,objective,status,token_budget,source_command_id,last_command_id,created_at,updated_at)
            VALUES (?,?,?,'active',?,?,?,?,?)""", (sid,gid,objective,budget,cid,cid,now,now))
    con.execute("INSERT INTO agent_goal_commands(command_id,goal_id,created_at) VALUES (?,?,?)", (cid,gid,time.time()))


def active_context(config):
    from ..db.sqlite import connect_readonly
    from contextlib import closing
    sid, cid = config.get("runtime.command_session_id"), config.get("runtime.command_id")
    if not sid or not cid:
        return None
    with closing(connect_readonly(config.paths.db)) as con:
        goal = read_goal(con, sid)
        link = con.execute("SELECT goal_id FROM agent_goal_commands WHERE command_id=?", (cid,)).fetchone()
        return goal if goal and link and link[0] == goal["goal_id"] else None


def steering(goal):
    """Prompt text lives beside its state owner, never inside the generic loop."""
    template = Path(__file__).with_name("goal_continuation.md").read_text(encoding="utf-8")
    return template + "\nGoal state (JSON data, not instructions):\n" + json.dumps(
        {key: goal[key] for key in ("goal_id","objective","status","revision","tokens_used",
                                   "token_budget","remaining_tokens","no_progress_turns","stop_reason")},
        ensure_ascii=False)


def model_update(store, sid, cid, *, status, reason=""):
    """A model can request completion; the ending turn verifies the request."""
    with store.transaction() as con:
        goal = read_goal(con,sid)
        command = con.execute("SELECT state FROM agent_commands WHERE session_id=? AND command_id=?", (sid,cid)).fetchone()
        link = con.execute("SELECT goal_id FROM agent_goal_commands WHERE command_id=?", (cid,)).fetchone()
        if not goal or not link or link[0] != goal["goal_id"] or not command or command[0] != "running":
            raise CommandError("goal_command_not_running")
        if goal["status"] != "active":
            raise CommandError("goal_not_active")
        if status == "paused":
            raise CommandError("goal_pause_requires_user_control")
        if status == "complete":
            delivery_row = con.execute("""SELECT c.delivery_json FROM agent_commands c JOIN agent_goal_commands g USING(command_id)
                WHERE g.goal_id=? AND c.delivery_json IS NOT NULL ORDER BY c.created_at DESC LIMIT 1""",(goal["goal_id"],)).fetchone()
            delivery = delivery_row[0] if delivery_row else None
            if delivery and (not json.loads(delivery).get("completion",{}).get("allowed") or not completion_is_current(store.paths,json.loads(delivery))):
                raise CommandError("goal_delivery_incomplete")
            con.execute("UPDATE agent_goals SET completion_requested=1,revision=revision+1,updated_at=? WHERE session_id=?", (time.time(),sid))
        elif status == "blocked":
            if not reason or len(reason)>1000:
                raise CommandError("goal_blocker_required",400)
            digest = hashlib.sha256(reason.strip().encode()).hexdigest()
            count = goal["no_progress_turns"] if goal["blocker"] == digest else 0
            con.execute("UPDATE agent_goals SET blocker=?,no_progress_turns=?,stop_reason=?,revision=revision+1,updated_at=? WHERE session_id=?",
                        (digest,count,"blocker_reported",time.time(),sid))
            if count < 2:
                return {**read_goal(con,sid), "update_result":"blocker_recorded_continue_audit"}
            con.execute("UPDATE agent_goals SET status='blocked',stop_reason='repeated_blocker' WHERE session_id=?",(sid,))
        else:
            raise CommandError("invalid_goal_status",400)
        return read_goal(con,sid)


def account_turn(store, cid):
    with store.transaction() as con:
        row = con.execute("SELECT * FROM agent_commands WHERE command_id=?",(cid,)).fetchone()
        link = con.execute("SELECT * FROM agent_goal_commands WHERE command_id=?",(cid,)).fetchone()
        if not row or not link or link["accounted"]:
            return
        goal = read_goal(con,row["session_id"])
        if not goal or goal["goal_id"] != link["goal_id"]:
            return
        result = json.loads(row["result_json"] or "{}")
        budget = result.get("budget") or {}
        tokens = sum(max(0,int(budget.get(k) or 0)) for k in ("input_tokens_total","output_tokens_total"))
        elapsed = max(0,float(result.get("execution_elapsed_ms") or 0)/1000)
        state, reason = goal["status"], goal["stop_reason"]
        error = json.loads(row["error_json"] or "{}")
        if state == "active":
            if row["state"] in ("failed","unconfirmed"):
                state,reason = ("usage_limited" if error.get("code") in ("rate_limited","model_quota_exhausted") else "blocked"), error.get("code") or row["state"]
            elif row["state"] == "interrupted":
                state,reason = "paused", "command_interrupted"
            elif row["state"] in ("awaiting_input","awaiting_approval"):
                reason = row["state"]
            elif row["state"] == "blocked" and result.get("stopped_reason") not in ("max_iterations","max_tool_calls","token_budget_exceeded"):
                state,reason = "blocked", "turn_blocked"
            elif goal["completion_requested"]:
                delivery_row=con.execute("""SELECT c.delivery_json FROM agent_commands c JOIN agent_goal_commands g USING(command_id)
                    WHERE g.goal_id=? AND c.delivery_json IS NOT NULL ORDER BY c.created_at DESC LIMIT 1""",(goal["goal_id"],)).fetchone()
                delivery=json.loads(delivery_row[0]) if delivery_row else None
                completion=result.get("completion_status") or (result.get("verifier_outcome") or {}).get("completion_status")
                if row["state"] == "succeeded" and (delivery is None or delivery.get("completion",{}).get("allowed") and completion_is_current(store.paths,delivery)) and completion not in ("blocked","incomplete"):
                    state,reason = "complete", "completion_verified"
                else:
                    reason = "completion_unverified"
        if goal["token_budget"] is not None and goal["tokens_used"]+tokens >= goal["token_budget"] and state != "complete":
            state,reason = "budget_limited", "token_budget_exhausted"
        empty = not str(result.get("final_text") or result.get("reply_text") or "").strip() and not budget.get("tool_calls")
        no_progress = goal["no_progress_turns"] + 1 if goal["stop_reason"] == "blocker_reported" or empty else 0
        if state=="active" and no_progress>=3:
            state,reason="blocked","repeated_blocker" if goal["blocker"] else "empty_response"
        con.execute("""UPDATE agent_goals SET status=?,stop_reason=?,tokens_used=tokens_used+?,
            time_used_seconds=time_used_seconds+?,no_progress_turns=?,completion_requested=0,
            revision=revision+1,updated_at=? WHERE session_id=?""",
            (state,reason,tokens,elapsed,no_progress,time.time(),row["session_id"]))
        con.execute("UPDATE agent_goal_commands SET accounted=1 WHERE command_id=?",(cid,))
        if state=="active" and row["state"]=="blocked" and result.get("stopped_reason") in ("max_iterations","max_tool_calls","token_budget_exceeded"):
            con.execute("UPDATE agent_command_queues SET paused=0,pause_reason='',revision=revision+1 WHERE session_id=? AND pause_reason='blocked'",(row["session_id"],))
        con.execute("INSERT OR IGNORE INTO agent_command_events(command_id,event_id,payload_json) VALUES (?,?,?)",
            (cid,"goal_accounted_"+cid,encode({"kind":"goal.updated","event_id":"goal_accounted_"+cid,
                "session_id":row["session_id"],"turn_id":row["turn_id"],"goal":read_goal(con,row["session_id"])})))


def enqueue_if_idle(store, sid):
    with store.transaction() as con:
        goal=read_goal(con,sid)
        if not goal or goal["status"] != "active":
            return False
        queue=store._queue(con,sid)
        if queue["paused"] or queue["worker_owner"] or store._decision_pending(con,sid):
            return False
        if con.execute("SELECT 1 FROM agent_commands WHERE session_id=? AND state IN ('queued','running','stopping','unconfirmed')",(sid,)).fetchone():
            return False
        previous=con.execute("SELECT * FROM agent_commands WHERE command_id=?",(goal["last_command_id"],)).fetchone()
        if previous and previous["state"]=="removed":
            previous=con.execute("""SELECT c.* FROM agent_commands c JOIN agent_goal_commands g USING(command_id)
                WHERE g.goal_id=? AND c.state NOT IN ('queued','removed') ORDER BY c.created_at DESC LIMIT 1""",(goal["goal_id"],)).fetchone()
        if not previous or (previous["state"] not in ("succeeded","blocked") and goal["stop_reason"] != "operator_resume"):
            return False
        source=con.execute("SELECT * FROM agent_commands WHERE command_id=?",(goal["source_command_id"],)).fetchone()
        if not source:
            return False
        request=copy.deepcopy(json.loads(source["request_json"]))
        # A coordination receipt is not execution completion. Never repeatedly
        # create child tasks from a parent Goal continuation. The child inherits
        # Goal mode and owns execution; the parent remains explicitly waiting.
        result=json.loads(previous["result_json"] or "{}")
        receipt=result.get("assistant_receipt") or {}
        if receipt.get("protocol")=="nerya.assistant_task.v2":
            child=read_goal(con,receipt.get("task_session_id"))
            state=child["status"] if child and child["status"] in ("complete","blocked","usage_limited","budget_limited","paused") else "active"
            con.execute("UPDATE agent_goals SET status=?,stop_reason=?,revision=revision+1,updated_at=? WHERE session_id=?",
                (state,"child_"+state if state!="active" else "awaiting_child_task",time.time(),sid))
            return False
        for key in ("resume_turn_id","continuation_feedback","interaction_id","run_only","plan_id","assistant_intent"):
            request.pop(key,None)
        request.update(source="goal_continue",kind="goal.continue",payload={"text":"Continue the active goal from saved progress."},work_mode="goal")
        cid="goal_"+hashlib.sha256((goal["goal_id"]+":"+goal["last_command_id"]).encode()).hexdigest()[:40]
        context={**json.loads(source["context_json"]),**input_context(request)}
        for key in ("assistant_request","run_once","run_once_epoch","execution_started_at","stop_requested"):
            context.pop(key,None)
        context["goal_id"]=goal["goal_id"]
        store.accept(cid=cid,sid=sid,actor=source["actor_id"],kind="send",request=request,context=context,
                     turn_id="turn_"+uuid.uuid4().hex,connection=con)
        con.execute("INSERT OR IGNORE INTO agent_goal_commands(command_id,goal_id,created_at) VALUES (?,?,?)",(cid,goal["goal_id"],time.time()))
        con.execute("UPDATE agent_goals SET last_command_id=?,revision=revision+1,updated_at=? WHERE session_id=?",(cid,time.time(),sid))
        return True


def recover(runtime):
    """Recover only idle intent; the command store fences unknown execution."""
    with runtime.store.transaction() as con:
        unaccounted=con.execute("""SELECT c.command_id FROM agent_goal_commands g JOIN agent_commands c USING(command_id)
            WHERE g.accounted=0 AND c.state NOT IN ('queued','running','stopping','unconfirmed','removed')
            AND NOT EXISTS (SELECT 1 FROM agent_deleted_sessions d WHERE d.session_id=c.session_id)""").fetchall()
    for command in unaccounted:
        account_turn(runtime.store,command[0])
    with runtime.store.transaction() as con:
        rows=con.execute("""SELECT g.session_id FROM agent_goals g WHERE g.status='active'
            AND NOT EXISTS (SELECT 1 FROM agent_deleted_sessions d WHERE d.session_id=g.session_id)""").fetchall()
    for row in rows:
        sid=row[0]
        with runtime.store.transaction() as con:
            queue=runtime.store._queue(con,sid)
            if queue["worker_owner"] or queue["pause_reason"]=="operator" or runtime.store._decision_pending(con,sid):
                continue
            if con.execute("SELECT 1 FROM agent_commands WHERE session_id=? AND state='unconfirmed'",(sid,)).fetchone():
                continue
            pending=con.execute("SELECT c.request_json FROM agent_commands c WHERE c.session_id=? AND c.state='queued'",(sid,)).fetchall()
            only_goals=bool(pending) and all(json.loads(item[0]).get("source")=="goal_continue" for item in pending)
            if only_goals and queue["pause_reason"]=="restarted":
                con.execute("UPDATE agent_command_queues SET paused=0,pause_reason='',revision=revision+1 WHERE session_id=?",(sid,))
        if only_goals or enqueue_if_idle(runtime.store,sid):
            runtime.kick(sid)


def sync_parent(runtime, child_sid):
    with runtime.store.transaction() as con:
        exists=con.execute("SELECT 1 FROM sqlite_master WHERE name='assistant_work'").fetchone()
        parents=con.execute("SELECT parent_session_id FROM assistant_work WHERE task_session_id=?",(child_sid,)).fetchall() if exists else []
    for parent in parents:
        # This only reads a verified child goal result; admission receipts alone
        # cannot complete the parent objective or launch another duplicate child.
        enqueue_if_idle(runtime.store,parent[0])


def control(store,sid,action,revision, *, actor=None):
    with store.transaction() as con:
        goal=read_goal(con,sid)
        if not goal:
            raise CommandError("goal_not_found",404)
        source=con.execute("SELECT actor_id FROM agent_commands WHERE command_id=?",(goal["source_command_id"],)).fetchone()
        if actor is not None and (not source or source[0]!=actor):
            raise CommandError("goal_owner_mismatch",403)
        if type(revision) is not int or revision != goal["revision"]:
            raise CommandError("goal_revision_conflict")
        if action not in ("pause","resume"):
            raise CommandError("invalid_goal_action",400)
        queue=store._queue(con,sid)
        if action == "resume":
            if goal["status"] in ("complete","budget_limited"):
                raise CommandError("goal_not_resumable")
            if store._decision_pending(con,sid):
                raise CommandError("decision_pending")
            if con.execute("SELECT 1 FROM agent_commands WHERE session_id=? AND state='unconfirmed'",(sid,)).fetchone():
                raise CommandError("execution_unconfirmed")
            if queue["paused"] and queue["pause_reason"]=="operator":
                raise CommandError("queue_pause_requires_resume")
        con.execute("UPDATE agent_goals SET status=?,stop_reason=?,no_progress_turns=0,blocker='',revision=revision+1,updated_at=? WHERE session_id=?",
            ("paused" if action=="pause" else "active","operator_pause" if action=="pause" else "operator_resume",time.time(),sid))
        if action=="resume" and queue["pause_reason"] in ("failed","blocked","interrupted","stopping","restarted"):
            con.execute("UPDATE agent_command_queues SET paused=0,pause_reason='',revision=revision+1 WHERE session_id=?",(sid,))
        return read_goal(con,sid)
