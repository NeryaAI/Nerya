"""Root and child use the same provider loop and host completion boundary."""
from __future__ import annotations

import pytest

from nerya.agent.runtime import (
    AgentRuntime, GateDecision, GateStatus, RuntimeRequest, TurnSnapshot,
    normalize_gate_decision,
)
from nerya.harness.cancellation import CancelToken
from test_agent_loop_final_summary import _loop, _descriptor, _json_result
from test_subagent_native_runtime import Gateway, call, final, runtime, spec, run

pytestmark = pytest.mark.smoke


@pytest.mark.parametrize("child", [False, True])
def test_root_and_child_continue_with_same_checkpoint_and_feedback(tmp_path, child):
    gateway = Gateway(call(), final("draft"), final("evidence-backed"))
    snapshots, executions = [], []
    def handler(c):
        executions.append(c.id)
        return _json_result(c, {"ok": True, "evidence": "verified"})
    def gate(snapshot):
        snapshots.append(snapshot)
        return GateDecision.continue_("include observed evidence") if len(snapshots) == 1 else GateDecision.complete(reason="verified")
    if child:
        output = run(runtime(tmp_path, gateway, [_descriptor("probe", handler)]), spec(tmp_path), completion_gate=gate)
        assert output["completion_status"] == "complete"
        assert output["completion_rounds"] == 2
        assert output["output"]["summary"] == "evidence-backed"
    else:
        output = _loop(gateway, [_descriptor("probe", handler)], max_iterations=5).run(
            system="system", user_message="task", completion_gate=gate)
        assert output.completion_status == "complete"
        assert output.completion_rounds == 2
        assert "evidence-backed" in output.final_text
    assert executions == ["one"]
    assert len(gateway.calls) == 3
    assert [len(s.tool_results) for s in snapshots] == [1, 1]
    assert "include observed evidence" in str(gateway.calls[-1]["messages"])


@pytest.mark.parametrize("child", [False, True])
@pytest.mark.parametrize("when", ["before", "gate"])
def test_cancellation_prevents_completion_continuation(tmp_path, child, when):
    token = CancelToken()
    gateway = Gateway(final("draft"))
    gate_calls = []
    def gate(snapshot):
        gate_calls.append(snapshot)
        token.cancel("operator_stop")
        return GateDecision.continue_("must not continue")
    if when == "before":
        token.cancel("operator_stop")
    if child:
        output = run(runtime(tmp_path, gateway), spec(tmp_path), completion_gate=gate, cancel_token=token)
        assert output["cancelled"] is True
        assert output["completion_status"] == "blocked"
    else:
        output = _loop(gateway, []).run(system="system", user_message="task", completion_gate=gate, cancel_token=token)
        assert output.aborted is True
        assert output.completion_reason == "cancelled"
    assert len(gateway.calls) == len(gate_calls) == (0 if when == "before" else 1)


def test_shared_runtime_requires_stateful_continuation():
    calls = []
    result = AgentRuntime().run(RuntimeRequest(max_rounds=2),
        lambda snapshot: GateDecision.continue_("try again"),
        execute=lambda feedback: calls.append(feedback) or "draft")
    assert calls == [""]
    assert result.rounds == 1
    assert result.decision.reason == "stateful_continuation_required"


def test_shared_runtime_uses_explicit_stateful_continuation():
    calls, gates = [], []
    def gate(snapshot):
        gates.append(snapshot)
        return GateDecision.continue_("include evidence") if len(gates) == 1 else GateDecision.complete(reason="verified")
    result = AgentRuntime().run(RuntimeRequest(max_rounds=2), gate,
        execute=lambda feedback: calls.append(("initial", feedback)) or "draft",
        continue_from=lambda previous, feedback: calls.append((previous, feedback)) or "verified")
    assert calls == [("initial", ""), ("draft", "include evidence")]
    assert result.value == "verified"
    assert result.rounds == 2
    assert result.decision.status == "complete"


@pytest.mark.parametrize("phase", ["execute", "snapshot", "gate"])
@pytest.mark.parametrize("stop", ["cancel", "timeout"])
def test_runtime_host_limits_win_at_every_callback_boundary(phase, stop):
    token, now, calls = CancelToken(), [0.0], []
    def boundary(name):
        calls.append(name)
        if name == phase:
            if stop == "cancel":
                token.cancel("operator_stop")
            else:
                now[0] = 2.0
    def execute(feedback):
        boundary("execute")
        return "evidence"
    def snapshot(value, iteration):
        boundary("snapshot")
        return TurnSnapshot(iteration=iteration, output=value)
    def gate(snapshot):
        boundary("gate")
        return GateDecision.complete()
    result = AgentRuntime(clock=lambda: now[0]).run(
        RuntimeRequest(max_rounds=2, max_wall_seconds=1.0, cancel=token),
        gate, execute=execute, snapshot=snapshot)
    assert result.decision.status == "blocked"
    assert result.decision.reason == ("cancelled" if stop == "cancel" else "runtime_wall_time_exceeded")
    assert result.rounds == 1
    assert result.value == "evidence"
    assert len(result.snapshots) == 1
    assert calls == (["execute", "snapshot", "gate"] if phase == "gate" else ["execute", "snapshot"])


def test_gate_status_enum_is_normalized():
    assert GateDecision(GateStatus.COMPLETE).status == "complete"
    assert normalize_gate_decision({"message": "missing evidence"}).status == "blocked"
    assert normalize_gate_decision("unexpected text").reason == "completion_gate_invalid"


@pytest.mark.parametrize("phase,expected", [("snapshot", "snapshot_error"), ("gate", "completion_gate_error"),
    ("continue", "continuation_error")])
def test_runtime_exceptions_are_explicit_blocked_outcomes(phase, expected):
    def fail(*args):
        raise RuntimeError("fixture boundary failure")
    result = AgentRuntime().run(RuntimeRequest(max_rounds=2),
        fail if phase == "gate" else lambda s: GateDecision.continue_("continue"),
        execute=lambda feedback: "evidence", snapshot=fail if phase == "snapshot" else None,
        continue_from=fail)
    assert result.decision.reason == expected
    assert result.decision.status == "blocked"
