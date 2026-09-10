"""The tool boundary must preserve identity and never replay ambiguous effects."""
from __future__ import annotations

from types import SimpleNamespace
from functools import partial

import pytest

from nerya.agent.tool_phase import ToolBatchPhase, ToolBatchPolicy
from nerya.agent.loop_state import LoopRunState
from nerya.tools.orchestrator import BatchResult
from nerya.tools.registry import ToolRegistry
from nerya.tools.types import (
    PermissionScope, RiskLevel, ToolCall, ToolDescriptor, ToolError, ToolErrorKind, ToolResult,
)

pytestmark = pytest.mark.smoke


def _result(call: ToolCall) -> ToolResult:
    return ToolResult.from_json(
        tool_use_id=call.id, name=call.name, data={"ok": True, "value": call.id},
    )


def _phase(run_batch, *, read_only=True):
    registry = ToolRegistry()
    registry.register(ToolDescriptor(
        name="probe", description="probe", handler=_result,
        input_schema={"type": "object", "properties": {}},
        risk=RiskLevel.READ if read_only else RiskLevel.EXEC,
        permission_scope=PermissionScope.NONE if read_only else PermissionScope.WORKSPACE,
        read_only=read_only,
    ))
    phase = ToolBatchPhase(orchestrator=SimpleNamespace(run_batch=run_batch), registry=registry)
    policy = ToolBatchPolicy(frozenset({"probe"}), frozenset({"probe"}))
    return SimpleNamespace(run=partial(phase.run, policy=policy))


def _state(**overrides):
    return LoopRunState(**{
        "turn_id": "turn", "message_id": "message",
        "required_next_tool_names": set(), "attempted_tool_names": set(),
        "successful_tool_names": set(), "completed_tool_results": [],
        "tool_result_by_fingerprint": {}, "recent_tool_fingerprints": [],
        "deduped_counts_by_fingerprint": {}, **overrides,
    })


def test_reordered_results_are_matched_by_call_id_not_position():
    calls = [ToolCall(name="probe", id="first"), ToolCall(name="probe", id="second")]
    state = _state()
    effects = _phase(lambda items: BatchResult(results=[_result(c) for c in reversed(items)])).run(
        calls, state=state,
    )
    assert [result.tool_use_id for result in effects.batch.results] == ["first", "second"]
    assert state.completed_tool_results == effects.batch.results


@pytest.mark.parametrize("fault", ["missing", "duplicate", "wrong_name", "exception"])
def test_invalid_batch_output_stays_paired_and_fails_closed(fault):
    calls = [ToolCall(name="probe", id="first"), ToolCall(name="probe", id="second")]

    def dispatch(items):
        if fault == "exception":
            raise RuntimeError("executor failed after dispatch")
        results = [_result(items[1])]
        if fault == "duplicate":
            results = [_result(items[0]), _result(items[0]), _result(items[1])]
        elif fault == "wrong_name":
            results = [ToolResult.from_text(tool_use_id="first", name="other", text="wrong"), _result(items[1])]
        return BatchResult(results=results)

    effects = _phase(dispatch).run(calls, state=_state())
    assert len(effects.batch.results) == len(calls)
    assert [result.tool_use_id for result in effects.batch.results] == ["first", "second"]
    assert effects.batch.results[0].is_error
    assert effects.batch.results[0].error.retryable is False
    if fault != "exception":
        assert not effects.batch.results[1].is_error


def test_identical_write_calls_in_one_batch_execute_only_once():
    executed = []

    def dispatch(items):
        executed.extend(items)
        return BatchResult(results=[_result(call) for call in items])

    calls = [ToolCall(name="probe", id="one"), ToolCall(name="probe", id="two")]
    effects = _phase(dispatch, read_only=False).run(calls, state=_state())
    assert [call.id for call in executed] == ["one"]
    assert [result.tool_use_id for result in effects.batch.results] == ["one", "two"]
    assert effects.batch.results[1].error.kind == ToolErrorKind.DEDUPED


def test_failed_required_call_invalidates_earlier_success_for_the_same_tool():
    call = ToolCall(name="probe", id="retry")
    state = _state(successful_tool_names={"probe"}, required_next_tool_names={"probe"})
    failure = ToolResult.from_error(
        tool_use_id=call.id, name=call.name,
        error=ToolError(kind=ToolErrorKind.EXECUTION_ERROR, message="failed"),
    )
    _phase(lambda _: BatchResult(results=[failure])).run([call], state=state)
    assert "probe" not in state.successful_tool_names
    assert "probe" in state.required_next_tool_names


def test_later_failure_in_batch_cannot_leave_success_or_discharge_required_action():
    calls = [ToolCall(name="probe", id=str(i), arguments={"value": i}) for i in range(2)]
    state = _state(required_next_tool_names={"probe"})
    failure = ToolResult.from_error(
        tool_use_id=calls[1].id, name="probe",
        error=ToolError(kind=ToolErrorKind.EXECUTION_ERROR, message="failed"),
    )
    effects = _phase(lambda _: BatchResult(results=[_result(calls[0]), failure]), read_only=False).run(
        calls, state=state,
    )
    assert "probe" not in state.successful_tool_names
    assert "probe" in state.required_next_tool_names
    assert not effects.semantic_success_names
    assert not effects.completed_required_action_names


def test_uncertain_write_is_not_replayed_in_the_same_turn():
    executed = []

    def dispatch(calls):
        executed.extend(calls)
        raise RuntimeError("unknown outcome")

    phase = _phase(dispatch, read_only=False)
    state = _state()
    first = phase.run([ToolCall(name="probe", id="one")], state=state)
    second = phase.run([ToolCall(name="probe", id="two")], state=state)
    assert len(executed) == 1
    assert first.batch.results[0].error.detail["execution_state"] == "unknown"
    assert second.batch.results[0].error.kind == ToolErrorKind.DEDUPED


def test_duplicate_call_ids_fail_before_dispatch():
    executed = []
    phase = _phase(lambda calls: executed.extend(calls) or BatchResult())
    with pytest.raises(ValueError, match="unique"):
        phase.run([ToolCall(name="probe", id="same"), ToolCall(name="probe", id="same")], state=_state())
    assert not executed


def test_batch_policy_is_a_frozen_capability_snapshot():
    exposed = {"probe"}
    policy = ToolBatchPolicy(exposed, exposed)
    exposed.add("hidden")
    assert policy.allowed_tool_names == frozenset({"probe"})
    assert policy.provider_tool_names == frozenset({"probe"})
