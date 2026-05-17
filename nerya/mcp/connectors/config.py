"""Schema + loader for ``<workspace>/connectors/mcp_servers.yml``.

The config is operator-edited so the loader is intentionally
**permissive on input but strict on validation**: unknown keys are
ignored (so future Nerya versions can add fields without breaking old
configs), but malformed values raise a typed
:class:`ConnectorConfigError` with the offending server id in the
message.

Schema (per server entry):

.. code-block:: yaml

    - id: alpha_vantage             # required, used as registry namespace
      enabled: false                # default false — explicit opt-in
      namespace: av                 # optional, defaults to ``id``
      transport:
        kind: http | stdio          # required
        # HTTP shape:
        url: https://...            # required when kind=http
        timeout_seconds: 30         # optional
        extra_headers:              # optional, operator-controlled
          X-API-Version: "2025-01"
        # stdio shape:
        command: ["uvx", "name"]    # required when kind=stdio
        cwd: /opt/somewhere         # optional
        startup_timeout: 30         # optional
        read_timeout: 60            # optional
        env_refs:                   # optional, vault refs → env vars
          ALPHA_VANTAGE_KEY: vault://mcp_alpha_vantage_api_key
      auth:
        kind: none                  # default
                                    # | bearer_static | oauth_client_credentials
        token_ref: vault://...      # required when kind=bearer_static
        # OAuth shape:
        client_id: "32kfd2..."      # may be plaintext (often public OAuth client_id)
        client_id_ref: vault://...  # alternative — read from vault
        client_secret_ref: vault://...
        token_url: https://.../token        # may be plaintext
        token_url_ref: vault://...          # or read from vault
        scope: "read:filings"               # optional
        audience: "https://api.example.com" # optional
      notes: "Free tier — get key at alphavantage.co"
      # Per-server tool filter. When set, the named tools are
      # filtered out at registration time so they NEVER reach the
      # ToolRegistry (and therefore can never be dispatched via
      # ``mcp_call`` or appear in ``mcp_describe``). Use this to retire
      # MCP tools that overlap with native connectors. ``allow_tools``,
      # if set, is treated as a strict whitelist.
      deny_tools: ["get_historical_stock_prices"]
      # allow_tools: ["get_stock_info", "get_financial_statement"]
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional, Union

from ...core import yaml_io


class ConnectorConfigError(ValueError):
    """Raised when ``mcp_servers.yml`` fails validation."""


# ---------------------------------------------------------------------------
# Vault references — opaque pointers; resolution happens at bootstrap time
# ---------------------------------------------------------------------------


VAULT_PREFIX = "vault://"


@dataclass(frozen=True)
class VaultRef:
    """A pointer to a value stored in :class:`SecretVault`.

    Construction from a string:
        ``VaultRef.parse("vault://mcp_aiera_client_secret")``
    """

    name: str

    @classmethod
    def parse(cls, raw: str) -> "VaultRef":
        if not isinstance(raw, str) or not raw.startswith(VAULT_PREFIX):
            raise ConnectorConfigError(
                f"vault ref must start with {VAULT_PREFIX!r}, got {raw!r}"
            )
        name = raw[len(VAULT_PREFIX):].strip()
        if not name:
            raise ConnectorConfigError("vault ref has empty name after prefix")
        return cls(name=name)

    def as_str(self) -> str:
        return f"{VAULT_PREFIX}{self.name}"


def _maybe_vault_ref(raw: Any) -> Optional[VaultRef]:
    """Best-effort conversion: returns None if not a vault ref string."""

    if isinstance(raw, str) and raw.startswith(VAULT_PREFIX):
        return VaultRef.parse(raw)
    return None


# ---------------------------------------------------------------------------
# Auth config
# ---------------------------------------------------------------------------


_VALID_AUTH_KINDS = frozenset({"none", "bearer_static", "oauth_client_credentials"})


@dataclass(frozen=True)
class AuthConfig:
    """Validated auth descriptor (no secret values yet — those are vault refs)."""

    kind: str  # "none" | "bearer_static" | "oauth_client_credentials"

    # bearer_static
    token_ref: Optional[VaultRef] = None

    # oauth_client_credentials — at least one of (client_id, client_id_ref)
    # and one of (token_url, token_url_ref) must be provided.
    client_id: Optional[str] = None
    client_id_ref: Optional[VaultRef] = None
    client_secret_ref: Optional[VaultRef] = None
    token_url: Optional[str] = None
    token_url_ref: Optional[VaultRef] = None
    scope: Optional[str] = None
    audience: Optional[str] = None

    @classmethod
    def from_raw(cls, server_id: str, raw: Optional[dict[str, Any]]) -> "AuthConfig":
        if raw is None:
            return cls(kind="none")
        if not isinstance(raw, dict):
            raise ConnectorConfigError(
                f"server {server_id!r}: 'auth' must be a mapping, got {type(raw).__name__}"
            )
        kind = str(raw.get("kind") or "none").strip().lower()
        if kind not in _VALID_AUTH_KINDS:
            raise ConnectorConfigError(
                f"server {server_id!r}: auth.kind={kind!r} (allowed: {sorted(_VALID_AUTH_KINDS)})"
            )

        if kind == "none":
            return cls(kind="none")

        if kind == "bearer_static":
            token_ref_raw = raw.get("token_ref")
            if not isinstance(token_ref_raw, str):
                raise ConnectorConfigError(
                    f"server {server_id!r}: bearer_static requires auth.token_ref (vault://...)"
                )
            return cls(kind=kind, token_ref=VaultRef.parse(token_ref_raw))

        # oauth_client_credentials
        client_id = raw.get("client_id")
        client_id_ref_raw = raw.get("client_id_ref")
        client_secret_ref_raw = raw.get("client_secret_ref")
        token_url = raw.get("token_url")
        token_url_ref_raw = raw.get("token_url_ref")

        if client_id is None and client_id_ref_raw is None:
            raise ConnectorConfigError(
                f"server {server_id!r}: oauth requires client_id or client_id_ref"
            )
        if not client_secret_ref_raw or not isinstance(client_secret_ref_raw, str):
            raise ConnectorConfigError(
                f"server {server_id!r}: oauth requires client_secret_ref (vault://...)"
            )
        if token_url is None and token_url_ref_raw is None:
            raise ConnectorConfigError(
                f"server {server_id!r}: oauth requires token_url or token_url_ref"
            )

        return cls(
            kind=kind,
            client_id=str(client_id) if client_id else None,
            client_id_ref=VaultRef.parse(client_id_ref_raw) if isinstance(client_id_ref_raw, str) else None,
            client_secret_ref=VaultRef.parse(client_secret_ref_raw),
            token_url=str(token_url) if token_url else None,
            token_url_ref=VaultRef.parse(token_url_ref_raw) if isinstance(token_url_ref_raw, str) else None,
            scope=str(raw["scope"]) if raw.get("scope") else None,
            audience=str(raw["audience"]) if raw.get("audience") else None,
        )


# ---------------------------------------------------------------------------
# Transport configs
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class HttpTransportConfig:
    url: str
    timeout_seconds: float = 30.0
    extra_headers: dict[str, str] = field(default_factory=dict)
    #: Modern MCP Streamable HTTP servers (e.g. CoinGecko) require an
    #: explicit ``initialize`` + ``notifications/initialized`` handshake
    #: before any tools/list call. Older request/response JSON-RPC HTTP
    #: servers (alpha_vantage, daloopa, factset, …) do NOT — sending
    #: initialize there can be a hard error. So this is opt-in per server.
    auto_initialize: bool = False


@dataclass(frozen=True)
class StdioTransportConfig:
    command: tuple[str, ...]
    cwd: Optional[str] = None
    startup_timeout: float = 30.0
    read_timeout: float = 60.0
    #: plain str→str env vars for the subprocess. Use this for non-secret
    #: identifiers like SEC_EDGAR_USER_AGENT or DEBUG flags. Always merged
    #: BEFORE env_refs so vault-resolved values win on collision.
    env: dict[str, str] = field(default_factory=dict)
    #: vault refs that resolve to environment variables for the subprocess.
    #: Use this for any actual credential. Wins over plain ``env`` on key
    #: collision so an operator can promote a default identifier to a
    #: vault-stored one without removing the ``env`` entry.
    env_refs: dict[str, VaultRef] = field(default_factory=dict)


def _parse_transport(
    server_id: str, raw: Any,
) -> Union[HttpTransportConfig, StdioTransportConfig]:
    if not isinstance(raw, dict):
        raise ConnectorConfigError(
            f"server {server_id!r}: 'transport' must be a mapping"
        )
    kind = str(raw.get("kind") or "").strip().lower()
    if kind == "http":
        url = raw.get("url")
        if not isinstance(url, str) or not url.startswith(("http://", "https://")):
            raise ConnectorConfigError(
                f"server {server_id!r}: http transport requires url (https://...)"
            )
        extra = raw.get("extra_headers") or {}
        if not isinstance(extra, dict):
            raise ConnectorConfigError(
                f"server {server_id!r}: extra_headers must be a mapping"
            )
        return HttpTransportConfig(
            url=url.strip(),
            timeout_seconds=float(raw.get("timeout_seconds") or 30.0),
            extra_headers={str(k): str(v) for k, v in extra.items()},
            auto_initialize=bool(raw.get("auto_initialize", False)),
        )

    if kind == "stdio":
        cmd = raw.get("command")
        if not isinstance(cmd, list) or not all(isinstance(c, str) for c in cmd) or not cmd:
            raise ConnectorConfigError(
                f"server {server_id!r}: stdio transport requires non-empty command list of strings"
            )
        env_raw = raw.get("env") or {}
        if not isinstance(env_raw, dict):
            raise ConnectorConfigError(
                f"server {server_id!r}: env must be a mapping of envvar name → string"
            )
        env: dict[str, str] = {}
        for k, v in env_raw.items():
            if not isinstance(v, (str, int, float, bool)):
                raise ConnectorConfigError(
                    f"server {server_id!r}: env[{k!r}] must be a scalar str/int/float/bool, got {type(v).__name__}"
                )
            env[str(k)] = str(v)

        env_refs_raw = raw.get("env_refs") or {}
        if not isinstance(env_refs_raw, dict):
            raise ConnectorConfigError(
                f"server {server_id!r}: env_refs must be a mapping of envvar name → vault ref"
            )
        env_refs: dict[str, VaultRef] = {}
        for k, v in env_refs_raw.items():
            ref = _maybe_vault_ref(v)
            if ref is None:
                raise ConnectorConfigError(
                    f"server {server_id!r}: env_refs[{k!r}] must be a vault:// ref, got {v!r}"
                )
            env_refs[str(k)] = ref
        return StdioTransportConfig(
            command=tuple(cmd),
            cwd=str(raw["cwd"]) if raw.get("cwd") else None,
            startup_timeout=float(raw.get("startup_timeout") or 30.0),
            read_timeout=float(raw.get("read_timeout") or 60.0),
            env=env,
            env_refs=env_refs,
        )

    raise ConnectorConfigError(
        f"server {server_id!r}: transport.kind={kind!r} (allowed: 'http', 'stdio')"
    )


# ---------------------------------------------------------------------------
# Server + top-level config
# ---------------------------------------------------------------------------


_ID_RE_OK = lambda s: isinstance(s, str) and s.replace("_", "").replace("-", "").isalnum()


@dataclass(frozen=True)
class MCPServerConfig:
    """One server entry in ``mcp_servers.yml``."""

    id: str
    enabled: bool
    namespace: str
    transport: Union[HttpTransportConfig, StdioTransportConfig]
    auth: AuthConfig
    notes: str = ""

    #: MCP prompt-visibility override.
    #: When ``False`` (default), the server's tools land in the registry
    #: with ``ToolDescriptor.lazy=True`` so they DO NOT appear in the
    #: agent's prompt-time tool list until the model explicitly calls
    #: ``mcp_describe(namespace=...)`` for this server's namespace.
    #: When ``True``, the server's tools are eagerly visible from the
    #: first prompt — operators set this for hot-path namespaces where
    #: the meta-tool round-trip latency is unacceptable.
    always_eager: bool = False

    #: Per-server tool filter applied at registration time.
    #:
    #: ``deny_tools`` lists tool names that should be **filtered out
    #: before** the descriptor is built. They never enter the registry,
    #: never appear in ``mcp_namespaces`` counts, never show up in
    #: ``mcp_describe`` output, and cannot be dispatched via
    #: ``mcp_call``. Use this to retire MCP tools that overlap with
    #: native Nerya connectors (e.g. yahoo MCP's
    #: ``get_historical_stock_prices`` overlaps the native
    #: ``YahooFinanceConnector`` reachable via the built-in
    #: ``market_data`` tool with ``venue="yahoo"``).
    #:
    #: ``allow_tools``, if not ``None``, is a strict whitelist — only
    #: those names are kept, everything else is dropped. ``deny_tools``
    #: is applied first, then ``allow_tools`` filters the survivors.
    #:
    #: Both names match the **upstream MCP tool name** (the ``name``
    #: field returned by ``tools/list``), not the namespaced public
    #: name (``mcp__server__tool``). This matches the
    #: :class:`RegistryBridgePolicy` shape already in
    #: ``nerya/mcp/registry_bridge.py``.
    deny_tools: tuple[str, ...] = ()
    allow_tools: Optional[tuple[str, ...]] = None

    @classmethod
    def from_raw(cls, raw: Any) -> "MCPServerConfig":
        if not isinstance(raw, dict):
            raise ConnectorConfigError(
                f"server entry must be a mapping, got {type(raw).__name__}"
            )
        sid_raw = raw.get("id")
        if not _ID_RE_OK(sid_raw):
            raise ConnectorConfigError(
                f"server entry has missing or invalid 'id' (alphanum + _ - only): {sid_raw!r}"
            )
        sid = str(sid_raw).strip()
        namespace = str(raw.get("namespace") or sid).strip()
        if not _ID_RE_OK(namespace):
            raise ConnectorConfigError(
                f"server {sid!r}: namespace must be alphanumeric / underscore / hyphen"
            )
        deny_tools_raw = raw.get("deny_tools")
        if deny_tools_raw is None:
            deny_tools: tuple[str, ...] = ()
        elif isinstance(deny_tools_raw, (list, tuple)):
            deny_tools = tuple(
                str(name).strip()
                for name in deny_tools_raw
                if isinstance(name, str) and str(name).strip()
            )
        else:
            raise ConnectorConfigError(
                f"server {sid!r}: deny_tools must be a list of strings, "
                f"got {type(deny_tools_raw).__name__}"
            )

        allow_tools_raw = raw.get("allow_tools")
        if allow_tools_raw is None:
            allow_tools: Optional[tuple[str, ...]] = None
        elif isinstance(allow_tools_raw, (list, tuple)):
            allow_tools = tuple(
                str(name).strip()
                for name in allow_tools_raw
                if isinstance(name, str) and str(name).strip()
            )
        else:
            raise ConnectorConfigError(
                f"server {sid!r}: allow_tools must be a list of strings (or "
                f"omitted), got {type(allow_tools_raw).__name__}"
            )

        return cls(
            id=sid,
            enabled=bool(raw.get("enabled", False)),
            namespace=namespace,
            transport=_parse_transport(sid, raw.get("transport")),
            auth=AuthConfig.from_raw(sid, raw.get("auth")),
            notes=str(raw.get("notes") or "").strip(),
            always_eager=bool(raw.get("always_eager", False)),
            deny_tools=deny_tools,
            allow_tools=allow_tools,
        )


@dataclass
class MCPServersConfig:
    """Loaded view of ``mcp_servers.yml``."""

    version: int
    servers: list[MCPServerConfig]

    def enabled_servers(self) -> list[MCPServerConfig]:
        return [s for s in self.servers if s.enabled]

    def by_id(self, server_id: str) -> Optional[MCPServerConfig]:
        for s in self.servers:
            if s.id == server_id:
                return s
        return None


def load_mcp_servers_config(path: Path) -> MCPServersConfig:
    """Read + validate ``mcp_servers.yml`` from ``path``.

    Returns an empty config (``servers=[]``) if the file is missing —
    bootstrap auto-creates a stub on first run, but tests sometimes
    pass a fresh tmp_path before the seed step ran.
    """

    raw_doc = yaml_io.load(path, default=None)
    if raw_doc is None:
        return MCPServersConfig(version=1, servers=[])
    if not isinstance(raw_doc, dict):
        raise ConnectorConfigError(
            f"{path}: top-level must be a mapping, got {type(raw_doc).__name__}"
        )
    version = int(raw_doc.get("version") or 1)
    if version != 1:
        raise ConnectorConfigError(
            f"{path}: unsupported config version {version!r} (only v1 currently)"
        )
    raw_servers = raw_doc.get("servers") or []
    if not isinstance(raw_servers, list):
        raise ConnectorConfigError(
            f"{path}: 'servers' must be a list, got {type(raw_servers).__name__}"
        )

    seen: set[str] = set()
    out: list[MCPServerConfig] = []
    for entry in raw_servers:
        cfg = MCPServerConfig.from_raw(entry)
        if cfg.id in seen:
            raise ConnectorConfigError(
                f"{path}: duplicate server id {cfg.id!r}"
            )
        seen.add(cfg.id)
        out.append(cfg)
    return MCPServersConfig(version=version, servers=out)


# ---------------------------------------------------------------------------
# Result type for bootstrap (forward reference in __init__.py)
# ---------------------------------------------------------------------------


@dataclass
class BootstrapResult:
    """Per-server outcome of :func:`bootstrap.bootstrap_mcp_connectors`."""

    server_id: str
    namespace: str
    enabled: bool
    transport_kind: str
    skipped_reason: Optional[str]
    error: Optional[str]
    tool_count: int
    resource_count: int
    public_tool_names: list[str] = field(default_factory=list)


__all__ = [
    "AuthConfig",
    "BootstrapResult",
    "ConnectorConfigError",
    "HttpTransportConfig",
    "MCPServerConfig",
    "MCPServersConfig",
    "StdioTransportConfig",
    "VAULT_PREFIX",
    "VaultRef",
    "load_mcp_servers_config",
]
