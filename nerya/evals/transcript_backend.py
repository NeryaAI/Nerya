"""Transcript-level mock provider for the eval harness.

Phase 15 §"transcript-level mock provider" — the offline
:class:`~nerya.llm.messages.MockMessagesBackend` only knows how to
replay a single ``[[call_tool: …]]`` marker. For evals we need a
backend that walks a *scripted* multi-turn transcript so a scenario
can drive the loop through:

* an initial ``read_file`` ``tool_use`` block,
* observe the synthetic ``tool_result``,
* emit a follow-up ``grep_search`` ``tool_use``,
* react to a permission denial by switching to an alternative path,
* finish with ``end_turn`` and a final summary.

The script is plain data: each :class:`ScriptedTurn` lists the content
blocks to emit, the ``stop_reason`` to surface, and (optionally) a
``match`` predicate against the latest assistant call so the script
can branch on what the agent loop actually did.

If the agent loop reaches a turn we have no script for, the backend
returns an ``end_turn`` text block ``"[mock-script] no scripted turn
remaining"`` so the loop terminates cleanly instead of hanging — eval
runners check transcript length to detect under-driven scripts.
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Callable, Optional, Sequence

from ..llm.messages import MessagesRequest, MessagesResponse


# ---------------------------------------------------------------------------
# Block helpers
# ---------------------------------------------------------------------------


@dataclass
class TextBlock:
    """Assistant ``text`` content block."""

    text: str

    def to_dict(self) -> dict[str, Any]:
        return {"type": "text", "text": self.text}


@dataclass
class ToolUseBlock:
    """Assistant ``tool_use`` content block."""

    name: str
    input: dict[str, Any] = field(default_factory=dict)
    id: Optional[str] = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "type": "tool_use",
            "id": self.id or f"toolu_{uuid.uuid4().hex[:10]}",
            "name": self.name,
            "input": dict(self.input),
        }


AssistantBlock = TextBlock | ToolUseBlock


# ---------------------------------------------------------------------------
# Scripted turn
# ---------------------------------------------------------------------------


@dataclass
class ScriptedTurn:
    """One assistant turn in a scripted transcript.

    Attributes
    ----------
    blocks:
        Content blocks the backend should return, in order. Mix of
        :class:`TextBlock` and :class:`ToolUseBlock`.
    stop_reason:
        Anthropic-vocabulary stop reason. Use ``"tool_use"`` for turns
        that emit at least one :class:`ToolUseBlock` (the loop won't
        execute tools otherwise) and ``"end_turn"`` for the final turn.
    match:
        Optional predicate. Receives the :class:`MessagesRequest` the
        backend was called with; if it returns ``False`` the turn is
        skipped and the next scripted turn is tried. Useful for
        branching evals (eg. "if last tool result was an error,
        run this turn; otherwise this other one").
    label:
        Optional label surfaced in :class:`MessagesResponse.metadata`
        so eval verdicts can assert which branch fired.
    delay_ms:
        Optional artificial latency. Defaults to ``1`` so the loop
        records non-zero ``latency_ms`` for realism.
    """

    blocks: Sequence[AssistantBlock]
    stop_reason: str = "end_turn"
    match: Optional[Callable[[MessagesRequest], bool]] = None
    label: str = ""
    delay_ms: int = 1


# ---------------------------------------------------------------------------
# Scripted transcript
# ---------------------------------------------------------------------------


@dataclass
class TranscriptScript:
    """Ordered list of :class:`ScriptedTurn` instances.

    The :class:`TranscriptMockBackend` walks this list with a per-call
    cursor. ``finalize`` controls what happens once the cursor has
    advanced past the last turn:

    * ``"end_turn"`` (default) — return a ``[mock-script] exhausted``
      text block with ``stop_reason="end_turn"``. This terminates the
      loop without an error.
    * ``"raise"`` — raise :class:`RuntimeError`, useful when an eval
      wants to detect under-driven scripts hard.
    """

    turns: list[ScriptedTurn]
    finalize: str = "end_turn"

    def __post_init__(self) -> None:
        if self.finalize not in ("end_turn", "raise"):
            raise ValueError(
                f"finalize must be 'end_turn' or 'raise', got {self.finalize!r}"
            )


# ---------------------------------------------------------------------------
# Backend
# ---------------------------------------------------------------------------


@dataclass
class TranscriptMockBackend:
    """Mock backend that walks a :class:`TranscriptScript`.

    Use:
        backend = TranscriptMockBackend(script=...)
        gateway = LLMGateway(...)  # configure tier "mock-script"
        gateway.register_messages_backend("mock-script", backend)

    The eval runner shortcuts this by injecting the backend directly
    onto the gateway via :meth:`LLMGateway._messages_backends`.
    """

    script: TranscriptScript
    model: str = "mock-script"
    provider_name: str = "mock-script"
    cursor: int = 0
    history: list[dict[str, Any]] = field(default_factory=list)

    def __call__(self, request: MessagesRequest) -> MessagesResponse:
        # Pick the next matching turn, skipping turns whose ``match`` rejects.
        while self.cursor < len(self.script.turns):
            turn = self.script.turns[self.cursor]
            if turn.match is None or self._safe_match(turn.match, request):
                break
            self.cursor += 1
        else:
            return self._finalize(request)

        self.cursor += 1
        content = [b.to_dict() for b in turn.blocks]
        if not content:
            content = [{"type": "text", "text": "[mock-script] empty turn"}]

        # Time-travel friendly latency so transcripts don't all
        # collapse to ``latency_ms=0``.
        if turn.delay_ms > 0:
            time.sleep(turn.delay_ms / 1000.0)

        usage = {
            "input_tokens": _token_estimate(request),
            "output_tokens": sum(
                len(b.get("text", "")) // 4 + 4
                for b in content
                if b.get("type") == "text"
            )
            + sum(8 for b in content if b.get("type") == "tool_use"),
        }
        meta: dict[str, Any] = {"label": turn.label} if turn.label else {}
        response = MessagesResponse(
            content=content,
            stop_reason=turn.stop_reason or "end_turn",
            usage=usage,
            provider=self.provider_name,
            model=self.model,
            raw={"script_cursor": self.cursor, **meta},
            latency_ms=max(1, int(turn.delay_ms)),
        )
        self.history.append(
            {
                "cursor": self.cursor,
                "stop_reason": response.stop_reason,
                "label": turn.label,
                "blocks": list(content),
            }
        )
        return response

    def _finalize(self, request: MessagesRequest) -> MessagesResponse:
        if self.script.finalize == "raise":
            raise RuntimeError(
                f"transcript script exhausted after {self.cursor} turns; "
                "the loop kept asking for more LLM rounds"
            )
        return MessagesResponse(
            content=[{"type": "text", "text": "[mock-script] exhausted"}],
            stop_reason="end_turn",
            usage={"input_tokens": _token_estimate(request), "output_tokens": 6},
            provider=self.provider_name,
            model=self.model,
            raw={"script_cursor": self.cursor, "exhausted": True},
            latency_ms=1,
        )

    @staticmethod
    def _safe_match(
        predicate: Callable[[MessagesRequest], bool], request: MessagesRequest
    ) -> bool:
        try:
            return bool(predicate(request))
        except Exception:
            return False


def _token_estimate(request: MessagesRequest) -> int:
    """Cheap token estimate so eval transcripts have plausible usage stats."""

    chars = len(request.system or "")
    for msg in request.messages:
        content = msg.get("content")
        if isinstance(content, str):
            chars += len(content)
        elif isinstance(content, list):
            for block in content:
                if isinstance(block, dict):
                    chars += len(str(block.get("text") or ""))
                    chars += len(str(block.get("content") or ""))
    return max(1, chars // 4)


__all__ = [
    "AssistantBlock",
    "ScriptedTurn",
    "TextBlock",
    "ToolUseBlock",
    "TranscriptMockBackend",
    "TranscriptScript",
]
