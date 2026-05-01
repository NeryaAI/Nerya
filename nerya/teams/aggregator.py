"""Conflict matrix + final-context synthesis for a team run.

The aggregator never calls an LLM directly. It compiles the durable
state (tasks + blackboard + gates) into:

* ``conflict_matrix.json`` — opposing signals/claims grouped pair-wise.
* ``final_context.json`` — structured input for the parent kernel.
* ``final_report.md`` — human-readable memo.
"""

from __future__ import annotations

from typing import Any

from .blackboard import Blackboard
from .gates import GateOutcome
from .models import TeamRun, TeamTask
from .store import TeamStore


class TeamAggregator:
    def __init__(self, store: TeamStore):
        self.store = store

    def build_conflict_matrix(self, run_id: str) -> dict[str, Any]:
        bb = Blackboard(self.store, run_id)
        pairs = bb.conflict_candidates()
        rows: list[dict[str, Any]] = []
        for a, b in pairs:
            rows.append({
                "kind": "signal_conflict",
                "left": {
                    "id": a.id, "author": a.author,
                    "signal": (a.payload or {}).get("signal"),
                    "summary": a.summary, "confidence": a.confidence,
                },
                "right": {
                    "id": b.id, "author": b.author,
                    "signal": (b.payload or {}).get("signal"),
                    "summary": b.summary, "confidence": b.confidence,
                },
            })
        risks = [e for e in bb.list() if e.kind == "risk"]
        for risk in risks:
            rows.append({
                "kind": "risk_flag",
                "author": risk.author,
                "summary": risk.summary,
                "payload": risk.payload,
            })
        out = {"count": len(rows), "items": rows}
        self.store.write_synthesis_json(run_id, "conflict_matrix", out)
        return out

    def build_final_context(
        self,
        *,
        run: TeamRun,
        tasks: list[TeamTask],
        gates: list[GateOutcome],
    ) -> dict[str, Any]:
        bb = Blackboard(self.store, run.id)
        entries = bb.list()
        by_kind: dict[str, list[dict[str, Any]]] = {}
        for e in entries:
            by_kind.setdefault(e.kind, []).append({
                "id": e.id,
                "author": e.author,
                "summary": e.summary,
                "confidence": e.confidence,
                "task_id": e.task_id,
                "payload": e.payload,
            })
        signals = [item for items in by_kind.values() for item in items
                   if item.get("payload", {}).get("signal")]
        signal_counts: dict[str, int] = {}
        for s in signals:
            sig = str(s["payload"].get("signal")).lower()
            signal_counts[sig] = signal_counts.get(sig, 0) + 1
        consensus_signal = max(signal_counts.items(), key=lambda kv: kv[1])[0] if signal_counts else "none"
        actionable = all(g.ok for g in gates) and consensus_signal != "none"
        out = {
            "run_id": run.id,
            "template_id": run.template_id,
            "goal": run.goal,
            "phase": run.phase,
            "status": run.status,
            "actionable": actionable,
            "consensus_signal": consensus_signal,
            "signal_distribution": signal_counts,
            "tasks_summary": [
                {
                    "id": t.id,
                    "owner": t.owner,
                    "status": t.status,
                    "summary": t.result_summary,
                    "error": t.error,
                }
                for t in tasks
            ],
            "blackboard_by_kind": by_kind,
            "gates": [g.asdict() for g in gates],
        }
        self.store.write_synthesis_json(run.id, "final_context", out)
        return out

    def build_final_report(
        self,
        *,
        run: TeamRun,
        final_context: dict[str, Any],
    ) -> str:
        lines: list[str] = []
        lines.append(f"# Team Run: {run.template_id}")
        lines.append("")
        lines.append(f"Run id: `{run.id}`")
        lines.append(f"Goal: {run.goal}")
        lines.append(f"Phase: {run.phase}  Status: {run.status}")
        lines.append("")
        lines.append("## Consensus")
        lines.append(f"- Signal: **{final_context.get('consensus_signal')}**")
        lines.append(f"- Distribution: {final_context.get('signal_distribution')}")
        lines.append(f"- Actionable: **{final_context.get('actionable')}**")
        lines.append("")
        lines.append("## Tasks")
        for t in final_context.get("tasks_summary") or []:
            err = f" (error: {t.get('error')})" if t.get("error") else ""
            summary = t.get("summary") or ""
            lines.append(f"- `{t['id']}` — {t['owner']}: {t['status']}{err}")
            if summary:
                lines.append(f"    > {summary}")
        lines.append("")
        lines.append("## Gates")
        for g in final_context.get("gates") or []:
            mark = "✓" if g.get("ok") else "✗"
            lines.append(f"- {mark} `{g.get('gate_id')}` — {g.get('reason')}")
        lines.append("")
        lines.append("## Evidence digest")
        for kind, items in (final_context.get("blackboard_by_kind") or {}).items():
            lines.append(f"### {kind}")
            for item in items[:6]:
                lines.append(f"- ({item['author']}, conf={item.get('confidence')}) {item.get('summary') or ''}")
            lines.append("")
        text = "\n".join(lines).rstrip() + "\n"
        self.store.write_synthesis_text(run.id, "final_report.md", text)
        return text


__all__ = ["TeamAggregator"]
