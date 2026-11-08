"""Contract coverage for repairing an explicitly identified existing candidate."""

from pathlib import Path


BUILTIN_ROOT = Path(__file__).parents[1] / "nerya" / "skills" / "builtin"


def test_existing_candidate_repair_has_delivery_and_validation_boundary() -> None:
    skill = (BUILTIN_ROOT / "strategy_author/SKILL.md").read_text(encoding="utf-8")
    workflow = (
        BUILTIN_ROOT / "strategy_author/references/authoring-workflow.md"
    ).read_text(encoding="utf-8")

    entry = " ".join(skill.split())
    repair = " ".join(workflow.split()).lower()

    assert "existing-candidate repair" in entry
    assert "load `references/authoring-workflow.md`" in entry
    for requirement in (
        "call `delivery_start` for the current command before any write or validation",
        "required technical lookback capacity alongside the helper",
        "preserve the configured indicator periods, sizing, and pinned window",
        "a helper-only guard is not a complete repair",
        "validation transport failure",
        "stop and report validation as unverified",
    ):
        assert requirement in repair, requirement
