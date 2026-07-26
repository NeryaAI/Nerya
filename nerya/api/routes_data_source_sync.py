"""HTTP routes for Unified Data Source Sync State.

Routes:

- ``GET  /data-sources``                  - same as /data-sources/status
- ``GET  /data-sources/status``           - per-source freshness rollup
- ``POST /data-sources/sync-now``         - force-sync a source ``{"source_id": ...}``
- ``GET  /data-sources/events``           - recent sync events (limit query)

These reuse the in-repo ``nerya.data_sources.sync_state`` ledger so the
state is persistent across server restarts. Routes return the canonical
operator envelope so the dashboard renders them uniformly.
"""

from __future__ import annotations


from ..data_sources import sync_state as ss
from ..runtime import feature_flags as ff
from ._envelope import action, blocked, debug_ref, ok, source_ref


_FLAG = "runtime.data_source_sync_state"


def _gated(client):
    if ff.is_enabled(client, _FLAG):
        return None
    return blocked(
        "Data source sync state is disabled via feature flag",
        data={"flag": _FLAG},
        debug_refs=[debug_ref("flag", _FLAG)],
    )


def _status_handler(client, _payload):
    g = _gated(client)
    if g is not None:
        return g
    summary = ss.summarize(client)
    stale = summary.get("stale_count", 0)
    total = summary.get("total", 0)
    if stale:
        env = ok(
            f"{stale} of {total} data source(s) are stale",
            data=summary,
            primary_action=action(
                id="open_settings",
                label="Open data settings",
                href="/settings?section=integrations",
            ),
        )
    else:
        env = ok(
            f"All {total} data source(s) are fresh",
            data=summary,
        )
    env["debug_refs"] = [debug_ref("module", "data_sources.sync_state")]
    return env


def _sync_now_handler(client, payload):
    g = _gated(client)
    if g is not None:
        return g
    body = payload or {}
    sid = str(body.get("source_id") or "").strip()
    if not sid:
        return {
            "ok": False,
            "error": "source_id_required",
            "_status": 400,
        }
    result = ss.sync_now(client, sid)
    note = result.get("note") if isinstance(result, dict) else None
    env = ok(
        f"sync triggered for {sid}" + (f" ({note})" if note else ""),
        data={"result": result},
        source_refs=[source_ref("data_source", sid)],
    )
    return env


def _events_handler(client, query):
    g = _gated(client)
    if g is not None:
        return g
    q = query if isinstance(query, dict) else {}
    try:
        limit = max(1, int(q.get("limit") or 64))
    except Exception:
        limit = 64
    events = ss.events(client, limit=limit)
    return ok(
        f"{len(events)} recent event(s)",
        data={"events": events, "count": len(events)},
    )


def routes():
    return [
        ("GET", "/data-sources", _status_handler),
        ("GET", "/data-sources/status", _status_handler),
        ("POST", "/data-sources/sync-now", _sync_now_handler),
        ("GET", "/data-sources/events", _events_handler),
    ]
