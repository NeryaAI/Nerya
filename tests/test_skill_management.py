"""All Skill definitions and scoped changes remain safe and applyable proposals."""
import json
from pathlib import Path

import pytest

from nerya.core import yaml_io
from nerya.mcp.tools import NeryaTools
from nerya.skills import management as m
from nerya.skills.registry import SkillRegistry
from nerya.subagents.registry import save_role, load_registry
from nerya.evolution.candidate_bundle import verify_candidate_bundle
from nerya.evolution.patch_proposal import list_proposals
from nerya.evolution.promotion import build_mutation_plan

pytestmark = pytest.mark.smoke


def playbook(name="test_skill", body="Read local data safely."):
    return f"---\nname: {name}\ndescription: A testing workflow.\n---\n\n{body}\n"


@pytest.fixture
def config(tmp_path, monkeypatch):
    monkeypatch.setenv("NERYA_USER_SKILLS_ROOT", str(tmp_path / "no-home-skills"))
    cfg = NeryaTools.boot(tmp_path).client.config
    directory = tmp_path / "skills" / "test_skill"
    directory.mkdir(parents=True)
    (directory / "SKILL.md").write_text(playbook())
    (directory / "references").mkdir()
    (directory / "references" / "notes.md").write_text("Local notes.\n")
    return cfg


def test_complete_catalog_and_asset_read(config):
    first = m.catalog(config, limit=2)
    assert first["total"] > 30
    assert first["next_offset"] == 2
    assert len(m.catalog(config, offset=2, limit=2)["skills"]) == 2
    ws = m.catalog(config, scope="workspace")
    assert [row["id"] for row in ws["skills"]] == ["test_skill"]
    result = m.read(config, "test_skill", file="references/notes.md", limit=5)
    assert result["text"] == "Local"
    assert result["next_offset"] == 5
    assert "references/notes.md" in result["files"]
    yaml_io.dump(config.paths.skills_enabled, {"enabled": []})
    assert not any(row["enabled"] for row in m.catalog(config, limit=200)["skills"])
    assert SkillRegistry.load_builtin(config.paths, config=config).list() == []
    assert m.read(config, "test_skill")["text"] == playbook()


def test_update_create_and_delete_proposal_bundles(config):
    old = m.read(config, "test_skill")
    with pytest.raises(ValueError, match="stale_revision"):
        m.manage(config, "update", "test_skill", revision="stale", content=playbook(body="New"))
    for action, sid, revision, content in (("update", "test_skill", old["revision"], playbook(body="Changed")),
                                          ("create", "another_skill", "missing", playbook("another_skill")),
                                          ("delete", "test_skill", old["revision"], "")):
        result = m.manage(config, action, sid, revision=revision, content=content)
        assert result["applied"] is False
        proposal = next(p for p in list_proposals(config.paths) if p.id == result["proposal"]["id"])
        bundle = json.loads((proposal.path / "candidate_bundle.json").read_text())
        assert verify_candidate_bundle(config.paths.root, proposal.path, bundle)["ok"]
        from nerya.evolution.promotion import proposal_action_gates
        gates = proposal_action_gates(config.paths, proposal)
        assert gates["blockers"] == ["state_pending_review"], gates
        plan = build_mutation_plan(config.paths, proposal)
        if action == "delete":
            assert "skills/test_skill/SKILL.md" in plan["manifest"]["deleted"]
        else:
            assert plan["after_entries"]
    assert (config.paths.skills / "test_skill" / "SKILL.md").read_text() == playbook()
    assert not (config.paths.skills / "another_skill").exists()


def test_agent_assignment_is_proposal_only_and_empty_means_none(config):
    save_role(config.paths, name="test_agent", prompt="A safe role", allowed_skills=["test_skill"])
    catalog = m.catalog(config, scope="agent", agent_id="test_agent")
    assert [row["id"] for row in catalog["skills"]] == ["test_skill"]
    assert m.read(config, "test_skill", scope="agent", agent_id="test_agent")["shared_definition"]
    result = m.manage(config, "disable", "test_skill", scope="agent", agent_id="test_agent", revision=catalog["binding_revision"])
    path = Path(result["proposal"]["path"]) / "after/subagents/test_agent.role.yaml"
    assert yaml_io.load(path)["allowed_skills"] == []
    assert load_registry(config.paths)["test_agent"].allowed_skills == ["test_skill"]
    save_role(config.paths, name="test_agent", prompt="A safe role", allowed_skills=[])
    assert load_registry(config.paths)["test_agent"].allowed_skills == []
    with pytest.raises(ValueError, match="not assigned"):
        m.read(config, "test_skill", scope="agent", agent_id="test_agent")


def test_paths_symlinks_content_and_redaction(config, tmp_path):
    outside = tmp_path / "outside.txt"
    outside.write_text("private")
    root = config.paths.skills / "test_skill"
    (root / "references" / "leak.md").symlink_to(outside)
    for path in ("../../outside.txt", "/etc/passwd", "references/leak.md", "../outside.txt"):
        with pytest.raises(ValueError):
            m.read(config, "test_skill", file=path)
    notes = root / "references/notes.md"
    notes.write_text("password: tiny-secret\n" + "a" * 5)
    pieces = []
    offset = 0
    while True:
        value = m.read(config, "test_skill", file="references/notes.md", offset=offset, limit=8)
        pieces.append(value["text"])
        if value["next_offset"] is None:
            break
        offset = value["next_offset"]
    assert "tiny-secret" not in "".join(pieces)
    with pytest.raises(ValueError):
        m.manage(config, "create", "bad", revision="missing", content="not a Skill playbook")
