"""Focused safety and lifecycle tests for declarative workspace UI."""

from __future__ import annotations

from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace

import pytest

from nerya.api import route_scopes, routes_operator, routes_workspace_ui
from nerya.core.config import DEFAULT_CONFIG, Config
from nerya.core.paths import WorkspacePaths
from nerya.evolution.patch_proposal import list_proposals, set_state
from nerya.workspace import ui


pytestmark = pytest.mark.smoke


def _client(root: Path):
    config = Config(paths=WorkspacePaths(root=root), data=deepcopy(DEFAULT_CONFIG))
    return SimpleNamespace(config=config)


def _approve_and_apply(paths: WorkspacePaths, proposal_id: str):
    assert set_state(paths, proposal_id, "approved") is not None
    return ui.apply(paths, proposal_id)


def test_default_manifest_is_versioned_and_catalogued(tmp_path):
    result = ui.read(WorkspacePaths(tmp_path))

    assert result["ok"] is True
    assert result["status"] == "ok"
    assert result["source"] == "default"
    assert result["path"] == "ui/workspace.yml"
    assert result["manifest"] == {"version": 1, "home": {"widgets": []}, "pages": []}
    assert {row["kind"] for row in result["catalog"]["widget_kinds"]} >= {
        "kpi", "chart", "table", "attention", "markdown", "skill_panel",
    }


def test_manifest_rejects_unknown_widget_and_executable_config(tmp_path):
    result = ui.validate_manifest(
        {
            "version": 1,
            "home": {
                "widgets": [
                    {
                        "id": "unsafe",
                        "kind": "remote_component",
                        "config": {"script": "alert(1)"},
                    }
                ]
            },
            "pages": [],
        }
    )

    assert not result.ok
    assert any("not registered" in error for error in result.errors)
    assert any("not allowed" in error for error in result.errors)


def test_propose_is_review_only_then_apply_writes_canonical_manifest(tmp_path):
    paths = WorkspacePaths(tmp_path)
    body = {
        "manifest": {
            "version": 1,
            "home": {
                "widgets": [
                    {"id": "btc-price", "kind": "chart", "config": {"symbol": "BTCUSDT"}}
                ]
            },
            "pages": [
                {
                    "id": "market-watch",
                    "title": "Market Watch",
                    "widgets": [],
                    "nav": {"section": "primary", "order": -1},
                }
            ],
        },
        "summary": "Add a market watch page",
    }

    proposed = ui.propose(paths, body)
    assert proposed["ok"] is True
    assert proposed["state"] == "pending_review"
    assert proposed["proposal_id"]
    assert not (tmp_path / "ui" / "workspace.yml").exists()

    proposals = list_proposals(paths)
    assert len(proposals) == 1
    assert proposals[0].target == "ui/workspace.yml"

    applied = _approve_and_apply(paths, proposed["proposal_id"])
    assert applied["ok"] is True
    assert applied["applied"] is True
    assert applied["revision"] == 1
    assert applied["path"] == "ui/workspace.yml"
    assert (tmp_path / "ui" / "workspace.yml").is_file()
    assert applied["manifest"]["pages"][0]["id"] == "market-watch"


def test_legacy_manifest_is_read_but_canonical_path_wins(tmp_path):
    legacy = tmp_path / "workspace" / "ui.yml"
    legacy.parent.mkdir(parents=True)
    legacy.write_text(
        "version: 1\nhome:\n  widgets: []\npages:\n  - id: old-page\n    title: Old Page\n    widgets: []\n",
        encoding="utf-8",
    )
    result = ui.read(WorkspacePaths(tmp_path))
    assert result["ok"] is True
    assert result["source"] == "legacy"
    assert result["path"] == "workspace/ui.yml"
    assert result["manifest"]["pages"][0]["id"] == "old-page"

    canonical = tmp_path / "ui" / "workspace.yml"
    canonical.parent.mkdir(parents=True)
    canonical.write_text(
        "version: 1\nhome:\n  widgets: []\npages:\n  - id: new-page\n    title: New Page\n    widgets: []\n",
        encoding="utf-8",
    )
    assert ui.read(WorkspacePaths(tmp_path))["manifest"]["pages"][0]["id"] == "new-page"


def test_structured_patch_adds_widget_and_page_and_nav(tmp_path):
    paths = WorkspacePaths(tmp_path)
    result = ui.propose(
        paths,
        {
            "patch": {
                "operations": [
                    {
                        "op": "add_widget",
                        "page": "home",
                        "widget": {"id": "overview-kpi", "kind": "kpi", "config": {"metric": "equity"}},
                    },
                    {
                        "op": "add_page",
                        "page": {"id": "research", "title": "Research", "widgets": []},
                    },
                    {"op": "set_nav", "page_id": "research", "nav": {"section": "primary", "order": 2}},
                ]
            }
        },
    )
    assert result["ok"] is True
    applied = _approve_and_apply(paths, result["proposal_id"])
    assert applied["manifest"]["home"]["widgets"][0]["id"] == "overview-kpi"
    assert applied["manifest"]["pages"][0]["nav"]["section"] == "primary"

    entries, warnings = ui.nav_pages(applied)
    assert warnings == []
    assert entries[0]["href"] == "/workspace/pages/research"
    assert entries[0]["id"] == "research"
    assert entries[0]["_section"] == "primary"


def test_structured_patch_upserts_home_widget_page_and_metadata(tmp_path):
    paths = WorkspacePaths(tmp_path)
    first = ui.propose(
        paths,
        {
            "operations": [
                {
                    "op": "upsert_widget",
                    "page": "home",
                    "widget": {
                        "id": "btc-ticker",
                        "kind": "market_ticker",
                        "title": "BTC",
                        "config": {
                            "symbol": "BTCUSDT",
                            "venue": "bybit",
                            "refresh_seconds": 10,
                        },
                    },
                },
                {
                    "op": "upsert_page",
                    "page": {
                        "id": "market-watch",
                        "title": "Market Watch",
                        "widgets": [],
                        "nav": {
                            "label": "Markets",
                            "section": "primary",
                            "order": 2,
                            "hidden": False,
                        },
                    },
                },
            ]
        },
    )
    assert first["ok"] is True
    _approve_and_apply(paths, first["proposal_id"])

    second = ui.propose(
        paths,
        {
            "operations": [
                {
                    "op": "update_home",
                    "changes": {"title": "Operator Console", "description": "Live overview"},
                },
                {
                    "op": "upsert_widget",
                    "page": "home",
                    "widget": {
                        "id": "btc-ticker",
                        "title": "BTC / USDT",
                        "span": "half",
                        "config": {"refresh_seconds": 30},
                    },
                },
                {
                    "op": "upsert_page",
                    "page": {
                        "id": "market-watch",
                        "description": "Read-only market monitoring",
                        "nav": {"order": 4},
                    },
                },
                {
                    "op": "upsert_widget",
                    "page": "market-watch",
                    "widget": {
                        "id": "market-note",
                        "kind": "markdown",
                        "text": "Read-only market view",
                    },
                },
            ]
        },
    )
    assert second["ok"] is True
    applied = _approve_and_apply(paths, second["proposal_id"])

    assert applied["manifest"]["home"]["title"] == "Operator Console"
    assert len(applied["manifest"]["home"]["widgets"]) == 1
    home_widget = applied["manifest"]["home"]["widgets"][0]
    assert home_widget["kind"] == "market_ticker"
    assert home_widget["title"] == "BTC / USDT"
    assert home_widget["config"] == {
        "symbol": "BTCUSDT",
        "venue": "bybit",
        "refresh_seconds": 30,
    }
    assert home_widget["span"] == "half"
    page = applied["manifest"]["pages"][0]
    assert page["title"] == "Market Watch"
    assert page["description"] == "Read-only market monitoring"
    assert page["nav"] == {
        "label": "Markets",
        "section": "primary",
        "order": 4,
        "hidden": False,
    }
    assert page["widgets"][0]["id"] == "market-note"


def test_structured_patch_remove_can_be_idempotent(tmp_path):
    paths = WorkspacePaths(tmp_path)
    result = ui.propose(
        paths,
        {
            "operations": [
                {"op": "remove_widget", "page": "home", "id": "missing", "ignore_missing": True},
                {"op": "update_home", "changes": {"title": "Still valid"}},
            ]
        },
    )
    assert result["ok"] is True
    applied = _approve_and_apply(paths, result["proposal_id"])
    assert applied["manifest"]["home"]["title"] == "Still valid"


def test_routes_and_scopes_are_registered(tmp_path):
    route_map = {(method, path): handler for method, path, handler in routes_workspace_ui.routes()}
    client = _client(tmp_path)
    response = route_map[("GET", "/workspace/ui")](client, {})
    assert response["manifest"]["version"] == 1
    assert route_scopes.required_scope("GET", "/workspace/ui") == "read:runtime"
    assert route_scopes.required_scope("POST", "/workspace/ui/propose") == "write:config"
    assert route_scopes.required_scope("POST", "/workspace/ui/apply") == "write:config"


def test_operator_nav_merges_custom_page_safely(tmp_path):
    paths = WorkspacePaths(tmp_path)
    proposed = ui.propose(
        paths,
        {
            "manifest": {
                "version": 1,
                "home": {"widgets": []},
                "pages": [{"id": "market-watch", "title": "Market Watch", "widgets": []}],
            }
        },
    )
    _approve_and_apply(paths, proposed["proposal_id"])
    data = routes_operator._nav_handler(_client(tmp_path), {})["data"]
    custom = [entry for entry in data["advanced"] if entry.get("workspace_ui")]
    assert len(custom) == 1
    assert custom[0]["href"] == "/workspace/pages/market-watch"
