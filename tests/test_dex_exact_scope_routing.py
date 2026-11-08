"""Offline boundary from childacmd_68b276cfbf9495ddcfc9d43d.

Exercise installed Skill reads and producer contracts, not model compliance or
historical replay. No live candidate, service, wallet or network is accessed.
"""
from copy import deepcopy
import json
from pathlib import Path
import re
import sys
from types import ModuleType, SimpleNamespace

import pytest
from jsonschema import Draft7Validator

from nerya.core.config import Config, DEFAULT_CONFIG
from nerya.core.paths import WorkspacePaths
from nerya.tools.native.connectors import connector_list_handler, market_data_handler
from nerya.tools.native.financial import register_financial_tools
from nerya.tools.native.skill import SkillIndex
from nerya.tools.native.skill_tool import skill_tool_handler
from nerya.tools.registry import ToolRegistry
from nerya.tools.result_contracts import tool_json_data
from nerya.tools.types import ToolCall

pytestmark = pytest.mark.smoke
BUILTINS = Path(__file__).parents[1] / "nerya/skills/builtin"
ORIGINAL = "帮我做一个 Jupiter SOL/USDC 趋势策略，回测最近30天，不启用交易。"


@pytest.fixture
def producer(tmp_path, monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("discovery must not access network, secrets or financial actions")

    monkeypatch.setattr("socket.socket.connect", forbidden)
    monkeypatch.setattr("nerya.security.secrets.SecretVault.resolve", forbidden)
    monkeypatch.setattr("nerya.financial.gateway.FinancialGateway.prepare", forbidden)
    monkeypatch.setattr("nerya.financial.gateway.FinancialGateway.execute", forbidden)
    data = deepcopy(DEFAULT_CONFIG)
    data["runtime"].update(financial_actor_id="assistant",
                           financial_actor_scopes=["execute:strategy"])
    config = Config(WorkspacePaths(tmp_path), data)
    registry = ToolRegistry()
    register_financial_tools(registry, SimpleNamespace(config=config))
    return config, registry.get("financial_components")


def read_skill(**arguments):
    pages = []
    index = SkillIndex([BUILTINS])
    offset = 0
    while True:
        result = skill_tool_handler(ToolCall(name="Skill", arguments={**arguments, "offset": offset}),
                                    skill_index=index)
        assert not result.is_error, result.text()
        pages.append(result.content[0].text)
        next_offset = tool_json_data(result)["next_offset"]
        if next_offset is None:
            return "\n".join(pages)
        assert next_offset > offset
        offset = next_offset


@pytest.mark.parametrize("mode", ["paper", "backtest"])
@pytest.mark.parametrize("requested", [
    {"protocol": "Jupiter", "market": "SOL/USDC", "kind": "swap"},
    {"protocol": "aave_v3", "market": "USDC", "kind": "lend_supply"},
    {"protocol": "uniswap_v3", "market": "ETH/USDC", "kind": "lp_add"},
    {"market": "SOL/USDC"},
    {},
])
def test_inventory_routes_to_exact_source_without_failing_supported_actions(producer, mode, requested):
    config, descriptor = producer
    call = ToolCall(name=descriptor.name, arguments={"mode": mode, **requested})
    before = deepcopy(call.arguments)
    Draft7Validator(descriptor.input_schema).validate(call.arguments)
    result = descriptor.handler(call)
    assert not result.is_error, result.text()
    inventory = tool_json_data(result)
    assert inventory["requested"] == requested
    assert "capability_gap" not in inventory
    assert inventory["historical_data_verified"] is False
    assert inventory["financial_execution_allowed"] is False
    hint = inventory["next_required_action"]
    assert hint["tool"] == "Skill"
    assert hint["arguments"] == {"skill": "backtest", "file": "references/replay-capabilities.md"}
    assert hint["requested"] == requested
    assert hint["preserve_requested_scope"] is True
    assert hint["allow_proxy_substitution"] is False
    reference = read_skill(**hint["arguments"])
    assert "missing source/window" in reference
    assert call.arguments == before
    assert config.get("runtime.financial_actor_scopes") == ["execute:strategy"]
    assert not config.paths.db.exists()


def test_ccxt_failure_does_not_replace_skill_request_or_swap_support(producer, monkeypatch):
    config, descriptor = producer
    # Exercise the real enumerator's unsupported-exchange branch, not HTTP.
    monkeypatch.setitem(sys.modules, "ccxt", ModuleType("ccxt"))
    listed = market_data_handler(ToolCall(name="market_data", arguments={
        "action": "list_symbols", "venue": "jupiter",
    }), config_like=config)
    assert tool_json_data(listed)["error"] == "ccxt has no exchange named 'jupiter'"
    catalog = connector_list_handler(ToolCall(name="connector_list",
        arguments={"query": "jupiter"}), config_like=config)
    assert tool_json_data(catalog)["count"] == 0

    text = read_skill(skill="strategy_author", file="references/mainstream-strategies.md")
    assert ORIGINAL in text
    section = text.split("## Exact-source recovery for Jupiter SOL/USDC", 1)[1].split("\n## ", 1)[0]
    example = json.loads(re.search(r"```json\s*(.*?)\s*```", section, re.S).group(1))
    assert example["tool"] == descriptor.name
    assert example["arguments"] == {"mode": "backtest", "protocol": "jupiter",
                                       "market": "SOL/USDC", "kind": "swap"}
    Draft7Validator(descriptor.input_schema).validate(example["arguments"])
    inventory = tool_json_data(descriptor.handler(ToolCall(
        name=example["tool"], arguments=example["arguments"])))
    assert inventory["matched_component"]["id"] == "builtin:trading"
    assert inventory["requested"]["market"] == "SOL/USDC"
    assert "capability_gap" not in inventory
    assert inventory["historical_data_verified"] is False
    # These are instruction-regression checks, not semantic enforcement.
    for boundary in ("requested_scope.summary", "model_interpretation",
                     "limitations_are_authorization=false", "BINANCE:SOLUSDT",
                     "USDC is not USDT", "disabled candidate", "UTC"):
        assert boundary in section
    financial = read_skill(skill="financial_ops", file="references/mainstream-defi.md")
    assert "market_data_list_symbols" in financial
    assert "historical OHLC" in financial
    assert not config.paths.db.exists()
