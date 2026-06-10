"""Search engine config loader.

Resolves *engine chain* + *per-engine key list* from three sources, in
this priority (later wins):

1. ``workspace/search_engines.json`` — dashboard-managed config
2. ``NERYA_SEARCH_ENGINES`` env var (CSV of engine names) +
   ``NERYA_SEARCH_<ENGINE>_KEYS`` per-engine key CSVs
3. Optional ``vault://search.<engine>.keys`` secrets
4. Inline kwargs at call time

Multi-key convention: keys are stored as comma-separated string
("k1,k2,k3"). Whitespace and empty entries are stripped. Order matters
— left-most key is tried first.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


_DEFAULT_CHAIN: tuple[str, ...] = (
    "exa", "tavily", "perplexity", "langsearch", "brave", "serper",
    "firecrawl", "searxng", "bing", "duckduckgo", "duckduckgo_lite",
)
_KEYLESS_ENGINES: frozenset[str] = frozenset(
    {"duckduckgo", "duckduckgo_html", "duckduckgo_lite", "searxng"}
)
# Engines that accept a configurable base URL (per-engine, not per-key).
# Used both for self-hosted instances (searxng) and for vendors that
# provide an enterprise / regional endpoint override (firecrawl).
_BASE_URL_ENGINES: frozenset[str] = frozenset({"searxng", "firecrawl"})
_DEFAULT_BASE_URLS: dict[str, str] = {
    "searxng": "http://127.0.0.1:8888",
    "firecrawl": "https://api.firecrawl.dev",
}


@dataclass
class EngineSpec:
    name: str
    keys: list[str] = field(default_factory=list)
    base_url: str = ""

    @property
    def needs_key(self) -> bool:
        return self.name not in _KEYLESS_ENGINES

    @property
    def needs_base_url(self) -> bool:
        return self.name in _BASE_URL_ENGINES

    @property
    def effective_base_url(self) -> str:
        if self.base_url:
            return self.base_url
        return _DEFAULT_BASE_URLS.get(self.name, "")

    @property
    def usable(self) -> bool:
        if self.name in _BASE_URL_ENGINES and not self.effective_base_url:
            return False
        if not self.needs_key:
            return True
        return bool(self.keys)

    def adapter_config(self) -> dict[str, Any]:
        cfg: dict[str, Any] = {}
        if self.name in _BASE_URL_ENGINES:
            cfg["base_url"] = self.effective_base_url
        return cfg


@dataclass
class SearchEngineConfig:
    """Resolved engine chain to walk in order."""

    engines: list[EngineSpec]
    region: str = "wt-wt"
    safesearch: str = "moderate"
    sources: dict[str, str] = field(default_factory=dict)

    def usable_chain(self) -> list[EngineSpec]:
        return [e for e in self.engines if e.usable]


def _split_keys(raw: str | None) -> list[str]:
    if not raw:
        return []
    parts = raw.replace("\n", ",").split(",")
    return [p.strip() for p in parts if p and p.strip()]


def _load_workspace_config() -> dict[str, Any]:
    workspace = os.environ.get("NERYA_WORKSPACE")
    if not workspace:
        workspace = str(Path.home() / ".nerya")
    cfg_path = Path(workspace).expanduser() / "search_engines.json"
    if not cfg_path.exists():
        return {}
    try:
        return json.loads(cfg_path.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _load_vault_keys(engine: str) -> list[str]:
    """Best-effort load of ``vault://search.<engine>.keys``.

    Catches any vault import / open error so the script keeps working
    even when the agent runtime is not booted (CLI usage).
    """
    try:
        from nerya.security.secrets import SecretVault
    except Exception:
        return []
    workspace = os.environ.get("NERYA_WORKSPACE") or str(Path.home() / ".nerya")
    vault_path = Path(workspace).expanduser() / "vault" / "secrets.enc"
    if not vault_path.exists():
        return []
    try:
        vault = SecretVault.open(vault_path)
        raw = vault.resolve(f"search.{engine}.keys")
    except Exception:
        return []
    return _split_keys(raw)


def _resolve_base_url(engine: str, *, workspace_cfg: dict[str, Any],
                      extra_base_urls: dict[str, str] | None = None) -> str:
    """Pick base_url for an engine that needs one.

    Priority: kwargs > workspace JSON > env > default.
    """
    extra_base_urls = extra_base_urls or {}
    if extra_base_urls.get(engine):
        return str(extra_base_urls[engine]).strip().rstrip("/")
    base_urls_block = workspace_cfg.get("base_urls") or {}
    if isinstance(base_urls_block, dict) and base_urls_block.get(engine):
        return str(base_urls_block[engine]).strip().rstrip("/")
    env_name = f"NERYA_SEARCH_{engine.upper()}_BASE_URL"
    env_val = os.environ.get(env_name)
    if env_val:
        return env_val.strip().rstrip("/")
    return _DEFAULT_BASE_URLS.get(engine, "")


def resolve_config(
    *,
    engines: list[str] | None = None,
    region: str = "wt-wt",
    safesearch: str = "moderate",
    extra_keys: dict[str, list[str]] | None = None,
    extra_base_urls: dict[str, str] | None = None,
) -> SearchEngineConfig:
    """Build the engine chain.

    Resolution order for the chain itself:
        kwargs ``engines`` >  workspace JSON > env > default

    Resolution order for per-engine keys:
        kwargs ``extra_keys`` > workspace JSON > env > vault
        (all sources are *merged* in this order; duplicates dropped)
    """

    workspace_cfg = _load_workspace_config()
    sources: dict[str, str] = {}

    chain: list[str]
    if engines:
        chain = [e.strip() for e in engines if e and e.strip()]
        sources["chain"] = "kwargs"
    elif workspace_cfg.get("engines"):
        chain = [str(e).strip() for e in workspace_cfg["engines"] if str(e).strip()]
        sources["chain"] = "workspace"
    elif os.environ.get("NERYA_SEARCH_ENGINES"):
        chain = _split_keys(os.environ.get("NERYA_SEARCH_ENGINES"))
        sources["chain"] = "env"
    else:
        chain = list(_DEFAULT_CHAIN)
        sources["chain"] = "default"

    # Region / safesearch
    region = (workspace_cfg.get("region") or os.environ.get("NERYA_SEARCH_REGION")
              or region or "wt-wt")
    safesearch = (workspace_cfg.get("safesearch")
                  or os.environ.get("NERYA_SEARCH_SAFESEARCH")
                  or safesearch or "moderate")

    workspace_keys = (workspace_cfg.get("keys") or {})
    extra_keys = extra_keys or {}

    specs: list[EngineSpec] = []
    for engine in chain:
        merged: list[str] = []
        seen: set[str] = set()

        def _push(values: list[str]):
            for v in values:
                if v and v not in seen:
                    merged.append(v)
                    seen.add(v)

        _push(extra_keys.get(engine, []))
        _push(_split_keys(",".join(workspace_keys.get(engine, []) or [])))
        _push(_split_keys(os.environ.get(f"NERYA_SEARCH_{engine.upper()}_KEYS")))
        # Legacy single-key envs:
        legacy_envs = {
            "exa": "EXASEARCH_API_KEY",
            "tavily": "TAVILY_API_KEY",
            "perplexity": "PERPLEXITY_API_KEY",
            "brave": "BRAVE_API_KEY",
            "serper": "SERPER_API_KEY",
            "bing": "BING_SEARCH_KEY",
            "langsearch": "LANGSEARCH_API_KEY",
            "firecrawl": "FIRECRAWL_API_KEY",
        }
        if engine in legacy_envs:
            _push(_split_keys(os.environ.get(legacy_envs[engine])))
        _push(_load_vault_keys(engine))
        base_url = ""
        if engine in _BASE_URL_ENGINES:
            base_url = _resolve_base_url(
                engine,
                workspace_cfg=workspace_cfg,
                extra_base_urls=extra_base_urls,
            )
        specs.append(EngineSpec(name=engine, keys=merged, base_url=base_url))

    return SearchEngineConfig(
        engines=specs, region=region, safesearch=safesearch,
        sources=sources,
    )


__all__ = [
    "EngineSpec",
    "SearchEngineConfig",
    "resolve_config",
    "_KEYLESS_ENGINES",
    "_BASE_URL_ENGINES",
    "_DEFAULT_BASE_URLS",
]
