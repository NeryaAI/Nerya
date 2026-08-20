"""Stable, producer-owned contracts for interpreting :class:`ToolResult`.

Tool handlers know whether an operation made semantic progress and whether a
result participates in a higher-level completion protocol. The agent loop
should consume those declarations instead of routing on tool names or growing
another list of status-shaped JSON guesses.

Legacy payload inference remains as a compatibility boundary for adapters that
have not adopted explicit fields yet. New tools should set ``semantic_success``
and, when applicable, ``result_protocol`` on the result they produce.
"""

from __future__ import annotations

import json
from typing import Any

from .types import ToolResult


TEAM_REPORT_RESULT_PROTOCOL = "nerya.team_report.v1"
"""Result protocol consumed by the built-in team-report finalizer."""

_LEGACY_TEAM_RUN_TOOL_NAME = "team_run"
NON_SUCCESS_RESULT_STATUSES = frozenset({
    "blocked",
    "cancelled",
    "canceled",
    "error",
    "failed",
    "missing",
    "not_configured",
    "not_found",
    "pending",
    "rejected",
    "timeout",
    "unavailable",
    "validation_blocked",
})


def parse_json_text(
    text: Any,
    *,
    allow_suffix: bool = False,
    allow_trailing_lines: bool = False,
) -> Any:
    """Parse JSON from a provider/tool text envelope without raising."""

    stripped = str(text or "").strip()
    if not stripped:
        return None
    candidates = [stripped]
    if allow_trailing_lines and "\n" in stripped:
        candidates.extend(
            line.strip()
            for line in reversed(stripped.splitlines())
            if line.strip().startswith(("{", "["))
        )
    for candidate in candidates:
        try:
            if allow_suffix:
                parsed, _end = json.JSONDecoder().raw_decode(candidate)
            else:
                parsed = json.loads(candidate)
            return parsed
        except Exception:
            continue
    return None


def tool_json_data(result: ToolResult) -> dict[str, Any] | None:
    """Return the first structured JSON object carried by ``result``."""

    for part in result.content:
        if part.type == "json" and isinstance(part.data, dict):
            return part.data
    parsed = parse_json_text(result.text())
    return parsed if isinstance(parsed, dict) else None


def parse_compacted_kept_jsonish(text: str) -> dict[str, Any] | None:
    """Recover the structured ``[compacted_kept]`` compatibility payload."""

    marker = "[compacted_kept]"
    marker_index = text.find(marker)
    if marker_index < 0:
        return None
    tail = text[marker_index + len(marker):].strip()
    if not tail:
        return None
    parsed = parse_json_text(tail, allow_suffix=True)
    return parsed if isinstance(parsed, dict) else None


def compacted_kept_data(result: ToolResult) -> dict[str, Any] | None:
    return parse_compacted_kept_jsonish(result.text())


def result_counts_as_success(result: ToolResult) -> bool:
    """Return producer-declared progress, with legacy JSON inference fallback."""

    if result.is_error:
        return False
    if result.semantic_success is not None:
        return bool(result.semantic_success)
    data = tool_json_data(result) or compacted_kept_data(result)
    if not isinstance(data, dict):
        return True
    if data.get("ok") is False or data.get("success") is False:
        return False
    if data.get("terminal") is False or data.get("complete") is False:
        return False
    status = str(data.get("status") or data.get("state") or "").strip().lower()
    if status in NON_SUCCESS_RESULT_STATUSES:
        return False
    error = data.get("error")
    if error not in (None, "", [], {}):
        return False
    return True


def team_report_data(result: ToolResult) -> dict[str, Any] | None:
    """Return a valid team-report payload declared by the result producer.

    ``result_protocol`` is authoritative. The legacy tool-name fallback keeps
    persisted/adapted ``team_run`` results working while callers migrate.
    An explicitly different protocol is never reclassified by payload shape.
    """

    if result.is_error:
        return None
    declared_protocol = str(result.result_protocol or "").strip()
    if declared_protocol:
        if declared_protocol != TEAM_REPORT_RESULT_PROTOCOL:
            return None
    elif result.name != _LEGACY_TEAM_RUN_TOOL_NAME:
        return None
    data = tool_json_data(result)
    if not isinstance(data, dict) or not str(data.get("team_run_id") or "").strip():
        return None
    return data


def team_report_should_finalize(data: dict[str, Any]) -> bool:
    status = str(data.get("status") or "").strip().lower()
    return (
        bool(data.get("failures"))
        or status in {"completed_with_failures", "failed", "timeout"}
        or data.get("ok") is False
    )


def team_report_has_usable_output(data: dict[str, Any]) -> bool:
    roles_succeeded = data.get("roles_succeeded")
    if isinstance(roles_succeeded, list) and roles_succeeded:
        return True
    results = data.get("results")
    if isinstance(results, list) and results:
        return True
    aggregated = data.get("aggregated")
    if isinstance(aggregated, dict) and aggregated:
        return True
    return False


__all__ = [
    "NON_SUCCESS_RESULT_STATUSES",
    "TEAM_REPORT_RESULT_PROTOCOL",
    "compacted_kept_data",
    "parse_compacted_kept_jsonish",
    "parse_json_text",
    "result_counts_as_success",
    "team_report_data",
    "team_report_has_usable_output",
    "team_report_should_finalize",
    "tool_json_data",
]
