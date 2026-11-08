"""Native tools for the data-source sync ledger."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

from ...core.config import Config
from ...data_sources import sync_contributors, sync_state
from ..types import ToolCall, ToolError, ToolErrorKind, ToolResult


_STATUS_GUIDANCE = (
    "This is a historical sync ledger inventory, not a live endpoint probe. "
    "ok means the inventory was read successfully, not that providers are healthy. "
    "Observed providers/source IDs identify ledger rows only, including seeded rows. "
    "stale and stale_count describe ledger freshness (including no recorded success), "
    "not current provider availability. Never infer DefiLlama health from CCXT, "
    "another source, or global stale counts. Provider health is not_checked by "
    "this tool, even when matching ledger rows or recent events exist. An empty "
    "provider match means no ledger coverage, not an unavailable provider. "
    "Report a data_api transport failure only for the attempted provider/endpoint; "
    "it does not establish the upstream cause or an outage. Do not fabricate health."
)


DATA_SOURCE_STATUS_SCHEMA: dict[str, Any] = {
    "type": "object",
    "description": _STATUS_GUIDANCE,
    "properties": {
        "provider": {
            "type": "string",
            "description": (
                "Optional provider identifier, e.g. defillama or ccxt, to identify "
                "matching ledger rows (case-insensitive exact match). Does not "
                "filter the inventory or probe the provider; health stays not_checked."
            ),
        },
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
    sources = summary.get("sources") or []
    provider = str(args.get("provider") or "").strip().casefold()
    payload: dict[str, Any] = {
        "ok": True,
        "interpretation_guidance": _STATUS_GUIDANCE,
        "diagnostic_scope": {
            "kind": "historical_sync_ledger",
            "live_probe_performed": False,
            "provider_health": "not_checked",
            "observed_source_ids": sorted(row["source_id"] for row in sources),
            "observed_providers": sorted({
                row["provider"] for row in sources if row.get("provider")
            }),
            "requested_provider": provider or None,
            "requested_provider_source_ids": [
                row["source_id"] for row in sources
                if provider and str(row.get("provider") or "").strip().casefold() == provider
            ],
        },
        "summary": summary,
        "sources": sources,
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
