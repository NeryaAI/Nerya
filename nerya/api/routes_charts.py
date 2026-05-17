"""HTTP surface for the workspace's chart artifacts.

The dashboard's ``ChartBlock`` component renders both ``inline`` and
``bulk`` chart blocks. ``inline`` carries its own ``series.data`` so it
needs no HTTP. ``bulk`` blocks reference an artifact via
``nerya://chart/<id>``; this module is what serves that artifact back.

Routes
------

``GET /charts/get?id=<chart_id>``
    Returns the JSON payload that the composer wrote to
    ``artifacts/charts/<chart_id>.json``. Shape::

        {
          "ok": true,
          "chart_id": "<id>",
          "payload": {
            "chart_id": "<id>",
            "title": "...",
            "series": [{"name": "...", "type": "...", "data": [...]}, ...],
            "as_of": "..."
          }
        }

    On miss the route returns ``{"ok": False, "error": "not_found",
    "chart_id": "<id>"}`` (HTTP status remains 200; this matches the
    existing local_server pattern of returning errors as JSON envelopes
    so the dashboard fetcher can branch on ``ok`` rather than parsing
    HTTP status codes).

``POST /charts/publish``
    The dynamic-code path's deposit slot. Agent-authored Python uses
    ``client.charts.publish(chart_block_dict)`` to persist a chart's
    series into ``artifacts/charts/<id>.json`` and pull a stable
    ``chart_id`` back. The script then prints the
    ``@@nerya:chart@@ <id>`` stdout marker; the kernel's
    :func:`nerya.agent.chart_hook.extract_chart_marker_ids` picks it up
    and splices the chart envelope into the chat next to the
    originating ``run_shell`` call. Request body::

        {"chart_block": <ChartBlock dict, path='inline' or 'bulk'>}

    The handler runs the dict through
    :func:`nerya.charting.composer.build_chart_block` (path forced to
    ``"bulk"`` so we always persist) and replies with::

        {"ok": true, "chart_id": "...", "bulk_data_uri": "nerya://chart/<id>"}
"""

from __future__ import annotations

from typing import Any

from ..agent.chart_block import ChartBlock, ChartSeries, ChartSource, validate_chart_block
from ..charting import build_chart_block, load_chart_artifact, persist_chart_artifact
from ..charting.composer import BulkContext
from ..workspace.artifact_store import ArtifactStore


def _store(client: Any) -> ArtifactStore:
    """Build an ArtifactStore from the client's workspace paths.

    Cheap to construct (just wraps a Path); we don't bother caching it
    on the client to avoid layering concerns.
    """

    return ArtifactStore(client.config.paths)


def _get(client: Any, query: dict[str, Any]) -> dict[str, Any]:
    chart_id = str(query.get("id") or "").strip()
    if not chart_id:
        return {"ok": False, "error": "id is required"}
    try:
        payload = load_chart_artifact(_store(client), chart_id)
    except ValueError as exc:
        # ``load_chart_artifact`` rejects ids with directory traversal or
        # other illegal characters via ``chart_artifact_path``.
        return {"ok": False, "error": "invalid_chart_id", "detail": str(exc)}
    if payload is None:
        return {"ok": False, "error": "not_found", "chart_id": chart_id}
    return {"ok": True, "chart_id": chart_id, "payload": payload}


def _post_publish(client: Any, payload: dict[str, Any]) -> dict[str, Any]:
    """Persist a chart_block authored by the dynamic-code path.

    The caller (typically a script invoked through ``run_shell``) sends
    a fully-shaped ``ChartBlock`` dict whose ``series`` carry inline
    ``data``. We force ``path="bulk"`` so the data lands on disk and
    return the canonical URI; the caller never sees the inline payload
    again.

    Validation is lenient on the wire keys (we use the composer's
    coercers) but strict on the shape — a malformed block returns a
    descriptive ``invalid_chart_block`` error rather than a 500.
    """

    if not isinstance(payload, dict):
        return {"ok": False, "error": "payload must be a JSON object"}
    raw_block = payload.get("chart_block")
    if not isinstance(raw_block, dict):
        return {
            "ok": False,
            "error": "chart_block missing or not an object",
        }

    # Required surface — these mirror the composer's positional kwargs.
    chart_kind = str(raw_block.get("chart_kind") or "line")
    title = str(raw_block.get("title") or "")
    series = raw_block.get("series") or []
    source = raw_block.get("source") or {"skill": "agent", "action": "publish"}
    if not isinstance(series, list) or not series:
        return {"ok": False, "error": "chart_block.series must be a non-empty list"}

    ctx = BulkContext(artifact_store=_store(client))
    try:
        block = build_chart_block(
            chart_kind=chart_kind,
            title=title,
            series=series,
            source=source,
            path="bulk",
            ctx=ctx,
            chart_id=raw_block.get("chart_id") or None,
            subtitle=raw_block.get("subtitle"),
            overlays=raw_block.get("overlays") or [],
            panes=raw_block.get("panes") or [],
            time=raw_block.get("time"),
            default_range=raw_block.get("default_range"),
            caption=raw_block.get("caption"),
            insights=raw_block.get("insights") or [],
            ui=raw_block.get("ui"),
        )
    except (TypeError, ValueError) as exc:
        return {
            "ok": False,
            "error": "invalid_chart_block",
            "detail": str(exc),
        }

    return {
        "ok": True,
        "chart_id": block.chart_id,
        "bulk_data_uri": block.bulk_data_uri or f"nerya://chart/{block.chart_id}",
        "chart_block": block.as_dict(),
    }


# Re-export so external callers (kernel marker hook, future
# diagnostics) can persist raw payloads without re-implementing the
# wire shape.
__all__ = [
    "routes",
    "ChartBlock",
    "ChartSeries",
    "ChartSource",
    "validate_chart_block",
    "persist_chart_artifact",
]


def routes():
    return [
        ("GET", "/charts/get", _get),
        ("POST", "/charts/publish", _post_publish),
    ]
