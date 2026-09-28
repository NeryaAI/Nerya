import argparse

import pytest

from nerya.core.config import Config
from nerya.core.paths import WorkspacePaths
from nerya.data.history_store import HistoryStore
from nerya.skills.builtin.backtest.scripts.history_data import prepare_series
from nerya.tools.native.historical_data import historical_data_handler, historical_data_descriptors, tool_progress
from nerya.tools.types import ToolCall

pytestmark = pytest.mark.smoke


def test_native_inventory_and_inspect_are_network_free(tmp_path, monkeypatch):
    cfg = Config(paths=WorkspacePaths(tmp_path))
    def no_network(*a, **kw):
        pytest.fail("read-only historical operations cannot download data")
    monkeypatch.setattr("nerya.skills.builtin.backtest.scripts.data_cache._source_fetch", no_network)
    result = historical_data_handler(ToolCall(name="historical_data", arguments={"action": "list"}), config=cfg)
    assert result.content[0].data["datasets"] == []
    out = historical_data_handler(ToolCall(name="historical_data", arguments={"action": "inspect",
        "markets": ["BINANCE:BTCUSDT"], "timeframes": ["1h"], "start": "2025-01-01", "end": "2025-01-02"}), config=cfg)
    data = out.content[0].data
    assert data["status"] == "incomplete" and data["datasets"][0]["missing_bars"] == 24
    assert not HistoryStore(cfg.paths.artifacts / "backtest_cache").path.exists()


def test_tool_cancellation_prevents_network(tmp_path, monkeypatch):
    from nerya.harness.cancellation import CancelToken
    token = CancelToken()
    token.cancel("controlled cancellation")
    cfg = Config(paths=WorkspacePaths(tmp_path))
    monkeypatch.setattr("nerya.skills.builtin.backtest.scripts.data_cache._source_fetch", lambda *a, **kw: pytest.fail("cancelled download started network"))
    out = historical_data_handler(ToolCall(name="historical_data", arguments={"action": "download",
        "markets": ["BINANCE:BTCUSDT"], "timeframes": ["1h"], "start": "2025-01-01", "end": "2025-01-02"},
        metadata={"cancel_token": token}), config=cfg)
    assert out.is_error and out.content[0].data["status"] == "cancelled"


def test_progress_can_be_rendered_by_existing_chat_contract():
    from nerya.agent.streaming import get_default_bus
    events = []
    stop = get_default_bus().subscribe(events.append)
    try:
        call = ToolCall(name="historical_data", id="history-progress-test", turn_id="turn-history-test", metadata={"session_id": "history-progress-test"})
        tool_progress(call)({"status": "downloading", "market": "BINANCE:BTCUSDT", "timeframe": "1h",
            "rows": 50, "expected_bars": 100, "cached_rows": 20, "downloaded_rows": 30})
        event = next(e for e in reversed(events) if e.get("call_id") == call.id)
        assert event["kind"] == "tool.progress" and event["progress"] == 0.5
        assert "50/100 candles" in event["message"]
        assert "reused 20, downloaded 30" in event["message"]
    finally:
        stop()


def test_cli_and_native_schema_share_actions(tmp_path):
    from nerya.cli.commands.data import register
    parser = argparse.ArgumentParser()
    register(parser.add_subparsers())
    args = parser.parse_args(["data", "download", "--markets", "BINANCE:BTCUSDT",
        "--timeframes", "1h", "--start", "2025-01-01", "--end", "2026-01-01"])
    assert args.data_action == "download" and args.timeframes == ["1h"]
    descriptor = historical_data_descriptors(Config(paths=WorkspacePaths(tmp_path)))[0]
    assert descriptor.name == "historical_data"
    assert args.data_action in descriptor.input_schema["properties"]["action"]["enum"]


def test_binance_monthly_download_keeps_recent_post_listing_rows_first(tmp_path):
    store = HistoryStore(tmp_path)
    calls = []

    def fetch(_market, *, tf, start, end, **_kwargs):
        calls.append((start, end))
        step = 86400
        return [
            {"ts": stamp, "open": 1, "high": 2, "low": 0.5, "close": 1.5, "volume": 10}
            for stamp in range(start, end + 1, step)
        ]

    rows, receipt = prepare_series(
        store,
        "BINANCE:BTCUSDT",
        "1d",
        1735689600,  # 2025-01-01
        1743465600,  # 2025-04-01
        max_requests=1,
        retries=0,
        timeout_seconds=5,
        fetch=fetch,
    )

    assert calls == [(1740787200, 1743465599)]  # 2025-03-01 .. 2025-03-31
    assert rows
    assert rows[0]["ts"] == 1740787200
    assert receipt["complete"] is False
    assert receipt["missing_bars"] > 0
