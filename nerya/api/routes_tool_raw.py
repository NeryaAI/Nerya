"""HTTP routes for the durable raw-tool-result store.

The original payload of a compacted tool result must remain fetchable so
operators and downstream skills can audit what the LLM saw. These routes
expose the on-disk store backing
:mod:`nerya.llm.tool_raw_store`.

Endpoints
~~~~~~~~~

- ``GET /runtime/tool_raw?ref=raw://YYYY-MM-DD/<tool_use_id>``
    Fetch the original payload. Also accepts the legacy ``call:<id>``
    shape for backward compatibility with refs emitted before this
    store was wired up.
- ``GET /runtime/tool_raw/list?limit=50``
    List recently-stored records (metadata only — no payload bodies).
"""

from __future__ import annotations

from ..llm.tool_raw_store import open_store
from ..runtime import feature_flags as ff
from ._envelope import blocked, debug_ref, ok


_FLAG = "runtime.tool_result_compaction"


def _gated(client):
    if ff.is_enabled(client, _FLAG):
        return None
    return blocked(
        "Tool result compaction (and its raw-result store) is disabled via feature flag",
        data={"flag": _FLAG},
        debug_refs=[debug_ref("flag", _FLAG)],
    )


def _get_handler(client, query):
    g = _gated(client)
    if g is not None:
        return g
    q = query if isinstance(query, dict) else {}
    ref = str(q.get("ref") or q.get("raw_ref") or "").strip()
    if not ref:
        return {"ok": False, "error": "ref_required", "_status": 400}
    store = open_store(client)
    rec = store.read(ref)
    if rec is None:
        return {"ok": False, "error": "not_found", "ref": ref, "_status": 404}
    return ok(
        f"raw result {rec.tool_use_id} ({rec.size_bytes} bytes)",
        data={"record": rec.as_dict()},
        debug_refs=[debug_ref("module", "llm.tool_raw_store")],
    )


def _list_handler(client, query):
    g = _gated(client)
    if g is not None:
        return g
    q = query if isinstance(query, dict) else {}
    try:
        limit = max(1, min(500, int(q.get("limit") or 50)))
    except Exception:
        limit = 50
    rows = open_store(client).list_recent(limit=limit)
    return ok(
        f"{len(rows)} raw record(s)",
        data={"records": rows, "count": len(rows)},
    )


def routes():
    return [
        ("GET", "/runtime/tool_raw", _get_handler),
        ("GET", "/runtime/tool_raw/list", _list_handler),
    ]
