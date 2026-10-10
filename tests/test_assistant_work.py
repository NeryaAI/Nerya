from __future__ import annotations

import copy
import threading
import time

import pytest

from nerya.agent import command_runtime
from nerya.agent.assistant_work import AssistantWork
from nerya.agent.assistants import AssistantError, AssistantStore
from nerya.agent.command_runtime import CommandRuntime
from nerya.core.config import Config, DEFAULT_CONFIG
from nerya.core.paths import WorkspacePaths
from nerya.db.repositories import AgentSessionRepository
from nerya.tools.types import ToolCall


@pytest.fixture
def setup(tmp_path, monkeypatch):
    cfg = Config(paths=WorkspacePaths(tmp_path), data=copy.deepcopy(DEFAULT_CONFIG))
    manager = CommandRuntime(cfg, lambda _cfg, request: {"ok": True, "final_text": "Conclusion: " + request["payload"]["text"]})
    monkeypatch.setattr(command_runtime, "runtime", lambda _cfg: manager)
    assistants = AssistantStore(cfg.paths)
    sid = assistants.home("main")["session_id"]
    return cfg, manager, assistants, sid, AssistantWork(cfg.paths)


def call(identifier="one", kind="research"):
    return ToolCall(id=identifier, turn_id="turn-one", name="assistant_task_create", arguments={"title":identifier, "prompt":"Read the evidence and produce a report.", "kind":kind})


def settled(work, sid):
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        rows = work.tasks(sid=sid)
        if rows and all(row["state"] in {"succeeded", "failed", "cancelled", "awaiting_approval", "awaiting_input", "unconfirmed"} for row in rows):
            return rows
        time.sleep(0.02)
    raise AssertionError("task commands did not settle")


def test_home_and_multiple_native_task_conversations_are_separate(setup):
    cfg, manager, assistants, sid, work = setup
    assert assistants.home("main")["session_id"] == sid
    first = work.create(cfg, call(), sid, "owner", manager=manager)
    second = work.create(cfg, call("two", "strategy"), sid, "owner", manager=manager)
    assert len({sid, first["task_session_id"], second["task_session_id"]}) == 3
    rows = settled(work, sid)
    assert len(rows) == 2
    assert {r["meta"]["conversation_kind"] for r in assistants.conversations("main", "task")} == {"task"}
    assert not manager.store.snapshot(sid)["commands"]
    with manager.store.transaction() as con:
        messages = AgentSessionRepository(con).transcript(sid)
    assert len(messages) == 2 and all("Conclusion" in m["content"] for m in messages)
    work.sync(sid); work.sync(sid)
    with manager.store.transaction() as con:
        assert len(AgentSessionRepository(con).transcript(sid)) == 2
    assert len(AssistantWork(cfg.paths).results("main")) == 2
    assert "Conclusion" in work.context(sid)


def test_repeated_creation_and_completion_are_idempotent(setup):
    cfg, manager, _assistants, sid, work = setup
    first = work.create(cfg, call(), sid, "owner", manager=manager)
    assert work.create(cfg, call(), sid, "owner", manager=manager)["task_id"] == first["task_id"]
    settled(work, sid)
    assert len(manager.store.snapshot(first["task_session_id"])["commands"]) == 1
    changed = call(); changed.arguments["prompt"] = "A different goal"
    with pytest.raises(AssistantError, match="idempotency_conflict"):
        work.create(cfg, changed, sid, "owner", manager=manager)
    assert len(work.results("main")) == 1


def test_task_conversation_cannot_delegate_itself_and_other_assistant_is_isolated(setup):
    cfg, manager, assistants, sid, work = setup
    child = work.create(cfg, call(), sid, "owner", manager=manager)["task_session_id"]
    with pytest.raises(AssistantError, match="nested"):
        work.create(cfg, call("nested"), child, "owner", manager=manager)
    other = assistants.save({"name":"Other"})["id"]
    settled(work, sid)
    assert work.results(other) == []
    with pytest.raises(AssistantError, match="owner_mismatch"):
        work.stop(cfg, work.tasks(sid=sid)[0]["task_id"], assistants.home(other)["session_id"])


@pytest.mark.parametrize("result,state", [({"ok":False,"final_text":"Failed evidence"},"failed"), ({"stopped_reason":"user_input_pending"},"awaiting_input"), ({"awaiting_approval":True},"awaiting_approval"), ({"execution_status":"unconfirmed"},"unconfirmed")])
def test_tasks_keep_native_failure_and_decision_states(setup, result, state):
    cfg, manager, _assistants, sid, work = setup
    manager.execute = lambda *_: result
    receipt = work.create(cfg, call(), sid, "owner", manager=manager)
    row = settled(work, sid)[0]
    assert row["state"] == state
    assert manager.store.snapshot(receipt["task_session_id"])["commands"][0]["state"] == state
    if state != "failed": assert work.results("main") == []


def test_never_started_tasks_recover_but_manual_pause_is_preserved(setup, monkeypatch):
    cfg, manager, _assistants, sid, work = setup
    kicked = []; monkeypatch.setattr(manager, "kick", lambda child: kicked.append(child))
    first = work.create(cfg, call(), sid, "owner", manager=manager)
    second = work.create(cfg, call("two"), sid, "owner", manager=manager)
    snapshot = manager.store.snapshot(second["task_session_id"])
    manager.control({"session_id":second["task_session_id"],"action":"pause","expected_revision":snapshot["queue"]["revision"]})
    kicked.clear(); work.recover(manager)
    assert kicked == [first["task_session_id"]]
    assert manager.store.snapshot(second["task_session_id"])["queue"]["paused"]


def test_assistant_is_coordinator_and_task_keeps_execution_tools(setup):
    from nerya.agent.assistants import assistant_config
    from nerya.tools.capability_policy import tool_policy_allows
    cfg, manager, assistants, sid, work = setup
    profile = assistants.get("main")
    coordination = assistant_config(cfg, profile, session_id=sid)
    policy = coordination.get("agent.native.tool_policy")
    for name in ("strategy_draft_proposal", "strategy_submit_proposal", "account_upsert", "read_file", "subagent_run_async", "shell"):
        assert not tool_policy_allows(policy, name)
    assert tool_policy_allows(policy, "assistant_task_create")
    assert tool_policy_allows(policy, "send_message")
    receipt = work.create(coordination, call(), sid, "owner", manager=manager)
    execution = assistant_config(coordination, profile, session_id=receipt["task_session_id"])
    assert not execution.get("agent.native.assistant_coordination_only")
    assert tool_policy_allows(execution.get("agent.native.tool_policy"), "strategy_draft_proposal")
    assert tool_policy_allows(execution.get("agent.native.tool_policy"), "read_file")
    assistants.bind("scheduled-test", "main", source="scheduled_agent")
    scheduled = assistant_config(coordination, profile, session_id="scheduled-test")
    assert tool_policy_allows(scheduled.get("agent.native.tool_policy"), "read_file")


def test_task_inherits_admitted_model_budget_and_goal_and_plan_does_not_execute(setup, monkeypatch):
    cfg, manager, _assistants, sid, work = setup
    monkeypatch.setattr(manager, "kick", lambda _sid: None)
    model = manager.accepted_model({})
    manager.store.accept(cid="parent-goal", sid=sid, actor="owner", kind="send",
        request={"payload":{"text":"Research"},"work_mode":"goal","permission_mode":"default","max_iterations":3},
        context={"accepted_model":model}, turn_id="turn-one")
    receipt = work.create(cfg, call(), sid, "owner", manager=manager)
    with manager.store.transaction() as con:
        import json
        request = json.loads(con.execute("SELECT request_json FROM agent_commands WHERE command_id=?", (receipt["command_id"],)).fetchone()[0])
    assert request["max_iterations"] == 3 and request["work_mode"] == "goal"
    assert request["model_provider"] == model["provider"] and request["model_id"] == model["model"]
    manager.store.accept(cid="parent-plan", sid=sid, actor="owner", kind="send",
        request={"payload":{"text":"Only plan"},"work_mode":"plan"}, context={}, turn_id="plan-turn")
    planned = call("plan"); planned.turn_id = "plan-turn"
    with pytest.raises(AssistantError, match="plan_only"):
        work.create(cfg, planned, sid, "owner", manager=manager)
    assert len(work.tasks(sid=sid)) == 1


def test_task_tools_cannot_read_another_conversations_delegated_results(setup):
    from nerya.subagents.tasks import TaskStore
    from nerya.tools.native.tasks import task_get_handler, task_list_handler
    cfg, manager, _assistants, sid, work = setup
    receipt = work.create(cfg, call(), sid, "owner", manager=manager)
    settled(work, sid)
    query = ToolCall(id="get", name="task_get", arguments={"task_id":receipt["task_id"]})
    store = TaskStore(cfg.paths)
    assert not task_get_handler(query, store=store, owner_session_id=sid).is_error
    assert task_get_handler(query, store=store, owner_session_id="other-conversation").is_error
    listing = ToolCall(id="list", name="task_list", arguments={"session_id":sid})
    assert task_list_handler(listing, store=store, owner_session_id="other-conversation").is_error


def test_run_only_followup_with_lower_queue_position_delivers_success_and_keeps_failure(setup, monkeypatch):
    cfg, manager, _assistants, sid, work = setup
    monkeypatch.setattr(manager, "kick", lambda _sid: None)
    receipt = work.create(cfg, call(), sid, "owner", manager=manager)
    child = receipt["task_session_id"]
    with manager.store.transaction() as con:
        con.execute("UPDATE agent_commands SET state='failed',error_json=? WHERE command_id=?", ('{"code":"model_stream_tls_error"}',receipt["command_id"]))
    work.sync(sid)
    initial = manager.store.snapshot(child)["commands"][0]
    manager.control({"session_id":child,"action":"pause","expected_revision":manager.store.snapshot(child)["queue"]["revision"]})
    manager.submit({"session_id":child,"command_id":"followup-only","request":{"payload":{"text":"Inspect existing proposal"},"run_only":True},"_auth_actor_id":"owner"},start=False)
    with manager.store.transaction() as con:
        con.execute("UPDATE agent_commands SET state='succeeded',result_json=? WHERE command_id='followup-only'", ('{"final_text":"Recovered existing proposal"}',))
        positions=con.execute("SELECT command_id,position FROM agent_commands WHERE session_id=? ORDER BY created_at",(child,)).fetchall()
        assert positions[1]["position"] < positions[0]["position"]
    assert work.tasks(sid=sid)[0]["state"] == "succeeded"
    work.sync(sid);work.sync(sid)
    assert {r["state"] for r in work.results("main")} == {"failed","succeeded"}
    with manager.store.transaction() as con:
        messages=AgentSessionRepository(con).transcript(sid)
    assert len(messages)==2 and any("Recovered existing proposal" in row["content"] for row in messages)
