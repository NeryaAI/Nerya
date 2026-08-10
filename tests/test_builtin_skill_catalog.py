from __future__ import annotations

import re
from pathlib import Path

import pytest

from nerya.core.paths import WorkspacePaths
from nerya.core.config import Config
from nerya.core import yaml_io
from nerya.skills.manifest import SkillManifest, _split_frontmatter
from nerya.skills.registry import (
    SkillRegistry,
    _walk_skill_dirs,
    list_bundled_skill_names,
)
from nerya.workspace.manager import _DEFAULT_ENABLED_SKILLS


pytestmark = pytest.mark.smoke


BUILTIN_ROOT = Path(__file__).resolve().parents[1] / "nerya" / "skills" / "builtin"
LEGACY_SKILL_FILES = {"actions.py", "skill.yml", "skill.yaml", "manifest.yml", "manifest.yaml"}
SUPPORTED_FRONTMATTER_FIELDS = {
    "name",
    "description",
    "version",
    "license",
    "author",
}


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
        # A nested sub-skill (hub/<expert>/SKILL.md) shares its hub's
        # expanded playbook instead of shipping its own copy.
        has_playbook = (
            (skill_dir / "references" / "full-playbook.md").exists()
            or (skill_dir.parent / "references" / "full-playbook.md").exists()
        )
        assert has_playbook, (
            f"{skill_dir.name} should keep the expanded playbook under "
            "references/ (own or hub)"
        )


def test_builtin_skill_frontmatter_has_no_routing_extensions() -> None:
    for skill_dir in _skill_dirs():
        md = skill_dir / "SKILL.md"
        doc, _body = _split_frontmatter(md.read_text(encoding="utf-8"), source=md)
        extras = set(doc) - SUPPORTED_FRONTMATTER_FIELDS
        assert extras == set(), f"{md} has unsupported frontmatter: {sorted(extras)}"


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


def test_default_enabled_ids_are_real_bundled_playbooks() -> None:
    ids = set(list_bundled_skill_names())

    assert set(_DEFAULT_ENABLED_SKILLS) <= ids
    assert "market_data" not in _DEFAULT_ENABLED_SKILLS


def test_enabled_ids_are_loaded_as_exact_playbook_names(tmp_path: Path) -> None:
    paths = WorkspacePaths(root=tmp_path)
    yaml_io.dump(
        paths.skills_enabled,
        {
            "version": 1,
            "enabled": ["markets", "coding", "backtest", "quant_research"],
        },
    )

    ids = set(SkillRegistry.load_builtin(paths, config=Config(paths=paths)).by_id)

    assert {"markets", "coding", "backtest", "quant_research"} <= ids


def test_builtin_registry_loads_finance_namespace_skills() -> None:
    registry = SkillRegistry.load_builtin()
    ids = set(registry.by_id)

    assert "finance.private_equity.ic_memo" in ids
    assert {"dcf_valuation", "equity_research", "sec_filings"} <= ids
    assert "finance.financial_analysis.dcf_model" not in ids
    assert registry.get("finance.private_equity.ic_memo").manifest.source == "builtin"


def test_expert_investors_is_source_backed_and_self_contained() -> None:
    skill_dir = BUILTIN_ROOT / "expert_investors"
    manifest = SkillManifest.from_skill_md(skill_dir / "SKILL.md")

    assert manifest.id == "expert_investors"
    assert manifest.version == "0.3.0"

    # One sub-skill per expert; the hub routes to each of them.
    experts = {
        "buffett": "Warren Buffett",
        "damodaran": "Aswath Damodaran",
        "marks": "Howard Marks",
        "mauboussin": "Michael Mauboussin",
        "druckenmiller": "Stanley Druckenmiller",
    }
    hub_text = (skill_dir / "SKILL.md").read_text(encoding="utf-8")
    sub_skill_files: list[Path] = []
    for slug, display in experts.items():
        sub_md = skill_dir / slug / "SKILL.md"
        sub_manifest = SkillManifest.from_skill_md(sub_md)
        assert sub_manifest.id == f"expert_investors.{slug}"
        sub_text = sub_md.read_text(encoding="utf-8")
        assert display in sub_text
        assert "framework inference" in sub_text
        assert f"expert_investors.{slug}" in hub_text
        sub_skill_files.append(sub_md)
    assert sum(t.count("https://") for t in (
        p.read_text(encoding="utf-8") for p in sub_skill_files
    )) >= 20

    research_dir = skill_dir / "references" / "research"
    assert {path.name for path in research_dir.glob("*.md")} == {
        "01-writings.md",
        "02-conversations.md",
        "03-expression-dna.md",
        "04-external-views.md",
        "05-decisions.md",
        "06-timeline.md",
    }

    bindings: dict[str, set[str]] = {}
    source_id = re.compile(r"\*\*(?:\[[^]:]+:\s*)?([BDHMS]\d+)")
    url = re.compile(r"https?://[^ )]+")
    for path in [*sub_skill_files, *research_dir.glob("*.md")]:
        pending_id: str | None = None
        for line in path.read_text(encoding="utf-8").splitlines():
            if match := source_id.search(line):
                pending_id = match.group(1)
            if pending_id and (match := url.search(line)):
                bindings.setdefault(pending_id, set()).add(match.group(0).rstrip(".,;"))
                pending_id = None

    assert len(bindings) >= 30
    assert {key: values for key, values in bindings.items() if len(values) > 1} == {}


def test_finance_creators_is_english_source_backed_and_self_contained() -> None:
    skill_dir = BUILTIN_ROOT / "finance-creators"
    manifest = SkillManifest.from_skill_md(skill_dir / "SKILL.md")

    assert manifest.id == "finance-creators"
    assert manifest.version == "0.2.0"
    assert manifest.id in _DEFAULT_ENABLED_SKILLS

    # One sub-skill per creator; the hub routes to each of them.
    creators = {
        "serenity": "Serenity",
        "unusual_whales": "Unusual Whales",
        "kobeissi": "The Kobeissi Letter",
    }
    hub_text = (skill_dir / "SKILL.md").read_text(encoding="utf-8")
    sub_skill_files: list[Path] = []
    for slug, display in creators.items():
        sub_md = skill_dir / slug / "SKILL.md"
        sub_manifest = SkillManifest.from_skill_md(sub_md)
        assert sub_manifest.id == f"finance-creators.{slug}"
        sub_text = sub_md.read_text(encoding="utf-8")
        assert display in sub_text
        assert "framework inference" in sub_text
        assert f"finance-creators.{slug}" in hub_text
        sub_skill_files.append(sub_md)

    research_dir = skill_dir / "references" / "research"
    research_files = sorted(research_dir.glob("*.md"))
    assert {path.name for path in research_files} == {
        "01-writings.md",
        "02-conversations.md",
        "03-expression-dna.md",
        "04-external-views.md",
        "05-decisions.md",
        "06-timeline.md",
    }

    all_docs = [
        skill_dir / "SKILL.md",
        *sub_skill_files,
        skill_dir / "references" / "creator-lenses.md",
        skill_dir / "references" / "full-playbook.md",
        *research_files,
    ]
    assert not re.search(
        r"[\u3400-\u4dbf\u4e00-\u9fff]",
        "\n".join(path.read_text(encoding="utf-8") for path in all_docs),
    )

    bindings: dict[str, set[str]] = {}
    source_id = re.compile(r"\b((?:FW|FC|FE|FX|FD|FT|CL)-(?:SER|UW|KOB)-\d+)\b")
    url = re.compile(r"https?://[^ )]+")
    for path in [*sub_skill_files, *research_files]:
        pending_id: str | None = None
        for line in path.read_text(encoding="utf-8").splitlines():
            if match := source_id.search(line):
                pending_id = match.group(1)
            if pending_id and (match := url.search(line)):
                bindings.setdefault(pending_id, set()).add(match.group(0).rstrip(".,;"))
                pending_id = None

    assert len(bindings) >= 18
    assert all(
        sum(key.split("-")[1] == creator for key in bindings) >= 5
        for creator in ("SER", "UW", "KOB")
    )
    assert {key: values for key, values in bindings.items() if len(values) > 1} == {}


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
