from __future__ import annotations

from copy import deepcopy

import pytest

from nerya.agent.kernel import AgentKernel
from nerya.core.config import Config, DEFAULT_CONFIG
from nerya.core.paths import WorkspacePaths
from nerya.skills.kernel import SkillKernel
from nerya.skills.registry import SkillRegistry
from nerya.tools.native.skill import SkillIndex, index_skills
from nerya.tools.native.skill_tool import skill_tool_handler
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


def test_market_research_description_covers_stock_and_token_analysis(tmp_path) -> None:
    cfg = Config(paths=WorkspacePaths(root=tmp_path), data=deepcopy(DEFAULT_CONFIG))
    kernel = AgentKernel(config=cfg, skills=None)  # type: ignore[arg-type]
    description = kernel._ensure_registry().skill_index.get("market_research").description

    assert "stock" in description
    assert "crypto-token" in description
    assert "tokenomics" in description
    assert "on-chain" in description


def test_viewing_team_skill_reveals_team_run(tmp_path) -> None:
    cfg = Config(paths=WorkspacePaths(root=tmp_path), data=deepcopy(DEFAULT_CONFIG))
    kernel = AgentKernel(config=cfg, skills=SkillKernel.boot(cfg))
    deps = kernel._ensure_registry()
    registry = kernel._registry
    state = registry.lazy_mcp_state

    assert registry.find("team_run") is not None
    before = {tool.name for tool in registry.list_tools() if state.is_visible(tool)}
    result = registry.get("skill_view").handler(
        ToolCall(name="skill_view", arguments={"skill_id": "team"})
    )
    after = {tool.name for tool in registry.list_tools() if state.is_visible(tool)}

    assert deps.skill_index.get("team") is not None
    assert not result.is_error
    assert "team_run" not in before
    assert "team_run" in after


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
