"""Gate evaluator for team templates.

A gate either *passes* (``ok=True``) or *blocks* (``ok=False``) with a
human-readable reason. ``required_tasks`` checks that listed task IDs
have completed. ``required_artifacts`` checks that the blackboard
contains at least one entry of each requested kind.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable

from .blackboard import Blackboard
from .models import TeamGateSpec, TeamTask


@dataclass
class GateOutcome:
    gate_id: str
    ok: bool
    reason: str = ""

    def asdict(self) -> dict[str, object]:
        return {"gate_id": self.gate_id, "ok": self.ok, "reason": self.reason}


def evaluate_gates(
    gates: list[TeamGateSpec],
    *,
    tasks: Iterable[TeamTask],
    blackboard: Blackboard,
) -> list[GateOutcome]:
    task_status = {t.id: t.status for t in tasks}
    bb = blackboard.list()
    out: list[GateOutcome] = []
    for gate in gates:
        if gate.kind == "required_tasks":
            wanted = list(gate.detail.get("tasks") or [])
            missing = [tid for tid in wanted if task_status.get(tid) != "completed"]
            if missing:
                out.append(GateOutcome(
                    gate.id, False,
                    reason=f"required tasks not completed: {missing}",
                ))
            else:
                out.append(GateOutcome(gate.id, True, reason="all required tasks completed"))
            continue
        if gate.kind == "required_artifacts":
            wanted = set(gate.detail.get("kinds") or [])
            present = {e.kind for e in bb}
            missing = sorted(wanted - present)
            if missing:
                out.append(GateOutcome(
                    gate.id, False,
                    reason=f"missing blackboard kinds: {missing}",
                ))
            else:
                out.append(GateOutcome(gate.id, True, reason="all required artifacts present"))
            continue
        # ponytail: unknown policy is a blocker, not an implicit allow.
        # A newly deployed template must declare an evaluator before it can
        # produce an actionable/completed run.
        out.append(GateOutcome(gate.id, False, reason=f"unknown gate kind {gate.kind!r}"))
    return out


__all__ = ["GateOutcome", "evaluate_gates"]
