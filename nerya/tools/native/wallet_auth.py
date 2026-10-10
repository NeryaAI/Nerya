"""Bounded wallet login handoffs; never creates wallets or sends funds."""
from types import SimpleNamespace

from ..types import ToolCall, ToolError, ToolErrorKind, ToolResult


WALLET_AUTH_SCHEMA = {
    "type": "object",
    "properties": {
        "op": {"type": "string", "enum": ["status", "start", "verify"]},
        "provider": {"type": "string"},
        "wallet_id": {"type": "string"},
        "chain": {"type": "string"},
        "email": {"type": "string"},
        "auth_session_id": {"type": "string"},
        "restart": {"type": "boolean", "description": "Start a new login only after an expired/failed handoff and operator approval."},
        "qrCodeId": {"type": "string"},
    },
    "required": ["op", "provider"],
    "additionalProperties": False,
}


def wallet_auth_handler(call: ToolCall, *, config_like, actor_id: str, session_id: str | None) -> ToolResult:
    from ...api.routes_wallet import routes
    from ...wallet.registry import resolve_provider_name

    args = dict(call.arguments or {})
    op = str(args.pop("op", ""))
    provider = resolve_provider_name(str(args.get("provider") or ""))
    if config_like is None or provider is None or op not in {"status", "start", "verify"}:
        return ToolResult.from_error(tool_use_id=call.id, name=call.name, error=ToolError(
            kind=ToolErrorKind.SCHEMA_VALIDATION, message="wallet_auth requires a configured workspace, known provider and status/start/verify operation.",
            retryable=False))
    args.update(provider=provider, install=False, create_binding=False, auth_context_id=session_id or "default")
    # wallet_id scopes the login session, but must not turn login/status into a
    # binding write or an automatic wallet/account creation.
    wallet_id = args.pop("wallet_id", None)
    if wallet_id:
        args["auth_context_id"] = wallet_id
    client = SimpleNamespace(config=config_like, auth_actor_id=actor_id)
    handler = next(handler for method, path, handler in routes()
                   if method == "POST" and path == f"/wallet/auth/{op}")
    result = handler(client, args)
    if not result.get("ok"):
        return ToolResult.from_error(tool_use_id=call.id, name=call.name, error=ToolError(
            kind=ToolErrorKind.PROVIDER_ERROR, message=str(result.get("error") or "wallet_auth_failed"),
            detail=result, retryable=False,
            recovery_hint={"action": "wallet_auth", "hint": "Resume the pending session; complete browser/OTP approval in the operator UI before verifying. Do not retry unchanged."}))
    response = ToolResult.from_json(tool_use_id=call.id, name=call.name, data=result)
    response.metadata["read_only"] = op == "status"
    return response
