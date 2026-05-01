"""SQLite-backed indexes for dedupe, cooldown and proposal state.

The authoritative record stays in the workspace jsonl/yaml files; SQLite
is a rebuildable index."""

from .sqlite import connect
from .repositories import (
    ApprovalRepository,
    AgentSessionRepository,
    CooldownRepository,
    DedupeRepository,
    LLMUsageRepository,
    ProposalRepository,
)

__all__ = [
    "connect",
    "ApprovalRepository",
    "AgentSessionRepository",
    "CooldownRepository",
    "DedupeRepository",
    "LLMUsageRepository",
    "ProposalRepository",
]
