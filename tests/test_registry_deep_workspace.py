"""Tests for deep-namespace workspace skill discovery.

The original ``SkillRegistry.load_builtin`` only globbed one level under
``workspace/skills/`` and ``~/.nerya/skills/`` so that
``workspace/skills/<id>/SKILL.md`` was the only shape the runtime
recognised. Operators integrating sibling toolkits (e.g. the
``financial-services`` plugin marketplace) need namespacing such as
``workspace/skills/finance/private_equity/ic_memo/SKILL.md`` to keep
the surface organised. These tests pin the recursive walker behaviour
that makes that work without breaking the legacy one-level layout or
mis-registering a skill's own asset subtree (``references/``,
``scripts/``, …).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from nerya.core.paths import WorkspacePaths
from nerya.skills.registry import SkillRegistry, _walk_skill_dirs


pytestmark = pytest.mark.smoke


def _write_skill(path: Path, *, name: str, description: str = "test skill") -> None:
    """Helper: write a minimally valid Anthropic-spec SKILL.md."""
    path.parent.mkdir(parents=True, exist_ok=True)
    body = (
        "<!-- nerya-skill-frontmatter-start -->\n"
        "---\n"
        f"name: {name}\n"
        f'description: "{description}"\n'
        "version: 0.0.1\n"
        "license: Apache-2.0\n"
        "author: tests\n"
        "---\n"
        "<!-- nerya-skill-frontmatter-end -->\n"
        "\n"
        f"# {name}\n"
        f"\n{description}\n"
    )
    path.write_text(body, encoding="utf-8")


def _make_workspace(root: Path) -> WorkspacePaths:
    """Build a self-contained workspace with no nerya.yml side effects."""
    (root / "skills").mkdir(parents=True, exist_ok=True)
    return WorkspacePaths(root=root)


def test_flat_layout_still_loads(tmp_path: Path) -> None:
    """Legacy ``workspace/skills/<id>/SKILL.md`` keeps working."""
    paths = _make_workspace(tmp_path)
    _write_skill(paths.skills / "alpha" / "SKILL.md", name="alpha")

    reg = SkillRegistry.load_builtin(paths)
    workspace_ids = [
        e.manifest.id for e in reg.list() if e.manifest.source == "workspace"
    ]
    assert "alpha" in workspace_ids


def test_deep_namespace_layout_loads(tmp_path: Path) -> None:
    """``skills/<a>/<b>/<c>/SKILL.md`` is discovered after the rglob patch."""
    paths = _make_workspace(tmp_path)
    _write_skill(
        paths.skills / "finance" / "private_equity" / "ic_memo" / "SKILL.md",
        name="ic_memo",
        description="Investment-committee memo drafter (test).",
    )

    reg = SkillRegistry.load_builtin(paths)
    workspace_ids = [
        e.manifest.id for e in reg.list() if e.manifest.source == "workspace"
    ]
    assert "ic_memo" in workspace_ids


def test_mixed_layouts_coexist(tmp_path: Path) -> None:
    """Flat and deep skills live side by side without colliding."""
    paths = _make_workspace(tmp_path)
    _write_skill(paths.skills / "alpha" / "SKILL.md", name="alpha")
    _write_skill(
        paths.skills / "finance" / "wealth_management" / "rebalance" / "SKILL.md",
        name="rebalance",
    )
    _write_skill(
        paths.skills / "finance" / "private_equity" / "ic_memo" / "SKILL.md",
        name="ic_memo",
    )

    reg = SkillRegistry.load_builtin(paths)
    workspace_ids = {
        e.manifest.id for e in reg.list() if e.manifest.source == "workspace"
    }
    assert {"alpha", "rebalance", "ic_memo"}.issubset(workspace_ids)


def test_skill_asset_subtree_is_not_re_registered(tmp_path: Path) -> None:
    """A SKILL.md inside ``references/`` / ``scripts/`` must not be mistaken
    for its own skill — it is part of the parent skill's documentation."""
    paths = _make_workspace(tmp_path)
    parent = paths.skills / "pitch_deck"
    _write_skill(parent / "SKILL.md", name="pitch_deck")
    # Sibling reference doc that happens to also have a frontmatter-styled
    # SKILL.md (the upstream financial-services plugin uses this shape for
    # nested style guides). It must be ignored as a registration target.
    _write_skill(
        parent / "references" / "slide_templates" / "SKILL.md",
        name="slide_templates",
    )
    _write_skill(
        parent / "scripts" / "embedded" / "SKILL.md", name="embedded_helper"
    )

    reg = SkillRegistry.load_builtin(paths)
    workspace_ids = [
        e.manifest.id for e in reg.list() if e.manifest.source == "workspace"
    ]
    assert "pitch_deck" in workspace_ids
    assert "slide_templates" not in workspace_ids
    assert "embedded_helper" not in workspace_ids


def test_installed_subtree_is_not_walked(tmp_path: Path) -> None:
    """The ``installed/`` subtree is owned by ``skills_installed`` loading
    (path 2) and must not be re-discovered through the deep walker."""
    paths = _make_workspace(tmp_path)
    _write_skill(
        paths.skills / "installed" / "managed" / "SKILL.md", name="managed_x"
    )
    _write_skill(paths.skills / "free" / "SKILL.md", name="free_x")

    # Walker behaviour is the contract — the registry uses it.
    discovered = {
        d.name for d, _md in _walk_skill_dirs(paths.skills)
    }
    assert "free" in discovered
    assert "managed" not in discovered
    assert "installed" not in discovered


def test_hidden_directories_skipped(tmp_path: Path) -> None:
    """Dot-directories (``.git``, editor caches, …) are ignored."""
    paths = _make_workspace(tmp_path)
    _write_skill(paths.skills / ".cache" / "SKILL.md", name="cached")
    _write_skill(paths.skills / "real" / "SKILL.md", name="real")

    reg = SkillRegistry.load_builtin(paths)
    ids = [e.manifest.id for e in reg.list() if e.manifest.source == "workspace"]
    assert "real" in ids
    assert "cached" not in ids


def test_walker_yields_each_skill_once(tmp_path: Path) -> None:
    """A directory with SKILL.md is yielded exactly once and recursion
    stops there — the asset subtree is the skill's, not its own skills."""
    paths = _make_workspace(tmp_path)
    parent = paths.skills / "deep" / "namespace" / "leaf"
    _write_skill(parent / "SKILL.md", name="leaf")
    _write_skill(
        parent / "references" / "more" / "SKILL.md", name="should_be_invisible"
    )

    found = list(_walk_skill_dirs(paths.skills))
    found_names = [d.name for d, _md in found]
    assert found_names == ["leaf"]
