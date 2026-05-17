"""Charting helpers shared between skills, kernel, and the dynamic-code path.

The :mod:`nerya.agent.chart_block` module owns the wire schema (the
dataclass that lands in ``turn.blocks``). This package owns the
*construction* helpers — i.e. the composer that takes raw series data
and returns a validated :class:`~nerya.agent.chart_block.ChartBlock`,
optionally persisting the heavy payload to ``artifacts/charts/<id>.json``
when ``path="bulk"``.

Exports:

* :func:`build_chart_block` — primary composer entry point.
* :func:`persist_chart_artifact` — write a chart payload to the
  workspace artifact store, returning the canonical ``nerya://chart/<id>``
  URI.
* :func:`load_chart_artifact` — counterpart used by HTTP / SDK readers.
"""

from .composer import (
    BUILD_CONTEXT_REQUIRED_FOR_BULK,
    BulkContext,
    build_chart_block,
    chart_artifact_path,
    load_chart_artifact,
    persist_chart_artifact,
)
from .from_rows import (
    candle_chart_from_rows,
    equity_curve_from_rows,
    line_chart_from_rows,
)

__all__ = [
    "BUILD_CONTEXT_REQUIRED_FOR_BULK",
    "BulkContext",
    "build_chart_block",
    "candle_chart_from_rows",
    "chart_artifact_path",
    "equity_curve_from_rows",
    "line_chart_from_rows",
    "load_chart_artifact",
    "persist_chart_artifact",
]
