from __future__ import annotations

from types import SimpleNamespace

import pytest

from nerya.api import routes_evolution
from nerya.core.config import Config
from nerya.core.paths import WorkspacePaths
from nerya.evolution import assets as evolution_assets
from nerya.evolution.event_store import append_signal, record_event
from nerya.evolution.events import EvolutionSignal
from nerya.evolution.patch_proposal import create_proposal
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
    assert out["config"]["hooks"]["enabled"] is True
    assert out["config"]["memory_quality_gate"]["minimum_score"] == 0.55
    assert out["config"]["periodic_reflection"]["target"] == "skill:evolution.reflect"
    assert out["config"]["periodic_reflection"]["enabled"] is False
    assert any(item["type"] == "signal" for item in out["timeline"])
    assert any(item.get("proposal_id") == proposal.id for item in out["timeline"])
    assert any(item.get("validation_plan_id") == plan_id for item in out["timeline"])
    assert any(item["type"] == "asset_candidate" for item in out["timeline"])

    proposal_item = next(item for item in out["timeline"] if item.get("proposal_id") == proposal.id)
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
