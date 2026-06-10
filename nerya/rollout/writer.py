"""Append-only rollout writer backed by Nerya JSONL journals.

Nerya's runtime already journals ``agent.turn.start`` / ``agent.turn.end``
records. This module provides the standard ``Turn`` / ``RolloutWriter`` shape
used by AgentArchitecturePatterns diagnostics without replacing the existing
kernel journal flow.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Mapping

from ..core import jsonl
from ..core.redaction import redact_display_dict


@dataclass(frozen=True)
class Turn:
    turn_id: str
    session_id: str | None = None
    kind: str = "agent.turn"
    payload: dict[str, Any] = field(default_factory=dict)

    def as_record(self) -> dict[str, Any]:
        return asdict(self)


class RolloutWriter:
    """Write turn records to an append-only rollout ``.jsonl`` file."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)

    def write(self, turn: Turn | Mapping[str, Any]) -> dict[str, Any]:
        record = turn.as_record() if isinstance(turn, Turn) else dict(turn)
        safe_record = redact_display_dict(record)
        return jsonl.append(self.path, safe_record)


__all__ = ["RolloutWriter", "Turn"]
