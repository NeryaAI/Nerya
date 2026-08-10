from __future__ import annotations

import copy
import threading
import time

import pytest

from nerya.core.config import Config
from nerya.core.paths import WorkspacePaths
from nerya.agent.streaming import get_default_bus
from nerya.harness.cancellation import CancelToken
from nerya.subagents.dispatcher import SubAgentResult
from nerya.teams.models import TeamGateSpec
from nerya.teams.orchestrator import TeamOrchestrator, TeamRunRequest


def test_team_orchestrator_synthesizes_completed_status(tmp_path, monkeypatch) -> None:
    bus = get_default_bus()
    bus.clear()

    class FakeDispatcher:
        def __init__(self, *_args, **_kwargs) -> None:
            pass

        def _run_one(self, name, *, payload, **_kwargs):
            output = {
                "summary": f"{payload['task_id']} done",
                "signal": "neutral",
                "confidence": 0.8,
                "evidence": [{"summary": "stub evidence", "source": "test"}],
                "risks": ["stub risk"] if payload["task_id"] == "t-risk" else [],
                "done": True,
            }
            if payload["task_id"] == "t-report":
                output["report_markdown"] = "# Full committee report\n\nComplete report body."
            return SubAgentResult(
                ok=True,
                subagent=name,
                output=output,
            )

    monkeypatch.setattr("nerya.teams.orchestrator.SubAgentDispatcher", FakeDispatcher)
    cfg = Config(paths=WorkspacePaths(root=tmp_path), data={})
    orchestrator = TeamOrchestrator(config=cfg, skills=object())

    result = orchestrator.run(
        template="investment_committee_team",
        goal="test completed status",
    )

    assert result.status == "completed"
    assert result.phase == "close"
    assert result.final_context["status"] == "completed"
    assert result.final_context["phase"] == "close"
    assert "Status: completed" in (result.final_report_excerpt or "")
    assert "Full committee report" in (result.final_report_excerpt or "")

    events = [e for e in bus.recent() if e["kind"] == "team.event"]
    event_kinds = {str(e.get("team_event_kind")) for e in events}
    assert {
        "run.created",
        "run.updated",
        "task.created",
        "task.updated",
        "blackboard.appended",
        "message.sent",
        "artifact.written",
        "synthesis.written",
        "run.completed",
    } <= event_kinds
    assert any(e.get("task_id") == "t-risk" for e in events)
    assert any(e.get("content") for e in events if e.get("team_event_kind") == "message.sent")
    assert result.final_context["signal_distribution"].get("neutral", 0) > 0


def test_unknown_team_gate_blocks_terminal_status(tmp_path, monkeypatch) -> None:
    class FakeDispatcher:
        def __init__(self, *_args, **_kwargs) -> None:
            pass

        def _run_one(self, name, *, payload, **_kwargs):
            return SubAgentResult(
                ok=True,
                subagent=name,
                output={"summary": "done", "signal": "neutral", "done": True},
            )

    monkeypatch.setattr("nerya.teams.orchestrator.SubAgentDispatcher", FakeDispatcher)
    cfg = Config(paths=WorkspacePaths(root=tmp_path), data={})
    orchestrator = TeamOrchestrator(config=cfg, skills=object())
    template = copy.deepcopy(orchestrator._resolve_template("investment_committee_team"))
    template.gates.append(TeamGateSpec(id="future", kind="not_yet_implemented"))

    result = orchestrator.run(template=template, goal="unknown gate must fail closed")

    assert result.status == "blocked"
    assert result.phase == "close"
    assert result.final_context["status"] == "blocked"
    assert any(
        gate["gate_id"] == "future" and not gate["ok"]
        for gate in result.final_context["gates"]
    )


def test_team_request_propagates_executor_and_cancel_token(tmp_path) -> None:
    seen: dict[str, object] = {}

    class Token:
        is_set = False
        reason = ""

    class FakeDispatcher:
        def __init__(self, *_args, **_kwargs) -> None:
            pass

        def dispatch(self, target, *, payload, trigger_event_id, strategy_id,
                     session_id, turn_id=None, parent_call_id=None,
                     inline_spec=None, cancel_token=None):
            seen["cancel_token"] = cancel_token
            return {
                "ok": True,
                "subagent": target.split(":", 1)[1],
                "output": {"summary": "done", "done": True},
            }

    cfg = Config(paths=WorkspacePaths(root=tmp_path), data={})
    dispatcher = FakeDispatcher()
    executor = object()
    token = Token()
    orchestrator = TeamOrchestrator(
        config=cfg,
        skills=object(),
        dispatcher=dispatcher,
        executor=executor,
    )

    result = orchestrator.run_request(
        TeamRunRequest(
            task="test request",
            roles=["analyst"],
            cancel_token=token,
            executor=executor,
        )
    )

    assert result.status == "completed"
    assert seen["cancel_token"] is token
    assert dispatcher.executor is executor


def test_team_request_cancelled_before_dispatch(tmp_path) -> None:
    calls: list[str] = []

    class Token:
        is_set = True
        reason = "operator_stop"

    class FakeDispatcher:
        def __init__(self, *_args, **_kwargs) -> None:
            pass

        def dispatch(self, target, **_kwargs):
            calls.append(target)
            raise AssertionError("cancelled team must not dispatch members")

    cfg = Config(paths=WorkspacePaths(root=tmp_path), data={})
    result = TeamOrchestrator(
        config=cfg,
        skills=object(),
        dispatcher=FakeDispatcher(),
    ).run_request(
        TeamRunRequest(
            task="cancel before scheduling",
            roles=["analyst"],
            cancel_token=Token(),
        )
    )

    assert result.status == "cancelled"
    assert result.error == "operator_stop"
    assert calls == []


@pytest.mark.smoke
@pytest.mark.parametrize(
    ("stop_mode", "expected_reason"),
    [("timeout", "team_timeout"), ("cancel", "operator_stop")],
)
def test_team_request_stop_does_not_wait_for_running_member(
    tmp_path,
    stop_mode: str,
    expected_reason: str,
) -> None:
    release = threading.Event()
    slow_started = threading.Event()
    slow_finished = threading.Event()
    token = CancelToken()

    class FakeDispatcher:
        def dispatch(self, target, **_kwargs):
            if target == "subagent:slow":
                slow_started.set()
                try:
                    release.wait(2.0)
                finally:
                    slow_finished.set()
            else:
                assert slow_started.wait(1.0)
                if stop_mode == "cancel":
                    token.cancel("operator_stop")
            return {
                "ok": True,
                "subagent": target.split(":", 1)[1],
                "output": {"summary": "done", "done": True},
            }

    request = TeamRunRequest(
        task="stop promptly",
        roles=["slow", "fast"],
        max_parallel=2,
        timeout_s=0.05 if stop_mode == "timeout" else 5.0,
        cancel_token=token,
    )
    started = time.monotonic()
    result = TeamOrchestrator(
        config=Config(paths=WorkspacePaths(root=tmp_path), data={}),
        skills=object(),
        dispatcher=FakeDispatcher(),
    ).run_request(request)
    elapsed = time.monotonic() - started
    release.set()

    assert slow_finished.wait(1.0)
    assert elapsed < 0.75
    assert result.status == "cancelled"
    assert result.error == expected_reason
    slow_task = next(task for task in result.tasks if task["owner"] == "slow")
    assert slow_task["status"] == "cancelled"
