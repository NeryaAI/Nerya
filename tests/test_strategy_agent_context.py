from __future__ import annotations

from copy import deepcopy
import json
from pathlib import Path
import threading
from types import SimpleNamespace

import pytest
from nerya.core import yaml_io
from nerya.core.config import Config, DEFAULT_CONFIG
from nerya.core.paths import WorkspacePaths
from nerya.strategies.agent_execution import execution_config, validate_agent_configuration
from nerya.strategies.agent_task import StrategyAgentTask
from nerya.strategies.input_context import StrategyInputContext, collect_task_context, context_prompt
from nerya.strategies.package import load_package
from nerya.strategies.workflow_graph import build_workflows
from nerya.tools.capability_policy import normalise_tool_policy, tool_policy_allows
from nerya.triggers.runtime import TriggerRuntime
from nerya.triggers.strategy_agent_task_executor import StrategyAgentTaskExecutor, TARGET

pytestmark = pytest.mark.smoke


def seed(tmp_path: Path, *, team: bool = False, execution=None, script=None):
    config = Config(paths=WorkspacePaths(tmp_path), data=deepcopy(DEFAULT_CONFIG))
    config.data["runtime"]["mock_mode"] = False
    yaml_io.dump(config.paths.accounts_file, {"accounts": [{"id": "paper", "venue": "mock", "exchange": "mock", "mode": "paper", "status": "active", "permissions": {"place_order": False}}]})
    root = config.paths.strategy("flow_context")
    raw = {"version": 1, "strategy_id": "flow_context", "title": "Context contract", "mode": "paper",
           "entrypoint": "main.py:run", "execution_mode": "agent", "agent_task": {"enabled": True},
           "markets": ["mock:BTC/USDT"], "accounts": ["paper"], "schedule": {"type": "interval", "every_seconds": 300, "enabled": False},
           "policy": {"allow_direct_order": False, "max_run_seconds": 5},
           "agent_profile": {"role": "Review evidence, use tools, then return the requested table. No orders."},
           "data_sources": [{"id": "bars", "provider": "custom.fixture", "capability": "candles", "limit": 3}],
           "agent_context": {"sources": ["bars"]},
           "agent_execution": execution or {"capabilities": "inherit", "team": {"enabled": team, "roles": ["left", "right"] if team else []}},
           "subagents": ["left", "right"] if team else []}
    yaml_io.dump(root / "strategy.yml", raw)
    (root / "main.py").write_text(script or '''from nerya.strategies.agent_task import StrategyAgentTask

def run(ctx):
    ctx.inputs.publish("source:bars", [{"close": 123.45, "fixture_marker": "WORKFLOW_EVIDENCE_765"}], source="test fixture")
    ctx.inputs.publish("preflight", {"score": 0, "confirmed": False}, source="preflight.py")
    return StrategyAgentTask.dispatch(prompt="ORIGINAL_REQUEST: return a table; do not place orders", context={"signal": "fixture_signal"})
''')
    if team:
        for role in ("left", "right"):
            target = root / "subagents" / f"{role}.agent.md"
            target.parent.mkdir(exist_ok=True)
            target.write_text(f"Analyze the supplied workflow context as {role}. Read evidence before answering. Return JSON with summary, done=true. No orders.")
    (tmp_path / "fixture-evidence.txt").write_text("FILE_EVIDENCE_876: controlled evidence, not market data")
    return config, load_package(config.paths, "flow_context"), raw


def test_inherits_main_not_script_llm_cap_and_does_not_mutate(tmp_path):
    config, package, _ = seed(tmp_path)
    before = deepcopy(config.data)
    scoped = execution_config(config, package.manifest)
    assert scoped.get("agent.native.max_iterations") == config.get("agent.native.max_iterations") > 1
    assert scoped.get("agent.native.max_total_tool_calls") == config.get("agent.native.max_total_tool_calls") > 1
    assert scoped.get("agent.subagents.max_iterations") == config.get("agent.native.max_iterations")
    assert tool_policy_allows(scoped.get("agent.native.tool_policy"), "web_search_fetch")
    assert tool_policy_allows(scoped.get("agent.native.tool_policy"), "read_file")
    scoped.data["agent"]["native"]["max_iterations"] = 2
    assert config.data == before


def test_configured_budgets_and_global_deny_are_effective(tmp_path):
    config, package, _ = seed(tmp_path, execution={"capabilities": "inherit", "tier": "high", "max_iterations": 17, "max_tool_calls": 29, "max_wall_seconds": 77, "denied_tools": ["trade_*"]})
    config.data["agent"]["native"]["tool_policy"] = {"deny": ["run_shell"]}
    scoped = execution_config(config, package.manifest)
    assert scoped.get("agent.native.max_iterations") == 17
    assert scoped.get("agent.native.max_total_tool_calls") == 29
    assert scoped.get("agent.native.max_wall_seconds") == 77
    assert scoped.get("agent.native.tier") == "high"
    policy = scoped.get("agent.native.tool_policy")
    assert not tool_policy_allows(policy, "run_shell")
    assert not tool_policy_allows(policy, "trade_intent_submit")
    assert tool_policy_allows(policy, "read_file")


@pytest.mark.parametrize("bad", [0, -1, True, "20", float("inf"), float("nan"), 1.5])
def test_invalid_iteration_budget_rejected(bad):
    with pytest.raises(ValueError):
        validate_agent_configuration({"agent_execution": {"max_iterations": bad}})


def test_capability_intersection_and_explicit_empty():
    policy = normalise_tool_policy({"allow_groups": [["read_*", "web_*"], ["web_*", "write_*"]], "deny": ["web_private"]})
    assert tool_policy_allows(policy, "web_search_fetch")
    assert not tool_policy_allows(policy, "read_file")
    assert not tool_policy_allows(policy, "web_private")
    assert not tool_policy_allows(normalise_tool_policy({"allow": []}), "read_file")
    with pytest.raises(ValueError):
        validate_agent_configuration({"agent_execution": {"capabilities": "inherit"}, "agent_profile": {"allowed_tools": ["read_file"]}})


def test_source_fetch_once_and_values_are_isolated():
    calls = []
    data = [{"close": 0, "complete": False}]
    market = SimpleNamespace(candles=lambda *a, **kw: calls.append((a, kw)) or data)
    inputs = StrategyInputContext([{ "id": "bars", "provider": "runtime.market", "timeframe": "15m", "limit": 37}], market, None, ("mock:BTC/USDT",), "r1")
    first = inputs.source("bars"); first[0]["close"] = 999
    assert inputs.source("bars") == [{"close": 0, "complete": False}]
    assert len(calls) == 1 and calls[0][1] == {"timeframe": "15m", "limit": 37}
    inputs.publish("derived", {"score": 0, "pass": False})
    snapshot = inputs.snapshot(); snapshot["derived"]["value"]["score"] = 12
    assert inputs.snapshot()["derived"]["value"]["score"] == 0


def test_skip_no_prefetch_and_missing_source_is_explicit():
    inputs = StrategyInputContext([], None, None, (), "r")
    ctx = SimpleNamespace(inputs=inputs, trigger=SimpleNamespace(payload={}))
    collect_task_context(StrategyAgentTask.skip("no signal"), ctx, {"sources": ["missing"]})
    with pytest.raises(ValueError, match="unavailable"):
        collect_task_context(StrategyAgentTask.dispatch(prompt="analyze"), ctx, {"sources": ["missing"]})
    task = StrategyAgentTask.dispatch(prompt="analyze")
    collect_task_context(task, ctx, {"sources": ["missing"], "on_error": "continue"})
    assert "missing" in task.context["input_errors"]


def test_disabled_outputs_excluded_but_selected_source_retained():
    inputs = StrategyInputContext([], None, None, (), "r")
    inputs.publish("secret_step", {"score": 2}); inputs.publish("source:bars", [1, 2])
    task = StrategyAgentTask.dispatch(prompt="analyze", context={"private_selection": 4})
    collect_task_context(task, SimpleNamespace(inputs=inputs, trigger=SimpleNamespace(payload={"extra": 1})), {"include_script_outputs": False, "include_trigger": False, "sources": ["bars"]})
    assert task.context["script_outputs"] == {} and task.context["trigger"] == {}
    assert list(task.context["published"]) == ["source:bars"]


def test_large_input_artifact_is_complete_and_not_part_of_package(tmp_path):
    config, package, _ = seed(tmp_path)
    task = StrategyAgentTask.dispatch(prompt="ORIGINAL", context={"rows": "public evidence row " * 4000, "zero": 0})
    prompt = context_prompt(task, package, "task-safe")
    assert prompt.startswith("ORIGINAL") and '"truncated": true' in prompt
    descriptor = task.metadata["input_context"]
    assert json.loads((package.root / descriptor["path"]).read_text())["zero"] == 0
    assert load_package(config.paths, package.strategy_id).content_hash == package.content_hash
    assert descriptor["truncated"] is True


def test_real_task_builder_transfers_both_script_and_source(tmp_path):
    config, package, _ = seed(tmp_path)
    observed = {}
    class Kernel:
        def run_turn(self, **kwargs):
            observed.update(kwargs)
            return SimpleNamespace(turn_id=kwargs["turn_id"], final_text="done", actions=[], tool_trace=[], decision={}, stopped_reason="end_turn", iterations=3, budget={})
    runtime = TriggerRuntime(config=config, router=TriggerRuntime.boot(config).router,
        agent_task_executor_factory=lambda cfg: StrategyAgentTaskExecutor(cfg, kernel_factory=lambda _: Kernel()))
    out = runtime.emit(runtime.from_payload({"source": "test", "kind": "strategy.tick", "target": TARGET, "strategy_id": package.strategy_id, "payload": {"event_value": 42}}))
    assert out.status == "executed", out.asdict()
    prompt = observed["trigger"]["payload"]["text"]
    for expected in ("WORKFLOW_EVIDENCE_765", '"score": 0', '"confirmed": false', "fixture_signal", "event_value", "ORIGINAL_REQUEST"):
        assert expected in prompt
    assert out.result["iterations"] == 3


def test_workflow_projects_real_context_and_parallel_fan_in(tmp_path):
    _, package, _ = seed(tmp_path, team=True)
    files = {f: (package.root / f).read_text() for f in package.files}
    view = build_workflows(files)
    edges = view["strategy"]["edges"]
    assert {e["target"] for e in edges if e["relation"] == "parallel_dispatch"} == {"agent:role/left", "agent:role/right"}
    assert {e["source"] for e in edges if e["relation"] == "aggregate"} == {"agent:role/left", "agent:role/right"}
    assert any(e["source"] == "source:data_sources/bars" and e["relation"] == "context" for e in edges)
    assert any(e["source"] == "script:main.py" and e["relation"] == "context" for e in edges)


def test_real_kernel_and_team_use_multiple_rounds_and_shared_snapshot(tmp_path, monkeypatch):
    from nerya.llm.messages import MessagesResponse
    from nerya.agent.streaming import get_default_bus
    config, package, _ = seed(tmp_path, team=True)
    barrier = threading.Barrier(2)
    records, lock = [], threading.Lock()
    def response(content, reason):
        return MessagesResponse(content=content, stop_reason=reason, usage={"input_tokens": 10, "output_tokens": 5}, provider="controlled", model="test")
    class Gateway:
        def __init__(self, *_args, **_kwargs):
            self.calls = 0
        def effective_model_metadata(self, *_args, **_kwargs):
            return "controlled", "test", {}
        def call_messages(self, **kwargs):
            self.calls += 1
            child = str(kwargs.get("caller", "")).startswith("subagent:")
            # The provider observes actual loop messages, not a synthetic
            # handoff assembled by the test.
            content = json.dumps(kwargs.get("messages", []), ensure_ascii=False)
            with lock:
                records.append({"gateway": id(self), "n": self.calls, "child": child, "content": content, "tools": [t.get("name") for t in kwargs.get("tools", [])]})
            assert "WORKFLOW_EVIDENCE_765" in content
            if child and self.calls == 1:
                barrier.wait(timeout=10)  # A sequential implementation fails.
            if self.calls <= 2:
                return response([{"type": "tool_use", "id": f"check_{id(self)}_{self.calls}", "name": "read_file", "input": {"path": "fixture-evidence.txt"}}], "tool_use")
            assert "FILE_EVIDENCE_876" in content
            return response([{"type": "text", "text": json.dumps({"summary": "Controlled evidence checked in multiple rounds; no order", "done": True, "evidence_marker": "WORKFLOW_EVIDENCE_765"})}], "end_turn")
    monkeypatch.setattr("nerya.agent.kernel.LLMGateway", Gateway)
    monkeypatch.setattr("nerya.subagents.dispatcher.LLMGateway", Gateway)
    runtime = TriggerRuntime.boot(config)
    out = runtime.emit(runtime.from_payload({"source": "test", "kind": "strategy.tick", "target": TARGET, "strategy_id": package.strategy_id, "payload": {}}))
    assert out.status == "executed", out.asdict()
    assert out.result["required_team_run"]["roles_failed"] == [], out.result["required_team_run"]
    assert set(out.result["required_team_run"]["roles_succeeded"]) == {"left", "right"}
    assert out.result["iterations"] >= 3
    children = {r["gateway"] for r in records if r["child"]}
    assert len(children) == 2
    assert all(max(r["n"] for r in records if r["gateway"] == child) >= 3 for child in children)
    assert any("read_file" in r["tools"] for r in records)
    assert not any("buy|sell|reduce|hold" in r["content"] for r in records if not r["child"])
    events = [e for e in get_default_bus().recent() if e.get("turn_id") == out.turn_id]
    assert len([e for e in events if e["kind"] == "team.member.start"]) == 2
    assert len([e for e in events if e["kind"] == "team.member.end"]) == 2
