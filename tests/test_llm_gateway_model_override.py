from __future__ import annotations

import json
import time
from copy import deepcopy

import pytest

from nerya.core import jsonl
from nerya.core.config import Config, DEFAULT_CONFIG
from nerya.core.errors import LLMError
from nerya.core.paths import WorkspacePaths
from nerya.llm import gateway as gateway_mod
from nerya.llm import model_router as model_router_mod
from nerya.llm.adapters.openai import OpenAIAdapter, OpenAICompatAdapter
from nerya.llm.gateway import LLMGateway
from nerya.llm.messages import (
    AnthropicMessagesBackend,
    GeminiMessagesBackend,
    MessagesRequest,
    MessagesResponse,
    MockMessagesBackend,
    OpenAIMessagesBackend,
)
from nerya.llm.model_router import ModelRouter
from nerya.llm.providers import ProviderResult

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


class _StatusTransport(_CapturingTransport):
    def __init__(self, status: int, response: dict):
        super().__init__(response)
        self.status = status

    def post_json_with_headers(self, url, *, headers, body, timeout):  # noqa: ANN001
        self.calls.append({
            "url": url,
            "headers": headers,
            "body": body,
            "timeout": timeout,
        })
        return self.status, self.response, {}


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


def test_minimax_cn_catalog_routes_messages_to_openai_compat(monkeypatch, tmp_path):
    monkeypatch.setenv("MINIMAX_CN_API_KEY", "sk-test")
    cfg = _config(
        tmp_path,
        {
            "medium": {
                "provider": "minimax-cn",
                "model": "MiniMax-M3",
                "provider_key_env": "MINIMAX_CN_API_KEY",
                "allowed_tasks": ["agent.loop"],
            },
        },
    )

    backend = LLMGateway(cfg)._resolve_messages_backend("medium")

    assert isinstance(backend, OpenAIMessagesBackend)
    assert backend.base_url == "https://api.minimaxi.com/v1"


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


def test_minimax_openai_compat_disables_adaptive_thinking_by_default():
    transport = _CapturingTransport({
        "choices": [
            {
                "finish_reason": "stop",
                "message": {"content": "ok"},
            }
        ],
        "usage": {"prompt_tokens": 10, "completion_tokens": 2},
    })
    backend = OpenAIMessagesBackend(
        api_key="sk-test",
        model="MiniMax-M3",
        base_url="https://api.minimaxi.com/v1",
        provider_name="minimax-cn",
        transport=transport,
    )

    response = backend(MessagesRequest(system="test", messages=[{"role": "user", "content": "hi"}]))

    assert response.text() == "ok"
    body = transport.calls[0]["body"]
    assert body["max_completion_tokens"] == 4096
    assert "max_tokens" not in body
    assert body["thinking"] == {"type": "disabled"}
    assert "reasoning_split" not in body


def test_minimax_direct_adapter_uses_chat_completions_default():
    transport = _CapturingTransport({
        "choices": [
            {
                "finish_reason": "stop",
                "message": {"content": "ok"},
            }
        ],
        "usage": {"prompt_tokens": 10, "completion_tokens": 2},
    })
    adapter = OpenAICompatAdapter(transport=transport)

    response = adapter(
        tier="medium",
        task="subagent_analysis",
        model="MiniMax-M3",
        prompt="hi",
        schema=None,
        api_key="sk-test",
        base_url="",
        provider_name="minimax-cn",
    )

    assert response.text == "ok"
    assert transport.calls[0]["url"] == "https://api.minimaxi.com/v1/chat/completions"
    body = transport.calls[0]["body"]
    assert body["max_completion_tokens"] == 1024
    assert body["thinking"] == {"type": "disabled"}
    assert "max_tokens" not in body


def test_openai_messages_accepts_string_message_content():
    transport = _CapturingTransport({
        "choices": [
            {
                "finish_reason": "stop",
                "message": "plain text from compat provider",
            }
        ],
        "usage": {"prompt_tokens": 10, "completion_tokens": 4},
    })
    backend = OpenAIMessagesBackend(
        api_key="sk-test",
        model="compat-model",
        base_url="https://compat.example/v1",
        provider_name="compat",
        transport=transport,
    )

    response = backend(
        MessagesRequest(system="test", messages=[{"role": "user", "content": "hi"}])
    )

    assert response.text() == "plain text from compat provider"


def test_minimax_openai_compat_surfaces_string_error_without_crashing():
    transport = _StatusTransport(
        404,
        {"raw": "404 page not found", "error": "HTTP Error 404: 404 Page not found"},
    )
    backend = OpenAIMessagesBackend(
        api_key="sk-test",
        model="MiniMax-M3",
        base_url="https://api.minimaxi.com/v1",
        provider_name="minimax-cn",
        transport=transport,
    )

    with pytest.raises(LLMError, match="Page not found"):
        backend(MessagesRequest(system="test", messages=[{"role": "user", "content": "hi"}]))


def test_minimax_openai_compat_enables_split_reasoning_when_requested():
    transport = _CapturingTransport({
        "choices": [
            {
                "finish_reason": "stop",
                "message": {
                    "reasoning_details": [{"text": "short trace"}],
                    "content": "ok",
                },
            }
        ],
        "usage": {"prompt_tokens": 10, "completion_tokens": 2},
    })
    backend = OpenAIMessagesBackend(
        api_key="sk-test",
        model="MiniMax-M3",
        base_url="https://api.minimaxi.com/v1",
        provider_name="minimax-cn",
        transport=transport,
    )

    response = backend(
        MessagesRequest(
            system="test",
            messages=[{"role": "user", "content": "hi"}],
            reasoning_effort="adaptive",
        )
    )

    body = transport.calls[0]["body"]
    assert body["thinking"] == {"type": "adaptive"}
    assert body["reasoning_split"] is True
    assert response.content[0] == {"type": "thinking", "thinking": "short trace"}
    assert response.text() == "ok"


def test_minimax_openai_compat_does_not_map_generic_effort_to_thinking():
    transport = _CapturingTransport({
        "choices": [
            {
                "finish_reason": "stop",
                "message": {"content": "ok"},
            }
        ],
        "usage": {"prompt_tokens": 10, "completion_tokens": 2},
    })
    backend = OpenAIMessagesBackend(
        api_key="sk-test",
        model="MiniMax-M3",
        base_url="https://api.minimaxi.com/v1",
        provider_name="minimax-cn",
        transport=transport,
    )

    backend(
        MessagesRequest(
            system="test",
            messages=[{"role": "user", "content": "hi"}],
            reasoning_effort="medium",
        )
    )

    body = transport.calls[0]["body"]
    assert body["thinking"] == {"type": "disabled"}
    assert "reasoning_split" not in body


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


def test_context_full_logging_is_disabled_by_default(tmp_path):
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

    gateway.call_messages(
        task="agent.loop",
        caller="test",
        system="system",
        messages=[{"role": "user", "content": "hello"}],
        tier="medium",
    )

    assert not cfg.paths.dev_log("llm_context_full").exists()


def test_context_full_logging_records_complete_messages_context_and_redacts(tmp_path):
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
    cfg.data["llm"]["context_log_mode"] = "full"
    gateway = LLMGateway(cfg)
    vendor_prefixed_key = "tp-" + ("a1" * 24)
    dotted_provider_key = ("deadbeef" * 4) + ".syntheticSecretTail"

    response = gateway.call_messages(
        task="agent.loop",
        caller="test",
        system=(
            "system with sk-123456789012345678901234 and "
            f"{vendor_prefixed_key} plus {dotted_provider_key}"
        ),
        messages=[
            {
                "role": "user",
                "content": (
                    "preserve this complete user context\nsecond line\n"
                    f"vendor tokens: {vendor_prefixed_key} {dotted_provider_key}"
                ),
            },
            {
                "role": "assistant",
                "content": [
                    {
                        "type": "tool_use",
                        "id": "toolu_1",
                        "name": "read_file",
                        "input": {"api_key": "sk-123456789012345678901234"},
                    }
                ],
            },
        ],
        tools=[
            {
                "name": "read_file",
                "description": "Read a file",
                "input_schema": {
                    "type": "object",
                    "properties": {"path": {"type": "string"}},
                },
            }
        ],
        tool_choice={"type": "auto"},
        tier="medium",
        max_tokens=123,
        temperature=0.4,
        reasoning_effort="low",
        reasoning_summary="concise",
        model_provider="mock",
        model_id="front-end-model",
        metadata={
            "session_id": "sess_1",
            "turn_id": "turn_1",
            "iteration": 2,
            "context_scope": "agent_loop",
        },
    )

    assert response.text()
    records = jsonl.read_all(cfg.paths.dev_log("llm_context_full"))
    request = next(r for r in records if r["phase"] == "request")
    response_record = next(r for r in records if r["phase"] == "response")

    assert request["kind"] == "llm.context_full"
    assert request["api"] == "messages"
    assert request["task"] == "agent.loop"
    assert request["caller"] == "test"
    assert request["tier"] == "medium"
    assert request["provider"] == "mock"
    assert request["model"] == "front-end-model"
    assert request["ts"]
    assert (
        request["request"]["system"]
        == "system with ***REDACTED*** and ***REDACTED*** plus ***REDACTED***"
    )
    assert (
        request["request"]["messages"][0]["content"]
        == (
            "preserve this complete user context\nsecond line\n"
            "vendor tokens: ***REDACTED*** ***REDACTED***"
        )
    )
    assert request["request"]["messages"][1]["content"][0]["input"]["api_key"][
        "__redacted__"
    ]
    assert request["request"]["tools"][0]["name"] == "read_file"
    assert request["request"]["tool_choice"] == {"type": "auto"}
    assert request["request"]["max_tokens"] == 123
    assert request["request"]["temperature"] == 0.4
    assert request["request"]["reasoning_effort"] == "low"
    assert request["request"]["reasoning_summary"] == "concise"
    assert request["request"]["metadata"]["session_id"] == "sess_1"
    assert request["request"]["metadata"]["turn_id"] == "turn_1"
    assert request["request"]["metadata"]["iteration"] == 2
    assert "provider_native_web_search" in request["request"]["metadata"]
    for record in (request, response_record):
        assert record["session_id"] == "sess_1"
        assert record["turn_id"] == "turn_1"
        assert record["iteration"] == 2
        assert record["context_scope"] == "agent_loop"

    serialized_records = json.dumps(records, ensure_ascii=False)
    assert vendor_prefixed_key not in serialized_records
    assert dotted_provider_key not in serialized_records
    assert "tp-" not in serialized_records
    assert "deadbeefdeadbeefdeadbeefdeadbeef" not in serialized_records

    assert response_record["call_id"] == request["call_id"]
    assert response_record["ts"]
    assert response_record["response"]["provider"] == "mock"
    assert response_record["response"]["model"] == "front-end-model"
    assert response_record["response"]["content"]


def test_context_full_logging_records_provider_wire_messages_payload(
    monkeypatch,
    tmp_path,
) -> None:
    provider_key = "sk-wire-secret-12345678901234567890"
    monkeypatch.setenv("MINIMAX_CN_API_KEY", provider_key)
    transport = _CapturingTransport({
        "choices": [
            {
                "finish_reason": "stop",
                "message": {"content": "wire ok"},
            }
        ],
        "usage": {"prompt_tokens": 11, "completion_tokens": 4},
    })
    real_backend_cls = OpenAIMessagesBackend
    monkeypatch.setattr(
        gateway_mod,
        "OpenAIMessagesBackend",
        lambda **kwargs: real_backend_cls(transport=transport, **kwargs),
    )
    cfg = _config(
        tmp_path,
        {
            "medium": {
                "provider": "minimax-cn",
                "model": "MiniMax-M3",
                "base_url": "https://api.minimaxi.com/v1",
                "provider_key_env": "MINIMAX_CN_API_KEY",
                "allowed_tasks": ["agent.loop"],
            },
        },
    )
    cfg.data["llm"]["context_log_mode"] = "full"

    response = LLMGateway(cfg).call_messages(
        task="agent.loop",
        caller="test",
        system="system prompt",
        messages=[{"role": "user", "content": "hello"}],
        tools=[
            {
                "name": "read_file",
                "description": "Read a file",
                "input_schema": {
                    "type": "object",
                    "properties": {"path": {"type": "string"}},
                },
            }
        ],
        tool_choice={"type": "auto"},
        tier="medium",
        max_tokens=77,
        metadata={
            "session_id": "sess-wire",
            "turn_id": "turn-wire",
            "iteration": 3,
            "parent_call_id": "toolu-parent",
            "context_scope": "agent_loop",
        },
    )

    assert response.text() == "wire ok"
    records = jsonl.read_all(cfg.paths.dev_log("llm_context_full"))
    canonical = next(r for r in records if r["phase"] == "request")
    wire_request = next(r for r in records if r["phase"] == "wire_request")
    wire_response = next(r for r in records if r["phase"] == "wire_response")

    assert wire_request["call_id"] == canonical["call_id"]
    assert wire_response["call_id"] == canonical["call_id"]
    assert wire_request["api"] == "messages"
    assert wire_request["provider"] == "minimax-cn"
    assert wire_request["model"] == "MiniMax-M3"
    assert wire_request["session_id"] == "sess-wire"
    assert wire_request["turn_id"] == "turn-wire"
    assert wire_request["iteration"] == 3
    assert wire_request["parent_call_id"] == "toolu-parent"
    assert wire_request["wire_attempt"] == 1
    assert wire_request["request"]["method"] == "POST"
    assert wire_request["request"]["url"] == (
        "https://api.minimaxi.com/v1/chat/completions"
    )
    assert wire_request["request"]["headers"]["Authorization"]["__redacted__"]
    body = wire_request["request"]["body"]
    assert body["model"] == "MiniMax-M3"
    assert body["max_completion_tokens"] == 77
    assert body["thinking"] == {"type": "disabled"}
    assert body["messages"] == [
        {"role": "system", "content": "system prompt"},
        {"role": "user", "content": "hello"},
    ]
    assert body["tools"][0]["type"] == "function"
    assert body["tools"][0]["function"]["name"] == "read_file"
    assert body["tool_choice"] == "auto"
    assert wire_response["response"]["status"] == 200
    assert wire_response["response"]["body"]["choices"][0]["message"]["content"] == "wire ok"
    assert provider_key not in json.dumps(records, ensure_ascii=False)


def test_context_full_logging_records_provider_wire_prompt_payload(
    monkeypatch,
    tmp_path,
) -> None:
    provider_key = "sk-wire-prompt-secret-12345678901234567890"
    monkeypatch.setenv("OPENAI_API_KEY", provider_key)
    transport = _CapturingTransport({
        "choices": [
            {
                "finish_reason": "stop",
                "message": {"content": "legacy ok"},
            }
        ],
        "usage": {"prompt_tokens": 7, "completion_tokens": 2, "total_tokens": 9},
    })
    monkeypatch.setattr(
        model_router_mod,
        "builtin_providers",
        lambda _transport=None: {"openai": OpenAIAdapter(transport=transport)},
    )
    cfg = _config(
        tmp_path,
        {
            "light": {
                "provider": "openai",
                "model": "gpt-4o-mini",
                "base_url": "https://api.openai.com/v1",
                "provider_key_env": "OPENAI_API_KEY",
                "allowed_tasks": ["compress"],
            },
        },
    )
    cfg.data["llm"]["context_log_mode"] = "full"

    result = LLMGateway(cfg).call(
        task="compress",
        caller="test",
        tier="light",
        prompt="summarize this transcript",
        metadata={
            "session_id": "sess-prompt-wire",
            "turn_id": "turn-prompt-wire",
            "context_scope": "subagent",
            "subagent": "analyst",
            "parent_call_id": "toolu-subagent",
        },
    )

    assert result.raw == "legacy ok"
    records = jsonl.read_all(cfg.paths.dev_log("llm_context_full"))
    canonical = next(r for r in records if r["phase"] == "request")
    wire_request = next(r for r in records if r["phase"] == "wire_request")
    assert wire_request["call_id"] == canonical["call_id"]
    assert wire_request["api"] == "prompt"
    assert wire_request["session_id"] == "sess-prompt-wire"
    assert wire_request["subagent"] == "analyst"
    assert wire_request["parent_call_id"] == "toolu-subagent"
    assert wire_request["request"]["headers"]["Authorization"]["__redacted__"]
    body = wire_request["request"]["body"]
    assert body["model"] == "gpt-4o-mini"
    assert body["messages"][1] == {
        "role": "user",
        "content": "summarize this transcript",
    }
    assert provider_key not in json.dumps(records, ensure_ascii=False)


def test_context_full_wire_url_redacts_query_secrets() -> None:
    safe = LLMGateway._safe_context_wire_url(  # noqa: SLF001
        "https://generativelanguage.googleapis.com/v1beta/models/gemini:generateContent"
        "?key=secret-query-key&alt=json&access_token=secret-token"
    )

    assert "secret-query-key" not in safe
    assert "secret-token" not in safe
    assert "key=%2A%2A%2AREDACTED%2A%2A%2A" in safe
    assert "access_token=%2A%2A%2AREDACTED%2A%2A%2A" in safe
    assert "alt=json" in safe


def test_context_full_logging_can_be_enabled_by_env(monkeypatch, tmp_path):
    monkeypatch.setenv("NERYA_CONTEXT_FULL_LOG", "1")
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

    gateway.call_messages(
        task="agent.loop",
        caller="test",
        system="system",
        messages=[{"role": "user", "content": "hello"}],
        tier="medium",
    )

    records = jsonl.read_all(cfg.paths.dev_log("llm_context_full"))
    assert [record["phase"] for record in records] == ["request", "response"]


def test_context_full_logging_records_messages_errors(tmp_path):
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
    cfg.data["llm"]["context_log_mode"] = "full"
    gateway = LLMGateway(cfg)

    def backend(_request: MessagesRequest) -> MessagesResponse:
        raise LLMError("provider rejected sk-123456789012345678901234")

    gateway._resolve_messages_backend = lambda *_, **__: backend  # type: ignore[method-assign]

    with pytest.raises(LLMError):
        gateway.call_messages(
            task="agent.loop",
            caller="test",
            system="system",
            messages=[{"role": "user", "content": "hello"}],
            tier="medium",
            metadata={
                "session_id": "sess-error",
                "turn_id": "turn-error",
                "iteration": 3,
                "llm_attempt": 2,
            },
        )

    records = jsonl.read_all(cfg.paths.dev_log("llm_context_full"))
    assert [record["phase"] for record in records] == ["request", "error"]
    assert records[1]["call_id"] == records[0]["call_id"]
    assert records[1]["session_id"] == "sess-error"
    assert records[1]["turn_id"] == "turn-error"
    assert records[1]["iteration"] == 3
    assert records[1]["llm_attempt"] == 2
    assert records[1]["error"]["message"] == "provider rejected ***REDACTED***"


def test_context_full_logging_records_prompt_api_correlation_metadata(tmp_path):
    cfg = _config(
        tmp_path,
        {
            "medium": {
                "provider": "mock",
                "model": "medium-model",
                "allowed_tasks": ["subagent_analysis"],
            },
        },
    )
    cfg.data["llm"]["context_log_mode"] = "full"
    gateway = LLMGateway(cfg)

    result = gateway.call(
        task="subagent_analysis",
        caller="subagent:researcher",
        prompt="preserve this complete prompt context",
        tier="medium",
        metadata={
            "session_id": "sess-sub",
            "turn_id": "turn-parent",
            "iteration": 4,
            "subagent": "researcher",
            "strategy_id": "strategy-1",
            "trigger_event_id": "trigger-1",
            "parent_call_id": "toolu-parent",
        },
    )

    assert result.raw
    records = jsonl.read_all(cfg.paths.dev_log("llm_context_full"))
    assert [record["phase"] for record in records] == ["request", "response"]
    for record in records:
        assert record["api"] == "prompt"
        assert record["session_id"] == "sess-sub"
        assert record["turn_id"] == "turn-parent"
        assert record["iteration"] == 4
        assert record["subagent"] == "researcher"
        assert record["strategy_id"] == "strategy-1"
        assert record["trigger_event_id"] == "trigger-1"
        assert record["parent_call_id"] == "toolu-parent"

    request = records[0]["request"]
    assert request["prompt"] == "preserve this complete prompt context"
    assert request["metadata"]["subagent"] == "researcher"


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


def test_messages_call_retries_across_configured_models_and_keys(
    monkeypatch,
    tmp_path,
):
    monkeypatch.setenv("CLIPROXY_API_KEYS", "bad-key, good-key")
    cfg = _config(
        tmp_path,
        {
            "medium": {
                "provider": "cliproxy",
                "model": "primary-model, backup-model",
                "provider_key_env": "CLIPROXY_API_KEYS",
                "base_url": "https://cliproxy.example/v1",
                "allowed_tasks": ["agent.loop"],
            },
        },
    )
    gateway = LLMGateway(cfg)
    attempts: list[tuple[str, str]] = []

    def resolve_backend(_tier: str, **kwargs):  # noqa: ANN001
        route_cfg = kwargs["route_cfg"]
        model = str(route_cfg.get("model") or "")
        api_key = str(route_cfg.get("_resolved_provider_key") or "")
        attempts.append((model, api_key))

        def backend(_request: MessagesRequest) -> MessagesResponse:
            if model == "backup-model" and api_key == "good-key":
                return MessagesResponse(
                    content=[{"type": "text", "text": "backup ok"}],
                    usage={"input_tokens": 3, "output_tokens": 2},
                    provider="cliproxy",
                    model=model,
                )
            raise LLMError(f"route failed: {model}/{api_key}")

        return backend

    gateway._resolve_messages_backend = resolve_backend  # type: ignore[method-assign]

    response = gateway.call_messages(
        task="agent.loop",
        caller="test",
        system="system",
        messages=[{"role": "user", "content": "hello"}],
        tier="medium",
    )

    assert response.text() == "backup ok"
    assert response.model == "backup-model"
    assert attempts == [
        ("primary-model", "bad-key"),
        ("primary-model", "good-key"),
        ("backup-model", "bad-key"),
        ("backup-model", "good-key"),
    ]


def test_messages_call_retries_across_configured_provider_routes(
    monkeypatch,
    tmp_path,
):
    monkeypatch.setenv("PRIMARY_ROUTE_KEY", "bad-key")
    monkeypatch.setenv("BACKUP_ROUTE_KEY", "good-key")
    cfg = _config(
        tmp_path,
        {
            "medium": {
                "routes": [
                    {
                        "provider": "provider-a",
                        "model": "primary-model",
                        "provider_key_env": "PRIMARY_ROUTE_KEY",
                        "base_url": "https://primary.example/v1",
                        "provider_native_web_search": {"enabled": True},
                    },
                    {
                        "provider": "provider-b",
                        "model": "backup-model",
                        "provider_key_env": "BACKUP_ROUTE_KEY",
                        "base_url": "https://backup.example/v1",
                        "provider_native_web_search": {"enabled": False},
                    },
                ],
                "allowed_tasks": ["agent.loop"],
            },
        },
    )
    gateway = LLMGateway(cfg)
    attempts: list[tuple[str, str, str, dict]] = []

    def resolve_backend(_tier: str, **kwargs):  # noqa: ANN001
        route_cfg = kwargs["route_cfg"]
        provider = str(route_cfg.get("provider") or "")
        model = str(route_cfg.get("model") or "")
        api_key = str(route_cfg.get("_resolved_provider_key") or "")

        def backend(request: MessagesRequest) -> MessagesResponse:
            attempts.append((
                provider,
                model,
                api_key,
                request.metadata["provider_native_web_search"],
            ))
            if provider == "provider-b" and api_key == "good-key":
                return MessagesResponse(
                    content=[{"type": "text", "text": "backup ok"}],
                    usage={"input_tokens": 3, "output_tokens": 2},
                    provider=provider,
                    model=model,
                )
            raise LLMError(f"route failed: {provider}/{model}/{api_key}")

        return backend

    gateway._resolve_messages_backend = resolve_backend  # type: ignore[method-assign]

    response = gateway.call_messages(
        task="agent.loop",
        caller="test",
        system="system",
        messages=[{"role": "user", "content": "hello"}],
        tier="medium",
    )

    assert response.text() == "backup ok"
    assert attempts == [
        ("provider-a", "primary-model", "bad-key", {"enabled": True}),
        ("provider-b", "backup-model", "good-key", {"enabled": False}),
    ]


def test_model_router_retries_across_configured_models_and_keys(
    monkeypatch,
    tmp_path,
):
    monkeypatch.setenv("OPENAI_KEYS", "bad-key, good-key")
    calls: list[tuple[str, str]] = []

    def adapter(**kwargs):  # noqa: ANN001
        model = str(kwargs["model"])
        api_key = str(kwargs["api_key"])
        calls.append((model, api_key))
        if model == "backup-model" and api_key == "good-key":
            return ProviderResult(
                text="ok",
                prompt_tokens=2,
                completion_tokens=3,
                total_tokens=5,
                provider="openai",
                model=model,
            )
        raise LLMError(f"route failed: {model}/{api_key}")

    router = ModelRouter(
        tiers={
            "medium": {
                "provider": "openai",
                "model": "primary-model, backup-model",
                "provider_key_env": "OPENAI_KEYS",
            },
        },
        workspace=tmp_path,
        providers={"openai": adapter},
        allow_mock=False,
    )

    result = router.dispatch(
        tier="medium",
        task="subagent_analysis",
        prompt="hello",
    )

    assert result.text == "ok"
    assert result.model == "backup-model"
    assert result.provider == "openai"
    assert result.fallback_used is True
    assert calls == [
        ("primary-model", "bad-key"),
        ("primary-model", "good-key"),
        ("backup-model", "bad-key"),
        ("backup-model", "good-key"),
    ]


def test_model_router_retries_across_configured_provider_routes(
    monkeypatch,
    tmp_path,
):
    monkeypatch.setenv("PRIMARY_ROUTE_KEY", "bad-key")
    monkeypatch.setenv("BACKUP_ROUTE_KEY", "good-key")
    calls: list[tuple[str, str, str, str]] = []

    def adapter(**kwargs):  # noqa: ANN001
        provider = str(kwargs["provider_name"])
        model = str(kwargs["model"])
        api_key = str(kwargs["api_key"])
        base_url = str(kwargs["base_url"])
        calls.append((provider, model, api_key, base_url))
        if provider == "provider-b" and model == "backup-model" and api_key == "good-key":
            return ProviderResult(
                text="ok",
                prompt_tokens=2,
                completion_tokens=3,
                total_tokens=5,
                provider=provider,
                model=model,
            )
        raise LLMError(f"route failed: {provider}/{model}/{api_key}")

    router = ModelRouter(
        tiers={
            "medium": {
                "routes": [
                    {
                        "provider": "provider-a",
                        "model": "primary-model",
                        "provider_key_env": "PRIMARY_ROUTE_KEY",
                        "base_url": "https://primary.example/v1",
                        "kind": "chat_completions",
                    },
                    {
                        "provider": "provider-b",
                        "model": "backup-model",
                        "provider_key_env": "BACKUP_ROUTE_KEY",
                        "base_url": "https://backup.example/v1",
                        "kind": "chat_completions",
                    },
                ],
            },
        },
        workspace=tmp_path,
        providers={"compat": adapter},
        allow_mock=False,
    )

    result = router.dispatch(
        tier="medium",
        task="subagent_analysis",
        prompt="hello",
    )

    assert result.text == "ok"
    assert result.model == "backup-model"
    assert result.provider == "provider-b"
    assert result.fallback_used is True
    assert calls == [
        ("provider-a", "primary-model", "bad-key", "https://primary.example/v1"),
        ("provider-b", "backup-model", "good-key", "https://backup.example/v1"),
    ]


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
                "timeout_s": 42,
                "http_max_attempts": 2,
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
    assert backend.timeout == 42
    assert backend.max_attempts == 2


def test_openai_compatible_messages_backend_caps_timeout_to_request_deadline():
    transport = _CapturingTransport({
        "choices": [
            {
                "message": {"content": "ok"},
                "finish_reason": "stop",
            }
        ],
        "usage": {"prompt_tokens": 1, "completion_tokens": 1},
    })
    backend = OpenAIMessagesBackend(
        api_key="sk-test",
        model="MiniMax-M3",
        base_url="https://api.minimaxi.com/v1",
        transport=transport,
        timeout=42,
        max_attempts=1,
        provider_name="minimax-cn",
    )

    response = backend(
        MessagesRequest(
            system="system",
            messages=[{"role": "user", "content": "hello"}],
            deadline=time.time() + 5,
        )
    )

    assert response.text() == "ok"
    assert len(transport.calls) == 1
    assert 0 < transport.calls[0]["timeout"] <= 5
    assert transport.calls[0]["timeout"] < 42


def test_openai_compatible_messages_backend_does_not_split_deadline_by_retries():
    transport = _CapturingTransport({
        "choices": [
            {
                "message": {"content": "ok"},
                "finish_reason": "stop",
            }
        ],
        "usage": {"prompt_tokens": 1, "completion_tokens": 1},
    })
    backend = OpenAIMessagesBackend(
        api_key="sk-test",
        model="MiniMax-M3",
        base_url="https://api.minimaxi.com/v1",
        transport=transport,
        timeout=180,
        max_attempts=3,
        provider_name="minimax-cn",
    )

    response = backend(
        MessagesRequest(
            system="system",
            messages=[{"role": "user", "content": "final synthesis"}],
            max_tokens=4096,
            deadline=time.time() + 135,
        )
    )

    assert response.text() == "ok"
    assert len(transport.calls) == 1
    # The total deadline caps a provider attempt, but it must not be divided
    # by the number of configured retries. Long final-only requests need the
    # first attempt to have the full remaining provider window.
    assert 100 < transport.calls[0]["timeout"] <= 135


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
