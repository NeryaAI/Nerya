"""MCP OAuth authorization-code + S256 PKCE, using Nerya's admin password.

The SDK owns protocol validation. This provider owns consent and durable grants.
Only public PKCE clients are registered: no reusable client secrets to leak.
OAuth tokens are distinct from dashboard JWTs and stored only as SHA-256 digests.
"""
from __future__ import annotations

import hashlib
import html
import json
import secrets
import sqlite3
import time
from contextlib import contextmanager
from urllib.parse import parse_qs, urlsplit

from mcp.server.auth.provider import (
    AccessToken, AuthorizationCode, AuthorizationParams, AuthorizeError,
    RefreshToken, RegistrationError, TokenError, construct_redirect_uri,
)
from mcp.server.auth.routes import create_auth_routes, create_protected_resource_routes
from mcp.server.auth.settings import ClientRegistrationOptions, RevocationOptions
from mcp.shared.auth import OAuthClientInformationFull, OAuthToken
from pydantic import AnyHttpUrl
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, RedirectResponse
from starlette.routing import Route

from ..api.auth import has_admin_password, verify_admin_password
from ..core.config import load_config

SCOPE = "mcp"
COOKIE = "nerya_mcp_consent"


def digest(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


class AdminOAuthProvider:
    def __init__(self, config, origin: str):
        self.config = config
        self.origin = origin.rstrip("/")
        self.issuer = self.origin + "/mcp/oauth"
        self.resource = self.origin + "/mcp"
        self.path = config.paths.state / "mcp-oauth.sqlite3"
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if self.path.is_symlink():
            raise ValueError("OAuth store must not be a symlink")
        with self.db() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS records (
                    kind TEXT NOT NULL, key TEXT NOT NULL, body TEXT NOT NULL,
                    expires REAL NOT NULL, epoch TEXT NOT NULL, family TEXT NOT NULL DEFAULT '',
                    used INTEGER NOT NULL DEFAULT 0, PRIMARY KEY(kind,key));
                CREATE INDEX IF NOT EXISTS record_family ON records(family);
                CREATE TABLE IF NOT EXISTS rate_limits (key TEXT PRIMARY KEY, window INTEGER, count INTEGER);
            """)
        self.path.chmod(0o600)

    @contextmanager
    def db(self):
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

    def current_config(self):
        # Password changes and disabling MCP take effect without restarting a client.
        if self.config.paths.config.exists():
            return load_config(self.config.paths.root)
        return self.config

    def epoch(self):
        cfg = self.current_config()
        if cfg.get("mcp.enabled") is not True or not has_admin_password(cfg):
            return "disabled"
        return digest(str(cfg.get("runtime.auth.admin_password_hash")) + self.resource
                      + str(cfg.get("mcp.oauth_epoch", "")))

    def put(self, db, kind, key, body, expires, family=""):
        db.execute("DELETE FROM records WHERE expires < ?", (time.time(),))
        if db.execute("SELECT count(*) FROM records").fetchone()[0] >= 10000:
            raise ValueError("OAuth grant limit reached; revoke unused clients")
        db.execute("INSERT INTO records(kind,key,body,expires,epoch,family) VALUES(?,?,?,?,?,?)",
                   (kind, digest(key), json.dumps(body), expires, self.epoch(), family))

    def get(self, kind, key, *, consume=False):
        with self.db() as db:
            if consume:
                db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT * FROM records WHERE kind=? AND key=?",
                             (kind, digest(key))).fetchone()
            if not row or row["expires"] <= time.time():
                return None
            if kind != "client" and (row["epoch"] != self.epoch() or self.epoch() == "disabled"):
                return None
            if row["used"]:
                if kind == "refresh" and row["family"]:
                    db.execute("UPDATE records SET used=1 WHERE family=?", (row["family"],))
                return None
            if consume:
                db.execute("UPDATE records SET used=1 WHERE kind=? AND key=?", (kind, digest(key)))
            return json.loads(row["body"])

    def rate_limit(self, kind: str, maximum: int) -> bool:
        # Persistent global limits cannot be evaded with spoofed forwarding headers.
        window = int(time.time() // 60)
        with self.db() as db:
            db.execute("INSERT INTO rate_limits VALUES(?,?,1) ON CONFLICT(key) DO UPDATE SET "
                       "count=CASE WHEN window=excluded.window THEN count+1 ELSE 1 END, window=excluded.window",
                       (kind, window))
            return db.execute("SELECT count FROM rate_limits WHERE key=?", (kind,)).fetchone()[0] <= maximum

    async def get_client(self, client_id):
        data = self.get("client", client_id)
        return OAuthClientInformationFull.model_validate(data) if data else None

    async def register_client(self, client_info):
        if client_info.token_endpoint_auth_method != "none":
            raise RegistrationError("invalid_client_metadata", "Use token_endpoint_auth_method=none with S256 PKCE")
        if not client_info.redirect_uris or len(client_info.redirect_uris) > 8:
            raise RegistrationError("invalid_redirect_uri", "Register 1 to 8 exact redirect URIs")
        for uri in client_info.redirect_uris:
            parts = urlsplit(str(uri))
            if (parts.fragment or parts.username or parts.password or not parts.hostname
                    or not (parts.scheme == "https" or (parts.scheme == "http" and
                            parts.hostname in {"127.0.0.1", "localhost", "::1"}))):
                raise RegistrationError("invalid_redirect_uri", "HTTPS or loopback HTTP redirect required")
        if not self.rate_limit("register", 10):
            raise RegistrationError("invalid_client_metadata", "Registration rate limit reached")
        with self.db() as db:
            if db.execute("SELECT count(*) FROM records WHERE kind='client'").fetchone()[0] >= 256:
                raise RegistrationError("invalid_client_metadata", "Client limit reached; revoke unused clients")
            self.put(db, "client", client_info.client_id, client_info.model_dump(mode="json"),
                     time.time() + 10 * 365 * 86400)

    async def authorize(self, client, params: AuthorizationParams):
        if self.epoch() == "disabled":
            raise AuthorizeError("access_denied", "MCP or administrator password is not configured")
        if params.resource != self.resource:
            raise AuthorizeError("invalid_request", "resource must match the MCP protected resource")
        if str(params.redirect_uri) not in {str(uri) for uri in client.redirect_uris}:
            raise AuthorizeError("invalid_request", "redirect_uri must match exactly")
        if set(params.scopes or [SCOPE]) != {SCOPE}:
            raise AuthorizeError("invalid_scope", "Request the mcp scope")
        if not self.rate_limit("authorize", 30):
            raise AuthorizeError("temporarily_unavailable", "Try again later")
        ticket = secrets.token_urlsafe(32)
        with self.db() as db:
            self.put(db, "consent", ticket, {"client_id": client.client_id,
                     "client_name": client.client_name or client.client_id,
                     "params": params.model_dump(mode="json")}, time.time() + 600)
        return self.issuer + "/login?ticket=" + ticket

    async def login(self, request: Request):
        ticket = request.query_params.get("ticket", "")
        if request.method == "POST":
            form = await request.form()
            ticket = str(form.get("ticket", ""))
            nonce = str(form.get("csrf", ""))
            csrf = self.get("csrf", nonce, consume=True) if nonce else None
            if (not csrf or csrf.get("ticket") != ticket
                    or not secrets.compare_digest(request.cookies.get(COOKIE, ""), nonce)):
                return JSONResponse({"error": "invalid_consent"}, status_code=403)
            data = self.get("consent", ticket)
            if not data:
                return JSONResponse({"error": "expired_consent"}, status_code=400)
            if not self.rate_limit("login", 10):
                return JSONResponse({"error": "rate_limited"}, status_code=429,
                                    headers={"Retry-After": "60"})
            if not verify_admin_password(self.current_config(), str(form.get("password", ""))):
                return self.form(ticket, data, "管理员密码不正确 / Incorrect administrator password", status=401)
            data = self.get("consent", ticket, consume=True)
            if not data:
                return JSONResponse({"error": "expired_consent"}, status_code=400)
            params = AuthorizationParams.model_validate(data["params"])
            redirect = str(params.redirect_uri)
            if form.get("decision") != "allow":
                response = RedirectResponse(construct_redirect_uri(redirect, error="access_denied", state=params.state), 303)
            else:
                code = secrets.token_urlsafe(32)
                record = AuthorizationCode(code=code, client_id=data["client_id"],
                    scopes=params.scopes or [SCOPE], expires_at=time.time() + 120,
                    code_challenge=params.code_challenge, redirect_uri=params.redirect_uri,
                    redirect_uri_provided_explicitly=params.redirect_uri_provided_explicitly,
                    resource=self.resource, subject="admin:password")
                with self.db() as db:
                    self.put(db, "code", code, record.model_dump(mode="json", exclude={"code"}), record.expires_at)
                response = RedirectResponse(construct_redirect_uri(redirect, code=code, state=params.state), 303)
            response.delete_cookie(COOKIE, path="/mcp/oauth")
            response.headers["Cache-Control"] = "no-store"
            return response
        data = self.get("consent", ticket)
        if not data:
            return JSONResponse({"error": "expired_consent"}, status_code=400)
        return self.form(ticket, data)

    def form(self, ticket, data, error="", status=200):
        csrf = secrets.token_urlsafe(32)
        with self.db() as db:
            self.put(db, "csrf", csrf, {"ticket": ticket}, time.time() + 600)
        esc = html.escape
        response = HTMLResponse(f'''<!doctype html><html lang="zh"><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>Nerya MCP 授权</title>
<style>body{{font:16px system-ui;margin:0;background:#f6f7fa;color:#18202b}}main{{max-width:32rem;margin:8vh auto;padding:2rem}}label,input,button{{display:block}}input{{box-sizing:border-box;width:100%;padding:.8rem;margin:1rem 0}}button{{padding:.8rem 1rem;margin:.7rem 0}}p{{line-height:1.6;overflow-wrap:anywhere}}.error{{color:#a51c30}}</style>
<main><h1>Nerya MCP</h1><h2>授权外部客户端 / Authorize client</h2>
<p><strong>{esc(data['client_name'])}</strong></p><p>{esc(data['params']['redirect_uri'])}</p>
<p>使用 Nerya 管理员密码登录。此客户端将可以读取并调用当前开放的工具；开启写权限时可管理角色、Skill 和配置提案。密码不会交给客户端。</p>
<p>Sign in with your Nerya administrator password. This grants access to the configured MCP tools, including enabled management writes. Your password is not shared with the client.</p>
<p class="error" role="alert">{esc(error)}</p>
<form method="post" action="{esc(self.issuer)}/login"><input type="hidden" name="ticket" value="{esc(ticket)}"><input type="hidden" name="csrf" value="{csrf}">
<label for="password">管理员密码 / Administrator password</label><input id="password" name="password" type="password" autocomplete="current-password" required maxlength="1024">
<button name="decision" value="allow" type="submit">登录并授权 / Sign in and authorize</button>
<button name="decision" value="deny" type="submit">拒绝授权 / Deny access</button></form></main></html>''', status_code=status)
        response.set_cookie(COOKIE, csrf, max_age=600, path="/mcp/oauth", httponly=True,
                            secure=self.origin.startswith("https://"), samesite="lax")
        response.headers.update({"Cache-Control": "no-store", "Referrer-Policy": "no-referrer",
            "X-Frame-Options": "DENY", "Content-Security-Policy": "default-src 'none'; style-src 'unsafe-inline'; form-action 'self'; frame-ancestors 'none'; base-uri 'none'"})
        return response

    async def load_authorization_code(self, client, authorization_code):
        data = self.get("code", authorization_code)
        return AuthorizationCode(code=authorization_code, **data) if data else None

    def issue(self, client_id, scopes, *, family=None):
        access, refresh = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
        family = family or secrets.token_hex(16)
        common = {"client_id": client_id, "scopes": scopes, "resource": self.resource,
                  "subject": "admin:password", "family": family}
        with self.db() as db:
            # Rotation invalidates the previous access token too.
            db.execute("UPDATE records SET used=1 WHERE family=?", (family,))
            for kind, token, expiry in (("access", access, int(time.time()) + 3600),
                                         ("refresh", refresh, int(time.time()) + 30 * 86400)):
                self.put(db, kind, token, {**common, "expires_at": expiry}, expiry, family)
        return OAuthToken(access_token=access, token_type="Bearer", expires_in=3600,
                          refresh_token=refresh, scope=" ".join(scopes))

    async def exchange_authorization_code(self, client, authorization_code):
        data = self.get("code", authorization_code.code, consume=True)
        if not data or data["client_id"] != client.client_id:
            raise TokenError("invalid_grant", "Authorization code already used or expired")
        return self.issue(client.client_id, authorization_code.scopes)

    async def load_refresh_token(self, client, refresh_token):
        data = self.get("refresh", refresh_token)
        return RefreshToken(token=refresh_token, **data) if data else None

    async def exchange_refresh_token(self, client, refresh_token, scopes):
        data = self.get("refresh", refresh_token.token, consume=True)
        if not data or data["client_id"] != client.client_id:
            raise TokenError("invalid_grant", "Refresh token already used or expired")
        return self.issue(client.client_id, scopes, family=data["family"])

    async def load_access_token(self, token):
        data = self.get("access", token)
        return AccessToken(token=token, **data) if data and data.get("resource") == self.resource else None

    async def revoke_token(self, token):
        with self.db() as db:
            row = db.execute("SELECT family FROM records WHERE key=?", (digest(token.token),)).fetchone()
            if row:
                db.execute("UPDATE records SET used=1 WHERE family=?", (row["family"],))

    def routes(self):
        routes = create_auth_routes(self, AnyHttpUrl(self.issuer),
            client_registration_options=ClientRegistrationOptions(enabled=True, valid_scopes=[SCOPE], default_scopes=[SCOPE]),
            revocation_options=RevocationOptions(enabled=True))
        for route in routes:
            if route.path == "/.well-known/oauth-authorization-server":
                route.path = "/.well-known/oauth-authorization-server/mcp/oauth"
            else:
                route.path = "/mcp/oauth" + route.path
            # Rebuild the path matcher after remapping SDK handlers.
            from starlette.routing import compile_path
            route.path_regex, route.path_format, route.param_convertors = compile_path(route.path)
        routes += create_protected_resource_routes(AnyHttpUrl(self.resource), [AnyHttpUrl(self.issuer)], [SCOPE], "Nerya")
        routes.append(Route("/mcp/oauth/login", self.login, methods=["GET", "POST"]))
        # The SDK metadata omits public-client 'none'; advertise the actual contract.
        async def metadata(request):
            return JSONResponse({"issuer": self.issuer, "authorization_endpoint": self.issuer + "/authorize",
                "token_endpoint": self.issuer + "/token", "registration_endpoint": self.issuer + "/register",
                "revocation_endpoint": self.issuer + "/revoke", "scopes_supported": [SCOPE],
                "response_types_supported": ["code"], "grant_types_supported": ["authorization_code", "refresh_token"],
                "token_endpoint_auth_methods_supported": ["none"], "code_challenge_methods_supported": ["S256"]},
                headers={"Cache-Control": "no-store", "Access-Control-Allow-Origin": "*"})
        routes[0] = Route("/.well-known/oauth-authorization-server/mcp/oauth", metadata)
        routes.append(Route("/.well-known/oauth-authorization-server", metadata))
        return routes


class OAuthBoundary:
    def __init__(self, app, provider):
        self.app, self.provider = app, provider

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        p = self.provider
        if p.epoch() == "disabled":
            return await JSONResponse({"error": "mcp_disabled"}, status_code=404)(scope, receive, send)
        headers = scope.get("headers", [])
        if scope["path"] == "/mcp":
            values = [v for k, v in headers if k.lower() == b"authorization"]
            raw = values[0].decode("latin-1") if len(values) == 1 else ""
            token = await p.load_access_token(raw[7:]) if raw.startswith("Bearer ") else None
            if token is None or SCOPE not in token.scopes:
                response = JSONResponse({"error": "unauthorized"}, status_code=401,
                    headers={"WWW-Authenticate": f'Bearer resource_metadata="{p.origin}/.well-known/oauth-protected-resource/mcp", scope="mcp"', "Cache-Control": "no-store"})
                return await response(scope, receive, send)
        if scope["method"] == "POST" and scope["path"].startswith("/mcp/oauth/"):
            body = b""
            while True:
                message = await receive()
                if message["type"] == "http.disconnect":
                    return
                body += message.get("body", b"")
                if len(body) > 65536:
                    return await JSONResponse({"error": "request_too_large"}, status_code=413)(scope, receive, send)
                if not message.get("more_body"):
                    break
            if scope["path"].endswith("/token"):
                form = parse_qs(body.decode("utf-8", errors="replace"))
                if form.get("resource") != [p.resource]:
                    return await JSONResponse({"error": "invalid_target"}, status_code=400)(scope, receive, send)
            sent = False
            original_receive = receive
            async def replay():
                nonlocal sent
                if not sent:
                    sent = True
                    return {"type": "http.request", "body": body, "more_body": False}
                return await original_receive()
            receive = replay
        await self.app(scope, receive, send)
