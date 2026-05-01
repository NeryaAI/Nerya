"""Resource native tools — Phase 12.

Two read-only tools mirror what Claude Code's MCP layer exposes for
``resources/list`` and ``resources/read``:

* ``resource_list`` — enumerate everything in the workspace
  :class:`ResourceIndex` (typically populated from MCP servers'
  ``resources/list``).
* ``resource_read`` — fetch a single resource by URI; the
  :class:`ResourceEntry` decides how to materialise the body
  (in-memory text, lazy HTTP fetch, mcp ``resources/read`` RPC, …).

Both tools are ``risk=READ`` / ``permission_scope=NETWORK`` so MCP
mode policies can gate them — but they're auto-approved by default
because the model needs cheap discovery.
"""

from __future__ import annotations

from typing import Any

from ..resources import ResourceIndex
from ..types import (
    ToolCall,
    ToolError,
    ToolErrorKind,
    ToolResult,
)


RESOURCE_LIST_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "source": {
            "type": "string",
            "description": (
                "Optional source filter ('mcp', 'workspace', 'local'). "
                "Omit to list everything."
            ),
        },
        "limit": {"type": "integer", "minimum": 1, "default": 50},
    },
}

RESOURCE_READ_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "uri": {
            "type": "string",
            "description": "Exact URI of the resource to fetch.",
        },
    },
    "required": ["uri"],
}


def resource_list_handler(
    call: ToolCall, *, index: ResourceIndex,
) -> ToolResult:
    args = call.arguments or {}
    source = args.get("source") or None
    limit = max(1, int(args.get("limit") or 50))
    entries = index.list_entries(source=source)[:limit]
    return ToolResult.from_json(
        tool_use_id=call.id,
        name=call.name,
        data={
            "count": len(entries),
            "resources": [e.asdict() for e in entries],
        },
    )


def resource_read_handler(
    call: ToolCall, *, index: ResourceIndex,
) -> ToolResult:
    args = call.arguments or {}
    uri = (args.get("uri") or "").strip()
    if not uri:
        return ToolResult.from_error(
            tool_use_id=call.id,
            name=call.name,
            error=ToolError(
                kind=ToolErrorKind.SCHEMA_VALIDATION,
                message="uri is required",
            ),
        )
    try:
        body = index.fetch(uri)
    except KeyError:
        return ToolResult.from_error(
            tool_use_id=call.id,
            name=call.name,
            error=ToolError(
                kind=ToolErrorKind.NOT_FOUND,
                message=f"unknown resource: {uri}",
                retryable=False,
                recovery_hint={
                    "action": "resource_list_first",
                    "hint": "Call resource_list to see available URIs.",
                },
            ),
        )
    except Exception as exc:
        return ToolResult.from_error(
            tool_use_id=call.id,
            name=call.name,
            error=ToolError(
                kind=ToolErrorKind.PROVIDER_ERROR,
                message=f"{type(exc).__name__}: {exc}",
                retryable=True,
            ),
        )
    return ToolResult.from_json(
        tool_use_id=call.id,
        name=call.name,
        data=body,
    )


__all__ = [
    "RESOURCE_LIST_SCHEMA",
    "RESOURCE_READ_SCHEMA",
    "resource_list_handler",
    "resource_read_handler",
]
