"""Deterministic regressions. All prices in this file are labelled test fixtures."""
from pathlib import Path
import json
import runpy

import pytest
from nerya.core import yaml_io
from nerya.core.config import Config
from nerya.core.paths import WorkspacePaths
from nerya.skills.builtin.backtest.scripts.config import load_config
from nerya.skills.builtin.backtest.scripts.engine import run_backtest
from nerya.skills.builtin.backtest.scripts.metrics import assemble_metrics
from nerya.skills.builtin.backtest.scripts.writers import write_csv_artifacts
from nerya.skills.builtin.backtest.scripts.render_chart import render_chart
from nerya.tools.native.strategy_runtime import strategy_generate_proposal_handler, strategy_validate_handler, strategy_backtest_handler
from nerya.tools.types import ToolCall

pytestmark = pytest.mark.smoke


def test_compaction_preserves_observation_contract_and_terminal_equity():
    from nerya.llm.tool_compaction import compact_tool_result
    value = {"ok":True,"result_type":"backtest_result","strategy_id":"qa", "proposal_id":"prp_test", "backtest_ts":"20260926_120000",
        "evaluation_mode":"observation", "execution_mode":"agent","performance_evidence":False,
        "replay":{"agent_execution":"not_run","decisions":90,"dispatches":40,"skipped":50,"errors":0},
        "equity_preview":[{"time":i,"value":1000+i} for i in range(70)],"metrics":{}}
    result = compact_tool_result("strategy_backtest", value, size_threshold=0)
    assert result.kept["performance_evidence"] is False
    assert result.kept["replay"]["dispatches"] == 40
    assert result.kept["equity_preview"][-1] == {"time":69,"value":1069}
CASE = runpy.run_path(str(Path(__file__).parents[1] / "scripts/strategy_e2e_cases.py"))["case"]


def bars(n=30):
    return [{"ts": 1700000000 + i * 3600, "open": 100 + i, "high": 102 + i,
             "low": 99 + i, "close": 101 + i, "volume": 100, "fixture": True} for i in range(n)]


def test_concurrent_timestamp_does_not_overwrite_previous_run(tmp_path, monkeypatch):
    from nerya.skills.builtin.backtest.scripts.writers import create_run_dir
    monkeypatch.setattr("nerya.skills.builtin.backtest.scripts.writers.time.strftime", lambda *a: "20260926_120000")
    first = create_run_dir(tmp_path)
    (first / "evidence.txt").write_text("original")
    second = create_run_dir(tmp_path)
    assert first.name == "20260926_120000" and second.name == "run-1_20260926_120000"
    assert (first / "evidence.txt").read_text() == "original"
    assert create_run_dir(tmp_path, kind="freeform").name == "freeform_20260926_120000"
    assert create_run_dir(tmp_path, kind="freeform").name == "freeform-1_20260926_120000"


def test_forming_candle_is_not_historical_evidence(tmp_path, monkeypatch):
    from nerya.skills.builtin.backtest.scripts.backtest_run import _load_candles_with_timeframe_fallback
    now = 1700000000 + 12 * 3600
    cfg = load_config(markets=["MOCK:BTC"], overrides={"tf":"1h","warmup_bars":0})
    observed = []
    def fetch(market, tf, start, end, *a, **kw):
        observed.append(end)
        return bars(13)  # Misbehaving adapter includes the still-open last bar.
    monkeypatch.setattr("nerya.skills.builtin.backtest.scripts.backtest_run.get_candles", fetch)
    frames, _, _ = _load_candles_with_timeframe_fallback(cfg, now=now, cache_root=tmp_path, allow_mock=False, config_obj=None)
    assert observed == [now // 3600 * 3600 - 1]  # Inclusive legacy adapter bound for the closed UTC window.
    assert frames["MOCK:BTC"]["1h"][-1]["ts"] + 3600 <= now


@pytest.mark.parametrize("mode", ["script", "gated", "event"])
def test_create_validate_replay_report_same_candidate(tmp_path, monkeypatch, mode):
    cfg = Config(paths=WorkspacePaths(tmp_path))
    yaml_io.dump(tmp_path / "nerya.yml", {"runtime": {"live_trading_enabled": False}})
    created = strategy_generate_proposal_handler(ToolCall(name="strategy_generate_proposal", arguments=CASE(mode)), config=cfg)
    assert not created.is_error, created.text()
    pid = created.content[0].data["proposal_id"]
    checked = strategy_validate_handler(ToolCall(name="strategy_validate", arguments={"proposal_id": pid}), config=cfg)
    assert checked.content[0].data["ok"], checked.text()
    yaml_io.dump(tmp_path / "replay.yml", {"tf": "1h", "warmup_bars": 0, "window_days": 2})
    monkeypatch.setattr("nerya.skills.builtin.backtest.scripts.backtest_run.get_candles", lambda *a, **kw: bars())
    # This fixture intentionally requests replay.yml instead of the candidate defaults.
    out = strategy_backtest_handler(ToolCall(name="strategy_backtest", arguments={"proposal_id": pid,
        "engine": "native", "config_path": "replay.yml", "allow_mock": True,
        "override_candidate_backtest_defaults": True}), config=cfg)
    assert not out.is_error, out.text()
    data = out.content[0].data
    assert data["proposal_id"] == pid and data["strategy_id"] == CASE(mode)["strategy_id"]
    metrics = json.loads(Path(data["raw_metrics_file"]).read_text())
    chart = json.loads((Path(data["out_dir"]) / "chart.json").read_text())
    assert metrics["provenance"]["data_kind"] == "sample"  # Fixture is never real-history acceptance.
    equity = next(p for p in chart["panels"] if p["id"] == "equity")["series"][0]["data"]
    assert equity[-1]["value"] == metrics["final_equity_usd"]
    assert all(a["time"] < b["time"] for a, b in zip(equity, equity[1:]))
    assert metrics["replay"]["errors"] == 0
    if mode == "script":
        assert metrics["total_trades"] > 0 and metrics["performance_evidence"] is True
    else:
        assert metrics["replay"]["dispatches"] > 0 and metrics["performance_evidence"] is False
        assert metrics["replay"]["inputs_validated"] is True
        assert metrics["replay"]["agent_execution"] == "not_run"
        assert metrics["replay"]["order_attempts"] == 0
        assert "source:bars" in Path(data["decisions_path"]).read_text()
    assert not list(cfg.paths.strategies.glob("qa_*")), "proposal replay must not promote candidates"


def test_settlement_and_final_chart_match_independent_accounting(tmp_path):
    rows = bars(3)
    for row, opening, closing in zip(rows, [100, 110, 121], [105, 115, 125]):
        row.update(open=opening, close=closing, high=closing+1, low=opening-1)
    observed = []
    def run(ctx):
        observed.append(ctx.portfolio.cash_usd)
        if len(ctx.market.candles("MOCK:BTC", timeframe="1h")) == 1:
            ctx.trading.open_position(market="MOCK:BTC", side="long", sizing={"method":"fixed_base", "fixed_base":2})
        return ctx.result.hold(reason="fixture")
    cfg = load_config(markets=["MOCK:BTC"], overrides={"warmup_bars":0, "initial_capital_usd":1000,
        "fee_bps_by_venue":{"MOCK":100}, "slip_bps_by_venue":{"MOCK":0}})
    result = run_backtest(None, cfg, candles_by_market={"MOCK:BTC":rows}, run_fn=run)
    assert observed[0] == 1000 and observed[1] == pytest.approx(777.8)
    assert result.trades[0]["ts"] == rows[1]["ts"]
    metrics = assemble_metrics(result)
    assert metrics["final_equity_usd"] == pytest.approx(1025.3)  # 1000 + 2*(125-110) - 2.2 - 2.5
    write_csv_artifacts(result, tmp_path)
    (tmp_path / "metrics.json").write_text(json.dumps(metrics))
    chart = render_chart(tmp_path)
    points = next(p for p in chart["panels"] if p["id"] == "equity")["series"][0]["data"]
    assert points[-1]["value"] == metrics["final_equity_usd"]


def test_last_bar_signal_is_rejected_without_same_close_fill():
    rows = bars(3)
    def run(ctx):
        if len(ctx.market.candles("MOCK:BTC", timeframe="1h")) == len(rows):
            ctx.trading.open_position(
                market="MOCK:BTC",
                side="long",
                sizing={"method": "fixed_usd", "fixed_usd": 100},
                reasoning_ref="last bar signal",
            )
        return ctx.result.hold(reason="fixture")
    cfg = load_config(markets=["MOCK:BTC"], overrides={
        "warmup_bars": 0,
        "initial_capital_usd": 1000,
        "fee_bps_by_venue": {"MOCK": 0},
        "slip_bps_by_venue": {"MOCK": 0},
    })
    result = run_backtest(None, cfg, candles_by_market={"MOCK:BTC": rows}, run_fn=run)
    assert result.trades == []
    assert [row["reject_reason"] for row in result.rejected_signals] == ["end_of_data_no_next_bar"]


def test_agent_builder_has_runtime_precedence_and_input_errors_are_not_ignored(tmp_path):
    raw = CASE("event")
    raw["files"]["main.py"] = raw["files"]["main.py"].replace("return build_agent_task(ctx)", "raise AssertionError('stale script entrypoint must not run')")
    for name, content in raw["files"].items():
        (tmp_path / name).write_text(content)
    cfg = load_config(markets=raw["markets"], overrides={"warmup_bars":0, "evaluation_mode":"observation"})
    result = run_backtest(tmp_path, cfg, candles_by_market={raw["markets"][0]:bars()},
        strategy_config=yaml_io.loads(raw["files"]["strategy.yml"]))
    assert len(result.decisions) == 30 and all(d["status"] == "dispatch" for d in result.decisions)
    (tmp_path / "main.py").write_text(raw["files"]["main.py"].replace('outputs=[]', 'outputs=["missing"]'))
    with pytest.raises(ValueError, match="actual published"):
        run_backtest(tmp_path, cfg, candles_by_market={raw["markets"][0]:bars()},
            strategy_config=yaml_io.loads(raw["files"]["strategy.yml"]))


def test_dependency_failure_is_actionable_and_engine_can_be_selected(tmp_path, monkeypatch):
    from nerya.skills.builtin.backtest.scripts.freeform_run import FreeformDependencyError
    monkeypatch.setattr("nerya.tools.native.strategy_runtime._target_has_freeform_backtest_script", lambda *a, **kw: True)
    def unavailable(**kwargs):
        raise FreeformDependencyError("missing_qa_dependency", Path("research_backtest.py"))
    monkeypatch.setattr("nerya.skills.builtin.backtest.scripts.freeform_run.run_freeform_backtest", unavailable)
    monkeypatch.setattr("nerya.skills.builtin.backtest.scripts.backtest_run.run_strategy_backtest",
        lambda **kw: {"ok":True,"strategy_id":"qa","backtest_ts":"20260926_120000","metrics_display":{}})
    cfg = Config(paths=WorkspacePaths(tmp_path))
    out = strategy_backtest_handler(ToolCall(name="strategy_backtest", arguments={"strategy_id":"qa"}), config=cfg)
    assert out.is_error and out.content[0].data["backtest_status"] == "blocked"
    assert out.content[0].data["missing_module"] == "missing_qa_dependency"
    out = strategy_backtest_handler(ToolCall(name="strategy_backtest", arguments={"strategy_id":"qa", "engine":"native"}), config=cfg)
    assert not out.is_error and out.content[0].data["engine"] == "native"


def test_performance_failure_has_a_reason_without_changing_replay_success():
    cfg = load_config(markets=["MOCK:BTC"], overrides={"warmup_bars": 0})
    def run(ctx):
        if not ctx.portfolio.position("MOCK:BTC"):
            ctx.trading.open_position(market="MOCK:BTC", side="long", sizing={"method":"fixed_usd", "fixed_usd":10})
        return ctx.result.hold(reason="fixture")
    metrics = assemble_metrics(run_backtest(None, cfg, candles_by_market={"MOCK:BTC":bars()}, run_fn=run))
    assert metrics["verdict"] == "FAIL"
    assert "benchmark_capture_below_threshold" in metrics["flags"]
    assert metrics["replay"]["errors"] == 0
    assert metrics["total_return_pct"] > 0
