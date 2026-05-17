"""Runtime support modules.

This package hosts cross-cutting runtime helpers used by the API server,
operator dashboard, and gateway transcripts. Modules here intentionally
avoid importing the agent kernel or trading subsystems at import time so
they can be consumed by lightweight HTTP routes without forcing a full
SkillKernel boot.
"""

from __future__ import annotations

__all__: list[str] = []
