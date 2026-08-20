from __future__ import annotations

import json

import pytest

from nerya.agent.loop import LoopConfig, WorkspaceNativeAgentLoop
from nerya.agent.loop_state import LoopRunState, TurnCheckpoint
from nerya.agent.runtime import GateDecision
from nerya.agent.tool_phase import (
    ToolBatchPhase,
    ToolBatchState,
    tool_call_fingerprint,
)
from nerya.agent.transcript_blocks import BlockEnvelope, TextBlock
from nerya.llm.messages import MessagesResponse
from nerya.tools.executor import NativeToolExecutor
from nerya.tools.orchestrator import ToolOrchestrator
from nerya.tools.permissions import PermissionContext, PermissionEngine, PermissionMode
from nerya.tools.registry import ToolRegistry
from nerya.tools.types import (
    ContextModifier,
    PermissionScope,
    RiskLevel,
    ToolCall,
    ToolDescriptor,
    ToolError,
    ToolErrorKind,
    ToolResult,
)


pytestmark = pytest.mark.smoke


class _ContinuationGateway:
    def __init__(self) -> None:
        self.calls: list[dict] = []

    def call_messages(self, **kwargs):  # noqa: ANN001
        self.calls.append(kwargs)
        if len(self.calls) == 1:
            return MessagesResponse(
                content=[{"type": "text", "text": "draft"}],
                stop_reason="end_turn",
                usage={"input_tokens": 10, "output_tokens": 2},
                provider="mock",
                model="checkpoint-model",
                usd_cost=0.01,
            )
        if len(self.calls) == 2:
            return MessagesResponse(
                content=[{"type": "text", "text": "evidence-backed"}],
                stop_reason="end_turn",
                usage={"input_tokens": 20, "output_tokens": 4},
                provider="mock",
                model="checkpoint-model",
                usd_cost=0.02,
            )
        raise AssertionError("gateway response script exhausted")


class _ContinueOnceGate:
    max_rounds = 2

    def __init__(self) -> None:
        self.calls = 0

    def evaluate(self, _snapshot):  # noqa: ANN001
        self.calls += 1
        if self.calls == 1:
            return GateDecision.continue_(
                "include verified evidence",
                reason="draft_missing_evidence",
            )
        return GateDecision.complete(reason="evidence_present")


def _empty_loop(
    gateway: _ContinuationGateway,
    *,
    max_iterations: int = 3,
) -> WorkspaceNativeAgentLoop:
    registry = ToolRegistry()
    executor = NativeToolExecutor(
        registry=registry,
        permission_engine=PermissionEngine(),
        permission_context=PermissionContext(mode=PermissionMode.AUTO),
    )
    return WorkspaceNativeAgentLoop(
        gateway=gateway,  # type: ignore[arg-type]
        registry=registry,
        orchestrator=ToolOrchestrator(
            registry=registry,
            executor=executor,
        ),
        config=LoopConfig(max_iterations=max_iterations),
    )


class _RecordingOrchestrator:
    def __init__(self) -> None:
        self.calls: list[ToolCall] = []

    def run_batch(self, calls: list[ToolCall]):  # noqa: ANN201
        self.calls.extend(calls)
        raise AssertionError("checkpointed calls must not reach the orchestrator")


def test_real_loop_continuation_reuses_transcript_and_accumulates_usage() -> None:
    gateway = _ContinuationGateway()
    loop = _empty_loop(gateway)

    outcome = loop.run(
        system="system",
        user_message="inspect the workspace",
        turn_id="turn-live-checkpoint",
        completion_gate=_ContinueOnceGate(),
    )

    assert len(gateway.calls) == 2
    second_messages = gateway.calls[1]["messages"]
    assert sum(
        1
        for message in second_messages
        if message.get("role") == "user"
        and message.get("content") == "inspect the workspace"
    ) == 1
    assert any(
        message.get("role") == "assistant"
        and any(
            block.get("type") == "text" and block.get("text") == "draft"
            for block in message.get("content", [])
            if isinstance(block, dict)
        )
        for message in second_messages
        if isinstance(message, dict) and isinstance(message.get("content"), list)
    )
    continuation_messages = [
        message
        for message in second_messages
        if isinstance(message, dict)
        and message.get("role") == "user"
        and "[completion gate continuation]" in str(message.get("content") or "")
    ]
    assert len(continuation_messages) == 1
    assert "include verified evidence" in continuation_messages[0]["content"]
    assert continuation_messages[0]["pinned"] is True

    assert outcome.final_text == "evidence-backed"
    assert outcome.completion_status == "complete"
    assert outcome.completion_reason == "evidence_present"
    assert outcome.completion_rounds == 2
    assert outcome.iterations == 2
    assert outcome.llm_calls == 2
    assert outcome.input_tokens_total == 30
    assert outcome.output_tokens_total == 6
    assert outcome.usd_total == pytest.approx(0.03)
    assert outcome.checkpoint is not None
    assert outcome.checkpoint.turn_id == "turn-live-checkpoint"
    assert outcome.checkpoint.iterations == 2
    assert outcome.checkpoint.resume_count == 1

    sequences = [block.seq for block in outcome.blocks]
    assert sequences == sorted(sequences)
    assert len(sequences) == len(set(sequences))
    assert {block.turn_id for block in outcome.blocks} == {
        "turn-live-checkpoint"
    }
    assert len({block.message_id for block in outcome.blocks}) == 1


def test_checkpoint_only_resume_uses_persisted_turn_identity() -> None:
    gateway = _ContinuationGateway()
    loop = _empty_loop(gateway)

    first = loop.run(
        system="system",
        user_message="inspect the workspace",
        turn_id="turn-direct-checkpoint",
    )
    assert first.checkpoint is not None

    resumed = loop.run(
        system="system",
        user_message="inspect the workspace",
        checkpoint=first.checkpoint,
        continuation_feedback="continue from checkpoint",
    )

    assert resumed.final_text == "evidence-backed"
    assert resumed.checkpoint is not None
    assert resumed.checkpoint.turn_id == "turn-direct-checkpoint"
    assert resumed.checkpoint.message_id == first.checkpoint.message_id
    assert resumed.checkpoint.resume_count == 1


def test_turn_checkpoint_rejects_unknown_version() -> None:
    with pytest.raises(ValueError, match="unsupported turn checkpoint version"):
        TurnCheckpoint.from_dict({"version": 2})


def test_turn_checkpoint_json_round_trip_preserves_resume_state() -> None:
    state = LoopRunState.new(
        turn_id="turn-1",
        message_id="message-1",
        deadline_epoch=1234.5,
        original_user_text="inspect the workspace",
        context_window=128_000,
    )
    result = ToolResult.from_error(
        tool_use_id="call-1",
        name="write_file",
        error=ToolError(
            kind=ToolErrorKind.STALE_FILE,
            message="read before edit",
            detail={"path": "README.md"},
            retryable=True,
            recovery_hint={"action": "read_file_first"},
        ),
    )
    result.context_modifiers.append(
        ContextModifier(
            kind="file_read",
            path="README.md",
            payload={"sha256": "abc"},
        )
    )
    result.metadata["bytes"] = 42
    fingerprint = "write_file:{\"path\":\"README.md\"}"

    state.transcript.extend(
        [
            {"role": "user", "content": "inspect the workspace"},
            {
                "role": "user",
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": "call-1",
                        "content": [{"type": "text", "text": "read before edit"}],
                    }
                ],
            },
        ]
    )
    state.blocks.append(
        BlockEnvelope(
            seq=1,
            turn_id="turn-1",
            message_id="message-1",
            role="assistant",
            block=TextBlock(text="draft").as_dict(),
            ts=100.0,
        )
    )
    state.seq = 1
    state.iterations = 2
    state.total_tool_calls = 1
    state.error_count = 1
    state.tool_result_by_fingerprint[fingerprint] = result
    state.completed_tool_results.append(result)
    state.recent_tool_fingerprints.append(fingerprint)
    state.deduped_counts_by_fingerprint[fingerprint] = 1
    state.recovery_required_args_by_tool["write_file"] = ("path",)
    state.attempted_tool_names.add("write_file")
    state.required_next_tool_names.add("read_file")
    state.next_action_nudges.add(("read_file",))
    state.required_artifact_announcements.add(("write_file",))
    state.interrupted_required_tool_retry_keys.add(("read_file",))
    state.transient_required_tool_retry_keys.add((("read_file",), 2, 1))
    state.truncated_no_tool_retry_used = True
    state.wall_time_final_synthesis_used = True
    state.preserved_pre_tool_answer = "draft"
    state.last_optional_tool_gap_notes.append("write_file: stale")
    state.recent_text_lengths.extend([12, 20])
    state.usage.llm_calls = 2
    state.usage.input_tokens_total = 100
    state.usage.output_tokens_total = 25
    state.usage.model_calls.append({"provider": "mock", "model": "test"})
    assert state.attempt_budget.claim("transport_retry")
    assert state.attempt_budget.claim("transient_retry")
    state.steer_message_count = 1
    state.stop_reason = "end_turn"
    state.transition_reason = "model_done"
    state.final_text = "draft"

    checkpoint = state.to_checkpoint(resumable=True)
    payload = json.loads(json.dumps(checkpoint.asdict()))
    restored_checkpoint = TurnCheckpoint.from_dict(payload)
    restored = LoopRunState.from_checkpoint(restored_checkpoint)

    assert restored.turn_id == "turn-1"
    assert restored.message_id == "message-1"
    assert restored.iterations == 2
    assert restored.total_tool_calls == 1
    assert restored.usage.total_tokens == 125
    assert restored.attempt_budget.used == 2
    assert restored.attempt_budget.remaining == 6
    assert restored.attempt_budget.by_reason == {
        "transport_retry": 1,
        "transient_retry": 1,
    }
    assert restored.tool_result_by_fingerprint[fingerprint].error is not None
    assert (
        restored.tool_result_by_fingerprint[fingerprint].error.kind
        is ToolErrorKind.STALE_FILE
    )
    assert restored.completed_tool_results[0].context_modifiers[0].path == "README.md"
    assert restored.required_next_tool_names == {"read_file"}
    assert restored.transient_required_tool_retry_keys == {
        (("read_file",), 2, 1)
    }
    assert restored.blocks[0].block["text"] == "draft"

    message = restored.prepare_continuation("include verified evidence")

    assert message.endswith("include verified evidence")
    assert restored.resume_count == 1
    assert restored.attempt_budget.used == 2
    assert restored.attempt_budget.remaining == 6
    assert restored.final_text == ""
    assert restored.stop_reason == ""
    assert restored.recent_text_lengths == []
    assert restored.checkpointed_fingerprints == {fingerprint}
    assert restored.transcript[-1]["pinned"] is True


def test_checkpointed_read_only_fingerprint_can_refresh_evidence() -> None:
    executions: list[str] = []
    call = ToolCall(
        id="call-new",
        name="read_status",
        arguments={"scope": "workspace"},
        turn_id="turn-1",
    )
    fingerprint = tool_call_fingerprint(call)
    prior = ToolResult.from_text(
        tool_use_id="call-old",
        name="read_status",
        text="old status",
    )
    registry = ToolRegistry()
    registry.register(ToolDescriptor(
        name="read_status",
        description="Read workspace status.",
        input_schema={"type": "object"},
        handler=lambda current: (
            executions.append(current.id)
            or ToolResult.from_text(
                tool_use_id=current.id,
                name=current.name,
                text="fresh status",
            )
        ),
        risk=RiskLevel.READ,
        permission_scope=PermissionScope.NONE,
        read_only=True,
        auto_approve=True,
    ))
    executor = NativeToolExecutor(
        registry=registry,
        permission_engine=PermissionEngine(),
        permission_context=PermissionContext(mode=PermissionMode.AUTO),
    )
    state = ToolBatchState(
        allowed_tool_names={"read_status"},
        provider_tool_names={"read_status"},
        required_next_tool_names=set(),
        attempted_tool_names=set(),
        successful_tool_names={"read_status"},
        completed_tool_results=[prior],
        tool_result_by_fingerprint={fingerprint: prior},
        recent_tool_fingerprints=[fingerprint],
        deduped_counts_by_fingerprint={},
        checkpointed_fingerprints={fingerprint},
        repeated_tool_threshold=3,
    )

    effects = ToolBatchPhase(
        orchestrator=ToolOrchestrator(registry=registry, executor=executor),
        registry=registry,
    ).run([call], state=state)

    assert executions == ["call-new"]
    assert effects.batch.results[0].is_error is False
    assert effects.batch.results[0].text() == "fresh status"
    assert state.tool_result_by_fingerprint[fingerprint].tool_use_id == "call-new"


def test_checkpointed_tool_fingerprint_reuses_prior_result_without_execution() -> None:
    call = ToolCall(
        id="call-new",
        name="write_file",
        arguments={"path": "README.md", "content": "updated"},
        turn_id="turn-1",
    )
    fingerprint = tool_call_fingerprint(call)
    prior = ToolResult.from_json(
        tool_use_id="call-old",
        name="write_file",
        data={"ok": True, "path": "README.md"},
        semantic_success=True,
    )
    orchestrator = _RecordingOrchestrator()
    state = ToolBatchState(
        allowed_tool_names={"write_file"},
        provider_tool_names={"write_file"},
        required_next_tool_names=set(),
        attempted_tool_names=set(),
        successful_tool_names={"write_file"},
        completed_tool_results=[prior],
        tool_result_by_fingerprint={fingerprint: prior},
        recent_tool_fingerprints=[fingerprint],
        deduped_counts_by_fingerprint={},
        checkpointed_fingerprints={fingerprint},
        repeated_tool_threshold=3,
    )

    effects = ToolBatchPhase(
        orchestrator=orchestrator,  # type: ignore[arg-type]
        registry=ToolRegistry(),
    ).run([call], state=state)

    assert orchestrator.calls == []
    assert len(effects.batch.results) == 1
    deduped = effects.batch.results[0]
    assert deduped.is_error is True
    assert deduped.error is not None
    assert deduped.error.kind is ToolErrorKind.DEDUPED
    assert deduped.error.detail["prior_tool_use_id"] == "call-old"
    assert state.tool_result_by_fingerprint[fingerprint] is prior
    assert state.total_tool_calls == 1
