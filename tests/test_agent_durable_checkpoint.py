from __future__ import annotations

from copy import deepcopy
from typing import Any

import pytest

import nerya.agent.kernel as kernel_module
from nerya.agent.kernel import AgentKernel
from nerya.agent.loop_state import TurnCheckpointResumeError
from nerya.core.config import Config, DEFAULT_CONFIG
from nerya.core.paths import WorkspacePaths
from nerya.db.repositories import AgentSessionRepository
from nerya.db.sqlite import connect
from nerya.llm.messages import MessagesResponse


pytestmark = pytest.mark.smoke


def _config(tmp_path) -> Config:
    data = deepcopy(DEFAULT_CONFIG)
    data.setdefault("agent", {}).setdefault("native", {})[
        "max_extra_llm_attempts_per_turn"
    ] = 4
    return Config(paths=WorkspacePaths(root=tmp_path), data=data)


def _install_scripted_gateway(
    monkeypatch,
    responses: list[MessagesResponse],
) -> list[list[dict[str, Any]]]:
    scripted = list(responses)
    calls: list[list[dict[str, Any]]] = []

    class _Gateway:
        def __init__(self, *_args, **_kwargs):
            pass

        def effective_model_metadata(self, *_args, **_kwargs):
            return "fixture", "checkpoint-model", {}

        def call_messages(self, **kwargs):  # noqa: ANN001
            calls.append(deepcopy(list(kwargs.get("messages") or [])))
            if not scripted:
                raise AssertionError("checkpoint gateway script exhausted")
            return scripted.pop(0)

    monkeypatch.setattr(kernel_module, "LLMGateway", _Gateway)
    return calls


def _text_response(text: str) -> MessagesResponse:
    return MessagesResponse(
        content=[{"type": "text", "text": text}],
        stop_reason="end_turn",
        usage={"input_tokens": 10, "output_tokens": 2},
        provider="fixture",
        model="checkpoint-model",
        usd_cost=0.01,
    )


def _trigger(event_id: str, text: str = "") -> dict[str, Any]:
    return {
        "id": event_id,
        "source": "dashboard",
        "kind": "user.chat",
        "payload": {"text": text},
    }


def test_durable_checkpoint_resumes_across_kernel_instances_without_leak(
    tmp_path,
    monkeypatch,
) -> None:
    cfg = _config(tmp_path)
    calls = _install_scripted_gateway(
        monkeypatch,
        [_text_response("draft"), _text_response("evidence-backed")],
    )

    first = AgentKernel(config=cfg, skills=None).run_turn(  # type: ignore[arg-type]
        trigger=_trigger("event-1", "inspect the workspace"),
        session_id="session-durable",
        turn_id="turn-durable",
    )

    assert first.final_text == "draft"
    assert first.budget["checkpoint"] == {
        "state": "saved",
        "persisted": True,
        "resumable": True,
        "turn_id": "turn-durable",
        "resume_count": 0,
        "bytes": first.budget["checkpoint"]["bytes"],
    }
    assert first.budget["checkpoint"]["bytes"] > 0

    second = AgentKernel(config=cfg, skills=None).run_turn(  # type: ignore[arg-type]
        trigger=_trigger("event-2"),
        session_id="session-durable",
        resume_turn_id="turn-durable",
        continuation_feedback="include verified evidence",
    )

    assert second.turn_id == "turn-durable"
    assert second.final_text == "evidence-backed"
    assert second.budget["checkpoint_continue"] is True
    assert second.budget["checkpoint"]["state"] == "saved"
    assert second.budget["checkpoint"]["resume_count"] == 1
    assert second.execution_state["checkpoint"] == second.budget["checkpoint"]
    assert len(calls) == 2

    second_messages = calls[1]
    original_requests = [
        message
        for message in second_messages
        if message.get("role") == "user"
        and message.get("content") == "inspect the workspace"
    ]
    continuation_messages = [
        message
        for message in second_messages
        if message.get("role") == "user"
        and "[completion gate continuation]" in str(message.get("content") or "")
    ]
    assert len(original_requests) == 1
    assert len(continuation_messages) == 1
    assert "include verified evidence" in continuation_messages[0]["content"]
    assert continuation_messages[0]["pinned"] is True

    con = connect(cfg.paths.db)
    try:
        repo = AgentSessionRepository(con)
        transcript = repo.transcript("session-durable", limit=0)
        checkpoint_row = repo.peek_turn_checkpoint("session-durable")
        session_row = repo.get_session("session-durable")
    finally:
        con.close()

    assert [row["role"] for row in transcript] == ["user", "assistant"]
    assert transcript[0]["content"] == "inspect the workspace"
    assert transcript[1]["content"] == "evidence-backed"
    assert checkpoint_row is not None
    assert checkpoint_row["claim_id"] is None
    assert checkpoint_row["checkpoint"]["resume_count"] == 1
    assert session_row is not None
    assert "checkpoint_json" not in session_row
    assert "turn_checkpoint" not in str(session_row.get("meta_json") or "")
    assert "inspect the workspace" not in str(session_row.get("meta_json") or "")


def test_normal_turn_cannot_replace_another_workers_live_lease(
    tmp_path,
    monkeypatch,
) -> None:
    cfg = _config(tmp_path)
    calls = _install_scripted_gateway(monkeypatch, [_text_response("draft")])
    AgentKernel(config=cfg, skills=None).run_turn(  # type: ignore[arg-type]
        trigger=_trigger("event-1", "inspect the workspace"),
        session_id="session-live-lease",
        turn_id="turn-live-lease",
    )

    con = connect(cfg.paths.db)
    try:
        repo = AgentSessionRepository(con)
        claimed = repo.claim_turn_checkpoint(
            "session-live-lease",
            turn_id="turn-live-lease",
            claim_id="tcp_live_worker",
        )
    finally:
        con.close()
    assert claimed is not None

    with pytest.raises(
        TurnCheckpointResumeError,
        match="Another worker already owns",
    ) as exc_info:
        AgentKernel(config=cfg, skills=None).run_turn(  # type: ignore[arg-type]
            trigger=_trigger("event-2", "start a different task"),
            session_id="session-live-lease",
            turn_id="turn-new",
        )

    assert exc_info.value.code == "turn_checkpoint_already_claimed"
    assert exc_info.value.status == 409
    assert len(calls) == 1
    con = connect(cfg.paths.db)
    try:
        current = AgentSessionRepository(con).peek_turn_checkpoint(
            "session-live-lease"
        )
    finally:
        con.close()
    assert current is not None
    assert current["turn_id"] == "turn-live-lease"
    assert current["claim_id"] == "tcp_live_worker"


def test_failed_durable_resume_leaves_checkpoint_claimed_and_unreplayable(
    tmp_path,
    monkeypatch,
) -> None:
    cfg = _config(tmp_path)
    _install_scripted_gateway(monkeypatch, [_text_response("draft")])
    AgentKernel(config=cfg, skills=None).run_turn(  # type: ignore[arg-type]
        trigger=_trigger("event-1", "inspect the workspace"),
        session_id="session-failed-resume",
        turn_id="turn-failed-resume",
    )

    def _fail_after_claim(self, **_kwargs):  # noqa: ANN001
        raise RuntimeError("provider failed after checkpoint claim")

    monkeypatch.setattr(AgentKernel, "_run", _fail_after_claim)

    with pytest.raises(RuntimeError, match="failed after checkpoint claim"):
        AgentKernel(config=cfg, skills=None).run_turn(  # type: ignore[arg-type]
            trigger=_trigger("event-2"),
            session_id="session-failed-resume",
            resume_turn_id="turn-failed-resume",
            continuation_feedback="continue safely",
        )

    con = connect(cfg.paths.db)
    try:
        claimed = AgentSessionRepository(con).peek_turn_checkpoint(
            "session-failed-resume"
        )
    finally:
        con.close()
    assert claimed is not None
    assert str(claimed["claim_id"] or "").startswith("tcp_")

    with pytest.raises(
        TurnCheckpointResumeError,
        match="already being resumed",
    ) as exc_info:
        AgentKernel(config=cfg, skills=None).run_turn(  # type: ignore[arg-type]
            trigger=_trigger("event-3"),
            session_id="session-failed-resume",
            resume_turn_id="turn-failed-resume",
            continuation_feedback="retry the same checkpoint",
        )
    assert exc_info.value.code == "turn_checkpoint_already_claimed"
    assert exc_info.value.status == 409
