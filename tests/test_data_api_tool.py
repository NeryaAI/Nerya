from __future__ import annotations

from copy import deepcopy
import sys
from types import ModuleType

import pytest

from nerya.agent.kernel import AgentKernel as _AgentKernel  # noqa: F401
from nerya.core.config import Config, DEFAULT_CONFIG
from nerya.core.paths import WorkspacePaths
from nerya.data_api.registry import compact_data_result
from nerya.tools.native.bootstrap import build_native_tool_deps, register_native_tools
from nerya.tools.native.connectors import connector_list_handler, connector_view_handler
from nerya.tools.native.data_api import data_api_handler
from nerya.tools.registry import ToolRegistry
from nerya.tools.types import ToolCall, ToolErrorKind


pytestmark = pytest.mark.smoke


def _config(tmp_path) -> Config:
    return Config(paths=WorkspacePaths(root=tmp_path), data=deepcopy(DEFAULT_CONFIG))


def _json_payload(result):
    assert not result.is_error, result.text()
    assert result.content
    return result.content[0].data


def test_data_api_lists_and_schemas_akshare_without_importing_dependency(tmp_path) -> None:
    cfg = _config(tmp_path)

    listed = _json_payload(
        data_api_handler(
            ToolCall(
                name="data_api",
                arguments={"op": "list", "provider": "akshare", "query": "stock_zh_a", "limit": 10},
            ),
            config_like=cfg,
        )
    )
    actions = {row["action"] for row in listed["actions"]}
    assert "stock_zh_a_hist" in actions

    schema = _json_payload(
        data_api_handler(
            ToolCall(
                name="data_api",
                arguments={"op": "schema", "provider": "akshare", "action": "stock_zh_a_hist"},
            ),
            config_like=cfg,
        )
    )
    assert schema["input_schema"]["properties"]["symbol"]["type"] == "string"
    assert "symbol" in schema["input_schema"]["required"]


def test_data_api_calls_akshare_function_with_bounded_table_result(tmp_path, monkeypatch) -> None:
    cfg = _config(tmp_path)
    module = ModuleType("akshare")

    def stock_zh_a_hist(symbol: str, period: str = "daily", **kwargs):
        assert symbol == "000001"
        assert period == "daily"
        return [
            {"date": "2026-01-01", "close": 10.0, "volume": 100},
            {"date": "2026-01-02", "close": 11.0, "volume": 120},
        ]

    module.stock_zh_a_hist = stock_zh_a_hist
    monkeypatch.setitem(sys.modules, "akshare", module)

    payload = _json_payload(
        data_api_handler(
            ToolCall(
                name="data_api",
                arguments={
                    "op": "call",
                    "provider": "akshare",
                    "action": "stock_zh_a_hist",
                    "args": {"symbol": "000001", "period": "daily"},
                    "columns": ["date", "close"],
                    "limit": 1,
                },
            ),
            config_like=cfg,
        )
    )

    assert payload["kind"] == "table"
    assert payload["row_count"] == 2
    assert payload["truncated"] is True
    assert payload["rows"] == [{"date": "2026-01-01", "close": 10.0}]


def test_data_api_lists_financial_datasets_equity_actions(tmp_path) -> None:
    cfg = _config(tmp_path)

    payload = _json_payload(
        data_api_handler(
            ToolCall(
                name="data_api",
                arguments={
                    "op": "list",
                    "provider": "equities",
                    "query": "sec filing",
                    "limit": 20,
                },
            ),
            config_like=cfg,
        )
    )

    assert payload["provider"] == "financial_datasets"
    assert payload["requested_provider"] == "equities"
    actions = {row["action"] for row in payload["actions"]}
    assert "filings" in actions
    assert any("sec" in " ".join(row.get("tags") or []) for row in payload["actions"])


def test_data_api_financial_datasets_schema_exposes_statement_and_filing_args(tmp_path) -> None:
    cfg = _config(tmp_path)

    filings = _json_payload(
        data_api_handler(
            ToolCall(
                name="data_api",
                arguments={
                    "op": "schema",
                    "provider": "financial_datasets",
                    "action": "filings",
                },
            ),
            config_like=cfg,
        )
    )
    statements = _json_payload(
        data_api_handler(
            ToolCall(
                name="data_api",
                arguments={
                    "op": "schema",
                    "provider": "financial_datasets",
                    "action": "income_statements",
                },
            ),
            config_like=cfg,
        )
    )

    assert filings["input_schema"]["required"] == ["ticker"]
    assert filings["input_schema"]["properties"]["form"]["description"]
    assert statements["input_schema"]["properties"]["period"]["default"] == "annual"
    assert "financialdatasets.ai" in filings["docs_url"]


def test_data_api_calls_financial_datasets_filings_with_bounded_result(tmp_path, monkeypatch) -> None:
    cfg = _config(tmp_path)
    calls: list[tuple[str, dict[str, object]]] = []

    class FakeEquitiesClient:
        def filings(self, ticker: str, *, form: str | None = None, limit: int = 10):
            calls.append(("filings", {"ticker": ticker, "form": form, "limit": limit}))
            return {
                "data": {
                    "filings": [
                        {"ticker": ticker, "form": form, "filing_date": "2026-03-01", "url": "https://sec.example/1"},
                        {"ticker": ticker, "form": form, "filing_date": "2025-03-01", "url": "https://sec.example/2"},
                    ]
                },
                "_envelope": {"source": "financial_datasets", "mode": "live"},
                "source_url": "https://api.financialdatasets.ai/filings/?ticker=NVDA",
            }

    monkeypatch.setattr("nerya.data_api.builtins.EquitiesClient", FakeEquitiesClient)

    payload = _json_payload(
        data_api_handler(
            ToolCall(
                name="data_api",
                arguments={
                    "op": "call",
                    "provider": "sec_filings",
                    "action": "filings",
                    "args": {"ticker": "NVDA", "form": "10-K", "limit": 2},
                    "limit": 1,
                },
            ),
            config_like=cfg,
        )
    )

    assert calls == [("filings", {"ticker": "NVDA", "form": "10-K", "limit": 2})]
    assert payload["provider"] == "sec_filings"
    assert payload["action"] == "filings"
    assert payload["kind"] == "table"
    assert payload["row_count"] == 2
    assert payload["truncated"] is True
    assert payload["rows"] == [
        {
            "ticker": "NVDA",
            "form": "10-K",
            "filing_date": "2026-03-01",
            "url": "https://sec.example/1",
        }
    ]


def test_data_api_common_aliases_survive_column_projection() -> None:
    payload = compact_data_result(
        "onchainos",
        "token_hot_tokens",
        [
            {
                "tokenContractAddress": "ENRAEN9assGLHU2QQCo4cAv818mDrMkb6f6pG8hHpump",
                "tokenSymbol": "hausdorff",
                "marketCap": "80510.36",
                "volume": "11574.50",
                "liquidity": "31391.90",
                "top10HoldPercent": "19.26",
            }
        ],
        columns=[
            "symbol",
            "address",
            "market_cap",
            "volume_24h",
            "liquidity_usd",
            "top_holder_pct",
        ],
    )

    assert payload["rows"] == [
        {
            "symbol": "hausdorff",
            "address": "ENRAEN9assGLHU2QQCo4cAv818mDrMkb6f6pG8hHpump",
            "market_cap": "80510.36",
            "volume_24h": "11574.50",
            "liquidity_usd": "31391.90",
            "top_holder_pct": "19.26",
        }
    ]


def test_data_api_wallet_sources_do_not_expose_config_values(tmp_path) -> None:
    cfg = _config(tmp_path)
    cfg.data["wallet"] = {
        "providers": {
            "okx_main": {
                "provider": "okx_os",
                "label": "OKX Web3",
                "config": {"api_key_ref": "vault://wallet/okx/key"},
            }
        }
    }

    payload = _json_payload(
        data_api_handler(
            ToolCall(
                name="data_api",
                arguments={"op": "call", "provider": "wallet", "action": "list_sources"},
            ),
            config_like=cfg,
        )
    )

    binding = payload["data"]["bindings"][0]
    assert binding["wallet_id"] == "okx_main"
    assert binding["provider"] == "okx_os"
    assert binding["config_keys"] == ["api_key_ref"]
    assert "vault://wallet/okx/key" not in str(payload)


def test_data_api_provider_aliases_route_to_wallet_and_onchainos(tmp_path) -> None:
    cfg = _config(tmp_path)

    wallet = _json_payload(
        data_api_handler(
            ToolCall(
                name="data_api",
                arguments={"op": "list", "provider": "xagt_agent_plugin", "limit": 5},
            ),
            config_like=cfg,
        )
    )
    onchainos = _json_payload(
        data_api_handler(
            ToolCall(
                name="data_api",
                arguments={"op": "list", "provider": "okx_os", "limit": 100},
            ),
            config_like=cfg,
        )
    )

    assert wallet["provider"] == "wallet"
    assert wallet["requested_provider"] == "xagt_agent_plugin"
    assert any(row["action"] == "list_sources" for row in wallet["actions"])
    assert onchainos["provider"] == "onchainos"
    assert onchainos["requested_provider"] == "okx_os"
    assert any(row["action"] == "wallet_status" for row in onchainos["actions"])


def test_connector_view_omits_source_by_default_for_token_efficiency() -> None:
    payload = _json_payload(
        connector_list_handler(ToolCall(name="connector_list", arguments={"query": "solana"}))
    )
    assert payload["count"] >= 1

    viewed = _json_payload(
        connector_view_handler(
            ToolCall(
                name="connector_view",
                arguments={"id": "solana", "max_source_bytes": 20000},
            )
        )
    )

    assert viewed["found"] is True
    assert viewed["source_omitted"] is True
    assert "source" not in viewed
    assert "strategy_generate_proposal" in viewed["source_hint"]


def test_data_api_onchainos_exposes_meme_and_signal_sources(tmp_path) -> None:
    cfg = _config(tmp_path)

    payload = _json_payload(
        data_api_handler(
            ToolCall(
                name="data_api",
                arguments={"op": "list", "provider": "onchainos", "query": "meme", "limit": 50},
            ),
            config_like=cfg,
        )
    )

    actions = {row["action"] for row in payload["actions"]}
    assert {
        "token_hot_tokens",
        "memepump_tokens",
        "memepump_token_bundle_info",
        "signal_list",
        "token_cluster_overview",
        "security_approvals",
    }.issubset(actions)


def test_data_api_wallet_capability_catalog_surfaces_logged_in_usage(tmp_path) -> None:
    cfg = _config(tmp_path)
    cfg.data["wallet"] = {
        "providers": {
            "okx_main": {
                "provider": "okx_os",
                "label": "OKX Web3",
                "config": {"account_id": "acct_1", "api_key_ref": "vault://wallet/okx/key"},
            },
            "xagt_main": {
                "provider": "xagt_agent_plugin",
                "label": "XAgent",
                "config": {"plugin_path": "C:/xagt", "access_token_ref": "vault://wallet/xagt/access"},
            },
        }
    }

    payload = _json_payload(
        data_api_handler(
            ToolCall(
                name="data_api",
                arguments={
                    "op": "call",
                    "provider": "wallet",
                    "action": "capability_catalog",
                    "args": {
                        "topic": "meme",
                        "include_live_status": False,
                        "include_details": True,
                    },
                },
            ),
            config_like=cfg,
        )
    )

    data = payload["data"]
    assert "meme_strategy_guide" in data["next_required_action"]
    assert "Do not repeat" in str(data["rules"])
    assert data["available_route_count"] >= 1
    assert any(row["provider"] == "okx_os" for row in data["provider_summary"])
    assert {row["wallet_id"] for row in data["bindings"]} == {"okx_main", "xagt_main"}
    assert "vault://wallet/okx/key" not in str(payload)
    onchain_actions = {row["action"] for row in data["callable_read_actions"]["onchainos"]}
    assert {"token_hot_tokens", "memepump_tokens", "token_report"}.issubset(onchain_actions)
    assert any(
        fn.get("action") == "market_data.get_candles"
        and "OKX_ONCHAIN" in str(fn.get("call"))
        for row in data["bindings"]
        for fn in row["functions"]
    )
    assert any("RiskGate" in row["required_path"] for row in data["gated_execution_actions"])


def test_data_api_wallet_meme_strategy_guide_is_actionable(tmp_path) -> None:
    cfg = _config(tmp_path)

    payload = _json_payload(
        data_api_handler(
            ToolCall(
                name="data_api",
                arguments={
                    "op": "call",
                    "provider": "wallet",
                    "action": "meme_strategy_guide",
                    "args": {"chain": "solana", "token": "So11111111111111111111111111111111111111112"},
                },
            ),
            config_like=cfg,
        )
    )

    text = str(payload["data"])
    assert payload["data"]["authoring_contract"]["skill"] == "strategy_author"
    assert "strategy_generate_proposal" in payload["data"]["next_required_action"]
    assert payload["data"]["bounded_sequence"][0]["call"]["skill_id"] == "strategy_author"
    assert "token_hot_tokens" in text
    assert "security_token_scan" in text
    assert "market_data" in text
    assert "must_feed_strategy_markets" in text
    assert "exact chain:token market" in text
    assert "StrategyAgentTask" in text
    assert "Do not use run_shell" in text
    assert "trade_intent_submit" in text


def test_data_api_error_detail_includes_provider_and_action(tmp_path) -> None:
    cfg = _config(tmp_path)
    cfg.data["wallet"] = {
        "providers": {
            "self": {"provider": "self_custody", "config": {}},
        }
    }

    result = data_api_handler(
        ToolCall(
            name="data_api",
            arguments={
                "op": "call",
                "provider": "wallet",
                "action": "balance",
                "args": {"chain": "ethereum"},
            },
        ),
        config_like=cfg,
    )

    assert result.is_error
    assert result.error is not None
    assert result.error.kind is ToolErrorKind.SCHEMA_VALIDATION
    assert result.error.detail["provider"] == "wallet"
    assert result.error.detail["action"] == "balance"
    assert result.error.detail["field"] == "address"


def test_data_api_wallet_catalog_rejects_limit_expansion(tmp_path) -> None:
    cfg = _config(tmp_path)

    result = data_api_handler(
        ToolCall(
            name="data_api",
            arguments={
                "op": "call",
                "provider": "wallet",
                "action": "capability_catalog",
                "args": {"topic": "meme"},
                "limit": 500,
            },
        ),
        config_like=cfg,
    )

    assert result.is_error is True
    assert result.error is not None
    assert result.error.kind == ToolErrorKind.SCHEMA_VALIDATION
    assert "strategy_generate_proposal" in result.text()


def test_data_api_wallet_capability_catalog_falls_back_to_goat_without_wallet(tmp_path) -> None:
    cfg = _config(tmp_path)
    cfg.data["wallet"] = {"providers": {}}

    payload = _json_payload(
        data_api_handler(
            ToolCall(
                name="data_api",
                arguments={
                    "op": "call",
                    "provider": "wallet",
                    "action": "capability_catalog",
                    "args": {
                        "topic": "meme",
                        "include_live_status": False,
                        "chain": "solana",
                        "token": "So11111111111111111111111111111111111111112",
                    },
                },
            ),
            config_like=cfg,
        )
    )

    selection = payload["data"]["selection"]
    assert selection["mode"] == "goat_self_custody_fallback"
    assert selection["selected_route"]["venue"] == "onchain"
    assert selection["selected_route"]["market"].startswith("ONCHAIN:solana:")
    assert "npm:@goat-sdk/core" in selection["fallback"]["install"]["commands"]
    assert any(row["provider"] == "self_custody" for row in selection["install_recommendations"])
    assert any(row["provider"] == "okx_os" for row in selection["install_recommendations"])


def test_data_api_wallet_meme_strategy_guide_uses_ready_wallet_route(tmp_path, monkeypatch) -> None:
    cfg = _config(tmp_path)
    cfg.data["wallet"] = {
        "providers": {
            "bitget_main": {
                "provider": "bitget",
                "label": "Bitget Wallet",
                "config": {},
            }
        }
    }

    def readiness_report(config, *, workspace=None, vault_passphrase=None):
        return [
            {
                "id": "bitget",
                "label": "Bitget Wallet",
                "readiness": {"provider": "bitget", "ready": True, "installed": True},
                "capabilities": {
                    "balance": {"supported": True, "status": "real"},
                    "quote": {"supported": True, "status": "real"},
                    "swap": {"supported": True, "status": "guarded"},
                    "market_data": {"supported": True, "status": "real"},
                    "chains": ["solana", "base"],
                    "execution_profile": "partial",
                },
                "stability": "partial",
            }
        ]

    monkeypatch.setattr("nerya.wallet.readiness_report", readiness_report)

    payload = _json_payload(
        data_api_handler(
            ToolCall(
                name="data_api",
                arguments={
                    "op": "call",
                    "provider": "wallet",
                    "action": "meme_strategy_guide",
                    "args": {"chain": "base", "token": "0xtoken"},
                },
            ),
            config_like=cfg,
        )
    )

    selection = payload["data"]["selection"]
    assert payload["data"]["selected_route"]["canonical"] == "BITGET_ONCHAIN"
    assert payload["data"]["available_route_count"] == 1
    assert "use selected_route directly" in payload["data"]["next_required_action"]
    assert selection["mode"] == "wallet_binding"
    assert selection["selected_route"]["canonical"] == "BITGET_ONCHAIN"
    assert selection["selected_route"]["market"] == "BITGET_ONCHAIN:base:0xtoken"
    assert selection["fallback"]["active"] is False
    assert "install" not in selection["fallback"]
    assert any(
        step.get("step") == "fetch_historical_ohlcv"
        and step.get("call", {}).get("venue") == "bitget_onchain"
        for step in payload["data"]["workflow"]
    )


def test_data_api_wallet_meme_strategy_guide_honors_preferred_xagt_route(tmp_path, monkeypatch) -> None:
    cfg = _config(tmp_path)
    cfg.data["wallet"] = {
        "providers": {
            "okx_main": {
                "provider": "okx_os",
                "label": "OKX Web3",
                "config": {},
            },
            "xagt_main": {
                "provider": "xagt_agent_plugin",
                "label": "XAgent",
                "config": {},
            },
        }
    }

    def readiness_report(config, *, workspace=None, vault_passphrase=None):
        base_caps = {
            "balance": {"supported": True, "status": "real"},
            "quote": {"supported": True, "status": "real"},
            "swap": {"supported": True, "status": "guarded"},
            "market_data": {"supported": True, "status": "real"},
            "chains": ["solana"],
            "execution_profile": "partial",
        }
        return [
            {
                "id": "okx_os",
                "label": "OKX Web3",
                "readiness": {"provider": "okx_os", "ready": True, "installed": True},
                "capabilities": dict(base_caps),
                "stability": "partial",
            },
            {
                "id": "xagt_agent_plugin",
                "label": "XAgent",
                "readiness": {
                    "provider": "xagt_agent_plugin",
                    "ready": True,
                    "installed": True,
                },
                "capabilities": dict(base_caps),
                "stability": "partial",
            },
        ]

    monkeypatch.setattr("nerya.wallet.readiness_report", readiness_report)

    payload = _json_payload(
        data_api_handler(
            ToolCall(
                name="data_api",
                arguments={
                    "op": "call",
                    "provider": "wallet",
                    "action": "meme_strategy_guide",
                    "args": {
                        "chain": "solana",
                        "preferred_provider": "xagt_onchain",
                    },
                },
            ),
            config_like=cfg,
        )
    )

    selection = payload["data"]["selection"]
    assert payload["data"]["preferred_provider"] == "xagt_agent_plugin"
    assert payload["data"]["selected_route"]["provider"] == "xagt_agent_plugin"
    assert payload["data"]["selected_route"]["canonical"] == "XAGT_ONCHAIN"
    assert payload["data"]["selected_route"]["market"].startswith("XAGT_ONCHAIN:solana:")
    assert selection["preference"]["matched_ready_route"] is True
    assert "do not silently substitute" in payload["data"]["next_required_action"]
    assert "wallet_install" not in str(selection["fallback"])
    discover_call = payload["data"]["workflow"][0]["call"]
    assert discover_call["args"]["preferred_provider"] == "xagt_agent_plugin"
    assert any(
        step.get("step") == "fetch_historical_ohlcv"
        and step.get("call", {}).get("venue") == "xagt_onchain"
        for step in payload["data"]["workflow"]
    )


def test_data_api_wallet_catalog_prefers_ready_xagt_route(tmp_path, monkeypatch) -> None:
    cfg = _config(tmp_path)
    cfg.data["wallet"] = {
        "providers": {
            "okx_main": {"provider": "okx_os", "label": "OKX Web3", "config": {}},
            "xagt_main": {
                "provider": "xagt_agent_plugin",
                "label": "XAgent",
                "config": {},
            },
        }
    }

    def readiness_report(config, *, workspace=None, vault_passphrase=None):
        base_caps = {
            "balance": {"supported": True, "status": "real"},
            "quote": {"supported": True, "status": "real"},
            "swap": {"supported": True, "status": "guarded"},
            "market_data": {"supported": True, "status": "real"},
            "chains": ["solana"],
            "execution_profile": "partial",
        }
        return [
            {
                "id": "okx_os",
                "label": "OKX Web3",
                "readiness": {"provider": "okx_os", "ready": True, "installed": True},
                "capabilities": dict(base_caps),
            },
            {
                "id": "xagt_agent_plugin",
                "label": "XAgent",
                "readiness": {
                    "provider": "xagt_agent_plugin",
                    "ready": True,
                    "installed": True,
                },
                "capabilities": dict(base_caps),
            },
        ]

    monkeypatch.setattr("nerya.wallet.readiness_report", readiness_report)

    payload = _json_payload(
        data_api_handler(
            ToolCall(
                name="data_api",
                arguments={
                    "op": "call",
                    "provider": "wallet",
                    "action": "capability_catalog",
                    "args": {"topic": "meme", "include_live_status": False},
                },
            ),
            config_like=cfg,
        )
    )

    assert payload["data"]["selected_route"]["provider"] == "xagt_agent_plugin"
    assert payload["data"]["selected_route"]["canonical"] == "XAGT_ONCHAIN"


def test_data_api_wallet_readiness_filters_preferred_provider(tmp_path, monkeypatch) -> None:
    cfg = _config(tmp_path)

    def readiness_report(config, *, workspace=None, vault_passphrase=None):
        return [
            {
                "id": "self_custody",
                "label": "Self custody",
                "readiness": {"provider": "self_custody", "ready": False, "installed": False},
                "capabilities": {},
            },
            {
                "id": "xagt_agent_plugin",
                "label": "XAgent",
                "readiness": {
                    "provider": "xagt_agent_plugin",
                    "ready": True,
                    "installed": True,
                },
                "capabilities": {
                    "market_data": {"supported": True, "status": "real"},
                    "chains": ["solana"],
                },
            },
        ]

    monkeypatch.setattr("nerya.wallet.readiness_report", readiness_report)

    payload = _json_payload(
        data_api_handler(
            ToolCall(
                name="data_api",
                arguments={
                    "op": "call",
                    "provider": "wallet",
                    "action": "readiness",
                    "args": {"provider": "xagt-plugin"},
                },
            ),
            config_like=cfg,
        )
    )

    data = payload["data"]
    assert data["provider"] == "xagt_agent_plugin"
    assert data["ready"] is True
    assert data["count"] == 1
    assert data["provider_status"][0]["id"] == "xagt_agent_plugin"
    assert "install_hint" not in data["provider_status"][0]
    assert "self_custody" not in str(data)
    assert "do not repeat wallet.readiness" in data["next_required_action"]


def test_data_api_wallet_readiness_maps_binance_alias(tmp_path, monkeypatch) -> None:
    cfg = _config(tmp_path)

    def readiness_report(config, *, workspace=None, vault_passphrase=None):
        return [
            {
                "id": "self_custody",
                "label": "Self custody",
                "readiness": {"provider": "self_custody", "ready": False},
                "capabilities": {},
            },
            {
                "id": "binance_agentic",
                "label": "Binance Agentic Wallet",
                "readiness": {
                    "provider": "binance_agentic",
                    "ready": False,
                    "installed": True,
                    "missing": ["skill:binance-agentic-wallet"],
                    "reason": "binance-agentic-wallet skill not installed.",
                },
                "capabilities": {
                    "market_data": {"supported": True, "status": "real"},
                },
            },
        ]

    monkeypatch.setattr("nerya.wallet.readiness_report", readiness_report)

    payload = _json_payload(
        data_api_handler(
            ToolCall(
                name="data_api",
                arguments={
                    "op": "call",
                    "provider": "wallet",
                    "action": "readiness",
                    "args": {"preferred_provider": "binance"},
                },
            ),
            config_like=cfg,
        )
    )

    data = payload["data"]
    assert data["provider"] == "binance_agentic"
    assert data["ready"] is False
    assert data["count"] == 1
    assert data["provider_status"][0]["id"] == "binance_agentic"
    assert "self_custody" not in str(data)


def test_data_api_unknown_provider_returns_recovery_hint(tmp_path) -> None:
    cfg = _config(tmp_path)

    payload = _json_payload(
        data_api_handler(
            ToolCall(
                name="data_api",
                arguments={"op": "list", "provider": "coingecko", "limit": 5},
            ),
            config_like=cfg,
        )
    )

    assert payload["count"] == 0
    assert "wallet" in payload["providers"]
    assert "onchainos" in payload["providers"]
    assert "mcp_describe" in payload["hint"]


def test_connector_list_points_wallet_backed_sources_to_data_api() -> None:
    payload = _json_payload(
        connector_list_handler(
            ToolCall(name="connector_list", arguments={"query": "meme"})
        )
    )

    assert payload["count"] == 0
    assert "data_api" in payload["hint"]
    assert "provider-specific data_api surface" in payload["hint"]
    assert payload["suggested_data_api_checks"][0] == {
        "op": "list",
        "provider": "wallet",
        "query": "meme",
    }
    assert "next_required_action" not in payload
    assert "blocked_until_data_api" not in payload


def test_data_api_wallet_list_plain_query_does_not_force_readiness(tmp_path) -> None:
    payload = _json_payload(
        data_api_handler(
            ToolCall(
                name="data_api",
                arguments={
                    "op": "list",
                    "provider": "wallet",
                    "query": "cryptopanic",
                },
            ),
            config_like=_config(tmp_path),
        )
    )

    assert payload["count"] == 0
    assert "next_required_action" not in payload


def test_data_api_wallet_list_wallet_alias_suggests_readiness_followup(tmp_path) -> None:
    payload = _json_payload(
        data_api_handler(
            ToolCall(
                name="data_api",
                arguments={"op": "list", "provider": "wallet", "query": "xagt"},
            ),
            config_like=_config(tmp_path),
        )
    )

    assert payload["next_required_action"]["tool"] == "data_api"
    assert payload["next_required_action"]["arguments"] == {
        "op": "call",
        "provider": "wallet",
        "action": "readiness",
        "args": {"provider": "xagt"},
    }
    assert "readiness" in payload["next_required_action"]["message"]


def test_data_api_onchainos_allowlisted_action_routes_to_okx_wallet(tmp_path, monkeypatch) -> None:
    cfg = _config(tmp_path)
    cfg.data["wallet"] = {
        "providers": {
            "okx_main": {"provider": "okx_os", "label": "OKX Web3", "config": {}}
        }
    }
    calls: list[tuple[list[str], float]] = []

    from nerya.wallet.providers.okx_os import OkxOsWallet

    def fake_run(self, args, *, timeout_s=30.0):
        calls.append((list(args), float(timeout_s)))
        return {"ok": True, "args": list(args)}

    monkeypatch.setattr(OkxOsWallet, "_run_onchainos", fake_run)

    payload = _json_payload(
        data_api_handler(
            ToolCall(
                name="data_api",
                arguments={
                    "op": "call",
                    "provider": "onchainos",
                    "action": "wallet_status",
                    "args": {"timeout_s": 12},
                },
            ),
            config_like=cfg,
        )
    )

    assert calls == [(["wallet", "status"], 12.0)]
    assert payload["data"]["args"] == ["wallet", "status"]


def test_native_bootstrap_registers_data_api(tmp_path) -> None:
    cfg = _config(tmp_path)
    registry = ToolRegistry()
    deps = build_native_tool_deps(
        workspace_root=tmp_path,
        skill_roots=[tmp_path / "skills"],
        paths=cfg.paths,
        config=cfg,
    )

    register_native_tools(registry, deps)

    names = {tool.name for tool in registry.list_tools()}
    assert "data_api" in names
