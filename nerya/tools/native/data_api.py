"""Native tool wrapper for provider-specific read-only data APIs."""

from __future__ import annotations

from typing import Any

from ...data_api import (
    DataApiContext,
    DataApiError,
    DataApiRegistry,
    build_data_api_registry,
    compact_data_result,
)
from ..types import ToolCall, ToolError, ToolErrorKind, ToolResult


DATA_API_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "op": {
            "type": "string",
            "enum": ["list", "schema", "call"],
            "description": "Use list to discover actions, schema to inspect one action, call to execute a read-only action.",
        },
        "provider": {
            "type": "string",
            "description": "Provider namespace, e.g. akshare, wallet, onchainos.",
        },
        "action": {
            "type": "string",
            "description": "Provider action/function name. For AkShare this is the AkShare function name.",
        },
        "query": {
            "type": "string",
            "description": "Optional search text for op=list.",
        },
        "tags": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Optional tag filter for op=list.",
        },
        "args": {
            "type": "object",
            "additionalProperties": True,
            "description": "JSON arguments passed to provider.action for op=call.",
        },
        "limit": {
            "type": "integer",
            "minimum": 1,
            "maximum": 500,
            "default": 50,
            "description": "Max actions for list or max rows returned for call.",
        },
        "columns": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Optional column projection for table-like call results.",
        },
    },
    "required": ["op"],
}


def data_api_handler(
    call: ToolCall,
    *,
    config_like: Any | None = None,
    registry: DataApiRegistry | None = None,
) -> ToolResult:
    args = dict(call.arguments or {})
    op = str(args.get("op") or "").strip().lower()
    reg = registry or build_data_api_registry()
    context = DataApiContext(config_like=config_like)
    if op in ("call", "schema"):
        missing = [
            field
            for field in ("provider", "action")
            if str(args.get(field) or "").strip() == ""
        ]
        if missing:
            return _missing_route_error(call, reg, op=op, missing=missing)
    try:
        if op == "list":
            payload = reg.list(
                provider=args.get("provider"),
                query=str(args.get("query") or ""),
                tags=args.get("tags") if isinstance(args.get("tags"), list) else (),
                limit=_limit(args.get("limit"), default=20, maximum=100),
            )
            return ToolResult.from_json(tool_use_id=call.id, name=call.name, data=payload)
        if op == "schema":
            provider = _required(args, "provider")
            action = _required(args, "action")
            payload = reg.schema(provider, action)
            return ToolResult.from_json(tool_use_id=call.id, name=call.name, data=payload)
        if op == "call":
            provider = _required(args, "provider")
            action = _required(args, "action")
            if (
                provider.lower() == "wallet"
                and action.lower() in {"capability_catalog", "meme_strategy_guide"}
                and "limit" in args
            ):
                return _error(
                    call,
                    ToolErrorKind.SCHEMA_VALIDATION,
                    (
                        "limit does not expand wallet catalog/guide object results. "
                        "Use the compact next_required_action and bounded_sequence "
                        "already returned. For read-only lookup, call the selected "
                        "market_data/data_api route and summarize. For strategy "
                        "authoring, read strategy_author with skill_view and move "
                        "to strategy_draft_proposal, then edit the staged proposal "
                        "files with SDK code instead of repeating catalog discovery."
                    ),
                    detail={"provider": provider, "action": action},
                    retryable=False,
                )
            payload_args = args.get("args")
            if payload_args is None:
                payload_args = args.get("payload")
            if not isinstance(payload_args, dict):
                payload_args = {}
            raw = reg.call(provider, action, payload_args, context=context)
            payload = compact_data_result(
                provider,
                action,
                raw,
                limit=_limit(args.get("limit"), default=50, maximum=500),
                columns=args.get("columns") if isinstance(args.get("columns"), list) else (),
            )
            return ToolResult.from_json(tool_use_id=call.id, name=call.name, data=payload)
        return _error(
            call,
            ToolErrorKind.SCHEMA_VALIDATION,
            "data_api op must be one of list, schema, call",
            detail={"op": args.get("op")},
            retryable=False,
        )
    except DataApiError as exc:
        detail = exc.detail if isinstance(exc.detail, dict) else {}
        detail = {
            **detail,
            "op": op,
            "provider": args.get("provider"),
            "action": args.get("action"),
        }
        return _error(
            call,
            _tool_error_kind(exc.kind),
            exc.message,
            detail=detail,
            retryable=exc.retryable,
        )
    except Exception as exc:
        return _error(
            call,
            ToolErrorKind.PROVIDER_ERROR,
            str(exc),
            detail={"op": op, "provider": args.get("provider"), "action": args.get("action")},
            retryable=None,
        )


def _missing_route_error(
    call: ToolCall,
    reg: DataApiRegistry,
    *,
    op: str,
    missing: list[str],
) -> ToolResult:
    """Self-correcting error for op=call/schema with no provider/action.

    The model frequently emits a bare ``{"op": "call"}`` and then retries
    the identical empty call (which the dedupe guard suppresses), so the
    terse "missing required field" was producing back-to-back failures.
    Return the available providers/aliases and the exact discovery path so
    the next call is valid instead of a repeat.
    """

    try:
        providers = reg.providers()
    except Exception:
        providers = []
    try:
        aliases = reg.aliases()
    except Exception:
        aliases = {}
    example_provider = providers[0] if providers else None
    return _error(
        call,
        ToolErrorKind.SCHEMA_VALIDATION,
        (
            f"data_api op={op!r} needs both 'provider' and 'action' "
            f"(missing: {', '.join(missing)}). There is no default route — "
            "discover one first: call op='list' (optionally with a provider "
            "to narrow), then op='schema' to inspect an action's inputs, then "
            "op='call' with provider + action + args. Do not resend this "
            "exact call with empty arguments."
        ),
        detail={
            "op": op,
            "missing": missing,
            "providers": providers,
            "aliases": aliases,
            "example_next_call": (
                {"op": "list", "provider": example_provider}
                if example_provider
                else {"op": "list"}
            ),
        },
        retryable=False,
        recovery_hint={
            "action": "data_api_list_first",
            "hint": "Run data_api op='list' to discover provider/action, then call.",
            "providers": providers[:40],
        },
    )


def _required(args: dict[str, Any], key: str) -> str:
    value = args.get(key)
    if value is None or str(value).strip() == "":
        raise DataApiError(
            f"missing required field: {key}",
            kind="schema_validation",
            detail={"field": key},
            retryable=False,
        )
    return str(value)


def _limit(value: Any, *, default: int, maximum: int) -> int:
    try:
        return max(1, min(int(value), maximum))
    except (TypeError, ValueError):
        return default


def _tool_error_kind(kind: str) -> ToolErrorKind:
    mapping = {
        "schema_validation": ToolErrorKind.SCHEMA_VALIDATION,
        "not_found": ToolErrorKind.NOT_FOUND,
        "provider_error": ToolErrorKind.PROVIDER_ERROR,
        "execution_error": ToolErrorKind.EXECUTION_ERROR,
    }
    return mapping.get(kind, ToolErrorKind.PROVIDER_ERROR)


def _error(
    call: ToolCall,
    kind: ToolErrorKind,
    message: str,
    *,
    detail: dict[str, Any] | None = None,
    retryable: bool | None = None,
    recovery_hint: dict[str, Any] | None = None,
) -> ToolResult:
    return ToolResult.from_error(
        tool_use_id=call.id,
        name=call.name,
        error=ToolError(
            kind=kind,
            message=message,
            detail=dict(detail or {}),
            retryable=retryable,
            recovery_hint=dict(recovery_hint or {}),
        ),
    )
