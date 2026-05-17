"""Admin password and JWT login routes for the local API."""

from __future__ import annotations

from typing import Any

from . import auth as auth_mod


def routes():
    def status(client, _payload):
        return auth_mod.admin_auth_status(client.config)

    def login(client, payload: dict[str, Any]):
        password = str((payload or {}).get("password") or "")
        if not auth_mod.has_admin_password(client.config):
            return {
                "ok": False,
                "error": "admin_password_not_configured",
                "detail": "Set the admin password from a local dashboard session first.",
            }
        if not auth_mod.verify_admin_password(client.config, password):
            return {"ok": False, "error": "invalid_password"}
        token = auth_mod.issue_admin_jwt(client.config)
        return {"ok": True, **token}

    def set_password(client, payload: dict[str, Any]):
        body = payload or {}
        new_password = str(body.get("new_password") or body.get("password") or "")
        current_password = str(body.get("current_password") or "")
        if auth_mod.has_admin_password(client.config):
            if not current_password:
                return {"ok": False, "error": "current_password_required"}
            if not auth_mod.verify_admin_password(client.config, current_password):
                return {"ok": False, "error": "invalid_current_password"}
        try:
            auth_mod.set_admin_password(client.config, new_password)
        except ValueError as exc:
            return {"ok": False, "error": str(exc)}
        token = auth_mod.issue_admin_jwt(client.config)
        return {
            "ok": True,
            "password_configured": True,
            **token,
        }

    def logout(_client, _payload):
        # JWTs are stateless; the browser clears its stored token.
        return {"ok": True}

    return [
        ("GET", "/auth/status", status),
        ("POST", "/auth/status", status),
        ("POST", "/auth/login", login),
        ("POST", "/auth/admin/password", set_password),
        ("POST", "/auth/logout", logout),
    ]
