"""Factory helpers used by the cron-driven scheduled-session path.

The :class:`~nerya.triggers.scheduled_session.ScheduledSessionRunner`
needs an :class:`AgentKernel` for each tick, but the runtime-ownership
ADR forbids ``triggers`` from importing ``agent`` or ``skills``. This
module sits in :mod:`nerya.sdk` (which is allowed to bridge layers)
and exposes a default factory that callers can inject when they wire up
the cron loop.
"""

from __future__ import annotations

from typing import Any

from ..agent.kernel import AgentKernel
from ..core.config import Config
from ..skills.kernel import SkillKernel


def default_kernel_factory(config: Config) -> Any:
    """Boot a fresh :class:`AgentKernel` for a single scheduled tick.

    Splitting this out of ``triggers`` keeps the boundary clean — only
    higher layers (sdk / api / cli) wire kernels into trigger plumbing
    so the trigger runtime stays pure.
    """
    skills = SkillKernel.boot(config)
    return AgentKernel(config=config, skills=skills)


__all__ = ["default_kernel_factory"]
