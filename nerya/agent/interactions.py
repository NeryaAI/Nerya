"""Durable user decisions. Responses do not confer tool or trading permission."""
from __future__ import annotations
import hashlib
import json
import time
from ..db.sqlite import connect
from .command_store import CommandError, command_id, encode
from .history_mutations import _session_id, is_session_deleted


def validate_payload(kind, payload):
    if kind not in ("question", "plan") or not isinstance(payload, dict):
        raise CommandError("invalid_interaction", 400)
    title = payload.get("title")
    if not isinstance(title, str) or not title.strip() or len(title) > 240:
        raise CommandError("invalid_interaction_title", 400)
    out = {"title": title.strip()}
    message = payload.get("message", "")
    if not isinstance(message, str):
        raise CommandError("invalid_interaction_message", 400)
    out["message"] = message
    for key in (("choices",) if kind == "question" else ("steps", "deliverables", "constraints")):
        values = payload.get(key, [])
        if not isinstance(values, list) or len(values) > (12 if key == "choices" else 30) or any(not isinstance(v, str) or not v.strip() or len(v) > 1000 for v in values):
            raise CommandError("invalid_interaction_options", 400)
        out[key] = list(dict.fromkeys(values))
    if kind == "plan" and not out["steps"]:
        raise CommandError("plan_steps_required", 400)
    if type(payload.get("multiple", False)) is not bool:
        raise CommandError("invalid_interaction_multiple", 400)
    out["multiple"] = payload.get("multiple", False)
    if kind == "question" and "questions" in payload:
        questions = payload["questions"]
        if not isinstance(questions, list) or not 1 <= len(questions) <= 12 or out["choices"]:
            raise CommandError("invalid_interaction_questions", 400)
        out["questions"] = []
        ids = set()
        for question in questions:
            if not isinstance(question, dict):
                raise CommandError("invalid_interaction_question", 400)
            qid, text = question.get("id"), question.get("question")
            if not isinstance(qid, str) or not qid.strip() or len(qid) > 120 or qid in ids or not isinstance(text, str) or not text.strip():
                raise CommandError("invalid_interaction_question", 400)
            ids.add(qid)
            options = question.get("options", [])
            if not isinstance(options, list) or len(options) > 12 or any(not isinstance(v, str) or not v.strip() for v in options) or type(question.get("multiple", False)) is not bool:
                raise CommandError("invalid_interaction_options", 400)
            out["questions"].append({"id": qid, "question": text.strip(), "options": list(dict.fromkeys(options)), "multiple": question.get("multiple", False)})
    return out


def create_interaction(config, *, sid, tid, call_id, kind, payload):
    sid = _session_id(sid)
    payload = validate_payload(kind, payload)
    iid = "interaction_" + hashlib.sha256(encode([sid, tid, call_id]).encode()).hexdigest()[:32]
    con = connect(config.paths.db)
    try:
        con.execute("BEGIN IMMEDIATE")
        if is_session_deleted(con, sid):
            raise CommandError("session_deleted", 410)
        old = con.execute("SELECT * FROM agent_interactions WHERE interaction_id=?", (iid,)).fetchone()
        if old:
            if old["payload_json"] != encode(payload):
                raise CommandError("interaction_conflict")
        else:
            if con.execute("SELECT 1 FROM agent_interactions WHERE session_id=? AND state IN ('pending','deferred','answered')", (sid,)).fetchone():
                raise CommandError("interaction_already_pending")
            con.execute("INSERT INTO agent_interactions(interaction_id,session_id,turn_id,kind,payload_json,state,created_at,updated_at) VALUES (?,?,?,?,?,'pending',?,?)", (iid,sid,tid,kind,encode(payload),time.time(),time.time()))
        con.commit()
        return {"interaction_id": iid, "session_id": sid, "turn_id": tid, "kind": kind, "payload": payload, "state": "pending", "revision": 1}
    finally:
        con.close()


def respond(manager, body):
    if body.get("action") == "reconcile":
        # The answer may have been persisted before continuation admission
        # failed. Recover that exact saved response on an explicit user click;
        # never invent a new answer or automatically replay completed tools.
        return _reconcile_saved_answer(manager, body)
    sid = _session_id(body.get("session_id"))
    iid = command_id(body.get("interaction_id"))
    rid = command_id(body.get("response_id"))
    action = body.get("action", "answer")
    actor = str(body.get("_auth_actor_id") or "local")
    response = {"action": action, "text": body.get("text", ""), "selected": body.get("selected", [])}
    if not isinstance(response["text"], str) or not isinstance(response["selected"], list):
        raise CommandError("invalid_interaction_answer", 400)
    if "answers" in body:
        response["answers"] = body["answers"]
    with manager.store.transaction() as con:
        manager.store._queue(con, sid)
        row = con.execute("SELECT * FROM agent_interactions WHERE interaction_id=? AND session_id=?", (iid,sid)).fetchone()
        if not row:
            raise CommandError("interaction_not_found", 404)
        row = dict(row)
        payload = json.loads(row["payload_json"])
        same = row["response_id"] == rid and row["actor_id"] == actor and row["response_json"] == encode(response)
        if row["state"] == "deferred" and same and action == "defer":
            return {"ok":True,"deferred":True,"duplicate":True}
        if row["state"] == "resolved":
            if same:
                return {"ok": True, "duplicate": True, "interaction_id": iid}
            raise CommandError("interaction_resolved")
        if not same and (row["state"] == "answered" or body.get("expected_revision") != row["revision"]):
            raise CommandError("interaction_revision_conflict")
        if action == "defer":
            con.execute("UPDATE agent_interactions SET state='deferred',response_id=?,response_json=?,actor_id=?,revision=revision+1,updated_at=? WHERE interaction_id=?", (rid,encode(response),actor,time.time(),iid))
            return {"ok": True, "deferred": True}
        if action not in (("answer", "accept", "revise", "reject") if row["kind"] == "plan" else ("answer",)):
            raise CommandError("invalid_interaction_action", 400)
        if action == "revise" and not response["text"].strip():
            raise CommandError("plan_revision_required", 400)
        if any(not isinstance(v,str) or v not in payload.get("choices",[]) for v in response["selected"]):
            raise CommandError("invalid_interaction_choice", 400)
        if not payload.get("multiple") and len(response["selected"]) > 1:
            raise CommandError("invalid_interaction_choice", 400)
        if "questions" in payload:
            validate_answers(payload, response.get("answers", {}))
            if response["selected"]:
                raise CommandError("invalid_interaction_choice", 400)
        elif "answers" in response:
            raise CommandError("invalid_interaction_answer", 400)
        if row["kind"] == "question" and "questions" not in payload and not response["text"].strip() and not response["selected"]:
            raise CommandError("interaction_answer_required", 400)
        original = con.execute("SELECT * FROM agent_commands WHERE session_id=? AND turn_id=? AND kind!='guide' ORDER BY created_at DESC LIMIT 1", (sid,row["turn_id"])).fetchone()
        admitted = con.execute("SELECT 1 FROM agent_commands WHERE command_id=?", ("response_"+iid,)).fetchone()
        if not original or (original["state"] != "awaiting_input" and not (same and admitted)):
            raise CommandError("interaction_turn_not_ready")
        if action == "reject":
            manager.store.end_decision(con, original, "plan_rejected")
            con.execute("UPDATE agent_interactions SET state='resolved',response_id=?,response_json=?,actor_id=?,revision=revision+1,updated_at=? WHERE interaction_id=?",
                        (rid,encode(response),actor,time.time(),iid))
            return {"ok": True, "interaction_id": iid, "decision": "reject"}
        if row["kind"] == "question" and not admitted:
            from ..db.repositories import AgentSessionRepository
            cp = AgentSessionRepository(con).peek_turn_checkpoint(sid)
            if not cp or cp["turn_id"] != row["turn_id"] or cp.get("claim_id") or not cp["checkpoint"].get("resumable"):
                raise CommandError("interaction_checkpoint_stale")
        decision_command = original
        # The original input remains immutable even after a continuation completes.
        original = con.execute("SELECT * FROM agent_commands WHERE session_id=? AND turn_id=? AND command_id!=? ORDER BY created_at LIMIT 1", (sid,row["turn_id"],"response_"+iid)).fetchone()
        request = json.loads(original["request_json"])
        if request.get("source") == "approval_continue" or request.get("kind") == "approval.continue":
            request["source"], request["kind"] = "user_chat", "user.message"
        if not same:
            con.execute("UPDATE agent_interactions SET state='answered',response_id=?,response_json=?,actor_id=?,revision=revision+1,updated_at=? WHERE interaction_id=?", (rid,encode(response),actor,time.time(),iid))
    # Stable command identity makes response/admission retries safe across crashes.
    feedback = "User response to " + payload["title"] + ": " + encode(response)
    if "questions" in payload:
        feedback += " Questions: " + encode(payload["questions"]) + " Omitted answers are unknown. Continue using best judgment within existing permissions. This is not approval for trading or other gated actions."
    request["interaction_id"] = iid
    # run_only is only an isolated *new send*. A question/plan continuation
    # already has its own run_once authority; copying this flag from the
    # original command makes resume validation fail with HTTP 400 and leaves
    # an answered interaction permanently blocking normal follow-ups.
    request.pop("run_only", None)
    if row["kind"] == "question":
        request.update(resume_turn_id=row["turn_id"], continuation_feedback=feedback)
        request["payload"] = {"text": ""}
        kind = "resume"
    else:
        request.pop("resume_turn_id", None)
        request.pop("continuation_feedback", None)
        request.update(work_mode="plan" if action == "revise" else "execute", plan_id=iid, source="interaction_continue")
        instruction = ("Revise this plan using the user's feedback and propose a new plan for review. Do not execute it."
                       if action == "revise" else "Execute this accepted plan within existing permissions.")
        request["payload"] = {"text": instruction + "\n" + encode(payload) + "\n" + feedback}
        kind = "send"
    result = manager.submit({"command_id": "response_"+iid, "session_id":sid,"command_type":kind,"request":request,
                             **{key:body[key] for key in ("_auth_actor_id","_auth_scope","_auth_scopes") if body.get(key) is not None}},start=False)
    with manager.store.transaction() as con:
        con.execute("UPDATE agent_interactions SET state='resolved',revision=revision+1,updated_at=? WHERE interaction_id=? AND response_id=?", (time.time(),iid,rid))
        if row["kind"] == "plan":
            manager.store.end_decision(con, decision_command, "plan_revised" if action == "revise" else "plan_accepted", state="succeeded")
    manager.kick(sid)
    return {"ok": True, "interaction_id": iid, "command":result["command"]}


def _reconcile_saved_answer(manager, body):
    """Explicit, actor-scoped recovery of a saved answer without re-answering."""
    sid = _session_id(body.get("session_id"))
    iid = command_id(body.get("interaction_id"))
    actor = str(body.get("_auth_actor_id") or "local")
    with manager.store.transaction() as con:
        row = con.execute("SELECT state,response_id,response_json,actor_id,revision "
                          "FROM agent_interactions WHERE interaction_id=? AND session_id=?",
                          (iid, sid)).fetchone()
        if row is None:
            raise CommandError("interaction_not_found", 404)
        if row["state"] not in ("answered", "resolved"):
            raise CommandError("interaction_not_answered")
        if row["actor_id"] != actor:
            raise CommandError("interaction_response_owner_mismatch", 403)
        try:
            saved = json.loads(row["response_json"] or "null")
        except (TypeError, ValueError) as exc:
            raise CommandError("invalid_interaction_answer", 400) from exc
        if not isinstance(saved, dict) or saved.get("action") not in ("answer", "accept", "revise", "reject"):
            raise CommandError("invalid_interaction_answer", 400)
        response = {
            "session_id": sid,
            "interaction_id": iid,
            "response_id": row["response_id"],
            "expected_revision": row["revision"],
            "action": saved["action"],
            "text": saved.get("text", ""),
            "selected": saved.get("selected", []),
        }
        response.update({key:body[key] for key in ("_auth_actor_id", "_auth_scope", "_auth_scopes")
                         if body.get(key) is not None})
        if "answers" in saved:
            response["answers"] = saved["answers"]
    # This is the same admission path and deterministic response_ command ID
    # as the original click, so duplicate/reconnect retries stay idempotent.
    return respond(manager, response)


def validate_answers(payload, answers):
    """Missing answers are unknown, never consent."""
    questions = {q["id"]: q for q in payload["questions"]}
    if not isinstance(answers, dict) or any(key not in questions for key in answers):
        raise CommandError("invalid_interaction_answer", 400)
    for key, answer in answers.items():
        if not isinstance(answer, dict) or set(answer) - {"selected", "text"}:
            raise CommandError("invalid_interaction_answer", 400)
        selected, text = answer.get("selected", []), answer.get("text", "")
        question = questions[key]
        if not isinstance(text, str) or not isinstance(selected, list) or any(not isinstance(v, str) or v not in question["options"] for v in selected):
            raise CommandError("invalid_interaction_answer", 400)
        if len(set(selected)) != len(selected) or (not question["multiple"] and (len(selected) > 1 or (selected and text.strip()))):
            raise CommandError("invalid_interaction_choice", 400)
