"""Helpers for expanding configured LLM models and keys into routes."""

from __future__ import annotations

from typing import Any

RESOLVED_PROVIDER_KEY = "_resolved_provider_key"
ROUTE_INDEX = "_route_index"


def split_csv_values(value: Any) -> list[str]:
    """Return non-empty values from strings/lists, splitting strings on commas."""

    raw_items: list[Any]
    if value is None:
        raw_items = []
    elif isinstance(value, str):
        raw_items = value.split(",")
    elif isinstance(value, (list, tuple, set)):
        raw_items = []
        for item in value:
            raw_items.extend(split_csv_values(item))
    else:
        raw_items = [value]

    out: list[str] = []
    seen: set[str] = set()
    for item in raw_items:
        text = str(item or "").strip()
        if not text or text in seen:
            continue
        seen.add(text)
        out.append(text)
    return out


def configured_models(
    cfg: dict[str, Any],
    *,
    model_override: str | None = None,
) -> list[str]:
    """Return the ordered model candidates for a tier/profile config."""

    if model_override is not None and str(model_override).strip():
        models = split_csv_values(model_override)
        return models or [str(model_override).strip()]
    models = split_csv_values(cfg.get("models"))
    if models:
        return models
    models = split_csv_values(cfg.get("model"))
    return models or [""]


def _route_overrides(raw: Any) -> list[dict[str, Any]]:
    if raw is None:
        return []
    items = raw if isinstance(raw, list) else [raw]
    out: list[dict[str, Any]] = []
    for item in items:
        if isinstance(item, dict):
            route = dict(item)
            if route:
                out.append(route)
    return out


def configured_routes(cfg: dict[str, Any]) -> list[dict[str, Any]]:
    """Return base route configs for a tier.

    ``llm.tiers.<tier>.routes`` is the rich multi-provider shape. When it is
    missing, the legacy tier-level provider/model/key fields are treated as a
    single route so older workspaces keep working.
    """

    routes = _route_overrides(cfg.get("routes"))
    if not routes:
        return [dict(cfg)]

    inherited_policy_keys = (
        "max_tokens",
        "temperature",
        "timeout_s",
        "timeout",
        "http_max_attempts",
        "max_attempts",
        "prices",
        "reasoning_effort",
        "reasoning_summary",
        "provider_native_web_search",
        "allowed_tasks",
        "allowed_classes",
    )
    out: list[dict[str, Any]] = []
    for index, route in enumerate(routes):
        merged = {
            key: cfg[key]
            for key in inherited_policy_keys
            if key in cfg and key not in route
        }
        merged.update(route)
        merged[ROUTE_INDEX] = index
        out.append(merged)
    return out or [dict(cfg)]


def first_configured_route(cfg: dict[str, Any]) -> dict[str, Any]:
    return configured_routes(cfg)[0]


def expand_route_cfgs(
    cfg: dict[str, Any],
    *,
    keys: list[str],
    model_override: str | None = None,
) -> list[dict[str, Any]]:
    """Return configs for every model/key route candidate.

    Ordering intentionally tries all keys for a model before moving to the
    next model. That keeps same-model quota/key failover cheaper than changing
    model behavior.
    """

    models = configured_models(cfg, model_override=model_override)
    key_candidates = keys or [""]
    out: list[dict[str, Any]] = []
    for model in models:
        for key in key_candidates:
            route = dict(cfg)
            route["model"] = model
            if key:
                route[RESOLVED_PROVIDER_KEY] = key
            else:
                route.pop(RESOLVED_PROVIDER_KEY, None)
            out.append(route)
    return out or [dict(cfg)]


def expand_tier_route_cfgs(
    cfg: dict[str, Any],
    *,
    keys_for_route: Any,
    model_override: str | None = None,
) -> list[dict[str, Any]]:
    """Return every configured route's model/key candidates in order."""

    out: list[dict[str, Any]] = []
    for route in configured_routes(cfg):
        out.extend(
            expand_route_cfgs(
                route,
                keys=keys_for_route(route),
                model_override=model_override,
            )
        )
    return out or [dict(cfg)]
