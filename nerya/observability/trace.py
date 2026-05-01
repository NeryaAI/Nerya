"""Unified trace builder.

Phase 10 exit criterion: *an operator can trace any trigger from ingress
to final outcome*.

We don't keep a separate trace log. Instead we reconstruct an end-to-end
trace on demand from the journals we already write:

- ``journals/triggers.jsonl``       — ingress plane
- ``journals/turn_steps.jsonl``     — agent turn phases
- ``journals/skill_calls.jsonl``    — skill dispatches
- ``journals/subagents.jsonl``      — sub-agent outputs
- ``journals/evolution.jsonl``      — proposal lifecycle
- ``workspace/strategies/<sid>/history/*.jsonl`` — strategy-scoped
  intents/risk/orders/fills/reviews

The builder groups events by the strongest available correlator in
this order: ``trigger_id``, then ``turn_id``, then ``session_id``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

from ..core import jsonl
from ..core.paths import WorkspacePaths
from ..strategy_history import store as hist_store


TRACE_SURFACES: tuple[str, ...] = (
    "trigger", "turn", "subagent", "skill",
    "intent", "risk", "order", "fill", "review", "evolution",
)


@dataclass
class TraceEvent:
    surface: str
    ts: str | None
    record: dict[str, Any]


@dataclass
class Trace:
    correlator: dict[str, str | None]
    events: list[TraceEvent] = field(default_factory=list)

    def surfaces(self) -> set[str]:
        return {e.surface for e in self.events}

    def as_dict(self) -> dict[str, Any]:
        return {
            "correlator": dict(self.correlator),
            "events": [
                {"surface": e.surface, "ts": e.ts, "record": e.record}
                for e in self.events
            ],
            "surfaces": sorted(self.surfaces()),
        }


def _matches(row: dict[str, Any], keys: Iterable[str], values: set[str]) -> bool:
    if not values:
        return True
    for k in keys:
        v = row.get(k)
        if v is None:
            # Some writers nest the id one level deep.
            for candidate in ("event", "intent", "fill", "order",
                              "risk_decision", "decision", "review"):
                nested = row.get(candidate)
                if isinstance(nested, dict):
                    v = nested.get(k)
                    if v is not None:
                        break
        if v is not None and str(v) in values:
            return True
    return False


def _read(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    return jsonl.read_all(path)


def _strategy_dirs(paths: WorkspacePaths) -> list[str]:
    if not paths.strategies.exists():
        return []
    return [p.name for p in paths.strategies.iterdir() if p.is_dir()]


def build_trace(
    paths: WorkspacePaths,
    *,
    trigger_id: str | None = None,
    turn_id: str | None = None,
    session_id: str | None = None,
    strategy_id: str | None = None,
) -> Trace:
    if not any((trigger_id, turn_id, session_id)):
        raise ValueError(
            "build_trace requires at least one of trigger_id/turn_id/session_id"
        )

    trigger_set = {trigger_id} if trigger_id else set()
    turn_set    = {turn_id} if turn_id else set()
    session_set = {session_id} if session_id else set()

    events: list[TraceEvent] = []

    def push(surface: str, row: dict[str, Any]) -> None:
        events.append(TraceEvent(
            surface=surface,
            ts=row.get("ts"),
            record=row,
        ))

    # Global journals.
    for row in _read(paths.journal("triggers")):
        if _matches(row, ("trigger_id", "id"), trigger_set):
            push("trigger", row)
            # triggers carry an implicit turn_id when the router hands off
            nested = row.get("event") or {}
            if row.get("turn_id"):
                turn_set.add(str(row["turn_id"]))
            if isinstance(nested, dict) and nested.get("turn_id"):
                turn_set.add(str(nested["turn_id"]))

    for row in _read(paths.journal("turn_steps")):
        if _matches(row, ("turn_id",), turn_set) or _matches(
                row, ("trigger_id",), trigger_set):
            push("turn", row)
            if row.get("session_id"):
                session_set.add(str(row["session_id"]))

    for row in _read(paths.journal("skill_calls")):
        if (_matches(row, ("turn_id", "session_id"), turn_set | session_set)
                or _matches(row, ("trigger_id",), trigger_set)):
            push("skill", row)

    # Harness journal rows are the agent-visible tool calls. They carry the
    # actual LLM action aliases (create_strategy/add_schedule/send_message)
    # and results operators care about when asking "how did you implement it?".
    for row in _read(paths.journal("harness")):
        if (_matches(row, ("turn_id", "session_id"), turn_set | session_set)
                or _matches(row, ("trigger_event_id", "trigger_id"), trigger_set)):
            push("skill", row)

    for row in _read(paths.journal("evolution")):
        if _matches(row, ("session_id", "turn_id"), session_set | turn_set):
            push("evolution", row)

    # Per-strategy journals.
    strategies = [strategy_id] if strategy_id else _strategy_dirs(paths)
    for sid in strategies:
        for ledger, surface in (
            ("subagents", "subagent"),
            ("intents", "intent"),
            ("risk", "risk"),
            ("orders", "order"),
            ("fills", "fill"),
            ("reviews", "review"),
        ):
            for row in hist_store.read_ledger(paths, sid, ledger):
                if _matches(row, ("session_id", "turn_id", "trigger_id"),
                            session_set | turn_set | trigger_set):
                    row = {**row, "strategy_id": sid}
                    push(surface, row)

    events.sort(key=lambda e: e.ts or "")
    return Trace(
        correlator={
            "trigger_id": trigger_id,
            "turn_id": turn_id,
            "session_id": session_id,
            "strategy_id": strategy_id,
        },
        events=events,
    )


# ==================================================================
# Phase 10 v2 — operator explain / degradation surfaces
# ==================================================================

def explain_trace(paths: WorkspacePaths, *,
                  trigger_id: str | None = None,
                  turn_id: str | None = None,
                  session_id: str | None = None,
                  strategy_id: str | None = None) -> dict[str, Any]:
    """Summarise a trace from an operator's viewpoint.

    Adds, on top of the raw event list:

    * ``stages`` — ingress → turn → subagents → skill → intent → risk →
      order → fill → review, with event counts per stage;
    * ``degradations`` — truth-gate / degraded-envelope / capability
      error rows extracted from the trace;
    * ``attribution`` — when ``session_id`` and ``strategy_id`` resolve,
      the Phase 8 root-cause summary;
    * ``active_strategy_version`` — the strategy version active at
      query time (so post-mortems can anchor "which version ran this").
    """
    trace = build_trace(
        paths,
        trigger_id=trigger_id, turn_id=turn_id,
        session_id=session_id, strategy_id=strategy_id,
    )
    stages: dict[str, int] = {k: 0 for k in TRACE_SURFACES}
    degradations: list[dict[str, Any]] = []
    for evt in trace.events:
        stages[evt.surface] = stages.get(evt.surface, 0) + 1
        rec = evt.record or {}
        # Truth-gate / degraded signals can live inline on many rows.
        env = rec.get("_envelope") or rec.get("envelope") or {}
        if isinstance(env, dict) and env.get("source_mode") == "degraded":
            degradations.append({
                "surface": evt.surface,
                "reason": env.get("reason") or "degraded_envelope",
                "ts": evt.ts,
            })
        if rec.get("kind") == "skill.call.error" or rec.get("status") == "error":
            degradations.append({
                "surface": evt.surface,
                "reason": rec.get("error") or rec.get("reason") or "error",
                "ts": evt.ts,
            })

    attribution: dict[str, Any] = {}
    active_version: str | None = None
    if strategy_id and session_id:
        try:
            from ..strategy_history.attribution import attribute_session
            attribution = attribute_session(paths, strategy_id, session_id).as_dict()
        except Exception:
            attribution = {}
    if strategy_id:
        try:
            from ..trading import strategy_versions
            active_version = strategy_versions.active_version_id(paths, strategy_id)
        except Exception:
            active_version = None

    return {
        "correlator": dict(trace.correlator),
        "stages": stages,
        "degradations": degradations,
        "attribution": attribution,
        "active_strategy_version": active_version,
        "event_count": len(trace.events),
        "surfaces": sorted(trace.surfaces()),
    }
