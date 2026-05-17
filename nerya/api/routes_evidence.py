"""HTTP routes for the Trading Evidence Vault.

Routes:

- ``GET  /evidence/sources``                 - per-source rollup
- ``GET  /evidence/topics``                  - per-topic rollup
- ``GET  /evidence/search?q=...``            - text + source/topic filters
- ``GET  /evidence/get?id=...``              - single doc fetch
- ``GET  /evidence/topic?topic=...``         - alias of search filtered by topic
- ``POST /evidence/ingest/run``              - synthetic demo ingestion

ACL: ``/evidence/search`` honors ``strategy_id`` / ``session_id`` query
params so strategy-private and session-private evidence does not leak
into shared queries.
"""

from __future__ import annotations

from typing import Any

from ..evidence import ingest as ev_ingest
from ..evidence.store import open_store
from ..runtime import feature_flags as ff
from ._envelope import action, blocked, debug_ref, ok, source_ref


_FLAG = "runtime.evidence_vault"


def _gated(client):
    if ff.is_enabled(client, _FLAG):
        return None
    return blocked(
        "Evidence vault is disabled via feature flag",
        data={"flag": _FLAG},
        debug_refs=[debug_ref("flag", _FLAG)],
    )


def _sources_handler(client, _payload):
    g = _gated(client)
    if g is not None:
        return g
    store = open_store(client)
    rows = store.list_sources()
    return ok(
        f"{len(rows)} source(s) in evidence vault",
        data={"sources": rows, "count": len(rows)},
        debug_refs=[debug_ref("module", "evidence.store")],
    )


def _topics_handler(client, _payload):
    g = _gated(client)
    if g is not None:
        return g
    store = open_store(client)
    rows = store.topics()
    return ok(
        f"{len(rows)} topic(s) tracked",
        data={"topics": rows, "count": len(rows)},
    )


def _search_handler(client, query):
    g = _gated(client)
    if g is not None:
        return g
    q = query if isinstance(query, dict) else {}
    text = str(q.get("q") or q.get("query") or "").strip()
    src = str(q.get("source_type") or "").strip()
    topic = str(q.get("topic") or "").strip()
    scope = str(q.get("scope") or "any").strip()
    strategy_id = q.get("strategy_id") or None
    session_id = q.get("session_id") or None
    try:
        limit = max(1, min(200, int(q.get("limit") or 50)))
    except Exception:
        limit = 50
    store = open_store(client)
    results = store.search(
        query=text,
        source_type=src,
        topic=topic,
        scope=scope,
        strategy_id=strategy_id,
        session_id=session_id,
        limit=limit,
    )
    return ok(
        f"{len(results)} match(es) for q={text!r}",
        data={"results": results, "count": len(results), "query": text},
    )


def _get_handler(client, query):
    g = _gated(client)
    if g is not None:
        return g
    q = query if isinstance(query, dict) else {}
    eid = str(q.get("id") or q.get("evidence_id") or "").strip()
    if not eid:
        return {"ok": False, "error": "id_required", "_status": 400}
    store = open_store(client)
    rec = store.get(eid)
    if rec is None:
        return {"ok": False, "error": "not_found", "id": eid, "_status": 404}
    return ok(
        f"evidence {eid}",
        data={"evidence": rec},
        source_refs=[source_ref("evidence", eid)],
    )


def _topic_handler(client, query):
    g = _gated(client)
    if g is not None:
        return g
    q = query if isinstance(query, dict) else {}
    topic = str(q.get("topic") or "").strip()
    if not topic:
        return {"ok": False, "error": "topic_required", "_status": 400}
    store = open_store(client)
    rows = store.search(topic=topic, limit=200)
    return ok(
        f"{len(rows)} doc(s) for topic={topic}",
        data={"topic": topic, "results": rows, "count": len(rows)},
    )


def _ingest_run_handler(client, payload):
    """Emit a synthetic demo batch.

    Useful for operator smoke runs and HTTP verification. Production
    ingestion is wired from
    the strategy/trading/gateway subsystems themselves.
    """
    g = _gated(client)
    if g is not None:
        return g
    body = payload or {}
    kind = str(body.get("kind") or "demo").lower()
    store = open_store(client)
    if kind == "demo":
        docs = ev_ingest.run_synthetic_demo(store)
        return ok(
            f"emitted {len(docs)} demo evidence doc(s)",
            data={"docs": [d.as_dict() for d in docs]},
            primary_action=action(
                id="open_evidence",
                label="Open Evidence",
                href="/memory?tab=evidence",
            ),
        )
    # Custom ingest payload — used by integration tests.
    src_type = str(body.get("source_type") or "research")
    src_id = str(body.get("source_id") or "manual")
    title = str(body.get("title") or "manual evidence")
    summary = str(body.get("summary") or "")
    text = str(body.get("body") or summary)
    tags = body.get("tags") or []
    scope = str(body.get("scope") or "shared")
    strategy_id = body.get("strategy_id")
    session_id = body.get("session_id")
    try:
        doc = store.ingest(
            source_type=src_type,
            source_id=src_id,
            title=title,
            summary=summary,
            body=text,
            tags=list(tags) if isinstance(tags, list) else [],
            scope=scope,
            strategy_id=strategy_id,
            session_id=session_id,
            route="POST /evidence/ingest/run",
            created_by="operator",
        )
    except ValueError as exc:
        return {"ok": False, "error": str(exc), "_status": 400}
    return ok(
        "evidence ingested",
        data={"doc": doc.as_dict()},
    )


def routes():
    return [
        ("GET", "/evidence/sources", _sources_handler),
        ("GET", "/evidence/topics", _topics_handler),
        ("GET", "/evidence/search", _search_handler),
        ("GET", "/evidence/get", _get_handler),
        ("GET", "/evidence/topic", _topic_handler),
        ("POST", "/evidence/ingest/run", _ingest_run_handler),
    ]
