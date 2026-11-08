"""Producer regression for acmd_d5510915cd083870893ca24c, event 37065.

The six-file bundle is minimized; the load-bearing input is the scalar
strategy.yml::tuning.subagent, not the valid top-level list of role names.
All persistence checks use pytest's isolated workspace, never the runtime DB.
"""
from copy import deepcopy
from contextlib import closing

import pytest
from jsonschema import Draft7Validator

from nerya.core import yaml_io
from nerya.core.config import Config
from nerya.core.errors import NeryaError
from nerya.core.paths import WorkspacePaths
from nerya.db import connect
from nerya.evolution.strategy_code_generator import (
    StrategyCodeGenerator,
    StrategyGenerationRequest,
)
from nerya.tools.native.strategy_runtime import (
    STRATEGY_DRAFT_PROPOSAL_SCHEMA,
    strategy_draft_proposal_handler,
)
from nerya.tools.types import ToolCall, ToolErrorKind

pytestmark = pytest.mark.smoke


def _args():
    return {
        "strategy_id": "btc_ema_subagent_1h",
        "execution_mode": "script",
        "markets": ["BINANCE:BTC/USDT"],
        "accounts": ["paper_main"],
        "subagents": ["market_analyst", "strategy_tuner"],
        "create_tuning": True,
        "files": {
            "strategy.yml": yaml_io.dumps({
                "strategy_id": "btc_ema_subagent_1h",
                "execution_mode": "script",
                "subagents": ["market_analyst", "strategy_tuner"],
                "schedule": {"type": "cron", "cron": "5 * * * *", "enabled": False},
                "tuning": {
                    "enabled": True,
                    "schedule": {"type": "cron", "cron": "0 9 * * 1", "enabled": False},
                    "subagent": "strategy_tuner",
                    "prompt_file": "subagents/strategy_tuner.agent.md",
                },
            }),
            "main.py": "def run(ctx):\n    return ctx.result.hold(reason='producer_fixture')\n",
            "strategy.md": "# Authored script with an explicit analyst gate\n",
            "subagents/market_analyst.agent.md": "# Authored analyst instructions\n",
            "subagents/strategy_tuner.agent.md": "# Authored review instructions\n",
            "tests/test_contract.py": "def test_authored_contract():\n    assert 2 + 2 == 4\n",
        },
    }


def _correct_tuning(args):
    manifest = yaml_io.loads(args["files"]["strategy.yml"])
    tuning = manifest["tuning"]
    tuning["subagent"] = {
        "name": tuning["subagent"],
        "prompt_file": tuning.pop("prompt_file"),
        "tier": "medium",
    }
    args["files"]["strategy.yml"] = yaml_io.dumps(manifest)


def _call(paths, args):
    return strategy_draft_proposal_handler(
        ToolCall(name="strategy_draft_proposal", arguments=args),
        config=Config(paths=paths, data={}),
    )


def _snapshot(paths):
    # Every path is checked, including auxiliary files, journals and SQLite.
    return {
        str(path.relative_to(paths.root)): path.read_bytes() if path.is_file() else None
        for path in paths.root.rglob("*")
    }


def _assert_atomic_rejection(tmp_path, args, field):
    paths = WorkspacePaths(tmp_path)
    with closing(connect(paths.db)) as db:
        before_db = list(db.iterdump())
    before_files = _snapshot(paths)
    before_args = deepcopy(args)

    result = _call(paths, args)

    assert result.is_error
    assert result.error.kind == ToolErrorKind.SCHEMA_VALIDATION, result.error
    assert result.error.recovery_hint["field"] == field
    assert result.error.recovery_hint["correction"]
    assert result.error.recovery_hint["preserve_other_arguments"] is True
    assert args == before_args
    assert not paths.proposals.exists()
    assert _snapshot(paths) == before_files
    with closing(connect(paths.db)) as db:
        assert list(db.execute("SELECT * FROM proposals")) == []
        assert list(db.iterdump()) == before_db
    return result


def test_trace_scalar_tuning_subagent_is_actionable_and_atomic(tmp_path):
    args = _args()
    Draft7Validator(STRATEGY_DRAFT_PROPOSAL_SCHEMA).validate(args)
    result = _assert_atomic_rejection(
        tmp_path, args, ["files", "strategy.yml", "tuning", "subagent"]
    )
    correction = result.error.recovery_hint["correction"]
    assert "name" in correction and "prompt_file" in correction
    assert "strategy_id" in result.error.message


def test_corrected_full_files_keep_authored_source_and_roles(tmp_path, monkeypatch):
    args = _args()
    _correct_tuning(args)
    def unused_template(*args, **kwargs):
        pytest.fail("Full authored files must not render replaced templates")
    for method in ("_render_main", "_render_playbook", "_render_contract_test"):
        monkeypatch.setattr(StrategyCodeGenerator, method, unused_template)
    monkeypatch.setattr("nerya.evolution.strategy_code_generator._default_subagent_prompt", unused_template)
    monkeypatch.setattr("nerya.evolution.strategy_code_generator._tuning_subagent_prompt", unused_template)
    before_args = deepcopy(args)
    paths = WorkspacePaths(tmp_path)
    result = _call(paths, args)
    assert not result.is_error, result.error
    data = result.content[0].data
    assert data["state"] == "draft"
    assert data["seeded_from"] == "provided_files"
    main = paths.root / data["proposal_paths"]["main_path"]
    for name, content in args["files"].items():
        if name != "strategy.yml":
            assert (main.parent / name).read_text() == content
    manifest = yaml_io.load(main.parent / "strategy.yml")
    assert manifest["strategy_id"] == args["strategy_id"]
    assert manifest["subagents"] == args["subagents"]
    assert manifest["execution_mode"] == "script"
    assert manifest["schedule"]["enabled"] is False
    assert manifest["tuning"] == yaml_io.loads(args["files"]["strategy.yml"])["tuning"]
    assert args == before_args


@pytest.mark.parametrize(("key", "value", "field"), [
    ("subagents", [{"name": "market_analyst"}], ["subagents", 0]),
    ("subagents", [["market_analyst"]], ["subagents", 0]),
    ("subagents", [None], ["subagents", 0]),
    ("subagents", [7], ["subagents", 0]),
    ("markets", [7], ["markets", 0]),
    ("extra_subagent_prompts", {"market_analyst": {}}, ["extra_subagent_prompts", "market_analyst"]),
    ("files", {"main.py": 7}, ["files", "main.py"]),
    ("unadvertised", True, ["unadvertised"]),
])
def test_nested_arguments_reject_before_coercion_or_persistence(tmp_path, key, value, field):
    args = _args()
    _correct_tuning(args)
    args[key] = value
    assert not Draft7Validator(STRATEGY_DRAFT_PROPOSAL_SCHEMA).is_valid(args)
    _assert_atomic_rejection(tmp_path, args, field)


def test_missing_strategy_id_is_not_inferred_from_full_manifest(tmp_path):
    args = _args()
    _correct_tuning(args)
    del args["strategy_id"]
    _assert_atomic_rejection(tmp_path, args, ["strategy_id"])


def test_malformed_bundle_has_no_artifacts_in_fresh_workspace(tmp_path):
    result = _call(WorkspacePaths(tmp_path), _args())
    assert result.error.kind == ToolErrorKind.SCHEMA_VALIDATION
    assert list(tmp_path.rglob("*")) == []


def test_shared_scaffold_rejects_trace_shape_before_proposal_creation(tmp_path):
    args = _args()
    request = StrategyGenerationRequest(
        strategy_id=args["strategy_id"], markets=tuple(args["markets"]),
        accounts=tuple(args["accounts"]), subagents=tuple(args["subagents"]),
        execution_mode="script", files=args["files"],
    )
    with pytest.raises(NeryaError, match="tuning.subagent"):
        StrategyCodeGenerator(WorkspacePaths(tmp_path)).generate(request, validate=False)
    assert list(tmp_path.rglob("*")) == []


@pytest.mark.parametrize("schedule", [{}, {"schedule_every_seconds": 3600}, {"schedule_cron": "5 * * * *"}])
def test_new_draft_schedules_default_disabled(tmp_path, schedule):
    args = _args()
    del args["files"]
    args.update(schedule)
    paths = WorkspacePaths(tmp_path)
    result = _call(paths, args)
    assert not result.is_error, result.error
    assert any("First read_file the exact returned path" in step
               for step in result.content[0].data["next_steps"])
    manifest = yaml_io.load(paths.root / result.content[0].data["proposal_paths"]["strategy_yml_path"])
    assert manifest["schedule"]["enabled"] is False
    assert manifest["tuning"]["schedule"]["enabled"] is False
    if "schedule_every_seconds" in schedule:
        assert manifest["schedule"]["every_seconds"] == 3600
    if "schedule_cron" in schedule:
        assert manifest["schedule"]["cron"] == schedule["schedule_cron"]
    assert not paths.triggers_schedules_file.exists()


@pytest.mark.parametrize("enabled", [None, False, True])
def test_authored_schedule_defaults_preserve_explicit_settings(tmp_path, enabled):
    args = _args()
    _correct_tuning(args)
    manifest = yaml_io.loads(args["files"]["strategy.yml"])
    for schedule in (manifest["schedule"], manifest["tuning"]["schedule"]):
        if enabled is None:
            del schedule["enabled"]
        else:
            schedule["enabled"] = enabled
    args["files"]["strategy.yml"] = yaml_io.dumps(manifest)
    paths = WorkspacePaths(tmp_path)
    result = _call(paths, args)
    assert not result.is_error, result.error
    staged = yaml_io.load(paths.root / result.content[0].data["proposal_paths"]["strategy_yml_path"])
    assert staged["schedule"]["enabled"] is (enabled is True)
    assert staged["tuning"]["schedule"]["enabled"] is (enabled is True)
    assert not paths.triggers_schedules_file.exists()


@pytest.mark.parametrize("enabled", [False, True])
def test_cloned_schedule_settings_are_preserved(tmp_path, enabled):
    args = _args()
    _correct_tuning(args)
    manifest = yaml_io.loads(args["files"]["strategy.yml"])
    manifest["markets"] = args["markets"]
    manifest["accounts"] = args["accounts"]
    manifest["schedule"]["enabled"] = enabled
    manifest["tuning"]["schedule"]["enabled"] = enabled
    args["files"]["strategy.yml"] = yaml_io.dumps(manifest)
    paths = WorkspacePaths(tmp_path)
    source = paths.strategy(args["strategy_id"])
    for name, content in args["files"].items():
        path = source / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)
    original = (source / "strategy.yml").read_bytes()
    result = _call(paths, {"strategy_id": "cloned_script", "from_strategy_id": args["strategy_id"]})
    assert not result.is_error, result.error
    staged = yaml_io.load(paths.root / result.content[0].data["proposal_paths"]["strategy_yml_path"])
    assert staged["schedule"]["enabled"] is enabled
    assert staged["tuning"]["schedule"]["enabled"] is enabled
    assert (source / "strategy.yml").read_bytes() == original
    assert not paths.triggers_schedules_file.exists()
