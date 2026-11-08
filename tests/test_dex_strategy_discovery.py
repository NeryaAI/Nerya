"""The Jupiter request must reach financial capabilities without funds access."""
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace

import pytest

from nerya.core.config import Config, DEFAULT_CONFIG
from nerya.core.paths import WorkspacePaths
from nerya.financial.readiness import execution_readiness
from nerya.trading.components import trading_components
from nerya.tools.native.connectors import connector_list_handler
from nerya.tools.native.financial import register_financial_tools
from nerya.tools.native.skill import SkillIndex
from nerya.tools.native.skill_tool import skill_tool_handler
from nerya.tools.registry import ToolRegistry
from nerya.tools.result_contracts import tool_json_data
from nerya.tools.types import ToolCall

pytestmark = pytest.mark.smoke
BUILTINS = Path(__file__).parents[1] / "nerya/skills/builtin"


def test_loaded_skills_stop_at_missing_history_before_package_writes():
    # Instruction regression, not proof of model compliance or a real replay.
    index = SkillIndex([BUILTINS])
    for skill, file in [
        ("strategy_author", None), ("financial_ops", None),
        ("strategy_author", "references/mainstream-strategies.md"),
        ("financial_ops", "references/mainstream-defi.md"),
        ("backtest", "references/replay-capabilities.md"),
    ]:
        pages, offset = [], 0
        while True:
            arguments = {"skill": skill, "offset": offset}
            if file:
                arguments["file"] = file
            result = skill_tool_handler(
                ToolCall(name="Skill", arguments=arguments), skill_index=index,
            )
            assert not result.is_error, result.text()
            pages.append(result.content[0].text)
            following = tool_json_data(result).get("next_offset")
            if following is None:
                break
            offset = following
        text = "\n".join(pages)
        assert "Historical discovery stop" in text, (skill, file)
        if skill == "strategy_author" and file is None:
            gate = text.split("## Historical discovery stop", 1)[1].split("\n## ", 1)[0]
            for instruction in ("before package writes", "bounded discovery", "blocked",
                                "end this turn", "draft-only", "existing proposal",
                                "unknown is not zero"):
                assert instruction in gate, instruction
            assert text.index("## Historical discovery stop") < text.index("## Default path")


def test_jupiter_backtest_requires_exact_history_before_fetch(tmp_path, monkeypatch):
    from nerya.evolution.patch_proposal import create_proposal
    from nerya.skills.builtin.backtest.scripts import data_cache
    from nerya.skills.builtin.backtest.scripts.backtest_run import run_strategy_backtest

    market = "JUPITER:SOL/USDC"
    proposal = create_proposal(
        WorkspacePaths(tmp_path), kind="strategy_package_proposal",
        summary="Jupiter exact-market historical capability",
        extra_files={
            "after/strategies/jupiter_history/strategy.yml": (
                "strategy_id: jupiter_history\nmarkets: [JUPITER:SOL/USDC]\n"
                "accounts: [paper]\nschedule:\n  type: none\n  enabled: false\n"
            ),
            "after/strategies/jupiter_history/main.py": (
                "def run(ctx):\n    return ctx.result.hold(reason='history required')\n"
            ),
        },
    )

    def no_substitute(*args, **kwargs):
        pytest.fail("Unregistered Jupiter history must not fetch substitute candles")

    monkeypatch.setattr(data_cache, "fetch_candles", no_substitute)
    with pytest.raises(data_cache.NoHistoricalDataError, match=market) as caught:
        run_strategy_backtest(
            proposal_id=proposal.id, workspace=tmp_path, allow_mock=False,
        )
    assert caught.value.reason == "historical_provider_unsupported"
    assert caught.value.retryable is False
    assert caught.value.receipt["markets"] == [market]


def test_empty_jupiter_connector_catalog_points_to_financial_discovery(tmp_path):
    result = connector_list_handler(
        ToolCall(name="connector_list", arguments={"query": "jupiter"}),
        config_like=Config(WorkspacePaths(tmp_path), deepcopy(DEFAULT_CONFIG)),
    )
    catalog = tool_json_data(result)
    assert catalog["count"] == 0
    assert catalog["connectors"] == []
    assert catalog["inventory_scope"] == "exchange_providers"
    assert catalog["financial_components_included"] is False
    assert catalog["financial_component_discovery"] == {
        "tool": "financial_components", "skill": "financial_ops",
    }
    assert "credential_status" not in catalog


@pytest.mark.parametrize("query", [
    "Jupiter",
    "帮我做一个 Jupiter 上 SOL/USDC 的趋势策略，回测最近30天，不启用交易。",
])
def test_jupiter_request_discovers_financial_workflow(query):
    result = skill_tool_handler(
        ToolCall(name="Skill", arguments={"action": "search", "query": query}),
        skill_index=SkillIndex([BUILTINS]),
    )
    rows = tool_json_data(result)["skills"]
    assert any(row["load"] == {"skill": "financial_ops"} for row in rows), rows


def test_strategy_draft_preserves_structured_chain_and_protocol_for_scope_compare(tmp_path):
    from nerya.core import yaml_io
    from nerya.evolution.strategy_code_generator import (
        StrategyCodeGenerator, StrategyGenerationRequest,
    )
    from nerya.strategies.request_scope import _protocol, compare

    result = StrategyCodeGenerator(WorkspacePaths(tmp_path)).generate(
        StrategyGenerationRequest(
            strategy_id="jupiter_trend",
            strategy_class="trend",
            execution_mode="script",
            mode="paper",
            markets=("JUPITER:SOL/USDC",),
            accounts=("solana_paper",),
            chain="solana",
            protocol="jupiter",
        ),
        validate=False,
        create_proposal_record=False,
    )
    manifest = yaml_io.loads(result.files["strategy.yml"])
    assert manifest["chain"] == "solana"
    assert manifest["protocol"] == "jupiter"
    assert compare({"constraints": {"resources": [{
        "protocol": _protocol("jupiter"),
        "chain": "solana",
        "base": "SOL",
        "quote": "USDC",
    }]}}, manifest) == {}


def test_draft_adopts_unique_admitted_scope_identity(tmp_path, monkeypatch):
    from nerya.core import yaml_io
    from nerya.strategies.request_scope import _protocol
    from nerya.tools.native import strategy_runtime

    contract = {"constraints": {"resources": [{
        "protocol": _protocol("jupiter"), "chain": "solana",
        "base": "SOL", "quote": "USDC",
    }]}}
    monkeypatch.setattr("nerya.strategies.request_scope.current_contract", lambda _config: contract)
    monkeypatch.setattr("nerya.strategies.request_scope.draft_guard", lambda *args, **kwargs: None)
    monkeypatch.setattr("nerya.strategies.request_scope._snapshot", lambda *args, **kwargs: {
        "context": {"request_scope": {}}
    })
    result = strategy_runtime.strategy_draft_proposal_handler(
        ToolCall(name="strategy_draft_proposal", arguments={
            "strategy_id": "jupiter_adopted", "strategy_class": "trend",
            "execution_mode": "script", "mode": "paper",
            "markets": ["JUPITER:SOL/USDC"], "accounts": ["solana_paper"],
            "files": {"strategy.yml": "markets: [JUPITER:SOL/USDC]\n"},
        }),
        config=Config(WorkspacePaths(tmp_path), {"runtime": {"command_id": "cmd"}}),
    )
    assert not result.is_error, result.error
    paths = result.content[0].data["proposal_paths"]
    manifest = yaml_io.load(tmp_path / paths["strategy_yml_path"])
    assert manifest["chain"] == "solana"
    assert manifest["protocol"] == "jupiter"


def test_draft_ensures_unsaved_scope_before_adopting_identity(tmp_path, monkeypatch):
    from nerya.core import yaml_io
    from nerya.strategies.request_scope import ScopeError, _protocol
    from nerya.tools.native import strategy_runtime

    contract = {"constraints": {"resources": [{
        "protocol": _protocol("jupiter"), "chain": "solana",
        "base": "SOL", "quote": "USDC",
    }]}}
    def unsaved(_config):
        raise ScopeError()
    monkeypatch.setattr("nerya.strategies.request_scope.current_contract", unsaved)
    monkeypatch.setattr("nerya.strategies.request_scope.ensure_contract", lambda _config: contract)
    monkeypatch.setattr("nerya.strategies.request_scope.draft_guard", lambda *args, **kwargs: None)
    monkeypatch.setattr("nerya.strategies.request_scope._snapshot", lambda *args, **kwargs: {
        "context": {"request_scope": {}}
    })
    result = strategy_runtime.strategy_draft_proposal_handler(
        ToolCall(name="strategy_draft_proposal", arguments={
            "strategy_id": "jupiter_unsaved_scope", "strategy_class": "trend",
            "execution_mode": "script", "mode": "paper",
            "markets": ["JUPITER:SOL/USDC"], "accounts": ["solana_paper"],
            "files": {"strategy.yml": "markets: [JUPITER:SOL/USDC]\n"},
        }),
        config=Config(WorkspacePaths(tmp_path), {"runtime": {"command_id": "cmd"}}),
    )
    assert not result.is_error, result.error
    paths = result.content[0].data["proposal_paths"]
    manifest = yaml_io.load(tmp_path / paths["strategy_yml_path"])
    assert manifest["chain"] == "solana"
    assert manifest["protocol"] == "jupiter"


@pytest.mark.parametrize("protocol", ["jupiter", "jupiter_v2"])
def test_jupiter_inventory_finds_existing_swap_without_wallet_or_history(tmp_path, protocol):
    data = deepcopy(DEFAULT_CONFIG)
    data.setdefault("runtime", {}).update(
        financial_actor_id="assistant", financial_actor_scopes=["execute:strategy"],
    )
    registry = ToolRegistry()
    cfg = Config(WorkspacePaths(tmp_path), data)
    register_financial_tools(registry, SimpleNamespace(config=cfg))
    result = registry.get("financial_components").handler(ToolCall(
        name="financial_components",
        arguments={"mode": "backtest", "protocol": protocol, "market": "SOL/USDC"},
    ))
    assert not result.is_error, result.text()
    inventory = tool_json_data(result)
    assert "capability_gap" not in inventory, inventory.get("capability_gap")
    trading = next(row for row in inventory["components"] if row["id"] == "builtin:trading")
    assert "swap" in trading["actions"]
    assert trading["available"] is True
    assert trading["live_verified"] is False
    assert inventory["financial_execution_allowed"] is False
    assert inventory["inventory_scope"] == "paper_backtest"
    assert inventory["matched_component"] == {
        "id": trading["id"], "revision": trading["revision"],
    }
    assert inventory["historical_data_verified"] is False
    # Match the component selected by the actual swap resolver, without
    # constructing a transport, quoting, reading balances or resolving keys.
    bound = trading_components(cfg).resolve({
        "kind": "swap", "protocol": protocol, "chain": "solana",
    })
    assert inventory["matched_component"] == {"id": bound.id, "revision": bound.revision}
    readiness = execution_readiness(cfg)
    jupiter = next(row for row in readiness["families"] if row["id"] == "jupiter_v2")
    assert jupiter["implementation"] == "implemented"
    assert jupiter["state"] == "configuration_required"
    assert "solana_rpc_signer_and_jupiter_key_required" in jupiter["blockers"]
    assert not (tmp_path / "nerya.db").exists()


def test_unknown_protocol_still_has_a_scoped_capability_gap(tmp_path):
    data = deepcopy(DEFAULT_CONFIG)
    data.setdefault("runtime", {}).update(
        financial_actor_id="assistant", financial_actor_scopes=["execute:strategy"],
    )
    registry = ToolRegistry()
    register_financial_tools(registry, SimpleNamespace(config=Config(WorkspacePaths(tmp_path), data)))
    request = {"mode": "backtest", "protocol": "unlisted_dex", "market": "SOL/USDC"}
    result = registry.get("financial_components").handler(
        ToolCall(name="financial_components", arguments=request),
    )
    inventory = tool_json_data(result)
    assert inventory["capability_gap"]["requested"] == {
        "protocol": "unlisted_dex", "market": "SOL/USDC",
    }
    assert inventory["capability_gap"]["status"] == "blocked"
    assert "matched_component" not in inventory
    assert inventory["historical_data_verified"] is False
