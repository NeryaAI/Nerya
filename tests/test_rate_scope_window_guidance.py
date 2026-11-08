"""Offline Skill-load contract; not scope admission or DeFi task acceptance."""

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


def test_rate_guidance_preserves_explicit_pair_over_relative_period(monkeypatch):
    def denied(*args, **kwargs):
        pytest.fail("Rate guidance loading must remain offline")

    monkeypatch.setattr(socket.socket, "connect", denied)
    monkeypatch.setattr(socket, "create_connection", denied)
    catalog = SkillRegistry()
    catalog.register(load_entry(SKILL / "SKILL.md", source="builtin"))
    tools = ToolRegistry()
    register_skill_tool(tools, skill_index=SkillIndex([], registry_provider=lambda: catalog))
    executor = NativeToolExecutor(
        registry=tools,
        permission_engine=PermissionEngine(),
        permission_context=PermissionContext(),
    )

    def read(file):
        pages, offset = [], 0
        while True:
            result = executor.execute(ToolCall(name="Skill", arguments={
                "skill": "strategy_author", "file": file, "offset": offset,
            }))
            assert not result.is_error, result.text()
            pages.append(result.content[0].text)
            following = tool_json_data(result).get("next_offset")
            if following is None:
                return " ".join("\n".join(pages).split())
            assert following > offset
            offset = following

    assert "references/rate-policy.md" in read("SKILL.md")
    text = read("references/rate-policy.md")
    for instruction in (
        "Before drafting, bind the rate replay window",
        "relative period such as 最近30天 and explicit UTC bounds in the same admitted request",
        "preserve the producer-admitted explicit `start_utc`/`end_utc` pair",
        "The explicit date pair owns scope",
        "`window_days` and `start_utc`/`end_utc` are mutually exclusive",
        "omit `window_days` when emitting the explicit pair",
        "Do not reconstruct timezone, midnight/default boundaries or a new relative-period anchor",
        "Copy the exact admitted pair into `strategy.md` and `replay_inputs.json`",
        "Historical coverage limits remain separate from scope binding",
        "missing observations cannot authorize changing the pair or relaxing gap bounds",
    ):
        assert instruction in text
    assert text.index("Before drafting, bind the rate replay window") < text.index("1. Declare thresholds")
