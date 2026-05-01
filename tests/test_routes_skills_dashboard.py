from __future__ import annotations

from copy import deepcopy
from types import SimpleNamespace

import pytest

from nerya.api import routes_skills
from nerya.api.route_scopes import required_scope
from nerya.core.config import DEFAULT_CONFIG, Config
from nerya.core.paths import WorkspacePaths
from nerya.skills.installer import _normalize_source_hints, _pick_skill_dir
from nerya.skills.kernel import SkillKernel
from nerya.core.errors import SkillActionError


pytestmark = pytest.mark.smoke


def _client(tmp_path):
    paths = WorkspacePaths(root=tmp_path)
    config = Config(paths=paths, data=deepcopy(DEFAULT_CONFIG))
    skills = SkillKernel.boot(config)
    return SimpleNamespace(config=config, skills=skills)


def test_skill_detail_exposes_workspace_folder_and_playbook(tmp_path) -> None:
    skill_dir = tmp_path / "skills" / "demo"
    (skill_dir / "scripts").mkdir(parents=True)
    (skill_dir / "references").mkdir()
    (skill_dir / "SKILL.md").write_text(
        "---\nname: demo\ndescription: Demo workspace skill\nversion: 0.2.0\n---\n"
        "# Demo\n\nUse this skill for dashboard tests.\n",
        encoding="utf-8",
    )
    (skill_dir / "scripts" / "helper.py").write_text("print('ok')\n", encoding="utf-8")
    (skill_dir / "references" / "notes.md").write_text("# Notes\n", encoding="utf-8")

    res = routes_skills._detail(_client(tmp_path), {"skill_id": "demo"})

    assert res["ok"] is True
    skill = res["skill"]
    assert skill["source"] == "workspace"
    assert skill["editable"] is True
    assert skill["skill_md"].startswith("---\nname: demo")
    assert {row["path"] for row in skill["files"]} >= {
        "SKILL.md",
        "scripts/helper.py",
        "references/notes.md",
    }


def test_skill_update_rewrites_workspace_skill_and_reloads(tmp_path) -> None:
    skill_dir = tmp_path / "skills" / "demo"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        "---\nname: demo\ndescription: Old description\n---\n# Old\n",
        encoding="utf-8",
    )
    client = _client(tmp_path)

    res = routes_skills._update(
        client,
        {
            "skill_id": "demo",
            "skill_md": "---\nname: demo\ndescription: New description\n---\n# New\n",
        },
    )

    assert res["ok"] is True
    assert "New description" in (skill_dir / "SKILL.md").read_text(encoding="utf-8")
    assert client.skills.registry.get("demo").manifest.description == "New description"


def test_skill_update_rejects_builtin_skill(tmp_path) -> None:
    client = _client(tmp_path)

    res = routes_skills._update(
        client,
        {
            "skill_id": "agents",
            "skill_md": "---\nname: agents\ndescription: nope\n---\n# Nope\n",
        },
    )

    assert res["ok"] is False
    assert res["error"] == "skill_not_editable"


def test_skill_create_writes_workspace_skill_and_reloads(tmp_path) -> None:
    client = _client(tmp_path)

    res = routes_skills._create(
        client,
        {
            "name": "clawcast wallet",
            "description": "Wallet skill created from the dashboard.",
            "body": "Use this skill to manage the local wallet flow.",
        },
    )

    assert res["ok"] is True
    assert res["skill"]["id"] == "clawcast_wallet"
    md = tmp_path / "skills" / "clawcast_wallet" / "SKILL.md"
    assert md.exists()
    assert "Wallet skill created from the dashboard." in md.read_text(encoding="utf-8")
    assert client.skills.registry.get("clawcast_wallet").manifest.description.startswith("Wallet")


def test_github_tree_url_is_normalized_for_skill_install() -> None:
    source, subdir, git_ref = _normalize_source_hints(
        "https://github.com/openclaw/skills/tree/main/skills/tezatezaz/clawcast-wallet",
        subdir=None,
        git_ref=None,
    )

    assert source == "https://github.com/openclaw/skills.git"
    assert git_ref == "main"
    assert subdir == "skills/tezatezaz/clawcast-wallet"


def test_pick_skill_dir_rejects_traversal_subdir(tmp_path) -> None:
    with pytest.raises(SkillActionError):
        _pick_skill_dir(tmp_path, subdir="../outside")


def test_skill_detail_and_update_routes_are_scoped() -> None:
    assert required_scope("GET", "/skills/detail") == "read:runtime"
    assert required_scope("POST", "/skills/create") == "write:skills"
    assert required_scope("POST", "/skills/update") == "write:skills"
