from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from nerya.api import routes_evolution
from nerya.core.config import Config
from nerya.core.paths import WorkspacePaths
from nerya.evolution.evidence_resolver import resolve_evidence_refs
from nerya.evolution.patch_proposal import create_proposal
from nerya.evolution.post_apply_observation import record_post_apply_observation
from nerya.evolution.promotion import apply_proposal
from nerya.strategies.evolution import _tuning_asset_selection_signals
from nerya.strategies.package import load_package
from nerya.strategies.performance import build_snapshot
from nerya.strategies.runner import StrategyRunner
from nerya.strategies.state import StrategyVersionRegistry
from nerya.tools.native.evolve import evolve_post_apply_observation_handler
from nerya.tools.types import ToolCall, ToolErrorKind


pytestmark = pytest.mark.smoke


def _route_map():
    return {(method, path): handler for method, path, handler in routes_evolution.routes()}


def test_post_apply_observation_route_updates_detail_fitness_and_timeline(tmp_path):
    paths = WorkspacePaths(root=tmp_path)
    client = SimpleNamespace(config=Config(paths=paths, data={}))
    proposal = create_proposal(
        paths,
        kind="learning_update",
        summary="applied learning",
        initial_state="applied",
        evidence_refs=["turn:t_apply"],
        metadata={"strategy_id": "alpha"},
    )
    routes = _route_map()

    out = routes[("POST", "/evolution/post_apply_observation")](
        client,
        {
            "proposal_id": proposal.id,
            "source": "backtest",
            "summary": "post-apply backtest stayed inside risk",
            "backtest_result": {
                "ok": True,
                "coverage_ok": True,
                "verdict": "PASS",
                "metrics": {"total_return_pct": 1.7},
            },
            "evidence_refs": ["validation:vrn_alpha:step:0"],
            "run_id": "bt_alpha_post_apply",
        },
    )

    assert out["ok"] is True
    assert out["status"] == "healthy"
    assert out["journal_ref"].startswith("journal:evolution:")
    assert "validation:vrn_alpha:step:0" in out["evidence_refs"]

    detail = routes[("POST", "/evolution/proposals/{proposal_id}")](
        client,
        {"proposal_id": proposal.id},
    )
    assert detail["post_apply_monitor"]["status"] == "healthy"
    weighted = detail["post_apply_monitor"]["weighted_summary"]
    assert weighted["count"] == 1
    assert weighted["by_status"] == {"healthy": 1}
    assert weighted["weighted_healthy_count"] == 1.0
    assert weighted["decay"]["source_weight_cap"] == 3.0
    dims = {row["id"]: row for row in detail["fitness_vector"]["dimensions"]}
    assert dims["post_apply"]["status"] == "passed"
    assert out["journal_ref"] in dims["post_apply"]["evidence_refs"]

    timeline = routes[("POST", "/evolution/timeline")](client, {"limit": 50})
    groups = {group["id"]: group for group in timeline["inbox"]["groups"]}
    assert any(
        entry["proposal_id"] == proposal.id
        and "post_apply_observation:healthy" in entry["reasons"]
        for entry in groups["reusable_learning"]["items"]
    )


def test_post_apply_observation_route_requires_applied_proposal_and_evidence(tmp_path):
    paths = WorkspacePaths(root=tmp_path)
    client = SimpleNamespace(config=Config(paths=paths, data={}))
    proposal = create_proposal(
        paths,
        kind="learning_update",
        summary="draft learning",
        initial_state="pending_review",
    )
    route = _route_map()[("POST", "/evolution/post_apply_observation")]

    not_applied = route(
        client,
        {
            "proposal_id": proposal.id,
            "status": "healthy",
            "evidence_refs": ["validation:vrn_alpha:step:0"],
        },
    )
    assert not_applied["_status"] == 409
    assert not_applied["error"] == "proposal_not_applied"

    applied = create_proposal(
        paths,
        kind="learning_update",
        summary="applied learning",
        initial_state="applied",
    )
    no_evidence = route(
        client,
        {
            "proposal_id": applied.id,
            "status": "healthy",
            "summary": "looks fine",
        },
    )
    assert no_evidence["_status"] == 400
    assert no_evidence["error"] == "evidence_required"


def test_evolve_post_apply_observation_native_tool_records_regression(tmp_path):
    paths = WorkspacePaths(root=tmp_path)
    proposal = create_proposal(
        paths,
        kind="learning_update",
        summary="applied risky learning",
        initial_state="applied",
        metadata={"strategy_id": "alpha"},
    )

    result = evolve_post_apply_observation_handler(
        ToolCall(
            name="evolve_post_apply_observation",
            arguments={
                "proposal_id": proposal.id,
                "source": "paper",
                "status": "regressed",
                "summary": "paper run showed larger drawdown",
                "metrics": {"max_drawdown_pct": -12.5},
                "evidence_refs": ["file:evolution/paper/alpha.json"],
            },
        ),
        config=Config(paths=paths),
    )

    assert not result.is_error
    data = result.content[0].data
    assert data["ok"] is True
    assert data["status"] == "regressed"
    assert data["next_step"] == "review_rollback_or_negative_capsule"

    bad = evolve_post_apply_observation_handler(
        ToolCall(
            name="evolve_post_apply_observation",
            arguments={"proposal_id": "prp_missing", "metrics": {"x": 1}},
        ),
        config=Config(paths=paths),
    )
    assert bad.is_error
    assert bad.error is not None
    assert bad.error.kind == ToolErrorKind.NOT_FOUND


def test_strategy_run_records_post_apply_observation_for_applied_version(tmp_path):
    paths = WorkspacePaths(root=tmp_path)
    proposal = create_proposal(
        paths,
        kind="strategy_package_proposal",
        summary="Promote alpha strategy",
        target="strategies/alpha",
        initial_state="approved",
        evidence_refs=["turn:t_alpha"],
        metadata={"strategy_id": "alpha"},
        extra_files={
            "after/strategies/alpha/strategy.yml": """
version: 1
strategy_id: alpha
title: Alpha strategy
mode: paper
entrypoint: main.py:run
markets: [mock:BTC/USDT]
accounts: [paper_main]
schedule: {type: interval, every_seconds: 60}
policy:
  min_confidence: 0
""",
            "after/strategies/alpha/main.py": (
                "def run(ctx):\n"
                "    return ctx.result.hold(reason='post apply paper tick')\n"
            ),
        },
    )
    (proposal.path / "validation_report.json").write_text(
        json.dumps({"ok": True, "issues": []}),
        encoding="utf-8",
    )

    applied = apply_proposal(paths, proposal.id)

    assert applied["ok"] is True
    versions = StrategyVersionRegistry(paths, "alpha").list()
    assert versions
    assert versions[0].proposal_id == proposal.id

    record = StrategyRunner(config=Config(paths=paths, data={})).run_tick(
        "alpha",
        mode_override="paper",
        run_id="run_post_apply_paper",
    )

    assert record.status == "hold"
    rows = [
        row for row in paths.journal("evolution").read_text(encoding="utf-8").splitlines()
        if "proposal.post_apply_observation" in row
    ]
    assert rows
    observations = _route_map()[("POST", "/evolution/timeline")](
        SimpleNamespace(config=Config(paths=paths, data={})),
        {"strategy_id": "alpha", "limit": 50},
    )
    item = next(
        row for row in observations["timeline"]
        if row.get("proposal_id") == proposal.id
    )
    monitor = item["post_apply_monitor"]
    assert monitor["status"] == "observing"
    assert monitor["weighted_summary"]["count"] == 1
    assert monitor["weighted_summary"]["weighted_observing_count"] == 1.0
    latest = monitor["latest"]
    assert latest["source"] == "strategy_run_paper"
    assert latest["run_id"] == "run_post_apply_paper"
    assert latest["metrics"]["run_status"] == "hold"
    assert latest["metrics"]["mode"] == "paper"
    run_ref = f"file:strategies/alpha/runs/{record.run_id}.json"
    assert run_ref in latest["evidence_refs"]

    resolved = resolve_evidence_refs(paths, [run_ref])
    assert resolved["items"][0]["resolved"] is True

    package = load_package(paths, "alpha")
    snapshot = build_snapshot(
        paths,
        "alpha",
        package=package,
        config_like=Config(paths=paths, data={}),
    )
    context = snapshot.evolution_context
    assert context["post_apply_observation_count"] == 1
    assert context["recent_observations"][0]["run_id"] == "run_post_apply_paper"
    assert context["by_source"]["strategy_run_paper"] == 1
    signals = _tuning_asset_selection_signals(package, snapshot, "tune_post_apply")
    assert "post_apply_observation" in {signal["kind"] for signal in signals}

    for idx in range(2, 10):
        recorded = record_post_apply_observation(
            paths,
            proposal_id=proposal.id,
            status="observing",
            source="strategy_run_paper",
            observed_at=f"2026-06-{10 + idx:02d}T00:00:00+00:00",
            evidence_refs=[f"file:strategies/alpha/runs/run_post_apply_paper_{idx}.json"],
            metrics={"mode": "paper", "run_status": "hold"},
            run_id=f"run_post_apply_paper_{idx}",
        )
        assert recorded["ok"] is True

    snapshot = build_snapshot(
        paths,
        "alpha",
        package=package,
        config_like=Config(paths=paths, data={}),
    )
    context = snapshot.evolution_context
    assert context["post_apply_observation_count"] == 9
    assert context["by_source"]["strategy_run_paper"] == 9
    assert context["weighted_by_source"]["strategy_run_paper"] == 3.0
    assert context["weighted_observing_count"] == 3.0
    signals = _tuning_asset_selection_signals(package, snapshot, "tune_post_apply_capped")
    signal = next(row for row in signals if row["kind"] == "post_apply_observation")
    assert signal["metadata"]["raw_recent_count"] == 9
    assert signal["metadata"]["weighted_observing_count"] == 3.0
