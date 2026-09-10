"""plugin_author skill — agent-authored plugins through the proposal lane.

Covers the full loop promised by the skill: static validation that
never executes draft code, proposal staging (``plugin_proposal``,
``pending_review``, never auto-applied), promotion into
``plugins/<id>/``, and load/activation via the harness loader. Also
checks the ``self_modify`` routing table points at the new lane.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from nerya.core.paths import WorkspacePaths
from nerya.evolution.patch_proposal import list_proposals, set_state
from nerya.evolution.promotion import apply_proposal
from nerya.harness.loader import build_host
from nerya.skills.builtin.plugin_author.scripts.propose_plugin import run as propose_run
from nerya.skills.builtin.plugin_author.scripts.validate_plugin import run as validate_run
from nerya.skills.manifest import SkillManifest
from nerya.skills.registry import list_bundled_skill_names
from nerya.workspace.manager import _DEFAULT_ENABLED_SKILLS

pytestmark = pytest.mark.smoke

SKILL_DIR = (
    Path(__file__).resolve().parents[1]
    / "nerya" / "skills" / "builtin" / "plugin_author"
)

_GOOD_PLUGIN = '''
from nerya.harness import Plugin, PluginContext
from nerya.tools.types import (
    PermissionScope, RiskLevel, ToolDescriptor, ToolResult,
)

SCHEMA = {"type": "object", "properties": {"q": {"type": "string"}}}


class LookupPlugin(Plugin):
    name = "lookup"
    requires = ()

    def setup(self, ctx: PluginContext):
        def handler(call):
            return ToolResult.from_text(
                tool_use_id=call.id,
                name="user_lookup",
                text="result",
                semantic_success=True,
            )

        ctx.register_tool(ToolDescriptor(
            name="user_lookup",
            description="Proposed lookup tool.",
            input_schema=SCHEMA,
            handler=handler,
            risk=RiskLevel.READ,
            permission_scope=PermissionScope.NETWORK,
        ))
        return None


PLUGIN = LookupPlugin()
'''


# --------------------------------------------------------------- skill shape


def test_skill_manifest_parses_with_trigger_description() -> None:
    manifest = SkillManifest.from_skill_md(SKILL_DIR / "SKILL.md")
    assert manifest.id == "plugin_author"
    # Context-selection: the description must say when to reach for it.
    assert "workspace plugin" in manifest.description
    assert "proposal" in manifest.description.lower()
    assert (SKILL_DIR / "references" / "full-playbook.md").exists()


def test_skill_is_default_enabled_and_bundled() -> None:
    assert "plugin_author" in _DEFAULT_ENABLED_SKILLS
    assert "plugin_author" in list_bundled_skill_names()


def test_self_modify_routes_plugin_requests_to_plugin_author() -> None:
    text = (
        SKILL_DIR.parent / "self_modify" / "SKILL.md"
    ).read_text(encoding="utf-8")
    assert "plugin_author" in text
    assert "plugin_proposal" in text
    assert "never `write_file`\n  into `plugins/`" in text or (
        "never `write_file`" in text and "plugins/" in text
    )


# --------------------------------------------------------------- propose lane


def test_propose_plugin_stages_pending_review_proposal(tmp_path) -> None:
    result = propose_run(
        workspace=str(tmp_path),
        plugin_id="sec_lookup",
        code=_GOOD_PLUGIN,
        summary="Add SEC lookup plugin",
    )
    assert result["ok"] is True
    assert result["state"] == "pending_review"
    assert result["target"] == "plugins/sec_lookup/plugin.py"

    paths = WorkspacePaths(root=tmp_path)
    proposal = list_proposals(paths)[0]
    assert proposal.kind == "plugin_proposal"
    staged = proposal.path / "after" / "plugins" / "sec_lookup" / "plugin.py"
    assert staged.read_text(encoding="utf-8") == _GOOD_PLUGIN
    # Nothing was installed live.
    assert not (paths.plugins / "sec_lookup").exists()


@pytest.mark.parametrize(
    "plugin_id,code,fragment",
    [
        ("Bad ID", _GOOD_PLUGIN, "plugin_id must match"),
        ("ok_id", "def broken(:", "syntax error"),
        ("ok_id", "X = 1\n", "must expose PLUGIN"),
        (
            "ok_id",
            "import subprocess\n\nPLUGIN = 1\n",
            "module-level import 'subprocess'",
        ),
        (
            "ok_id",
            "exec('boom')\n\nPLUGIN = 1\n",
            "module-level exec()",
        ),
    ],
)
def test_propose_plugin_fails_closed_on_bad_input(
    tmp_path, plugin_id: str, code: str, fragment: str
) -> None:
    result = propose_run(workspace=str(tmp_path), plugin_id=plugin_id, code=code)
    assert result["ok"] is False
    assert result["staged"] is False
    assert any(fragment in e for e in result["errors"])
    assert list_proposals(WorkspacePaths(root=tmp_path)) == []


# --------------------------------------------------------------- full lane


def test_propose_approve_apply_load_roundtrip(tmp_path) -> None:
    result = propose_run(
        workspace=str(tmp_path),
        plugin_id="roundtrip",
        code=_GOOD_PLUGIN,
    )
    assert result["ok"] is True

    paths = WorkspacePaths(root=tmp_path)
    set_state(paths, result["proposal_id"], "approved")
    applied = apply_proposal(paths, result["proposal_id"])
    assert applied.get("ok") is True, applied
    installed = paths.plugins / "roundtrip" / "plugin.py"
    assert installed.read_text(encoding="utf-8") == _GOOD_PLUGIN

    # The installed plugin loads through the same path the kernel uses.
    host, load_errors = build_host(paths.plugins, services={"paths": paths})
    assert load_errors == []
    assert "user:roundtrip" in host.active_plugins
    from nerya.tools.registry import ToolRegistry

    registry = ToolRegistry()
    assert host.attach_tools(registry) == 1
    assert registry.get("user_lookup") is not None


def test_plugin_proposals_are_never_auto_appliable(tmp_path) -> None:
    from nerya.evolution.auto_apply import AUTO_APPLY_KINDS

    assert "plugin_proposal" not in AUTO_APPLY_KINDS


# --------------------------------------------------------------- diagnostics


def test_validate_plugin_reports_installed_plugin(tmp_path) -> None:
    plugin_dir = tmp_path / "plugins" / "diag"
    plugin_dir.mkdir(parents=True)
    (plugin_dir / "plugin.py").write_text(_GOOD_PLUGIN, encoding="utf-8")

    report = validate_run(workspace=str(tmp_path), plugin_id="diag")
    assert report["ok"] is True
    assert report["plugin"] == "user:diag"
    assert report["tools_registered"] == 1
    assert report["tool_names"] == ["user_lookup"]


def test_validate_plugin_reports_missing_and_broken(tmp_path) -> None:
    missing = validate_run(workspace=str(tmp_path), plugin_id="ghost")
    assert missing["ok"] is False

    broken_dir = tmp_path / "plugins" / "broken"
    broken_dir.mkdir(parents=True)
    (broken_dir / "plugin.py").write_text("def broken(:\n", encoding="utf-8")
    broken = validate_run(workspace=str(tmp_path), plugin_id="broken")
    assert broken["ok"] is False
    assert any("import failed" in e for e in broken["errors"])
