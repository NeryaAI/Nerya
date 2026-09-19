"""Operator-requested continuous strategy lifecycle, never auto-start on draft."""
from __future__ import annotations

from ..types import ToolCall, ToolResult, ToolError, ToolErrorKind

SCHEMA = {
    "type": "object", "additionalProperties": False,
    "properties": {
        "action": {"type": "string", "enum": ["status", "events", "start", "stop"]},
        "strategy_id": {"type": "string"},
        "expected_hash": {"type": "string", "description": "Current reviewed package hash from status; required for start"},
    }, "required": ["action", "strategy_id"],
}


def handle(call: ToolCall, *, deps) -> ToolResult:
    args = call.arguments or {}
    try:
        from ...sdk.strategy_api import StrategyAPI
        if deps.active_strategy_id:
            raise PermissionError("strategy workers cannot control service lifecycles; ask the operator in the main conversation")
        api = StrategyAPI(deps.config, None)
        sid, action = args["strategy_id"], args["action"]
        if action == "status":
            result = api.status(sid)
        elif action == "events":
            result = api.service_events(sid)
        elif action == "start":
            result = api.service_start(sid, expected_hash=args.get("expected_hash", ""))
        elif action == "stop":
            result = api.service_stop(sid)
        else:
            raise ValueError("unknown service action")
        return ToolResult.from_json(tool_use_id=call.id, name=call.name, data=result)
    except Exception as exc:
        return ToolResult.from_error(tool_use_id=call.id, name=call.name,
            error=ToolError(kind=ToolErrorKind.PERMISSION_DENIED if isinstance(exc, PermissionError) else ToolErrorKind.EXECUTION_ERROR, message=str(exc)))
