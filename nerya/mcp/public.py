"""Trusted MCP public origins, shared by OAuth, the settings UI and tunnels."""
from __future__ import annotations

from urllib.parse import urlsplit

PUBLIC_PATHS = frozenset({
    "/mcp", "/mcp/oauth/authorize", "/mcp/oauth/token", "/mcp/oauth/register",
    "/mcp/oauth/revoke", "/mcp/oauth/login", "/.well-known/oauth-authorization-server",
    "/.well-known/oauth-authorization-server/mcp/oauth",
    "/.well-known/oauth-protected-resource/mcp",
})


def validate_origin(value: str) -> str:
    parts = urlsplit(value)
    if (not parts.hostname or parts.username or parts.password or parts.query or parts.fragment
            or parts.path not in {"", "/"} or any(c.isspace() for c in value)
            or not (parts.scheme == "https" or (parts.scheme == "http" and
                     parts.hostname in {"127.0.0.1", "localhost", "::1"}))):
        raise ValueError("Public URL must be an HTTPS origin without a path (loopback HTTP is allowed)")
    return value.rstrip("/")


def tunnel_origins(config) -> list[str]:
    from ..core.tunnels import PROVIDERS, _load_state, _is_pid_running, _provider_log_urls
    result = []
    for provider in PROVIDERS:
        cfg = config.get(f"network.tunnels.providers.{provider}", {}) or {}
        if not cfg.get("enabled") or cfg.get("target", "dashboard") not in {"dashboard", "api"}:
            continue
        state = _load_state(config.paths, provider)
        running = bool(state.get("started_at")) if provider == "tailscale" else _is_pid_running(state.get("pid"))
        if not running:
            continue
        urls = state.get("external_urls", [])
        if not urls and provider == "cloudflare":
            urls = _provider_log_urls(config.paths, provider)
        if not urls and cfg.get("public_hostname"):
            name = cfg["public_hostname"]
            urls = [name if name.startswith("https://") else "https://" + name]
        for value in urls:
            try:
                origin = validate_origin(str(value))
                if origin not in result:
                    result.append(origin)
            except ValueError:
                continue
    return result


def public_origin(config, *, fallback: str | None = None) -> str:
    configured = str(config.get("mcp.public_url", "") or "").strip()
    if configured:
        return validate_origin(configured)
    detected = tunnel_origins(config)
    if detected:
        return detected[0]
    if fallback:
        return validate_origin(fallback)
    from ..core.dashboard import dashboard_url
    return validate_origin(dashboard_url(config))
