"""SubAgents — isolated LLM runs with their own context window and skills."""

from .dispatcher import (
    SUBAGENT_SKILL_DENYLIST,
    SubAgentDispatcher,
    SubAgentResult,
)
from .registry import SubAgentSpec, load_registry
from .runtime import SubAgentRuntime
from .strategy_registry import StrategySubAgentRegistry, resolve_spec

__all__ = [
    "SubAgentSpec", "load_registry",
    "SubAgentRuntime", "SubAgentDispatcher",
    "SubAgentResult", "SUBAGENT_SKILL_DENYLIST",
    "StrategySubAgentRegistry", "resolve_spec",
]
