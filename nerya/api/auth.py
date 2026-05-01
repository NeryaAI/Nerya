"""Minimal request-auth layer for the local API server.
11-auth-api-tool-permissions.md.

The earlier local server exposed every route with ``Access-Control-Allow-Origin: *``
and no auth check. That was fine for a single-operator loopback dev box but
becomes a live foot-gun the moment the server is reachable off-host (e.g. via
``--host 0.0.0.0``) or any future gateway re-uses the same HTTP entrypoint.

This module introduces three auth modes, selected via ``runtime.auth.mode`` in
``nerya.yml`` or the ``NERYA_AUTH_MODE`` environment variable:

- ``local`` (default) — requests from ``127.0.0.1`` / ``::1`` are allowed
  without a token. Any remote request is rejected outright. This preserves
  current developer UX while preventing accidental remote exposure.
- ``token`` — every request must carry ``Authorization: Bearer <token>`` or
  ``X-Nerya-Token: <token>``. Tokens are compared against the configured
  token list (``runtime.auth.tokens`` or the ``NERYA_API_TOKEN`` env var).
- ``off`` — explicitly disable auth. Useful only for local smoke tests.

All outcomes (accept/reject) are appended to ``journals/security_events.jsonl``
with a redacted payload so operators have an audit trail even in dev mode.
"""

from __future__ import annotations

import hmac
import os
from dataclasses import dataclass
from typing import Any

from ..core import jsonl
from ..core.config import Config
from ..core.time import now_iso
from . import route_scopes


_LOCAL_HOSTS = frozenset({"127.0.0.1", "::1", "localhost"})


@dataclass
class AuthResult:
    """Outcome of an auth check.

    ``ok`` tells the HTTP layer whether to proceed. ``status`` is the
    HTTP status code to return when ``ok`` is False. ``actor`` is a
    stable short identifier for the caller — the skill runtime and
    journals use it as ``caller`` for traceability.

    ``scope`` is the legacy single-scope label that we still surface for
    backwards-compatible logging (e.g. ``"api:all"`` or ``"write:chat"``).
    ``scopes`` is the parsed grant set used by the route authorization
    matrix — see :mod:`nerya.api.route_scopes`.
    """

    ok: bool
    status: int = 200
    actor: str = "local:anonymous"
    scope: str = "local:all"
    reason: str = ""
    scopes: frozenset[str] = frozenset()


def _config_tokens(config: Config) -> dict[str, dict[str, Any]]:
    """Return ``{token: {actor, scope}}`` from config, merging env var.

    ``scope`` may be any value accepted by
    :func:`nerya.api.route_scopes.parse_scopes` — a single string, a
    comma/space separated string, or a list. For backwards compat the
    representative ``scope`` label remains the original string while the
    parsed set is stored under ``scopes``.
    """
    tokens: dict[str, dict[str, Any]] = {}
    configured = config.get("runtime.auth.tokens") or []
    if isinstance(configured, list):
        for entry in configured:
            if isinstance(entry, dict):
                t = str(entry.get("token") or "").strip()
                if not t:
                    continue
                raw_scope = entry.get("scope")
                tokens[t] = {
                    "actor": str(entry.get("actor") or "token:user"),
                    "scope": str(raw_scope) if raw_scope is not None else "api:all",
                    "scopes": route_scopes.parse_scopes(raw_scope) or frozenset({route_scopes.WILDCARD_SCOPE}),
                }
            elif isinstance(entry, str) and entry.strip():
                tokens[entry.strip()] = {
                    "actor": "token:user",
                    "scope": "api:all",
                    "scopes": frozenset({route_scopes.WILDCARD_SCOPE}),
                }
    env_token = os.environ.get("NERYA_API_TOKEN") or ""
    if env_token.strip():
        tokens.setdefault(env_token.strip(), {
            "actor": "token:env",
            "scope": "api:all",
            "scopes": frozenset({route_scopes.WILDCARD_SCOPE}),
        })
    return tokens


def _resolve_mode(config: Config) -> str:
    env_mode = (os.environ.get("NERYA_AUTH_MODE") or "").strip().lower()
    if env_mode in ("local", "token", "off"):
        return env_mode
    cfg_mode = str(config.get("runtime.auth.mode") or "local").strip().lower()
    return cfg_mode if cfg_mode in ("local", "token", "off") else "local"


def _extract_token(headers: dict[str, str]) -> str:
    """Extract a bearer token from standard or legacy header."""
    auth = headers.get("authorization") or headers.get("Authorization") or ""
    if auth.lower().startswith("bearer "):
        return auth[len("bearer "):].strip()
    alt = headers.get("x-nerya-token") or headers.get("X-Nerya-Token") or ""
    return alt.strip()


def _emit(config: Config, *, kind: str, **payload: Any) -> None:
    rec = {"kind": kind, "ts": now_iso(), **payload}
    try:
        jsonl.append(config.paths.journal("security_events"), rec)
    except Exception:
        pass


def check_request(
    config: Config,
    *,
    method: str,
    path: str,
    client_addr: str,
    headers: dict[str, str],
) -> AuthResult:
    """Return an :class:`AuthResult` describing whether the caller may
    proceed. The HTTP handler must honour ``ok``/``status``/``actor``."""
    mode = _resolve_mode(config)
    client_host = (client_addr or "").split(":")[0].lower()

    if path == "/health":
        return AuthResult(
            ok=True, actor="public:health", scope="read:runtime",
            scopes=frozenset({"read:runtime"}),
        )

    if mode == "off":
        _emit(
            config, kind="auth.accepted",
            actor="local:anonymous",
            reason="mode_off",
            method=method, path=path, client=client_host,
        )
        return AuthResult(
            ok=True, actor="local:anonymous", scope="api:all",
            scopes=frozenset({route_scopes.WILDCARD_SCOPE}),
        )

    token = _extract_token(headers)

    if mode == "token":
        tokens = _config_tokens(config)
        if not token:
            _emit(
                config, kind="auth.rejected",
                actor="unknown",
                reason="missing_token",
                method=method, path=path, client=client_host,
            )
            return AuthResult(
                ok=False, status=401, actor="unknown",
                reason="missing_token",
            )
        for known, meta in tokens.items():
            if hmac.compare_digest(known, token):
                _emit(
                    config, kind="auth.accepted",
                    actor=meta["actor"], scope=meta["scope"],
                    method=method, path=path, client=client_host,
                )
                return AuthResult(
                    ok=True, actor=meta["actor"], scope=meta["scope"],
                    scopes=meta.get("scopes") or frozenset(),
                )
        _emit(
            config, kind="auth.rejected",
            actor="unknown",
            reason="invalid_token",
            method=method, path=path, client=client_host,
        )
        return AuthResult(
            ok=False, status=403, actor="unknown",
            reason="invalid_token",
        )

    # mode == "local"
    if client_host in _LOCAL_HOSTS or client_host == "":
        _emit(
            config, kind="auth.accepted",
            actor="local:loopback", scope="api:all",
            method=method, path=path, client=client_host,
        )
        return AuthResult(
            ok=True, actor="local:loopback", scope="api:all",
            scopes=frozenset({route_scopes.WILDCARD_SCOPE}),
        )

    if token:
        tokens = _config_tokens(config)
        for known, meta in tokens.items():
            if hmac.compare_digest(known, token):
                _emit(
                    config, kind="auth.accepted",
                    actor=meta["actor"], scope=meta["scope"],
                    method=method, path=path, client=client_host,
                )
                return AuthResult(
                    ok=True, actor=meta["actor"], scope=meta["scope"],
                    scopes=meta.get("scopes") or frozenset(),
                )

    _emit(
        config, kind="auth.rejected",
        actor="unknown",
        reason="remote_without_token",
        method=method, path=path, client=client_host,
    )
    return AuthResult(
        ok=False, status=403, actor="unknown",
        reason="remote_without_token",
    )


def authorize_route(
    config: Config,
    auth: AuthResult,
    *,
    method: str,
    path: str,
    client_addr: str = "",
) -> AuthResult:
    """Return ``auth`` if its scope set covers the route, else 403.

    The HTTP layer must call this after ``check_request`` returned an
    ``AuthResult`` with ``ok=True``. Anonymous endpoints (e.g.
    ``/health``) and callers holding the ``api:all`` wildcard pass
    through unchanged. Anything else is checked against
    :func:`nerya.api.route_scopes.required_scope`. The decision (allow
    or deny) is journaled with kind ``permission.allowed`` /
    ``permission.denied`` so the audit trail stays in step with
    ``auth.accepted`` / ``auth.rejected``.
    """
    if not auth.ok:
        return auth

    ok, reason = route_scopes.authorize(auth.scopes, method, path)
    client_host = (client_addr or "").split(":")[0].lower()
    needed = route_scopes.required_scope(method, path)

    if ok:
        _emit(
            config, kind="permission.allowed",
            actor=auth.actor,
            scope=auth.scope,
            granted=sorted(auth.scopes),
            needed=needed,
            method=method, path=path, client=client_host,
        )
        return auth

    _emit(
        config, kind="permission.denied",
        actor=auth.actor,
        scope=auth.scope,
        granted=sorted(auth.scopes),
        needed=needed,
        reason=reason,
        method=method, path=path, client=client_host,
    )
    return AuthResult(
        ok=False,
        status=403,
        actor=auth.actor,
        scope=auth.scope,
        scopes=auth.scopes,
        reason=reason or "insufficient_scope",
    )
