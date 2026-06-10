from __future__ import annotations

from nerya.core.config import Config
from nerya.core.paths import WorkspacePaths
from nerya.tools.native.data_sources import (
    data_source_status_handler,
    data_source_sync_now_handler,
)
from nerya.tools.types import ToolCall


def _json(result):
    for part in result.content:
        if part.type == "json":
            return part.data
    raise AssertionError("missing json result")


def _config(tmp_path):
    return Config(
        paths=WorkspacePaths(root=tmp_path),
        data={"runtime": {"data_source_sync_state": True}},
    )


def test_data_source_status_tool_seeds_default_contributors(tmp_path) -> None:
    result = data_source_status_handler(
        ToolCall(name="data_source_status", arguments={}),
        config=_config(tmp_path),
    )

    data = _json(result)
    source_ids = {row["source_id"] for row in data["sources"]}
    assert result.is_error is False
    assert data["total"] >= 5
    assert "memory:notebook" in source_ids
    assert "account:paper_main" in source_ids
    assert "market:public_ccxt" in source_ids


def test_data_source_sync_now_tool_records_events(tmp_path) -> None:
    result = data_source_sync_now_handler(
        ToolCall(
            name="data_source_sync_now",
            arguments={"source_id": "memory:notebook"},
        ),
        config=_config(tmp_path),
    )

    data = _json(result)
    assert result.is_error is False
    assert data["ok"] is True
    assert data["row"]["source_id"] == "memory:notebook"
    assert any(e["source_id"] == "memory:notebook" for e in data["events"])
