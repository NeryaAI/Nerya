"""Offline contracts for compact catalogs, safe references and composed roles."""
from __future__ import annotations

import re
from pathlib import Path

import pytest

from nerya.core.paths import WorkspacePaths
from nerya.skills.discovery import catalog_ids, catalog_parent
from nerya.skills.registry import SkillRegistry, _enabled_ok
from nerya.tools.native.skill import SkillIndex
from nerya.tools.native.skill_tool import skill_tool_handler
from nerya.tools.registry import ToolRegistry, compact_tool_catalog, make_native_descriptor
from nerya.tools.types import ToolCall, ToolResult
from nerya.workspace import prompt_bundles

pytestmark = pytest.mark.smoke


def _skill(root: Path, directory: str, name: str, parent: str = "") -> Path:
    path = root / directory / "SKILL.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    metadata = f"metadata:\n  nerya:\n    catalog_parent: {parent}\n" if parent else ""
    path.write_text(f"---\nname: {name}\ndescription: Test {name}\n{metadata}---\n# {name}\n", encoding="utf-8")
    return path


def _json(result: ToolResult) -> dict:
    return next(part.data for part in result.content if part.type == "json")


def _call(index: SkillIndex, **args) -> ToolResult:
    return skill_tool_handler(ToolCall(id="test-call", name="Skill", arguments=args), skill_index=index)


def test_catalog_folds_only_available_hubs_and_keeps_exact_lookup(tmp_path):
    hub = _skill(tmp_path, "research", "research")
    leaf = _skill(tmp_path, "news", "news", "research")
    index = SkillIndex([], skill_files=[hub, leaf])
    assert [r.skill_id for r in index.catalog()] == ["research"]
    assert len(index.records()) == 2
    assert not _call(index, skill="news").is_error
    assert [r.skill_id for r in SkillIndex([], skill_files=[leaf]).catalog()] == ["news"]
    assert _enabled_ok("finance.fund_admin.nav_tieout", {"finance.fund_admin"})
    assert not _enabled_ok("finance.fund_admin", {"finance.fund_admin.nav_tieout"})


def test_recursive_discovery_skips_assets_and_unrelated_names_are_not_folded(tmp_path):
    _skill(tmp_path, "hub", "hub")
    _skill(tmp_path, "hub/child", "hub.child")
    _skill(tmp_path, "hub/unrelated", "custom")
    _skill(tmp_path, "hub/references/example", "do-not-discover")
    index = SkillIndex([tmp_path])
    assert {r.skill_id for r in index.records()} == {"hub", "hub.child", "custom"}
    assert {r.skill_id for r in index.catalog()} == {"hub", "custom"}


def test_catalog_cycles_and_invalid_metadata_fail_open():
    rows = [("a", None, "b"), ("b", None, "a"), ("c", None, "missing")]
    assert catalog_ids(rows) == {"a", "b", "c"}
    for value in (None, [], {"nerya": []}, {"nerya": {"catalog_parent": 7}}):
        assert catalog_parent(value) == ""


def test_builtin_catalog_and_native_catalog_agree_and_cover_finance_references():
    registry = SkillRegistry.load_builtin()
    entries = registry.list()
    index = SkillIndex([], skill_files=[e.manifest.path / "SKILL.md" for e in entries])
    assert {e.manifest.id for e in registry.catalog()} == {r.skill_id for r in index.catalog()}
    hubs = [e.manifest for e in registry.catalog() if e.manifest.id.startswith("finance.")]
    assert len(hubs) == 7
    for hub in hubs:
        methods = [e.manifest for e in entries if e.manifest.id.startswith(hub.id + ".")]
        assert methods
        for method in methods:
            reference = f"{method.path.relative_to(hub.path).as_posix()}/references/full-playbook.md"
            assert reference in hub.instructions, method.id
            assert (hub.path / reference).is_file(), reference
            assert not _call(index, skill=hub.id, file=reference, limit=2).is_error
    assert len(index.records()) == len(entries)
    print(f"\nCATALOG registered={len(entries)} primary={len(index.catalog())} finance_hubs={len(hubs)}")


def test_new_canonical_reference_paths_exist():
    registry = SkillRegistry.load_builtin()
    for name in ("research", "markets", "analysis", "team"):
        manifest = registry.get(name).manifest
        for path in re.findall(r"`(references/[^`]+\.md)`", manifest.instructions):
            assert (manifest.path / path).is_file(), (name, path)


def test_skill_list_and_bounded_reference_pages(tmp_path):
    path = _skill(tmp_path, "sample", "sample")
    reference = path.parent / "references" / "long.md"
    reference.parent.mkdir()
    reference.write_text("\n".join(f"line {i}" for i in range(205)), encoding="utf-8")
    index = SkillIndex([tmp_path])
    listing = _call(index, action="list")
    assert not listing.is_error
    assert _json(listing)["skills"][0]["skill_id"] == "sample"
    first = _call(index, skill="sample", file="references/long.md")
    assert not first.is_error
    assert first.tool_use_id == "test-call" and first.name == "Skill"
    assert _json(first)["next_offset"] == 200
    last = _call(index, action="read", skill="sample", file="references/long.md", offset=200)
    assert _json(last)["next_offset"] is None
    page_text = next(part.text for part in last.content if part.type == "text")
    assert page_text.splitlines() == [f"line {i}" for i in range(200, 205)]


@pytest.mark.parametrize("args", [
    {"action": "run", "skill": "sample"},
    {"action": "read", "skill": "sample"},
    {"skill": "sample", "file": "SKILL.md", "limit": 0},
    {"skill": "sample", "file": "SKILL.md", "offset": -1},
    {"skill": "sample", "file": "SKILL.md", "offset": True},
])
def test_skill_rejects_execution_and_invalid_read_arguments(tmp_path, args):
    _skill(tmp_path, "sample", "sample")
    assert _call(SkillIndex([tmp_path]), **args).is_error


def test_references_and_script_inspection_cannot_escape_skill_root(tmp_path):
    path = _skill(tmp_path, "sample", "sample")
    outside = tmp_path / "outside.txt"
    outside.write_text("outside secret", encoding="utf-8")
    (path.parent / "escape.md").symlink_to(outside)
    index = SkillIndex([tmp_path])
    for file in ("../outside.txt", str(outside), "escape.md"):
        assert _call(index, skill="sample", file=file).is_error
    external_scripts = tmp_path / "external_scripts"
    external_scripts.mkdir()
    (external_scripts / "bad.py").write_text("raise AssertionError('never execute')", encoding="utf-8")
    (path.parent / "scripts").symlink_to(external_scripts, target_is_directory=True)
    assert _call(index, action="inspect", skill="sample", script="bad.py").is_error


def test_inspect_is_read_only_and_invalid_encoding_is_a_tool_error(tmp_path):
    path = _skill(tmp_path, "sample", "sample")
    scripts = path.parent / "scripts"
    scripts.mkdir()
    (scripts / "helper.py").write_text("raise AssertionError('never execute')", encoding="utf-8")
    (path.parent / "binary.md").write_bytes(b"\xff\xfe")
    index = SkillIndex([tmp_path])
    inspected = _call(index, action="inspect", skill="sample", script="helper.py")
    assert not inspected.is_error and "never execute" in inspected.text()
    assert _call(index, skill="sample", file="binary.md").is_error


def _descriptor(name):
    return make_native_descriptor(name=name, description=name, input_schema={"type": "object"}, handler=lambda call: ToolResult(name=call.name))


def test_compact_tools_preserve_legacy_only_policies_and_execution_entries():
    names = ["Skill", "skill_index", "skill_view", "script_inspect", "script_run", "task_get", "task_output", "trade_intent_submit"]
    descriptors = [_descriptor(name) for name in names]
    compact = {d.name for d in compact_tool_catalog(descriptors)}
    assert compact == {"Skill", "script_run", "task_get", "trade_intent_submit"}
    legacy_only = [d for d in descriptors if d.name in {"skill_view", "task_output"}]
    assert compact_tool_catalog(legacy_only) == legacy_only
    registry = ToolRegistry()
    for descriptor in descriptors:
        registry.register(descriptor)
    assert {d.name for d in registry.list_tools()} == set(names)
    assert registry.get("skill_view") is not None
    assert len(registry.to_provider_tools()) == len(compact)


def test_main_loop_compacts_only_after_permission_and_lazy_filters():
    from types import SimpleNamespace
    from nerya.agent.loop import WorkspaceNativeAgentLoop

    registry = ToolRegistry()
    for name in ("Skill", "skill_view", "script_inspect", "script_run"):
        registry.register(_descriptor(name))
    loop = WorkspaceNativeAgentLoop(
        gateway=SimpleNamespace(), registry=registry, orchestrator=SimpleNamespace(),
    )
    def visible(rows):
        return {row["name"] for row in rows}

    assert visible(loop._render_tools(None)) == {"Skill", "script_run"}
    def legacy_only(tool):
        return tool.name in {"skill_view", "script_inspect"}

    assert visible(loop._render_tools(legacy_only)) == {"skill_view", "script_inspect"}
    registry.lazy_mcp_state = SimpleNamespace(is_visible=lambda tool: tool.name != "Skill")
    assert visible(loop._render_tools(None)) == {"skill_view", "script_inspect", "script_run"}


def test_shared_role_contracts_are_composed_once_and_main_uses_real_trade_entry():
    from nerya.security.prompt_injection import flag_suspicious
    bundle = prompt_bundles.load_bundle()
    assert flag_suspicious(bundle.subagents["strategy_tuner"]) == []
    for name, prompt in bundle.subagents.items():
        assert prompt.count("## Shared worker contract") == 1, name
        if name.endswith("_lens"):
            assert prompt.count("## Shared expert-lens result") == 1, name
            assert "facts_used" in prompt and "framework_inferences" in prompt
    assert "trade_intent_submit" in bundle.agents["system"]
    assert "skill:trading.submit_trade_intent" not in str(bundle.agents)
    for name in ("execution_planner", "portfolio_manager", "strategy_reviewer", "message_writer"):
        assert "Return JSON" in bundle.subagents[name]


@pytest.mark.parametrize("reference", [[], ["system.md", 1], ["../outside.md"], 7])
def test_composed_prompt_validation_rejects_invalid_or_escaping_paths(monkeypatch, reference):
    manifest = {"version": 1, "agents": {}, "subagents": {"test": reference}}
    monkeypatch.setattr(prompt_bundles.yaml_io, "load", lambda _path: manifest)
    with pytest.raises((ValueError, FileNotFoundError)):
        prompt_bundles.load_bundle()


def test_composed_prompt_deduplicates_parts_and_preserves_operator_edits(tmp_path, monkeypatch):
    original_bundle = prompt_bundles.load_bundle()
    manifest = {"version": 1, "agents": {}, "subagents": {"test": ["system.md", "policies.md", "system.md"]}}
    with monkeypatch.context() as patch:
        patch.setattr(prompt_bundles.yaml_io, "load", lambda _path: manifest)
        composed = prompt_bundles.load_bundle().subagents["test"]
        assert composed.count("# Nerya system prompt") == 1
        assert composed.count("# Policies") == 1
    paths = WorkspacePaths(root=tmp_path)
    prompt_bundles.seed_bundle(paths, original_bundle)
    target = paths.subagents / "market_analyst.agent.md"
    target.write_text("operator's custom role", encoding="utf-8")
    prompt_bundles.seed_bundle(paths, original_bundle)
    assert target.read_text(encoding="utf-8") == "operator's custom role"
