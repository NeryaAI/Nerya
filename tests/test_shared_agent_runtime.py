"""Differential contract for the root/child shared runtime adapter."""

from __future__ import annotations

from pathlib import Path

import pytest

from nerya.agent.loop import LoopConfig, LoopOutcome, WorkspaceNativeAgentLoop
from nerya.agent.runtime import (
    AgentRuntime,
    GateDecision,
    GateStatus,
    RuntimeRequest,
    normalize_gate_decision,
)
from nerya.harness.cancellation import CancelToken
from nerya.subagents.registry import SubAgentSpec
from nerya.subagents.runtime import SubAgentRuntime


pytestmark = pytest.mark.smoke


class _EvidenceGate:
    max_rounds = 2

    def __init__(self) -> None:
        self.snapshots = []

    def evaluate(self, snapshot):  # noqa: ANN001
        self.snapshots.append(snapshot)
        if len(self.snapshots) == 1:
            return GateDecision.continue_("include the observed evidence")
        return GateDecision.complete(reason="evidence_present")


def test_root_and_child_share_gate_lifecycle_and_feedback(monkeypatch) -> None:
    """The legacy engines can be swapped independently behind one contract."""

    root = WorkspaceNativeAgentLoop.__new__(WorkspaceNativeAgentLoop)
    root.config = LoopConfig()
    root_messages: list[object] = []

    def root_legacy(**kwargs):  # noqa: ANN003
        root_messages.append(kwargs["user_message"])
        final = "draft" if len(root_messages) == 1 else "evidence-backed"
        return LoopOutcome(
            transcript=[
                {"role": "user", "content": "task"},
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "tool_result",
                            "tool_use_id": "probe-1",
                            "content": [{"type": "text", "text": "ok"}],
                        }
                    ],
                },
            ],
            iterations=1,
            stop_reason="end_turn",
            final_text=final,
            tool_calls=1,
            error_count=0,
        )

    monkeypatch.setattr(root, "_run_legacy", root_legacy)

    child = SubAgentRuntime.__new__(SubAgentRuntime)
    child_payloads: list[dict] = []

    def child_legacy(spec, **kwargs):  # noqa: ANN001
        child_payloads.append(dict(kwargs["payload"]))
        final = "draft" if len(child_payloads) == 1 else "evidence-backed"
        return {
            "subagent": spec.name,
            "output": {"summary": final},
            "metrics": {
                "skill_calls": [
                    {"skill": "probe", "action": "read", "ok": True}
                ],
                "rejected_actions": [],
            },
            "tokens": 1,
            "usd": 0.0,
            "steps": [],
            "audit": {"prompt_records": []},
        }

    monkeypatch.setattr(child, "_run_legacy", child_legacy)
    spec = SubAgentSpec(name="analyst", prompt_path=Path("/tmp/analyst.agent"))

    root_gate = _EvidenceGate()
    child_gate = _EvidenceGate()
    root_outcome = root.run(
        system="system",
        user_message="task",
        completion_gate=root_gate,
    )
    child_output = child.run(
        spec,
        trigger_event_id=None,
        payload={"subject": "BTC"},
        completion_gate=child_gate,
    )

    assert root_outcome.completion_status == "blocked"
    assert child_output["completion"]["status"] == "blocked"
    assert root_outcome.completion_rounds == child_output["completion_rounds"] == 1
    assert [len(s.tool_results) for s in root_gate.snapshots] == [1]
    assert [len(s.tool_results) for s in child_gate.snapshots] == [1]
    assert len(root_messages) == 1
    assert len(child_payloads) == 1


def test_child_runtime_stops_between_gate_rounds_when_cancelled(monkeypatch) -> None:
    """A cancellation after one attempt cannot trigger another child LLM call."""

    child = SubAgentRuntime.__new__(SubAgentRuntime)
    calls: list[dict] = []
    token = CancelToken()

    def child_legacy(spec, **kwargs):  # noqa: ANN001
        calls.append(kwargs)
        token.cancel("operator_stop")
        return {
            "subagent": spec.name,
            "output": {"summary": "first pass"},
            "metrics": {"skill_calls": [], "rejected_actions": []},
            "tokens": 1,
            "usd": 0.0,
            "steps": [],
            "audit": {"prompt_records": []},
        }

    class _ContinueGate:
        max_rounds = 3

        def evaluate(self, _snapshot):  # noqa: ANN001
            return GateDecision.continue_("continue once")

    monkeypatch.setattr(child, "_run_legacy", child_legacy)
    spec = SubAgentSpec(name="analyst", prompt_path=Path("/tmp/analyst.agent"))

    output = child.run(
        spec,
        trigger_event_id=None,
        payload={"subject": "BTC"},
        completion_gate=_ContinueGate(),
        cancel_token=token,
    )

    assert len(calls) == 1
    assert calls[0]["cancel_token"] is token
    assert output["cancelled"] is True
    assert output["completion_status"] == "blocked"
    assert output["completion"]["reason"] == "cancelled"


def test_root_runtime_does_not_reenter_legacy_loop_when_pre_cancelled(monkeypatch) -> None:
    """Pre-cancelled root turns stay inside the shared adapter boundary."""

    root = WorkspaceNativeAgentLoop.__new__(WorkspaceNativeAgentLoop)
    root.config = LoopConfig()
    token = CancelToken()
    token.cancel("operator_stop")

    def legacy_was_called(**_kwargs):  # noqa: ANN001
        raise AssertionError("pre-cancelled turn must not invoke _run_legacy")

    monkeypatch.setattr(root, "_run_legacy", legacy_was_called)

    class _NeverReachedGate:
        max_rounds = 3

        def evaluate(self, _snapshot):  # noqa: ANN001
            raise AssertionError("no snapshot should be evaluated")

    outcome = root.run(
        system="system",
        user_message="task",
        completion_gate=_NeverReachedGate(),
        cancel_token=token,
    )

    assert outcome.completion_status == "blocked"
    assert outcome.completion_reason == "cancelled"
    assert outcome.completion_rounds == 0
    assert outcome.aborted is True
    assert outcome.stop_reason == "cancelled"
    assert outcome.transition_reason == "cancelled"


def test_legacy_completion_gate_fails_closed_before_reentry(monkeypatch) -> None:
    root = WorkspaceNativeAgentLoop.__new__(WorkspaceNativeAgentLoop)
    root.config = LoopConfig(max_iterations=3)
    calls: list[object] = []

    def legacy(**_kwargs):  # noqa: ANN003
        calls.append(object())
        return LoopOutcome(
            transcript=[],
            iterations=1,
            stop_reason="end_turn",
            final_text="first pass",
            tool_calls=0,
            error_count=0,
        )

    class _ContinueGate:
        max_rounds = 2

        def evaluate(self, _snapshot):  # noqa: ANN001
            return GateDecision.continue_("need stateful evidence")

    monkeypatch.setattr(root, "_run_legacy", legacy)
    outcome = root.run(
        system="system",
        user_message="task",
        completion_gate=_ContinueGate(),
    )

    assert len(calls) == 1
    assert outcome.completion_status == "blocked"
    assert outcome.completion_reason == "stateful_continuation_required"
    assert outcome.aborted is True


def test_shared_runtime_always_requires_stateful_continuation_state() -> None:
    calls: list[str] = []

    result = AgentRuntime().run(
        RuntimeRequest(max_rounds=2),
        lambda _snapshot: GateDecision.continue_("try again"),
        execute=lambda feedback: calls.append(feedback) or "draft",
    )

    assert calls == [""]
    assert result.rounds == 1
    assert result.decision.reason == "stateful_continuation_required"


def test_runtime_cancellation_after_execute_beats_completion_gate():
    token = CancelToken()
    gate_calls = []

    def execute(_feedback):
        token.cancel("during_execute")
        return "late result"

    result = AgentRuntime().run(
        RuntimeRequest(max_rounds=2, cancel=token),
        lambda _snapshot: gate_calls.append(True) or GateDecision.complete(),
        execute=execute,
    )

    assert result.decision.status == GateStatus.BLOCKED.value
    assert result.decision.reason == "cancelled"
    assert gate_calls == []


def test_runtime_wall_timeout_after_execute_beats_completion_gate():
    ticks = iter((0.0, 2.0))
    gate_calls = []
    result = AgentRuntime(clock=lambda: next(ticks)).run(
        RuntimeRequest(max_rounds=2, max_wall_seconds=1.0),
        lambda _snapshot: gate_calls.append(True) or GateDecision.complete(),
        execute=lambda _feedback: "late result",
    )

    assert result.decision.reason == "runtime_wall_time_exceeded"
    assert gate_calls == []


def test_gate_status_enum_is_normalized():
    assert GateDecision(GateStatus.COMPLETE).status == "complete"
    assert normalize_gate_decision({"message": "missing evidence"}).status == "blocked"
    assert normalize_gate_decision("unexpected text").reason == "completion_gate_invalid"
