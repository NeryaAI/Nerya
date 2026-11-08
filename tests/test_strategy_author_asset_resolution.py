"""Regression coverage for the packaged strategy_author EMA reference."""

import ast
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


def test_repair_reference_routes_last_edit_to_same_candidate_producer():
    reference = _skill(file="references/authoring-workflow.md")
    assert not reference.is_error, reference.text()
    text = " ".join(reference.text().split())
    assert 'strategy_validate({"proposal_id": "<same returned proposal_id>"})' in text
    assert "after the last source or test edit" in text
    assert "Zero shell commands are needed for package tests" in text
    assert "`run_shell`" in text
    assert "`behavioral.failures`" in text
    assert "`behavioral.diagnostic`" in text
    assert "deleting, skipping or weakening assertions is not a repair" in text
    assert "do not self-report a pass" in text
    assert "leave the pending approval untouched" in text


def test_research_visuals_reference_loads_from_its_owning_skill():
    playbook = (BUILTIN_ROOT / "strategy_author/SKILL.md").read_text(encoding="utf-8")
    assert 'Skill(skill="research", file="references/visual-deliverables.md")' in playbook
    reference = skill_tool_handler(
        ToolCall(name="Skill", arguments={
            "skill": "research", "file": "references/visual-deliverables.md",
        }),
        skill_index=SkillIndex([BUILTIN_ROOT]),
    )
    assert not reference.is_error, reference.text()
    assert "Interactive research deliverables" in reference.text()
    assert _json(reference)["path"].endswith("research/references/visual-deliverables.md")


def test_strategy_validate_description_advertises_isolated_repair_route():
    # Inspect only the registered description; no bootstrap or runtime execution.
    source = BUILTIN_ROOT.parents[1] / "tools/native/bootstrap.py"
    descriptions = [
        ast.literal_eval(keyword.value)
        for node in ast.walk(ast.parse(source.read_text(encoding="utf-8")))
        if isinstance(node, ast.Call)
        and any(
            arg.arg == "name" and isinstance(arg.value, ast.Constant)
            and arg.value.value == "strategy_validate" for arg in node.keywords
        )
        for keyword in node.keywords if keyword.arg == "description"
    ]
    assert len(descriptions) == 1
    description = descriptions[0]
    for required in (
        "isolated", "package tests", "same proposal_id", "last source or test edit",
        "behavioral.failures", "behavioral.diagnostic", "run_shell",
        "source-bound",
    ):
        assert required in description, required
