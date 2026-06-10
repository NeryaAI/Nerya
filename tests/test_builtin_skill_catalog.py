from __future__ import annotations

from pathlib import Path

import pytest

from nerya.core.paths import WorkspacePaths
from nerya.skills.manifest import SkillManifest
from nerya.skills.registry import SkillRegistry, _walk_skill_dirs


pytestmark = pytest.mark.smoke


BUILTIN_ROOT = Path(__file__).resolve().parents[1] / "nerya" / "skills" / "builtin"
LEGACY_SKILL_FILES = {"actions.py", "skill.yml", "skill.yaml", "manifest.yml", "manifest.yaml"}


def _skill_dirs() -> list[Path]:
    return sorted(d for d, _md in _walk_skill_dirs(BUILTIN_ROOT))


def test_builtin_skill_md_files_parse_and_stay_compact() -> None:
    for skill_dir in _skill_dirs():
        md = skill_dir / "SKILL.md"
        assert md.exists(), f"{skill_dir.name} is missing SKILL.md"
        manifest = SkillManifest.from_skill_md(md)
        assert manifest.id
        assert manifest.description

        line_count = len(md.read_text(encoding="utf-8").splitlines())
        assert line_count <= 80, f"{skill_dir.name} SKILL.md is too large: {line_count}"
        assert (skill_dir / "references" / "full-playbook.md").exists(), (
            f"{skill_dir.name} should keep the expanded playbook under references/"
        )


def test_builtin_skill_tree_has_no_legacy_definition_surfaces() -> None:
    legacy = [
        p.relative_to(BUILTIN_ROOT).as_posix()
        for p in BUILTIN_ROOT.rglob("*")
        if p.is_file() and p.name in LEGACY_SKILL_FILES
    ]
    assert legacy == []

    ref_dirs = [
        p.relative_to(BUILTIN_ROOT).as_posix()
        for p in BUILTIN_ROOT.rglob("ref")
        if p.is_dir()
    ]
    assert ref_dirs == []


def test_builtin_registry_includes_core_compact_skills() -> None:
    registry = SkillRegistry.load_builtin()
    ids = set(registry.by_id)

    assert {
        "agents",
        "analysis",
        "backtest",
        "browser",
        "coding",
        "evolve",
        "markets",
        "research",
        "strategy_author",
        "tasks",
        "trading",
        "triggers",
    }.issubset(ids)

    for entry in registry.list():
        if entry.manifest.source == "builtin":
            assert entry.actions == {}


def test_builtin_registry_loads_finance_namespace_skills() -> None:
    registry = SkillRegistry.load_builtin()
    ids = set(registry.by_id)

    assert "finance.private_equity.ic_memo" in ids
    assert "finance.financial_analysis.dcf_model" not in ids
    assert registry.get("finance.private_equity.ic_memo").manifest.source == "builtin"


def test_workspace_skills_override_user_home_skills(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    paths = WorkspacePaths(root=tmp_path / "workspace")
    home = tmp_path / "home_skills"
    monkeypatch.setenv("NERYA_USER_SKILLS_ROOT", str(home))

    _write_skill(paths.skills / "dupe" / "SKILL.md", name="dupe")
    _write_skill(home / "dupe" / "SKILL.md", name="dupe")
    _write_skill(home / "only_home" / "SKILL.md", name="only_home")

    registry = SkillRegistry.load_builtin(paths)

    assert registry.get("dupe").manifest.source == "workspace"
    assert registry.get("only_home").manifest.source == "user_home"


def _write_skill(path: Path, *, name: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "\n".join([
            "---",
            f"name: {name}",
            'description: "test skill"',
            "version: 0.0.1",
            "license: MIT",
            "author: tests",
            "---",
            "",
            f"# {name}",
            "",
        ]),
        encoding="utf-8",
    )
