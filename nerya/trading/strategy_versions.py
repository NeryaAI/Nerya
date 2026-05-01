"""Strategy versioning, promotion, and rollback.

A Nerya strategy is not just a folder on disk: it evolves. We need an
auditable record of *every* change that affects live trading surface —
prompt edits, limit tweaks, account/wallet rebinds, route remaps,
environment snapshot deltas — along with explicit promotion and
rollback records.

This module writes three artifacts per strategy:

* ``strategies/<id>/versions.jsonl`` — append-only version ledger.
  Each row is a snapshot with a content-derived version id.
* ``strategies/<id>/active_version.json`` — a single pointer to the
  currently-active version id (what's executing on the runtime).
* ``strategies/<id>/promotions.jsonl`` — append-only log of
  ``paper -> canary -> live`` / rollback events, cross-referencing
  the version id that was promoted or rolled-back-to.

Design intent
-------------
* **Content-derived IDs** — the version id is ``v-<12 hex of sha256
  of snapshot>``. Two identical snapshots collapse to the same id so
  rollbacks don't introduce synthetic history.
* **Snapshots are plain dicts** — they capture prompts/routes/account/
  environment metadata so a rollback can restore every field that
  matters, without having to replay mutations on strategy.yml.
* **Promotions cross-link status transitions and version ids** — so
  reflection / review / evolution can answer "which prompt was live
  when trade T was opened?" precisely.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from ..core import jsonl
from ..core.atomic_write import atomic_write_text
from ..core.paths import WorkspacePaths
from ..core.time import now_iso


# ---------------------------------------------------------------- types
@dataclass
class StrategyVersion:
    version_id: str
    strategy_id: str
    created_at: str
    status: str
    reason: str
    author: str
    snapshot: dict[str, Any] = field(default_factory=dict)
    parent_version_id: str | None = None

    def asdict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class PromotionRecord:
    strategy_id: str
    ts: str
    kind: str               # "promote" | "rollback"
    from_status: str
    to_status: str
    version_id: str         # version id that is active *after* this event
    previous_version_id: str | None = None
    reason: str = ""
    author: str = ""

    def asdict(self) -> dict[str, Any]:
        return asdict(self)


# ---------------------------------------------------------------- paths
def _versions_path(paths: WorkspacePaths, sid: str) -> Path:
    return paths.strategy(sid) / "versions.jsonl"


def _promotions_path(paths: WorkspacePaths, sid: str) -> Path:
    return paths.strategy(sid) / "promotions.jsonl"


def _active_pointer_path(paths: WorkspacePaths, sid: str) -> Path:
    return paths.strategy(sid) / "active_version.json"


# ---------------------------------------------------------------- hashing
def _hash_snapshot(snapshot: dict[str, Any]) -> str:
    payload = json.dumps(snapshot, sort_keys=True, ensure_ascii=False,
                         default=str).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()[:12]


# ---------------------------------------------------------------- snapshot
def build_snapshot(paths: WorkspacePaths, sid: str) -> dict[str, Any]:
    """Read the live strategy files and return the canonical snapshot dict.

    Captures every artifact whose change must produce a new version:

    * ``strategy.yml``, ``config.yml``, ``limits.yml`` — YAML config files
    * every prompt under ``prompts/*.agent.md``
    * the ``bindings`` block extracted from ``strategy.yml`` — account_id,
      wallet_id, markets, trigger_kinds, subagents (the fields that
      determine which live accounts, routes, and collaborators a
      strategy will touch when it runs). These are already inside
      ``strategy.yml`` but we also surface them here so a ``compare``
      diff can render a clear binding delta without re-parsing YAML.

    The shape is backward-compatible: existing callers that only read
    ``files`` and ``prompts`` continue to work; the ``bindings`` block
    is additive and content-hashed like the rest.
    """
    root = paths.strategy(sid)
    snapshot: dict[str, Any] = {"files": {}}
    for fname in ("strategy.yml", "config.yml", "limits.yml"):
        p = root / fname
        if p.exists():
            snapshot["files"][fname] = p.read_text(encoding="utf-8")
    prompts_dir = root / "prompts"
    if prompts_dir.exists():
        snapshot["prompts"] = {}
        for p in sorted(prompts_dir.glob("*.agent.md")):
            snapshot["prompts"][p.name] = p.read_text(encoding="utf-8")

    snapshot["bindings"] = _extract_bindings(root)
    return snapshot


def _extract_bindings(root: Path) -> dict[str, Any]:
    """Pull the bindings block out of ``strategy.yml``.

    Best-effort: if ``strategy.yml`` is missing or malformed we return
    an empty dict so an old strategy never blocks version recording.
    """
    from ..core import yaml_io
    try:
        doc = yaml_io.load(root / "strategy.yml", default={}) or {}
    except Exception:
        return {}
    if not isinstance(doc, dict):
        return {}
    bindings: dict[str, Any] = {}
    for key in ("account_id", "wallet_id"):
        val = doc.get(key)
        if val is not None:
            bindings[key] = val
    for key in ("markets", "trigger_kinds", "subagents"):
        val = doc.get(key)
        if isinstance(val, list):
            bindings[key] = sorted(str(x) for x in val if x is not None)
    status = doc.get("status")
    if status is not None:
        bindings["status"] = str(status)
    return bindings


def apply_snapshot(paths: WorkspacePaths, sid: str,
                   snapshot: dict[str, Any]) -> None:
    """Write a snapshot back onto disk (used by rollback)."""
    root = paths.strategy(sid)
    root.mkdir(parents=True, exist_ok=True)
    for fname, body in (snapshot.get("files") or {}).items():
        # Only rewrite files we know about; never touch history.
        if fname not in {"strategy.yml", "config.yml", "limits.yml"}:
            continue
        atomic_write_text(root / fname, body)
    prompts = snapshot.get("prompts") or {}
    if prompts:
        (root / "prompts").mkdir(exist_ok=True)
        for pname, body in prompts.items():
            atomic_write_text(root / "prompts" / pname, body)


# ---------------------------------------------------------------- versions
def record_version(paths: WorkspacePaths, sid: str, *,
                   status: str,
                   reason: str,
                   author: str = "runtime",
                   parent_version_id: str | None = None,
                   ) -> StrategyVersion:
    snapshot = build_snapshot(paths, sid)
    version_id = "v-" + _hash_snapshot(snapshot)
    entry = StrategyVersion(
        version_id=version_id,
        strategy_id=sid,
        created_at=now_iso(),
        status=status,
        reason=reason,
        author=author,
        snapshot=snapshot,
        parent_version_id=parent_version_id,
    )
    p = _versions_path(paths, sid)
    p.parent.mkdir(parents=True, exist_ok=True)
    # De-duplicate — if the last recorded version has the same id the
    # snapshot is already captured and we just return it.
    existing = list_versions(paths, sid)
    if existing and existing[-1].version_id == version_id:
        return existing[-1]
    jsonl.append(p, entry.asdict())
    _set_active_version(paths, sid, version_id)
    return entry


def list_versions(paths: WorkspacePaths, sid: str) -> list[StrategyVersion]:
    p = _versions_path(paths, sid)
    if not p.exists():
        return []
    out: list[StrategyVersion] = []
    for line in p.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except Exception:
            continue
        out.append(StrategyVersion(
            version_id=row["version_id"],
            strategy_id=row["strategy_id"],
            created_at=row.get("created_at", ""),
            status=row.get("status", ""),
            reason=row.get("reason", ""),
            author=row.get("author", ""),
            snapshot=row.get("snapshot") or {},
            parent_version_id=row.get("parent_version_id"),
        ))
    return out


def get_version(paths: WorkspacePaths, sid: str,
                version_id: str) -> StrategyVersion | None:
    for v in list_versions(paths, sid):
        if v.version_id == version_id:
            return v
    return None


def active_version_id(paths: WorkspacePaths, sid: str) -> str | None:
    p = _active_pointer_path(paths, sid)
    if not p.exists():
        return None
    try:
        doc = json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return None
    v = doc.get("version_id")
    return str(v) if v else None


def _set_active_version(paths: WorkspacePaths, sid: str,
                        version_id: str) -> None:
    p = _active_pointer_path(paths, sid)
    p.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_text(p, json.dumps({
        "version_id": version_id,
        "strategy_id": sid,
        "updated_at": now_iso(),
    }, indent=2))


# ---------------------------------------------------------------- promotion
def record_promotion(paths: WorkspacePaths, sid: str, *,
                     from_status: str, to_status: str,
                     reason: str = "",
                     author: str = "runtime") -> PromotionRecord:
    """Record a status-change promotion and pin the current version."""
    # Ensure the current strategy state is captured in the version
    # ledger before we record the promotion — this way every promotion
    # always has a matching ``version_id``.
    version = record_version(
        paths, sid, status=to_status,
        reason=reason or f"promote:{from_status}->{to_status}",
        author=author,
        parent_version_id=active_version_id(paths, sid),
    )
    prev = active_version_id(paths, sid)
    # The active pointer was updated inside record_version; prev is the
    # previous value — pull it from the ledger by finding the second
    # most recent row if any.
    versions = list_versions(paths, sid)
    prev_version = (versions[-2].version_id
                    if len(versions) >= 2 else None)
    rec = PromotionRecord(
        strategy_id=sid, ts=now_iso(), kind="promote",
        from_status=from_status, to_status=to_status,
        version_id=version.version_id,
        previous_version_id=prev_version,
        reason=reason, author=author,
    )
    jsonl.append(_promotions_path(paths, sid), rec.asdict())
    return rec


def rollback_to(paths: WorkspacePaths, sid: str, version_id: str, *,
                reason: str = "",
                author: str = "runtime") -> PromotionRecord:
    """Roll the live strategy files back to the snapshot of ``version_id``.

    Writes every file in the target version's snapshot back to disk,
    updates the active pointer, and appends a promotion row tagged
    ``kind="rollback"`` so the history is explicit.
    """
    target = get_version(paths, sid, version_id)
    if target is None:
        raise ValueError(f"unknown version {version_id!r} for strategy {sid!r}")
    current_active = active_version_id(paths, sid)
    apply_snapshot(paths, sid, target.snapshot)
    _set_active_version(paths, sid, target.version_id)
    # Also append a ledger row so the most-recent active matches.
    snapshot_again = build_snapshot(paths, sid)
    # After apply_snapshot the live files equal the target snapshot; the
    # resulting content-hash equals target.version_id.
    assert ("v-" + _hash_snapshot(snapshot_again)) == target.version_id, (
        "rollback snapshot did not produce the expected version id"
    )
    rec = PromotionRecord(
        strategy_id=sid, ts=now_iso(), kind="rollback",
        from_status=target.status, to_status=target.status,
        version_id=target.version_id,
        previous_version_id=current_active,
        reason=reason or f"rollback_to:{version_id}",
        author=author,
    )
    jsonl.append(_promotions_path(paths, sid), rec.asdict())
    return rec


def list_promotions(paths: WorkspacePaths, sid: str) -> list[PromotionRecord]:
    p = _promotions_path(paths, sid)
    if not p.exists():
        return []
    out: list[PromotionRecord] = []
    for line in p.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except Exception:
            continue
        out.append(PromotionRecord(
            strategy_id=row["strategy_id"],
            ts=row.get("ts", ""),
            kind=row.get("kind", "promote"),
            from_status=row.get("from_status", ""),
            to_status=row.get("to_status", ""),
            version_id=row.get("version_id", ""),
            previous_version_id=row.get("previous_version_id"),
            reason=row.get("reason", ""),
            author=row.get("author", ""),
        ))
    return out


def _diff_scope(lv_map: dict[str, Any], rv_map: dict[str, Any]
                ) -> tuple[dict[str, dict[str, Any]], dict[str, int]]:
    out: dict[str, dict[str, Any]] = {}
    counts = {"same": 0, "added": 0, "removed": 0, "changed": 0}
    for name in sorted(set(lv_map.keys()) | set(rv_map.keys())):
        l = lv_map.get(name)
        r = rv_map.get(name)
        if l is None and r is not None:
            status = "added"
        elif r is None and l is not None:
            status = "removed"
        elif l == r:
            status = "same"
        else:
            status = "changed"
        counts[status] += 1
        entry: dict[str, Any] = {"status": status}
        if status == "changed":
            entry["left"] = l
            entry["right"] = r
        out[name] = entry
    return out, counts


def compare_versions(paths: WorkspacePaths, sid: str, *,
                     left: str, right: str) -> dict[str, Any]:
    """Return a structured diff between two strategy version snapshots.

    The diff is split by scope — ``files`` (yaml) and ``prompts`` (agent
    markdown) — mirroring :func:`build_snapshot`. Each leaf entry
    carries one of four statuses: ``added``, ``removed``, ``changed``,
    ``same``. An aggregate ``summary`` totals the counts across scopes
    so the operator UI can render a single badge.
    """
    lv = get_version(paths, sid, left)
    rv = get_version(paths, sid, right)
    if lv is None:
        raise ValueError(f"unknown version {left!r} for strategy {sid!r}")
    if rv is None:
        raise ValueError(f"unknown version {right!r} for strategy {sid!r}")

    files, fc = _diff_scope(lv.snapshot.get("files") or {},
                            rv.snapshot.get("files") or {})
    prompts, pc = _diff_scope(lv.snapshot.get("prompts") or {},
                              rv.snapshot.get("prompts") or {})
    bindings, bc = _diff_scope(lv.snapshot.get("bindings") or {},
                               rv.snapshot.get("bindings") or {})
    summary = {k: fc[k] + pc[k] + bc[k] for k in fc}
    return {
        "strategy_id": sid,
        "left": {
            "version_id": lv.version_id,
            "status": lv.status,
            "created_at": lv.created_at,
            "reason": lv.reason,
            "author": lv.author,
        },
        "right": {
            "version_id": rv.version_id,
            "status": rv.status,
            "created_at": rv.created_at,
            "reason": rv.reason,
            "author": rv.author,
        },
        "files": files,
        "prompts": prompts,
        "bindings": bindings,
        "summary": summary,
    }


__all__ = [
    "StrategyVersion",
    "PromotionRecord",
    "build_snapshot",
    "apply_snapshot",
    "record_version",
    "list_versions",
    "get_version",
    "active_version_id",
    "record_promotion",
    "rollback_to",
    "list_promotions",
    "compare_versions",
]
