"""Block-shaped transcript schema for the workspace-native agent loop.

  (streaming chunk shape) — these blocks are what each chunk
  *materialises* into when it lands in the journal/dashboard.

Why
---
The workspace-native agent loop emits messages as a sequence of
*blocks*: ``text``, ``thinking``, ``tool_use``, ``tool_result``,
``image``, ``redacted_thinking`` — exactly the shape Anthropic /
OpenAI / Gemini speak natively. Each block has a stable id and an
``index`` within the assistant turn. Streaming chunks reference these
ids so the UI can grow / replace / commit individual blocks instead
of rebuilding the whole turn on each delta.

This module gives us the typed shape; the kernel, streaming bus and
SSE writer all import from here so there is exactly one schema.
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Optional


__all__ = [
    "TextBlock",
    "ThinkingBlock",
    "ToolUseBlock",
    "ToolResultBlock",
    "ApprovalRequestBlock",
    "BlockEnvelope",
    "make_block_id",
]


def make_block_id(prefix: str = "blk") -> str:
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


@dataclass
class TextBlock:
    block_id: str = field(default_factory=lambda: make_block_id("txt"))
    index: int = 0
    text: str = ""
    is_partial: bool = False

    def as_dict(self) -> dict[str, Any]:
        return {
            "kind": "text",
            "block_id": self.block_id,
            "index": self.index,
            "text": self.text,
            "is_partial": self.is_partial,
        }


@dataclass
class ThinkingBlock:
    block_id: str = field(default_factory=lambda: make_block_id("think"))
    index: int = 0
    text: str = ""
    summary: str = ""
    redacted: bool = False
    is_partial: bool = False
    effort: str = ""
    tokens: int = 0

    def as_dict(self) -> dict[str, Any]:
        return {
            "kind": "thinking",
            "block_id": self.block_id,
            "index": self.index,
            "text": self.text,
            "summary": self.summary,
            "redacted": self.redacted,
            "is_partial": self.is_partial,
            "effort": self.effort,
            "tokens": self.tokens,
        }


@dataclass
class ToolUseBlock:
    block_id: str = field(default_factory=lambda: make_block_id("tool"))
    index: int = 0
    skill_id: str = ""
    action: str = ""
    payload: dict[str, Any] = field(default_factory=dict)
    call_id: str = ""
    started_at: float = 0.0
    is_partial: bool = False

    def as_dict(self) -> dict[str, Any]:
        return {
            "kind": "tool_use",
            "block_id": self.block_id,
            "index": self.index,
            "skill_id": self.skill_id,
            "action": self.action,
            "payload": dict(self.payload),
            "call_id": self.call_id,
            "started_at": self.started_at,
            "is_partial": self.is_partial,
        }


@dataclass
class ToolResultBlock:
    block_id: str = field(default_factory=lambda: make_block_id("res"))
    index: int = 0
    call_id: str = ""
    skill_id: str = ""
    action: str = ""
    ok: bool = False
    result: Any = None
    error: Optional[str] = None
    error_kind: Optional[str] = None
    elapsed_ms: float = 0.0
    completed_at: float = 0.0
    recovery: Optional[dict[str, Any]] = None
    compaction: Optional[dict[str, Any]] = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "kind": "tool_result",
            "block_id": self.block_id,
            "index": self.index,
            "call_id": self.call_id,
            "skill_id": self.skill_id,
            "action": self.action,
            "ok": self.ok,
            "result": self.result,
            "error": self.error,
            "error_kind": self.error_kind,
            "elapsed_ms": self.elapsed_ms,
            "completed_at": self.completed_at,
            "recovery": self.recovery,
            "compaction": self.compaction,
        }


@dataclass
class ApprovalRequestBlock:
    block_id: str = field(default_factory=lambda: make_block_id("approval"))
    index: int = 0
    approval_id: str = ""
    call_id: str = ""
    skill_id: str = "native"
    action: str = ""
    prompt: dict[str, Any] = field(default_factory=dict)
    record: dict[str, Any] = field(default_factory=dict)
    reason: str = ""
    status: str = "pending"

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "ApprovalRequestBlock":
        return cls(
            approval_id=str(value.get("approval_id") or ""),
            call_id=str(value.get("call_id") or ""),
            skill_id=str(value.get("skill_id") or "native"),
            action=str(value.get("action") or ""),
            prompt=(
                dict(value.get("prompt"))
                if isinstance(value.get("prompt"), dict)
                else {}
            ),
            record=(
                dict(value.get("record"))
                if isinstance(value.get("record"), dict)
                else {}
            ),
            reason=str(value.get("reason") or ""),
            status=str(value.get("status") or "pending"),
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "kind": "approval_request",
            "block_id": self.block_id,
            "index": self.index,
            "approval_id": self.approval_id,
            "call_id": self.call_id,
            "skill_id": self.skill_id,
            "action": self.action,
            "prompt": dict(self.prompt),
            "record": dict(self.record),
            "reason": self.reason,
            "status": self.status,
        }


@dataclass
class BlockEnvelope:
    """A streaming-friendly block + ordering metadata.

    ``seq`` is the global event sequence (from the streaming bus).
    ``turn_id`` and ``message_id`` scope the block. ``role`` is
    ``assistant`` for model output and ``tool`` for tool results
    (so the dashboard can split them visually).
    """

    seq: int
    turn_id: str
    message_id: str
    role: str
    block: dict[str, Any]
    ts: float = field(default_factory=time.time)

    def as_dict(self) -> dict[str, Any]:
        return {
            "seq": self.seq,
            "turn_id": self.turn_id,
            "message_id": self.message_id,
            "role": self.role,
            "ts": self.ts,
            "block": dict(self.block),
        }
