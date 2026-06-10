"""Read-only messaging gateway diagnostics."""

from __future__ import annotations

from typing import Any

from ...messaging.diagnostics import diagnose_telegram_gateway
from ..types import ToolCall, ToolError, ToolErrorKind, ToolResult


GATEWAY_DIAGNOSE_SCHEMA = {
    "type": "object",
    "properties": {
        "platform": {
            "type": "string",
            "default": "telegram",
            "description": "Messaging platform to diagnose, e.g. telegram.",
        },
        "channel": {
            "type": "string",
            "default": "telegram",
            "description": "Configured channel id from messages/channels.yml.",
        },
        "chat_id": {
            "type": "string",
            "description": "Optional explicit Telegram chat_id to probe.",
        },
    },
}


def gateway_diagnose_handler(call: ToolCall, *, paths: Any | None = None) -> ToolResult:
    if paths is None:
        return ToolResult.from_error(
            tool_use_id=call.id,
            name=call.name,
            error=ToolError(
                kind=ToolErrorKind.NOT_FOUND,
                message="gateway_diagnose requires workspace paths",
                retryable=False,
            ),
        )

    args = call.arguments or {}
    platform = str(args.get("platform") or "telegram").strip().lower()
    channel = str(args.get("channel") or platform or "telegram").strip() or "telegram"
    if platform != "telegram":
        return ToolResult.from_json(
            tool_use_id=call.id,
            name=call.name,
            data={
                "ok": False,
                "platform": platform,
                "channel": channel,
                "error": f"gateway diagnose for platform '{platform}' is not available",
                "hint": "Use gateway platform/status APIs for this platform, or configure a platform-specific diagnostic.",
            },
        )

    data = diagnose_telegram_gateway(
        paths,
        channel=channel,
        chat_id=args.get("chat_id"),
    )
    return ToolResult.from_json(tool_use_id=call.id, name=call.name, data=data)


__all__ = ["GATEWAY_DIAGNOSE_SCHEMA", "gateway_diagnose_handler"]
