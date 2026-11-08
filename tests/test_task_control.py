from __future__ import annotations


import pytest

from nerya.agent.task_control import (
    assignment_ended,
    assignment_for,
    assignment_started,
    create_mandate,
    handback,
    input_receipt,
    mark_input,
    public_control,
    coordination_instruction,
    role_plan_for,
    worker_report,
)
from nerya.core.paths import WorkspacePaths


pytestmark = pytest.mark.smoke


def test_control_plane_persists_mandate_assignment_report_and_handback(tmp_path):
    paths = WorkspacePaths(tmp_path)
    mandate = create_mandate(paths, task_id="awork_one", command_id="cmd_one", session_id="task_one",
                             actor_id="owner", task_kind="strategy", objective="Research BTC", scope={"paper_only": True},
                             allowed_tools=["research"], execution_mode="paper")
    assignment = assignment_for(paths, task_id=mandate["task_id"], session_id="task_one", command_id="cmd_one")
    assignment_started(paths, assignment["assignment_id"], worker_owner="worker")
    input_row = input_receipt(paths, task_id="awork_one", assignment_id=assignment["assignment_id"], command_id="cmd_one",
                              sender_session_id="assistant_one", body="Research BTC")
    assert mark_input(paths, input_row["input_id"])["state"] == "read"
    report = worker_report(paths, task_id="awork_one", assignment_id=assignment["assignment_id"], command_id="cmd_one",
                           payload={"status": "ready", "summary": "Drafted a paper-only candidate", "evidence_refs": ["report.md"]})
    returned = handback(paths, task_id="awork_one", assignment_id=assignment["assignment_id"], command_id="cmd_one",
                        request="Need the user to choose a benchmark window", reason="The requested window is ambiguous")
    assignment_ended(paths, assignment["assignment_id"], state="waiting")
    control = public_control(paths, "awork_one")
    assert control["mandate"]["scope"] == {"paper_only": True}
    assert control["assignment"]["state"] == "waiting"
    assert control["report"]["report_id"] == report["report_id"]
    assert control["open_handbacks"][0]["handback_id"] == returned["handback_id"]
    assert control["pending_inputs"] == []


def test_research_and_strategy_tasks_use_fixed_team_roles():
    plan = role_plan_for("strategy")
    assert [row["persona"] for row in plan] == [
        "market_analyst", "risk_critic", "strategy_reviewer", "quant_researcher", "research_manager"
    ]
    instruction = coordination_instruction("research")
    assert "team_run" in instruction and "paper/proposal-only" in instruction and "task_handback" in instruction


@pytest.mark.parametrize("payload", [
    {"status": "waiting", "summary": "Need a choice"},
    {"status": "blocked", "summary": ""},
])
def test_worker_report_requires_structured_blocker_or_question(tmp_path, payload):
    paths = WorkspacePaths(tmp_path)
    create_mandate(paths, task_id="task", command_id="cmd", session_id="session", actor_id="owner",
                   task_kind="research", objective="Research", scope={}, allowed_tools=[])
    assignment = assignment_for(paths, task_id="task", session_id="session", command_id="cmd")
    with pytest.raises(ValueError):
        worker_report(paths, task_id="task", assignment_id=assignment["assignment_id"], command_id="cmd", payload=payload)
