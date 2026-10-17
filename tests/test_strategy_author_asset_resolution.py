"""Regression coverage for the packaged strategy_author EMA reference."""

from pathlib import Path

from nerya.tools.native.skill import SkillIndex
from nerya.tools.native.skill_tool import skill_tool_handler
from nerya.tools.types import ToolCall


BUILTIN_ROOT = Path(__file__).parents[1] / "nerya" / "skills" / "builtin"


def _skill(**arguments):
    return skill_tool_handler(
        ToolCall(name="Skill", arguments={"skill": "strategy_author", **arguments}),
        skill_index=SkillIndex([BUILTIN_ROOT]),
    )


def _json(result):
    return next(part.data for part in result.content if part.type == "json")


def test_plain_cex_ema_uses_packaged_canonical_reference():
    playbook = _skill()

    assert not playbook.is_error, playbook.text()
    assert "plain paper CEX EMA/crossover" in playbook.text()
    assert "Do not guess a path such as" in playbook.text()
    assert "templates/mainstream/ema_cross.py.j2" in playbook.text()

    reference = _skill(
        action="read",
        file="references/mainstream-strategies.md",
    )

    assert not reference.is_error, reference.text()
    assert "Authoring executable mainstream market strategies" in reference.text()
    assert _json(reference)["path"].endswith(
        "strategy_author/references/mainstream-strategies.md"
    )
