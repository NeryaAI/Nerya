"""HTTP surface for Nerya-managed OAuth logins.

The dashboard uses these to render "Sign in with ChatGPT" /
"Sign in with Claude" cards in the model-allocation tab. They are
companion endpoints to ``routes_provider_auth`` (which deals with
arbitrary OAuth records) — this module is the user-facing slice that
covers the dashboard-managed login providers.

Endpoints:

* ``GET  /llm/oauth/providers`` — static catalogue of supported logins.
* ``GET  /llm/oauth/status``    — per-provider status (configured,
  expired, env-fallback present).
* ``POST /llm/oauth/import``    — copy credentials from the upstream
  CLI (``~/.codex/auth.json`` or ``~/.claude/.credentials.json``).
* ``POST /llm/oauth/paste``     — accept a manually-pasted token.
* ``POST /llm/oauth/revoke``    — revoke a stored OAuth record.
"""

from __future__ import annotations


from ..llm import oauth_login as _oauth


def routes():
    def providers(_client, _payload):
        return {"providers": _oauth.list_oauth_providers()}

    def status(client, payload):
        body = payload or {}
        provider = str(body.get("provider") or "").strip()
        if provider:
            try:
                return _oauth.status_for(
                    client.config,
                    provider=provider,
                    actor_id=str(body.get("actor_id") or "default"),
                    vault_passphrase=body.get("vault_passphrase"),
                )
            except _oauth.OAuthLoginError as exc:
                return {"ok": False, "error": str(exc)}
        return _oauth.all_status(
            client.config, vault_passphrase=body.get("vault_passphrase"),
        )

    def import_credentials(client, payload):
        body = payload or {}
        try:
            return _oauth.import_provider_credentials(
                client.config,
                provider=str(body.get("provider") or ""),
                actor_id=str(body.get("actor_id") or "default"),
                path=body.get("path"),
                vault_passphrase=body.get("vault_passphrase"),
            )
        except _oauth.OAuthLoginError as exc:
            return {"ok": False, "error": str(exc)}

    def paste_token(client, payload):
        body = payload or {}
        try:
            expires_at = body.get("expires_at")
            if isinstance(expires_at, (int, float, str)) and str(expires_at).strip():
                try:
                    expires_at = float(expires_at)
                except (TypeError, ValueError):
                    expires_at = None
            else:
                expires_at = None
            return _oauth.set_paste_token(
                client.config,
                provider=str(body.get("provider") or ""),
                token=str(body.get("token") or ""),
                refresh_token=str(body.get("refresh_token") or ""),
                actor_id=str(body.get("actor_id") or "default"),
                expires_at=expires_at,
                vault_passphrase=body.get("vault_passphrase"),
            )
        except _oauth.OAuthLoginError as exc:
            return {"ok": False, "error": str(exc)}

    def revoke(client, payload):
        body = payload or {}
        try:
            return _oauth.revoke(
                client.config,
                provider=str(body.get("provider") or ""),
                actor_id=str(body.get("actor_id") or "default"),
                reason=str(body.get("reason") or "manual"),
                vault_passphrase=body.get("vault_passphrase"),
            )
        except _oauth.OAuthLoginError as exc:
            return {"ok": False, "error": str(exc)}

    def login_directive(_client, payload):
        body = payload or {}
        try:
            return {"ok": True, "directive": _oauth.login_directive(
                provider=str(body.get("provider") or ""),
            )}
        except _oauth.OAuthLoginError as exc:
            return {"ok": False, "error": str(exc)}

    def device_code_start(_client, payload):
        body = payload or {}
        try:
            data = _oauth.device_code_start(provider=str(body.get("provider") or ""))
            return {"ok": True, **data}
        except _oauth.OAuthLoginError as exc:
            return {"ok": False, "error": str(exc)}

    def device_code_poll(client, payload):
        body = payload or {}
        try:
            data = _oauth.device_code_poll(
                client.config,
                provider=str(body.get("provider") or ""),
                device_code=str(body.get("device_code") or ""),
                actor_id=str(body.get("actor_id") or "default"),
                vault_passphrase=body.get("vault_passphrase"),
            )
            return {"ok": True, **data}
        except _oauth.OAuthLoginError as exc:
            return {"ok": False, "error": str(exc)}

    return [
        ("GET", "/llm/oauth/providers", providers),
        ("GET", "/llm/oauth/status", status),
        ("POST", "/llm/oauth/status", status),
        ("POST", "/llm/oauth/import", import_credentials),
        ("POST", "/llm/oauth/paste", paste_token),
        ("POST", "/llm/oauth/revoke", revoke),
        ("POST", "/llm/oauth/login_directive", login_directive),
        ("POST", "/llm/oauth/device_code/start", device_code_start),
        ("POST", "/llm/oauth/device_code/poll", device_code_poll),
    ]
