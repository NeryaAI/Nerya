"""Journal-aware turn recovery (Phase 4).

The :class:`AgentKernel` journals every phase of a turn into
``journals/turn_steps.jsonl``. That journal is the authoritative record
of what ran, what stopped it, and with which budgets. This module turns
those records back into a structured summary an operator (or another
kernel) can reason about before deciding whether to resume.

Design goals:

* **Read-only**: loading a recovery view never mutates a journal.
* **Truth-based**: if a turn never closed (``close`` step missing), we
  surface that explicitly instead of guessing.
* **Operator-grade**: the output lists the last step kind, the stop
  reason, the iteration index, and a compact action timeline so the
  operator can decide safely whether to resume, replay in trace, or
  abandon.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from ..core import jsonl
from ..core.paths import WorkspacePaths


@dataclass
class TurnRecoveryState:
    turn_id: str
    trigger_event_id: str | None
    strategy_id: str | None
    session_id: str | None
    last_step_kind: str | None
    last_step_status: str | None
    last_iteration: int
    stopped_reason: str | None
    closed: bool
    has_error: bool
    steps: list[dict[str, Any]] = field(default_factory=list)
    actions: list[dict[str, Any]] = field(default_factory=list)
    tokens: int = 0
    usd: float = 0.0

    def asdict(self) -> dict[str, Any]:
        return {
            "turn_id": self.turn_id,
            "trigger_event_id": self.trigger_event_id,
            "strategy_id": self.strategy_id,
            "session_id": self.session_id,
            "last_step_kind": self.last_step_kind,
            "last_step_status": self.last_step_status,
            "last_iteration": self.last_iteration,
            "stopped_reason": self.stopped_reason,
            "closed": self.closed,
            "has_error": self.has_error,
            "steps_count": len(self.steps),
            "actions": list(self.actions),
            "tokens": self.tokens,
            "usd": self.usd,
            "resumable": self.is_resumable(),
        }

    def is_resumable(self) -> bool:
        """A turn is resumable when it has step rows but never closed
        and was stopped for a *budget* or *max_steps* condition, not a
        permanent ``llm_error``/``permission`` failure."""
        if self.closed:
            return False
        if not self.steps:
            return False
        if self.stopped_reason in {"budget", "max_steps", "max_iterations"}:
            return True
        if self.last_step_kind in {"act", "observe", "replan"}:
            return not self.has_error
        return False


def load_turn_state(paths: WorkspacePaths, turn_id: str) -> TurnRecoveryState:
    """Reconstruct a :class:`TurnRecoveryState` for ``turn_id`` from the
    persisted turn-step journal.

    Raises :class:`KeyError` if no record for ``turn_id`` exists.
    """
    journal = paths.journal("turn_steps")
    if not journal.exists():
        raise KeyError(f"turn_steps journal missing: {journal}")
    rows = [
        row for row in jsonl.read_all(journal)
        if row.get("turn_id") == turn_id
    ]
    if not rows:
        raise KeyError(f"no turn step records for turn_id={turn_id!r}")
    rows.sort(key=lambda r: int(r.get("index") or 0))

    closed = any(r.get("step_kind") == "close" for r in rows)
    last = rows[-1]
    has_error = any(r.get("status") == "error" for r in rows)

    actions = []
    tokens = 0
    usd = 0.0
    for r in rows:
        tokens += int(r.get("tokens") or 0)
        usd += float(r.get("usd") or 0.0)
        if r.get("step_kind") == "act":
            detail = r.get("detail") or {}
            actions.append({
                "action": detail.get("action"),
                "skill": detail.get("skill"),
                "status": r.get("status"),
                "error": r.get("error"),
            })

    stopped_reason: str | None = None
    for r in rows:
        detail = r.get("detail") or {}
        if detail.get("stopped_reason"):
            stopped_reason = str(detail["stopped_reason"])
    if stopped_reason is None and last.get("error_kind") == "budget":
        stopped_reason = "budget"

    first = rows[0]
    return TurnRecoveryState(
        turn_id=turn_id,
        trigger_event_id=first.get("trigger_event_id"),
        strategy_id=first.get("strategy_id"),
        session_id=first.get("session_id"),
        last_step_kind=last.get("step_kind"),
        last_step_status=last.get("status"),
        last_iteration=int(last.get("iteration") or 0),
        stopped_reason=stopped_reason,
        closed=closed,
        has_error=has_error,
        steps=rows,
        actions=actions,
        tokens=tokens,
        usd=usd,
    )


def list_open_turns(paths: WorkspacePaths) -> list[TurnRecoveryState]:
    """Enumerate every turn in the journal whose state is *resumable*.

    Useful when the runtime process is re-booted after a crash and
    wants to triage work that was in-flight.
    """
    journal = paths.journal("turn_steps")
    if not journal.exists():
        return []
    seen: dict[str, list[dict[str, Any]]] = {}
    for row in jsonl.read_all(journal):
        tid = row.get("turn_id")
        if not tid:
            continue
        seen.setdefault(str(tid), []).append(row)
    out: list[TurnRecoveryState] = []
    for tid in seen:
        try:
            state = load_turn_state(paths, tid)
        except KeyError:
            continue
        if state.is_resumable():
            out.append(state)
    return out
