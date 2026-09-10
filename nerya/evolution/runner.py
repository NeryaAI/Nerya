"""Workspace observation tick; a proposal requires a concrete recommendation.

Scanning alone does not establish a learned rule or an actionable patch.
Strategy tuning owns model-authored candidates and validation/promotion.
"""
from __future__ import annotations

from typing import Any

from ..core.config import Config
from .event_store import record_event
from .reflection_engine import run_reflection
from .selector import select_assets_for_signals
from .signals import collect_signals


def evolve(config: Config) -> dict[str, Any]:
    reflection = run_reflection(config.paths, config=config)
    if not reflection["ok"] or not reflection["has_evidence"]:
        return {
            "status": "collection_failed" if not reflection["ok"] else "no_evidence",
            "reflection": reflection, "proposal": None, "signals": [], "selected_assets": {},
        }
    signals = collect_signals(config.paths, persist=True)
    strategies = list(reflection["strategies"])
    selected = select_assets_for_signals(
        config.paths, signals, strategy_id=strategies[0] if len(strategies) == 1 else None,
    )
    refs = list(dict.fromkeys([
        reflection["snapshot_ref"],
        *(str(ref) for signal in signals for ref in signal.get("evidence_refs") or []),
    ]))
    event = record_event(
        config.paths, signals=[str(signal["id"]) for signal in signals],
        genes_used=[str(gene["id"]) for gene in selected.get("genes", [])],
        validation_status="not_run", outcome="candidate",
        summary="Collected evidence for review; no learning rule or strategy change has been inferred.",
        evidence_refs=refs, metadata={"evidence_sha256": reflection["evidence_sha256"]},
    )
    return {
        "status": "awaiting_evidence_review", "reflection": reflection, "proposal": None,
        "signals": signals, "selected_assets": selected, "event": event,
        "next_action": "Review the frozen evidence and decide whether a concrete, testable change is justified. No change is a valid outcome.",
    }
