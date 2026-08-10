from __future__ import annotations

from copy import deepcopy

import pytest

from nerya.agent.kernel import AgentKernel
from nerya.core.config import Config, DEFAULT_CONFIG
from nerya.core.paths import WorkspacePaths
from nerya.skills.kernel import SkillKernel
from nerya.skills.registry import SkillRegistry
from nerya.tools.native.skill import SkillIndex, index_skills
from nerya.tools.native.skill_tool import SKILL_TOOL_DESCRIPTION, skill_tool_handler
from nerya.tools.types import ToolCall


pytestmark = pytest.mark.smoke


def test_team_skill_uses_standard_description_for_activation(tmp_path) -> None:
    cfg = Config(paths=WorkspacePaths(root=tmp_path), data=deepcopy(DEFAULT_CONFIG))
    kernel = AgentKernel(config=cfg, skills=None)  # type: ignore[arg-type]
    deps = kernel._ensure_registry()
    record = deps.skill_index.get("team")

    assert record is not None
    assert "asks to start or launch an Agent Team" in record.description
    assert "triggers" not in record.asdict()
    assert "required_tools" not in record.asdict()
    assert "title" not in record.asdict()
    assert "tags" not in record.asdict()
    assert "permissions" not in record.asdict()
    assert record.description in deps.skill_index.render_for_prompt()


def test_team_skill_puts_explicit_launch_before_discovery_or_research(tmp_path) -> None:
    cfg = Config(paths=WorkspacePaths(root=tmp_path), data=deepcopy(DEFAULT_CONFIG))
    kernel = AgentKernel(config=cfg, skills=None)  # type: ignore[arg-type]
    entry = SkillRegistry.load_builtin().get("team")
    body = entry.manifest.instructions
    playbook = (entry.manifest.path / "references" / "full-playbook.md").read_text(
        encoding="utf-8"
    )

    assert "first tool action after loading this skill must be `team_run`" in body
    assert "Do not spend" in body and "the tool budget on `role_list`" in body
    assert "call `team_run` directly" in playbook


def test_market_research_description_covers_stock_and_token_analysis(tmp_path) -> None:
    cfg = Config(paths=WorkspacePaths(root=tmp_path), data=deepcopy(DEFAULT_CONFIG))
    kernel = AgentKernel(config=cfg, skills=None)  # type: ignore[arg-type]
    description = kernel._ensure_registry().skill_index.get("market_research").description

    assert "stock" in description
    assert "crypto-token" in description
    assert "tokenomics" in description
    assert "on-chain" in description


def test_research_skills_do_not_force_approval_gated_scripts() -> None:
    registry = SkillRegistry.load_builtin()
    market_research = registry.get("market_research").manifest.instructions
    news_social = registry.get("news_social").manifest.instructions
    markets = registry.get("markets").manifest.instructions

    assert "CALL `research_run` once" in market_research
    assert "CALL `research_run` once" in news_social
    assert "script_run" not in news_social
    assert "READ quotes, candles, and computed features through native `market_data`" in markets


def test_native_tools_are_visible_without_loading_a_skill(tmp_path) -> None:
    cfg = Config(paths=WorkspacePaths(root=tmp_path), data=deepcopy(DEFAULT_CONFIG))
    kernel = AgentKernel(config=cfg, skills=SkillKernel.boot(cfg))
    deps = kernel._ensure_registry()
    registry = kernel._registry
    assert deps.skill_index.get("team") is not None
    team_run = registry.find("team_run")
    assert team_run is not None
    assert "Do not prefetch market or research data" in team_run.description
    assert registry.find("research_run") is not None
    assert registry.find("strategy_draft_proposal") is not None


def test_skill_tool_strips_marked_frontmatter(tmp_path) -> None:
    cfg = Config(paths=WorkspacePaths(root=tmp_path), data=deepcopy(DEFAULT_CONFIG))
    kernel = AgentKernel(config=cfg, skills=None)  # type: ignore[arg-type]
    kernel._ensure_registry()
    registry = kernel._registry
    result = registry.get("Skill").handler(
        ToolCall(name="Skill", arguments={"skill": "team"})
    )

    assert not result.is_error
    assert "nerya-skill-frontmatter-start" not in result.text()
    assert "# Team" in result.text()
    assert registry.find("skill") is None


def test_skill_tool_description_forbids_calling_catalog_names_as_tools() -> None:
    assert "Catalog skill names are not callable tools" in SKILL_TOOL_DESCRIPTION


def test_skill_index_ignores_nonstandard_id_field(tmp_path) -> None:
    skill_dir = tmp_path / "standard-name"
    skill_dir.mkdir()
    (skill_dir / "SKILL.md").write_text(
        "---\nname: standard-name\nid: legacy-route\ndescription: Demo\n---\n# Demo\n",
        encoding="utf-8",
    )

    records = index_skills([tmp_path])

    assert [record.skill_id for record in records] == ["standard-name"]


def test_minimal_standard_skill_loads_exactly_without_routing_fields(tmp_path) -> None:
    skill_dir = tmp_path / "stock-analysis"
    skill_dir.mkdir()
    description = "Analyze a listed company; use this for stock research."
    (skill_dir / "SKILL.md").write_text(
        f"---\nname: stock-analysis\ndescription: {description}\n---\n# Stock Analysis\n",
        encoding="utf-8",
    )
    index = SkillIndex([tmp_path])

    result = skill_tool_handler(
        ToolCall(name="Skill", arguments={"skill": "stock-analysis"}),
        skill_index=index,
    )
    near_miss = skill_tool_handler(
        ToolCall(name="Skill", arguments={"skill": "stock"}),
        skill_index=index,
    )

    assert description in index.render_for_prompt()
    assert not result.is_error
    assert "# Stock Analysis" in result.text()
    assert "description:" not in result.text()
    payload = next(part.data for part in result.content if part.type == "json")
    assert "scripts" not in payload
    assert "has_scripts" not in payload
    assert near_miss.is_error


def test_skill_prompt_keeps_full_description_for_precise_selection(tmp_path) -> None:
    skill_dir = tmp_path / "long-description"
    skill_dir.mkdir()
    discriminator = "Use specifically for pre-earnings scenario analysis."
    description = f"{'General financial analysis. ' * 12}{discriminator}"
    (skill_dir / "SKILL.md").write_text(
        f"---\nname: long-description\ndescription: {description}\n---\n# Long\n",
        encoding="utf-8",
    )

    rendered = SkillIndex([tmp_path]).render_for_prompt()

    assert len(description) > 240
    assert discriminator in rendered


def test_every_builtin_skill_loads_by_its_exact_standard_name() -> None:
    entries = SkillRegistry.load_builtin().list()
    index = SkillIndex(
        [],
        skill_files=[entry.manifest.path / "SKILL.md" for entry in entries],
    )
    records = index.records()

    assert len(records) == len(entries)
    for record in records:
        result = skill_tool_handler(
            ToolCall(name="Skill", arguments={"skill": record.skill_id}),
            skill_index=index,
        )
        payloads = [part.data for part in result.content if part.type == "json"]
        assert not result.is_error, record.skill_id
        assert payloads[0]["skill_id"] == record.skill_id
        assert "nerya-skill-frontmatter-start" not in result.text()
