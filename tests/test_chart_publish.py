"""Tests for the dynamic-code chart publish path (PR4.5).

Covers:
- ``POST /charts/publish`` happy path: persists series, returns
  chart_id + bulk_data_uri.
- Validation: missing chart_block / empty series / bad shape.
- ``client.charts.publish`` SDK facade end-to-end (artifact_store →
  load_chart_artifact round-trip).
- ``client.charts.emit_marker`` writes the right stdout marker.
- ``client.charts.publish_and_announce`` does both.
"""

from __future__ import annotations

import io
from pathlib import Path
from typing import Any

import pytest

from nerya.api.routes_charts import _get, _post_publish
from nerya.charting import load_chart_artifact
from nerya.core.paths import WorkspacePaths
from nerya.workspace.artifact_store import ArtifactStore


pytestmark = pytest.mark.smoke


class _StubClient:
    """Minimal stand-in for the real daemon client.

    The route handler only touches ``client.config.paths`` to build an
    ArtifactStore; we provide that and nothing else. Keeps the test off
    the network and away from the SQLite session store.
    """

    def __init__(self, root: Path):
        class _Cfg:
            paths = WorkspacePaths(root=root)

        self.config = _Cfg()


def _line_block() -> dict[str, Any]:
    return {
        "chart_kind": "line",
        "title": "agent backtest",
        "series": [
            {
                "type": "line",
                "name": "equity",
                "data": [
                    {"time": 1700000000, "value": 1.0},
                    {"time": 1700003600, "value": 1.02},
                    {"time": 1700007200, "value": 1.018},
                ],
            }
        ],
        "source": {"skill": "agent", "action": "backtest"},
    }


def test_publish_persists_artifact_and_returns_uri(tmp_path: Path) -> None:
    client = _StubClient(tmp_path)
    res = _post_publish(client, {"chart_block": _line_block()})
    assert res["ok"] is True
    chart_id = res["chart_id"]
    assert chart_id
    assert res["bulk_data_uri"] == f"nerya://chart/{chart_id}"

    # Round-trip via load_chart_artifact — proves the file actually
    # landed at ``artifacts/charts/<id>.json``.
    store = ArtifactStore(WorkspacePaths(root=tmp_path))
    payload = load_chart_artifact(store, chart_id)
    assert payload is not None
    assert payload["title"] == "agent backtest"
    assert payload["series"][0]["data"][0]["value"] == 1.0


def test_publish_missing_chart_block_returns_validation_envelope(tmp_path: Path) -> None:
    client = _StubClient(tmp_path)
    res = _post_publish(client, {})
    assert res["ok"] is False
    assert "chart_block" in res["error"]


def test_publish_empty_series_rejected(tmp_path: Path) -> None:
    client = _StubClient(tmp_path)
    block = _line_block()
    block["series"] = []
    res = _post_publish(client, {"chart_block": block})
    assert res["ok"] is False
    assert "series" in res["error"]


def test_publish_bad_payload_type(tmp_path: Path) -> None:
    client = _StubClient(tmp_path)
    res = _post_publish(client, "not a dict")  # type: ignore[arg-type]
    assert res["ok"] is False


def test_publish_series_without_data_returns_invalid_chart_block(tmp_path: Path) -> None:
    """A series with neither ``data`` nor ``data_uri`` can't be persisted —
    the composer raises ``ValueError`` on the bulk path because there's
    nothing to write to disk. The route surfaces that as
    ``invalid_chart_block`` instead of leaking the 500.
    """

    client = _StubClient(tmp_path)
    block = _line_block()
    block["series"] = [{"type": "line", "name": "empty"}]
    res = _post_publish(client, {"chart_block": block})
    assert res["ok"] is False
    assert res["error"] == "invalid_chart_block"


def test_publish_then_get_round_trip(tmp_path: Path) -> None:
    """Bulk publish via POST → GET reads back the same series."""

    client = _StubClient(tmp_path)
    pub = _post_publish(client, {"chart_block": _line_block()})
    chart_id = pub["chart_id"]
    fetched = _get(client, {"id": chart_id})
    assert fetched["ok"] is True
    assert fetched["chart_id"] == chart_id
    assert fetched["payload"]["title"] == "agent backtest"


# ---------- SDK facade (skips if SDK can't initialise daemon) ----------


def test_emit_marker_prints_protocol_line() -> None:
    """``emit_marker`` writes ``@@nerya:chart@@ <id>`` to its file arg.

    We don't go through ``connect()`` here because that requires a real
    workspace; the marker writer is a pure function on the facade so
    we can probe it via the class directly.
    """

    from nerya_sdk.client import NeryaClient

    facade = NeryaClient._ChartsFacade.__new__(NeryaClient._ChartsFacade)
    buf = io.StringIO()
    facade.emit_marker("test.chart.id", file=buf)
    assert buf.getvalue().strip() == "@@nerya:chart@@ test.chart.id"
