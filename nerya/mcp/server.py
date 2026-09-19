"""Optional MCP transports over the same catalog consumed by ``nerya tools``.

Uses the official SDK's low-level API to preserve authoritative JSON schemas.
Importing this module or using the local CLI does not require the MCP extra.
"""
from __future__ import annotations

import json
import logging
import secrets
import sys
from contextlib import asynccontextmanager, redirect_stdout
from pathlib import Path
from typing import Any

from ..core.config import load_config
from ..sdk.internal_client import InternalClient
from .catalog import ToolCatalog, build_catalog, is_error
from .dynamic_tools import DynamicMCPRegistry, MCPPolicy, policy_from_config
from .settings import ServerSettings, require_enabled, server_settings
from .tools import NeryaTools


_INSTALL_HINT = "MCP SDK is optional; install it with: pip install 'nerya[mcp]' (or pip install -e '.[mcp]')"


def create_server(tools: NeryaTools | None = None, *, workspace: str | Path | None = None,
                  profile: str | None = None, catalog: ToolCatalog | None = None,
                  dynamic_policy=None, native_policy=None, include_legacy=None,
                  include_dynamic=None, include_native=None):
    config = tools.client.config if tools else load_config(workspace, profile=profile)
    require_enabled(config)
    try:
        import anyio
        from mcp.server.lowlevel import Server
        from mcp import types
    except ImportError as exc:
        raise RuntimeError(_INSTALL_HINT) from exc
    with redirect_stdout(sys.stderr):
        if catalog is None:
            tools = tools or NeryaTools(InternalClient.from_config(config))
            catalog = build_catalog(tools, dynamic_policy=dynamic_policy,
                                    native_policy=native_policy, include_legacy=include_legacy,
                                    include_dynamic=include_dynamic, include_native=include_native)
    server = Server("nerya", instructions=config.get("mcp.instructions") or (
        "Discover tools first and use their inputSchema verbatim. Results are in "
        "structuredContent; isError marks failures. Strategy/config proposals do "
        "not activate changes. Approval, credentials and live trading remain "
        "operator-controlled. Never retry a mutation blindly after a timeout."
    ))

    @server.list_tools()
    async def list_tools():
        return [types.Tool(**row) for row in catalog.list_tools()]

    # Shared validation provides identical CLI/MCP errors without echoing secrets.
    @server.call_tool(validate_input=False)
    async def call_tool(name: str, arguments: dict[str, Any] | None):
        result = await anyio.to_thread.run_sync(catalog.call, name, arguments)
        error = is_error(result)
        return types.CallToolResult(
            content=[types.TextContent(type="text", text=json.dumps(result, ensure_ascii=False, default=str))],
            structuredContent=result, isError=error,
        )

    return server


def build_dynamic_registry(tools: NeryaTools | None = None, *, workspace=None,
                           policy: MCPPolicy | None = None) -> DynamicMCPRegistry:
    """Retain the SDK-free capability-matrix entry point."""
    nerya = tools or NeryaTools.boot(workspace)
    return DynamicMCPRegistry.build(nerya.client, policy=policy or policy_from_config(nerya.client.config))


class _BearerAuth:
    """Authenticate every HTTP request before parsing MCP or starting a session."""
    def __init__(self, app, token: str):
        self.app = app
        self._expected = ("Bearer " + token).encode("ascii")

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http":
            values = [v for k, v in scope.get("headers", []) if k.lower() == b"authorization"]
            if len(values) != 1 or not secrets.compare_digest(values[0], self._expected):
                from starlette.responses import JSONResponse
                response = JSONResponse({"error": "unauthorized"}, status_code=401,
                                        headers={"WWW-Authenticate": "Bearer", "Cache-Control": "no-store"})
                await response(scope, receive, send)
                return
        await self.app(scope, receive, send)


def create_http_app(server, settings: ServerSettings, *, config=None):
    if (settings.auth_mode == "bearer" and not settings.token) or not settings.allowed_hosts:
        raise ValueError("HTTP MCP requires authentication and explicit allowed hosts")
    from mcp.server.streamable_http_manager import StreamableHTTPSessionManager
    from mcp.server.transport_security import TransportSecuritySettings
    from starlette.applications import Starlette
    from starlette.routing import Route

    manager = StreamableHTTPSessionManager(
        app=server, json_response=True, stateless=True,
        security_settings=TransportSecuritySettings(
            enable_dns_rebinding_protection=True,
            allowed_hosts=settings.allowed_hosts, allowed_origins=settings.allowed_origins,
        ),
    )

    async def endpoint(scope, receive, send):
        await manager.handle_request(scope, receive, send)

    @asynccontextmanager
    async def lifespan(app):
        async with manager.run():
            yield

    # ASGI callable instance avoids Starlette treating a function as Request -> Response.
    class Endpoint:
        async def __call__(self, scope, receive, send):
            await endpoint(scope, receive, send)

    routes = [Route("/mcp", Endpoint(), methods=["GET", "POST", "DELETE"])]
    provider = None
    if settings.auth_mode == "oauth2":
        if config is None:
            raise ValueError("OAuth2 MCP requires workspace configuration")
        from .oauth import AdminOAuthProvider
        provider = AdminOAuthProvider(config, settings.public_origin)
        routes.extend(provider.routes())
    app = Starlette(routes=routes, lifespan=lifespan)
    if provider is not None:
        from .oauth import OAuthBoundary
        boundary = OAuthBoundary(app, provider)
    else:
        boundary = _BearerAuth(app, settings.token)
    boundary.lifespan_app = app
    return boundary


def serve(workspace: str | Path | None = None, *, profile: str | None = None,
          verbose: bool = False, transport: str | None = None,
          host: str | None = None, port: int | None = None) -> None:
    logging.basicConfig(level=logging.DEBUG if verbose else logging.WARNING, stream=sys.stderr)
    with redirect_stdout(sys.stderr):
        config = load_config(workspace, profile=profile)
        settings = server_settings(config, transport=transport, host=host, port=port)
        # Gate and validate before booting a skill kernel or opening a socket.
        try:
            import anyio
            from mcp.server.stdio import stdio_server
        except ImportError as exc:
            raise RuntimeError(_INSTALL_HINT) from exc
        server = create_server(NeryaTools(InternalClient.from_config(config)))
    if settings.transport == "streamable-http":
        import uvicorn
        uvicorn.run(create_http_app(server, settings, config=config), host=settings.host, port=settings.port,
                    access_log=False, log_level="debug" if verbose else "warning")
        return

    async def run_stdio():
        # Capture the actual stdout transport first; all incidental prints go to stderr.
        async with stdio_server() as (reader, writer):
            with redirect_stdout(sys.stderr):
                await server.run(reader, writer, server.create_initialization_options())

    anyio.run(run_stdio)
