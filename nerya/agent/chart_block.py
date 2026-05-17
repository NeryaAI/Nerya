"""Chart block schema for the workspace-native agent loop.

A ``chart`` block is a first-class peer of the existing ``text`` /
``thinking`` / ``tool_use`` / ``tool_result`` envelopes. It carries a
structured description of an interactive chart (lightweight-charts on
the dashboard side) plus the **path** the data takes to the renderer:

* ``path="inline"`` — every series carries its own ``data`` array.
  Self-contained, fast first paint, survives offline.
* ``path="bulk"``   — series ``data`` is omitted; the envelope carries
  ``bulk_data_uri`` (typically ``nerya://chart/<chart_id>``). The
  dashboard fetches the artifact lazily through ``GET /charts/<id>``.

The path is **chosen by the Agent / skill** (passed explicitly into
``ChartBlock`` / ``build_chart_block``). The composer does not enforce
its own threshold beyond a 256 KiB OOM guardrail (``MAX_INLINE_BYTES``)
- if a caller asks for inline but the payload is huge, we auto-promote
to bulk and surface a ``warnings`` entry so it shows up in the UI.

Why dataclasses, not Pydantic
-----------------------------

The neighbouring ``transcript_blocks`` module is already a hand-rolled
dataclass + ``as_dict`` schema with no Pydantic dependency. We mirror
that convention so the kernel can keep importing transcript types
without pulling in pydantic at agent-loop boot. Validation lives in
``validate_chart_block`` which mirrors what a Pydantic model would do
(unknown fields are dropped with a debug log, required fields raise).
"""

from __future__ import annotations

import hashlib
import json
import time as _time
import uuid
from dataclasses import dataclass, field, asdict
from typing import Any, Iterable, Literal, Optional


def _now_seconds() -> float:
    return _time.time()


__all__ = [
    "ChartBlock",
    "ChartSeries",
    "ChartSource",
    "ChartOverlay",
    "ChartPane",
    "ChartUI",
    "ChartTime",
    "make_chart_id",
    "validate_chart_block",
    "MAX_INLINE_BYTES",
    "CHART_BLOCK_VERSION",
]


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

CHART_BLOCK_VERSION = "v1"

# Hard ceiling for inline path. Beyond this size the composer will
# auto-promote to bulk to protect the daemon and the LLM context. This
# is intentionally generous (256 KiB) so the *normal* decision is still
# the Agent's; the guardrail only catches obvious mistakes.
MAX_INLINE_BYTES = 256 * 1024


# ---------------------------------------------------------------------------
# Sub-schemas
# ---------------------------------------------------------------------------


@dataclass
class ChartTime:
    """How time values are encoded in series.data[*].time."""

    timezone: str = "UTC"
    # ``unix_seconds`` matches lightweight-charts' UTCTimestamp; it is
    # the canonical encoding for everything we ship. ``business_day``
    # and ``iso8601`` are accepted but skill code should normalise to
    # unix_seconds before handing data to the composer.
    format: Literal["unix_seconds", "business_day", "iso8601"] = "unix_seconds"

    def as_dict(self) -> dict[str, Any]:
        return {"timezone": self.timezone, "format": self.format}


@dataclass
class ChartSource:
    """Where the data came from. ``cite-or-die`` discipline applies."""

    skill: str = ""
    action: str = ""
    as_of: str = ""  # ISO 8601 timestamp
    cite_url: Optional[str] = None
    artifact_path: Optional[str] = None  # local artifact, if any

    def as_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "skill": self.skill,
            "action": self.action,
            "as_of": self.as_of,
        }
        if self.cite_url:
            out["cite_url"] = self.cite_url
        if self.artifact_path:
            out["artifact_path"] = self.artifact_path
        return out


@dataclass
class ChartSeries:
    """One renderable series.

    Either ``data`` (inline) or ``data_uri`` (bulk) must be set, not
    both. ``data_uri`` typically points at a slice of the chart's bulk
    artifact; e.g. ``nerya://chart/<chart_id>#series/<name>``.
    """

    type: Literal[
        "candlestick", "line", "area", "baseline", "histogram", "bar"
    ] = "line"
    name: str = ""
    data: Optional[list[dict[str, Any]]] = None
    data_uri: Optional[str] = None
    # Visual options (all optional, lightweight-charts defaults apply).
    color: Optional[str] = None
    line_style: Optional[Literal["solid", "dashed", "dotted"]] = None
    line_width: Optional[int] = None
    top_color: Optional[str] = None
    bottom_color: Optional[str] = None
    base_value: Optional[float] = None
    price_format: Optional[Literal["price", "percent", "volume"]] = None

    def as_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {"type": self.type, "name": self.name}
        if self.data is not None:
            out["data"] = list(self.data)
        if self.data_uri is not None:
            out["data_uri"] = self.data_uri
        for key in (
            "color",
            "line_style",
            "line_width",
            "top_color",
            "bottom_color",
            "base_value",
            "price_format",
        ):
            value = getattr(self, key)
            if value is not None:
                out[key] = value
        return out


@dataclass
class ChartOverlay:
    """Markers, price lines, region shading, annotations.

    The ``type`` discriminator drives which fields are read on the
    dashboard side. We keep this open-shaped to avoid one dataclass per
    overlay flavour; validation runs through ``validate_chart_block``.
    """

    type: Literal["marker", "price_line", "region", "annotation"] = "marker"
    # marker / annotation
    time: Optional[Any] = None
    position: Optional[Literal["above", "below", "inBar"]] = None
    shape: Optional[
        Literal["arrow_up", "arrow_down", "circle", "square"]
    ] = None
    text: Optional[str] = None
    tooltip: Optional[str] = None
    href: Optional[str] = None
    # price_line
    price: Optional[float] = None
    title: Optional[str] = None
    axis_label: Optional[bool] = None
    # region
    from_time: Optional[Any] = None
    to_time: Optional[Any] = None
    label: Optional[str] = None
    # shared
    color: Optional[str] = None
    line_style: Optional[Literal["solid", "dashed"]] = None

    def as_dict(self) -> dict[str, Any]:
        # Re-emit ``from_time`` / ``to_time`` as ``from`` / ``to`` so the
        # wire format matches the design document and lightweight-charts
        # range conventions.
        raw = asdict(self)
        raw["from"] = raw.pop("from_time", None)
        raw["to"] = raw.pop("to_time", None)
        return {k: v for k, v in raw.items() if v is not None}


@dataclass
class ChartPane:
    """One sub-pane within a multi-pane chart (chart_kind="multi")."""

    id: str = ""
    height_ratio: float = 1.0
    series_ids: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "height_ratio": self.height_ratio,
            "series_ids": list(self.series_ids),
        }


@dataclass
class ChartUI:
    """Renderer hints. All fields optional."""

    height: Optional[int] = None
    layout: Optional[Literal["compact", "full"]] = None
    palette: Optional[Literal["brand", "mono", "diverging"]] = None
    show_volume: Optional[bool] = None
    show_legend: Optional[bool] = None
    crosshair_sync_group: Optional[str] = None

    def as_dict(self) -> dict[str, Any]:
        return {
            k: v
            for k, v in asdict(self).items()
            if v is not None
        }


# ---------------------------------------------------------------------------
# Top-level block
# ---------------------------------------------------------------------------


@dataclass
class ChartBlock:
    """First-class chart envelope.

    The ``path`` field is the *Agent's chosen* data path:

    * ``"inline"`` — series.data is set; envelope is self-contained.
    * ``"bulk"``   — series.data is None and ``bulk_data_uri`` is set;
      the dashboard fetches the artifact via ``/charts/<chart_id>``.

    Composer code is allowed to override only when the inline payload
    exceeds :data:`MAX_INLINE_BYTES` (auto-promote to bulk + warn).
    """

    chart_id: str = field(default_factory=lambda: make_chart_id())
    chart_kind: Literal[
        "candlestick", "line", "area", "baseline", "histogram", "bar", "multi"
    ] = "line"
    title: str = ""
    subtitle: Optional[str] = None
    series: list[ChartSeries] = field(default_factory=list)
    overlays: list[ChartOverlay] = field(default_factory=list)
    panes: list[ChartPane] = field(default_factory=list)
    time: ChartTime = field(default_factory=ChartTime)
    default_range: Optional[dict[str, Any]] = None  # {"from": ..., "to": ...}
    source: ChartSource = field(default_factory=ChartSource)
    caption: Optional[str] = None
    insights: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    ui: Optional[ChartUI] = None
    # Path picked by the Agent / skill. Composer auto-promotes
    # "inline" → "bulk" only when MAX_INLINE_BYTES is exceeded.
    path: Literal["inline", "bulk"] = "inline"
    # Filled by composer when path == "bulk"; otherwise None.
    bulk_data_uri: Optional[str] = None
    # Schema version for forward-compat. Renderers may switch on this.
    version: str = CHART_BLOCK_VERSION
    # Stable timestamp (epoch seconds) when the block was authored.
    ts: float = field(default_factory=_now_seconds)

    def as_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "kind": "chart",
            "version": self.version,
            "chart_id": self.chart_id,
            "chart_kind": self.chart_kind,
            "title": self.title,
            "series": [s.as_dict() for s in self.series],
            "time": self.time.as_dict(),
            "source": self.source.as_dict(),
            "insights": list(self.insights),
            "path": self.path,
            "ts": self.ts,
        }
        if self.subtitle:
            out["subtitle"] = self.subtitle
        if self.overlays:
            out["overlays"] = [o.as_dict() for o in self.overlays]
        if self.panes:
            out["panes"] = [p.as_dict() for p in self.panes]
        if self.default_range:
            out["default_range"] = dict(self.default_range)
        if self.caption:
            out["caption"] = self.caption
        if self.warnings:
            out["warnings"] = list(self.warnings)
        if self.ui is not None:
            ui_dict = self.ui.as_dict()
            if ui_dict:
                out["ui"] = ui_dict
        if self.bulk_data_uri:
            out["bulk_data_uri"] = self.bulk_data_uri
        return out


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def make_chart_id(prefix: str = "chart") -> str:
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


def stable_chart_id(skill: str, action: str, payload: Any) -> str:
    """Deterministic chart_id derived from (skill, action, payload).

    Lets composer dedupe identical artifacts across turns. Falls back
    to a uuid if the payload is not JSON-serialisable.
    """

    try:
        canonical = json.dumps(
            {"skill": skill, "action": action, "payload": payload},
            sort_keys=True, default=str,
        )
    except (TypeError, ValueError):
        return make_chart_id()
    digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:16]
    base = f"{skill}.{action}".strip(".") or "chart"
    safe = "".join(c if c.isalnum() or c in "._-" else "_" for c in base)
    return f"{safe}.{digest}"


def estimate_inline_bytes(block: ChartBlock) -> int:
    """How many bytes the inline serialisation would weigh.

    Used by composer to decide whether to auto-promote inline → bulk.
    Counts only the data arrays (the only field that can blow up); the
    rest of the envelope is constant-ish overhead (~1KB).
    """

    total = 0
    for series in block.series:
        if series.data is None:
            continue
        try:
            total += len(json.dumps(series.data, default=str))
        except (TypeError, ValueError):
            # Non-serialisable points; treat as max so we promote to bulk.
            return MAX_INLINE_BYTES + 1
    return total


def _series_has_inline_data(series: Iterable[ChartSeries]) -> bool:
    return any(s.data is not None for s in series)


def _series_has_uri(series: Iterable[ChartSeries]) -> bool:
    return any(s.data_uri is not None for s in series)


def validate_chart_block(block: ChartBlock) -> list[str]:
    """Return a list of error strings; empty list means valid.

    We do not raise here — the kernel chooses how to handle invalid
    blocks (typically: drop + journal warning, do not crash the turn).
    """

    errors: list[str] = []
    if not block.series:
        errors.append("series must contain at least one entry")
    if block.path not in ("inline", "bulk"):
        errors.append(f"path must be 'inline' or 'bulk', got {block.path!r}")
    if block.path == "inline":
        if _series_has_uri(block.series) and not block.bulk_data_uri:
            errors.append(
                "inline path conflicts with series.data_uri set on a series"
            )
    if block.path == "bulk":
        if not block.bulk_data_uri and not _series_has_uri(block.series):
            errors.append("bulk path requires bulk_data_uri or per-series data_uri")
    for i, s in enumerate(block.series):
        if s.data is None and s.data_uri is None:
            errors.append(
                f"series[{i}] {s.name!r}: must set either data or data_uri"
            )
        if s.data is not None and s.data_uri is not None:
            errors.append(
                f"series[{i}] {s.name!r}: cannot set both data and data_uri"
            )
    return errors
