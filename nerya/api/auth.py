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

import base64
import hashlib
import hmac
import json
import os
import secrets
import time
from dataclasses import dataclass
from typing import Any

from ..core import jsonl, yaml_io
from ..core.config import Config
from ..core.time import now_iso
from . import route_scopes


_LOCAL_HOSTS = frozenset({"127.0.0.1", "::1", "localhost"})
_PASSWORD_HASH_ALG = "pbkdf2_sha256"
_PASSWORD_HASH_ITERATIONS = 260_000
_JWT_ALG = "HS256"
_JWT_ISSUER = "nerya.local_api"
_JWT_DEFAULT_TTL_SECONDS = 24 * 60 * 60


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


def _normalise_host(value: str) -> str:
    host = (value or "").strip().lower()
    if not host:
        return ""
    # X-Forwarded-For may be a comma-separated chain.
    host = host.split(",", 1)[0].strip()
    if host.startswith("[") and "]" in host:
        return host[1:host.index("]")]
    if host.count(":") == 1:
        host = host.rsplit(":", 1)[0]
    return host


def _is_local_host(host: str) -> bool:
    host = _normalise_host(host)
    if host in _LOCAL_HOSTS or host == "":
        return True
    if host.startswith("127."):
        return True
    return False


def _effective_client_host(client_addr: str, headers: dict[str, str]) -> str:
    peer = _normalise_host(client_addr)
    if not _is_local_host(peer):
        return peer
    # When the local API sits behind the dashboard proxy or a reverse proxy,
    # the socket peer is loopback. Trust forwarded client headers only in
    # that loopback case so remote direct callers cannot spoof themselves
    # into the local trust lane.
    for key in ("x-forwarded-for", "X-Forwarded-For", "x-real-ip", "X-Real-IP"):
        forwarded = _normalise_host(headers.get(key) or "")
        if forwarded:
            return forwarded
    return peer


def _b64url_encode(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _b64url_decode(raw: str) -> bytes:
    padded = raw + "=" * (-len(raw) % 4)
    return base64.urlsafe_b64decode(padded.encode("ascii"))


def _set_dotted(doc: dict[str, Any], dotted: str, value: Any) -> None:
    cur = doc
    parts = dotted.split(".")
    for part in parts[:-1]:
        nxt = cur.get(part)
        if not isinstance(nxt, dict):
            nxt = {}
            cur[part] = nxt
        cur = nxt
    cur[parts[-1]] = value


def _persist_config_values(config: Config, values: dict[str, Any]) -> None:
    doc = yaml_io.load(config.paths.config, default={}) or {}
    if not isinstance(doc, dict):
        doc = {}
    for dotted, value in values.items():
        _set_dotted(doc, dotted, value)
        _set_dotted(config.data, dotted, value)
    yaml_io.dump(config.paths.config, doc)


def _password_hash_value(config: Config) -> str:
    return str(config.get("runtime.auth.admin_password_hash") or "").strip()


def has_admin_password(config: Config) -> bool:
    return bool(_password_hash_value(config))


def _hash_password(password: str, *, salt: str | None = None) -> str:
    salt = salt or secrets.token_urlsafe(18)
    digest = hashlib.pbkdf2_hmac(
        "sha256",
        password.encode("utf-8"),
        salt.encode("utf-8"),
        _PASSWORD_HASH_ITERATIONS,
    )
    return (
        f"{_PASSWORD_HASH_ALG}${_PASSWORD_HASH_ITERATIONS}"
        f"${salt}${_b64url_encode(digest)}"
    )


def verify_admin_password(config: Config, password: str) -> bool:
    encoded = _password_hash_value(config)
    if not encoded or not isinstance(password, str):
        return False
    try:
        alg, iterations_raw, salt, digest = encoded.split("$", 3)
        if alg != _PASSWORD_HASH_ALG:
            return False
        iterations = int(iterations_raw)
        candidate = hashlib.pbkdf2_hmac(
            "sha256",
            password.encode("utf-8"),
            salt.encode("utf-8"),
            iterations,
        )
        return hmac.compare_digest(_b64url_encode(candidate), digest)
    except Exception:
        return False


def _jwt_ttl_seconds(config: Config) -> int:
    env = (os.environ.get("NERYA_AUTH_JWT_TTL_SECONDS") or "").strip()
    raw = env or config.get("runtime.auth.jwt_ttl_seconds")
    try:
        ttl = int(raw)
    except Exception:
        ttl = _JWT_DEFAULT_TTL_SECONDS
    return max(300, min(ttl, 30 * 24 * 60 * 60))


def _jwt_secret(config: Config) -> str:
    return str(
        os.environ.get("NERYA_AUTH_JWT_SECRET")
        or config.get("runtime.auth.jwt_secret")
        or ""
    ).strip()


def _ensure_jwt_secret(config: Config) -> str:
    secret = _jwt_secret(config)
    if secret:
        return secret
    secret = secrets.token_urlsafe(48)
    _persist_config_values(config, {"runtime.auth.jwt_secret": secret})
    return secret


def set_admin_password(config: Config, password: str) -> None:
    password = password or ""
    if len(password) < 8:
        raise ValueError("password_too_short")
    values = {"runtime.auth.admin_password_hash": _hash_password(password)}
    if not _jwt_secret(config):
        values["runtime.auth.jwt_secret"] = secrets.token_urlsafe(48)
    _persist_config_values(config, values)


def issue_admin_jwt(config: Config, *, actor: str = "admin:password") -> dict[str, Any]:
    secret = _ensure_jwt_secret(config)
    now = int(time.time())
    ttl = _jwt_ttl_seconds(config)
    payload = {
        "iss": _JWT_ISSUER,
        "sub": actor,
        "actor": actor,
        "scope": route_scopes.WILDCARD_SCOPE,
        "scopes": [route_scopes.WILDCARD_SCOPE],
        "iat": now,
        "exp": now + ttl,
        "jti": secrets.token_urlsafe(12),
    }
    header = {"typ": "JWT", "alg": _JWT_ALG}
    signing_input = ".".join([
        _b64url_encode(json.dumps(header, separators=(",", ":")).encode("utf-8")),
        _b64url_encode(json.dumps(payload, separators=(",", ":")).encode("utf-8")),
    ])
    sig = hmac.new(
        secret.encode("utf-8"),
        signing_input.encode("ascii"),
        hashlib.sha256,
    ).digest()
    token = f"{signing_input}.{_b64url_encode(sig)}"
    return {
        "token": token,
        "token_type": "Bearer",
        "expires_at": payload["exp"],
        "expires_in": ttl,
        "actor": actor,
        "scope": route_scopes.WILDCARD_SCOPE,
    }


def _verify_jwt(config: Config, token: str) -> tuple[dict[str, Any] | None, str]:
    secret = _jwt_secret(config)
    if not secret:
        return None, "jwt_not_configured"
    parts = token.split(".")
    if len(parts) != 3:
        return None, "not_jwt"
    signing_input = ".".join(parts[:2])
    expected = hmac.new(
        secret.encode("utf-8"),
        signing_input.encode("ascii"),
        hashlib.sha256,
    ).digest()
    try:
        provided = _b64url_decode(parts[2])
    except Exception:
        return None, "invalid_token"
    if not hmac.compare_digest(expected, provided):
        return None, "invalid_token"
    try:
        header = json.loads(_b64url_decode(parts[0]).decode("utf-8"))
        payload = json.loads(_b64url_decode(parts[1]).decode("utf-8"))
    except Exception:
        return None, "invalid_token"
    if header.get("alg") != _JWT_ALG:
        return None, "invalid_token"
    if payload.get("iss") != _JWT_ISSUER:
        return None, "invalid_token"
    try:
        exp = int(payload.get("exp") or 0)
    except Exception:
        exp = 0
    if exp < int(time.time()):
        return None, "expired_token"
    scopes = route_scopes.parse_scopes(payload.get("scopes") or payload.get("scope"))
    if not scopes:
        return None, "invalid_token"
    actor = str(payload.get("actor") or payload.get("sub") or "admin:password")
    return {
        "actor": actor,
        "scope": str(payload.get("scope") or " ".join(sorted(scopes))),
        "scopes": scopes,
    }, ""


def _authenticate_token(config: Config, token: str) -> tuple[dict[str, Any] | None, str]:
    tokens = _config_tokens(config)
    for known, meta in tokens.items():
        if hmac.compare_digest(known, token):
            return meta, ""
    if "." in token:
        return _verify_jwt(config, token)
    return None, "invalid_token"


def admin_auth_status(config: Config) -> dict[str, Any]:
    return {
        "ok": True,
        "mode": _resolve_mode(config),
        "password_configured": has_admin_password(config),
        "jwt_configured": bool(_jwt_secret(config)),
        "jwt_ttl_seconds": _jwt_ttl_seconds(config),
        "static_token_configured": bool(_config_tokens(config)),
    }


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
    client_host = _effective_client_host(client_addr, headers)

    if path in route_scopes.ANONYMOUS_PATHS:
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
        meta, token_reason = _authenticate_token(config, token)
        if meta is not None:
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
            reason=token_reason or "invalid_token",
            method=method, path=path, client=client_host,
        )
        return AuthResult(
            ok=False,
            status=401 if token_reason == "expired_token" else 403,
            actor="unknown",
            reason=token_reason or "invalid_token",
        )

    # mode == "local"
    if _is_local_host(client_host):
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
        meta, token_reason = _authenticate_token(config, token)
        if meta is not None:
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
            reason=token_reason or "invalid_token",
            method=method, path=path, client=client_host,
        )
        return AuthResult(
            ok=False,
            status=401 if token_reason == "expired_token" else 403,
            actor="unknown",
            reason=token_reason or "invalid_token",
        )

    _emit(
        config, kind="auth.rejected",
        actor="unknown",
        reason="missing_token" if has_admin_password(config) else "remote_without_token",
        method=method, path=path, client=client_host,
    )
    return AuthResult(
        ok=False,
        status=401 if has_admin_password(config) else 403,
        actor="unknown",
        reason="missing_token" if has_admin_password(config) else "remote_without_token",
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
