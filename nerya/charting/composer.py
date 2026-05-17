"""ChartBlock composer — the single entry point used by skills, kernel
hooks, and the dynamic-code path to build a validated
:class:`~nerya.agent.chart_block.ChartBlock` and (when the Agent picked
``path="bulk"``) persist the heavy payload to ``artifacts/charts/<id>.json``.

Design contract
---------------

* **Path is the Agent's call.** ``path="inline"`` keeps every series'
  ``data`` inside the envelope. ``path="bulk"`` writes the payload to
  the workspace artifact store and replaces ``series.data`` with a
  ``nerya://chart/<id>`` URI. The composer never silently flips paths
  *unless* the inline payload exceeds
  :data:`~nerya.agent.chart_block.MAX_INLINE_BYTES`, in which case it
  auto-promotes to ``"bulk"`` and surfaces a warning so the dashboard
  can show why.

* **Bulk requires a context.** Persisting an artifact needs an
  :class:`ArtifactStore`. We don't reach for the global config here —
  callers (skills, kernel, dynamic SDK) supply a :class:`BulkContext`
  explicitly. This keeps the composer pure for inline calls and
  testable in isolation.

* **chart_id is stable when callers want it.** ``stable_chart_id`` in
  :mod:`nerya.agent.chart_block` derives an id from
  ``(skill, action, payload)``. Callers that pass ``chart_id`` directly
  override the random default, which is required for dedup across
  turns / sessions.

* **One file owns the artifact name pattern.** Both the composer and
  the HTTP reader use :func:`chart_artifact_path`, so any future
  versioning (e.g. ``v1/<id>.json``) flips here.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Literal, Optional

from ..agent.chart_block import (
    MAX_INLINE_BYTES,
    ChartBlock,
    ChartOverlay,
    ChartPane,
    ChartSeries,
    ChartSource,
    ChartTime,
    ChartUI,
    estimate_inline_bytes,
    make_chart_id,
    stable_chart_id,
    validate_chart_block,
)


__all__ = [
    "BUILD_CONTEXT_REQUIRED_FOR_BULK",
    "BulkContext",
    "build_chart_block",
    "chart_artifact_path",
    "load_chart_artifact",
    "persist_chart_artifact",
]


# Sentinel string used by tests to confirm the error path.
BUILD_CONTEXT_REQUIRED_FOR_BULK = (
    "build_chart_block(path='bulk') requires a BulkContext "
    "(pass ctx=BulkContext(artifact_store=...))"
)


_CHARTS_KIND = "charts"


@dataclass
class BulkContext:
    """Glue object the composer needs to persist a bulk payload.

    Skills receive an ``ArtifactStore`` from the kernel; tests construct
    one directly. The dynamic-code path passes one through the SDK's
    ``charts.publish`` server handler. The composer never reaches into
    config or globals.
    """

    artifact_store: Any  # nerya.workspace.artifact_store.ArtifactStore
    journal: Optional[Any] = None  # callable taking a dict, optional


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def chart_artifact_path(artifact_store: Any, chart_id: str) -> Path:
    """Where on disk a chart artifact lives. Useful for tests + HTTP."""

    if not chart_id:
        raise ValueError("chart_id must be a non-empty string")
    if "/" in chart_id or ".." in chart_id:
        # Defensive — chart_id flows from external callers (Agent /
        # dynamic code). Reject directory traversal at the seam.
        raise ValueError(f"chart_id contains illegal characters: {chart_id!r}")
    return artifact_store.paths.artifacts / _CHARTS_KIND / f"{chart_id}.json"


def persist_chart_artifact(
    artifact_store: Any,
    chart_id: str,
    payload: dict[str, Any],
) -> str:
    """Write ``payload`` to ``artifacts/charts/<chart_id>.json`` and return
    the canonical ``nerya://chart/<chart_id>`` URI."""

    body = json.dumps(payload, default=str, ensure_ascii=False)
    artifact_store.put_text(_CHARTS_KIND, f"{chart_id}.json", body)
    return f"nerya://chart/{chart_id}"


def load_chart_artifact(artifact_store: Any, chart_id: str) -> Optional[dict[str, Any]]:
    """Read back what :func:`persist_chart_artifact` wrote.

    Returns ``None`` for missing artifacts so the HTTP layer can map to
    a 404 without raising. JSON decode failures bubble up — those are
    bugs, not user-facing errors.
    """

    target = chart_artifact_path(artifact_store, chart_id)
    if not target.exists():
        return None
    return json.loads(target.read_text(encoding="utf-8"))


def _coerce_series(raw: Iterable[Any]) -> list[ChartSeries]:
    out: list[ChartSeries] = []
    for item in raw:
        if isinstance(item, ChartSeries):
            out.append(item)
            continue
        if isinstance(item, dict):
            out.append(ChartSeries(**item))
            continue
        raise TypeError(
            f"series entries must be ChartSeries or dict, got {type(item).__name__}"
        )
    return out


def _coerce_overlays(raw: Iterable[Any]) -> list[ChartOverlay]:
    out: list[ChartOverlay] = []
    for item in raw:
        if isinstance(item, ChartOverlay):
            out.append(item)
        elif isinstance(item, dict):
            # ``from`` / ``to`` are reserved keywords on the wire format
            # but Python ChartOverlay uses ``from_time`` / ``to_time``.
            normalized = dict(item)
            if "from" in normalized and "from_time" not in normalized:
                normalized["from_time"] = normalized.pop("from")
            if "to" in normalized and "to_time" not in normalized:
                normalized["to_time"] = normalized.pop("to")
            out.append(ChartOverlay(**normalized))
        else:
            raise TypeError(
                f"overlay entries must be ChartOverlay or dict, got {type(item).__name__}"
            )
    return out


def _coerce_panes(raw: Iterable[Any]) -> list[ChartPane]:
    out: list[ChartPane] = []
    for item in raw:
        if isinstance(item, ChartPane):
            out.append(item)
        elif isinstance(item, dict):
            out.append(ChartPane(**item))
        else:
            raise TypeError(
                f"pane entries must be ChartPane or dict, got {type(item).__name__}"
            )
    return out


def _coerce_source(raw: Any) -> ChartSource:
    if isinstance(raw, ChartSource):
        return raw
    if isinstance(raw, dict):
        return ChartSource(**raw)
    raise TypeError(
        f"source must be ChartSource or dict, got {type(raw).__name__}"
    )


def _coerce_time(raw: Any) -> ChartTime:
    if raw is None:
        return ChartTime()
    if isinstance(raw, ChartTime):
        return raw
    if isinstance(raw, dict):
        return ChartTime(**raw)
    raise TypeError(
        f"time must be ChartTime or dict, got {type(raw).__name__}"
    )


def _coerce_ui(raw: Any) -> Optional[ChartUI]:
    if raw is None:
        return None
    if isinstance(raw, ChartUI):
        return raw
    if isinstance(raw, dict):
        return ChartUI(**raw)
    raise TypeError(f"ui must be ChartUI or dict, got {type(raw).__name__}")


# ---------------------------------------------------------------------------
# Composer entry point
# ---------------------------------------------------------------------------


def build_chart_block(
    *,
    chart_kind: str,
    title: str,
    series: Iterable[Any],
    source: Any,
    path: Literal["inline", "bulk"] = "inline",
    ctx: Optional[BulkContext] = None,
    chart_id: Optional[str] = None,
    subtitle: Optional[str] = None,
    overlays: Iterable[Any] = (),
    panes: Iterable[Any] = (),
    time: Any = None,
    default_range: Optional[dict[str, Any]] = None,
    caption: Optional[str] = None,
    insights: Iterable[str] = (),
    ui: Any = None,
) -> ChartBlock:
    """Build a validated :class:`ChartBlock` from raw series + metadata.

    Parameters
    ----------
    path:
        ``"inline"`` (default) keeps ``series.data`` inside the
        envelope. ``"bulk"`` writes to the workspace artifact store
        and replaces ``series.data`` with ``data_uri`` placeholders.
    ctx:
        Required when ``path="bulk"`` (or when an inline payload
        exceeds :data:`MAX_INLINE_BYTES` and the composer auto-promotes
        to bulk). Holds the :class:`ArtifactStore` and an optional
        journal sink.
    chart_id:
        Override the random default. Use :func:`stable_chart_id` for
        deduplicating identical artifacts across turns.

    The returned block has been ``validate_chart_block``-checked; any
    schema errors raise :class:`ValueError` with the full error list so
    misuse fails loudly instead of corrupting transcripts.
    """

    if path not in ("inline", "bulk"):
        raise ValueError(f"path must be 'inline' or 'bulk', got {path!r}")

    series_list = _coerce_series(series)
    if not series_list:
        raise ValueError("series must contain at least one entry")
    overlays_list = _coerce_overlays(overlays)
    panes_list = _coerce_panes(panes)
    source_obj = _coerce_source(source)
    time_obj = _coerce_time(time)
    ui_obj = _coerce_ui(ui)

    block = ChartBlock(
        chart_id=chart_id or _derive_chart_id(source_obj, series_list),
        chart_kind=chart_kind,  # type: ignore[arg-type]
        title=title,
        subtitle=subtitle,
        series=series_list,
        overlays=overlays_list,
        panes=panes_list,
        time=time_obj,
        default_range=default_range,
        source=source_obj,
        caption=caption,
        insights=list(insights),
        warnings=[],
        ui=ui_obj,
        path=path,  # type: ignore[arg-type]
    )

    if path == "bulk":
        if ctx is None:
            raise ValueError(BUILD_CONTEXT_REQUIRED_FOR_BULK)
        _persist_to_bulk(block, ctx)
    else:
        # Auto-promote oversized inline payloads to keep the daemon /
        # LLM context safe. Document the action in ``warnings`` so the
        # operator can see *why* their inline ask got rerouted.
        size = estimate_inline_bytes(block)
        if size > MAX_INLINE_BYTES:
            if ctx is None:
                raise ValueError(
                    f"inline payload of {size} bytes exceeds MAX_INLINE_BYTES "
                    f"({MAX_INLINE_BYTES}); pass ctx=BulkContext(...) so the "
                    "composer can auto-promote to bulk, or shrink the data."
                )
            block.warnings.append(
                f"inline payload {size} bytes exceeded {MAX_INLINE_BYTES}; "
                "auto-promoted to bulk path"
            )
            block.path = "bulk"
            _persist_to_bulk(block, ctx)

    errors = validate_chart_block(block)
    if errors:
        raise ValueError("invalid chart block: " + "; ".join(errors))
    return block


def _derive_chart_id(source: ChartSource, series: list[ChartSeries]) -> str:
    """Default chart_id strategy.

    If both ``skill`` and ``action`` are populated we hash the series
    data so the id is stable across re-runs of the same skill query.
    Otherwise we fall back to a random uuid — required for ad-hoc
    Agent-authored inline blocks where there's nothing meaningful to
    hash on.
    """

    if source.skill and source.action:
        # Hash an ordered fingerprint of (series name, point count, first/last
        # point) — enough to detect "identical query, identical result"
        # without iterating thousands of points.
        fingerprint = []
        for s in series:
            data_len = len(s.data) if s.data else 0
            head = s.data[0] if s.data else None
            tail = s.data[-1] if s.data else None
            fingerprint.append(
                {"name": s.name, "type": s.type, "n": data_len, "head": head, "tail": tail}
            )
        return stable_chart_id(source.skill, source.action, fingerprint)
    return make_chart_id()


def _persist_to_bulk(block: ChartBlock, ctx: BulkContext) -> None:
    """Move every inline series.data into ``artifacts/charts/<id>.json``
    and rewrite the block to reference it via ``bulk_data_uri``."""

    series_payload: list[dict[str, Any]] = []
    has_inline_data = False
    for s in block.series:
        if s.data is not None:
            has_inline_data = True
            series_payload.append({"name": s.name, "type": s.type, "data": s.data})
            s.data = None
            s.data_uri = f"nerya://chart/{block.chart_id}#series/{s.name}"
    if not has_inline_data and not block.bulk_data_uri:
        # Caller asked for bulk but provided no data — nothing to
        # persist. Surface as a validation error so downstream doesn't
        # produce an empty artifact silently.
        raise ValueError(
            "bulk path requested but every series already had data_uri set "
            "and no inline data was provided"
        )
    if has_inline_data:
        artifact_payload = {
            "chart_id": block.chart_id,
            "title": block.title,
            "series": series_payload,
            "as_of": block.source.as_of,
        }
        block.bulk_data_uri = persist_chart_artifact(
            ctx.artifact_store, block.chart_id, artifact_payload
        )
        if ctx.journal is not None:
            try:
                ctx.journal(
                    {
                        "kind": "chart.artifact.persist",
                        "chart_id": block.chart_id,
                        "bulk_data_uri": block.bulk_data_uri,
                        "size_hint_bytes": len(json.dumps(artifact_payload, default=str)),
                    }
                )
            except Exception:
                # Journal is observability — never fail a chart build
                # because a journal sink misbehaved.
                pass
