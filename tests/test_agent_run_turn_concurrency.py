from __future__ import annotations

from copy import deepcopy
from types import SimpleNamespace
import threading
import time

import pytest

from nerya.agent.kernel import AgentKernel, AgentTurnResult
from nerya.api import routes_agent
from nerya.core.config import Config, DEFAULT_CONFIG
from nerya.core.paths import WorkspacePaths
from nerya.llm.messages import MessagesResponse


pytestmark = pytest.mark.smoke


def _run_turn_route():
    return next(
        handler
        for method, path, handler in routes_agent.routes()
        if method == "POST" and path == "/agent/run_turn"
    )


def _interrupt_route():
    return next(
        handler
        for method, path, handler in routes_agent.routes()
        if method == "POST" and path == "/agent/interrupt"
    )


def test_run_turn_rejects_concurrent_turn_for_same_session(tmp_path, monkeypatch):
    cfg = Config(paths=WorkspacePaths(root=tmp_path), data=deepcopy(DEFAULT_CONFIG))
    client = SimpleNamespace(config=cfg, skills=None)
    started = threading.Event()
    release = threading.Event()

    class SlowKernel:
        def __init__(self, **_kwargs):
            pass

        def run_turn(self, **kwargs):
            started.set()
            assert release.wait(timeout=5), "test timed out waiting to release slow turn"
            return AgentTurnResult(
                trigger_event_id="evt_1",
                strategy_id=kwargs.get("strategy_id"),
                session_id=kwargs.get("session_id"),
                turn_id=kwargs.get("turn_id") or "turn_1",
                decision={"action": "send_message", "text": "done"},
                actions=[{"action": "send_message", "ok": True, "text": "done"}],
                tool_trace=[],
                stopped_reason="end_turn",
                final_text="done",
            )

    monkeypatch.setattr(routes_agent, "AgentKernel", SlowKernel)
    run_turn = _run_turn_route()
    payload = {
        "session_id": "shared-session",
        "trigger": {
            "id": "evt_1",
            "source": "dashboard",
            "kind": "user.chat",
            "payload": {"text": "first"},
        },
    }
    first_result: dict[str, object] = {}

    def call_first() -> None:
        first_result.update(run_turn(client, payload))

    worker = threading.Thread(target=call_first)
    worker.start()
    assert started.wait(timeout=2)

    second = run_turn(
        client,
        {
            **payload,
            "trigger": {
                "id": "evt_2",
                "source": "dashboard",
                "kind": "user.chat",
                "payload": {"text": "continue"},
            },
        },
    )

    release.set()
    worker.join(timeout=5)

    assert worker.is_alive() is False
    assert first_result["turn_id"] == "turn_1"
    assert second["_status"] == 409
    assert second["ok"] is False
    assert second["error"] == "session_turn_in_progress"


def test_run_turn_registers_cancel_token_for_interrupt(tmp_path, monkeypatch):
    cfg = Config(paths=WorkspacePaths(root=tmp_path), data=deepcopy(DEFAULT_CONFIG))
    client = SimpleNamespace(config=cfg, skills=None)
    started = threading.Event()

    def fake_run(self, **kwargs):  # noqa: ANN001
        token = kwargs.get("cancel_token")
        started.set()
        deadline = time.time() + 5
        while time.time() < deadline:
            if token is not None and token.is_set:
                return AgentTurnResult(
                    trigger_event_id="evt_cancel",
                    strategy_id=kwargs.get("strategy_id"),
                    session_id=kwargs.get("session_id"),
                    turn_id=kwargs.get("turn_id"),
                    decision={"action": "send_message", "text": "cancelled"},
                    actions=[],
                    tool_trace=[],
                    stopped_reason="cancelled",
                    final_text="cancelled",
                )
            time.sleep(0.01)
        raise AssertionError("cancel token was not signalled")

    monkeypatch.setattr(AgentKernel, "_run", fake_run)

    run_turn = _run_turn_route()
    interrupt = _interrupt_route()
    payload = {
        "session_id": "cancel-session",
        "trigger": {
            "id": "evt_cancel",
            "source": "dashboard",
            "kind": "user.chat",
            "payload": {"text": "cancel me"},
        },
    }
    result: dict[str, object] = {}

    def call_run_turn() -> None:
        result.update(run_turn(client, payload))

    worker = threading.Thread(target=call_run_turn)
    worker.start()
    assert started.wait(timeout=2)

    cancelled = interrupt(client, {"session_id": "cancel-session", "reason": "test_cancel"})
    worker.join(timeout=5)

    assert cancelled["ok"] is True
    assert cancelled["cancelled"] is True
    assert worker.is_alive() is False
    assert result["turn_id"]
    assert result["stopped_reason"] == "cancelled"
    assert interrupt(client, {"session_id": "cancel-session"})["cancelled"] is False


def test_run_turn_does_not_route_prompt_text_to_light_tier(tmp_path, monkeypatch):
    data = deepcopy(DEFAULT_CONFIG)
    data.setdefault("llm", {}).setdefault("tiers", {})["light"] = {
        "provider": "mock",
        "model": "mock-light",
    }
    cfg = Config(paths=WorkspacePaths(root=tmp_path), data=data)
    client = SimpleNamespace(config=cfg, skills=None)
    captured: dict[str, object] = {}

    class CapturingKernel:
        def __init__(self, **kwargs):
            captured.update(kwargs)

        def run_turn(self, **kwargs):
            return AgentTurnResult(
                trigger_event_id="evt_1",
                strategy_id=kwargs.get("strategy_id"),
                session_id=kwargs.get("session_id"),
                turn_id=kwargs.get("turn_id") or "turn_1",
                decision={"action": "send_message", "text": "done"},
                actions=[{"action": "send_message", "ok": True, "text": "done"}],
                tool_trace=[],
                stopped_reason="end_turn",
                final_text="done",
            )

    monkeypatch.setattr(routes_agent, "AgentKernel", CapturingKernel)

    run_turn = _run_turn_route()
    result = run_turn(
        client,
        {
            "session_id": "news-session",
            "source": "chat",
            "kind": "user.chat",
            "payload": {"text": "帮我获取热门的经济新闻进行总结"},
        },
    )

    assert result["final_text"] == "done"
    assert captured["llm_tier"] is None


def test_run_turn_respects_explicit_model_tier_and_env_permission_mode(
    tmp_path,
    monkeypatch,
):
    data = deepcopy(DEFAULT_CONFIG)
    data.setdefault("llm", {}).setdefault("tiers", {})["light"] = {
        "provider": "mock",
        "model": "mock-light",
    }
    cfg = Config(paths=WorkspacePaths(root=tmp_path), data=data)
    client = SimpleNamespace(config=cfg, skills=None)
    captured: dict[str, object] = {}

    class CapturingKernel:
        def __init__(self, **kwargs):
            captured.update(kwargs)

        def run_turn(self, **kwargs):
            return AgentTurnResult(
                trigger_event_id="evt_1",
                strategy_id=kwargs.get("strategy_id"),
                session_id=kwargs.get("session_id"),
                turn_id=kwargs.get("turn_id") or "turn_1",
                decision={"action": "send_message", "text": "done"},
                actions=[{"action": "send_message", "ok": True, "text": "done"}],
                tool_trace=[],
                stopped_reason="end_turn",
                final_text="done",
            )

    monkeypatch.setenv("NERYA_PERMISSION_MODE", "yolo")
    monkeypatch.setattr(routes_agent, "AgentKernel", CapturingKernel)

    run_turn = _run_turn_route()
    result = run_turn(
        client,
        {
            "session_id": "explicit-tier-session",
            "source": "chat",
            "kind": "user.chat",
            "model_tier": "light",
            "payload": {"text": "帮我获取热门的经济新闻进行总结"},
        },
    )

    assert result["final_text"] == "done"
    assert captured["llm_tier"] == "light"
    assert getattr(captured["permission_mode"], "value", "") == "yolo"


def test_run_turn_uses_config_permission_mode_when_env_absent(
    tmp_path,
    monkeypatch,
):
    data = deepcopy(DEFAULT_CONFIG)
    data.setdefault("runtime", {})["permission_mode"] = "yolo"
    cfg = Config(paths=WorkspacePaths(root=tmp_path), data=data)
    client = SimpleNamespace(config=cfg, skills=None)
    captured: dict[str, object] = {}

    class CapturingKernel:
        def __init__(self, **kwargs):
            captured.update(kwargs)

        def run_turn(self, **kwargs):
            return AgentTurnResult(
                trigger_event_id="evt_1",
                strategy_id=kwargs.get("strategy_id"),
                session_id=kwargs.get("session_id"),
                turn_id=kwargs.get("turn_id") or "turn_1",
                decision={"action": "send_message", "text": "done"},
                actions=[{"action": "send_message", "ok": True, "text": "done"}],
                tool_trace=[],
                stopped_reason="end_turn",
                final_text="done",
            )

    monkeypatch.delenv("NERYA_PERMISSION_MODE", raising=False)
    monkeypatch.setattr(routes_agent, "AgentKernel", CapturingKernel)

    run_turn = _run_turn_route()
    result = run_turn(
        client,
        {
            "session_id": "config-permission-session",
            "source": "chat",
            "kind": "user.chat",
            "payload": {"text": "直接执行低风险测试"},
        },
    )

    assert result["final_text"] == "done"
    assert getattr(captured["permission_mode"], "value", "") == "yolo"


def test_kernel_loop_metadata_turn_id_matches_result_turn_id(tmp_path, monkeypatch):
    cfg = Config(paths=WorkspacePaths(root=tmp_path), data=deepcopy(DEFAULT_CONFIG))
    captured_metadata: list[dict[str, object]] = []

    class CapturingGateway:
        def __init__(self, *_args, **_kwargs):
            pass

        def effective_model_metadata(self, *_args, **_kwargs):
            return "mock", "mock", {}

        def call_messages(self, **kwargs):  # noqa: ANN001
            captured_metadata.append(deepcopy(kwargs.get("metadata") or {}))
            return MessagesResponse(
                content=[{"type": "text", "text": "done"}],
                stop_reason="end_turn",
            )

    monkeypatch.setattr("nerya.agent.kernel.LLMGateway", CapturingGateway)

    result = AgentKernel(config=cfg, skills=None).run_turn(
        trigger={
            "id": "evt_turn_id",
            "source": "test",
            "kind": "user.chat",
            "payload": {"text": "hello"},
        },
        session_id="turn-id-session",
    )

    assert result.turn_id.startswith("trn_")
    assert captured_metadata[0]["turn_id"] == result.turn_id


def test_run_turn_passes_evidence_contract_to_kernel(tmp_path, monkeypatch):
    cfg = Config(paths=WorkspacePaths(root=tmp_path), data=deepcopy(DEFAULT_CONFIG))
    client = SimpleNamespace(config=cfg, skills=None)
    captured: dict[str, object] = {}

    class CapturingKernel:
        def __init__(self, **_kwargs):
            pass

        def run_turn(self, **kwargs):
            captured.update(kwargs)
            return AgentTurnResult(
                trigger_event_id="evt_contract",
                strategy_id=kwargs.get("strategy_id"),
                session_id=kwargs.get("session_id"),
                turn_id=kwargs.get("turn_id") or "turn_contract",
                decision={"action": "send_message", "text": "done"},
                actions=[{"action": "send_message", "ok": True, "text": "done"}],
                tool_trace=[],
                stopped_reason="end_turn",
                final_text="done",
            )

    monkeypatch.setattr(routes_agent, "AgentKernel", CapturingKernel)
    run_turn = _run_turn_route()
    contract = {
        "required_artifacts": [
            {
                "kind": "strategy_package_proposal",
                "tool": "strategy_generate_proposal",
                "source": "csv.api_check",
            }
        ]
    }

    result = run_turn(
        client,
        {
            "session_id": "contract-session",
            "source": "chat",
            "kind": "user.chat",
            "payload": {"text": "make a proposal"},
            "evidence_contract": contract,
        },
    )

    assert result["final_text"] == "done"
    assert captured["evidence_contract"] == contract


def test_run_turn_response_exposes_verifier_and_execution_state(
    tmp_path,
    monkeypatch,
):
    cfg = Config(paths=WorkspacePaths(root=tmp_path), data=deepcopy(DEFAULT_CONFIG))
    client = SimpleNamespace(config=cfg, skills=None)

    class CapturingKernel:
        def __init__(self, **_kwargs):
            pass

        def run_turn(self, **kwargs):
            return AgentTurnResult(
                trigger_event_id="evt_1",
                strategy_id=kwargs.get("strategy_id"),
                session_id=kwargs.get("session_id"),
                turn_id=kwargs.get("turn_id") or "turn_1",
                decision={"action": "send_message", "text": "done"},
                actions=[{"action": "send_message", "ok": True, "text": "done"}],
                tool_trace=[],
                stopped_reason="end_turn",
                transition_reason="model_done",
                final_text="done",
                verifier_outcome={
                    "transition_label": "model_done",
                    "hard_status": "missing",
                    "trusted": False,
                },
                execution_state={
                    "version": 1,
                    "items": [],
                    "surfaces": {"status": []},
                    "counters": {"status": 1},
                },
            )

    monkeypatch.setattr(routes_agent, "AgentKernel", CapturingKernel)

    result = _run_turn_route()(
        client,
        {
            "session_id": "state-session",
            "source": "chat",
            "kind": "user.chat",
            "payload": {"text": "hello"},
        },
    )

    assert result["verifier_outcome"]["transition_label"] == "model_done"
    assert result["verifier_outcome"]["trusted"] is False
    assert result["execution_state"]["counters"]["status"] == 1
