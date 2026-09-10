"""Collect reproducible observations for an agent to review, not canned lessons.

Collection never changes policy, assumes a trade was required, or turns raw
journal counts into durable memory. A verified review can explicitly remember
its conclusion through MemoryRuntime and propose changes through PatchProposal.
"""
from __future__ import annotations

import hashlib
import json
from typing import Any, Iterable

from ..core import jsonl
from ..core.atomic_write import atomic_write_text
from ..core.config import Config
from ..core.paths import WorkspacePaths
from ..core.redaction import redact_display_dict
from ..strategy_history.attribution import (
    attribute_session, execution_quality, paper_vs_live_divergence,
    read_strategy_evidence, subagent_contribution,
)


def collect_strategy_observations(paths: WorkspacePaths, strategy_id: str, *,
                                  max_sessions: int = 25) -> dict[str, Any]:
    if max_sessions < 0:
        raise ValueError("max_sessions must be non-negative")
    ledgers = read_strategy_evidence(paths, strategy_id)
    grouped: dict[str, dict[str, list[dict[str, Any]]]] = {}
    unattributed = {}
    for name, rows in ledgers.items():
        for row in rows:
            session = row.get("session_id")
            if not session:
                unattributed[name] = unattributed.get(name, 0) + 1
                continue
            grouped.setdefault(str(session), {}).setdefault(name, []).append(row)
    selected = list(grouped)[-max_sessions:] if max_sessions else []
    sessions = []
    for sid in selected:
        snapshot = grouped[sid]
        bundle = attribute_session(paths, strategy_id, sid, ledgers=snapshot).as_dict()
        bundle["execution_quality"] = execution_quality(paths, strategy_id, sid, ledgers=snapshot)
        bundle["subagent_contribution"] = subagent_contribution(paths, strategy_id, sid, ledgers=snapshot)
        sessions.append(bundle)
    return {
        "strategy_id": strategy_id, "sessions": sessions,
        "sessions_total": len(grouped), "sessions_included": len(selected),
        "unattributed_rows": unattributed,
        "counts": {name: len(rows) for name, rows in ledgers.items()},
        "divergence": paper_vs_live_divergence(paths, strategy_id, window_sessions=max_sessions, ledgers=ledgers),
        "interpretation": "Observed records only. Trade quality and causal explanations require review and out-of-sample validation.",
    }


def run_reflection(paths: WorkspacePaths, strategy_ids: Iterable[str] | None = None, *,
                   config: Config | None = None) -> dict[str, Any]:
    """Freeze an evidence packet; do not automatically learn or prescribe changes."""
    root = paths.strategies.resolve()
    requested = list(strategy_ids) if strategy_ids is not None else (
        sorted(path.name for path in root.iterdir() if path.is_dir() and (path / "history").exists())
        if root.exists() else []
    )
    limit = int(config.get("evolution.reflection.max_sessions", 25) if config is not None else 25)
    if limit < 0:
        raise ValueError("evolution.reflection.max_sessions must be non-negative")
    observations: dict[str, Any] = {}
    invalid: list[str] = []
    errors: list[dict[str, str]] = []
    for sid in dict.fromkeys(str(value).strip() for value in requested):
        target = (root / sid).resolve()
        if not sid or target.parent != root or not target.is_dir():
            invalid.append(sid)
            continue
        try:
            observations[sid] = collect_strategy_observations(paths, sid, max_sessions=limit)
        except (OSError, ValueError, TypeError) as exc:
            errors.append({"strategy_id": sid, "error": f"{type(exc).__name__}: {exc}"})
    # Do not count this subsystem's own events as new evidence for more evolution.
    journals = {name: jsonl.read_all(paths.journal(name)) for name in ("errors", "trading", "skills")}
    packet = redact_display_dict({"strategies": observations, "journals": journals})
    encoded = json.dumps(packet, ensure_ascii=False, sort_keys=True, default=str)
    digest = hashlib.sha256(encoded.encode("utf-8")).hexdigest()
    snapshot = paths.evolution / "reflections" / f"{digest}.json"
    atomic_write_text(snapshot, encoded)
    return {
        "ok": not invalid and not errors, "strategies": packet["strategies"],
        "errors": len(journals["errors"]), "collection_errors": errors,
        "journal_counts": {name: len(rows) for name, rows in journals.items()},
        "invalid_strategy_ids": invalid, "evidence_sha256": digest,
        "snapshot_ref": f"file:{snapshot.relative_to(paths.root).as_posix()}",
        "has_evidence": any(journals.values()) or any(any(item["counts"].values()) for item in observations.values()),
    }
