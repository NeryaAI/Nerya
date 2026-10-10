"""R05/R06: exercise admission, wire payloads and context with no live IO."""
from copy import deepcopy
import json
import sqlite3
from types import SimpleNamespace

import pytest

from nerya.agent.command_runtime import CommandRuntime
from nerya.agent.loop_contracts import LoopConfig
from nerya.agent.loop_state import LoopRunState, LoopUsage
from nerya.api import routes_agent
from nerya.core.config import Config, DEFAULT_CONFIG
from nerya.core.errors import LLMError
from nerya.core.paths import WorkspacePaths
from nerya.llm.attempt_budget import AttemptBudget, attempt_budget_scope
from nerya.llm.gateway import LLMGateway
from nerya.llm.messages import (
    AnthropicMessagesBackend, CodexResponsesMessagesBackend, GeminiMessagesBackend,
    MessagesRequest, MessagesResponse, OllamaMessagesBackend, OpenAIMessagesBackend,
)
from nerya.llm.model_catalog import ModelCatalog
from nerya.llm.model_registry import resolve_context_window

pytestmark = pytest.mark.smoke
TOOL = {"name": "echo", "description": "fixture", "input_schema": {"type": "object"}}
OK = {"choices": [{"message": {"content": "fixture ok"}, "finish_reason": "stop"}],
      "usage": {"prompt_tokens": 10, "completion_tokens": 2}}
REJECTION = {"error": {"message": "reasoning_effort with function tools: Please use /v1/responses instead"}}


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    import socket
    import urllib.request
    def forbidden(*args, **kwargs):
        raise AssertionError("network forbidden")
    monkeypatch.setattr(socket.socket, "connect", forbidden)
    monkeypatch.setattr(urllib.request, "urlopen", forbidden)


class Transport:
    def __init__(self, responses=None):
        self.responses = list(responses or [(200, OK)])
        self.calls = []

    def post_json_with_headers(self, url, *, headers, body, timeout):
        self.calls.append({"url": url, "body": deepcopy(body)})
        status, doc = self.responses.pop(0)
        return status, deepcopy(doc), {}


def config(tmp_path, routes):
    data = deepcopy(DEFAULT_CONFIG)
    data["runtime"]["mock_mode"] = False
    data["llm"]["tiers"] = {"medium": {"routes": routes, "allowed_tasks": ["agent.loop"]}}
    data["agent"]["native"]["tier"] = "medium"
    return Config(paths=WorkspacePaths(tmp_path), data=data)


def call(gateway, **kwargs):
    return gateway.call_messages(task="agent.loop", caller="fixture", tier="medium",
                                 system="fixture", messages=[{"role": "user", "content": "hi"}], **kwargs)


def request(**kwargs):
    return MessagesRequest(system="fixture", messages=[{"role": "user", "content": "hi"}], **kwargs)


def test_admission_keeps_selection_and_allows_same_tier_failover(tmp_path, monkeypatch):
    routes = [{"provider": "openai", "model": "o3", "reasoning_effort": "high"},
              {"provider": "other", "model": "backup", "reasoning_effort": "low"}]
    cfg = config(tmp_path, routes)
    runtime = CommandRuntime(cfg, lambda *_: {}, epoch="model-fixture")
    attempts = []
    def backend(self, tier, **kwargs):
        route = kwargs["route_cfg"]
        def run(req):
            attempts.append((route["provider"], route["model"], req.reasoning_effort))
            if route["provider"] == "openai":
                raise LLMError("fixture rejection")
            return MessagesResponse(content=[], provider=route["provider"], model=route["model"])
        return run
    monkeypatch.setattr(LLMGateway, "_resolve_messages_backend", backend)
    for explicit in (False, True):
        selected = {"model_provider": "openai", "model_id": "o3"} if explicit else {}
        cid = f"fixture-{explicit}"
        runtime.submit({"command_id": cid, "session_id": cid,
                        "request": {"payload": {"text": "fixture"}, **selected}}, start=False)
        with sqlite3.connect(cfg.paths.db) as con:
            row = con.execute("SELECT request_json,context_json FROM agent_commands WHERE command_id=?", (cid,)).fetchone()
        payload, context = map(json.loads, row)
        assert context["accepted_model"]["model"] == "o3"
        assert bool(payload.get("model_provider")) is explicit
        kwargs = {key: payload.get(key) for key in ("model_provider", "model_id")}
        attempts.clear()
        assert call(LLMGateway(cfg), **kwargs).model == "backup"
        assert attempts == [("openai", "o3", "high"), ("other", "backup", "low")]


def test_same_provider_exact_model_uses_own_endpoint_key_and_reasoning(tmp_path, monkeypatch):
    monkeypatch.setenv("FIXTURE_ROUTE_A", "fixture-a")
    monkeypatch.setenv("FIXTURE_ROUTE_B", "fixture-b")
    cfg = config(tmp_path, [
        {"provider": "openai", "model": "o3", "base_url": "https://a.invalid/v1", "provider_key_env": "FIXTURE_ROUTE_A", "reasoning_effort": "low"},
        {"provider": "openai", "models": ["o3-mini", "gpt-5.4"], "base_url": "https://b.invalid/v1", "provider_key_env": "FIXTURE_ROUTE_B", "reasoning_effort": "extra_high"},
    ])
    transport = Transport()
    monkeypatch.setattr("nerya.llm.messages.UrllibTransport.post_json_with_headers", transport.post_json_with_headers)
    gateway = LLMGateway(cfg)
    effective = gateway._messages_route_cfgs("medium", provider_override="openai", model_override="gpt-5.4")
    assert [route["model"] for route in effective] == ["gpt-5.4", "o3", "o3-mini"]
    assert effective[0]["_resolved_provider_key"] == "fixture-b"
    call(gateway, model_provider="openai", model_id="gpt-5.4", tools=[TOOL])
    assert transport.calls[0]["url"] == "https://b.invalid/v1/chat/completions"
    assert transport.calls[0]["body"]["reasoning_effort"] == "xhigh"


@pytest.mark.parametrize("effort,expected", [(None, "high"), ("inherit", "high"), ("off", "none"), ("none", "none")])
def test_reasoning_inherit_and_explicit_off_are_distinct(tmp_path, monkeypatch, effort, expected):
    gateway = LLMGateway(config(tmp_path, [{"provider": "mock", "model": "fixture", "reasoning_effort": "high"}]))
    seen = []
    def backend(*args, **kwargs):
        def run(req):
            seen.append(req.reasoning_effort)
            return MessagesResponse(content=[])
        return run
    monkeypatch.setattr(gateway, "_resolve_messages_backend", backend)
    call(gateway, reasoning_effort=effort)
    assert seen == [expected]


@pytest.mark.parametrize("effort,expected", [("inherit", None), ("off", "none"), ("none", "none"), ("extra_high", "xhigh")])
def test_api_preserves_reasoning_intent_before_kernel(tmp_path, monkeypatch, effort, expected):
    cfg = config(tmp_path, [{"provider": "mock", "model": "fixture"}])
    seen = []
    class StopAtKernel(Exception):
        pass
    def kernel(**kwargs):
        seen.append(kwargs["reasoning_effort"])
        raise StopAtKernel
    monkeypatch.setattr(routes_agent, "AgentKernel", kernel)
    handler = next(h for method, path, h in routes_agent.routes() if path == "/agent/run_turn_internal")
    with pytest.raises(StopAtKernel):
        handler(SimpleNamespace(config=cfg, skills=object()), {"payload": {"text": "hello fixture"}, "reasoning_effort": effort})
    assert seen == [expected]


@pytest.mark.parametrize("model", ["o3", "gpt-5.4", "step-3.7-flash", "openai/gpt-5.5"])
def test_tools_keep_reasoning_effort(model):
    transport = Transport()
    OpenAIMessagesBackend(api_key="fixture", model=model, transport=transport)(request(tools=[TOOL], reasoning_effort="high"))
    assert transport.calls[0]["body"]["reasoning_effort"] == "high"


@pytest.mark.parametrize("factory", [
    lambda t: OpenAIMessagesBackend(api_key="fixture", model="o3", transport=t),
    lambda t: GeminiMessagesBackend(api_key="fixture", model="gemini-3-pro", transport=t),
    lambda t: OllamaMessagesBackend(model="unverified", transport=t),
    lambda t: CodexResponsesMessagesBackend(api_key="fixture", account_id="fixture", model="fixture"),
])
def test_unsupported_off_explains_capability_before_network(factory):
    transport = Transport()
    with pytest.raises(LLMError, match="reasoning_off_unsupported.*no verified mapping"):
        factory(transport)(request(reasoning_effort="off"))
    assert not transport.calls


@pytest.mark.parametrize("provider", ["anthropic", "minimax"])
def test_supported_off_emits_disabled(provider):
    transport = Transport([(200, {"content": [], "stop_reason": "end_turn", "usage": {}} if provider == "anthropic" else OK)])
    backend = (AnthropicMessagesBackend(api_key="fixture", model="claude-sonnet-4-5", transport=transport)
               if provider == "anthropic" else OpenAIMessagesBackend(api_key="fixture", model="MiniMax-M3", reasoning_effort="adaptive", transport=transport))
    backend(request(reasoning_effort="none"))
    assert transport.calls[0]["body"]["thinking"] == {"type": "disabled"}


def test_compat_retry_requires_exact_error_and_consumes_shared_budget():
    transport = Transport([(400, REJECTION), (200, OK)])
    budget = AttemptBudget(limit=1)
    with attempt_budget_scope(budget):
        OpenAIMessagesBackend(api_key="fixture", model="openai/gpt-5.5", transport=transport)(request(tools=[TOOL], reasoning_effort="high"))
    assert transport.calls[0]["body"]["reasoning_effort"] == "high"
    assert "reasoning_effort" not in transport.calls[1]["body"]
    assert budget.by_reason == {"reasoning_tools_compat": 1}


@pytest.mark.parametrize("model,error,limit", [("o3", REJECTION, 1), ("gpt-5.5", {"error": "other invalid request"}, 1), ("gpt-5.5", REJECTION, 0)])
def test_compat_never_masks_other_errors_or_exhausted_budget(model, error, limit):
    transport = Transport([(400, error)])
    with attempt_budget_scope(AttemptBudget(limit=limit)), pytest.raises(LLMError):
        OpenAIMessagesBackend(api_key="fixture", model=model, transport=transport)(request(tools=[TOOL], reasoning_effort="high"))
    assert len(transport.calls) == 1


def test_catalog_aliases_and_runtime_budget_preserve_decimal_limits(tmp_path):
    catalog = ModelCatalog(workspace=tmp_path)
    catalog.import_models(provider="fixture", models=[{"id": "tiny", "context_length": 8192}, {"id": "decimal", "context_window": 128000}])
    assert catalog.list("fixture")[0]["context_window"] == 8192
    assert catalog.list("fixture")[1]["context_length"] == 128000
    assert resolve_context_window("fixture", "tiny", 1048576, workspace=tmp_path) == 8192
    assert routes_agent._payload_context_window({"model_context_window": 128000}) == 128000
    assert routes_agent._payload_context_window({"context_window": 1000000}) == 1000000
    cfg = config(tmp_path, [{"provider": "fixture", "model": "tiny"}])
    cfg.data["agent"]["native"]["model_context_window"] = 1048576
    loop = LoopConfig.from_config(cfg, tier="medium")
    assert loop.model_context_window == 8192
    assert loop.requested_model_context_window == 1048576
    assert loop.model_options()["model_context_window"] == 1048576
    assert LLMGateway(cfg).effective_model_metadata("medium")[2].context_window == 8192


def test_known_small_model_never_uses_default_megawindow(tmp_path):
    cfg = config(tmp_path, [{"provider": "openai", "model": "gpt-4o"}])
    loop = LoopConfig.from_config(cfg, tier="medium")
    assert loop.model_context_window == 128000
    assert loop.model_options()["model_context_window"] == 0  # auto, not pinned to the first route
    cfg.data["llm"]["tiers"]["medium"]["routes"][0]["model"] = "unknown-fixture"
    assert LoopConfig.from_config(cfg, tier="medium").model_context_window == 0


def test_response_model_switch_updates_budget_and_preserves_requested_checkpoint():
    usage = LoopUsage(context_window=1048576)
    for model, expected in [("high-model", 131072), ("light-model", 8192), ("high-model", 131072)]:
        usage.record_response(MessagesResponse(content=[], provider="mock", model=model), iteration=1)
        assert usage.context_window == expected
        assert usage.requested_context_window == 1048576
        usage = LoopUsage.from_dict(usage.asdict())
    assert usage.outcome_kwargs()["requested_context_window"] == 1048576
    state = LoopRunState.begin(config=LoopConfig(model_provider="mock", model_id="light-model", model_context_window=1048576),
                               user_message="fixture", original_user_text="fixture", now=1)
    assert state.usage.context_window == 8192
    assert state.usage.requested_context_window == 1048576


def test_automatic_selection_and_smaller_user_budget_survive_model_switch(tmp_path):
    cfg = config(tmp_path, [{"provider": "mock", "model": "light-model"}])
    loop = LoopConfig.from_config(cfg, tier="medium")
    state = LoopRunState.begin(config=loop, user_message="fixture", original_user_text="fixture", now=1)
    assert state.usage.requested_context_window == 0
    state.usage.record_response(MessagesResponse(content=[], provider="mock", model="high-model",
                                               requested_context_window=0, context_window=131072), iteration=1)
    assert state.usage.context_window == 131072
    usage = LoopUsage(context_window=4096, requested_context_window=4096)
    usage.record_response(MessagesResponse(content=[], provider="mock", model="high-model"), iteration=1)
    assert usage.context_window == 4096


def test_selected_route_budget_not_first_route_budget(tmp_path):
    cfg = config(tmp_path, [{"provider": "fixture", "model": "A", "context_window": 1000000},
                            {"provider": "fixture", "model": "B", "context_length": 8192}])
    loop = LoopConfig.from_config(cfg, tier="medium", model_provider="fixture", model_id="B")
    assert loop.model_context_window == 8192
    assert loop.requested_model_context_window == 0


def test_continuation_keeps_requested_cap_on_next_gateway_call():
    cfg = LoopConfig(model_context_window=4096, requested_model_context_window=4096)
    state = LoopRunState.begin(config=cfg, user_message="fixture", original_user_text="fixture", now=1)
    checkpoint = state.to_checkpoint(resumable=True)
    resumed_cfg = LoopConfig(model_context_window=128000, requested_model_context_window=0)
    resumed = LoopRunState.begin(config=resumed_cfg, user_message="continue", original_user_text="fixture",
                                now=2, checkpoint=checkpoint)
    assert resumed.usage.requested_context_window == 4096
    assert resumed_cfg.model_options()["model_context_window"] == 4096


def test_fallback_recalculates_window_and_output_before_transport(tmp_path, monkeypatch):
    cfg = config(tmp_path, [{"provider": "fixture", "model": "large", "context_window": 1000000},
                            {"provider": "fixture", "model": "tiny", "context_length": 4096}])
    gateway = LLMGateway(cfg)
    attempts = []
    def backend(tier, **kwargs):
        route = kwargs["route_cfg"]
        def run(req):
            attempts.append((route["model"], req.max_tokens))
            if route["model"] == "large":
                raise LLMError("fixture unavailable")
            return MessagesResponse(content=[], provider="fixture", model="tiny")
        return run
    monkeypatch.setattr(gateway, "_resolve_messages_backend", backend)
    response = call(gateway, model_context_window=1000000, max_tokens=5000)
    assert attempts[0] == ("large", 5000)
    assert 0 < attempts[1][1] < 4096
    assert response.context_window == 4096
    usage = LoopUsage(context_window=1000000)
    usage.record_response(response, iteration=1)
    assert usage.context_window == 4096
    assert usage.requested_context_window == 1000000
    attempts.clear()
    with pytest.raises(LLMError, match="context_length_exceeded"):
        gateway.call_messages(task="agent.loop", caller="fixture", tier="medium", system="fixture",
                              messages=[{"role": "user", "content": "x" * 20000}])
    assert [model for model, _ in attempts] == ["large"]
