"""Fail-closed external server settings (not required by the local tools CLI)."""
from __future__ import annotations

import os
import re
from dataclasses import dataclass, field

from ..core.config import Config


@dataclass(frozen=True)
class ServerSettings:
    transport: str
    host: str
    port: int
    allowed_hosts: list[str] = field(default_factory=list)
    allowed_origins: list[str] = field(default_factory=list)
    token: str = field(default="", repr=False)
    auth_mode: str = "bearer"  # explicit constructor preserves older embedded callers
    public_origin: str = ""


def require_enabled(config: Config) -> None:
    if config.get("mcp.enabled", False) is not True:
        raise ValueError("MCP server is disabled; the local operator must set mcp.enabled: true")


def server_settings(config: Config, *, transport=None, host=None, port=None) -> ServerSettings:
    require_enabled(config)
    transport = transport if transport is not None else config.get("mcp.transport", "stdio")
    if transport == "http":
        transport = "streamable-http"
    if not isinstance(transport, str) or transport not in {"stdio", "streamable-http"}:
        raise ValueError("mcp.transport must be stdio or streamable-http")
    host = host if host is not None else config.get("mcp.host", "127.0.0.1")
    port = port if port is not None else config.get("mcp.port", 8765)
    if not isinstance(host, str) or not re.fullmatch(r"[A-Za-z0-9_.:\-]+", host):
        raise ValueError("mcp.host must be a host name or IP address, not a URL")
    if type(port) is not int or not 1 <= port <= 65535:
        raise ValueError("mcp.port must be an integer between 1 and 65535")
    hosts = config.get("mcp.allowed_hosts", [])
    origins = config.get("mcp.allowed_origins", [])
    for key, values in (("allowed_hosts", hosts), ("allowed_origins", origins)):
        if not isinstance(values, list) or any(
            not isinstance(v, str) or not v or v == "*" for v in values
        ):
            raise ValueError(f"mcp.{key} must be a list of explicit strings, not a wildcard")
    token = ""
    auth_mode = config.get("mcp.auth_mode", "oauth2")
    if auth_mode not in {"oauth2", "bearer"}:
        raise ValueError("mcp.auth_mode must be oauth2 or bearer")
    if transport == "streamable-http" and auth_mode == "bearer":
        env_name = config.get("mcp.token_env", "NERYA_MCP_TOKEN")
        if not isinstance(env_name, str) or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", env_name):
            raise ValueError("mcp.token_env must name an environment variable")
        token = os.environ.get(env_name, "")
        if len(token) < 32 or not token.isascii() or any(c.isspace() for c in token):
            raise ValueError("HTTP MCP requires a non-whitespace ASCII token of at least 32 characters in mcp.token_env")
        if not hosts:
            if host in {"0.0.0.0", "::"}:
                raise ValueError("Wildcard HTTP binding requires explicit mcp.allowed_hosts")
            authority = f"[{host}]" if ":" in host else host
            hosts = [f"{authority}:{port}"]
    origin = ""
    if transport == "streamable-http" and auth_mode == "oauth2":
        from ..api.auth import has_admin_password
        from .public import public_origin
        if not has_admin_password(config):
            raise ValueError("Set the administrator password before enabling HTTP MCP")
        origin = public_origin(config, fallback=f"http://{host}:{port}")
        from urllib.parse import urlsplit
        authority = f"[{host}]" if ":" in host else host
        hosts = list(dict.fromkeys([*hosts, urlsplit(origin).netloc, f"{authority}:{port}"]))
        origins = list(dict.fromkeys([*origins, origin]))
    return ServerSettings(transport, host, port, list(hosts), list(origins), token, auth_mode, origin)
