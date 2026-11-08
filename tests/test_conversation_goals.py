"""Goal lifecycle over real durable command admission; no provider/network."""

import json
from concurrent.futures import ThreadPoolExecutor

import pytest

from test_conversation_commands import config, payload, wait_for
from nerya.agent.command_runtime import CommandRuntime
from nerya.agent.command_store import CommandError
from nerya.agent.goals import (
    model_update,
    account_turn,
    enqueue_if_idle,
    control,
    recover,
)

pytestmark = pytest.mark.smoke


def start(runtime, *, budget=None):
    request = payload()
    request["request"]["work_mode"] = "goal"
    if budget is not None:
        request["request"]["goal_token_budget"] = budget
    return runtime.submit(request, start=False)["command"]


def goal(runtime):
    return runtime.store.snapshot("conversation-1")["goal"]


def finish(runtime, *, result=None, state="succeeded", error=None):
    row = runtime.store.claim("conversation-1", "worker")
    assert row
    runtime.store.finish(
        row["command_id"],
        "conversation-1",
        "worker",
        state,
        result
        or {
            "final_text": "Progress checked",
            "stopped_reason": "end_turn",
            "budget": {"input_tokens_total": 3, "output_tokens_total": 2},
        },
        error,
    )
    account_turn(runtime.store, row["command_id"])
    return row


def test_goal_is_durable_and_accounting_idempotent(tmp_path):
    runtime = CommandRuntime(config(tmp_path), lambda *_: {}, epoch="a")
    start(runtime, budget=50)
    first = goal(runtime)
    assert first["status"] == "active" and first["token_budget"] == 50
    command = finish(runtime)
    account_turn(runtime.store, command["command_id"])
    assert goal(runtime)["tokens_used"] == 5
    reopened = CommandRuntime(runtime.config, lambda *_: {}, epoch="b")
    assert goal(reopened)["goal_id"] == first["goal_id"]
    assert goal(reopened)["tokens_used"] == 5


def test_completed_turn_is_not_completed_goal_and_idle_enqueue_is_exactly_once(
    tmp_path,
):
    runtime = CommandRuntime(config(tmp_path), lambda *_: {}, epoch="a")
    start(runtime)
    finish(runtime)
    with ThreadPoolExecutor(max_workers=4) as pool:
        answers = list(
            pool.map(
                lambda _: enqueue_if_idle(runtime.store, "conversation-1"), range(8)
            )
        )
    assert sum(answers) == 1
    rows = runtime.store.snapshot("conversation-1")["commands"]
    assert (
        len(rows) == 2
        and rows[-1]["state"] == "queued"
        and rows[-1]["show_user"] is False
    )
    assert goal(runtime)["status"] == "active"


@pytest.mark.parametrize(
    "state",
    ["failed", "unconfirmed", "interrupted", "awaiting_input", "awaiting_approval"],
)
def test_unsafe_or_decision_terminal_does_not_autorun(tmp_path, state):
    runtime = CommandRuntime(config(tmp_path), lambda *_: {}, epoch="a")
    start(runtime)
    finish(runtime, state=state, error={"code": "failure"})
    assert not enqueue_if_idle(runtime.store, "conversation-1")
    assert len(runtime.store.snapshot("conversation-1")["commands"]) == 1


def test_budget_limit_is_not_completion(tmp_path):
    runtime = CommandRuntime(config(tmp_path), lambda *_: {}, epoch="a")
    start(runtime, budget=4)
    finish(runtime)
    assert goal(runtime)["status"] == "budget_limited"
    assert not enqueue_if_idle(runtime.store, "conversation-1")


def test_goal_completion_requested_is_finalized_only_after_success(tmp_path):
    runtime = CommandRuntime(config(tmp_path), lambda *_: {}, epoch="a")
    start(runtime)
    row = runtime.store.claim("conversation-1", "worker")
    pending = model_update(
        runtime.store, "conversation-1", row["command_id"], status="complete"
    )
    assert pending["status"] == "active" and pending["completion_requested"]
    runtime.store.finish(
        row["command_id"],
        "conversation-1",
        "worker",
        "failed",
        None,
        {"code": "failure"},
    )
    account_turn(runtime.store, row["command_id"])
    assert goal(runtime)["status"] == "blocked"


def test_incomplete_delivery_cannot_complete_goal(tmp_path):
    runtime = CommandRuntime(config(tmp_path), lambda *_: {}, epoch="a")
    start(runtime)
    row = runtime.store.claim("conversation-1", "worker")
    with runtime.store.transaction() as con:
        con.execute(
            "UPDATE agent_commands SET delivery_json=? WHERE command_id=?",
            (json.dumps({"completion": {"allowed": False}}), row["command_id"]),
        )
    with pytest.raises(CommandError, match="goal_delivery_incomplete"):
        model_update(
            runtime.store, "conversation-1", row["command_id"], status="complete"
        )
    runtime.store.finish(
        row["command_id"],
        "conversation-1",
        "worker",
        "succeeded",
        {"final_text": "partial", "stopped_reason": "end_turn"},
    )
    account_turn(runtime.store, row["command_id"])
    assert enqueue_if_idle(runtime.store, "conversation-1")
    followup = runtime.store.claim("conversation-1", "worker")
    with pytest.raises(CommandError, match="goal_delivery_incomplete"):
        model_update(
            runtime.store, "conversation-1", followup["command_id"], status="complete"
        )


def test_pause_and_revision_control_do_not_resume_unknown_effects(tmp_path):
    runtime = CommandRuntime(config(tmp_path), lambda *_: {}, epoch="a")
    start(runtime)
    finish(runtime)
    first = goal(runtime)
    paused = control(runtime.store, "conversation-1", "pause", first["revision"])
    assert paused["status"] == "paused" and not enqueue_if_idle(
        runtime.store, "conversation-1"
    )
    with pytest.raises(CommandError, match="revision"):
        control(runtime.store, "conversation-1", "resume", first["revision"])
    with runtime.store.transaction() as con:
        con.execute(
            "UPDATE agent_commands SET state='unconfirmed' WHERE session_id='conversation-1'"
        )
    with pytest.raises(CommandError, match="execution_unconfirmed"):
        control(runtime.store, "conversation-1", "resume", paused["revision"])


def test_real_worker_continues_and_stops_after_explicit_verified_completion(tmp_path):
    seen = []
    runtime = None

    def execute(cfg, request):
        seen.append(request)
        if len(seen) == 2:
            model_update(
                runtime.store,
                "conversation-1",
                cfg.get("runtime.command_id"),
                status="complete",
            )
        return {
            "final_text": "Verified output",
            "stopped_reason": "end_turn",
            "budget": {"input_tokens_total": 3, "output_tokens_total": 2},
        }

    runtime = CommandRuntime(config(tmp_path), execute, epoch="a")
    start(runtime)
    runtime.kick("conversation-1")
    wait_for(lambda: goal(runtime)["status"] == "complete")
    assert len(seen) == 2
    assert (
        seen[1]["source"] == "goal_continue" and seen[1]["_auth_actor_id"] == "operator"
    )
    assert goal(runtime)["tokens_used"] == 10


def test_model_cannot_pause_or_replace_unfinished_goal(tmp_path):
    runtime = CommandRuntime(config(tmp_path), lambda *_: {}, epoch="a")
    start(runtime)
    second = payload("command-002", text="unrelated goal")
    second["request"]["work_mode"] = "goal"
    with pytest.raises(CommandError, match="unfinished_goal_exists"):
        runtime.submit(second, start=False)
    row = runtime.store.claim("conversation-1", "worker")
    with pytest.raises(CommandError, match="requires_user"):
        model_update(
            runtime.store, "conversation-1", row["command_id"], status="paused"
        )


def test_three_unchanged_blocker_turns_stop_goal(tmp_path):
    runtime = CommandRuntime(config(tmp_path), lambda *_: {}, epoch="a")
    start(runtime)
    for index in range(3):
        row = runtime.store.claim("conversation-1", "worker")
        model_update(
            runtime.store,
            "conversation-1",
            row["command_id"],
            status="blocked",
            reason="missing input",
        )
        runtime.store.finish(
            row["command_id"],
            "conversation-1",
            "worker",
            "succeeded",
            {"final_text": "Still blocked", "stopped_reason": "end_turn"},
        )
        account_turn(runtime.store, row["command_id"])
        if index < 2:
            assert enqueue_if_idle(runtime.store, "conversation-1")
    assert goal(runtime)["status"] == "blocked"
    assert not enqueue_if_idle(runtime.store, "conversation-1")


def test_recovery_runs_only_never_started_goal_continuation(tmp_path):
    runtime = CommandRuntime(config(tmp_path), lambda *_: {}, epoch="a")
    start(runtime)
    finish(runtime)
    enqueue_if_idle(runtime.store, "conversation-1")
    resumed = CommandRuntime(runtime.config, lambda *_: {}, epoch="b")
    kicks = []
    resumed.kick = kicks.append
    recover(resumed)
    assert kicks == ["conversation-1"]
    row = resumed.store.claim("conversation-1", "worker")
    assert json.loads(row["request_json"])["source"] == "goal_continue"


def test_user_pause_new_input_and_actor_boundaries(tmp_path):
    runtime = CommandRuntime(config(tmp_path), lambda *_: {}, epoch="a")
    start(runtime)
    finish(runtime)
    with pytest.raises(CommandError, match="owner"):
        control(
            runtime.store,
            "conversation-1",
            "pause",
            goal(runtime)["revision"],
            actor="other",
        )
    runtime.submit(payload("command-002", text="new direction"), start=False)
    assert (
        goal(runtime)["status"] == "paused"
        and goal(runtime)["stop_reason"] == "new_user_input"
    )
    assert not enqueue_if_idle(runtime.store, "conversation-1")


def test_new_input_fences_previously_queued_goal_continuation(tmp_path):
    runtime = CommandRuntime(config(tmp_path), lambda *_: {}, epoch="a")
    start(runtime)
    finish(runtime)
    assert enqueue_if_idle(runtime.store, "conversation-1")
    runtime.submit(payload("command-002", text="new direction"), start=False)
    assert (
        runtime.store.claim("conversation-1", "worker")["command_id"] == "command-002"
    )


def test_goal_resume_does_not_clear_independent_queue_pause(tmp_path):
    runtime = CommandRuntime(config(tmp_path), lambda *_: {}, epoch="a")
    start(runtime)
    finish(runtime)
    control(runtime.store, "conversation-1", "pause", goal(runtime)["revision"])
    snapshot = runtime.store.snapshot("conversation-1")
    runtime.store.control(
        "conversation-1", "pause", revision=snapshot["queue"]["revision"]
    )
    with pytest.raises(CommandError, match="queue_pause"):
        control(runtime.store, "conversation-1", "resume", goal(runtime)["revision"])
    assert runtime.store.snapshot("conversation-1")["queue"]["paused"]


def test_restart_accounts_terminal_before_any_continuation(tmp_path):
    runtime = CommandRuntime(config(tmp_path), lambda *_: {}, epoch="a")
    start(runtime, budget=4)
    row = runtime.store.claim("conversation-1", "worker")
    runtime.store.finish(
        row["command_id"],
        "conversation-1",
        "worker",
        "succeeded",
        {
            "final_text": "progress",
            "stopped_reason": "end_turn",
            "budget": {"input_tokens_total": 5},
        },
    )
    restarted = CommandRuntime(runtime.config, lambda *_: {}, epoch="b")
    kicks = []
    restarted.kick = kicks.append
    recover(restarted)
    assert goal(restarted)["status"] == "budget_limited"
    assert goal(restarted)["tokens_used"] == 5 and kicks == []


def test_question_in_goal_continuation_admits_one_authenticated_answer(tmp_path):
    from nerya.agent.interactions import create_interaction, respond
    from nerya.db.repositories import AgentSessionRepository
    runtime=CommandRuntime(config(tmp_path),lambda *_:{},epoch="a")
    start(runtime)
    finish(runtime)
    assert enqueue_if_idle(runtime.store,"conversation-1")
    row=runtime.store.claim("conversation-1","worker")
    question=create_interaction(runtime.config,sid="conversation-1",tid=row["turn_id"],call_id="goal-question",
        kind="question",payload={"title":"Which result?"})
    with runtime.store.transaction() as con:
        AgentSessionRepository(con).save_turn_checkpoint(session_id="conversation-1",turn_id=row["turn_id"],checkpoint={"resumable":True})
    runtime.store.finish(row["command_id"],"conversation-1","worker","awaiting_input",{"stopped_reason":"user_input_pending"})
    account_turn(runtime.store,row["command_id"])
    runtime.kick=lambda _:None
    body={"session_id":"conversation-1","interaction_id":question["interaction_id"],
        "response_id":"goal-answer","expected_revision":1,"_auth_actor_id":"operator","action":"answer","text":"Latest"}
    answer=respond(runtime,body)
    assert answer["ok"]
    retry=respond(runtime,body)
    assert retry["ok"]
    continuation=runtime.store.claim("conversation-1","worker")
    request=json.loads(continuation["request_json"])
    assert request["source"]=="user_chat" and request["resume_turn_id"]==row["turn_id"]
    assert continuation["command_id"]=="response_"+question["interaction_id"]
