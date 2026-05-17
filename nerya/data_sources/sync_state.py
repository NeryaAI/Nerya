"""Persistent data-source sync state.

Tracks per-source sync state in
``workspace/state/data_sources/sync_state.json`` and exposes a small set
of pure functions the HTTP routes and capability catalog consume:

- :func:`summarize(client)` -> {"sources": [...], "stale_count": N, ...}
- :func:`mark_attempt(client, source_id, ...)` -> updates sync row
- :func:`mark_success(client, source_id, ...)` -> updates sync row, clears error
- :func:`record_event(client, source_id, kind, payload)` -> appends to events.jsonl
- :func:`events(client, limit=...)` -> returns recent events
- :func:`get(client, source_id)` -> returns the row or None
- :func:`sync_now(client, source_id)` -> calls registered contributor or returns "noop"
"""

from __future__ import annotations

import json
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Optional


_DEFAULT_SLA = 300  # 5 minutes
_EVENTS_LIMIT = 256


@dataclass
class SyncBudget:
    daily_limit: int = 0
    used_today: int = 0


@dataclass
class SyncRow:
    source_id: str
    kind: str = "generic"
    provider: str = ""
    account_id: str = ""
    enabled: bool = True
    last_success_at: str = ""
    last_attempt_at: str = ""
    next_due_at: str = ""
    cursor: str = ""
    freshness_sla_seconds: int = _DEFAULT_SLA
    budget: SyncBudget = field(default_factory=SyncBudget)
    last_error: Optional[str] = None
    stale: bool = False

    def as_dict(self) -> dict[str, Any]:
        out = asdict(self)
        out["budget"] = asdict(self.budget)
        return out


def _state_dir(client) -> Path:
    paths = client.config.paths
    return paths.root / "state" / "data_sources"


def _state_file(client) -> Path:
    return _state_dir(client) / "sync_state.json"


def _events_file(client) -> Path:
    return _state_dir(client) / "events.jsonl"


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse_iso(s: str) -> Optional[float]:
    if not s:
        return None
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp()
    except Exception:
        return None


def _load(client) -> dict[str, SyncRow]:
    path = _state_file(client)
    if not path.exists():
        return {}
    try:
        raw = json.loads(path.read_text(encoding="utf-8") or "{}")
    except Exception:
        return {}
    if not isinstance(raw, dict):
        return {}
    out: dict[str, SyncRow] = {}
    for sid, blob in raw.items():
        if not isinstance(blob, dict):
            continue
        budget_blob = blob.get("budget") or {}
        out[sid] = SyncRow(
            source_id=sid,
            kind=str(blob.get("kind") or "generic"),
            provider=str(blob.get("provider") or ""),
            account_id=str(blob.get("account_id") or ""),
            enabled=bool(blob.get("enabled", True)),
            last_success_at=str(blob.get("last_success_at") or ""),
            last_attempt_at=str(blob.get("last_attempt_at") or ""),
            next_due_at=str(blob.get("next_due_at") or ""),
            cursor=str(blob.get("cursor") or ""),
            freshness_sla_seconds=int(blob.get("freshness_sla_seconds") or _DEFAULT_SLA),
            budget=SyncBudget(
                daily_limit=int(budget_blob.get("daily_limit") or 0),
                used_today=int(budget_blob.get("used_today") or 0),
            ),
            last_error=blob.get("last_error"),
            stale=False,  # recomputed
        )
    return out


def _save(client, rows: dict[str, SyncRow]) -> None:
    path = _state_file(client)
    path.parent.mkdir(parents=True, exist_ok=True)
    blob = {sid: row.as_dict() for sid, row in rows.items()}
    path.write_text(json.dumps(blob, ensure_ascii=False, indent=2), encoding="utf-8")


def _refresh_stale(rows: Iterable[SyncRow]) -> None:
    now = time.time()
    for row in rows:
        last_success = _parse_iso(row.last_success_at)
        if last_success is None:
            row.stale = row.enabled
            continue
        row.stale = (now - last_success) > row.freshness_sla_seconds


# ---------------------------------------------------------------------------
# Default contributors — minimal, defensive
# ---------------------------------------------------------------------------


def _default_seed(client) -> list[SyncRow]:
    """Built-in known sources that the platform always tracks.

    We intentionally avoid registering accounts/strategies dynamically here.
    Callers from trading subsystems should call :func:`mark_attempt` /
    :func:`mark_success` to keep the runtime ledger fresh.
    """

    cfg = client.config
    seed: list[SyncRow] = []
    seed.append(SyncRow(
        source_id="memory:notebook",
        kind="memory_provider",
        provider="filesystem",
        freshness_sla_seconds=3600,
        enabled=True,
    ))
    seed.append(SyncRow(
        source_id="llm:model_catalog",
        kind="model_provider",
        provider="registry",
        freshness_sla_seconds=86400,
        enabled=True,
    ))
    seed.append(SyncRow(
        source_id="gateway:platforms",
        kind="gateway",
        provider="registry",
        freshness_sla_seconds=600,
        enabled=bool(cfg.get("messaging.platforms")),
    ))
    return seed


def _seeded_rows(client) -> dict[str, SyncRow]:
    rows = _load(client)
    for s in _default_seed(client):
        rows.setdefault(s.source_id, s)
    return rows


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def summarize(client) -> dict[str, Any]:
    rows = _seeded_rows(client)
    _refresh_stale(rows.values())
    stale = [r for r in rows.values() if r.stale]
    return {
        "sources": [r.as_dict() for r in sorted(rows.values(),
                                                 key=lambda r: r.source_id)],
        "total": len(rows),
        "stale_count": len(stale),
        "stale_ids": [r.source_id for r in stale],
        "generated_at": _now_iso(),
    }


def get(client, source_id: str) -> Optional[dict[str, Any]]:
    rows = _seeded_rows(client)
    _refresh_stale(rows.values())
    row = rows.get(source_id)
    if row is None:
        return None
    return row.as_dict()


def mark_attempt(
    client, source_id: str, *, kind: str = "generic", provider: str = "",
    account_id: str = "", freshness_sla_seconds: int = _DEFAULT_SLA,
    next_due_at: str = "",
) -> dict[str, Any]:
    rows = _load(client)
    row = rows.get(source_id) or SyncRow(source_id=source_id)
    row.kind = kind or row.kind
    row.provider = provider or row.provider
    row.account_id = account_id or row.account_id
    row.last_attempt_at = _now_iso()
    if freshness_sla_seconds:
        row.freshness_sla_seconds = freshness_sla_seconds
    if next_due_at:
        row.next_due_at = next_due_at
    rows[source_id] = row
    _save(client, rows)
    record_event(client, source_id, "attempt", {"kind": kind})
    return row.as_dict()


def mark_success(
    client, source_id: str, *, cursor: str = "",
    next_due_at: str = "",
) -> dict[str, Any]:
    rows = _load(client)
    row = rows.get(source_id) or SyncRow(source_id=source_id)
    row.last_success_at = _now_iso()
    row.last_error = None
    if cursor:
        row.cursor = cursor
    if next_due_at:
        row.next_due_at = next_due_at
    row.enabled = True
    rows[source_id] = row
    _save(client, rows)
    record_event(client, source_id, "success", {"cursor": cursor})
    return row.as_dict()


def mark_error(
    client, source_id: str, *, message: str,
) -> dict[str, Any]:
    rows = _load(client)
    row = rows.get(source_id) or SyncRow(source_id=source_id)
    row.last_error = str(message)[:512]
    rows[source_id] = row
    _save(client, rows)
    record_event(client, source_id, "error", {"message": str(message)[:256]})
    return row.as_dict()


def set_enabled(client, source_id: str, enabled: bool) -> dict[str, Any]:
    rows = _load(client)
    row = rows.get(source_id) or SyncRow(source_id=source_id)
    row.enabled = bool(enabled)
    rows[source_id] = row
    _save(client, rows)
    record_event(client, source_id, "enabled" if enabled else "disabled", {})
    return row.as_dict()


def record_event(client, source_id: str, kind: str, payload: dict[str, Any]) -> None:
    path = _events_file(client)
    path.parent.mkdir(parents=True, exist_ok=True)
    rec = {
        "ts": _now_iso(),
        "source_id": source_id,
        "kind": kind,
        "payload": payload,
    }
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(rec, ensure_ascii=False) + "\n")


def events(client, *, limit: int = _EVENTS_LIMIT) -> list[dict[str, Any]]:
    path = _events_file(client)
    if not path.exists():
        return []
    lines = path.read_text(encoding="utf-8").splitlines()
    out: list[dict[str, Any]] = []
    for line in reversed(lines):
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except Exception:
            continue
        if len(out) >= limit:
            break
    return out


# In-process sync_now contributor registry. Subsystems can register a
# callable that takes (client, source_id) and returns a dict describing
# what was refreshed. Defaults to a marker handler that just records
# a fresh attempt so the operator can see the row updated even when no
# real provider is wired yet.
_SYNC_NOW_REGISTRY: dict[str, Callable[[Any, str], dict[str, Any]]] = {}


def register_sync_now(source_id: str, fn: Callable[[Any, str], dict[str, Any]]) -> None:
    _SYNC_NOW_REGISTRY[source_id] = fn


def sync_now(client, source_id: str) -> dict[str, Any]:
    fn = _SYNC_NOW_REGISTRY.get(source_id)
    if fn is not None:
        try:
            return fn(client, source_id) or {"ok": True, "source_id": source_id}
        except Exception as exc:  # pragma: no cover - defensive
            mark_error(client, source_id, message=str(exc))
            return {"ok": False, "source_id": source_id, "error": str(exc)}
    # marker handler: just touch the row so the operator sees movement
    mark_attempt(client, source_id, kind="manual")
    row = mark_success(client, source_id, cursor="manual")
    return {"ok": True, "source_id": source_id, "row": row, "note": "marker_only"}
