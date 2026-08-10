from pathlib import Path

from nerya.skills.manifest import SkillManifest
from nerya.workspace.manager import _DEFAULT_ENABLED_SKILLS


SKILL_DIR = (
    Path(__file__).resolve().parents[1]
    / "nerya"
    / "skills"
    / "builtin"
    / "quant-strategy-loop"
)


def test_quant_strategy_loop_is_safe_and_available() -> None:
    manifest = SkillManifest.from_skill_md(SKILL_DIR / "SKILL.md")
    playbook = (SKILL_DIR / "references" / "full-playbook.md").read_text(
        encoding="utf-8"
    )

    assert manifest.id == "quant-strategy-loop"
    assert manifest.id in _DEFAULT_ENABLED_SKILLS
    assert "GOAL GATE" in manifest.instructions
    assert "allow_mock=false" in manifest.instructions
    assert "available_at <= decision_at" in playbook
    assert "Never keep optimizing against the same historical test period" in playbook
