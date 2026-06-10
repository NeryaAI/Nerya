from __future__ import annotations

from pathlib import Path

import pytest

from nerya.core.paths import WorkspacePaths
from nerya.core.config import Config
from nerya.evolution.patch_proposal import list_proposals
from nerya.evolution.skill_proposal import propose_skill_from_workflow
from nerya.skills.proposal import scaffold as scaffold_legacy_skill_proposal
from nerya.skills.builtin.evolve.scripts.propose_skill import run as script_run
from nerya.skills.manifest import SkillManifest
from nerya.tools.native.evolve import evolve_provider_proposal_handler
from nerya.tools.types import ToolCall


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


def test_legacy_skill_scaffolder_does_not_create_executable_action_surface(tmp_path: Path) -> None:
    target = scaffold_legacy_skill_proposal(
        tmp_path,
        "daily_ops_sweep",
        {
            "name": "Daily Ops Sweep",
            "description": "Use for repeated daily operator checks.",
            "version": "0.1.0",
        },
        actions_py="print('legacy executable surface must not be written')\n",
    )

    assert (target / "SKILL.md").exists()
    assert (target / "references").is_dir()
    assert (target / "scripts").is_dir()
    assert (target / "templates").is_dir()
    for legacy_name in (
        "actions.py",
        "skill.yml",
        "skill.yaml",
        "manifest.yml",
        "manifest.yaml",
    ):
        assert not (target / legacy_name).exists()

    skill_md = (target / "SKILL.md").read_text(encoding="utf-8")
    assert "actions.py" not in skill_md


def test_provider_proposal_tool_writes_reviewable_provider_artifact(tmp_path: Path) -> None:
    cfg = Config(paths=WorkspacePaths(root=tmp_path), data={})

    result = evolve_provider_proposal_handler(
        ToolCall(
            name="evolve_provider_proposal",
            arguments={
                "venue": "aster",
                "label": "Aster DEX Perpetual",
                "kind": "dex",
                "runtime": "custom_http",
                "base_url": "https://fapi.asterdex.com",
                "docs_url": "https://docs.asterdex.com/",
                "auth": "EIP-712 Agent Key",
                "evidence_refs": ["https://docs.asterdex.com/"],
            },
            id="toolu_provider",
        ),
        config=cfg,
    )

    assert not result.is_error
    proposal = list_proposals(cfg.paths)[0]
    assert proposal.kind == "provider_proposal"
    assert proposal.state == "pending_review"
    assert proposal.metadata is not None
    assert proposal.metadata["venue"] == "aster"
    assert proposal.metadata["base_url"] == "https://fapi.asterdex.com"
    assert proposal.metadata["auth"] == "EIP-712 Agent Key"
    assert (proposal.path / "after" / "providers" / "aster" / "provider.md").exists()
