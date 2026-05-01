"""Journal = an append-only jsonl with a stable schema envelope.

Every journal entry carries:
    ts, kind, strategy_id, session_id, correlation_id, payload
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..core import jsonl
from ..core.time import now_iso


@dataclass
class Journal:
    path: Path

    def append(
        self,
        *,
        kind: str,
        payload: dict[str, Any],
        strategy_id: str | None = None,
        session_id: str | None = None,
        correlation_id: str | None = None,
    ) -> dict[str, Any]:
        record = {
            "ts": now_iso(),
            "kind": kind,
            "strategy_id": strategy_id,
            "session_id": session_id,
            "correlation_id": correlation_id,
            "payload": payload,
        }
        return jsonl.append(self.path, record, stamp=False)

    def tail(self, n: int = 200) -> list[dict[str, Any]]:
        return jsonl.tail(self.path, n)

    def read_all(self) -> list[dict[str, Any]]:
        return jsonl.read_all(self.path)
