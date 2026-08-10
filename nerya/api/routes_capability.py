"""Runtime capability matrix endpoint.

("hardcoded product copy shapes operator behavior") and the
P1 "runtime capability/support matrix" item both call out that UI / docs
/ gateway help should render *current* capabilities rather than baked-in
strings. This module provides one place a dashboard, docs page, gateway
help renderer, or capability-drift test can hit to discover:

- What skills + actions are currently loaded (from manifests).
- Which gateway commands the registry exposes today.
- Which gateway platforms exist and at what support level.
- LLM tier / planner route topology pulled from config.
- Workspace-level toggles (live trading, paper trading, mock fallback).

Everything is a read-only view; nothing mutates state.
"""

from __future__ import annotations

from typing import Any

from ..agent import operator_presets as _operator_presets
from ..skills.listing import build_skill_listing
from ..agent import recipes as _recipes
from ..agent import route_manifests as _route_manifests
from ..messaging.platforms import list_platforms
from . import route_scopes as _route_scopes
from .gateway_commands import DEFAULT_REGISTRY


def _skill_listing(client) -> list[dict[str, Any]]:
    """agent-skill style listing of installed skills.

    Returns ``[{name, description}, ...]``. The dashboard
    renders this as a discovery list; tool-level capability lives on
    :class:`~nerya.tools.types.ToolDescriptor`, not on skills.
    """

    try:
        return build_skill_listing(getattr(client, "skills", None))
    except Exception:  # pragma: no cover - defensive
        return []


def _skill_summaries(client) -> list[dict[str, Any]]:
    """Per-skill metadata for the dashboard ``Skills`` panel.

    Mirrors the official Agent Skills frontmatter contract: ``name``,
    ``description`` and ``version``. No action arrays — anything
    that can be invoked is a tool, surfaced separately by the
    ``ToolRegistry``-backed tools endpoint.
    """

    skills = getattr(client, "skills", None)
    if skills is None:
        return []
    try:
        entries = list(skills.registry.list())
    except Exception:
        return []

    out: list[dict[str, Any]] = []
    for entry in entries:
        manifest = getattr(entry, "manifest", None)
        if manifest is None:
            continue
        row: dict[str, Any] = {
            "name": getattr(manifest, "id", ""),
            "description": (getattr(manifest, "description", "") or "").strip(),
            "version": getattr(manifest, "version", "") or "",
        }
        out.append(row)
    out.sort(key=lambda r: r.get("name") or "")
    return out


def _gateway_section() -> dict[str, Any]:
    return {
        "commands": DEFAULT_REGISTRY.menu(),
        "platforms": list_platforms(),
    }


def _runtime_section(client) -> dict[str, Any]:
    cfg = client.config
    return {
        "live_trading_enabled": cfg.live_trading_enabled(),
        "paper_trading_enabled": cfg.paper_trading_enabled(),
        "kill_switch": cfg.kill_switch(),
        "mock_mode": bool(cfg.get("runtime.mock_mode", False)),
        "default_tier": cfg.get("llm.default_tier", "medium"),
        "tiers": list((cfg.get("llm.tiers") or {}).keys()),
    }


def _planner_section(client) -> dict[str, Any]:
    cfg = client.config
    paths = getattr(cfg, "paths", None)
    # When a manifest is pinned the resolver wins, even when the selected
    # manifest deliberately contains no routes. Only manifest-less legacy
    # workspaces fall back to the freeform ``agent.planner.routes`` table.
    try:
        manifest_routes, manifest_fallback, manifest_id = (
            _route_manifests.resolve_routes(cfg, paths=paths)
        )
    except (KeyError, ValueError):
        manifest_routes, manifest_fallback, manifest_id = {}, "generic", None
    if manifest_id:
        routes = manifest_routes
        fallback = manifest_fallback
    else:
        routes = cfg.get("agent.planner.routes", {}) or {}
        fallback = cfg.get("agent.planner.fallback", "")
    summary: list[dict[str, Any]] = []
    if isinstance(routes, dict):
        for name, spec in routes.items():
            if not isinstance(spec, dict):
                continue
            summary.append({
                "name": name,
                "match": list(spec.get("match") or []),
                "skills": list(spec.get("skills") or []),
                "subagents": list(spec.get("subagents") or []),
                "tier": spec.get("tier") or "",
            })
    try:
        manifests = _route_manifests.manifest_summary(paths=paths)
    except Exception:
        manifests = [m.as_dict() for m in _route_manifests.builtin_manifests()]
    return {
        "routes": summary,
        "fallback": fallback,
        "active_manifest": manifest_id,
        "manifests": manifests,
    }


def _recipes_section(client) -> dict[str, Any]:
    try:
        return _recipes.recipe_summary(client)
    except Exception:  # pragma: no cover - defensive
        return {"available": [], "all": []}


def _dashboard_extensions(client) -> list[dict[str, Any]]:
    """surface manifest-declared dashboard panels.

    Skills can declare ``dashboard:`` entries in ``skill.yml``. Today
    we just expose the descriptors so the dashboard can render them
    side-by-side with bundled pages; tomorrow a generic forms renderer
    can consume the ``schema`` field directly.
    """

    skills = getattr(client, "skills", None)
    if skills is None:
        return []
    out: list[dict[str, Any]] = []
    try:
        entries = list(skills.registry.list())
    except Exception:
        return []
    for entry in entries:
        manifest = getattr(entry, "manifest", None)
        if manifest is None:
            continue
        for ext in getattr(manifest, "dashboard", []) or []:
            try:
                row = ext.as_dict()
            except Exception:  # pragma: no cover
                continue
            row["skill_version"] = getattr(manifest, "version", "")
            out.append(row)
    out.sort(key=lambda r: (r.get("slot", ""), r.get("skill_id", ""), r.get("title", "")))
    return out


def _operator_presets_section(client) -> dict[str, Any]:
    """surface the operator preset catalog and the
    currently active preset id so the dashboard / capability-drift
    tests can show ``read_only`` / ``dev`` / ``deploy`` / ``live_trading``
    side-by-side."""

    cfg = getattr(client, "config", None)
    active_id = None
    extra_allow: list[str] = []
    extra_deny: list[str] = []
    if cfg is not None:
        try:
            active_id = cfg.get(
                "agent.operator.preset", _operator_presets.DEFAULT_PRESET_ID
            )
            extra_allow = list(
                cfg.get("agent.operator.extra_allow_actions", []) or []
            )
            extra_deny = list(
                cfg.get("agent.operator.extra_deny_actions", []) or []
            )
        except Exception:  # pragma: no cover - defensive
            active_id = _operator_presets.DEFAULT_PRESET_ID
    summary = _operator_presets.describe_presets(active_id)
    summary["extra_allow_actions"] = extra_allow
    summary["extra_deny_actions"] = extra_deny
    return summary


def _mcp_dynamic_section(client) -> dict[str, Any]:
    """manifest-driven MCP tool surface summary.

    Builds the same registry the MCP server registers at boot time and
    returns its serialised view. Both ``tools`` (kept) and ``dropped``
    (filtered) entries are exposed so operators can see why an action
    was hidden from the MCP surface (preset deny, mutating-blocked,
    deny-list, unimplemented, ...).
    """

    try:
        from ..mcp.dynamic_tools import DynamicMCPRegistry, policy_from_config
    except Exception:  # pragma: no cover - defensive
        return {"tools": [], "dropped": [], "policy": {}, "total": 0}

    try:
        policy = policy_from_config(client.config)
        registry = DynamicMCPRegistry.build(client, policy=policy)
    except Exception as exc:  # pragma: no cover - defensive
        return {"tools": [], "dropped": [], "policy": {},
                "total": 0, "error": f"{type(exc).__name__}: {exc}"}
    return registry.asdict()


def _model_registry_section(client) -> dict[str, Any]:
    """surface per-model metadata (context window, costs,
    modalities, knowledge cutoff) so the dashboard can render the
    *exact* model behind each tier rather than only the provider name.
    """

    cfg = getattr(client, "config", None)
    if cfg is None:
        return {"tiers": {}, "models": [], "providers": [], "builtin_count": 0}
    try:
        from ..llm.model_registry import ModelRegistry

        tiers = cfg.get("llm.tiers") or {}
        registry = ModelRegistry(workspace=cfg.paths.root)
        return registry.summary(tiers=tiers)
    except Exception:  # pragma: no cover - defensive
        return {"tiers": {}, "models": [], "providers": [], "builtin_count": 0}


def _route_scopes_section() -> dict[str, Any]:
    """surface the route → minimum-scope matrix so dashboards
    and capability-drift tests can render and verify it."""

    return {
        "scopes": sorted(_route_scopes.ALL_SCOPES),
        "wildcard": _route_scopes.WILDCARD_SCOPE,
        "anonymous": sorted(_route_scopes.ANONYMOUS_PATHS),
        "rules": _route_scopes.describe_matrix(),
    }


def _capability_matrix(client, _payload):
    return {
        "ok": True,
        "runtime": _runtime_section(client),
        "skills": _skill_summaries(client),
        "skill_listing": _skill_listing(client),
        "gateway": _gateway_section(),
        "planner": _planner_section(client),
        "operator_presets": _operator_presets_section(client),
        "model_registry": _model_registry_section(client),
        "mcp_dynamic": _mcp_dynamic_section(client),
        "recipes": _recipes_section(client),
        "dashboard_extensions": _dashboard_extensions(client),
        "route_scopes": _route_scopes_section(),
    }


def _dashboard_extensions_endpoint(client, _payload):
    return {
        "ok": True,
        "extensions": _dashboard_extensions(client),
    }


def _recipes_endpoint(client, _payload):
    """Return the recipes whose capability requirements are met today.

    moves the dashboard chat empty-state suggestions
    out of TypeScript and into a manifest-driven endpoint so installed
    skills are the source of truth.
    """

    available = _recipes.available_recipes(client)
    return {"ok": True, "recipes": available, "count": len(available)}


def _operator_presets_endpoint(client, _payload):
    """standalone endpoint for the dashboard preset
    selector."""

    return {"ok": True, "operator_presets": _operator_presets_section(client)}


def _model_registry_endpoint(client, _payload):
    """standalone endpoint for the model-metadata UI."""

    return {"ok": True, "model_registry": _model_registry_section(client)}


def _mcp_dynamic_endpoint(client, _payload):
    """standalone endpoint that mirrors what the MCP server
    would expose given the current configuration."""

    return {"ok": True, "mcp_dynamic": _mcp_dynamic_section(client)}


def routes():
    return [
        ("GET", "/runtime/capability_matrix", _capability_matrix),
        ("POST", "/runtime/capability_matrix", _capability_matrix),
        ("GET", "/runtime/recipes", _recipes_endpoint),
        ("POST", "/runtime/recipes", _recipes_endpoint),
        ("GET", "/runtime/dashboard_extensions", _dashboard_extensions_endpoint),
        ("POST", "/runtime/dashboard_extensions", _dashboard_extensions_endpoint),
        ("GET", "/runtime/operator_presets", _operator_presets_endpoint),
        ("POST", "/runtime/operator_presets", _operator_presets_endpoint),
        ("GET", "/runtime/model_registry", _model_registry_endpoint),
        ("POST", "/runtime/model_registry", _model_registry_endpoint),
        ("GET", "/runtime/mcp_dynamic", _mcp_dynamic_endpoint),
        ("POST", "/runtime/mcp_dynamic", _mcp_dynamic_endpoint),
    ]
