"""Isolated replay regressions. All candle values here are controlled fixtures."""
from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from nerya.core import yaml_io
from nerya.data.history_store import HistoryStore
from nerya.skills.builtin.backtest.scripts.backtest_run import run_strategy_backtest
from nerya.skills.builtin.backtest.scripts.config import BacktestConfig
from nerya.skills.builtin.backtest.scripts.preflight import BacktestPreflightError

pytestmark = pytest.mark.smoke
T = 1700006400


def package(tmp_path, code="def run(ctx):\n    return ctx.result.hold(reason='test')\n", **settings):
    root = tmp_path / "strategies" / "pipeline_test"
    root.mkdir(parents=True)
    yaml_io.dump(tmp_path / "nerya.yml", {"runtime": {"live_trading_enabled": False}})
    yaml_io.dump(root / "strategy.yml", {"version": 1, "strategy_id": root.name,
        "title": "Pipeline fixture", "mode": "paper", "entrypoint": "main.py:run",
        "markets": ["BYBIT:BTCUSDT"], "accounts": ["fixture_account"],
        "execution_mode": "script", "schedule": {"type": "none", "enabled": False},
        "policy": {"allow_direct_order": True}})
    (root / "main.py").write_text(code)
    cfg = {"start_utc": str(T), "end_utc": str(T + 6 * 3600), "warmup_bars": 0,
           "data_mode": "local", "tf": "1h", **settings}
    yaml_io.dump(tmp_path / "replay.yml", cfg)
    return root


def rows(count=6):
    return [dict(ts=T + i * 3600, open=100+i, high=103+i, low=99+i, close=101+i, volume=10) for i in range(count)]


def seed(tmp_path, count=6):
    # A deterministic storage fixture; these rows are not presented as live or
    # market acceptance evidence. Separate external acceptance uses real data.
    HistoryStore(tmp_path / "artifacts/backtest_cache").put("BYBIT:BTCUSDT", "1h", rows(count), source="test_transport")


def replay(tmp_path, **kwargs):
    return run_strategy_backtest(strategy_id="pipeline_test", workspace=tmp_path, config_path="replay.yml", **kwargs)


@pytest.mark.parametrize("config", [{"unknown_key": 1}, {"allow_short": "false"}, {"tf": "1M"},
    {"initial_capital_usd": float("nan")}, {"fee_bps_by_venue": {"BYBIT": -1}}, {"warmup_bars": -1}])
def test_invalid_config_rejected(config):
    with pytest.raises(ValueError):
        BacktestConfig.from_raw(config)


@pytest.mark.parametrize("code, expected", [
    ("def run(ctx):\n return (\n", "syntax_error"),
    ("import definitely_missing_nerya_library\ndef run(ctx): pass\n", "dependency_missing"),
    ("def run(ctx):\n return ctx.trading.open_position(market='BYBIT:BTCUSDT', side='long', protection={'stop_loss': {'type':'atr','value':2.0}})\n", "unsupported_protection"),
])
def test_known_errors_before_network_or_execution(tmp_path, monkeypatch, code, expected):
    root = package(tmp_path, code, data_mode="download")
    def forbidden(*a, **kw):
        pytest.fail("preflight must run before network or candles")
    monkeypatch.setattr("nerya.skills.builtin.backtest.scripts.backtest_run.get_candles", forbidden)
    with pytest.raises(BacktestPreflightError) as failure:
        replay(tmp_path)
    assert expected in {row["code"] for row in failure.value.receipt["blockers"]}
    receipt = json.loads(Path(failure.value.failure_path).read_text())
    assert receipt["phase"] == "preflight" and receipt["status"] == "failed"
    assert not list(root.glob("backtests/**/metrics.json"))


@pytest.mark.parametrize("expression, expected", [
    ("series.shift(-1)", "lookahead_negative_shift"),
    ("series.diff(periods=-2)", "lookahead_negative_shift"),
    ("series.pct_change(-3)", "lookahead_negative_shift"),
    ("series.bfill()", "lookahead_backward_fill"),
    ("series.fillna(method='backfill')", "lookahead_backward_fill"),
    ("series.rolling(10, center=True).mean()", "lookahead_centered_window"),
    ("pd.merge_asof(left, right, on='ts', direction='forward')", "lookahead_forward_asof"),
    ("series.interpolate(limit_direction='both')", "lookahead_backward_interpolation"),
])
def test_static_preflight_warns_on_explicit_future_data_patterns(tmp_path, expression, expected):
    package(tmp_path, f"def run(ctx):\n    {expression}\n    return ctx.result.hold(reason='unreachable')\n")
    receipt = replay(tmp_path, preflight_only=True)["preflight"]
    assert receipt["ok"] and not receipt["blockers"]
    assert expected in {row["code"] for row in receipt["warnings"]}
    assert "temporal_bias_static_scan" in receipt["checks"]


def test_static_preflight_warns_when_shift_direction_is_dynamic(tmp_path):
    package(tmp_path, """def run(ctx):
    periods = ctx.config.extras.get("periods", 1)
    series.shift(periods=periods)
    return ctx.result.hold(reason="fixture")
""")
    result = replay(tmp_path, preflight_only=True)
    assert "lookahead_dynamic_shift" in {row["code"] for row in result["preflight"]["warnings"]}


def test_preflight_only_never_runs_user_top_level_code(tmp_path):
    marker = tmp_path / "should_not_exist"
    package(tmp_path, f"from pathlib import Path\nPath({str(marker)!r}).write_text('bad')\ndef run(ctx): pass\n")
    result = replay(tmp_path, preflight_only=True)
    assert result["ok"] and result["result_type"] == "backtest_preflight"
    assert not marker.exists()


def test_local_replay_receipts_source_and_data_snapshot(tmp_path, monkeypatch):
    package(tmp_path)
    seed(tmp_path)
    def forbidden(*a, **kw):
        pytest.fail("local replay cannot fetch network data")
    monkeypatch.setattr("nerya.skills.builtin.backtest.scripts.data_cache._source_fetch", forbidden)
    result = replay(tmp_path)
    assert result["ok"] and result["data_manifest"]["requested_window_complete"]
    folder = Path(result["run_path"])
    assert json.loads((folder / "run.json").read_text())["status"] == "completed"
    metrics = json.loads((folder / "metrics.json").read_text())
    assert metrics["bias_checks"]["static_temporal_scan"] == "passed"
    assert metrics["bias_checks"]["historical_prefix_only"] is True
    assert metrics["bias_checks"]["strategy_order_execution"] == "next_bar_open"
    assert metrics["bias_checks"]["end_of_data_signal"] == "rejected_no_next_bar"
    assert metrics["provenance"]["assumptions"]["execution_model_limits"]["funding"] == "not_modeled"
    assert result["bias_checks"] == metrics["bias_checks"]
    assert result["research_checks"] == metrics["research_checks"]
    assert metrics["research_checks"]["scope"] == "this_run_only"
    checks = {row["id"]: row["status"] for row in metrics["research_checks"]["checks"]}
    assert checks == dict.fromkeys(("dynamic_lookahead", "warmup_stability", "out_of_sample",
                                   "walk_forward", "cost_stress", "parameter_sensitivity", "ablation"), "not_run")
    chart = json.loads((folder / "chart.json").read_text())
    assert chart["meta"]["research_checks"] == metrics["research_checks"]
    report = (folder / "report.md").read_text()
    assert "| dynamic_lookahead | not_run |" in report
    assert "funding: not_modeled" in report
    assert "remaining orders use the recorded last close" not in report
    assert "falls back to signal close" not in metrics["provenance"]["assumptions"]["fill_rule"]
    assert (folder / "source/pipeline_test/main.py").is_file()
    assert json.loads((folder / "replay_input.json").read_text())["candles_by_market"]["BYBIT:BTCUSDT"] == rows()
    assert not (folder / "failure.json").exists()


def test_incomplete_local_history_runs_with_strict_policy_and_truthful_coverage(tmp_path):
    package(tmp_path, "def run(ctx):\n    if False:\n        series.shift(-1)\n    return ctx.result.hold(reason='test')\n",
            coverage_policy="strict", timeframes=["1h", "15m"])
    seed(tmp_path, 4)
    result = replay(tmp_path)
    assert result["ok"] and result["coverage_ok"] is False
    assert result["data_manifest"]["requested_window_complete"] is False
    assert result["data_manifest"]["datasets"][0]["missing_bars"] == 2
    assert "incomplete_history" in {row["code"] for row in result["data_manifest"]["warnings"]}
    assert "missing_timeframe" in {row["code"] for row in result["data_manifest"]["warnings"]}
    assert result["bias_checks"]["static_temporal_scan"] == "warnings"
    assert result["bias_checks"]["static_warnings"][0]["code"] == "lookahead_negative_shift"
    assert json.loads((Path(result["run_path"]) / "run.json").read_text())["status"] == "completed"
    assert len(HistoryStore(tmp_path / "artifacts/backtest_cache").read("BYBIT:BTCUSDT", "1h", T, T + 6 * 3600)) == 4


def test_partial_multiframe_and_protocol_guidance_do_not_force_cadence(tmp_path, monkeypatch):
    from dataclasses import replace
    from nerya.strategies.package import load_package_from_dir
    from nerya.skills.builtin.backtest.scripts import backtest_run, freeform_run
    from nerya.skills.builtin.backtest.scripts.preflight import inspect_package

    cfg = BacktestConfig.from_raw({"markets": ["BYBIT:BTCUSDT"], "warmup_bars": 0,
                                   "timeframes": ["1h", "4h"]})
    assert cfg.coverage_policy == BacktestConfig().coverage_policy == "allow_partial"
    cfg._window_start, cfg._window_end = T, T + 6 * 3600
    series = {"BYBIT:BTCUSDT": {"1h": rows(4)}}
    manifest = backtest_run._validate_replay_data(cfg, series, allow_mock=False)
    assert manifest["ok"] and manifest["requested_window_complete"] is False
    assert {"missing_timeframe", "incomplete_history"} <= {r["code"] for r in manifest["warnings"]}
    assert series["BYBIT:BTCUSDT"]["1h"] == rows(4) and "4h" not in series["BYBIT:BTCUSDT"]
    missing = backtest_run._validate_replay_data(cfg, {}, allow_mock=False)
    assert not missing["ok"] and missing["blockers"][0]["code"] == "missing_timeframe"
    malformed = {"BYBIT:BTCUSDT": {"1h": [{**row, "high": 0} for row in rows(4)]}}
    assert "invalid_ohlcv" in {r["code"] for r in backtest_run._validate_replay_data(
        cfg, malformed, allow_mock=False)["blockers"]}

    root = package(tmp_path, "def run(ctx):\n    series.shift(-1)\n")
    target = load_package_from_dir(root)
    target = replace(target, manifest=replace(target.manifest,
        extras={"params": {"indicator": {"fast_period": 2, "slow_period": 5}, "lookback": 2}},
        schedule=replace(target.manifest.schedule, type="cron", cron="0 * * * *")))
    monkeypatch.setattr("nerya.strategies.request_scope._protocol", lambda venue: {"market_types": ["lp", "lending"]})
    receipt, _ = inspect_package(target, cfg)
    assert receipt["ok"] and not receipt["blockers"]
    assert {"lookahead_negative_shift", "protocol_replay_model_required", "insufficient_closed_history_lookback"} <= {
        r["code"] for r in receipt["warnings"]}

    monkeypatch.setattr(freeform_run, "_load_target_package", lambda *args: target)
    monkeypatch.setattr(freeform_run, "_find_script", lambda path: root / "main.py")
    monkeypatch.setattr(freeform_run, "uses_host_lp_replay", lambda *args: True)
    def stop_generic(*args, **kwargs):
        raise RuntimeError("generic preparation selected")
    def stop_cadence(*args, **kwargs):
        raise RuntimeError("explicit cadence selected")
    monkeypatch.setattr("nerya.skills.builtin.backtest.scripts.replay_inputs.prepare_inputs", stop_generic)
    monkeypatch.setattr("nerya.skills.builtin.backtest.scripts.lp_state_requirements.prepare_lp_cadence", stop_cadence)
    with pytest.raises(RuntimeError, match="generic preparation selected"):
        freeform_run.run_freeform_backtest(strategy_id="pipeline_test", workspace=tmp_path)
    target.manifest.extras["params"]["lp_replay"] = {"mode": "cadence"}
    with pytest.raises(RuntimeError, match="explicit cadence selected"):
        freeform_run.run_freeform_backtest(strategy_id="pipeline_test", workspace=tmp_path)
    metrics = freeform_run._build_metrics(payload={"performance_evidence": True, "coverage_ok": True},
        equity_rows=[{"ts": T, "equity": 100}, {"ts": T + 3600, "equity": 101}],
        trade_rows=[], strategy_id="pipeline_test", proposal_id=None, script_path=root / "main.py")
    assert metrics["performance_evidence"] is False and metrics["coverage_ok"] is None


def test_dynamic_failure_isolated_and_recorded(tmp_path):
    package(tmp_path, "def run(ctx):\n raise RuntimeError('controlled branch failure')\n")
    seed(tmp_path)
    with pytest.raises(RuntimeError) as error:
        replay(tmp_path)
    receipt = json.loads(Path(error.value.failure_path).read_text())
    assert receipt["phase"] == "replaying"
    assert "controlled branch failure" in receipt["error"]["message"]
    assert receipt["error"]["market"] == "BYBIT:BTCUSDT"


def test_hung_strategy_worker_is_stopped_by_parent_deadline(tmp_path):
    package(tmp_path, "def run(ctx):\n while True: pass\n", max_run_seconds=0.7)
    seed(tmp_path)
    start = time.monotonic()
    with pytest.raises(TimeoutError) as error:
        replay(tmp_path)
    assert time.monotonic() - start < 8
    assert Path(error.value.failure_path).exists()


def test_worker_does_not_fetch_current_data(tmp_path):
    package(tmp_path, "import socket\ndef run(ctx):\n socket.create_connection(('example.com', 443))\n")
    seed(tmp_path)
    with pytest.raises(RuntimeError, match="Network access is disabled"):
        replay(tmp_path)


def test_explicit_partial_research_is_not_full_coverage(tmp_path):
    package(tmp_path, coverage_policy="allow_partial")
    seed(tmp_path, 4)
    result = replay(tmp_path)
    assert result["ok"] and result["coverage_ok"] is False
    assert "NOT a complete" in result["coverage_message"]
