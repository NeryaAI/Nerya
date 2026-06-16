from __future__ import annotations

from types import SimpleNamespace

import pytest

from nerya.api import routes_evolution
from nerya.core import jsonl
from nerya.core.config import Config
from nerya.core.paths import WorkspacePaths
from nerya.evolution.evidence_resolver import resolve_evidence_refs
from nerya.evolution.patch_proposal import create_proposal
from nerya.evolution.validation_plan import (
    build_validation_plan,
    run_validation_plan,
    write_validation_plan,
)

pytestmark = pytest.mark.smoke


def test_resolves_proposal_and_after_artifacts(tmp_path):
    paths = WorkspacePaths(tmp_path)
    proposal = create_proposal(
        paths,
        kind="strategy_tuning_proposal",
        summary="Tuned alpha",
        target="strategies/alpha",
        initial_state="pending_review",
        extra_files={
            "after/strategies/alpha/main.py": "def run(ctx):\n    return {'ok': True}\n",
            "materialization.json": '{"materialized": true}',
        },
    )

    out = resolve_evidence_refs(paths, [f"proposal:{proposal.id}"])

    item = out["items"][0]
    assert item["resolved"] is True
    assert item["type"] == "proposal"
    assert item["record"]["id"] == proposal.id
    assert any(
        artifact["path"].endswith("after/strategies/alpha/main.py")
        for artifact in item["artifacts"]
    )


def test_resolves_validation_run_step(tmp_path):
    paths = WorkspacePaths(tmp_path)
    sample = tmp_path / "test_sample.py"
    sample.write_text("def test_sample():\n    assert True\n", encoding="utf-8")
    plan = build_validation_plan(
        [{"type": "unit_test", "command": f"python -m pytest {sample} -q"}],
        source="test",
    )
    plan_id = write_validation_plan(paths, plan)
    result = run_validation_plan(paths, plan_id=plan_id, dry_run=False)
    ref = result["run"]["steps"][0]["evidence_ref"]

    out = resolve_evidence_refs(paths, [f"validation:{plan_id}", ref])

    plan_item, step_item = out["items"]
    assert plan_item["resolved"] is True
    assert plan_item["type"] == "validation_plan"
    assert step_item["resolved"] is True
    assert step_item["type"] == "validation_step"
    assert step_item["record"]["status"] == "passed"


def test_resolves_validation_backtest_step_artifacts(tmp_path, monkeypatch):
    paths = WorkspacePaths(tmp_path)
    metrics = paths.strategy("alpha") / "backtests" / "bt1" / "metrics.json"
    report = metrics.with_name("report.md")
    metrics.parent.mkdir(parents=True, exist_ok=True)
    metrics.write_text('{"verdict": "PASS"}', encoding="utf-8")
    report.write_text("# Backtest\n", encoding="utf-8")

    def fake_backtest(**kwargs):  # noqa: ANN003, ANN202
        return {
            "ok": True,
            "strategy_id": kwargs.get("strategy_id"),
            "backtest_ts": "bt1",
            "verdict": "PASS",
            "coverage_ok": True,
            "metrics_path": str(metrics),
            "report_path": str(report.relative_to(paths.root)),
            "out_dir": str(metrics.parent),
        }

    monkeypatch.setattr(
        "nerya.evolution.validation_plan.run_strategy_backtest",
        fake_backtest,
    )
    plan = build_validation_plan(
        [{"type": "backtest", "required": True}],
        source="test",
        strategy_id="alpha",
    )
    plan_id = write_validation_plan(paths, plan)
    result = run_validation_plan(paths, plan_id=plan_id, dry_run=False)

    out = resolve_evidence_refs(paths, [result["run"]["steps"][0]["evidence_ref"]])

    item = out["items"][0]
    assert item["resolved"] is True
    assert item["type"] == "validation_step"
    assert item["record"]["type"] == "backtest"
    assert any(artifact["path"].endswith("metrics.json") for artifact in item["artifacts"])
    assert any(artifact["preview"] == "# Backtest\n" for artifact in item["artifacts"])


def test_resolves_workspace_file_ref_and_blocks_outside_paths(tmp_path):
    paths = WorkspacePaths(tmp_path)
    metrics = paths.strategy("alpha") / "backtests" / "bt1" / "metrics.json"
    metrics.parent.mkdir(parents=True, exist_ok=True)
    metrics.write_text('{"verdict": "PASS"}', encoding="utf-8")
    outside = tmp_path.parent / "outside_metrics.json"
    outside.write_text("{}", encoding="utf-8")

    out = resolve_evidence_refs(paths, [f"file:{metrics}", f"file:{outside}"])

    inside, blocked = out["items"]
    assert inside["resolved"] is True
    assert inside["type"] == "file"
    assert inside["artifacts"][0]["preview"] == '{"verdict": "PASS"}'
    assert blocked["resolved"] is False
    assert blocked["reason"] == "file_outside_workspace"


def test_resolves_strategy_tuning_and_journal_refs(tmp_path):
    paths = WorkspacePaths(tmp_path)
    review = paths.strategy("alpha") / "reviews" / "tuning_tune_1.md"
    review.parent.mkdir(parents=True, exist_ok=True)
    review.write_text("# Review\n", encoding="utf-8")
    audit = paths.strategy("alpha") / "reviews" / "tuning_tune_1_audit.json"
    audit.write_text('{"ok": true, "secret": "sk-this-will-be-redacted-1234567890"}', encoding="utf-8")
    jsonl.append(
        paths.journal("strategy_evolution"),
        {
            "kind": "strategy.tuning",
            "run_id": "tune_1",
            "strategy_id": "alpha",
            "status": "ok",
            "review_path": str(review),
            "audit_path": str(audit),
        },
    )
    jsonl.append(
        paths.journal("agent"),
        {"kind": "agent.turn.start", "turn_id": "trn_1", "session_id": "ses_1"},
    )

    out = resolve_evidence_refs(
        paths,
        [
            "strategy_tuning:tune_1",
            "journal:agent:0",
            "turn:trn_1",
            "session:ses_1",
        ],
    )

    tuning, journal, turn, session = out["items"]
    assert tuning["resolved"] is True
    assert tuning["type"] == "strategy_tuning"
    assert len(tuning["artifacts"]) == 2
    assert journal["record"]["turn_id"] == "trn_1"
    assert turn["metadata"]["count"] == 1
    assert session["metadata"]["count"] == 1


def test_unresolved_refs_are_explicit(tmp_path):
    paths = WorkspacePaths(tmp_path)

    out = resolve_evidence_refs(paths, ["proposal:missing", "nonsense"])

    assert out["items"][0]["resolved"] is False
    assert out["items"][0]["reason"] == "proposal_not_found"
    assert out["items"][1]["resolved"] is False
    assert out["items"][1]["reason"] == "unsupported_ref"


def test_evidence_resolve_route(tmp_path):
    config = Config(paths=WorkspacePaths(tmp_path), data={})
    proposal = create_proposal(
        config.paths,
        kind="learning_update",
        summary="Learning",
        initial_state="pending_review",
    )
    route_map = {(method, path): handler for method, path, handler in routes_evolution.routes()}

    out = route_map[("POST", "/evolution/evidence/resolve")](
        SimpleNamespace(config=config),
        {"refs": [f"proposal:{proposal.id}"]},
    )

    assert out["ok"] is True
    assert out["items"][0]["resolved"] is True
    assert out["items"][0]["record"]["id"] == proposal.id
