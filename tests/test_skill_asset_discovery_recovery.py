"""Missing native Skill assets recover through exact, authorized sibling paths."""

import json

import pytest

from nerya.skills.registry import SkillRegistry, load_entry
from nerya.tools.native.skill import SkillIndex
from nerya.tools.native.skill_tool import register_skill_tool
from nerya.tools.registry import ToolRegistry
from nerya.tools.types import ToolCall, ToolErrorKind

pytestmark = pytest.mark.smoke


def write_skill(root, name):
    root.mkdir(parents=True, exist_ok=True)
    path = root / "SKILL.md"
    path.write_text(f"---\nname: {name}\ndescription: test assets\n---\nPRIVATE PLAYBOOK\n")
    return path


@pytest.fixture
def assets(tmp_path):
    root = tmp_path / "installed" / "author"
    catalog = SkillRegistry()
    catalog.register(load_entry(write_skill(root, "author"), source="workspace_installed"))
    (root / "templates").mkdir()
    (root / "templates" / "range_policy.py.j2").write_text("POLICY CONTENT\n")
    (root / "templates" / "range_strategy.py.j2").write_text("STRATEGY CONTENT\n")
    index = SkillIndex([], registry_provider=lambda: catalog)
    tools = ToolRegistry()
    register_skill_tool(tools, skill_index=index)

    def read(file, *, skill="author", allowed=None):
        return tools.get("Skill").handler(ToolCall(
            name="Skill", arguments={"skill": skill, "file": file},
            metadata={"allowed_skills": allowed} if allowed is not None else {},
        ))

    return root, catalog, read


def test_registered_skill_missing_asset_exposes_exact_paths_in_hint_and_message(assets):
    root, catalog, read = assets
    # Other directories and skills must not turn a miss into a catalog dump.
    (root / "references").mkdir()
    (root / "references" / "unrelated.md").write_text("REFERENCE CONTENT")
    catalog.register(load_entry(write_skill(root.parent / "other", "other")))
    result = read("templates/guessed_protocol.py.j2")
    assert result.is_error and result.error.kind == ToolErrorKind.NOT_FOUND
    hint = result.error.recovery_hint
    assert hint == {
        "action": "read_skill_asset", "skill": "author",
        "candidates": ["templates/range_policy.py.j2", "templates/range_strategy.py.j2"],
    }
    for path in hint["candidates"]:
        assert path in result.error.message
        assert path in result.text()
    serialized = json.dumps(result.asdict())
    for excluded in [str(root), "PRIVATE PLAYBOOK", "POLICY CONTENT",
                     "STRATEGY CONTENT", "unrelated.md", "REFERENCE CONTENT"]:
        assert excluded not in serialized
    recovered = read(hint["candidates"][1], skill=hint["skill"])
    assert not recovered.is_error
    assert "STRATEGY CONTENT" in recovered.text()


def test_siblings_are_sorted_bounded_files_without_contents_or_recursive_search(assets, monkeypatch):
    root, _, read = assets
    directory = root / "templates"
    for number in reversed(range(20)):
        (directory / f"asset_{number:02}.txt").write_text("SECRET CONTENT")
    (directory / ".env").write_text("SECRET ENV")
    (directory / "nested").mkdir()
    (directory / "nested" / "deep.txt").write_text("DEEP CONTENT")
    (directory / ("z" * 250 + ".txt")).touch()
    # After catalog load, discovery must enumerate only the requested directory.
    read("templates/range_policy.py.j2")
    path_type = type(root)
    original = path_type.iterdir

    def confined_iterdir(path):
        assert path == directory.resolve()
        return original(path)

    monkeypatch.setattr(path_type, "iterdir", confined_iterdir)
    result = read("templates/missing.txt")
    assert result.error.recovery_hint["candidates"] == [
        "templates/asset_00.txt", "templates/asset_01.txt",
        "templates/asset_02.txt", "templates/asset_03.txt",
        "templates/asset_04.txt", "templates/asset_05.txt",
        "templates/asset_06.txt", "templates/asset_07.txt",
        "templates/asset_08.txt", "templates/asset_09.txt",
        "templates/asset_10.txt", "templates/asset_11.txt",
    ]
    assert len(result.text()) < 8192
    for excluded in ["SECRET", ".env", "deep.txt", "nested", str(root)]:
        assert excluded not in json.dumps(result.asdict())


@pytest.mark.parametrize("file", ["absent/missing.txt", "templates/nested/missing.txt"])
def test_nonexistent_or_empty_subdirectory_does_not_fall_back_to_parent(assets, file):
    root, _, read = assets
    (root / "templates" / "nested").mkdir()
    result = read(file)
    assert result.error.kind == ToolErrorKind.NOT_FOUND
    assert result.error.recovery_hint["candidates"] == []
    assert "range_strategy" not in result.text()


def test_external_traversal_and_symlink_escape_never_enumerate(assets, tmp_path, monkeypatch):
    root, _, read = assets
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "private.txt").write_text("OUTSIDE SECRET")
    (root / "escape").symlink_to(outside, target_is_directory=True)
    (root / "templates" / "escaped.txt").symlink_to(outside / "private.txt")
    # Preload metadata before placing a filesystem enumeration guard.
    read("templates/range_policy.py.j2")

    def no_listing(path):
        pytest.fail(f"denied request enumerated {path}")

    with monkeypatch.context() as guard:
        guard.setattr(type(root), "iterdir", no_listing)
        for file in ["../../outside/missing.txt", str(outside / "missing.txt"),
                     "escape/missing.txt", "templates/escaped.txt"]:
            result = read(file)
            assert result.error.kind == ToolErrorKind.SCHEMA_VALIDATION
            assert not result.error.recovery_hint
            assert "OUTSIDE SECRET" not in result.text()
    result = read("templates/missing.txt")
    assert "escaped.txt" not in result.text()
    assert "private.txt" not in result.text()


@pytest.mark.parametrize("enabled,allowed,kind", [
    (False, None, ToolErrorKind.NOT_FOUND),
    (True, ["author"], ToolErrorKind.PERMISSION_DENIED),
])
def test_missing_nested_asset_checks_active_catalog_and_role_before_listing(
    assets, monkeypatch, enabled, allowed, kind,
):
    root, catalog, read = assets
    child = write_skill(root / "child", "private_child")
    (child.parent / "private.txt").write_text("CHILD SECRET")
    if enabled:
        catalog.register(load_entry(child))
    read("templates/range_policy.py.j2")

    def no_listing(path):
        pytest.fail(f"unauthorized nested asset enumerated {path}")

    monkeypatch.setattr(type(root), "iterdir", no_listing)
    result = read("child/missing.txt", allowed=allowed)
    assert result.error.kind == kind
    assert not result.error.recovery_hint
    assert "private.txt" not in result.text()
    assert "CHILD SECRET" not in result.text()


def test_sibling_playbooks_are_filtered_by_existing_asset_access_validation(assets):
    root, catalog, read = assets
    sibling = root / "private.md"
    sibling.write_text("---\nid: private\n---\nPRIVATE OTHER BODY\n")
    catalog.register(load_entry(sibling, procedural=True))
    result = read("missing.md", allowed=["author"])
    assert result.error.recovery_hint["candidates"] == ["SKILL.md"]
    assert "private.md" not in result.text()
    assert "PRIVATE OTHER BODY" not in result.text()


def test_disabled_root_and_disallowed_role_cannot_discover_assets(assets):
    _, catalog, read = assets
    denied = read("templates/missing.txt", allowed=["another"])
    assert denied.error.kind == ToolErrorKind.PERMISSION_DENIED
    assert not denied.error.recovery_hint
    catalog.by_id.pop("author")
    disabled = read("templates/missing.txt")
    assert disabled.error.kind == ToolErrorKind.NOT_FOUND
    assert not disabled.error.recovery_hint


def test_model_message_bounds_json_escaped_names_without_truncating_candidate_paths(assets):
    root, _, read = assets
    for number in range(12):
        (root / "templates" / (f"{number:02}_" + "模" * 80)).touch()
    result = read("templates/missing.txt")
    candidates = result.error.recovery_hint["candidates"]
    assert 0 < len(candidates) <= 12
    assert len(result.error.message) < 4096
    assert len(json.dumps(candidates)) <= 3072
    for candidate in candidates:
        assert json.dumps(candidate) in result.text()
        assert not read(candidate).is_error


def test_enabled_namespaced_child_keeps_inherited_read_access(assets):
    root, catalog, read = assets
    child = write_skill(root / "leaf", "author.leaf")
    catalog.register(load_entry(child))
    (child.parent / "notes.md").write_text("LEAF CONTENT")
    result = read("leaf/missing.md", allowed=["author"])
    assert result.error.recovery_hint["candidates"] == ["leaf/SKILL.md", "leaf/notes.md"]
    recovered = read(result.error.recovery_hint["candidates"][1], allowed=["author"])
    assert not recovered.is_error
    assert "LEAF CONTENT" in recovered.text()
