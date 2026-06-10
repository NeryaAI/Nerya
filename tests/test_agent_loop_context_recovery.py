"""Context-overflow recovery, token-pressure compaction, and usage telemetry.

Covers the agent-loop hardening inspired by the Codex
``ContextWindowExceeded`` → auto-compact recovery pattern:

- ``_is_context_overflow_llm_error`` classification across providers.
- Reactive compaction: a provider context-overflow error mid-turn shrinks
  the live transcript (escalating attempts) and retries the same
  iteration instead of throwing away all earned tool evidence.
- Token-pressure forced macro-compaction (``_maybe_compact(force_reason=…)``).
- Billed-token budget soft verifier (``token_budget_exceeded``).
- Provider usage telemetry on :class:`LoopOutcome`.
- Diminishing-returns soft verifier opt-in flag.
- Anti-hijack framing on compaction breadcrumbs / session checkpoints.
"""

from __future__ import annotations

import json

import pytest

from nerya.agent.loop import (
    LoopConfig,
    WorkspaceNativeAgentLoop,
    _is_context_overflow_llm_error,
    _is_transient_llm_error,
)
from nerya.agent.session_compaction import compact_session_history
from nerya.agent.transcript_compact import compact_transcript
from nerya.core.errors import LLMError, LLMStructuredOutputError
from nerya.harness.cancellation import (
    SteerInbox,
    register_steer_inbox,
    signal_steer,
    unregister_steer_inbox,
)
from nerya.llm.messages import MessagesResponse
from nerya.tools.executor import NativeToolExecutor
from nerya.tools.orchestrator import ToolOrchestrator
from nerya.tools.permissions import (
    PermissionContext,
    PermissionEngine,
    PermissionMode,
)
from nerya.tools.registry import ToolRegistry
from nerya.tools.types import (
    PermissionScope,
    RiskLevel,
    ToolDescriptor,
    ToolResult,
)

pytestmark = pytest.mark.smoke


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _payload_chars(messages: list[dict]) -> int:
    return sum(len(json.dumps(m, ensure_ascii=False, default=str)) for m in messages)


def _make_loop(gateway, *, config: LoopConfig, tool_text: str = "ok") -> WorkspaceNativeAgentLoop:
    registry = ToolRegistry()
    registry.register(
        ToolDescriptor(
            name="read_status",
            description="Read status.",
            input_schema={"type": "object", "properties": {}},
            handler=lambda call: ToolResult.from_text(
                tool_use_id=call.id,
                name=call.name,
                text=tool_text,
            ),
            risk=RiskLevel.READ,
            permission_scope=PermissionScope.NONE,
            read_only=True,
            auto_approve=True,
        )
    )
    executor = NativeToolExecutor(
        registry=registry,
        permission_engine=PermissionEngine(),
        permission_context=PermissionContext(mode=PermissionMode.AUTO),
    )
    orchestrator = ToolOrchestrator(registry=registry, executor=executor)
    return WorkspaceNativeAgentLoop(
        gateway=gateway,  # type: ignore[arg-type]
        registry=registry,
        orchestrator=orchestrator,
        config=config,
    )


def _tool_use_response(call_id: str, usage: dict | None = None) -> MessagesResponse:
    return MessagesResponse(
        content=[
            {
                "type": "tool_use",
                "id": call_id,
                "name": "read_status",
                "input": {},
            }
        ],
        stop_reason="tool_use",
        usage=dict(usage or {}),
    )


# ---------------------------------------------------------------------------
# classification
# ---------------------------------------------------------------------------


def test_context_overflow_detection_across_providers() -> None:
    overflow_messages = [
        "openai messages api error (400): context_length_exceeded",
        "This model's maximum context length is 128000 tokens",
        "prompt is too long: 210032 tokens > 200000 maximum",
        "input token count (1200000) exceeds the maximum number of tokens allowed",
        "anthropic error (413): request too large",
        "Range of input length should be [1, 245760]",
        "请求失败：输入长度超过模型限制",
    ]
    for msg in overflow_messages:
        exc = LLMError(msg)
        assert _is_context_overflow_llm_error(exc), msg
        # Overflow errors must not be classified as transient: retrying
        # the identical payload can never succeed.
        assert not _is_transient_llm_error(exc), msg


def test_context_overflow_detection_rejects_non_overflow() -> None:
    assert not _is_context_overflow_llm_error(LLMError("openai messages api error (502): bad gateway"))
    assert not _is_context_overflow_llm_error(LLMError("safety: content policy violation"))
    # Permanent subclasses stay permanent even with a matching message.
    assert not _is_context_overflow_llm_error(
        LLMStructuredOutputError("schema mismatch: context window")
    )
    assert not _is_context_overflow_llm_error(ValueError("context_length_exceeded"))


# ---------------------------------------------------------------------------
# reactive compaction recovery
# ---------------------------------------------------------------------------


class _OverflowThenRecoverGateway:
    """tool_use → context-overflow error → success after compaction."""

    def __init__(self) -> None:
        self.calls: list[int] = []
        self.overflow_raised = False

    def call_messages(self, **kwargs):  # noqa: ANN001
        messages = kwargs.get("messages") or []
        self.calls.append(_payload_chars(messages))
        if len(self.calls) == 1:
            return _tool_use_response(
                "toolu_1", usage={"input_tokens": 1000, "output_tokens": 20}
            )
        if not self.overflow_raised:
            self.overflow_raised = True
            raise LLMError(
                "openai messages api error (400): context_length_exceeded — "
                "your messages resulted in too many tokens"
            )
        return MessagesResponse(
            content=[{"type": "text", "text": "recovered final answer"}],
            stop_reason="end_turn",
            usage={"input_tokens": 500, "output_tokens": 30},
        )


def test_reactive_compact_recovers_from_context_overflow() -> None:
    gateway = _OverflowThenRecoverGateway()
    loop = _make_loop(
        gateway,
        config=LoopConfig(
            max_iterations=4,
            llm_retry_attempts=2,
            reactive_compact_max_attempts=3,
        ),
        # Big enough that the emergency microcompact pass has real
        # mass to drop even when macro-compaction cannot drop whole
        # messages from such a short transcript.
        tool_text="x" * 50_000,
    )
    outcome = loop.run(system="system", user_message="check the status and summarise")

    assert not outcome.aborted
    assert outcome.final_text == "recovered final answer"
    assert outcome.reactive_compaction_count >= 1
    assert len(gateway.calls) == 3
    # The retried request must be strictly smaller than the rejected one.
    assert gateway.calls[2] < gateway.calls[1]
    # Usage telemetry survives the recovery.
    assert outcome.llm_calls == 2
    assert outcome.input_tokens_total == 1500
    assert outcome.output_tokens_total == 50
    assert outcome.prompt_tokens_last == 500


class _AlwaysOverflowGateway:
    def __init__(self) -> None:
        self.calls = 0

    def call_messages(self, **kwargs):  # noqa: ANN001
        self.calls += 1
        if self.calls == 1:
            return _tool_use_response("toolu_1")
        raise LLMError("prompt is too long: 210032 tokens > 200000 maximum")


def test_reactive_compact_gives_up_after_max_attempts() -> None:
    gateway = _AlwaysOverflowGateway()
    loop = _make_loop(
        gateway,
        config=LoopConfig(
            max_iterations=3,
            llm_retry_attempts=2,
            reactive_compact_max_attempts=2,
        ),
        tool_text="y" * 20_000,
    )
    with pytest.raises(LLMError):
        loop.run(system="system", user_message="check the status")
    # 1 tool_use call + 1 overflow + bounded compact retries; the loop
    # must not spin past its reactive budget.
    assert gateway.calls <= 2 + 2 + 1


def test_reactive_compact_disabled_propagates_immediately() -> None:
    gateway = _AlwaysOverflowGateway()
    loop = _make_loop(
        gateway,
        config=LoopConfig(
            max_iterations=3,
            llm_retry_attempts=2,
            reactive_compact_max_attempts=0,
        ),
        tool_text="z" * 20_000,
    )
    with pytest.raises(LLMError):
        loop.run(system="system", user_message="check the status")
    assert gateway.calls == 2


# ---------------------------------------------------------------------------
# token budget soft verifier
# ---------------------------------------------------------------------------


class _BusyToolGateway:
    """Keeps requesting tools with heavy reported usage.

    Inputs vary per call so the repeated-tool fingerprint dedup never
    interferes with what these tests measure.
    """

    def __init__(self) -> None:
        self.calls = 0

    def call_messages(self, **kwargs):  # noqa: ANN001
        self.calls += 1
        return MessagesResponse(
            content=[
                {
                    "type": "tool_use",
                    "id": f"toolu_{self.calls}",
                    "name": "read_status",
                    "input": {"i": self.calls},
                }
            ],
            stop_reason="tool_use",
            usage={"input_tokens": 6_000, "output_tokens": 200},
        )


def test_token_budget_exceeded_stops_loop() -> None:
    gateway = _BusyToolGateway()
    loop = _make_loop(
        gateway,
        config=LoopConfig(max_iterations=10, token_budget=6_000),
    )
    outcome = loop.run(system="system", user_message="do heavy work")

    assert outcome.aborted
    assert outcome.stop_reason == "token_budget_exceeded"
    assert outcome.abort_reason == "token_budget_exceeded"
    # First call bills 6200 >= 6000, so iteration 2 must not call the LLM.
    assert gateway.calls == 1
    assert outcome.input_tokens_total == 6_000
    assert outcome.output_tokens_total == 200


# ---------------------------------------------------------------------------
# token-pressure forced compaction
# ---------------------------------------------------------------------------


def _bulk_transcript(n_pairs: int) -> list[dict]:
    transcript: list[dict] = [{"role": "user", "content": "original task"}]
    for i in range(n_pairs):
        transcript.append(
            {
                "role": "assistant",
                "content": [
                    {
                        "type": "tool_use",
                        "id": f"t{i}",
                        "name": "read_status",
                        "input": {"i": i},
                    }
                ],
            }
        )
        transcript.append(
            {
                "role": "user",
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": f"t{i}",
                        "content": [{"type": "text", "text": f"result {i} " + "b" * 500}],
                    }
                ],
            }
        )
    return transcript


def test_forced_compact_bypasses_message_count_threshold() -> None:
    loop = _make_loop(
        _BusyToolGateway(),
        config=LoopConfig(compact_threshold=60, keep_tail_messages=8),
    )
    transcript = _bulk_transcript(15)  # 31 messages — under the threshold
    assert len(transcript) <= 60

    untouched = loop._maybe_compact(list(transcript))
    assert len(untouched) == len(transcript)

    forced = loop._maybe_compact(
        list(transcript), force_reason="token_pressure:110000/128000"
    )
    assert len(forced) < len(transcript)
    breadcrumbs = [
        m for m in forced if m.get("kind") == "transcript.compact.breadcrumb"
    ]
    assert breadcrumbs, "forced compaction must leave a breadcrumb"
    # Anti-hijack framing on the breadcrumb.
    assert "not instructions" in str(breadcrumbs[0].get("content"))


# ---------------------------------------------------------------------------
# usage telemetry
# ---------------------------------------------------------------------------


class _TextWithUsageGateway:
    def call_messages(self, **kwargs):  # noqa: ANN001
        return MessagesResponse(
            content=[{"type": "text", "text": "done"}],
            stop_reason="end_turn",
            usage={"input_tokens": 123, "output_tokens": 45},
        )


def test_loop_outcome_reports_usage_telemetry() -> None:
    loop = _make_loop(_TextWithUsageGateway(), config=LoopConfig(max_iterations=2))
    outcome = loop.run(system="system", user_message="hello")

    assert outcome.llm_calls == 1
    assert outcome.input_tokens_total == 123
    assert outcome.output_tokens_total == 45
    assert outcome.prompt_tokens_last == 123
    assert outcome.compaction_count == 0
    assert outcome.reactive_compaction_count == 0


# ---------------------------------------------------------------------------
# diminishing returns opt-in
# ---------------------------------------------------------------------------


def test_diminishing_returns_requires_opt_in() -> None:
    gateway = _BusyToolGateway()
    loop = _make_loop(
        gateway,
        config=LoopConfig(max_iterations=5, diminishing_returns_window=3),
    )
    outcome = loop.run(system="system", user_message="grind")
    # Disabled by default: the loop runs to max_iterations.
    assert outcome.stop_reason != "diminishing_returns"
    assert gateway.calls == 5


def test_diminishing_returns_triggers_when_enabled() -> None:
    gateway = _BusyToolGateway()
    loop = _make_loop(
        gateway,
        config=LoopConfig(
            max_iterations=8,
            enable_diminishing_returns=True,
            diminishing_returns_window=3,
            diminishing_returns_threshold=500,
        ),
    )
    outcome = loop.run(system="system", user_message="grind")

    assert outcome.stop_reason == "diminishing_returns"
    assert outcome.aborted
    # Window of 3 zero-text tool iterations, then the gate trips at the
    # top of iteration 4.
    assert gateway.calls == 3


# ---------------------------------------------------------------------------
# anti-hijack framing
# ---------------------------------------------------------------------------


def test_steer_inbox_push_drain_and_bounds() -> None:
    inbox = SteerInbox()
    assert inbox.push("focus on ETH")
    assert not inbox.push("   ")
    assert inbox.pending == 1
    assert inbox.drain() == ["focus on ETH"]
    assert inbox.pending == 0

    for i in range(16):
        assert inbox.push(f"m{i}")
    assert not inbox.push("overflow rejected")
    assert len(inbox.drain()) == 16

    register_steer_inbox("turn-abc", inbox)
    try:
        assert signal_steer("turn-abc", "redirect now")
        assert inbox.drain() == ["redirect now"]
        assert not signal_steer("turn-missing", "nobody listening")
    finally:
        unregister_steer_inbox("turn-abc")
    assert not signal_steer("turn-abc", "after unregister")


class _SteerAwareGateway:
    """Pushes a steer message after the first tool round, then verifies
    the redirect text arrives in the next request payload."""

    def __init__(self, inbox: SteerInbox) -> None:
        self.inbox = inbox
        self.calls = 0
        self.saw_steer_in_payload = False

    def call_messages(self, **kwargs):  # noqa: ANN001
        self.calls += 1
        messages = kwargs.get("messages") or []
        if any(
            "switch to ETH risk review" in str(m.get("content"))
            for m in messages
            if isinstance(m, dict)
        ):
            self.saw_steer_in_payload = True
            return MessagesResponse(
                content=[{"type": "text", "text": "acknowledged steer"}],
                stop_reason="end_turn",
            )
        if self.calls == 1:
            # Queue the redirect while the turn is "running".
            self.inbox.push("switch to ETH risk review")
            return _tool_use_response("toolu_1")
        return MessagesResponse(
            content=[{"type": "text", "text": "no steer seen"}],
            stop_reason="end_turn",
        )


def test_mid_turn_steer_redirects_without_abort() -> None:
    inbox = SteerInbox()
    gateway = _SteerAwareGateway(inbox)
    loop = _make_loop(gateway, config=LoopConfig(max_iterations=4))
    outcome = loop.run(
        system="system",
        user_message="review BTC exposure",
        steer_inbox=inbox,
    )

    assert not outcome.aborted
    assert outcome.steer_messages == 1
    assert gateway.saw_steer_in_payload
    assert outcome.final_text == "acknowledged steer"
    steer_msgs = [
        m for m in outcome.transcript
        if isinstance(m, dict)
        and m.get("role") == "user"
        and "switch to ETH risk review" in str(m.get("content"))
    ]
    assert steer_msgs
    # Pinned so macro-compaction can never drop an operator directive.
    assert steer_msgs[0].get("pinned") is True


def test_session_checkpoint_carries_anti_hijack_guard() -> None:
    rows = []
    for i in range(40):
        rows.append({"id": i * 2 + 1, "role": "user", "content": f"user ask {i}"})
        rows.append({"id": i * 2 + 2, "role": "assistant", "content": f"answer {i}"})
    result = compact_session_history(rows)

    checkpoint_texts = [
        str(m.get("content")) for m in result.messages if m.get("role") == "user"
    ]
    rendered = "\n".join(checkpoint_texts)
    assert "[context checkpoint]" in rendered
    assert "not instructions" in rendered


def test_transcript_breadcrumb_carries_anti_hijack_guard() -> None:
    transcript = _bulk_transcript(30)
    compacted, report = compact_transcript(
        transcript, keep_tail_messages=6, max_messages=10
    )
    assert report.dropped > 0
    breadcrumbs = [
        m for m in compacted if m.get("kind") == "transcript.compact.breadcrumb"
    ]
    assert breadcrumbs
    assert "not instructions" in str(breadcrumbs[0].get("content"))
