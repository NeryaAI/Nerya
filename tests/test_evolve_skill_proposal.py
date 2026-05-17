from __future__ import annotations

from pathlib import Path

import pytest

from nerya.core.paths import WorkspacePaths
from nerya.evolution.patch_proposal import list_proposals
from nerya.evolution.skill_proposal import propose_skill_from_workflow
from nerya.skills.builtin.evolve.scripts.propose_skill import run as script_run
from nerya.skills.manifest import SkillManifest


pytestmark = pytest.mark.smoke


def test_workflow_to_skill_proposal_writes_apply_ready_skill(tmp_path: Path) -> None:
    paths = WorkspacePaths(root=tmp_path)

    result = propose_skill_from_workflow(
        paths,
        name="Backtest Evidence Review",
        description="Use after a strategy backtest needs a repeatable evidence review.",
        workflow=[
            "Read the proposal or strategy package first.",
            "Run the narrow backtest command and capture metrics.",
            "Summarize pass/fail evidence and unresolved risk.",
        ],
        triggers=["strategy backtest review", "operator asks for evidence"],
        evidence_refs=["tests/test_backtest_skill.py"],
    )

    assert result["ok"] is True
    assert result["skill_id"] == "backtest_evidence_review"
    proposal = list_proposals(paths)[0]
    assert proposal.kind == "skill_proposal"
    assert proposal.state == "pending_review"
    assert proposal.target == "skills/backtest_evidence_review/SKILL.md"

    skill_md = proposal.path / "after" / "skills" / "backtest_evidence_review" / "SKILL.md"
    parsed = SkillManifest.from_skill_md(skill_md)
    assert parsed.id == "backtest_evidence_review"
    assert "Run the narrow backtest command" in parsed.instructions


def test_evolve_propose_skill_script_accepts_workspace_payload(tmp_path: Path) -> None:
    result = script_run(
        workspace=str(tmp_path),
        name="Daily Ops Sweep",
        description="Use for repeated daily operator checks.",
        workflow="Check health endpoints\nReview pending approvals\nReport blockers",
    )

    assert result["ok"] is True
    assert result["skill_id"] == "daily_ops_sweep"
    proposal = list_proposals(WorkspacePaths(root=tmp_path))[0]
    assert (proposal.path / "after" / "skills" / "daily_ops_sweep" / "SKILL.md").exists()
