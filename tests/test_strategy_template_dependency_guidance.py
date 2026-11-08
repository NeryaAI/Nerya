"""Offline regression for strategy template dependency recovery guidance."""

from pathlib import Path

import pytest

from nerya.skills.registry import SkillRegistry, load_entry
from nerya.tools.native.skill import SkillIndex
from nerya.tools.native.skill_tool import skill_tool_handler
from nerya.tools.types import ToolCall


pytestmark = pytest.mark.smoke
SKILL_ROOT = Path(__file__).resolve().parents[1] / "nerya/skills/builtin/strategy_author"


@pytest.fixture
def read_skill():
    registry = SkillRegistry()
    registry.register(load_entry(SKILL_ROOT / "SKILL.md", source="builtin"))
    index = SkillIndex([], registry_provider=lambda: registry)

    def read(file=None):
        arguments = {"skill": "strategy_author"}
        if file is not None:
            arguments["file"] = file
        result = skill_tool_handler(
            ToolCall(name="Skill", arguments=arguments), skill_index=index,
        )
        assert not result.is_error, result.text()
        return result.text()

    return read


def test_dependency_recovery_guidance_is_registered_and_bounded(read_skill):
    text = " ".join((read_skill() + " " + read_skill(
        "references/authoring-workflow.md")).split())

    for phrase in (
        "enumerate every import",
        "revision-bound write_file.source_asset",
        "each imported companion or contract asset",
        "Read the exact saved candidate files",
        "producer exact import or collection error",
        "Edit the SAME proposal",
        "do not invent a replacement helper, import, or new proposal",
        "candidate root to sys.path before pytest collection",
        "retain it for collection",
        "nerya/tools/native/strategy_runtime.py",
        "do not invent a candidate workaround",
    ):
        assert phrase in text


def test_existing_ema_contract_seam_is_documented_without_template_edit(read_skill):
    guide = " ".join((read_skill() + " " + read_skill(
        "references/authoring-workflow.md")).split())
    contract = (SKILL_ROOT / "templates/ema_cross_contract.py.j2").read_text()

    assert "from main import ema_cross, run" in contract
    assert "ema_cross_contract.py.j2" in guide
    assert "producer runner must add the candidate root to sys.path" in guide
