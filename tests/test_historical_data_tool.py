import argparse
import socket

import pytest

from nerya.core.config import Config
from nerya.core.paths import WorkspacePaths
from nerya.data.history_store import HistoryStore
from nerya.skills.builtin.backtest.scripts.history_data import prepare_series
from nerya.tools.native.historical_data import historical_data_handler, historical_data_descriptors, tool_progress
from nerya.tools.types import ToolCall

pytestmark = pytest.mark.smoke


@pytest.fixture(autouse=True)
def no_real_network(monkeypatch):
    def blocked(*args, **kwargs):
        pytest.fail("historical data tests must not use the network")
    monkeypatch.setattr(socket.socket, "connect", blocked)
    monkeypatch.setattr(socket.socket, "connect_ex", blocked)
    monkeypatch.setattr(socket, "getaddrinfo", blocked)


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


@pytest.mark.parametrize("action,kind,read_only", [
    ("list", "candles", True), ("inspect", "lp_state_requirements", True),
    ("status", "lp_state_job", True), ("download", "candles", False),
    ("start", "lp_state_job", False), ("resume", "lp_state_job", False),
    ("stop", "lp_state_job", False),
])
def test_producer_read_metadata_survives_checkpoint_without_replaying_writes(
        tmp_path, monkeypatch, action, kind, read_only):
    from types import SimpleNamespace
    from nerya.agent.loop_state import LoopRunState
    from nerya.agent.tool_phase import ToolBatchPhase, ToolBatchPolicy
    from nerya.tools.orchestrator import BatchResult
    from nerya.tools.registry import ToolRegistry
    from nerya.tools.types import RiskLevel, ToolErrorKind

    executed = []

    def operation(config, args, **kwargs):
        executed.append(dict(args))
        return {"ok": True, "read_only": not read_only, "observation": len(executed)}

    monkeypatch.setattr("nerya.tools.native.historical_data.history_operation", operation)
    monkeypatch.setattr("nerya.skills.builtin.backtest.scripts.lp_state_requirements.inspect_lp_state_requirements", operation)
    monkeypatch.setattr("nerya.skills.builtin.backtest.scripts.lp_state_job.operation", operation)
    descriptor = historical_data_descriptors(Config(paths=WorkspacePaths(tmp_path)))[0]
    assert descriptor.read_only is False and descriptor.risk == RiskLevel.WRITE
    registry = ToolRegistry()
    registry.register(descriptor)
    phase = ToolBatchPhase(registry=registry, orchestrator=SimpleNamespace(
        run_batch=lambda calls: BatchResult(results=[descriptor.handler(call) for call in calls])))
    policy = ToolBatchPolicy(frozenset({"historical_data"}), frozenset({"historical_data"}))
    state = LoopRunState(turn_id="turn", message_id="message",
        required_next_tool_names=set(), attempted_tool_names=set(), successful_tool_names=set(),
        completed_tool_results=[], tool_result_by_fingerprint={}, recent_tool_fingerprints=[],
        deduped_counts_by_fingerprint={})
    # Neither caller metadata nor JSON fields may turn a write into a read.
    args = {"action": action, "dataset_kind": kind, "read_only": not read_only}
    assert descriptor.per_call_risk(args) == (RiskLevel.READ if read_only else RiskLevel.WRITE)
    first = phase.run([ToolCall(name="historical_data", id="first", arguments=args,
                               metadata={"read_only": not read_only})], state=state, policy=policy).batch.results[0]
    assert (first.metadata.get("read_only") is True) is read_only
    resumed = LoopRunState.from_checkpoint(state.to_checkpoint(resumable=True, resume_block_reason=""))
    second = phase.run([ToolCall(name="historical_data", id="second", arguments=args)],
                       state=resumed, policy=policy).batch.results[0]
    assert len(executed) == (2 if read_only else 1)
    if read_only:
        assert not second.is_error and second.content[0].data["observation"] == 2
        # Ordinary repeat limits still apply; this is not a fresh-poll exemption.
        third = phase.run([ToolCall(name="historical_data", id="third", arguments=args)],
                          state=resumed, policy=policy).batch.results[0]
        assert third.error.kind == ToolErrorKind.DEDUPED and len(executed) == 2
    else:
        assert second.error.kind == ToolErrorKind.DEDUPED


@pytest.mark.parametrize("market", ["UNISWAP_V3:ETH/USDC", "BYBIT:BTCUSDT", "CUSTOM_POOL:asset/pair"])
@pytest.mark.parametrize("now,window_start,gap_start,expected_bars", [
    (1767225600, 1751673600, 1751673600, 4320),
    (1767225660, 1751673660, 1751677200, 4319),
])
def test_inspect_missing_history_is_successful_query_without_readiness(
        tmp_path, monkeypatch, market, now, window_start, gap_start, expected_bars):
    cfg = Config(paths=WorkspacePaths(tmp_path))
    monkeypatch.setattr("nerya.skills.builtin.backtest.scripts.data_cache._source_fetch",
        lambda *a, **kw: pytest.fail("inspect cannot fetch history"))
    monkeypatch.setattr("nerya.skills.builtin.backtest.scripts.history_data.time.time",
        lambda: now)
    out = historical_data_handler(ToolCall(name="historical_data", arguments={
        "action": "inspect", "markets": [market], "timeframes": ["1h"]}), config=cfg)
    data = out.content[0].data

    assert data["ok"] is True and out.semantic_success is True
    assert not out.is_error and out.error is None
    assert data["status"] == "incomplete" and data["coverage_complete"] is False
    assert data["reason"] == "historical_coverage_missing"
    assert data["resumable"] is False and data["safe_to_retry"] is False
    assert data["provider_status"] == "not_checked"
    assert data["download_authorization"] == "not_checked"
    assert data["protocol_replay_status"] == "not_checked"
    assert data["window"] == {
        "start": window_start, "end": now, "end_exclusive": True,
        "days": 180, "default_days_used": True}
    receipt = data["datasets"][0]
    assert receipt["market"] == market
    assert receipt["status"] == "incomplete" and receipt["complete"] is False
    assert receipt["cached_rows"] == receipt["downloaded_rows"] == receipt["requests"] == 0
    assert receipt["missing_bars"] == expected_bars and receipt["expected_bars"] == expected_bars
    assert receipt["missing_ranges"] == [{"start": gap_start, "end": 1767225600, "bars": expected_bars}]
    assert receipt["offline"] is True and receipt["resumable"] is False
    assert not any(word in data["message"].lower() for word in ("repeat", "retry", "resume"))
    assert not (cfg.paths.artifacts / "backtest_cache").exists()


@pytest.mark.parametrize("bounds,expected_days", [
    ({"days": 30}, 30),
    ({"start": "2025-12-01", "end": "2026-01-01"}, 31),
    ({"end": "2025-12-01", "days": 30}, 30),
])
def test_inspect_reports_explicit_window_and_descriptor_requires_admitted_bounds(
        tmp_path, monkeypatch, bounds, expected_days):
    cfg = Config(paths=WorkspacePaths(tmp_path))
    monkeypatch.setattr("nerya.skills.builtin.backtest.scripts.history_data.time.time",
        lambda: 1767225600)
    out = historical_data_handler(ToolCall(name="historical_data", arguments={
        "action": "inspect", "markets": ["CUSTOM:ETH/USDC"],
        "timeframes": ["1h"], **bounds}), config=cfg)
    data = out.content[0].data
    assert data["ok"] is True and data["coverage_complete"] is False
    assert data["window"]["default_days_used"] is False
    assert data["window"]["days"] == expected_days
    assert data["datasets"][0]["expected_bars"] == expected_days * 24
    description = historical_data_descriptors(cfg)[0].description
    assert "admitted start/end or days" in description
    assert "relative recent-N-days requests, pass days and omit start/end" in description
    assert "native producer's own history preparation" in description
    assert "default 180" in description
    assert "coverage_complete" in description
    assert "provider" in description and "permission" in description and "protocol replay" in description
    assert "An incomplete receipt is resumable" not in description


def test_invalid_inspect_is_failure_without_download_recovery_advice(tmp_path):
    cfg = Config(paths=WorkspacePaths(tmp_path))
    out = historical_data_handler(ToolCall(name="historical_data", arguments={
        "action": "inspect", "markets": ["CUSTOM:ETH/USDC"], "timeframes": ["invalid"]}), config=cfg)
    data = out.content[0].data
    assert data["ok"] is False and out.semantic_success is False and out.is_error
    assert data["status"] == "failed" and data["error"] == "HistoryDataError"
    assert data["resumable"] is False
    assert "resume" not in data.get("message_hint", "").lower()


@pytest.mark.parametrize("cached_bars", [1, 24])
def test_inspect_coverage_uses_all_local_datasets_without_claiming_protocol_readiness(tmp_path, cached_bars):
    cfg = Config(paths=WorkspacePaths(tmp_path))
    store = HistoryStore(cfg.paths.artifacts / "backtest_cache")
    start = 1735689600  # 2025-01-01 UTC.
    for market, count in [("CUSTOM:ETH/USDC", 24), ("OTHER:ETH/USDC", cached_bars)]:
        store.put(market, "1h", [
            {"ts": start + i * 3600, "open": 1, "high": 2, "low": 0.5, "close": 1.5, "volume": 10}
            for i in range(count)], source="test")
    before = store.path.read_bytes()
    out = historical_data_handler(ToolCall(name="historical_data", arguments={
        "action": "inspect", "markets": ["CUSTOM:ETH/USDC", "OTHER:ETH/USDC"],
        "timeframes": ["1h"], "start": "2025-01-01", "end": "2025-01-02"}), config=cfg)
    data = out.content[0].data
    assert data["ok"] is True and out.semantic_success is True
    assert data["coverage_complete"] is (cached_bars == 24)
    assert data["status"] == ("ready" if cached_bars == 24 else "incomplete")
    assert data["reason"] == (None if cached_bars == 24 else "historical_coverage_missing")
    assert [r["missing_bars"] for r in data["datasets"]] == [0, 24 - cached_bars]
    assert data["provider_status"] == data["protocol_replay_status"] == "not_checked"
    assert data["download_authorization"] == "not_checked"
    assert all(r["requests"] == 0 and r["resumable"] is False for r in data["datasets"])
    assert store.path.read_bytes() == before


@pytest.mark.parametrize("downloaded_bars", [1, 24])
def test_download_keeps_completeness_success_and_resumable_job_contract(tmp_path, monkeypatch, downloaded_bars):
    cfg = Config(paths=WorkspacePaths(tmp_path))
    def fetch(_market, *, start, **kwargs):
        return [
            {"ts": start + i * 3600, "open": 1, "high": 2, "low": 0.5, "close": 1.5, "volume": 10}
            for i in range(downloaded_bars)]
    monkeypatch.setattr("nerya.skills.builtin.backtest.scripts.data_cache._source_fetch", fetch)
    out = historical_data_handler(ToolCall(name="historical_data", arguments={
        "action": "download", "markets": ["BYBIT:ETHUSDT"], "timeframes": ["1h"],
        "start": "2025-01-01", "end": "2025-01-02", "max_requests": 1}), config=cfg)
    data = out.content[0].data
    assert data["ok"] is (downloaded_bars == 24)
    assert out.semantic_success is (downloaded_bars == 24)
    assert not out.is_error
    assert data["status"] == ("ready" if downloaded_bars == 24 else "incomplete")
    receipt = data["datasets"][0]
    assert receipt["complete"] is (downloaded_bars == 24)
    assert receipt["resumable"] is True and receipt["offline"] is False
    assert receipt["downloaded_rows"] == downloaded_bars and receipt["requests"] == 1
    status = historical_data_handler(ToolCall(name="historical_data", arguments={
        "action": "status", "job_id": receipt["job_id"]}), config=cfg).content[0].data
    assert status["ok"] is True and status["job"]["complete"] is (downloaded_bars == 24)
    assert status["job"]["resumable"] is True


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
