from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from nerya.api import routes_evolution
from nerya.core import jsonl
from nerya.core.config import Config
from nerya.core.paths import WorkspacePaths
from nerya.evolution import assets as evolution_assets
from nerya.evolution.event_store import append_signal, record_event
from nerya.evolution.events import EvolutionSignal
from nerya.evolution.patch_proposal import create_proposal, set_state
from nerya.evolution.validation_plan import build_validation_plan, write_validation_plan

pytestmark = pytest.mark.smoke


def test_evolution_timeline_stitches_history_and_config(tmp_path):
    paths = WorkspacePaths(tmp_path)
    config = Config(
        paths=paths,
        data={"agent": {"native": {"evolution_hooks_enabled": True}}},
    )
    client = SimpleNamespace(config=config)

    signal = EvolutionSignal.create(
        source="tool",
        kind="tool_failure_cluster",
        severity="warn",
        strategy_id="alpha",
        summary="tool failed repeatedly",
        evidence_refs=["turn:t1"],
        dedupe_key="tool_failure:alpha",
    )
    append_signal(paths, signal)
    event = record_event(
        paths,
        signals=[signal.id],
        outcome="candidate",
        strategy_id="alpha",
        summary="candidate repair found",
        evidence_refs=["turn:t1"],
    )
    plan = build_validation_plan(
        [{"type": "unit_test", "command": "python -m pytest tests/test_evolution_events.py -q"}],
        source="test",
        strategy_id="alpha",
    )
    plan_id = write_validation_plan(paths, plan)
    proposal = create_proposal(
        paths,
        kind="learning_update",
        summary="remember repair pattern",
        rationale="# Rationale\n\nGenerated from repeated tool failures.\n",
        evidence_refs=["turn:t1"],
        source_event_id=event["id"],
        validation_plan_id=plan_id,
        metadata={"strategy_id": "alpha"},
        extra_files={
            "after/skills/repair_pattern/SKILL.md": "# Repair Pattern\n\nUse after repeated tool failures.\n",
            "reflection.json": '{"summary":"reflection payload"}',
            "ranked_seeds.json": '{"seeds":[{"id":"seed-1"}]}',
        },
    )
    evolution_assets.create_candidate(
        paths,
        kind="capsule",
        summary="repair capsule",
        payload={"summary": "repair capsule", "validation_results": []},
        evidence_refs=["turn:t1"],
        source_event_id=event["id"],
        strategy_id="alpha",
    )

    route_map = {(method, path): handler for method, path, handler in routes_evolution.routes()}
    out = route_map[("POST", "/evolution/timeline")](
        client,
        {"strategy_id": "alpha", "limit": 50},
    )

    assert out["ok"] is True
    assert out["summary"]["signals"] == 1
    assert out["summary"]["open_proposals"] == 1
    assert list(out["config"].keys()) == ["periodic_reflection"]
    assert out["config"]["periodic_reflection"]["target"] == "skill:evolution.reflect"
    assert out["config"]["periodic_reflection"]["enabled"] is False
    assert any(item["type"] == "signal" for item in out["timeline"])
    assert any(item.get("proposal_id") == proposal.id for item in out["timeline"])
    assert any(item.get("validation_plan_id") == plan_id for item in out["timeline"])
    assert any(item["type"] == "asset_candidate" for item in out["timeline"])

    proposal_item = next(item for item in out["timeline"] if item.get("proposal_id") == proposal.id)
    graph = proposal_item["lineage_graph"]
    assert graph["version"] == "lineage_graph_v1"
    assert graph["root_id"] == f"proposal:{proposal.id}"
    assert any(node["type"] == "event" for node in graph["nodes"])
    assert any(node["type"] == "file_change" for node in graph["nodes"])
    assert any(edge["type"] == "requires_validation" for edge in graph["edges"])
    process = proposal_item["process"]
    assert process["has_generated_docs"] is True
    assert process["has_validation"] is True
    titles = [
        artifact["title"]
        for section in process["sections"]
        for artifact in section["artifacts"]
    ]
    assert "rationale.md" in titles
    assert "reflection.json" in titles
    assert "SKILL.md" in titles
    assert "Validation plan" in titles
    change_artifact = next(
        artifact
        for section in process["sections"]
        for artifact in section["artifacts"]
        if artifact["title"] == "SKILL.md"
    )
    assert change_artifact["kind"] == "change"
    assert change_artifact["metadata"]["workspace_path"] == "skills/repair_pattern/SKILL.md"


def test_evolution_timeline_backfills_tuning_model_metadata_from_llm_journal(tmp_path):
    paths = WorkspacePaths(tmp_path)
    config = Config(paths=paths, data={})
    client = SimpleNamespace(config=config)
    run_id = "tune_legacy_model"
    root = paths.strategy("alpha")
    reviews = root / "reviews"
    reviews.mkdir(parents=True, exist_ok=True)
    (reviews / f"tuning_{run_id}.md").write_text(
        "# Tuning review\n\nUse the recent market data.",
        encoding="utf-8",
    )
    (reviews / f"tuning_{run_id}_audit.json").write_text(
        json.dumps(
            {
                "run_id": run_id,
                "strategy_id": "alpha",
                "created_at": "2026-06-17T06:00:27.250000+00:00",
                "subagent": "strategy_tuner",
                "tier": "medium",
                "tokens": 8212,
                "usd": 0.01105,
                "wall_ms": 20152,
                "prompt_records": [
                    {"iteration": 0, "prompt": "Tune alpha", "prompt_chars": 19499}
                ],
                "role_prompt": "Tune alpha",
                "payload": {"strategy_id": "alpha"},
                "subagent_output": {"summary": "tighten momentum trigger"},
                "redacted": True,
            }
        ),
        encoding="utf-8",
    )
    proposal = create_proposal(
        paths,
        kind="strategy_tuning_proposal",
        summary="legacy tuning proposal",
        evidence_refs=[f"strategy_tuning:{run_id}"],
        metadata={"strategy_id": "alpha", "advisory_only": True},
    )
    jsonl.append(
        paths.journal("strategy_evolution"),
        {
            "kind": "strategy.tuning",
            "run_id": run_id,
            "strategy_id": "alpha",
            "status": "ok",
            "reason": "tuning proposal created",
            "proposal_id": proposal.id,
            "review_path": str(reviews / f"tuning_{run_id}.md"),
            "audit_path": str(reviews / f"tuning_{run_id}_audit.json"),
            "ts": "2026-06-17T06:00:27.270000+00:00",
        },
        stamp=False,
    )
    jsonl.append(
        paths.journal("llm"),
        {
            "kind": "llm.call",
            "tier": "medium",
            "task": "subagent_analysis",
            "caller": "subagent:strategy_tuner",
            "tokens": 8212,
            "usd": 0.01105,
            "prompt_len": 19499,
            "response_len": 2411,
            "provider": "sensenova",
            "model": "deepseek-v4-flash",
            "ts": "2026-06-17T06:00:27.250605+00:00",
        },
        stamp=False,
    )

    route_map = {(method, path): handler for method, path, handler in routes_evolution.routes()}
    out = route_map[("POST", "/evolution/timeline")](
        client,
        {"strategy_id": "alpha", "limit": 50},
    )

    item = next(row for row in out["timeline"] if row.get("proposal_id") == proposal.id)
    run = item["process"]["run"]
    assert run["provider"] == "sensenova"
    assert run["model"] == "deepseek-v4-flash"
    assert run["model_metadata_source"] == "llm_journal"
    assert run["model_metadata_evidence_ref"] == "journal:llm:0"
    assert run["model_calls"][0]["evidence_ref"] == "journal:llm:0"


def test_evolution_timeline_builds_read_only_inbox_groups(tmp_path):
    paths = WorkspacePaths(tmp_path)
    config = Config(paths=paths, data={})
    client = SimpleNamespace(config=config)

    missing_evidence = EvolutionSignal.create(
        source="tool",
        kind="tool_failure_cluster",
        severity="warn",
        summary="missing proof",
        evidence_refs=[],
        dedupe_key="missing-proof",
    )
    append_signal(paths, missing_evidence)

    needs_materialization = create_proposal(
        paths,
        kind="strategy_tuning_proposal",
        summary="advisory tuning only",
        evidence_refs=["turn:t1"],
        metadata={"strategy_id": "alpha", "advisory_only": True},
    )

    validation_plan = build_validation_plan(
        [{"type": "manual_review", "required": True}],
        source="test",
        strategy_id="alpha",
    )
    validation_plan_id = write_validation_plan(paths, validation_plan)
    needs_validation = create_proposal(
        paths,
        kind="learning_update",
        summary="needs validation",
        evidence_refs=["turn:t2"],
        validation_plan_id=validation_plan_id,
        metadata={"strategy_id": "alpha"},
    )

    passed_plan = build_validation_plan(
        [{"type": "manual_review", "required": True}],
        source="test",
        strategy_id="alpha",
    )
    passed_plan_id = write_validation_plan(paths, passed_plan)
    passed_path = paths.evolution_validation_plans / f"{passed_plan_id}.json"
    passed_record = json.loads(passed_path.read_text(encoding="utf-8"))
    passed_record["status"] = "passed"
    passed_record["steps"][0]["status"] = "passed"
    passed_record["steps"][0]["evidence_ref"] = "validation:vrn_ok:step:0"
    passed_path.write_text(json.dumps(passed_record), encoding="utf-8")
    needs_approval = create_proposal(
        paths,
        kind="learning_update",
        summary="ready for approval",
        initial_state="pending_review",
        evidence_refs=["turn:t3"],
        validation_plan_id=passed_plan_id,
        metadata={"strategy_id": "alpha"},
    )

    rejected = create_proposal(
        paths,
        kind="learning_update",
        summary="bad idea",
        evidence_refs=["turn:t4"],
        metadata={"strategy_id": "alpha"},
    )
    set_state(paths, rejected.id, "rejected", note="test rejection")
    applied_pending = create_proposal(
        paths,
        kind="learning_update",
        summary="applied but not observed",
        initial_state="applied",
        evidence_refs=["turn:t6"],
        metadata={"strategy_id": "alpha"},
    )
    applied_healthy = create_proposal(
        paths,
        kind="learning_update",
        summary="applied and healthy",
        initial_state="applied",
        evidence_refs=["turn:t7"],
        metadata={"strategy_id": "alpha"},
    )
    jsonl.append(
        paths.journal("evolution"),
        {
            "kind": "proposal.post_apply_observation",
            "proposal_id": applied_healthy.id,
            "status": "healthy",
            "summary": "post-apply paper run stayed within risk",
            "evidence_refs": ["validation:vrn_healthy:step:0"],
        },
    )
    applied_regressed = create_proposal(
        paths,
        kind="learning_update",
        summary="applied and regressed",
        initial_state="applied",
        evidence_refs=["turn:t8"],
        metadata={"strategy_id": "alpha"},
    )
    jsonl.append(
        paths.journal("evolution"),
        {
            "kind": "proposal.post_apply_observation",
            "proposal_id": applied_regressed.id,
            "status": "regressed",
            "summary": "post-apply drawdown worsened",
            "evidence_refs": ["validation:vrn_regressed:step:0"],
        },
    )

    candidate = evolution_assets.create_candidate(
        paths,
        kind="capsule",
        summary="reusable lesson",
        payload={"summary": "reusable lesson"},
        evidence_refs=["turn:t5"],
        strategy_id="alpha",
    )

    route_map = {(method, path): handler for method, path, handler in routes_evolution.routes()}
    out = route_map[("POST", "/evolution/timeline")](
        client,
        {"limit": 100},
    )
    groups = {group["id"]: group for group in out["inbox"]["groups"]}

    assert out["inbox"]["total"] >= 6
    assert any(
        entry["item_id"] == f"signal:{missing_evidence.id}"
        for entry in groups["needs_evidence"]["items"]
    )
    assert any(
        entry["proposal_id"] == needs_materialization.id
        and "advisory_only_no_applyable_after_files" in entry["reasons"]
        for entry in groups["needs_materialization"]["items"]
    )
    assert any(
        entry["proposal_id"] == needs_validation.id
        for entry in groups["needs_validation"]["items"]
    )
    assert any(
        entry["proposal_id"] == needs_approval.id
        for entry in groups["needs_approval"]["items"]
    )
    assert any(
        entry["proposal_id"] == rejected.id
        for entry in groups["negative_learning"]["items"]
    )
    assert any(
        entry["proposal_id"] == applied_pending.id
        and "post_apply_observation_pending" in entry["reasons"]
        for entry in groups["monitoring"]["items"]
    )
    assert any(
        entry["proposal_id"] == applied_healthy.id
        and "post_apply_observation:healthy" in entry["reasons"]
        for entry in groups["reusable_learning"]["items"]
    )
    assert any(
        entry["proposal_id"] == applied_regressed.id
        and "post_apply_observation:regressed" in entry["reasons"]
        for entry in groups["negative_learning"]["items"]
    )
    healthy_item = next(
        item for item in out["timeline"]
        if item.get("proposal_id") == applied_healthy.id
        and item.get("type") == "proposal"
    )
    regressed_item = next(
        item for item in out["timeline"]
        if item.get("proposal_id") == applied_regressed.id
        and item.get("type") == "proposal"
    )
    healthy_dims = {
        row["id"]: row for row in healthy_item["fitness_vector"]["dimensions"]
    }
    regressed_dims = {
        row["id"]: row for row in regressed_item["fitness_vector"]["dimensions"]
    }
    assert healthy_dims["post_apply"]["status"] == "passed"
    assert regressed_item["fitness_vector"]["status"] == "failed"
    assert regressed_dims["post_apply"]["status"] == "failed"
    assert "post_apply:regressed" in regressed_item["fitness_vector"]["blockers"]
    assert any(
        entry["record_id"] == candidate["id"]
        for entry in groups["reusable_learning"]["items"]
    )


def test_evolution_timeline_and_assets_expose_optimizer_feedback_summary(tmp_path):
    paths = WorkspacePaths(tmp_path)
    config = Config(paths=paths, data={})
    client = SimpleNamespace(config=config)
    candidate = evolution_assets.create_candidate(
        paths,
        kind="capsule",
        summary="Backtest preview passed for strategy alpha optimizer candidate safe_backtest.",
        payload={
            "summary": "safe backtest preview learning",
            "outcome_score": 0.7,
            "metadata": {
                "origin": "strategy_optimizer_preview",
                "optimizer_run_id": "tune_feedback",
                "optimizer_candidate_id": "safe_backtest",
                "preview_type": "backtest",
                "preview_status": "passed",
                "selected_by_optimizer": True,
            },
        },
        evidence_refs=[
            "strategy_tuning:tune_feedback",
            "file:evolution/optimizer_runs/tune_feedback/candidates/safe_backtest/backtest_preview.json",
        ],
        strategy_id="alpha",
    )
    promoted = evolution_assets.promote_candidate(paths, candidate["id"], operator="tester")
    assert promoted["ok"] is True
    optimizer_report = {
        "version": "strategy_tuning_optimizer_v1",
        "candidate_count": 2,
        "evaluated_count": 2,
        "selected_candidate_id": "safe_backtest",
        "selected_score": 137,
        "outcome_feedback": {
            "version": "optimizer_outcome_feedback_v1",
            "sample_count": 2,
            "positive_samples": 1,
            "negative_samples": 1,
            "neutral_samples": 0,
            "proposal_samples": 1,
            "candidate_decision_samples": 1,
            "candidate_decision_positive_samples": 1,
            "candidate_decision_negative_samples": 0,
            "candidate_decision_neutral_samples": 0,
            "top_features": [
                {
                    "feature": "validation:backtest",
                    "positive": 1.5,
                    "negative": 0,
                    "net": 1.5,
                    "samples": 1,
                    "sources": {"proposal_outcome": 1},
                },
                {
                    "feature": "risk:leverage",
                    "positive": 0,
                    "negative": 2,
                    "net": -2,
                    "samples": 1,
                    "sources": {"asset_candidate_decision": 1},
                },
            ],
        },
        "candidates": [
            {
                "candidate_id": "safe_backtest",
                "status": "materialized",
                "score": 137,
                "validation_types": ["unit_test", "backtest"],
                "asset_candidate": {
                    "id": candidate["id"],
                    "kind": "capsule",
                    "preview_type": "backtest",
                    "preview_status": "passed",
                    "evidence_refs": candidate["evidence_refs"],
                },
            }
        ],
    }
    proposal = create_proposal(
        paths,
        kind="strategy_tuning_proposal",
        summary="optimizer feedback source",
        target="strategies/alpha",
        initial_state="applied",
        evidence_refs=["strategy_tuning:tune_feedback"],
        metadata={"strategy_id": "alpha", "materialized": True},
        extra_files={
            "tuning_run.json": json.dumps({"optimizer_report": optimizer_report}),
            "after/strategies/alpha/main.py": "def run(ctx):\n    return {'ok': True}\n",
        },
    )
    jsonl.append(
        paths.journal("strategy_evolution"),
        {
            "kind": "strategy.tuning",
            "run_id": "tune_feedback",
            "strategy_id": "alpha",
            "proposal_id": proposal.id,
            "status": "ok",
            "ts": "2026-06-17T00:00:00+00:00",
        },
        stamp=False,
    )

    route_map = {(method, path): handler for method, path, handler in routes_evolution.routes()}
    timeline = route_map[("POST", "/evolution/timeline")](
        client,
        {"strategy_id": "alpha", "limit": 50},
    )
    assets = route_map[("POST", "/evolution/assets")](
        client,
        {"strategy_id": "alpha", "limit": 50},
    )
    detail = route_map[("POST", "/evolution/proposals/{proposal_id}")](
        client,
        {"proposal_id": proposal.id},
    )

    feedback = timeline["raw"]["optimizer_feedback"]
    assert feedback["version"] == "optimizer_feedback_summary_v1"
    assert feedback["run_count"] == 1
    assert feedback["sample_count"] == 2
    assert feedback["positive_samples"] == 1
    assert feedback["negative_samples"] == 1
    assert feedback["proposal_samples"] == 1
    assert feedback["candidate_decision_samples"] == 1
    assert feedback["top_positive_features"][0]["feature"] == "validation:backtest"
    assert feedback["top_positive_features"][0]["sources"]["proposal_outcome"] == 1
    assert feedback["top_negative_features"][0]["feature"] == "risk:leverage"
    assert feedback["top_negative_features"][0]["sources"]["asset_candidate_decision"] == 1
    assert feedback["calibration"]["version"] == "optimizer_feedback_calibration_v1"
    assert feedback["calibration"]["status"] == "needs_more_evidence"
    assert feedback["calibration"]["confidence"] == "low"
    assert feedback["calibration"]["source_mix"]["proposal_samples"] == 1
    assert feedback["calibration"]["source_mix"]["candidate_decision_samples"] == 1
    assert feedback["calibration"]["source_mix"]["candidate_decision_ratio"] == 0.5
    assert "low_sample_count" in feedback["calibration"]["warnings"]
    assert f"proposal:{proposal.id}" in feedback["evidence_refs"]
    assert "strategy_tuning:tune_feedback" in feedback["evidence_refs"]
    assert feedback["candidate_decisions"]["promoted"] == 1
    assert feedback["candidate_decisions"]["recent"][0]["candidate_id"] == candidate["id"]
    assert feedback["candidate_decisions"]["recent"][0]["optimizer_candidate_id"] == "safe_backtest"
    assert feedback["candidate_decisions"]["recent"][0]["state"] == "promoted"
    assert assets["optimizer_feedback"]["top_positive_features"][0]["feature"] == "validation:backtest"
    assert assets["optimizer_feedback"]["candidate_decisions"]["promoted"] == 1
    assert assets["optimizer_feedback"]["calibration"]["confidence"] == "low"
    detail_candidate = detail["optimizer_report"]["candidates"][0]
    assert detail_candidate["asset_candidate"]["state"] == "promoted"
    assert detail_candidate["asset_candidate"]["promoted_ref"] == promoted["promoted_ref"]
