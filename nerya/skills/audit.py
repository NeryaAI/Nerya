"""Per-skill audit journal helper."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from ..core import jsonl


def record(journal_path: Path, *, skill_id: str, action: str, outcome: str, payload: dict[str, Any] | None = None) -> None:
    jsonl.append(journal_path, {
        "kind": "skill.audit",
        "skill_id": skill_id,
        "action": action,
        "outcome": outcome,
        "payload": payload or {},
    })
