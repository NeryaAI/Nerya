"""Aggregate outputs from multiple subagents into a single decision context."""

from __future__ import annotations

from typing import Any


def aggregate(outputs: list[dict[str, Any]]) -> dict[str, Any]:
    """Collapse subagent outputs into a simple dict keyed by subagent name."""
    merged: dict[str, Any] = {}
    confidences: list[float] = []
    for o in outputs:
        name = o.get("subagent", "unknown")
        data = o.get("output") or {}
        merged[name] = data
        c = data.get("confidence") or data.get("strength")
        if isinstance(c, (int, float)):
            confidences.append(float(c))
    return {
        "subagents": merged,
        "avg_confidence": round(sum(confidences) / max(1, len(confidences)), 3) if confidences else None,
    }
