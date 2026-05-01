"""Provider-agnostic streaming primitives for LLM adapters.

  ("text/thinking/tool_use blocks form the streaming wire format").

Why
---
Every provider wire format streams differently:

- OpenAI sends ``data: {...}`` SSE with ``delta.content`` strings,
  occasional tool-call deltas, and ``usage`` only at the end.
- Anthropic sends ``message_start``, ``content_block_start``,
  ``content_block_delta`` with ``thinking``/``text``/``input_json``
  variants, ``content_block_stop``, ``message_delta``, ``message_stop``.
- Gemini sends concatenated JSON objects with ``parts[].text`` /
  ``thoughtSignature`` / ``functionCall`` per chunk.

We translate all of those into one neutral shape so the kernel,
gateway, and dashboard can be provider-agnostic. Adapters yield
:class:`ProviderStreamChunk` instances; callers reassemble blocks /
emit dashboard events from them.

The schema is intentionally simple: every chunk has a ``kind`` (one
of the canonical event types), a stable ``index`` (block-aligned for
multi-block messages), and only the fields the caller actually
needs.
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from typing import Any, AsyncIterator, Iterator, Optional


__all__ = [
    "ProviderStreamChunk",
    "make_chunk_id",
    "stream_to_blocks",
]


def make_chunk_id() -> str:
    return uuid.uuid4().hex[:12]


@dataclass
class ProviderStreamChunk:
    """One incremental piece of a streaming model response.

    ``kind`` discriminates the chunk shape:

    * ``message_start``     — assistant message about to begin
    * ``content_start``     — a new content block opened (text /
                              thinking / tool_use)
    * ``text_delta``        — assistant visible text fragment
    * ``thinking_delta``    — reasoning summary fragment (when
                              provider exposes it)
    * ``tool_use_start``    — model has decided to call a tool;
                              ``tool_call_id``/``skill_id``/``action``
                              are populated
    * ``tool_use_input``    — incremental JSON for the tool payload
                              (Anthropic streams ``input_json_delta``)
    * ``content_stop``      — current content block finished
    * ``message_stop``      — assistant message complete
    * ``usage``             — final usage / cost / model meta
    * ``error``             — provider error mid-stream

    ``index`` is the content-block index inside the assistant
    message (0 = first block, 1 = second block, …). For
    ``text_delta``/``thinking_delta``/``tool_use_input`` the caller
    appends to the block at this index.
    """

    kind: str
    index: int = 0
    chunk_id: str = field(default_factory=make_chunk_id)
    ts: float = field(default_factory=time.time)
    text: str = ""
    summary: str = ""
    tool_call_id: str = ""
    skill_id: str = ""
    action: str = ""
    payload_partial: str = ""
    block_id: str = ""
    finish_reason: str = ""
    error: str = ""
    usage: dict[str, Any] | None = None
    raw: dict[str, Any] | None = None

    def as_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "kind": self.kind,
            "index": self.index,
            "chunk_id": self.chunk_id,
            "ts": self.ts,
        }
        if self.text:
            out["text"] = self.text
        if self.summary:
            out["summary"] = self.summary
        if self.tool_call_id:
            out["tool_call_id"] = self.tool_call_id
        if self.skill_id:
            out["skill_id"] = self.skill_id
        if self.action:
            out["action"] = self.action
        if self.payload_partial:
            out["payload_partial"] = self.payload_partial
        if self.block_id:
            out["block_id"] = self.block_id
        if self.finish_reason:
            out["finish_reason"] = self.finish_reason
        if self.error:
            out["error"] = self.error
        if self.usage:
            out["usage"] = self.usage
        return out


# ---- helper: collapse a stream into final blocks ---------------------------


def stream_to_blocks(
    chunks: Iterator[ProviderStreamChunk],
) -> dict[str, Any]:
    """Drain ``chunks`` and assemble a final message + block list.

    Returns ``{"text": str, "thinking": str, "tool_calls": [...],
    "usage": dict|None, "finish_reason": str, "blocks": [...]}``.

    Used by ``LLMGateway.call_stream`` to provide the same return
    shape as :meth:`LLMGateway.call` once the stream finishes,
    so callers can choose between sync/streaming without rewriting
    their downstream logic.
    """

    text_parts: dict[int, list[str]] = {}
    thinking_parts: dict[int, list[str]] = {}
    tool_calls: dict[int, dict[str, Any]] = {}
    usage: dict[str, Any] | None = None
    finish_reason = ""
    blocks: list[dict[str, Any]] = []
    error: str = ""

    for c in chunks:
        if c.kind == "text_delta":
            text_parts.setdefault(c.index, []).append(c.text)
        elif c.kind == "thinking_delta":
            thinking_parts.setdefault(c.index, []).append(c.text)
        elif c.kind == "tool_use_start":
            tool_calls[c.index] = {
                "tool_call_id": c.tool_call_id,
                "skill_id": c.skill_id,
                "action": c.action,
                "payload_partial": "",
                "index": c.index,
            }
        elif c.kind == "tool_use_input":
            entry = tool_calls.setdefault(c.index, {
                "tool_call_id": c.tool_call_id,
                "skill_id": "",
                "action": "",
                "payload_partial": "",
                "index": c.index,
            })
            entry["payload_partial"] = (entry.get("payload_partial") or "") + c.payload_partial
            if c.skill_id and not entry.get("skill_id"):
                entry["skill_id"] = c.skill_id
            if c.action and not entry.get("action"):
                entry["action"] = c.action
        elif c.kind == "content_stop":
            if c.index in text_parts:
                blocks.append({
                    "kind": "text",
                    "index": c.index,
                    "block_id": c.block_id,
                    "text": "".join(text_parts.get(c.index, [])),
                })
            elif c.index in thinking_parts:
                blocks.append({
                    "kind": "thinking",
                    "index": c.index,
                    "block_id": c.block_id,
                    "text": "".join(thinking_parts.get(c.index, [])),
                })
            elif c.index in tool_calls:
                tc = tool_calls[c.index]
                blocks.append({
                    "kind": "tool_use",
                    "index": c.index,
                    "block_id": c.block_id,
                    "tool_call_id": tc.get("tool_call_id") or "",
                    "skill_id": tc.get("skill_id") or "",
                    "action": tc.get("action") or "",
                    "payload_partial": tc.get("payload_partial") or "",
                })
        elif c.kind == "usage":
            usage = c.usage
        elif c.kind == "message_stop":
            finish_reason = c.finish_reason or finish_reason
        elif c.kind == "error":
            error = c.error or error

    text = "".join(
        "".join(parts) for _, parts in sorted(text_parts.items())
    )
    thinking = "".join(
        "".join(parts) for _, parts in sorted(thinking_parts.items())
    )
    return {
        "text": text,
        "thinking": thinking,
        "tool_calls": [tool_calls[i] for i in sorted(tool_calls.keys())],
        "usage": usage,
        "finish_reason": finish_reason,
        "blocks": blocks,
        "error": error,
    }
