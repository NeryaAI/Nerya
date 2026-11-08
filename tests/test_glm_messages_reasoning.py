"""Offline wire checks for the documented GLM thinking switch.

Contract supplied and verified by the operator:
https://docs.bigmodel.cn/cn/guide/capabilities/thinking-mode.md
"""

import pytest

from nerya.core.errors import LLMError
from nerya.llm.messages import MessagesRequest, OpenAIMessagesBackend


class Transport:
    def __init__(self):
        self.calls = []

    def post_json(self, url, **kwargs):
        self.calls.append(kwargs["body"])
        return 200, {
            "choices": [{"message": {"content": "ok"}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 5, "completion_tokens": 1},
        }


def call(transport, *, provider="zai", model="glm-5.2", effort="none", backend_effort=None):
    backend = OpenAIMessagesBackend(
        api_key="fixture", provider_name=provider, model=model, transport=transport,
        base_url="https://fixture.invalid/v1", max_attempts=1, reasoning_effort=backend_effort,
    )
    return backend(MessagesRequest(
        system="fixture", messages=[{"role": "user", "content": "fixture"}],
        max_tokens=4096, temperature=0, reasoning_effort=effort,
        tools=[{"name": "record_replay_decision", "input_schema": {"type": "object"}}],
        tool_choice={"type": "tool", "name": "record_replay_decision"},
    ))


def test_glm_52_explicit_none_disables_thinking_on_wire():
    transport = Transport()
    call(transport)
    assert len(transport.calls) == 1
    body = transport.calls[0]
    assert body["thinking"] == {"type": "disabled"}
    assert body["max_tokens"] == 4096 and body["temperature"] == 0
    assert body["tool_choice"] == "required"
    assert "reasoning_effort" not in body and "max_completion_tokens" not in body


@pytest.mark.parametrize("provider", ["zai", "zhipu", "glm"])
@pytest.mark.parametrize("model", ["glm-5.2", "GLM-5.2", "glm-5.1", "glm-5", "glm-4.7"])
def test_known_models_and_provider_aliases_support_explicit_off(provider, model):
    transport = Transport()
    call(transport, provider=provider, model=model)
    assert len(transport.calls) == 1
    assert transport.calls[0]["thinking"] == {"type": "disabled"}


@pytest.mark.parametrize("provider,model", [
    ("zai", "glm-5.3"), ("zhipu", "GLM-5.3"),
    ("zai", "glm-5.3-flash"), ("zhipu", "GLM-5.3-FLASH"),
    ("zai", "glm-5.4"), ("zai", "glm-5.2-unverified"),
    ("zai", "unknown"), ("other", "glm-5.2"), ("openai", "o3"),
])
def test_forced_thinking_and_unverified_models_reject_none_before_transport(provider, model):
    transport = Transport()
    with pytest.raises(LLMError, match="reasoning_off_unsupported"):
        call(transport, provider=provider, model=model)
    assert transport.calls == []


@pytest.mark.parametrize("model", ["glm-5.2", "glm-5.3", "glm-5.3-flash"])
@pytest.mark.parametrize("effort", ["low", "medium", "high"])
def test_positive_effort_enables_thinking_without_inventing_intensity(model, effort):
    transport = Transport()
    call(transport, model=model, effort=effort)
    assert transport.calls[0]["thinking"] == {"type": "enabled"}
    assert "reasoning_effort" not in transport.calls[0]


@pytest.mark.parametrize("effort", ["minimal", "xhigh", "adaptive", "invalid"])
def test_unverified_effort_levels_fail_before_transport(effort):
    transport = Transport()
    with pytest.raises(LLMError, match="reasoning_effort_unsupported"):
        call(transport, effort=effort)
    assert transport.calls == []


@pytest.mark.parametrize("effort,backend_effort,expected", [
    (None, None, None), ("inherit", None, None),
    (None, "none", "disabled"), (None, "high", "enabled"),
    ("none", "high", "disabled"), ("high", "none", "enabled"),
    ("off", None, "disabled"), ("disabled", None, "disabled"),
])
def test_request_precedence_and_default_are_preserved(effort, backend_effort, expected):
    transport = Transport()
    call(transport, effort=effort, backend_effort=backend_effort)
    assert transport.calls[0].get("thinking") == ({"type": expected} if expected else None)


def test_registry_exposes_exact_switch_capability_and_wire_protocol():
    from nerya.llm.model_registry import lookup

    for model in ("glm-5.2", "glm-5.1", "glm-5", "glm-4.7"):
        meta = lookup("zai", model)
        assert meta.supports_reasoning and meta.supports_reasoning_disable
        assert meta.to_dict()["reasoning_wire_protocol"] == "thinking_type"
    assert not lookup("zai", "glm-5.3").supports_reasoning_disable
    assert not lookup("zai", "glm-5.4").supports_reasoning_disable
    assert lookup("other", "glm-5.2").reasoning_wire_protocol == ""
