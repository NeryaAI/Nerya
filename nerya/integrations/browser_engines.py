"""Pluggable headless-browser engine registry.

Engines are available, all opt-in. Dependencies are **only**
installed when the operator explicitly enables a given engine from the
dashboard (or calls the matching ``/browsers/install`` API).

| engine        | kind        | install                                      | invoke                                               |
|---------------|-------------|----------------------------------------------|------------------------------------------------------|
| camofox       | node_service| clone + npm install local REST server        | ``POST /tabs`` + tab action REST endpoints           |
| cloakbrowser  | python_pkg  | ``pip install cloakbrowser``                 | python module ``cloakbrowser.launch()``              |
| lightpanda    | binary      | download GitHub release binary               | ``lightpanda fetch --dump markdown <URL>``           |
| obscura       | binary      | download GitHub release archive              | ``obscura fetch <URL> --dump html``                  |

State is persisted to ``<workspace>/browser_engines.json`` so the
dashboard can restore selection across reboots. Binaries live under
``<workspace>/state/browsers/<name>/``. Service checkouts live under
``<workspace>/state/browsers/<name>/repo``.

Recommended display order is **camofox → cloakbrowser → lightpanda →
obscura**. Camofox is first because its REST API is purpose-built for
agent snapshots and actions; CloakBrowser remains the richest CDP path
for console/network capture.
"""

from __future__ import annotations

import hashlib
import json
import os
import platform
import re
import shutil
import stat
import subprocess
import sys
import tarfile
import time
import urllib.error
import urllib.request
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from ..core.proxy import browser_proxy_config_for_workspace


_DEFAULT_TIMEOUT_S = 60.0
_DOWNLOAD_TIMEOUT_S = 600.0
_CAMOFOX_DEFAULT_URL = "http://127.0.0.1:9377"
_RECOMMENDED_ENGINE_ORDER = ("camofox", "cloakbrowser", "lightpanda", "obscura")
_SECRET_QUERY_KEYS = frozenset({
    "access_token", "api_key", "apikey", "auth", "authorization",
    "code", "key", "password", "refresh_token", "secret", "session",
    "sig", "signature", "token",
})
_SECRET_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"(?i)(bearer\s+)[a-z0-9._~+/=-]{12,}"),
    re.compile(
        r"(?i)\b(api[_-]?key|access[_-]?token|refresh[_-]?token|token|secret|password)"
        r"([:=]\s*)[^\s'\"&]{4,}"
    ),
)


# ---------------------------------------------------------------------------
# Spec registry
# ---------------------------------------------------------------------------


@dataclass
class BrowserSpec:
    name: str
    title: str
    kind: str  # "binary" | "python_pkg" | "node_service"
    summary: str
    homepage: str
    license: str
    # Binary kind
    release_assets: dict[str, str] = field(default_factory=dict)
    release_url_template: str = ""  # uses {asset} placeholder
    archive_format: str = ""  # ""|"tar.gz"|"zip" — "" means raw binary
    binary_in_archive: str = ""  # path inside extracted archive (for archive_format)
    extra_files_in_archive: tuple[str, ...] = ()
    binary_name: str = ""  # final exe filename (e.g. "lightpanda" / "lightpanda.exe")
    # Python kind
    pip_package: str = ""
    pip_extras: str = ""
    import_check: str = ""
    # Node/local service kind
    repo_url: str = ""
    service_base_url: str = ""
    service_health_path: str = "/health"
    npm_script: str = "start"
    # Common
    version_command: tuple[str, ...] = ()  # ["--version"] or similar
    fetch_template: str = ""  # CLI fetch command template; {bin} {url} placeholders
    serve_template: str = ""  # CLI serve template
    # Optional screenshot command template. ``{bin} {url} {path}`` placeholders.
    # When empty *and* kind=="python_pkg", the engine-specific Python branch
    # is used (e.g. Playwright ``page.screenshot(path=...)``).
    screenshot_template: str = ""
    notes: str = ""


_REGISTRY: dict[str, BrowserSpec] = {
    "camofox": BrowserSpec(
        name="camofox",
        title="Camofox Browser",
        kind="node_service",
        summary=(
            "Camoufox-powered anti-detection browser server for AI agents. "
            "Exposes REST tabs, accessibility snapshots, element refs, actions, "
            "screenshots, evaluation, and optional tracing."
        ),
        homepage="https://github.com/jo-inc/camofox-browser",
        license="MIT",
        repo_url="https://github.com/jo-inc/camofox-browser.git",
        service_base_url=_CAMOFOX_DEFAULT_URL,
        service_health_path="/health",
        npm_script="start",
        notes=(
            "Preferred interactive backend when the local service is running. "
            "Install clones the upstream Node service and runs npm install; "
            "start it with npm start or Docker, then point CAMOFOX_URL / "
            "NERYA_BROWSER_CAMOFOX_URL at the service."
        ),
    ),
    "lightpanda": BrowserSpec(
        name="lightpanda",
        title="Lightpanda",
        kind="binary",
        summary="Headless browser for AI agents, written in Zig. CDP-compatible, can dump pages as markdown.",
        homepage="https://github.com/lightpanda-io/browser",
        license="AGPL-3.0-only",
        release_assets={
            "linux-x86_64": "lightpanda-x86_64-linux",
            "linux-aarch64": "lightpanda-aarch64-linux",
            "darwin-x86_64": "lightpanda-x86_64-macos",
            "darwin-aarch64": "lightpanda-aarch64-macos",
        },
        release_url_template="https://github.com/lightpanda-io/browser/releases/download/nightly/{asset}",
        archive_format="",
        binary_name="lightpanda",
        version_command=("--version",),
        fetch_template="{bin} fetch --dump markdown {url}",
        serve_template="{bin} serve --host 127.0.0.1 --port {port}",
        # Lightpanda's CLI flag for PNG dumps. Recent nightly builds expose
        # ``--screenshot <path>`` on the ``fetch`` subcommand. If older
        # binaries reject the flag, the screenshot endpoint surfaces the
        # stderr tail so the operator can update.
        screenshot_template="{bin} fetch --screenshot {path} {url}",
        notes=(
            "Upstream does not publish Windows binaries — on Windows this engine "
            "will install only via WSL or a custom build. Use cloakbrowser or "
            "obscura instead."
        ),
    ),
    "cloakbrowser": BrowserSpec(
        name="cloakbrowser",
        title="CloakBrowser",
        kind="python_pkg",
        summary="Stealth Chromium with C++ source-level fingerprint patches. Drop-in Playwright replacement.",
        homepage="https://github.com/CloakHQ/CloakBrowser",
        license="see upstream",
        pip_package="cloakbrowser",
        import_check="cloakbrowser",
        notes=(
            "First launch downloads ~200MB stealth Chromium binary into the "
            "package cache. Optional ``cloakbrowser[geoip]`` extra for proxy "
            "auto-locale."
        ),
    ),
    "obscura": BrowserSpec(
        name="obscura",
        title="Obscura",
        kind="binary",
        summary="Rust headless browser with V8 + CDP. ~30MB RAM, built-in stealth, Puppeteer/Playwright compatible.",
        homepage="https://github.com/h4ckf0r0day/obscura",
        license="Apache-2.0",
        release_assets={
            "linux-x86_64": "obscura-x86_64-linux.tar.gz",
            "darwin-aarch64": "obscura-aarch64-macos.tar.gz",
            "darwin-x86_64": "obscura-x86_64-macos.tar.gz",
            "windows-x86_64": "obscura-x86_64-windows.zip",
        },
        release_url_template="https://github.com/h4ckf0r0day/obscura/releases/latest/download/{asset}",
        archive_format="auto",
        binary_in_archive="obscura",
        extra_files_in_archive=("obscura-worker",),
        binary_name="obscura",
        version_command=("--version",),
        fetch_template="{bin} fetch {url} --dump html",
        serve_template="{bin} serve --port {port} --stealth",
        screenshot_template="{bin} fetch {url} --screenshot {path}",
    ),
}


# ---------------------------------------------------------------------------
# Workspace state
# ---------------------------------------------------------------------------


def _state_file(workspace_root: Path | str) -> Path:
    return Path(workspace_root) / "browser_engines.json"


def _state_dir(workspace_root: Path | str) -> Path:
    return Path(workspace_root) / "state" / "browsers"


def _read_state(workspace_root: Path | str) -> dict[str, Any]:
    path = _state_file(workspace_root)
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8")) or {}
    except Exception:
        return {}


def _write_state(workspace_root: Path | str, data: dict[str, Any]) -> None:
    path = _state_file(workspace_root)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2),
                    encoding="utf-8")


# ---------------------------------------------------------------------------
# Platform helpers
# ---------------------------------------------------------------------------


def _platform_token() -> str:
    sys_name = platform.system().lower()
    if sys_name == "darwin":
        os_token = "darwin"
    elif sys_name == "linux":
        os_token = "linux"
    elif sys_name in ("windows", "win32"):
        os_token = "windows"
    else:
        os_token = sys_name
    machine = platform.machine().lower()
    if machine in ("amd64", "x86_64"):
        arch_token = "x86_64"
    elif machine in ("arm64", "aarch64"):
        arch_token = "aarch64"
    else:
        arch_token = machine or "x86_64"
    return f"{os_token}-{arch_token}"


def _exe_suffix() -> str:
    return ".exe" if platform.system().lower().startswith("win") else ""


def _is_windows() -> bool:
    return platform.system().lower().startswith("win")


# ---------------------------------------------------------------------------
# Binary install
# ---------------------------------------------------------------------------


def _binary_dir(workspace_root: Path | str, name: str) -> Path:
    return _state_dir(workspace_root) / name


def _binary_path(workspace_root: Path | str, spec: BrowserSpec) -> Path:
    return _binary_dir(workspace_root, spec.name) / (
        spec.binary_name + _exe_suffix()
    )


def _service_dir(workspace_root: Path | str, spec: BrowserSpec) -> Path:
    return _state_dir(workspace_root) / spec.name / "repo"


def _system_binary(spec: BrowserSpec) -> str:
    """Look for the binary on PATH (e.g. system-wide install)."""
    candidate = shutil.which(spec.binary_name)
    return candidate or ""


def _download(url: str, dest: Path) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    req = urllib.request.Request(url, headers={"User-Agent": "Nerya/installer"})
    with urllib.request.urlopen(req, timeout=_DOWNLOAD_TIMEOUT_S) as resp:
        if resp.status >= 400:
            raise RuntimeError(f"HTTP {resp.status} downloading {url}")
        with open(dest, "wb") as fp:
            shutil.copyfileobj(resp, fp)


def _extract_archive(archive: Path, dest_dir: Path,
                     archive_format: str) -> None:
    dest_dir.mkdir(parents=True, exist_ok=True)
    fmt = archive_format
    if fmt == "auto":
        suffix = "".join(archive.suffixes).lower()
        if suffix.endswith(".tar.gz") or suffix.endswith(".tgz"):
            fmt = "tar.gz"
        elif suffix.endswith(".zip"):
            fmt = "zip"
        else:
            raise RuntimeError(f"unknown archive type: {archive.name}")
    if fmt == "tar.gz":
        with tarfile.open(archive, mode="r:gz") as tf:
            tf.extractall(dest_dir)
    elif fmt == "zip":
        with zipfile.ZipFile(archive, "r") as zf:
            zf.extractall(dest_dir)
    else:
        raise RuntimeError(f"unsupported archive format: {fmt}")


def _resolve_archive_member(extract_root: Path, member: str) -> Path | None:
    direct = extract_root / member
    if direct.exists():
        return direct
    candidate = extract_root / (member + _exe_suffix())
    if candidate.exists():
        return candidate
    # Fallback: walk to find the exact file name match (handles archives
    # that wrap their files in a versioned subdir).
    target_name = member + _exe_suffix()
    for path in extract_root.rglob(target_name):
        return path
    for path in extract_root.rglob(member):
        return path
    return None


def _make_executable(path: Path) -> None:
    if _is_windows():
        return
    try:
        st = path.stat()
        path.chmod(st.st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    except Exception:
        pass


def _install_binary(workspace_root: Path | str, spec: BrowserSpec) -> dict[str, Any]:
    plat = _platform_token()
    asset = spec.release_assets.get(plat)
    if not asset:
        return {
            "ok": False,
            "name": spec.name,
            "error": "platform_unsupported",
            "platform": plat,
            "supported": sorted(spec.release_assets.keys()),
            "detail": f"{spec.title} has no upstream binary for {plat}",
        }
    url = spec.release_url_template.format(asset=asset)
    dest_dir = _binary_dir(workspace_root, spec.name)
    dest_dir.mkdir(parents=True, exist_ok=True)

    if spec.archive_format:
        archive_path = dest_dir / asset
    else:
        archive_path = dest_dir / (spec.binary_name + _exe_suffix())

    started = time.monotonic()
    try:
        _download(url, archive_path)
    except urllib.error.HTTPError as exc:
        return {
            "ok": False, "name": spec.name, "error": "download_failed",
            "detail": f"HTTP {exc.code}: {exc.reason}", "url": url,
        }
    except Exception as exc:  # noqa: BLE001
        return {
            "ok": False, "name": spec.name, "error": "download_failed",
            "detail": f"{type(exc).__name__}: {exc}", "url": url,
        }

    if spec.archive_format:
        try:
            _extract_archive(archive_path, dest_dir,
                             spec.archive_format)
        except Exception as exc:  # noqa: BLE001
            return {
                "ok": False, "name": spec.name, "error": "extract_failed",
                "detail": f"{type(exc).__name__}: {exc}",
            }
        # Move primary binary + any worker companions to the top of dest_dir
        member = spec.binary_in_archive or spec.binary_name
        resolved = _resolve_archive_member(dest_dir, member)
        if resolved is None:
            return {
                "ok": False, "name": spec.name, "error": "binary_missing",
                "detail": f"{member} not found in archive",
            }
        target = dest_dir / (spec.binary_name + _exe_suffix())
        if resolved.resolve() != target.resolve():
            shutil.move(str(resolved), str(target))
        for extra in spec.extra_files_in_archive:
            extra_path = _resolve_archive_member(dest_dir, extra)
            if extra_path is None:
                continue
            extra_target = dest_dir / (extra + _exe_suffix())
            if extra_path.resolve() != extra_target.resolve():
                shutil.move(str(extra_path), str(extra_target))
        try:
            archive_path.unlink()
        except Exception:
            pass
    target = _binary_path(workspace_root, spec)
    _make_executable(target)
    if spec.extra_files_in_archive:
        for extra in spec.extra_files_in_archive:
            _make_executable(dest_dir / (extra + _exe_suffix()))

    elapsed_ms = int((time.monotonic() - started) * 1000)
    version = _probe_version(target, spec)
    return {
        "ok": True, "name": spec.name,
        "binary": str(target),
        "version": version,
        "elapsed_ms": elapsed_ms,
        "asset": asset,
        "platform": plat,
    }


def _uninstall_binary(workspace_root: Path | str, spec: BrowserSpec) -> dict[str, Any]:
    target_dir = _binary_dir(workspace_root, spec.name)
    if not target_dir.exists():
        return {"ok": True, "name": spec.name, "removed": False}
    try:
        shutil.rmtree(target_dir)
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "name": spec.name, "error": f"{type(exc).__name__}: {exc}"}
    return {"ok": True, "name": spec.name, "removed": True}


def _probe_version(binary: Path | str, spec: BrowserSpec) -> str:
    if not spec.version_command:
        return ""
    try:
        res = subprocess.run(
            [str(binary), *spec.version_command],
            capture_output=True, text=True, timeout=10,
        )
    except Exception:
        return ""
    out = (res.stdout or "").strip()
    if not out and res.stderr:
        out = res.stderr.strip()
    return out.splitlines()[0][:200] if out else ""


# ---------------------------------------------------------------------------
# Python pkg install
# ---------------------------------------------------------------------------


def _module_present(module: str) -> bool:
    if not module:
        return False
    import importlib.util
    return importlib.util.find_spec(module) is not None


def _module_version(module: str) -> str:
    if not module:
        return ""
    try:
        from importlib.metadata import version
        return version(module) or ""
    except Exception:
        return ""


def _install_python(spec: BrowserSpec) -> dict[str, Any]:
    if not spec.pip_package:
        return {"ok": False, "name": spec.name, "error": "no_pip_package"}
    started = time.monotonic()
    args = [sys.executable, "-m", "pip", "install", "--upgrade"]
    pkg = spec.pip_package + (f"[{spec.pip_extras}]" if spec.pip_extras else "")
    args.append(pkg)
    try:
        res = subprocess.run(args, capture_output=True, text=True, timeout=900)
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "name": spec.name, "error": "pip_failed",
                "detail": f"{type(exc).__name__}: {exc}"}
    elapsed_ms = int((time.monotonic() - started) * 1000)
    if res.returncode != 0:
        return {
            "ok": False, "name": spec.name, "error": "pip_failed",
            "returncode": res.returncode,
            "stderr_tail": (res.stderr or "")[-2000:],
            "elapsed_ms": elapsed_ms,
        }
    return {
        "ok": True, "name": spec.name,
        "package": pkg, "version": _module_version(spec.import_check),
        "elapsed_ms": elapsed_ms,
        "stdout_tail": (res.stdout or "")[-1000:],
    }


def _uninstall_python(spec: BrowserSpec) -> dict[str, Any]:
    if not spec.pip_package:
        return {"ok": False, "name": spec.name, "error": "no_pip_package"}
    args = [sys.executable, "-m", "pip", "uninstall", "-y", spec.pip_package]
    try:
        res = subprocess.run(args, capture_output=True, text=True, timeout=120)
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "name": spec.name, "error": "pip_failed",
                "detail": f"{type(exc).__name__}: {exc}"}
    return {
        "ok": res.returncode == 0,
        "name": spec.name,
        "returncode": res.returncode,
        "stdout_tail": (res.stdout or "")[-500:],
        "stderr_tail": (res.stderr or "")[-500:],
    }


# ---------------------------------------------------------------------------
# Local Node service install / probe
# ---------------------------------------------------------------------------


def _safe_child_path(parent: Path, child: Path) -> bool:
    try:
        child.resolve().relative_to(parent.resolve())
        return True
    except Exception:
        return False


def _service_url(
    workspace_root: Path | str,
    spec: BrowserSpec,
    *,
    state: dict[str, Any] | None = None,
) -> str:
    env_names = (
        f"NERYA_BROWSER_{spec.name.upper()}_URL",
        f"{spec.name.upper()}_BROWSER_URL",
        f"{spec.name.upper()}_URL",
    )
    for key in env_names:
        value = os.environ.get(key)
        if value:
            return value.rstrip("/")

    data = state if state is not None else _read_state(workspace_root)
    service_urls = data.get("service_urls") if isinstance(data, dict) else None
    if isinstance(service_urls, dict):
        value = str(service_urls.get(spec.name) or "").strip()
        if value:
            return value.rstrip("/")
    return (spec.service_base_url or "").rstrip("/")


def _append_query(url: str, params: dict[str, Any] | None) -> str:
    clean = {
        key: value
        for key, value in (params or {}).items()
        if value is not None and value != ""
    }
    if not clean:
        return url
    sep = "&" if "?" in url else "?"
    return url + sep + urlencode(clean)


def _redact_text(value: Any, limit: int = 8000) -> str:
    text = str(value or "")
    for rx in _SECRET_PATTERNS:
        if rx.groups >= 2:
            text = rx.sub(lambda m: f"{m.group(1)}{m.group(2)}[redacted]", text)
        else:
            text = rx.sub(lambda m: f"{m.group(1)}[redacted]", text)
    if len(text) > limit:
        return text[:limit] + "\n[truncated]"
    return text


def _redact_url(raw: Any) -> str:
    url = str(raw or "")
    if not url:
        return ""
    try:
        parts = urlsplit(url)
        query = urlencode(
            [
                (
                    key,
                    "[redacted]" if key.lower() in _SECRET_QUERY_KEYS else value,
                )
                for key, value in parse_qsl(parts.query, keep_blank_values=True)
            ],
            doseq=True,
        )
        fragment = "[redacted]" if parts.fragment else ""
        return urlunsplit((parts.scheme, parts.netloc, parts.path, query, fragment))
    except Exception:
        return _redact_text(url, 2000)


def _http_json(
    method: str,
    url: str,
    *,
    payload: dict[str, Any] | None = None,
    params: dict[str, Any] | None = None,
    timeout_s: float = _DEFAULT_TIMEOUT_S,
    raw: bool = False,
) -> dict[str, Any]:
    body = None
    headers = {"Accept": "application/json"}
    if payload is not None:
        body = json.dumps(payload, ensure_ascii=False, default=str).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(
        _append_query(url, params),
        data=body,
        headers=headers,
        method=method.upper(),
    )
    with urllib.request.urlopen(req, timeout=timeout_s) as resp:
        data = resp.read()
        headers_out = dict(resp.headers.items())
        if raw:
            return {
                "ok": 200 <= resp.status < 300,
                "status": resp.status,
                "body": data,
                "headers": headers_out,
            }
        text = data.decode("utf-8", errors="replace")
        parsed: Any = {}
        if text.strip():
            try:
                parsed = json.loads(text)
            except Exception:
                parsed = {"text": text}
        if not isinstance(parsed, dict):
            parsed = {"value": parsed}
        parsed.setdefault("ok", 200 <= resp.status < 300)
        parsed.setdefault("status", resp.status)
        return parsed


def _service_health(
    workspace_root: Path | str,
    spec: BrowserSpec,
    *,
    state: dict[str, Any],
    timeout_s: float = 1.5,
) -> dict[str, Any]:
    url = _service_url(workspace_root, spec, state=state)
    if not url:
        return {"ok": False, "service_url": ""}
    try:
        data = _http_json(
            "GET",
            url + (spec.service_health_path or "/health"),
            timeout_s=timeout_s,
        )
        return {
            "ok": bool(data.get("ok", True)),
            "service_url": url,
            "health": data,
        }
    except Exception as exc:  # noqa: BLE001
        return {
            "ok": False,
            "service_url": url,
            "error": f"{type(exc).__name__}: {exc}",
        }


def _install_node_service(workspace_root: Path | str, spec: BrowserSpec) -> dict[str, Any]:
    if not spec.repo_url:
        return {"ok": False, "name": spec.name, "error": "no_repo_url"}
    git = shutil.which("git")
    npm = shutil.which("npm")
    if not git:
        return {"ok": False, "name": spec.name, "error": "git_missing"}
    target = _service_dir(workspace_root, spec)
    root = _state_dir(workspace_root)
    if not _safe_child_path(root, target):
        return {"ok": False, "name": spec.name, "error": "invalid_install_path"}
    target.parent.mkdir(parents=True, exist_ok=True)
    started = time.monotonic()

    if target.exists() and not (target / ".git").exists():
        if any(target.iterdir()):
            return {
                "ok": False,
                "name": spec.name,
                "error": "service_dir_exists",
                "path": str(target),
            }
    try:
        if (target / ".git").exists():
            res = subprocess.run(
                [git, "-C", str(target), "pull", "--ff-only"],
                capture_output=True,
                text=True,
                timeout=120,
            )
        else:
            res = subprocess.run(
                [git, "clone", "--depth", "1", spec.repo_url, str(target)],
                capture_output=True,
                text=True,
                timeout=300,
            )
        if res.returncode != 0:
            return {
                "ok": False,
                "name": spec.name,
                "error": "git_failed",
                "returncode": res.returncode,
                "stderr_tail": (res.stderr or "")[-2000:],
            }
    except Exception as exc:  # noqa: BLE001
        return {
            "ok": False,
            "name": spec.name,
            "error": "git_failed",
            "detail": f"{type(exc).__name__}: {exc}",
        }

    if not npm:
        return {
            "ok": False,
            "name": spec.name,
            "error": "npm_missing",
            "path": str(target),
            "detail": "Repository cloned, but npm is not available on PATH.",
        }
    try:
        res = subprocess.run(
            [npm, "install"],
            cwd=str(target),
            capture_output=True,
            text=True,
            timeout=900,
        )
    except Exception as exc:  # noqa: BLE001
        return {
            "ok": False,
            "name": spec.name,
            "error": "npm_failed",
            "detail": f"{type(exc).__name__}: {exc}",
            "path": str(target),
        }
    elapsed_ms = int((time.monotonic() - started) * 1000)
    if res.returncode != 0:
        return {
            "ok": False,
            "name": spec.name,
            "error": "npm_failed",
            "returncode": res.returncode,
            "stderr_tail": (res.stderr or "")[-2000:],
            "stdout_tail": (res.stdout or "")[-1000:],
            "elapsed_ms": elapsed_ms,
            "path": str(target),
        }
    return {
        "ok": True,
        "name": spec.name,
        "path": str(target),
        "service_url": spec.service_base_url,
        "start_hint": f"cd {target} && npm run {spec.npm_script}",
        "elapsed_ms": elapsed_ms,
        "stdout_tail": (res.stdout or "")[-1000:],
    }


def _uninstall_node_service(workspace_root: Path | str, spec: BrowserSpec) -> dict[str, Any]:
    target = _service_dir(workspace_root, spec)
    root = _state_dir(workspace_root)
    if not _safe_child_path(root, target):
        return {"ok": False, "name": spec.name, "error": "invalid_install_path"}
    if not target.exists():
        return {"ok": True, "name": spec.name, "removed": False}
    try:
        shutil.rmtree(target)
    except Exception as exc:  # noqa: BLE001
        return {
            "ok": False,
            "name": spec.name,
            "error": f"{type(exc).__name__}: {exc}",
        }
    return {"ok": True, "name": spec.name, "removed": True}


# ---------------------------------------------------------------------------
# Public surface
# ---------------------------------------------------------------------------


def list_specs() -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for rank, spec in enumerate(_ordered_specs(), start=1):
        out.append({
            "name": spec.name,
            "title": spec.title,
            "kind": spec.kind,
            "recommended_rank": rank,
            "summary": spec.summary,
            "homepage": spec.homepage,
            "license": spec.license,
            "supported_platforms": (
                sorted(spec.release_assets.keys()) if spec.kind == "binary" else None
            ),
            "pip_package": spec.pip_package or None,
            "repo_url": spec.repo_url or None,
            "service_url": spec.service_base_url or None,
            "notes": spec.notes,
        })
    return out


def _ordered_specs() -> list[BrowserSpec]:
    specs: list[BrowserSpec] = []
    seen: set[str] = set()
    for name in _RECOMMENDED_ENGINE_ORDER:
        spec = _REGISTRY.get(name)
        if spec is not None:
            specs.append(spec)
            seen.add(name)
    for name, spec in _REGISTRY.items():
        if name not in seen:
            specs.append(spec)
    return specs


def _engine_status(workspace_root: Path | str, spec: BrowserSpec,
                   *, state: dict[str, Any]) -> dict[str, Any]:
    plat = _platform_token()
    if spec.kind == "binary":
        path = _binary_path(workspace_root, spec)
        managed_present = path.exists()
        system_path = _system_binary(spec) if not managed_present else ""
        binary_path = str(path) if managed_present else system_path
        installed = bool(binary_path)
        version = (_probe_version(binary_path, spec)
                   if installed else "")
        supported = plat in spec.release_assets
        return {
            "name": spec.name,
            "kind": spec.kind,
            "installed": installed,
            "managed": managed_present,
            "binary_path": binary_path,
            "version": version,
            "platform": plat,
            "platform_supported": supported,
            "asset": spec.release_assets.get(plat, "") if supported else "",
        }
    if spec.kind == "python_pkg":
        present = _module_present(spec.import_check or spec.pip_package)
        return {
            "name": spec.name,
            "kind": spec.kind,
            "installed": present,
            "managed": present,
            "module": spec.import_check or spec.pip_package,
            "version": _module_version(spec.import_check or spec.pip_package),
            "platform": plat,
            "platform_supported": True,
        }
    if spec.kind == "node_service":
        checkout = _service_dir(workspace_root, spec)
        health = _service_health(workspace_root, spec, state=state)
        return {
            "name": spec.name,
            "kind": spec.kind,
            "installed": checkout.exists() or bool(health.get("ok")),
            "ready": bool(health.get("ok")),
            "managed": checkout.exists(),
            "checkout_path": str(checkout) if checkout.exists() else "",
            "service_url": health.get("service_url") or spec.service_base_url,
            "service_ready": bool(health.get("ok")),
            "service_error": health.get("error", ""),
            "health": health.get("health") if health.get("ok") else None,
            "platform": plat,
            "platform_supported": True,
        }
    return {"name": spec.name, "kind": spec.kind, "installed": False}


def status(workspace_root: Path | str) -> dict[str, Any]:
    state = _read_state(workspace_root)
    selected = (state.get("selected") or "").strip().lower()
    enabled = state.get("enabled") or {}
    if not isinstance(enabled, dict):
        enabled = {}
    rows: list[dict[str, Any]] = []
    for rank, spec in enumerate(_ordered_specs(), start=1):
        row = _engine_status(workspace_root, spec, state=state)
        row["enabled"] = bool(enabled.get(spec.name, False))
        row["recommended_rank"] = rank
        row["title"] = spec.title
        row["summary"] = spec.summary
        row["homepage"] = spec.homepage
        row["pip_package"] = spec.pip_package or None
        row["repo_url"] = spec.repo_url or None
        row["notes"] = spec.notes
        rows.append(row)
    # Auto-fall-back if selected engine is no longer installed.
    if not selected:
        for row in rows:
            ready = row.get("ready") if "ready" in row else row.get("installed")
            if row.get("enabled") and row.get("installed") and ready:
                selected = row["name"]
                break
    return {
        "ok": True,
        "engines": rows,
        "selected": selected or None,
        "platform": _platform_token(),
        "state_file": str(_state_file(workspace_root)),
        "binaries_dir": str(_state_dir(workspace_root)),
    }


def configure(
    workspace_root: Path | str,
    *,
    selected: str | None = None,
    enabled: dict[str, bool] | None = None,
) -> dict[str, Any]:
    state = _read_state(workspace_root)
    cur_enabled = state.get("enabled") or {}
    if not isinstance(cur_enabled, dict):
        cur_enabled = {}
    if enabled:
        for name, value in enabled.items():
            if name not in _REGISTRY:
                continue
            cur_enabled[name] = bool(value)
    if selected is not None:
        sel_norm = selected.strip().lower() or ""
        if sel_norm and sel_norm not in _REGISTRY:
            return {"ok": False, "error": "unknown_engine", "name": sel_norm}
        state["selected"] = sel_norm
        if sel_norm:
            cur_enabled[sel_norm] = True
    state["enabled"] = cur_enabled
    _write_state(workspace_root, state)
    return status(workspace_root)


def install(workspace_root: Path | str, name: str) -> dict[str, Any]:
    spec = _REGISTRY.get(name)
    if not spec:
        return {"ok": False, "error": "unknown_engine", "name": name}
    if spec.kind == "binary":
        result = _install_binary(workspace_root, spec)
    elif spec.kind == "python_pkg":
        result = _install_python(spec)
    elif spec.kind == "node_service":
        result = _install_node_service(workspace_root, spec)
    else:
        return {"ok": False, "error": "unsupported_kind", "name": name}
    if result.get("ok"):
        # Auto-enable on successful install + select if nothing else picked.
        state = _read_state(workspace_root)
        enabled = state.get("enabled") or {}
        if not isinstance(enabled, dict):
            enabled = {}
        enabled[name] = True
        state["enabled"] = enabled
        if not state.get("selected"):
            if spec.kind == "node_service":
                health = _service_health(workspace_root, spec, state=state)
                if health.get("ok"):
                    state["selected"] = name
            else:
                state["selected"] = name
        _write_state(workspace_root, state)
    result["status"] = status(workspace_root)
    return result


def uninstall(workspace_root: Path | str, name: str) -> dict[str, Any]:
    spec = _REGISTRY.get(name)
    if not spec:
        return {"ok": False, "error": "unknown_engine", "name": name}
    if spec.kind == "binary":
        result = _uninstall_binary(workspace_root, spec)
    elif spec.kind == "python_pkg":
        result = _uninstall_python(spec)
    elif spec.kind == "node_service":
        result = _uninstall_node_service(workspace_root, spec)
    else:
        return {"ok": False, "error": "unsupported_kind"}
    state = _read_state(workspace_root)
    enabled = state.get("enabled") or {}
    if isinstance(enabled, dict):
        enabled[name] = False
        state["enabled"] = enabled
    if state.get("selected") == name:
        state["selected"] = ""
    _write_state(workspace_root, state)
    result["status"] = status(workspace_root)
    return result


# ---------------------------------------------------------------------------
# Fetch helper (used by ``research/fetch_url`` as a tier)
# ---------------------------------------------------------------------------


def resolve_binary(workspace_root: Path | str, name: str) -> str:
    spec = _REGISTRY.get(name)
    if not spec:
        return ""
    if spec.kind != "binary":
        return ""
    managed = _binary_path(workspace_root, spec)
    if managed.exists():
        return str(managed)
    return _system_binary(spec)


def _camofox_spec() -> BrowserSpec:
    return _REGISTRY["camofox"]


def _camofox_identity(workspace_root: Path | str, session_id: str) -> dict[str, str]:
    root = str(Path(workspace_root).resolve())
    sid = session_id or "default"
    user_digest = hashlib.sha256(f"nerya-camofox-user:{root}".encode()).hexdigest()[:10]
    sess_digest = hashlib.sha256(f"nerya-camofox-session:{root}:{sid}".encode()).hexdigest()[:16]
    return {
        "user_id": f"nerya_{user_digest}",
        "session_key": f"task_{sess_digest}",
    }


def _camofox_base_from_runtime(runtime: dict[str, Any]) -> str:
    return str(runtime.get("service_url") or _CAMOFOX_DEFAULT_URL).rstrip("/")


def camofox_open_tab(
    workspace_root: Path | str,
    *,
    session_id: str,
    url: str,
    timeout_s: float = _DEFAULT_TIMEOUT_S,
    trace: bool = True,
) -> dict[str, Any]:
    """Create a Camofox tab and return runtime metadata."""
    spec = _camofox_spec()
    state = _read_state(workspace_root)
    base = _service_url(workspace_root, spec, state=state)
    if not base:
        return {"ok": False, "error": "service_url_missing", "name": spec.name}
    identity = _camofox_identity(workspace_root, session_id)
    payload = {
        "userId": identity["user_id"],
        "sessionKey": identity["session_key"],
        "url": url,
        "trace": bool(trace),
    }
    try:
        data = _http_json("POST", f"{base}/tabs", payload=payload, timeout_s=timeout_s)
    except Exception as exc:  # noqa: BLE001
        return {
            "ok": False,
            "error": "service_unavailable",
            "name": spec.name,
            "service_url": base,
            "detail": f"{type(exc).__name__}: {exc}",
        }
    tab_id = str(data.get("tabId") or data.get("id") or "").strip()
    if not tab_id:
        return {
            "ok": False,
            "error": "tab_id_missing",
            "name": spec.name,
            "service_url": base,
            "response": data,
        }
    runtime = {
        "service_url": base,
        "user_id": identity["user_id"],
        "session_key": identity["session_key"],
        "tab_id": tab_id,
        "current_url": str(data.get("url") or url),
    }
    result: dict[str, Any] = {
        "ok": True,
        "name": spec.name,
        "service_url": base,
        "tab_id": tab_id,
        "user_id": identity["user_id"],
        "session_key": identity["session_key"],
        "current_url": runtime["current_url"],
        "runtime": runtime,
        "response": data,
    }
    try:
        result["snapshot"] = camofox_snapshot_runtime(runtime, max_chars=12000, timeout_s=timeout_s)
    except Exception:
        pass
    return result


def camofox_snapshot_runtime(
    runtime: dict[str, Any],
    *,
    max_chars: int = 8000,
    include_screenshot: bool = False,
    timeout_s: float = _DEFAULT_TIMEOUT_S,
) -> dict[str, Any]:
    base = _camofox_base_from_runtime(runtime)
    tab_id = str(runtime.get("tab_id") or "")
    user_id = str(runtime.get("user_id") or "")
    data = _http_json(
        "GET",
        f"{base}/tabs/{tab_id}/snapshot",
        params={
            "userId": user_id,
            "includeScreenshot": "true" if include_screenshot else None,
        },
        timeout_s=timeout_s,
    )
    snap = str(data.get("snapshot") or data.get("text") or "")
    if max_chars > 0 and len(snap) > max_chars:
        snap = snap[:max_chars] + "\n[truncated]"
    return {
        "ok": True,
        "snapshot": snap,
        "text": snap,
        "element_count": data.get("refsCount") or data.get("elementCount") or 0,
        "current_url": runtime.get("current_url") or "",
        "response": data,
    }


def camofox_screenshot_runtime(
    runtime: dict[str, Any],
    *,
    out_path: Path | str,
    timeout_s: float = _DEFAULT_TIMEOUT_S,
) -> dict[str, Any]:
    base = _camofox_base_from_runtime(runtime)
    tab_id = str(runtime.get("tab_id") or "")
    user_id = str(runtime.get("user_id") or "")
    data = _http_json(
        "GET",
        f"{base}/tabs/{tab_id}/screenshot",
        params={"userId": user_id},
        timeout_s=timeout_s,
        raw=True,
    )
    raw = data.get("body") or b""
    path = Path(out_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(raw)
    return {
        "ok": True,
        "name": "camofox",
        "path": str(path),
        "bytes": len(raw),
        "fetch_method": "camofox_screenshot",
    }


def camofox_action_runtime(
    runtime: dict[str, Any],
    action: str,
    params: dict[str, Any],
    *,
    timeout_s: float = _DEFAULT_TIMEOUT_S,
) -> dict[str, Any]:
    base = _camofox_base_from_runtime(runtime)
    tab_id = str(runtime.get("tab_id") or "")
    user_id = str(runtime.get("user_id") or "")
    if not tab_id or not user_id:
        return {"ok": False, "error": "camofox_runtime_incomplete"}

    def _post_tab(endpoint: str, body: dict[str, Any], *, timeout: float | None = None) -> dict[str, Any]:
        payload = {"userId": user_id, **body}
        return _http_json(
            "POST",
            f"{base}/tabs/{tab_id}/{endpoint}",
            payload=payload,
            timeout_s=timeout if timeout is not None else timeout_s,
        )

    if action == "click_xy":
        x = float(params.get("x") or 0)
        y = float(params.get("y") or 0)
        data = _post_tab("click", {"coordinates": {"x": x, "y": y}})
        return {"ok": True, "action": action, "click": {"x": x, "y": y}, "response": data}
    if action == "click_selector":
        selector = str(params.get("selector") or "").strip()
        ref = str(params.get("ref") or "").strip().lstrip("@")
        if not selector and not ref:
            return {"ok": False, "error": "selector_or_ref_required"}
        data = _post_tab("click", {"selector": selector or None, "ref": ref or None})
        return {"ok": True, "action": action, "selector": selector, "ref": ref, "response": data}
    if action == "type":
        text = str(params.get("text") or "")
        if text == "":
            return {"ok": False, "error": "text_required"}
        selector = str(params.get("selector") or "").strip()
        ref = str(params.get("ref") or "").strip().lstrip("@")
        data = _post_tab(
            "type",
            {
                "text": text,
                "selector": selector or None,
                "ref": ref or None,
                "clear": bool(params.get("clear", False)),
                "submit": bool(params.get("submit", False)),
            },
        )
        return {"ok": True, "action": action, "typed": len(text), "response": data}
    if action == "press":
        key = str(params.get("key") or "").strip()
        if not key:
            return {"ok": False, "error": "key_required"}
        data = _post_tab("press", {"key": key})
        return {"ok": True, "action": action, "key": key, "response": data}
    if action == "scroll":
        dy = float(params.get("dy") or params.get("amount") or 0)
        direction = str(params.get("direction") or "").strip().lower()
        if not direction:
            direction = "up" if dy < 0 else "down"
        amount = int(abs(dy)) if dy else int(params.get("amount") or 600)
        data = _post_tab("scroll", {"direction": direction, "amount": amount})
        return {
            "ok": True,
            "action": action,
            "direction": direction,
            "amount": amount,
            "response": data,
        }
    if action == "drag":
        ref = str(
            params.get("source_ref")
            or params.get("ref")
            or ""
        ).strip().lstrip("@")
        selector = str(
            params.get("source_selector")
            or params.get("selector")
            or ""
        ).strip()
        target_id = str(
            params.get("target_ref")
            or params.get("target_id")
            or params.get("targetId")
            or ""
        ).strip().lstrip("@")
        if not (ref or selector) or not target_id:
            return {
                "ok": False,
                "error": "drag_requires_refs",
                "detail": (
                    "Camofox drag uses the OpenClaw /act endpoint and needs "
                    "a source ref/selector plus a target ref. Use cloakbrowser "
                    "for coordinate drag."
                ),
            }
        data = _http_json(
            "POST",
            f"{base}/act",
            payload={
                "userId": user_id,
                "kind": "drag",
                "ref": ref or None,
                "selector": selector or None,
                "targetId": target_id,
            },
            timeout_s=timeout_s,
        )
        return {
            "ok": True,
            "action": action,
            "ref": ref,
            "selector": selector,
            "target_id": target_id,
            "response": data,
        }
    if action == "goto":
        target = str(params.get("url") or "").strip()
        if not target:
            return {"ok": False, "error": "url_required"}
        data = _post_tab("navigate", {"url": target}, timeout=float(params.get("timeout_s") or 60))
        current = str(data.get("url") or target)
        runtime["current_url"] = current
        return {"ok": True, "action": action, "url": current, "current_url": current, "response": data}
    if action == "go_back":
        data = _post_tab("back", {})
        current = str(data.get("url") or runtime.get("current_url") or "")
        runtime["current_url"] = current
        return {"ok": True, "action": action, "url": current, "current_url": current, "response": data}
    if action == "go_forward":
        data = _post_tab("forward", {})
        current = str(data.get("url") or runtime.get("current_url") or "")
        runtime["current_url"] = current
        return {"ok": True, "action": action, "url": current, "current_url": current, "response": data}
    if action == "reload":
        data = _post_tab("refresh", {})
        current = str(data.get("url") or runtime.get("current_url") or "")
        runtime["current_url"] = current
        return {"ok": True, "action": action, "url": current, "current_url": current, "response": data}
    if action == "eval":
        expr = str(params.get("expression") or "")
        if not expr:
            return {"ok": False, "error": "expression_required"}
        data = _post_tab("evaluate", {"expression": expr})
        return {"ok": True, "action": action, "value": data.get("result", data)}
    if action == "api_fetch":
        target = str(params.get("url") or "").strip()
        if not target:
            return {"ok": False, "error": "url_required"}
        method = str(params.get("method") or "GET").upper()
        headers = params.get("headers") if isinstance(params.get("headers"), dict) else {}
        body_value = params.get("body")
        if "json" in params:
            body_value = json.dumps(params.get("json"), ensure_ascii=False)
            headers = dict(headers or {})
            headers.setdefault("content-type", "application/json")
        if body_value is not None and not isinstance(body_value, str):
            body_value = json.dumps(body_value, ensure_ascii=False, default=str)
        max_chars = max(0, min(int(params.get("max_chars") or 8000), 50000))
        expression = f"""
            (async () => {{
              const response = await fetch({json.dumps(target)}, {{
                method: {json.dumps(method)},
                headers: {json.dumps(headers or {}, ensure_ascii=False)},
                credentials: {json.dumps(params.get("credentials") or "same-origin")},
                body: {json.dumps(body_value) if body_value is not None else "undefined"}
              }});
              const text = await response.text();
              return {{
                ok: response.ok,
                status: response.status,
                status_text: response.statusText,
                url: response.url,
                content_type: response.headers.get("content-type") || "",
                text: text.slice(0, {max_chars}),
                truncated: text.length > {max_chars}
              }};
            }})()
        """
        data = _post_tab("evaluate", {"expression": expression})
        response = data.get("result", data)
        if isinstance(response, dict):
            if "url" in response:
                response["url"] = _redact_url(response.get("url"))
            if "text" in response:
                response["text"] = _redact_text(response.get("text"), max_chars)
        return {"ok": True, "action": action, "response": response}
    if action == "snapshot":
        return {
            "ok": True,
            "action": action,
            "snapshot": camofox_snapshot_runtime(
                runtime,
                max_chars=max(200, min(int(params.get("max_chars") or 8000), 50000)),
                include_screenshot=bool(params.get("include_screenshot", False)),
                timeout_s=timeout_s,
            ),
        }
    if action == "wait_for_selector":
        selector = str(params.get("selector") or "").strip()
        if not selector:
            return {"ok": False, "error": "selector_required"}
        data = _post_tab(
            "wait",
            {"selector": selector, "timeout": int(float(params.get("timeout_s", 10)) * 1000)},
        )
        return {"ok": True, "action": action, "selector": selector, "response": data}
    if action == "wait":
        ms = max(0, min(int(params.get("ms") or 1000), 60000))
        data = _post_tab("wait", {"timeout": ms})
        return {"ok": True, "action": action, "waited_ms": ms, "response": data}
    if action == "title":
        data = _post_tab("evaluate", {"expression": "document.title"})
        return {"ok": True, "action": action, "title": data.get("result", "")}
    if action in {"get_console", "get_network", "get_api_requests"}:
        traces: dict[str, Any] = {}
        try:
            traces = _http_json("GET", f"{base}/sessions/{user_id}/traces", timeout_s=timeout_s)
        except Exception:
            traces = {}
        key = "console" if action == "get_console" else "events"
        return {
            "ok": True,
            "action": action,
            key: [],
            "count": 0,
            "total": 0,
            "trace_files": traces.get("traces") or traces.get("files") or traces,
            "note": (
                "Camofox exposes console/network details through Playwright trace "
                "archives, not live event streams. Use cloakbrowser for live "
                "console/API event capture."
            ),
        }
    if action == "clear_events":
        return {
            "ok": True,
            "action": action,
            "cleared": {"console": 0, "network": 0},
            "note": "Camofox does not keep live console/network event buffers in Nerya.",
        }
    return {"ok": False, "error": f"unknown_action: {action}"}


def camofox_close_runtime(
    runtime: dict[str, Any],
    *,
    timeout_s: float = 10.0,
) -> dict[str, Any]:
    base = _camofox_base_from_runtime(runtime)
    user_id = str(runtime.get("user_id") or "")
    if not user_id:
        return {"ok": True, "closed": False}
    try:
        data = _http_json(
            "DELETE",
            f"{base}/sessions/{user_id}",
            timeout_s=timeout_s,
        )
        return {"ok": True, "closed": True, "response": data}
    except Exception as exc:  # noqa: BLE001
        return {
            "ok": True,
            "closed": True,
            "warning": f"{type(exc).__name__}: {exc}",
        }


def fetch(
    workspace_root: Path | str,
    *,
    name: str | None = None,
    url: str,
    timeout_s: float = _DEFAULT_TIMEOUT_S,
) -> dict[str, Any]:
    """Run a one-shot fetch through the selected (or named) engine."""
    if not url:
        return {"ok": False, "error": "url_required"}
    state = _read_state(workspace_root)
    target = (name or state.get("selected") or "").strip().lower()
    if not target:
        return {"ok": False, "error": "no_engine_selected"}
    spec = _REGISTRY.get(target)
    if not spec:
        return {"ok": False, "error": "unknown_engine", "name": target}

    started = time.monotonic()
    if spec.kind == "binary":
        binary = resolve_binary(workspace_root, target)
        if not binary:
            return {"ok": False, "error": "engine_not_installed", "name": target}
        if not spec.fetch_template:
            return {"ok": False, "error": "engine_has_no_fetch_command", "name": target}
        cmd = spec.fetch_template.format(bin=binary, url=url).split()
        try:
            res = subprocess.run(
                cmd, capture_output=True, text=True, timeout=timeout_s,
            )
        except subprocess.TimeoutExpired:
            return {"ok": False, "error": "timeout", "name": target,
                    "elapsed_ms": int(timeout_s * 1000)}
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "error": f"{type(exc).__name__}: {exc}",
                    "name": target}
        elapsed_ms = int((time.monotonic() - started) * 1000)
        if res.returncode != 0:
            return {
                "ok": False, "error": "non_zero_exit", "name": target,
                "returncode": res.returncode,
                "stderr_tail": (res.stderr or "")[-2000:],
                "elapsed_ms": elapsed_ms,
            }
        body = res.stdout or ""
        return {
            "ok": True, "name": target,
            "url": url, "elapsed_ms": elapsed_ms,
            "fetch_method": f"{target}_cli",
            "markdown": body, "text": body,
            "bytes": len(body.encode("utf-8")),
        }

    if spec.kind == "python_pkg":
        if target == "cloakbrowser":
            try:
                from cloakbrowser import launch  # type: ignore[import-not-found]
            except Exception as exc:  # noqa: BLE001
                return {"ok": False, "error": "module_unavailable",
                        "name": target,
                        "detail": f"{type(exc).__name__}: {exc}"}
            try:
                browser = launch()
                context = None
                proxy_cfg = browser_proxy_config_for_workspace(workspace_root)
                try:
                    context_kwargs: dict[str, Any] = {"ignore_https_errors": True}
                    if proxy_cfg:
                        context_kwargs["proxy"] = proxy_cfg
                    context = browser.new_context(**context_kwargs)
                    page = context.new_page()
                except TypeError:
                    try:
                        context = browser.new_context()
                        page = context.new_page()
                    except Exception:  # noqa: BLE001
                        page = None
                except Exception:  # noqa: BLE001
                    page = None
                if page is None:
                    try:
                        page = browser.new_page(ignore_https_errors=True)
                    except TypeError:
                        page = browser.new_page()
                try:
                    page.goto(
                        url,
                        timeout=int(timeout_s * 1000),
                        wait_until="domcontentloaded",
                    )
                    body = page.content() or ""
                finally:
                    try:
                        if context is not None:
                            context.close()
                    except Exception:
                        pass
                    browser.close()
            except Exception as exc:  # noqa: BLE001
                return {"ok": False, "error": "page_error",
                        "name": target,
                        "detail": f"{type(exc).__name__}: {exc}"}
            elapsed_ms = int((time.monotonic() - started) * 1000)
            return {
                "ok": True, "name": target, "url": url,
                "elapsed_ms": elapsed_ms,
                "fetch_method": "cloakbrowser",
                "html": body,
                "text": body,
                "bytes": len(body.encode("utf-8")),
            }
        return {"ok": False, "error": "unsupported_python_engine", "name": target}

    if spec.kind == "node_service":
        if target == "camofox":
            sid = f"fetch_{int(time.time() * 1000)}"
            opened = camofox_open_tab(
                workspace_root,
                session_id=sid,
                url=url,
                timeout_s=timeout_s,
                trace=False,
            )
            if not opened.get("ok"):
                return opened
            runtime = opened.get("runtime") if isinstance(opened.get("runtime"), dict) else {}
            try:
                # The tab starts loading asynchronously; an immediate
                # snapshot on a JS-heavy page (IR portals, SPAs) returns
                # an empty accessibility tree. Poll with short waits until
                # content appears or the budget is spent.
                body = ""
                snapshot: dict[str, Any] = {}
                deadline = time.monotonic() + max(10.0, min(timeout_s, 60.0))
                delay = 1.0
                while True:
                    snapshot = camofox_snapshot_runtime(
                        runtime,
                        max_chars=80000,
                        timeout_s=timeout_s,
                    )
                    body = str(snapshot.get("snapshot") or "")
                    if len(body) >= 200 or time.monotonic() >= deadline:
                        break
                    time.sleep(delay)
                    delay = min(delay * 1.6, 4.0)
                elapsed_ms = int((time.monotonic() - started) * 1000)
                return {
                    "ok": True,
                    "name": target,
                    "url": url,
                    "elapsed_ms": elapsed_ms,
                    "fetch_method": "camofox_snapshot",
                    "markdown": body,
                    "text": body,
                    "bytes": len(body.encode("utf-8")),
                    "element_count": snapshot.get("element_count") or 0,
                }
            except Exception as exc:  # noqa: BLE001
                return {
                    "ok": False,
                    "error": "page_error",
                    "name": target,
                    "detail": f"{type(exc).__name__}: {exc}",
                }
            finally:
                if runtime:
                    camofox_close_runtime(runtime)
        return {"ok": False, "error": "unsupported_node_service", "name": target}

    return {"ok": False, "error": "unsupported_kind", "name": target}


def screenshot(
    workspace_root: Path | str,
    *,
    name: str | None = None,
    url: str,
    out_path: Path | str | None = None,
    timeout_s: float = _DEFAULT_TIMEOUT_S,
) -> dict[str, Any]:
    """Capture a PNG screenshot of ``url`` via the selected (or named) engine.

    Returns ``{ok, path, bytes, elapsed_ms, name, fetch_method}`` on
    success. CLI-based engines (Lightpanda / Obscura) shell out using
    their ``screenshot_template``; ``cloakbrowser`` uses Playwright's
    ``page.screenshot(path=...)``.

    The output file is written to ``out_path`` when provided, otherwise
    to ``<workspace>/state/browsers/screenshots/<engine>-<ts>.png``.
    """
    if not url:
        return {"ok": False, "error": "url_required"}
    state = _read_state(workspace_root)
    target = (name or state.get("selected") or "").strip().lower()
    if not target:
        return {"ok": False, "error": "no_engine_selected"}
    spec = _REGISTRY.get(target)
    if not spec:
        return {"ok": False, "error": "unknown_engine", "name": target}

    if out_path is None:
        ts = time.strftime("%Y%m%d-%H%M%S", time.gmtime())
        out_path = (
            _state_dir(workspace_root) / "screenshots" / f"{target}-{ts}.png"
        )
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)

    started = time.monotonic()
    if spec.kind == "binary":
        if not spec.screenshot_template:
            return {"ok": False, "error": "engine_has_no_screenshot_command",
                    "name": target}
        binary = resolve_binary(workspace_root, target)
        if not binary:
            return {"ok": False, "error": "engine_not_installed", "name": target}
        cmd = spec.screenshot_template.format(
            bin=binary, url=url, path=str(out_path),
        ).split()
        try:
            res = subprocess.run(
                cmd, capture_output=True, text=True, timeout=timeout_s,
            )
        except subprocess.TimeoutExpired:
            return {"ok": False, "error": "timeout", "name": target,
                    "elapsed_ms": int(timeout_s * 1000)}
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "error": f"{type(exc).__name__}: {exc}",
                    "name": target}
        elapsed_ms = int((time.monotonic() - started) * 1000)
        if res.returncode != 0 or not out_path.exists():
            return {
                "ok": False, "error": "screenshot_failed", "name": target,
                "returncode": res.returncode,
                "stderr_tail": (res.stderr or "")[-2000:],
                "stdout_tail": (res.stdout or "")[-1000:],
                "elapsed_ms": elapsed_ms,
            }
        return {
            "ok": True, "name": target, "url": url,
            "path": str(out_path),
            "bytes": out_path.stat().st_size,
            "elapsed_ms": elapsed_ms,
            "fetch_method": f"{target}_cli_screenshot",
        }

    if spec.kind == "python_pkg":
        if target == "cloakbrowser":
            try:
                from cloakbrowser import launch  # type: ignore[import-not-found]
            except Exception as exc:  # noqa: BLE001
                return {"ok": False, "error": "module_unavailable",
                        "name": target,
                        "detail": f"{type(exc).__name__}: {exc}"}
            try:
                browser = launch()
                context = None
                proxy_cfg = browser_proxy_config_for_workspace(workspace_root)
                try:
                    context_kwargs: dict[str, Any] = {"ignore_https_errors": True}
                    if proxy_cfg:
                        context_kwargs["proxy"] = proxy_cfg
                    context = browser.new_context(**context_kwargs)
                    page = context.new_page()
                except TypeError:
                    try:
                        context = browser.new_context()
                        page = context.new_page()
                    except Exception:  # noqa: BLE001
                        page = None
                except Exception:  # noqa: BLE001
                    page = None
                if page is None:
                    try:
                        page = browser.new_page(ignore_https_errors=True)
                    except TypeError:
                        page = browser.new_page()
                try:
                    page.goto(
                        url,
                        timeout=int(timeout_s * 1000),
                        wait_until="domcontentloaded",
                    )
                    page.screenshot(path=str(out_path), full_page=True)
                finally:
                    try:
                        if context is not None:
                            context.close()
                    except Exception:
                        pass
                    browser.close()
            except Exception as exc:  # noqa: BLE001
                return {"ok": False, "error": "page_error",
                        "name": target,
                        "detail": f"{type(exc).__name__}: {exc}"}
            elapsed_ms = int((time.monotonic() - started) * 1000)
            if not out_path.exists():
                return {"ok": False, "error": "screenshot_missing",
                        "name": target, "elapsed_ms": elapsed_ms}
            return {
                "ok": True, "name": target, "url": url,
                "path": str(out_path),
                "bytes": out_path.stat().st_size,
                "elapsed_ms": elapsed_ms,
                "fetch_method": "cloakbrowser_screenshot",
            }
        return {"ok": False, "error": "unsupported_python_engine", "name": target}

    if spec.kind == "node_service":
        if target == "camofox":
            sid = f"screenshot_{int(time.time() * 1000)}"
            opened = camofox_open_tab(
                workspace_root,
                session_id=sid,
                url=url,
                timeout_s=timeout_s,
                trace=False,
            )
            if not opened.get("ok"):
                return opened
            runtime = opened.get("runtime") if isinstance(opened.get("runtime"), dict) else {}
            try:
                shot = camofox_screenshot_runtime(
                    runtime,
                    out_path=out_path,
                    timeout_s=timeout_s,
                )
                elapsed_ms = int((time.monotonic() - started) * 1000)
                shot.update({
                    "url": url,
                    "elapsed_ms": elapsed_ms,
                })
                return shot
            except Exception as exc:  # noqa: BLE001
                return {
                    "ok": False,
                    "error": "screenshot_failed",
                    "name": target,
                    "detail": f"{type(exc).__name__}: {exc}",
                }
            finally:
                if runtime:
                    camofox_close_runtime(runtime)
        return {"ok": False, "error": "unsupported_node_service", "name": target}

    return {"ok": False, "error": "unsupported_kind", "name": target}


__all__ = [
    "BrowserSpec",
    "list_specs",
    "status",
    "configure",
    "install",
    "uninstall",
    "fetch",
    "screenshot",
    "resolve_binary",
    "camofox_action_runtime",
    "camofox_close_runtime",
    "camofox_open_tab",
    "camofox_screenshot_runtime",
    "camofox_snapshot_runtime",
]
