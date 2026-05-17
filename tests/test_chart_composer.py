"""Tests for ``nerya.charting.composer`` and ``nerya.api.routes_charts``.

The composer is the seam every chart-producing path (static skill,
dynamic code, kernel inline) flows through, so we check:

* ``path="inline"`` returns a self-contained block with data intact.
* ``path="bulk"`` writes to ``artifacts/charts/<id>.json`` and clears
  the inline series data.
* Stable chart_id derivation deduplicates identical inputs.
* The 256KB inline guardrail auto-promotes to bulk and surfaces a
  warning, not a silent flip.
* The HTTP route round-trips the artifact on hit and produces a clean
  ``not_found`` envelope on miss without raising.
* ``chart_id`` is sanitised against directory traversal at the seam.
"""

from __future__ import annotations

import tempfile
from pathlib import Path

import pytest

from nerya.agent.chart_block import MAX_INLINE_BYTES, validate_chart_block
from nerya.api.routes_charts import routes as charts_routes
from nerya.charting import (
    BUILD_CONTEXT_REQUIRED_FOR_BULK,
    BulkContext,
    build_chart_block,
    chart_artifact_path,
    load_chart_artifact,
    persist_chart_artifact,
)
from nerya.core.paths import WorkspacePaths
from nerya.workspace.artifact_store import ArtifactStore


pytestmark = pytest.mark.smoke


@pytest.fixture()
def workspace(tmp_path: Path):
    paths = WorkspacePaths(root=tmp_path)
    (tmp_path / "artifacts" / "charts").mkdir(parents=True, exist_ok=True)
    store = ArtifactStore(paths)
    yield {
        "paths": paths,
        "store": store,
        "ctx": BulkContext(artifact_store=store),
    }


def _line_series(n: int = 5):
    return [
        {
            "type": "line",
            "name": "x",
            "data": [{"time": i, "value": float(i)} for i in range(n)],
        }
    ]


# ---------------------------------------------------------------------------
# Composer behaviour
# ---------------------------------------------------------------------------


def test_inline_block_round_trips(workspace) -> None:
    block = build_chart_block(
        chart_kind="line",
        title="inline",
        series=_line_series(5),
        source={"skill": "demo", "action": "inline", "as_of": "now"},
        path="inline",
    )
    assert block.path == "inline"
    assert block.bulk_data_uri is None
    assert block.series[0].data is not None
    assert block.series[0].data_uri is None
    assert validate_chart_block(block) == []


def test_bulk_block_persists_to_artifact_store(workspace) -> None:
    block = build_chart_block(
        chart_kind="candlestick",
        title="bulk",
        series=[
            {
                "type": "candlestick",
                "name": "ohlc",
                "data": [
                    {"time": i, "open": 100, "high": 105, "low": 95, "close": 101}
                    for i in range(10)
                ],
            }
        ],
        source={"skill": "markets", "action": "get_quote", "as_of": "now"},
        path="bulk",
        ctx=workspace["ctx"],
    )
    assert block.path == "bulk"
    assert block.bulk_data_uri == f"nerya://chart/{block.chart_id}"
    assert block.series[0].data is None
    assert block.series[0].data_uri == (
        f"nerya://chart/{block.chart_id}#series/ohlc"
    )

    artifact_path = chart_artifact_path(workspace["store"], block.chart_id)
    assert artifact_path.exists()
    payload = load_chart_artifact(workspace["store"], block.chart_id)
    assert payload["chart_id"] == block.chart_id
    assert len(payload["series"]) == 1
    assert len(payload["series"][0]["data"]) == 10


def test_bulk_without_ctx_raises(workspace) -> None:
    with pytest.raises(ValueError) as excinfo:
        build_chart_block(
            chart_kind="line",
            title="should fail",
            series=_line_series(),
            source={"skill": "demo", "action": "x", "as_of": "now"},
            path="bulk",
        )
    assert BUILD_CONTEXT_REQUIRED_FOR_BULK in str(excinfo.value)


def test_stable_chart_id_dedupes_identical_inputs(workspace) -> None:
    args = dict(
        chart_kind="line",
        title="dup",
        series=_line_series(5),
        source={"skill": "demo", "action": "dedup", "as_of": "now"},
        path="bulk",
        ctx=workspace["ctx"],
    )
    a = build_chart_block(**args)
    b = build_chart_block(**args)
    assert a.chart_id == b.chart_id


def test_inline_oversize_auto_promotes_to_bulk_with_warning(workspace) -> None:
    huge = [{"time": i, "value": float(i)} for i in range(50_000)]
    block = build_chart_block(
        chart_kind="line",
        title="huge",
        series=[{"type": "line", "name": "x", "data": huge}],
        source={"skill": "demo", "action": "auto_promote", "as_of": "now"},
        path="inline",
        ctx=workspace["ctx"],
    )
    assert block.path == "bulk"
    assert block.bulk_data_uri is not None
    assert any("auto-promoted to bulk" in w for w in block.warnings)


def test_inline_oversize_without_ctx_raises(workspace) -> None:
    huge = [{"time": i, "value": float(i)} for i in range(50_000)]
    with pytest.raises(ValueError) as excinfo:
        build_chart_block(
            chart_kind="line",
            title="huge_no_ctx",
            series=[{"type": "line", "name": "x", "data": huge}],
            source={"skill": "demo", "action": "auto_no_ctx", "as_of": "now"},
            path="inline",
            # ctx omitted on purpose
        )
    msg = str(excinfo.value)
    assert "exceeds MAX_INLINE_BYTES" in msg
    assert str(MAX_INLINE_BYTES) in msg


def test_bulk_path_with_only_data_uri_raises(workspace) -> None:
    # Caller asked for bulk but every series already had a data_uri and
    # no inline data — composer can't persist anything, so it should
    # surface that as a hard error rather than write an empty artifact.
    with pytest.raises(ValueError) as excinfo:
        build_chart_block(
            chart_kind="line",
            title="empty bulk",
            series=[{"type": "line", "name": "x", "data_uri": "nerya://chart/foo"}],
            source={"skill": "demo", "action": "x", "as_of": "now"},
            path="bulk",
            ctx=workspace["ctx"],
        )
    assert "no inline data was provided" in str(excinfo.value)


def test_chart_artifact_path_rejects_traversal(workspace) -> None:
    with pytest.raises(ValueError):
        chart_artifact_path(workspace["store"], "../../etc/passwd")
    with pytest.raises(ValueError):
        chart_artifact_path(workspace["store"], "")


def test_persist_chart_artifact_returns_canonical_uri(workspace) -> None:
    uri = persist_chart_artifact(
        workspace["store"], "demo.test.123", {"chart_id": "demo.test.123"}
    )
    assert uri == "nerya://chart/demo.test.123"


# ---------------------------------------------------------------------------
# HTTP route
# ---------------------------------------------------------------------------


class _FakeClient:
    def __init__(self, paths):
        class _Cfg:
            pass

        self.config = _Cfg()
        self.config.paths = paths


def _route(method: str, path: str):
    for m, p, h in charts_routes():
        if m == method and p == path:
            return h
    raise AssertionError(f"route not registered: {method} {path}")


def test_route_get_returns_payload_on_hit(workspace) -> None:
    block = build_chart_block(
        chart_kind="line",
        title="route hit",
        series=_line_series(8),
        source={"skill": "demo", "action": "route_hit", "as_of": "now"},
        path="bulk",
        ctx=workspace["ctx"],
    )
    handler = _route("GET", "/charts/get")
    res = handler(_FakeClient(workspace["paths"]), {"id": block.chart_id})
    assert res["ok"] is True
    assert res["chart_id"] == block.chart_id
    assert len(res["payload"]["series"][0]["data"]) == 8


def test_route_get_returns_not_found_envelope_on_miss(workspace) -> None:
    handler = _route("GET", "/charts/get")
    res = handler(_FakeClient(workspace["paths"]), {"id": "unknown.chart"})
    assert res == {"ok": False, "error": "not_found", "chart_id": "unknown.chart"}


def test_route_get_rejects_traversal_attempts(workspace) -> None:
    handler = _route("GET", "/charts/get")
    res = handler(_FakeClient(workspace["paths"]), {"id": "../../etc/passwd"})
    assert res["ok"] is False
    assert res["error"] == "invalid_chart_id"


def test_route_get_requires_id(workspace) -> None:
    handler = _route("GET", "/charts/get")
    assert handler(_FakeClient(workspace["paths"]), {}) == {
        "ok": False,
        "error": "id is required",
    }
    assert handler(_FakeClient(workspace["paths"]), {"id": "  "}) == {
        "ok": False,
        "error": "id is required",
    }
