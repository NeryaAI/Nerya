"""Network-free contract tests for resource graphs and proposal-only editing."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from nerya.core import yaml_io
from nerya.core.paths import WorkspacePaths
from nerya.evolution.patch_proposal import list_proposals
from nerya.evolution.strategy_code_generator import StrategyCodeGenerator, StrategyGenerationRequest
from nerya.strategies.workflow_graph import WorkflowError, build_workflows, package_revision
from nerya.strategies.workflow_service import propose_workflow, source_files, view_workflow, workflow_index
from nerya.strategies.workflow_templates import TEMPLATES, create_workflow_template
from nerya.strategies.validator import validate_proposal_files

pytestmark = pytest.mark.smoke


@pytest.fixture
def paths(tmp_path: Path) -> WorkspacePaths:
    return WorkspacePaths(tmp_path)


def example(paths: WorkspacePaths, template: str = "multi_script"):
    return create_workflow_template(paths, {"template": template, "strategy_id": f"test_{template}",
                                           "markets": ["BINANCE:BTCUSDT"], "accounts": ["paper_main"]})


def save_payload(out, **extra):
    return {"strategy_id": out["strategy_id"], "proposal_id": out["proposal_id"],
            "base_revision": out["workflow"]["revision"], **extra}


@pytest.mark.parametrize("template", TEMPLATES)
def test_templates_are_real_validated_paper_drafts(paths, template):
    out = example(paths, template)
    assert out["ok"], out
    assert out["validation"]["ok"], out["validation"]
    assert out["state"] == "draft"
    assert not paths.strategy(out["strategy_id"]).exists()
    assert not paths.triggers_schedules_file.exists()
    manifest = out["workflow"]["manifest"]
    assert manifest["mode"] == "paper"
    assert manifest["schedule"]["enabled"] is False
    assert manifest["tuning"]["schedule"]["enabled"] is False
    assert manifest["policy"]["allow_direct_order"] is False
    for name in ("strategy", "evolution"):
        graph = out["workflow"][name]
        ids = [node["id"] for node in graph["nodes"]]
        assert len(ids) == len(set(ids))
        assert all(edge["source"] in ids and edge["target"] in ids for edge in graph["edges"])
    indexed = workflow_index(paths)
    assert indexed["total"] == 1
    assert indexed["workflows"][0]["proposal_id"] == out["proposal_id"]


def test_directory_scans_proposals_once_and_reads_fresh_on_next_request(paths, monkeypatch):
    from nerya.strategies import workflow_service

    for template in TEMPLATES:
        example(paths, template)
    original = workflow_service.list_proposals
    calls = []

    def counted(workspace):
        calls.append(workspace)
        return original(workspace)

    monkeypatch.setattr(workflow_service, "list_proposals", counted)
    first = workflow_index(paths)
    assert len(calls) == 1
    assert first["total"] == len(TEMPLATES)
    assert all(not row.get("error") for row in first["workflows"])
    workflow_index(paths)
    assert len(calls) == 2  # The snapshot is not a persistent cache.


def test_static_dependencies_and_agent_dispatch(paths):
    multi = example(paths)["workflow"]["strategy"]
    assert any(e["relation"] == "imports" and e["origin"] == "static" for e in multi["edges"])
    assert len([n for n in multi["nodes"] if n["kind"] == "script"]) == 4
    agent = example(paths, "script_agent")["workflow"]["strategy"]
    assert any(e["relation"] == "dispatch" and e["target"] == "agent:runtime" for e in agent["edges"])
    assert len([n for n in agent["nodes"] if n["kind"] == "agent"]) == 3


def test_node_edit_saves_proposal_without_mutating_source(paths):
    out = example(paths)
    before, _ = source_files(paths, out["strategy_id"], out["proposal_id"])
    edited = propose_workflow(paths, save_payload(out, changes=[
        {"node_id": f"strategy:{out['strategy_id']}", "config": {"title": "Edited workflow", "description": "Reviewed changes"}},
        {"node_id": "scheduler:trading", "config": {"type": "interval", "every_seconds": 600, "enabled": False}},
        {"node_id": "script:signals.py", "content": before["signals.py"] + "\n# edited in workflow\n"},
    ]))
    assert edited["ok"], edited
    assert edited["state"] == "pending_review"
    assert edited["workflow"]["manifest"]["title"] == "Edited workflow"
    assert edited["workflow"]["manifest"]["schedule"]["every_seconds"] == 600
    after, _ = source_files(paths, out["strategy_id"], out["proposal_id"])
    assert before == after
    assert not paths.strategy(out["strategy_id"]).exists()


def test_schedule_card_timezone_survives_candidate_save_and_reload(paths):
    out = example(paths, "scheduler_agent")
    before, _ = source_files(paths, out["strategy_id"], out["proposal_id"])
    edited = propose_workflow(paths, save_payload(out, changes=[
        {"node_id": "scheduler:trading", "config": {"type": "cron", "cron": "0 9 * * *", "timezone": "Asia/Shanghai", "enabled": False}},
    ]))
    assert edited["ok"], edited
    reloaded = view_workflow(paths, out["strategy_id"], edited["proposal_id"])
    schedule = reloaded["manifest"]["schedule"]
    assert schedule["timezone"] == "Asia/Shanghai" and schedule["cron"] == "0 9 * * *"
    assert schedule["enabled"] is False
    node = next(n for n in reloaded["strategy"]["nodes"] if n["id"] == "scheduler:trading")
    assert node["config"]["timezone"] == "Asia/Shanghai"
    assert source_files(paths, out["strategy_id"], out["proposal_id"])[0] == before
    count = len(list_proposals(paths))
    invalid = propose_workflow(paths, save_payload(out, changes=[
        {"node_id": "scheduler:trading", "config": {"type": "cron", "cron": "0 9 * * *", "timezone": "invented/zone", "enabled": False}},
    ]))
    assert invalid["ok"] is False and invalid["error"] == "validation_failed"
    assert "timezone" in str(invalid["validation"])
    assert len(list_proposals(paths)) == count


def test_revision_conflict_does_not_create_a_proposal(paths):
    out = example(paths)
    with pytest.raises(WorkflowError, match="revision_conflict"):
        propose_workflow(paths, save_payload(out, base_revision="stale"))
    assert len(list_proposals(paths)) == 1


@pytest.mark.parametrize("extra", [
    {"changes": [{"node_id": "approval:operator", "config": {"required": False}}]},
    {"changes": [{"node_id": "validation:tuning", "config": {"require_operator_approval": False}}]},
    {"changes": [{"node_id": "strategy:test_multi_script", "config": {"mode": "live"}}]},
    {"additions": [{"kind": "script", "name": "../escape.py", "content": "pass"}]},
    {"additions": [{"kind": "script", "name": "/tmp/escape.py", "content": "pass"}]},
    {"additions": [{"kind": "script", "name": "secrets/key.py", "content": "pass"}]},
])
def test_protected_or_out_of_scope_edits_are_refused(paths, extra):
    out = example(paths)
    with pytest.raises(WorkflowError):
        propose_workflow(paths, save_payload(out, **extra))
    assert len(list_proposals(paths)) == 1


def test_metadata_persists_but_never_becomes_execution(paths):
    out = example(paths)
    metadata = {"version": 1, "nodes": {"script:signals.py": {"position": {"x": 123, "y": 456}, "description": "signal notes"}},
                "edges": [{"id": "note-1", "source": "script:signals.py", "target": "risk:policy", "relation": "annotation", "label": "review this dependency"}]}
    saved = propose_workflow(paths, save_payload(out, metadata=metadata))
    assert saved["ok"], saved
    reloaded = view_workflow(paths, saved["strategy_id"], saved["proposal_id"])
    signal = next(n for n in reloaded["strategy"]["nodes"] if n["id"] == "script:signals.py")
    assert signal["position"] == {"x": 123, "y": 456}
    edge = next(e for e in reloaded["strategy"]["edges"] if e["id"] == "note-1")
    assert edge["origin"] == "annotation"
    metadata["edges"][0]["relation"] = "dispatch"
    with pytest.raises(WorkflowError, match="annotations"):
        propose_workflow(paths, save_payload(out, metadata=metadata))


@pytest.mark.parametrize("metadata", [
    {"version": 99},
    {"version": 1, "nodes": {"x": {"position": {"x": float("inf"), "y": 0}}}},
    {"version": 1, "edges": [{"id": "x", "source": "missing", "target": "risk:policy"}]},
])
def test_invalid_workflow_is_validation_blocker(paths, metadata):
    out = example(paths)
    files, _ = source_files(paths, out["strategy_id"], out["proposal_id"])
    files["workflow.json"] = json.dumps(metadata)
    report = validate_proposal_files(strategy_id=out["strategy_id"], files=files)
    assert not report.ok
    assert any(b.code == "workflow_schema" for b in report.blockers)


def test_new_files_are_discovered_without_regenerating_metadata(paths):
    req = StrategyGenerationRequest(strategy_id="auto_graph", markets=("BINANCE:BTCUSDT",), accounts=("paper_main",))
    files = StrategyCodeGenerator(paths).generate(req, create_proposal_record=False).files
    assert "workflow.json" in files
    files["new_helper.py"] = "def feature():\n    return 1\n"
    graph = build_workflows(files)
    assert any(n["resource"] == "new_helper.py" for n in graph["strategy"]["nodes"])


def test_new_resource_is_reviewed_not_installed(paths):
    out = example(paths)
    edited = propose_workflow(paths, save_payload(out, additions=[
        {"kind": "script", "name": "helpers/normalize.py", "content": "def normalize(value):\n    return float(value)\n"},
        {"kind": "source", "name": "daily", "config": {"capability": "candles", "timeframe": "1d", "consumers": ["market_inputs.py"]}},
        {"kind": "account", "name": "paper_hedge"},
    ]))
    assert edited["ok"], edited
    assert len([n for n in edited["workflow"]["strategy"]["nodes"] if n["kind"] == "account"]) == 2
    assert not paths.strategy(out["strategy_id"]).exists()


def test_legacy_strategy_remains_visible(paths):
    root = paths.strategy("legacy")
    root.mkdir(parents=True)
    (root / "strategy.yml").write_text(yaml_io.dumps({"id": "legacy", "title": "Legacy", "driver": "prompt", "account_id": "paper_main", "markets": ["BINANCE:BTCUSDT"]}))
    out = view_workflow(paths, "legacy")
    assert out["legacy"] and not out["can_edit"]
    assert any(n["kind"] == "account" for n in out["strategy"]["nodes"])


def test_workspace_status_serializes_boolean_not_config_repr(paths):
    from types import SimpleNamespace
    from nerya.api.routes_workspace import routes

    handler = next(handler for method, route, handler in routes() if method == "GET" and route == "/workspace")
    client = SimpleNamespace(config=SimpleNamespace(paths=paths, live_trading_enabled=lambda: False, kill_switch=lambda: False))
    value = handler(client, {})
    assert value["live_trading_enabled"] is False
    assert "bound method" not in json.dumps(value)


def test_symlink_file_is_not_exposed(paths, tmp_path):
    root = paths.strategy("safe")
    root.mkdir(parents=True)
    (root / "strategy.yml").write_text("strategy_id: safe\ntitle: Safe\n")
    secret = tmp_path / "outside.txt"
    secret.write_text("do-not-expose")
    (root / "exfiltrate.md").symlink_to(secret)
    files, info = source_files(paths, "safe")
    assert "exfiltrate.md" not in files
    assert "exfiltrate.md" in info["omitted_files"]
