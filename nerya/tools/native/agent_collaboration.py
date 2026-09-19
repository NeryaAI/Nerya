"""Collaboration tools derive sender and conversation from trusted call metadata."""
from __future__ import annotations

from typing import Any
from ...subagents.threads import AgentThreadStore
from ..types import ToolCall, ToolError, ToolErrorKind, ToolResult

PEERS_SCHEMA: dict[str, Any] = {"type": "object", "properties": {}}
MESSAGE_SCHEMA: dict[str, Any] = {
    "type": "object", "properties": {
        "to": {"type": "string", "description": "Persistent agent id returned by subagent_peers."},
        "message": {"type": "string", "minLength": 1, "maxLength": 16000},
    }, "required": ["to", "message"],
}


def collaboration_handler(call: ToolCall, *, config: Any, send: bool = False) -> ToolResult:
    meta = call.metadata or {}
    session_id = str(meta.get("agent_parent_session_id") or meta.get("session_id") or "")
    sender = str(meta.get("agent_id") or "lead")
    try:
        if not session_id:
            raise ValueError("collaboration requires an active conversation")
        store = AgentThreadStore(config.paths)
        if send:
            args = call.arguments or {}
            data = {"message": store.send(
                session_id=session_id, sender=sender,
                recipient=str(args.get("to") or ""), content=str(args.get("message") or ""),
                message_id=f"tool:{call.id}",
            ), "delivery": "queued; injected at the recipient's next loop boundary, or on continuation"}
        else:
            peers = store.list(session_id)
            if sender != "lead":
                source = store.load(sender, session_id)
                peers = [p for p in peers if p["group_id"] == source["group_id"]]
            data = {"self": sender, "agents": peers}
        return ToolResult.from_json(tool_use_id=call.id, name=call.name, data=data)
    except (ValueError, OSError) as exc:
        return ToolResult.from_error(tool_use_id=call.id, name=call.name,
            error=ToolError(kind=ToolErrorKind.SCHEMA_VALIDATION, message=str(exc)))
