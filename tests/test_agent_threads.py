"""Persistent child collaboration: offline tests through the real native loop."""
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from types import SimpleNamespace
import json

import pytest

from nerya.api.routes_teams import _agent_request
from nerya.core.config import Config
from nerya.core.paths import WorkspacePaths
from nerya.subagents.threads import AgentThreadInbox, AgentThreadStore
from nerya.subagents.runtime import DEFAULT_CONTEXT_SCOPE
from nerya.tools.native.agent_collaboration import collaboration_handler
from nerya.tools.types import ToolCall, ToolResult
from test_subagent_native_runtime import Gateway, call, descriptor, final, run, runtime, spec

pytestmark = pytest.mark.smoke


def setup_store(tmp_path):
    return AgentThreadStore(WorkspacePaths(tmp_path))


def begin(store, tmp_path, *, name="analyst", session="session", group="task", scope=DEFAULT_CONTEXT_SCOPE, agent_id="", request=""):
    return store.begin(spec=replace(spec(tmp_path), name=name), payload={"task": "Verify evidence"},
        session_id=session, parent_call_id=request or group, strategy_id=None,
        turn_id="turn", context_scope=scope, agent_id=agent_id)


def test_runtime_reuses_identity_and_full_tool_conversation(tmp_path, monkeypatch):
    inherited = [{"role": "user", "content": "Parent context: timezone is UTC."}]
    monkeypatch.setattr(AgentThreadStore, "parent_context", lambda *_: inherited)
    gateway = Gateway(call(value=7), final("first conclusion"))
    rt = runtime(tmp_path, gateway, [descriptor()])
    first = run(rt, spec(tmp_path), parent_call_id="team")
    store = setup_store(tmp_path)
    row = store.load(first["agent_id"], "session")
    assert row["state"] == "completed"
    assert inherited[0] in gateway.calls[0]["messages"]
    assert "tool_result" in json.dumps(row["transcript"])
    assert "first conclusion" in json.dumps(row["transcript"])
    assert "_transcript" not in first
    next_gateway = Gateway(final("second conclusion"))
    rt.llm = next_gateway
    second = run(rt, replace(spec(tmp_path), prompt="different role must not replace the saved role"),
                parent_call_id="followup", agent_id=first["agent_id"], continuation_text="Check the previous result again.")
    assert second["agent_id"] == first["agent_id"]
    assert len(store.list("session")) == 1
    assert store.load(first["agent_id"], "session")["attempt"] == 2
    messages = json.dumps(next_gateway.calls[0]["messages"])
    assert "first conclusion" in messages and "tool_result" in messages
    assert "Check the previous result again." in messages
    assert "different role must not replace" not in messages
    assert store.restore_spec(store.load(first["agent_id"], "session")).execution_policy == spec(tmp_path).execution_policy
    events = store.detail(first["agent_id"], "session")["events"]
    assert [e["data"]["text"] for e in events if e["kind"] == "instruction"] == ["verify result", "Check the previous result again."]
    outputs = [e["data"]["output"]["summary"] for e in events if e["kind"] == "completed"]
    assert outputs == ["first conclusion", "second conclusion"]
    assert any(e["kind"] == "text" for e in events)
    assert not any(e["kind"] in {"thinking", "system"} for e in events)
    assert "Parent context: timezone is UTC." not in json.dumps(events)


def test_real_peer_tool_and_next_loop_boundary_injection(tmp_path):
    store = setup_store(tmp_path)
    peer = begin(store, tmp_path, name="peer")
    store.finish(peer, state="completed", transcript=[{"role": "user", "content": "Peer history"}])
    config = Config(paths=WorkspacePaths(tmp_path), data={})
    desc = descriptor("subagent_message", handler=lambda c: collaboration_handler(c, config=config, send=True))
    gateway = Gateway(call("subagent_message", "peer-message", to=peer["id"], message="Use the verified figure 42."), final())
    result = run(runtime(tmp_path, gateway, [desc]), spec(tmp_path), parent_call_id="task")
    mail = store.detail(peer["id"], "session")["messages"][0]
    assert mail["sender"] == result["agent_id"]
    assert mail["status"] == "queued"
    observed = []
    def inject_during_tool(c):
        own = store.list("session")
        target = next(a for a in own if a["name"] == "peer")
        store.send(session_id="session", sender=result["agent_id"], recipient=target["id"],
                   content="The second finding arrived mid-run.", message_id="mid-run")
        return ToolResult.from_json(tool_use_id=c.id, name=c.name, data={"ok": True})
    peer_gateway = Gateway(call(), final())
    original_call = peer_gateway.call_messages
    def capture(**kwargs):
        observed.append(json.loads(json.dumps(kwargs["messages"])))
        return original_call(**kwargs)
    peer_gateway.call_messages = capture
    rt = runtime(tmp_path, peer_gateway, [descriptor(handler=inject_during_tool)])
    rt.run(replace(spec(tmp_path), name="peer"), trigger_event_id=None, payload={}, session_id="session",
           agent_id=peer["id"], parent_call_id="continue-peer", continuation_text="Continue your research.",
           context_scope=DEFAULT_CONTEXT_SCOPE)
    assert "Use the verified figure 42." in json.dumps(observed[0])
    assert "The second finding arrived mid-run." not in json.dumps(observed[0])
    assert "The second finding arrived mid-run." in json.dumps(observed[1])
    injected = [m for m in observed[1] if "collaborator message" in str(m.get("content"))]
    assert injected and all(not m.get("pinned") for m in injected)
    assert all("operator steer" not in str(m["content"]) for m in injected)
    assert {m["status"] for m in store.detail(peer["id"], "session")["messages"]} == {"consumed"}


def test_fifo_idempotency_and_unacknowledged_redelivery(tmp_path):
    store = setup_store(tmp_path)
    row = begin(store, tmp_path)
    for i in range(3):
        first = store.send(session_id="session", sender="operator", recipient=row["id"], content=f"finding-{i}", message_id=f"m{i}")
        assert first == store.send(session_id="session", sender="operator", recipient=row["id"], content=f"finding-{i}", message_id=f"m{i}")
    inbox = AgentThreadInbox(store, row)
    batch = inbox.drain()
    assert len(batch) == 3 and all(f"finding-{i}" in item for i, item in enumerate(batch))
    assert inbox.drain() == []
    store.finish(row, state="failed", error="crashed before saving transcript")
    resumed = begin(store, tmp_path, agent_id=row["id"], request="retry")
    assert AgentThreadInbox(store, resumed).drain() == batch
    store.finish(resumed, state="completed", transcript=[{"role": "user", "content": batch}])
    again = begin(store, tmp_path, agent_id=row["id"], request="next")
    assert AgentThreadInbox(store, again).drain() == []
    with pytest.raises(ValueError, match="different message"):
        store.send(session_id="session", sender="operator", recipient=row["id"], content="changed", message_id="m0")


def test_resume_claim_is_atomic_across_workers(tmp_path):
    store = setup_store(tmp_path)
    row = begin(store, tmp_path)
    store.finish(row, state="completed", transcript=[])
    def claim(i):
        try:
            return begin(setup_store(tmp_path), tmp_path, agent_id=row["id"], request=f"request-{i}")["attempt"]
        except ValueError:
            return "busy"
    with ThreadPoolExecutor(max_workers=6) as pool:
        results = list(pool.map(claim, range(6)))
    assert results.count(2) == 1
    assert results.count("busy") == 5


@pytest.mark.parametrize("access", ["load", "detail", "resume", "message"])
def test_cross_session_access_is_rejected(tmp_path, access):
    store = setup_store(tmp_path)
    row = begin(store, tmp_path)
    with pytest.raises(ValueError, match="conversation"):
        if access == "resume":
            begin(store, tmp_path, agent_id=row["id"], session="other")
        elif access == "message":
            store.send(session_id="other", sender="operator", recipient=row["id"], content="secret", message_id="cross")
        else:
            getattr(store, access)(row["id"], "other")
    assert store.list("other") == []


def test_other_task_and_explicit_payload_are_isolated(tmp_path, monkeypatch):
    store = setup_store(tmp_path)
    monkeypatch.setattr(store, "parent_context", lambda _: [{"role": "user", "content": "private"}])
    a = begin(store, tmp_path)
    b = begin(store, tmp_path, name="other", group="other-task")
    isolated = begin(store, tmp_path, name="isolated", scope="explicit_payload_only")
    assert isolated["inherited_context"] == []
    with pytest.raises(ValueError, match="same task"):
        store.send(session_id="session", sender=a["id"], recipient=b["id"], content="no", message_id="cross-task")
    with pytest.raises(ValueError, match="isolated"):
        store.send(session_id="session", sender="operator", recipient=isolated["id"], content="no", message_id="isolated")


def test_operation_audit_and_public_projection(tmp_path):
    gateway = Gateway(call(path="README.md"), final())
    rt = runtime(tmp_path, gateway, [descriptor()])
    result = run(rt, spec(tmp_path))
    store = setup_store(tmp_path)
    detail = store.detail(result["agent_id"], "session")
    assert any(e["kind"] == "tool_use" and "README.md" in json.dumps(e["data"]) for e in detail["events"])
    assert any(e["kind"] == "tool_result" for e in detail["events"])
    assert not {"transcript", "inherited_context", "payload", "spec"} & detail["agent"].keys()
    for i in range(105):
        store.event(store.load(result["agent_id"], "session"), "tool_use", {"index": i})
    latest = store.detail(result["agent_id"], "session")
    assert latest["has_more"] and len(latest["events"]) == 100
    old = store.detail(result["agent_id"], "session", before=latest["events"][0]["seq"])
    assert old["events"][-1]["seq"] < latest["events"][0]["seq"]


def test_http_sender_is_operator_and_scope_is_required(tmp_path):
    client = SimpleNamespace(config=Config(paths=WorkspacePaths(tmp_path), data={}))
    row = begin(setup_store(tmp_path), tmp_path)
    response = _agent_request(client, {"session_id": "session", "agent_id": row["id"],
        "sender": "spoofed-peer", "message": "operator note", "request_id": "request"}, "message")
    assert response["message"]["sender"] == "operator"
    assert _agent_request(client, {}, "list")["ok"] is False


def test_dashboard_continuation_uses_existing_permission_boundary(tmp_path, monkeypatch):
    from nerya.subagents.control import continue_agent
    from nerya.subagents.dispatcher import SubAgentDispatcher
    from nerya.tools import PermissionMode
    row = begin(setup_store(tmp_path), tmp_path)
    store = setup_store(tmp_path)
    store.finish(row, state="completed", transcript=[])
    seen = {}
    def dispatch(self, target, **kwargs):
        seen.update(target=target, kwargs=kwargs, executor=self.executor)
        return {"ok": True}
    monkeypatch.setattr(SubAgentDispatcher, "dispatch", dispatch)
    client = SimpleNamespace(config=Config(paths=WorkspacePaths(tmp_path), data={}), skills=None)
    result = continue_agent(client, session_id="session", agent_id=row["id"], message="Continue", request_id="same")
    assert result["ok"]
    assert seen["kwargs"]["agent_id"] == row["id"]
    assert seen["executor"].permission_context.mode == PermissionMode.DEFAULT
    assert seen["executor"].approval_resolver is not None
