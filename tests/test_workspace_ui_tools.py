"""Native-tool coverage for conversation-driven workspace customization."""

from __future__ import annotations

from copy import deepcopy

import pytest

from nerya.core.config import DEFAULT_CONFIG, Config
from nerya.core.paths import WorkspacePaths
from nerya.tools.native.bootstrap import build_native_tool_deps, register_native_tools
from nerya.tools.native.workspace_ui import (
    workspace_ui_inspect_handler,
    workspace_ui_propose_handler,
)
from nerya.tools.registry import ToolRegistry
from nerya.tools.types import (
    PermissionScope,
    RiskLevel,
    ToolCall,
    ToolErrorKind,
)


pytestmark = pytest.mark.smoke


def _config(tmp_path) -> Config:
    return Config(paths=WorkspacePaths(root=tmp_path), data=deepcopy(DEFAULT_CONFIG))


def test_workspace_ui_native_tools_are_registered_with_review_safe_profiles(tmp_path) -> None:
    config = _config(tmp_path)
    registry = ToolRegistry()
    deps = build_native_tool_deps(
        workspace_root=tmp_path,
        skill_roots=[tmp_path],
        paths=config.paths,
        config=config,
    )
    register_native_tools(registry, deps)

    inspect = registry.get("workspace_ui_inspect")
    assert inspect.risk is RiskLevel.READ
    assert inspect.permission_scope is PermissionScope.WORKSPACE
    assert inspect.read_only is True
    assert inspect.is_concurrency_safe is True
    assert inspect.mutates_paths is False
    assert inspect.auto_approve is True

    propose = registry.get("workspace_ui_propose")
    assert propose.risk is RiskLevel.WRITE
    assert propose.permission_scope is PermissionScope.WORKSPACE
    assert propose.read_only is False
    assert propose.is_concurrency_safe is False
    assert propose.mutates_paths is True
    assert propose.auto_approve is False
    assert "never changes the live dashboard" in propose.description


def test_workspace_ui_inspect_returns_layout_revision_and_catalog(tmp_path) -> None:
    result = workspace_ui_inspect_handler(
        ToolCall(name="workspace_ui_inspect", arguments={}),
        config=_config(tmp_path),
    )

    assert not result.is_error
    data = result.content[0].data
    assert data["ok"] is True
    assert data["revision"] == 0
    assert data["summary"]["page_count"] == 0
    assert data["summary"]["home"]["widget_count"] == 0
    assert {row["kind"] for row in data["catalog"]["widget_kinds"]} >= {
        "chart", "market_ticker", "markdown", "skill_panel", "agent_panel",
    }
    assert data["manifest"]["version"] == 1


def test_workspace_ui_inspect_rejects_unknown_page(tmp_path) -> None:
    result = workspace_ui_inspect_handler(
        ToolCall(name="workspace_ui_inspect", arguments={"page_id": "missing"}),
        config=_config(tmp_path),
    )

    assert result.is_error
    assert result.error is not None
    assert result.error.kind == ToolErrorKind.NOT_FOUND


def test_workspace_ui_propose_creates_review_only_incremental_proposal(tmp_path) -> None:
    config = _config(tmp_path)
    result = workspace_ui_propose_handler(
        ToolCall(
            name="workspace_ui_propose",
            arguments={
                "summary": "Add market watch dashboard",
                "operations": [
                    {
                        "op": "upsert_widget",
                        "page": "home",
                        "widget": {
                            "id": "btc-ticker",
                            "kind": "market_ticker",
                            "title": "BTC/USDT",
                            "config": {"symbol": "BTCUSDT", "venue": "bybit"},
                        },
                    },
                    {
                        "op": "upsert_page",
                        "page": {
                            "id": "market-watch",
                            "title": "Market Watch",
                            "widgets": [],
                            "nav": {"section": "primary", "order": 3},
                        },
                    },
                ],
            },
        ),
        config=config,
    )

    assert not result.is_error
    data = result.content[0].data
    assert data["resource_kind"] == "workspace_ui"
    assert data["proposal"]["kind"] == "core_config_patch"
    assert data["proposal"]["metadata"]["workspace_ui"] is True
    assert data["affected"]["home"] is True
    assert data["affected"]["navigation"] is True
    assert data["affected"]["pages"] == ["market-watch"]
    assert data["affected"]["widgets"] == ["btc-ticker"]
    assert data["proposal_id"]
    assert data["diff"].startswith("--- a/ui/workspace.yml")
    assert not (tmp_path / "ui" / "workspace.yml").exists()
    staged = (
        config.paths.proposals
        / data["proposal_id"]
        / "after"
        / "ui"
        / "workspace.yml"
    )
    assert staged.exists()


def test_workspace_ui_propose_binds_to_current_revision_by_default(tmp_path) -> None:
    config = _config(tmp_path)
    result = workspace_ui_propose_handler(
        ToolCall(
            name="workspace_ui_propose",
            arguments={
                "summary": "Name the home dashboard",
                "operations": [
                    {"op": "update_home", "changes": {"title": "Operator Console"}},
                ],
            },
        ),
        config=config,
    )

    assert not result.is_error
    data = result.content[0].data
    assert data["base_revision"] == 0
    assert data["candidate_revision"] == 1


def test_workspace_ui_propose_surfaces_stale_revision_as_conflict(tmp_path) -> None:
    result = workspace_ui_propose_handler(
        ToolCall(
            name="workspace_ui_propose",
            arguments={
                "summary": "Stale edit",
                "base_revision": 99,
                "operations": [
                    {"op": "update_home", "changes": {"title": "Stale"}},
                ],
            },
        ),
        config=_config(tmp_path),
    )

    assert result.is_error
    assert result.error is not None
    assert result.error.kind == ToolErrorKind.CONFLICT


def test_workspace_ui_propose_requires_non_empty_operations(tmp_path) -> None:
    result = workspace_ui_propose_handler(
        ToolCall(
            name="workspace_ui_propose",
            arguments={"summary": "No-op", "operations": []},
        ),
        config=_config(tmp_path),
    )

    assert result.is_error
    assert result.error is not None
    assert result.error.kind == ToolErrorKind.SCHEMA_VALIDATION

