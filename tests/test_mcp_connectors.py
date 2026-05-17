"""Phase E — MCP connector + transport tests.

Covers the config loader, OAuth helpers, both transports (with
in-process fakes), the bootstrap flow, and the CLI subcommands. All
tests are offline — no real network is touched.

Live external probes against the open-tier servers (sec_edgar,
yahoo_finance, coingecko) are split into a separate
``test_mcp_connectors_live.py`` skipped by default; this file is
expected to PASS in any developer environment, including CI without
internet.
"""

from __future__ import annotations

import io
import json
import subprocess
import sys
import threading
import time
import urllib.error
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

import pytest

from nerya.core.paths import WorkspacePaths
from nerya.mcp.connectors import (
    AuthConfig,
    BootstrapResult,
    ConnectorConfigError,
    DEFAULT_MCP_SERVERS_YML,
    HttpTransportConfig,
    MCPServerConfig,
    MCPServersConfig,
    StdioTransportConfig,
    VaultRef,
    bootstrap_mcp_connectors,
    build_adapter_for_server,
    ensure_mcp_servers_config,
    load_mcp_servers_config,
)
from nerya.mcp.connectors.bootstrap import VaultResolver
from nerya.mcp.session_adapter import MCPSessionExpiredError
from nerya.mcp.transports import (
    HttpMCPClient,
    HttpTransportError,
    OAuthCredentials,
    OAuthTokenCache,
    OAuthTokenError,
    StdioMCPClient,
    StdioTransportError,
    fetch_client_credentials_token,
)
from nerya.mcp.transports.oauth import resolve_token_for


pytestmark = pytest.mark.smoke


# ---------------------------------------------------------------------------
# Tiny helpers — fake HTTP opener, fake stdio subprocess, fake vault
# ---------------------------------------------------------------------------


class _FakeHttpResponse:
    def __init__(self, body: bytes, *, status: int = 200, headers: Optional[dict[str, str]] = None):
        self._body = body
        self.status = status
        self.headers = headers or {"Content-Type": "application/json"}

    def read(self) -> bytes:
        return self._body

    def __enter__(self) -> "_FakeHttpResponse":
        return self

    def __exit__(self, *exc: Any) -> None:
        return None


class _FakeHttpOpener:
    """Drop-in replacement for ``urllib.request.build_opener()``.

    Callers register handlers per request URL; each handler returns
    either a ``_FakeHttpResponse`` (success) or raises an exception
    (network/HTTP error).
    """

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []
        self._handlers: dict[str, list[Any]] = {}

    def register(self, url: str, *responses: Any) -> None:
        """Push response(s) to be returned on subsequent calls to ``url``."""
        self._handlers.setdefault(url, []).extend(responses)

    def open(self, request: Any, timeout: float = 30.0) -> _FakeHttpResponse:  # noqa: A003
        url = getattr(request, "full_url", None) or getattr(request, "_full_url", None) or request.get_full_url()
        body = request.data or b""
        try:
            payload = json.loads(body.decode("utf-8")) if body else None
        except (UnicodeDecodeError, json.JSONDecodeError):
            payload = body
        self.calls.append({"url": url, "headers": dict(request.headers or {}), "payload": payload})

        queue = self._handlers.get(url) or []
        if not queue:
            raise AssertionError(f"FakeOpener got unexpected call to {url!r}")
        nxt = queue.pop(0)
        if isinstance(nxt, Exception):
            raise nxt
        return nxt


@dataclass
class _FakeStdioProc:
    """Subprocess look-alike — JSON-RPC frames over BytesIO buffers."""

    stdin: io.BytesIO = field(default_factory=io.BytesIO)
    stdout: Any = field(default=None)
    stderr: io.BytesIO = field(default_factory=io.BytesIO)
    returncode: Optional[int] = None
    _replies: list[dict[str, Any]] = field(default_factory=list)
    _terminated: bool = False

    def __post_init__(self) -> None:
        # Pre-load replies into the stdout pipe so the transport's
        # readline() returns them in order.
        body = b"".join(
            (json.dumps(r) + "\n").encode("utf-8") for r in self._replies
        )
        self.stdout = io.BytesIO(body)

    def poll(self) -> Optional[int]:
        return self.returncode

    def terminate(self) -> None:
        self._terminated = True
        self.returncode = 0

    def kill(self) -> None:
        self._terminated = True
        self.returncode = -9

    def wait(self, timeout: Optional[float] = None) -> int:
        return 0


def _make_fake_spawn(*replies: dict[str, Any]) -> Any:
    """Return a ``_spawn`` callable that yields a fresh fake proc each call."""

    def _spawn(command: list[str], env: dict[str, str], cwd: Optional[str]) -> _FakeStdioProc:
        return _FakeStdioProc(_replies=list(replies))

    return _spawn


@dataclass
class _FakeVault:
    """Stand-in for VaultResolver in tests (no real SecretVault)."""

    secrets: dict[str, str] = field(default_factory=dict)

    def resolve(self, ref: VaultRef, *, scope: str = "mcp.read") -> str:
        if ref.name not in self.secrets:
            raise ConnectorConfigError(f"fake vault: unknown ref {ref.as_str()}")
        return self.secrets[ref.name]


# ---------------------------------------------------------------------------
# Config loader
# ---------------------------------------------------------------------------


class TestVaultRef:
    def test_parse_happy_path(self) -> None:
        ref = VaultRef.parse("vault://my_token")
        assert ref.name == "my_token"
        assert ref.as_str() == "vault://my_token"

    def test_rejects_non_vault_prefix(self) -> None:
        with pytest.raises(ConnectorConfigError):
            VaultRef.parse("my_token")

    def test_rejects_empty_name(self) -> None:
        with pytest.raises(ConnectorConfigError):
            VaultRef.parse("vault://")


class TestAuthConfig:
    def test_none_default(self) -> None:
        auth = AuthConfig.from_raw("srv", None)
        assert auth.kind == "none"
        assert auth.token_ref is None

    def test_bearer_static_requires_token_ref(self) -> None:
        with pytest.raises(ConnectorConfigError, match="token_ref"):
            AuthConfig.from_raw("srv", {"kind": "bearer_static"})

    def test_bearer_static_happy_path(self) -> None:
        auth = AuthConfig.from_raw(
            "srv", {"kind": "bearer_static", "token_ref": "vault://my_tok"}
        )
        assert auth.kind == "bearer_static"
        assert auth.token_ref is not None and auth.token_ref.name == "my_tok"

    def test_oauth_requires_client_id_or_ref(self) -> None:
        with pytest.raises(ConnectorConfigError, match="client_id"):
            AuthConfig.from_raw(
                "srv",
                {
                    "kind": "oauth_client_credentials",
                    "client_secret_ref": "vault://cs",
                    "token_url": "https://x/token",
                },
            )

    def test_oauth_requires_client_secret_ref(self) -> None:
        with pytest.raises(ConnectorConfigError, match="client_secret_ref"):
            AuthConfig.from_raw(
                "srv",
                {
                    "kind": "oauth_client_credentials",
                    "client_id": "abc",
                    "token_url": "https://x/token",
                },
            )

    def test_oauth_requires_token_url(self) -> None:
        with pytest.raises(ConnectorConfigError, match="token_url"):
            AuthConfig.from_raw(
                "srv",
                {
                    "kind": "oauth_client_credentials",
                    "client_id": "abc",
                    "client_secret_ref": "vault://cs",
                },
            )

    def test_unknown_kind_rejected(self) -> None:
        with pytest.raises(ConnectorConfigError, match="auth.kind"):
            AuthConfig.from_raw("srv", {"kind": "magic_unicorn"})


class TestServerConfig:
    def test_http_happy_path(self) -> None:
        cfg = MCPServerConfig.from_raw(
            {
                "id": "alpha_vantage",
                "enabled": True,
                "transport": {
                    "kind": "http",
                    "url": "https://mcp.alphavantage.co/mcp",
                },
                "auth": {"kind": "bearer_static", "token_ref": "vault://av_key"},
            }
        )
        assert cfg.id == "alpha_vantage"
        assert cfg.namespace == "alpha_vantage"  # defaults to id
        assert isinstance(cfg.transport, HttpTransportConfig)
        assert cfg.transport.url == "https://mcp.alphavantage.co/mcp"
        assert cfg.auth.kind == "bearer_static"

    def test_stdio_happy_path(self) -> None:
        cfg = MCPServerConfig.from_raw(
            {
                "id": "fred",
                "enabled": False,
                "transport": {
                    "kind": "stdio",
                    "command": ["uvx", "fred-mcp-server"],
                    "env_refs": {"FRED_API_KEY": "vault://mcp_fred_api_key"},
                },
            }
        )
        assert isinstance(cfg.transport, StdioTransportConfig)
        assert cfg.transport.command == ("uvx", "fred-mcp-server")
        assert cfg.transport.env_refs["FRED_API_KEY"].name == "mcp_fred_api_key"
        assert cfg.auth.kind == "none"

    def test_stdio_plain_env_field(self) -> None:
        # New since seed-swap: plain env values are supported for non-secret
        # identifiers like SEC_EDGAR_USER_AGENT (SEC fair-use policy header).
        cfg = MCPServerConfig.from_raw(
            {
                "id": "sec_edgar",
                "transport": {
                    "kind": "stdio",
                    "command": ["uvx", "sec-edgar-mcp"],
                    "env": {"SEC_EDGAR_USER_AGENT": "Nerya MCP Agent (mcp-bridge@nerya.local)"},
                },
            }
        )
        assert isinstance(cfg.transport, StdioTransportConfig)
        assert cfg.transport.env == {
            "SEC_EDGAR_USER_AGENT": "Nerya MCP Agent (mcp-bridge@nerya.local)"
        }
        assert cfg.transport.env_refs == {}

    def test_stdio_env_must_be_mapping(self) -> None:
        with pytest.raises(ConnectorConfigError, match="env must be a mapping"):
            MCPServerConfig.from_raw(
                {
                    "id": "x",
                    "transport": {
                        "kind": "stdio",
                        "command": ["x"],
                        "env": "not-a-dict",
                    },
                }
            )

    def test_stdio_env_value_must_be_scalar(self) -> None:
        with pytest.raises(ConnectorConfigError, match=r"env\['nested'\] must be a scalar"):
            MCPServerConfig.from_raw(
                {
                    "id": "x",
                    "transport": {
                        "kind": "stdio",
                        "command": ["x"],
                        "env": {"nested": {"oh": "no"}},
                    },
                }
            )

    def test_stdio_env_and_env_refs_coexist(self) -> None:
        # Both fields populate the resulting transport without collision.
        cfg = MCPServerConfig.from_raw(
            {
                "id": "x",
                "transport": {
                    "kind": "stdio",
                    "command": ["x"],
                    "env": {"PUBLIC_ID": "demo"},
                    "env_refs": {"SECRET_TOKEN": "vault://mcp_x_token"},
                },
            }
        )
        assert isinstance(cfg.transport, StdioTransportConfig)
        assert cfg.transport.env == {"PUBLIC_ID": "demo"}
        assert "SECRET_TOKEN" in cfg.transport.env_refs

    def test_invalid_id_rejected(self) -> None:
        with pytest.raises(ConnectorConfigError, match="invalid 'id'"):
            MCPServerConfig.from_raw({"id": "has space", "transport": {"kind": "http", "url": "https://x"}})

    def test_unknown_transport_kind(self) -> None:
        with pytest.raises(ConnectorConfigError, match="transport.kind"):
            MCPServerConfig.from_raw({"id": "x", "transport": {"kind": "magic"}})

    def test_http_without_url(self) -> None:
        with pytest.raises(ConnectorConfigError, match="requires url"):
            MCPServerConfig.from_raw({"id": "x", "transport": {"kind": "http"}})

    def test_stdio_without_command(self) -> None:
        with pytest.raises(ConnectorConfigError, match="non-empty command"):
            MCPServerConfig.from_raw({"id": "x", "transport": {"kind": "stdio", "command": []}})

    def test_namespace_override(self) -> None:
        cfg = MCPServerConfig.from_raw(
            {
                "id": "alpha_vantage",
                "namespace": "av",
                "transport": {"kind": "http", "url": "https://x"},
            }
        )
        assert cfg.namespace == "av"


class TestMCPServersConfigLoader:
    def test_default_seed_is_well_formed(self, tmp_path: Path) -> None:
        target = tmp_path / "connectors" / "mcp_servers.yml"
        ensure_mcp_servers_config(target)
        cfg = load_mcp_servers_config(target)

        assert cfg.version == 1
        assert len(cfg.servers) == 17

        ids = [s.id for s in cfg.servers]
        # The 3 zero-key open-tier servers are enabled by default per
        # USER E-1 + E-5 decisions.
        assert sorted(s.id for s in cfg.enabled_servers()) == [
            "coingecko", "sec_edgar", "yahoo_finance"
        ]
        # Spot-check that the catalogue covers every paid upstream
        # listed in financial-services/.mcp.json.
        for paid in ("daloopa", "morningstar", "factset", "sp_global", "moodys",
                     "lseg", "pitchbook", "chronograph", "aiera", "mtnewswire"):
            assert paid in ids, f"paid upstream {paid} missing from catalogue"
        # And that every PAID one has a vault token_ref (they're
        # disabled-by-default with the operator-fillable secret slot).
        for sid in ("daloopa", "morningstar", "factset", "moodys", "lseg",
                    "chronograph", "mtnewswire"):
            srv = cfg.by_id(sid)
            assert srv is not None
            assert srv.auth.kind == "bearer_static"
            assert srv.auth.token_ref is not None

    def test_seed_idempotent(self, tmp_path: Path) -> None:
        target = tmp_path / "connectors" / "mcp_servers.yml"
        first = ensure_mcp_servers_config(target)
        second = ensure_mcp_servers_config(target)
        assert first is True
        assert second is False  # second call must NOT overwrite

    def test_missing_file_returns_empty(self, tmp_path: Path) -> None:
        cfg = load_mcp_servers_config(tmp_path / "nope.yml")
        assert cfg.servers == []

    def test_duplicate_ids_rejected(self, tmp_path: Path) -> None:
        target = tmp_path / "dup.yml"
        target.write_text(
            "version: 1\nservers:\n"
            "  - id: a\n    transport: { kind: http, url: https://x }\n"
            "  - id: a\n    transport: { kind: http, url: https://y }\n",
            encoding="utf-8",
        )
        with pytest.raises(ConnectorConfigError, match="duplicate server id"):
            load_mcp_servers_config(target)

    def test_unsupported_version_rejected(self, tmp_path: Path) -> None:
        target = tmp_path / "v2.yml"
        target.write_text("version: 2\nservers: []\n", encoding="utf-8")
        with pytest.raises(ConnectorConfigError, match="unsupported config version"):
            load_mcp_servers_config(target)


# ---------------------------------------------------------------------------
# OAuth
# ---------------------------------------------------------------------------


class TestOAuth:
    def test_credentials_cache_key_distinguishes_scope(self) -> None:
        a = OAuthCredentials(token_url="https://t", client_id="c", client_secret="s")
        b = OAuthCredentials(token_url="https://t", client_id="c", client_secret="s", scope="read:x")
        assert a.cache_key() != b.cache_key()

    def test_fetch_client_credentials_happy_path(self) -> None:
        opener = _FakeHttpOpener()
        opener.register(
            "https://example.com/oauth/token",
            _FakeHttpResponse(json.dumps({
                "access_token": "tok-abc", "token_type": "Bearer",
                "expires_in": 3600, "scope": "read",
            }).encode("utf-8")),
        )
        envelope = fetch_client_credentials_token(
            OAuthCredentials(
                token_url="https://example.com/oauth/token",
                client_id="cid", client_secret="csec", scope="read",
            ),
            _opener=opener,
        )
        assert envelope["access_token"] == "tok-abc"
        # Verify the URL + form fields landed in the request body.
        # urllib sends form data as ``application/x-www-form-urlencoded``
        # bytes; the fake opener captures them verbatim because
        # they are not JSON-decodable.
        assert opener.calls[0]["url"] == "https://example.com/oauth/token"
        body = opener.calls[0]["payload"]
        assert isinstance(body, bytes)
        decoded = body.decode("ascii")
        assert "grant_type=client_credentials" in decoded
        assert "client_id=cid" in decoded
        assert "client_secret=csec" in decoded
        assert "scope=read" in decoded

    def test_fetch_raises_on_missing_access_token(self) -> None:
        opener = _FakeHttpOpener()
        opener.register(
            "https://x/token",
            _FakeHttpResponse(b'{"foo": "bar"}'),
        )
        with pytest.raises(OAuthTokenError, match="access_token"):
            fetch_client_credentials_token(
                OAuthCredentials(token_url="https://x/token", client_id="c", client_secret="s"),
                _opener=opener,
            )

    def test_fetch_raises_on_non_json(self) -> None:
        opener = _FakeHttpOpener()
        opener.register("https://x/t", _FakeHttpResponse(b"<html>nope</html>"))
        with pytest.raises(OAuthTokenError, match="not JSON"):
            fetch_client_credentials_token(
                OAuthCredentials(token_url="https://x/t", client_id="c", client_secret="s"),
                _opener=opener,
            )

    def test_token_cache_round_trip(self, tmp_path: Path) -> None:
        cache = OAuthTokenCache(cache_path=tmp_path / "cache.json")
        cache.put("key1", access_token="aaa", token_type="Bearer", expires_in=3600)
        # Fresh cache instance reading the same file should see it.
        cache2 = OAuthTokenCache(cache_path=tmp_path / "cache.json")
        hit = cache2.get("key1")
        assert hit is not None and hit.access_token == "aaa"

    def test_token_cache_expiry(self, tmp_path: Path) -> None:
        cache = OAuthTokenCache(cache_path=tmp_path / "cache.json")
        # expires_in is clamped to >= 60s by put(), but is_expired uses
        # 30s slack — so a 60s token is "expired" at t+30s. We can verify
        # the slack behavior by hand-rolling a _CachedToken.
        from nerya.mcp.transports.oauth import _CachedToken

        cached = _CachedToken(access_token="x", token_type="Bearer", expires_at=time.time() + 10)
        assert cached.is_expired(slack_seconds=30)

    def test_resolve_token_caches_first_mint(self, tmp_path: Path) -> None:
        cache = OAuthTokenCache(cache_path=tmp_path / "c.json")
        opener = _FakeHttpOpener()
        opener.register(
            "https://x/t",
            _FakeHttpResponse(json.dumps({
                "access_token": "tok1", "token_type": "Bearer", "expires_in": 3600,
            }).encode("utf-8")),
        )
        creds = OAuthCredentials(token_url="https://x/t", client_id="c", client_secret="s")

        # Patch the module-level urlopen in oauth via the _opener kwarg.
        # We need to monkey-patch the helper since resolve_token_for
        # internally calls fetch_client_credentials_token without an opener.
        import nerya.mcp.transports.oauth as oauth_mod
        original = oauth_mod.fetch_client_credentials_token
        oauth_mod.fetch_client_credentials_token = lambda c, **kw: original(c, _opener=opener, **kw)
        try:
            tok = resolve_token_for(creds, cache=cache)
            assert tok == "tok1"
            # Second call returns from cache without making a network call.
            tok2 = resolve_token_for(creds, cache=cache)
            assert tok2 == "tok1"
            assert len(opener.calls) == 1
        finally:
            oauth_mod.fetch_client_credentials_token = original


# ---------------------------------------------------------------------------
# HTTP transport
# ---------------------------------------------------------------------------


class TestHttpTransport:
    def test_init_validates_auth(self) -> None:
        with pytest.raises(ValueError, match="bearer_static"):
            HttpMCPClient(server_id="x", url="https://x", auth_kind="bearer_static")
        with pytest.raises(ValueError, match="oauth_client_credentials"):
            HttpMCPClient(server_id="x", url="https://x", auth_kind="oauth_client_credentials")
        with pytest.raises(ValueError, match="unknown auth_kind"):
            HttpMCPClient(server_id="x", url="https://x", auth_kind="bogus")

    def test_list_tools_round_trip(self) -> None:
        opener = _FakeHttpOpener()
        opener.register(
            "https://x/mcp",
            _FakeHttpResponse(json.dumps({
                "jsonrpc": "2.0", "id": "x", "result": {
                    "tools": [
                        {"name": "get_filings", "description": "SEC filings",
                         "inputSchema": {}, "annotations": {"readOnlyHint": True}}
                    ]
                }
            }).encode("utf-8")),
        )
        client = HttpMCPClient(server_id="x", url="https://x/mcp", auth_kind="none", _opener=opener)
        tools = client.list_tools()
        assert len(tools) == 1
        assert tools[0]["name"] == "get_filings"
        # No Authorization header was set (auth_kind=none).
        assert "Authorization" not in opener.calls[0]["headers"]

    def test_bearer_static_sets_authorization(self) -> None:
        opener = _FakeHttpOpener()
        opener.register(
            "https://x/mcp",
            _FakeHttpResponse(b'{"jsonrpc":"2.0","id":"x","result":{"tools":[]}}'),
        )
        client = HttpMCPClient(
            server_id="x", url="https://x/mcp", auth_kind="bearer_static",
            static_bearer="tok-static", _opener=opener,
        )
        client.list_tools()
        assert opener.calls[0]["headers"].get("Authorization") == "Bearer tok-static"

    def test_call_tool_returns_result(self) -> None:
        opener = _FakeHttpOpener()
        opener.register(
            "https://x/mcp",
            _FakeHttpResponse(json.dumps({
                "jsonrpc": "2.0", "id": "x",
                "result": {"content": [{"type": "text", "text": "ok"}], "isError": False}
            }).encode("utf-8")),
        )
        client = HttpMCPClient(server_id="x", url="https://x/mcp", auth_kind="none", _opener=opener)
        result = client.call_tool("get_filings", {"ticker": "AAPL"})
        assert result["content"][0]["text"] == "ok"

    def test_session_expired_on_401(self) -> None:
        opener = _FakeHttpOpener()
        opener.register(
            "https://x/mcp",
            urllib.error.HTTPError("https://x/mcp", 401, "unauth", {}, None),
        )
        client = HttpMCPClient(server_id="x", url="https://x/mcp", auth_kind="none", _opener=opener)
        with pytest.raises(MCPSessionExpiredError):
            client.list_tools()

    def test_jsonrpc_session_error_raises_expired(self) -> None:
        opener = _FakeHttpOpener()
        opener.register(
            "https://x/mcp",
            _FakeHttpResponse(json.dumps({
                "jsonrpc": "2.0", "id": "x",
                "error": {"code": -32001, "message": "session expired"}
            }).encode("utf-8")),
        )
        client = HttpMCPClient(server_id="x", url="https://x/mcp", auth_kind="none", _opener=opener)
        with pytest.raises(MCPSessionExpiredError):
            client.list_tools()

    def test_resources_list_tolerates_missing(self) -> None:
        opener = _FakeHttpOpener()
        opener.register(
            "https://x/mcp",
            urllib.error.HTTPError("https://x/mcp", 405, "method not allowed", {}, None),
        )
        client = HttpMCPClient(server_id="x", url="https://x/mcp", auth_kind="none", _opener=opener)
        # Should NOT raise — servers without resources/list return empty.
        assert client.list_resources() == []

    # ------------------------------------------------------------------
    # Streamable HTTP support (SSE-framed responses + Mcp-Session-Id).
    # Added when CoinGecko was promoted to a default-on zero-key server
    # — its endpoint speaks the modern MCP Streamable HTTP transport.
    # ------------------------------------------------------------------

    def test_sse_response_parses_into_envelope(self) -> None:
        opener = _FakeHttpOpener()
        sse_body = (
            b"event: message\n"
            b"data: {\"jsonrpc\":\"2.0\",\"id\":\"x\",\"result\":{\"tools\":["
            b"{\"name\":\"price\",\"description\":\"crypto price\",\"inputSchema\":{}}"
            b"]}}\n\n"
        )
        opener.register(
            "https://x/mcp",
            _FakeHttpResponse(
                sse_body,
                headers={"Content-Type": "text/event-stream"},
            ),
        )
        client = HttpMCPClient(server_id="x", url="https://x/mcp", auth_kind="none", _opener=opener)
        tools = client.list_tools()
        assert tools[0]["name"] == "price"

    def test_sse_multi_frame_picks_envelope_with_result(self) -> None:
        # Server emits a progress notification before the actual result —
        # the parser must skip the notif and pick the result frame.
        opener = _FakeHttpOpener()
        sse_body = (
            b": ping\n\n"  # SSE comment frame
            b"event: progress\n"
            b"data: {\"jsonrpc\":\"2.0\",\"method\":\"notifications/progress\","
            b"\"params\":{\"progress\":0.5}}\n\n"
            b"event: message\n"
            b"data: {\"jsonrpc\":\"2.0\",\"id\":\"x\",\"result\":{\"tools\":[]}}\n\n"
        )
        opener.register(
            "https://x/mcp",
            _FakeHttpResponse(sse_body, headers={"Content-Type": "text/event-stream"}),
        )
        client = HttpMCPClient(server_id="x", url="https://x/mcp", auth_kind="none", _opener=opener)
        assert client.list_tools() == []

    def test_session_id_captured_from_response_header(self) -> None:
        opener = _FakeHttpOpener()
        opener.register(
            "https://x/mcp",
            _FakeHttpResponse(
                b'{"jsonrpc":"2.0","id":"x","result":{"tools":[]}}',
                headers={
                    "Content-Type": "application/json",
                    "Mcp-Session-Id": "sess-abc-123",
                },
            ),
        )
        client = HttpMCPClient(server_id="x", url="https://x/mcp", auth_kind="none", _opener=opener)
        client.list_tools()
        assert client._session_id == "sess-abc-123"

    def test_session_id_replayed_on_subsequent_request(self) -> None:
        opener = _FakeHttpOpener()
        # First call delivers the session id; second call must echo it.
        opener.register(
            "https://x/mcp",
            _FakeHttpResponse(
                b'{"jsonrpc":"2.0","id":"x","result":{"tools":[]}}',
                headers={
                    "Content-Type": "application/json",
                    "Mcp-Session-Id": "sess-xyz",
                },
            ),
            _FakeHttpResponse(b'{"jsonrpc":"2.0","id":"y","result":{"resources":[]}}'),
        )
        client = HttpMCPClient(server_id="x", url="https://x/mcp", auth_kind="none", _opener=opener)
        client.list_tools()
        client.list_resources()
        # urllib normalises Request header keys (Mcp-Session-Id stored
        # as 'Mcp-session-id'). HTTP headers are case-insensitive — match
        # any key whose lowercase form matches.
        def _ci_get(headers: dict[str, str], key: str) -> Optional[str]:
            for k, v in headers.items():
                if k.lower() == key.lower():
                    return v
            return None

        # First call: no session header (we hadn't received one yet).
        first_headers = opener.calls[0]["headers"]
        assert _ci_get(first_headers, "Mcp-Session-Id") is None
        # Second call: session header MUST be echoed.
        second_headers = opener.calls[1]["headers"]
        assert _ci_get(second_headers, "Mcp-Session-Id") == "sess-xyz"

    def test_reconnect_clears_session_id(self) -> None:
        opener = _FakeHttpOpener()
        opener.register(
            "https://x/mcp",
            _FakeHttpResponse(
                b'{"jsonrpc":"2.0","id":"x","result":{"tools":[]}}',
                headers={
                    "Content-Type": "application/json",
                    "Mcp-Session-Id": "sess-old",
                },
            ),
        )
        client = HttpMCPClient(server_id="x", url="https://x/mcp", auth_kind="none", _opener=opener)
        client.list_tools()
        assert client._session_id == "sess-old"
        client.reconnect()
        assert client._session_id is None

    # ------------------------------------------------------------------
    # auto_initialize handshake (Streamable HTTP servers like CoinGecko)
    # ------------------------------------------------------------------

    def test_auto_initialize_runs_initialize_then_tools_list(self) -> None:
        # With auto_initialize=True, the very first user-driven call
        # (list_tools here) MUST be preceded by an initialize request +
        # a notifications/initialized notification. So the opener sees
        # 3 calls total in this order:
        #   1) initialize (request, response carries session id)
        #   2) notifications/initialized (notification, server returns 202)
        #   3) tools/list (request, replays the session id header)
        opener = _FakeHttpOpener()
        opener.register(
            "https://x/mcp",
            _FakeHttpResponse(
                b'{"jsonrpc":"2.0","id":"x","result":{"protocolVersion":"2024-11-05"}}',
                headers={
                    "Content-Type": "application/json",
                    "Mcp-Session-Id": "sess-init-42",
                },
            ),
            urllib.error.HTTPError("https://x/mcp", 202, "Accepted", {}, None),
            _FakeHttpResponse(b'{"jsonrpc":"2.0","id":"y","result":{"tools":[]}}'),
        )
        client = HttpMCPClient(
            server_id="x",
            url="https://x/mcp",
            auth_kind="none",
            auto_initialize=True,
            _opener=opener,
        )
        assert client.list_tools() == []

        assert len(opener.calls) == 3
        # Call 1: initialize
        assert opener.calls[0]["payload"]["method"] == "initialize"
        # Call 2: notification (no id field)
        assert opener.calls[1]["payload"]["method"] == "notifications/initialized"
        assert "id" not in opener.calls[1]["payload"]
        # Call 3: tools/list with session-id replayed (case-insensitive match)
        assert opener.calls[2]["payload"]["method"] == "tools/list"
        sid_header = next(
            (v for k, v in opener.calls[2]["headers"].items() if k.lower() == "mcp-session-id"),
            None,
        )
        assert sid_header == "sess-init-42"

    def test_no_auto_initialize_skips_handshake(self) -> None:
        # Default behaviour (auto_initialize=False) — the very first call
        # goes straight to tools/list with no preamble. Locks in
        # backwards compatibility with older HTTP MCP servers that reject
        # an initialize request.
        opener = _FakeHttpOpener()
        opener.register(
            "https://x/mcp",
            _FakeHttpResponse(b'{"jsonrpc":"2.0","id":"x","result":{"tools":[]}}'),
        )
        client = HttpMCPClient(
            server_id="x",
            url="https://x/mcp",
            auth_kind="none",
            _opener=opener,
        )
        client.list_tools()
        assert len(opener.calls) == 1
        assert opener.calls[0]["payload"]["method"] == "tools/list"

    def test_reconnect_clears_initialized_flag(self) -> None:
        opener = _FakeHttpOpener()
        # First handshake then user call.
        opener.register(
            "https://x/mcp",
            _FakeHttpResponse(
                b'{"jsonrpc":"2.0","id":"x","result":{"protocolVersion":"x"}}',
                headers={"Mcp-Session-Id": "s1"},
            ),
            urllib.error.HTTPError("https://x/mcp", 202, "Accepted", {}, None),
            _FakeHttpResponse(b'{"jsonrpc":"2.0","id":"y","result":{"tools":[]}}'),
            # After reconnect: handshake runs again, then user call.
            _FakeHttpResponse(
                b'{"jsonrpc":"2.0","id":"x","result":{"protocolVersion":"x"}}',
                headers={"Mcp-Session-Id": "s2"},
            ),
            urllib.error.HTTPError("https://x/mcp", 202, "Accepted", {}, None),
            _FakeHttpResponse(b'{"jsonrpc":"2.0","id":"y","result":{"tools":[]}}'),
        )
        client = HttpMCPClient(
            server_id="x",
            url="https://x/mcp",
            auth_kind="none",
            auto_initialize=True,
            _opener=opener,
        )
        client.list_tools()
        assert client._initialized is True
        assert client._session_id == "s1"

        client.reconnect()
        assert client._initialized is False
        assert client._session_id is None

        client.list_tools()
        assert client._initialized is True
        assert client._session_id == "s2"


# ---------------------------------------------------------------------------
# Stdio transport
# ---------------------------------------------------------------------------


class TestStdioTransport:
    def test_init_handshake_then_list_tools(self) -> None:
        spawn = _make_fake_spawn(
            # initialize reply
            {"jsonrpc": "2.0", "id": "x", "result": {"protocolVersion": "2024-11-05"}},
            # tools/list reply
            {"jsonrpc": "2.0", "id": "y", "result": {
                "tools": [{"name": "get_quote", "description": "yfinance", "inputSchema": {}}]
            }},
        )
        client = StdioMCPClient(
            server_id="yahoo", command=["fake-cmd"], _spawn=spawn,
        )
        try:
            tools = client.list_tools()
            assert tools[0]["name"] == "get_quote"
        finally:
            client.close()

    def test_subprocess_exit_raises_session_expired(self) -> None:
        # Pre-mark the proc as exited (rc=1) so any read sees EOF + poll!=None.
        def _spawn(command: Any, env: Any, cwd: Any) -> _FakeStdioProc:
            proc = _FakeStdioProc()
            proc.returncode = 1
            return proc

        client = StdioMCPClient(
            server_id="x", command=["dead-cmd"], _spawn=_spawn,
            startup_timeout=2,
        )
        with pytest.raises((MCPSessionExpiredError, StdioTransportError)):
            client.list_tools()

    def test_command_not_found(self) -> None:
        # Use a command we know won't exist on any platform.
        client = StdioMCPClient(
            server_id="x",
            command=["__definitely_not_a_real_program_42__"],
            startup_timeout=2,
        )
        with pytest.raises(StdioTransportError, match="command not found"):
            client.list_tools()

    def test_close_terminates_subprocess(self) -> None:
        spawn = _make_fake_spawn(
            {"jsonrpc": "2.0", "id": "x", "result": {}},
        )
        client = StdioMCPClient(server_id="x", command=["fake"], _spawn=spawn)
        client._ensure_started()
        proc = client._proc
        client.close()
        assert proc is not None
        assert proc._terminated is True


# ---------------------------------------------------------------------------
# Bootstrap
# ---------------------------------------------------------------------------


class TestBootstrap:
    def _empty_registry(self) -> Any:
        # Stand-in for ToolRegistry — only register_all + list_tools are
        # invoked by the bootstrap path we test below.
        class _StubRegistry:
            def __init__(self) -> None:
                self.descriptors: list[Any] = []

            def register_all(self, descs: list[Any], replace: bool = False) -> None:
                self.descriptors.extend(descs)

            def list_tools(self) -> list[Any]:
                return list(self.descriptors)

        return _StubRegistry()

    def test_seed_then_bootstrap_no_executor(self, tmp_path: Path) -> None:
        paths = WorkspacePaths(root=tmp_path)
        registry = self._empty_registry()

        # Inject a fake adapter factory so we don't actually open a network.
        class _FakeAdapter:
            def __init__(self, server_id: str) -> None:
                self.server_id = server_id
                self.client = type("C", (), {"list_tools": lambda self: [], "list_resources": lambda self: []})()

        def _factory(cfg: MCPServerConfig, *, vault: Any, token_cache: Any) -> _FakeAdapter:
            return _FakeAdapter(server_id=cfg.namespace)

        diagnostics = bootstrap_mcp_connectors(
            paths=paths,
            registry=registry,
            executor=None,
            auto_seed=True,
            _vault=_FakeVault(),
            _adapter_factory=_factory,
        )

        # Seed file was created.
        assert paths.connectors_mcp_servers.exists()
        assert diagnostics.seeded_default is True
        assert diagnostics.total_declared == 17
        assert diagnostics.total_enabled == 3

        # The 3 enabled servers attempted to attach (zero tools because
        # the fake adapter returned no tools).
        success_ids = {r.server_id for r in diagnostics.successes()}
        assert success_ids == {"sec_edgar", "yahoo_finance", "coingecko"}

        # The 14 disabled servers landed as skipped, not failed.
        skipped = [r for r in diagnostics.results if r.skipped_reason]
        assert len(skipped) == 14
        assert all(r.skipped_reason == "not enabled" for r in skipped)

    def test_disabled_server_not_built(self, tmp_path: Path) -> None:
        paths = WorkspacePaths(root=tmp_path)
        # Write a minimal config with one disabled server that has invalid
        # auth — bootstrap MUST NOT try to build it because only_enabled=True.
        paths.connectors_mcp_servers.parent.mkdir(parents=True, exist_ok=True)
        paths.connectors_mcp_servers.write_text(
            "version: 1\nservers:\n"
            "  - id: bad_server\n"
            "    enabled: false\n"
            "    transport: { kind: http, url: https://nope }\n"
            "    auth: { kind: bearer_static, token_ref: 'vault://nonexistent' }\n",
            encoding="utf-8",
        )
        registry = self._empty_registry()
        diagnostics = bootstrap_mcp_connectors(
            paths=paths,
            registry=registry,
            executor=None,
            auto_seed=False,
            _vault=_FakeVault(),  # empty — would explode on any vault.resolve
        )
        assert diagnostics.total_declared == 1
        assert diagnostics.total_enabled == 0
        assert diagnostics.results[0].skipped_reason == "not enabled"
        assert diagnostics.results[0].error is None

    def test_failing_server_does_not_abort_others(self, tmp_path: Path) -> None:
        paths = WorkspacePaths(root=tmp_path)
        paths.connectors_mcp_servers.parent.mkdir(parents=True, exist_ok=True)
        paths.connectors_mcp_servers.write_text(
            "version: 1\nservers:\n"
            "  - id: good\n"
            "    enabled: true\n"
            "    transport: { kind: http, url: https://good }\n"
            "  - id: bad\n"
            "    enabled: true\n"
            "    transport: { kind: http, url: https://bad }\n"
            "    auth: { kind: bearer_static, token_ref: 'vault://missing' }\n",
            encoding="utf-8",
        )
        registry = self._empty_registry()

        diagnostics = bootstrap_mcp_connectors(
            paths=paths,
            registry=registry,
            executor=None,
            auto_seed=False,
            _vault=_FakeVault(),  # missing vault entry causes 'bad' to fail
            _adapter_factory=lambda cfg, **kw: _build_fake_adapter_or_raise(cfg, kw["vault"]),
        )

        assert diagnostics.total_declared == 2
        assert diagnostics.total_enabled == 2
        successes = {r.server_id for r in diagnostics.successes()}
        failures = {r.server_id for r in diagnostics.failures()}
        assert successes == {"good"}
        assert failures == {"bad"}
        # The error message includes the cause.
        bad_row = [r for r in diagnostics.results if r.server_id == "bad"][0]
        assert "missing" in (bad_row.error or "")

    def test_build_stdio_client_merges_env_with_vault_winning(self) -> None:
        # Locks in the seed-swap contract: a plain ``env:`` default (e.g.
        # SEC_EDGAR_USER_AGENT) goes through unchanged when no vault entry
        # competes; a vault-resolved env_ref overrides a plain default on
        # key collision so an operator can promote a baked-in identifier
        # to a vault-stored one without removing the ``env`` entry.
        from nerya.mcp.connectors.bootstrap import _build_stdio_client

        cfg = MCPServerConfig.from_raw(
            {
                "id": "sec_edgar",
                "transport": {
                    "kind": "stdio",
                    "command": ["uvx", "sec-edgar-mcp"],
                    "env": {
                        "SEC_EDGAR_USER_AGENT": "Default Agent (mcp-bridge@nerya.local)",
                        "EXTRA_FLAG": "1",
                    },
                    "env_refs": {
                        "SEC_EDGAR_USER_AGENT": "vault://mcp_sec_edgar_user_agent",
                    },
                },
            }
        )
        vault = _FakeVault(secrets={"mcp_sec_edgar_user_agent": "Real Org (real@example.com)"})

        client = _build_stdio_client(cfg, vault=vault)

        assert client.command == ["uvx", "sec-edgar-mcp"]
        # Vault-resolved value wins on the colliding key.
        assert client.env["SEC_EDGAR_USER_AGENT"] == "Real Org (real@example.com)"
        # Non-colliding plain env entries pass through unchanged.
        assert client.env["EXTRA_FLAG"] == "1"

    def test_build_stdio_client_plain_env_only(self) -> None:
        # When only plain env is provided (no env_refs), it lands in the
        # subprocess env verbatim without touching the vault. This is the
        # zero-key default-on path for sec_edgar in the seed.
        from nerya.mcp.connectors.bootstrap import _build_stdio_client

        cfg = MCPServerConfig.from_raw(
            {
                "id": "sec_edgar",
                "transport": {
                    "kind": "stdio",
                    "command": ["uvx", "sec-edgar-mcp"],
                    "env": {
                        "SEC_EDGAR_USER_AGENT": "Nerya MCP Agent (mcp-bridge@nerya.local)",
                    },
                },
            }
        )
        # Empty vault — no vault.resolve call should ever happen.
        vault = _FakeVault()

        client = _build_stdio_client(cfg, vault=vault)

        assert client.env == {
            "SEC_EDGAR_USER_AGENT": "Nerya MCP Agent (mcp-bridge@nerya.local)"
        }


def _build_fake_adapter_or_raise(cfg: MCPServerConfig, vault: Any) -> Any:
    """Test helper — if the server's auth needs a vault ref the fake
    vault is missing, propagate the error so bootstrap records it.

    Otherwise return a fake adapter that pretends list_tools succeeded.
    """

    if cfg.auth.kind == "bearer_static" and cfg.auth.token_ref is not None:
        # This will raise ConnectorConfigError on missing entries.
        vault.resolve(cfg.auth.token_ref)

    class _FakeAdapter:
        def __init__(self) -> None:
            self.server_id = cfg.namespace

            class _C:
                def list_tools(self_inner) -> list[Any]:  # noqa: N805
                    return []

                def list_resources(self_inner) -> list[Any]:  # noqa: N805
                    return []

            self.client = _C()

    return _FakeAdapter()


# ---------------------------------------------------------------------------
# CLI smoke
# ---------------------------------------------------------------------------


class TestCLI:
    def test_materialize_writes_seed(self, tmp_path: Path, capsys: Any) -> None:
        from scripts.finance_mcp_connectors.cli import main

        rc = main(["materialize", "--workspace", str(tmp_path)])
        assert rc == 0

        out = capsys.readouterr().out
        payload = json.loads(out)
        assert payload["written_new_file"] is True
        assert (tmp_path / "connectors" / "mcp_servers.yml").exists()

        # Re-running is a no-op.
        rc2 = main(["materialize", "--workspace", str(tmp_path)])
        assert rc2 == 0
        out2 = capsys.readouterr().out
        payload2 = json.loads(out2)
        assert payload2["written_new_file"] is False

    def test_list_after_materialize(self, tmp_path: Path, capsys: Any) -> None:
        from scripts.finance_mcp_connectors.cli import main

        main(["materialize", "--workspace", str(tmp_path)])
        capsys.readouterr()

        rc = main(["list", "--workspace", str(tmp_path)])
        assert rc == 0
        payload = json.loads(capsys.readouterr().out)
        assert payload["total"] == 17
        assert payload["enabled"] == 3
        # Enabled ids match the zero-key open-tier set.
        enabled = sorted(s["id"] for s in payload["servers"] if s["enabled"])
        assert enabled == ["coingecko", "sec_edgar", "yahoo_finance"]

    def test_list_materializes_when_missing(self, tmp_path: Path, capsys: Any) -> None:
        from scripts.finance_mcp_connectors.cli import main

        rc = main(["list", "--workspace", str(tmp_path), "--materialize-if-missing"])
        assert rc == 0
        payload = json.loads(capsys.readouterr().out)
        assert payload["total"] == 17

    def test_doctor_refuses_without_config(self, tmp_path: Path, capsys: Any) -> None:
        from scripts.finance_mcp_connectors.cli import main

        rc = main(["doctor", "--workspace", str(tmp_path)])
        assert rc == 2
        payload = json.loads(capsys.readouterr().out)
        assert "does not exist" in payload["error"]

    def test_doctor_unknown_server(self, tmp_path: Path, capsys: Any) -> None:
        from scripts.finance_mcp_connectors.cli import main

        main(["materialize", "--workspace", str(tmp_path)])
        capsys.readouterr()

        rc = main([
            "doctor", "--workspace", str(tmp_path),
            "--server", "no_such_server",
        ])
        assert rc == 2
        payload = json.loads(capsys.readouterr().out)
        assert "unknown server id" in payload["error"]


# ---------------------------------------------------------------------------
# Phase I — kernel boot integration
# ---------------------------------------------------------------------------


class TestKernelMCPAttach:
    """Ensure ``AgentKernel._ensure_registry`` honours the
    ``mcp.connectors.enabled`` config gate per Phase I.

    These tests poke the kernel's *internal* MCP attach helper directly
    (rather than spinning up a full agent loop) so they're fast and
    don't require a live LLM. The contract under test is strictly the
    config gate + fail-soft semantics.
    """

    def _make_minimal_config(self, tmp_path: Path, *, mcp_data: dict[str, Any]):
        """Build a Config-like stub the helper recognises.

        We avoid importing ``Config`` from ``nerya.core.config`` to keep
        the test independent of YAML disk shape; the helper only reads
        ``config.data`` (must be a dict) and ``config.paths`` (must
        provide ``connectors_mcp_servers`` / ``connectors_oauth_cache``
        / ``vault_enc``).
        """

        from nerya.core.paths import WorkspacePaths

        class _StubConfig:
            def __init__(self, data: dict[str, Any], paths: WorkspacePaths) -> None:
                self.data = data
                self.paths = paths

        return _StubConfig(
            data={"mcp": mcp_data},
            paths=WorkspacePaths(root=tmp_path),
        )

    def test_default_off_no_attach(self, tmp_path: Path) -> None:
        """No 'mcp.connectors.enabled' key → bootstrap MUST NOT run."""

        from nerya.agent.kernel import AgentKernel

        called: dict[str, int] = {"calls": 0}

        # Monkey-patch the bootstrap so any accidental call is loud.
        import nerya.mcp.connectors as connectors_pkg

        original_bootstrap = connectors_pkg.bootstrap_mcp_connectors

        def _fake(*args: Any, **kwargs: Any) -> Any:
            called["calls"] += 1
            return original_bootstrap(*args, **kwargs)

        connectors_pkg.bootstrap_mcp_connectors = _fake
        try:
            cfg = self._make_minimal_config(tmp_path, mcp_data={})
            # Build a kernel-shaped object without going through the
            # full __init__ (which requires a real LLMGateway etc.).
            kernel = AgentKernel.__new__(AgentKernel)
            kernel.config = cfg
            kernel._registry = type("R", (), {"register_all": lambda self, *a, **k: None,
                                              "list_tools": lambda self: []})()
            kernel._maybe_attach_mcp_connectors()
            assert called["calls"] == 0, "bootstrap should not be invoked when default-off"
        finally:
            connectors_pkg.bootstrap_mcp_connectors = original_bootstrap

    def test_explicit_enable_triggers_attach(self, tmp_path: Path) -> None:
        """``mcp.connectors.enabled = true`` → bootstrap is invoked."""

        from nerya.agent.kernel import AgentKernel

        called: dict[str, Any] = {"calls": 0, "auto_seed": None, "paths_root": None}

        # Stub bootstrap_mcp_connectors at the kernel's import path.
        import nerya.mcp.connectors as connectors_pkg

        class _FakeDiag:
            total_declared = 0

            def successes(self) -> list[Any]:
                return []

            def failures(self) -> list[Any]:
                return []

            total_enabled = 0

        def _fake(*, paths: Any, registry: Any, executor: Any = None,
                   resource_index: Any = None, vault_passphrase: Any = None,
                   auto_seed: bool = True, **kw: Any) -> Any:
            called["calls"] += 1
            called["auto_seed"] = auto_seed
            called["paths_root"] = paths.root
            return _FakeDiag()

        original = connectors_pkg.bootstrap_mcp_connectors
        connectors_pkg.bootstrap_mcp_connectors = _fake
        try:
            cfg = self._make_minimal_config(
                tmp_path,
                mcp_data={"connectors": {"enabled": True, "auto_seed": False}},
            )
            kernel = AgentKernel.__new__(AgentKernel)
            kernel.config = cfg
            kernel._registry = type("R", (), {"register_all": lambda self, *a, **k: None,
                                              "list_tools": lambda self: []})()
            kernel._maybe_attach_mcp_connectors()
            assert called["calls"] == 1
            assert called["auto_seed"] is False
            assert called["paths_root"] == tmp_path
        finally:
            connectors_pkg.bootstrap_mcp_connectors = original

    def test_bootstrap_failure_does_not_crash_kernel(self, tmp_path: Path) -> None:
        """If bootstrap_mcp_connectors raises, kernel boot continues."""

        from nerya.agent.kernel import AgentKernel

        import nerya.mcp.connectors as connectors_pkg

        def _exploding_bootstrap(*args: Any, **kwargs: Any) -> Any:
            raise RuntimeError("simulated catastrophic MCP failure")

        original = connectors_pkg.bootstrap_mcp_connectors
        connectors_pkg.bootstrap_mcp_connectors = _exploding_bootstrap
        try:
            cfg = self._make_minimal_config(
                tmp_path,
                mcp_data={"connectors": {"enabled": True}},
            )
            kernel = AgentKernel.__new__(AgentKernel)
            kernel.config = cfg
            kernel._registry = type("R", (), {"register_all": lambda self, *a, **k: None,
                                              "list_tools": lambda self: []})()
            # MUST NOT raise.
            kernel._maybe_attach_mcp_connectors()
        finally:
            connectors_pkg.bootstrap_mcp_connectors = original
