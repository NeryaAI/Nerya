from __future__ import annotations

from copy import deepcopy
from pathlib import Path
import runpy
import threading
from types import SimpleNamespace

import pytest
from nerya.strategies.agent_execution import execution_config, validate_agent_configuration
from nerya.strategies.agent_task import StrategyAgentTask
from nerya.strategies.input_context import StrategyInputContext, collect_task_context, context_prompt
from nerya.strategies.context import StrategySubAgents
from nerya.subagents.dispatcher import SubAgentDispatcher
from nerya.tools.types import ToolCall, ToolErrorKind

pytestmark = pytest.mark.smoke
seed = runpy.run_path(str(Path(__file__).with_name("test_strategy_agent_context.py")))["seed"]


@pytest.mark.parametrize("mode", ["default", "auto", "yolo"])
def test_actual_child_executor_cannot_bypass_capability_deny(tmp_path, mode):
    from nerya.skills.kernel import SkillKernel
    cfg, package, _ = seed(tmp_path)
    cfg.data["runtime"]["permission_mode"] = mode
    cfg.data["agent"]["native"]["tool_policy"] = {"deny": ["read_file"]}
    cfg = execution_config(cfg, package.manifest)
    dispatcher = SubAgentDispatcher.for_workspace(cfg, SkillKernel.boot(cfg), strategy_id=package.strategy_id)
    result = dispatcher.executor.execute(ToolCall(id="deny-read", name="read_file", arguments={"path": "fixture-evidence.txt"}))
    assert result.is_error and result.error.kind == ToolErrorKind.PERMISSION_DENIED
    assert "capability scope" in result.error.message
    assert dispatcher.executor.permission_context.mode.value == mode


def test_unselected_source_is_not_smuggled_in_as_script_output():
    inputs = StrategyInputContext([], None, None, (), "run")
    inputs.publish("source:bars", [1, 2]); inputs.publish("computed", {"score": 0})
    task = StrategyAgentTask.dispatch(prompt="review")
    collect_task_context(task, SimpleNamespace(inputs=inputs, trigger=SimpleNamespace(payload={})), {"sources": [], "include_script_outputs": True})
    assert "source:bars" not in task.context["published"]
    assert task.context["published"]["computed"]["value"]["score"] == 0


def test_named_source_mapping_uses_its_key_as_identity():
    calls = []
    market = SimpleNamespace(candles=lambda *a, **kw: calls.append(kw) or [{"close": 5}])
    inputs = StrategyInputContext({"bars": {"provider": "runtime.market", "timeframe": "1h", "limit": 9}}, market, None, ("mock:X",), "run")
    validate_agent_configuration({"data_sources": {"bars": {"provider": "runtime.market"}}, "agent_context": {"sources": ["bars"]}})
    assert inputs.source("bars") == [{"close": 5}]
    assert calls == [{"timeframe": "1h", "limit": 9}]


@pytest.mark.parametrize("bad", [0, True, -2, 2.2, "15"])
def test_member_round_budget_cannot_be_silently_coerced(bad):
    with pytest.raises(ValueError):
        validate_agent_configuration({"subagents": ["analyst"], "agent_execution": {"team": {"role_policies": {"analyst": {"max_iterations": bad}}}}})


def test_enabled_team_cannot_silently_reselect_deselected_roles():
    with pytest.raises(ValueError, match="at least one"):
        validate_agent_configuration({"subagents": ["analyst"], "agent_execution": {"team": {"enabled": True, "roles": []}}})


def test_context_artifact_rejects_escape_and_redacts_secrets(tmp_path):
    _, package, _ = seed(tmp_path)
    task = StrategyAgentTask.dispatch(prompt="review", context={"api_key": "not-a-real-credential", "value": 3})
    prompt = context_prompt(task, package, "valid-task")
    assert "not-a-real-credential" not in prompt
    with pytest.raises(ValueError, match="outside strategy root"):
        context_prompt(task, package, "../../../escape")


def test_script_run_many_is_parallel_and_schema_failure_keeps_sibling():
    from nerya.core.config import Config, DEFAULT_CONFIG
    from nerya.core.paths import WorkspacePaths
    barrier = threading.Barrier(2)
    def run(name, **kwargs):
        barrier.wait(timeout=5)
        data = {"subagent": name, "ok": True, "output": {"n": 0} if name == "left" else {"other": 2}}
        return SimpleNamespace(subagent=name, asdict=lambda: deepcopy(data))
    config = Config(paths=WorkspacePaths(Path(".")), data=deepcopy(DEFAULT_CONFIG))
    real_parallel = SimpleNamespace(config=config, _run_one=run, _journal=lambda *a, **kw: None)
    facade = object.__new__(StrategySubAgents)
    facade.strategy_id = "test"; facade.session_id = "test"; facade.audit = None
    facade._disp = lambda: SimpleNamespace(dispatch_many=lambda *a, **kw: SubAgentDispatcher.dispatch_many(real_parallel, *a, **kw))
    out = facade.run_many(["left", "right"], max_parallel=2, schema={"type": "object", "required": ["n"]})
    assert [r["subagent"] for r in out] == ["left", "right"]
    assert out[0]["ok"] is True and out[0]["output"]["n"] == 0
    assert out[1]["ok"] is False and out[1]["error_kind"] == "schema"
