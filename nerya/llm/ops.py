"""Operator-facing LLM control surfaces.

The CLI already exposes ``nerya llm models refresh`` / ``list`` and
related. This module gives the HTTP/SDK callers the same capabilities
so the dashboard can drive the LLM plane without shelling out.

Surfaces:

* :func:`provider_readiness` — per-provider "have adapter + have key"
  state, to power integration cards.
* :func:`tier_list` — the currently configured ``llm.tiers`` mapping.
* :func:`models_list` — cached catalog content.
* :func:`models_refresh` — refresh the catalog against live provider
  ``/models`` endpoints.
* :func:`validate_tier_assignment` — verifies a proposed
  provider/model/tier combination actually exists before it's written
  into config.

The adapter registry is the same one used by the gateway — we never
stand up a separate LLM stack just for the operator surface.
"""

from __future__ import annotations

import hashlib
import re
from typing import Any

from ..core import yaml_io
from ..core.config import Config
from ..llm.adapters import builtin_providers
from ..llm.model_catalog import ModelCatalog
from ..llm.providers import DEFAULT_BASE_URLS, ModelInfo
from ..llm.provider_routing import load as routing_load
from ..llm.provider_routing import save as routing_save
from ..security.secrets import SecretVault


def _get_cfg(config: Config, dotted: str, default: Any = None) -> Any:
    try:
        return config.get(dotted)
    except Exception:
        return default


def provider_readiness(config: Config) -> dict[str, Any]:
    """Return per-provider readiness: adapter present + key configured.

    Does NOT resolve the key itself — just reports whether a
    ``vault://`` reference is attached to any tier using the provider.
    That's enough for the operator UI to render a ``needs key`` badge
    without exposing the vault.
    """
    adapters = builtin_providers()
    tiers = _get_cfg(config, "llm.tiers", {}) or {}
    profiles = _provider_profiles(config)
    # Aggregate per provider: which tiers use it + whether any of them
    # has a ``provider_key_ref``.
    used: dict[str, dict[str, Any]] = {}
    for tier_name, tier_cfg in tiers.items():
        provider = (tier_cfg.get("provider") or "").lower()
        if not provider:
            continue
        entry = used.setdefault(provider, {"tiers": [], "has_key_ref": False,
                                           "base_url": None})
        entry["tiers"].append(tier_name)
        if tier_cfg.get("provider_key_ref"):
            entry["has_key_ref"] = True
        if tier_cfg.get("base_url"):
            entry["base_url"] = tier_cfg["base_url"]
    for provider, profile in profiles.items():
        entry = used.setdefault(provider, {"tiers": [], "has_key_ref": False,
                                           "base_url": None})
        if profile.get("provider_key_ref"):
            entry["has_key_ref"] = True
        if profile.get("base_url"):
            entry["base_url"] = profile["base_url"]
    out = []
    for name in sorted(set(adapters.keys()).union(profiles.keys())):
        info = used.get(name) or {"tiers": [], "has_key_ref": False,
                                    "base_url": None}
        out.append({
            "provider": name,
            "adapter_present": True,
            "base_url": info.get("base_url") or DEFAULT_BASE_URLS.get(name),
            "configured_tiers": info.get("tiers") or [],
            "has_key_ref": bool(info.get("has_key_ref")),
            "ready": name == "ollama" or bool(info.get("has_key_ref")),
        })
    return {"count": len(out), "providers": out}


def tier_list(config: Config) -> dict[str, Any]:
    tiers = _get_cfg(config, "llm.tiers", {}) or {}
    rows = []
    for tier_name, tier_cfg in sorted(tiers.items()):
        rows.append({
            "tier": tier_name,
            "provider": tier_cfg.get("provider"),
            "model": tier_cfg.get("model"),
            "base_url": tier_cfg.get("base_url"),
            "has_key_ref": bool(tier_cfg.get("provider_key_ref")),
        })
    return {"count": len(rows), "tiers": rows}


_TIER_RE = re.compile(r"^[A-Za-z0-9_.-]{1,48}$")
_PROVIDER_RE = re.compile(r"^[a-z0-9_.-]{1,64}$")


def _safe_secret_part(value: str) -> str:
    out = "".join(c if c.isalnum() or c in "-_." else "_" for c in value.lower())
    return out.strip("._-") or "provider"


def _llm_secret_name(provider: str, slot: str, value: str) -> str:
    digest = hashlib.sha1(
        f"llm::{provider}::{slot}::{value}".encode("utf-8")
    ).hexdigest()[:12]
    return f"llm_{_safe_secret_part(provider)}_{_safe_secret_part(slot)}_{digest}"


def _store_llm_key(
    config: Config,
    *,
    provider: str,
    slot: str,
    value: str,
    vault_passphrase: str | None = None,
) -> str:
    secret = str(value or "").strip()
    if not secret:
        return ""
    if secret.startswith("vault://"):
        return secret
    name = _llm_secret_name(provider, slot, secret)
    vault = SecretVault.open(config.paths.vault_enc, passphrase=vault_passphrase)
    vault.put(
        name=name,
        value=secret,
        kind="llm_provider_key",
        scope=["llm"],
        owner=f"llm/{provider}/{slot}",
    )
    return f"vault://{name}"


def _resolve_llm_key(
    config: Config,
    ref: str,
    *,
    vault_passphrase: str | None = None,
) -> str:
    if not ref.startswith("vault://"):
        return ""
    vault = SecretVault.open(config.paths.vault_enc, passphrase=vault_passphrase)
    return vault.resolve(ref.removeprefix("vault://"), required_scope="llm")


def _provider_profiles(config: Config) -> dict[str, dict[str, Any]]:
    raw = _get_cfg(config, "llm.providers", {}) or {}
    if not isinstance(raw, dict):
        return {}
    out: dict[str, dict[str, Any]] = {}
    for provider, cfg in raw.items():
        provider_id = str(provider or "").strip().lower()
        if not provider_id or not isinstance(cfg, dict):
            continue
        out[provider_id] = dict(cfg)
    return out


def _normalise_provider_profile(
    config: Config,
    raw: Any,
    *,
    vault_passphrase: str | None = None,
) -> tuple[str, dict[str, Any]]:
    if not isinstance(raw, dict):
        raise ValueError("provider row must be an object")
    provider = str(raw.get("provider") or raw.get("id") or "").strip().lower()
    if not provider or not _PROVIDER_RE.fullmatch(provider):
        raise ValueError(f"invalid provider: {provider!r}")
    base_url = str(raw.get("base_url") or "").strip()
    if base_url and not (base_url.startswith("http://") or base_url.startswith("https://")):
        raise ValueError(f"{provider}: base_url must start with http:// or https://")
    key_ref = str(raw.get("provider_key_ref") or "").strip()
    one_time_key = str(
        raw.get("provider_key") or raw.get("api_key") or raw.get("key") or ""
    ).strip()
    if one_time_key:
        key_ref = _store_llm_key(
            config,
            provider=provider,
            slot="provider",
            value=one_time_key,
            vault_passphrase=vault_passphrase,
        )
    elif key_ref and not key_ref.startswith("vault://"):
        key_ref = _store_llm_key(
            config,
            provider=provider,
            slot="provider",
            value=key_ref,
            vault_passphrase=vault_passphrase,
        )
    out: dict[str, Any] = {}
    if base_url:
        out["base_url"] = base_url
    if key_ref:
        out["provider_key_ref"] = key_ref
    return provider, out


def _tier_policy_defaults(tier: str) -> dict[str, Any]:
    if tier == "intent":
        return {
            "max_tokens": 2048,
            "temperature": 0.0,
            "timeout_s": 30,
            "daily_budget_usd": 3,
            "allowed_tasks": [
                "classify",
                "intent_classification",
                "trigger_triage",
                "news_filtering",
                "auto_session_title",
                "extract_json",
            ],
            "allowed_classes": [
                "classification",
                "structured_extraction",
            ],
        }
    return {}


def effective_tiers(config: Config) -> dict[str, dict[str, Any]]:
    """Return tier configs with provider-level profiles applied."""
    profiles = _provider_profiles(config)
    tiers = _get_cfg(config, "llm.tiers", {}) or {}
    out: dict[str, dict[str, Any]] = {}
    if not isinstance(tiers, dict):
        return out
    for tier, raw_cfg in tiers.items():
        cfg = dict(raw_cfg or {})
        provider = str(cfg.get("provider") or "").strip().lower()
        profile = profiles.get(provider) or {}
        for key in ("base_url", "provider_key_ref", "provider_key_env"):
            if not cfg.get(key) and profile.get(key):
                cfg[key] = profile[key]
        out[str(tier)] = cfg
    return out


def _normalise_model_tier_row(
    config: Config,
    raw: Any,
    *,
    vault_passphrase: str | None = None,
) -> tuple[str, dict[str, Any]]:
    if not isinstance(raw, dict):
        raise ValueError("tier row must be an object")
    tier = str(raw.get("tier") or "").strip()
    if not _TIER_RE.fullmatch(tier):
        raise ValueError(f"invalid tier name: {tier!r}")
    provider = str(raw.get("provider") or "").strip().lower()
    model = str(raw.get("model") or "").strip()
    if not provider:
        raise ValueError(f"{tier}: provider is required")
    if not _PROVIDER_RE.fullmatch(provider):
        raise ValueError(f"{tier}: invalid provider {provider!r}")
    if not model:
        raise ValueError(f"{tier}: model is required")
    if len(model) > 160:
        raise ValueError(f"{tier}: model id is too long")
    base_url = str(raw.get("base_url") or "").strip()
    provider_key_ref = str(raw.get("provider_key_ref") or "").strip()
    one_time_key = str(
        raw.get("provider_key") or raw.get("api_key") or raw.get("key") or ""
    ).strip()
    if one_time_key:
        provider_key_ref = _store_llm_key(
            config,
            provider=provider,
            slot=tier,
            value=one_time_key,
            vault_passphrase=vault_passphrase,
        )
    elif provider_key_ref and not provider_key_ref.startswith("vault://"):
        provider_key_ref = _store_llm_key(
            config,
            provider=provider,
            slot=tier,
            value=provider_key_ref,
            vault_passphrase=vault_passphrase,
        )
    out: dict[str, Any] = {
        "provider": provider,
        "model": model,
    }
    if base_url:
        if not (base_url.startswith("http://") or base_url.startswith("https://")):
            raise ValueError(f"{tier}: base_url must start with http:// or https://")
        out["base_url"] = base_url
    if provider_key_ref:
        out["provider_key_ref"] = provider_key_ref
    return tier, out


def llm_config(config: Config) -> dict[str, Any]:
    tiers = _get_cfg(config, "llm.tiers", {}) or {}
    profiles = _provider_profiles(config)
    rows = []
    for tier_name, tier_cfg in sorted(tiers.items()):
        provider = str(tier_cfg.get("provider") or "").strip().lower()
        inherited = profiles.get(provider) or {}
        rows.append({
            "tier": tier_name,
            "provider": provider,
            "model": tier_cfg.get("model") or "",
            "base_url": tier_cfg.get("base_url") or inherited.get("base_url") or "",
            "provider_key_ref": tier_cfg.get("provider_key_ref") or inherited.get("provider_key_ref") or "",
            "has_key_ref": bool(tier_cfg.get("provider_key_ref") or inherited.get("provider_key_ref")),
        })
    profile_rows = []
    for provider, profile in sorted(profiles.items()):
        profile_rows.append({
            "provider": provider,
            "base_url": profile.get("base_url") or DEFAULT_BASE_URLS.get(provider) or "",
            "provider_key_ref": profile.get("provider_key_ref") or "",
            "has_key_ref": bool(profile.get("provider_key_ref")),
        })
    return {
        "ok": True,
        "default_tier": _get_cfg(config, "llm.default_tier", "medium"),
        "intent_tier": _get_cfg(config, "llm.intent_tier", "light"),
        "provider_profiles": profile_rows,
        "tiers": rows,
    }


def llm_config_set(
    config: Config,
    *,
    default_tier: str | None = None,
    intent_tier: str | None = None,
    providers: list[Any] | None = None,
    tiers: list[Any] | None = None,
    vault_passphrase: str | None = None,
) -> dict[str, Any]:
    """Persist operator-selected LLM tier/provider/model assignments.

    Model-routing fields are writable here. One-time plaintext provider keys
    are accepted only at this edge, immediately stored in SecretVault, and
    persisted as ``vault://`` references.
    Existing tier policy fields such as budgets, allowed tasks, prices,
    and reasoning controls are preserved.
    """
    existing = yaml_io.load(config.paths.config, default={}) or {}
    if not isinstance(existing, dict):
        existing = {}
    llm = existing.setdefault("llm", {})
    if not isinstance(llm, dict):
        llm = {}
        existing["llm"] = llm

    current_tiers = dict(config.get("llm.tiers") or {})
    current_profiles = _provider_profiles(config)
    yaml_profiles = llm.setdefault("providers", {})
    if not isinstance(yaml_profiles, dict):
        yaml_profiles = {}
        llm["providers"] = yaml_profiles
    yaml_tiers = llm.setdefault("tiers", {})
    if not isinstance(yaml_tiers, dict):
        yaml_tiers = {}
        llm["tiers"] = yaml_tiers

    if providers is not None:
        for raw in providers:
            provider, patch = _normalise_provider_profile(
                config, raw, vault_passphrase=vault_passphrase,
            )
            merged_profile = dict(current_profiles.get(provider) or {})
            merged_profile.update(dict(yaml_profiles.get(provider) or {}))
            for key in ("base_url", "provider_key_ref"):
                if key in patch:
                    merged_profile[key] = patch[key]
            yaml_profiles[provider] = merged_profile

    if tiers is not None:
        seen: set[str] = set()
        for raw in tiers:
            tier, patch = _normalise_model_tier_row(
                config, raw, vault_passphrase=vault_passphrase,
            )
            seen.add(tier)
            merged = dict(current_tiers.get(tier) or {})
            merged.update(dict(yaml_tiers.get(tier) or {}))
            for key, value in _tier_policy_defaults(tier).items():
                merged.setdefault(key, value)
            for key in ("provider", "model", "base_url", "provider_key_ref"):
                if key in patch:
                    merged[key] = patch[key]
                else:
                    merged.pop(key, None)
            yaml_tiers[tier] = merged
        if default_tier and default_tier not in seen and default_tier not in current_tiers:
            raise ValueError(f"default_tier {default_tier!r} is not configured")

    if default_tier is not None:
        default = str(default_tier or "").strip()
        if default and not _TIER_RE.fullmatch(default):
            raise ValueError(f"invalid default_tier: {default!r}")
        if default:
            llm["default_tier"] = default

    if intent_tier is not None:
        intent = str(intent_tier or "").strip()
        if intent and not _TIER_RE.fullmatch(intent):
            raise ValueError(f"invalid intent_tier: {intent!r}")
        if intent:
            known_tiers = set(current_tiers).union(yaml_tiers)
            if intent not in known_tiers:
                raise ValueError(f"intent_tier {intent!r} is not configured")
            llm["intent_tier"] = intent

    yaml_io.dump(config.paths.config, existing)
    config.data.setdefault("llm", {})
    config.data["llm"].update(llm)
    return llm_config(config)


def models_list(config: Config) -> dict[str, Any]:
    catalog = ModelCatalog(workspace=config.paths.root)
    doc = catalog.load()
    providers = doc.get("providers") or {}
    errors = doc.get("errors") or {}
    return {
        "updated_at": doc.get("updated_at"),
        "providers": providers,
        "errors": errors,
        "counts": {k: len(v) for k, v in providers.items()},
    }


def models_refresh(
    config: Config, *, vault_passphrase: str | None = None,
) -> dict[str, Any]:
    catalog = ModelCatalog(workspace=config.paths.root)
    tiers = effective_tiers(config)
    for provider, profile in _provider_profiles(config).items():
        pseudo_name = f"provider:{provider}"
        tiers.setdefault(pseudo_name, {
            "provider": provider,
            "base_url": profile.get("base_url") or DEFAULT_BASE_URLS.get(provider, ""),
            "provider_key_ref": profile.get("provider_key_ref"),
        })
    return catalog.refresh(tiers=tiers, vault_passphrase=vault_passphrase)


def _persist_provider_profile(
    config: Config,
    *,
    provider: str,
    base_url: str | None = None,
    provider_key_ref: str | None = None,
) -> None:
    if not provider:
        return
    existing = yaml_io.load(config.paths.config, default={}) or {}
    if not isinstance(existing, dict):
        existing = {}
    llm = existing.setdefault("llm", {})
    if not isinstance(llm, dict):
        llm = {}
        existing["llm"] = llm
    profiles = llm.setdefault("providers", {})
    if not isinstance(profiles, dict):
        profiles = {}
        llm["providers"] = profiles
    profile = dict(profiles.get(provider) or {})
    if base_url:
        profile["base_url"] = base_url
    if provider_key_ref:
        profile["provider_key_ref"] = provider_key_ref
    profiles[provider] = profile
    yaml_io.dump(config.paths.config, existing)
    config.data.setdefault("llm", {})
    config.data["llm"].setdefault("providers", {})
    if not isinstance(config.data["llm"]["providers"], dict):
        config.data["llm"]["providers"] = {}
    config.data["llm"]["providers"][provider] = profile


def _model_info_to_dict(model: Any, provider: str) -> dict[str, Any]:
    def _capabilities(value: Any) -> list[str]:
        if isinstance(value, list):
            return [str(v) for v in value if str(v)]
        if isinstance(value, tuple):
            return [str(v) for v in value if str(v)]
        if isinstance(value, str) and value:
            return [value]
        return []

    if isinstance(model, ModelInfo):
        return {
            "id": model.id,
            "owned_by": model.owned_by,
            "context_length": model.context_length,
            "capabilities": _capabilities(model.capabilities),
        }
    if isinstance(model, dict):
        mid = str(model.get("id") or model.get("model") or model.get("name") or "").strip()
        return {
            "id": mid,
            "owned_by": str(model.get("owned_by") or model.get("owner") or provider),
            "context_length": model.get("context_length"),
            "capabilities": _capabilities(model.get("capabilities")),
        }
    mid = str(model or "").strip()
    return {
        "id": mid,
        "owned_by": provider,
        "context_length": None,
        "capabilities": [],
    }


def models_discover(
    config: Config,
    *,
    provider: str,
    base_url: str | None = None,
    provider_key: str | None = None,
    provider_key_ref: str | None = None,
    vault_passphrase: str | None = None,
) -> dict[str, Any]:
    """Call one provider's live model-list endpoint for onboarding."""

    provider_id = (provider or "").strip().lower()
    if not provider_id or not _PROVIDER_RE.fullmatch(provider_id):
        raise ValueError("valid provider is required")
    target_base_url = str(base_url or DEFAULT_BASE_URLS.get(provider_id) or "").strip()
    if target_base_url and not (
        target_base_url.startswith("http://") or target_base_url.startswith("https://")
    ):
        raise ValueError("base_url must start with http:// or https://")

    key_ref = str(provider_key_ref or "").strip()
    one_time_key = str(provider_key or "").strip()
    if one_time_key:
        key_ref = _store_llm_key(
            config,
            provider=provider_id,
            slot="provider",
            value=one_time_key,
            vault_passphrase=vault_passphrase,
        )
    elif key_ref and not key_ref.startswith("vault://"):
        key_ref = _store_llm_key(
            config,
            provider=provider_id,
            slot="provider",
            value=key_ref,
            vault_passphrase=vault_passphrase,
        )

    api_key = ""
    if key_ref:
        api_key = _resolve_llm_key(
            config, key_ref, vault_passphrase=vault_passphrase,
        )
    if provider_id != "ollama" and not api_key:
        return {
            "ok": False,
            "error": "provider_key_required",
            "detail": "store or paste a provider API key before discovering models",
        }

    adapter = builtin_providers().get(provider_id)
    if adapter is None or not hasattr(adapter, "list_models"):
        return {"ok": False, "error": "no_adapter", "detail": provider_id}

    try:
        kwargs: dict[str, Any] = {
            "api_key": api_key,
            "base_url": target_base_url or None,
        }
        try:
            models = adapter.list_models(**kwargs, provider_name=provider_id)
        except TypeError:
            models = adapter.list_models(**kwargs)
    except Exception as exc:
        return {
            "ok": False,
            "error": "discover_failed",
            "detail": f"{type(exc).__name__}: {exc}",
            "provider": provider_id,
            "base_url": target_base_url,
            "provider_key_ref": key_ref,
        }

    rows = [
        row for row in (_model_info_to_dict(m, provider_id) for m in models)
        if row.get("id")
    ]
    _persist_provider_profile(
        config,
        provider=provider_id,
        base_url=target_base_url,
        provider_key_ref=key_ref,
    )
    return {
        "ok": True,
        "provider": provider_id,
        "base_url": target_base_url,
        "provider_key_ref": key_ref,
        "models": rows,
        "count": len(rows),
    }


def models_import(
    config: Config,
    *,
    provider: str,
    models: list[Any],
    base_url: str | None = None,
) -> dict[str, Any]:
    provider_id = (provider or "").strip().lower()
    if not provider_id or not _PROVIDER_RE.fullmatch(provider_id):
        raise ValueError("valid provider is required")
    rows = [
        _model_info_to_dict(m, provider_id)
        for m in (models or [])
        if isinstance(m, (dict, str)) or m is not None
    ]
    if not rows:
        raise ValueError("at least one model is required")
    catalog = ModelCatalog(workspace=config.paths.root)
    doc = catalog.import_models(
        provider=provider_id,
        models=rows,
        base_url=str(base_url or "") or None,
        merge=True,
    )
    return {
        "ok": True,
        "provider": provider_id,
        "imported": len(rows),
        "updated_at": doc.get("updated_at"),
        "providers": doc.get("providers") or {},
        "errors": doc.get("errors") or {},
        "counts": {
            k: len(v)
            for k, v in (doc.get("providers") or {}).items()
            if isinstance(v, list)
        },
    }


def validate_tier_assignment(
    config: Config, *, provider: str, model: str,
) -> dict[str, Any]:
    """Check if (provider, model) is known to the refreshed catalog."""
    catalog = ModelCatalog(workspace=config.paths.root)
    known = catalog.exists(provider, model)
    sample: list[dict[str, Any]] = []
    if not known:
        sample = catalog.list(provider)[:8]
    return {
        "provider": provider,
        "model": model,
        "known": known,
        "sample_models": sample,
    }


def provider_routing_get(config: Config) -> dict[str, Any]:
    return routing_load(config.paths.root)


def provider_routing_set(
    config: Config, *,
    default: dict[str, Any] | None = None,
    per_provider: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return routing_save(
        config.paths.root,
        default=default or {},
        per_provider=per_provider or {},
    )


__all__ = [
    "provider_readiness",
    "tier_list",
    "effective_tiers",
    "llm_config",
    "llm_config_set",
    "models_list",
    "models_refresh",
    "models_discover",
    "models_import",
    "validate_tier_assignment",
    "provider_routing_get",
    "provider_routing_set",
]
