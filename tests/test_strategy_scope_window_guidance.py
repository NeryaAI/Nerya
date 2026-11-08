"""Offline Skill-load regression for producer-admitted replay windows."""

import socket
from pathlib import Path

import pytest

from nerya.skills.registry import SkillRegistry, load_entry
from nerya.tools.executor import NativeToolExecutor
from nerya.tools.native.skill import SkillIndex
from nerya.tools.native.skill_tool import register_skill_tool
from nerya.tools.permissions import PermissionContext, PermissionEngine
from nerya.tools.registry import ToolRegistry
from nerya.tools.result_contracts import tool_json_data
from nerya.tools.types import ToolCall

pytestmark = pytest.mark.smoke
SKILL = Path(__file__).resolve().parents[1] / "nerya/skills/builtin/strategy_author"


@pytest.fixture
def read_skill(monkeypatch):
    def denied(*args, **kwargs):
        pytest.fail("Skill guidance loading must remain offline")

    monkeypatch.setattr(socket.socket, "connect", denied)
    monkeypatch.setattr(socket, "create_connection", denied)
    catalog = SkillRegistry()
    catalog.register(load_entry(SKILL / "SKILL.md", source="builtin"))
    tools = ToolRegistry()
    register_skill_tool(
        tools,
        skill_index=SkillIndex([], registry_provider=lambda: catalog),
    )
    executor = NativeToolExecutor(
        registry=tools,
        permission_engine=PermissionEngine(),
        permission_context=PermissionContext(),
    )

    def read(file):
        pages, offset = [], 0
        while True:
            result = executor.execute(
                ToolCall(
                    name="Skill",
                    arguments={
                        "skill": "strategy_author",
                        "file": file,
                        "offset": offset,
                    },
                )
            )
            assert not result.is_error, result.text()
            pages.append(result.content[0].text)
            following = tool_json_data(result).get("next_offset")
            if following is None:
                return " ".join("\n".join(pages).split())
            assert following > offset
            offset = following

    return read


def test_playbook_loads_exact_admitted_window_guidance(read_skill):
    text = read_skill("SKILL.md")
    for phrase in (
        "producer-admitted `request_scope` owns the replay window",
        "`start_utc` and `end_utc`",
        "timezone and the timeframe anchor",
        "Never reconstruct UTC boundaries from date-only values",
        "normal proposal lane",
        "SAME candidate",
        "`coverage_missing`",
        "never authorizes shortening, shifting or bypassing the admitted scope",
    ):
        assert phrase in text


def test_resume_workflow_loads_manifest_comparison_and_bounded_repair(read_skill):
    text = read_skill("references/authoring-workflow.md")
    for phrase in (
        "producer-admitted `request_scope` is authoritative for replay bounds",
        "copy its `start_utc` and `end_utc` into the saved candidate/replay manifest exactly",
        "Do not rebuild them from date-only fields",
        "Before `strategy_validate`, compare the saved manifest window",
        "changing only that candidate field through the normal proposal lane",
        "`coverage_missing` or incomplete historical coverage is a separate replay limitation",
    ):
        assert phrase in text
