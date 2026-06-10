"""Native tools for the data-source sync ledger."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

from ...core.config import Config
from ...data_sources import sync_contributors, sync_state
from ..types import ToolCall, ToolError, ToolErrorKind, ToolResult


DATA_SOURCE_STATUS_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "include_events": {
            "type": "boolean",
            "default": False,
            "description": "Include recent sync events with the status snapshot.",
        },
        "limit": {
            "type": "integer",
            "minimum": 1,
            "maximum": 100,
            "default": 20,
            "description": "Maximum recent events when include_events is true.",
        },
    },
}


DATA_SOURCE_SYNC_NOW_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "source_id": {
            "type": "string",
            "description": "Data-source id such as memory:notebook, account:paper_main, or market:public_ccxt.",
        },
        "include_events": {
            "type": "boolean",
            "default": True,
            "description": "Include recent sync events after triggering the sync.",
        },
    },
    "required": ["source_id"],
}


def data_source_status_handler(
    call: ToolCall,
    *,
    config: Config | None,
) -> ToolResult:
    client = _client_or_error(call, config)
    if isinstance(client, ToolResult):
        return client
    args = dict(call.arguments or {})
    sync_contributors.install_default_contributors()
    sync_contributors.seed_additional_rows(client)
    summary = sync_state.summarize(client)
    payload: dict[str, Any] = {
        "ok": True,
        "summary": summary,
        "sources": summary.get("sources") or [],
        "total": summary.get("total") or 0,
        "stale_count": summary.get("stale_count") or 0,
    }
    if bool(args.get("include_events")):
        payload["events"] = sync_state.events(
            client,
            limit=_limit(args.get("limit"), default=20, maximum=100),
        )
    return ToolResult.from_json(tool_use_id=call.id, name=call.name, data=payload)


def data_source_sync_now_handler(
    call: ToolCall,
    *,
    config: Config | None,
) -> ToolResult:
    client = _client_or_error(call, config)
    if isinstance(client, ToolResult):
        return client
    args = dict(call.arguments or {})
    source_id = str(args.get("source_id") or "").strip()
    if not source_id:
        return _error(
            call,
            ToolErrorKind.SCHEMA_VALIDATION,
            "source_id is required",
            retryable=False,
        )
    sync_contributors.install_default_contributors()
    sync_contributors.seed_additional_rows(client)
    result = sync_state.sync_now(client, source_id)
    payload: dict[str, Any] = {
        "ok": bool(result.get("ok")) if isinstance(result, dict) else False,
        "source_id": source_id,
        "result": result,
        "row": sync_state.get(client, source_id),
    }
    if bool(args.get("include_events", True)):
        payload["events"] = sync_state.events(client, limit=20)
    return ToolResult.from_json(tool_use_id=call.id, name=call.name, data=payload)


def _client_or_error(call: ToolCall, config: Config | None) -> SimpleNamespace | ToolResult:
    if config is None:
        return _error(
            call,
            ToolErrorKind.PROVIDER_ERROR,
            "data source tools require a workspace config",
            retryable=False,
        )
    return SimpleNamespace(config=config)


def _limit(value: Any, *, default: int, maximum: int) -> int:
    try:
        return max(1, min(int(value), maximum))
    except (TypeError, ValueError):
        return default


def _error(
    call: ToolCall,
    kind: ToolErrorKind,
    message: str,
    *,
    retryable: bool | None,
) -> ToolResult:
    return ToolResult.from_error(
        tool_use_id=call.id,
        name=call.name,
        error=ToolError(kind=kind, message=message, retryable=retryable),
    )


__all__ = [
    "DATA_SOURCE_STATUS_SCHEMA",
    "DATA_SOURCE_SYNC_NOW_SCHEMA",
    "data_source_status_handler",
    "data_source_sync_now_handler",
]
