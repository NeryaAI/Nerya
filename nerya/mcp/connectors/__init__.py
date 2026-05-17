"""MCP connector configuration + bootstrap.

This package turns a workspace YAML config into live
:class:`MCPSessionAdapter` instances registered on the agent's
:class:`ToolRegistry`. The transport layer (HTTP/stdio) lives in
:mod:`nerya.mcp.transports`; this package owns:

* :mod:`.config`    — schema + loader for ``connectors/mcp_servers.yml``
* :mod:`.seed`      — the default stub written on first agent boot
* :mod:`.bootstrap` — one-call "load config → wire adapters → attach"

Workspace contract:

* ``<workspace>/connectors/mcp_servers.yml``     — operator-edited config
* ``<workspace>/connectors/.oauth_cache.json``   — OAuth token cache
                                                   (auto-managed; safe to ``rm``)

Everything in this package is **side-effect free** until you call
:func:`bootstrap.bootstrap_mcp_connectors` — instantiating config
objects does not open a network connection.
"""

from __future__ import annotations

from .config import (
    AuthConfig,
    BootstrapResult,
    ConnectorConfigError,
    HttpTransportConfig,
    MCPServerConfig,
    MCPServersConfig,
    StdioTransportConfig,
    VaultRef,
    load_mcp_servers_config,
)
from .seed import (
    DEFAULT_MCP_SERVERS_YML,
    SEED_HEADER,
    ensure_mcp_servers_config,
)
from .bootstrap import (
    BootstrapDiagnostics,
    bootstrap_mcp_connectors,
    build_adapter_for_server,
)

__all__ = [
    "AuthConfig",
    "BootstrapDiagnostics",
    "BootstrapResult",
    "ConnectorConfigError",
    "DEFAULT_MCP_SERVERS_YML",
    "HttpTransportConfig",
    "MCPServerConfig",
    "MCPServersConfig",
    "SEED_HEADER",
    "StdioTransportConfig",
    "VaultRef",
    "bootstrap_mcp_connectors",
    "build_adapter_for_server",
    "ensure_mcp_servers_config",
    "load_mcp_servers_config",
]
