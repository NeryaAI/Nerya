"""Kernel-side helper that extracts ``chart_blocks`` from a tool result.

Skills that want to surface an interactive chart return a JSON payload
whose top level contains ``chart_blocks: list[ChartBlock dict]``. Most
skills emit JSON via ``run_shell``, which means the kernel sees the
chart blocks as a string buried inside ``ToolResultBlock.result``.

This module owns the parse + extract logic so:

* the kernel's ``_event_sink`` can publish a ``chart.block`` event the
  moment a tool finishes (live stream),
* the same kernel can splice a ``BlockEnvelope`` into ``outcome.blocks``
  after the loop returns (replay / msg.turn.blocks).

The extractor is intentionally lenient — agents may pretty-print JSON,
prepend prose, or wrap the JSON in markdown — but it never parses
arbitrary expressions and never falls back to ``eval``. Worst case it
returns an empty list and the chat renders nothing chart-shaped.

A second hook, :func:`extract_chart_marker_ids`, recognises the
``@@nerya:chart@@ <chart_id>`` stdout marker that the dynamic-code
path uses. Chart markers are wired into the same
splice path; for now the function is exported so callers can probe.
"""

from __future__ import annotations

import json
import re
from typing import Any, Iterable


_CHART_MARKER_RE = re.compile(
    r"@@nerya:chart@@\s+([A-Za-z0-9._\-]+)"
)


def _looks_like_chart_block(value: Any) -> bool:
    return (
        isinstance(value, dict)
        and value.get("kind") == "chart"
        and isinstance(value.get("chart_id"), str)
        and value.get("chart_id")
    )


def _extract_from_dict(payload: dict[str, Any]) -> list[dict[str, Any]]:
    """Pull ``chart_blocks`` out of a parsed JSON dict.

    Accepts both the canonical shape (``chart_blocks: [block, ...]``)
    and the lenient single-block shape (``chart_block: block``) so an
    agent that forgets the plural still gets a render. Anything that
    doesn't look like a chart block is silently dropped — we'd rather
    miss a chart than splice garbage into the transcript.
    """

    candidates: list[Any] = []
    blocks = payload.get("chart_blocks")
    if isinstance(blocks, list):
        candidates.extend(blocks)
    single = payload.get("chart_block")
    if single is not None:
        candidates.append(single)
    return [c for c in candidates if _looks_like_chart_block(c)]


def extract_chart_blocks(result: Any) -> list[dict[str, Any]]:
    """Best-effort extractor for chart blocks in a tool result.

    ``result`` may be:

    * a ``dict`` already parsed (native skills) — fast path.
    * a ``str`` (run_shell output) — scan for the first JSON object that
      contains ``chart_blocks``.
    * any other type — returns ``[]``.
    """

    if isinstance(result, dict):
        return _extract_from_dict(result)
    if not isinstance(result, str):
        return []
    text = result
    # Cheap early exit: if neither key occurs in the text we can't
    # possibly have chart blocks.
    if "chart_blocks" not in text and "chart_block" not in text:
        return []

    decoder = json.JSONDecoder()
    idx = 0
    text_len = len(text)
    while idx < text_len:
        opener = text.find("{", idx)
        if opener < 0:
            return []
        try:
            obj, end_offset = decoder.raw_decode(text[opener:])
        except json.JSONDecodeError:
            idx = opener + 1
            continue
        if isinstance(obj, dict):
            extracted = _extract_from_dict(obj)
            if extracted:
                return extracted
        # Move past the parsed segment and keep looking — agents may
        # embed multiple JSON blobs in a single shell stdout.
        idx = opener + end_offset
    return []


def extract_chart_marker_ids(result: Any) -> list[str]:
    """Return chart_ids announced via ``@@nerya:chart@@ <id>`` markers.

    Used by the dynamic-code path: a script that calls
    ``client.charts.publish(...)`` and prints the marker can have its
    chart spliced into the chat without bundling the data dict in the
    JSON payload.
    """

    if not isinstance(result, str) or "@@nerya:chart@@" not in result:
        return []
    return _CHART_MARKER_RE.findall(result)


def normalise_chart_blocks(blocks: Iterable[Any]) -> list[dict[str, Any]]:
    """Filter an iterable down to ChartBlock-shaped dicts.

    Useful for the rare native skill that constructs the list itself
    (no run_shell hop) and wants the same vetting before publishing.
    """

    return [dict(b) for b in blocks if _looks_like_chart_block(b)]


__all__ = [
    "extract_chart_blocks",
    "extract_chart_marker_ids",
    "normalise_chart_blocks",
]
