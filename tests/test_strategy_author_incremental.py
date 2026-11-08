"""Real incremental file tools and isolated validation; no runtime workspace."""
import sys

import pytest

from nerya.agent.file_state import FileStateCache
from nerya.core import yaml_io
from nerya.core.config import Config
from nerya.core.paths import WorkspacePaths
from nerya.evolution.patch_proposal import list_proposals
from nerya.tools.native import file_ops, strategy_runtime
from nerya.tools.types import ToolCall

pytestmark = pytest.mark.smoke

MAIN = '''def signal(previous, current, positioned):
    if positioned and current < previous:
        return "exit"
    if not positioned and current > previous:
        return "entry"
    return "hold"

def run(ctx):
    return ctx.result.hold(reason="observation_only")
'''

TESTS = '''from types import SimpleNamespace
from main import run, signal

def test_signals():
    assert signal(100, 101, False) == "entry"
    assert signal(100, 101, True) == "hold"
    assert signal(101, 100, True) == "exit"
    assert signal(100, 100, False) == "hold"

def test_no_ai_or_orders():
    ctx = SimpleNamespace(result=SimpleNamespace(hold=lambda **kw: kw))
    assert run(ctx) == {"reason": "observation_only"}
'''


@pytest.mark.parametrize("producer", ["strategy_draft_proposal", "strategy_generate_proposal"])
def test_unbound_draft_and_failed_static_review_can_be_saved_and_delivered(tmp_path, monkeypatch, producer):
    from test_task_delivery import _runtime, _command, _deps
    from nerya.tools.native import delivery
    from nerya.tools.result_contracts import tool_json_data

    def forbidden(*args, **kwargs):
        pytest.fail("Creation must not invoke scope extraction, network or candidate execution")

    monkeypatch.setattr("socket.socket.connect", forbidden)
    monkeypatch.setattr("nerya.strategies.request_scope.ensure_contract", forbidden)
    monkeypatch.setattr("nerya.strategies.candidate_validation.run_candidate_validation", forbidden)
    config, runtime = _runtime(tmp_path)
    command = _command(runtime)
    deps = _deps(config, command)
    started = delivery.delivery_start_handler(ToolCall(name="delivery_start", arguments={
        "skill_id": "strategy_author", "workflow_id": "strategy_authoring",
        "requested_scope": {"summary": "Create an editable LP research draft; resources unresolved"},
        "stages": [{"id": "author", "state": "in_progress"}],
    }), deps=deps)
    assert not started.is_error, started.text()
    drafted = getattr(strategy_runtime, producer + "_handler")(ToolCall(
        name=producer, arguments={
            "strategy_id": "unbound_research", "execution_mode": "script",
            "files": {"main.py": "def run(ctx)\n    return None\n",
                      "backtests/study.py": "# synthetic stress scenario, not historical data\n",
                      "tests/test_main.py": "raise RuntimeError('must not execute on save')\n"},
        }), config=config)
    assert not drafted.is_error and drafted.semantic_success is True, drafted.text()
    data = tool_json_data(drafted)
    assert data["validation"]["behavioral"]["status"] == "not_run"
    root = tmp_path / data["proposal_paths"]["strategy_root"]
    manifest = yaml_io.load(root / "strategy.yml")
    assert manifest["markets"] == [] and manifest["accounts"] == []
    submitted = strategy_runtime.strategy_submit_proposal_handler(ToolCall(
        name="strategy_submit_proposal", arguments={"proposal_id": data["proposal_id"]}), config=config)
    assert not submitted.is_error and submitted.semantic_success is True, submitted.text()
    result = tool_json_data(submitted)
    assert result["state"] == "pending_review" and result["backtest_required"] is False
    assert result["validation"]["ok"] is False
    assert next(p for p in list_proposals(config.paths) if p.id == data["proposal_id"]).state == "pending_review"
    updated = delivery.delivery_update_handler(ToolCall(name="delivery_update", arguments={
        "expected_revision": result["delivery_revision"],
        "stages": [{"id": "author", "state": "completed"}],
    }), deps=deps)
    assert not updated.is_error, updated.text()
    final = delivery.delivery_finalize_handler(ToolCall(name="delivery_finalize", arguments={
        "expected_revision": tool_json_data(updated)["delivery_revision"],
    }), deps=deps)
    completion = tool_json_data(final)["completion"]
    assert (completion["allowed"], completion["status"]) == (True, "partial"), final.text()


@pytest.mark.skipif(sys.platform != "darwin", reason="real isolated candidate runner")
@pytest.mark.parametrize("draft_only", [False, True])
def test_scaffold_file_edits_validate_and_optional_submit(tmp_path, draft_only):
    cfg = Config(paths=WorkspacePaths(tmp_path), data={})
    cache = FileStateCache()

    def producer(name, **arguments):
        result = getattr(strategy_runtime, name + "_handler")(
            ToolCall(name=name, arguments=arguments), config=cfg,
        )
        assert not result.is_error, result.error
        return result.content[0].data

    def read(path):
        result = file_ops.read_file_handler(
            ToolCall(name="read_file", arguments={"path": path}),
            root=tmp_path, file_state=cache,
        )
        assert not result.is_error, result.error
        return (tmp_path / path).read_text()

    def write(path, content):
        if (tmp_path / path).exists():
            read(path)
        result = file_ops.write_file_handler(
            ToolCall(name="write_file", arguments={"path": path, "contents": content}),
            root=tmp_path, file_state=cache,
        )
        assert not result.is_error, result.error
        assert (tmp_path / path).read_text() == content

    draft = producer("strategy_draft_proposal", strategy_id="incremental_observer",
                     execution_mode="script", markets=["mock:BTC/USDT"],
                     accounts=["paper_main"], create_tuning=not draft_only)
    assert draft["seeded_from"] == "template" and draft["state"] == "draft"
    pid, paths = draft["proposal_id"], draft["proposal_paths"]
    write(paths["main_path"], MAIN)
    test_file = "tests/test_contract.py"
    assert test_file not in draft["files"]
    write(paths["strategy_root"] + "/" + test_file, TESTS)
    manifest = yaml_io.loads(read(paths["strategy_yml_path"]))
    manifest["agent_task"] = {"enabled": False}
    review = manifest["tuning"]["review_plan"]
    review.update({
        "scope": "BTC observation only; no live orders, promotion or activation.",
        "focus": "Check rising entry, positioned hold and falling exit signals.",
        "validation_plan": "Require isolated signal and no-AI/no-order contract tests.",
        "next_review": "Review seven days of observations in UTC; insufficient evidence means hold.",
    })
    if draft_only:
        review["scope"] += " Explicit no-AI/no-review/no-submit opt-out."
        assert "subagents/strategy_tuner.agent.md" not in draft["files"]
    else:
        tuner = next(name for name in draft["files"] if name == "subagents/strategy_tuner.agent.md")
        write(paths["strategy_root"] + "/" + tuner, "# Observer review\n" + "\n".join(review.values()))
    write(paths["strategy_yml_path"], yaml_io.dumps(manifest))
    guide = "# Observer\n" + review["scope"] + "\n" + review["focus"] + "\n"
    edited = file_ops.edit_file_handler(
        ToolCall(name="edit_file", arguments={"path": paths["strategy_md_path"],
                 "old_string": read(paths["strategy_md_path"]), "new_string": guide}),
        root=tmp_path, file_state=cache,
    )
    assert not edited.is_error, edited.error
    assert read(paths["strategy_md_path"]) == guide

    receipt = producer("strategy_validate", proposal_id=pid)
    assert receipt["ok"], receipt
    assert receipt["structural"]["status"] == "passed"
    assert receipt["behavioral"]["status"] == "passed"
    assert receipt["behavioral"]["summary"]["passed"] == 2
    assert receipt["behavioral"]["source_revision"] == receipt["package_hash"]
    assert receipt["package_hash"] != draft["validation"]["package_hash"]
    if not draft_only:
        submitted = producer("strategy_submit_proposal", proposal_id=pid)
        assert submitted["proposal_id"] == pid
        assert submitted["validation"]["package_hash"] == receipt["package_hash"]
    assert [(p.id, p.state) for p in list_proposals(cfg.paths)] == [
        (pid, "draft" if draft_only else "pending_review"),
    ]
    saved = yaml_io.loads(read(paths["strategy_yml_path"]))
    assert saved["execution_mode"] == "script"
    assert saved["agent_task"]["enabled"] is False
    assert saved["schedule"]["enabled"] is False
    assert saved["tuning"]["enabled"] is (not draft_only)
    assert saved["tuning"]["review_plan"] == review
    if not draft_only:
        assert saved["tuning"]["schedule"]["enabled"] is False
    assert read(paths["main_path"]) == MAIN
    assert not cfg.paths.strategy("incremental_observer").exists()
    assert not cfg.paths.triggers_schedules_file.exists()
