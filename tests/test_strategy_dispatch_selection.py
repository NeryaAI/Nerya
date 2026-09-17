"""Generic invocation selection tests, independent of any indicator strategy."""
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace
import runpy
import pytest
from nerya.strategies import StrategyAgentTask
from nerya.strategies.input_context import StrategyInputContext, collect_task_context
from nerya.strategies.context import build_strategy_context
from nerya.triggers.strategy_agent_task_executor import StrategyAgentTaskExecutor

pytestmark = pytest.mark.smoke


def inputs():
    calls = []
    market = SimpleNamespace(candles=lambda m, **kw: calls.append((m, kw)) or [{"close": 0}])
    store = StrategyInputContext([{"id": "bars", "timeframe": "15m"}, {"id": "daily", "timeframe": "1d"}], market, None, ("MOCK:BTC",), "r1")
    store.publish("selected", {"score": 0, "confirmed": False})
    store.publish("unselected", {"private": "DO_NOT_TRANSFER"})
    return SimpleNamespace(inputs=store, trigger=SimpleNamespace(payload={"extra": "TRIGGER_NOT_SELECTED"})), calls


def test_stop_is_skip_and_does_not_prefetch():
    ctx, calls = inputs()
    task = StrategyAgentTask.stop("condition false", path="no_signal")
    collect_task_context(task, ctx, {"sources": ["bars", "daily"]})
    assert task.status == "skip" and task.metadata["path"] == "no_signal"
    assert calls == [] and task.context == {}


@pytest.mark.parametrize("status", ["stop", "continue", "route", "typo"])
def test_bad_dictionary_status_never_dispatches(status):
    task = StrategyAgentTask.from_value({"status": status, "prompt": "do not execute"})
    assert task.status == "error"


@pytest.mark.parametrize("kwargs", [{"roles": "left"}, {"roles": ["left", "left"]}, {"sources": [None]}, {"outputs": [" a "]}, {"include_trigger": "false"}])
def test_invalid_selection_fails(kwargs):
    with pytest.raises(ValueError):
        StrategyAgentTask.dispatch(prompt="test", **kwargs)


def test_explicit_empty_selection_excludes_defaults_but_keeps_payload():
    ctx, calls = inputs()
    task = StrategyAgentTask.dispatch(prompt="test", context={"zero": 0, "false": False}, sources=[], outputs=[], include_trigger=False, roles=[])
    collect_task_context(task, ctx, {"sources": ["bars", "daily"], "include_trigger": True})
    assert task.context == {"published": {}, "script_outputs": {"zero": 0, "false": False}, "trigger": {}, "input_errors": {}}
    assert calls == []
    assert task.metadata["input_selection"]["sources"] == []


def test_only_selected_published_result_and_source_are_passed():
    ctx, calls = inputs()
    task = StrategyAgentTask.dispatch(prompt="test", sources=["bars"], outputs=["selected"], include_trigger=False)
    collect_task_context(task, ctx, {"sources": ["daily"]})
    assert len(calls) == 1 and calls[0][1]["timeframe"] == "15m"
    assert set(task.context["published"]) == {"selected", "source:bars"}
    assert task.context["published"]["selected"]["value"] == {"score": 0, "confirmed": False}
    assert "DO_NOT_TRANSFER" not in str(task.context)
    assert "TRIGGER_NOT_SELECTED" not in str(task.context)


def test_missing_published_name_fails_before_fetch():
    ctx, calls = inputs()
    task = StrategyAgentTask.dispatch(prompt="test", sources=["bars"], outputs=["missing"])
    with pytest.raises(ValueError, match="actual published"):
        collect_task_context(task, ctx, {})
    assert calls == []


def test_unknown_source_and_output_namespace_are_rejected():
    ctx, calls = inputs()
    for task in [StrategyAgentTask.dispatch(prompt="test", sources=["missing"]), StrategyAgentTask.dispatch(prompt="test", outputs=["source:bars"])]:
        with pytest.raises(ValueError): collect_task_context(task, ctx, {})
    assert calls == []


def test_explicit_output_selection_does_not_enable_disabled_transfer():
    ctx, _ = inputs()
    with pytest.raises(ValueError, match="disabled"):
        collect_task_context(StrategyAgentTask.dispatch(prompt="test", outputs=["selected"]), ctx, {"include_script_outputs": False})


def test_round_trip_copies_selection_and_preserves_none_vs_empty():
    names = ["left"]
    task = StrategyAgentTask.dispatch(prompt="test", roles=names, path="risk", sources=[], outputs=[])
    names.append("right")
    restored = StrategyAgentTask.from_value(task.asdict())
    assert restored.roles == ["left"] and restored.sources == [] and restored.include_trigger is None
    assert restored.metadata["path"] == "risk"


def test_selected_roles_override_defaults_without_mutating_package(tmp_path):
    seed = runpy.run_path(str(Path(__file__).with_name("test_strategy_agent_context.py")))["seed"]
    config, package, _ = seed(tmp_path, team=True)
    original = deepcopy(package.manifest.asdict())
    executor = StrategyAgentTaskExecutor(config)
    ctx = build_strategy_context(config=config, package=package, run_id="choice", session_id=None)
    for names in (["left"], ["right"], ["left", "right"], []):
        task = StrategyAgentTask.dispatch(prompt="test", roles=names, sources=[], outputs=[], include_trigger=False)
        actual = executor._call_task_builder(package, lambda _: task, ctx)
        assert actual.metadata["selected_roles"] == names
        assert executor._is_agent_team_task(actual, package) == bool(names)
        assert [r["name"] for r in executor._team_role_entries(package, actual)] == names
    assert package.manifest.asdict() == original
    inherited = StrategyAgentTask.dispatch(prompt="test")
    assert [r["name"] for r in executor._team_role_entries(package, inherited)] == ["left", "right"]


def test_unknown_role_is_rejected_before_context_collection(tmp_path):
    seed = runpy.run_path(str(Path(__file__).with_name("test_strategy_agent_context.py")))["seed"]
    config, package, _ = seed(tmp_path, team=True)
    executor = StrategyAgentTaskExecutor(config)
    task = StrategyAgentTask.dispatch(prompt="test", roles=["undeclared"])
    with pytest.raises(ValueError, match="declared"):
        executor._call_task_builder(package, lambda _: task, object())


def seed_selected_branch(root):
    """Acceptance fixture only. Extend, do not reimplement, the MACD contract."""
    from nerya.core import yaml_io
    from nerya.strategies.package import load_package
    legacy = runpy.run_path(str(Path(__file__).with_name("test_workflow_branch_contract.py")))
    config, package = legacy["seed_branch"](root, team=True)
    manifest = yaml_io.load(package.root / "strategy.yml")
    manifest["agent_context"].update(sources=["bars"], include_trigger=True)
    yaml_io.dump(package.root / "strategy.yml", manifest)
    main = legacy["FILES"]["main.py"].replace("StrategyAgentTask.skip(", "StrategyAgentTask.stop(")
    main = main.replace('        signal = read_signal(ctx)', '        signal = read_signal(ctx)\n        ctx.inputs.publish("local_diagnostic", {"detail": "OUTPUT_NOT_FOR_AGENT"})')
    (package.root / "main.py").write_text(main)
    for filename, role, branch in [("wfbc_up.py", "left", "上穿：机会分析"), ("wfbc_down.py", "right", "下穿：风险分析")]:
        content = legacy["FILES"][filename].replace('        prompt=', f'        path={branch!r}, roles=[{role!r}],\n        sources=[], outputs=[], include_trigger=False,\n        prompt=')
        (package.root / filename).write_text(content)
    import json
    metadata = json.loads((package.root / "workflow.json").read_text())
    metadata["nodes"].update({"agent:role/left": {"title": "机会分析 Agent"}, "agent:role/right": {"title": "风险分析 Agent"}})
    (package.root / "workflow.json").write_text(json.dumps(metadata, ensure_ascii=False))
    return config, load_package(config.paths, package.strategy_id)


def test_static_branches_target_only_selected_roles(tmp_path):
    from nerya.strategies.workflow_graph import build_workflows
    _, package = seed_selected_branch(tmp_path)
    files = {path: (package.root / path).read_text() for path in package.files}
    view = build_workflows(files)["strategy"]
    routes = [e for e in view["edges"] if e["relation"] == "conditional_dispatch"]
    assert {(e["source"], e["target"]) for e in routes} == {("script:wfbc_up.py", "agent:role/left"), ("script:wfbc_down.py", "agent:role/right")}
    assert {e["label"] for e in routes} == {"上穿：机会分析", "下穿：风险分析"}
    assert not any(e["source"] == "source:data_sources/bars" and e["relation"] == "context" for e in view["edges"])
    assert next(n for n in view["nodes"] if n["id"] == "script:main.py")["control"]["can_stop"] is True
    assert all(n["execution"]["mode"] == "conditional" for n in view["nodes"] if n["id"].startswith("agent:role/"))


def test_unknown_computed_choice_does_not_claim_default_team(tmp_path):
    from nerya.strategies.workflow_graph import build_workflows
    _, package = seed_selected_branch(tmp_path)
    files = {path: (package.root / path).read_text() for path in package.files}
    files["wfbc_up.py"] = files["wfbc_up.py"].replace("roles=['left']", "roles=selected_roles(ctx)")
    graph = build_workflows(files)["strategy"]
    assert not any(e["source"] == "script:wfbc_up.py" and e["target"].startswith("agent:role/") and e["relation"].endswith("dispatch") for e in graph["edges"])
