from __future__ import annotations

from copy import deepcopy
from types import SimpleNamespace
import threading
import time

import pytest

from nerya.agent.kernel import AgentKernel, AgentTurnResult
from nerya.agent.loop_state import TurnCheckpointResumeError
from nerya.api import routes_agent
from nerya.core import jsonl
from nerya.core.config import Config, DEFAULT_CONFIG
from nerya.core.paths import WorkspacePaths
from nerya.db.repositories import AgentSessionRepository
from nerya.db.sqlite import connect
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


def _completed_turn(
    kwargs: dict[str, object],
    *,
    event_id: str = "evt_1",
    default_turn_id: str = "turn_1",
    text: str = "done",
) -> AgentTurnResult:
    return AgentTurnResult(
        trigger_event_id=event_id,
        strategy_id=kwargs.get("strategy_id"),
        session_id=kwargs.get("session_id"),
        turn_id=str(kwargs.get("turn_id") or default_turn_id),
        decision={"action": "send_message", "text": text},
        actions=[{"action": "send_message", "ok": True, "text": text}],
        tool_trace=[],
        stopped_reason="end_turn",
        final_text=text,
    )


@pytest.mark.parametrize(
    ("payload", "status", "error"),
    [
        (
            {
                "session_id": "session-1",
                "resume_turn_id": "turn-1",
            },
            400,
            "turn_checkpoint_resume_fields_required",
        ),
        (
            {
                "resume_turn_id": "turn-1",
                "continuation_feedback": "continue",
            },
            400,
            "turn_checkpoint_session_required",
        ),
        (
            {
                "session_id": "session-1",
                "turn_id": "turn-other",
                "resume_turn_id": "turn-1",
                "continuation_feedback": "continue",
            },
            409,
            "turn_checkpoint_turn_mismatch",
        ),
        (
            {
                "session_id": "session-1",
                "resume_turn_id": "turn-1",
                "continuation_feedback": "continue",
                "attachments": [{"name": "new.txt"}],
            },
            400,
            "turn_checkpoint_attachments_not_supported",
        ),
    ],
)
def test_run_turn_validates_durable_resume_before_kernel(
    tmp_path,
    monkeypatch,
    payload,
    status,
    error,
):
    cfg = Config(paths=WorkspacePaths(root=tmp_path), data=deepcopy(DEFAULT_CONFIG))
    client = SimpleNamespace(config=cfg, skills=None)

    class UnexpectedKernel:
        def __init__(self, **_kwargs):
            raise AssertionError("invalid resume request reached AgentKernel")

    monkeypatch.setattr(routes_agent, "AgentKernel", UnexpectedKernel)

    result = _run_turn_route()(client, payload)

    assert result["_status"] == status
    assert result["ok"] is False
    assert result["error"] == error


def test_run_turn_durable_resume_bypasses_commands_and_passes_fields(
    tmp_path,
    monkeypatch,
):
    cfg = Config(paths=WorkspacePaths(root=tmp_path), data=deepcopy(DEFAULT_CONFIG))
    client = SimpleNamespace(config=cfg, skills=None)
    captured: dict[str, object] = {}

    def unexpected_command(*_args, **_kwargs):
        raise AssertionError("checkpoint feedback was dispatched as a slash command")

    class CapturingKernel:
        def __init__(self, **_kwargs):
            pass

        def run_turn(self, **kwargs):
            captured.update(kwargs)
            return _completed_turn(
                kwargs,
                event_id="evt_resume",
                default_turn_id="turn-resume",
            )

    monkeypatch.setattr(
        routes_agent,
        "_run_turn_command_response",
        unexpected_command,
    )
    monkeypatch.setattr(routes_agent, "AgentKernel", CapturingKernel)

    result = _run_turn_route()(
        client,
        {
            "session_id": "session-resume",
            "resume_turn_id": "turn-resume",
            "continuation_feedback": "/include verified evidence",
        },
    )

    assert result["turn_id"] == "turn-resume"
    assert captured["session_id"] == "session-resume"
    assert captured["resume_turn_id"] == "turn-resume"
    assert captured["continuation_feedback"] == "/include verified evidence"
    assert captured["trigger"] == {}


def test_run_turn_surfaces_typed_checkpoint_kernel_error(tmp_path, monkeypatch):
    cfg = Config(paths=WorkspacePaths(root=tmp_path), data=deepcopy(DEFAULT_CONFIG))
    client = SimpleNamespace(config=cfg, skills=None)

    class MissingCheckpointKernel:
        def __init__(self, **_kwargs):
            pass

        def run_turn(self, **_kwargs):
            raise TurnCheckpointResumeError(
                "turn_checkpoint_not_found",
                "No durable checkpoint exists.",
                status=404,
            )

    monkeypatch.setattr(routes_agent, "AgentKernel", MissingCheckpointKernel)

    result = _run_turn_route()(
        client,
        {
            "session_id": "session-resume",
            "resume_turn_id": "turn-resume",
            "continuation_feedback": "continue",
        },
    )

    assert result == {
        "_status": 404,
        "ok": False,
        "error": "turn_checkpoint_not_found",
        "message": "No durable checkpoint exists.",
    }


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
            return _completed_turn(kwargs)

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
            return _completed_turn(kwargs)

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
            return _completed_turn(kwargs)

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
            return _completed_turn(kwargs)

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
            return _completed_turn(
                kwargs,
                event_id="evt_contract",
                default_turn_id="turn_contract",
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


def test_kernel_persists_failed_session_turn_when_run_raises(tmp_path, monkeypatch):
    cfg = Config(paths=WorkspacePaths(root=tmp_path), data=deepcopy(DEFAULT_CONFIG))

    def fail_run(self, **_kwargs):  # noqa: ANN001
        raise RuntimeError("provider exploded")

    monkeypatch.setattr(AgentKernel, "_run", fail_run)

    with pytest.raises(RuntimeError, match="provider exploded"):
        AgentKernel(config=cfg, skills=None).run_turn(
            trigger={
                "id": "evt_failed",
                "source": "dashboard",
                "kind": "user.chat",
                "payload": {"text": "写个游戏"},
            },
            session_id="failed-session",
            turn_id="turn_failed",
        )

    rows = jsonl.read_all(cfg.paths.journal("agent"))
    end_rows = [
        row
        for row in rows
        if row.get("kind") == "agent.turn.end"
        and row.get("turn_id") == "turn_failed"
    ]
    assert end_rows
    assert end_rows[-1]["stop_reason"] == "error"
    assert end_rows[-1]["transition_reason"] == "runtime_error"
    assert end_rows[-1]["aborted"] is True

    con = connect(cfg.paths.db)
    try:
        messages = AgentSessionRepository(con).transcript("failed-session", limit=0)
    finally:
        con.close()
    assert [m["role"] for m in messages] == ["user", "assistant"]
    assert messages[0]["content"] == "写个游戏"
    assert "provider exploded" in messages[1]["content"]
