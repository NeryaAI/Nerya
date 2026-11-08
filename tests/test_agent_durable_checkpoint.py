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


@pytest.mark.parametrize("crash_at", ["next_model", "next_dispatch", "unknown", "approval", "interaction", "persistence", "pending_receipt", "handled_error", "legacy_handled_error", "provider429"])
def test_closed_batch_restart_recovery_never_replays_writes(tmp_path, monkeypatch, crash_at):
    import os
    from nerya.agent.command_runtime import CommandRuntime
    from nerya.tools.types import ToolDescriptor, ToolResult, ToolError, ToolErrorKind, RiskLevel
    from nerya.tools.permissions import PermissionMode

    cfg = _config(tmp_path)
    cfg.data.setdefault("runtime", {})["command_id"] = "batch-command"
    if crash_at == "provider429":
        cfg.data["agent"]["native"]["llm_retry_attempts"] = 1
    sid = "batch-session"
    writes = tmp_path / "effect.txt"
    parent_pid = os.getpid()
    phase = [0]
    fail_resume = [crash_at == "provider429"]
    handled_error = crash_at in ("handled_error", "legacy_handled_error")
    read_attempts = [0]

    def response(call_id, operation="first"):
        return MessagesResponse(content=[{"type": "tool_use", "id": call_id,
            "name": "fixture_write", "input": {"operation": operation}}], stop_reason="tool_use",
            usage={"input_tokens": 10, "output_tokens": 2})

    class Gateway:
        def __init__(self, *args, **kwargs): pass
        def effective_model_metadata(self, *args, **kwargs): return "fixture", "checkpoint-model", {}
        def call_messages(self, **kwargs):
            if os.getpid() == parent_pid:
                if crash_at == "provider429":
                    assert kwargs["model_provider"] == "healthy-provider"
                    assert kwargs["model_id"] == "healthy-model"
                    if fail_resume[0]:
                        from nerya.core.errors import LLMError
                        fail_resume[0] = False
                        error = LLMError("fixture resumed provider rate limited (429)")
                        error.status_code = 429
                        raise error
                # Ask for the exact mutation again: the restored ledger must
                # answer it without invoking its handler.
                phase[0] += 1
                return response("repeat-write") if phase[0] == 1 else _text_response("recovered")
            con = connect(cfg.paths.db)
            try:
                cp = AgentSessionRepository(con).peek_turn_checkpoint(sid)
            finally:
                con.close()
            if writes.exists():
                assert cp["claim_id"]
                assert cp["checkpoint"]["total_tool_calls"] == 1 + handled_error
                results = cp["checkpoint"]["tool_ledger"]["completed_tool_results"]
                assert len(results) == 1 + handled_error
                if handled_error:
                    assert results[0]["name"] == "read_file"
                    assert results[0]["error"]["kind"] == "not_found"
                if crash_at == "next_dispatch":
                    return response("second-dispatch", "second")
                if crash_at == "provider429":
                    from nerya.core.errors import LLMError
                    error = LLMError("fixture provider rate limited (429)")
                    error.status_code = 429
                    raise error
                os._exit(0)
            if handled_error and not read_attempts[0]:
                read_attempts[0] += 1
                return MessagesResponse(content=[{"type": "tool_use", "id": "missing-read",
                    "name": "read_file", "input": {"path": "missing-typo.txt"}}],
                    stop_reason="tool_use", usage={"input_tokens": 10, "output_tokens": 2})
            return response("first-write")

    monkeypatch.setattr(kernel_module, "LLMGateway", Gateway)
    original_run = kernel_module.WorkspaceNativeAgentLoop.run
    def run_with_crash(loop, **kwargs):
        save = loop.checkpoint_sink
        def boundary(checkpoint):
            if checkpoint is not None and handled_error:
                assert checkpoint.resumable
                if crash_at == "legacy_handled_error":
                    from dataclasses import replace
                    checkpoint = replace(checkpoint, resumable=False,
                        resume_block_reason="batch_recovery_blocked")
            try:
                save(checkpoint)
            except TurnCheckpointResumeError:
                if crash_at == "persistence":
                    os._exit(0)
                raise
            if checkpoint is not None and crash_at in ("unknown", "approval", "interaction", "pending_receipt"):
                assert not checkpoint.resumable
                assert checkpoint.tool_ledger["completed_tool_results"]
                os._exit(0)
        loop.checkpoint_sink = boundary
        return original_run(loop, **kwargs)
    monkeypatch.setattr(kernel_module.WorkspaceNativeAgentLoop, "run", run_with_crash)

    def execute(config, request):
        kernel = AgentKernel(config=config, skills=None, permission_mode=PermissionMode.AUTO,
            model_provider=request.get("model_provider"), model_id=request.get("model_id"))
        kernel._ensure_registry()
        def write(call):
            if writes.exists():
                os._exit(0) if os.getpid() != parent_pid else pytest.fail("mutation replayed")
            writes.write_text("one durable effect")
            if crash_at == "persistence":
                # Lose the claim after dispatch: persistence must stop the loop.
                con = connect(config.paths.db)
                con.execute("UPDATE agent_turn_checkpoints SET claim_id='other-owner' WHERE session_id=?", (sid,))
                con.close()
            if crash_at in ("unknown", "approval"):
                return ToolResult.from_error(tool_use_id=call.id, name=call.name, error=ToolError(
                    kind=ToolErrorKind.PERMISSION_PENDING if crash_at == "approval" else ToolErrorKind.EXECUTION_ERROR,
                    message="unresolved", retryable=False,
                    detail={"execution_state": "unknown"} if crash_at == "unknown" else {}))
            result = ToolResult.from_text(tool_use_id=call.id, name=call.name, text="written")
            if crash_at == "pending_receipt":
                result = ToolResult.from_json(tool_use_id=call.id, name=call.name,
                    data={"status": "submitted"}, semantic_success=True)
            if crash_at == "interaction":
                result.result_protocol = "user_interaction_pending"
            return result
        kernel._registry.register(ToolDescriptor(name="fixture_write", description="fixture mutation",
            input_schema={"type": "object", "properties": {"operation": {"type": "string"}}}, handler=write,
            risk=RiskLevel.WRITE, read_only=False))
        return kernel.run_turn(trigger=_trigger("batch-event", "write once"), session_id=sid,
            turn_id=request.get("turn_id"), resume_turn_id=request.get("resume_turn_id"),
            continuation_feedback=request.get("continuation_feedback")).asdict()

    manager = CommandRuntime(cfg, execute, epoch="batch-before")
    manager.submit({"command_id": "batch-command", "session_id": sid,
        "request": {"payload": {"text": "write once"}}}, start=False)
    row = manager.store.claim(sid, "owner")
    pid = os.fork()
    if pid == 0:
        try:
            manager._run(row, "owner")
            if crash_at == "provider429":
                os._exit(0)
            os._exit(3)
        except BaseException:
            import traceback
            traceback.print_exc()
            os._exit(4)
    _, status = os.waitpid(pid, 0)
    assert os.waitstatus_to_exitcode(status) == 0
    assert writes.read_text() == "one durable effect"
    with manager.store.transaction() as con:
        con.execute("UPDATE agent_command_queues SET lease_until=0 WHERE session_id=?", (sid,))
    fresh = CommandRuntime(cfg, execute, epoch="batch-after")
    receipt = fresh.reconcile(sid, "batch-command")
    if crash_at not in ("next_model", "provider429") and not handled_error:
        assert receipt["command"]["state"] == "unconfirmed"
        return
    if crash_at == "provider429":
        assert receipt["command"]["state"] == "failed"
        assert receipt["command"]["error"]["status_code"] == 429
        con = connect(cfg.paths.db)
        try:
            assert AgentSessionRepository(con).peek_turn_checkpoint(sid) is not None
        finally:
            con.close()
        snapshot = fresh.store.snapshot(sid)
        assert snapshot["queue"]["paused"]
        assert snapshot["checkpoint"] == {"turn_id": row["turn_id"], "resumable": True, "resume_count": 0}
    else:
        assert receipt["reconciliation"]["source"] == "host_batch_checkpoint"
        assert receipt["command"]["state"] == "interrupted"
    con = connect(cfg.paths.db)
    try:
        cp = AgentSessionRepository(con).peek_turn_checkpoint(sid)
    finally:
        con.close()
    assert cp["claim_id"] is None
    assert cp["checkpoint"]["resumable"] is True
    assert cp["checkpoint"]["resume_block_reason"] == ""
    assert cp["checkpoint"]["iterations"] == 1 + handled_error
    assert cp["checkpoint"]["usage"]["llm_calls"] == 1 + handled_error
    resumed = fresh.submit({"command_id": "batch-resume", "session_id": sid, "command_type": "resume",
        "request": {"resume_turn_id": row["turn_id"], "continuation_feedback": "continue safely",
                    "payload": {}, **({"model_provider": "healthy-provider", "model_id": "healthy-model"}
                                      if crash_at == "provider429" else {})}}, start=False)
    resume_row = fresh.store.claim(sid, "resume-owner")
    assert resume_row["command_id"] == resumed["command"]["command_id"]
    fresh._run(resume_row, "resume-owner")
    resumed = fresh.store.snapshot(sid, "batch-resume")["command"]
    if crash_at == "provider429":
        assert resumed["state"] == "failed"
        assert resumed["error"]["status_code"] == 429
        assert fresh.store.snapshot(sid)["checkpoint"]["resumable"]
        retry = fresh.submit({"command_id": "batch-resume-retry", "session_id": sid, "command_type": "resume",
            "request": {"resume_turn_id": row["turn_id"], "continuation_feedback": "continue safely",
                        "payload": {}, "model_provider": "healthy-provider", "model_id": "healthy-model"}}, start=False)
        retry_row = fresh.store.claim(sid, "retry-owner")
        assert retry_row["command_id"] == retry["command"]["command_id"]
        fresh._run(retry_row, "retry-owner")
        resumed = fresh.store.snapshot(sid, "batch-resume-retry")["command"]
    assert resumed["state"] == "succeeded"
    assert resumed["result"]["final_text"] == "recovered"
    assert phase[0] == 2
    assert writes.read_text() == "one durable effect"


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


def test_failed_durable_resume_discards_checkpoint_and_prevents_replay(
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
    assert claimed is None

    with pytest.raises(
        TurnCheckpointResumeError,
        match="No durable checkpoint",
    ) as exc_info:
        AgentKernel(config=cfg, skills=None).run_turn(  # type: ignore[arg-type]
            trigger=_trigger("event-3"),
            session_id="session-failed-resume",
            resume_turn_id="turn-failed-resume",
            continuation_feedback="retry the same checkpoint",
        )
    assert exc_info.value.code == "turn_checkpoint_not_found"
    assert exc_info.value.status == 404

def test_large_checkpoint_persists_and_resumes_across_instances(tmp_path, monkeypatch):
    cfg = _config(tmp_path)
    cfg.data['agent']['native']['turn_checkpoint_max_bytes'] = 1024
    answer = '研究证据' * 200000
    calls = _install_scripted_gateway(monkeypatch, [_text_response(answer), _text_response('next')])
    kernel = AgentKernel(config=cfg, skills=None)
    first = kernel.run_turn(trigger=_trigger('large', 'research'), session_id='overflow', turn_id='large-turn')
    assert first.final_text == answer
    assert first.budget['checkpoint']['persisted'] is True
    assert first.budget['checkpoint']['resumable'] is True
    assert first.budget['checkpoint']['bytes'] > 2 * 1024 * 1024
    second = AgentKernel(config=cfg, skills=None).run_turn(
        trigger=_trigger('next'), session_id='overflow', resume_turn_id='large-turn',
        continuation_feedback='continue from the complete evidence')
    assert second.final_text == 'next'
    assert second.budget['checkpoint']['resume_count'] == 1
    assert answer in str(calls[-1])


def test_failed_turn_releases_only_its_own_lease(tmp_path, monkeypatch):
    cfg = _config(tmp_path)
    kernel = AgentKernel(config=cfg, skills=None)
    original = kernel._run
    def fail(**kwargs):
        raise RuntimeError('injected failure after lease acquisition')
    monkeypatch.setattr(kernel, '_run', fail)
    with pytest.raises(RuntimeError, match='injected failure'):
        kernel.run_turn(trigger=_trigger('fail'), session_id='failed-lease')
    monkeypatch.setattr(kernel, '_run', original)
    _install_scripted_gateway(monkeypatch, [_text_response('recovered')])
    result = kernel.run_turn(trigger=_trigger('recover'), session_id='failed-lease')
    assert result.final_text == 'recovered'


def test_provider_skill_load_compatibility_uses_canonical_tool(tmp_path, monkeypatch):
    from nerya.tools.native.skill import SkillIndex
    from nerya.tools.native.skill_tool import register_skill_tool
    skill = tmp_path / 'skills' / 'fixture'
    skill.mkdir(parents=True)
    (skill / 'SKILL.md').write_text('---\nname: fixture\ndescription: fixture\n---\nRead-only fixture instructions.')
    kernel = AgentKernel(config=_config(tmp_path), skills=None)
    kernel._ensure_registry()
    register_skill_tool(kernel._registry, skill_index=SkillIndex([skill.parent]), replace=True)
    calls = _install_scripted_gateway(monkeypatch, [
        MessagesResponse(content=[{'type': 'tool_use', 'id': 'skill-call', 'name': 'skill_load', 'input': {'skill': 'fixture'}}], stop_reason='tool_use'),
        _text_response('loaded'),
    ])
    result = kernel.run_turn(trigger=_trigger('skill', 'load fixture'), session_id='skill-alias')
    assert result.final_text == 'loaded'
    import json
    transcript = json.dumps(calls[-1])
    assert 'Read-only fixture instructions.' in transcript
    assert 'permission_denied' not in transcript
