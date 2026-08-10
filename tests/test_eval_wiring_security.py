from __future__ import annotations

from types import SimpleNamespace

import pytest

from nerya.api.route_scopes import required_scope
from nerya.agent.loop import LoopConfig, _action_tool_wall_reserve_seconds
from nerya.evals.cli import _load_scenarios
from nerya.evals.runner import EvalRunner
from nerya.evals.scenarios import scenario_template
from nerya.evolution.asset_policy import validate_validation_command
from nerya.evolution.validation_plan import (
    build_validation_plan,
    load_validation_plan,
    run_validation_plan,
    validate_plan_record,
)
from nerya.core.paths import WorkspacePaths


pytestmark = pytest.mark.smoke


def test_builtin_catalog_loads_fresh_scenarios():
    first = list(_load_scenarios("nerya.evals.scenarios"))
    second = list(_load_scenarios("nerya.evals.scenarios"))

    assert len(first) == 10
    assert [scenario.id for scenario in first] == [scenario.id for scenario in second]
    assert first[0] is not second[0]


def test_eval_loader_rejects_unregistered_python_without_importing(tmp_path):
    marker = tmp_path / "imported"
    module = tmp_path / "evil.py"
    module.write_text(
        f"from pathlib import Path\nPath({str(marker)!r}).write_text('ran')\n",
        encoding="utf-8",
    )

    with pytest.raises(PermissionError, match="scenario_module_not_registered"):
        list(_load_scenarios(str(module)))
    assert not marker.exists()


def test_eval_validation_command_is_registered_and_narrow():
    assert validate_validation_command(
        "python -m nerya.evals --module nerya.evals.scenarios"
    ).ok
    assert validate_validation_command(
        "python -m nerya.evals --module nerya.evals.scenarios --stop-on-failure"
    ).ok

    blocked = validate_validation_command(
        "python -m nerya.evals --module /tmp/agent_authored.py"
    )
    assert blocked.ok is False
    assert any(reason.startswith("eval_module_not_registered:") for reason in blocked.reasons)

    unknown_flag = validate_validation_command(
        "python -m nerya.evals --module nerya.evals.scenarios --module /tmp/evil.py"
    )
    assert unknown_flag.ok is False
    assert not validate_validation_command(
        "python -m pytest /tmp/evil_test.py -q",
        workspace=WorkspacePaths("/tmp/nerya-validation-root").root,
    ).ok
    assert not validate_validation_command(
        "python -m pytest -p evil_plugin tests -q",
        workspace=WorkspacePaths("/tmp/nerya-validation-root").root,
    ).ok


def test_eval_plan_rejects_unregistered_module_before_execution():
    plan = build_validation_plan(
        [
            {
                "type": "eval_scenario",
                "command": "python -m nerya.evals --module /tmp/agent_authored.py",
            }
        ],
        source="test",
    )

    assert plan.status == "blocked"
    assert any("eval_module_not_registered" in reason for reason in plan.blocked_reasons)
    assert validate_plan_record(plan.asdict())["safe_to_run"] is False


def test_validation_plan_id_cannot_escape_workspace(tmp_path):
    paths = WorkspacePaths(root=tmp_path)
    assert load_validation_plan(paths, "../../../outside") is None
    result = run_validation_plan(paths, plan_id="../../../outside", dry_run=False)
    assert result["ok"] is False
    assert result["reason"] == "not_found"


def test_validation_execution_requires_operator_scope():
    assert required_scope("POST", "/evolution/validation/run") == "admin:ops"
    assert required_scope("GET", "/evolution/auto_apply/status") == "read:runtime"
    assert required_scope("POST", "/evolution/auto_apply/tick") == "admin:ops"
    assert required_scope("GET", "/evolution/proposals") == "read:runtime"
    assert required_scope("GET", "/evolution/proposals/prp_demo") == "read:runtime"
    assert required_scope("POST", "/evolution/proposals/prp_demo/approve") == "write:skills"


def test_runner_uses_advertised_agent_loop_task_and_patched_backend_kwargs():
    captured: dict[str, object] = {}

    def factory(**kwargs):
        captured.update(kwargs)
        return kwargs["config"]

    runner = EvalRunner(
        gateway=SimpleNamespace(_resolve_messages_backend=lambda *_a, **_kw: None),
        registry=None,  # type: ignore[arg-type]
        orchestrator=None,  # type: ignore[arg-type]
        loop_factory=factory,
    )
    config = runner._build_loop(scenario_template("read_grep_edit_shell_final"))
    assert config.task == "normal_agent_loop"
    assert config.action_tool_wall_reserve_seconds == 0.0

    backend = object()
    with runner._patched_backend(backend):
        resolved = runner.gateway._resolve_messages_backend(
            "medium",
            provider_override="mock",
            model_override="scripted",
            route_cfg={},
        )
    assert resolved is backend


def test_action_tool_reserve_keeps_production_default_and_allows_eval_override():
    assert _action_tool_wall_reserve_seconds(LoopConfig()) == 60.0
    assert (
        _action_tool_wall_reserve_seconds(
            LoopConfig(action_tool_wall_reserve_seconds=0.0)
        )
        == 0.0
    )
