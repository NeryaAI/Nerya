from __future__ import annotations

from nerya.core.config import Config
from nerya.core.paths import WorkspacePaths
from nerya.agent.streaming import get_default_bus
from nerya.subagents.dispatcher import SubAgentResult
from nerya.teams.orchestrator import TeamOrchestrator


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
