from __future__ import annotations

import pytest

from nerya.skills.discovery import rank_skill_metadata
from nerya.tools.native.skill import SkillIndex
from nerya.tools.native.skill_tool import skill_tool_handler
from nerya.tools.types import ToolCall

pytestmark = pytest.mark.smoke


def fixture(tmp_path):
    for name, description in [("research", "Market research 投研"),
                              ("research.validation", "Out-of-sample checks 样本外验证"),
                              ("trading", "Submit trades 下单交易")]:
        path = tmp_path / name / "SKILL.md"
        path.parent.mkdir()
        parent = "metadata:\n  nerya:\n    catalog_parent: research\n" if name.startswith("research.") else ""
        path.write_text(f"---\nname: {name}\ndescription: {description}\n{parent}---\nSECRET_BODY\n")
    return SkillIndex([tmp_path])


def search(index, query, **values):
    return skill_tool_handler(ToolCall(name="Skill", arguments={"action": "search", "query": query, **values}), skill_index=index)


def test_exact_names_rank_first_and_zero_match_returns_empty():
    assert rank_skill_metadata("backtest", [("backtest", "Replay"), ("analysis", "Discuss backtest")])[0]["skill_id"] == "backtest"
    assert rank_skill_metadata("volcano", [("backtest", "Replay")]) == []


def test_chinese_mixed_queries_recall_nested_methods_without_loading_body(tmp_path):
    index = fixture(tmp_path)
    result = search(index, "请帮我做样本外验证 out-of-sample")
    data = next(part.data for part in result.content if part.type == "json")
    assert data["skills"][0]["skill_id"] == "research.validation"
    assert data["skills"][0]["workflow"] == "research"
    assert "SECRET_BODY" not in result.text()


def test_role_filter_precedes_ranking_and_never_grants_parent(tmp_path):
    index = fixture(tmp_path)
    call = ToolCall(name="Skill", arguments={"action": "search", "query": "样本外验证 research 下单交易"},
                    metadata={"allowed_skills": ["research.validation"]})
    data = skill_tool_handler(call, skill_index=index).content[0].data
    assert [row["skill_id"] for row in data["skills"]] == ["research.validation"]
    assert data["skills"][0]["workflow"] == "research.validation"


@pytest.mark.parametrize("query,limit", [("", 1), (" ", 1), (None, 1), ("x" * 513, 1), ("research", True), ("research", 0), ("research", 21)])
def test_search_argument_bounds(tmp_path, query, limit):
    assert search(fixture(tmp_path), query, limit=limit).is_error


def test_refresh_removes_deleted_entries(tmp_path):
    index = fixture(tmp_path)
    assert index.search("样本外")
    (tmp_path / "research.validation" / "SKILL.md").unlink()
    assert not index.search("样本外", refresh=True)
