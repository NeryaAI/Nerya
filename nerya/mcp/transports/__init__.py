"""MCP transports — concrete :class:`MCPClient` implementations.

USER decision E-6 (locked) ships both HTTP and stdio transports so the
full free-MCP catalogue (sec-edgar, yahoo-finance, fred, fmp, finviz,
polygon, …) can be wired without operator-side code changes.

* :class:`HttpMCPClient` speaks JSON-RPC 2.0 over HTTP/SSE to a remote
  MCP endpoint. Auth modes: ``oauth_client_credentials`` (full flow with
  refresh per USER decision E-3), ``bearer_static`` (vault-stored token),
  or ``none`` (open-tier servers like SEC EDGAR).

* :class:`StdioMCPClient` spawns an MCP server as a subprocess and
  exchanges JSON-RPC frames over its stdin/stdout pipes. Auth is
  passed through environment variables resolved from vault refs.

Both classes implement the :class:`nerya.mcp.session_adapter.MCPClient`
Protocol so :class:`MCPSessionAdapter` can wrap them transparently.

The transports are **lazy on the network**: instantiating one does NOT
open a connection. The adapter calls ``list_tools()`` /
``list_resources()`` at boot, which is the implicit handshake.
"""

from __future__ import annotations

from .oauth import (
    OAuthCredentials,
    OAuthTokenCache,
    OAuthTokenError,
    fetch_client_credentials_token,
)
from .http import HttpMCPClient, HttpTransportError
from .stdio import StdioMCPClient, StdioTransportError

__all__ = [
    "HttpMCPClient",
    "HttpTransportError",
    "OAuthCredentials",
    "OAuthTokenCache",
    "OAuthTokenError",
    "StdioMCPClient",
    "StdioTransportError",
    "fetch_client_credentials_token",
]
