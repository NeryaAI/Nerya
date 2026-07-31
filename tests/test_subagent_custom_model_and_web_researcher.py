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

from nerya.core.config import Config
from nerya.core.paths import WorkspacePaths
from nerya.llm.gateway import LLMCall
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
from nerya.subagents import runtime as subagent_runtime
from nerya.tools.native import agents as native_agents
from nerya.tools.native.web import _save_raw_capture
from nerya.tools.registry import ToolRegistry, make_native_descriptor
from nerya.tools.types import PermissionScope, RiskLevel, ToolCall, ToolResult
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
        llm=SimpleNamespace(),
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
    invocation_aliases: tuple[str, ...] = ()
    subject_action_aliases: tuple[str, ...] = ()
    subject_argument: str = ""
    argument_aliases: tuple[tuple[str, str], ...] = ()


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
        tool_registry=_FakeRegistry(names),
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
        metadata={"delegation_depth": 0},
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


def test_raw_tool_request_without_parsed_calls_gets_one_protocol_repair():
    raw = '{"skill_calls":[{"skill":"web_search_fetch","payload":'

    assert subagent_runtime._is_unstructured_protocol_miss(
        {"raw": raw},
        raw,
    ) is True


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


class _RecordingHandlerRegistry:
    """Registry whose descriptors record the ToolCall they receive."""

    def __init__(self, names, *, descriptors=None):
        self.calls = []
        self._names = list(names)
        self._descriptors = descriptors or {}

    def list_tools(self):
        return [self._descriptors.get(n, _FakeDescriptor(name=n)) for n in self._names]

    def get(self, name):
        registry = self

        configured = self._descriptors.get(name)

        class _Desc:
            invocation_aliases = getattr(configured, "invocation_aliases", ())
            subject_action_aliases = getattr(configured, "subject_action_aliases", ())
            subject_argument = getattr(configured, "subject_argument", "")
            argument_aliases = getattr(configured, "argument_aliases", ())
            child_max_depth = getattr(configured, "child_max_depth", None)

            def handler(self, call):
                registry.calls.append(call)
                from nerya.tools.types import ToolResult
                return ToolResult.from_json(
                    tool_use_id=call.id, name=call.name, data={"ok": True},
                )

        if name not in self._names:
            raise KeyError(name)
        return _Desc()


def test_view_action_rewritten_onto_skill_view_native():
    """`{skill: <id>, action: view}` should hit skill_view, not error."""

    reg = _RecordingHandlerRegistry(
        ["skill_view", "web_fetch"],
        descriptors={
            "skill_view": _FakeDescriptor(
                "skill_view",
                subject_action_aliases=("view", "skill_view"),
                subject_argument="skill_id",
            ),
        },
    )
    runtime = SubAgentRuntime(
        config=SimpleNamespace(), skills=SimpleNamespace(),
        llm=SimpleNamespace(), tool_registry=reg,
    )

    out = runtime._dispatch_one(
        {"skill": "expert_investors.buffett", "action": "view"},
        spec_name="buffett_lens",
        allowed=["expert_investors.buffett"],
        allowed_native_tools=["skill_view", "web_fetch"],
        trigger_event_id=None, strategy_id=None, session_id=None,
    )

    assert out["ok"] is True
    assert len(reg.calls) == 1
    assert reg.calls[0].name == "skill_view"
    assert reg.calls[0].arguments["skill_id"] == "expert_investors.buffett"


@pytest.mark.parametrize(
    "entry",
    [
        {
            "skill": "research",
            "action": "research_run",
            "payload": {"query": "AI capex"},
        },
        {
            "skill": "research",
            "action": "web_search",
            "payload": {"query": "AI capex"},
        },
        {
            "skill": "research",
            "action": "run",
            "payload": {"query": "AI capex"},
        },
        {
            "skill": "research",
            "payload": {
                "action": "research_run",
                "inputs": {"query": "AI capex"},
            },
        },
    ],
)
def test_descriptor_invocation_alias_routes_skill_intent_onto_native(entry):
    reg = _RecordingHandlerRegistry(
        ["research_run"],
        descriptors={
            "research_run": _FakeDescriptor(
                "research_run",
                invocation_aliases=(
                    "research",
                    "research.run",
                    "research.research_run",
                    "research.web_search",
                ),
            ),
        },
    )
    runtime = SubAgentRuntime(
        config=SimpleNamespace(), skills=SimpleNamespace(),
        llm=SimpleNamespace(), tool_registry=reg,
    )

    out = runtime._dispatch_one(
        entry,
        spec_name="buffett_lens",
        allowed=["research"],
        allowed_native_tools=["research_run"],
        trigger_event_id=None, strategy_id=None, session_id=None,
    )

    assert out["ok"] is True
    assert len(reg.calls) == 1
    assert reg.calls[0].name == "research_run"
    assert reg.calls[0].arguments == {"query": "AI capex"}


def test_required_native_tool_policy_requests_one_corrective_turn(tmp_path):
    class _SkillRegistry:
        def list(self):
            return []

        def get(self, _name):
            raise KeyError(_name)

    class _Skills:
        registry = _SkillRegistry()

    class _FinalThenResearchLLM:
        def __init__(self):
            self.prompts = []

        def call(self, **kwargs):
            self.prompts.append(kwargs["prompt"])
            if len(self.prompts) == 1:
                parsed = {"summary": "premature", "done": True}
            elif len(self.prompts) == 2:
                parsed = {
                    "skill_calls": [{
                        "skill": "research_run",
                        "payload": {"query": "AI capex"},
                    }],
                    "replan": True,
                }
            else:
                parsed = {"summary": "grounded", "done": True}
            return LLMCall(
                tier="medium",
                task=kwargs["task"],
                caller=kwargs["caller"],
                tokens=3,
                usd=0.001,
                raw="{}",
                parsed=parsed,
                provider="fake",
                model="fake-model",
            )

    calls = []

    def _research(call):
        calls.append(call)
        return ToolResult.from_json(
            tool_use_id=call.id,
            name=call.name,
            data={"ok": True, "captures": ["capture.json"]},
        )

    registry = ToolRegistry()
    registry.register(make_native_descriptor(
        name="research_run",
        description="delegate research",
        input_schema={"type": "object"},
        handler=_research,
        risk=RiskLevel.READ,
        permission_scope=PermissionScope.NONE,
        auto_approve=True,
    ))
    llm = _FinalThenResearchLLM()
    runtime = SubAgentRuntime(
        config=Config(paths=WorkspacePaths(tmp_path), data={}),
        skills=_Skills(),
        llm=llm,
        tool_registry=registry,
    )
    spec = SubAgentSpec(
        name="expert",
        prompt_path=tmp_path / "expert.agent.md",
        prompt="Use the loaded research playbook.",
        execution_policy=SubAgentExecutionPolicy(
            native_tool_allow=["research_run"],
            required_native_tools=["research_run"],
            max_iterations=4,
        ),
    )

    result = runtime.run(
        spec,
        trigger_event_id=None,
        payload={"task": "Research AI"},
    )

    assert len(calls) == 1
    assert len(llm.prompts) == 3
    assert "explicit execution contract" in llm.prompts[0]
    assert "research_run" in llm.prompts[1]
    assert "cite at least one of those exact references" in llm.prompts[2]
    assert result["output"]["summary"] == "grounded"
    assert result["metrics"]["rejected_actions"] == []


def test_missing_required_native_tool_marks_output_degraded(tmp_path):
    class _SkillRegistry:
        def list(self):
            return []

        def get(self, _name):
            raise KeyError(_name)

    class _Skills:
        registry = _SkillRegistry()

    class _FinalOnlyLLM:
        def call(self, **kwargs):
            return LLMCall(
                tier="medium",
                task=kwargs["task"],
                caller=kwargs["caller"],
                tokens=1,
                usd=0.0,
                raw='{"summary":"memory only","done":true}',
                parsed={"summary": "memory only", "done": True},
                provider="fake",
                model="fake-model",
            )

    registry = ToolRegistry()
    registry.register(make_native_descriptor(
        name="research_run",
        description="delegate research",
        input_schema={"type": "object"},
        handler=lambda call: ToolResult.from_json(
            tool_use_id=call.id,
            name=call.name,
            data={"ok": True},
        ),
        risk=RiskLevel.READ,
        permission_scope=PermissionScope.NONE,
        auto_approve=True,
    ))
    runtime = SubAgentRuntime(
        config=Config(paths=WorkspacePaths(tmp_path), data={}),
        skills=_Skills(),
        llm=_FinalOnlyLLM(),
        tool_registry=registry,
    )
    spec = SubAgentSpec(
        name="expert",
        prompt_path=tmp_path / "expert.agent.md",
        prompt="Return evidence.",
        execution_policy=SubAgentExecutionPolicy(
            native_tool_allow=["research_run"],
            required_native_tools=["research_run"],
            max_iterations=2,
        ),
    )

    result = runtime.run(
        spec,
        trigger_event_id=None,
        payload={"task": "Research AI"},
    )

    assert result["output"]["degraded"] is True
    assert result["output"]["error_kind"] == "required_native_tool_missing"
    assert result["output"]["required_tools_missing"] == ["research_run"]


def test_tool_argument_defaults_are_applied_declaratively(tmp_path):
    reg = _RecordingHandlerRegistry(["web_search_fetch"])
    runtime = SubAgentRuntime(
        config=SimpleNamespace(), skills=SimpleNamespace(),
        llm=SimpleNamespace(), tool_registry=reg,
    )

    runtime._dispatch_native(
        "web_search_fetch", payload={"query": "x"}, entry={},
        spec_name="buffett_lens", strategy_id=None, session_id=None,
        trigger_event_id=None, delegation_depth=0,
        execution_policy=SubAgentExecutionPolicy(),
    )
    assert "save_raw" not in reg.calls[-1].arguments

    runtime._dispatch_native(
        "web_search_fetch", payload={"query": "y"}, entry={},
        spec_name="web_researcher", strategy_id=None, session_id=None,
        trigger_event_id=None, delegation_depth=1,
        execution_policy=SubAgentExecutionPolicy(
            tool_argument_defaults={"web_search_fetch": {"save_raw": True}},
        ),
    )
    assert reg.calls[-1].arguments["save_raw"] is True
    assert reg.calls[-1].metadata["delegation_depth"] == 1


def test_declarative_tool_defaults_respect_explicit_arguments():
    reg = _RecordingHandlerRegistry(["web_fetch"])
    runtime = SubAgentRuntime(
        config=SimpleNamespace(), skills=SimpleNamespace(),
        llm=SimpleNamespace(), tool_registry=reg,
    )
    runtime._dispatch_native(
        "web_fetch", payload={"url": "https://x", "save_raw": False}, entry={},
        spec_name="web_researcher", strategy_id=None, session_id=None,
        trigger_event_id=None, delegation_depth=1,
        execution_policy=SubAgentExecutionPolicy(
            tool_argument_defaults={"web_fetch": {"save_raw": True}},
        ),
    )
    assert reg.calls[-1].arguments["save_raw"] is False


def test_descriptor_argument_alias_avoids_schema_retry():
    descriptor = _FakeDescriptor(
        name="collector",
        argument_aliases=(
            ("request", "query"),
            ("task", "query"),
            ("fetch_urls", "urls"),
        ),
    )
    reg = _RecordingHandlerRegistry(
        ["collector"],
        descriptors={"collector": descriptor},
    )
    runtime = SubAgentRuntime(
        config=SimpleNamespace(),
        skills=SimpleNamespace(),
        llm=SimpleNamespace(),
        tool_registry=reg,
    )

    record = runtime._dispatch_native(
        "collector",
        payload={
            "request": "AI capex",
            "fetch_urls": ["https://example.com"],
        },
        entry={},
        spec_name="expert",
        strategy_id=None,
        session_id=None,
        trigger_event_id=None,
    )

    assert record["ok"] is True
    assert reg.calls[-1].arguments == {
        "query": "AI capex",
        "urls": ["https://example.com"],
    }

    second = runtime._dispatch_native(
        "collector",
        payload={"task": "Fetch the supplied sources"},
        entry={},
        spec_name="expert",
        strategy_id=None,
        session_id=None,
        trigger_event_id=None,
    )

    assert second["ok"] is True
    assert reg.calls[-1].arguments == {"query": "Fetch the supplied sources"}


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
    assert "already-loaded expert lens" in spec.prompt


def test_team_result_preserves_member_provider_and_model(monkeypatch):
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
        config=SimpleNamespace(paths=None, get=lambda *_args: None),
        skills=SimpleNamespace(),
    )
    member = result.content[0].data["results"][0]

    assert member["provider"] == "openai"
    assert member["model"] == "gpt-5-mini"
