"""Remote-access tunnel provider management.

Tunnel binaries are optional host dependencies. This module never installs
anything during normal startup; installs only run from the explicit dashboard
action routed through ``/network/tunnels/install``.
"""

from __future__ import annotations

import json
import os
import platform
import re
import shutil
import signal
import subprocess
import tarfile
import threading
import time
import urllib.request
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from . import yaml_io
from .dashboard import DEFAULT_DASHBOARD_PORT, dashboard_url
from .paths import WorkspacePaths


PROVIDERS = ("tailscale", "cloudflare", "zrok", "ngrok")
DEFAULT_DASHBOARD_URL = f"http://127.0.0.1:{DEFAULT_DASHBOARD_PORT}"
DEFAULT_API_URL = "http://127.0.0.1:18317"
GITHUB_API_TIMEOUT_S = 30
_URL_RE = re.compile(r"https?://[^\s|]+")


@dataclass(frozen=True)
class TunnelProviderSpec:
    id: str
    label: str
    executable: str
    description: str
    docs_url: str
    free_tier: str
    install_hint: str
    token_label: str = ""
    token_required_for_start: bool = False
    supports_workspace_install: bool = False
    supports_process: bool = True
    modes: tuple[str, ...] = ("public",)

    def as_public(self) -> dict[str, Any]:
        return asdict(self)


PROVIDER_SPECS: dict[str, TunnelProviderSpec] = {
    "tailscale": TunnelProviderSpec(
        id="tailscale",
        label="Tailscale",
        executable="tailscale",
        description="Serve keeps access inside your tailnet; Funnel publishes the selected local service to the public internet.",
        docs_url="https://tailscale.com/docs/reference/tailscale-cli/funnel",
        free_tier="Personal use is free; Funnel availability depends on tailnet policy and Tailscale client support.",
        install_hint="Windows: winget install -e --id tailscale.tailscale",
        supports_process=False,
        modes=("funnel", "serve"),
    ),
    "cloudflare": TunnelProviderSpec(
        id="cloudflare",
        label="Cloudflare Tunnel",
        executable="cloudflared",
        description="cloudflared creates outbound-only tunnels to Cloudflare; token mode supports a named tunnel from Zero Trust.",
        docs_url="https://developers.cloudflare.com/tunnel/",
        free_tier="Cloudflare Tunnel can be used from the free Cloudflare Zero Trust plan; Access can add another login layer.",
        install_hint="Windows: winget install --id Cloudflare.cloudflared -e",
        token_label="Cloudflare tunnel token",
    ),
    "zrok": TunnelProviderSpec(
        id="zrok",
        label="zrok",
        executable="zrok",
        description="Open-source sharing tool from NetFoundry/OpenZiti with public and private share modes.",
        docs_url="https://docs.zrok.io/docs/getting-started",
        free_tier="Hosted zrok has a free tier; self-hosting is also available.",
        install_hint="Downloads the zrok release binary into the Nerya workspace tools directory.",
        token_label="zrok account token",
        token_required_for_start=True,
        supports_workspace_install=True,
    ),
    "ngrok": TunnelProviderSpec(
        id="ngrok",
        label="ngrok",
        executable="ngrok",
        description="ngrok agent publishes local HTTP services and can reuse an account authtoken.",
        docs_url="https://ngrok.com/docs/agent/",
        free_tier="Free plan supports limited endpoints, bandwidth, and request volume.",
        install_hint="Windows: winget install -e --id Ngrok.Ngrok",
        token_label="ngrok authtoken",
    ),
}


def _paths(config: Any) -> WorkspacePaths:
    paths = getattr(config, "paths", None)
    if paths is None:
        return WorkspacePaths(root=Path(config).expanduser().resolve())
    return paths


def _state_dir(paths: WorkspacePaths) -> Path:
    return paths.state / "tunnels"


def _tools_dir(paths: WorkspacePaths) -> Path:
    return paths.root / "tools" / "tunnels"


def _provider_state_path(paths: WorkspacePaths, provider: str) -> Path:
    return _state_dir(paths) / f"{provider}.json"


def _provider_log_path(paths: WorkspacePaths, provider: str) -> Path:
    return paths.dev_logs / "tunnels" / f"{provider}.log"


def _provider_config(config: Any) -> dict[str, Any]:
    raw = config.get("network.tunnels", {}) if hasattr(config, "get") else {}
    return raw if isinstance(raw, dict) else {}


def _providers_config(config: Any) -> dict[str, Any]:
    raw = (_provider_config(config).get("providers") or {})
    return raw if isinstance(raw, dict) else {}


def _selected_provider_config(config: Any, provider: str) -> dict[str, Any]:
    raw = _providers_config(config).get(provider) or {}
    return raw if isinstance(raw, dict) else {}


def _platform_key() -> str:
    name = platform.system().lower()
    if name.startswith("win"):
        return "windows"
    if name == "darwin":
        return "darwin"
    if name == "linux":
        return "linux"
    return name or "unknown"


def _arch_key() -> str:
    machine = platform.machine().lower()
    if machine in ("amd64", "x86_64"):
        return "amd64"
    if machine in ("arm64", "aarch64"):
        return "arm64"
    return machine


def _workspace_executable(paths: WorkspacePaths, provider: str) -> Path:
    suffix = ".exe" if _platform_key() == "windows" else ""
    return _tools_dir(paths) / provider / f"{PROVIDER_SPECS[provider].executable}{suffix}"


def executable_path(paths: WorkspacePaths, provider: str) -> str:
    exe = PROVIDER_SPECS[provider].executable
    found = shutil.which(exe)
    if found:
        return found
    workspace = _workspace_executable(paths, provider)
    return str(workspace) if workspace.exists() else ""


def _run_version(path: str) -> str:
    if not path:
        return ""
    try:
        proc = subprocess.run(
            [path, "version"],
            check=False,
            capture_output=True,
            text=True,
            timeout=5,
        )
    except Exception:
        return ""
    out = ((proc.stdout or "") + "\n" + (proc.stderr or "")).strip()
    return out.splitlines()[0][:160] if out else ""


def _run_json(cmd: list[str], *, timeout: int = 10) -> tuple[dict[str, Any], str]:
    try:
        proc = subprocess.run(
            cmd,
            check=False,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except Exception as exc:
        return {}, str(exc)
    out = (proc.stdout or "").strip()
    err = (proc.stderr or "").strip()
    if proc.returncode != 0:
        return {}, err or out
    if not out:
        return {}, ""
    try:
        data = json.loads(out)
    except Exception:
        return {}, out
    return data if isinstance(data, dict) else {}, ""


def _collect_urls(value: Any, urls: set[str]) -> None:
    if isinstance(value, str):
        for match in _URL_RE.findall(value):
            urls.add(match.rstrip(".,)"))
    elif isinstance(value, dict):
        for child in value.values():
            _collect_urls(child, urls)
    elif isinstance(value, (list, tuple)):
        for child in value:
            _collect_urls(child, urls)


def _provider_log_urls(paths: WorkspacePaths, provider: str) -> list[str]:
    log_path = _provider_log_path(paths, provider)
    try:
        text = log_path.read_text(encoding="utf-8", errors="ignore")[-20000:]
    except Exception:
        return []
    # Logs are append-only across tunnel sessions. For quick tunnels the old
    # trycloudflare hostname dies when the process exits, so status must only
    # expose URLs discovered after the most recent Nerya-managed start marker.
    marker = "--- nerya tunnel start "
    if marker in text:
        text = text.rsplit(marker, 1)[-1]
    urls: set[str] = set()
    _collect_urls(text, urls)
    if provider == "cloudflare":
        roots: set[str] = set()
        for url in urls:
            try:
                parsed = urlparse(url)
            except Exception:
                continue
            host = (parsed.hostname or "").lower()
            if not host.endswith(".trycloudflare.com"):
                continue
            roots.add(f"https://{host}")
        return sorted(roots)
    return sorted(urls)


def _tailscale_status(paths: WorkspacePaths) -> dict[str, Any]:
    exe = executable_path(paths, "tailscale")
    if not exe:
        return {}
    data, _err = _run_json([exe, "status", "--json"])
    return data


def _tailscale_ready(paths: WorkspacePaths) -> tuple[bool, dict[str, Any]]:
    status = _tailscale_status(paths)
    backend = str(status.get("BackendState") or "")
    auth_url = str(status.get("AuthURL") or "")
    ok = backend.lower() in {"running", "started"}
    return ok, {
        "backend_state": backend or "unknown",
        "auth_url": auth_url,
        "login_command": "tailscale up",
    }


def _tailscale_external_urls(paths: WorkspacePaths, mode: str = "funnel") -> list[str]:
    exe = executable_path(paths, "tailscale")
    if not exe:
        return []
    urls: set[str] = set()
    status_cmds = (
        [exe, "funnel", "status", "--json"],
        [exe, "serve", "status", "--json"],
    )
    plain_cmds = (
        [exe, "funnel", "status"],
        [exe, "serve", "status"],
    )
    has_config = False
    for cmd in status_cmds:
        data, _err = _run_json(cmd)
        if data:
            has_config = True
            _collect_urls(data, urls)
    for cmd in plain_cmds:
        try:
            proc = subprocess.run(cmd, check=False, capture_output=True, text=True, timeout=10)
        except Exception:
            continue
        text = ((proc.stdout or "") + "\n" + (proc.stderr or "")).strip()
        if proc.returncode == 0 and text and "No serve config" not in text:
            has_config = True
            _collect_urls(text, urls)

    if has_config:
        status = _tailscale_status(paths)
        self_info = status.get("Self") if isinstance(status.get("Self"), dict) else {}
        dns_name = str(self_info.get("DNSName") or "").strip().rstrip(".")
        if dns_name:
            urls.add(f"https://{dns_name}")
    return sorted(urls)


def _load_state(paths: WorkspacePaths, provider: str) -> dict[str, Any]:
    try:
        return json.loads(_provider_state_path(paths, provider).read_text(encoding="utf-8"))
    except Exception:
        return {}


def _save_state(paths: WorkspacePaths, provider: str, state: dict[str, Any]) -> None:
    path = _provider_state_path(paths, provider)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")


def _clear_state(paths: WorkspacePaths, provider: str) -> None:
    try:
        _provider_state_path(paths, provider).unlink()
    except FileNotFoundError:
        pass


def _legacy_desired_running(paths: WorkspacePaths, provider: str) -> bool:
    state = _load_state(paths, provider)
    return bool(state.get("started_at"))


def _desired_running(config: Any, provider: str, cfg: dict[str, Any]) -> bool:
    if "desired_running" in cfg:
        return bool(cfg.get("desired_running", False))
    return bool(cfg.get("enabled", False)) and _legacy_desired_running(_paths(config), provider)


def _set_desired_running(config: Any, provider: str, desired: bool) -> None:
    paths = _paths(config)
    existing = yaml_io.load(paths.config, default={}) or {}
    if not isinstance(existing, dict):
        existing = {}
    network = existing.setdefault("network", {})
    if not isinstance(network, dict):
        network = {}
        existing["network"] = network
    tunnels = network.setdefault("tunnels", {})
    if not isinstance(tunnels, dict):
        tunnels = {}
        network["tunnels"] = tunnels
    providers = tunnels.setdefault("providers", {})
    if not isinstance(providers, dict):
        providers = {}
        tunnels["providers"] = providers
    current = providers.get(provider) if isinstance(providers.get(provider), dict) else {}
    next_cfg = dict(current)
    next_cfg["desired_running"] = bool(desired)
    providers[provider] = next_cfg
    yaml_io.dump(paths.config, existing)

    data = getattr(config, "data", None)
    if isinstance(data, dict):
        data_network = data.setdefault("network", {})
        if not isinstance(data_network, dict):
            data_network = {}
            data["network"] = data_network
        data_tunnels = data_network.setdefault("tunnels", {})
        if not isinstance(data_tunnels, dict):
            data_tunnels = {}
            data_network["tunnels"] = data_tunnels
        data_providers = data_tunnels.setdefault("providers", {})
        if not isinstance(data_providers, dict):
            data_providers = {}
            data_tunnels["providers"] = data_providers
        data_current = data_providers.get(provider) if isinstance(data_providers.get(provider), dict) else {}
        data_next = dict(data_current)
        data_next["desired_running"] = bool(desired)
        data_providers[provider] = data_next


def _is_pid_running(pid: Any) -> bool:
    try:
        value = int(pid)
    except Exception:
        return False
    if value <= 0:
        return False
    if _platform_key() == "windows":
        try:
            import ctypes
            from ctypes import wintypes

            handle = ctypes.windll.kernel32.OpenProcess(0x1000, False, value)
            if not handle:
                return False
            try:
                exit_code = wintypes.DWORD()
                if not ctypes.windll.kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code)):
                    return False
                return exit_code.value == 259  # STILL_ACTIVE
            finally:
                ctypes.windll.kernel32.CloseHandle(handle)
        except Exception:
            return False
    try:
        os.kill(value, 0)
        return True
    except OSError:
        return False


def _tail_file(path: Path, max_chars: int = 2000) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="ignore")[-max_chars:]
    except Exception:
        return ""


def _redact_command(parts: list[str], secret: str = "") -> list[str]:
    if not secret:
        return parts
    return ["<redacted>" if part == secret else part for part in parts]


def _resolve_ref(paths: WorkspacePaths, ref: str) -> str:
    ref = str(ref or "").strip()
    if not ref:
        return ""
    if not ref.startswith("vault://"):
        return ref
    from ..security.secrets import SecretVault

    vault = SecretVault.open(paths.vault_enc)
    return vault.resolve(ref.removeprefix("vault://"), required_scope="runtime")


def _secret_name(provider: str, value: str) -> str:
    import hashlib

    digest = hashlib.sha256(value.encode("utf-8")).hexdigest()[:12]
    return f"network_tunnel_{provider}_token_{digest}"


def _store_token(paths: WorkspacePaths, provider: str, value: str) -> str:
    from ..security.secrets import SecretVault

    vault = SecretVault.open(paths.vault_enc)
    meta = vault.put(
        name=_secret_name(provider, value),
        value=value,
        kind="network_tunnel",
        scope=["runtime", "network"],
        owner=f"network.tunnels/{provider}",
    )
    return meta.ref()


def _target_url(config: Any, cfg: dict[str, Any]) -> str:
    target = str(cfg.get("target") or "dashboard").strip().lower()
    if target == "api":
        return DEFAULT_API_URL
    if target == "custom":
        raw = str(cfg.get("target_url") or "").strip()
        if raw.startswith("http://") or raw.startswith("https://"):
            return raw
        if raw:
            return f"http://{raw}"
    return dashboard_url(config)


def _target_port(config: Any, cfg: dict[str, Any]) -> str:
    url = _target_url(config, cfg)
    parsed = urlparse(url)
    if parsed.port:
        return str(parsed.port)
    if parsed.scheme == "https":
        return "443"
    return "80"


def _token_required(provider: str, cfg: dict[str, Any]) -> bool:
    if PROVIDER_SPECS[provider].token_required_for_start:
        return True
    if provider == "cloudflare" and str(cfg.get("cloudflare_mode") or "quick") == "token":
        return True
    return False


def _install_command(provider: str) -> list[str]:
    os_key = _platform_key()
    if os_key == "windows":
        ids = {
            "tailscale": "tailscale.tailscale",
            "cloudflare": "Cloudflare.cloudflared",
            "ngrok": "Ngrok.Ngrok",
        }
        if provider in ids:
            return [
                "winget",
                "install",
                "-e",
                "--id",
                ids[provider],
                "--accept-package-agreements",
                "--accept-source-agreements",
            ]
    if os_key == "darwin":
        if provider == "cloudflare":
            return ["brew", "install", "cloudflared"]
        if provider == "ngrok":
            return ["brew", "install", "ngrok/ngrok/ngrok"]
    return []


def _install_zrok_workspace(paths: WorkspacePaths) -> dict[str, Any]:
    os_key = _platform_key()
    arch = _arch_key()
    if os_key not in {"windows", "linux", "darwin"} or arch not in {"amd64", "arm64"}:
        return {"ok": False, "error": "unsupported_platform", "detail": f"{os_key}/{arch}"}

    tools = _tools_dir(paths) / "zrok"
    tools.mkdir(parents=True, exist_ok=True)
    api_url = "https://api.github.com/repos/openziti/zrok/releases/latest"
    try:
        with urllib.request.urlopen(api_url, timeout=GITHUB_API_TIMEOUT_S) as resp:
            release = json.loads(resp.read().decode("utf-8"))
    except Exception as exc:
        return {"ok": False, "error": "zrok_release_lookup_failed", "detail": str(exc)}

    assets = release.get("assets") or []
    asset_url = ""
    for asset in assets:
        name = str(asset.get("name") or "").lower()
        if os_key in name and arch in name and (name.endswith(".tar.gz") or name.endswith(".tgz")):
            asset_url = str(asset.get("browser_download_url") or "")
            break
    if not asset_url:
        return {"ok": False, "error": "zrok_release_asset_not_found", "detail": f"{os_key}/{arch}"}

    archive = tools / "zrok-release.tar.gz"
    try:
        with urllib.request.urlopen(asset_url, timeout=GITHUB_API_TIMEOUT_S) as resp:
            archive.write_bytes(resp.read())
        exe_name = "zrok.exe" if os_key == "windows" else "zrok"
        with tarfile.open(archive, "r:gz") as tf:
            member = next((m for m in tf.getmembers() if Path(m.name).name == exe_name), None)
            if member is None:
                return {"ok": False, "error": "zrok_binary_not_found_in_archive"}
            member.name = exe_name
            tf.extract(member, path=tools)
        exe = tools / exe_name
        if os_key != "windows":
            exe.chmod(0o755)
        return {"ok": True, "path": str(exe), "version": _run_version(str(exe))}
    except Exception as exc:
        return {"ok": False, "error": "zrok_install_failed", "detail": str(exc)}
    finally:
        try:
            archive.unlink()
        except Exception:
            pass


def install_provider(config: Any, provider: str, *, approve: bool = False) -> dict[str, Any]:
    provider = str(provider or "").strip().lower()
    if provider not in PROVIDER_SPECS:
        return {"ok": False, "error": "unknown_provider"}
    if not approve:
        return {
            "ok": False,
            "error": "operator_approval_required",
            "detail": "Optional tunnel dependencies are installed only from an explicit settings action.",
        }

    paths = _paths(config)
    if executable_path(paths, provider):
        return {"ok": True, "provider": provider, "already_installed": True}
    if provider == "zrok":
        result = _install_zrok_workspace(paths)
        return {"provider": provider, **result}

    cmd = _install_command(provider)
    if not cmd:
        return {
            "ok": False,
            "provider": provider,
            "error": "manual_install_required",
            "detail": PROVIDER_SPECS[provider].install_hint,
        }
    try:
        proc = subprocess.run(
            cmd,
            check=False,
            capture_output=True,
            text=True,
            timeout=900,
        )
    except FileNotFoundError:
        return {"ok": False, "provider": provider, "error": "installer_not_found", "detail": cmd[0]}
    except subprocess.TimeoutExpired:
        return {"ok": False, "provider": provider, "error": "install_timeout"}
    out = ((proc.stdout or "") + "\n" + (proc.stderr or "")).strip()
    ok = proc.returncode == 0
    return {
        "ok": ok,
        "provider": provider,
        "command": cmd,
        "returncode": proc.returncode,
        "output_preview": out[-2000:],
        "path": executable_path(paths, provider),
        "error": "" if ok else "install_failed",
    }


def save_tunnel_config(config: Any, payload: dict[str, Any]) -> dict[str, Any]:
    provider = str((payload or {}).get("provider") or "").strip().lower()
    if provider not in PROVIDER_SPECS:
        return {"ok": False, "error": "unknown_provider"}

    paths = _paths(config)
    existing = yaml_io.load(paths.config, default={}) or {}
    if not isinstance(existing, dict):
        existing = {}
    network = existing.setdefault("network", {})
    if not isinstance(network, dict):
        network = {}
        existing["network"] = network
    tunnels = network.setdefault("tunnels", {})
    if not isinstance(tunnels, dict):
        tunnels = {}
        network["tunnels"] = tunnels
    providers = tunnels.setdefault("providers", {})
    if not isinstance(providers, dict):
        providers = {}
        tunnels["providers"] = providers

    prev = providers.get(provider) if isinstance(providers.get(provider), dict) else {}
    token = str((payload or {}).get("token") or "").strip()
    token_ref = str((payload or {}).get("token_ref") or prev.get("token_ref") or "").strip()
    if token:
        token_ref = _store_token(paths, provider, token)
    elif token_ref and not token_ref.startswith("vault://"):
        return {"ok": False, "error": "invalid_token_ref", "detail": "token_ref must start with vault://"}

    target = str((payload or {}).get("target") or prev.get("target") or "dashboard").strip().lower()
    if target not in {"dashboard", "api", "custom"}:
        target = "dashboard"
    target_url = str((payload or {}).get("target_url") or prev.get("target_url") or "").strip()
    if target == "custom" and not target_url:
        return {"ok": False, "error": "target_url_required"}

    next_cfg = {
        "enabled": bool((payload or {}).get("enabled", prev.get("enabled", False))),
        "target": target,
        "target_url": target_url if target == "custom" else "",
        "mode": str((payload or {}).get("mode") or prev.get("mode") or PROVIDER_SPECS[provider].modes[0]),
        "token_ref": token_ref,
        "public_hostname": str((payload or {}).get("public_hostname") or prev.get("public_hostname") or "").strip(),
        "region": str((payload or {}).get("region") or prev.get("region") or "").strip(),
        "cloudflare_mode": str((payload or {}).get("cloudflare_mode") or prev.get("cloudflare_mode") or "quick").strip(),
        "desired_running": bool(
            (payload or {}).get(
                "desired_running",
                prev.get("desired_running", _legacy_desired_running(paths, provider)),
            )
        ),
    }
    if next_cfg["mode"] not in PROVIDER_SPECS[provider].modes:
        next_cfg["mode"] = PROVIDER_SPECS[provider].modes[0]
    if next_cfg["cloudflare_mode"] not in {"quick", "token"}:
        next_cfg["cloudflare_mode"] = "quick"
    if not next_cfg["enabled"]:
        next_cfg["desired_running"] = False

    providers[provider] = next_cfg
    yaml_io.dump(paths.config, existing)

    data = getattr(config, "data", None)
    if isinstance(data, dict):
        data.setdefault("network", {}).setdefault("tunnels", {}).setdefault("providers", {})[provider] = next_cfg

    return public_tunnel_status(config)


def _provider_public_status(config: Any, provider: str) -> dict[str, Any]:
    paths = _paths(config)
    cfg = _selected_provider_config(config, provider)
    exe = executable_path(paths, provider)
    state = _load_state(paths, provider)
    saved_urls = state.get("external_urls") if isinstance(state.get("external_urls"), list) else []
    external_urls = [str(url) for url in saved_urls if str(url or "").startswith(("http://", "https://"))]
    tailscale_detail: dict[str, Any] = {}
    if provider == "tailscale" and exe:
        live_urls = _tailscale_external_urls(paths, str(cfg.get("mode") or "funnel"))
        if live_urls:
            external_urls = live_urls
        _ready, tailscale_detail = _tailscale_ready(paths)
    elif provider == "cloudflare" and exe:
        log_urls = _provider_log_urls(paths, provider)
        if log_urls:
            external_urls = log_urls
    if PROVIDER_SPECS[provider].supports_process:
        running = _is_pid_running(state.get("pid"))
    elif provider == "tailscale":
        running = bool(tailscale_detail.get("backend_state", "").lower() in {"running", "started"} and (external_urls or state.get("started_at")))
    else:
        running = bool(state.get("started_at"))
    return {
        "spec": PROVIDER_SPECS[provider].as_public(),
        "config": {
            "enabled": bool(cfg.get("enabled", False)),
            "target": str(cfg.get("target") or "dashboard"),
            "target_url": str(cfg.get("target_url") or ""),
            "mode": str(cfg.get("mode") or PROVIDER_SPECS[provider].modes[0]),
            "token_ref": str(cfg.get("token_ref") or ""),
            "token_configured": bool(cfg.get("token_ref")),
            "public_hostname": str(cfg.get("public_hostname") or ""),
            "region": str(cfg.get("region") or ""),
            "cloudflare_mode": str(cfg.get("cloudflare_mode") or "quick"),
            "desired_running": _desired_running(config, provider, cfg),
        },
        "installed": bool(exe),
        "executable_path": exe,
        "version": _run_version(exe),
        "running": running,
        "state": {
            "pid": state.get("pid") if running else None,
            "started_at": state.get("started_at") if running else None,
            "target_url": state.get("target_url") if running else "",
            "log_path": state.get("log_path") or str(_provider_log_path(paths, provider)),
            "command": state.get("command") if running else [],
            "external_urls": external_urls if running else [],
            "tailscale": tailscale_detail if provider == "tailscale" and tailscale_detail else {},
        },
    }


def public_tunnel_status(config: Any) -> dict[str, Any]:
    providers = [_provider_public_status(config, provider) for provider in PROVIDERS]
    return {
        "ok": True,
        "providers": providers,
        "auth": {
            "admin_password_configured": bool(config.get("runtime.auth.admin_password_hash", "")) if hasattr(config, "get") else False,
            "auth_mode": str(config.get("runtime.auth.mode", "local")) if hasattr(config, "get") else "local",
            "dashboard_target": dashboard_url(config),
            "api_target": DEFAULT_API_URL,
            "direct_api_requires_token_mode": True,
        },
    }


def _start_command(config: Any, paths: WorkspacePaths, provider: str, cfg: dict[str, Any], token: str) -> tuple[list[str], list[str], dict[str, str]]:
    exe = executable_path(paths, provider)
    target_url = _target_url(config, cfg)
    env: dict[str, str] = {}
    if provider == "tailscale":
        port = _target_port(config, cfg)
        mode = str(cfg.get("mode") or "funnel")
        if mode == "serve":
            cmd = [exe, "serve", "--yes", "--bg", port]
        else:
            cmd = [exe, "funnel", "--yes", "--bg", port]
        return cmd, cmd, env
    if provider == "cloudflare":
        if str(cfg.get("cloudflare_mode") or "quick") == "token":
            cmd = [exe, "tunnel", "--no-autoupdate", "run", "--token", token]
            return cmd, _redact_command(cmd, token), env
        cmd = [exe, "tunnel", "--url", target_url]
        return cmd, cmd, env
    if provider == "zrok":
        cmd = [exe, "share", "public", _target_port(config, cfg)]
        return cmd, cmd, env
    if provider == "ngrok":
        if token:
            env["NGROK_AUTHTOKEN"] = token
        cmd = [exe, "http", target_url]
        public_hostname = str(cfg.get("public_hostname") or "").strip()
        if public_hostname:
            cmd.extend(["--url", public_hostname])
        region = str(cfg.get("region") or "").strip()
        if region:
            cmd.extend(["--region", region])
        return cmd, cmd, env
    raise ValueError(f"unsupported provider: {provider}")


def _enable_zrok(paths: WorkspacePaths, provider: str, token: str) -> dict[str, Any]:
    if not token:
        return {"ok": False, "error": "token_required"}
    exe = executable_path(paths, provider)
    try:
        proc = subprocess.run(
            [exe, "enable", token],
            check=False,
            capture_output=True,
            text=True,
            timeout=60,
        )
    except Exception as exc:
        return {"ok": False, "error": "zrok_enable_failed", "detail": str(exc)}
    if proc.returncode != 0:
        out = ((proc.stdout or "") + "\n" + (proc.stderr or "")).replace(token, "<redacted>")
        return {"ok": False, "error": "zrok_enable_failed", "detail": out[-1000:]}
    return {"ok": True}


def start_tunnel(config: Any, provider: str) -> dict[str, Any]:
    provider = str(provider or "").strip().lower()
    if provider not in PROVIDER_SPECS:
        return {"ok": False, "error": "unknown_provider"}
    cfg = _selected_provider_config(config, provider)
    if not bool(cfg.get("enabled", False)):
        return {"ok": False, "provider": provider, "error": "provider_not_enabled"}
    if not bool(config.get("runtime.auth.admin_password_hash", "")):
        return {
            "ok": False,
            "provider": provider,
            "error": "admin_password_required",
            "detail": "Set an admin password before exposing Nerya through a tunnel.",
        }
    if str(cfg.get("target") or "dashboard") == "api" and str(config.get("runtime.auth.mode", "local")) != "token":
        return {
            "ok": False,
            "provider": provider,
            "error": "api_tunnel_requires_token_auth",
            "detail": "Direct API tunnels must use runtime.auth.mode=token; expose the dashboard target otherwise.",
        }
    paths = _paths(config)
    if not executable_path(paths, provider):
        return {"ok": False, "provider": provider, "error": "dependency_missing"}
    token = _resolve_ref(paths, str(cfg.get("token_ref") or ""))
    if _token_required(provider, cfg) and not token:
        return {"ok": False, "provider": provider, "error": "token_required"}
    if provider == "tailscale":
        ready, detail = _tailscale_ready(paths)
        if not ready:
            backend = detail.get("backend_state") or "unknown"
            return {
                "ok": False,
                "provider": provider,
                "error": "tailscale_not_ready",
                "detail": f"Tailscale backend state is {backend}. Run `tailscale up` and complete login before starting Serve/Funnel.",
                "tailscale": detail,
            }
    if provider == "zrok":
        enabled = _enable_zrok(paths, provider, token)
        if not enabled.get("ok"):
            return {"provider": provider, **enabled}

    cmd, redacted, extra_env = _start_command(config, paths, provider, cfg, token)
    log_path = _provider_log_path(paths, provider)
    log_path.parent.mkdir(parents=True, exist_ok=True)
    env = os.environ.copy()
    env.update(extra_env)

    if provider == "tailscale":
        proc = subprocess.run(cmd, check=False, capture_output=True, text=True, timeout=60, env=env)
        out = ((proc.stdout or "") + "\n" + (proc.stderr or "")).strip()
        if proc.returncode != 0 and "--yes" in cmd and ("unknown flag" in out.lower() or "flag provided but not defined" in out.lower()):
            fallback_cmd = [part for part in cmd if part != "--yes"]
            proc = subprocess.run(fallback_cmd, check=False, capture_output=True, text=True, timeout=60, env=env)
            out = ((proc.stdout or "") + "\n" + (proc.stderr or "")).strip()
            cmd = fallback_cmd
            redacted = fallback_cmd
        if proc.returncode != 0:
            return {"ok": False, "provider": provider, "error": "start_failed", "detail": out[-1000:]}
        external_urls = _tailscale_external_urls(paths, str(cfg.get("mode") or "funnel"))
        state = {
            "started_at": time.time(),
            "target_url": _target_url(config, cfg),
            "log_path": str(log_path),
            "command": redacted,
            "managed_by": "tailscale-cli",
            "external_urls": external_urls,
        }
        _save_state(paths, provider, state)
        result = {"ok": True, "provider": provider, "state": state, "external_urls": external_urls, "output_preview": out[-1000:]}
        if not external_urls:
            result["warning"] = "external_url_not_detected"
        _set_desired_running(config, provider, True)
        return result

    with log_path.open("ab") as log:
        log.write(f"\n--- nerya tunnel start {time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())} ---\n".encode("utf-8"))
        proc = subprocess.Popen(
            cmd,
            stdout=log,
            stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
            env=env,
            cwd=str(paths.root),
        )
    state = {
        "pid": proc.pid,
        "started_at": time.time(),
        "target_url": _target_url(config, cfg),
        "log_path": str(log_path),
        "command": redacted,
        "external_urls": [],
    }
    _save_state(paths, provider, state)
    if provider == "cloudflare":
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            if not _is_pid_running(proc.pid):
                break
            urls = _provider_log_urls(paths, provider)
            if urls:
                state["external_urls"] = urls
                _save_state(paths, provider, state)
                break
            time.sleep(1)
    if not _is_pid_running(proc.pid):
        detail = _tail_file(log_path)
        _clear_state(paths, provider)
        return {
            "ok": False,
            "provider": provider,
            "error": "start_failed",
            "detail": detail[-1000:],
        }
    _set_desired_running(config, provider, True)
    return {"ok": True, "provider": provider, "state": state, "external_urls": state.get("external_urls", [])}


def stop_tunnel(config: Any, provider: str) -> dict[str, Any]:
    provider = str(provider or "").strip().lower()
    if provider not in PROVIDER_SPECS:
        return {"ok": False, "error": "unknown_provider"}
    paths = _paths(config)
    state = _load_state(paths, provider)
    if provider == "tailscale":
        exe = executable_path(paths, provider)
        if exe:
            mode = str(_selected_provider_config(config, provider).get("mode") or "funnel")
            cmd = [exe, "serve", "reset"] if mode == "serve" else [exe, "funnel", "reset"]
            subprocess.run(cmd, check=False, capture_output=True, text=True, timeout=30)
        _clear_state(paths, provider)
        _set_desired_running(config, provider, False)
        return {"ok": True, "provider": provider}

    pid = state.get("pid")
    if _is_pid_running(pid):
        try:
            os.kill(int(pid), signal.SIGTERM)
        except Exception:
            try:
                subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"], check=False, capture_output=True, text=True, timeout=10)
            except Exception:
                pass
    _clear_state(paths, provider)
    _set_desired_running(config, provider, False)
    return {"ok": True, "provider": provider}


def restore_configured_tunnels_on_start(config: Any) -> dict[str, Any]:
    """Best-effort startup restore for tunnels the operator last left running."""

    if os.environ.get("NERYA_DISABLE_TUNNEL_RESTORE", "").strip().lower() in {"1", "true", "yes", "on"}:
        return {"ok": True, "disabled": True, "started": [], "skipped": [], "errors": []}

    started: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    errors: list[dict[str, Any]] = []
    for provider in PROVIDERS:
        cfg = _selected_provider_config(config, provider)
        if not _desired_running(config, provider, cfg):
            continue
        if not bool(cfg.get("enabled", False)):
            skipped.append({"provider": provider, "reason": "disabled"})
            continue
        status = _provider_public_status(config, provider)
        if status.get("running"):
            skipped.append({"provider": provider, "reason": "already_running"})
            continue
        try:
            result = start_tunnel(config, provider)
        except Exception as exc:  # pragma: no cover - defensive startup guard
            errors.append({"provider": provider, "error": f"{type(exc).__name__}: {exc}"})
            continue
        if result.get("ok"):
            started.append({
                "provider": provider,
                "external_urls": result.get("external_urls") or (result.get("state") or {}).get("external_urls") or [],
            })
        else:
            errors.append({
                "provider": provider,
                "error": result.get("error") or "start_failed",
                "detail": result.get("detail") or "",
            })
    return {"ok": not errors, "started": started, "skipped": skipped, "errors": errors}


_STARTUP_RESTORE_THREADS: dict[str, threading.Thread] = {}
_STARTUP_RESTORE_LOCK = threading.RLock()
_LAST_STARTUP_RESTORE: dict[str, Any] = {}


def launch_tunnel_restore_on_start(config: Any) -> dict[str, Any]:
    """Schedule startup restore without blocking the local API boot path."""

    key = str(_paths(config).root.resolve())
    with _STARTUP_RESTORE_LOCK:
        thread = _STARTUP_RESTORE_THREADS.get(key)
        if thread is not None and thread.is_alive():
            return {"scheduled": False, "already_running": True}

        def _run() -> None:
            _LAST_STARTUP_RESTORE[key] = restore_configured_tunnels_on_start(config)

        thread = threading.Thread(
            target=_run,
            name=f"nerya-tunnel-restore-{_paths(config).root.name}",
            daemon=True,
        )
        _STARTUP_RESTORE_THREADS[key] = thread
        thread.start()
    return {"scheduled": True}
