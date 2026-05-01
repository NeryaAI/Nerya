"""Per-route statistics for the trigger plane.

The router already journals every terminal event. This module reads the
trigger journal and aggregates per-route counters — useful for the
dashboard, for flaky-route detection, and for the architecture audit.
"""

from __future__ import annotations

import json
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path


TERMINAL_STATUSES: frozenset[str] = frozenset({
    "routed", "dedup", "cooldown", "rate_limited", "dead_letter", "dry_run",
})


@dataclass
class RouteStats:
    route_id: str | None
    counts: dict[str, int] = field(default_factory=lambda: defaultdict(int))

    def add(self, status: str) -> None:
        self.counts[status] = self.counts.get(status, 0) + 1

    @property
    def total(self) -> int:
        return sum(self.counts.values())

    def asdict(self) -> dict:
        return {
            "route_id": self.route_id,
            "total": self.total,
            "counts": dict(self.counts),
        }


def aggregate_from_journal(path: Path) -> dict[str | None, RouteStats]:
    """Read `journals/triggers.jsonl` and aggregate counts per route.

    The router writes one of six terminal kinds per event:
    ``trigger.routed``, ``trigger.dedup``, ``trigger.cooldown``,
    ``trigger.rate_limited``, ``trigger.dead_letter`` (actually written
    to errors.jsonl but mirrored via `kind`), and the dry-run journal
    entry. This function is robust to either source journal and to the
    event shape written by `RouterResult.asdict`.
    """
    out: dict[str | None, RouteStats] = {}
    if not path.exists():
        return out
    with open(path, "r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            kind = rec.get("kind", "")
            if not kind.startswith("trigger."):
                continue
            status = kind.split(".", 1)[1]
            if status not in TERMINAL_STATUSES:
                continue
            route_id = rec.get("route_id")
            stats = out.setdefault(route_id, RouteStats(route_id=route_id))
            stats.add(status)
    return out


def summary(path: Path) -> dict[str, int]:
    """Flat summary across all routes — one counter per terminal status."""
    totals: dict[str, int] = {s: 0 for s in TERMINAL_STATUSES}
    for stats in aggregate_from_journal(path).values():
        for status, count in stats.counts.items():
            totals[status] = totals.get(status, 0) + count
    return totals
