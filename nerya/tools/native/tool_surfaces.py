"""Progressive disclosure for native tools.

The native tool registry ships ~90 first-class tools. Rendering all of
them on every turn bloats the provider request (every tool schema is
re-sent each iteration), which slows simple turns ("hi") and confuses
weaker providers. MCP tools already solve this with lazy gating
(:mod:`nerya.mcp.lazy`): tools are registered but hidden from the
prompt until their namespace is described.

This module brings the same idea to *native* tools, reusing the exact
same machinery (``ToolDescriptor.lazy`` + ``LazyMcpState.is_visible``
+ the kernel's per-session described-namespace cache). The model keeps
a small always-on **core** (file/search/shell/skill-discovery/todo/
memory-recall plus the two cross-cutting reads ``web_search`` and
``market_data``). Specialized families become **lazy** and are revealed
for the rest of the session the moment the model opens the matching
skill with ``skill_view``.

How it fits together:

* :data:`NATIVE_SURFACES` maps a *surface* name to the native tool
  names that belong to it. Any tool not listed here stays in the core
  (always visible).
* :func:`apply_native_lazy_surfaces` flips those tools to ``lazy=True``
  and tags them ``surface:<name>`` so ``LazyMcpState.is_visible`` can
  decide visibility. It is idempotent (safe to re-run per turn) and a
  no-op for tools that are not registered in the current config.
* :data:`SKILL_SURFACES` maps a skill id to the surfaces it unlocks.
  The ``skill_view`` wrapper calls :func:`reveal_surfaces_for_skill`
  after a successful view, marking ``native:<surface>`` described on
  the registry's lazy state.
* The described set is persisted across turns of one session by the
  kernel's existing ``pull_session_cache_into`` /
  ``push_state_into_session_cache`` plumbing, so a skill viewed once
  keeps its tools visible for the rest of the conversation.

Hidden tools remain fully dispatchable — visibility only governs what
is advertised in the prompt, so nothing breaks if the model calls a
gated tool by name without viewing its skill first.
"""

from __future__ import annotations

import dataclasses
from typing import Any, Iterable, Optional

from ..registry import ToolRegistry
from ..types import ToolDescriptor

#: Tag prefix used to mark which surface a lazy native tool belongs to.
SURFACE_TAG_PREFIX = "surface:"

#: Prefix for the described-set key stored in ``LazyMcpState`` so native
#: surfaces never collide with MCP server ids in the same set.
NATIVE_DESCRIBED_PREFIX = "native:"


# ---------------------------------------------------------------------------
# Surface taxonomy
# ---------------------------------------------------------------------------
#
# surface name -> native tool names that belong to it. Tools NOT listed in
# any surface stay in the always-on core. Keep families coherent with the
# skill that documents them so ``skill_view`` reveals exactly what the
# playbook talks about.

NATIVE_SURFACES: dict[str, tuple[str, ...]] = {
    # Strategy authoring + self-evolution tuning (the strategy_author skill).
    "strategy": (
        "strategy_draft_proposal",
        "strategy_submit_proposal",
        "strategy_validate",
        "strategy_delete_proposal",
        "strategy_backtest",
        "strategy_promote",
        "strategy_run_tick",
        "strategy_kill_switch",
        "strategy_run_history",
        "strategy_tuning_generate",
        "strategy_tuning_run",
        "strategy_tuning_status",
        "strategy_tuning_snapshot",
    ),
    # Portfolio / risk / order placement + account control (the trading skill).
    "trading": (
        "portfolio_summary",
        "portfolio_positions",
        "portfolio_pnl",
        "virtual_ledger",
        "risk_check",
        "strategy_list",
        "strategy_view",
        "strategy_history",
        "kill_switch_set",
        "trade_intent_submit",
        "account_list",
        "account_upsert",
        "wallet_install",
    ),
    # Provider / connector / data-source discovery + bounded data reads
    # (markets + market_data_routing skills). ``market_data`` itself stays
    # core because quick price/candle reads are cross-cutting.
    "markets": (
        "connector_list",
        "connector_view",
        "data_api",
        "data_source_status",
        "data_source_sync_now",
    ),
    # Subagents / AgentTeam / persona roles (the team skill).
    "team": (
        "subagent_list",
        "subagent_run",
        "subagent_run_async",
        "team_run",
        "role_list",
        "role_get",
        "role_save",
        "role_delete",
    ),
    # Background task scheduling (the tasks skill).
    "tasks": (
        "task_create",
        "task_list",
        "task_get",
        "task_output",
        "task_stop",
        "task_update",
        "task_summary",
    ),
    # Tiered LLM helper calls (the llm skill).
    "llm": (
        "llm_complete",
        "llm_classify",
        "llm_extract_json",
        "llm_compress",
    ),
    # Heavier web fetch/scrape (the research skill). ``web_search`` stays
    # core because a single ranked search is the common cheap entry point.
    "research": (
        "web_fetch",
        "web_search_fetch",
    ),
    # Self-improvement proposals (the evolve skill).
    "evolve": (
        "evolve_reflect",
        "evolve_proposals",
        "evolve_skill_proposal",
        "evolve_core_config_patch",
        "evolve_provider_proposal",
        "evolve_post_apply_observation",
    ),
    # Long-term memory writes + journal search (the memory skill).
    # ``memory_recall`` stays core so recall is always one call away.
    "memory": (
        "memory_remember",
        "journal_search",
    ),
    # Messaging gateway diagnostics (the notify skill).
    "notify": (
        "gateway_diagnose",
    ),
}


#: skill id -> surfaces it reveals when viewed via ``skill_view``.
#: Multiple skills may reveal the same surface.
SKILL_SURFACES: dict[str, tuple[str, ...]] = {
    "strategy_author": ("strategy", "trading", "markets"),
    "quant-strategy-loop": ("strategy", "trading", "markets"),
    "trading": ("trading", "markets"),
    "markets": ("markets",),
    "market_data_routing": ("markets",),
    "agents": ("team",),  # compatibility alias
    "team": ("team",),
    "tasks": ("tasks",),
    "llm": ("llm",),
    "research": ("research",),
    "evolve": ("evolve",),
    "memory": ("memory",),
    "notify": ("notify",),
    "news_social": ("research",),
    # Expert-lens hubs: a committee of two or more lenses dispatches one
    # subagent lane per expert with team_run, so viewing the hub (or a
    # lens sub-skill) must reveal the team surface + heavier research.
    "expert_investors": ("team", "research"),
    "finance-creators": ("team", "research"),
}


def surfaces_for_skill(skill_id: str) -> tuple[str, ...]:
    """Return the surfaces a skill reveals when viewed.

    Namespaced sub-skills (``expert_investors.buffett``) inherit their
    hub's surfaces so viewing a single lens still unlocks the same
    toolset the hub promises.
    """

    sid = (skill_id or "").strip()
    direct = SKILL_SURFACES.get(sid)
    if direct is not None:
        return direct
    if "." in sid:
        return SKILL_SURFACES.get(sid.split(".", 1)[0], ())
    return ()


# Reverse index: tool name -> surface. Built once at import.
_SURFACE_BY_TOOL: dict[str, str] = {
    tool: surface
    for surface, tools in NATIVE_SURFACES.items()
    for tool in tools
}


def core_tool_names(all_tool_names: Iterable[str]) -> list[str]:
    """Return the subset of ``all_tool_names`` that stays always-on."""

    gated = set(_SURFACE_BY_TOOL)
    return sorted(name for name in all_tool_names if name not in gated)


def native_surface_of(descriptor: ToolDescriptor) -> Optional[str]:
    """Return the surface a native lazy tool belongs to, or ``None``.

    Reads the ``surface:<name>`` tag written by
    :func:`apply_native_lazy_surfaces`. Used by
    ``LazyMcpState.is_visible`` to decide native-tool visibility without
    importing this module.
    """

    if descriptor.namespace != "native":
        return None
    for tag in descriptor.tags:
        if tag.startswith(SURFACE_TAG_PREFIX):
            return tag[len(SURFACE_TAG_PREFIX):]
    return None


def described_key(surface: str) -> str:
    """Return the ``LazyMcpState.described_namespaces`` key for a surface."""

    return f"{NATIVE_DESCRIBED_PREFIX}{surface}"


# ---------------------------------------------------------------------------
# Applying the gate
# ---------------------------------------------------------------------------


def apply_native_lazy_surfaces(registry: ToolRegistry) -> int:
    """Flip every gated native tool to ``lazy=True`` + a surface tag.

    Idempotent: re-running re-applies cleanly (the surface tag is added
    only once). Tools that are not registered in the current config are
    skipped. Also ensures a :class:`LazyMcpState` is attached to the
    registry so ``_render_tools`` actually consults visibility even when
    no MCP connectors are configured.

    Returns the number of tools that were (re-)marked lazy.
    """

    count = 0
    for tool_name, surface in _SURFACE_BY_TOOL.items():
        descriptor = registry.find(tool_name)
        if descriptor is None:
            continue
        surface_tag = f"{SURFACE_TAG_PREFIX}{surface}"
        already = descriptor.lazy and surface_tag in descriptor.tags
        if already:
            count += 1
            continue
        tags = tuple(descriptor.tags)
        if surface_tag not in tags:
            tags = tags + (surface_tag,)
        new_descriptor = dataclasses.replace(descriptor, lazy=True, tags=tags)
        registry.register(new_descriptor, replace=True)
        count += 1

    _ensure_lazy_state_attached(registry)
    return count


def _ensure_lazy_state_attached(registry: ToolRegistry) -> Any:
    """Attach a ``LazyMcpState`` if the registry has none.

    Imported lazily to avoid a hard import cycle
    (``mcp.lazy`` imports ``tools.registry``). The same state object
    transparently carries both MCP namespaces and native surfaces.
    """

    from ...mcp.lazy import LazyMcpState, attach_lazy_state

    existing = getattr(registry, "lazy_mcp_state", None)
    if isinstance(existing, LazyMcpState):
        return existing
    state = LazyMcpState()
    return attach_lazy_state(registry, state)


def reveal_surfaces_for_tools(
    registry: ToolRegistry,
    tool_names: Iterable[str],
) -> list[str]:
    """Mark the surfaces owning the given native tools as described.

    Used by the agent loop when a caller declares an explicit
    ``required_artifacts`` contract: a contract tool that lives on a
    lazily gated surface (e.g. ``team_run`` on the ``team`` surface)
    must be advertised from the very first iteration, otherwise the
    contract-order enforcement would skip it as "not available" and
    force a later always-on artifact tool (such as ``write_file``)
    first. Returns the surface names newly revealed.
    """

    state = getattr(registry, "lazy_mcp_state", None)
    mark = getattr(state, "mark_described", None)
    if not callable(mark):
        return []
    newly: list[str] = []
    for name in tool_names:
        surface = _SURFACE_BY_TOOL.get(str(name or "").strip())
        if not surface:
            continue
        if mark(described_key(surface)) and surface not in newly:
            newly.append(surface)
    return newly


def reveal_surfaces_for_skill(registry: ToolRegistry, skill_id: str) -> list[str]:
    """Mark a skill's surfaces described on the registry's lazy state.

    Called by the ``skill_view`` wrapper after a successful view so the
    skill's specialized tools appear in the next iteration's prompt and
    stay visible for the rest of the session. Returns the surface names
    that were newly revealed (empty when nothing changed or no lazy
    state is attached).
    """

    surfaces = surfaces_for_skill(skill_id)
    if not surfaces:
        return []
    state = getattr(registry, "lazy_mcp_state", None)
    if state is None:
        return []
    mark = getattr(state, "mark_described", None)
    if not callable(mark):
        return []
    newly: list[str] = []
    for surface in surfaces:
        if mark(described_key(surface)):
            newly.append(surface)
    return newly


def revealed_tool_names(registry: ToolRegistry, surfaces: Iterable[str]) -> list[str]:
    """Return the registered tool names belonging to ``surfaces``."""

    wanted = set(surfaces)
    out: list[str] = []
    for surface in wanted:
        for tool in NATIVE_SURFACES.get(surface, ()):
            if registry.find(tool) is not None:
                out.append(tool)
    return sorted(out)


__all__ = [
    "NATIVE_DESCRIBED_PREFIX",
    "NATIVE_SURFACES",
    "SKILL_SURFACES",
    "SURFACE_TAG_PREFIX",
    "apply_native_lazy_surfaces",
    "core_tool_names",
    "described_key",
    "native_surface_of",
    "reveal_surfaces_for_skill",
    "revealed_tool_names",
    "surfaces_for_skill",
]
