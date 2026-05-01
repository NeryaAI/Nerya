"""Lightweight journal analyzers used by evolution and review."""

from __future__ import annotations

from collections import Counter
from typing import Any

from ..core import jsonl
from ..core.paths import WorkspacePaths


def summarize_errors(paths: WorkspacePaths, limit: int = 200) -> dict[str, Any]:
    rows = jsonl.read_all(paths.journal("errors"))[-limit:]
    by_kind = Counter(r.get("kind", "unknown") for r in rows)
    return {"count": len(rows), "by_kind": dict(by_kind), "tail": rows[-5:]}


def summarize_risk(paths: WorkspacePaths, strategy_id: str, limit: int = 200) -> dict[str, Any]:
    from ..strategy_history.store import read_ledger
    rows = read_ledger(paths, strategy_id, "risk")[-limit:]
    decisions = Counter((r.get("risk_decision") or {}).get("decision") for r in rows)
    reasons: Counter = Counter()
    for r in rows:
        for reason in (r.get("risk_decision") or {}).get("reasons", []):
            reasons[reason.split(":", 1)[0]] += 1
    return {"count": len(rows), "decisions": dict(decisions),
            "top_reasons": reasons.most_common(10)}
