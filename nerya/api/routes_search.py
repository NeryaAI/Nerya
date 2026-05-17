"""Search engine configuration HTTP endpoints.

Manages ``workspace/search_engines.json`` — the engine chain
(``exa``, ``tavily``, ``perplexity``, ``brave``, ``serper``, ``bing``,
``duckduckgo``, ``langsearch``, ``searxng``, ``firecrawl``), per-engine
API keys, and per-engine ``base_url`` overrides (used by ``searxng``
and ``firecrawl``). Read by
:mod:`nerya.skills.builtin.research.scripts._engine_config`.

Multi-key rotation: keys are stored as comma-separated strings under
``vault://search.<engine>.keys``. The workspace JSON carries the
engine chain + region / safesearch + per-engine ``base_urls`` — never
plaintext keys.

Exposed routes:

- ``GET  /search/engines/status``           — chain, per-engine readiness
- ``POST /search/engines/config``           — patch chain / region / keys / base_urls
- ``POST /search/engines/test``             — one-shot probe query
- ``GET  /search/engines/searxng/status``   — local SearXNG container state
- ``POST /search/engines/searxng/deploy``   — start / restart local SearXNG
- ``POST /search/engines/searxng/teardown`` — stop / remove local SearXNG
"""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path
from typing import Any

from ..security.secrets import SecretVault
from . import _searxng_deploy


_SUPPORTED_ENGINES: tuple[str, ...] = (
    "exa", "tavily", "perplexity", "langsearch", "brave", "serper",
    "firecrawl", "searxng", "bing", "duckduckgo",
)
_KEYLESS_ENGINES: frozenset[str] = frozenset({"duckduckgo", "searxng"})
_BASE_URL_ENGINES: frozenset[str] = frozenset({"searxng", "firecrawl"})
_DEFAULT_BASE_URLS: dict[str, str] = {
    "searxng": "http://127.0.0.1:8888",
    "firecrawl": "https://api.firecrawl.dev",
}
_DEFAULT_REGION = "wt-wt"
_DEFAULT_SAFESEARCH = "moderate"


def _config_path(client) -> Path:
    return Path(client.config.paths.root) / "search_engines.json"


def _read_config(client) -> dict[str, Any]:
    path = _config_path(client)
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8")) or {}
    except Exception:
        return {}


def _write_config(client, data: dict[str, Any]) -> None:
    path = _config_path(client)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def _split_keys(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, list):
        out: list[str] = []
        for v in value:
            text = str(v or "").strip()
            if text:
                out.append(text)
        return out
    text = str(value).replace("\n", ",")
    return [p.strip() for p in text.split(",") if p and p.strip()]


def _vault_secret_name(engine: str) -> str:
    return f"search.{engine}.keys"


def _open_vault(client) -> SecretVault | None:
    path = client.config.paths.vault_enc
    try:
        return SecretVault.open(path)
    except Exception:
        return None


def _vault_keys_count(vault: SecretVault | None, engine: str) -> int:
    if vault is None:
        return 0
    try:
        raw = vault.resolve(_vault_secret_name(engine))
    except Exception:
        return 0
    return len(_split_keys(raw))


def _vault_has_secret(vault: SecretVault | None, engine: str) -> bool:
    if vault is None:
        return False
    try:
        return any(meta.name == _vault_secret_name(engine) for meta in vault.list())
    except Exception:
        return False


def _legacy_env_count(engine: str) -> int:
    legacy = {
        "exa": "EXASEARCH_API_KEY",
        "tavily": "TAVILY_API_KEY",
        "perplexity": "PERPLEXITY_API_KEY",
        "brave": "BRAVE_API_KEY",
        "serper": "SERPER_API_KEY",
        "bing": "BING_SEARCH_KEY",
        "langsearch": "LANGSEARCH_API_KEY",
        "firecrawl": "FIRECRAWL_API_KEY",
    }
    keys: list[str] = []
    keys.extend(_split_keys(os.environ.get(f"NERYA_SEARCH_{engine.upper()}_KEYS")))
    if engine in legacy:
        keys.extend(_split_keys(os.environ.get(legacy[engine])))
    return len({k for k in keys if k})


def _resolve_base_url(*, engine: str, workspace_cfg: dict[str, Any]) -> dict[str, Any]:
    """Return per-source base_url info for an engine (priority chain)."""
    block = workspace_cfg.get("base_urls") or {}
    if not isinstance(block, dict):
        block = {}
    workspace_value = (str(block.get(engine) or "").strip()).rstrip("/")
    env_value = (os.environ.get(f"NERYA_SEARCH_{engine.upper()}_BASE_URL") or "").strip().rstrip("/")
    default_value = _DEFAULT_BASE_URLS.get(engine, "")
    effective = workspace_value or env_value or default_value
    return {
        "workspace": workspace_value,
        "env": env_value,
        "default": default_value,
        "effective": effective,
    }


def _normalize_chain(chain: list[Any] | None) -> list[str]:
    if not chain:
        return []
    seen: set[str] = set()
    out: list[str] = []
    for raw in chain:
        name = str(raw or "").strip().lower()
        if not name or name in seen:
            continue
        if name not in _SUPPORTED_ENGINES:
            # Allow unknown engines through so future adapters can register
            # without a backend release; UI will still render them.
            seen.add(name)
            out.append(name)
            continue
        seen.add(name)
        out.append(name)
    return out


def _engine_status_row(
    *,
    engine: str,
    workspace_keys: list[str],
    vault: SecretVault | None,
    workspace_cfg: dict[str, Any],
) -> dict[str, Any]:
    keyless = engine in _KEYLESS_ENGINES
    vault_count = _vault_keys_count(vault, engine)
    env_count = _legacy_env_count(engine)
    workspace_count = len(workspace_keys)
    total_keys = workspace_count + vault_count + env_count
    needs_base_url = engine in _BASE_URL_ENGINES
    base_url_info: dict[str, Any] = {}
    base_url_ready = True
    if needs_base_url:
        base_url_info = _resolve_base_url(engine=engine, workspace_cfg=workspace_cfg)
        base_url_ready = bool(base_url_info.get("effective"))

    ready = (keyless or total_keys > 0) and base_url_ready
    return {
        "name": engine,
        "needs_key": not keyless,
        "needs_base_url": needs_base_url,
        "ready": ready,
        "key_counts": {
            "workspace": workspace_count,
            "vault": vault_count,
            "env": env_count,
            "total": total_keys,
        },
        "vault_ref": f"vault://{_vault_secret_name(engine)}" if not keyless else None,
        "key_preview": (
            [k[:4] + "…" + k[-2:] for k in workspace_keys] if workspace_keys else []
        ),
        "base_url": base_url_info,
    }


def _build_status_payload(client) -> dict[str, Any]:
    cfg = _read_config(client)
    raw_chain = cfg.get("engines") or list(_SUPPORTED_ENGINES)
    chain = _normalize_chain(raw_chain) or list(_SUPPORTED_ENGINES)
    region = cfg.get("region") or _DEFAULT_REGION
    safesearch = cfg.get("safesearch") or _DEFAULT_SAFESEARCH
    workspace_keys = (cfg.get("keys") or {}) if isinstance(cfg.get("keys"), dict) else {}

    vault = _open_vault(client)
    chain_set = set(chain)
    all_engines = list(_SUPPORTED_ENGINES) + [e for e in chain if e not in _SUPPORTED_ENGINES]
    seen: set[str] = set()
    engine_status: list[dict[str, Any]] = []
    for engine in all_engines:
        if engine in seen:
            continue
        seen.add(engine)
        engine_status.append(_engine_status_row(
            engine=engine,
            workspace_keys=_split_keys(workspace_keys.get(engine)),
            vault=vault,
            workspace_cfg=cfg,
        ))

    usable = sum(1 for row in engine_status
                 if row["name"] in chain_set and row["ready"])

    # Surface SearXNG container status at the top level so the dashboard
    # can render a single "deploy" panel without making a second call.
    searxng_status = None
    if "searxng" in chain_set:
        try:
            searxng_status = _searxng_deploy.status(
                client.config.paths.root,
                base_url=_resolve_base_url(
                    engine="searxng", workspace_cfg=cfg,
                )["effective"],
            )
        except Exception as exc:  # noqa: BLE001
            searxng_status = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}

    return {
        "ok": True,
        "engines": chain,
        "region": region,
        "safesearch": safesearch,
        "supported": list(_SUPPORTED_ENGINES),
        "keyless": sorted(_KEYLESS_ENGINES),
        "base_url_engines": sorted(_BASE_URL_ENGINES),
        "default_base_urls": dict(_DEFAULT_BASE_URLS),
        "engine_status": engine_status,
        "usable_in_chain": usable,
        "workspace_path": str(_config_path(client)),
        "searxng": searxng_status,
    }


def routes():

    def status(client, _payload):
        return _build_status_payload(client)

    def config(client, payload):
        body = payload or {}

        existing = _read_config(client)
        next_cfg: dict[str, Any] = dict(existing)

        if "engines" in body:
            chain = _normalize_chain(body.get("engines") or [])
            if chain:
                next_cfg["engines"] = chain

        if "region" in body and body["region"]:
            next_cfg["region"] = str(body["region"]).strip().lower() or _DEFAULT_REGION
        if "safesearch" in body and body["safesearch"]:
            next_cfg["safesearch"] = str(body["safesearch"]).strip().lower() or _DEFAULT_SAFESEARCH

        # Per-engine base_url overrides (used by searxng, firecrawl).
        # Pass an empty string to clear the override (falls back to env or default).
        base_urls_block: dict[str, str] = next_cfg.get("base_urls") or {}
        if not isinstance(base_urls_block, dict):
            base_urls_block = {}
        urls_payload = body.get("base_urls")
        if isinstance(urls_payload, dict):
            for engine_raw, value in urls_payload.items():
                engine = str(engine_raw or "").strip().lower()
                if not engine:
                    continue
                text = str(value or "").strip().rstrip("/")
                if text:
                    base_urls_block[engine] = text
                else:
                    base_urls_block.pop(engine, None)
        if base_urls_block:
            next_cfg["base_urls"] = base_urls_block
        else:
            next_cfg.pop("base_urls", None)

        # Workspace-stored keys (legacy / plaintext path) — only apply when
        # caller explicitly asked for ``store: "workspace"`` per engine. The
        # default path stores keys in the SecretVault.
        plaintext_block: dict[str, list[str]] = next_cfg.get("keys") or {}
        if not isinstance(plaintext_block, dict):
            plaintext_block = {}

        keys_payload = body.get("keys")
        store_pref = (body.get("store") or "vault").strip().lower()
        if isinstance(keys_payload, dict):
            vault = _open_vault(client)
            for engine_raw, value in keys_payload.items():
                engine = str(engine_raw or "").strip().lower()
                if not engine:
                    continue
                if engine in _KEYLESS_ENGINES:
                    continue
                key_list = _split_keys(value)
                if store_pref == "workspace":
                    if key_list:
                        plaintext_block[engine] = key_list
                    else:
                        plaintext_block.pop(engine, None)
                    continue
                # Default: store in vault as comma-separated string.
                if vault is None:
                    continue
                name = _vault_secret_name(engine)
                if not key_list:
                    try:
                        vault.delete(name)
                    except Exception:
                        pass
                    continue
                joined = ",".join(key_list)
                try:
                    vault.put(
                        name=name,
                        value=joined,
                        kind="search_keys",
                        scope=["search", engine],
                        owner="dashboard",
                    )
                except Exception:
                    continue

        if plaintext_block:
            next_cfg["keys"] = plaintext_block
        else:
            next_cfg.pop("keys", None)

        _write_config(client, next_cfg)
        return _build_status_payload(client)

    def test(client, payload):
        body = payload or {}
        query = str(body.get("query") or "Nerya search engine probe").strip()
        engine = (body.get("engine") or "").strip().lower() or None
        max_results = int(body.get("max_results") or 3)

        # Run as a subprocess so we hit the exact same code path the
        # ``research`` skill uses, and so a misbehaving engine cannot
        # poison the dashboard request loop.
        import subprocess

        cmd = [
            sys.executable, "-m",
            "nerya.skills.builtin.research.scripts.web_search",
            "--json", json.dumps({
                "query": query,
                "engine": engine,
                "max_results": max_results,
            }, ensure_ascii=False),
        ]
        started = time.monotonic()
        try:
            res = subprocess.run(
                cmd,
                cwd=str(client.config.paths.root),
                capture_output=True,
                timeout=45,
                text=True,
            )
        except subprocess.TimeoutExpired:
            return {"ok": False, "error": "timeout", "elapsed_ms": 45_000}
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}

        elapsed_ms = int((time.monotonic() - started) * 1000)
        stdout = (res.stdout or "").strip()
        stderr = (res.stderr or "").strip()
        if res.returncode != 0:
            return {
                "ok": False,
                "error": "non_zero_exit",
                "returncode": res.returncode,
                "stderr_tail": stderr[-2000:],
                "elapsed_ms": elapsed_ms,
            }
        try:
            parsed = json.loads(stdout) if stdout else {}
        except Exception:
            parsed = {"raw_stdout_tail": stdout[-2000:]}
        return {
            "ok": True,
            "elapsed_ms": elapsed_ms,
            "engine": engine,
            "result": parsed,
            "stderr_tail": stderr[-1000:] if stderr else "",
        }

    def searxng_status(client, _payload):
        cfg = _read_config(client)
        base_url = _resolve_base_url(engine="searxng", workspace_cfg=cfg)["effective"]
        return _searxng_deploy.status(client.config.paths.root, base_url=base_url)

    def searxng_deploy(client, payload):
        body = payload or {}
        host_port = body.get("host_port")
        try:
            port = int(host_port) if host_port else None
        except Exception:
            port = None
        result = _searxng_deploy.deploy(
            client.config.paths.root,
            host_port=port,
            image=str(body.get("image") or "").strip() or None,
            container_name=str(body.get("container_name") or "").strip() or None,
            rebuild=bool(body.get("rebuild")),
        )
        # When a deploy succeeds and the operator hasn't supplied a custom
        # base_url, persist the new local URL so the next search call
        # automatically routes through the freshly-started container.
        if result.get("ok"):
            cfg = _read_config(client)
            urls = cfg.get("base_urls") or {}
            if not isinstance(urls, dict):
                urls = {}
            if not urls.get("searxng"):
                urls["searxng"] = result["base_url"]
                cfg["base_urls"] = urls
                _write_config(client, cfg)
        return result

    def searxng_teardown(client, payload):
        body = payload or {}
        return _searxng_deploy.teardown(
            client.config.paths.root,
            remove=bool(body.get("remove", True)),
        )

    return [
        ("GET", "/search/engines/status", status),
        ("POST", "/search/engines/config", config),
        ("POST", "/search/engines/test", test),
        ("GET", "/search/engines/searxng/status", searxng_status),
        ("POST", "/search/engines/searxng/deploy", searxng_deploy),
        ("POST", "/search/engines/searxng/teardown", searxng_teardown),
    ]
