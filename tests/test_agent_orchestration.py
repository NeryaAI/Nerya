"""Dispatch boundaries: explicit retry refusal, side effects, cancellation and identity."""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from nerya.harness.cancellation import CancelToken
from nerya.tools import orchestrator as module
from nerya.tools.orchestrator import ToolOrchestrator
from nerya.tools.registry import ToolRegistry
from nerya.tools.types import RiskLevel, ToolCall, ToolDescriptor, ToolError, ToolErrorKind, ToolResult

pytestmark = pytest.mark.smoke


def _orchestrator(execute, *, read_only=True):
    registry = ToolRegistry()
    registry.register(ToolDescriptor(
        name="probe", description="probe", handler=execute,
        input_schema={"type": "object"}, read_only=read_only,
        risk=RiskLevel.READ if read_only else RiskLevel.WRITE,
    ))
    return ToolOrchestrator(registry=registry, executor=SimpleNamespace(execute=execute))


def _ok(call):
    return ToolResult.from_text(tool_use_id=call.id, name=call.name, text="observed")


@pytest.mark.parametrize(("read_only", "retryable", "expected"), [
    (True, False, 1), (False, None, 1), (False, True, 1),
    (True, None, 2), (True, True, 2),
])
def test_retry_policy_respects_producer_and_side_effect_boundaries(monkeypatch, read_only, retryable, expected):
    attempts = []
    monkeypatch.setattr(module, "time", SimpleNamespace(time=lambda: 100.0, sleep=lambda _: None))

    def execute(call):
        attempts.append(call.id)
        return _ok(call) if len(attempts) > 1 else ToolResult.from_error(
            tool_use_id=call.id, name=call.name,
            error=ToolError(kind=ToolErrorKind.TIMEOUT, message="timed out", retryable=retryable),
        )

    batch = _orchestrator(execute, read_only=read_only).run_batch([ToolCall(name="probe", id="id")])
    assert len(attempts) == expected
    assert batch.auto_retries == expected - 1


@pytest.mark.parametrize("deadline", [99.0, float("nan"), "invalid"])
def test_expired_or_invalid_deadline_never_reaches_executor(monkeypatch, deadline):
    executed = []
    monkeypatch.setattr(module, "time", SimpleNamespace(time=lambda: 100.0))
    batch = _orchestrator(lambda call: executed.append(call) or _ok(call)).run_batch([
        ToolCall(name="probe", id="id", metadata={"turn_deadline_epoch": deadline}),
    ])
    assert executed == []
    assert batch.results[0].error.detail["execution_state"] == "not_started"


def test_cancel_during_serial_batch_preserves_first_result_and_skips_second():
    token = CancelToken()
    executed = []

    def execute(call):
        executed.append(call.id)
        token.cancel("operator_stop")
        return _ok(call)

    calls = [ToolCall(name="probe", id=name, metadata={"cancel_token": token}) for name in ("first", "second")]
    batch = _orchestrator(execute, read_only=False).run_batch(calls)
    assert executed == ["first"]
    assert not batch.results[0].is_error
    assert batch.results[1].error.detail["reason"] == "cancelled"


def test_cancelled_transient_failure_does_not_retry(monkeypatch):
    token = CancelToken()
    attempts = []
    monkeypatch.setattr(module, "time", SimpleNamespace(time=lambda: 100.0, sleep=lambda _: None))

    def execute(call):
        attempts.append(call.id)
        token.cancel("operator_stop")
        return ToolResult.from_error(
            tool_use_id=call.id, name=call.name,
            error=ToolError(kind=ToolErrorKind.TIMEOUT, message="timed out"),
        )

    batch = _orchestrator(execute).run_batch([ToolCall(name="probe", metadata={"cancel_token": token})])
    assert len(attempts) == 1
    assert batch.auto_retries == 0
    assert batch.results[0].error.kind == ToolErrorKind.TIMEOUT


def test_wrong_identity_does_not_replay_context_modifiers():
    from nerya.tools.types import ContextModifier
    seen = []

    def execute(_call):
        result = ToolResult.from_text(tool_use_id="wrong", name="probe", text="not ours")
        result.context_modifiers.append(ContextModifier(kind="file_mutate", path="x"))
        return result

    orchestrator = _orchestrator(execute)
    orchestrator.modifier_sink = lambda call, mod: seen.append((call, mod))
    batch = orchestrator.run_batch([ToolCall(name="probe", id="expected")])
    assert seen == []
    assert batch.results[0].tool_use_id == "expected"
    assert batch.results[0].is_error
