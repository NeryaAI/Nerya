"""Append to the per-strategy learnings.md file."""

from __future__ import annotations

from pathlib import Path

from ..core.paths import WorkspacePaths
from ..core.time import now_iso


def append_learning(paths: WorkspacePaths, *, strategy_id: str, note: str) -> Path:
    p = paths.strategy(strategy_id) / "learnings.md"
    p.parent.mkdir(parents=True, exist_ok=True)
    block = f"\n## {now_iso()}\n\n{note}\n"
    if p.exists():
        p.write_text(p.read_text(encoding="utf-8") + block, encoding="utf-8")
    else:
        p.write_text(f"# Learnings — {strategy_id}\n{block}", encoding="utf-8")
    return p


def append_strategy_learning(paths: WorkspacePaths, strategy_id: str,
                             *, note: str, kind: str = "strategy") -> Path:
    """Kind-aware alias of ``append_learning`` used by reflection/review."""
    _ = kind  # currently only a single strategy learnings.md file
    return append_learning(paths, strategy_id=strategy_id, note=note)


def append_global_learning(paths: WorkspacePaths, *, note: str, kind: str = "global") -> Path:
    target = paths.memory / {"global": "global.md",
                              "mistake": "mistakes.md",
                              "regime": "market_regimes.md",
                              "skill": "skill_learnings.md"}.get(kind, "global.md")
    target.parent.mkdir(parents=True, exist_ok=True)
    block = f"\n## {now_iso()}\n\n{note}\n"
    if target.exists():
        target.write_text(target.read_text(encoding="utf-8") + block, encoding="utf-8")
    else:
        target.write_text(f"# {kind.title()} memory\n{block}", encoding="utf-8")
    return target
