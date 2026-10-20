from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace

import pytest

from nerya.core.config import Config, DEFAULT_CONFIG
from nerya.core.paths import WorkspacePaths
from nerya.tools.native.connectors import connector_list_handler
from nerya.tools.native.financial import register_financial_tools
from nerya.tools.registry import ToolRegistry
from nerya.tools.result_contracts import tool_json_data
from nerya.tools.types import ToolCall


pytestmark = pytest.mark.smoke


def _config(tmp_path: Path, scopes: list[str]) -> Config:
    data = deepcopy(DEFAULT_CONFIG)
    data.setdefault("runtime", {}).update(
        financial_actor_id="assistant",
        financial_actor_scopes=scopes,
    )
    return Config(WorkspacePaths(tmp_path), data)


def test_paper_uniswap_discovery_returns_a_capability_gap_without_funds_scope(tmp_path):
    cfg = _config(tmp_path, ["execute:strategy"])
    connector = connector_list_handler(
        ToolCall(
            id="connectors",
            name="connector_list",
            arguments={"query": "uniswap"},
        ),
        config_like=cfg,
    )
    connector_data = tool_json_data(connector)
    evm = connector_data["connectors"][0]
    assert evm["id"] == "evm"
    assert evm["credential_status"]["status"] == "missing"
    assert evm["credential_status"]["configured"] is False

    registry = ToolRegistry()
    register_financial_tools(registry, SimpleNamespace(config=cfg))
    assert registry.get("financial_components").input_schema["required"] == []
    result = registry.get("financial_components").handler(
        ToolCall(
            id="components",
            name="financial_components",
            arguments={
                "mode": "paper",
                "protocol": "uniswap",
                "market": "ETH/USDC",
            },
        )
    )

    assert not result.is_error, result.text()
    data = tool_json_data(result)
    assert data["inventory_scope"] == "paper_backtest"
    assert data["financial_execution_allowed"] is False
    assert data["capability_gap"]["code"] == "paper_replay_component_unavailable"
    assert data["capability_gap"]["status"] == "blocked"
    assert data["capability_gap"]["retryable"] is False
    assert data["capability_gap"]["requested"] == {
        "protocol": "uniswap",
        "market": "ETH/USDC",
    }
    assert data["capability_gap"]["available_actions"] == [
        "lp_add",
        "lp_collect",
        "lp_remove",
    ]
    assert "stage a reviewed protocol replay component" in data["capability_gap"]["next_required_action"]


def test_component_inventory_without_paper_mode_keeps_funds_scope_boundary(tmp_path):
    cfg = _config(tmp_path, ["execute:strategy"])
    registry = ToolRegistry()
    register_financial_tools(registry, SimpleNamespace(config=cfg))

    result = registry.get("financial_components").handler(
        ToolCall(id="components", name="financial_components", arguments={})
    )

    assert result.is_error
    assert result.text() == "financial_scope_denied"
