"""Microcompact — per-tool-result token cap.

Where macro-compaction (``transcript_compact``) drops whole
tool_use/tool_result pairs to keep the *message count* in budget,
microcompact does the opposite: it keeps every pair but **truncates
the body** of the bulkiest read/grep/glob/shell/web tool results so
the per-message token bill goes down. This targets "low-value,
high-volume" tool results.

The function is called immediately *before* every model round so
the assistant never sees a tool result it doesn't need verbatim.

Heuristics
----------

* Targets only tool results from a configurable name list
  (``read_file`` / ``grep`` / ``glob`` / ``run_shell`` / ``fetch_url``
  by default).
* Skips the **last K results** so the most recent observations
  arrive in full fidelity.
* Replaces the truncated body with ``[microcompacted: kept N chars,
  dropped M chars]`` so the model knows the data was redacted, not
  lost.
* Never touches ``is_error=True`` results — error feedback is
  precisely the thing the model needs to debug from.
* Never breaks the tool_use/tool_result pair invariant
  (:func:`validate_transcript` still passes after a pass).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


# Tools whose results commonly bloat the transcript and where a
# truncated tail still tells the model "I read it, here's the gist".
DEFAULT_BULK_TOOLS: frozenset[str] = frozenset({
    "read_file",
    "grep",
    "glob",
    "run_shell",
    "fetch_url",
    "web_search",
    "list_dir",
})


@dataclass
class MicrocompactReport:
    inspected: int = 0
    truncated: int = 0
    bytes_dropped: int = 0
    skipped_recent: int = 0
    notes: list[str] = field(default_factory=list)

    def asdict(self) -> dict[str, Any]:
        return {
            "inspected": self.inspected,
            "truncated": self.truncated,
            "bytes_dropped": self.bytes_dropped,
            "skipped_recent": self.skipped_recent,
            "notes": list(self.notes),
        }


def _content_text_chars(content: list[dict[str, Any]]) -> int:
    total = 0
    for part in content or ():
        if not isinstance(part, dict):
            continue
        text = part.get("text")
        if isinstance(text, str):
            total += len(text)
    return total


def _truncate_text(text: str, *, head: int, tail: int) -> tuple[str, int]:
    """Keep the first ``head`` and the last ``tail`` characters.

    Returns the truncated body and the number of chars dropped.
    The middle section is replaced with a one-line breadcrumb so
    the model knows *why* the body shrank.
    """

    if len(text) <= head + tail + 64:
        return text, 0
    dropped = len(text) - head - tail
    breadcrumb = (
        f"\n\n[microcompacted: kept first {head} + last {tail} chars; "
        f"dropped {dropped} chars in the middle to fit context budget]\n\n"
    )
    return text[:head] + breadcrumb + text[-tail:], dropped


def microcompact(
    messages: list[dict[str, Any]],
    *,
    max_chars_per_result: int = 8000,
    head_chars: int = 3000,
    tail_chars: int = 1500,
    bulk_tools: frozenset[str] = DEFAULT_BULK_TOOLS,
    keep_recent_results: int = 3,
    tool_use_lookup: dict[str, str] | None = None,
) -> tuple[list[dict[str, Any]], MicrocompactReport]:
    """Truncate bulky tool results in-place (returns a new list).

    Parameters
    ----------
    messages:
        Provider-shaped transcript. Tool results are recognised by
        the dict shape ``{"type": "tool_result", "tool_use_id":
        ..., "content": [{"type": "text", ...}, ...]}``.
    max_chars_per_result:
        Soft cap on the total text length per tool result. Anything
        larger gets truncated to ``head_chars + breadcrumb +
        tail_chars``.
    head_chars / tail_chars:
        Bytes to retain at the start / end of an oversized body.
    bulk_tools:
        Set of tool names whose results are eligible for truncation.
        Other tools (e.g. ``edit_file``, ``todo_write``) keep full
        bodies because their *whole* output is normally required
        for the next step.
    keep_recent_results:
        Always keep the last N tool results untouched. The model
        usually needs the most recent observation in full.
    tool_use_lookup:
        Optional map ``tool_use_id -> tool_name``. When the caller
        already maintains this mapping, supply it for a precise
        ``bulk_tools`` filter; otherwise we resort to scanning the
        prior messages for matching ``tool_use`` blocks.
    """

    report = MicrocompactReport()
    if not messages:
        return list(messages), report

    if tool_use_lookup is None:
        tool_use_lookup = {}
        for m in messages:
            content = m.get("content")
            if not isinstance(content, list):
                continue
            for c in content:
                if isinstance(c, dict) and c.get("type") == "tool_use":
                    tid = str(c.get("id") or c.get("tool_use_id") or "")
                    name = str(c.get("name") or "")
                    if tid and name:
                        tool_use_lookup[tid] = name

    indices_with_results: list[int] = []
    for i, m in enumerate(messages):
        content = m.get("content")
        if not isinstance(content, list):
            continue
        if any(
            isinstance(c, dict) and c.get("type") == "tool_result"
            for c in content
        ):
            indices_with_results.append(i)

    # The last ``keep_recent_results`` messages with tool results stay full.
    skip_idx: set[int] = set(indices_with_results[-keep_recent_results:]) \
        if keep_recent_results > 0 else set()
    report.skipped_recent = len(skip_idx)

    out: list[dict[str, Any]] = []
    for i, m in enumerate(messages):
        content = m.get("content")
        if i in skip_idx or not isinstance(content, list):
            out.append(m)
            continue
        new_content: list[dict[str, Any]] = []
        for c in content:
            if not isinstance(c, dict) or c.get("type") != "tool_result":
                new_content.append(c)
                continue
            if c.get("is_error"):
                new_content.append(c)
                continue
            tid = str(c.get("tool_use_id") or "")
            name = tool_use_lookup.get(tid, "")
            if name and name not in bulk_tools:
                new_content.append(c)
                continue
            inner = c.get("content")
            if not isinstance(inner, list):
                new_content.append(c)
                continue
            report.inspected += 1
            total_chars = _content_text_chars(inner)
            if total_chars <= max_chars_per_result:
                new_content.append(c)
                continue
            new_inner: list[dict[str, Any]] = []
            char_dropped = 0
            for part in inner:
                if (
                    isinstance(part, dict)
                    and part.get("type") == "text"
                    and isinstance(part.get("text"), str)
                ):
                    truncated, dropped = _truncate_text(
                        part["text"], head=head_chars, tail=tail_chars,
                    )
                    new_inner.append({"type": "text", "text": truncated})
                    char_dropped += dropped
                else:
                    new_inner.append(part)
            new_c = dict(c)
            new_c["content"] = new_inner
            new_content.append(new_c)
            if char_dropped > 0:
                report.truncated += 1
                report.bytes_dropped += char_dropped
                report.notes.append(
                    f"truncated {name or '<unknown>'}::{tid}: "
                    f"dropped {char_dropped} chars"
                )
        new_msg = dict(m)
        new_msg["content"] = new_content
        out.append(new_msg)
    return out, report


__all__ = [
    "DEFAULT_BULK_TOOLS",
    "MicrocompactReport",
    "microcompact",
]
