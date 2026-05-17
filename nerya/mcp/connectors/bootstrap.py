"""MCP connector bootstrap — config → live adapter → ToolRegistry.

Single entry point: :func:`bootstrap_mcp_connectors` reads
``<workspace>/connectors/mcp_servers.yml``, builds an
:class:`MCPSessionAdapter` per enabled server, and calls the existing
:func:`nerya.mcp.session_adapter.attach_mcp_adapters` to register
their tools on the agent's :class:`ToolRegistry`.

Per USER decisions:

* E-2: if the YAML file is missing, write the seed stub first.
* E-3: OAuth credentials are minted via :mod:`.transports.oauth`
       with persistent token cache.
* E-4: tool permission posture is whatever
       :class:`MCPSessionAdapter` already enforces (auto_approve for
       readonly), no extra filtering here.

The bootstrap is **fail-soft per server**: a single misconfigured
server does NOT abort the boot. We log the error, mark the entry as
``error=...`` in the diagnostics, and continue with the rest. This
matches the rest of Nerya boot which never refuses to start because
one optional integration is broken.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

from ...core.paths import WorkspacePaths
from ...core.proxy import proxy_env_for_workspace
from ...security.runtime_env import runtime_env_values
from ..lazy import LazyMcpState, attach_lazy_state, install_meta_tools
from ..session_adapter import (
    MCPSessionAdapter,
    attach_mcp_adapters,
    register_external_mcp_resources,
    register_external_mcp_tools,
)
from ..transports import (
    HttpMCPClient,
    OAuthCredentials,
    OAuthTokenCache,
    StdioMCPClient,
)
from .config import (
    AuthConfig,
    BootstrapResult,
    ConnectorConfigError,
    HttpTransportConfig,
    MCPServerConfig,
    StdioTransportConfig,
    VaultRef,
    load_mcp_servers_config,
)
from .seed import ensure_mcp_servers_config


_LOG = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Public diagnostics dataclass
# ---------------------------------------------------------------------------


@dataclass
class BootstrapDiagnostics:
    """Summary of one :func:`bootstrap_mcp_connectors` run."""

    config_path: Path
    seeded_default: bool
    total_declared: int
    total_enabled: int
    results: list[BootstrapResult] = field(default_factory=list)
    #: Lazy-loading state attached to the registry. Tests and the dashboard
    #: read this to confirm meta-tools were installed and which namespaces
    #: are eager vs lazy.
    lazy_state: Optional[LazyMcpState] = None
    meta_tools: list[str] = field(default_factory=list)

    def successes(self) -> list[BootstrapResult]:
        return [r for r in self.results if r.error is None and not r.skipped_reason]

    def failures(self) -> list[BootstrapResult]:
        return [r for r in self.results if r.error is not None]

    def asdict(self) -> dict[str, Any]:
        return {
            "config_path": str(self.config_path),
            "seeded_default": self.seeded_default,
            "total_declared": self.total_declared,
            "total_enabled": self.total_enabled,
            "successes": len(self.successes()),
            "failures": len(self.failures()),
            "meta_tools": list(self.meta_tools),
            "lazy_state": (
                self.lazy_state.snapshot() if self.lazy_state is not None else None
            ),
            "results": [
                {
                    "server_id": r.server_id,
                    "namespace": r.namespace,
                    "enabled": r.enabled,
                    "transport_kind": r.transport_kind,
                    "skipped_reason": r.skipped_reason,
                    "error": r.error,
                    "tool_count": r.tool_count,
                    "resource_count": r.resource_count,
                    "public_tool_names": list(r.public_tool_names),
                }
                for r in self.results
            ],
        }


# ---------------------------------------------------------------------------
# Vault resolver — abstracts away SecretVault for testability
# ---------------------------------------------------------------------------


class VaultResolver:
    """Minimal protocol — anything with a ``resolve(name) -> str`` works.

    The default implementation wraps :class:`SecretVault.open` lazily so
    the bootstrap doesn't crash if no vault file exists yet (operator
    hasn't put any secrets) — only servers that *need* a secret will
    fail.
    """

    def __init__(self, *, paths: WorkspacePaths, passphrase: Optional[str] = None):
        self._paths = paths
        self._passphrase = passphrase
        self._vault: Any = None
        self._loaded = False

    def _ensure_vault(self) -> Any:
        if self._loaded:
            return self._vault
        self._loaded = True
        # Defer import: SecretVault has its own crypto deps; we only
        # need it when a server config actually references a vault://.
        from ...security.secrets import SecretVault

        try:
            self._vault = SecretVault.open(
                self._paths.vault_enc, passphrase=self._passphrase,
            )
        except Exception as exc:
            _LOG.debug("VaultResolver: SecretVault.open failed: %s", exc)
            self._vault = None
        return self._vault

    def resolve(self, ref: VaultRef, *, scope: str = "mcp.read") -> str:
        vault = self._ensure_vault()
        if vault is None:
            raise ConnectorConfigError(
                f"vault unavailable when resolving {ref.as_str()}"
            )
        try:
            return vault.resolve(ref.name, required_scope=scope)
        except Exception as exc:
            raise ConnectorConfigError(
                f"vault resolve failed for {ref.as_str()}: {exc}"
            ) from exc


# ---------------------------------------------------------------------------
# Build adapters
# ---------------------------------------------------------------------------


def _resolve_oauth_creds(
    auth: AuthConfig, *, vault: VaultResolver,
) -> OAuthCredentials:
    if auth.kind != "oauth_client_credentials":
        raise ConnectorConfigError("auth kind is not oauth_client_credentials")

    client_id = auth.client_id
    if not client_id and auth.client_id_ref is not None:
        client_id = vault.resolve(auth.client_id_ref, scope="mcp.read")
    assert client_id is not None  # validated by AuthConfig.from_raw

    if auth.client_secret_ref is None:  # pragma: no cover - validated
        raise ConnectorConfigError("oauth missing client_secret_ref")
    client_secret = vault.resolve(auth.client_secret_ref, scope="mcp.read")

    token_url = auth.token_url
    if not token_url and auth.token_url_ref is not None:
        token_url = vault.resolve(auth.token_url_ref, scope="mcp.read")
    assert token_url is not None  # validated

    return OAuthCredentials(
        token_url=token_url,
        client_id=client_id,
        client_secret=client_secret,
        scope=auth.scope,
        audience=auth.audience,
    )


def _build_http_client(
    cfg: MCPServerConfig,
    *,
    vault: VaultResolver,
    token_cache: OAuthTokenCache,
) -> HttpMCPClient:
    assert isinstance(cfg.transport, HttpTransportConfig)
    transport = cfg.transport
    auth = cfg.auth

    kwargs: dict[str, Any] = {
        "server_id": cfg.id,
        "url": transport.url,
        "auth_kind": auth.kind,
        "extra_headers": dict(transport.extra_headers),
        "timeout_seconds": transport.timeout_seconds,
        "auto_initialize": transport.auto_initialize,
    }

    if auth.kind == "bearer_static":
        assert auth.token_ref is not None
        kwargs["static_bearer"] = vault.resolve(auth.token_ref, scope="mcp.read")
    elif auth.kind == "oauth_client_credentials":
        kwargs["oauth"] = _resolve_oauth_creds(auth, vault=vault)
        kwargs["token_cache"] = token_cache

    return HttpMCPClient(**kwargs)


def _build_stdio_client(
    cfg: MCPServerConfig,
    *,
    vault: VaultResolver,
) -> StdioMCPClient:
    assert isinstance(cfg.transport, StdioTransportConfig)
    transport = cfg.transport
    auth = cfg.auth

    # Plain env defaults first (e.g. SEC_EDGAR_USER_AGENT identifier),
    # then overlay vault-resolved env_refs so a real secret always wins
    # over a baked-in default identifier.
    env: dict[str, str] = {}
    try:
        env.update(runtime_env_values(vault._paths))
    except Exception as exc:
        _LOG.debug("stdio server %s: runtime env overlay unavailable: %s", cfg.id, exc)
    env.update(transport.env)
    for env_name, ref in transport.env_refs.items():
        env[env_name] = vault.resolve(ref, scope="mcp.read")
    try:
        env.update(proxy_env_for_workspace(vault._paths))
    except Exception as exc:
        _LOG.debug("stdio server %s: proxy env overlay unavailable: %s", cfg.id, exc)

    if auth.kind == "bearer_static":
        # bearer_static on a stdio transport is a contract violation
        # — stdio servers don't see Authorization headers. We keep it
        # supported for symmetry but the operator is almost certainly
        # better off using env_refs. Emit a debug log and pass via
        # MCP_BEARER_TOKEN env so a custom transport can read it.
        assert auth.token_ref is not None
        _LOG.debug(
            "stdio server %s: bearer_static -> MCP_BEARER_TOKEN env var "
            "(consider using transport.env_refs instead)",
            cfg.id,
        )
        env.setdefault("MCP_BEARER_TOKEN", vault.resolve(auth.token_ref, scope="mcp.read"))
    elif auth.kind == "oauth_client_credentials":
        # Same caveat as above — stdio servers don't OAuth at the
        # transport level. We refuse loudly to avoid silently doing
        # the wrong thing.
        raise ConnectorConfigError(
            f"server {cfg.id!r}: oauth_client_credentials not supported "
            f"on stdio transport (use env_refs to pass the token)"
        )

    return StdioMCPClient(
        server_id=cfg.id,
        command=list(transport.command),
        env=env,
        cwd=transport.cwd,
        startup_timeout=transport.startup_timeout,
        read_timeout=transport.read_timeout,
    )


def build_adapter_for_server(
    cfg: MCPServerConfig,
    *,
    vault: VaultResolver,
    token_cache: OAuthTokenCache,
) -> MCPSessionAdapter:
    """Build one :class:`MCPSessionAdapter` from a validated server config.

    Public so tests + the CLI ``doctor`` subcommand can construct an
    adapter without going through the full bootstrap loop.
    """

    if isinstance(cfg.transport, HttpTransportConfig):
        client = _build_http_client(cfg, vault=vault, token_cache=token_cache)
    elif isinstance(cfg.transport, StdioTransportConfig):
        client = _build_stdio_client(cfg, vault=vault)
    else:  # pragma: no cover - exhaustive
        raise ConnectorConfigError(
            f"server {cfg.id!r}: unsupported transport type {type(cfg.transport).__name__}"
        )

    return MCPSessionAdapter(client=client, server_id=cfg.namespace)


# ---------------------------------------------------------------------------
# Top-level bootstrap
# ---------------------------------------------------------------------------


def bootstrap_mcp_connectors(
    *,
    paths: WorkspacePaths,
    registry: Any,
    executor: Any = None,
    resource_index: Any = None,
    vault_passphrase: Optional[str] = None,
    auto_seed: bool = True,
    only_enabled: bool = True,
    extra_config_path: Optional[Path] = None,
    _vault: Optional[VaultResolver] = None,
    _adapter_factory: Optional[Any] = None,
) -> BootstrapDiagnostics:
    """Read config → build adapters → attach tools onto ``registry``.

    Args:
        paths: workspace path layout (provides ``connectors_mcp_servers``
            and ``connectors_oauth_cache``).
        registry: the live :class:`ToolRegistry` the agent loop drives.
        executor: optional :class:`NativeToolExecutor` — if provided,
            the post-tool MCP redaction hook is installed on it.
        resource_index: optional :class:`ResourceIndex` — if provided,
            MCP server resources also land in the index.
        vault_passphrase: passphrase for opening the SecretVault. If
            None, defaults to the env var the SecretVault itself reads.
        auto_seed: if True (USER E-5 default) and the config file is
            missing, the seed stub is written first. Tests pass False
            to assert "operator must opt-in".
        only_enabled: if True (default), only servers with
            ``enabled: true`` are bootstrapped. Tests / CLI doctor pass
            False to materialise *all* declared servers.
        extra_config_path: override the resolved YAML path (used by the
            CLI's ``--workspace`` flag).
        _vault, _adapter_factory: test injection points.

    Returns a :class:`BootstrapDiagnostics` summary; never raises for
    per-server failures (those land as ``error=...`` rows).
    """

    config_path = extra_config_path or paths.connectors_mcp_servers
    seeded = False
    if auto_seed:
        seeded = ensure_mcp_servers_config(config_path)

    cfg_doc = load_mcp_servers_config(config_path)

    diagnostics = BootstrapDiagnostics(
        config_path=config_path,
        seeded_default=seeded,
        total_declared=len(cfg_doc.servers),
        total_enabled=len(cfg_doc.enabled_servers()),
    )

    vault = _vault if _vault is not None else VaultResolver(
        paths=paths, passphrase=vault_passphrase,
    )
    token_cache = OAuthTokenCache(cache_path=paths.connectors_oauth_cache)

    adapters_to_attach: list[MCPSessionAdapter] = []
    # Derive each adapter's lazy flag from the server config.
    # ``adapter.server_id`` is set to ``cfg.namespace`` (see
    # ``build_adapter_for_server``), so we keep the mapping in
    # namespace-keyed form to match ``adapter.server_id`` later.
    namespace_lazy: dict[str, bool] = {}
    namespace_to_server_id: dict[str, str] = {}
    # Per-adapter overlap filters keyed by adapter.server_id
    # (== cfg.namespace). Forwarded to attach_mcp_adapters /
    # register_external_mcp_tools so denied tools never enter the
    # registry. Empty/None entries are no-ops.
    namespace_deny_tools: dict[str, tuple[str, ...]] = {}
    namespace_allow_tools: dict[str, tuple[str, ...]] = {}
    pending: list[BootstrapResult] = []

    for server_cfg in cfg_doc.servers:
        if only_enabled and not server_cfg.enabled:
            pending.append(
                BootstrapResult(
                    server_id=server_cfg.id,
                    namespace=server_cfg.namespace,
                    enabled=False,
                    transport_kind=_transport_kind(server_cfg),
                    skipped_reason="not enabled",
                    error=None,
                    tool_count=0,
                    resource_count=0,
                )
            )
            continue

        try:
            if _adapter_factory is not None:
                adapter = _adapter_factory(server_cfg, vault=vault, token_cache=token_cache)
            else:
                adapter = build_adapter_for_server(
                    server_cfg, vault=vault, token_cache=token_cache,
                )
        except Exception as exc:
            _LOG.warning(
                "mcp bootstrap: %s build failed: %s", server_cfg.id, exc,
            )
            pending.append(
                BootstrapResult(
                    server_id=server_cfg.id,
                    namespace=server_cfg.namespace,
                    enabled=server_cfg.enabled,
                    transport_kind=_transport_kind(server_cfg),
                    skipped_reason=None,
                    error=f"build_adapter: {exc}",
                    tool_count=0,
                    resource_count=0,
                )
            )
            continue

        adapters_to_attach.append(adapter)
        # Adapter.server_id == cfg.namespace (see build_adapter_for_server).
        namespace_lazy[adapter.server_id] = not server_cfg.always_eager
        namespace_to_server_id[adapter.server_id] = server_cfg.id
        if server_cfg.deny_tools:
            namespace_deny_tools[adapter.server_id] = tuple(server_cfg.deny_tools)
        if server_cfg.allow_tools is not None:
            namespace_allow_tools[adapter.server_id] = tuple(server_cfg.allow_tools)
        pending.append(
            BootstrapResult(
                server_id=server_cfg.id,
                namespace=server_cfg.namespace,
                enabled=server_cfg.enabled,
                transport_kind=_transport_kind(server_cfg),
                skipped_reason=None,
                error=None,
                tool_count=0,
                resource_count=0,
            )
        )

    # Attach in one call so the post-tool hook is installed exactly once
    # per executor (existing attach_mcp_adapters semantics).
    if adapters_to_attach and executor is not None:
        lazy_servers = {
            ns for ns, is_lazy in namespace_lazy.items() if is_lazy
        }
        try:
            tool_map = attach_mcp_adapters(
                registry=registry,
                executor=executor,
                adapters=adapters_to_attach,
                resource_index=resource_index,
                replace=True,
                lazy_servers=lazy_servers,
                deny_tools_by_server=namespace_deny_tools or None,
                allow_tools_by_server=namespace_allow_tools or None,
            )
        except Exception as exc:
            _LOG.exception("mcp bootstrap: attach_mcp_adapters failed: %s", exc)
            tool_map = {}
    else:
        # No executor — register tools (and resources) per adapter
        # directly. This is the path tests + CLI take.
        tool_map: dict[str, list[str]] = {}
        for adapter in adapters_to_attach:
            try:
                names = register_external_mcp_tools(
                    registry=registry,
                    adapter=adapter,
                    replace=True,
                    lazy=namespace_lazy.get(adapter.server_id, True),
                    deny_tools=namespace_deny_tools.get(adapter.server_id, ()),
                    allow_tools=namespace_allow_tools.get(adapter.server_id),
                )
            except Exception as exc:
                _LOG.warning(
                    "mcp bootstrap: register tools failed for %s: %s",
                    adapter.server_id, exc,
                )
                names = []
            if resource_index is not None:
                try:
                    register_external_mcp_resources(
                        index=resource_index, adapter=adapter,
                    )
                except Exception:
                    _LOG.exception(
                        "mcp bootstrap: register resources failed for %s",
                        adapter.server_id,
                    )
            tool_map[adapter.server_id] = list(names)

    # Backfill counts onto the diagnostics rows.
    for row in pending:
        if row.skipped_reason or row.error:
            continue
        names = tool_map.get(row.namespace, [])
        row.tool_count = len(names)
        row.public_tool_names = list(names)
    diagnostics.results = pending

    # Build the per-session lazy state from what actually registered,
    # attach it to the registry, and install the 3 eager meta-tools so
    # the model has a stable surface for discovering / describing /
    # dispatching MCP tools without paying the full
    # eager-registration context cost.
    if tool_map:
        state = LazyMcpState()
        for namespace, names in tool_map.items():
            if not names:
                continue
            is_lazy = namespace_lazy.get(namespace, True)
            state.register_namespace(
                namespace, names, always_eager=not is_lazy,
            )
        state = attach_lazy_state(registry, state)
        try:
            meta_names = install_meta_tools(
                registry=registry, state=state, replace=True,
            )
        except Exception as exc:
            _LOG.exception("mcp bootstrap: install_meta_tools failed: %s", exc)
            meta_names = []
        diagnostics.lazy_state = state
        diagnostics.meta_tools = list(meta_names)

    return diagnostics


def _transport_kind(cfg: MCPServerConfig) -> str:
    if isinstance(cfg.transport, HttpTransportConfig):
        return "http"
    if isinstance(cfg.transport, StdioTransportConfig):
        return "stdio"
    return "unknown"  # pragma: no cover


__all__ = [
    "BootstrapDiagnostics",
    "VaultResolver",
    "bootstrap_mcp_connectors",
    "build_adapter_for_server",
]
