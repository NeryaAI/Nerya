"""Per-agent custom model routing + dedicated web_researcher lane.

Covers the four new surfaces:

1. ``SubAgentSpec`` / role.yaml round-trip of the optional
   ``provider`` / ``model`` overrides (save_role → load_registry).
2. ``LLMGateway.call``'s per-call ``model_provider`` / ``model_id``
   override reaching the router as a cfg override.
3. The default ``web_researcher`` role: light tier, browser + search
   skill surface, alias routing, and the light tier actually accepting
   ``subagent_analysis``.
4. Declarative execution policies for tool exposure/defaults, a locked
   light tier, and generic delegation-depth enforcement.
"""

from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace

import pytest

from nerya.agent.loop import LoopConfig, WorkspaceNativeAgentLoop
from nerya.core.config import Config
from nerya.core.paths import WorkspacePaths
from nerya.llm.messages import MessagesResponse
from nerya.llm.model_router import ModelRouter
from nerya.llm.tier_policy import TierPolicy
from nerya.subagents.registry import (
    DEFAULT_SUBAGENT_EXECUTION_POLICIES,
    DEFAULT_SUBAGENT_PROMPTS,
    DEFAULT_SUBAGENT_SKILLS,
    DEFAULT_TIERS,
    SubAgentExecutionPolicy,
    SubAgentSpec,
    build_inline_spec,
    canonical_subagent_name,
    describe_role,
    load_registry,
    save_role,
)
from nerya.subagents.runtime import SubAgentRuntime
from nerya.tools.executor import NativeToolExecutor
from nerya.tools.native import agents as native_agents
from nerya.tools.native import web as native_web
from nerya.tools.native.bootstrap import build_native_tool_deps, register_native_tools
from nerya.tools.native.web import _save_raw_capture
from nerya.tools.orchestrator import ToolOrchestrator
from nerya.tools.permissions import PermissionContext, PermissionEngine, PermissionMode
from nerya.tools.registry import ToolRegistry
from nerya.tools.types import ToolCall, ToolResult
from nerya.workspace.prompt_bundles import load_bundle
from nerya.workspace import prompt_bundles


pytestmark = pytest.mark.smoke


# --------------------------------------------------------- role persistence


def test_save_role_persists_provider_and_model(tmp_path):
    paths = WorkspacePaths(tmp_path)

    record = save_role(
        paths,
        name="cheap_scout",
        prompt="Collect data only.",
        tier="light",
        provider="openai",
        model="gpt-5-mini",
    )

    assert record["provider"] == "openai"
    assert record["model"] == "gpt-5-mini"

    loaded = load_registry(paths)["cheap_scout"]
    assert loaded.provider == "openai"
    assert loaded.model == "gpt-5-mini"
    assert loaded.tier == "light"

    described = describe_role(paths, "cheap_scout")
    assert described["provider"] == "openai"
    assert described["model"] == "gpt-5-mini"


def test_save_role_without_override_keeps_meta_clean(tmp_path):
    paths = WorkspacePaths(tmp_path)

    record = save_role(paths, name="plain_role", prompt="Do things.")

    assert record["provider"] == ""
    assert record["model"] == ""
    spec = load_registry(paths)["plain_role"]
    assert spec.provider == ""
    assert spec.model == ""


def test_build_inline_spec_carries_provider_and_model(tmp_path):
    paths = WorkspacePaths(tmp_path)

    spec = build_inline_spec(
        paths,
        name="adhoc_lane",
        prompt="Inline role.",
        tier="light",
        provider="deepseek",
        model="deepseek-chat",
    )

    assert spec.provider == "deepseek"
    assert spec.model == "deepseek-chat"


def test_inline_role_spec_built_from_model_only_args(tmp_path):
    """A role entry that only pins a model still produces an inline spec."""

    config = SimpleNamespace(paths=WorkspacePaths(tmp_path))
    spec = native_agents._build_inline_role_spec(
        config, name="market_analyst", model="gpt-5-mini",
    )

    assert spec is not None
    assert spec.model == "gpt-5-mini"
    assert spec.provider == ""
    # Canonical defaults still apply for everything else.
    assert spec.tier == DEFAULT_TIERS["market_analyst"]


# --------------------------------------------------------- router override


def test_model_router_dispatch_honours_cfg_override(monkeypatch):
    monkeypatch.setenv("CUSTOM_TEST_API_KEY", "k-test")
    captured: dict = {}

    def adapter(**kwargs):
        captured.update(kwargs)
        return SimpleNamespace(
            text="{}", tokens=1, usd_cost=0.0, provider="custom",
            model=kwargs.get("model"), latency_ms=1, finish_reason="stop",
            mode="live", degraded=False, fallback_used=False, error="",
            total_tokens=1,
        )

    router = ModelRouter(
        tiers={"medium": {"provider": "mock", "model": "medium-model"}},
        providers={"custom": adapter},
        allow_mock=True,
    )

    router.dispatch(
        tier="medium",
        task="subagent_analysis",
        prompt="hi",
        cfg_override={
            "provider": "custom",
            "model": "my-model",
            "provider_key_env": "CUSTOM_TEST_API_KEY",
            "kind": "chat_completions",
        },
    )

    assert captured.get("model") == "my-model"
    assert captured.get("provider_name") == "custom"


# ------------------------------------------------------------ web_researcher


def test_web_researcher_default_role_shape():
    assert DEFAULT_TIERS["web_researcher"] == "light"
    skills = set(DEFAULT_SUBAGENT_SKILLS["web_researcher"])
    assert "browser" in skills
    assert "web_search_fetch" in skills
    prompt = DEFAULT_SUBAGENT_PROMPTS["web_researcher"]
    assert "saved_path" in prompt
    assert "captures" in prompt

    bundled = load_bundle()
    assert bundled.subagents["web_researcher"] == prompt
    policy = DEFAULT_SUBAGENT_EXECUTION_POLICIES["web_researcher"]
    assert policy.locked_tier == "light"
    assert policy.allow_model_override is True
    assert policy.model_override_scope == "tier_routes"
    assert not hasattr(policy, "runtime")
    assert policy.tool_argument_defaults["web_fetch"]["save_raw"] is True
    assert "research_run" not in (
        DEFAULT_SUBAGENT_EXECUTION_POLICIES["buffett_lens"].required_native_tools
    )


def test_web_researcher_tier_is_locked_for_inline_overrides(tmp_path):
    spec = build_inline_spec(
        WorkspacePaths(tmp_path),
        name="web_researcher",
        tier="high",
        provider="openai",
        model="gpt-5-mini",
        execution_policy={"max_iterations": 99, "llm_max_attempts": 9},
    )

    assert spec.tier == "light"
    assert spec.provider == "openai"
    assert spec.model == "gpt-5-mini"
    assert spec.execution_policy.model_override_scope == "tier_routes"
    assert spec.execution_policy.max_iterations == 4
    assert spec.execution_policy.llm_max_attempts == 1


def test_non_collector_role_keeps_custom_model_override(tmp_path):
    spec = build_inline_spec(
        WorkspacePaths(tmp_path),
        name="buffett_lens",
        provider="openai",
        model="gpt-5-mini",
    )

    assert spec.provider == "openai"
    assert spec.model == "gpt-5-mini"


def test_collector_model_override_must_belong_to_light_tier(tmp_path):
    config = Config(
        paths=WorkspacePaths(tmp_path),
        data={
            "llm": {
                "tiers": {
                    "light": {
                        "routes": [
                            {"provider": "openai", "model": "gpt-5-mini"},
                            {"provider": "gemini", "model": "gemini-flash"},
                        ],
                    },
                    "high": {
                        "provider": "openai",
                        "model": "gpt-5-pro",
                    },
                },
            },
        },
    )
    runtime = SubAgentRuntime(
        config=config,
        skills=SimpleNamespace(),
        llm=SimpleNamespace(), tool_registry=ToolRegistry(), tool_executor=object(),
    )
    allowed = build_inline_spec(
        WorkspacePaths(tmp_path),
        name="web_researcher",
        provider="gemini",
        model="gemini-flash",
    )
    rejected = build_inline_spec(
        WorkspacePaths(tmp_path),
        name="web_researcher",
        provider="openai",
        model="gpt-5-pro",
    )

    assert runtime._model_override(allowed) == ("gemini", "gemini-flash")
    assert runtime._model_override(rejected) == (None, None)


def test_web_researcher_aliases_route_to_lane():
    for alias in ("web_scraper", "data_scout", "web_research", "data_collector"):
        assert canonical_subagent_name(alias) == "web_researcher"


def test_light_tier_accepts_subagent_analysis():
    from nerya.core.config import DEFAULT_CONFIG

    tiers = DEFAULT_CONFIG["llm"]["tiers"]
    policy = TierPolicy(tiers=tiers, default_tier="medium")

    assert policy.resolve(
        task="subagent_analysis",
        requested_tier="light",
        caller_allowed_tiers=None,
    ) == "light"
    # Roles that ask for medium keep medium.
    assert policy.resolve(
        task="subagent_analysis",
        requested_tier="medium",
        caller_allowed_tiers=None,
    ) == "medium"


def _write_minimal_bundle(root: Path, policy_text: str) -> None:
    bundle = root / "test"
    bundle.mkdir(parents=True)
    (bundle / "bundle.yml").write_text(
        "\n".join([
            "version: 1",
            "id: test",
            "profile: test",
            "agents: {}",
            "subagents: {}",
            "execution_policies: execution-policies.json",
        ]),
        encoding="utf-8",
    )
    (bundle / "execution-policies.json").write_text(
        policy_text,
        encoding="utf-8",
    )


def test_external_execution_policy_json_resolves_profiles(monkeypatch, tmp_path):
    _write_minimal_bundle(
        tmp_path,
        """
        {
          "subagent_policy_profiles": {
            "collector": {
              "locked_tier": "light",
              "allow_model_override": false,
              "native_tools": {"allow": ["collect"]}
            }
          },
          "subagent_policies": {
            "scout": {"extends": "collector", "max_iterations": 3}
          }
        }
        """,
    )
    monkeypatch.setattr(prompt_bundles, "bundles_root", lambda: tmp_path)

    bundle = prompt_bundles.load_bundle("test")

    assert bundle.sources["execution_policies"] == "execution-policies.json"
    assert bundle.subagent_policies["scout"] == {
        "locked_tier": "light",
        "allow_model_override": False,
        "native_tools": {"allow": ["collect"]},
        "max_iterations": 3,
    }


@pytest.mark.parametrize("policy_text", ["[]", "{not-json"])
def test_external_execution_policy_json_rejects_invalid_content(
    monkeypatch,
    tmp_path,
    policy_text,
):
    _write_minimal_bundle(tmp_path, policy_text)
    monkeypatch.setattr(prompt_bundles, "bundles_root", lambda: tmp_path)

    with pytest.raises(ValueError):
        prompt_bundles.load_bundle("test")


def test_prompt_bundle_package_data_includes_json():
    pyproject = (Path(__file__).parents[1] / "pyproject.toml").read_text(
        encoding="utf-8",
    )

    assert '"workspace/_prompt_bundles/**/*.json"' in pyproject


# --------------------------------------------------------------- raw capture


def test_save_raw_capture_writes_full_payload(tmp_path):
    rel = _save_raw_capture(
        tmp_path,
        kind="web_fetch",
        subject="https://example.com/ir",
        data={"url": "https://example.com/ir", "content": "x" * 10_000},
    )

    assert rel is not None
    saved = tmp_path / rel
    assert saved.is_file()
    body = saved.read_text(encoding="utf-8")
    assert "https://example.com/ir" in body
    assert "state/research_data" in str(saved)


def test_save_raw_capture_without_workspace_is_noop():
    assert _save_raw_capture(
        None, kind="web_fetch", subject="u", data={},
    ) is None


# ---------------------------------------------------------- research nesting


@dataclass
class _FakeDescriptor:
    name: str
    risk: SimpleNamespace = None  # type: ignore[assignment]
    child_max_depth: int | None = None
    delegates_to: str = ""


class _FakeRegistry:
    def __init__(self, descriptors):
        self._descriptors = [
            item if isinstance(item, _FakeDescriptor) else _FakeDescriptor(name=item)
            for item in descriptors
        ]

    def list_tools(self):
        return list(self._descriptors)

    def get(self, name):
        for descriptor in self._descriptors:
            if descriptor.name == name:
                return descriptor
        raise KeyError(name)


def _runtime_with_tools(names):
    return SubAgentRuntime(
        config=SimpleNamespace(),
        skills=SimpleNamespace(),
        llm=SimpleNamespace(),
        tool_registry=_FakeRegistry(names), tool_executor=object(),
    )


def _spec(tmp_path, name="expert"):
    return SubAgentSpec(
        name=name,
        prompt_path=tmp_path / f"{name}.agent.md",
        execution_policy=SubAgentExecutionPolicy(),
    )


def test_delegation_tool_visible_until_its_declared_depth(tmp_path):
    runtime = _runtime_with_tools([
        _FakeDescriptor("research_run", child_max_depth=1),
        "web_fetch",
        "subagent_run",
    ])

    allowed = runtime._allowed_native_tool_names(
        spec=_spec(tmp_path), delegation_depth=0,
    )

    assert "research_run" in allowed
    assert "web_fetch" in allowed
    # Children still can't spawn arbitrary subagents.
    assert "subagent_run" not in allowed


def test_delegation_tool_hidden_at_its_declared_depth(tmp_path):
    runtime = _runtime_with_tools([
        _FakeDescriptor("research_run", child_max_depth=1),
        "web_fetch",
    ])

    allowed = runtime._allowed_native_tool_names(
        spec=_spec(tmp_path), delegation_depth=1,
    )

    assert "research_run" not in allowed
    assert "web_fetch" in allowed


def test_research_run_handler_uses_descriptor_target_and_metadata_depth(
    monkeypatch,
    tmp_path,
):
    captured: dict = {}
    capture = tmp_path / "state" / "research_data" / "capture.json"
    capture.parent.mkdir(parents=True)
    capture.write_text('{"ok": true}', encoding="utf-8")

    class _FakeDispatcher:
        def __init__(self, **kwargs):
            captured["kwargs"] = kwargs

        def dispatch(self, target, *, payload, **kwargs):
            captured["target"] = target
            captured["payload"] = payload
            captured["dispatch_kwargs"] = kwargs
            return {
                "ok": True,
                "subagent": "web_researcher",
                "tier": "light",
                "output": {},
                "metrics": {
                    "skill_calls": [{
                        "ok": True,
                        "skill": "collector_tool",
                        "result": {
                            "data": {
                                "saved_path": "state/research_data/capture.json",
                            },
                        },
                    }],
                },
            }

    monkeypatch.setattr(native_agents, "SubAgentDispatcher", _FakeDispatcher)

    call = ToolCall(
        id="t1",
        name="research_run",
        arguments={"query": "TSLA deliveries", "urls": ["https://ir.tesla.com"]},
        metadata={"delegation_depth": 0, "remaining_wall_seconds": 42.0},
    )
    registry = _FakeRegistry([
        _FakeDescriptor(
            "research_run", delegates_to="web_researcher", child_max_depth=1,
        ),
    ])
    result = native_agents.research_run_handler(
        call,
        config=SimpleNamespace(paths=WorkspacePaths(tmp_path)),
        skills=SimpleNamespace(),
        tool_registry=registry,
    )

    assert result.is_error is False
    assert captured["target"] == "subagent:web_researcher"
    assert "__research_depth" not in captured["payload"]
    assert captured["dispatch_kwargs"]["delegation_depth"] == 1
    assert 0.0 <= captured["dispatch_kwargs"]["max_wall_seconds"] <= 42.0
    assert captured["payload"]["query"] == "TSLA deliveries"
    assert captured["payload"]["urls"] == ["https://ir.tesla.com"]
    assert result.content[0].data["capture_paths"] == [
        "state/research_data/capture.json"
    ]

    multi_query_call = ToolCall(
        id="t1-multi",
        name="research_run",
        arguments={"queries": ["AI capex", "data-center power bottlenecks"]},
        metadata={"delegation_depth": 0},
    )
    multi_query_result = native_agents.research_run_handler(
        multi_query_call,
        config=SimpleNamespace(paths=WorkspacePaths(tmp_path)),
        skills=SimpleNamespace(),
        tool_registry=registry,
    )
    assert multi_query_result.is_error is False
    assert "AI capex" in captured["payload"]["query"]
    assert "data-center power bottlenecks" in captured["payload"]["query"]

    structured_query_result = native_agents.research_run_handler(
        ToolCall(
            id="t1-structured",
            name="research_run",
            arguments={
                "queries": [
                    {"query": "AI capex", "type": "web"},
                    {"query": "GPU supply", "type": "web"},
                ],
            },
            metadata={"delegation_depth": 0},
        ),
        config=SimpleNamespace(paths=WorkspacePaths(tmp_path)),
        skills=SimpleNamespace(),
        tool_registry=registry,
    )
    assert structured_query_result.is_error is False
    assert "AI capex" in captured["payload"]["query"]
    assert "GPU supply" in captured["payload"]["query"]

    string_queries_result = native_agents.research_run_handler(
        ToolCall(
            id="t1-string-queries",
            name="research_run",
            arguments={"queries": "AI capex and GPU supply"},
            metadata={"delegation_depth": 0},
        ),
        config=SimpleNamespace(paths=WorkspacePaths(tmp_path)),
        skills=SimpleNamespace(),
        tool_registry=registry,
    )
    assert string_queries_result.is_error is False
    assert "AI capex and GPU supply" in captured["payload"]["query"]


def test_research_run_native_child_can_use_lazy_web_search_fetch(
    monkeypatch,
    tmp_path,
):
    class _EmptySkillRegistry:
        def list(self):
            return []

        def get(self, name):
            raise KeyError(name)

    class _ScriptedGateway:
        def __init__(self, responses):
            self.responses = list(responses)
            self.tools_per_call = []
            self.messages_per_call = []

        def call_messages(self, **kwargs):
            self.tools_per_call.append({
                str(tool.get("name") or "")
                for tool in (kwargs.get("tools") or [])
            })
            self.messages_per_call.append(kwargs.get("messages") or [])
            return self.responses.pop(0)

    fake_capture = {
        "ok": True,
        "query": "Nerya agent loop",
        "count": 1,
        "attempted": 1,
        "search": {
            "ok": True,
            "engine": "fixture",
            "count": 1,
            "results": [{
                "title": "Nerya",
                "url": "https://example.com/nerya",
                "snippet": "Agent-loop evidence.",
            }],
        },
        "documents": [{
            "rank": 1,
            "title": "Nerya",
            "url": "https://example.com/nerya",
            "ok": True,
            "status": 200,
            "fetch_method": "fixture",
            "markdown": "# Nerya\n\nAgent-loop evidence.",
        }],
    }
    monkeypatch.setattr(native_web.search_fetch, "run", lambda **_kwargs: fake_capture)

    child_gateway = _ScriptedGateway([
        MessagesResponse(
            content=[{
                "type": "tool_use",
                "id": "toolu_child_search",
                "name": "web_search_fetch",
                "input": {"query": "Nerya agent loop"},
            }],
            stop_reason="tool_use",
        ),
        MessagesResponse(
            content=[{
                "type": "text",
                "text": '{"summary":"capture saved","done":true}',
            }],
            stop_reason="end_turn",
        ),
    ])
    monkeypatch.setattr(
        "nerya.subagents.dispatcher.LLMGateway",
        lambda _config: child_gateway,
    )

    root_gateway = _ScriptedGateway([
        MessagesResponse(
            content=[{
                "type": "tool_use",
                "id": "toolu_root_research",
                "name": "research_run",
                "input": {"query": "Nerya agent loop"},
            }],
            stop_reason="tool_use",
        ),
        MessagesResponse(
            content=[{"type": "text", "text": "research complete"}],
            stop_reason="end_turn",
        ),
    ])
    paths = WorkspacePaths(tmp_path)
    config = Config(paths=paths, data={})
    skills = SimpleNamespace(registry=_EmptySkillRegistry())
    registry = ToolRegistry()
    deps = build_native_tool_deps(
        workspace_root=tmp_path,
        skill_roots=[],
        paths=paths,
        config=config,
        skills=skills,
    )
    register_native_tools(registry, deps)
    executor = NativeToolExecutor(
        registry=registry,
        permission_engine=PermissionEngine(),
        permission_context=PermissionContext(mode=PermissionMode.YOLO),
    )
    deps.executor = executor
    loop = WorkspaceNativeAgentLoop(
        gateway=root_gateway,
        registry=registry,
        orchestrator=ToolOrchestrator(registry=registry, executor=executor),
        config=LoopConfig(max_iterations=3, caller="test:research"),
    )

    outcome = loop.run(system="system", user_message="research Nerya")

    assert "research_run" in root_gateway.tools_per_call[0]
    assert "web_search_fetch" in child_gateway.tools_per_call[0]
    assert outcome.error_count == 0
    assert outcome.final_text == "research complete"
    assert "capture_paths" in str(root_gateway.messages_per_call[1])
    saved_paths = list((tmp_path / "state" / "research_data").rglob("*.json"))
    assert len(saved_paths) == 1
    assert "Agent-loop evidence" in saved_paths[0].read_text(encoding="utf-8")


def test_subagent_run_handler_forwards_parent_wall_budget(monkeypatch, tmp_path):
    captured: dict[str, object] = {}

    class _FakeDispatcher:
        def __init__(self, **kwargs):  # noqa: ARG002
            pass

        def dispatch(self, target, *, payload, **kwargs):
            captured["target"] = target
            captured["payload"] = payload
            captured["dispatch_kwargs"] = kwargs
            return {
                "ok": True,
                "subagent": "analyst",
                "output": {"done": True},
                "metrics": {},
            }

    monkeypatch.setattr(native_agents, "SubAgentDispatcher", _FakeDispatcher)
    result = native_agents.subagent_run_handler(
        ToolCall(
            id="subagent-parent-budget",
            name="subagent_run",
            arguments={"name": "analyst", "payload": {"task": "inspect"}},
            metadata={"remaining_wall_seconds": 42.0},
        ),
        config=SimpleNamespace(paths=WorkspacePaths(tmp_path)),
        skills=SimpleNamespace(),
    )

    assert result.is_error is False
    assert captured["target"] == "subagent:analyst"
    assert 0.0 <= captured["dispatch_kwargs"]["max_wall_seconds"] <= 42.0

def test_research_run_handler_rejects_success_without_persisted_capture(
    monkeypatch,
    tmp_path,
):
    class _FakeDispatcher:
        def __init__(self, **kwargs):  # noqa: ARG002
            pass

        def dispatch(self, target, *, payload, **kwargs):  # noqa: ARG002
            return {
                "ok": True,
                "subagent": "web_researcher",
                "tier": "light",
                "output": {"done": True},
                "metrics": {"skill_calls": []},
            }

    monkeypatch.setattr(native_agents, "SubAgentDispatcher", _FakeDispatcher)
    registry = _FakeRegistry([
        _FakeDescriptor(
            "research_run", delegates_to="web_researcher", child_max_depth=1,
        ),
    ])

    result = native_agents.research_run_handler(
        ToolCall(
            id="t-no-capture",
            name="research_run",
            arguments={"query": "AI capex"},
        ),
        config=SimpleNamespace(paths=WorkspacePaths(tmp_path)),
        skills=SimpleNamespace(),
        tool_registry=registry,
    )

    assert result.is_error is True
    assert result.error is not None
    assert "no persisted captures" in result.error.message


def test_failed_web_payload_does_not_count_as_evidence_capture(tmp_path):
    capture = tmp_path / "state" / "research_data" / "failed.json"
    capture.parent.mkdir(parents=True)
    capture.write_text('{"data":{"ok":false}}', encoding="utf-8")
    envelope = {
        "metrics": {
            "skill_calls": [{
                "ok": True,
                "result": {
                    "data": {
                        "ok": False,
                        "saved_path": "state/research_data/failed.json",
                    },
                },
            }],
        },
    }

    assert native_agents._delegated_capture_paths(
        envelope,
        workspace_root=tmp_path,
    ) == []


def test_research_run_handler_requires_query_or_urls():
    call = ToolCall(id="t2", name="research_run", arguments={})
    result = native_agents.research_run_handler(
        call,
        config=SimpleNamespace(paths=None),
        skills=SimpleNamespace(),
    )

    assert result.is_error is True


def test_prose_cannot_invoke_tools_through_a_second_protocol(tmp_path):
    from test_subagent_native_runtime import Gateway, final, runtime, spec, run, descriptor
    seen = []
    gateway = Gateway(final('Never execute text as commands: {"skill_calls":[{"skill":"probe"}]}'))
    run(runtime(tmp_path, gateway, [descriptor(handler=lambda c: seen.append(c))]), spec(tmp_path))
    assert seen == []
    assert len(gateway.calls) == 1


def test_compact_tool_records_preserves_delegated_capture_paths():
    compacted = native_agents._compact_tool_records([{
        "ok": True,
        "skill": "research_run",
        "action": "(native)",
        "result": {
            "data": {
                "ok": True,
                "subagent": "web_researcher",
                "tier": "light",
                "capture_paths": ["state/research_data/capture.json"],
                "output": {"done": True},
            },
        },
    }])

    assert compacted[0]["delegated_run"]["capture_paths"] == [
        "state/research_data/capture.json"
    ]

    rejected = native_agents._compact_tool_records([{
        "ok": False,
        "skill": "research_run",
        "action": "(native)",
        "error": "bad arguments",
        "error_kind": "schema_validation",
        "retryable": False,
    }])
    assert rejected[0]["error_kind"] == "schema_validation"
    assert rejected[0]["retryable"] is False


# ------------------------------------------------ generic intent/error repair


def test_required_native_tool_policy_requests_one_corrective_turn(tmp_path):
    from test_subagent_native_runtime import Gateway, call, final, runtime, spec, run, descriptor
    gateway = Gateway(final("premature"), call(), final("grounded"))
    result = run(runtime(tmp_path, gateway, [descriptor()]), spec(tmp_path, required_native_tools=["probe"]))
    assert len(gateway.calls) == 3
    assert len(result["metrics"]["skill_calls"]) == 1
    assert gateway.calls[0]["tool_choice"] == {"type": "tool", "name": "probe"}
    assert result["output"]["summary"] == "grounded"


def test_missing_required_native_tool_marks_output_degraded(tmp_path):
    from test_subagent_native_runtime import Gateway, final, runtime, spec, run, descriptor
    gateway = Gateway(final("memory only"), final("still unverified"))
    result = run(runtime(tmp_path, gateway, [descriptor()]),
                 spec(tmp_path, max_iterations=2, required_native_tools=["probe"]))
    assert result["completion_status"] == "blocked"
    assert result["output"]["done"] is False
    assert not result["metrics"]["skill_calls"]
    assert len(gateway.calls) == 2


@pytest.mark.parametrize("explicit,expected", [(None, True), (False, False), (True, True)])
def test_tool_argument_defaults_respect_explicit_arguments(tmp_path, explicit, expected):
    from test_subagent_native_runtime import Gateway, call, final, runtime, spec, run, descriptor
    seen = []
    def handler(c):
        seen.append(c)
        return ToolResult.from_json(tool_use_id=c.id, name=c.name, data={"ok": True})
    arguments = {} if explicit is None else {"save_raw": explicit}
    gateway = Gateway(call(**arguments), final())
    child = spec(tmp_path, tool_argument_defaults={"probe": {"save_raw": True}})
    run(runtime(tmp_path, gateway, [descriptor(handler=handler)]), child, delegation_depth=1)
    assert seen[0].arguments["save_raw"] is expected
    assert seen[0].metadata["delegation_depth"] == 1


@pytest.mark.parametrize(
    ("role", "expert_skill"),
    [
        ("buffett_lens", "expert_investors.buffett"),
        ("damodaran_lens", "expert_investors.damodaran"),
        ("marks_lens", "expert_investors.marks"),
        ("mauboussin_lens", "expert_investors.mauboussin"),
        ("druckenmiller_lens", "expert_investors.druckenmiller"),
        ("serenity_lens", "finance-creators.serenity"),
        ("unusual_whales_lens", "finance-creators.unusual_whales"),
        ("kobeissi_lens", "finance-creators.kobeissi"),
    ],
)
def test_expert_policy_preloads_lens_and_allows_autonomous_research_delegate(
    tmp_path, role, expert_skill,
):
    runtime = _runtime_with_tools([
        _FakeDescriptor("research_run", child_max_depth=1),
        "web_search",
        "web_fetch",
        "web_search_fetch",
        "skill_view",
    ])
    spec = build_inline_spec(WorkspacePaths(tmp_path), name=role)

    allowed = runtime._allowed_native_tool_names(spec=spec, delegation_depth=0)

    assert "research_run" in allowed
    assert "skill_view" in allowed
    assert "web_search" not in allowed
    assert "web_fetch" not in allowed
    assert "web_search_fetch" not in allowed
    assert spec.execution_policy.required_native_tools == []
    assert spec.execution_policy.preload_skills == ["research", expert_skill]
    assert "FIRST call ``skill_view" not in spec.prompt
    assert spec.prompt == load_bundle().subagents[role]
    assert expert_skill in spec.prompt


def test_team_result_preserves_member_provider_and_model(monkeypatch, tmp_path):
    class _FakeDispatcher:
        def __init__(self, **_kwargs):
            pass

        def dispatch(self, target, **_kwargs):
            return {
                "ok": True,
                "subagent": target.split(":", 1)[1],
                "tier": "medium",
                "provider": "openai",
                "model": "gpt-5-mini",
                "tokens": 12,
                "usd": 0.001,
                "output": {
                    "summary": "done",
                    "evidence": [{"source": "https://example.com", "claim": "x"}],
                    "done": True,
                },
                "metrics": {},
                "steps": [],
            }

    monkeypatch.setattr(native_agents, "SubAgentDispatcher", _FakeDispatcher)
    call = ToolCall(
        id="team-models",
        name="team_run",
        arguments={"task": "analyse", "roles": [{"name": "analyst"}]},
        turn_id="turn-models",
    )

    result = native_agents.team_run_handler(
        call,
        config=Config(paths=WorkspacePaths(tmp_path), data={}),
        skills=SimpleNamespace(),
    )
    member = result.content[0].data["results"][0]

    assert member["provider"] == "openai"
    assert member["model"] == "gpt-5-mini"
