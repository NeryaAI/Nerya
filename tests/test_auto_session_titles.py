"""A chat title is a cheap, non-authoritative enrichment of the session."""

from copy import deepcopy
from types import SimpleNamespace

import pytest

from nerya.agent import kernel as kernel_module
from nerya.agent.kernel import AgentKernel
from nerya.agent.session import SessionStore
from nerya.core.config import Config, DEFAULT_CONFIG
from nerya.core.paths import WorkspacePaths

pytestmark = pytest.mark.smoke


def _kernel(tmp_path):
    instance = AgentKernel.__new__(AgentKernel)
    instance.config = Config(
        paths=WorkspacePaths(root=tmp_path), data=deepcopy(DEFAULT_CONFIG)
    )
    instance._sessions = SessionStore(tmp_path)
    instance._sessions.append_turn("chat-1", "turn-1")
    return instance


def _title(kernel):
    return kernel._sessions.load("chat-1").meta


def _generate(kernel):
    kernel._maybe_auto_title_session(
        session_id="chat-1",
        strategy_id=None,
        user_text="帮我优化 ETH 网格交易策略",
        final_text="已准备策略和回测结果",
    )


def test_title_uses_light_model_even_when_chat_model_is_high(tmp_path, monkeypatch):
    instance = _kernel(tmp_path)
    instance.llm_tier = "high"
    seen = []

    class Gateway:
        def __init__(self, config):
            pass

        def call(self, **kwargs):
            seen.append(kwargs)
            return SimpleNamespace(parsed={"title": "ETH 网格策略优化"})

    monkeypatch.setattr(kernel_module, "LLMGateway", Gateway)
    _generate(instance)
    assert seen[0]["tier"] == "light"
    assert seen[0]["task"] == "auto_session_title"
    assert _title(instance)["title"] == "ETH 网格策略优化"
    assert _title(instance)["title_source"] == "light"
    assert _title(instance)["title_generation_attempts"] == 1


def test_manual_rename_during_title_generation_is_not_replaced(tmp_path, monkeypatch):
    instance = _kernel(tmp_path)

    class Gateway:
        def __init__(self, config):
            pass

        def call(self, **kwargs):
            instance._sessions.update_meta(
                "chat-1", {"title": "我的人工标题", "title_source": "operator"}
            )
            return SimpleNamespace(parsed={"title": "自动标题"})

    monkeypatch.setattr(kernel_module, "LLMGateway", Gateway)
    _generate(instance)
    assert _title(instance)["title"] == "我的人工标题"
    assert _title(instance)["title_source"] == "operator"


def test_transient_title_error_has_fallback_and_one_retry(tmp_path, monkeypatch):
    instance = _kernel(tmp_path)
    calls = []

    class Gateway:
        def __init__(self, config):
            pass

        def call(self, **kwargs):
            calls.append(kwargs)
            if len(calls) == 1:
                raise RuntimeError("model temporarily unavailable")
            return SimpleNamespace(parsed={"title": "ETH 网格策略复盘"})

    monkeypatch.setattr(kernel_module, "LLMGateway", Gateway)
    _generate(instance)
    assert _title(instance)["title_source"] == "fallback"
    assert _title(instance)["title_generation_attempts"] == 1

    instance._sessions.append_turn("chat-1", "turn-2")
    _generate(instance)
    assert _title(instance)["title_source"] == "light"
    assert _title(instance)["title"] == "ETH 网格策略复盘"

    instance._sessions.append_turn("chat-1", "turn-3")
    _generate(instance)
    assert len(calls) == 2
