"""One native engine for every child: behavior, not a legacy compatibility suite."""
from __future__ import annotations

import inspect
import json
from collections import deque
from dataclasses import replace
from types import SimpleNamespace

import pytest

from nerya.agent.runtime import GateDecision
from nerya.core.config import Config
from nerya.core.paths import WorkspacePaths
from nerya.harness.cancellation import CancelToken
from nerya.llm.messages import MessagesResponse
from nerya.subagents.registry import SubAgentExecutionPolicy, SubAgentSpec
from nerya.subagents.runtime import SubAgentRuntime, _native_final_output
from nerya.tools import NativeToolExecutor, PermissionContext, PermissionEngine
from nerya.tools.registry import ToolRegistry, make_native_descriptor
from nerya.tools.types import PermissionScope, RiskLevel, ToolError, ToolErrorKind, ToolResult

pytestmark = pytest.mark.smoke


class Gateway:
    def __init__(self, *responses):
        self.responses = deque(responses)
        self.calls = []

    def call_messages(self, **kwargs):
        self.calls.append(kwargs)
        response = self.responses.popleft()
        if isinstance(response, Exception):
            raise response
        return response


def final(text="verified", **fields):
    return MessagesResponse(content=[{"type": "text", "text": json.dumps({
        "summary": text, "done": True, **fields,
    })}], stop_reason="end_turn", usage={"input_tokens": 3, "output_tokens": 2},
        provider="fixture", model="native", usd_cost=0.01)


def call(name="probe", call_id="one", **arguments):
    return MessagesResponse(content=[{
        "type": "tool_use", "id": call_id, "name": name, "input": arguments,
    }], stop_reason="tool_use", usage={"input_tokens": 2, "output_tokens": 1})


def descriptor(name="probe", handler=None, **kwargs):
    return make_native_descriptor(
        name=name, description=name, input_schema={"type": "object"},
        handler=handler or (lambda c: ToolResult.from_json(
            tool_use_id=c.id, name=c.name, data={"ok": True, "value": c.arguments},
        )), risk=kwargs.pop("risk", RiskLevel.READ),
        permission_scope=kwargs.pop("scope", PermissionScope.NONE),
        auto_approve=kwargs.pop("auto_approve", True), **kwargs,
    )


def runtime(tmp_path, gateway, descriptors=()):
    registry = ToolRegistry()
    registry.register_all(descriptors)
    executor = NativeToolExecutor(registry=registry, permission_engine=PermissionEngine(),
                                  permission_context=PermissionContext())
    return SubAgentRuntime(
        config=Config(paths=WorkspacePaths(tmp_path), data={"agent": {"native": {
            "llm_retry_base_delay": 0, "llm_retry_max_delay": 0,
        }}}), skills=SimpleNamespace(registry=SimpleNamespace(list=lambda: [])),
        llm=gateway, tool_registry=registry, tool_executor=executor,
    )


def spec(tmp_path, **policy):
    return SubAgentSpec(name="specialist", prompt_path=tmp_path / "specialist.md",
        prompt="Complete the assignment using verified evidence.", execution_policy=SubAgentExecutionPolicy(
            max_iterations=policy.pop("max_iterations", 5),
            max_skill_calls=policy.pop("max_skill_calls", 5),
            max_wall_seconds=120.0, llm_max_attempts=1, **policy,
        ))


def run(rt, sp, **kwargs):
    return rt.run(sp, trigger_event_id="event", payload={"task": "verify result"},
                  session_id="session", turn_id="turn", **kwargs)


def test_there_is_only_one_engine_and_one_call_protocol():
    parameters = inspect.signature(SubAgentRuntime.run).parameters
    assert "runtime_mode" not in parameters
    assert "require_tool_executor" not in inspect.signature(SubAgentRuntime).parameters
    assert not hasattr(SubAgentRuntime, "_run_legacy")
    assert not hasattr(SubAgentRuntime, "_dispatch_native")
    assert "runtime" not in SubAgentExecutionPolicy.__dataclass_fields__


def test_real_tool_result_returns_to_model_with_usage(tmp_path):
    gateway = Gateway(call(value=7), final())
    result = run(runtime(tmp_path, gateway, [descriptor()]), spec(tmp_path))
    assert result["output"]["summary"] == "verified"
    assert result["completion_status"] == "complete"
    assert len(result["metrics"]["skill_calls"]) == 1
    assert result["tokens"] == 8
    assert sum(row["tokens"] for row in result["model_calls"]) == 8
    assert result["model"] == "native"
    assert "tool_result" in str(gateway.calls[1]["messages"])
    assert "_turn_checkpoint" not in result
    assert result["steps"][-1]["kind"] == "close"


@pytest.mark.parametrize("status", ["cancelled", "approval_pending", "max_iterations", "wall_time"])
def test_host_stop_overrules_model_claiming_done(status):
    output = _native_final_output('{"done":true,"summary":"claimed completion"}', stop_reason=status)
    assert output["done"] is False
    assert output["degraded"] is True
    assert output["error_kind"] == status


def test_schema_and_argument_defaults_are_applied_by_executor(tmp_path):
    seen = []
    desc = replace(descriptor(handler=lambda c: seen.append(c) or ToolResult.from_json(
        tool_use_id=c.id, name=c.name, data={"ok": True})), input_schema={
        "type": "object", "properties": {"value": {"type": "integer"}}, "required": ["value"],
    })
    gateway = Gateway(call(), final())
    run(runtime(tmp_path, gateway, [desc]), spec(tmp_path, tool_argument_defaults={"probe": {"value": 7}}))
    assert seen[0].arguments == {"value": 7}
    assert gateway.calls[0]["tools"][0]["input_schema"] == desc.input_schema


def test_third_distinct_source_is_allowed_after_two_failures(tmp_path):
    attempts = []
    def handler(c):
        attempts.append(c.arguments["source"])
        if c.arguments["source"] != "working":
            return ToolResult.from_error(tool_use_id=c.id, name=c.name, error=ToolError(
                kind=ToolErrorKind.NOT_FOUND, message="unavailable", retryable=False))
        return ToolResult.from_json(tool_use_id=c.id, name=c.name, data={"verified": 7})
    gateway = Gateway(*(call(call_id=s, source=s) for s in ("first", "second", "working")), final())
    result = run(runtime(tmp_path, gateway, [descriptor(handler=handler)]), spec(tmp_path))
    assert attempts == ["first", "second", "working"]
    assert result["output"]["summary"] == "verified"


@pytest.mark.parametrize("same_batch", [False, True])
def test_failures_count_against_tool_budget(tmp_path, same_batch):
    attempts = []
    def handler(c):
        attempts.append(c.id)
        return ToolResult.from_error(tool_use_id=c.id, name=c.name, error=ToolError(
            kind=ToolErrorKind.NOT_FOUND, message="not found", retryable=False))
    responses = [call(call_id=str(i), index=i) for i in range(2)]
    if same_batch:
        responses = [MessagesResponse(content=[r.content[0] for r in responses], stop_reason="tool_use")]
    gateway = Gateway(*responses, final("explicit evidence gap"))
    run(runtime(tmp_path, gateway, [descriptor(handler=handler)]), spec(tmp_path, max_skill_calls=1))
    assert len(attempts) == 1


def test_approval_after_evidence_stops_without_hidden_synthesis(tmp_path):
    gateway = Gateway(call(), call("protected", "two"))
    result = run(runtime(tmp_path, gateway, [descriptor(), descriptor("protected", risk=RiskLevel.EXEC,
                  scope=PermissionScope.SYSTEM, auto_approve=False)]), spec(tmp_path))
    assert len(gateway.calls) == 2
    assert result["close_reason"] == "approval_pending"
    assert result["output"]["done"] is False
    assert len(result["metrics"]["skill_calls"]) == 1


@pytest.mark.parametrize("stage", ["before", "provider", "tool"])
def test_cancelled_child_never_starts_more_work(tmp_path, monkeypatch, stage):
    token = CancelToken()
    seen = []
    gateway = Gateway(call(), final())
    original = gateway.call_messages
    def provider(**kwargs):
        response = original(**kwargs)
        if stage == "provider":
            token.cancel("operator_stop")
        return response
    monkeypatch.setattr(gateway, "call_messages", provider)
    def handler(c):
        seen.append(c.id)
        token.cancel("operator_stop")
        return ToolResult.from_json(tool_use_id=c.id, name=c.name, data={"ok": True})
    if stage == "before":
        token.cancel("operator_stop")
    result = run(runtime(tmp_path, gateway, [descriptor(handler=handler)]), spec(tmp_path), cancel_token=token)
    assert result["cancelled"] is True
    assert len(gateway.calls) == (0 if stage == "before" else 1)
    assert len(seen) == (1 if stage == "tool" else 0)


def test_completion_gate_continues_from_checkpoint_not_replayed_input(tmp_path):
    gateway = Gateway(call(), final("missing detail"), final("verified detail"))
    seen = []
    rt = runtime(tmp_path, gateway, [descriptor(handler=lambda c: seen.append(c.id) or ToolResult.from_json(
        tool_use_id=c.id, name=c.name, data={"ok": True}))])
    decisions = iter([GateDecision.continue_("include detail"), GateDecision.complete(reason="verified")])
    result = run(rt, spec(tmp_path), completion_gate=lambda snapshot: next(decisions))
    assert seen == ["one"]
    assert result["completion_rounds"] == 2
    assert result["completion"]["reason"] == "verified"
    assert result["output"]["summary"] == "verified detail"
    assert "include detail" in str(gateway.calls[-1]["messages"])


def test_isolated_payload_does_not_receive_tools(tmp_path):
    gateway = Gateway(final())
    run(runtime(tmp_path, gateway, [descriptor()]), spec(tmp_path), context_scope="explicit_payload_only")
    assert gateway.calls[0]["tools"] == []


def test_unavailable_required_tool_is_not_silently_dropped(tmp_path):
    gateway = Gateway(final())
    with pytest.raises(ValueError, match="required tools unavailable"):
        run(runtime(tmp_path, gateway), spec(tmp_path, required_native_tools=["missing"]))
    assert gateway.calls == []


def test_disallowed_tool_never_executes(tmp_path):
    seen = []
    gateway = Gateway(call(), final("not permitted"))
    result = run(runtime(tmp_path, gateway, [descriptor(handler=lambda c: seen.append(c))]),
                 spec(tmp_path, native_tool_allow=[]))
    assert seen == []
    assert gateway.calls[0]["tools"] == []
    assert result["metrics"]["rejected_actions"]
