"""Replay a strategy session from its history ledgers."""

from __future__ import annotations

from pathlib import Path

from ..core import jsonl
from ..core.paths import WorkspacePaths


def replay_session(paths: WorkspacePaths, strategy_id: str, session_id: str) -> list[dict]:
    hist = paths.strategy_history(strategy_id)
    events: list[dict] = []
    for name in ("triggers", "skill_calls", "subagents", "decisions",
                 "intents", "risk", "orders", "fills", "pnl",
                 "messages", "reviews"):
        p = hist / f"{name}.jsonl"
        for row in jsonl.read_all(p):
            if row.get("session_id") == session_id:
                row = {**row, "_kind": name}
                events.append(row)
    events.sort(key=lambda r: r.get("ts") or "")
    return events
