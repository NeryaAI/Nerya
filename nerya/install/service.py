"""Platform-specific service installer.

This module registers (or unregisters) ``nerya serve`` as a host-level
service:

    * Linux    → ``systemd --user`` unit at ``~/.config/systemd/user/nerya.service``
    * macOS    → launchd agent at ``~/Library/LaunchAgents/com.nerya.agent.plist``
    * Windows  → NSSM service ``nerya-agent`` (requires ``nssm`` on PATH)

The design mirrors The runtime' one-command service experience: after
``nerya service install``, the API is available on the configured port
across restarts with a single command.

The functions are deliberately side-effect-local to the current user —
we never require sudo/UAC for the default path.
"""

from __future__ import annotations

import os
import platform
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

SERVICE_NAME = "nerya-agent"
UNIT_NAME_LINUX = "nerya.service"
PLIST_LABEL = "com.nerya.agent"


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _os_kind() -> str:
    s = platform.system().lower()
    if s == "darwin":
        return "macos"
    if s == "linux":
        return "linux"
    if s == "windows":
        return "windows"
    return s


def _home() -> Path:
    return Path.home()


def _nerya_bin() -> str:
    """Best-effort path to the ``nerya`` entry point."""
    candidate = shutil.which("nerya")
    if candidate:
        return candidate
    # Fall back to the Python interpreter with ``-m nerya.cli.app``.
    return f"{sys.executable} -m nerya.cli.app"


def _run(cmd: list[str], *, check: bool = False, capture: bool = False,
         env: dict[str, str] | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(
        cmd,
        check=check,
        env={**os.environ, **(env or {})} if env is not None else None,
        text=True,
        capture_output=capture,
    )


def _workspace_default(user: str | None) -> Path:
    if user:
        return Path(user).expanduser().resolve()
    env = os.environ.get("NERYA_WORKSPACE")
    if env:
        return Path(env).expanduser().resolve()
    return (_home() / "nerya-ws").resolve()


# ---------------------------------------------------------------------------
# linux / systemd
# ---------------------------------------------------------------------------

def _systemd_unit_path() -> Path:
    return _home() / ".config" / "systemd" / "user" / UNIT_NAME_LINUX


def _install_systemd(workspace: Path, port: int, force: bool) -> dict[str, Any]:
    unit_path = _systemd_unit_path()
    unit_path.parent.mkdir(parents=True, exist_ok=True)
    if unit_path.exists() and not force:
        # Replace quietly — the user explicitly re-ran `service install`.
        pass
    unit = f"""[Unit]
Description=Nerya Autonomous Agent Runtime
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
Environment=NERYA_WORKSPACE={workspace}
ExecStart={_nerya_bin()} serve --port {port}
Restart=on-failure
RestartSec=5

[Install]
WantedBy=default.target
"""
    unit_path.write_text(unit, encoding="utf-8")
    _run(["systemctl", "--user", "daemon-reload"])
    _run(["systemctl", "--user", "enable", "--now", UNIT_NAME_LINUX])
    return {"kind": "systemd", "unit": str(unit_path), "port": port,
            "workspace": str(workspace)}


def _uninstall_systemd() -> dict[str, Any]:
    unit_path = _systemd_unit_path()
    _run(["systemctl", "--user", "disable", "--now", UNIT_NAME_LINUX])
    removed = unit_path.exists()
    if removed:
        unit_path.unlink()
    _run(["systemctl", "--user", "daemon-reload"])
    return {"kind": "systemd", "removed": removed, "unit": str(unit_path)}


def _status_systemd() -> dict[str, Any]:
    res = _run(["systemctl", "--user", "is-active", UNIT_NAME_LINUX], capture=True)
    enabled = _run(["systemctl", "--user", "is-enabled", UNIT_NAME_LINUX], capture=True)
    return {
        "kind": "systemd",
        "unit": str(_systemd_unit_path()),
        "active": (res.stdout or "").strip(),
        "enabled": (enabled.stdout or "").strip(),
    }


# ---------------------------------------------------------------------------
# macos / launchd
# ---------------------------------------------------------------------------

def _launchd_plist_path() -> Path:
    return _home() / "Library" / "LaunchAgents" / f"{PLIST_LABEL}.plist"


def _install_launchd(workspace: Path, port: int, force: bool) -> dict[str, Any]:
    plist_path = _launchd_plist_path()
    plist_path.parent.mkdir(parents=True, exist_ok=True)
    bin_path = _nerya_bin()
    log_dir = _home() / ".nerya"
    log_dir.mkdir(parents=True, exist_ok=True)
    out_log = log_dir / "nerya.out.log"
    err_log = log_dir / "nerya.err.log"
    plist = f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>{PLIST_LABEL}</string>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>ProgramArguments</key>
  <array>
    <string>/bin/sh</string>
    <string>-c</string>
    <string>exec {bin_path} serve --port {port}</string>
  </array>
  <key>EnvironmentVariables</key>
  <dict>
    <key>NERYA_WORKSPACE</key><string>{workspace}</string>
  </dict>
  <key>StandardOutPath</key><string>{out_log}</string>
  <key>StandardErrorPath</key><string>{err_log}</string>
</dict>
</plist>
"""
    plist_path.write_text(plist, encoding="utf-8")
    _run(["launchctl", "unload", str(plist_path)], capture=True)
    _run(["launchctl", "load", str(plist_path)], capture=True)
    return {"kind": "launchd", "plist": str(plist_path), "port": port,
            "workspace": str(workspace), "logs": {"out": str(out_log), "err": str(err_log)}}


def _uninstall_launchd() -> dict[str, Any]:
    plist_path = _launchd_plist_path()
    _run(["launchctl", "unload", str(plist_path)], capture=True)
    removed = plist_path.exists()
    if removed:
        plist_path.unlink()
    return {"kind": "launchd", "plist": str(plist_path), "removed": removed}


def _status_launchd() -> dict[str, Any]:
    res = _run(["launchctl", "list", PLIST_LABEL], capture=True)
    return {
        "kind": "launchd",
        "plist": str(_launchd_plist_path()),
        "status": (res.stdout or "").strip(),
        "error": (res.stderr or "").strip(),
    }


# ---------------------------------------------------------------------------
# windows / nssm
# ---------------------------------------------------------------------------

def _nssm() -> str | None:
    return shutil.which("nssm") or shutil.which("nssm.exe")


def _install_nssm(workspace: Path, port: int, force: bool) -> dict[str, Any]:
    nssm = _nssm()
    if not nssm:
        return {
            "kind": "nssm",
            "ok": False,
            "error": "nssm not found on PATH. install via `winget install nssm` or `choco install nssm` and retry.",
        }
    exe = _nerya_bin()
    log_dir = _home() / ".nerya"
    log_dir.mkdir(parents=True, exist_ok=True)
    _run([nssm, "stop", SERVICE_NAME], capture=True)
    _run([nssm, "remove", SERVICE_NAME, "confirm"], capture=True)
    # Execute via cmd.exe so shims (.cmd) are resolved correctly.
    args = ["/c", f'"{exe}"' + f" serve --port {port}"]
    _run([nssm, "install", SERVICE_NAME, os.environ.get("ComSpec", "cmd.exe"), *args], check=True)
    _run([nssm, "set", SERVICE_NAME, "AppEnvironmentExtra", f"NERYA_WORKSPACE={workspace}"])
    _run([nssm, "set", SERVICE_NAME, "Start", "SERVICE_AUTO_START"])
    _run([nssm, "set", SERVICE_NAME, "AppStdout", str(log_dir / "nerya.out.log")])
    _run([nssm, "set", SERVICE_NAME, "AppStderr", str(log_dir / "nerya.err.log")])
    _run([nssm, "start", SERVICE_NAME])
    return {"kind": "nssm", "service": SERVICE_NAME, "port": port,
            "workspace": str(workspace), "ok": True}


def _uninstall_nssm() -> dict[str, Any]:
    nssm = _nssm()
    if not nssm:
        return {"kind": "nssm", "ok": False,
                "error": "nssm not found on PATH"}
    _run([nssm, "stop", SERVICE_NAME], capture=True)
    res = _run([nssm, "remove", SERVICE_NAME, "confirm"], capture=True)
    return {"kind": "nssm", "service": SERVICE_NAME,
            "removed": (res.returncode == 0),
            "stderr": (res.stderr or "").strip()}


def _status_nssm() -> dict[str, Any]:
    nssm = _nssm()
    if not nssm:
        return {"kind": "nssm", "ok": False, "error": "nssm not on PATH"}
    res = _run([nssm, "status", SERVICE_NAME], capture=True)
    return {"kind": "nssm", "service": SERVICE_NAME,
            "status": (res.stdout or "").strip(),
            "error": (res.stderr or "").strip()}


# ---------------------------------------------------------------------------
# public api
# ---------------------------------------------------------------------------

def install(*, workspace: str | None = None, port: int = 18317,
            force: bool = False) -> int:
    """Install the appropriate host service for the current platform."""
    ws = _workspace_default(workspace)
    ws.mkdir(parents=True, exist_ok=True)
    kind = _os_kind()
    if kind == "linux":
        info = _install_systemd(ws, port, force)
    elif kind == "macos":
        info = _install_launchd(ws, port, force)
    elif kind == "windows":
        info = _install_nssm(ws, port, force)
    else:
        print(f"[nerya] unsupported platform: {kind}")
        return 2
    print("[nerya] service installed:")
    for k, v in info.items():
        print(f"        {k}: {v}")
    return 0 if info.get("ok", True) else 1


def uninstall() -> int:
    kind = _os_kind()
    if kind == "linux":
        info = _uninstall_systemd()
    elif kind == "macos":
        info = _uninstall_launchd()
    elif kind == "windows":
        info = _uninstall_nssm()
    else:
        print(f"[nerya] unsupported platform: {kind}")
        return 2
    print("[nerya] uninstalled:")
    for k, v in info.items():
        print(f"        {k}: {v}")
    return 0


def status() -> dict[str, Any]:
    kind = _os_kind()
    if kind == "linux":
        return _status_systemd()
    if kind == "macos":
        return _status_launchd()
    if kind == "windows":
        return _status_nssm()
    return {"kind": kind, "error": "unsupported"}
