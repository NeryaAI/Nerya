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
import ipaddress
import json
import re
from typing import Any
from urllib.parse import urlparse

from ..core import yaml_io
from ..core.config import Config
from ..core.normalize import norm_key
from ..llm.adapters import builtin_providers
from ..llm.messages import normalise_provider_native_web_search
from ..llm.model_catalog import ModelCatalog
from ..llm.provider_catalog import (
    REASONING_EFFORT_LEVELS,
    catalog_for_dashboard,
    default_base_url as _catalog_default_base_url,
    lookup as _catalog_lookup,
)
from ..llm.route_candidates import configured_routes, split_csv_values
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
        if not isinstance(tier_cfg, dict):
            continue
        for route_cfg in configured_routes(tier_cfg):
            provider = (route_cfg.get("provider") or "").lower()
            if not provider:
                continue
            entry = used.setdefault(provider, {"tiers": [], "has_key_ref": False,
                                               "base_url": None})
            if tier_name not in entry["tiers"]:
                entry["tiers"].append(tier_name)
            if route_cfg.get("provider_key_ref"):
                entry["has_key_ref"] = True
            if route_cfg.get("base_url"):
                entry["base_url"] = route_cfg["base_url"]
    for provider, profile in profiles.items():
        entry = used.setdefault(provider, {"tiers": [], "has_key_ref": False,
                                           "base_url": None})
        if profile.get("provider_key_ref"):
            entry["has_key_ref"] = True
        if profile.get("base_url"):
            entry["base_url"] = profile["base_url"]
    out = []
    for name in sorted(set(adapters.keys()).union(profiles.keys()).union(used.keys())):
        info = used.get(name) or {"tiers": [], "has_key_ref": False,
                                    "base_url": None}
        catalog_entry = _catalog_lookup(name)
        out.append({
            "provider": name,
            "adapter_present": True,
            "base_url": info.get("base_url")
                        or _catalog_default_base_url(name)
                        or DEFAULT_BASE_URLS.get(name),
            "configured_tiers": info.get("tiers") or [],
            "has_key_ref": bool(info.get("has_key_ref")),
            "ready": (
                name == "ollama"
                or bool(info.get("has_key_ref"))
                or bool(catalog_entry and catalog_entry.extra.get("key_optional"))
            ),
            "auth_type": catalog_entry.auth_type if catalog_entry else "api_key",
            "api_mode": catalog_entry.api_mode if catalog_entry else "chat_completions",
            "display_name": catalog_entry.name if catalog_entry else name,
        })
    return {"count": len(out), "providers": out}


def tier_list(config: Config) -> dict[str, Any]:
    tiers = effective_tiers(config)
    rows = []
    for tier_name, tier_cfg in sorted(tiers.items()):
        routes = [_route_config_to_row(route, {}) for route in configured_routes(tier_cfg)]
        rows.append({
            "tier": tier_name,
            "provider": tier_cfg.get("provider"),
            "model": tier_cfg.get("model"),
            "models": split_csv_values(tier_cfg.get("model")),
            "base_url": tier_cfg.get("base_url"),
            "has_key_ref": bool(tier_cfg.get("provider_key_ref")),
            "reasoning_effort": tier_cfg.get("reasoning_effort") or "",
            "context_window": _normalise_model_context_window(
                tier_cfg.get("context_window"), label=str(tier_name)
            ),
            "routes": routes,
            "provider_native_web_search": normalise_provider_native_web_search(
                tier_cfg.get("provider_native_web_search")
            ),
        })
    return {"count": len(rows), "tiers": rows}


def catalog(config: Config) -> dict[str, Any]:  # noqa: ARG001 — keep client signature
    """Return the operator-facing provider catalogue.

    The dashboard pulls this so it can render a provider dropdown
    without hardcoding the list. ``reasoning_levels`` is the canonical
    set of effort rungs the UI should expose.
    """
    return {
        "providers": catalog_for_dashboard(),
        "reasoning_levels": list(REASONING_EFFORT_LEVELS),
    }


_TIER_RE = re.compile(r"^[A-Za-z0-9_.-]{1,48}$")
_PROVIDER_RE = re.compile(r"^[a-z0-9_.-]{1,64}$")
DEFAULT_MODEL_CONTEXT_WINDOW = 1_048_576
_MIN_MODEL_CONTEXT_WINDOW = 4_096
_MAX_MODEL_CONTEXT_WINDOW = 16_777_216


def _normalise_model_context_window(value: Any, *, label: str) -> int:
    if value in (None, ""):
        return DEFAULT_MODEL_CONTEXT_WINDOW
    raw = str(value).strip().lower().replace("_", "")
    try:
        if raw.endswith("k"):
            window = int(float(raw[:-1]) * 1_000)
        elif raw.endswith("m"):
            window = int(float(raw[:-1]) * 1_000_000)
        else:
            window = int(raw)
    except (TypeError, ValueError):
        raise ValueError(f"{label}: context_window must be a token count") from None
    if not (_MIN_MODEL_CONTEXT_WINDOW <= window <= _MAX_MODEL_CONTEXT_WINDOW):
        raise ValueError(
            f"{label}: context_window must be between "
            f"{_MIN_MODEL_CONTEXT_WINDOW} and {_MAX_MODEL_CONTEXT_WINDOW} tokens"
        )
    return window


def _looks_like_local_url(raw: str) -> bool:
    """Return True only for loopback model endpoints that may omit API keys."""
    try:
        parsed = urlparse(str(raw or ""))
    except ValueError:
        return False
    host = (parsed.hostname or "").strip().lower()
    if not host:
        return False
    if host in {"localhost", "ip6-localhost", "ip6-loopback"}:
        return True
    if host.endswith(".localhost"):
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


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


def _normalise_provider_key_refs(
    config: Config,
    *,
    provider: str,
    slot: str,
    value: Any,
    vault_passphrase: str | None = None,
) -> str:
    refs: list[str] = []
    for index, item in enumerate(split_csv_values(value), start=1):
        if item.startswith("vault://"):
            refs.append(item)
            continue
        refs.append(
            _store_llm_key(
                config,
                provider=provider,
                slot=f"{slot}_{index}" if index > 1 else slot,
                value=item,
                vault_passphrase=vault_passphrase,
            )
        )
    return ", ".join(ref for ref in refs if ref)


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
    key_ref = _normalise_provider_key_refs(
        config,
        provider=provider,
        slot="provider",
        value=raw.get("provider_key_refs") or raw.get("provider_key_ref"),
        vault_passphrase=vault_passphrase,
    )
    one_time_key = ", ".join(
        split_csv_values(
            raw.get("provider_keys")
            or raw.get("api_keys")
            or raw.get("provider_key")
            or raw.get("api_key")
            or raw.get("key")
            or ""
        )
    )
    if one_time_key:
        key_ref = _store_llm_key(
            config,
            provider=provider,
            slot="provider",
            value=one_time_key,
            vault_passphrase=vault_passphrase,
        )
    out: dict[str, Any] = {}
    if base_url:
        out["base_url"] = base_url
    if key_ref:
        out["provider_key_ref"] = key_ref
    # Custom providers can override the API shape: ``chat_completions``
    # routes through OpenAI-compat, ``anthropic_messages`` routes through
    # Anthropic Messages. When omitted we infer from the catalog.
    raw_kind = str(raw.get("kind") or "").strip().lower()
    if raw_kind:
        if raw_kind not in {"chat_completions", "anthropic_messages"}:
            raise ValueError(
                f"{provider}: kind must be 'chat_completions' or "
                f"'anthropic_messages', got {raw_kind!r}"
            )
        out["kind"] = raw_kind
    raw_name = str(raw.get("name") or raw.get("display_name") or "").strip()
    if raw_name:
        out["name"] = raw_name[:80]
    if "provider_native_web_search" in raw:
        out["provider_native_web_search"] = normalise_provider_native_web_search(
            raw.get("provider_native_web_search")
        )
    return provider, out


def _apply_provider_profile(
    cfg: dict[str, Any],
    profiles: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    out = dict(cfg or {})
    provider = str(out.get("provider") or "").strip().lower()
    profile = profiles.get(provider) or {}
    for key in (
        "base_url", "provider_key_ref", "provider_key_env", "kind",
        "provider_native_web_search",
    ):
        if key == "provider_native_web_search":
            if key not in out and profile.get(key) is not None:
                out[key] = profile[key]
        elif not out.get(key) and profile.get(key):
            out[key] = profile[key]
    return out


def _tier_policy_defaults(tier: str) -> dict[str, Any]:
    if tier == "intent":
        return {
            "max_tokens": 2048,
            "temperature": 0.0,
            "timeout_s": 30,
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
        cfg = _apply_provider_profile(cfg, profiles)
        if cfg.get("routes"):
            routes = [
                _apply_provider_profile(route, profiles)
                for route in configured_routes(cfg)
            ]
            cfg["routes"] = routes
            if routes:
                first = routes[0]
                for key in (
                    "provider", "model", "base_url", "provider_key_ref",
                    "provider_key_env", "kind",
                    "provider_native_web_search",
                ):
                    if key in first:
                        cfg[key] = first[key]
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
    raw_routes = raw.get("routes")
    if isinstance(raw_routes, list) and raw_routes:
        routes = [
            _normalise_route_row(
                config,
                tier=tier,
                raw_route=route,
                route_index=index,
                vault_passphrase=vault_passphrase,
            )
            for index, route in enumerate(raw_routes)
        ]
        routes = [
            route for route in routes
            if route.get("provider") and route.get("model")
        ]
        if not routes:
            raise ValueError(f"{tier}: at least one route requires provider and model")
        out: dict[str, Any] = {"routes": routes}
        first = routes[0]
        for key in (
            "provider", "model", "base_url", "provider_key_ref", "kind",
            "context_window", "provider_native_web_search",
        ):
            if key in first:
                out[key] = first[key]
        raw_effort = raw.get("reasoning_effort")
        if raw_effort is not None:
            eff = norm_key(str(raw_effort))
            if eff and eff not in REASONING_EFFORT_LEVELS:
                raise ValueError(
                    f"{tier}: invalid reasoning_effort {raw_effort!r}; "
                    f"must be one of {list(REASONING_EFFORT_LEVELS)}"
                )
            if eff:
                out["reasoning_effort"] = eff
        if "provider_native_web_search" in raw:
            out["provider_native_web_search"] = normalise_provider_native_web_search(
                raw.get("provider_native_web_search")
            )
        return tier, out

    provider = str(raw.get("provider") or "").strip().lower()
    model_values = split_csv_values(raw.get("models"))
    if not model_values:
        model_values = split_csv_values(raw.get("model"))
    model = ", ".join(model_values)
    if not provider:
        raise ValueError(f"{tier}: provider is required")
    if not _PROVIDER_RE.fullmatch(provider):
        raise ValueError(f"{tier}: invalid provider {provider!r}")
    if not model:
        raise ValueError(f"{tier}: model is required")
    if any(len(item) > 160 for item in model_values):
        raise ValueError(f"{tier}: model id is too long")
    base_url = str(raw.get("base_url") or "").strip()
    provider_key_ref = _normalise_provider_key_refs(
        config,
        provider=provider,
        slot=tier,
        value=raw.get("provider_key_refs") or raw.get("provider_key_ref"),
        vault_passphrase=vault_passphrase,
    )
    one_time_key = ", ".join(
        split_csv_values(
            raw.get("provider_keys")
            or raw.get("api_keys")
            or raw.get("provider_key")
            or raw.get("api_key")
            or raw.get("key")
            or ""
        )
    )
    if one_time_key:
        provider_key_ref = _store_llm_key(
            config,
            provider=provider,
            slot=tier,
            value=one_time_key,
            vault_passphrase=vault_passphrase,
        )
    out: dict[str, Any] = {
        "provider": provider,
        "model": model,
        "context_window": _normalise_model_context_window(
            raw.get("context_window"), label=tier
        ),
    }
    if base_url:
        if not (base_url.startswith("http://") or base_url.startswith("https://")):
            raise ValueError(f"{tier}: base_url must start with http:// or https://")
        out["base_url"] = base_url
    if provider_key_ref:
        out["provider_key_ref"] = provider_key_ref
    # Reasoning effort is a per-tier knob, validated against the
    # canonical level list so the UI and the router agree on
    # vocabulary. Unknown values are rejected up-front to fail loud.
    raw_effort = raw.get("reasoning_effort")
    if raw_effort is not None:
        eff = norm_key(str(raw_effort))
        if eff and eff not in REASONING_EFFORT_LEVELS:
            raise ValueError(
                f"{tier}: invalid reasoning_effort {raw_effort!r}; "
                f"must be one of {list(REASONING_EFFORT_LEVELS)}"
            )
        if eff:
            out["reasoning_effort"] = eff
    if "provider_native_web_search" in raw:
        out["provider_native_web_search"] = normalise_provider_native_web_search(
            raw.get("provider_native_web_search")
        )
    return tier, out


def _normalise_route_row(
    config: Config,
    *,
    tier: str,
    raw_route: Any,
    route_index: int,
    vault_passphrase: str | None = None,
) -> dict[str, Any]:
    if not isinstance(raw_route, dict):
        raise ValueError(f"{tier}: route {route_index + 1} must be an object")
    provider = str(raw_route.get("provider") or "").strip().lower()
    model_values = split_csv_values(raw_route.get("models"))
    if not model_values:
        model_values = split_csv_values(raw_route.get("model"))
    model = ", ".join(model_values)
    if not provider:
        raise ValueError(f"{tier}: route {route_index + 1} provider is required")
    if not _PROVIDER_RE.fullmatch(provider):
        raise ValueError(
            f"{tier}: route {route_index + 1} invalid provider {provider!r}"
        )
    if not model:
        raise ValueError(f"{tier}: route {route_index + 1} model is required")
    if any(len(item) > 160 for item in model_values):
        raise ValueError(f"{tier}: route {route_index + 1} model id is too long")
    base_url = str(raw_route.get("base_url") or "").strip()
    if base_url and not (
        base_url.startswith("http://") or base_url.startswith("https://")
    ):
        raise ValueError(
            f"{tier}: route {route_index + 1} base_url must start with http:// or https://"
        )
    route_slot = f"{tier}_route_{route_index + 1}"
    provider_key_ref = _normalise_provider_key_refs(
        config,
        provider=provider,
        slot=route_slot,
        value=raw_route.get("provider_key_refs") or raw_route.get("provider_key_ref"),
        vault_passphrase=vault_passphrase,
    )
    one_time_key = ", ".join(
        split_csv_values(
            raw_route.get("provider_keys")
            or raw_route.get("api_keys")
            or raw_route.get("provider_key")
            or raw_route.get("api_key")
            or raw_route.get("key")
            or ""
        )
    )
    if one_time_key:
        provider_key_ref = _store_llm_key(
            config,
            provider=provider,
            slot=route_slot,
            value=one_time_key,
            vault_passphrase=vault_passphrase,
        )
    out: dict[str, Any] = {
        "provider": provider,
        "model": model,
        "context_window": _normalise_model_context_window(
            raw_route.get("context_window"),
            label=f"{tier}: route {route_index + 1}",
        ),
    }
    if base_url:
        out["base_url"] = base_url
    if provider_key_ref:
        out["provider_key_ref"] = provider_key_ref
    raw_effort = raw_route.get("reasoning_effort")
    if raw_effort is not None:
        eff = norm_key(str(raw_effort))
        if eff and eff not in REASONING_EFFORT_LEVELS:
            raise ValueError(
                f"{tier}: route {route_index + 1} invalid reasoning_effort "
                f"{raw_effort!r}; must be one of {list(REASONING_EFFORT_LEVELS)}"
            )
        if eff:
            out["reasoning_effort"] = eff
    raw_kind = str(raw_route.get("kind") or "").strip().lower()
    if raw_kind:
        if raw_kind not in {"chat_completions", "anthropic_messages"}:
            raise ValueError(
                f"{tier}: route {route_index + 1} kind must be "
                f"'chat_completions' or 'anthropic_messages', got {raw_kind!r}"
            )
        out["kind"] = raw_kind
    if "provider_native_web_search" in raw_route:
        out["provider_native_web_search"] = normalise_provider_native_web_search(
            raw_route.get("provider_native_web_search")
        )
    return out


def _route_config_to_row(
    route_cfg: dict[str, Any],
    inherited: dict[str, Any] | None = None,
) -> dict[str, Any]:
    inherited = inherited or {}
    provider = str(route_cfg.get("provider") or "").strip().lower()
    key_ref = route_cfg.get("provider_key_ref") or inherited.get("provider_key_ref") or ""
    key_refs = split_csv_values(key_ref)
    return {
        "provider": provider,
        "model": route_cfg.get("model") or "",
        "models": split_csv_values(route_cfg.get("model")),
        "reasoning_effort": str(
            route_cfg.get("reasoning_effort")
            or inherited.get("reasoning_effort")
            or ""
        ).strip().lower(),
        "context_window": _normalise_model_context_window(
            route_cfg.get("context_window", inherited.get("context_window")),
            label=f"{provider or 'model'} route",
        ),
        "base_url": route_cfg.get("base_url") or inherited.get("base_url") or "",
        "provider_key_ref": key_ref,
        "provider_key_refs": key_refs,
        "has_key_ref": bool(key_ref),
        "kind": str(route_cfg.get("kind") or inherited.get("kind") or "").strip(),
        "provider_native_web_search": normalise_provider_native_web_search(
            route_cfg.get(
                "provider_native_web_search",
                inherited.get("provider_native_web_search"),
            )
        ),
    }


def llm_config(config: Config) -> dict[str, Any]:
    tiers = effective_tiers(config)
    declared_tiers = _get_cfg(config, "llm.tiers", {}) or {}
    profiles = _provider_profiles(config)
    rows = []
    for tier_name, tier_cfg in sorted(tiers.items()):
        provider = str(tier_cfg.get("provider") or "").strip().lower()
        inherited = profiles.get(provider) or {}
        routes = [
            _route_config_to_row(
                route,
                profiles.get(str(route.get("provider") or "").strip().lower()) or {},
            )
            for route in configured_routes(tier_cfg)
        ]
        declared_tier = declared_tiers.get(tier_name) or {}
        raw_routes = declared_tier.get("routes")
        declared_routes = ([route for route in (raw_routes if isinstance(raw_routes, list) else [raw_routes]) if isinstance(route, dict) and route] if raw_routes else []) or [declared_tier]
        for index, route in enumerate(routes):
            declared = _declared_fields(declared_routes[index])
            route_provider = route["provider"]
            profile = profiles.get(route_provider) or {}
            entry = _catalog_lookup(route_provider)
            effective = dict(route)
            effective["base_url"] = route["base_url"] or _catalog_default_base_url(route_provider) or DEFAULT_BASE_URLS.get(route_provider) or ""
            effective["kind"] = route["kind"] or (entry.api_mode if entry else "chat_completions")
            source = {}
            for key in _DECLARED_MODEL_FIELDS:
                if key in declared and declared[key] not in (None, ""):
                    source[key] = "route" if declared_tier.get("routes") else "tier"
                elif key in {"reasoning_effort", "context_window", "provider_native_web_search"} and key in declared_tier:
                    source[key] = "tier"
                elif key in {"base_url", "provider_key_ref", "provider_key_env", "kind", "provider_native_web_search"} and profile.get(key) not in (None, ""):
                    source[key] = "provider"
                else:
                    source[key] = "catalog" if key in {"base_url", "kind"} and entry else "default"
            route.update(declared=declared, effective=effective, source=source)
        rows.append({
            "tier": tier_name,
            "provider": provider,
            "model": tier_cfg.get("model") or "",
            "models": split_csv_values(tier_cfg.get("model")),
            "base_url": tier_cfg.get("base_url") or inherited.get("base_url") or "",
            "provider_key_ref": tier_cfg.get("provider_key_ref") or inherited.get("provider_key_ref") or "",
            "has_key_ref": bool(tier_cfg.get("provider_key_ref") or inherited.get("provider_key_ref")),
            "reasoning_effort": str(tier_cfg.get("reasoning_effort") or "").strip().lower(),
            "context_window": _normalise_model_context_window(
                tier_cfg.get("context_window"), label=str(tier_name)
            ),
            "routes": routes,
            "declared": _declared_fields(declared_tier),
            "provider_native_web_search": normalise_provider_native_web_search(
                tier_cfg.get(
                    "provider_native_web_search",
                    inherited.get("provider_native_web_search"),
                )
            ),
        })
    profile_rows = []
    for provider, profile in sorted(profiles.items()):
        catalog_entry = _catalog_lookup(provider)
        profile_rows.append({
            "provider": provider,
            "declared": _declared_fields(profile),
            "source": {key: "provider" if profile.get(key) not in (None, "") else ("catalog" if key != "provider_key_ref" and catalog_entry else "default") for key in ("base_url", "kind", "provider_key_ref")},
            "base_url": profile.get("base_url") or _catalog_default_base_url(provider) or DEFAULT_BASE_URLS.get(provider) or "",
            "provider_key_ref": profile.get("provider_key_ref") or "",
            "has_key_ref": bool(profile.get("provider_key_ref")),
            # ``kind`` lets the dashboard mark a custom-created profile
            # as Anthropic-shaped vs OpenAI-shaped without re-deriving
            # it from the catalog every render.
            "kind": str(
                profile.get("kind")
                or (catalog_entry.api_mode if catalog_entry else "chat_completions")
            ),
            "name": profile.get("name") or (catalog_entry.name if catalog_entry else provider),
            "provider_native_web_search": normalise_provider_native_web_search(
                profile.get("provider_native_web_search")
            ),
        })
    for profile in profile_rows:
        profile["effective"] = {key: value for key, value in profile.items() if key not in {"declared", "source"}}
    return {
        "ok": True,
        "revision": config_revision(config),
        "default_tier": _get_cfg(config, "llm.default_tier", "medium"),
        "intent_tier": _get_cfg(config, "llm.intent_tier", "light"),
        "provider_profiles": profile_rows,
        "tiers": rows,
        "reasoning_levels": list(REASONING_EFFORT_LEVELS),
    }


_DECLARED_MODEL_FIELDS = (
    "provider", "model", "models", "base_url", "provider_key_ref",
    "provider_key_env", "kind", "name", "reasoning_effort", "context_window",
    "provider_native_web_search",
)


def _declared_fields(raw: dict[str, Any]) -> dict[str, Any]:
    fields = {key: raw[key] for key in _DECLARED_MODEL_FIELDS if key in raw}
    if "provider_key_ref" in fields:
        fields["provider_key_ref"] = ", ".join(
            ref for ref in split_csv_values(fields["provider_key_ref"]) if ref.startswith("vault://")
        )
    return fields


def config_revision(config: Config) -> str:
    """Opaque revision of saved routing policy; never expose key material."""
    data = json.dumps(_get_cfg(config, "llm", {}) or {}, sort_keys=True, default=str)
    return hashlib.sha256(data.encode()).hexdigest()[:20]


def llm_config_set(
    config: Config,
    *,
    default_tier: str | None = None,
    intent_tier: str | None = None,
    providers: list[Any] | None = None,
    tiers: list[Any] | None = None,
    vault_passphrase: str | None = None,
    explicit_overrides: bool = False,
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
    yaml_profiles = llm.setdefault("providers", dict(current_profiles))
    if not isinstance(yaml_profiles, dict):
        yaml_profiles = {}
        llm["providers"] = yaml_profiles
    yaml_tiers = llm.setdefault("tiers", dict(current_tiers) if tiers is None or explicit_overrides else {})
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
            for key in (
                "base_url", "provider_key_ref", "kind", "name",
                "provider_native_web_search",
            ):
                if key in patch:
                    merged_profile[key] = patch[key]
                elif key in raw and raw[key] in (None, ""):
                    merged_profile.pop(key, None)
            yaml_profiles[provider] = merged_profile

    if tiers is not None:
        seen: set[str] = set()
        for raw in tiers:
            tier, patch = _normalise_model_tier_row(
                config, raw, vault_passphrase=vault_passphrase,
            )
            if explicit_overrides:
                # The settings editor sends declarations, not resolved values.
                for route, raw_route in zip(patch.get("routes", []), raw.get("routes", [])):
                    if "context_window" not in raw_route:
                        route.pop("context_window", None)
                    if raw_route.get("provider_key_env"):
                        route["provider_key_env"] = str(raw_route["provider_key_env"])
                for key in ("context_window", "reasoning_effort", "provider_native_web_search"):
                    if key in raw:
                        if key == "context_window":
                            patch[key] = _normalise_model_context_window(raw[key], label=tier)
                    else:
                        patch.pop(key, None)
            seen.add(tier)
            merged = dict(current_tiers.get(tier) or {})
            merged.update(dict(yaml_tiers.get(tier) or {}))
            for key, value in _tier_policy_defaults(tier).items():
                merged.setdefault(key, value)
            for key in (
                "provider", "model", "base_url", "provider_key_ref",
                "reasoning_effort", "context_window", "routes",
            ):
                if key in patch:
                    merged[key] = patch[key]
                else:
                    merged.pop(key, None)
            if "routes" in patch:
                first = patch["routes"][0] if patch["routes"] else {}
                for key in (
                    "provider", "model", "base_url", "provider_key_ref", "kind",
                    "context_window", "provider_key_env",
                ):
                    if explicit_overrides and key == "context_window":
                        continue
                    if key in first:
                        merged[key] = first[key]
                    elif key in {"base_url", "provider_key_ref", "provider_key_env", "kind"}:
                        merged.pop(key, None)
            if "provider_native_web_search" in patch:
                merged["provider_native_web_search"] = patch[
                    "provider_native_web_search"
                ]
            elif explicit_overrides:
                merged.pop("provider_native_web_search", None)
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
    effective = effective_tiers(config)
    tiers: dict[str, dict[str, Any]] = {}
    for tier_name, tier_cfg in effective.items():
        routes = configured_routes(tier_cfg)
        if len(routes) <= 1:
            tiers[tier_name] = dict(routes[0] if routes else tier_cfg)
            continue
        for index, route_cfg in enumerate(routes, start=1):
            tiers[f"{tier_name}:route:{index}"] = dict(route_cfg)
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
    api_mode: str | None = None,
    save_connection: bool = True,
) -> dict[str, Any]:
    """Call one provider's live model-list endpoint for onboarding.

    Explicit ``api_mode`` and saved API-format overrides also control
    discovery for builtin provider names used with compatibility endpoints.
    Allowed values: ``"chat_completions"`` (OpenAI-compat) or
    ``"anthropic_messages"`` (Anthropic-compat). The default falls
    back to ``chat_completions`` because the vast majority of
    self-hosted servers (Ollama, vLLM, llama.cpp, Together, Fireworks,
    Groq, ...) speak OpenAI-compat.
    """

    provider_id = (provider or "").strip().lower()
    if not provider_id or not _PROVIDER_RE.fullmatch(provider_id):
        raise ValueError("valid provider is required")
    profile = _provider_profiles(config).get(provider_id) or {}
    target_base_url = str(base_url or profile.get("base_url") or _catalog_default_base_url(provider_id) or DEFAULT_BASE_URLS.get(provider_id) or "").strip()
    if target_base_url and not (
        target_base_url.startswith("http://") or target_base_url.startswith("https://")
    ):
        raise ValueError("base_url must start with http:// or https://")
    if not target_base_url:
        return {"ok": False, "error": "provider_url_required", "provider": provider_id,
                "detail": "set an API endpoint before discovering this provider's models"}

    key_ref = str(provider_key_ref or profile.get("provider_key_ref") or "").strip()
    one_time_key = str(provider_key or "").strip()
    if key_ref and not key_ref.startswith("vault://"):
        one_time_key = one_time_key or key_ref
        key_ref = ""
    api_key = one_time_key
    if not api_key and key_ref:
        api_key = _resolve_llm_key(
            config, key_ref, vault_passphrase=vault_passphrase,
        )
    # Ollama is the canonical no-key local server; for anything else
    # (including a custom OpenAI-compat target) we still need a token.
    is_local = provider_id == "ollama" or _looks_like_local_url(target_base_url)
    if not is_local and not api_key:
        return {
            "ok": False,
            "error": "provider_key_required",
            "detail": "store or paste a provider API key before discovering models",
        }

    providers = builtin_providers()
    adapter = providers.get(provider_id)
    mode = (api_mode or profile.get("kind") or "").strip().lower()
    if mode == "anthropic_messages":
        adapter = providers.get("anthropic-compat") or providers.get("anthropic")
    elif mode == "chat_completions":
        adapter = providers.get("compat")
    if adapter is None or not hasattr(adapter, "list_models"):
        # Custom provider id — pick a compat adapter from ``api_mode``.
        # The "compat" entry is always an OpenAICompatAdapter; the
        # anthropic compat is the same instance used for Anthropic
        # itself. Both speak `list_models` against the supplied URL.
        mode = (api_mode or profile.get("kind") or "").strip().lower() or "chat_completions"
        if mode == "anthropic_messages":
            adapter = providers.get("anthropic-compat") or providers.get("anthropic")
        else:
            adapter = providers.get("compat")
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
            "detail": f"{type(exc).__name__}: model discovery failed; connection was not saved",
            "connection_saved": False,
            "provider": provider_id,
            "base_url": target_base_url,
            "provider_key_ref": key_ref,
        }

    rows = [
        row for row in (_model_info_to_dict(m, provider_id) for m in models)
        if row.get("id")
    ]
    if not save_connection:
        return {"ok": True, "provider": provider_id, "base_url": target_base_url,
                "models": rows, "count": len(rows), "connection_saved": False}
    saved = llm_config_set(
        config,
        providers=[{
            "provider": provider_id,
            "base_url": target_base_url,
            "provider_key_ref": key_ref,
            **({"provider_key": one_time_key} if one_time_key else {}),
            **({"kind": api_mode} if api_mode else {}),
        }],
        vault_passphrase=vault_passphrase,
    )
    saved_profile = next(row for row in saved["provider_profiles"] if row["provider"] == provider_id)
    return {
        "ok": True,
        "provider": provider_id,
        "base_url": target_base_url,
        "provider_key_ref": saved_profile["provider_key_ref"],
        "provider_profile": saved_profile,
        "connection_saved": True,
        "revision": saved["revision"],
        "models": rows,
        "count": len(rows),
    }


def models_provider_refresh(
    config: Config, *, provider: str, vault_passphrase: str | None = None,
) -> dict[str, Any]:
    """Replace one provider's model cache using its saved connection only."""
    provider_id = str(provider or "").strip().lower()
    if not provider_id or not _PROVIDER_RE.fullmatch(provider_id):
        raise ValueError("valid provider is required")
    profile = _provider_profiles(config).get(provider_id)
    routes = [{"provider": provider_id, **profile}] if profile is not None else [
        route for tier in effective_tiers(config).values()
        for route in configured_routes(tier) if route.get("provider") == provider_id
    ]
    if not routes:
        return {"ok": False, "error": "provider_not_configured", "provider": provider_id}
    from .gateway import LLMGateway
    gateway = LLMGateway(config)
    result: dict[str, Any] = {"ok": False, "error": "provider_key_required", "provider": provider_id}
    for route in routes:
        keys = gateway.router._resolve_route_api_keys(route) or [""]
        if vault_passphrase:
            resolved = [_resolve_llm_key(config, ref, vault_passphrase=vault_passphrase)
                        for ref in split_csv_values(route.get("provider_key_ref"))]
            keys = split_csv_values([key for key in resolved if key] + keys) or [""]
        for key in keys:
            result = models_discover(
                config, provider=provider_id, base_url=route.get("base_url"),
                provider_key=key or None, api_mode=route.get("kind"),
                vault_passphrase=vault_passphrase, save_connection=False,
            )
            if not result.get("ok"):
                continue
            doc = ModelCatalog(workspace=config.paths.root).import_models(
                provider=provider_id, models=result["models"],
                base_url=result.get("base_url"), merge=False,
            )
            return {**result, "models": doc["providers"][provider_id],
                    "count": len(doc["providers"][provider_id]), "updated_at": doc.get("updated_at")}
    return result


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
    "models_provider_refresh",
    "models_import",
    "validate_tier_assignment",
    "provider_routing_get",
    "provider_routing_set",
]
