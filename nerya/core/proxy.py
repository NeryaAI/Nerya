"""Workspace-managed network proxy configuration.

The runtime uses this module at two boundaries:

* process start / settings save: apply proxy variables to the current
  Python process and urllib's global opener;
* subprocess launch: inject the same proxy variables into shell, skill,
  and stdio MCP child processes.

Proxy URLs with embedded credentials are stored in SecretVault and only
resolved at the runtime boundary.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import time
from pathlib import Path
from typing import Any
from urllib.parse import urlparse, urlunparse

from . import yaml_io
from .paths import WorkspacePaths


DEFAULT_NO_PROXY = "127.0.0.1,localhost,::1"
PROXY_ENV_KEYS = (
    "HTTP_PROXY",
    "HTTPS_PROXY",
    "ALL_PROXY",
    "NO_PROXY",
    "http_proxy",
    "https_proxy",
    "all_proxy",
    "no_proxy",
)
_URL_FIELDS = ("http_url", "https_url", "all_url", "pool_url")
_URL_RE = re.compile(r"^[A-Za-z][A-Za-z0-9+.-]*://")
_ALLOWED_PROXY_SCHEMES = {"http", "https", "socks4", "socks5", "socks5h"}
_LAST_MANAGED_ENV: dict[str, str] = {}
_LAST_APPLIED: dict[str, Any] = {
    "enabled": False,
    "workspace": "",
    "env": {},
    "error": "",
    "applied_at": 0,
}


PROXY_POOL_PRESETS: list[dict[str, str]] = [
    {
        "id": "custom",
        "label": "Custom proxy",
        "mode": "direct",
        "description": "Manually enter HTTP(S) or SOCKS proxy URLs.",
        "all_url": "http://127.0.0.1:7890",
        "docs_url": "",
    },
    {
        "id": "clash_local",
        "label": "Clash / Mihomo local",
        "mode": "direct",
        "description": "Common local mixed-port proxy used by Clash/Mihomo.",
        "all_url": "http://127.0.0.1:7890",
        "docs_url": "https://github.com/MetaCubeX/mihomo",
    },
    {
        "id": "jhao104_proxy_pool",
        "label": "jhao104 ProxyPool",
        "mode": "pool",
        "description": "Fetch a random proxy from a local jhao104/proxy_pool API.",
        "pool_url": "http://127.0.0.1:5010/get/?type=https",
        "pool_format": "jhao_json",
        "docs_url": "https://github.com/jhao104/proxy_pool",
    },
    {
        "id": "smart_proxy_pool_dynamic",
        "label": "SmartProxyPool dynamic",
        "mode": "direct",
        "description": "Use SmartProxyPool's dynamic proxy port directly.",
        "all_url": "http://127.0.0.1:36050",
        "docs_url": "https://pypi.org/project/SmartProxyPool/",
    },
    {
        "id": "smart_proxy_pool_api",
        "label": "SmartProxyPool API",
        "mode": "pool",
        "description": "Fetch a random proxy from SmartProxyPool's REST API.",
        "pool_url": "http://127.0.0.1:35050/api/v1/proxy/?https=1",
        "pool_format": "smart_json",
        "docs_url": "https://pypi.org/project/SmartProxyPool/",
    },
]


def _paths(value: Any) -> WorkspacePaths:
    if isinstance(value, WorkspacePaths):
        return value
    paths = getattr(value, "paths", None)
    if isinstance(paths, WorkspacePaths):
        return paths
    return WorkspacePaths(root=Path(value))


def _cfg_get(config_like: Any, dotted: str, default: Any = None) -> Any:
    if hasattr(config_like, "get") and callable(config_like.get):
        try:
            return config_like.get(dotted, default)
        except TypeError:
            try:
                value = config_like.get(dotted)
                return default if value is None else value
            except Exception:
                return default
    cur = getattr(config_like, "data", config_like)
    for part in dotted.split("."):
        if not isinstance(cur, dict) or part not in cur:
            return default
        cur = cur[part]
    return cur


def _raw_proxy_config(config_like: Any) -> dict[str, Any]:
    raw = _cfg_get(config_like, "network.proxy", {}) or {}
    return dict(raw) if isinstance(raw, dict) else {}


def _normalize_proxy_url(raw: Any) -> str:
    value = str(raw or "").strip()
    if not value:
        return ""
    if value.startswith("vault://"):
        return value
    if not _URL_RE.match(value):
        value = "http://" + value
    parsed = urlparse(value)
    if parsed.scheme.lower() not in _ALLOWED_PROXY_SCHEMES:
        raise ValueError(
            f"proxy URL scheme must be one of {sorted(_ALLOWED_PROXY_SCHEMES)}"
        )
    if not parsed.hostname or not parsed.netloc:
        raise ValueError("proxy URL must include a host")
    return value


def _has_userinfo(url: str) -> bool:
    try:
        parsed = urlparse(url)
    except ValueError:
        return False
    return bool(parsed.username or parsed.password)


def redact_proxy_url(url: Any) -> str:
    value = str(url or "").strip()
    if not value:
        return ""
    if value.startswith("vault://"):
        return value
    if not _URL_RE.match(value):
        value = "http://" + value
    try:
        parsed = urlparse(value)
    except ValueError:
        return "<invalid>"
    if not (parsed.username or parsed.password):
        return value
    host = parsed.hostname or ""
    port = f":{parsed.port}" if parsed.port else ""
    user = parsed.username or "user"
    netloc = f"{user}:***@{host}{port}"
    return urlunparse((parsed.scheme, netloc, parsed.path, parsed.params, parsed.query, parsed.fragment))


def _secret_name(slot: str, value: str) -> str:
    digest = hashlib.sha1(f"network-proxy::{slot}::{value}".encode("utf-8")).hexdigest()[:12]
    return f"network_proxy_{slot}_{digest}"


def _store_secret_url(
    paths: WorkspacePaths,
    *,
    slot: str,
    value: str,
    vault_passphrase: str | None = None,
) -> str:
    from ..security.secrets import SecretVault

    vault = SecretVault.open(paths.vault_enc, passphrase=vault_passphrase)
    meta = vault.put(
        name=_secret_name(slot, value),
        value=value,
        kind="network_proxy",
        scope=["runtime", "env"],
        owner=f"network.proxy/{slot}",
    )
    return meta.ref()


def _resolve_ref(
    paths: WorkspacePaths,
    ref: str,
    *,
    vault_passphrase: str | None = None,
) -> str:
    ref = str(ref or "").strip()
    if not ref.startswith("vault://"):
        return ref
    from ..security.secrets import SecretVault

    vault = SecretVault.open(paths.vault_enc, passphrase=vault_passphrase)
    return vault.resolve(ref.removeprefix("vault://"), required_scope="runtime")


def _persistable_url(
    paths: WorkspacePaths,
    *,
    slot: str,
    raw_url: Any,
    raw_ref: Any = "",
    vault_passphrase: str | None = None,
) -> tuple[str, str]:
    url = str(raw_url or "").strip()
    ref = str(raw_ref or "").strip()
    if not url:
        if ref and not ref.startswith("vault://"):
            raise ValueError(f"{slot}_ref must start with vault://")
        return "", ref
    normalized = _normalize_proxy_url(url)
    if normalized.startswith("vault://"):
        return "", normalized
    if _has_userinfo(normalized):
        return "", _store_secret_url(
            paths, slot=slot, value=normalized, vault_passphrase=vault_passphrase,
        )
    return normalized, ""


def save_proxy_config(
    config: Any,
    payload: dict[str, Any],
    *,
    vault_passphrase: str | None = None,
) -> dict[str, Any]:
    paths = _paths(config)
    existing = yaml_io.load(paths.config, default={}) or {}
    if not isinstance(existing, dict):
        existing = {}
    network = existing.setdefault("network", {})
    if not isinstance(network, dict):
        network = {}
        existing["network"] = network

    current = _raw_proxy_config(config)
    enabled = bool(payload.get("enabled", current.get("enabled", False)))
    mode = str(payload.get("mode") or current.get("mode") or "direct").strip().lower()
    if mode not in {"direct", "pool"}:
        raise ValueError("proxy mode must be 'direct' or 'pool'")
    preset = str(payload.get("preset") or current.get("preset") or "custom").strip()
    pool_format = str(
        payload.get("pool_format") or current.get("pool_format") or "auto"
    ).strip().lower()
    if pool_format not in {"auto", "jhao_json", "smart_json", "text", "json"}:
        raise ValueError("proxy pool_format must be auto, json, text, jhao_json, or smart_json")
    no_proxy = str(
        payload.get("no_proxy")
        if "no_proxy" in payload
        else current.get("no_proxy", DEFAULT_NO_PROXY)
    ).strip()

    next_proxy: dict[str, Any] = {
        "enabled": enabled,
        "mode": mode,
        "preset": preset or "custom",
        "no_proxy": no_proxy or DEFAULT_NO_PROXY,
    }
    if mode == "pool":
        next_proxy["pool_format"] = pool_format

    for key in _URL_FIELDS:
        url, ref = _persistable_url(
            paths,
            slot=key,
            raw_url=payload.get(key),
            raw_ref=payload.get(f"{key}_ref"),
            vault_passphrase=vault_passphrase,
        )
        if url:
            next_proxy[key] = url
        if ref:
            next_proxy[f"{key}_ref"] = ref

    network["proxy"] = next_proxy
    yaml_io.dump(paths.config, existing)

    data = getattr(config, "data", None)
    if isinstance(data, dict):
        data.setdefault("network", {})
        if not isinstance(data["network"], dict):
            data["network"] = {}
        data["network"]["proxy"] = dict(next_proxy)
    return public_proxy_status(config)


def _public_config(raw: dict[str, Any], paths: WorkspacePaths) -> dict[str, Any]:
    out: dict[str, Any] = {
        "enabled": bool(raw.get("enabled", False)),
        "mode": str(raw.get("mode") or "direct"),
        "preset": str(raw.get("preset") or "custom"),
        "no_proxy": str(raw.get("no_proxy") or DEFAULT_NO_PROXY),
        "pool_format": str(raw.get("pool_format") or "auto"),
    }
    for key in _URL_FIELDS:
        value = str(raw.get(key) or "")
        ref = str(raw.get(f"{key}_ref") or "")
        out[key] = redact_proxy_url(value)
        out[f"{key}_ref"] = ref
        if ref:
            try:
                out[f"{key}_preview"] = redact_proxy_url(_resolve_ref(paths, ref))
            except Exception:
                out[f"{key}_preview"] = "<vault unavailable>"
    return out


def public_proxy_status(config: Any) -> dict[str, Any]:
    paths = _paths(config)
    raw = _raw_proxy_config(config)
    return {
        "ok": True,
        "config": _public_config(raw, paths),
        "presets": PROXY_POOL_PRESETS,
        "applied": {
            **_LAST_APPLIED,
            "env": {k: redact_proxy_url(v) for k, v in (_LAST_APPLIED.get("env") or {}).items()},
        },
    }


def _resolve_url_field(raw: dict[str, Any], paths: WorkspacePaths, key: str) -> str:
    value = str(raw.get(key) or "").strip()
    ref = str(raw.get(f"{key}_ref") or "").strip()
    if ref:
        return _normalize_proxy_url(_resolve_ref(paths, ref))
    return _normalize_proxy_url(value) if value else ""


def _proxy_from_obj(obj: Any) -> str:
    if isinstance(obj, str):
        text = obj.strip()
        if not text:
            return ""
        if text.startswith("{") or text.startswith("["):
            try:
                return _proxy_from_obj(json.loads(text))
            except Exception:
                pass
        return text.splitlines()[0].strip().strip('"')
    if isinstance(obj, list):
        for item in obj:
            proxy = _proxy_from_obj(item)
            if proxy:
                return proxy
        return ""
    if isinstance(obj, dict):
        for key in ("proxy", "url", "http", "https", "all"):
            proxy = _proxy_from_obj(obj.get(key))
            if proxy:
                return proxy
        data = obj.get("data") or obj.get("result") or obj.get("proxy_list")
        proxy = _proxy_from_obj(data)
        if proxy:
            return proxy
        host = obj.get("host") or obj.get("ip")
        port = obj.get("port")
        if host and port:
            return f"{host}:{port}"
    return ""


def parse_proxy_pool_response(body: str, *, pool_format: str = "auto") -> str:
    text = str(body or "").strip()
    if not text:
        return ""
    fmt = (pool_format or "auto").lower()
    if fmt == "text":
        return _normalize_proxy_url(_proxy_from_obj(text))
    try:
        return _normalize_proxy_url(_proxy_from_obj(json.loads(text)))
    except Exception:
        if fmt in {"json", "jhao_json", "smart_json"}:
            return ""
        return _normalize_proxy_url(_proxy_from_obj(text))


def fetch_proxy_from_pool(pool_url: str, *, pool_format: str = "auto") -> str:
    import urllib.request

    url = _normalize_proxy_url(pool_url)
    req = urllib.request.Request(
        url,
        headers={
            "Accept": "application/json,text/plain,*/*",
            "User-Agent": "Nerya/proxy-pool",
        },
    )
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(req, timeout=8) as resp:
        raw = resp.read(64 * 1024).decode("utf-8", errors="replace")
    return parse_proxy_pool_response(raw, pool_format=pool_format)


def _env_from_urls(
    *,
    http_url: str = "",
    https_url: str = "",
    all_url: str = "",
    no_proxy: str = DEFAULT_NO_PROXY,
) -> dict[str, str]:
    env: dict[str, str] = {}
    if all_url:
        http_url = http_url or all_url
        https_url = https_url or all_url
        env["ALL_PROXY"] = all_url
        env["all_proxy"] = all_url
    if http_url:
        env["HTTP_PROXY"] = http_url
        env["http_proxy"] = http_url
    if https_url:
        env["HTTPS_PROXY"] = https_url
        env["https_proxy"] = https_url
    if no_proxy:
        env["NO_PROXY"] = no_proxy
        env["no_proxy"] = no_proxy
    return env


def resolve_proxy_env(
    config_or_workspace: Any,
    *,
    resolve_pool: bool = True,
) -> tuple[dict[str, str], dict[str, Any]]:
    paths = _paths(config_or_workspace)
    raw = _raw_proxy_config(config_or_workspace)
    if not raw and not hasattr(config_or_workspace, "data"):
        doc = yaml_io.load(paths.config, default={}) or {}
        raw = ((doc.get("network") or {}).get("proxy") or {}) if isinstance(doc, dict) else {}
    if not bool(raw.get("enabled", False)):
        return {}, {"enabled": False}

    mode = str(raw.get("mode") or "direct").lower()
    no_proxy = str(raw.get("no_proxy") or DEFAULT_NO_PROXY)
    details: dict[str, Any] = {"enabled": True, "mode": mode, "preset": raw.get("preset") or "custom"}

    if mode == "pool":
        pool_url = _resolve_url_field(raw, paths, "pool_url")
        if not pool_url:
            raise ValueError("proxy pool mode requires pool_url")
        if not resolve_pool:
            return _env_from_urls(no_proxy=no_proxy), {**details, "pool_url": redact_proxy_url(pool_url)}
        proxy_url = fetch_proxy_from_pool(
            pool_url,
            pool_format=str(raw.get("pool_format") or "auto"),
        )
        if not proxy_url:
            raise ValueError("proxy pool returned no usable proxy")
        env = _env_from_urls(all_url=proxy_url, no_proxy=no_proxy)
        return env, {**details, "pool_url": redact_proxy_url(pool_url), "selected_proxy": redact_proxy_url(proxy_url)}

    http_url = _resolve_url_field(raw, paths, "http_url")
    https_url = _resolve_url_field(raw, paths, "https_url")
    all_url = _resolve_url_field(raw, paths, "all_url")
    if not (http_url or https_url or all_url):
        raise ValueError("proxy direct mode requires at least one proxy URL")
    env = _env_from_urls(
        http_url=http_url,
        https_url=https_url,
        all_url=all_url,
        no_proxy=no_proxy,
    )
    return env, details


def proxy_env_for_workspace(workspace: WorkspacePaths | Path | str) -> dict[str, str]:
    env, _details = resolve_proxy_env(workspace, resolve_pool=True)
    return env


def browser_proxy_config_for_workspace(workspace: WorkspacePaths | Path | str) -> dict[str, str]:
    """Return Playwright-style proxy kwargs for browser contexts."""
    env = proxy_env_for_workspace(workspace)
    server = env.get("HTTPS_PROXY") or env.get("ALL_PROXY") or env.get("HTTP_PROXY") or ""
    if not server:
        return {}
    cfg = {"server": server}
    bypass = env.get("NO_PROXY") or ""
    if bypass:
        cfg["bypass"] = bypass
    return cfg


def _apply_managed_env(env: dict[str, str]) -> None:
    global _LAST_MANAGED_ENV
    next_values = {k: v for k, v in env.items() if k in PROXY_ENV_KEYS and v}
    for key, old_value in list(_LAST_MANAGED_ENV.items()):
        if key not in next_values and os.environ.get(key) == old_value:
            os.environ.pop(key, None)
    for key, value in next_values.items():
        os.environ[key] = value
    _LAST_MANAGED_ENV = next_values


def _install_urllib_opener(env: dict[str, str] | None) -> None:
    import urllib.request

    if env is None:
        urllib.request.install_opener(None)
        return
    proxies: dict[str, str] = {}
    if env.get("HTTP_PROXY"):
        proxies["http"] = env["HTTP_PROXY"]
    if env.get("HTTPS_PROXY"):
        proxies["https"] = env["HTTPS_PROXY"]
    elif env.get("ALL_PROXY"):
        proxies["https"] = env["ALL_PROXY"]
    urllib.request.install_opener(
        urllib.request.build_opener(urllib.request.ProxyHandler(proxies))
    )


def apply_network_proxy(config: Any) -> dict[str, Any]:
    """Apply workspace proxy settings to the current Python process."""
    global _LAST_APPLIED
    paths = _paths(config)
    raw = _raw_proxy_config(config)
    if not bool(raw.get("enabled", False)):
        _apply_managed_env({})
        _install_urllib_opener(None)
        _LAST_APPLIED = {
            "enabled": False,
            "workspace": str(paths.root),
            "env": {},
            "error": "",
            "applied_at": int(time.time()),
        }
        return _LAST_APPLIED
    try:
        env, details = resolve_proxy_env(config, resolve_pool=True)
        _apply_managed_env(env)
        _install_urllib_opener(env)
        _LAST_APPLIED = {
            **details,
            "workspace": str(paths.root),
            "env": env,
            "error": "",
            "applied_at": int(time.time()),
        }
    except Exception as exc:
        _apply_managed_env({})
        _install_urllib_opener(None)
        _LAST_APPLIED = {
            "enabled": bool(raw.get("enabled", False)),
            "workspace": str(paths.root),
            "env": {},
            "error": f"{type(exc).__name__}: {exc}",
            "applied_at": int(time.time()),
        }
    return _LAST_APPLIED


def test_proxy_request(config: Any, *, url: str = "https://httpbin.org/ip") -> dict[str, Any]:
    import urllib.error
    import urllib.request

    target = str(url or "").strip() or "https://httpbin.org/ip"
    if not target.startswith(("http://", "https://")):
        return {"ok": False, "error": "url must start with http:// or https://"}
    started = time.monotonic()
    try:
        env, details = resolve_proxy_env(config, resolve_pool=True)
        proxies = {}
        if env.get("HTTP_PROXY"):
            proxies["http"] = env["HTTP_PROXY"]
        if env.get("HTTPS_PROXY"):
            proxies["https"] = env["HTTPS_PROXY"]
        elif env.get("ALL_PROXY"):
            proxies["https"] = env["ALL_PROXY"]
        opener = urllib.request.build_opener(urllib.request.ProxyHandler(proxies))
        req = urllib.request.Request(target, headers={"User-Agent": "Nerya/proxy-test"})
        with opener.open(req, timeout=12) as resp:
            raw = resp.read(4096).decode("utf-8", errors="replace")
        return {
            "ok": True,
            "status": getattr(resp, "status", 200),
            "elapsed_ms": int((time.monotonic() - started) * 1000),
            "proxy": {
                **details,
                "env": {k: redact_proxy_url(v) for k, v in env.items()},
            },
            "body_preview": raw[:500],
        }
    except urllib.error.URLError as exc:
        return {
            "ok": False,
            "error": f"URLError: {getattr(exc, 'reason', exc)}",
            "elapsed_ms": int((time.monotonic() - started) * 1000),
        }
    except Exception as exc:  # noqa: BLE001
        return {
            "ok": False,
            "error": f"{type(exc).__name__}: {exc}",
            "elapsed_ms": int((time.monotonic() - started) * 1000),
        }


__all__ = [
    "DEFAULT_NO_PROXY",
    "PROXY_POOL_PRESETS",
    "apply_network_proxy",
    "browser_proxy_config_for_workspace",
    "parse_proxy_pool_response",
    "proxy_env_for_workspace",
    "public_proxy_status",
    "redact_proxy_url",
    "resolve_proxy_env",
    "save_proxy_config",
    "test_proxy_request",
]
