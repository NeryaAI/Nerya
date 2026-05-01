"""Tier selection + allowed task enforcement.

Supports two matching modes:

1. ``allowed_tasks`` — exact string membership. Preserved for backwards
   compatibility and for tiers that want strict routing.
2. ``allowed_classes`` — capability-family membership. Tasks are
   normalised to a canonical class (see :mod:`nerya.llm.task_classes`)
   and matched against the tier's advertised families.

A tier matches if either mechanism succeeds.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from ..core.errors import LLMTaskNotAllowed, LLMTierDenied
from .task_classes import normalise_task_class


@dataclass
class TierPolicy:
    tiers: dict[str, dict[str, Any]]
    default_tier: str
    extra_class_map: dict[str, str] | None = None

    def _tier_accepts(self, cfg: dict[str, Any], task: str) -> bool:
        if task in (cfg.get("allowed_tasks") or []):
            return True
        cls = normalise_task_class(task, extra_map=self.extra_class_map)
        if cls and cls in (cfg.get("allowed_classes") or []):
            return True
        return False

    def resolve(self, task: str, *, requested_tier: str | None,
                caller_allowed_tiers: list[str] | None) -> str:
        candidates = [name for name, cfg in self.tiers.items()
                      if self._tier_accepts(cfg, task)]
        if not candidates:
            raise LLMTaskNotAllowed(
                f"no tier advertises task '{task}' "
                f"(add it to allowed_tasks or allowed_classes)"
            )
        if requested_tier and requested_tier in candidates:
            tier = requested_tier
        else:
            order = ["light", "medium", "high"]
            candidates.sort(key=lambda c: order.index(c) if c in order else 99)
            tier = candidates[0]
        if caller_allowed_tiers is not None and tier not in caller_allowed_tiers:
            raise LLMTierDenied(f"caller not allowed to use tier '{tier}'")
        return tier
