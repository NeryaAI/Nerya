"""Decision recovery uses temp workspaces and fake runners only."""
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from types import SimpleNamespace
import json
import sqlite3
import time

import pytest

from nerya.agent import command_runtime
from nerya.agent.command_runtime import CommandRuntime
from nerya.agent.command_store import CommandError
from nerya.agent.interactions import create_interaction, respond
from nerya.api.routes_approvals import _callback
from nerya.approval_service import ApprovalService
from nerya.core import jsonl
from nerya.core.config import Config, DEFAULT_CONFIG
from nerya.core.paths import WorkspacePaths


SID = "decision-session"

def request_snapshot(request):
    # The live request contains a runtime-owned CancelToken with thread locks;
    # assertions inspect only its serializable admission fields.
    return deepcopy({key:value for key,value in request.items() if key!="_task_cancel_token"})


@pytest.fixture
def manager(tmp_path, monkeypatch):
    config = Config(paths=WorkspacePaths(root=tmp_path), data=deepcopy(DEFAULT_CONFIG))
    calls = []
    def execute(config, request):
        calls.append(request_snapshot(request))
        return {"stopped_reason": "end_turn", "final_text": "Fixture result"}
    rt = CommandRuntime(config, execute, epoch="decisions-test")
    rt.calls = calls
    monkeypatch.setitem(command_runtime._RUNTIMES, str(config.paths.db.resolve()), rt)
    yield rt
    for thread in list(rt._workers.values()):
        thread.join(3)


def submit(rt, cid="command-original", **request):
    return rt.submit({"command_id": cid, "session_id": SID, "_auth_actor_id": "operator",
        "_auth_scopes": ["read:sessions"],
        "request": {"payload": {"text": "Read evidence"}, **request}}, start=False)["command"]


def seed_approval(rt, *, expires_at=None, state="pending", finish=True):
    submit(rt)
    row = rt.store.claim(SID, "owner")
    submit(rt, "command-unrelated")
    record = {"approval_id": "approval-test", "state": state, "kind": "tool_permission_batch",
        "turn_id": row["turn_id"], "session_id": SID, "requester_actor_id": "operator",
        "requester_session_id": SID, "expires_at": expires_at or time.time() + 60,
        "items": [{"tool": {"name": "fixture_read"}}]}
    jsonl.append(rt.config.paths.approvals_pending, record)
    if finish:
        rt.store.finish(row["command_id"], SID, "owner", "awaiting_approval", {"stopped_reason": "approval_pending"})
    return record, row


def callback(rt, action="approve"):
    return _callback(SimpleNamespace(config=rt.config), {"callback_data": action+":approval-test",
        "_auth_actor_id": "operator", "_auth_scopes": ["approve:tool"]})


def wait_for(predicate):
    deadline = time.monotonic()+5
    while time.monotonic() < deadline:
        result = predicate()
        if result:
            return result
        time.sleep(.03)
    raise AssertionError("fake runner did not settle")


def test_two_callbacks_continue_once_without_browser_and_keep_queue_paused(manager):
    record, _ = seed_approval(manager)
    with ThreadPoolExecutor(max_workers=2) as pool:
        receipts = list(pool.map(lambda _: callback(manager), range(2)))
    assert all(receipt["ok"] for receipt in receipts), receipts
    wait_for(lambda: len(manager.calls) == 1)
    wait_for(lambda: not manager._workers)
    assert len(manager.calls) == 1
    assert manager.calls[0]["payload"]["approval_id"] == record["approval_id"]
    assert manager.calls[0]["_auth_scopes"] == ["read:sessions"]
    snapshot = manager.store.snapshot(SID)
    assert snapshot["queue"]["paused"]
    assert next(c for c in snapshot["commands"] if c["command_id"] == "command-unrelated")["state"] == "queued"
    assert len([c for c in snapshot["commands"] if c["command_id"].startswith("approval_")]) == 1
    assert callback(manager)["duplicate"]
    old_client = manager.submit({"command_id": "client-extra-command", "session_id": SID,
        "_auth_actor_id": "operator", "request": {"source": "approval_continue",
        "payload": {"approval_id": record["approval_id"], "text": "continue"}}})
    assert old_client["duplicate"]
    assert len(manager.calls) == 1


def test_explicit_reconciliation_recovers_saved_permission_without_reapproval(manager):
    record, original=seed_approval(manager)
    service=ApprovalService(manager.config)
    service.move(record["approval_id"],state="approved",resolver_actor_id="operator")
    assert service.resolution_states(SID)==[{"id":record["approval_id"],"state":"approved",
                                            "kind":"tool_permission_batch","turn_id":original["turn_id"]}]
    assert manager.store.snapshot(SID,original["command_id"])["command"]["state"]=="awaiting_approval"
    result=manager.reconcile(SID,original["command_id"])
    assert result["reconciliation"]["source"]=="durable_approval"
    wait_for(lambda:len(manager.calls)==1)
    wait_for(lambda:not manager._workers)
    manager.reconcile(SID,original["command_id"])
    assert len(manager.calls)==1
    assert manager.store.snapshot(SID,"command-unrelated")["command"]["state"]=="queued"
    assert manager.calls[0]["payload"]["approval_id"]==record["approval_id"]


@pytest.mark.parametrize("principal", ["local:loopback", "admin:password"])
def test_native_tool_approval_matches_the_same_local_owner_domain_as_kernel(manager, principal):
    record, original = seed_approval(manager)
    # Emulate trusted command admission plus the kernel's existing scope
    # projection. Only fixtures are written; production identities are untouched.
    with manager.store.transaction() as con:
        request = json.loads(con.execute("SELECT request_json FROM agent_commands WHERE command_id=?",(original["command_id"],)).fetchone()[0])
        request["_auth_actor_id"] = principal
        con.execute("UPDATE agent_commands SET actor_id=?,request_json=? WHERE command_id=?",
                    (principal,json.dumps(request),original["command_id"]))
    record.update(requester_actor_id="default",state="approved")
    receipt = manager.resolve_approval(record)
    assert receipt and receipt["ok"]
    wait_for(lambda:len(manager.calls)==1)
    wait_for(lambda:not manager._workers)
    assert manager.calls[0]["_auth_actor_id"] == principal
    assert manager.store.snapshot(SID,"command-unrelated")["command"]["state"] == "queued"


def test_remote_actor_does_not_acquire_the_local_owner_approval(manager):
    record, original = seed_approval(manager)
    with manager.store.transaction() as con:
        con.execute("UPDATE agent_commands SET actor_id=? WHERE command_id=?",("remote:alice",original["command_id"]))
    record.update(requester_actor_id="default",state="approved")
    assert manager.resolve_approval(record) is None
    assert manager.calls == []


def test_callback_before_finish_is_recovered_by_worker(manager):
    _, row = seed_approval(manager, finish=False)
    assert callback(manager)["ok"]
    wait_for(lambda: not manager._workers)
    manager.store.finish(row["command_id"], SID, "owner", "awaiting_approval", {"stopped_reason": "approval_pending"})
    manager.kick(SID)
    wait_for(lambda: len(manager.calls) == 1)
    wait_for(lambda: not manager._workers)
    assert manager.store.snapshot(SID)["queue"]["paused"]


@pytest.mark.parametrize("state", ["rejected", "expired", "cancelled"])
def test_negative_decisions_end_wait_without_running_or_draining(manager, state):
    record, row = seed_approval(manager, expires_at=time.time()-.1 if state=="expired" else None)
    if state == "rejected":
        assert callback(manager, "reject")["ok"]
    elif state == "cancelled":
        service = ApprovalService(manager.config)
        moved = service.move(record["approval_id"], state=state, resolver_actor_id="operator")
        service.publish_resolution(record["approval_id"], state=state, record=moved)
    else:
        manager.kick(SID)
    wait_for(lambda: manager.store.snapshot(SID, row["command_id"])["command"]["state"] == "interrupted")
    wait_for(lambda: not manager._workers)
    assert not manager.calls
    snapshot = manager.store.snapshot(SID)
    assert snapshot["queue"]["paused"]
    assert next(c for c in snapshot["commands"] if c["command_id"] == "command-unrelated")["state"] == "queued"
    assert manager.store.snapshot(SID, row["command_id"])["command"]["result"]["stopped_reason"] == "approval_"+state


def test_pending_approval_expires_with_no_browser_poll(manager):
    _, row = seed_approval(manager, expires_at=time.time()+.15)
    manager.kick(SID)
    wait_for(lambda: manager.store.snapshot(SID, row["command_id"])["command"]["state"] == "interrupted")
    wait_for(lambda: not manager._workers)
    assert not manager.calls
    assert ApprovalService(manager.config).resolved("approval-test")["state"] == "expired"


@pytest.mark.parametrize("kind", ["trade_intent", "wallet_swap"])
def test_financial_approval_never_becomes_generic_continuation(manager, kind):
    record, _ = seed_approval(manager)
    record.update(kind=kind, state="approved")
    assert manager.resolve_approval(record) is None
    assert not manager.calls
    assert len(manager.store.snapshot(SID)["commands"]) == 2


def seed_plan(rt):
    submit(rt, work_mode="plan")
    row = rt.store.claim(SID, "owner")
    submit(rt, "command-unrelated")
    item = create_interaction(rt.config, sid=SID, tid=row["turn_id"], call_id="call-plan",
        kind="plan", payload={"title": "Evidence plan", "steps": ["Read", "Report"]})
    rt.store.finish(row["command_id"], SID, "owner", "awaiting_input", {"stopped_reason": "user_input_pending"})
    return item, {"session_id": SID, "interaction_id": item["interaction_id"],
        "response_id": "plan-response-test", "expected_revision": 1, "_auth_actor_id": "operator"}


@pytest.mark.parametrize("action,mode", [("accept","execute"), ("answer","execute"), ("revise","plan")])
def test_plan_decision_modes_and_duplicate_response(manager, action, mode):
    _, body = seed_plan(manager)
    body.update(action=action, text="Read only the latest report")
    receipt = respond(manager, body)
    wait_for(lambda: not manager._workers)
    assert receipt["ok"]
    assert len(manager.calls) == 1
    assert manager.calls[0]["work_mode"] == mode
    assert respond(manager, body)["duplicate"]
    assert manager.store.snapshot(SID)["queue"]["paused"]
    assert manager.store.snapshot(SID, "command-unrelated")["command"]["state"] == "queued"


def test_plan_reject_and_defer_never_execute(manager):
    item, body = seed_plan(manager)
    deferred = respond(manager, {**body, "action": "defer"})
    assert deferred["deferred"]
    assert respond(manager, {**body, "action": "defer"})["duplicate"]
    with manager.store.transaction() as con:
        saved = con.execute("SELECT * FROM agent_interactions WHERE interaction_id=?", (item["interaction_id"],)).fetchone()
        assert saved["state"] == "deferred"
        assert json.loads(saved["payload_json"]) == item["payload"]
        body.update(expected_revision=saved["revision"], response_id="plan-reject-test", action="reject")
    assert respond(manager, body)["decision"] == "reject"
    assert respond(manager, body)["duplicate"]
    assert not manager.calls
    assert manager.store.snapshot(SID, "command-original")["command"]["state"] == "interrupted"
    assert manager.store.claim(SID, "other") is None


def test_plan_revision_requires_feedback_and_rejects_stale_accept(manager):
    _, body = seed_plan(manager)
    with pytest.raises(CommandError, match="plan_revision_required"):
        respond(manager, {**body, "action": "revise"})
    respond(manager, {**body, "action": "reject"})
    with pytest.raises(CommandError, match="interaction_resolved"):
        respond(manager, {**body, "action": "accept", "response_id": "stale-accept"})


def test_revision_proposes_new_plan_and_only_its_accept_executes(manager):
    _, body = seed_plan(manager)
    proposed = []
    def execute(config, request):
        manager.calls.append(request_snapshot(request))
        if request["work_mode"] == "plan":
            proposed.append(create_interaction(config, sid=SID, tid=request["turn_id"], call_id="revised-plan",
                kind="plan", payload={"title": "Revised plan", "steps": ["Read latest report only"]}))
            return {"stopped_reason": "user_input_pending"}
        return {"stopped_reason": "end_turn"}
    manager.execute = execute
    receipt = respond(manager, {**body, "action": "revise", "text": "Latest report only"})
    wait_for(lambda: not manager._workers)
    assert manager.store.snapshot(SID, receipt["command"]["command_id"])["command"]["state"] == "awaiting_input"
    assert [call["work_mode"] for call in manager.calls] == ["plan"]
    assert proposed[0]["interaction_id"] != body["interaction_id"]
    respond(manager, {**body, "interaction_id": proposed[0]["interaction_id"],
        "response_id": "revised-plan-accept", "action": "accept"})
    wait_for(lambda: not manager._workers)
    assert [call["work_mode"] for call in manager.calls] == ["plan", "execute"]
    assert manager.store.snapshot(SID, "command-unrelated")["command"]["state"] == "queued"


def test_question_after_approval_continuation_uses_checkpoint_not_approval_again(manager):
    from nerya.db.repositories import AgentSessionRepository
    seed_approval(manager)
    questions = []
    def execute(config, request):
        manager.calls.append(request_snapshot(request))
        if len(manager.calls) == 1:
            questions.append(create_interaction(config, sid=SID, tid=request["turn_id"], call_id="after-approval",
                kind="question", payload={"title": "Which report?"}))
            with manager.store.transaction() as con:
                AgentSessionRepository(con).save_turn_checkpoint(session_id=SID, turn_id=request["turn_id"], checkpoint={"resumable": True})
            return {"stopped_reason": "user_input_pending"}
        return {"stopped_reason": "end_turn"}
    manager.execute = execute
    assert callback(manager)["ok"]
    wait_for(lambda: questions and not manager._workers)
    respond(manager, {"session_id": SID, "interaction_id": questions[0]["interaction_id"],
        "response_id": "answer-after-approval", "expected_revision": 1,
        "_auth_actor_id": "operator", "action": "answer", "text": "Latest report"})
    wait_for(lambda: not manager._workers)
    assert len(manager.calls) == 2
    # Question answers resume the checkpoint without replaying approval or
    # projecting internal continuation feedback as a new user-chat message.
    assert manager.calls[1]["source"] == "interaction_continue"
    assert manager.calls[1]["kind"] == "user.message"
    assert manager.calls[1]["payload"] == {"text": ""}
    assert manager.calls[1]["resume_turn_id"] == manager.calls[0]["turn_id"]
    continuation = manager.store.snapshot(SID, "response_" + questions[0]["interaction_id"])["command"]
    assert continuation["kind"] == "resume"
    assert continuation["show_user"] is False
    assert continuation["context"]["interaction_response"]["text"] == "Latest report"
    assert manager.store.snapshot(SID, "command-unrelated")["command"]["state"] == "queued"


def test_outcome_survives_finish_failure_and_matches_exact_command(manager, monkeypatch):
    submit(manager)
    row = manager.store.claim(SID, "owner")
    def failed_finish(*args):
        raise RuntimeError("fixture finish failure")
    monkeypatch.setattr(manager.store, "finish", failed_finish)
    with pytest.raises(RuntimeError, match="fixture finish"):
        manager._run(row, "owner")
    with manager.store.transaction() as con:
        con.execute("UPDATE agent_command_queues SET lease_until=0 WHERE session_id=?", (SID,))
    receipt = manager.reconcile(SID, row["command_id"])
    assert receipt["reconciliation"]["status"] == "matched"
    assert receipt["reconciliation"]["source"] == "command.outcome"
    assert receipt["command"]["state"] == "succeeded"
    assert receipt["command"]["result"]["command_id"] == row["command_id"]
    assert len(manager.calls) == 1
    assert manager.store.snapshot(SID)["queue"]["paused"]


def test_unknown_does_not_infer_completion_from_older_turn_or_bad_evidence(manager):
    from nerya.db.repositories import AgentSessionRepository
    submit(manager)
    row = manager.store.claim(SID, "owner")
    with manager.store.transaction() as con:
        con.execute("UPDATE agent_command_queues SET lease_until=0 WHERE session_id=?", (SID,))
        AgentSessionRepository(con).record_message(message_id=row["turn_id"]+":assistant", session_id=SID,
            turn_id=row["turn_id"], role="assistant", content="Old result",
            meta={"source_command_id": "older-command", "turn": {"stopped_reason": "end_turn"}})
    receipt = manager.reconcile(SID, row["command_id"])
    assert receipt["reconciliation"]["status"] == "insufficient_evidence"
    assert receipt["command"]["state"] == "unconfirmed"
    with manager.store.transaction() as con:
        con.execute("UPDATE agent_messages SET meta_json='broken' WHERE session_id=? AND role='assistant'", (SID,))
    receipt = manager.reconcile(SID, row["command_id"])
    assert receipt["reconciliation"]["status"] == "lookup_failed"
    assert receipt["command"]["state"] == "unconfirmed"
    assert not manager.calls


def test_lookup_failure_has_no_manufactured_command(manager, monkeypatch):
    def broken(*args):
        raise sqlite3.OperationalError("fixture unavailable")
    monkeypatch.setattr(manager.store, "snapshot", broken)
    result = manager.reconcile(SID, "command-unconfirmed")
    assert result["reconciliation"]["status"] == "lookup_failed"
    assert "command" not in result


def test_stop_after_outcome_before_failed_finish_is_retained_by_reconcile(manager):
    submit(manager)
    row = manager.store.claim(SID, "owner")
    manager.store.save_outcome(row["command_id"], SID, row["turn_id"], "succeeded",
        {"command_id": row["command_id"], "stopped_reason": "end_turn"}, None)
    manager.store.control(SID, "stop", cid=row["command_id"], revision=row["revision"])
    with manager.store.transaction() as con:
        con.execute("UPDATE agent_command_queues SET lease_until=0 WHERE session_id=?", (SID,))
    result = manager.reconcile(SID, row["command_id"])
    assert result["reconciliation"]["status"] == "matched"
    assert result["command"]["state"] == "interrupted"
    assert result["command"]["result"]["stopped_reason"] == "cancelled"


@pytest.mark.parametrize("overrides", [{}, {"model_provider": "explicit-provider", "model_id": "explicit-model"}])
def test_default_model_snapshot_does_not_pin_request_fallback(manager, monkeypatch, overrides):
    from nerya.llm.gateway import LLMGateway
    monkeypatch.setattr(LLMGateway, "effective_model_metadata",
        lambda *a, **k: ("default-provider-A", "default-model-A", SimpleNamespace(context_window=1000)))
    command = submit(manager, **overrides)
    with manager.store.transaction() as con:
        request = json.loads(con.execute("SELECT request_json FROM agent_commands WHERE command_id=?", (command["command_id"],)).fetchone()[0])
    assert {key: request[key] for key in ("model_provider", "model_id") if key in request} == overrides
    assert command["context"]["accepted_model"]["provider"] == "default-provider-A"
