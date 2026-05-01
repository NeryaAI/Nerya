"""HTTP surface for the provider auth manager.

Exposes the OAuth/provider auth scaffolding so dashboards and CLI
tools can:

- ``GET  /security/provider_auth/list`` — redacted list of stored
  provider credentials (no secret material).
- ``GET  /security/provider_auth/status`` — per-provider snapshot
  (does it have credentials, are they expired, what scopes).
- ``POST /security/provider_auth/register`` — register a provider
  credential.  Token + refresh_token go into the vault if it is
  attached, otherwise the test mode is stored in the JSON record.
- ``POST /security/provider_auth/revoke`` — revoke a credential.
- ``POST /security/provider_auth/refresh`` — call the registered
  refresh callback (if any) to mint a new token.

The handlers deliberately do not start an OAuth dance themselves —
they're scaffolding so a future browser/web tool can plug into the
same store.  Nothing here writes to the network.
"""

from __future__ import annotations

from typing import Any

from ..security.provider_auth import (
    NeedsReauth,
    ProviderAuthError,
    ProviderAuthManager,
    ProviderAuthStore,
    ProviderConfig,
    ProviderNotConfigured,
)


def _open_manager(client) -> ProviderAuthManager:
    """Return the per-client manager, creating it on demand.

    We bind the manager to the workspace path so subsequent calls
    re-read the store (and pick up records written through the CLI).
    """
    paths = client.config.paths
    cache = getattr(client, "_provider_auth_manager", None)
    if cache is not None:
        return cache
    store = ProviderAuthStore.open(paths.provider_auth, vault=getattr(client, "vault", None))
    mgr = ProviderAuthManager(store=store)
    setattr(client, "_provider_auth_manager", mgr)
    return mgr


def _list(client, _payload):
    mgr = _open_manager(client)
    return {"ok": True, "records": mgr.public_view()}


def _status(client, payload):
    provider = str(payload.get("provider") or "").strip()
    actor_id = str(payload.get("actor_id") or "default").strip() or "default"
    mgr = _open_manager(client)
    if provider:
        rec = mgr.store.get(provider, actor_id)
        return {
            "ok": True,
            "provider": provider,
            "actor_id": actor_id,
            "configured": rec is not None,
            "active": bool(rec and rec.is_active()),
            "expired": bool(rec and rec.is_expired()),
            "record": rec.public_view() if rec else None,
        }
    out = []
    for rec in mgr.store.list():
        out.append({
            "provider": rec.provider,
            "actor_id": rec.actor_id,
            "active": rec.is_active(),
            "expired": rec.is_expired(),
            "health": rec.health,
            "scopes": list(rec.scopes),
            "expires_at": rec.expires_at,
        })
    return {"ok": True, "records": out}


def _register(client, payload):
    mgr = _open_manager(client)
    provider = str(payload.get("provider") or "").strip()
    if not provider:
        return {"ok": False, "error": "provider required"}
    kind = str(payload.get("kind") or "api_key").strip() or "api_key"
    actor_id = str(payload.get("actor_id") or "default").strip() or "default"
    scopes = payload.get("scopes") or []
    token = str(payload.get("token") or "")
    refresh_token = str(payload.get("refresh_token") or "")
    expires_at = payload.get("expires_at")
    metadata = payload.get("metadata") or {}
    try:
        rec = mgr.store.register(
            provider=provider,
            kind=kind,
            actor_id=actor_id,
            scopes=scopes if isinstance(scopes, list) else [str(scopes)],
            token=token,
            refresh_token=refresh_token,
            expires_at=expires_at,
            metadata=metadata if isinstance(metadata, dict) else {},
        )
    except ProviderAuthError as exc:
        return {"ok": False, "error": str(exc)}
    return {"ok": True, "record": rec.public_view()}


def _revoke(client, payload):
    mgr = _open_manager(client)
    provider = str(payload.get("provider") or "").strip()
    actor_id = str(payload.get("actor_id") or "default").strip() or "default"
    reason = str(payload.get("reason") or "manual")
    if not provider:
        return {"ok": False, "error": "provider required"}
    mgr.store.revoke(provider, actor_id, reason=reason)
    rec = mgr.store.get(provider, actor_id)
    return {"ok": True, "record": rec.public_view() if rec else None}


def _refresh(client, payload):
    mgr = _open_manager(client)
    provider = str(payload.get("provider") or "").strip()
    actor_id = str(payload.get("actor_id") or "default").strip() or "default"
    if not provider:
        return {"ok": False, "error": "provider required"}
    try:
        token = mgr.get_token(provider, actor_id=actor_id)
    except NeedsReauth as exc:
        payload = exc.to_dict()
        payload.update({"ok": False, "actor_id": actor_id})
        return payload
    except ProviderNotConfigured:
        return {
            "ok": False,
            "error": "provider not configured",
            "provider": provider,
            "actor_id": actor_id,
        }
    rec = mgr.store.get(provider, actor_id)
    return {
        "ok": True,
        "provider": provider,
        "actor_id": actor_id,
        "token_fingerprint": mgr.store.fingerprint_for(provider, actor_id),
        "record": rec.public_view() if rec else None,
        "token_present": bool(token),
    }


def _reauth(client, payload):
    mgr = _open_manager(client)
    provider = str(payload.get("provider") or "").strip()
    actor_id = str(payload.get("actor_id") or "default").strip() or "default"
    if not provider:
        return {"ok": False, "error": "provider required"}
    return {"ok": True, "payload": mgr.reauth_payload(provider, actor_id=actor_id)}


def routes():
    return [
        ("GET", "/security/provider_auth/list", _list),
        ("POST", "/security/provider_auth/list", _list),
        ("GET", "/security/provider_auth/status", _status),
        ("POST", "/security/provider_auth/status", _status),
        ("POST", "/security/provider_auth/register", _register),
        ("POST", "/security/provider_auth/revoke", _revoke),
        ("POST", "/security/provider_auth/refresh", _refresh),
        ("POST", "/security/provider_auth/reauth", _reauth),
    ]


def register_default_configs(manager: ProviderAuthManager) -> None:
    """Pre-populate a small set of provider configs.

    These intentionally have no ``refresh_fn`` — they document the
    expected providers so the dashboard can show them, even if no
    record has been registered yet. Actual refresh wiring is deferred
    to provider-specific modules.
    """

    defaults = [
        ProviderConfig(
            provider="openai",
            kind="api_key",
            description="OpenAI API key (chat/completions/responses).",
        ),
        ProviderConfig(
            provider="anthropic",
            kind="api_key",
            description="Anthropic API key (Claude messages API).",
        ),
        ProviderConfig(
            provider="google",
            kind="oauth",
            scopes=["https://www.googleapis.com/auth/userinfo.email"],
            description="Google OAuth (used for Gemini API + workspace).",
        ),
        ProviderConfig(
            provider="okx_os",
            kind="api_key",
            description="OKX Web3 wallet API key.",
        ),
        ProviderConfig(
            provider="mcp_server",
            kind="oauth",
            description="External MCP server OAuth credential.",
        ),
    ]
    for cfg in defaults:
        manager.register_config(cfg)


__all__ = ["routes", "register_default_configs"]
