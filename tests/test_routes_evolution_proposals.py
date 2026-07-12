import json
from types import SimpleNamespace

import pytest

from nerya.api.routes_evolution import _proposal_detail_dict, routes
from nerya.core import yaml_io
from nerya.core.paths import WorkspacePaths
from nerya.evolution.patch_proposal import create_proposal
from nerya.evolution.validation_plan import build_validation_plan, write_validation_plan
from nerya.strategies.package import load_package

pytestmark = pytest.mark.smoke


def _write_metrics(root, ts: str, metrics: dict):
    out = root / "backtests" / ts
    out.mkdir(parents=True, exist_ok=True)
    (out / "metrics.json").write_text(json.dumps(metrics), encoding="utf-8")
    (out / "report.md").write_text("# Report\n", encoding="utf-8")
    return out


def _seed_alpha_package(paths: WorkspacePaths, *, main: str | None = None):
    root = paths.strategy("alpha")
    yaml_io.dump(
        root / "strategy.yml",
        {
            "version": 1,
            "strategy_id": "alpha",
            "mode": "paper",
            "entrypoint": "main.py:run",
            "schedule": {"type": "cron", "cron": "*/5 * * * *"},
        },
    )
    (root / "main.py").write_text(
        main or "def run(ctx):\n    return {'ok': False}\n",
        encoding="utf-8",
    )
    return load_package(paths, "alpha")


def test_proposal_detail_includes_strategy_package_files(tmp_path):
    paths = WorkspacePaths(root=tmp_path)
    strategy_root = paths.strategy("btc_macd_agent")
    strategy_root.mkdir(parents=True, exist_ok=True)
    (strategy_root / "main.py").write_text(
        "def run(ctx):\n    return 'old'\n",
        encoding="utf-8",
    )
    proposal = create_proposal(
        paths,
        kind="strategy_package_proposal",
        summary="BTC MACD agent",
        extra_files={
            "after/strategies/btc_macd_agent/strategy.yml": (
                "strategy_id: btc_macd_agent\nexecution_mode: agent\n"
            ),
            "after/strategies/btc_macd_agent/main.py": "def run(ctx):\n    return 'macd'\n",
        },
    )

    detail = _proposal_detail_dict(proposal)

    assert detail["files"]["strategy.yml"].startswith("strategy_id: btc_macd_agent")
    assert "execution_mode: agent" in detail["files"]["strategy.yml"]
    assert "macd" in detail["files"]["main.py"]
    main_change = next(
        row for row in detail["file_changes"]
        if row["path"] == "strategies/btc_macd_agent/main.py"
    )
    assert main_change["before_exists"] is True
    assert "return 'old'" in main_change["before"]
    assert "return 'macd'" in main_change["after"]
    assert "--- before/strategies/btc_macd_agent/main.py" in main_change["diff"]
    assert "+++ after/strategies/btc_macd_agent/main.py" in main_change["diff"]


def test_proposal_detail_includes_backtest_before_after_comparison(tmp_path):
    paths = WorkspacePaths(root=tmp_path)
    strategy_root = paths.strategy("alpha")
    strategy_root.mkdir(parents=True, exist_ok=True)
    plan = build_validation_plan(
        [{"type": "manual_review", "required": True}],
        source="test",
        strategy_id="alpha",
    )
    plan_id = write_validation_plan(paths, plan)
    plan_path = paths.evolution_validation_plans / f"{plan_id}.json"
    plan_record = json.loads(plan_path.read_text(encoding="utf-8"))
    plan_record["status"] = "passed"
    plan_record["steps"][0]["status"] = "passed"
    plan_record["steps"][0]["evidence_ref"] = "validation:vrn_alpha:step:0"
    plan_path.write_text(json.dumps(plan_record), encoding="utf-8")
    _write_metrics(
        strategy_root,
        "20260101_000000",
        {
            "verdict": "WARN",
            "total_return_pct": 1.0,
            "max_drawdown_pct": 5.0,
            "sharpe_ratio": 0.5,
            "total_trades": 4,
            "tf": "1h",
            "coverage_ok": True,
        },
    )
    proposal = create_proposal(
        paths,
        kind="strategy_tuning_proposal",
        summary="Tune alpha",
        target="strategies/alpha",
        validation_plan_id=plan_id,
        metadata={"strategy_id": "alpha"},
        extra_files={
            "after/strategies/alpha/main.py": "def run(ctx):\n    return ctx.result.hold(reason='after')\n",
            "after/strategies/alpha/backtests/20260102_000000/metrics.json": json.dumps(
                {
                    "verdict": "PASS",
                    "total_return_pct": 2.5,
                    "max_drawdown_pct": 3.0,
                    "sharpe_ratio": 0.9,
                    "total_trades": 5,
                    "tf": "1h",
                    "coverage_ok": True,
                }
            ),
            "after/strategies/alpha/backtests/20260102_000000/report.md": "# After report\n",
        },
    )

    detail = _proposal_detail_dict(proposal, workspace_root=tmp_path)

    comparison = detail["backtest_comparison"]
    assert comparison["status"] == "complete"
    assert comparison["strategy_id"] == "alpha"
    assert comparison["before"]["backtest_id"] == "20260101_000000"
    assert comparison["after"]["backtest_id"] == "20260102_000000"
    return_delta = next(row for row in comparison["metrics_delta"] if row["key"] == "total_return_pct")
    drawdown_delta = next(row for row in comparison["metrics_delta"] if row["key"] == "max_drawdown_pct")
    assert return_delta["delta"] == 1.5
    assert return_delta["direction"] == "improved"
    assert drawdown_delta["delta"] == -2.0
    assert drawdown_delta["direction"] == "improved"
    fitness = detail["fitness_vector"]
    dimensions = {row["id"]: row for row in fitness["dimensions"]}
    assert fitness["version"] == "fitness_vector_v0"
    assert fitness["status"] == "warning"
    assert dimensions["validation"]["status"] == "passed"
    assert dimensions["performance_delta"]["status"] == "passed"
    assert dimensions["safety"]["status"] == "passed"
    assert dimensions["human_preference"]["status"] == "pending"
    assert "validation:vrn_alpha:step:0" in fitness["evidence_refs"]
    graph = detail["lineage_graph"]
    assert graph["version"] == "lineage_graph_v1"
    assert graph["root_id"] == f"proposal:{proposal.id}"
    node_types = {node["type"] for node in graph["nodes"]}
    edge_types = {edge["type"] for edge in graph["edges"]}
    assert {"proposal", "file_change", "validation_plan", "validation_step", "backtest_comparison"} <= node_types
    assert {"proposed_change", "requires_validation", "contains", "validated_by"} <= edge_types
    assert any(
        node["type"] == "file_change"
        and node["metadata"]["path"] == "strategies/alpha/main.py"
        for node in graph["nodes"]
    )
    assert "validation:vrn_alpha:step:0" in graph["evidence_refs"]


def test_proposal_detail_route_includes_generic_before_after_changes(tmp_path):
    paths = WorkspacePaths(root=tmp_path)
    (tmp_path / "agents.yml").write_text(
        "agent:\n  max_parallel: 2\n",
        encoding="utf-8",
    )
    proposal = create_proposal(
        paths,
        kind="core_config_patch",
        summary="Tune parallelism",
        target="agents.yml",
        extra_files={
            "after/agents.yml": "agent:\n  max_parallel: 4\n",
        },
    )
    route = next(
        handler
        for method, path, handler in routes()
        if method == "POST" and path == "/evolution/proposals/{proposal_id}"
    )
    client = SimpleNamespace(config=SimpleNamespace(paths=paths))

    detail = route(client, {"proposal_id": proposal.id})

    change = detail["file_changes"][0]
    assert change["path"] == "agents.yml"
    assert change["before_exists"] is True
    assert "max_parallel: 2" in change["before"]
    assert "max_parallel: 4" in change["after"]
    assert "-  max_parallel: 2" in change["diff"]
    assert "+  max_parallel: 4" in change["diff"]


def test_proposal_detail_includes_action_gates(tmp_path):
    paths = WorkspacePaths(root=tmp_path)
    proposal = create_proposal(
        paths,
        kind="strategy_package_proposal",
        summary="Validated alpha package",
        target="strategies/alpha",
        initial_state="approved",
        metadata={"strategy_id": "alpha"},
        extra_files={
            "after/strategies/alpha/main.py": "def run(ctx):\n    return {'ok': True}\n",
            "validation_report.json": json.dumps({"ok": True, "issues": []}),
        },
    )

    detail = _proposal_detail_dict(proposal, workspace_root=tmp_path)

    gates = detail["action_gates"]
    assert gates["can_apply"] is True
    assert gates["materialization"]["after_file_count"] == 1
    assert gates["validation"]["source"] == "validation_report"
    assert gates["evidence"]["count"] == 1
    graph = detail["lineage_graph"]
    assert any(node["type"] == "action_gates" for node in graph["nodes"])
    assert any(edge["type"] == "gates" for edge in graph["edges"])


def test_validation_run_route_refreshes_action_gates(tmp_path):
    paths = WorkspacePaths(root=tmp_path)
    package = _seed_alpha_package(paths)
    sample = tmp_path / "test_gate_sample.py"
    sample.write_text("def test_gate_sample():\n    assert True\n", encoding="utf-8")
    plan = build_validation_plan(
        [{"type": "unit_test", "command": "python -m pytest test_gate_sample.py -q"}],
        source="test",
        strategy_id="alpha",
    )
    plan_id = write_validation_plan(paths, plan)
    proposal = create_proposal(
        paths,
        kind="strategy_tuning_proposal",
        summary="Validated from route",
        target="strategies/alpha",
        initial_state="approved",
        validation_plan_id=plan_id,
        metadata={
            "strategy_id": "alpha",
            "package_hash": package.content_hash,
            "materialized": True,
        },
        extra_files={
            "after/strategies/alpha/main.py": "def run(ctx):\n    return {'ok': True}\n",
        },
    )
    route_map = {(method, path): handler for method, path, handler in routes()}
    client = SimpleNamespace(config=SimpleNamespace(paths=paths))

    before = _proposal_detail_dict(proposal, workspace_root=tmp_path)
    run = route_map[("POST", "/evolution/validation/run")](
        client,
        {"proposal_id": proposal.id, "dry_run": False},
    )
    after = route_map[("POST", "/evolution/proposals/{proposal_id}")](
        client,
        {"proposal_id": proposal.id},
    )

    assert before["action_gates"]["can_apply"] is False
    assert run["ok"] is True
    assert run["status"] == "passed"
    assert after["action_gates"]["can_apply"] is True
    assert after["action_gates"]["validation"]["status"] == "passed"
    assert after["action_gates"]["evidence"]["count"] >= 1


def test_approve_proposal_route_updates_state_and_action_gates(tmp_path):
    paths = WorkspacePaths(root=tmp_path)
    package = _seed_alpha_package(paths)
    proposal = create_proposal(
        paths,
        kind="strategy_tuning_proposal",
        summary="Approve validated alpha",
        target="strategies/alpha",
        initial_state="pending_review",
        metadata={
            "strategy_id": "alpha",
            "package_hash": package.content_hash,
            "materialized": True,
        },
        extra_files={
            "after/strategies/alpha/main.py": "def run(ctx):\n    return {'ok': True}\n",
            "validation_report.json": json.dumps({"ok": True, "issues": []}),
        },
    )
    route_map = {(method, path): handler for method, path, handler in routes()}
    client = SimpleNamespace(config=SimpleNamespace(paths=paths))

    approved = route_map[("POST", "/evolution/proposals/{proposal_id}/approve")](
        client,
        {"proposal_id": proposal.id},
    )

    assert approved["id"] == proposal.id
    assert approved["state"] == "approved"
    assert approved["action_gates"]["can_apply"] is True
    assert approved["action_gates"]["validation"]["source"] == "validation_report"


def test_reject_proposal_route_updates_state(tmp_path):
    paths = WorkspacePaths(root=tmp_path)
    proposal = create_proposal(
        paths,
        kind="strategy_tuning_proposal",
        summary="Reject alpha",
        target="strategies/alpha",
        initial_state="pending_review",
        metadata={"strategy_id": "alpha"},
    )
    route_map = {(method, path): handler for method, path, handler in routes()}
    client = SimpleNamespace(config=SimpleNamespace(paths=paths))

    rejected = route_map[("POST", "/evolution/proposals/{proposal_id}/reject")](
        client,
        {"proposal_id": proposal.id, "note": "not useful"},
    )

    assert rejected["id"] == proposal.id
    assert rejected["state"] == "rejected"
    assert rejected["action_gates"]["can_apply"] is False
    assert "state_rejected" in rejected["action_gates"]["blockers"]


def test_apply_route_applies_approved_proposal_when_gates_pass(tmp_path):
    paths = WorkspacePaths(root=tmp_path)
    package = _seed_alpha_package(
        paths,
        main="def run(ctx):\n    return {'ok': False}\n",
    )
    strategy_root = package.root
    proposal = create_proposal(
        paths,
        kind="strategy_tuning_proposal",
        summary="Apply alpha change",
        target="strategies/alpha",
        initial_state="approved",
        evidence_refs=["proposal:test"],
        metadata={
            "strategy_id": "alpha",
            "package_hash": package.content_hash,
            "materialized": True,
        },
        extra_files={
            "after/strategies/alpha/main.py": "def run(ctx):\n    return {'ok': True}\n",
            "validation_report.json": json.dumps({"ok": True, "issues": []}),
        },
    )
    route_map = {(method, path): handler for method, path, handler in routes()}
    client = SimpleNamespace(config=SimpleNamespace(paths=paths))

    result = route_map[("POST", "/evolution/apply")](client, {"proposal_id": proposal.id})

    assert result["ok"] is True
    assert "strategies/alpha/main.py" in result["applied_files"]
    assert "return {'ok': True}" in (strategy_root / "main.py").read_text(encoding="utf-8")
    detail = route_map[("POST", "/evolution/proposals/{proposal_id}")](
        client,
        {"proposal_id": proposal.id},
    )
    assert detail["state"] == "applied"


def test_rollback_route_restores_before_snapshot(tmp_path):
    paths = WorkspacePaths(root=tmp_path)
    package = _seed_alpha_package(
        paths,
        main="def run(ctx):\n    return {'ok': False}\n",
    )
    strategy_root = package.root
    proposal = create_proposal(
        paths,
        kind="strategy_tuning_proposal",
        summary="Rollback alpha change",
        target="strategies/alpha",
        initial_state="approved",
        evidence_refs=["proposal:test"],
        metadata={
            "strategy_id": "alpha",
            "package_hash": package.content_hash,
            "materialized": True,
        },
        extra_files={
            "after/strategies/alpha/main.py": "def run(ctx):\n    return {'ok': True}\n",
            "validation_report.json": json.dumps({"ok": True, "issues": []}),
        },
    )
    route_map = {(method, path): handler for method, path, handler in routes()}
    client = SimpleNamespace(config=SimpleNamespace(paths=paths))

    applied = route_map[("POST", "/evolution/apply")](client, {"proposal_id": proposal.id})
    rolled_back = route_map[("POST", "/evolution/rollback")](
        client,
        {"proposal_id": proposal.id},
    )

    assert applied["ok"] is True
    assert rolled_back["ok"] is True
    assert "return {'ok': False}" in (strategy_root / "main.py").read_text(encoding="utf-8")
    detail = route_map[("POST", "/evolution/proposals/{proposal_id}")](
        client,
        {"proposal_id": proposal.id},
    )
    assert detail["state"] == "rolled_back"


def test_apply_route_returns_action_gate_blockers_when_validation_missing(tmp_path):
    paths = WorkspacePaths(root=tmp_path)
    proposal = create_proposal(
        paths,
        kind="strategy_tuning_proposal",
        summary="Missing validation",
        target="strategies/alpha",
        initial_state="approved",
        evidence_refs=["proposal:test"],
        metadata={"strategy_id": "alpha", "materialized": True},
        extra_files={
            "after/strategies/alpha/main.py": "def run(ctx):\n    return {'ok': True}\n",
        },
    )
    route_map = {(method, path): handler for method, path, handler in routes()}
    client = SimpleNamespace(config=SimpleNamespace(paths=paths))

    result = route_map[("POST", "/evolution/apply")](client, {"proposal_id": proposal.id})

    assert result["ok"] is False
    assert result["reason"] == "missing_validation_evidence"
    assert result["action_gates"]["can_apply"] is False
    assert "missing_validation_evidence" in result["action_gates"]["blockers"]


def test_list_proposals_route_limits_after_newest_first_sort(tmp_path):
    paths = WorkspacePaths(root=tmp_path)
    paths.proposals.mkdir(parents=True)
    old_dir = paths.proposals / "prp_a_old"
    new_dir = paths.proposals / "prp_z_new"
    old_dir.mkdir()
    new_dir.mkdir()
    (old_dir / "proposal.yml").write_text(
        yaml_io.dumps(
            {
                "id": "prp_a_old",
                "kind": "strategy_package_proposal",
                "state": "pending_review",
                "summary": "Old BTC proposal",
                "ts": "2026-01-01T00:00:00+00:00",
            }
        ),
        encoding="utf-8",
    )
    (new_dir / "proposal.yml").write_text(
        yaml_io.dumps(
            {
                "id": "prp_z_new",
                "kind": "strategy_package_proposal",
                "state": "pending_review",
                "summary": "New whale proposal",
                "ts": "2026-02-01T00:00:00+00:00",
            }
        ),
        encoding="utf-8",
    )
    list_route = next(
        handler
        for method, path, handler in routes()
        if method == "GET" and path == "/evolution/proposals"
    )
    client = SimpleNamespace(config=SimpleNamespace(paths=paths))

    result = list_route(
        client,
        {"kind": "strategy_package_proposal", "limit": "1"},
    )

    assert [p["id"] for p in result["proposals"]] == ["prp_z_new"]
