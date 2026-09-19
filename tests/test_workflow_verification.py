"""Product checks are not trading approval; test real contracts, not UI wording."""
from copy import deepcopy
import json
from pathlib import Path
import runpy
from unittest.mock import patch
from types import SimpleNamespace

import pytest
from nerya.core import yaml_io
from nerya.strategies.verification import check_workflow, replay_status, source_revision, replay_provenance
from nerya.strategies.workflow_service import source_files, propose_workflow, view_workflow
from nerya.strategies.workflow_graph import WorkflowError

pytestmark = pytest.mark.smoke


def seed(root):
    fn = runpy.run_path(str(Path(__file__).with_name("test_strategy_agent_context.py")))["seed"]
    return fn(root, team=False)[:2]


def report(revision="same", **metrics):
    return {"id": "20260918_100000", "metrics": {"verdict": "PASS", "coverage_ok": True,
        "timeframe_fallback": False, "missing_timeframes": {}, "requested_window_days":1, "backtest_days":1, "tf":"15m",
        "provenance": {"version": 1, "strategy_id": "flow_context", "proposal_id": None,
            "source_revision": revision, "data_kind": "historical", "source_changed_during_run": False,
            "datasets": [{"market": "BINANCE:BTCUSDT", "timeframe": "15m", "rows": 160,
                          "sha256": "a"*64, "first_ts": 10, "last_ts": 20,
                          "duplicate_timestamps": 0, "out_of_order": False}]}, **metrics}}


def write_report(root, value):
    target = root / "backtests" / value["id"]
    target.mkdir(parents=True, exist_ok=True)
    (target / "metrics.json").write_text(json.dumps(value["metrics"]))


def test_readonly_check_never_imports_or_calls_strategy(tmp_path):
    cfg, pkg = seed(tmp_path)
    before = {str(p.relative_to(tmp_path)): p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()}
    with patch("nerya.strategies.validator._smoke_test_import", side_effect=AssertionError("must not execute")):
        out = check_workflow(cfg.paths, pkg.strategy_id, schedules=[])
    assert out["validation"]["ok"] and out["validation"]["scope"] == "schema_and_ast_only"
    files, _ = source_files(cfg.paths, pkg.strategy_id)
    assert out["target"]["source_revision"] == source_revision(files)
    assert out["replay"]["status"] == "missing" and out["operation"]["state"] == "not_installed"
    assert before == {str(p.relative_to(tmp_path)): p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()}


def test_expected_revision_conflict_is_not_a_pass(tmp_path):
    cfg, pkg = seed(tmp_path)
    with pytest.raises(WorkflowError, match="revision_conflict"):
        check_workflow(cfg.paths, pkg.strategy_id, base_revision="old")


def test_graph_layout_is_not_execution_source_revision():
    a = {"main.py": "x=1", "workflow.json": "old"}
    assert source_revision(a) == source_revision({**a, "workflow.json": "new"})
    assert source_revision(a) != source_revision({**a, "main.py": "x=2"})


@pytest.mark.parametrize("change,state", [
    ({}, "verified"), ({"verdict":"FAIL"}, "failed"), ({"verdict":"WARN"}, "limited"),
    ({"coverage_ok":False}, "limited"), ({"coverage_ok":None}, "limited"),
    ({"backtest_days":0.2}, "limited"), ({"requested_window_days":None}, "limited"),
    ({"timeframe_fallback":True}, "limited"), ({"timeframe_fallback":None}, "limited"),
    ({"missing_timeframes":{"BTC":{"1d":"missing"}}}, "limited"),
    ({"provenance":None}, "unbound"),
])
def test_replay_status_requires_explicit_evidence(change, state):
    assert replay_status(report(**change), "same", "flow_context", None)["status"] == state


@pytest.mark.parametrize("change,state", [
    ({"source_revision":"old"}, "stale"), ({"source_changed_during_run":True}, "stale"),
    ({"data_kind":"sample"}, "sample"), ({"data_kind":"unverified"}, "unbound"),
    ({"strategy_id":"other"}, "unbound"), ({"proposal_id":"prp_other"}, "unbound"),
    ({"datasets":[]}, "limited"), ({"datasets":None}, "limited"),
])
def test_provenance_mismatch_sample_and_empty_data_are_not_pass(change,state):
    r=report(); r["metrics"]["provenance"].update(change)
    assert replay_status(r,"same","flow_context",None)["status"] == state


def test_exact_version_artifact_is_invalidated_by_code_change(tmp_path):
    cfg,pkg=seed(tmp_path)
    files,_=source_files(cfg.paths,pkg.strategy_id)
    write_report(pkg.root,report(revision=source_revision(files)))
    assert check_workflow(cfg.paths,pkg.strategy_id)["replay"]["status"] == "verified"
    path=pkg.root/"main.py"; path.write_text(path.read_text()+"\n# new logic version\n")
    assert check_workflow(cfg.paths,pkg.strategy_id)["replay"]["status"] == "stale"


def test_candidate_does_not_borrow_active_replay(tmp_path):
    cfg,pkg=seed(tmp_path)
    files,_=source_files(cfg.paths,pkg.strategy_id)
    write_report(pkg.root,report(revision=source_revision(files)))
    view=view_workflow(cfg.paths,pkg.strategy_id)
    saved=propose_workflow(cfg.paths,{"strategy_id":pkg.strategy_id,"base_revision":view["revision"],
        "changes":[{"node_id":"script:main.py","content":files["main.py"]+"\n# candidate\n"}],
        "metadata":view["metadata"]})
    assert saved["ok"]
    out=check_workflow(cfg.paths,pkg.strategy_id,saved["proposal_id"],schedules=[{"strategy_id":pkg.strategy_id,"enabled":True}])
    assert out["replay"]["status"] == "missing" and out["operation"]["state"] == "candidate"
    assert out["operation"]["installed_schedules"] == []


def test_symlink_report_is_not_followed(tmp_path):
    cfg,pkg=seed(tmp_path)
    external=tmp_path/"external"; external.mkdir()
    (pkg.root/"backtests").symlink_to(external,target_is_directory=True)
    out=check_workflow(cfg.paths,pkg.strategy_id)
    assert out["replay"]["status"] == "missing" and out["report_warnings"] == ["unsafe_report_directory"]


def test_corrupt_newer_report_prevents_clean_pass_from_older(tmp_path):
    cfg,pkg=seed(tmp_path); files,_=source_files(cfg.paths,pkg.strategy_id)
    write_report(pkg.root,report(revision=source_revision(files)))
    later=pkg.root/"backtests"/"zz_invalid"; later.mkdir(); (later/"metrics.json").write_text("invalid")
    out=check_workflow(cfg.paths,pkg.strategy_id)
    assert out["replay"]["status"] == "limited" and out["report_warnings"]


def test_syntax_error_names_actual_file(tmp_path):
    cfg,pkg=seed(tmp_path); (pkg.root/"main.py").write_text("def run(ctx)\n  return None\n")
    out=check_workflow(cfg.paths,pkg.strategy_id)
    assert not out["validation"]["ok"] and out["next_step"] == "repair"
    assert any("main.py" in i["where"] for i in out["validation"]["blockers"])


def test_dataset_digest_changes_with_values_and_preserves_zero(tmp_path):
    from nerya.skills.builtin.backtest.scripts.config import BacktestConfig
    _,pkg=seed(tmp_path); cfg=BacktestConfig()
    data={"mock:BTCUSDT":{"15m":[{"ts":1,"close":100,"volume":0},{"ts":2,"close":101,"volume":0}]}}
    a=replay_provenance(pkg,cfg,data,proposal_id=None,allow_mock=False)
    data["mock:BTCUSDT"]["15m"][1]["close"]=102
    b=replay_provenance(pkg,cfg,data,proposal_id=None,allow_mock=False)
    assert a["data_kind"] == "sample" and a["datasets"][0]["rows"] == 2
    assert a["datasets"][0]["sha256"] != b["datasets"][0]["sha256"]


def test_real_standard_replay_writes_provenance(tmp_path,monkeypatch):
    from nerya.strategies.package import load_package
    from nerya.skills.builtin.backtest.scripts import backtest_run as replay
    cfg,pkg=seed(tmp_path)
    raw=yaml_io.load(pkg.root/"strategy.yml"); raw["evaluation"]={"mode":"observation"}
    yaml_io.dump(pkg.root/"strategy.yml",raw)
    (pkg.root/"main.py").write_text('def run(ctx):\n    return {"status":"ok","reason":"controlled engine check"}\n')
    monkeypatch.setattr(replay,"load_workspace_config",lambda *_:cfg)
    def series(config,**_):
        config.tf="15m"; config.timeframes=["15m"]
        rows=[{"ts":1700000000+i*900,"open":100,"high":101,"low":99,"close":100,"volume":0,"fixture":"controlled"} for i in range(240)]
        return {m:{"15m":deepcopy(rows)} for m in config.markets},["15m"],{}
    monkeypatch.setattr(replay,"_load_candles_with_timeframe_fallback",series)
    out=replay.run_strategy_backtest(strategy_id=pkg.strategy_id,workspace=tmp_path,allow_mock=True)
    stored=json.loads(Path(out["metrics_path"]).read_text())
    assert stored["provenance"] == out["provenance"]
    assert out["provenance"]["data_kind"] == "sample"
    assert out["provenance"]["source_changed_during_run"] is False
    assert check_workflow(cfg.paths,pkg.strategy_id)["replay"]["status"] == "sample"
    assert "Sample data only" in stored["coverage_message"]
    assert stored["replay"]["agent_execution"] == "not_run"
    assert stored["replay"]["order_attempts"] == 0


@pytest.mark.parametrize("change", [{"rows":0}, {"rows":True}, {"sha256":"missing"}, {"first_ts":None}, {"last_ts":0}, {"out_of_order":True}, {"duplicate_timestamps":1}])
def test_incomplete_dataset_is_not_verified(change):
    r=report(); r["metrics"]["provenance"]["datasets"][0].update(change)
    assert replay_status(r,"same","flow_context",None)["status"] == "limited"


def test_non_object_dataset_is_not_verified():
    r=report(); r["metrics"]["provenance"]["datasets"]=[None]
    assert replay_status(r,"same","flow_context",None)["status"] == "limited"


def test_replay_uses_real_multifile_entrypoint_without_module_leak(tmp_path):
    import sys
    from nerya.skills.builtin.backtest.scripts.engine import _load_run_fn
    for name, number in [("one",17),("two",92)]:
        _,pkg=seed(tmp_path/name)
        (pkg.root/"local_factor.py").write_text(f"FACTOR = {number}\n")
        (pkg.root/"decision.py").write_text("from local_factor import FACTOR\ndef evaluate(ctx):\n    return FACTOR\n")
        raw=yaml_io.load(pkg.root/"strategy.yml");raw["entrypoint"]="decision.py:evaluate"
        yaml_io.dump(pkg.root/"strategy.yml",raw)
        fn=_load_run_fn(pkg.root)
        assert fn(None)==number
        assert "local_factor" not in sys.modules
        assert str(pkg.root) not in sys.path


def test_read_scope_cannot_execute_or_activate():
    from nerya.api.route_scopes import authorize
    assert authorize(["read:runtime"],"GET","/strategies/runtime/workflow/check")[0]
    assert not authorize(["read:runtime"],"POST","/strategies/runtime/run_tick")[0]
    assert not authorize(["read:runtime"],"POST","/strategies/runtime/schedule")[0]
