"""Evidence artifacts backing the production certification gates.

`docs/release-checklists.md` mandates an evidence package for every
live promotion:

* `explain`      — a recent agent/trigger explain bundle,
* `attribution`  — strategy attribution output,
* `scenario_replay` — scenario replay record,
* `divergence`   — paper-vs-live divergence evidence,
* `approval`     — signoff record from the approvals pipeline,
* `rehearsal`    — record of a successful paper→canary rehearsal.

Each kind has its own JSON file under
``workspace/release_evidence/<strategy_id>/<kind>.json`` containing at
minimum an ISO ``recorded_at`` field, a ``kind``, and an arbitrary
``payload``. Operators can record evidence programmatically via the
:func:`record` helper (also wrapped by HTTP/CLI surfaces) or by the
runtime when it emits the underlying analysis.

The certification gate (`nerya.ops.certification`) reads these
artifacts and checks freshness. Freshness defaults to the cautious end
of the audit window: 7 days for Gate A, 3 days for Gate B, 24 hours
for Gate C. Callers can override via `fresh_within_s`.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

from ..core.atomic_write import atomic_write_text
from ..core.paths import WorkspacePaths
from ..core.time import now_iso


EVIDENCE_KINDS: tuple[str, ...] = (
    "explain",
    "attribution",
    "scenario_replay",
    "divergence",
    "approval",
    "rehearsal",
)


@dataclass
class EvidenceRecord:
    kind: str
    strategy_id: str
    recorded_at: str
    payload: dict[str, Any]
    path: Path

    def asdict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "strategy_id": self.strategy_id,
            "recorded_at": self.recorded_at,
            "payload": dict(self.payload),
            "path": str(self.path),
        }


def _root(paths: WorkspacePaths) -> Path:
    return paths.root / "release_evidence"


def _strategy_dir(paths: WorkspacePaths, strategy_id: str) -> Path:
    return _root(paths) / strategy_id


def record(
    paths: WorkspacePaths, *,
    kind: str, strategy_id: str,
    payload: dict[str, Any] | None = None,
) -> EvidenceRecord:
    """Persist an evidence artifact for ``strategy_id``/``kind``.

    Overwrites any previous record for the same kind; the certification
    gate cares about "most recent" + freshness, not historical
    accumulation. The ``recorded_at`` timestamp is always stamped
    server-side so callers can't lie about freshness.
    """
    if kind not in EVIDENCE_KINDS:
        raise ValueError(
            f"unknown evidence kind {kind!r}; expected one of {list(EVIDENCE_KINDS)}"
        )
    if not strategy_id:
        raise ValueError("strategy_id required")
    ts = now_iso()
    doc = {
        "kind": kind,
        "strategy_id": strategy_id,
        "recorded_at": ts,
        "payload": dict(payload or {}),
    }
    sdir = _strategy_dir(paths, strategy_id)
    sdir.mkdir(parents=True, exist_ok=True)
    path = sdir / f"{kind}.json"
    atomic_write_text(path, json.dumps(doc, indent=2))
    return EvidenceRecord(kind=kind, strategy_id=strategy_id,
                          recorded_at=ts, payload=doc["payload"],
                          path=path)


def read(
    paths: WorkspacePaths, *, kind: str, strategy_id: str,
) -> EvidenceRecord | None:
    path = _strategy_dir(paths, strategy_id) / f"{kind}.json"
    if not path.exists():
        return None
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None
    return EvidenceRecord(
        kind=str(doc.get("kind") or kind),
        strategy_id=str(doc.get("strategy_id") or strategy_id),
        recorded_at=str(doc.get("recorded_at") or ""),
        payload=dict(doc.get("payload") or {}),
        path=path,
    )


def summary(paths: WorkspacePaths) -> dict[str, Any]:
    root = _root(paths)
    out: dict[str, Any] = {"strategies": []}
    if not root.exists():
        return out
    for sdir in sorted(root.iterdir()):
        if not sdir.is_dir():
            continue
        entry: dict[str, Any] = {"strategy_id": sdir.name, "kinds": {}}
        for kind in EVIDENCE_KINDS:
            rec = read(paths, kind=kind, strategy_id=sdir.name)
            entry["kinds"][kind] = (
                {"recorded_at": rec.recorded_at} if rec else None
            )
        out["strategies"].append(entry)
    return out


def _is_fresh(iso_ts: str, fresh_within_s: int) -> bool:
    import datetime as _dt
    try:
        ts = _dt.datetime.fromisoformat(iso_ts)
    except Exception:
        return False
    now = _dt.datetime.now(_dt.timezone.utc)
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=_dt.timezone.utc)
    age = (now - ts).total_seconds()
    return age >= 0 and age <= fresh_within_s


def has_fresh_bundle(
    paths: WorkspacePaths, *,
    strategy_id: str, kinds: Iterable[str], fresh_within_s: int,
) -> dict[str, Any]:
    """Return ``{kind: {"ok": bool, "recorded_at": str|None}}``."""
    rows: dict[str, Any] = {}
    for kind in kinds:
        rec = read(paths, kind=kind, strategy_id=strategy_id)
        if rec is None:
            rows[kind] = {"ok": False, "reason": "missing",
                          "recorded_at": None}
            continue
        if not _is_fresh(rec.recorded_at, fresh_within_s):
            rows[kind] = {"ok": False, "reason": "stale",
                          "recorded_at": rec.recorded_at}
            continue
        rows[kind] = {"ok": True, "recorded_at": rec.recorded_at}
    return rows


def pick_certifiable_strategy(
    paths: WorkspacePaths, *, required_kinds: Iterable[str],
    fresh_within_s: int,
) -> tuple[str | None, dict[str, Any]]:
    """Return the first strategy whose required-kind bundle is complete
    and fresh (if any), along with its per-kind status map.

    If no strategy fully qualifies, returns the strategy with the most
    green cells so the report still contains actionable information.
    """
    root = _root(paths)
    if not root.exists():
        return None, {}
    required = list(required_kinds)
    best_sid: str | None = None
    best_rows: dict[str, Any] = {}
    best_score = -1
    for sdir in sorted(root.iterdir()):
        if not sdir.is_dir():
            continue
        rows = has_fresh_bundle(
            paths, strategy_id=sdir.name,
            kinds=required, fresh_within_s=fresh_within_s,
        )
        score = sum(1 for r in rows.values() if r.get("ok"))
        if score == len(required):
            return sdir.name, rows
        if score > best_score:
            best_score = score
            best_sid = sdir.name
            best_rows = rows
    if best_sid is not None:
        return None, {"best_candidate": best_sid, "rows": best_rows}
    return None, {}


__all__ = [
    "EVIDENCE_KINDS",
    "EvidenceRecord",
    "record",
    "read",
    "summary",
    "has_fresh_bundle",
    "pick_certifiable_strategy",
]
