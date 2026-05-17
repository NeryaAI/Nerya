"""Dashboard runtime endpoint configuration."""

from __future__ import annotations

from typing import Any

from . import yaml_io

DEFAULT_DASHBOARD_HOST = "127.0.0.1"
DEFAULT_DASHBOARD_PORT = 18380


def _port(value: Any, default: int = DEFAULT_DASHBOARD_PORT) -> int:
    try:
        port = int(value)
    except Exception:
        return default
    if 1 <= port <= 65535:
        return port
    return default


def dashboard_host(config: Any) -> str:
    if hasattr(config, "get"):
        value = config.get("dashboard.host", DEFAULT_DASHBOARD_HOST)
    else:
        value = DEFAULT_DASHBOARD_HOST
    host = str(value or "").strip()
    return host or DEFAULT_DASHBOARD_HOST


def dashboard_port(config: Any) -> int:
    if hasattr(config, "get"):
        return _port(config.get("dashboard.port", DEFAULT_DASHBOARD_PORT))
    return DEFAULT_DASHBOARD_PORT


def dashboard_url(config: Any) -> str:
    return f"http://{dashboard_host(config)}:{dashboard_port(config)}"


def public_dashboard_config(config: Any) -> dict[str, Any]:
    return {
        "ok": True,
        "config": {
            "host": dashboard_host(config),
            "port": dashboard_port(config),
            "url": dashboard_url(config),
        },
    }


def save_dashboard_config(config: Any, payload: dict[str, Any]) -> dict[str, Any]:
    host = str((payload or {}).get("host") or dashboard_host(config)).strip()
    if not host:
        host = DEFAULT_DASHBOARD_HOST
    port = _port((payload or {}).get("port"), dashboard_port(config))

    paths = getattr(config, "paths", None)
    if paths is None:
        raise ValueError("config.paths is required")
    existing = yaml_io.load(paths.config, default={}) or {}
    if not isinstance(existing, dict):
        existing = {}
    dashboard = existing.setdefault("dashboard", {})
    if not isinstance(dashboard, dict):
        dashboard = {}
        existing["dashboard"] = dashboard
    dashboard["host"] = host
    dashboard["port"] = port
    yaml_io.dump(paths.config, existing)

    data = getattr(config, "data", None)
    if isinstance(data, dict):
        data_dashboard = data.setdefault("dashboard", {})
        if not isinstance(data_dashboard, dict):
            data_dashboard = {}
            data["dashboard"] = data_dashboard
        data_dashboard["host"] = host
        data_dashboard["port"] = port

    return public_dashboard_config(config)
