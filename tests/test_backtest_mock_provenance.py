"""Synthetic, network-free evidence contracts for mocked strategy inputs."""
from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from nerya.core import yaml_io
from nerya.skills.builtin.backtest.scripts.config import BacktestConfig
from nerya.skills.builtin.backtest.scripts.metrics import assemble_metrics
from nerya.skills.builtin.backtest.scripts.process_replay import execute_replay, ReplayWorkerError
from nerya.skills.builtin.backtest.scripts.render_chart import render_chart
from nerya.skills.builtin.backtest.scripts.report import render_report
from nerya.skills.builtin.backtest.scripts.writers import write_csv_artifacts

pytestmark = pytest.mark.smoke

MARKET = "BYBIT:BTCUSDT"
ENTRY_SCRIPT = '''def run(ctx):
    market = ctx.config.markets[0]
    count = len(ctx.market.candles(market, timeframe="1h", limit=10))
    if count == 1:
        answer = ctx.subagents.run("analyst", task="synthetic private prompt")
        if answer.get("verdict") != "ENTRY":
            return ctx.result.error(message="missing ENTRY verdict")
        ctx.trading.submit_intent(market=market, side="buy", size=1000, size_unit="usd")
        return ctx.result.ok(reason="fixture entry")
    if count == 3:
        ctx.trading.close_position(market=market, side="long")
        return ctx.result.ok(reason="fixture exit")
    return ctx.result.hold(reason="no signal")
'''


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def blocked(*args, **kwargs):
        pytest.fail("mock provenance regressions must not use the network")

    monkeypatch.setattr("socket.socket.connect", blocked)
    monkeypatch.setattr("socket.create_connection", blocked)


def prepare_replay(tmp_path: Path, code: str = ENTRY_SCRIPT, mock_surfaces=None):
    root = tmp_path / "strategy"
    root.mkdir()
    manifest = {"version": 1, "strategy_id": "strategy", "title": "Synthetic fixture",
        "mode": "paper", "entrypoint": "main.py:run", "markets": [MARKET],
        "accounts": ["fixture_account"], "execution_mode": "script",
        "schedule": {"type": "none", "enabled": False},
        "policy": {"allow_direct_order": True}}
    yaml_io.dump(root / "strategy.yml", manifest)
    (root / "main.py").write_text(code, encoding="utf-8")
    if mock_surfaces is None:
        mock_surfaces = {"subagents": {"mode": "stub",
            "payload": {"verdict": "ENTRY", "reasoning": "synthetic private answer"}}}
    cfg = BacktestConfig.from_raw({"markets": [MARKET], "tf": "1h", "warmup_bars": 0,
        "mock_surfaces": mock_surfaces})
    rows = [{"ts": 1700006400 + i * 3600, "open": 100 + i * 5,
             "high": 103 + i * 5, "low": 99 + i * 5, "close": 101 + i * 5,
             "volume": 10, "fixture": True} for i in range(6)]
    out = tmp_path / "backtest"
    out.mkdir()
    return root, cfg, rows, out, manifest


def replay(tmp_path: Path, code: str = ENTRY_SCRIPT, mock_surfaces=None):
    root, cfg, rows, out, manifest = prepare_replay(tmp_path, code, mock_surfaces)
    result = execute_replay(root, cfg, candles_by_market={MARKET: rows},
        timeframe_candles_by_market={MARKET: {"1h": rows}}, artefacts_dir=out,
        strategy_config=manifest)
    return result, out


def test_stub_entry_executes_in_real_worker_without_ai_performance_evidence(tmp_path):
    result, out = replay(tmp_path)
    metrics = assemble_metrics(result)
    assert metrics["total_trades"] == 1
    assert metrics["replay"]["orders_filled"] == 2
    assert metrics["replay"]["order_accounting_ok"] is True
    assert metrics["performance_evidence"] is False
    calls = [{"surface": "subagents", "mode": "stub", "count": 1}]
    assert metrics["replay"]["mock_surface_calls"] == calls
    assert metrics["replay"]["agent_execution"] == "not_run"
    assert "mocked_inputs_not_performance_evidence" in metrics["flags"]
    worker = json.loads((out / "worker_result.json").read_text())
    assert worker["result"]["decisions"][0]["mock_surface_calls"] == calls
    for path in (out / "worker_result.json", out / "logs/engine.log"):
        assert "synthetic private" not in path.read_text()

    write_csv_artifacts(result, out)
    (out / "metrics.json").write_text(json.dumps(metrics), encoding="utf-8")
    chart = render_chart(out)
    assert chart["meta"]["performance_evidence"] is False
    assert chart["meta"]["replay"]["mock_surface_calls"] == calls
    assert not any("Equity vs B&H" in block.get("title", "") for block in chart["chart_blocks"])
    report = render_report(metrics, result, result.config.asdict())
    assert "not trading-performance evidence" in report
    assert "| subagents | stub | 1 |" in report
    assert "synthetic private" not in report


def test_report_respects_inherited_nonperformance_flag(tmp_path):
    result, _ = replay(tmp_path)
    metrics = assemble_metrics(result)
    metrics["performance_evidence"] = False
    report = render_report(metrics, result, result.config.asdict())
    assert "performance_evidence" in report
    assert "false" in report.lower()


@pytest.mark.parametrize("currency", ["USDC", "token1:fixture", None])
def test_pool_price_report_preserves_quote_units_and_producer_provenance(currency):
    result = SimpleNamespace(strategy_id="fixture", trades=[{"reason": "exit",
        "notional": 120, "fee": 0.5, "pnl": 5, "pnl_pct": 5}])
    proof = {"execution_target": "JUPITER:SOL/USDC",
        "source": {"provider": "geckoterminal", "quote": "USDC"},
        "assumptions": ["fixture modeled costs"]}
    metrics = {"initial_capital_usd": 1000, "accounting_currency": currency,
        "replay": {"scope": "historical_pool_price_simulation"},
        "provenance": {"price_simulation": proof}}
    report = render_report(metrics, result, {})
    unit = currency or "quote unit not recorded"
    assert f"initial 1,000.00 {unit}" in report
    assert f"120.0000 {unit} | 0.5000 {unit}" in report
    assert f"5.0000 {unit} (5.0000%)" in report
    assert "$" not in report
    assert yaml_io.dumps(proof).strip() in report
    assert "no USD conversion is implied" in report
    assert "not historical target-venue routes, fees or fills" in report

    metrics["replay"] = {}
    legacy = render_report(metrics, result, {})
    assert "initial $1,000.00" in legacy
    assert "120.0000" in legacy and "$0.5000" in legacy
    assert "$5.0000 (5.0000%)" in legacy
    assert "Pool-price simulation provenance" not in legacy


@pytest.mark.parametrize("mode", ["stub", "replay"])
def test_no_signal_does_not_claim_unused_ai_calls(tmp_path, mode):
    result, _ = replay(tmp_path, 'def run(ctx): return ctx.result.hold(reason="no signal")',
        {name: {"mode": mode, "payload": {}} for name in ("llm", "news", "subagents")})
    metrics = assemble_metrics(result)
    assert metrics["replay"]["mock_surface_calls"] == []
    assert metrics["replay"]["agent_execution"] == "not_run"
    assert metrics["replay"]["orders_filled"] == 0
    assert metrics["performance_evidence"] is True
    assert "mocked_inputs_not_performance_evidence" not in metrics["flags"]


def test_trade_only_cex_keeps_performance_evidence_with_unused_stubs(tmp_path):
    code = ENTRY_SCRIPT.replace('ctx.subagents.run("analyst", task="synthetic private prompt")',
                                '{"verdict": "ENTRY"}')
    result, _ = replay(tmp_path, code)
    metrics = assemble_metrics(result)
    assert metrics["total_trades"] == 1
    assert metrics["replay"]["orders_filled"] == 2
    assert metrics["replay"]["order_accounting_ok"] is True
    assert metrics["performance_evidence"] is True
    assert metrics["replay"]["mock_surface_calls"] == []


@pytest.mark.parametrize("surface, method, payload, performance", [
    ("llm", "compress", {}, False),
    ("llm", "classify", {}, False),
    ("llm", "extract_json", {}, False),
    ("llm", "analyze_signal", {}, False),
    ("subagents", "run", {}, False),
    ("subagents", "run_many", [], False),
    ("news", "fetch", [], False),
    ("messages", "enqueue", {}, True),
])
def test_actual_gated_calls_accumulate_across_worker_ticks(tmp_path, surface, method, payload, performance):
    code = f'''def run(ctx):
    ctx.{surface}.{method}()
    ctx.{surface}.{method}()
    return ctx.result.hold(reason="fixture")
'''
    result, _ = replay(tmp_path, code, {surface: {"mode": "stub", "payload": payload}})
    metrics = assemble_metrics(result)
    assert metrics["replay"]["mock_surface_calls"] == [{"surface": surface, "mode": "stub", "count": 12}]
    assert metrics["performance_evidence"] is performance


@pytest.mark.parametrize("mode, payload, message", [
    ("error", None, "unsupported"),
    ("replay", {"verdict": "ENTRY"}, "not implemented"),
    ("stub", [], "missing ENTRY verdict"),
])
def test_unavailable_or_invalid_input_still_fails_the_worker(tmp_path, mode, payload, message):
    with pytest.raises(ReplayWorkerError, match=message) as error:
        replay(tmp_path, mock_surfaces={"subagents": {"mode": mode, "payload": payload}})
    details = error.value.receipt["details"]
    assert details["performance_evidence"] is False
    assert details["mock_surface_calls"] == [{"surface": "subagents", "mode": mode, "count": 1}]
    assert details["orders_attempted"] == 0
    if mode != "stub":
        assert error.value.surface == "subagents"
        assert "Do not bypass strategy gates" in str(error.value)
    assert not (tmp_path / "backtest/metrics.json").exists()


def test_caught_unsupported_input_is_not_performance_evidence(tmp_path):
    code = '''def run(ctx):
    try:
        ctx.subagents.run()
    except RuntimeError:
        pass
    return ctx.result.hold(reason="caught unavailable input")
'''
    result, _ = replay(tmp_path, code, {})
    metrics = assemble_metrics(result)
    assert metrics["performance_evidence"] is False
    assert metrics["replay"]["mock_surface_calls"] == [{"surface": "subagents", "mode": "error", "count": 6}]


def test_producer_receipt_report_and_chart_inherit_worker_evidence(tmp_path, monkeypatch):
    from nerya.skills.builtin.backtest.scripts.backtest_run import run_strategy_backtest

    root, cfg, rows, _, _ = prepare_replay(tmp_path)
    yaml_io.dump(tmp_path / "nerya.yml", {"runtime": {"live_trading_enabled": False}})
    monkeypatch.setattr("nerya.skills.builtin.backtest.scripts.backtest_run.get_candles",
                        lambda *args, **kwargs: rows)
    # Evidence-vault storage is outside this producer contract and test workspace.
    monkeypatch.setattr("nerya.evidence.autoingest.on_backtest_finalize", lambda *args, **kwargs: None)
    receipt = run_strategy_backtest(package_dir=root, workspace=tmp_path, allow_mock=True,
        settings={"tf": "1h", "warmup_bars": 0, "start_utc": str(rows[0]["ts"]),
            "end_utc": str(rows[-1]["ts"] + 3600), "mock_surfaces": cfg.asdict()["mock_surfaces"]})
    metrics = json.loads(Path(receipt["metrics_path"]).read_text())
    chart = json.loads((Path(receipt["run_path"]) / "chart.json").read_text())
    assert receipt["performance_evidence"] is metrics["performance_evidence"] is chart["meta"]["performance_evidence"] is False
    assert receipt["replay"]["mock_surface_calls"] == [{"surface": "subagents", "mode": "stub", "count": 1}]
    assert receipt["equity_preview"] == []
    assert "total_return_pct" not in receipt["metrics_display"]
    assert "no Agent trading performance claim" in receipt["operator_summary"]["evidence_scope"]
    assert "performance_evidence: false" in (tmp_path / receipt["report_path"]).read_text()
    assert metrics["provenance"]["data_kind"] == "sample"
