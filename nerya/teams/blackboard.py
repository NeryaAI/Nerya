"""Append-only shared blackboard for a team run.

Stored as JSONL alongside the run state. Reads are best-effort:
malformed lines are skipped. Writes are append-only via
``nerya.core.jsonl.append`` so they survive concurrent threads and
crashes.
"""

from __future__ import annotations

import uuid
from typing import Any, Optional

from ..core import jsonl
from ..core.redaction import redact_dict as redact_obj
from ..core.time import now_iso
from .models import BlackboardEntry
from .store import TeamStore


_VALID_KINDS = {
    "evidence", "claim", "signal", "risk", "question",
    "assumption", "conflict", "decision_input", "memory",
}


class Blackboard:
    def __init__(self, store: TeamStore, run_id: str):
        self.store = store
        self.run_id = run_id

    def append(
        self,
        *,
        kind: str,
        author: str,
        summary: str = "",
        payload: Optional[dict[str, Any]] = None,
        confidence: Optional[float] = None,
        source_refs: Optional[list[str]] = None,
        task_id: Optional[str] = None,
    ) -> BlackboardEntry:
        if kind not in _VALID_KINDS:
            kind = "claim"
        try:
            safe_payload = redact_obj(payload or {})
        except Exception:
            safe_payload = payload or {}
        entry = BlackboardEntry(
            id=uuid.uuid4().hex[:12],
            run_id=self.run_id,
            kind=kind,
            author=author,
            summary=str(summary or "")[:1024],
            payload=safe_payload if isinstance(safe_payload, dict) else {"value": safe_payload},
            confidence=confidence,
            source_refs=list(source_refs or []),
            task_id=task_id,
            created_at=now_iso(),
        )
        jsonl.append(self.store.blackboard_path(self.run_id), entry.asdict(), stamp=False)
        self.store.append_event(
            self.run_id,
            kind="blackboard.appended",
            entry_id=entry.id,
            entry_kind=entry.kind,
            author=entry.author,
            summary=entry.summary,
            confidence=entry.confidence,
            task_id=entry.task_id,
            source_refs=list(entry.source_refs),
        )
        return entry

    def list(self) -> list[BlackboardEntry]:
        rows = jsonl.read_all(self.store.blackboard_path(self.run_id))
        out: list[BlackboardEntry] = []
        for row in rows:
            try:
                out.append(BlackboardEntry(**{
                    k: v for k, v in row.items()
                    if k in BlackboardEntry.__dataclass_fields__
                }))
            except Exception:
                continue
        return out

    def preview_for_agent(
        self,
        agent: str,
        *,
        max_entries: int = 12,
        include_kinds: Optional[list[str]] = None,
    ) -> list[dict[str, Any]]:
        """Return a compacted, role-aware preview of recent entries.

        We exclude the agent's own outputs (so a member doesn't
        re-consume its own evidence) and prefer cross-author entries
        that are explicitly relevant to this role's typical concerns.
        """

        wanted = set(include_kinds or [
            "evidence", "claim", "signal", "risk", "question", "conflict",
        ])
        items = [e for e in self.list()
                 if e.kind in wanted and e.author != agent]
        # Score: high-signal kinds (risk, conflict) first, then recent.
        priority = {"risk": 3, "conflict": 3, "evidence": 2, "claim": 1, "signal": 2, "question": 2}
        items.sort(
            key=lambda e: (-priority.get(e.kind, 1), e.created_at),
            reverse=False,
        )
        out: list[dict[str, Any]] = []
        for e in items[:max_entries]:
            out.append({
                "id": e.id,
                "kind": e.kind,
                "author": e.author,
                "summary": e.summary,
                "confidence": e.confidence,
                "source_refs": list(e.source_refs),
                "task_id": e.task_id,
                "created_at": e.created_at,
            })
        return out

    def conflict_candidates(self) -> list[tuple[BlackboardEntry, BlackboardEntry]]:
        """Heuristic disagreement detector for ``signal`` / ``claim`` entries.

        Returns pairs of entries whose ``payload.signal`` field
        contradicts (e.g. ``bullish`` vs ``bearish``). Used by the
        aggregator to seed ``conflict_matrix.json``.
        """

        signals = [e for e in self.list() if e.kind in ("signal", "claim")]
        out: list[tuple[BlackboardEntry, BlackboardEntry]] = []
        for i, a in enumerate(signals):
            sa = str((a.payload or {}).get("signal") or "").lower()
            if not sa:
                continue
            for b in signals[i + 1:]:
                sb = str((b.payload or {}).get("signal") or "").lower()
                if not sb:
                    continue
                if _opposes(sa, sb):
                    out.append((a, b))
        return out

    def seed(
        self,
        *,
        goal: str,
        trigger: dict[str, Any],
        memory_preview: Optional[str] = None,
    ) -> None:
        self.append(
            kind="decision_input",
            author="orchestrator",
            summary="goal",
            payload={"goal": goal},
        )
        self.append(
            kind="decision_input",
            author="orchestrator",
            summary="trigger",
            payload={"kind": trigger.get("kind"), "payload": trigger.get("payload") or {}},
        )
        if memory_preview:
            self.append(
                kind="memory",
                author="orchestrator",
                summary="memory preview",
                payload={"text": str(memory_preview)[:2000]},
            )


_OPPOSITES = {
    ("bullish", "bearish"),
    ("buy", "sell"),
    ("long", "short"),
    ("expand", "tighten"),
    ("scale_in", "scale_out"),
}


def _opposes(a: str, b: str) -> bool:
    for x, y in _OPPOSITES:
        if {a, b} == {x, y}:
            return True
    return False


__all__ = ["Blackboard"]
