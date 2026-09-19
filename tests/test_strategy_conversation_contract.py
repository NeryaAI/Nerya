"""Regression for observable failures from real main-Agent strategy turns."""
from pathlib import Path
from types import SimpleNamespace

import pytest
from nerya.skills.builtin.backtest.scripts.backtest_run import _metrics_display, _operator_summary, _operator_summary_text
from nerya.tools.native.strategy_runtime import _model_facing_backtest_result

pytestmark = pytest.mark.smoke


def test_real_dates_survive_model_facing_display_conversion():
    metrics = {"start_utc": "2026-03-20T03:00:00Z", "end_utc": "2026-04-20T23:00:00Z",
               "requested_window_days": 180, "backtest_days": 31.8333, "tf": "1h", "verdict": "FAIL",
               "total_return_pct": -0.0367, "total_trades": 9}
    display = _metrics_display(metrics)
    text = _operator_summary_text(_operator_summary(metrics))
    result = _model_facing_backtest_result({"metrics": metrics, "metrics_display": display,
        "operator_summary_text": text, "metrics_path": "backtests/review/metrics.json"})
    assert result["metrics"]["start_utc"] == metrics["start_utc"]
    assert result["metrics"]["end_utc"] == metrics["end_utc"]
    assert result["metrics"]["requested_window_days"] == "180"
    assert "2026-03-20T03:00:00Z" in result["operator_summary_text"]
    assert "2026-04-20T23:00:00Z" in result["operator_summary_text"]
    assert "Requested window days: 180" in result["operator_summary_text"]
    assert result["metrics"]["total_return_pct"] == "-0.0367%"


def test_missing_dates_are_not_invented():
    display = _metrics_display({"tf": "1h", "total_trades": 0})
    text = _operator_summary_text(_operator_summary({"tf": "1h", "total_trades": 0}))
    assert "start_utc" not in display and "end_utc" not in display
    assert "Actual start UTC" not in text and "Actual end UTC" not in text
    assert display["total_trades"] == "0"


def test_workflow_hub_has_no_separate_creation_wizard_or_generator():
    root = Path(__file__).resolve().parents[1]
    hub = (root / "dashboard/components/workflows/StrategyWorkflowHub.tsx").read_text()
    assert "WorkflowCreate" not in hub
    assert "setComposeDraftPayload" not in hub
    assert "workflowApi.template" not in hub
    assert "newPrompt" not in hub and "新建策略" not in hub
    assert "主 Agent 对话" in hub
    assert "StrategyWorkflowPanel" in hub


def test_workflow_is_not_a_second_creation_or_verification_conversation():
    root = Path(__file__).resolve().parents[1] / "dashboard/components/workflows"
    panel = (root / "StrategyWorkflowPanel.tsx").read_text()
    evidence = (root / "WorkflowVerification.tsx").read_text()
    assert 'workflow-next-step' not in panel
    assert 'verificationPrompt' not in panel
    assert '验证记录' in panel and 'view === "runs"' in panel
    assert 'NodeInspector' in panel and 'WorkflowCommand' in panel
    assert 'onAgent' not in evidence and '让 Agent 继续验证' not in evidence
    assert 'exportVerification' in evidence


def test_missing_explicit_replay_config_is_not_a_default_run(tmp_path):
    from nerya.skills.builtin.backtest.scripts.config import load_config, BacktestConfigError
    with pytest.raises(BacktestConfigError, match="config not found"):
        load_config(config_path=tmp_path / "missing.yml")


def test_relative_config_uses_workspace_not_process_cwd(tmp_path, monkeypatch):
    from nerya.skills.builtin.backtest.scripts import backtest_run as replay
    from nerya.core import yaml_io
    workspace = tmp_path / "workspace"
    process = tmp_path / "application"
    workspace.mkdir(); process.mkdir()
    yaml_io.dump(workspace / "replay.yml", {"window_days": 30, "tf": "1h", "warmup_bars": 50})
    yaml_io.dump(process / "replay.yml", {"window_days": 180})
    monkeypatch.chdir(process)
    monkeypatch.setattr(replay, "load_workspace_config", lambda *_: SimpleNamespace(paths=SimpleNamespace(root=workspace)))
    monkeypatch.setattr(replay, "_load_target_package", lambda *_: SimpleNamespace(manifest=SimpleNamespace(markets=["BINANCE:BTCUSDT"])))
    def capture(**kwargs):
        from nerya.skills.builtin.backtest.scripts.config import load_config
        assert Path(kwargs["config_path"]) == workspace / "replay.yml"
        assert load_config(**kwargs).window_days == 30
        raise RuntimeError("config correctly resolved before data or execution")
    monkeypatch.setattr(replay, "load_config", capture)
    with pytest.raises(RuntimeError, match="correctly resolved"):
        replay.run_strategy_backtest(strategy_id="test", workspace=workspace, config_path="replay.yml")


def test_relative_replay_config_cannot_escape_workspace(tmp_path, monkeypatch):
    from nerya.skills.builtin.backtest.scripts import backtest_run as replay
    from nerya.core.errors import TradingError
    monkeypatch.setattr(replay, "load_workspace_config", lambda *_: SimpleNamespace(paths=SimpleNamespace(root=tmp_path)))
    with pytest.raises(TradingError, match="inside the workspace"):
        replay.run_strategy_backtest(strategy_id="test", workspace=tmp_path, config_path="../private.yml")
