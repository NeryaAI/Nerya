from __future__ import annotations

from copy import deepcopy

import pytest

from nerya.core.config import Config, DEFAULT_CONFIG
from nerya.core.errors import LLMError
from nerya.core.paths import WorkspacePaths
from nerya.llm.gateway import LLMGateway
from nerya.llm.messages import (
    AnthropicMessagesBackend,
    GeminiMessagesBackend,
    MessagesRequest,
    MessagesResponse,
    MockMessagesBackend,
    OpenAIMessagesBackend,
)

pytestmark = pytest.mark.smoke


def _config(tmp_path, tiers: dict) -> Config:
    data = deepcopy(DEFAULT_CONFIG)
    data["llm"]["tiers"] = tiers
    return Config(paths=WorkspacePaths(tmp_path), data=data)


class _CapturingTransport:
    def __init__(self, response: dict):
        self.response = response
        self.calls: list[dict] = []

    def post_json_with_headers(self, url, *, headers, body, timeout):  # noqa: ANN001
        self.calls.append({
            "url": url,
            "headers": headers,
            "body": body,
            "timeout": timeout,
        })
        return 200, self.response, {}

    def post_json(self, url, *, headers, body, timeout):  # noqa: ANN001
        self.calls.append({
            "url": url,
            "headers": headers,
            "body": body,
            "timeout": timeout,
        })
        return 200, self.response


def test_messages_call_uses_frontend_model_override_without_mutating_tier(tmp_path):
    cfg = _config(
        tmp_path,
        {
            "medium": {
                "provider": "mock",
                "model": "medium-model",
                "allowed_tasks": ["agent.loop"],
            },
        },
    )
    gateway = LLMGateway(cfg)

    response = gateway.call_messages(
        task="agent.loop",
        caller="test",
        system="You are testing.",
        messages=[{"role": "user", "content": "hello"}],
        tier="medium",
        model_provider="mock",
        model_id="front-end-picked-model",
    )

    assert response.provider == "mock"
    assert response.model == "front-end-picked-model"
    assert cfg.get("llm.tiers")["medium"]["model"] == "medium-model"


def test_mock_messages_call_tool_marker_accepts_documented_spacing():
    backend = MockMessagesBackend()
    response = backend(
        MessagesRequest(
            system="test",
            messages=[
                {
                    "role": "user",
                    "content": '[[call_tool: strategy_generate_proposal args={"strategy_id":"demo"}]]',
                }
            ],
            tools=[{"name": "strategy_generate_proposal"}],
        )
    )

    calls = response.tool_uses()
    assert response.stop_reason == "tool_use"
    assert calls[0]["name"] == "strategy_generate_proposal"
    assert calls[0]["input"] == {"strategy_id": "demo"}


def test_openai_messages_can_enable_provider_native_web_search():
    transport = _CapturingTransport({
        "choices": [
            {
                "finish_reason": "stop",
                "message": {"content": "fresh answer"},
            }
        ],
        "usage": {"prompt_tokens": 10, "completion_tokens": 3},
    })
    backend = OpenAIMessagesBackend(
        api_key="sk-test",
        model="gpt-4o-search-preview",
        transport=transport,
    )

    response = backend(
        MessagesRequest(
            system="test",
            messages=[{"role": "user", "content": "latest market news"}],
            metadata={
                "provider_native_web_search": {
                    "enabled": True,
                    "search_context_size": "high",
                    "user_location": {
                        "type": "approximate",
                        "country": "US",
                    },
                }
            },
        )
    )

    assert response.text() == "fresh answer"
    body = transport.calls[0]["body"]
    assert body["web_search_options"] == {
        "search_context_size": "high",
        "user_location": {"type": "approximate", "country": "US"},
    }


def test_anthropic_messages_can_enable_provider_native_web_search():
    transport = _CapturingTransport({
        "content": [{"type": "text", "text": "fresh answer"}],
        "stop_reason": "end_turn",
        "usage": {"input_tokens": 12, "output_tokens": 4},
    })
    backend = AnthropicMessagesBackend(
        api_key="sk-ant-test",
        model="claude-sonnet-4-5",
        base_url="https://api.anthropic.com",
        transport=transport,
    )

    response = backend(
        MessagesRequest(
            system="test",
            messages=[{"role": "user", "content": "latest market news"}],
            tools=[
                {
                    "name": "web_search",
                    "description": "local search",
                    "input_schema": {"type": "object"},
                },
                {
                    "name": "web_fetch",
                    "description": "local fetch",
                    "input_schema": {"type": "object"},
                },
            ],
            metadata={
                "provider_native_web_search": {
                    "enabled": True,
                    "max_uses": 3,
                    "allowed_domains": ["example.com"],
                }
            },
        )
    )

    assert response.text() == "fresh answer"
    assert transport.calls[0]["url"] == "https://api.anthropic.com/v1/messages"
    tools = transport.calls[0]["body"]["tools"]
    assert tools[0] == {
        "type": "web_search_20250305",
        "name": "web_search",
        "max_uses": 3,
        "allowed_domains": ["example.com"],
    }
    assert [tool["name"] for tool in tools] == ["web_search", "web_fetch"]


def test_gemini_messages_can_enable_provider_native_web_search():
    transport = _CapturingTransport({
        "candidates": [
            {
                "finishReason": "STOP",
                "content": {
                    "parts": [{"text": "fresh answer"}],
                },
                "groundingMetadata": {
                    "searchEntryPoint": {"renderedContent": "<div>search</div>"},
                },
            }
        ],
        "usageMetadata": {"promptTokenCount": 8, "candidatesTokenCount": 3},
    })
    backend = GeminiMessagesBackend(
        api_key="gemini-test",
        model="gemini-2.5-flash",
        transport=transport,
    )

    response = backend(
        MessagesRequest(
            system="test",
            messages=[{"role": "user", "content": "latest market news"}],
            tools=[
                {
                    "name": "web_search",
                    "description": "local search",
                    "input_schema": {"type": "object"},
                }
            ],
            metadata={"provider_native_web_search": True},
        )
    )

    assert response.text() == "fresh answer"
    body = transport.calls[0]["body"]
    assert body["tools"][0] == {"google_search": {}}
    assert body["tools"][1]["functionDeclarations"][0]["name"] == "web_search"


def test_gemini_provider_native_web_search_can_use_legacy_tool_type():
    transport = _CapturingTransport({
        "candidates": [
            {
                "finishReason": "STOP",
                "content": {"parts": [{"text": "legacy search"}]},
            }
        ],
    })
    backend = GeminiMessagesBackend(
        api_key="gemini-test",
        model="gemini-1.5-pro",
        transport=transport,
    )

    backend(
        MessagesRequest(
            system="test",
            messages=[{"role": "user", "content": "news"}],
            metadata={
                "provider_native_web_search": {
                    "enabled": True,
                    "gemini_tool_type": "google_search_retrieval",
                }
            },
        )
    )

    assert transport.calls[0]["body"]["tools"] == [
        {"google_search_retrieval": {}}
    ]


def test_gateway_passes_tier_provider_native_web_search_config(tmp_path):
    cfg = _config(
        tmp_path,
        {
            "medium": {
                "provider": "mock",
                "model": "medium-model",
                "allowed_tasks": ["agent.loop"],
                "provider_native_web_search": {
                    "enabled": True,
                    "search_context_size": "medium",
                },
            },
        },
    )
    gateway = LLMGateway(cfg)
    captured: dict[str, dict] = {}

    def backend(request: MessagesRequest) -> MessagesResponse:
        captured["metadata"] = request.metadata
        return MessagesResponse(
            content=[{"type": "text", "text": "ok"}],
            provider="mock",
            model="medium-model",
        )

    gateway._resolve_messages_backend = lambda *_, **__: backend  # type: ignore[method-assign]

    response = gateway.call_messages(
        task="agent.loop",
        caller="test",
        system="test",
        messages=[{"role": "user", "content": "hello"}],
        tier="medium",
    )

    assert response.text() == "ok"
    assert captured["metadata"]["provider_native_web_search"] == {
        "enabled": True,
        "search_context_size": "medium",
    }


def test_provider_override_reuses_matching_provider_credentials(tmp_path):
    cfg = _config(
        tmp_path,
        {
            "medium": {
                "provider": "mock",
                "model": "medium-model",
            },
            "alt": {
                "provider": "openrouter",
                "model": "default-openrouter-model",
                "provider_key_env": "OPENROUTER_API_KEY",
                "base_url": "https://openrouter.ai/api/v1",
            },
        },
    )

    effective = LLMGateway(cfg)._effective_tier_cfg(
        "medium",
        provider_override="openrouter",
        model_override="operator-selected-model",
    )

    assert effective["provider"] == "openrouter"
    assert effective["model"] == "operator-selected-model"
    assert effective["provider_key_env"] == "OPENROUTER_API_KEY"
    assert effective["base_url"] == "https://openrouter.ai/api/v1"


def test_tier_can_disable_provider_profile_native_web_search(tmp_path):
    cfg = _config(
        tmp_path,
        {
            "medium": {
                "provider": "openrouter",
                "model": "medium-model",
                "provider_native_web_search": {"enabled": False},
            },
        },
    )
    cfg.data["llm"]["providers"] = {
        "openrouter": {
            "base_url": "https://openrouter.ai/api/v1",
            "provider_native_web_search": {
                "enabled": True,
                "search_context_size": "low",
            },
        },
    }

    effective = LLMGateway(cfg)._effective_tier_cfg("medium")

    assert effective["provider_native_web_search"] == {"enabled": False}


def test_custom_openai_compatible_profile_uses_messages_backend(
    tmp_path,
    monkeypatch,
):
    cfg = _config(
        tmp_path,
        {
            "medium": {
                "provider": "cliproxy",
                "model": "gpt-5.4",
                "allowed_tasks": ["agent.loop"],
            },
        },
    )
    cfg.data["llm"]["providers"] = {
        "cliproxy": {
            "base_url": "http://127.0.0.1:8317/v1",
            "provider_key_env": "CLIPROXY_API_KEY",
        },
    }
    monkeypatch.setenv("CLIPROXY_API_KEY", "sk-test")

    backend = LLMGateway(cfg)._resolve_messages_backend("medium")

    assert isinstance(backend, OpenAIMessagesBackend)
    assert backend.provider_name == "cliproxy"
    assert backend.model == "gpt-5.4"
    assert backend.base_url == "http://127.0.0.1:8317/v1"


def test_real_messages_provider_missing_key_fails_loud(
    tmp_path,
    monkeypatch,
):
    cfg = _config(
        tmp_path,
        {
            "medium": {
                "provider": "cliproxy",
                "model": "gpt-5.4",
                "allowed_tasks": ["agent.loop"],
            },
        },
    )
    cfg.data["llm"]["providers"] = {
        "cliproxy": {
            "base_url": "http://127.0.0.1:8317/v1",
        },
    }
    monkeypatch.delenv("NERYA_ALLOW_MOCK_DATA", raising=False)
    monkeypatch.delenv("NERYA_MOCK_MODE", raising=False)

    with pytest.raises(LLMError, match="missing_api_key"):
        LLMGateway(cfg)._resolve_messages_backend("medium")
