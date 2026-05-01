"""ModelCatalog — dynamic model discovery backed by provider ``/models`` endpoints.

The catalog is the *source of truth* for "which models can Nerya use right
now". It never hard-codes model IDs — instead it pulls them live from each
configured provider and caches the result on disk so the agent doesn't
re-hit provider APIs on every startup.

Usage
-----

    catalog = ModelCatalog(workspace=paths.root)
    catalog.refresh(tiers=config.get("llm.tiers") or {})
    models = catalog.list("openai")       # live list of model IDs
    catalog.exists("openai", "gpt-4o")    # validation before tier assignment

Cache layout:

    workspace/llm/model_catalog.json
      {
        "updated_at": "2026-04-21T00:00:00Z",
        "providers": {
          "openai":   [{"id": "...", ...}, ...],
          "anthropic": [...],
          ...
        }
      }

Secret handling: the catalog *only* resolves keys through the SecretVault (same
contract as ModelRouter). If a provider has no vault-backed key we record
``{"error": "no key"}`` and carry on.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..core.time import now_iso
from .providers import (
    DEFAULT_BASE_URLS,
    ModelInfo,
    Transport,
    builtin_providers,
)


@dataclass
class ModelCatalog:
    workspace: Path
    providers: dict[str, Any] = field(default_factory=dict, init=False)
    transport: Transport | None = None

    def __post_init__(self) -> None:
        self.providers = builtin_providers(self.transport)

    # ------------------------------------------------------------ paths
    @property
    def cache_path(self) -> Path:
        return self.workspace / "llm" / "model_catalog.json"

    # ------------------------------------------------------------ read
    def load(self) -> dict[str, Any]:
        if not self.cache_path.exists():
            return {}
        try:
            return json.loads(self.cache_path.read_text(encoding="utf-8"))
        except Exception:
            return {}

    def list(self, provider: str) -> list[dict[str, Any]]:
        cache = self.load()
        return (cache.get("providers") or {}).get(provider.lower()) or []

    def exists(self, provider: str, model_id: str) -> bool:
        return any(m.get("id") == model_id for m in self.list(provider))

    def import_models(
        self,
        *,
        provider: str,
        models: list[dict[str, Any]],
        base_url: str | None = None,
        merge: bool = True,
    ) -> dict[str, Any]:
        """Persist operator-selected models into the local catalog cache."""

        provider_id = (provider or "").strip().lower()
        if not provider_id:
            raise ValueError("provider is required")
        cleaned: list[dict[str, Any]] = []
        seen: set[str] = set()
        for row in models:
            if not isinstance(row, dict):
                continue
            mid = str(row.get("id") or row.get("model") or row.get("name") or "").strip()
            if not mid or mid in seen:
                continue
            caps = row.get("capabilities") or []
            if isinstance(caps, str):
                caps = [caps]
            elif not isinstance(caps, (list, tuple)):
                caps = []
            seen.add(mid)
            cleaned.append({
                "id": mid,
                "owned_by": str(row.get("owned_by") or row.get("owner") or provider_id),
                "context_length": row.get("context_length"),
                "capabilities": [str(c) for c in caps if str(c)],
            })

        doc = self.load()
        providers = dict(doc.get("providers") or {})
        existing = providers.get(provider_id) or []
        if merge:
            by_id = {
                str(row.get("id")): dict(row)
                for row in existing
                if isinstance(row, dict) and row.get("id")
            }
            for row in cleaned:
                by_id[row["id"]] = row
            providers[provider_id] = list(by_id.values())
        else:
            providers[provider_id] = cleaned

        doc["updated_at"] = now_iso()
        doc["providers"] = providers
        errors = dict(doc.get("errors") or {})
        errors.pop(provider_id, None)
        doc["errors"] = errors
        if base_url:
            meta = dict(doc.get("provider_meta") or {})
            meta[provider_id] = {"base_url": base_url}
            doc["provider_meta"] = meta
        self.cache_path.parent.mkdir(parents=True, exist_ok=True)
        self.cache_path.write_text(json.dumps(doc, indent=2), encoding="utf-8")
        return doc

    # ------------------------------------------------------------ refresh
    def refresh(
        self,
        *,
        tiers: dict[str, dict[str, Any]] | None = None,
        vault_passphrase: str | None = None,
    ) -> dict[str, Any]:
        """Refresh the catalog by calling each provider's list_models.

        `tiers` is the parsed ``llm.tiers`` mapping from ``nerya.yml``; we use
        it to discover which providers have a vault key and what base URL to
        hit. Providers with no tier config fall back to `DEFAULT_BASE_URLS`
        and are still queried *if* a matching vault key exists.
        """
        wanted: dict[str, dict[str, Any]] = {}
        for tier_cfg in (tiers or {}).values():
            p = (tier_cfg.get("provider") or "").lower()
            if not p or p == "mock":
                continue
            # merge per-tier settings (the first one wins)
            entry = wanted.setdefault(p, {
                "base_url": tier_cfg.get("base_url") or DEFAULT_BASE_URLS.get(p, ""),
                "key_ref": tier_cfg.get("provider_key_ref"),
            })
            if tier_cfg.get("base_url"):
                entry["base_url"] = tier_cfg["base_url"]

        out: dict[str, list[dict[str, Any]]] = {}
        errors: dict[str, str] = {}

        for provider, cfg in wanted.items():
            adapter = self.providers.get(provider)
            if adapter is None or not hasattr(adapter, "list_models"):
                errors[provider] = "no adapter"
                continue
            api_key = self._resolve_key(cfg.get("key_ref"), vault_passphrase)
            if not api_key and provider != "ollama":
                errors[provider] = "no key"
                continue
            try:
                # OpenAICompatAdapter uses `provider_name` as a kw arg; most
                # others just take (api_key, base_url). Pass through kindly.
                kwargs: dict[str, Any] = {
                    "api_key": api_key or "",
                    "base_url": cfg.get("base_url"),
                }
                try:
                    models = adapter.list_models(**kwargs, provider_name=provider)
                except TypeError:
                    models = adapter.list_models(**kwargs)
                out[provider] = [
                    _model_info_to_dict(m) if isinstance(m, ModelInfo) else dict(m)
                    for m in models
                ]
            except Exception as exc:
                errors[provider] = f"{type(exc).__name__}: {exc}"

        doc = {
            "updated_at": now_iso(),
            "providers": out,
            "errors": errors,
        }
        self.cache_path.parent.mkdir(parents=True, exist_ok=True)
        self.cache_path.write_text(json.dumps(doc, indent=2), encoding="utf-8")
        return doc

    # ------------------------------------------------------------ internal
    def _resolve_key(self, ref: str | None, vault_passphrase: str | None) -> str | None:
        if not ref or not ref.startswith("vault://"):
            return None
        try:
            from ..security.secrets import SecretVault
            vault_path = self.workspace / "vault" / "secrets.enc"
            if not vault_path.exists():
                return None
            vault = SecretVault.open(vault_path, passphrase=vault_passphrase)
            name = ref.split("vault://", 1)[-1]
            return vault.resolve(name, required_scope="llm")
        except Exception:
            return None


def _model_info_to_dict(m: ModelInfo) -> dict[str, Any]:
    return {
        "id": m.id,
        "owned_by": m.owned_by,
        "context_length": m.context_length,
        "capabilities": m.capabilities,
    }


__all__ = ["ModelCatalog"]
