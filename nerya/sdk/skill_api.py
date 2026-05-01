"""Raw skill-call wrapper — for tests and advanced callers."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from ..core.config import Config
from ..skills.kernel import SkillKernel


@dataclass
class SkillAPI:
    config: Config
    skills: SkillKernel

    def call(self, skill_id: str, action: str, *, payload: dict[str, Any],
             caller: str = "sdk", strategy_id: str | None = None,
             session_id: str | None = None,
             trigger_event_id: str | None = None) -> dict[str, Any]:
        return self.skills.call(
            skill_id, action, payload=payload, caller=caller,
            strategy_id=strategy_id, session_id=session_id,
            trigger_event_id=trigger_event_id,
        )

    def list(self) -> list[dict[str, Any]]:
        return self.skills.list()

    def view(self, skill_id: str) -> dict[str, Any] | None:
        """Plan 02 P0 §2 — detailed manifest view for ``nerya skill view``."""

        return self.skills.view(skill_id)

    def doctor(self) -> dict[str, Any]:
        """Plan 02 P0 §2 — self-diagnostic for ``nerya skill doctor``."""

        return self.skills.doctor()

    def reload(self) -> int:
        """Re-read every manifest from disk; returns number of skills."""

        return self.skills.reload()
