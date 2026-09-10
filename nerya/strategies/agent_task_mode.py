"""Explicit strategy task selection; permissions and names are not workflow requests."""
from __future__ import annotations

from .package import StrategyManifest

AGENT_TASK_TARGET = "skill:strategy.agent_task"


def agent_task_requested(manifest: StrategyManifest) -> bool:
    request = manifest.extras.get("agent_task")
    if request is None:
        return False
    if not isinstance(request, dict) or not isinstance(request.get("enabled"), bool):
        raise ValueError("agent_task must contain an explicit boolean enabled field")
    return request["enabled"]


def agent_team_roles(manifest: StrategyManifest) -> list[str]:
    """Preserve the declared assignment; unspecified roles stay unspecified."""
    return list(dict.fromkeys(str(role).strip() for role in manifest.subagents or () if str(role).strip()))
