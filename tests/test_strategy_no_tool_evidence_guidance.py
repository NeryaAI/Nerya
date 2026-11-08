"""Offline Skill-loading contract; does not certify model behavior or execution."""

from pathlib import Path
import shutil

import pytest

from nerya.tools.native.skill import SkillIndex
from nerya.tools.native.skill_tool import skill_tool_handler
from nerya.tools.types import ToolCall


pytestmark = pytest.mark.smoke
SKILL = (
    Path(__file__).resolve().parents[1]
    / "nerya/skills/builtin/strategy_author/SKILL.md"
)


@pytest.fixture
def guidance(tmp_path, monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("NETWORK_FORBIDDEN")

    monkeypatch.setattr("socket.socket.connect", forbidden)
    monkeypatch.setattr("socket.create_connection", forbidden)
    skill_dir = tmp_path / "skills/strategy_author"
    skill_dir.mkdir(parents=True)
    shutil.copyfile(SKILL, skill_dir / "SKILL.md")
    result = skill_tool_handler(
        ToolCall(name="Skill", arguments={"skill": "strategy_author"}),
        skill_index=SkillIndex([tmp_path / "skills"]),
    )
    assert not result.is_error, result.text()
    section = result.text().split("## Current-turn evidence and recovery\n", 1)[1]
    return " ".join(section.split("\n## ", 1)[0].split())


def test_current_turn_claims_require_current_results_not_history(guidance):
    for requirement in (
        "actual current-turn tool result and the matching candidate/source revision",
        "Historical tool receipts retain their original turn and revision",
        "reconcile them before reuse and label them as prior evidence",
        "Assistant prose, copied tool-call text and unfinished arguments are not execution receipts",
        "report no new validation or replay",
        "do not say `strategy_validate` or `strategy_backtest` ran in this turn",
        "current advertised tool catalog and actual errors to establish availability",
        "absence of a call alone does not prove a tool is unavailable",
    ):
        assert requirement in guidance, requirement


def test_new_correction_is_distinct_from_unchanged_failed_retry(guidance):
    for requirement in (
        "separate the current request from historical failed operations",
        "Inspect the current schema, saved candidate and latest actual failure",
        "make the authorized correction in the SAME candidate",
        "call the needed producer once with corrected inputs",
        "only after a relevant input, source or evidenced precondition changes",
        "an unchanged failed operation remains blocked",
        "Reconcile pending approvals and uncertain effects before any retry",
        "does not approve a pending operation or waive scope, no-test, submission, replay or trading restrictions",
    ):
        assert requirement in guidance, requirement
