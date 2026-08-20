"""Security boundary tests for child native-tool dispatch."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from nerya.core.config import Config
from nerya.core.paths import WorkspacePaths
from nerya.harness.cancellation import CancelToken
from nerya.subagents.runtime import SubAgentRuntime
from nerya.subagents.tasks import TaskStore
from nerya.subagents.dispatcher import SubAgentDispatcher
from nerya.subagents.registry import SubAgentExecutionPolicy, SubAgentSpec
from nerya.strategies.context import StrategySubAgents
from nerya.teams.store import TeamStore
from nerya.tools import (
    NativeToolExecutor,
    PermissionContext,
    PermissionEngine,
    PermissionMode,
)
from nerya.tools.native import agents as native_agents
from nerya.tools.native import strategy_runtime
from nerya.tools.native import tasks as native_tasks
from nerya.tools.registry import ToolRegistry, make_native_descriptor
from nerya.tools.tool_approvals import ToolApprovalResolution
from nerya.tools.types import (
    PermissionScope,
    RiskLevel,
    ToolCall,
    ToolResult,
)


pytestmark = pytest.mark.smoke


def _runtime_with_executor(
    tmp_path,
    descriptor,
    *,
    mode=PermissionMode.DEFAULT,
    approval_resolver=None,
):
    registry = ToolRegistry()
    registry.register(descriptor)
    executor = NativeToolExecutor(
        registry=registry,
        permission_engine=PermissionEngine(),
        permission_context=PermissionContext(mode=mode),
        approval_resolver=approval_resolver,
    )
    runtime = SubAgentRuntime(
        config=Config(paths=WorkspacePaths(tmp_path), data={}),
        skills=SimpleNamespace(),
        llm=SimpleNamespace(),
        tool_registry=registry,
        tool_executor=executor,
    )
    return runtime, executor


def test_child_native_call_uses_parent_executor_schema_gate(tmp_path):
    invoked: list[ToolCall] = []
    descriptor = make_native_descriptor(
        name="child_probe",
        description="test child probe",
        input_schema={
            "type": "object",
            "properties": {"query": {"type": "string"}},
            "required": ["query"],
        },
        handler=lambda call: (
            invoked.append(call)
            or ToolResult.from_json(tool_use_id=call.id, name=call.name, data={"ok": True})
        ),
        risk=RiskLevel.READ,
        permission_scope=PermissionScope.NETWORK,
        auto_approve=True,
    )
    runtime, _executor = _runtime_with_executor(tmp_path, descriptor)

    record = runtime._dispatch_native(  # noqa: SLF001
        "child_probe",
        payload={},
        entry={"skill": "child_probe", "action": "run"},
        spec_name="researcher",
        strategy_id=None,
        session_id="session-1",
        trigger_event_id=None,
        context_metadata={"turn_id": "turn-1"},
        iteration=4,
    )

    assert record["ok"] is False
    assert record["error_kind"] == "schema_validation"
    assert invoked == []


def test_child_native_call_uses_parent_executor_approval_gate(tmp_path):
    invoked: list[ToolCall] = []
    descriptor = make_native_descriptor(
        name="child_exec",
        description="test child execution",
        input_schema={
            "type": "object",
            "properties": {"command": {"type": "string"}},
            "required": ["command"],
        },
        handler=lambda call: (
            invoked.append(call)
            or ToolResult.from_json(tool_use_id=call.id, name=call.name, data={"ran": True})
        ),
        risk=RiskLevel.EXEC,
        permission_scope=PermissionScope.SYSTEM,
    )
    class PendingResolver:
        def __init__(self):
            self.seen: list[tuple[ToolCall, object]] = []

        def resolve(self, call, _descriptor, decision):
            self.seen.append((call, decision))
            return ToolApprovalResolution(
                request={
                    "kind": "approval_request",
                    "approval_id": "tool_batch_turn-2",
                    "call_id": call.id,
                    "action": call.name,
                    "record": {"kind": "tool_permission_batch"},
                }
            )

    resolver = PendingResolver()
    runtime, _executor = _runtime_with_executor(
        tmp_path,
        descriptor,
        approval_resolver=resolver,
    )

    record = runtime._dispatch_native(  # noqa: SLF001
        "child_exec",
        payload={"command": "echo protected"},
        entry={"skill": "child_exec", "action": "run"},
        spec_name="researcher",
        strategy_id=None,
        session_id="session-2",
        trigger_event_id=None,
        context_metadata={"turn_id": "turn-2"},
        iteration=2,
    )

    assert record["ok"] is False
    assert record["error_kind"] == "permission_pending"
    assert invoked == []
    assert record["recovery_hint"]["approval_id"] == "tool_batch_turn-2"
    assert record["approval_request"]["approval_id"] == "tool_batch_turn-2"
    assert len(resolver.seen) == 1
    pending_call, pending_decision = resolver.seen[0]
    assert pending_call.id == record["tool_use_id"]
    assert pending_call.caller == record["caller"] == "subagent:researcher"
    assert pending_decision.risk is RiskLevel.EXEC


def test_executor_rejects_cancelled_call_before_handler(tmp_path):
    invoked: list[ToolCall] = []
    descriptor = make_native_descriptor(
        name="cancel_probe",
        description="test cancellation boundary",
        input_schema={"type": "object"},
        handler=lambda call: (
            invoked.append(call)
            or ToolResult.from_json(tool_use_id=call.id, name=call.name, data={"ok": True})
        ),
        risk=RiskLevel.WRITE,
        permission_scope=PermissionScope.WORKSPACE,
        auto_approve=True,
    )
    _runtime, executor = _runtime_with_executor(tmp_path, descriptor)
    token = CancelToken()
    token.cancel("operator_stop")

    result = executor.execute(
        ToolCall(
            name="cancel_probe",
            arguments={},
            metadata={"cancel_token": token},
        )
    )

    assert result.is_error is True
    assert result.error.kind.value == "aborted"
    assert result.error.detail["reason"] == "operator_stop"
    assert invoked == []


def test_child_native_delegation_preserves_remaining_wall_budget(tmp_path):
    seen: list[ToolCall] = []
    descriptor = make_native_descriptor(
        name="nested_probe",
        description="test nested budget propagation",
        input_schema={"type": "object"},
        handler=lambda call: (
            seen.append(call)
            or ToolResult.from_json(
                tool_use_id=call.id,
                name=call.name,
                data={"ok": True},
            )
        ),
        risk=RiskLevel.READ,
        permission_scope=PermissionScope.NETWORK,
        auto_approve=True,
    )
    runtime, _executor = _runtime_with_executor(tmp_path, descriptor)

    record = runtime._dispatch_native(  # noqa: SLF001
        "nested_probe",
        payload={},
        entry={"skill": "nested_probe", "action": "run"},
        spec_name="researcher",
        strategy_id=None,
        session_id="session-nested-budget",
        trigger_event_id=None,
        context_metadata={
            "turn_id": "turn-nested-budget",
            "remaining_wall_seconds": 17.5,
        },
        iteration=1,
    )

    assert record["ok"] is True
    assert seen[0].metadata["remaining_wall_seconds"] == 17.5


def test_dispatcher_created_child_fails_closed_without_parent_executor(tmp_path):
    invoked: list[ToolCall] = []
    descriptor = make_native_descriptor(
        name="child_probe",
        description="test child probe",
        input_schema={"type": "object"},
        handler=lambda call: (
            invoked.append(call)
            or ToolResult.from_json(tool_use_id=call.id, name=call.name, data={"ok": True})
        ),
        risk=RiskLevel.READ,
        permission_scope=PermissionScope.NETWORK,
        auto_approve=True,
    )
    registry = ToolRegistry()
    registry.register(descriptor)
    runtime = SubAgentRuntime(
        config=Config(paths=WorkspacePaths(tmp_path), data={}),
        skills=SimpleNamespace(),
        llm=SimpleNamespace(),
        tool_registry=registry,
        require_tool_executor=True,
    )

    assert runtime._allowed_native_tool_names() == []  # noqa: SLF001
    record = runtime._dispatch_native(  # noqa: SLF001
        "child_probe",
        payload={},
        entry={},
        spec_name="researcher",
        strategy_id=None,
        session_id=None,
        trigger_event_id=None,
    )

    assert record["ok"] is False
    assert record["error_kind"] == "native_executor_required"
    assert invoked == []


def test_dispatcher_rejects_required_native_contract_without_executor(
    monkeypatch, tmp_path
):
    spec = SubAgentSpec(
        name="native_required",
        prompt_path=Path(tmp_path) / "native_required.md",
        execution_policy=SubAgentExecutionPolicy(
            required_native_tools=["research_run"],
        ),
    )
    dispatcher = SubAgentDispatcher(
        config=Config(paths=WorkspacePaths(tmp_path), data={}),
        skills=SimpleNamespace(),
        runtime_mode="legacy",
    )
    monkeypatch.setattr(
        dispatcher,
        "_resolve_spec",
        lambda _name, *, strategy_id=None: spec,
    )

    envelope = dispatcher.dispatch(
        "subagent:native_required",
        payload={"task": "inspect"},
    )

    assert envelope["ok"] is False
    assert envelope["error_kind"] == "policy"
    assert "native executor required" in envelope["error"]


def test_native_subagent_handler_forwards_parent_executor(monkeypatch, tmp_path):
    captured: dict[str, object] = {}
    cancel_token = object()

    class FakeDispatcher:
        def __init__(self, **kwargs):
            captured.update(kwargs)

        def dispatch(self, *_args, **_kwargs):
            captured["dispatch_kwargs"] = _kwargs
            return {"ok": True, "subagent": "analyst", "output": {"done": True}}

    monkeypatch.setattr(native_agents, "SubAgentDispatcher", FakeDispatcher)
    config = Config(paths=WorkspacePaths(tmp_path), data={})
    parent_executor = object()
    result = native_agents.subagent_run_handler(
        ToolCall(
            name="subagent_run",
            id="child-launch",
            arguments={"name": "analyst", "payload": {"task": "inspect"}},
            metadata={"cancel_token": cancel_token},
        ),
        config=config,
        skills=SimpleNamespace(),
        executor=parent_executor,
    )

    assert result.is_error is False
    assert captured["executor"] is parent_executor
    assert captured["dispatch_kwargs"]["cancel_token"] is cancel_token


def test_native_subagent_handler_preserves_child_cancellation(monkeypatch, tmp_path):
    class FakeDispatcher:
        def __init__(self, **_kwargs):
            pass

        def dispatch(self, *_args, **_kwargs):
            return {
                "ok": False,
                "subagent": "analyst",
                "error": "operator_stop",
                "error_kind": "cancelled",
                "output": {},
            }

    monkeypatch.setattr(native_agents, "SubAgentDispatcher", FakeDispatcher)
    result = native_agents.subagent_run_handler(
        ToolCall(
            name="subagent_run",
            id="child-launch-cancelled",
            arguments={"name": "analyst", "payload": {"task": "inspect"}},
        ),
        config=Config(paths=WorkspacePaths(tmp_path), data={}),
        skills=SimpleNamespace(),
    )

    assert result.is_error is True
    assert result.error is not None
    assert result.error.kind.value == "aborted"
    assert result.error.message == "operator_stop"
    assert result.error.detail["reason"] == "operator_stop"
    assert result.error.recovery_hint == {
        "action": "cancelled",
        "reason": "operator_stop",
    }


def test_native_subagent_handler_surfaces_nested_permission_pending(monkeypatch, tmp_path):
    class FakeDispatcher:
        def __init__(self, **_kwargs):
            pass

        def dispatch(self, *_args, **_kwargs):
            return {
                "ok": True,
                "subagent": "analyst",
                "metrics": {
                    "rejected_actions": [
                        {
                            "ok": False,
                            "skill": "run_shell",
                            "action": "(native)",
                            "error": "approval required",
                            "error_kind": "permission_pending",
                            "tool_use_id": "toolu_nested",
                            "caller": "subagent:analyst",
                            "payload": {"command": "echo protected"},
                        }
                    ]
                },
            }

    monkeypatch.setattr(native_agents, "SubAgentDispatcher", FakeDispatcher)
    result = native_agents.subagent_run_handler(
        ToolCall(
            name="subagent_run",
            id="child-launch-pending",
            arguments={"name": "analyst", "payload": {"task": "inspect"}},
        ),
        config=Config(paths=WorkspacePaths(tmp_path), data={}),
        skills=SimpleNamespace(),
    )

    assert result.is_error is True
    assert result.error is not None
    assert result.error.kind.value == "permission_pending"
    assert result.error.recovery_hint == {
        "nested_permission_pending": True,
        "nested_tool_use_id": "toolu_nested",
        "tool_name": "run_shell",
        "payload": {"command": "echo protected"},
        "caller": "subagent:analyst",
    }


def test_research_handler_preserves_child_cancellation(monkeypatch, tmp_path):
    class FakeDispatcher:
        def __init__(self, **_kwargs):
            pass

        def dispatch(self, *_args, **_kwargs):
            return {
                "ok": False,
                "subagent": "web_researcher",
                "error": "operator_stop",
                "error_kind": "cancelled",
                "output": {},
            }

    monkeypatch.setattr(native_agents, "SubAgentDispatcher", FakeDispatcher)
    registry = SimpleNamespace(
        get=lambda _name: SimpleNamespace(
            delegates_to="web_researcher",
            child_max_depth=1,
        )
    )
    result = native_agents.research_run_handler(
        ToolCall(
            name="research_run",
            id="research-cancelled",
            arguments={"query": "AI infrastructure"},
        ),
        config=Config(paths=WorkspacePaths(tmp_path), data={}),
        skills=SimpleNamespace(),
        tool_registry=registry,
    )

    assert result.is_error is True
    assert result.error is not None
    assert result.error.kind.value == "aborted"
    assert result.error.message == "operator_stop"
    assert result.error.detail["reason"] == "operator_stop"
    assert result.error.recovery_hint == {
        "action": "cancelled",
        "reason": "operator_stop",
    }


def test_strategy_subagents_uses_legacy_without_parent_executor(monkeypatch, tmp_path):
    captured: list[dict[str, object]] = []

    class FakeDispatcher:
        def __init__(self, **kwargs):
            captured.append(kwargs)

        def dispatch(self, *_args, **_kwargs):
            return {"ok": True, "output": {"done": True}}

    monkeypatch.setattr(
        "nerya.subagents.dispatcher.SubAgentDispatcher", FakeDispatcher
    )
    config = Config(paths=WorkspacePaths(tmp_path), data={})
    StrategySubAgents(
        config=config,
        skills=SimpleNamespace(),
        strategy_id="strategy-legacy",
    ).run("web_researcher")
    assert captured[0]["runtime_mode"] == "legacy"
    assert "tool_registry" not in captured[0]
    assert "executor" not in captured[0]

    registry = object()
    executor = object()
    StrategySubAgents(
        config=config,
        skills=SimpleNamespace(),
        strategy_id="strategy-native",
        tool_registry=registry,
        executor=executor,
    ).run("web_researcher")
    assert captured[1]["tool_registry"] is registry
    assert captured[1]["executor"] is executor
    assert "runtime_mode" not in captured[1]


def test_strategy_run_tick_handler_forwards_parent_native_deps(monkeypatch, tmp_path):
    captured: dict[str, object] = {}

    class FakeRecord:
        def asdict(self):
            return {"status": "hold"}

    class FakeRunner:
        def __init__(self, **kwargs):
            captured.update(kwargs)

        def run_tick(self, *_args, **_kwargs):
            return FakeRecord()

    monkeypatch.setattr("nerya.strategies.runner.StrategyRunner", FakeRunner)
    registry = object()
    executor = object()
    result = strategy_runtime.strategy_run_tick_handler(
        ToolCall(
            name="strategy_run_tick",
            id="strategy-run-native",
            arguments={"strategy_id": "alpha"},
        ),
        config=Config(paths=WorkspacePaths(tmp_path), data={}),
        skills=SimpleNamespace(),
        tool_registry=registry,
        executor=executor,
    )

    assert result.is_error is False
    assert captured["tool_registry"] is registry
    assert captured["executor"] is executor


def test_native_team_handler_surfaces_nested_permission_pending(monkeypatch, tmp_path):
    nested_recovery = {
        "action": "await_approval",
        "tool_use_id": "toolu_nested_team",
        "approval_id": "approval-1",
    }

    class FakeDispatcher:
        def __init__(self, **_kwargs):
            pass

        def dispatch(self, target, **_kwargs):
            return {
                "ok": True,
                "subagent": target.split(":", 1)[1],
                "output": {"summary": "waiting for approval"},
                "metrics": {
                    "rejected_actions": [
                        {
                            "ok": False,
                            "skill": "run_shell",
                            "action": "(native)",
                            "error": "approval required",
                            "error_kind": "permission_pending",
                            "tool_use_id": "toolu_nested_team",
                            "caller": "subagent:analyst",
                            "payload": {"command": "echo protected"},
                            "recovery_hint": nested_recovery,
                        }
                    ]
                },
            }

    monkeypatch.setattr(native_agents, "SubAgentDispatcher", FakeDispatcher)
    paths = WorkspacePaths(tmp_path)
    result = native_agents.team_run_handler(
        ToolCall(
            name="team_run",
            id="team-launch-pending",
            arguments={
                "task": "inspect",
                "team_run_id": "team-pending-test",
                "roles": [{"name": "analyst"}],
            },
        ),
        config=Config(paths=paths, data={}),
        skills=SimpleNamespace(),
    )

    assert result.is_error is True
    assert result.error is not None
    assert result.error.kind.value == "permission_pending"
    assert result.error.detail["team_run"]["orchestrator_status"] == "blocked"
    assert result.error.recovery_hint == {
        "nested_permission_pending": True,
        "nested_tool_use_id": "toolu_nested_team",
        "tool_name": "run_shell",
        "payload": {"command": "echo protected"},
        "caller": "subagent:analyst",
        "recovery_hint": nested_recovery,
        "subagent": "analyst",
    }
    compact_pending = result.error.detail["team_run"]["failures"][0][
        "permission_pending"
    ]
    assert compact_pending["nested_tool_use_id"] == "toolu_nested_team"
    assert compact_pending["caller"] == "subagent:analyst"
    assert compact_pending["payload"] == {"command": "echo protected"}
    assert compact_pending["recovery_hint"] == nested_recovery

    store = TeamStore(paths)
    run = store.read_run("team-pending-test")
    assert run is not None
    assert run.status == "blocked"
    task = store.list_tasks("team-pending-test")[0]
    assert task.status == "blocked"
    assert task.payload["permission_pending"]["tool_use_id"] == "toolu_nested_team"


def test_async_worker_threads_task_stop_token_into_child(monkeypatch, tmp_path):
    paths = WorkspacePaths(tmp_path)
    store = TaskStore(paths)
    record = store.create(name="analyst", payload={"task": "inspect"})
    cancel_token = store.cancel_event(record.task_id)
    assert cancel_token is not None
    seen: dict[str, object] = {}

    class FakeDispatcher:
        def __init__(self, **kwargs):
            seen["init_kwargs"] = kwargs

        def dispatch(self, *_args, **kwargs):
            seen.update(kwargs)
            token = kwargs["cancel_token"]
            token.set()
            return {"ok": True, "output": {"done": True}}

    monkeypatch.setattr(native_tasks, "SubAgentDispatcher", FakeDispatcher)
    native_tasks._worker(  # noqa: SLF001
        config=Config(paths=paths, data={}),
        skills=SimpleNamespace(),
        store=store,
        task_id=record.task_id,
        name="analyst",
        payload={"task": "inspect"},
        trigger_event_id=None,
        strategy_id=None,
        session_id=None,
    )

    assert seen["cancel_token"] is cancel_token
    assert seen["init_kwargs"]["runtime_mode"] == "legacy"
    assert "executor" not in seen["init_kwargs"]
    assert "tool_registry" not in seen["init_kwargs"]
    finished = store.load(record.task_id)
    assert finished is not None
    assert finished.state == "cancelled"
