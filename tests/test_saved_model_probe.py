"""Connection tests must use the saved model's normal messages contract."""
from copy import deepcopy
from types import SimpleNamespace

import pytest

from nerya.api import routes_llm
from nerya.core.config import Config, DEFAULT_CONFIG
from nerya.core.paths import WorkspacePaths
from nerya.llm import ops

pytestmark = pytest.mark.smoke


@pytest.fixture
def config(tmp_path, monkeypatch):
    monkeypatch.setenv("PROBE_FIXTURE_KEY", "fixture-secret")
    data = deepcopy(DEFAULT_CONFIG)
    data["runtime"]["mock_mode"] = False
    data["llm"]["tiers"] = {"medium": {"max_tokens": 8192, "allowed_tasks": ["agent.loop"], "allowed_classes": ["agent_loop"], "routes": [
        {"provider": "fixture", "model": "reasoning-custom", "kind": "chat_completions", "base_url": "https://fixture.invalid/v1", "provider_key_env": "PROBE_FIXTURE_KEY", "reasoning_effort": "high"},
    ]}}
    return Config(paths=WorkspacePaths(tmp_path), data=data)


def test_probe_uses_saved_reasoning_and_sufficient_output_budget(config, monkeypatch):
    calls = []
    efforts = []
    from nerya.llm.messages import OpenAIMessagesBackend
    original_call = OpenAIMessagesBackend.__call__
    def backend(self, request):
        efforts.append(request.reasoning_effort)
        return original_call(self, request)
    monkeypatch.setattr(OpenAIMessagesBackend, "__call__", backend)
    def post(self, url, *, headers, body, timeout):
        calls.append(body)
        return 200, {"choices": [{"message": {"content": "READY"}, "finish_reason": "stop"}]}, {}
    monkeypatch.setattr("nerya.llm.messages.UrllibTransport.post_json_with_headers", post)
    before = deepcopy(config.data)
    result = routes_llm._messages_probe(SimpleNamespace(config=config), {"revision": ops.config_revision(config)})
    assert result["ok"], result
    assert len(calls) == 1
    assert efforts == ["high"]
    assert calls[0]["max_tokens"] == 8192
    assert result["text_preview"] == "READY"
    assert config.data == before


@pytest.mark.parametrize("status,kind", [(400, "invalid_request"), (401, "authentication"), (429, "rate_limit"), (503, "provider_unavailable")])
def test_probe_returns_actionable_safe_provider_diagnostics(config, monkeypatch, status, kind):
    def post(self, url, *, headers, body, timeout):
        return status, {"error": {"message": "fixture-secret must not escape"}}, {"x-request-id": "fixture-request"}
    monkeypatch.setattr("nerya.llm.messages.UrllibTransport.post_json_with_headers", post)
    config.data["llm"]["tiers"]["medium"]["http_max_attempts"] = 1
    result = routes_llm._messages_probe(SimpleNamespace(config=config), {})
    assert not result["ok"]
    assert result["failure_kind"] == kind
    assert result["status_code"] == status
    assert result["request_id"] == "fixture-request"
    assert "fixture-secret" not in str(result)


@pytest.mark.parametrize("stop,kind", [("length", "output_truncated"), ("stop", "empty_response")])
def test_probe_does_not_claim_empty_model_responses_are_success(config, monkeypatch, stop, kind):
    def post(self, url, *, headers, body, timeout):
        return 200, {"choices": [{"message": {"content": ""}, "finish_reason": stop}]}, {}
    monkeypatch.setattr("nerya.llm.messages.UrllibTransport.post_json_with_headers", post)
    result = routes_llm._messages_probe(SimpleNamespace(config=config), {})
    assert not result["ok"]
    assert result["failure_kind"] == kind
    assert result["connection_ok"]


def test_probe_does_not_force_temperature_for_reasoning_models(config, monkeypatch):
    captured = []
    class Gateway:
        def __init__(self, received):
            assert received.data == config.data
        def call_messages(self, **kwargs):
            captured.append(kwargs)
            return SimpleNamespace(provider="fixture", model="custom", stop_reason="end_turn", latency_ms=1, text=lambda: "READY", tool_uses=lambda: [])
    monkeypatch.setattr(routes_llm, "LLMGateway", Gateway)
    config.data["llm"]["tiers"]["medium"]["temperature"] = 0.8
    result = routes_llm._messages_probe(SimpleNamespace(config=config), {})
    assert result["ok"]
    assert captured[0]["reasoning_effort"] is None
    assert captured[0]["temperature"] == 0.8
