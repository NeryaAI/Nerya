from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace

import pytest
from jsonschema import validate

from nerya.core.config import Config, DEFAULT_CONFIG
from nerya.core.paths import WorkspacePaths
from nerya.trading.components import trading_components
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


@pytest.mark.parametrize("mode", ["paper", "backtest"])
def test_paper_uniswap_discovery_finds_lp_actions_without_funds_scope(tmp_path, mode):
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
                "mode": mode,
                "protocol": "uniswap",
                "market": "ETH/USDC",
            },
        )
    )

    assert not result.is_error, result.text()
    data = tool_json_data(result)
    assert data["inventory_scope"] == "paper_backtest"
    assert data["financial_execution_allowed"] is False
    assert "capability_gap" not in data, data.get("capability_gap")
    component = next(row for row in data["components"] if row["id"] == "builtin:uniswap")
    assert sorted(component["actions"]) == [
        "lp_add",
        "lp_collect",
        "lp_remove",
    ]
    assert component["available"] is True
    assert component["live_verified"] is False
    assert data["matched_component"] == {
        "id": component["id"], "revision": component["revision"],
    }
    assert data["historical_data_verified"] is False
    assert cfg.get("runtime.financial_actor_scopes") == ["execute:strategy"]
    assert not cfg.paths.db.exists()


@pytest.mark.parametrize("mode", ["paper", "backtest"])
def test_aave_discovery_finds_lending_actions_without_claiming_history(tmp_path, mode):
    cfg = _config(tmp_path, ["execute:strategy"])
    registry = ToolRegistry()
    register_financial_tools(registry, SimpleNamespace(config=cfg))
    result = registry.get("financial_components").handler(ToolCall(
        name="financial_components", arguments={"mode": mode, "protocol": "aave"},
    ))

    assert not result.is_error, result.text()
    data = tool_json_data(result)
    assert "capability_gap" not in data, data.get("capability_gap")
    component = next(row for row in data["components"] if row["id"] == "builtin:aave")
    assert sorted(component["actions"]) == ["borrow", "lend_supply", "lend_withdraw", "repay"]
    assert component["available"] is True
    assert component["live_verified"] is False
    assert data["matched_component"] == {
        "id": component["id"], "revision": component["revision"],
    }
    assert data["inventory_scope"] == "paper_backtest"
    assert data["historical_data_verified"] is False
    assert data["financial_execution_allowed"] is False
    assert cfg.get("runtime.financial_actor_scopes") == ["execute:strategy"]
    assert not cfg.paths.db.exists()


@pytest.mark.parametrize("mode", [None, "live"])
def test_component_inventory_without_paper_mode_keeps_funds_scope_boundary(tmp_path, mode):
    cfg = _config(tmp_path, ["execute:strategy"])
    registry = ToolRegistry()
    register_financial_tools(registry, SimpleNamespace(config=cfg))

    result = registry.get("financial_components").handler(
        ToolCall(id="components", name="financial_components", arguments={"mode": mode} if mode else {})
    )

    assert result.is_error
    assert result.text() == "financial_scope_denied"


@pytest.mark.parametrize(("protocol", "kind", "supported"), [
    ("uniswap", "lp_add", True),
    ("aave", "lend_supply", True),
    ("uniswap", "swap", False),
    ("aave", "swap", False),
    ("uniswap", "borrow", False),
    ("jupiter", "lp_add", False),
])
def test_component_discovery_checks_only_the_requested_financial_kind(tmp_path, protocol, kind, supported):
    cfg = _config(tmp_path, ["execute:strategy"])
    registry = ToolRegistry()
    register_financial_tools(registry, SimpleNamespace(config=cfg))
    descriptor = registry.get("financial_components")
    request = {"mode": "backtest", "protocol": protocol, "kind": kind}
    validate(request, descriptor.input_schema)
    result = descriptor.handler(ToolCall(name="financial_components", arguments=request))

    assert not result.is_error, result.text()
    data = tool_json_data(result)
    component = next(row for row in data["components"] if row["id"] == data["matched_component"]["id"])
    if supported:
        assert "capability_gap" not in data, data.get("capability_gap")
        assert kind in component["actions"]
    else:
        gap = data["capability_gap"]
        assert gap["code"] == "paper_replay_component_unavailable"
        assert gap["status"] == "blocked"
        assert gap["retryable"] is False
        assert gap["requested"] == {"protocol": protocol, "kind": kind}
        assert gap["available_actions"] == sorted(component["actions"])
        assert gap["missing_actions"] == [kind]
        assert kind not in component["actions"]
    assert data["financial_execution_allowed"] is False
    assert data["historical_data_verified"] is False
    assert component["live_verified"] is False
    assert cfg.get("runtime.financial_actor_scopes") == ["execute:strategy"]
    assert not cfg.paths.db.exists()


@pytest.mark.parametrize(("protocol", "component_id", "kind", "parameters"), [
    ("uniswap_v3", "builtin:uniswap", "lp_add", {"pool_id": "test-pool"}),
    ("uniswap_v4", "builtin:uniswap", "lp_collect", {"pool_id": "test-pool"}),
    ("aave_v3", "builtin:aave", "lend_supply", {}),
    ("pancakeswap_v3", "builtin:pancakeswap", "lp_remove", {"pool_id": "test-pool"}),
])
def test_protocol_alias_discovery_matches_existing_component_routes(tmp_path, protocol, component_id, kind, parameters):
    cfg = _config(tmp_path, ["execute:strategy"])
    registry = ToolRegistry()
    register_financial_tools(registry, SimpleNamespace(config=cfg))
    result = registry.get("financial_components").handler(ToolCall(
        name="financial_components",
        arguments={"mode": "backtest", "protocol": protocol, "kind": kind},
    ))

    assert not result.is_error, result.text()
    data = tool_json_data(result)
    assert "capability_gap" not in data, data.get("capability_gap")
    # Resolve metadata only: no adapter construction, quotes, balances or keys.
    bound = trading_components(cfg).resolve({
        "kind": kind, "protocol": protocol, "parameters": parameters,
    })
    assert bound.id == component_id
    assert data["matched_component"] == {"id": bound.id, "revision": bound.revision}
    assert data["financial_execution_allowed"] is False
    assert data["historical_data_verified"] is False
    assert not cfg.paths.db.exists()


@pytest.mark.parametrize("protocol", ["unlisted_dex", "uniswap_v2", "aave_v2"])
@pytest.mark.parametrize("kind", [None, "lend_supply"])
def test_unknown_protocol_gap_does_not_invent_a_swap_request(tmp_path, protocol, kind):
    cfg = _config(tmp_path, ["execute:strategy"])
    registry = ToolRegistry()
    register_financial_tools(registry, SimpleNamespace(config=cfg))
    requested = {"protocol": protocol, "market": "ETH/USDC"}
    if kind:
        requested["kind"] = kind
    result = registry.get("financial_components").handler(ToolCall(
        name="financial_components", arguments={"mode": "backtest", **requested},
    ))

    assert not result.is_error, result.text()
    data = tool_json_data(result)
    gap = data["capability_gap"]
    assert gap["code"] == "paper_replay_component_unavailable"
    assert gap["status"] == "blocked"
    assert gap["retryable"] is False
    assert gap["missing_actions"] == ([kind] if kind else [])
    assert gap["requested"] == requested
    assert gap["available_actions"] == []
    assert "matched_component" not in data
    assert data["historical_data_verified"] is False
    assert data["financial_execution_allowed"] is False
    assert not cfg.paths.db.exists()


@pytest.mark.parametrize("mode", ["paper", "backtest"])
@pytest.mark.parametrize("kind", [None, "lp_add", "lp_rebalance"])
def test_discovery_without_protocol_does_not_claim_a_protocol_match(tmp_path, mode, kind):
    cfg = _config(tmp_path, ["trade:paper"])
    registry = ToolRegistry()
    register_financial_tools(registry, SimpleNamespace(config=cfg))
    arguments = {"mode": mode, "market": "ETH/USDC"}
    if kind:
        arguments["kind"] = kind
    result = registry.get("financial_components").handler(ToolCall(
        name="financial_components", arguments=arguments,
    ))

    assert not result.is_error, result.text()
    data = tool_json_data(result)
    assert data["components"]
    assert "matched_component" not in data
    if kind == "lp_rebalance":
        gap = data["capability_gap"]
        assert gap["status"] == "blocked"
        assert gap["requested"] == {"market": "ETH/USDC", "kind": kind}
        assert gap["missing_actions"] == [kind]
        assert kind not in gap["available_actions"]
        assert "lp_add" in gap["available_actions"]
    else:
        assert "capability_gap" not in data
    assert data["inventory_scope"] == "paper_backtest"
    assert data["historical_data_verified"] is False
    assert data["financial_execution_allowed"] is False
    assert all(row["live_verified"] is False for row in data["components"])
    assert cfg.get("runtime.financial_actor_scopes") == ["trade:paper"]
    assert not cfg.paths.db.exists()
