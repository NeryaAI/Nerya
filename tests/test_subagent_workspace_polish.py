"""R02/R04/R09: offline entrypoint and scheduler regression fixtures."""
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, replace
from types import SimpleNamespace
from skill_fixtures import EmptySkillKernel
import threading

import pytest

from nerya.api import routes_teams
from nerya.core import yaml_io
from nerya.core.config import Config
from nerya.core.paths import WorkspacePaths
from nerya.subagents.control import continue_agent
from nerya.subagents.control import session_permission_mode
from nerya.subagents.dispatcher import SubAgentDispatcher, SubAgentResult
from nerya.subagents.permissions import child_permission_context
from nerya.subagents.threads import AgentThreadStore
from nerya.teams.models import TeamTemplate, TeamMemberSpec, TeamTaskSpec
from nerya.teams.orchestrator import TeamOrchestrator
from nerya.teams.store import TeamStore
from nerya.tools import NativeToolExecutor, PermissionContext, PermissionEngine, PermissionMode
from nerya.tools.native import agents
from nerya.tools.registry import ToolRegistry
from nerya.tools.types import RiskLevel, ToolCall, ToolResult
from nerya.agent.streaming import get_default_bus
from test_subagent_native_runtime import Gateway, call, descriptor, final, spec


def test_subagent_continuation_keeps_latest_operator_permission_after_compaction(tmp_path):
    """YOLO is a command fact, not a line from an LLM-compacted transcript."""
    from copy import deepcopy
    from nerya.agent.command_runtime import CommandRuntime
    from nerya.core.config import DEFAULT_CONFIG

    config = Config(paths=WorkspacePaths(tmp_path), data=deepcopy(DEFAULT_CONFIG))
    config.data["runtime"]["permission_mode"] = "auto"
    rt = CommandRuntime(config, lambda *_: {}, epoch="permissions-test")

    def submit(cid: str, mode: str):
        return rt.submit({
            "command_id": cid, "session_id": "session-yolo",
            "_auth_actor_id": "local:loopback", "request": {
                "payload": {"text": "Continue this task"}, "permission_mode": mode,
            },
        }, start=False)

    submit("command-grant-yolo", "yolo")
    assert session_permission_mode(config, "session-yolo", "local:loopback") == PermissionMode.YOLO
    # Same actor only. An unrelated token must not inherit another person's
    # ordinary native-tool permissions after reading their conversation.
    assert session_permission_mode(config, "session-yolo", "another-actor") == PermissionMode.AUTO
    assert session_permission_mode(config, "session-yolo", "") == PermissionMode.AUTO

    # Compaction may discard the entire text transcript, but not the durable
    # accepted command's mode or its actor binding.
    with rt.store.transaction() as con:
        con.execute("UPDATE agent_messages SET content='[context checkpoint]' "
                    "WHERE session_id=?", ("session-yolo",))
    assert session_permission_mode(config, "session-yolo", "local:loopback") == PermissionMode.YOLO

    submit("command-revoke-yolo", "auto")
    assert session_permission_mode(config, "session-yolo", "local:loopback") == PermissionMode.AUTO


@pytest.fixture
def scope(tmp_path, monkeypatch):
    config = Config(paths=WorkspacePaths(tmp_path), data={
        "runtime": {"permission_mode": "yolo"},
        "agent": {"native": {"llm_retry_base_delay": 0, "llm_retry_max_delay": 0}}})
    invoked = []
    def handler(c):
        invoked.append(c.name)
        return ToolResult.from_json(tool_use_id=c.id, name=c.name, data={"ok": True})
    registry = ToolRegistry()
    registry.register_all([
        descriptor("read_probe", handler=handler, read_only=True),
        descriptor("other_read", handler=handler, read_only=True),
        descriptor("write_probe", handler=handler, risk=RiskLevel.WRITE, read_only=False),
    ])
    skills = EmptySkillKernel()
    deps = SimpleNamespace()
    monkeypatch.setattr("nerya.tools.native.bootstrap.build_native_tool_deps", lambda **_: deps)
    monkeypatch.setattr("nerya.tools.native.bootstrap.register_native_tools",
                        lambda target, _: target.register_all(registry.list_tools()))
    class Kernel:
        def __init__(self, **_):
            self.permission_mode = PermissionMode.YOLO
            self.tool_registry = registry
            self.ext_host = None
        def _bind_session_strategy(self, **kwargs):
            return kwargs["requested_strategy_id"]
        def _ensure_registry(self):
            return deps
    monkeypatch.setattr("nerya.agent.kernel.AgentKernel", Kernel)
    return SimpleNamespace(config=config, registry=registry, skills=skills, invoked=invoked,
                           role=spec(tmp_path), tmp_path=tmp_path)


@pytest.mark.parametrize("entry", ["sync", "background", "dashboard"])
@pytest.mark.parametrize("restriction", ["plan", "tool_policy", "role"])
def test_entrypoints_reject_same_forbidden_tool(scope, monkeypatch, entry, restriction):
    if restriction == "plan":
        scope.config.data["agent"]["native"]["plan_only"] = True
    elif restriction == "tool_policy":
        scope.config.data["agent"]["native"]["tool_policy"] = {"deny": ["write_*"]}
    else:
        scope.role = replace(scope.role, execution_policy=replace(
            scope.role.execution_policy, native_tool_allow=["read_probe"]))
    gateway = Gateway(call("write_probe"), final())
    monkeypatch.setattr("nerya.subagents.dispatcher.LLMGateway", lambda _: gateway)
    monkeypatch.setattr(SubAgentDispatcher, "_resolve_spec", lambda *_, **__: scope.role)
    if entry == "dashboard":
        store = AgentThreadStore(scope.config.paths)
        row = store.begin(spec=scope.role, payload={}, session_id="session",
            parent_call_id="parent", strategy_id=None, turn_id="turn", context_scope="subagent")
        store.finish(row, state="completed", transcript=[])
        # The row predates the restriction; continuing must use current configuration.
        result = continue_agent(scope, session_id="session", agent_id=row["id"],
                                message="continue", request_id="resume")
        assert result["state"] in {"completed", "blocked"}, result
    else:
        if entry == "background":
            from nerya.subagents.tasks import TaskStore
            from nerya.tools.native.tasks import _worker
            tasks = TaskStore(scope.config.paths)
            record = tasks.create(name="specialist", payload={}, parent_session_id="session",
                                  parent_turn_id="turn", strategy_id=None)
            _worker(config=scope.config, skills=scope.skills, store=tasks, task_id=record.task_id,
                    name="specialist", payload={}, session_id="session",
                    strategy_id=None, trigger_event_id=None)
            assert AgentThreadStore(scope.config.paths).list("session")
        else:
            executor = NativeToolExecutor(registry=scope.registry, permission_engine=PermissionEngine(),
                permission_context=PermissionContext(mode=PermissionMode.YOLO))
            dispatcher = SubAgentDispatcher(scope.config, scope.skills, scope.registry, executor)
            result = dispatcher.dispatch("subagent:specialist", payload={}, session_id="session")
            assert result["agent_id"], result
    assert scope.invoked == []
    assert gateway.calls, "fixture must reach the real native child loop"


def test_resume_preserves_parent_ceiling_and_role_when_current_settings_widen(scope, monkeypatch):
    gateway = Gateway(final())
    monkeypatch.setattr("nerya.subagents.dispatcher.LLMGateway", lambda _: gateway)
    role = replace(scope.role, execution_policy=replace(scope.role.execution_policy,
                                                       native_tool_deny=["other_read"]))
    executor = NativeToolExecutor(registry=scope.registry, permission_engine=PermissionEngine(),
        permission_context=PermissionContext(mode=PermissionMode.YOLO, plan_only=True,
            tool_policy={"deny": ["read_probe"]}))
    first = SubAgentDispatcher(scope.config, scope.skills, scope.registry, executor).dispatch(
        "subagent:specialist", payload={}, inline_spec=role, session_id="session", turn_id="first")
    assert first["ok"]
    store = AgentThreadStore(scope.config.paths)
    saved = store.load(first["agent_id"], "session")
    assert saved["permission_ceiling"]["plan_only"] is True
    for i, name in enumerate(["read_probe", "write_probe", "other_read"]):
        gateway = Gateway(call(name), final())
        continue_agent(scope, session_id="session", agent_id=first["agent_id"],
                       message="continue", request_id=f"resume-{i}")
    assert scope.invoked == []
    assert executor.permission_context.tool_policy == {"deny": ["read_probe"]}
    assert store.restore_spec(store.load(first["agent_id"], "session")).execution_policy == role.execution_policy


def test_current_strategy_policy_intersects_saved_parent_policy(scope):
    root = scope.config.paths.strategy("fixture_strategy")
    root.mkdir(parents=True)
    yaml_io.dump(root / "strategy.yml", {"version": 1, "strategy_id": "fixture_strategy",
        "title": "fixture", "mode": "paper", "entrypoint": "main.py:run",
        "schedule": {"type": "none", "enabled": False},
        "agent_profile": {"allowed_tools": ["read_probe", "other_read"]},
        "agent_execution": {"capabilities": "custom", "denied_tools": ["other_read"]}})
    (root / "main.py").write_text("def run(ctx): pass")
    context = child_permission_context(scope.config, strategy_id="fixture_strategy",
        saved={"tool_policy": {"deny": ["read_probe"]}, "plan_only": True})
    executor = NativeToolExecutor(registry=scope.registry, permission_engine=PermissionEngine(),
                                  permission_context=context)
    for name in ["read_probe", "other_read", "write_probe"]:
        assert executor.execute(ToolCall(name=name, arguments={})).is_error
    assert scope.invoked == []


def test_legacy_role_snapshot_restores_native_tool_limits(scope):
    role = replace(scope.role, execution_policy=replace(scope.role.execution_policy,
        native_tool_allow=["read_probe"], native_tool_deny=["other_read"]))
    legacy = asdict(role)
    legacy["prompt_path"] = str(role.prompt_path)
    restored = AgentThreadStore.restore_spec({"spec": legacy})
    assert restored.execution_policy == role.execution_policy


def test_background_event_still_cancels_during_child_execution(scope, monkeypatch):
    token = threading.Event()
    gateway = Gateway(call("read_probe"), final())
    monkeypatch.setattr("nerya.subagents.dispatcher.LLMGateway", lambda _: gateway)
    scope.registry.register(descriptor("cancel_probe", read_only=True,
        handler=lambda c: token.set() or ToolResult.from_json(
            tool_use_id=c.id, name=c.name, data={"ok": True})))
    gateway = Gateway(call("cancel_probe"), final())
    dispatcher = SubAgentDispatcher.for_workspace(scope.config, scope.skills, session_id="session")
    result = dispatcher.dispatch("subagent:specialist", payload={}, inline_spec=scope.role,
                                 session_id="session", cancel_token=token)
    assert result["error_kind"] == "cancelled"
    assert len(gateway.calls) == 1
    assert AgentThreadStore(scope.config.paths).load(result["agent_id"], "session")["state"] == "cancelled"


def test_reused_strategy_child_cannot_discard_original_ceiling(scope, monkeypatch):
    monkeypatch.setattr("nerya.subagents.dispatcher.LLMGateway", lambda _: Gateway(final()))
    executor = NativeToolExecutor(registry=scope.registry, permission_engine=PermissionEngine(),
        permission_context=PermissionContext(plan_only=True, tool_policy={"deny": ["write_probe"]}))
    dispatcher = SubAgentDispatcher(scope.config, scope.skills, scope.registry, executor)
    first = dispatcher.dispatch("subagent:specialist", payload={}, session_id="sched_fixture",
                                inline_spec=scope.role, parent_call_id="one")
    executor.permission_context = PermissionContext()
    second = dispatcher.dispatch("subagent:specialist", payload={}, session_id="sched_fixture",
                                 inline_spec=scope.role, parent_call_id="two")
    assert first["agent_id"] == second["agent_id"]
    saved = AgentThreadStore(scope.config.paths).load(second["agent_id"], "sched_fixture")
    assert saved["permission_ceiling"] == {"plan_only": True,
        "tool_policy": {"allow_groups": [], "deny": ["write_probe"]}}


def test_scheduler_owns_running_events_and_durable_members(tmp_path, monkeypatch):
    config = Config(paths=WorkspacePaths(tmp_path), data={})
    bus = get_default_bus()
    bus.clear()
    release, two_started = threading.Event(), threading.Event()
    lock = threading.Lock()
    count = 0
    class Dispatcher:
        def __init__(self, **_): pass
        def dispatch(self, target, **_):
            nonlocal count
            with lock:
                count += 1
                if count == 2:
                    two_started.set()
            assert release.wait(5)
            return SubAgentResult(ok=True, subagent=target.split(":")[1],
                output={"summary": "fixture", "done": True}).asdict()
    monkeypatch.setattr(agents, "SubAgentDispatcher", Dispatcher)
    roles = [{"name": f"role{i}"} for i in range(5)]
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(agents.team_run_handler, ToolCall(name="team_run", id="call", turn_id="turn",
            arguments={"task": "fixture", "roles": roles, "max_parallel": 2, "team_run_id": "team-fixture"},
            metadata={"session_id": "session"}), config=config, skills=object())
        try:
            assert two_started.wait(5)
            refreshed = routes_teams._get_run(SimpleNamespace(config=config), {"run_id": "team-fixture"})
            assert sorted(m["status"] for m in refreshed["members"]) == ["queued"] * 3 + ["running"] * 2
            starts = [e for e in bus.recent() if e["kind"] == "team.member.start"]
            assert len(starts) == 2
        finally:
            release.set()
        result = future.result(timeout=5)
    assert not result.is_error
    active, maximum = set(), 0
    for event in bus.recent():
        if event["kind"] == "team.member.start":
            active.add(event["team_task_id"])
        elif event["kind"] == "team.member.end":
            active.discard(event["team_task_id"])
        maximum = max(maximum, len(active))
    assert maximum == 2 and not active
    assert {m.status for m in TeamStore(config.paths).read_members("team-fixture")} == {"completed"}


def test_dependency_wait_and_failure_are_server_states(tmp_path):
    config = Config(paths=WorkspacePaths(tmp_path), data={})
    template = TeamTemplate(id="deps", description="fixture", lead="a", max_parallel=2,
        members=[TeamMemberSpec(name=n, role=n, subagent_name=n) for n in ["a", "b"]],
        tasks=[TeamTaskSpec(id="a", owner="a", subagent_name="a", subject="first"),
               TeamTaskSpec(id="b", owner="b", subagent_name="b", subject="second", depends_on=["a"])])
    seen = []
    class Dispatcher:
        def dispatch(self, target, **_):
            seen.append(target)
            members = TeamStore(config.paths).read_members("team-deps")
            assert {m.name: m.status for m in members} == {"a": "running", "b": "blocked"}
            return SubAgentResult(ok=False, subagent="a", error="fixture failure").asdict()
    result = TeamOrchestrator(config, object(), dispatcher=Dispatcher()).run(
        template=template, goal="fixture", run_id="team-deps")
    assert seen == ["subagent:a"]
    assert result.status == "blocked"
    assert {m["name"]: m["status"] for m in result.members} == {"a": "failed", "b": "blocked"}
    events = TeamStore(config.paths).list_events("team-deps")
    assert any(e.get("wait_reason") == "dependencies" for e in events)


def test_http_starts_listed_workspace_template(scope, monkeypatch):
    from test_team_workspace_templates import _write_template
    _write_template(scope.tmp_path, "custom_team")
    seen = []
    def dispatch(self, target, **kwargs):
        seen.append(kwargs["payload"]["team_template"])
        return SubAgentResult(ok=True, subagent=target.split(":")[1],
                              output={"summary": "fixture", "done": True}).asdict()
    monkeypatch.setattr(SubAgentDispatcher, "dispatch", dispatch)
    listing = routes_teams._list_templates(scope, {})
    assert "custom_team" in listing["ids"]
    started = routes_teams._start_run(scope, {"template": "custom_team", "goal": "fixture"})
    assert started["ok"] and started["template_id"] == "custom_team"
    assert seen == ["custom_team"]
    unknown = routes_teams._start_run(scope, {"template": "missing", "goal": "fixture"})
    assert not unknown["ok"] and set(unknown["available"]) == set(listing["ids"])
