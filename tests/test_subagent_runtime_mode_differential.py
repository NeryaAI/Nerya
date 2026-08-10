"""Differential contract for replacing the legacy child loop."""

from __future__ import annotations

import json
from collections import deque
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from nerya.core.config import Config
from nerya.core.paths import WorkspacePaths
from nerya.harness.cancellation import CancelToken
from nerya.llm.gateway import LLMCall
from nerya.llm.messages import MessagesResponse
from nerya.subagents.registry import SubAgentExecutionPolicy, SubAgentSpec
from nerya.subagents.runtime import SubAgentRuntime
from nerya.tools import (
    NativeToolExecutor,
    PermissionContext,
    PermissionEngine,
    PermissionMode,
)
from nerya.tools.registry import ToolRegistry, make_native_descriptor
from nerya.tools.types import PermissionScope, RiskLevel, ToolCall, ToolResult


pytestmark = pytest.mark.smoke


class _EmptySkillRegistry:
    def list(self) -> list[Any]:
        return []

    def get(self, name: str) -> Any:
        raise KeyError(name)


class _DualProtocolGateway:
    """Serve the same script through legacy JSON and native messages APIs."""

    def __init__(
        self,
        *,
        legacy: list[dict[str, Any]],
        native: list[MessagesResponse],
    ) -> None:
        self._legacy = deque(legacy)
        self._native = deque(native)
        self.legacy_prompts: list[str] = []
        self.native_tool_names: list[list[str]] = []

    def call(self, **kwargs: Any) -> LLMCall:
        self.legacy_prompts.append(str(kwargs["prompt"]))
        parsed = self._legacy.popleft()
        return LLMCall(
            tier="light",
            task=kwargs["task"],
            caller=kwargs["caller"],
            tokens=1,
            usd=0.0,
            raw=json.dumps(parsed),
            parsed=parsed,
            provider="fixture",
            model="fixture",
        )

    def call_messages(self, **kwargs: Any) -> MessagesResponse:
        self.native_tool_names.append([
            str(tool.get("name") or "") for tool in kwargs.get("tools") or []
        ])
        return self._native.popleft()


def _tool_reply(name: str, arguments: dict[str, Any], call_id: str) -> MessagesResponse:
    return MessagesResponse(
        content=[{
            "type": "tool_use",
            "id": call_id,
            "name": name,
            "input": arguments,
        }],
        stop_reason="tool_use",
    )


def _final_reply(output: dict[str, Any]) -> MessagesResponse:
    return MessagesResponse(
        content=[{"type": "text", "text": json.dumps(output)}],
        stop_reason="end_turn",
    )


def _descriptor(
    name: str,
    calls: list[ToolCall],
    *,
    schema: dict[str, Any] | None = None,
    risk: RiskLevel = RiskLevel.READ,
    scope: PermissionScope = PermissionScope.NONE,
    auto_approve: bool = True,
):
    return make_native_descriptor(
        name=name,
        description=f"fixture {name}",
        input_schema=schema or {"type": "object"},
        handler=lambda call: (
            calls.append(call)
            or ToolResult.from_json(
                tool_use_id=call.id,
                name=call.name,
                data={"accepted": call.arguments},
            )
        ),
        risk=risk,
        permission_scope=scope,
        auto_approve=auto_approve,
    )


def _runtime(
    tmp_path: Path,
    gateway: _DualProtocolGateway,
    descriptors: list[Any],
) -> tuple[SubAgentRuntime, NativeToolExecutor]:
    registry = ToolRegistry()
    registry.register_all(descriptors)
    executor = NativeToolExecutor(
        registry=registry,
        permission_engine=PermissionEngine(),
        permission_context=PermissionContext(mode=PermissionMode.DEFAULT),
    )
    return (
        SubAgentRuntime(
            config=Config(paths=WorkspacePaths(tmp_path), data={}),
            skills=SimpleNamespace(registry=_EmptySkillRegistry()),
            llm=gateway,  # type: ignore[arg-type]
            tool_registry=registry,
            tool_executor=executor,
            require_tool_executor=True,
        ),
        executor,
    )


def _spec(tmp_path: Path, *allowed_tools: str) -> SubAgentSpec:
    return SubAgentSpec(
        name="differential_child",
        prompt_path=tmp_path / "differential_child.agent.md",
        prompt="Follow the tool and output contract.",
        tier="light",
        execution_policy=SubAgentExecutionPolicy(
            native_tool_allow=list(allowed_tools),
            max_iterations=5,
            max_skill_calls=5,
            max_wall_seconds=120.0,
            llm_max_attempts=1,
        ),
    )


def _run_pair(
    runtime: SubAgentRuntime,
    spec: SubAgentSpec,
    *,
    cancel_tokens: tuple[CancelToken, CancelToken] | None = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    kwargs = {
        "trigger_event_id": "event-1",
        "payload": {"task": "exercise the child runtime"},
        "session_id": "session-1",
        "turn_id": "turn-1",
    }
    legacy_token, native_token = cancel_tokens or (None, None)
    # TDD seam: production must keep this selector explicit until legacy is deleted.
    legacy = runtime.run(
        spec,
        runtime_mode="legacy",
        cancel_token=legacy_token,
        **kwargs,
    )
    native = runtime.run(
        spec,
        runtime_mode="native",
        cancel_token=native_token,
        **kwargs,
    )
    return legacy, native


def _records(result: dict[str, Any], key: str) -> list[dict[str, Any]]:
    records = result["metrics"].get(key) or []
    assert isinstance(records, list)
    return records


def test_native_child_matches_legacy_tool_success_and_structured_final(tmp_path: Path):
    expected = {
        "done": True,
        "summary": "probe complete",
        "artifact": {"value": 7},
    }
    gateway = _DualProtocolGateway(
        legacy=[
            {
                "skill_calls": [{"skill": "probe", "payload": {"value": 7}}],
                "replan": True,
            },
            expected,
        ],
        native=[
            _tool_reply("probe", {"value": 7}, "toolu_native_probe"),
            _final_reply(expected),
        ],
    )
    calls: list[ToolCall] = []
    runtime, _executor = _runtime(
        tmp_path,
        gateway,
        [_descriptor("probe", calls)],
    )

    legacy, native = _run_pair(runtime, _spec(tmp_path, "probe"))

    assert [call.arguments for call in calls] == [{"value": 7}, {"value": 7}]
    for result in (legacy, native):
        assert {
            key: result["output"].get(key)
            for key in ("done", "summary", "artifact")
        } == expected
        records = _records(result, "skill_calls")
        assert [row["skill"] for row in records] == ["probe"]
        assert records[0]["result"]["data"] == {"accepted": {"value": 7}}
        assert _records(result, "rejected_actions") == []


def test_native_child_matches_legacy_schema_error_and_repair(tmp_path: Path):
    final = {"done": True, "summary": "repaired"}
    bad = {}
    good = {"value": "fixed"}
    gateway = _DualProtocolGateway(
        legacy=[
            {"skill_calls": [{"skill": "probe", "payload": bad}], "replan": True},
            {"skill_calls": [{"skill": "probe", "payload": good}], "replan": True},
            final,
        ],
        native=[
            _tool_reply("probe", bad, "toolu_native_bad"),
            _tool_reply("probe", good, "toolu_native_fixed"),
            _final_reply(final),
        ],
    )
    calls: list[ToolCall] = []
    runtime, _executor = _runtime(
        tmp_path,
        gateway,
        [
            _descriptor(
                "probe",
                calls,
                schema={
                    "type": "object",
                    "properties": {"value": {"type": "string"}},
                    "required": ["value"],
                },
            )
        ],
    )

    spec = _spec(tmp_path, "probe")
    # A short production-style child budget must still leave one repair round.
    spec.execution_policy.max_wall_seconds = 30.0
    legacy, native = _run_pair(runtime, spec)

    assert [call.arguments for call in calls] == [good, good]
    for result in (legacy, native):
        assert result["output"]["summary"] == "repaired"
        assert [row["skill"] for row in _records(result, "skill_calls")] == [
            "probe"
        ]
        rejected = _records(result, "rejected_actions")
        assert [(row["skill"], row["error_kind"]) for row in rejected] == [
            ("probe", "schema_validation")
        ]


def test_native_child_matches_legacy_permission_pending(tmp_path: Path):
    gateway = _DualProtocolGateway(
        legacy=[{
            "skill_calls": [{
                "skill": "protected_probe",
                "payload": {"command": "inspect"},
            }],
            "replan": True,
        }],
        native=[_tool_reply(
            "protected_probe",
            {"command": "inspect"},
            "toolu_native_pending",
        )],
    )
    calls: list[ToolCall] = []
    runtime, _executor = _runtime(
        tmp_path,
        gateway,
        [
            _descriptor(
                "protected_probe",
                calls,
                schema={
                    "type": "object",
                    "properties": {"command": {"type": "string"}},
                    "required": ["command"],
                },
                risk=RiskLevel.EXEC,
                scope=PermissionScope.SYSTEM,
                auto_approve=False,
            )
        ],
    )

    legacy, native = _run_pair(runtime, _spec(tmp_path, "protected_probe"))

    assert calls == []
    assert len(gateway.legacy_prompts) == 1
    assert len(gateway.native_tool_names) == 1
    for result in (legacy, native):
        rejected = _records(result, "rejected_actions")
        assert len(rejected) == 1
        assert rejected[0]["skill"] == "protected_probe"
        assert rejected[0]["error_kind"] == "permission_pending"
        assert result["close_reason"] == "approval_pending"
        assert result["output"]["error_kind"] == "approval_pending"
        assert result["output"]["done"] is False


def test_native_child_matches_legacy_midflight_cancel(tmp_path: Path):
    gateway = _DualProtocolGateway(
        legacy=[{
            "skill_calls": [{"skill": "cancel_probe", "payload": {}}],
            "replan": True,
        }],
        native=[_tool_reply("cancel_probe", {}, "toolu_native_cancel")],
    )
    calls: list[ToolCall] = []

    def cancel_during_call(call: ToolCall) -> ToolResult:
        calls.append(call)
        call.metadata["cancel_token"].cancel("operator_stop")
        return ToolResult.from_json(
            tool_use_id=call.id,
            name=call.name,
            data={"ok": True},
        )

    descriptor = make_native_descriptor(
        name="cancel_probe",
        description="cancel during execution",
        input_schema={"type": "object"},
        handler=cancel_during_call,
        risk=RiskLevel.READ,
        permission_scope=PermissionScope.NONE,
        auto_approve=True,
    )
    runtime, _executor = _runtime(tmp_path, gateway, [descriptor])
    tokens = (CancelToken(), CancelToken())

    legacy, native = _run_pair(
        runtime,
        _spec(tmp_path, "cancel_probe"),
        cancel_tokens=tokens,
    )

    assert len(calls) == 2
    assert len(gateway.legacy_prompts) == 1
    assert len(gateway.native_tool_names) == 1
    for result in (legacy, native):
        assert result["cancelled"] is True
        assert result["close_reason"] == "operator_stop"
        assert result["output"]["error_kind"] == "cancelled"


def test_native_child_matches_legacy_tool_allowlist(tmp_path: Path):
    final = {"done": True, "summary": "blocked tool was not run"}
    gateway = _DualProtocolGateway(
        legacy=[
            {
                "skill_calls": [{"skill": "blocked_probe", "payload": {}}],
                "replan": True,
            },
            final,
        ],
        native=[
            _tool_reply("blocked_probe", {}, "toolu_native_blocked"),
            _final_reply(final),
        ],
    )
    calls: list[ToolCall] = []
    runtime, _executor = _runtime(
        tmp_path,
        gateway,
        [
            _descriptor("probe", calls),
            _descriptor("blocked_probe", calls),
        ],
    )

    legacy, native = _run_pair(runtime, _spec(tmp_path, "probe"))

    assert calls == []
    assert "probe" in gateway.legacy_prompts[0]
    assert "blocked_probe" not in gateway.legacy_prompts[0]
    assert gateway.native_tool_names[0] == ["probe"]
    assert legacy["output"]["summary"] == final["summary"]
    assert native["output"]["summary"] == final["summary"]
    # Both runtimes record the model-emitted disallowed action and fail closed;
    # native keeps the canonical tool-use/tool-result pair for observability.
    assert len(_records(legacy, "rejected_actions")) == 1
    assert _records(legacy, "rejected_actions")[0]["skill"] == "blocked_probe"
    assert len(_records(native, "rejected_actions")) == 1
    assert _records(native, "rejected_actions")[0]["skill"] == "blocked_probe"
    assert _records(native, "rejected_actions")[0]["error_kind"] == "permission_denied"


def test_native_child_enforces_tool_budget_within_one_provider_batch(tmp_path: Path):
    final = {"done": True, "summary": "one call allowed"}
    gateway = _DualProtocolGateway(
        legacy=[
            {
                "skill_calls": [
                    {"skill": "probe", "payload": {"i": 1}},
                    {"skill": "probe", "payload": {"i": 2}},
                ],
                "replan": True,
            },
            final,
        ],
        native=[
            MessagesResponse(
                content=[
                    {
                        "type": "tool_use",
                        "id": "toolu_native_one",
                        "name": "probe",
                        "input": {"i": 1},
                    },
                    {
                        "type": "tool_use",
                        "id": "toolu_native_two",
                        "name": "probe",
                        "input": {"i": 2},
                    },
                ],
                stop_reason="tool_use",
            ),
            _final_reply(final),
        ],
    )
    calls: list[ToolCall] = []
    runtime, _executor = _runtime(tmp_path, gateway, [_descriptor("probe", calls)])
    spec = _spec(tmp_path, "probe")
    spec.execution_policy.max_skill_calls = 1
    spec.execution_policy.max_iterations = 3

    legacy, native = _run_pair(runtime, spec)

    assert len(calls) == 2  # one execution in each runtime
    assert len(_records(legacy, "rejected_actions")) == 1
    assert _records(legacy, "rejected_actions")[0]["reason"] == (
        "skill_call_budget_exhausted"
    )
    assert len(_records(native, "skill_calls")) == 1
    rejected = _records(native, "rejected_actions")
    assert len(rejected) == 1
    assert rejected[0]["skill"] == "probe"
    assert rejected[0]["error_kind"] == "aborted"
