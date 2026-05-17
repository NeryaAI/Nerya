"""HTTP MCP client transport.

JSON-RPC 2.0 over HTTP — speaks to remote MCP servers like
``https://mcp.alphavantage.co/mcp`` (open free-key) or
``https://mcp.daloopa.com/server/mcp`` (paid).

Implements :class:`nerya.mcp.session_adapter.MCPClient` so it can be
wrapped by :class:`MCPSessionAdapter` without any additional shim.

Auth modes (per USER decision E-3 = ``full_oauth_dance``):

* ``none``                       — no Authorization header (open-tier servers)
* ``bearer_static``              — fixed token from vault
* ``oauth_client_credentials``   — full OAuth flow with token cache + refresh
                                   (delegated to :mod:`.oauth`)

A 401 response triggers exactly one retry: the cache is invalidated,
a fresh token is minted, and the call re-issued. If that retry also
401s the error propagates as :class:`MCPSessionExpiredError` so
:class:`MCPSessionAdapter` surfaces ``MCP_SESSION_EXPIRED`` and the
agent can decide whether to reconnect.

The transport is dependency-light (stdlib ``urllib`` only) so tests
run in offline environments without ``httpx`` / ``requests``.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
import uuid
from dataclasses import dataclass, field
from typing import Any, Optional

from ..session_adapter import MCPSessionExpiredError
from .oauth import OAuthCredentials, OAuthTokenCache, OAuthTokenError, resolve_token_for


class HttpTransportError(Exception):
    """Raised on transport-level failures (network, malformed JSON-RPC).

    *Not* raised for application-level MCP errors (those land inside the
    JSON-RPC envelope and the adapter maps them to ``isError=true``).
    """


# ---------------------------------------------------------------------------
# JSON-RPC helpers
# ---------------------------------------------------------------------------


def _next_id() -> str:
    return f"nerya-{uuid.uuid4().hex[:12]}"


def _build_rpc(method: str, params: dict[str, Any]) -> dict[str, Any]:
    return {
        "jsonrpc": "2.0",
        "id": _next_id(),
        "method": method,
        "params": params,
    }


def _parse_sse_envelope(raw: bytes, *, server_id: str) -> dict[str, Any]:
    """Parse a Streamable HTTP SSE response into a JSON-RPC envelope.

    The MCP Streamable HTTP transport returns ``text/event-stream`` framed
    bodies even for single request/response calls. Frames are separated by
    a blank line; within each frame, lines starting with ``data:`` carry
    JSON. A frame may also have ``event:`` / ``id:`` / ``retry:`` lines
    or ``:`` comments which we ignore.

    A server may stream multiple frames before the final result (progress
    notifications, log events). We pick the last frame whose JSON looks
    like a JSON-RPC envelope (has ``result`` or ``error`` keys), falling
    back to the last parseable frame if none match.
    """

    text = raw.decode("utf-8", errors="replace")
    candidate: Optional[dict[str, Any]] = None
    last_parseable: Optional[dict[str, Any]] = None

    for frame in text.split("\n\n"):
        if not frame.strip():
            continue
        data_lines: list[str] = []
        for line in frame.splitlines():
            if line.startswith(":"):
                continue  # SSE comment
            if line.startswith("data:"):
                data_lines.append(line[5:].lstrip())
        if not data_lines:
            continue
        body = "\n".join(data_lines).strip()
        if not body:
            continue
        try:
            obj = json.loads(body)
        except json.JSONDecodeError:
            continue
        if isinstance(obj, dict):
            last_parseable = obj
            if "result" in obj or "error" in obj:
                candidate = obj

    chosen = candidate if candidate is not None else last_parseable
    if chosen is None:
        raise HttpTransportError(
            f"server {server_id!r} returned SSE with no parseable JSON-RPC envelope"
        )
    return chosen


# ---------------------------------------------------------------------------
# Transport
# ---------------------------------------------------------------------------


@dataclass
class HttpMCPClient:
    """One live HTTP session against a single remote MCP server.

    Constructor args:

    * ``server_id`` — the operator-facing id (used in the
      ``mcp__<server_id>__<tool>`` registry name).
    * ``url`` — fully-qualified MCP endpoint (e.g.
      ``https://mcp.alphavantage.co/mcp``).
    * ``auth_kind`` / ``static_bearer`` / ``oauth`` — auth wiring.
      Mutually exclusive — pick one shape; the bootstrap validates this
      before constructing.
    * ``token_cache`` — required when ``auth_kind == "oauth_client_credentials"``;
      ignored otherwise.
    * ``extra_headers`` — operator-controlled header map (e.g. for
      providers that want a custom ``X-API-Version`` header).
    * ``timeout_seconds`` — per-request HTTP timeout.

    The transport keeps no per-call state beyond the persistent
    OAuth token cache; concurrent calls are safe at the transport
    layer (urllib handles its own connection pool internally and we
    never share a Request object across threads).
    """

    server_id: str
    url: str
    auth_kind: str = "none"  # "none" | "bearer_static" | "oauth_client_credentials"
    static_bearer: Optional[str] = None
    oauth: Optional[OAuthCredentials] = None
    token_cache: Optional[OAuthTokenCache] = None
    extra_headers: dict[str, str] = field(default_factory=dict)
    timeout_seconds: float = 30.0

    # Modern MCP Streamable HTTP servers (CoinGecko et al.) require a
    # full ``initialize`` + ``notifications/initialized`` handshake before
    # any ``tools/list`` call; older request/response JSON-RPC HTTP MCPs
    # (alpha_vantage, daloopa, factset, ...) reject ``initialize`` outright.
    # So this is opt-in per server, configured in mcp_servers.yml and
    # plumbed through HttpTransportConfig.
    auto_initialize: bool = False

    # Used only by tests — replaces ``urllib.request.build_opener()``
    # output. Production code leaves it as None and we use the default.
    _opener: Any = None

    # Streamable HTTP session id, captured from the response of the very
    # first call (initialize / tools-list) and replayed on every following
    # request as ``Mcp-Session-Id``. ``reconnect()`` clears it so a fresh
    # session can be negotiated. None for legacy request/response servers
    # that don't issue a session id at all.
    _session_id: Optional[str] = None

    # Set once the initialize handshake completes (only matters when
    # ``auto_initialize`` is True). Cleared by ``reconnect()`` so a fresh
    # handshake runs against any recovered session.
    _initialized: bool = False

    def __post_init__(self) -> None:
        if self.auth_kind not in {"none", "bearer_static", "oauth_client_credentials"}:
            raise ValueError(
                f"unknown auth_kind {self.auth_kind!r} for server {self.server_id!r}"
            )
        if self.auth_kind == "bearer_static" and not self.static_bearer:
            raise ValueError(
                f"server {self.server_id!r}: bearer_static requires static_bearer"
            )
        if self.auth_kind == "oauth_client_credentials":
            if self.oauth is None:
                raise ValueError(
                    f"server {self.server_id!r}: oauth_client_credentials requires oauth"
                )
            if self.token_cache is None:
                raise ValueError(
                    f"server {self.server_id!r}: oauth_client_credentials requires token_cache"
                )

    # ------------------------------------------------------------------
    # MCPClient Protocol
    # ------------------------------------------------------------------

    def list_tools(self) -> list[dict[str, Any]]:
        envelope = self._rpc("tools/list", {})
        result = envelope.get("result") or {}
        tools = result.get("tools") or []
        return list(tools) if isinstance(tools, list) else []

    def list_resources(self) -> list[dict[str, Any]]:
        try:
            envelope = self._rpc("resources/list", {})
        except HttpTransportError:
            # Some MCP servers don't implement resources/* at all; return
            # empty rather than blowing up the boot path.
            return []
        result = envelope.get("result") or {}
        resources = result.get("resources") or []
        return list(resources) if isinstance(resources, list) else []

    def list_skills(self) -> list[dict[str, Any]]:
        # MCP-spec ``skills/list`` is optional; treat absence as "none".
        try:
            envelope = self._rpc("skills/list", {})
        except HttpTransportError:
            return []
        result = envelope.get("result") or {}
        skills = result.get("skills") or []
        return list(skills) if isinstance(skills, list) else []

    def call_tool(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        envelope = self._rpc("tools/call", {"name": name, "arguments": arguments})
        result = envelope.get("result")
        if not isinstance(result, dict):
            raise HttpTransportError(
                f"server {self.server_id!r} returned non-dict result for tools/call"
            )
        return result

    def reconnect(self) -> None:
        """Drop session-id + handshake state + invalidate OAuth cache.

        For Streamable HTTP servers, the captured ``Mcp-Session-Id`` is
        cleared and the ``initialize`` handshake state is reset so the
        next call negotiates a fresh session from scratch. For OAuth
        servers, the token cache is invalidated so the next call mints a
        fresh token. That's the closest thing to "reconnect" an HTTP
        transport can offer — every call is otherwise its own request.
        """

        self._session_id = None
        self._initialized = False
        if self.token_cache is not None and self.oauth is not None:
            self.token_cache.invalidate(self.oauth.cache_key())

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _bearer_token(self, *, force_refresh: bool = False) -> Optional[str]:
        if self.auth_kind == "none":
            return None
        if self.auth_kind == "bearer_static":
            return self.static_bearer
        if self.auth_kind == "oauth_client_credentials":
            assert self.oauth is not None and self.token_cache is not None
            return resolve_token_for(
                self.oauth, cache=self.token_cache, force_refresh=force_refresh,
            )
        return None  # pragma: no cover - guarded in __post_init__

    def _build_request(
        self, payload: dict[str, Any], *, force_refresh_token: bool = False,
    ) -> urllib.request.Request:
        body = json.dumps(payload).encode("utf-8")
        headers: dict[str, str] = {
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
            "User-Agent": "Nerya-MCP/1.0",
        }
        try:
            token = self._bearer_token(force_refresh=force_refresh_token)
        except OAuthTokenError as exc:
            raise HttpTransportError(
                f"server {self.server_id!r} oauth mint failed: {exc}"
            ) from exc
        if token:
            headers["Authorization"] = f"Bearer {token}"
        # Streamable HTTP: replay the session id captured from the first
        # response so the server can resume per-session state (CoinGecko,
        # the official MCP spec, and most modern HTTP MCP servers).
        if self._session_id:
            headers["Mcp-Session-Id"] = self._session_id
        for k, v in (self.extra_headers or {}).items():
            headers[str(k)] = str(v)
        return urllib.request.Request(self.url, data=body, headers=headers, method="POST")

    def _ensure_initialized(self) -> None:
        """Run the MCP ``initialize`` + ``notifications/initialized`` dance.

        Only invoked when ``auto_initialize`` is True and the handshake
        hasn't completed yet. Mirrors the stdio transport behaviour but
        adapted to one-shot HTTP: the session id captured from the
        initialize response is what subsequent calls replay via the
        ``Mcp-Session-Id`` header.

        Idempotent — sets ``_initialized`` BEFORE issuing the call so a
        recursive ``_rpc`` triggered by the initialize itself short-circuits.
        On failure, clears the flag so the next user-driven call retries.
        """

        if self._initialized:
            return
        # Set early to break the recursion via _rpc.
        self._initialized = True
        try:
            envelope = self._rpc(
                "initialize",
                {
                    "protocolVersion": "2024-11-05",
                    "clientInfo": {"name": "Nerya", "version": "1.0"},
                    "capabilities": {},
                },
            )
        except Exception:
            self._initialized = False
            raise

        if not isinstance(envelope, dict) or "result" not in envelope:
            self._initialized = False
            err = envelope.get("error") if isinstance(envelope, dict) else envelope
            raise HttpTransportError(
                f"server {self.server_id!r}: initialize handshake failed: {err!r}"
            )

        # Best-effort follow-up notification. Some servers ignore it,
        # some require it, some return 202 Accepted with empty body
        # (which would otherwise trip the JSON parser). We send it via
        # a dedicated path that doesn't require a JSON-RPC envelope back.
        try:
            self._send_notification(
                {
                    "jsonrpc": "2.0",
                    "method": "notifications/initialized",
                    "params": {},
                }
            )
        except Exception:  # pragma: no cover - best effort
            pass

    def _send_notification(self, payload: dict[str, Any]) -> None:
        """POST a JSON-RPC notification (no id, no expected JSON body).

        Servers commonly answer with HTTP 202 Accepted + empty body.
        We discard whatever comes back; the only failure mode that
        matters is the network call itself raising.
        """

        request = self._build_request(payload)
        opener = self._opener if self._opener is not None else urllib.request.build_opener()
        try:
            with opener.open(request, timeout=self.timeout_seconds) as resp:  # nosec
                resp.read()
        except urllib.error.HTTPError as exc:
            # 202 Accepted is the typical "no-body OK" response.
            if exc.code in (200, 202, 204):
                return
            raise HttpTransportError(
                f"server {self.server_id!r}: notification HTTP {exc.code}: {exc.reason}"
            ) from exc

    def _rpc(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        # Lazy initialize handshake (Streamable HTTP servers only).
        if self.auto_initialize and not self._initialized and method != "initialize":
            self._ensure_initialized()

        rpc = _build_rpc(method, params)

        request = self._build_request(rpc)
        opener = self._opener if self._opener is not None else urllib.request.build_opener()

        # Capture content-type + session id alongside the body so the
        # parsing branch below can dispatch SSE vs plain JSON correctly.
        content_type = ""
        try:
            with opener.open(request, timeout=self.timeout_seconds) as resp:  # nosec - operator URL
                raw = resp.read()
                status = getattr(resp, "status", 200)
                resp_headers = getattr(resp, "headers", None)
                if resp_headers is not None:
                    # urllib HTTPMessage is case-insensitive on .get
                    content_type = (resp_headers.get("Content-Type") or "").lower()
                    if not self._session_id:
                        sid = (
                            resp_headers.get("Mcp-Session-Id")
                            or resp_headers.get("mcp-session-id")
                        )
                        if sid:
                            self._session_id = sid.strip()
        except urllib.error.HTTPError as exc:
            status = exc.code
            try:
                raw = exc.read()
            except Exception:  # pragma: no cover - defensive
                raw = b""
            if status == 401 and self.auth_kind == "oauth_client_credentials":
                # Single retry with a freshly minted token (USER E-3
                # promised "refresh-on-401 single retry").
                request2 = self._build_request(rpc, force_refresh_token=True)
                try:
                    with opener.open(request2, timeout=self.timeout_seconds) as resp2:  # nosec
                        raw = resp2.read()
                        status = getattr(resp2, "status", 200)
                except urllib.error.HTTPError as exc2:
                    if exc2.code == 401:
                        raise MCPSessionExpiredError(
                            f"server {self.server_id!r} returned 401 after refresh"
                        ) from exc2
                    raise HttpTransportError(
                        f"server {self.server_id!r} HTTP {exc2.code} after refresh: "
                        f"{exc2.reason}"
                    ) from exc2
                except Exception as exc2:
                    raise HttpTransportError(
                        f"server {self.server_id!r} retry failed: {exc2}"
                    ) from exc2
            elif status == 401:
                raise MCPSessionExpiredError(
                    f"server {self.server_id!r} returned 401 (no oauth refresh available)"
                ) from exc
            else:
                raise HttpTransportError(
                    f"server {self.server_id!r} HTTP {status}: {exc.reason}"
                ) from exc
        except Exception as exc:
            raise HttpTransportError(
                f"server {self.server_id!r} request failed: {exc}"
            ) from exc

        if "text/event-stream" in content_type:
            envelope = _parse_sse_envelope(raw, server_id=self.server_id)
        else:
            try:
                envelope = json.loads(raw.decode("utf-8"))
            except (json.JSONDecodeError, UnicodeDecodeError) as exc:
                raise HttpTransportError(
                    f"server {self.server_id!r} returned non-JSON for {method}: {exc}"
                ) from exc

        if not isinstance(envelope, dict):
            raise HttpTransportError(
                f"server {self.server_id!r} returned non-dict envelope: {envelope!r}"
            )

        if "error" in envelope and "result" not in envelope:
            err = envelope["error"]
            if isinstance(err, dict):
                code = err.get("code")
                msg = err.get("message") or "MCP error"
                if code == -32001 or "session" in str(msg).lower():
                    raise MCPSessionExpiredError(
                        f"server {self.server_id!r} reported session expiry: {msg}"
                    )
                raise HttpTransportError(
                    f"server {self.server_id!r} JSON-RPC error {code}: {msg}"
                )

        return envelope


__all__ = ["HttpMCPClient", "HttpTransportError"]
