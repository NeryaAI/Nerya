"""Administrator-only MCP settings. Never part of the external tool catalog."""
from __future__ import annotations

import hashlib
import importlib.util
import json
import secrets
import threading
from copy import deepcopy

from ..core import yaml_io
from ..core.config import Config, DEFAULT_CONFIG, _merge, load_config
from ..mcp import openai_tunnel
from ..mcp.catalog import validate_exposure_config
from ..mcp.public import public_origin, tunnel_origins, validate_origin
from .auth import has_admin_password

_LOCK = threading.RLock()


def _config(client):
    return load_config(client.config.paths.root) if client.config.paths.config.exists() else client.config


def _revision(config):
    return hashlib.sha256(json.dumps(config.get("mcp", {}), sort_keys=True).encode()).hexdigest()


def status(client, payload=None):
    config = _config(client)
    mcp = config.get("mcp", {}) or {}
    origin = public_origin(config)
    installed = importlib.util.find_spec("mcp") is not None
    return {"ok": True, "revision": _revision(config), "enabled": mcp.get("enabled") is True,
            "auth_mode": mcp.get("auth_mode", "oauth2"), "sdk_installed": installed,
            "admin_password_configured": has_admin_password(config),
            "public_url": mcp.get("public_url", ""), "endpoint": origin + "/mcp",
            "oauth_issuer": origin + "/mcp/oauth", "detected_public_urls": tunnel_origins(config),
            "allow_mutating": mcp.get("allow_mutating", False),
            "native_allow_mutating": config.get("mcp.native_tools.allow_mutating", False),
            "allow_tools": mcp.get("allow_tools"), "deny_tools": mcp.get("deny_tools", []),
            "openai_tunnel": openai_tunnel.status(config)}


def save(client, payload):
    allowed = {"revision", "enabled", "public_url", "allow_mutating", "native_allow_mutating",
               "allow_tools", "deny_tools", "openai_tunnel"}
    if not isinstance(payload, dict) or set(payload) - allowed:
        return {"ok": False, "error": "unsupported_settings", "_status": 400}
    with _LOCK:
        config = _config(client)
        if payload.get("revision") != _revision(config):
            return {"ok": False, "error": "stale_revision", "_status": 409}
        block = deepcopy(config.get("mcp", {}))
        try:
            for key in ("enabled", "allow_mutating", "native_allow_mutating"):
                if key in payload and type(payload[key]) is not bool:
                    raise ValueError(f"{key} must be a boolean")
            for key in ("enabled", "allow_mutating", "allow_tools", "deny_tools"):
                if key in payload:
                    block[key] = payload[key]
            if "native_allow_mutating" in payload:
                block.setdefault("native_tools", {})["allow_mutating"] = payload["native_allow_mutating"]
                block["native_tools"]["mode"] = "auto" if payload["native_allow_mutating"] else "default"
            # Settings UI only opens OAuth2, never an unauthenticated or static-token public server.
            block["auth_mode"] = "oauth2"
            if "public_url" in payload:
                value = str(payload["public_url"] or "").strip()
                block["public_url"] = validate_origin(value) if value else ""
            if block.get("enabled") and not has_admin_password(config):
                raise ValueError("Configure the administrator password in Access settings first")
            if block.get("enabled") and not importlib.util.find_spec("mcp"):
                raise ValueError("MCP dependency missing; install nerya[mcp] in the running backend environment")
            tunnel = dict(block.get("openai_tunnel", {}))
            requested = payload.get("openai_tunnel", {})
            if not isinstance(requested, dict) or set(requested) - {"enabled", "tunnel_id", "api_key", "clear_api_key"}:
                raise ValueError("Invalid OpenAI Tunnel settings")
            if "enabled" in requested:
                if type(requested["enabled"]) is not bool:
                    raise ValueError("Tunnel enabled must be a boolean")
                tunnel["enabled"] = requested["enabled"]
            if "tunnel_id" in requested:
                import re
                value = str(requested["tunnel_id"]).strip()
                if value and not re.fullmatch(r"tunnel_[A-Za-z0-9_-]{8,128}", value):
                    raise ValueError("Tunnel ID must start with tunnel_")
                tunnel["tunnel_id"] = value
            api_key = requested.get("api_key", "")
            if not isinstance(api_key, str) or len(api_key) > 4096:
                raise ValueError("Invalid API key")
            if requested.get("clear_api_key") is True:
                tunnel["api_key_ref"] = ""  # disconnect; do not erase a possibly shared Vault secret
            if not block.get("enabled"):
                tunnel["enabled"] = False
            block["openai_tunnel"] = tunnel
            candidate = Config(paths=config.paths, data={**config.data, "mcp": block})
            validate_exposure_config(candidate)
            if tunnel.get("enabled") and (not block.get("enabled") or not tunnel.get("tunnel_id")):
                raise ValueError("OpenAI Tunnel requires MCP enabled and a Tunnel ID")
            if tunnel.get("enabled") and not (api_key or tunnel.get("api_key_ref")):
                raise ValueError("OpenAI Tunnel requires a runtime API key")
            if api_key:
                from ..security.secrets import SecretVault
                try:
                    meta = SecretVault.open(config.paths.vault_enc).put(name="mcp_openai_runtime_key",
                        value=api_key, kind="openai_tunnel", scope=["mcp_tunnel"], owner="mcp")
                except Exception as exc:
                    raise ValueError("Vault cannot store the API key; configure the Vault passphrase first") from exc
                tunnel["api_key_ref"] = meta.ref()
            # Changing exposure invalidates existing grants; clients must consent again.
            block["oauth_epoch"] = secrets.token_hex(16)
            raw = yaml_io.load(config.paths.config, default={}) or {}
            raw["mcp"] = block
            yaml_io.dump(config.paths.config, raw)
            client.config.data["mcp"] = _merge(DEFAULT_CONFIG["mcp"], block)
        except ValueError as exc:
            return {"ok": False, "error": str(exc), "_status": 400}
    from ..mcp.bridge import close_workspace
    close_workspace(config.paths.root)
    openai_tunnel.stop(config)
    return status(client)


def revoke(client, payload):
    with _LOCK:
        config = _config(client)
        if payload.get("revision") != _revision(config):
            return {"ok": False, "error": "stale_revision", "_status": 409}
        raw = yaml_io.load(config.paths.config, default={}) or {}
        raw.setdefault("mcp", {})["oauth_epoch"] = secrets.token_hex(16)
        yaml_io.dump(config.paths.config, raw)
        client.config.data.setdefault("mcp", {})["oauth_epoch"] = raw["mcp"]["oauth_epoch"]
    return status(client)


def tunnel_start(client, payload):
    return openai_tunnel.start(_config(client))


def tunnel_stop(client, payload):
    # Persist the operator's stop so backend restart does not reconnect unexpectedly.
    result = save(client, {"revision": payload.get("revision"), "openai_tunnel": {"enabled": False}})
    return result if not result.get("ok") else {"ok": True, **openai_tunnel.status(_config(client))}


def roles(client, payload):
    from ..subagents.registry import list_roles
    return {"ok": True, "roles": list_roles(client.config.paths)}


def routes():
    return [("GET", "/mcp-settings", status), ("POST", "/mcp-settings", save),
            ("POST", "/mcp-settings/revoke", revoke), ("GET", "/mcp-settings/roles", roles),
            ("POST", "/mcp-settings/tunnel/start", tunnel_start),
            ("POST", "/mcp-settings/tunnel/stop", tunnel_stop)]
