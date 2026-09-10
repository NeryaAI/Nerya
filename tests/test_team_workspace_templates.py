"""Workspace team-template loading — ``<workspace>/teams/templates/*.yml``.

Covers the file-driven team topology surface added by the
extensibility upgrade (``docs/extensibility-upgrade.md``): YAML
round-trip via ``template_from_dict``, builtin-id shadow protection,
mtime cache invalidation, and the shipped example template.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from nerya.core import yaml_io
from nerya.core.paths import WorkspacePaths
from nerya.teams.templates import (
    BUILTIN_TEMPLATES,
    get_template,
    list_templates,
    load_workspace_templates,
    reload_workspace_templates,
    template_from_dict,
)

pytestmark = pytest.mark.smoke


def _write_template(root: Path, template_id: str, *, lead: str = "lead-1") -> Path:
    directory = root / "teams" / "templates"
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{template_id}.yml"
    yaml_io.dump(path, {
        "id": template_id,
        "description": f"test template {template_id}",
        "lead": lead,
        "members": [
            {
                "name": lead,
                "role": "research_manager",
                "subagent_name": "technical_analyst",
                "tier": "medium",
            }
        ],
        "tasks": [
            {
                "id": "t1",
                "owner": lead,
                "subagent_name": "technical_analyst",
                "subject": "do the thing",
            }
        ],
        "gates": [
            {"id": "g1", "kind": "required_tasks", "detail": {"tasks": ["t1"]}}
        ],
        "max_rounds": 1,
        "max_parallel": 2,
    })
    return path


def test_workspace_template_roundtrip(tmp_path) -> None:
    _write_template(tmp_path, "my_team")
    templates, errors = load_workspace_templates(WorkspacePaths(root=tmp_path))
    assert errors == []
    assert "my_team" in templates
    tpl = templates["my_team"]
    assert tpl.lead == "lead-1"
    assert tpl.members[0].subagent_name == "technical_analyst"
    assert tpl.tasks[0].depends_on == []
    assert tpl.gates[0].kind == "required_tasks"
    assert tpl.max_rounds == 1 and tpl.max_parallel == 2


def test_workspace_template_cannot_shadow_builtin(tmp_path) -> None:
    _write_template(tmp_path, "market_analysis_team")
    templates, errors = load_workspace_templates(WorkspacePaths(root=tmp_path))
    assert "market_analysis_team" not in templates
    assert any("collides with a builtin" in e for e in errors)
    # The builtin is untouched.
    assert "market_analysis_team" in BUILTIN_TEMPLATES


def test_workspace_template_bad_file_reports_error(tmp_path) -> None:
    directory = tmp_path / "teams" / "templates"
    directory.mkdir(parents=True)
    (directory / "broken.yml").write_text("id: [unclosed\n")
    _write_template(tmp_path, "good_team")
    templates, errors = load_workspace_templates(WorkspacePaths(root=tmp_path))
    assert "good_team" in templates
    assert len(errors) == 1 and "broken.yml" in errors[0]


def test_workspace_template_mtime_cache_reload(tmp_path) -> None:
    path = _write_template(tmp_path, "cached_team", lead="first-lead")
    paths = WorkspacePaths(root=tmp_path)
    templates, _ = load_workspace_templates(paths)
    assert templates["cached_team"].lead == "first-lead"

    # Same mtime — cached copy is reused even if the dict changes.
    data = yaml_io.load(path)
    data["lead"] = "second-lead"
    yaml_io.dump(path, data)
    templates, _ = load_workspace_templates(paths)
    assert templates["cached_team"].lead in {"first-lead", "second-lead"}

    reload_workspace_templates()
    templates, _ = load_workspace_templates(paths)
    assert templates["cached_team"].lead == "second-lead"


def test_get_template_and_listing_merge_sources(tmp_path) -> None:
    _write_template(tmp_path, "my_team")
    paths = WorkspacePaths(root=tmp_path)

    # Without paths: builtins only (backwards-compatible behaviour).
    assert get_template("my_team") is None
    assert all(t["source"] == "builtin" for t in list_templates())

    assert get_template("my_team", paths) is not None
    listing = {t["id"]: t for t in list_templates(paths)}
    assert listing["my_team"]["source"] == "workspace"
    assert listing["strategy_design_team"]["source"] == "builtin"


@pytest.mark.parametrize(
    "bad,expected_fragment",
    [
        ({"id": "", "description": "d", "lead": "l"}, "template.id"),
        ({"id": "x", "description": "", "lead": "l"}, "template.description"),
        (
            {
                "id": "x",
                "description": "d",
                "lead": "l",
                "members": [{"name": "m", "role": "r"}],
            },
            "members[0].subagent_name",
        ),
        (
            {
                "id": "x",
                "description": "d",
                "lead": "l",
                "members": [],
                "tasks": [{"id": "t", "owner": "o"}],
            },
            "tasks[0].subagent_name",
        ),
        (
            {
                "id": "x",
                "description": "d",
                "lead": "l",
                "members": [],
                "tasks": [],
                "max_rounds": "many",
            },
            "template.max_rounds",
        ),
    ],
)
def test_template_from_dict_validation(bad: dict, expected_fragment: str) -> None:
    with pytest.raises(ValueError) as excinfo:
        template_from_dict(bad)
    assert expected_fragment in str(excinfo.value)


def test_shipped_example_template_parses() -> None:
    repo_example = (
        Path(__file__).resolve().parent.parent
        / "workspace_template"
        / "teams"
        / "templates"
        / "overnight_watch_team.yml"
    )
    data = yaml_io.load(repo_example)
    tpl = template_from_dict(data)
    assert tpl.id == "overnight_watch_team"
    # The example is wired against real builtin subagent lanes.
    for member in tpl.members:
        assert member.subagent_name in {
            "technical_analyst",
            "risk_critic",
        }
