"""HTTP routes for the Runtime Capability Catalog.

Routes:

- ``GET  /capabilities/catalog``               - full catalog (sorted)
- ``GET  /capabilities/catalog/{capability_id}`` - single entry
- ``GET  /capabilities/readiness``             - blocked/degraded rollup

These are read-only and never include plaintext secrets. The dashboard
uses them to render readiness cards in Operator Overview and to explain
why a capability is blocked.
"""

from __future__ import annotations

from typing import Any

from ..runtime import capability_catalog as cc
from ..runtime import feature_flags as ff
from ._envelope import action, blocked, debug_ref, ok, source_ref


_FLAG = "runtime.capability_catalog_v2"


def _gated(client):
    if ff.is_enabled(client, _FLAG):
        return None
    return blocked(
        "Capability catalog is disabled via feature flag",
        data={"flag": _FLAG},
        debug_refs=[debug_ref("flag", _FLAG)],
    )


def _catalog_handler(client, _payload):
    g = _gated(client)
    if g is not None:
        return g
    entries = cc.build_catalog(client)
    return ok(
        f"{len(entries)} capabilities catalogued",
        data={"entries": [e.as_dict() for e in entries], "count": len(entries)},
        debug_refs=[debug_ref("module", "runtime.capability_catalog")],
    )


def _readiness_handler(client, _payload):
    g = _gated(client)
    if g is not None:
        return g
    summary = cc.readiness(client)
    blocked = summary.get("blocked") or []
    degraded = summary.get("degraded") or []
    if blocked:
        env = ok(
            f"{len(blocked)} blocked, {len(degraded)} degraded out of {summary.get('total', 0)}",
            data=summary,
            primary_action=action(
                id="open_inbox",
                label="Open Action Inbox",
                href="/inbox",
            ),
        )
    elif degraded:
        env = ok(
            f"{len(degraded)} degraded out of {summary.get('total', 0)}",
            data=summary,
            primary_action=action(
                id="open_settings",
                label="Open Settings",
                href="/settings",
            ),
        )
    else:
        env = ok(
            f"All {summary.get('total', 0)} capabilities ready",
            data=summary,
        )
    env["debug_refs"] = [debug_ref("module", "runtime.capability_catalog.readiness")]
    return env


def _catalog_entry_handler(client, query):
    """Single entry lookup. Uses ``id`` from query string.

    Local HTTP server does not parse path params; the dashboard reaches
    ``/capabilities/catalog?id=skill.research_skill``.
    """

    g = _gated(client)
    if g is not None:
        return g
    cap_id = ""
    if isinstance(query, dict):
        cap_id = str(query.get("id") or "").strip()
    if not cap_id:
        return {
            "ok": False,
            "error": "id_required",
            "detail": "Pass ?id=<capability_id>",
            "_status": 400,
        }
    entry = cc.find(client, cap_id)
    if entry is None:
        return {
            "ok": False,
            "error": "not_found",
            "id": cap_id,
            "_status": 404,
        }
    return ok(
        f"capability {cap_id}",
        data={"entry": entry.as_dict()},
        source_refs=[source_ref("capability", cap_id)],
    )


def routes():
    return [
        ("GET", "/capabilities/catalog", _catalog_handler),
        ("GET", "/capabilities/entry", _catalog_entry_handler),
        ("GET", "/capabilities/readiness", _readiness_handler),
    ]
