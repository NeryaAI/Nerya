"""TriggerEvent schema.

Audit 2026-04-24: ``VALID_SOURCES`` used to be a hard enum that rejected
any source the agent could legitimately invent (e.g. a new signal feed,
a novel scheduled-session tick). Per the production-readiness audit's
agent-inference guidance we keep the known names as a *canonical hint*
and log a warning on unknown sources, but we no longer reject them.
Structural validation (non-empty identifier-shaped string) still runs
so the trigger router never sees garbage.
"""

from __future__ import annotations

import logging
import re
from dataclasses import asdict, dataclass, field
from typing import Any

from ..core.errors import TriggerValidationError
from ..core.ids import event_id
from ..core.time import now_iso


log = logging.getLogger(__name__)

# Canonical source names that the runtime itself emits. Operator- or
# agent-emitted events may use additional identifier-shaped names; we
# log a one-time warning so the operator is aware, but we accept them.
KNOWN_SOURCES: frozenset[str] = frozenset({
    "script", "schedule", "price", "user_command", "webhook",
    "scheduled_session",   # compatibility cron/session path
})

# Back-compat alias for call sites that inspected the old name.
VALID_SOURCES = KNOWN_SOURCES

_SOURCE_RE = re.compile(r"^[a-zA-Z][a-zA-Z0-9_:\.\-]{0,63}$")
_UNKNOWN_SOURCES_WARNED: set[str] = set()


@dataclass
class TriggerEvent:
    event_id: str
    source: str
    kind: str
    payload: dict[str, Any]
    target: str = "main"
    strategy_id: str | None = None
    idempotency_key: str | None = None
    dry_run: bool = False
    occurred_at: str = field(default_factory=now_iso)

    def __post_init__(self):
        if not isinstance(self.source, str) or not _SOURCE_RE.match(self.source):
            raise TriggerValidationError(f"bad source: {self.source!r}")
        if self.source not in KNOWN_SOURCES and self.source not in _UNKNOWN_SOURCES_WARNED:
            _UNKNOWN_SOURCES_WARNED.add(self.source)
            log.warning(
                "trigger_event.unknown_source source=%r "
                "(accepted; add to KNOWN_SOURCES when canonical)",
                self.source,
            )
        if not self.kind:
            raise TriggerValidationError("empty kind")
        self._validate_target(self.target)

    @staticmethod
    def _validate_target(target: str) -> None:
        if target == "main":
            return
        if target.startswith("subagent:") and len(target.split(":", 1)[1]) > 0:
            return
        if target.startswith("skill:") and "." in target[len("skill:") :]:
            return
        raise TriggerValidationError(f"bad target: {target!r}")

    @classmethod
    def new(cls, **kwargs) -> "TriggerEvent":
        kwargs.setdefault("event_id", event_id())
        return cls(**kwargs)

    def asdict(self) -> dict[str, Any]:
        return asdict(self)
