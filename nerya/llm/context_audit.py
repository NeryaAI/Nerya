"""Read-only summaries for full-context LLM request journals."""

from __future__ import annotations

from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any

from ..core import jsonl


_PHASE_COUNT_KEYS = {
    "request": "requests",
    "response": "responses",
    "error": "errors",
    "wire_request": "wire_requests",
    "wire_response": "wire_responses",
    "wire_error": "wire_errors",
}

_CORRELATION_KEYS = (
    "session_id",
    "turn_id",
    "iteration",
    "max_iterations",
    "llm_attempt",
    "context_scope",
    "subagent",
    "team_run_id",
    "strategy_id",
    "trigger_event_id",
    "parent_call_id",
    "text_only_final_attempt",
    "tools_sent_count",
    "messages_sent_count",
    "remaining_wall_seconds",
)


def _value_matches(value: Any, expected: str | None) -> bool:
    if expected is None:
        return True
    return str(value or "") == expected


def _record_matches(
    record: dict[str, Any],
    *,
    session_id: str | None,
    turn_id: str | None,
    call_id: str | None,
    subagent: str | None,
    team_run_id: str | None,
    context_scope: str | None,
) -> bool:
    return (
        _value_matches(record.get("session_id"), session_id)
        and _value_matches(record.get("turn_id"), turn_id)
        and _value_matches(record.get("call_id"), call_id)
        and _value_matches(record.get("subagent"), subagent)
        and _value_matches(record.get("team_run_id"), team_run_id)
        and _value_matches(record.get("context_scope"), context_scope)
    )


def _content_chars(value: Any) -> int:
    if value is None:
        return 0
    if isinstance(value, str):
        return len(value)
    if isinstance(value, list):
        return sum(_content_chars(item) for item in value)
    if isinstance(value, dict):
        if "text" in value:
            return _content_chars(value.get("text"))
        if "content" in value:
            return _content_chars(value.get("content"))
        return 0
    return len(str(value))


def _message_chars(messages: Any) -> int:
    if not isinstance(messages, list):
        return 0
    total = 0
    for message in messages:
        if isinstance(message, dict):
            total += _content_chars(message.get("content"))
        else:
            total += _content_chars(message)
    return total


def _tool_name(tool: Any) -> str:
    if not isinstance(tool, dict):
        return ""
    name = tool.get("name")
    if name:
        return str(name)
    function = tool.get("function")
    if isinstance(function, dict) and function.get("name"):
        return str(function.get("name"))
    return ""


def _request_shape(request: dict[str, Any] | None) -> dict[str, Any]:
    if not isinstance(request, dict):
        return {}
    messages = request.get("messages")
    tools = request.get("tools")
    shape: dict[str, Any] = {}
    system = request.get("system")
    if system is not None:
        shape["system_chars"] = _content_chars(system)
    if isinstance(request.get("prompt"), str):
        shape["prompt_chars"] = len(str(request.get("prompt") or ""))
    if isinstance(messages, list):
        shape["message_count"] = len(messages)
        shape["message_chars"] = _message_chars(messages)
    if isinstance(tools, list):
        names = [_tool_name(tool) for tool in tools]
        shape["tool_count"] = len(tools)
        shape["tool_names"] = [name for name in names if name]
    if request.get("tool_choice") is not None:
        shape["tool_choice"] = request.get("tool_choice")
    if request.get("max_tokens") is not None:
        shape["max_tokens"] = request.get("max_tokens")
    if request.get("deadline") is not None:
        shape["deadline"] = request.get("deadline")
    return shape


def _phase_summary(records: list[dict[str, Any]]) -> dict[str, int]:
    counter = Counter(str(record.get("phase") or "") for record in records)
    return {
        out_key: int(counter.get(phase, 0))
        for phase, out_key in _PHASE_COUNT_KEYS.items()
    }


def _parse_ts(value: Any) -> datetime | None:
    if not value:
        return None
    text = str(value)
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        return datetime.fromisoformat(text)
    except Exception:
        return None


def _duration_ms(records: list[dict[str, Any]]) -> int | None:
    parsed = [_parse_ts(record.get("ts")) for record in records]
    parsed = [value for value in parsed if value is not None]
    if len(parsed) < 2:
        return None
    delta = max(parsed) - min(parsed)
    return int(delta.total_seconds() * 1000)


def _call_status(records: list[dict[str, Any]]) -> str:
    phases = {str(record.get("phase") or "") for record in records}
    if "response" in phases:
        return "completed"
    if "error" in phases:
        return "error"
    if "request" in phases:
        return "pending"
    return "orphaned"


def _wire_error_payload(record: dict[str, Any]) -> dict[str, Any] | None:
    response = record.get("response")
    if isinstance(response, dict) and isinstance(response.get("error"), dict):
        error = response.get("error") or {}
    elif isinstance(record.get("error"), dict):
        error = record.get("error") or {}
    else:
        return None
    out: dict[str, Any] = {"wire_attempt": record.get("wire_attempt")}
    for key in ("type", "message", "status_code", "request_id"):
        if error.get(key) is not None:
            out[key] = error.get(key)
    return {key: value for key, value in out.items() if value is not None}


def _first_value(records: list[dict[str, Any]], key: str) -> Any:
    for record in records:
        value = record.get(key)
        if value not in (None, ""):
            return value
    return None


def _wire_entry(record: dict[str, Any], *, include_payload: bool) -> dict[str, Any]:
    payload = record.get("request") if record.get("request") is not None else record.get("response")
    payload = payload if isinstance(payload, dict) else {}
    out: dict[str, Any] = {
        "phase": record.get("phase"),
        "ts": record.get("ts"),
        "provider": record.get("provider"),
        "model": record.get("model"),
        "wire_attempt": record.get("wire_attempt"),
    }
    for key in ("method", "url", "status", "elapsed_ms", "timeout"):
        if payload.get(key) is not None:
            out[key] = payload.get(key)
    if include_payload:
        if record.get("request") is not None:
            out["request"] = record.get("request")
        if record.get("response") is not None:
            out["response"] = record.get("response")
        if record.get("error") is not None:
            out["error"] = record.get("error")
    return {key: value for key, value in out.items() if value is not None}


def _wire_request_shape(record: dict[str, Any]) -> dict[str, Any]:
    request = record.get("request")
    if not isinstance(request, dict):
        return {}
    body = request.get("body")
    if not isinstance(body, dict):
        return {}
    shape: dict[str, Any] = {}
    messages = body.get("messages")
    if isinstance(messages, list):
        shape["message_count"] = len(messages)
        shape["message_chars"] = _message_chars(messages)
    tools = body.get("tools")
    if isinstance(tools, list):
        names = [_tool_name(tool) for tool in tools]
        shape["tool_count"] = len(tools)
        shape["tool_names"] = [name for name in names if name]
    max_tokens = body.get("max_completion_tokens", body.get("max_tokens"))
    if max_tokens is not None:
        shape["max_tokens"] = max_tokens
    for key in ("temperature", "tool_choice", "thinking"):
        if body.get(key) is not None:
            shape[key] = body.get(key)
    return shape


def _wire_response_summary(record: dict[str, Any]) -> dict[str, Any]:
    response = record.get("response")
    if not isinstance(response, dict):
        return {}
    out: dict[str, Any] = {}
    if response.get("status") is not None:
        out["status"] = response.get("status")
    body = response.get("body")
    if not isinstance(body, dict):
        return out
    choices = body.get("choices")
    choice = choices[0] if isinstance(choices, list) and choices else None
    if not isinstance(choice, dict):
        return out
    finish = choice.get("finish_reason")
    if finish is not None:
        out["finish_reason"] = finish
    message = choice.get("message")
    if isinstance(message, dict):
        tool_calls = message.get("tool_calls")
        if isinstance(tool_calls, list):
            names: list[str] = []
            for tool_call in tool_calls:
                if not isinstance(tool_call, dict):
                    continue
                function = tool_call.get("function")
                if isinstance(function, dict) and function.get("name"):
                    names.append(str(function.get("name")))
            if names:
                out["tool_call_names"] = names
    return out


def _summarise_call(
    call_id: str,
    records: list[dict[str, Any]],
    *,
    include_payload: bool,
) -> dict[str, Any]:
    records = sorted(records, key=lambda item: str(item.get("ts") or ""))
    request_record = next(
        (record for record in records if record.get("phase") == "request"),
        None,
    )
    response_record = next(
        (record for record in records if record.get("phase") == "response"),
        None,
    )
    error_record = next(
        (record for record in records if record.get("phase") == "error"),
        None,
    )
    anchor = request_record or response_record or error_record or records[0]
    out: dict[str, Any] = {
        "call_id": call_id,
        "status": _call_status(records),
        "api": anchor.get("api"),
        "caller": anchor.get("caller"),
        "task": anchor.get("task"),
        "tier": anchor.get("tier"),
        "provider": _first_value(records, "provider"),
        "model": _first_value(records, "model"),
        "first_ts": records[0].get("ts"),
        "last_ts": records[-1].get("ts"),
        "phases": _phase_summary(records),
    }
    duration = _duration_ms(records)
    if duration is not None:
        out["duration_ms"] = duration
    out["pending"] = out["status"] == "pending"
    for key in _CORRELATION_KEYS:
        value = _first_value(records, key)
        if value is not None:
            out[key] = value
    if request_record is not None:
        out["request_shape"] = _request_shape(request_record.get("request"))
    if response_record is not None:
        response = response_record.get("response")
        if isinstance(response, dict):
            out["response_summary"] = {
                key: response.get(key)
                for key in ("stop_reason", "usage", "provider", "model")
                if response.get(key) is not None
            }
    if error_record is not None and isinstance(error_record.get("error"), dict):
        out["error"] = error_record.get("error")
    wire_records = [
        record for record in records
        if str(record.get("phase") or "").startswith("wire_")
    ]
    if wire_records:
        last_wire = wire_records[-1]
        out["last_wire_phase"] = last_wire.get("phase")
        if last_wire.get("wire_attempt") is not None:
            out["last_wire_attempt"] = last_wire.get("wire_attempt")
        wire_errors = [
            payload for payload in (_wire_error_payload(record) for record in wire_records)
            if payload
        ]
        out["wire_error_count"] = len(wire_errors)
        if wire_errors:
            out["wire_error_summary"] = wire_errors
        out["wire"] = [
            _wire_entry(record, include_payload=include_payload)
            for record in wire_records
        ]
        wire_requests = [
            record for record in wire_records
            if record.get("phase") == "wire_request"
        ]
        wire_responses = [
            record for record in wire_records
            if record.get("phase") == "wire_response"
        ]
        if wire_requests:
            request_shape = _wire_request_shape(wire_requests[-1])
            if request_shape:
                out["wire_request_shape"] = request_shape
        if wire_responses:
            response_summary = _wire_response_summary(wire_responses[-1])
            if response_summary:
                out["wire_response_summary"] = response_summary
    if include_payload:
        if request_record is not None:
            out["request"] = request_record.get("request")
        if response_record is not None:
            out["response"] = response_record.get("response")
    return {key: value for key, value in out.items() if value not in (None, {}, [])}


def build_context_full_audit(
    path: str | Path,
    *,
    session_id: str | None = None,
    turn_id: str | None = None,
    call_id: str | None = None,
    subagent: str | None = None,
    team_run_id: str | None = None,
    context_scope: str | None = None,
    include_payload: bool = False,
    limit: int | None = None,
) -> dict[str, Any]:
    """Build a grouped audit view over ``llm_context_full.jsonl``.

    The JSONL file remains the canonical source. This helper only projects it
    into call-level groups so operators can join Agent, subagent, team-final,
    and provider wire records without manually scanning raw JSONL.
    """

    log_path = Path(path)
    records = [
        record for record in jsonl.read_all(log_path)
        if record.get("kind") == "llm.context_full"
        and _record_matches(
            record,
            session_id=session_id,
            turn_id=turn_id,
            call_id=call_id,
            subagent=subagent,
            team_run_id=team_run_id,
            context_scope=context_scope,
        )
    ]
    groups: dict[str, list[dict[str, Any]]] = {}
    order: list[str] = []
    for index, record in enumerate(records):
        cid = str(record.get("call_id") or f"record-{index}")
        if cid not in groups:
            groups[cid] = []
            order.append(cid)
        groups[cid].append(record)
    calls = [
        _summarise_call(cid, groups[cid], include_payload=include_payload)
        for cid in order
    ]
    calls = sorted(calls, key=lambda item: str(item.get("first_ts") or ""))
    if limit is not None and limit >= 0:
        calls = calls[-limit:]
    by_scope = Counter(
        str(call.get("context_scope") or "unknown")
        for call in calls
        if call.get("context_scope")
    )
    by_subagent = Counter(
        str(call.get("subagent") or "")
        for call in calls
        if call.get("subagent")
    )
    by_status = Counter(str(call.get("status") or "unknown") for call in calls)
    return {
        "path": str(log_path),
        "total_records": len(records),
        "call_count": len(calls),
        "pending_call_count": int(by_status.get("pending", 0)),
        "summary": _phase_summary(records),
        "by_status": dict(sorted(by_status.items())),
        "by_scope": dict(sorted(by_scope.items())),
        "by_subagent": dict(sorted(by_subagent.items())),
        "calls": calls,
    }


__all__ = ["build_context_full_audit"]
