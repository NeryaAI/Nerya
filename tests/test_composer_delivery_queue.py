"""Composer follow-ups exercise the real store in temporary SQLite workspaces."""
from copy import deepcopy
import json

import pytest

from nerya.agent.command_runtime import CommandRuntime
from nerya.agent.command_store import CommandError, encode
from nerya.agent.interactions import create_interaction, respond
from nerya.core.config import Config, DEFAULT_CONFIG
from nerya.core.paths import WorkspacePaths
from nerya.db.repositories import AgentSessionRepository

SID = "composer-followup"


@pytest.fixture
def runtime(tmp_path, monkeypatch):
    def never_execute(*_):
        raise AssertionError("This fixture must not execute a model or tool")

    rt = CommandRuntime(Config(paths=WorkspacePaths(root=tmp_path), data=deepcopy(DEFAULT_CONFIG)),
                        never_execute, epoch="composer-fixture")
    monkeypatch.setattr(rt, "kick", lambda _: None)
    return rt


def send(rt, cid="command-original", **request):
    return rt.submit({"command_id": cid, "session_id": SID, "_auth_actor_id": "operator",
                      "request": {"payload": {"text": cid}, **request}}, start=False)["command"]


def pause(rt):
    queue = rt.store.snapshot(SID)["queue"]
    rt.store.control(SID, "pause", revision=queue["revision"])


def continuation(rt, kind, *, held=False, operator_pause=None):
    send(rt)
    original = rt.store.claim(SID, "owner")
    if held:
        send(rt, "command-held")
    if operator_pause == "before_wait":
        pause(rt)
    if kind != "approval":
        interaction = create_interaction(rt.config, sid=SID, tid=original["turn_id"],
            call_id="fixture-decision", kind=kind, payload={"title": "Read evidence?", "steps": ["Read"]})
        if kind == "question":
            with rt.store.transaction() as con:
                AgentSessionRepository(con).save_turn_checkpoint(session_id=SID,
                    turn_id=original["turn_id"], checkpoint={"resumable": True})
    state = "awaiting_approval" if kind == "approval" else "awaiting_input"
    rt.store.finish(original["command_id"], SID, "owner", state)
    if operator_pause == "after_wait":
        pause(rt)
    if kind == "approval":
        receipt = rt.resolve_approval({"approval_id": "fixture-approval", "state": "approved",
            "kind": "tool_permission_batch", "session_id": SID, "turn_id": original["turn_id"],
            "requester_actor_id": "operator"}, start=False)
    else:
        receipt = respond(rt, {"session_id": SID, "interaction_id": interaction["interaction_id"],
            "response_id": "fixture-response", "expected_revision": interaction["revision"],
            "_auth_actor_id": "operator", "action": "answer" if kind == "question" else "accept",
            "text": "Read the evidence"})
    resumed = rt.store.claim(SID, "owner")
    assert resumed["command_id"] == receipt["command"]["command_id"]
    if operator_pause == "during_continuation":
        pause(rt)
    rt.store.finish(resumed["command_id"], SID, "owner", "succeeded")
    return resumed


@pytest.mark.parametrize("kind", ["question", "plan", "approval"])
def test_successful_continuation_allows_next_ordinary_send(runtime, kind):
    resumed = continuation(runtime, kind)
    before = runtime.store.snapshot(SID)
    assert all(row["state"] != "queued" for row in before["commands"])
    assert runtime.store.snapshot(SID, resumed["command_id"])["command"]["state"] == "succeeded"
    send(runtime, "command-followup")
    claimed = runtime.store.claim(SID, "followup-owner")
    assert claimed is not None, before["queue"]
    assert claimed["command_id"] == "command-followup"


def _run_only_question_or_plan(runtime, kind):
    """User sent one isolated run from a paused session, then the Agent asked."""
    send(runtime, "command-single", run_only=True)
    original = runtime.store.claim(SID, "owner")
    assert original is not None
    payload = {"title": "Continue the work?", "steps": ["Read"]} if kind == "plan" else {"title": "Which RPC?"}
    interaction = create_interaction(runtime.config, sid=SID, tid=original["turn_id"],
        call_id="single-run-decision", kind=kind, payload=payload)
    if kind == "question":
        with runtime.store.transaction() as con:
            AgentSessionRepository(con).save_turn_checkpoint(session_id=SID,
                turn_id=original["turn_id"], checkpoint={"resumable": True})
    runtime.store.finish("command-single", SID, "owner", "awaiting_input",
                         {"stopped_reason": "user_input_pending"})
    answer = {"session_id": SID, "interaction_id": interaction["interaction_id"],
        "response_id": "run-only-answer", "expected_revision": interaction["revision"],
        "_auth_actor_id": "operator", "action": "answer" if kind == "question" else "accept",
        "text": "Use a public RPC first"}
    return interaction, answer


@pytest.mark.parametrize("kind", ["question", "plan"])
def test_interaction_from_run_only_resumes_without_illegal_run_only_flag(runtime, kind):
    """A response continuation owns run_once; run_only is only for new sends."""
    _, answer = _run_only_question_or_plan(runtime, kind)
    receipt = respond(runtime, answer)
    resumed = runtime.store.claim(SID, "continuation-owner")
    assert resumed is not None and resumed["command_id"] == receipt["command"]["command_id"]
    with runtime.store.transaction() as con:
        request = json.loads(con.execute("SELECT request_json FROM agent_commands WHERE command_id=?",
            (resumed["command_id"],)).fetchone()[0])
        assert not request.get("run_only", False)
        assert con.execute("SELECT state FROM agent_interactions WHERE interaction_id=?",
            (answer["interaction_id"],)).fetchone()[0] == "resolved"
    runtime.store.finish(resumed["command_id"], SID, "continuation-owner", "succeeded")
    send(runtime, "command-followup")
    assert runtime.store.claim(SID, "followup-owner")["command_id"] == "command-followup"


def test_answered_interaction_can_reconcile_from_saved_response(runtime):
    """A failed admission must not strand the accepted answer after reload."""
    interaction, answer = _run_only_question_or_plan(runtime, "question")
    with runtime.store.transaction() as con:
        con.execute("UPDATE agent_interactions SET state='answered',response_id=?,response_json=?,actor_id=?,revision=revision+1 WHERE interaction_id=?",
            (answer["response_id"], encode({"action": "answer", "text": answer["text"], "selected": []}),
             "operator", interaction["interaction_id"]))
    with pytest.raises(CommandError, match="interaction_response_required"):
        send(runtime, "must-not-bypass")
    recovery = {"session_id": SID, "interaction_id": interaction["interaction_id"],
                "_auth_actor_id": "operator", "action": "reconcile"}
    receipt = respond(runtime, recovery)
    assert receipt["ok"]
    assert respond(runtime, recovery)["duplicate"]
    assert runtime.store.claim(SID, "continuation-owner")["command_id"] == receipt["command"]["command_id"]


def test_failed_answer_admission_is_recoverable_without_reanswering(runtime, monkeypatch):
    """Exercise the actual answered-before-admission failure boundary."""
    interaction, answer = _run_only_question_or_plan(runtime, "question")
    submit_original = runtime.submit
    def fail_once(payload, *, start=True):
        if payload.get("command_type") == "resume":
            raise CommandError("invalid_command_request", 400)
        return submit_original(payload, start=start)
    monkeypatch.setattr(runtime, "submit", fail_once)
    with pytest.raises(CommandError, match="invalid_command_request"):
        respond(runtime, answer)
    with runtime.store.transaction() as con:
        assert con.execute("SELECT state FROM agent_interactions WHERE interaction_id=?",
            (interaction["interaction_id"],)).fetchone()[0] == "answered"
        assert con.execute("SELECT count(*) FROM agent_commands WHERE command_id=?",
            ("response_" + interaction["interaction_id"],)).fetchone()[0] == 0
    monkeypatch.setattr(runtime, "submit", submit_original)
    receipt = respond(runtime, {"session_id": SID, "interaction_id": interaction["interaction_id"],
                               "action": "reconcile", "_auth_actor_id": "operator"})
    assert receipt["ok"]
    assert runtime.store.claim(SID, "resume-owner")["command_id"] == receipt["command"]["command_id"]


def test_answered_interaction_reconcile_requires_same_authenticated_actor(runtime):
    interaction, answer = _run_only_question_or_plan(runtime, "question")
    with runtime.store.transaction() as con:
        con.execute("UPDATE agent_interactions SET state='answered',response_id=?,response_json=?,actor_id=?,revision=revision+1 WHERE interaction_id=?",
            (answer["response_id"], encode({"action": "answer", "text": answer["text"], "selected": []}),
             "operator", interaction["interaction_id"]))
    with pytest.raises(CommandError, match="interaction_response_owner_mismatch"):
        respond(runtime, {"session_id": SID, "interaction_id": interaction["interaction_id"],
                          "_auth_actor_id": "different", "action": "reconcile"})
    assert runtime.store.snapshot(SID)["queue"]["paused"]


@pytest.mark.parametrize("kind", ["question", "plan", "approval"])
def test_successful_continuation_preserves_held_queue(runtime, kind):
    continuation(runtime, kind, held=True)
    send(runtime, "command-followup")
    assert runtime.store.claim(SID, "next-owner") is None
    assert runtime.store.snapshot(SID)["queue"]["paused"]
    assert runtime.store.snapshot(SID, "command-held")["command"]["state"] == "queued"


@pytest.mark.parametrize("kind", ["question", "plan", "approval"])
@pytest.mark.parametrize("when", ["before_wait", "after_wait", "during_continuation"])
def test_operator_pause_survives_continuation(runtime, kind, when):
    continuation(runtime, kind, operator_pause=when)
    send(runtime, "command-followup")
    assert runtime.store.claim(SID, "next-owner") is None
    queue = runtime.store.snapshot(SID)["queue"]
    assert queue["paused"] and queue["pause_reason"] == "operator"


@pytest.mark.parametrize("reason", ["operator", "restarted", "unconfirmed", "unknown", "failed", "interrupted", "blocked"])
def test_empty_protected_pause_is_not_automatically_released(runtime, reason):
    send(runtime)
    runtime.store.claim(SID, "owner")
    runtime.store.finish("command-original", SID, "owner", "succeeded")
    with runtime.store.transaction() as con:
        con.execute("UPDATE agent_command_queues SET paused=1,pause_reason=? WHERE session_id=?", (reason, SID))
    send(runtime, "command-followup")
    assert runtime.store.claim(SID, "next-owner") is None


@pytest.mark.parametrize("held", [False, True])
def test_explicit_run_only_keeps_operator_pause_and_older_queue(runtime, held):
    pause(runtime)
    if held:
        send(runtime, "command-held")
    send(runtime, "command-explicit", run_only=True)
    assert runtime.store.claim(SID, "owner")["command_id"] == "command-explicit"
    runtime.store.finish("command-explicit", SID, "owner", "succeeded")
    assert runtime.store.claim(SID, "next-owner") is None
    assert runtime.store.snapshot(SID)["queue"]["pause_reason"] == "operator"


@pytest.mark.parametrize("hold_reason", ["operator", "restarted", "failed", "interrupted", "unknown"])
def test_followup_runs_without_clearing_empty_held_queue(runtime, hold_reason):
    """Composer sends one new turn, never implicitly resumes old queue work."""
    send(runtime)
    original = runtime.store.claim(SID, "original-owner")
    runtime.store.finish(original["command_id"], SID, "original-owner", "succeeded")
    with runtime.store.transaction() as con:
        con.execute("UPDATE agent_command_queues SET paused=1,pause_reason=? WHERE session_id=?",
                    (hold_reason, SID))

    first = send(runtime, "followup-one", run_only=True)
    assert first["state"] == "queued"
    admitted = runtime.store.claim(SID, "followup-owner")
    assert admitted is not None and admitted["command_id"] == "followup-one"
    runtime.store.finish(admitted["command_id"], SID, "followup-owner", "succeeded")
    # The original hold remains authoritative. A new click starts *only* the
    # new follow-up; no operator pause or orphan queue is silently cleared.
    queue = runtime.store.snapshot(SID)["queue"]
    assert queue["paused"] and queue["pause_reason"] == hold_reason
    assert runtime.store.claim(SID, "other-owner") is None

    second = send(runtime, "followup-two", run_only=True)
    assert runtime.store.claim(SID, "second-owner")["command_id"] == second["command_id"]


@pytest.mark.parametrize("reason", ["operator", "unknown", "restarted", "unconfirmed"])
def test_success_cannot_release_a_safety_hold(runtime, reason):
    send(runtime)
    runtime.store.claim(SID, "owner")
    with runtime.store.transaction() as con:
        con.execute("UPDATE agent_command_queues SET paused=1,pause_reason=? WHERE session_id=?", (reason, SID))
    runtime.store.finish("command-original", SID, "owner", "succeeded")
    send(runtime, "command-followup")
    assert runtime.store.claim(SID, "next-owner") is None
    assert runtime.store.snapshot(SID)["queue"]["pause_reason"] == reason


@pytest.mark.parametrize("reason", ["operator", "unknown", "approval_continued"])
def test_restart_does_not_clear_an_empty_pause(runtime, reason):
    pause(runtime)
    with runtime.store.transaction() as con:
        con.execute("UPDATE agent_command_queues SET pause_reason=? WHERE session_id=?", (reason, SID))
    restarted = CommandRuntime(runtime.config, runtime.execute, epoch="after-restart")
    assert restarted.store.snapshot(SID)["queue"]["paused"]
    send(restarted, "command-followup")
    assert restarted.store.claim(SID, "next-owner") is None


@pytest.mark.parametrize("interaction_state", ["pending", "deferred", "answered"])
def test_success_with_unresolved_interaction_keeps_system_pause(runtime, interaction_state):
    send(runtime)
    original = runtime.store.claim(SID, "owner")
    item = create_interaction(runtime.config, sid=SID, tid=original["turn_id"],
        call_id="still-waiting", kind="question", payload={"title": "Choose"})
    with runtime.store.transaction() as con:
        con.execute("UPDATE agent_interactions SET state=? WHERE interaction_id=?", (interaction_state, item["interaction_id"]))
        con.execute("UPDATE agent_command_queues SET paused=1,pause_reason='awaiting_input' WHERE session_id=?", (SID,))
    runtime.store.finish("command-original", SID, "owner", "succeeded")
    assert runtime.store.snapshot(SID)["queue"]["paused"]
    with pytest.raises(CommandError, match="interaction_response_required"):
        send(runtime, "command-followup", run_only=True)


def test_pending_approval_blocks_ordinary_send_and_run_only(runtime):
    send(runtime)
    runtime.store.claim(SID, "owner")
    runtime.store.finish("command-original", SID, "owner", "awaiting_approval")
    send(runtime, "command-followup")
    assert runtime.store.claim(SID, "next-owner") is None
    with pytest.raises(CommandError, match="decision_pending"):
        send(runtime, "command-explicit", run_only=True)


def test_unconfirmed_execution_blocks_ordinary_send_and_run_only(runtime):
    send(runtime)
    runtime.store.claim(SID, "owner")
    with runtime.store.transaction() as con:
        con.execute("UPDATE agent_command_queues SET lease_until=0 WHERE session_id=?", (SID,))
    assert runtime.store.snapshot(SID, "command-original")["command"]["state"] == "unconfirmed"
    send(runtime, "command-followup")
    assert runtime.store.claim(SID, "next-owner") is None
    with pytest.raises(CommandError, match="execution_unconfirmed"):
        send(runtime, "command-explicit", run_only=True)
