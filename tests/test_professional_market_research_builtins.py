from __future__ import annotations

import pytest

from nerya.core.paths import WorkspacePaths
from nerya.skills.registry import SkillRegistry
from nerya.subagents.registry import (
    DEFAULT_SUBAGENT_PROMPTS,
    DEFAULT_SUBAGENT_SKILLS,
    describe_role,
)
from nerya.teams.templates import get_template, list_templates
from nerya.workspace.prompt_bundles import load_bundle


pytestmark = pytest.mark.smoke


def test_professional_market_research_skills_are_builtin() -> None:
    registry = SkillRegistry.load_builtin()
    ids = set(registry.by_id)

    assert {
        "market_data_routing",
        "market_research",
        "quant_research",
        "research_report",
    }.issubset(ids)

    report = registry.get("research_report").manifest
    assert "professional" in report.description.lower()
    assert "Rating" in report.instructions


def test_professional_subagents_have_prompts_and_skill_hints(tmp_path) -> None:
    paths = WorkspacePaths(root=tmp_path)
    role = describe_role(paths, "fundamentals_analyst")

    assert role is not None
    assert role["source"] == "default"
    assert "market_research" in role["allowed_skills"]
    assert "financial statements" in role["prompt"]

    for name in (
        "technical_analyst",
        "fundamentals_analyst",
        "sentiment_analyst",
        "macro_strategist",
        "quant_researcher",
        "bull_researcher",
        "bear_researcher",
        "research_manager",
        "research_editor",
    ):
        assert name in DEFAULT_SUBAGENT_SKILLS
        assert name in DEFAULT_SUBAGENT_PROMPTS


def test_prompt_bundle_seeds_professional_research_subagents() -> None:
    bundle = load_bundle("default")

    assert "technical_analyst" in bundle.subagents
    assert "fundamentals_analyst" in bundle.subagents
    assert "research_manager" in bundle.subagents
    assert "research_report" in bundle.subagents["research_editor"]


def test_research_team_templates_use_specialist_roles() -> None:
    templates = {row["id"] for row in list_templates()}
    assert "market_analysis_team" in templates
    assert "investment_committee_team" in templates

    market = get_template("market_analysis_team")
    assert market is not None
    market_roles = {member.subagent_name for member in market.members}
    assert {"technical_analyst", "sentiment_analyst", "research_manager"} <= market_roles

    committee = get_template("investment_committee_team")
    assert committee is not None
    committee_roles = {member.subagent_name for member in committee.members}
    assert {"bull_researcher", "bear_researcher", "risk_critic", "research_manager"} <= committee_roles
