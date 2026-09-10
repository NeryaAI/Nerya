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

        @classmethod
        def for_workspace(cls, config, skills):
            return cls()

        def dispatch(self, target, *, payload, **_kwargs):
            name = target.split(":", 1)[1]
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
            ).asdict()

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

        @classmethod
        def for_workspace(cls, config, skills):
            return cls()

        def dispatch(self, target, *, payload, **_kwargs):
            name = target.split(":", 1)[1]
            return SubAgentResult(
                ok=True,
                subagent=name,
                output={"summary": "done", "signal": "neutral", "done": True},
            ).asdict()

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
                     inline_spec=None, cancel_token=None, **kwargs):
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
    assert orchestrator.executor is executor


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


def _dependency_template(dependencies):
    from nerya.teams.models import TeamMemberSpec, TeamTaskSpec, TeamTemplate
    return TeamTemplate(
        id="dependency-test", description="scheduler contract", lead="lead",
        members=[TeamMemberSpec(name=name, role=name, subagent_name=name) for name in dependencies],
        tasks=[TeamTaskSpec(id=name, owner=name, subagent_name=name, subject=name, depends_on=deps)
               for name, deps in dependencies.items()], max_parallel=2,
    )


@pytest.mark.smoke
def test_ready_descendant_does_not_wait_for_unrelated_slow_member(tmp_path):
    descendant_ran = threading.Event()
    slow_saw_descendant = []

    class Dispatcher:
        def dispatch(self, target, **kwargs):
            name = target.split(":", 1)[1]
            if name == "slow":
                slow_saw_descendant.append(descendant_ran.wait(2.0))
            elif name == "descendant":
                descendant_ran.set()
            return SubAgentResult(ok=True, subagent=name, output={"done": True, "summary": name}).asdict()

    orchestrator = TeamOrchestrator(config=Config(paths=WorkspacePaths(tmp_path)), skills=object(), dispatcher=Dispatcher())
    try:
        result = orchestrator.run(
            template=_dependency_template({"fast": [], "slow": [], "descendant": ["fast"]}),
            goal="run ready dependencies without global wave barriers",
        )
    finally:
        descendant_ran.set()
    assert result.status == "completed"
    assert slow_saw_descendant == [True]


@pytest.mark.smoke
@pytest.mark.parametrize("dependencies", [
    {"leaf": ["middle"], "middle": ["root"], "root": []},
    {"first": ["second"], "second": ["first"]},
    {"first": ["missing"]},
])
def test_unsatisfied_dependencies_have_explicit_terminal_status(tmp_path, dependencies):
    class Dispatcher:
        def dispatch(self, target, **kwargs):
            return SubAgentResult(ok=False, subagent=target, error="source failed").asdict()

    result = TeamOrchestrator(
        config=Config(paths=WorkspacePaths(tmp_path)), skills=object(), dispatcher=Dispatcher(),
    ).run(template=_dependency_template(dependencies), goal="show dependency blockers")
    assert result.status == "blocked"
    assert all(task["status"] in {"failed", "blocked"} for task in result.tasks)


@pytest.mark.smoke
def test_cancelled_request_does_not_poison_next_run(tmp_path):
    class Dispatcher:
        def dispatch(self, target, **kwargs):
            return SubAgentResult(ok=True, subagent=target, output={"done": True, "summary": "done"}).asdict()

    orchestrator = TeamOrchestrator(config=Config(paths=WorkspacePaths(tmp_path)), skills=object(), dispatcher=Dispatcher())
    token = CancelToken()
    token.cancel("old_request")
    first = orchestrator.run_request(TeamRunRequest(task="cancelled", roles=["analyst"], cancel_token=token))
    second = orchestrator.run(template=_dependency_template({"analyst": []}), goal="new request")
    assert first.status == "cancelled"
    assert second.status == "completed"


@pytest.mark.smoke
@pytest.mark.parametrize("timeout", [-1.0, float("nan"), float("inf"), "invalid"])
def test_invalid_team_timeout_is_rejected(timeout):
    with pytest.raises(ValueError, match="timeout_s"):
        TeamRunRequest(task="bounded task", timeout_s=timeout)


@pytest.mark.smoke
def test_zero_team_timeout_never_dispatches(tmp_path):
    calls = []
    class Dispatcher:
        def dispatch(self, target, **kwargs):
            calls.append(target)
            return SubAgentResult(ok=True, subagent=target, output={"done": True}).asdict()

    result = TeamOrchestrator(config=Config(paths=WorkspacePaths(tmp_path)), skills=object(),
                              dispatcher=Dispatcher()).run_request(
        TeamRunRequest(task="already expired", roles=["analyst"], timeout_s=0.0),
    )
    assert calls == []
    assert result.status == "cancelled"
    assert result.error == "team_timeout"


@pytest.mark.smoke
def test_task_identity_and_template_policy_survive_payload_overrides(tmp_path):
    seen = []
    class Dispatcher:
        def dispatch(self, target, **kwargs):
            seen.append(kwargs)
            return SubAgentResult(ok=True, subagent=target, output={"done": True, "summary": "checked"}).asdict()

    template = _dependency_template({"analyst": []})
    template.members[0].execution_policy = {"native_tools": {"allow": []}, "max_skill_calls": 0}
    result = TeamOrchestrator(config=Config(paths=WorkspacePaths(tmp_path)), skills=object(),
                              dispatcher=Dispatcher()).run_request(TeamRunRequest(
        task="caller goal", template=template,
        shared_payload={"team_run_id": "wrong-run", "task_id": "wrong-task", "input": "useful data"},
        role_payloads={"analyst": {"task_owner": "wrong-owner", "team_goal": "wrong-goal"}},
    ))
    payload = seen[0]["payload"]
    assert payload["team_run_id"] == result.run_id
    assert payload["task_id"] == payload["task_owner"] == "analyst"
    assert payload["team_goal"] == "caller goal"
    assert payload["input"] == "useful data"
    assert seen[0]["inline_spec"].execution_policy.max_skill_calls == 0
    assert seen[0]["inline_spec"].execution_policy.native_tool_allow == []
    assert "signal_calls" not in payload["instruction"]


@pytest.mark.smoke
def test_duplicate_task_ids_are_rejected_before_dispatch(tmp_path):
    template = _dependency_template({"first": [], "second": []})
    template.tasks[1].id = "first"
    from types import SimpleNamespace
    dispatcher = SimpleNamespace(dispatch=lambda *args, **kwargs: pytest.fail("must not dispatch"))
    orchestrator = TeamOrchestrator(config=Config(paths=WorkspacePaths(tmp_path)), skills=object(), dispatcher=dispatcher)
    with pytest.raises(ValueError, match="task.*unique"):
        orchestrator.run(template=template, goal="invalid identity")


@pytest.mark.smoke
def test_explicit_template_honors_request_parallel_limit(tmp_path, monkeypatch):
    from nerya.teams import orchestrator as module
    from concurrent.futures import ThreadPoolExecutor
    workers = []
    def pool(*, max_workers):
        workers.append(max_workers)
        return ThreadPoolExecutor(max_workers=max_workers)
    monkeypatch.setattr(module, "ThreadPoolExecutor", pool)
    class Dispatcher:
        def dispatch(self, target, **kwargs):
            return SubAgentResult(ok=True, subagent=target, output={"done": True, "summary": "checked"}).asdict()
    result = TeamOrchestrator(config=Config(paths=WorkspacePaths(tmp_path)), skills=object(),
                              dispatcher=Dispatcher()).run_request(TeamRunRequest(
        task="serial request", template=_dependency_template({"first": [], "second": []}), max_parallel=1,
    ))
    assert result.status == "completed"
    assert workers == [1]
