"""Supervise the official OpenAI tunnel-client, not a custom tunnel protocol.

Only runtime API keys are passed via child environment. We do not mint or inject
MCP OAuth tokens: OpenAI still forwards the end user's OAuth authorization.
"""
from __future__ import annotations

import importlib.util
import os
import re
import shutil
import subprocess
import threading
import uuid
from pathlib import Path
from urllib.parse import urlsplit

from .public import public_origin

_PROCESSES = {}
_PORTS = {}
_LOCK = threading.RLock()
_ERRORS = {}


def executable():
    found = shutil.which("tunnel-client")
    if found:
        return found
    for path in (Path("/opt/homebrew/bin/tunnel-client"), Path("/usr/local/bin/tunnel-client")):
        if path.is_file() and os.access(path, os.X_OK):
            return str(path)
    return None


def status(config):
    key = str(config.paths.root.resolve())
    with _LOCK:
        item = _PROCESSES.get(key)
    running = bool(item and item[0].poll() is None)
    ready = False
    if running and item[1].exists() and not item[1].is_symlink():
        try:
            with item[1].open(encoding="utf-8") as stream:
                url = stream.read(1024).strip()
            parts = urlsplit(url)
            if parts.scheme == "http" and parts.hostname == "127.0.0.1" and parts.port:
                import httpx
                with httpx.Client(trust_env=False, timeout=1) as client:
                    ready = client.get(f"http://127.0.0.1:{parts.port}/readyz").status_code == 200
        except Exception:
            pass
    cfg = config.get("mcp.openai_tunnel", {}) or {}
    return {"installed": bool(executable()), "enabled": cfg.get("enabled") is True,
            "tunnel_id": str(cfg.get("tunnel_id", "")), "api_key_configured": bool(cfg.get("api_key_ref")),
            "running": running, "ready": ready,
            "error": _ERRORS.get(key, "") if not running else "",
            "install_command": "brew install openai/tools/tunnel-client",
            "api_tool": {"type": "mcp", "server_label": "nerya", "tunnel_id": str(cfg.get("tunnel_id", ""))}}


def start(config, *, api_port=None):
    from ..api.auth import has_admin_password
    key = str(config.paths.root.resolve())
    cfg = config.get("mcp.openai_tunnel", {}) or {}
    with _LOCK:
        if key in _PROCESSES and _PROCESSES[key][0].poll() is None:
            return {"ok": True, **status(config)}
        try:
            if config.get("mcp.enabled") is not True or cfg.get("enabled") is not True:
                raise ValueError("Enable MCP and OpenAI Tunnel first")
            if config.get("mcp.auth_mode", "oauth2") != "oauth2" or not has_admin_password(config):
                raise ValueError("OAuth2 and the administrator password are required")
            if not importlib.util.find_spec("mcp"):
                raise ValueError("Install the nerya[mcp] dependency first")
            origin = public_origin(config)
            if not origin.startswith("https://"):
                raise ValueError("OAuth needs a browser-reachable HTTPS public URL; configure the Nerya public tunnel first")
            tunnel_id = str(cfg.get("tunnel_id", ""))
            if not re.fullmatch(r"tunnel_[A-Za-z0-9_-]{8,128}", tunnel_id):
                raise ValueError("Enter a valid OpenAI Tunnel ID (tunnel_...)")
            binary = executable()
            if not binary:
                raise ValueError("Official tunnel-client is not installed")
            ref = str(cfg.get("api_key_ref", ""))
            if not ref.startswith("vault://"):
                raise ValueError("Save the runtime API key in MCP settings first")
            from ..security.secrets import SecretVault
            api_key = SecretVault.open(config.paths.vault_enc).resolve(ref[8:], required_scope="mcp_tunnel")
            port = api_port or _PORTS.get(key, 18317)
            health_dir = config.paths.state / "openai-mcp-tunnel"
            health_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
            if health_dir.is_symlink():
                raise ValueError("Tunnel state directory must not be a symlink")
            health_file = health_dir / (uuid.uuid4().hex + ".url")
            env = {k: v for k, v in os.environ.items() if k in {
                "PATH", "HOME", "LANG", "LC_ALL", "TMPDIR", "TEMP", "SystemRoot", "USERPROFILE",
                "SSL_CERT_FILE", "SSL_CERT_DIR", "HTTPS_PROXY", "HTTP_PROXY", "NO_PROXY"}}
            # The MCP listener is loopback, while browser-facing OAuth uses the
            # configured public origin. Explicit trust is required by tunnel-client.
            env.update(CONTROL_PLANE_API_KEY=api_key, CONTROL_PLANE_TUNNEL_ID=tunnel_id,
                       MCP_OAUTH_TRUSTED_ORIGINS=origin, MCP_STARTUP_WAIT_TIMEOUT="30s")
            command = [binary, "run", "--mcp.server-url", f"http://127.0.0.1:{port}/mcp",
                       "--health.listen-addr", "127.0.0.1:0", "--health.url-file", str(health_file)]
            process = subprocess.Popen(command, env=env, cwd=str(config.paths.root),
                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            _PROCESSES[key] = (process, health_file)
            _ERRORS.pop(key, None)
            return {"ok": True, **status(config)}
        except ValueError as exc:
            _ERRORS[key] = str(exc)
        except Exception:
            _ERRORS[key] = "Tunnel startup failed; check the installed client and Vault configuration"
    return {"ok": False, **status(config)}


def stop(config):
    key = str(config.paths.root.resolve())
    with _LOCK:
        item = _PROCESSES.pop(key, None)
    if item and item[0].poll() is None:
        item[0].terminate()
        try:
            item[0].wait(timeout=5)
        except subprocess.TimeoutExpired:
            item[0].kill()
            item[0].wait(timeout=5)
    # No pidfile scans or arbitrary process termination.
    return {"ok": True, **status(config)}


def restore(config, *, api_port):
    _PORTS[str(config.paths.root.resolve())] = api_port
    if os.environ.get("NERYA_DISABLE_TUNNEL_RESTORE", "").lower() in {"1", "true", "yes"}:
        return
    if config.get("mcp.enabled") is True and config.get("mcp.openai_tunnel.enabled") is True:
        # tunnel-client performs its own network reconnection while this child lives.
        start(config, api_port=api_port)
