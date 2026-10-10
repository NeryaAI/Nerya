from __future__ import annotations

import copy
import json
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import pytest

from nerya.agent.assistants import AssistantError, AssistantStore, assistant_config, current_assistant_policy, prepare_request
from nerya.agent.command_runtime import CommandRuntime
from nerya.api import routes_assistants, routes_gateway
from nerya.api.route_scopes import required_scope
from nerya.core.config import Config, DEFAULT_CONFIG
from nerya.core.paths import WorkspacePaths
from nerya.core import yaml_io
from nerya.messaging.platforms import list_platforms
from nerya.tools.capability_policy import tool_policy_allows, normalise_tool_policy

pytestmark = pytest.mark.smoke


@pytest.fixture
def config(tmp_path):
    return Config(paths=WorkspacePaths(tmp_path), data=copy.deepcopy(DEFAULT_CONFIG))


def test_profile_validation_persistence_revision_and_model(config):
    store = AssistantStore(config.paths)
    assert [profile["id"] for profile in store.list()] == ["main"]
    profile = store.save({"name": "Scout", "instructions": "Reply as Scout.", "model_provider": "fixture", "model_id": "specialist"})
    assert AssistantStore(config.paths).get(profile["id"]) == profile
    updated = store.save({"role": "Research"}, aid=profile["id"], expected_revision=profile["revision"])
    assert updated["instructions"] == profile["instructions"]
    with pytest.raises(AssistantError, match="revision_conflict"):
        store.save({"role": "Old"}, aid=profile["id"], expected_revision=profile["revision"])
    for fields in ({"name": ""}, {"name": "A", "enabled": "false"}, {"name": "A", "model_id": "x"}, {"name": "A", "unknown": 1}):
        with pytest.raises(ValueError):
            store.save(fields)


def test_conversations_immutable_isolated_and_survive_profile_edits(config):
    store = AssistantStore(config.paths)
    a, b = (store.save({"name": name}) for name in ("Scout", "Writer"))
    first, second = store.conversation(a["id"]), store.conversation(b["id"])
    assert first["session_id"] != second["session_id"]
    with pytest.raises(AssistantError, match="assistant_session_mismatch"):
        store.bind(first["session_id"], b["id"])
    assert len(store.conversations(a["id"])) == 1
    store.save({"name": "New Scout"}, aid=a["id"], expected_revision=a["revision"])
    assert store.for_session(first["session_id"])["name"] == "New Scout"
    assert store.for_session(second["session_id"])["name"] == "Writer"


def test_concurrent_binding_has_one_owner(config):
    store = AssistantStore(config.paths)
    a, b = (store.save({"name": name}) for name in ("A", "B"))
    def bind(aid):
        try:
            return store.bind("shared-session", aid)["id"]
        except AssistantError as exc:
            return exc.code
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(bind, [a["id"], b["id"]]))
    assert results.count("assistant_session_mismatch") == 1
    assert store.for_session("shared-session")["id"] in (a["id"], b["id"])


def test_policy_ceiling_composes_and_revocation_is_live(config):
    store = AssistantStore(config.paths)
    profile = store.save({"name": "Limited", "shell_allowed": False, "memory_allowed": False,
        "tool_policy": {"allow": ["read_file", "web_search", "mcp_*"], "deny": ["mcp_delete"]}})
    sid = store.conversation(profile["id"])["session_id"]
    config.data["agent"]["native"]["tool_policy"] = {"deny": ["mcp_send"]}
    effective = assistant_config(config, profile)
    policy = normalise_tool_policy(effective.get("agent.native.tool_policy"))
    assert tool_policy_allows(policy, "read_file")
    for name in ("run_shell", "script_run", "memory_recall", "mcp_delete", "mcp_send", "subagent_run", "team_run"):
        assert not tool_policy_allows(policy, name), name
    assert "assistant_profile" not in config.data["agent"]["native"]
    restored = assistant_config(effective, {**profile, "shell_allowed": True, "memory_allowed": True, "tool_policy": {"allow": ["run_shell"], "deny": []}})
    assert tool_policy_allows(normalise_tool_policy(restored.get("agent.native.tool_policy")), "run_shell")
    from nerya.tools.permissions import PermissionContext, PermissionEngine, PermissionMode, PermissionRequest, PermissionDecisionKind
    from nerya.tools.registry import make_native_descriptor
    descriptor = make_native_descriptor(name="web_search", description="Search", input_schema={}, handler=lambda call: None)
    context = PermissionContext(mode=PermissionMode.YOLO, current_tool_policy=lambda: current_assistant_policy(config, sid, policy))
    request = PermissionRequest(descriptor=descriptor)
    assert PermissionEngine().evaluate(request, context).kind == PermissionDecisionKind.ALLOW
    store.save({"research_allowed": False}, aid=profile["id"], expected_revision=profile["revision"])
    assert not tool_policy_allows(current_assistant_policy(config, sid, {}), "web_search")
    assert PermissionEngine().evaluate(request, context).kind == PermissionDecisionKind.DENY


def test_command_admission_identity_and_retry_survive_profile_edits(config):
    store = AssistantStore(config.paths)
    profile = store.save({"name": "Scout", "model_provider": "fixture", "model_id": "specialist"})
    sid = store.conversation(profile["id"])["session_id"]
    manager = CommandRuntime(config, lambda _c, _r: {"ok": True})
    payload = {"command_id": "assistant-command", "session_id": sid, "command_type": "send",
        "request": {"source": "dashboard", "kind": "user.chat", "payload": {"text": "Hello"}}}
    receipt = manager.submit(payload, start=False)
    assert receipt["command"]["context"]["assistant_id"] == profile["id"]
    with manager.store.transaction() as con:
        request = con.execute("SELECT request_json FROM agent_commands WHERE command_id=?", ("assistant-command",)).fetchone()[0]
    assert json.loads(request)["model_id"] == "specialist"
    store.save({"model_id": "specialist-v2"}, aid=profile["id"], expected_revision=profile["revision"])
    assert manager.submit(payload, start=False)["duplicate"]
    assert prepare_request(config, {"session_id": sid})["model_id"] == "specialist-v2"


class GatewayKernel:
    calls = []
    def __init__(self, *, config, skills):
        self.config = config
        self.hooks = SimpleNamespace(register=lambda *_a: None)
    def run_turn(self, *, trigger, session_id):
        self.calls.append((self.config, trigger, session_id))
        return SimpleNamespace(turn_id="turn-test", final_text="Assistant reply", decision={}, tool_trace=[], actions=[], blocks=[])


@pytest.mark.parametrize("platform", [p["id"] for p in list_platforms()])
def test_every_platform_routes_through_assistant(config, monkeypatch, platform):
    store = AssistantStore(config.paths)
    profile = store.save({"name": "Research", "instructions": "Remember my role."})
    channel = platform + "_test"
    yaml_io.dump(config.paths.messages_channels, {"channels": {channel: {"kind": platform, "assistant_id": profile["id"], "auto_reply": False}}})
    monkeypatch.setattr(routes_gateway, "AgentKernel", GatewayKernel)
    client = SimpleNamespace(config=config, skills=SimpleNamespace())
    inbound = next(h for m,p,h in routes_gateway.routes() if m == "POST" and p == "/gateway/inbound")
    result = inbound(client, {"channel": channel, "platform": platform, "chat_id": "room", "text": "Hello", "auto_reply": False})
    assert result["ok"], result
    assert result["assistant_id"] == profile["id"]
    assert store.for_session(result["session_id"])["id"] == profile["id"]
    assert GatewayKernel.calls[-1][0].get("agent.native.assistant_profile.instructions") == "Remember my role."


def test_gateway_rebinding_separates_context_and_unknown_assistant_fails(config, monkeypatch):
    store = AssistantStore(config.paths)
    a, b = (store.save({"name": name}) for name in ("Scout", "Writer"))
    monkeypatch.setattr(routes_gateway, "AgentKernel", GatewayKernel)
    client = SimpleNamespace(config=config, skills=SimpleNamespace())
    def run(aid):
        return routes_gateway._run_gateway_turn(client, platform="discord", channel="support", chat_id="same-room", text="hi", progress_cfg={"assistant_id": aid})
    first, second = run(a["id"]), run(b["id"])
    assert first["session_id"] != second["session_id"]
    assert run(a["id"])["session_id"] == first["session_id"]
    assert run("missing")["error"] == "assistant_not_found"
    assert routes_gateway._run_gateway_turn(client, platform="telegram", channel="telegram", chat_id="legacy", text="hi")["session_id"] == "telegram_legacy"


def test_assistant_routes_and_archiving_preserve_history(config):
    client = SimpleNamespace(config=config)
    handlers = {(m,p): h for m,p,h in routes_assistants.routes()}
    profile = handlers["POST", "/assistants/save"](client, {"profile": {"name": "Writer"}})["assistant"]
    sid = handlers["POST", "/assistants/conversations"](client, {"assistant_id": profile["id"]})["session_id"]
    yaml_io.dump(config.paths.messages_channels, {"channels": {"discord": {"kind": "discord", "assistant_id": profile["id"]}}})
    request = {"id": profile["id"], "expected_revision": profile["revision"]}
    assert handlers["POST", "/assistants/archive"](client, request)["error"] == "assistant_has_gateway_bindings"
    yaml_io.dump(config.paths.messages_channels, {"channels": {}})
    assert handlers["POST", "/assistants/archive"](client, request)["ok"]
    assert handlers["GET", "/assistants/session"](client, {"session_id": sid})["assistant"]["enabled"] is False
    assert handlers["POST", "/assistants/conversations"](client, {"assistant_id": profile["id"]})["error"] == "assistant_disabled"
    assert len(handlers["GET", "/assistants/conversations"](client, {"assistant_id": profile["id"]})["sessions"]) == 1
    assert required_scope("POST", "/assistants/save") == "write:config"
    assert required_scope("POST", "/assistants/conversations") == "write:chat"
    assert required_scope("GET", "/assistants/conversations") == "read:sessions"


def test_gateway_session_commands_cannot_cross_assistant_bindings(config, monkeypatch):
    store = AssistantStore(config.paths)
    a, b = (store.save({"name": name}) for name in ("A", "B"))
    target = store.conversation(b["id"])["session_id"]
    monkeypatch.setattr(routes_gateway, "AgentKernel", GatewayKernel)
    client = SimpleNamespace(config=config, skills=SimpleNamespace())
    result = routes_gateway._run_gateway_turn(client, platform="discord", channel="qa", chat_id="room",
        text="/session " + target, progress_cfg={"assistant_id": a["id"]})
    assert "another assistant" in result["reply_text"]
    assert target not in routes_gateway._load_state(client).get("active_sessions", {}).values()


def test_fork_retains_assistant_and_cannot_switch(config):
    from nerya.db.repositories import AgentSessionRepository
    from nerya.db.sqlite import connect
    from nerya.agent.history_branch import fork_session
    store = AssistantStore(config.paths)
    profile = store.save({"name": "Scout"})
    sid = store.conversation(profile["id"])["session_id"]
    con = connect(config.paths.db)
    AgentSessionRepository(con).record_message(message_id="user-one", session_id=sid, turn_id="turn-one", role="user", content="Fork here")
    con.close()
    fork = fork_session(config.paths, {"session_id":sid, "message_id":"user-one", "expected_content":"Fork here", "client_request_id":"assistant-fork-test"})
    assert store.for_session(fork["session_id"])["id"] == profile["id"]
    assert len(store.conversations(profile["id"])) == 2
    with pytest.raises(AssistantError, match="assistant_session_mismatch"):
        store.bind(fork["session_id"], "main")


def test_scheduled_sessions_bind_assistant_and_enforce_permissions(config):
    from nerya.triggers.schedule import ScheduleEntry
    from nerya.triggers.scheduled_session import ScheduledSessionRunner
    store = AssistantStore(config.paths)
    profile = store.save({"name":"Scheduled Scout", "shell_allowed":False})
    captured = []
    class Kernel:
        def run_turn(self, **kwargs):
            return SimpleNamespace(decision={}, actions=[], final_text="Done", stopped_reason="end_turn")
    def factory(effective):
        captured.append(effective)
        return Kernel()
    entry = ScheduleEntry(id="assistant-report", kind="assistant.report", every_seconds=3600, session_kind="agent", payload={"text":"Produce a report"}, execution={"assistant_id":profile["id"]})
    result = ScheduledSessionRunner(config, kernel_factory=factory).run_once(entry, now_ts=1000)
    assert result.ok
    assert captured[0].get("agent.native.assistant_profile.id") == profile["id"]
    assert store.for_session(result.session_id)["id"] == profile["id"]
    assert not tool_policy_allows(captured[0].get("agent.native.tool_policy"), "run_shell")


def test_reused_kernel_does_not_leak_identity_or_model_to_other_sessions(config, monkeypatch):
    import nerya.agent.kernel as kernel_module
    from nerya.llm.messages import MessagesResponse
    from nerya.sdk.internal_client import InternalClient
    captured = []
    class Gateway:
        def __init__(self, *_a, **_k): pass
        def effective_model_metadata(self, *_a, **_k): return "fixture", "test", {}
        def call_messages(self, **kwargs):
            captured.append(kwargs)
            return MessagesResponse(content=[{"type":"text", "text":"Identity check complete."}], stop_reason="end_turn", usage={"input_tokens":10,"output_tokens":10}, provider="fixture", model="test")
    monkeypatch.setattr(kernel_module, "LLMGateway", Gateway)
    store = AssistantStore(config.paths)
    a = store.save({"name":"Scout", "browser_allowed":False, "model_provider":"fixture", "model_id":"specialist"})
    b = store.save({"name":"Writer"})
    first, second = (store.conversation(p["id"])["session_id"] for p in (a,b))
    kernel = kernel_module.AgentKernel(config=config, skills=InternalClient.from_config(config).skills)
    for sid in (first, second, "unbound-conversation"):
        kernel.run_turn(trigger={"kind":"user.chat", "source":"dashboard", "payload":{"text":"Identify yourself"}}, session_id=sid)
    assert "Name: Scout" in captured[0]["system"]
    assert "Name: Writer" in captured[1]["system"] and "Name: Scout" not in captured[1]["system"]
    assert "Assistant identity:" not in captured[2]["system"]
    assert kernel.model_provider is None and kernel.model_id is None


def test_explicit_turn_model_survives_assistant_defaults(config):
    store = AssistantStore(config.paths)
    profile = store.save({"name":"Scout", "model_provider":"fixture", "model_id":"specialist"})
    sid = store.conversation(profile["id"])["session_id"]
    request = prepare_request(config, {"session_id":sid, "model_provider":"other", "model_id":"chosen"})
    assert (request["model_provider"], request["model_id"]) == ("other", "chosen")


def test_permission_changes_interrupt_only_the_bound_assistant(config, monkeypatch):
    from nerya.harness import cancellation
    stopped = []
    monkeypatch.setattr(cancellation, "signal_cancel", lambda sid, **_kwargs: stopped.append(sid))
    store = AssistantStore(config.paths)
    a, b = (store.save({"name":name}) for name in ("A", "B"))
    first = store.conversation(a["id"])["session_id"]
    second = store.conversation(b["id"])["session_id"]
    store.save({"memory_allowed":False}, aid=a["id"], expected_revision=a["revision"])
    assert first in stopped and second not in stopped


def test_chat_task_creation_is_persistent_scoped_and_idempotent(config, monkeypatch):
    from nerya.tools.native import tasks
    from nerya.subagents.tasks import TaskStore
    from nerya.tools.types import ToolCall
    store = AssistantStore(config.paths)
    aid = store.save({"name":"Scout"})["id"]
    sid = store.conversation(aid)["session_id"]
    task_store = TaskStore(config.paths)
    from nerya.agent.assistant_work import AssistantWork
    from nerya.agent import command_runtime
    manager = CommandRuntime(config, lambda *_: {"final_text": "fixture"})
    workers = []
    monkeypatch.setattr(manager, "kick", lambda sid: workers.append(sid))
    monkeypatch.setattr(command_runtime, "runtime", lambda _config: manager)
    call = ToolCall(id="create-one", name="assistant_task_create", turn_id="turn-one", arguments={"title":"Read a note", "prompt":"Read notes/proof.txt"})
    for _ in range(2):
        result = tasks.assistant_task_create_handler(call, config=config, skills=SimpleNamespace(), store=task_store, session_id=sid, actor_id="operator")
        assert not result.is_error
    rows = AssistantWork(config.paths).tasks(sid=sid)
    assert len(rows) == 1 and len(set(workers)) == 1
    assert rows[0]["assistant_id"] == aid
    assert rows[0]["task_session_id"] != sid
    with manager.store.transaction() as con:
        assert con.execute("SELECT actor_id FROM agent_commands WHERE command_id=?", (rows[0]["command_id"],)).fetchone()[0] == "operator"
    handlers = {(m,p): h for m,p,h in routes_assistants.routes()}
    client = SimpleNamespace(config=config)
    listed = handlers["GET", "/assistants/tasks"](client, {"session_id":sid})
    assert listed["tasks"][0]["title"] == "Read a note"
    refused = handlers["POST", "/assistants/tasks/stop"](client, {"session_id":"other-chat", "task_id":rows[0]["task_id"]})
    assert refused["error"] == "assistant_task_owner_mismatch"
    assert handlers["POST", "/assistants/tasks/stop"](client, {"session_id":sid,"task_id":rows[0]["task_id"]})["cancel_requested"]


def test_default_assistant_keeps_legacy_human_conversations(config):
    from nerya.db.sqlite import connect
    from nerya.db.repositories import AgentSessionRepository
    con = connect(config.paths.db)
    AgentSessionRepository(con).upsert_session(session_id="older-chat", source="dashboard", title="Original conversation")
    AgentSessionRepository(con).upsert_session(session_id="old-schedule", source="scheduled_agent", title="Scheduled execution")
    con.close()
    ids = {row["session_id"] for row in AssistantStore(config.paths).conversations("main")}
    assert "older-chat" in ids and "old-schedule" not in ids


def test_default_workspace_has_one_assistant_until_explicitly_created(config):
    store = AssistantStore(config.paths)
    for _ in range(2):
        assert [profile["id"] for profile in store.list()] == ["main"]
    store.conversation("main")
    assert [profile["id"] for profile in store.list()] == ["main"]
    created = store.save({"name":"User-created assistant"})
    assert [profile["id"] for profile in store.list()] == ["main", created["id"]]


def test_task_created_in_chat_inherits_assistant_and_schedule_owner(config):
    from nerya.tools.native.bootstrap import _wrap_task_create
    from nerya.tools.types import ToolCall
    from nerya.triggers.schedule import load_schedules
    aid = AssistantStore(config.paths).save({"name":"Reporter"})["id"]
    sid = AssistantStore(config.paths).conversation(aid)["session_id"]
    from nerya.core import yaml_io
    yaml_io.dump(config.paths.config, config.data)
    deps = SimpleNamespace(paths=config.paths, workspace_root=config.paths.root, active_session_id=sid, active_actor_id="operator")
    call = ToolCall(id="schedule-one",name="task_create",arguments={"title":"Hourly report","task_type":"agent","every_seconds":3600,"source_request":"Report once per hour","generated_prompt":"Summarize the report note.","enabled":False})
    result = _wrap_task_create(deps)(call)
    assert not result.is_error
    entry = load_schedules(config.paths)[0]
    assert entry.execution["assistant_id"] == aid and entry.payload["assistant_conversation_id"] == sid
    assert entry.owner_actor_id == "operator"
