"""Coordinator ingress/publication through real commands, tools and a temporary DB.

Only model responses and child scheduling are replaced. Receipt fault injection
uses the executor's post-tool hook, after the real producer has run.
"""
from copy import deepcopy
from concurrent.futures import ThreadPoolExecutor
import json
import socket
from threading import Barrier
import traceback
from types import SimpleNamespace

import pytest

from nerya.agent import command_runtime, streaming
from nerya.agent.assistant_intent import assistant_request_digest
from nerya.agent.assistant_work import AssistantWork
from nerya.agent.assistants import AssistantStore
from nerya.agent.command_runtime import CommandRuntime
from nerya.agent.kernel import AgentKernel
from nerya.api import routes_agent
from nerya.core.config import Config, DEFAULT_CONFIG
from nerya.core.paths import WorkspacePaths
from nerya.harness.cancellation import CancelToken, signal_cancel
from nerya.llm.adapters._base import _IO_CONTROL
from nerya.llm.gateway import LLMCall, LLMGateway
from nerya.llm.messages import MessagesResponse
from nerya.sdk.internal_client import InternalClient
from nerya.tools.result_contracts import tool_json_data
from nerya.tools.types import ToolResult

pytestmark = pytest.mark.smoke

FAKE_COMPLETION = "PROVIDER_CANARY: fabricated task completed with 999% profit."


def classification(disposition="task_create", target=None):
    return {"disposition": disposition, "title": "Research task",
            "target_task_id": target, "language": "en", "clarification": ""}


@pytest.fixture
def runner(tmp_path, monkeypatch):
    config = Config(WorkspacePaths(tmp_path), deepcopy(DEFAULT_CONFIG))
    for tier in config.data["llm"]["tiers"].values():
        tier.update(provider="openai", model="gpt-4o-mini")
    config.data["agent"]["native"].update(
        tier="medium", max_iterations=2, max_total_tool_calls=2, max_wall_seconds=20)
    monkeypatch.delenv("NERYA_PERMISSION_MODE", raising=False)

    def no_network(*args, **kwargs):
        pytest.fail("Integration fixture must not contact live services")

    monkeypatch.setattr(socket.socket, "connect", no_network)
    bus = streaming.StreamingEventBus()
    monkeypatch.setattr(streaming, "_default_bus", bus)
    assistants = AssistantStore(config.paths)
    sid = assistants.conversation("main", title="Coordinator regression")["session_id"]
    client = InternalClient.from_config(config)
    handler = next(h for _, path, h in routes_agent.routes()
                   if path == "/agent/run_turn_internal")
    env = SimpleNamespace(config=config, sid=sid, assistants=assistants, bus=bus,
                          classifications={}, classified=[], model_calls=[], tokens={},
                          before_classify=None, errors=[])

    def execute(run_config, request):
        token = request.get("_task_cancel_token")
        assert isinstance(token, CancelToken)
        env.tokens[request["turn_id"]] = token
        try:
            return handler(SimpleNamespace(config=run_config, skills=client.skills), request)
        except Exception:
            env.errors.append(traceback.format_exc())
            raise

    env.manager = CommandRuntime(config, execute, epoch="assistant-runner-tests")
    monkeypatch.setattr(env.manager, "kick", lambda _sid: None)
    monkeypatch.setattr(command_runtime, "runtime", lambda _config: env.manager)
    env.work = AssistantWork(config.paths)

    def gateway_call(gateway, **kwargs):
        if kwargs["caller"] == "assistant_intent":
            source = json.loads(kwargs["prompt"].split("\nInput: ", 1)[1])
            env.classified.append(source)
            if env.before_classify:
                env.before_classify(gateway.config, source)
            value = env.classifications.get(source["text"], classification())
        else:
            assert kwargs["task"] == "auto_session_title", kwargs["task"]
            value = {"title": "Coordinator regression"}
        return LLMCall(tier=kwargs["tier"], task=kwargs["task"], caller=kwargs["caller"],
                       tokens=1, usd=0, raw=json.dumps(value), parsed=value,
                       provider="openai", model="gpt-4o-mini")

    def gateway_messages(_gateway, **kwargs):
        if kwargs["caller"] == "assistant_intent":
            content = kwargs["messages"][0]["content"]
            if isinstance(content, list):
                content = "".join(part.get("text", "") for part in content)
            source = json.loads(content.split("\nInput: ", 1)[-1])
            env.classified.append(source)
            if env.before_classify:
                env.before_classify(_gateway.config, source)
            value = env.classifications.get(source["text"], classification())
            return MessagesResponse(content=[{"type": "text", "text": json.dumps(value)}],
                                    stop_reason="end_turn", provider="openai", model="gpt-4o-mini",
                                    usage={"input_tokens": 1, "output_tokens": 1})
        env.model_calls.append(kwargs)
        return MessagesResponse(content=[{"type": "text", "text": FAKE_COMPLETION}],
                                stop_reason="end_turn", provider="openai",
                                model="gpt-4o-mini",
                                usage={"input_tokens": 1, "output_tokens": 1})

    monkeypatch.setattr(LLMGateway, "call", gateway_call)
    monkeypatch.setattr(LLMGateway, "call_messages", gateway_messages)
    return env


def submit(env, cid, text, *, scopes=("read:funds",), **request):
    return env.manager.submit({
        "command_id": cid, "session_id": env.sid, "_auth_actor_id": "owner",
        "_auth_scope": "execute:strategy", "_auth_scopes": list(scopes),
        "request": {"source": "user_chat", "payload": {"text": text}, **request},
    }, start=False)["command"]


def run(env, cid):
    owner = "worker-" + cid
    row = env.manager.store.claim(env.sid, owner)
    assert row and row["command_id"] == cid
    env.manager._run(row, owner)
    assert not signal_cancel(row["turn_id"]), "_run leaked its registered token"
    assert not env.errors, "\n".join(env.errors)
    return env.manager.store.snapshot(env.sid, cid)["command"]


def durable_request(env, cid):
    with env.manager.store.transaction() as con:
        row = con.execute("SELECT request_json FROM agent_commands WHERE command_id=?",
                          (cid,)).fetchone()
    return json.loads(row[0])


def assert_quarantined(env, command):
    """Inspect every publication surface, including the pre-final bus replay."""
    assert not env.model_calls, "Task/clarification lane entered the generative kernel"
    cid, tid = command["command_id"], command["turn_id"]
    surfaces = {"bus": env.bus.recent(),
                "replay": env.manager.store.events(env.sid, cid)["events"],
                "command": command}
    with env.manager.store.transaction() as con:
        for table in ("agent_messages", "agent_turn_checkpoints"):
            surfaces[table] = [dict(row) for row in con.execute(
                f"SELECT * FROM {table} WHERE session_id=?", (env.sid,))]
        stored = dict(con.execute("SELECT * FROM agent_commands WHERE command_id=?", (cid,)).fetchone())
    for name, value in surfaces.items():
        assert FAKE_COMPLETION not in json.dumps(value, ensure_ascii=False), name
    assert "_task_cancel_token" not in json.dumps(stored)
    assert isinstance(env.tokens[tid], CancelToken)


def verified(env, command):
    assert_quarantined(env, command)
    assert command["state"] == "succeeded", command
    result = command["result"]
    receipt = result["assistant_receipt"]
    assert result["publication"]["receipt_verified"] is True
    assert receipt["verified"] is True
    contract = command["context"]["assistant_request"]
    assert result["assistant_request"] == contract
    assert receipt["binding"] == contract["binding"] == {
        "command_id": command["command_id"], "session_id": env.sid,
        "turn_id": command["turn_id"], "actor_id": "owner",
        "input_digest": assistant_request_digest(durable_request(env, command["command_id"])),
    }
    with env.manager.store.transaction() as con:
        message = con.execute("SELECT content,meta_json FROM agent_messages WHERE message_id=?",
                              (command["turn_id"] + ":assistant",)).fetchone()
    assert message["content"] == result["final_text"]
    assert json.loads(message["meta_json"])["turn"]["assistant_receipt"] == receipt
    for events in (env.bus.recent(), env.manager.store.events(env.sid, command["command_id"])["events"]):
        assert any(e.get("turn_id") == command["turn_id"]
                   and e.get("text") == result["final_text"] for e in events)
    return receipt


def test_concurrent_creates_preserve_original_input_scopes_and_publication_isolation(runner):
    env = runner
    prompt = "  Inspect BTC/USDT.\nKeep these exact instructions; do not enable trading.  \n"
    attachments = [{"id": "source-note", "name": "notes.txt", "mime_type": "text/plain",
                    "artifact_uri": "nerya://artifact/attachments/notes.txt"}]
    submit(env, "create-original", prompt, _auth_scopes=["api:all"],
           payload={"text": prompt, "attachments": attachments})
    other = SimpleNamespace(**vars(env))
    other.sid = env.assistants.conversation("main", title="Separate coordinator")["session_id"]
    other_prompt = "Inspect ETH independently; keep this request in its own session."
    submit(other, "create-concurrent", other_prompt, scopes=("execute:strategy",))
    rendezvous = Barrier(2)
    env.before_classify = lambda *_: rendezvous.wait(timeout=10)
    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(run, env, "create-original")
        second = pool.submit(run, other, "create-concurrent")
        command, other_command = first.result(timeout=20), second.result(timeout=20)
    receipt = verified(env, command)
    other_receipt = verified(other, other_command)
    for key in ("task_id", "task_session_id", "command_id"):
        assert receipt[key] != other_receipt[key]
    other_child = durable_request(other, other_receipt["command_id"])
    assert other_child["payload"]["text"] == other_prompt
    assert other_child["_auth_scopes"] == ["execute:strategy"]
    for local, completed, foreign in ((env, command, other_receipt), (other, other_command, receipt)):
        replay = local.manager.store.events(local.sid, completed["command_id"])["events"]
        assert replay and all(event["session_id"] == local.sid for event in replay)
        assert all(event["turn_id"] == completed["turn_id"] for event in replay)
        with env.manager.store.transaction() as con:
            messages = [dict(row) for row in con.execute(
                "SELECT * FROM agent_messages WHERE session_id=?", (local.sid,))]
        surface = json.dumps([completed, replay, messages])
        assert foreign["command_id"] not in surface
        assert foreign["task_id"] not in surface
    child = durable_request(env, receipt["command_id"])
    assert child["payload"]["attachments"] == attachments
    assert child["_auth_actor_id"] == "owner"
    assert child["_auth_scopes"] == ["read:funds"]
    assert child["payload"]["assistant_request_binding"] == receipt["binding"]
    assert receipt["state"] == "queued" and not receipt["task_complete"]
    assert len(env.work.tasks(sid=env.sid)) == 1
    assert child["payload"]["text"] == prompt


def test_conversation_runs_normal_kernel_without_task_writing_tools(runner):
    # A false negative is safe from task writes; classifier accuracy is a
    # separate model evaluation and is not established by this fixture.
    env = runner
    prompt = "What is the difference between paper and live trading?"
    env.classifications[prompt] = classification("conversation")
    submit(env, "ordinary-question", prompt)
    command = run(env, "ordinary-question")
    assert command["state"] == "succeeded", command
    assert command["result"]["final_text"] == FAKE_COMPLETION
    assert env.model_calls
    for call in env.model_calls:
        names = {tool["name"] for tool in call["tools"]}
        assert not names & {"assistant_task_create", "assistant_task_message",
                            "task_create", "task_update", "task_stop", "subagent_run_async"}
    assert not env.work.tasks(sid=env.sid)
    assert "assistant_receipt_missing" not in json.dumps(command)
    assert "_task_cancel_token" not in json.dumps(durable_request(env, command["command_id"]))


def test_child_execution_does_not_inherit_parent_publication_contract(runner):
    env = runner
    submit(env, "parent-bound", "Prepare a report")
    parent = run(env, "parent-bound")
    receipt = verified(env, parent)
    env.manager.config.data.setdefault("runtime", {})["assistant_request"] = parent["result"]["assistant_request"]
    row = env.manager.store.claim(receipt["task_session_id"], "child-worker")
    assert row is not None
    env.manager._run(row, "child-worker")
    child = env.manager.store.snapshot(receipt["task_session_id"], receipt["command_id"])["command"]
    assert child["state"] == "succeeded", child
    assert env.model_calls
    assert not env.errors
    assert "assistant_request" not in child["result"]


def test_delivered_followup_retains_independent_collaboration_tools(runner):
    env = runner
    submit(env, "followup-source", "Make a report")
    receipt = verified(env, run(env, "followup-source"))
    with env.manager.store.transaction() as con:
        con.execute("UPDATE agent_commands SET state='succeeded' WHERE command_id=?", (receipt["command_id"],))
    prompt = "Tell the report task: only say received."
    env.classifications[prompt] = classification("task_follow_up", receipt["task_id"])
    submit(env, "followup-targeted", prompt)
    followup = verified(env, run(env, "followup-targeted"))
    row = env.manager.store.claim(receipt["task_session_id"], "delivered-worker")
    assert row["command_id"] == followup["command_id"]
    env.manager._run(row, "delivered-worker")
    names = {tool["name"] for tool in env.model_calls[-1]["tools"]}
    assert "assistant_task_message" in names
    assert {"assistant_task_list", "assistant_task_read"} <= names


def test_invalid_classification_never_publishes_model_claims_or_creates_work(runner):
    env = runner
    prompt = "Inspect public evidence"
    env.classifications[prompt] = {**classification(), "unexpected": FAKE_COMPLETION}
    submit(env, "invalid-classification", prompt)
    command = run(env, "invalid-classification")
    assert_quarantined(env, command)
    assert command["state"] != "succeeded"
    assert command["context"]["assistant_request"]["disposition"] == "clarify"
    assert not command["result"].get("assistant_receipt")
    assert not command["result"].get("tool_trace")
    assert not env.work.tasks(sid=env.sid)


@pytest.mark.parametrize("fault", ["fake_identity", "orphan_admission"])
def test_unverified_receipt_cannot_publish_success(runner, monkeypatch, fault):
    env = runner
    original_bind = AgentKernel.bind_tool_executor
    injected = []

    def corrupt_after_real_tool(call, result):
        if call.name != "assistant_task_create":
            return
        assert not result.is_error
        data = tool_json_data(result)
        injected.append(data["command_id"])
        if fault == "orphan_admission":
            with env.manager.store.transaction() as con:
                con.execute("DELETE FROM agent_commands WHERE command_id=?", (data["command_id"],))
        else:
            data["command_id"] = "fabricated-command-id"
        data.update(final_text=FAKE_COMPLETION, state="succeeded")
        result.content = ToolResult.from_json(tool_use_id=call.id, name=call.name, data=data).content

    def bind(kernel, executor):
        original_bind(kernel, executor)
        executor.add_post_hook(corrupt_after_real_tool)

    monkeypatch.setattr(AgentKernel, "bind_tool_executor", bind)
    submit(env, "unverified-receipt", "Inspect evidence without trading")
    command = run(env, "unverified-receipt")
    assert injected, command
    assert_quarantined(env, command)
    assert command["state"] != "succeeded"
    result = command["result"]
    assert not result["assistant_receipt"]
    assert result["publication"]["receipt_verified"] is False
    assert result["publication"]["error_code"].startswith("assistant_receipt_")
    assert all(not item["ok"] for item in result["tool_trace"])
    assert "admission confirmed" not in result["final_text"]


def test_follow_up_has_exact_owned_receipt_and_read_uses_current_command(runner):
    env = runner
    submit(env, "initial-task", "Inspect initial evidence", scopes=("api:all",))
    initial = verified(env, run(env, "initial-task"))
    prompt = "Add a source citation and keep paper mode."
    env.classifications[prompt] = classification("task_follow_up", initial["task_id"])
    submit(env, "followup-command", prompt, scopes=("read:funds",))
    command = run(env, "followup-command")
    receipt = verified(env, command)
    assert receipt["task_id"] == initial["task_id"]
    assert receipt["task_session_id"] == initial["task_session_id"]
    assert receipt["command_id"] != initial["command_id"]
    child = durable_request(env, receipt["command_id"])
    assert child["payload"]["text"] == prompt
    assert child["assistant_request_binding"] == receipt["binding"]
    with env.manager.store.transaction() as con:
        audit = con.execute("SELECT * FROM assistant_task_messages").fetchall()
        assert len(audit) == 1
        assert audit[0]["command_id"] == receipt["command_id"]
        assert audit[0]["target_task_id"] == initial["task_id"]
        assert audit[0]["sender_session_id"] == env.sid
        assert audit[0]["message"] == prompt
    read_prompt = "What is the current status of that task?"
    submit(env, "read-followup", read_prompt, assistant_intent={
        "disposition": "task_read", "target_task_id": initial["task_id"]})
    current = verified(env, run(env, "read-followup"))
    assert len(env.classified) == 2, "Explicit read should bypass classification"
    assert current["command_id"] == receipt["command_id"]
    assert current["binding"]["command_id"] == "read-followup"
    assert current["state"] == "queued" and not current["task_complete"]
    assert len(env.work.tasks(sid=env.sid)) == 1
    assert child["_auth_scopes"] == ["read:funds"]


def test_queued_edit_digest_and_current_profile_policy_at_execution(runner):
    env = runner
    queued = submit(env, "edited-command", "Original request")
    before = assistant_request_digest(durable_request(env, "edited-command"))
    edited = "Edited request: inspect ETH, preserve paper mode."
    env.manager.control({"action": "edit", "session_id": env.sid,
                         "command_id": queued["command_id"],
                         "expected_revision": queued["revision"],
                         "request": {"payload": {"text": edited}}})
    receipt = verified(env, run(env, "edited-command"))
    assert env.classified[0]["text"] == edited
    assert receipt["binding"]["input_digest"] != before
    assert durable_request(env, receipt["command_id"])["payload"]["text"] == edited

    # A permission revoked after admission must be checked at dispatch.
    submit(env, "denied-after-admission", "Inspect another source")
    profile = env.assistants.get("main")
    env.assistants.save({"tool_policy": {"deny": ["assistant_task_create"]}},
                        aid="main", expected_revision=profile["revision"])
    denied = run(env, "denied-after-admission")
    assert_quarantined(env, denied)
    assert denied["state"] != "succeeded"
    assert denied["result"]["publication"]["receipt_verified"] is False
    assert denied["result"]["publication"]["error_code"]
    assert denied["result"]["tool_trace"][0]["action"] == "assistant_task_create"
    assert denied["result"]["tool_trace"][0]["ok"] is False
    assert not denied["result"]["assistant_receipt"]
    assert len(env.work.tasks(sid=env.sid)) == 1


@pytest.mark.parametrize("blocked", ["operator_pause", "unconfirmed"])
def test_follow_up_preserves_manual_pause_and_unconfirmed_target(runner, blocked):
    env = runner
    submit(env, "blocked-initial", "Inspect initial evidence")
    initial = verified(env, run(env, "blocked-initial"))
    if blocked == "operator_pause":
        queue = env.manager.store.snapshot(initial["task_session_id"])["queue"]
        env.manager.store.control(initial["task_session_id"], "pause", revision=queue["revision"])
    else:
        with env.manager.store.transaction() as con:
            con.execute("UPDATE agent_commands SET state='unconfirmed' WHERE command_id=?",
                        (initial["command_id"],))
    prompt = "Continue the existing task"
    env.classifications[prompt] = classification("task_follow_up", initial["task_id"])
    submit(env, "blocked-followup", prompt)
    command = run(env, "blocked-followup")
    assert_quarantined(env, command)
    assert command["state"] != "succeeded"
    assert command["result"]["publication"]["receipt_verified"] is False
    expected = "assistant_task_paused" if blocked == "operator_pause" else "execution_unconfirmed"
    assert expected in command["result"]["publication"]["error_code"]
    assert ("paused" if blocked == "operator_pause" else "unconfirmed") in command["result"]["final_text"].lower()
    target = env.manager.store.snapshot(initial["task_session_id"])
    assert len(target["commands"]) == 1
    if blocked == "operator_pause":
        assert target["queue"]["paused"] and target["queue"]["pause_reason"] == "operator"
    else:
        assert target["commands"][0]["state"] == "unconfirmed"
    with env.manager.store.transaction() as con:
        assert con.execute("SELECT COUNT(*) FROM assistant_task_messages").fetchone()[0] == 0


def test_stop_during_classification_uses_registered_token_and_never_publishes(runner):
    env = runner
    stopped = []

    def stop_classification(config, _source):
        tid = config.get("runtime.command_turn_id")
        token, _deadline = _IO_CONTROL.get()
        assert token is env.tokens[tid]
        current = env.manager.store.snapshot(env.sid, "cancel-ingress")["command"]
        env.manager.control({"action": "stop", "session_id": env.sid,
                             "command_id": current["command_id"],
                             "expected_revision": current["revision"]})
        assert signal_cancel(tid, reason="operator_cancel")
        assert token.is_set
        stopped.append(tid)

    env.before_classify = stop_classification
    submit(env, "cancel-ingress", "Inspect evidence")
    command = run(env, "cancel-ingress")
    assert stopped == [command["turn_id"]]
    assert_quarantined(env, command)
    assert command["state"] == "interrupted", command
    assert not env.work.tasks(sid=env.sid)
    assert not [event for event in env.bus.recent()
                if event.get("turn_id") == command["turn_id"] and event.get("text")]
