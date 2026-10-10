"""Same-tier priority and safe failover through the actual gateway paths."""
from copy import deepcopy

import pytest

from nerya.core.config import Config, DEFAULT_CONFIG
from nerya.core.errors import LLMError
from nerya.core.paths import WorkspacePaths
from nerya.harness.cancellation import CancelledError
from nerya.llm.gateway import LLMGateway
from nerya.llm.messages import MessagesResponse
from nerya.llm.providers import ProviderResult
from nerya.llm import ops
from nerya.llm.attempt_budget import AttemptBudget, attempt_budget_scope

pytestmark = pytest.mark.smoke


def gateway(tmp_path):
    data = deepcopy(DEFAULT_CONFIG)
    data["runtime"]["mock_mode"] = False
    data["llm"]["tiers"] = {
        "medium": {"allowed_tasks": ["agent.loop", "subagent_analysis"], "routes": [
            {"provider": "openai", "model": "first", "reasoning_effort": "low", "context_window": 32000},
            {"provider": "anthropic", "model": "second", "reasoning_effort": "high", "context_window": 64000},
            {"provider": "openai", "models": ["third", "fourth"], "reasoning_effort": "none", "context_window": 128000},
        ]},
        "high": {"provider": "openai", "model": "never-cross-tiers"},
    }
    return LLMGateway(Config(paths=WorkspacePaths(tmp_path), data=data))


def call(gw, **kwargs):
    return gw.call_messages(task="agent.loop", caller="fixture", tier="medium",
                           system="fixture", messages=[{"role": "user", "content": "hi"}], **kwargs)


@pytest.mark.parametrize("selection,expected", [
    ({"model_provider": "openai", "model_id": "first"}, ["first", "second"]),
    ({"model_provider": "openai", "model_id": "third"}, ["third", "first", "second"]),
    ({"model_id": "third"}, ["third", "first", "second"]),
])
def test_selected_model_fails_over_with_each_routes_own_policy(tmp_path, monkeypatch, selection, expected):
    gw = gateway(tmp_path)
    original = deepcopy(gw.config.get("llm.tiers"))
    attempts = []
    def backend(tier, **kwargs):
        route = kwargs["route_cfg"]
        def run(req):
            attempts.append((route["model"], req.reasoning_effort, route["context_window"]))
            if route["model"] != "second":
                raise LLMError("fixture model unavailable")
            return MessagesResponse(content=[{"type": "text", "text": "ok"}], provider=route["provider"], model=route["model"])
        return run
    monkeypatch.setattr(gw, "_resolve_messages_backend", backend)
    assert call(gw, **selection).model == "second"
    assert [row[0] for row in attempts] == expected
    assert attempts[-1] == ("second", "high", 64000)
    assert gw.config.get("llm.tiers") == original


def test_prompt_path_keeps_same_tier_fallback_for_selected_model(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "fixture-key")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "fixture-key")
    gw = gateway(tmp_path)
    attempts = []
    def adapter(**kwargs):
        attempts.append(kwargs["model"])
        if kwargs["model"] == "first":
            raise LLMError("fixture rejected")
        return ProviderResult(text="ok", provider=kwargs["provider_name"], model=kwargs["model"])
    gw.router.register_provider("openai", adapter)
    gw.router.register_provider("anthropic", adapter)
    result = gw.call(task="subagent_analysis", caller="fixture", tier="medium", prompt="hello",
                     model_provider="openai", model_id="first")
    assert result.model == "second"
    assert attempts == ["first", "second"]


@pytest.mark.parametrize("committed", [False, True])
def test_stream_failure_only_switches_before_output(tmp_path, monkeypatch, committed):
    gw = gateway(tmp_path)
    attempts = []
    error = LLMError("stream unavailable")
    error.stream_interrupted = True
    error.stream_output_committed = committed
    def backend(tier, **kwargs):
        route = kwargs["route_cfg"]
        def run(req):
            attempts.append(route["model"])
            if route["model"] == "first":
                raise error
            return MessagesResponse(content=[], model=route["model"])
        return run
    monkeypatch.setattr(gw, "_resolve_messages_backend", backend)
    if committed:
        with pytest.raises(LLMError) as raised:
            call(gw, stream=True)
        assert raised.value is error
        assert attempts == ["first"]
    else:
        assert call(gw, stream=True).model == "second"
        assert attempts == ["first", "second"]


@pytest.mark.parametrize("error", [LLMError("all routes failed"), CancelledError("cancelled")])
def test_exhaustion_and_cancellation_never_cross_tiers(tmp_path, monkeypatch, error):
    gw = gateway(tmp_path)
    attempts = []
    def backend(tier, **kwargs):
        def run(req):
            attempts.append(kwargs["route_cfg"]["model"])
            raise error
        return run
    monkeypatch.setattr(gw, "_resolve_messages_backend", backend)
    with pytest.raises(type(error)):
        call(gw)
    assert attempts == (["first"] if isinstance(error, CancelledError) else ["first", "second", "third", "fourth"])


def test_saved_priority_roundtrips_and_drives_gateway(tmp_path):
    gw = gateway(tmp_path)
    routes = deepcopy(gw.config.get("llm.tiers.medium.routes"))
    routes = [routes[2], routes[0], routes[1]]
    saved = ops.llm_config_set(gw.config, default_tier="medium", tiers=[{"tier": "medium", "routes": routes}])
    assert saved["ok"]
    row = next(row for row in saved["tiers"] if row["tier"] == "medium")
    assert [route["provider"] for route in row["routes"]] == ["openai", "openai", "anthropic"]
    assert [route["model"] for route in LLMGateway(gw.config)._messages_route_cfgs("medium")] == ["third", "fourth", "first", "second"]


def test_preference_does_not_leak_policy_into_other_routes(tmp_path):
    gw = gateway(tmp_path)
    routes = gw.config.get("llm.tiers.medium.routes")
    routes[1].pop("reasoning_effort")
    routes[1].pop("context_window")
    expanded = gw._messages_route_cfgs("medium", provider_override="openai", model_override="third")
    second = next(route for route in expanded if route["model"] == "second")
    assert "reasoning_effort" not in second
    assert "context_window" not in second


@pytest.mark.parametrize("selection", [{"model_provider": "openai", "model_id": "custom"}, {"model_id": "custom"}])
def test_unlisted_custom_model_stays_pinned(tmp_path, selection):
    gw = gateway(tmp_path)
    candidates = gw._messages_route_cfgs("medium", provider_override=selection.get("model_provider"), model_override=selection["model_id"])
    assert [(route["provider"], route["model"]) for route in candidates] == [("openai", "custom")]


@pytest.mark.parametrize("api", ["messages", "prompt"])
def test_failover_consumes_the_shared_extra_attempt_budget(tmp_path, monkeypatch, api):
    monkeypatch.setenv("OPENAI_API_KEY", "fixture-key")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "fixture-key")
    gw = gateway(tmp_path)
    attempts = []
    def run(model):
        attempts.append(model)
        raise LLMError("fixture rejected")
    def backend(tier, **kwargs):
        return lambda req: run(kwargs["route_cfg"]["model"])
    def adapter(**kwargs):
        return run(kwargs["model"])
    monkeypatch.setattr(gw, "_resolve_messages_backend", backend)
    gw.router.register_provider("openai", adapter)
    gw.router.register_provider("anthropic", adapter)
    budget = AttemptBudget(limit=1)
    with attempt_budget_scope(budget), pytest.raises(LLMError):
        if api == "messages":
            call(gw)
        else:
            gw.call(task="subagent_analysis", caller="fixture", tier="medium", prompt="hello")
    assert attempts == ["first", "second"]
    assert budget.by_reason == {"model_failover": 1}
    assert budget.denied == 1


def test_prompt_cancellation_is_preserved_without_fallback(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "fixture-key")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "fixture-key")
    gw = gateway(tmp_path)
    attempts = []
    def adapter(**kwargs):
        attempts.append(kwargs["model"])
        raise CancelledError("fixture cancelled")
    gw.router.register_provider("openai", adapter)
    gw.router.register_provider("anthropic", adapter)
    with pytest.raises(CancelledError):
        gw.call(task="subagent_analysis", caller="fixture", tier="medium", prompt="hello")
    assert attempts == ["first"]
