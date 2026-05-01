"""Deterministic signal extraction for the self-evolution loop."""

from __future__ import annotations

import json
from typing import Any

from ..core import jsonl
from ..core.paths import WorkspacePaths
from .events import EvolutionSignal
from .event_store import append_signals


_CORRECTION_MARKERS = (
    "不是",
    "不对",
    "错",
    "你理解错",
    "actually",
    "wrong",
    "incorrect",
    "not what i meant",
)


def collect_signals(
    paths: WorkspacePaths,
    *,
    strategy_id: str | None = None,
    persist: bool = False,
    limit: int = 200,
) -> list[dict[str, Any]]:
    """Extract recent deterministic signals from journals and proposals."""

    signals: list[EvolutionSignal] = []
    signals.extend(_agent_turn_signals(paths, strategy_id=strategy_id, limit=limit))
    signals.extend(_tool_failure_signals(paths, strategy_id=strategy_id, limit=limit))
    signals.extend(_operator_correction_signals(paths, strategy_id=strategy_id, limit=limit))
    signals.extend(_proposal_outcome_signals(paths, strategy_id=strategy_id, limit=limit))
    signals.extend(_strategy_tuning_signals(paths, strategy_id=strategy_id, limit=limit))
    if persist:
        return append_signals(paths, signals, dedupe=True)
    return [s.asdict() for s in _dedupe(signals)]


def _agent_turn_signals(
    paths: WorkspacePaths,
    *,
    strategy_id: str | None,
    limit: int,
) -> list[EvolutionSignal]:
    rows = [
        (idx, row)
        for idx, row in enumerate(jsonl.read_all(paths.journal("agent")))
        if row.get("kind") == "agent.turn.end"
    ][-limit:]
    if strategy_id:
        rows = [
            (i, r) for i, r in rows
            if str(r.get("strategy_id") or "") == strategy_id
        ]
    tail = rows[-10:]
    noop_rows = []
    for idx, row in tail:
        final_text = str(row.get("final_text") or "").strip()
        tool_calls = int(row.get("tool_calls") or 0)
        stop = str(row.get("stop_reason") or "")
        if not final_text and tool_calls == 0:
            noop_rows.append((idx, row))
        elif stop in {"max_iterations", "empty"} and tool_calls == 0:
            noop_rows.append((idx, row))
    if len(noop_rows) < 3:
        return []
    refs = [f"journal:agent:{idx}" for idx, _ in noop_rows[-5:]]
    sid = str(noop_rows[-1][1].get("strategy_id") or "") or None
    return [
        EvolutionSignal.create(
            source="turn",
            kind="repeated_noop",
            severity="warn",
            strategy_id=sid,
            evidence_refs=refs,
            summary=(
                f"{len(noop_rows)}/{len(tail)} recent turns produced no "
                "message and no tool calls."
            ),
            dedupe_key=f"turn:repeated_noop:{sid or '*'}",
            confidence=min(1.0, len(noop_rows) / max(1, len(tail))),
        )
    ]


def _tool_failure_signals(
    paths: WorkspacePaths,
    *,
    strategy_id: str | None,
    limit: int,
) -> list[EvolutionSignal]:
    rows = jsonl.read_all(paths.journal("agent"))[-limit:]
    failures: dict[str, list[tuple[int, dict[str, Any]]]] = {}
    for idx, row in enumerate(rows):
        if strategy_id and str(row.get("strategy_id") or "") != strategy_id:
            continue
        blob = json.dumps(row, ensure_ascii=False, default=str).lower()
        kind = str(row.get("kind") or "")
        looks_like_tool = "tool" in kind or "tool" in blob
        failed = (
            row.get("ok") is False
            or bool(row.get("error"))
            or "error_kind" in row
            or "permission_pending" in blob
        )
        if not looks_like_tool or not failed:
            continue
        tool = (
            row.get("tool")
            or row.get("action")
            or row.get("skill_id")
            or row.get("kind")
            or "unknown_tool"
        )
        failures.setdefault(str(tool), []).append((idx, row))
    out: list[EvolutionSignal] = []
    for tool, group in failures.items():
        if len(group) < 2:
            continue
        refs = [f"journal:agent:{idx}" for idx, _ in group[-5:]]
        sid = str(group[-1][1].get("strategy_id") or "") or None
        out.append(
            EvolutionSignal.create(
                source="tool",
                kind="tool_failure_cluster",
                severity="critical" if len(group) >= 5 else "warn",
                strategy_id=sid,
                evidence_refs=refs,
                summary=f"{len(group)} recent failures around tool/action {tool}.",
                dedupe_key=f"tool_failure_cluster:{tool}:{sid or '*'}",
                confidence=min(1.0, len(group) / 5.0),
                metadata={"tool": tool, "count": len(group)},
            )
        )
    return out


def _operator_correction_signals(
    paths: WorkspacePaths,
    *,
    strategy_id: str | None,
    limit: int,
) -> list[EvolutionSignal]:
    rows = jsonl.read_all(paths.journal("agent"))[-limit:]
    out: list[EvolutionSignal] = []
    for idx, row in enumerate(rows):
        if row.get("kind") != "agent.turn.start":
            continue
        if strategy_id and str(row.get("strategy_id") or "") != strategy_id:
            continue
        text = str(row.get("user_text") or "").lower()
        if not any(marker in text for marker in _CORRECTION_MARKERS):
            continue
        sid = str(row.get("strategy_id") or "") or None
        turn_id = str(row.get("turn_id") or idx)
        out.append(
            EvolutionSignal.create(
                source="operator",
                kind="user_correction",
                severity="warn",
                strategy_id=sid,
                evidence_refs=[f"journal:agent:{idx}", f"turn:{turn_id}"],
                summary="Operator correction detected in recent user turn.",
                dedupe_key=f"user_correction:{turn_id}",
                confidence=0.75,
            )
        )
    return out


def _proposal_outcome_signals(
    paths: WorkspacePaths,
    *,
    strategy_id: str | None,
    limit: int,
) -> list[EvolutionSignal]:
    del strategy_id
    rows = jsonl.read_all(paths.journal("evolution"))[-limit:]
    out: list[EvolutionSignal] = []
    for idx, row in enumerate(rows):
        kind = str(row.get("kind") or "")
        state = str(row.get("state") or "")
        pid = str(row.get("proposal_id") or "")
        if not pid:
            continue
        if kind == "proposal.state" and state in {"rejected", "rolled_back"}:
            out.append(
                EvolutionSignal.create(
                    source="proposal",
                    kind=(
                        "proposal_rolled_back"
                        if state == "rolled_back"
                        else "proposal_rejected"
                    ),
                    severity="warn",
                    strategy_id=None,
                    evidence_refs=[f"journal:evolution:{idx}", f"proposal:{pid}"],
                    summary=f"Proposal {pid} moved to {state}.",
                    dedupe_key=f"proposal_outcome:{pid}:{state}",
                    confidence=1.0,
                    metadata={"proposal_id": pid, "state": state},
                )
            )
    return out


def _strategy_tuning_signals(
    paths: WorkspacePaths,
    *,
    strategy_id: str | None,
    limit: int,
) -> list[EvolutionSignal]:
    rows = jsonl.read_all(paths.journal("strategy_evolution"))[-limit:]
    out: list[EvolutionSignal] = []
    for idx, row in enumerate(rows):
        sid = str(row.get("strategy_id") or "")
        if strategy_id and sid != strategy_id:
            continue
        status = str(row.get("status") or "")
        reason = str(row.get("reason") or "")
        if status != "error" and "validation" not in reason.lower():
            continue
        out.append(
            EvolutionSignal.create(
                source="strategy",
                kind=(
                    "validation_failed"
                    if "validation" in reason.lower()
                    else "strategy_tuning_failed"
                ),
                severity="warn",
                strategy_id=sid or None,
                evidence_refs=[f"journal:strategy_evolution:{idx}"],
                summary=f"Strategy tuning did not produce a promotable result: {reason}",
                dedupe_key=f"strategy_tuning:{sid}:{status}:{reason[:80]}",
                confidence=0.8,
            )
        )
    return out


def _dedupe(signals: list[EvolutionSignal]) -> list[EvolutionSignal]:
    seen: set[str] = set()
    out: list[EvolutionSignal] = []
    for sig in signals:
        if sig.dedupe_key in seen:
            continue
        seen.add(sig.dedupe_key)
        out.append(sig)
    return out


__all__ = ["collect_signals"]
