"""Offline Skill delivery wording; does not certify model compliance."""
from pathlib import Path
import socket

import pytest

from nerya.tools.native.skill import SkillIndex
from nerya.tools.native.skill_tool import skill_tool_handler
from nerya.tools.types import ToolCall


@pytest.mark.smoke
def test_strategy_failure_summary_is_loaded_with_preservation_and_stop_boundaries(monkeypatch):
    def deny_network(*args, **kwargs):
        pytest.fail("Failure-summary guidance must load offline")

    monkeypatch.setattr(socket.socket, "connect", deny_network)
    monkeypatch.setattr(socket, "create_connection", deny_network)
    skill = Path(__file__).resolve().parents[1] / "nerya/skills/builtin/strategy_author/SKILL.md"
    result = skill_tool_handler(
        ToolCall(name="Skill", arguments={"skill": "strategy_author"}),
        skill_index=SkillIndex([], skill_files=[skill]),
    )
    assert not result.is_error, result.text()
    text = " ".join(result.text().split())
    summary = text.split("### Failure summary for the user", 1)[1].split("## Workflow", 1)[0]
    assert text.index(summary) < text.index("Before drafting")
    assert "at most three short sentences in the user's language" in summary
    assert "same candidate/scope, failed/missing checks and uncertainty" in summary
    assert "in execution details" in summary
    assert "For a blocked or failed workflow, use **Failure summary for the user** above. For a completed replay, show actual versus" in text
