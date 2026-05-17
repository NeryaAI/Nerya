"""Helpers for strategy packages that should run through AgentKernel tasks."""

from __future__ import annotations

from typing import Any

from .package import StrategyManifest


AGENT_TASK_TARGET = "skill:strategy.agent_task"

_AGENT_TASK_MODES = frozenset({
    "agent",
    "agent_task",
    "agent-team",
    "agent_team",
    "team",
    "team_run",
})

_DEFAULT_TEAM_ROLES = (
    "technical_analyst",
    "fundamentals_analyst",
    "macro_strategist",
    "news_interpreter",
    "risk_critic",
)


def agent_task_requested(manifest: StrategyManifest) -> bool:
    """Return True when a strategy trading schedule should target AgentKernel."""

    if "team_run" in set(manifest.agent_profile.allowed_tools or ()):
        return True
    explicit = _explicit_agent_task_flag(manifest.extras)
    if explicit is not None:
        return explicit
    return legacy_agent_team_strategy(manifest)


def legacy_agent_team_strategy(manifest: StrategyManifest) -> bool:
    """Detect older generated team strategies that used direct subagents."""

    if not manifest.subagents:
        return False
    text = " ".join([
        manifest.strategy_id,
        manifest.title,
        manifest.description,
    ]).lower()
    if "agent team" in text or "agent_team" in text or "agent-team" in text:
        return True
    return "_team_" in text or " team " in f" {text} "


def agent_team_roles(manifest: StrategyManifest) -> list[str]:
    """Return the role names AgentKernel should pass into ``team_run``."""

    roles: list[str] = []
    for raw in manifest.subagents or _DEFAULT_TEAM_ROLES:
        name = str(raw or "").strip()
        if name and name not in roles:
            roles.append(name)
    if _looks_like_equity_strategy(manifest) and not any(
        "fundamental" in role.lower() for role in roles
    ):
        insert_at = 1 if roles else 0
        roles.insert(insert_at, "fundamentals_analyst")
    return roles or list(_DEFAULT_TEAM_ROLES)


def _looks_like_equity_strategy(manifest: StrategyManifest) -> bool:
    text = " ".join([
        manifest.strategy_id,
        manifest.title,
        manifest.description,
        " ".join(manifest.markets),
    ]).lower()
    if "fundamental" in text or "earnings" in text or "valuation" in text:
        return True
    return any(str(m).lower().startswith("yahoo:") for m in manifest.markets)


def _explicit_agent_task_flag(extras: dict[str, Any]) -> bool | None:
    raw = (
        extras.get("agent_task")
        if "agent_task" in extras
        else extras.get("execution_mode")
        if "execution_mode" in extras
        else extras.get("runtime_mode")
        if "runtime_mode" in extras
        else extras.get("execution")
        if "execution" in extras
        else extras.get("runtime")
        if "runtime" in extras
        else None
    )
    if raw is None:
        for key in ("requires_agent_team_memo", "team_run_required"):
            if key in extras:
                return bool(extras.get(key))
        return None
    if isinstance(raw, bool):
        return raw
    if isinstance(raw, str):
        return raw.strip().lower() in _AGENT_TASK_MODES
    if isinstance(raw, dict):
        if raw.get("enabled") is False:
            return False
        if raw.get("agent_task") is True or raw.get("team_run") is True:
            return True
        mode = str(
            raw.get("mode")
            or raw.get("target")
            or raw.get("type")
            or ""
        ).strip().lower()
        if mode:
            return mode in _AGENT_TASK_MODES
        return bool(raw.get("enabled"))
    return None


__all__ = [
    "AGENT_TASK_TARGET",
    "agent_task_requested",
    "agent_team_roles",
    "legacy_agent_team_strategy",
]
