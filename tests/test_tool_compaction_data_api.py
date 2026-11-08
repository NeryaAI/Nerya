from __future__ import annotations

import json

import pytest

from nerya.llm import tool_compaction as tc


pytestmark = pytest.mark.smoke


def test_saved_snapshot_compaction_preserves_public_token_identity_not_credentials():
    address = '0xa0b86991c6218b36c1d19d4a2e9eb0ce3606eb48'
    identity = {'pool': {'token0': address, 'token1': address, 'decimals': [6, 18],
                         'fee': 500, 'tick_spacing': 10, 'chain_id': 1, 'factory': address},
                'token_identities': [{'address': address, 'symbol': symbol, 'decimals': decimals,
                                      'chain_id': 1, 'identity_scope': 'current_official_catalog',
                                      'catalog_source_sha256': 'c' * 64}
                                     for symbol, decimals in [('USDC', 6), ('WETH', 18)]],
                'row_contract': 'uniswap_v3_pool_history.v1', 'rows_sha256': 'b' * 64,
                'access_token': 'private-access-value', 'api_key': 'private-api-value'}
    output = {'count': 1, 'scope': 'current_task', 'snapshots': [{
        'provider': 'uniswap', 'action': 'pool_history',
        'args': {'start': '2026-10-07', 'end': '2026-11-06', 'pool_address': address,
                 'rpc_provider': 'drpc', 'step_blocks': 4000, 'max_samples': 64,
                 'tick_lower': 191130, 'tick_upper': 207250},
        'coverage': {'requested_window_complete': False, 'observations': 55},
        'requested_window': {'start': '2026-10-07', 'end': '2026-11-06'},
        'row_count': 55, 'producer_ref': 'replay://' + 'a' * 64, 'identity': identity}],
        'padding': 'x' * 10000}
    kept = tc.compact_tool_result('data_api', output).kept
    public = kept['snapshots'][0]['identity']
    assert public['pool']['token0'] == address
    assert public['pool']['token1'] == address
    assert public['token_identities'] == identity['token_identities']
    assert public['row_contract'] == identity['row_contract']
    assert public['rows_sha256'] == identity['rows_sha256']
    assert kept['snapshots'][0]['args'] == output['snapshots'][0]['args']
    assert 'private-' not in json.dumps(kept)


def test_workspace_ui_inspect_compaction_keeps_editable_manifest_and_catalog(tmp_path, monkeypatch):
    from copy import deepcopy
    from nerya.core.config import Config, DEFAULT_CONFIG
    from nerya.core.paths import WorkspacePaths
    from nerya.tools.native.workspace_ui import workspace_ui_inspect_handler
    from nerya.tools.types import ToolCall
    from nerya.workspace import ui

    widgets = [{"id": f"ticker-{i}", "kind": "market_ticker", "title": f"Market {i}",
                "source": "market.ticker", "config": {"venue": "binance", "market": "BTC/USDT"},
                "read_only": True} for i in range(4)]
    manifest = {"version": 1, "home": {"widgets": []}, "pages": [
        {"id": "market-overview", "href": "/workspace/pages/market-overview", "title": "Markets", "nav": {"section": "primary", "order": 1},
         "widgets": widgets}]}
    monkeypatch.setattr(ui, "read", lambda paths: {
        "ok": True, "status": "ready", "manifest": manifest, "revision": 7,
        "digest": "d" * 64, "catalog": ui.catalog(), "source": "workspace",
    })
    config = Config(WorkspacePaths(tmp_path), deepcopy(DEFAULT_CONFIG))
    result = workspace_ui_inspect_handler(ToolCall(name="workspace_ui_inspect", arguments={
        "page_id": "market-overview", "include_manifest": True}), config=config)
    assert not result.is_error
    output = result.content[0].data
    compacted = tc.compact_tool_result("workspace_ui_inspect", output)
    assert not compacted.skipped
    kept = compacted.kept
    assert kept["revision"] == 7 and kept["digest"] == "d" * 64
    assert kept["manifest"] == manifest
    catalog = {row["kind"]: row for row in kept["catalog"]["widget_kinds"]}
    assert set(catalog) == ui.WIDGET_KINDS
    assert catalog["chart"]["config_example"] == ui.catalog()["widget_kinds"][2]["config_example"]
    assert catalog["market_ticker"]["source"] == "market.ticker"
    assert catalog["portfolio"]["data_contract"]["supports_account_filter"] is False
    output["manifest"]["pages"][0]["widgets"] = widgets * 100
    output["padding"] = "raw-body" * 10000
    bounded = tc.compact_tool_result("workspace_ui_inspect", output).kept
    page = bounded["manifest"]["pages"][0]
    assert len(page["widgets"]) <= 16
    assert page["widgets_truncated"] == 400 - len(page["widgets"])
    assert len(json.dumps(bounded)) < 16000
    assert "raw-body" not in json.dumps(bounded)


def test_connector_list_compaction_keeps_connector_ids_and_next_action() -> None:
    output = {
        "count": 2,
        "connectors": [
            {
                "id": "binance",
                "label": "Binance Spot",
                "kind": "cex",
                "runtime": "python_ccxt",
                "docs_url": "https://developers.binance.com/docs/binance-spot-api-docs/rest-api",
                "aliases": ["binance_spot", "binanceusdm"],
                "supports": {"balances": True, "place_order": True},
                "credential_status": {
                    "required": True,
                    "status": "missing",
                    "configured": False,
                    "required_fields": ["api_key", "api_secret"],
                    "operator_message": "Configure credentials.",
                },
                "padding": "x" * 3000,
            },
            {
                "id": "binance_agentic_wallet",
                "label": "Binance Agentic Wallet",
                "kind": "wallet",
                "runtime": "node_skill",
            },
        ],
        "next_required_action": {
            "tool": "data_api",
            "message": "Call data_api wallet readiness before authoring.",
        },
        "hint": "Use data_api before coding docs; meme DEX onchain setup." + ("x" * 3000),
    }

    result = tc.compact_tool_result("connector_list", output, size_threshold=0)

    assert not result.skipped
    assert result.rule_id == "connector_list.summary"
    assert result.kept["count"] == 2
    assert result.kept["connectors_sample"][0]["id"] == "binance"
    assert result.kept["connectors_sample"][0]["setup_status"]["status"] == "missing"
    assert result.kept["next_required_action"]["tool"] == "data_api"
    assert result.kept["status"] == "available"
    compacted_text = json.dumps(result.kept, ensure_ascii=False)
    assert "credential" not in compacted_text.lower()
    assert "api_key" not in compacted_text
    assert "api_secret" not in compacted_text
    assert "required_fields" not in compacted_text
    assert "operator_message" not in compacted_text
    assert "docs_url" not in compacted_text
    assert "place_order" not in compacted_text
    assert "meme" not in compacted_text.lower()
    assert "dex" not in compacted_text.lower()
    assert "onchain" not in compacted_text.lower()


def test_data_api_catalog_compaction_keeps_actions_and_next_action() -> None:
    output = {
        "providers": ["akshare", "onchainos", "wallet"],
        "aliases": {
            "binance": "wallet",
            "agentic_wallet": "wallet",
            "okx_onchain": "onchainos",
        },
        "requested_provider": "wallet",
        "provider": "wallet",
        "count": 2,
        "limit": 20,
        "actions": [
            {
                "provider": "wallet",
                "action": "readiness",
                "title": "Wallet provider readiness",
                "tags": ["wallet", "agentic", "token", "meme"],
                "description": "x" * 3000,
            },
            {
                "provider": "wallet",
                "action": "capability_catalog",
                "title": "Wallet capability catalog",
            },
            {
                "provider": "wallet",
                "action": "meme_strategy_guide",
                "title": "Meme strategy wallet data guide",
                "tags": ["wallet", "meme", "dex", "token"],
            },
        ],
        "next_required_action": {
            "tool": "data_api",
            "arguments": {
                "op": "call",
                "provider": "wallet",
                "action": "readiness",
                "args": {"provider": "binance"},
            },
        },
        "padding": "x" * 3000,
    }

    result = tc.compact_tool_result("data_api", output, size_threshold=0)

    assert not result.skipped
    assert result.rule_id == "data_api.catalog"
    assert result.kept["provider"] == "wallet"
    assert result.kept["actions_sample"] == [
        {
            "provider": "wallet",
            "action": "readiness",
            "title": "Wallet provider readiness",
        }
    ]
    assert result.kept["actions_sample"][0]["action"] == "readiness"
    assert "tags" not in result.kept["actions_sample"][0]
    assert result.kept["next_required_action"]["arguments"]["action"] == "readiness"
    compacted_text = json.dumps(result.kept, ensure_ascii=False)
    assert "aliases" not in compacted_text
    assert "token" not in compacted_text.lower()
    assert "meme" not in compacted_text.lower()
    assert "dex" not in compacted_text.lower()
    assert "onchain" not in compacted_text.lower()


def test_data_api_wallet_guide_compaction_keeps_actionable_route() -> None:
    output = {
        "provider": "wallet",
        "action": "meme_strategy_guide",
        "kind": "object",
        "data": {
            "next_required_action": "author SDK strategy package",
            "selected_route": {
                "provider": "byreal",
                "canonical": "BYREAL_ONCHAIN",
                "ready": True,
                "market": "BYREAL_ONCHAIN:solana:<pool_address>",
            },
            "authoring_contract": {
                "skill": "strategy_author",
                "sdk_import": (
                    "from nerya.strategies import StrategyContext, "
                    "StrategyResult, StrategyAgentTask"
                ),
                "proposal_tool_role": "package supplied files",
                "prompt_contract": "describe evidence categories",
            },
            "bounded_sequence": [
                {
                    "step": "candidate_discovery",
                    "tool": "data_api",
                    "calls": [
                        {
                            "op": "call",
                            "provider": "onchainos",
                            "action": "token_hot_tokens",
                        }
                    ],
                }
            ],
            "padding": "x" * 5000,
        },
    }

    result = tc.compact_tool_result("data_api", output)

    assert not result.skipped
    assert result.rule_id == "data_api.object"
    assert result.kept["selected_route"]["canonical"] == "BYREAL_ONCHAIN"
    assert "StrategyAgentTask" in result.kept["authoring_contract"]["sdk_import"]
    assert result.kept["bounded_sequence"][0]["calls"][0]["action"] == "token_hot_tokens"


def test_data_api_table_compaction_keeps_rows_sample() -> None:
    output = {
        "provider": "onchainos",
        "action": "token_hot_tokens",
        "kind": "table",
        "row_count": 12,
        "truncated": False,
        "rows": [
            {"symbol": f"T{i}", "address": f"addr{i}", "padding": "x" * 500}
            for i in range(12)
        ],
    }

    result = tc.compact_tool_result("data_api", output)

    assert not result.skipped
    assert result.rule_id == "data_api.table"
    assert result.kept["row_count"] == 12
    assert len(result.kept["rows_sample"]) == 12
    assert result.kept["sample_truncated"] is False
    assert result.kept["rows_sample"][0]["address"] == "addr0"


def test_data_api_onchainos_holder_compaction_keeps_profit_sample() -> None:
    rows = [
        {
            "holderWalletAddress": f"wallet{i}",
            "realizedPnlUsd": str(i * 100),
            "totalPnlUsd": str(i * 200),
            "avgBuyPrice": "0.0001",
            "avgSellPrice": "0.0005",
            "holdAmount": str(i * 1000),
            "padding": "x" * 1000,
        }
        for i in range(20)
    ]
    output = {
        "provider": "onchainos",
        "action": "token_holders",
        "kind": "object",
        "data": {"code": "0", "data": rows},
    }

    result = tc.compact_tool_result("data_api", output, size_threshold=0)

    assert not result.skipped
    assert result.rule_id == "data_api.onchainos_rows"
    assert result.kept["row_count"] == 20
    assert len(result.kept["rows_sample"]) == 5
    assert result.kept["top_profit_rows"][0]["wallet"] == "wallet19"
    assert "padding" not in str(result.kept)
    assert result.compacted_bytes < result.original_bytes * 0.25


def test_market_data_candle_compaction_keeps_non_empty_coverage() -> None:
    rows = [
        {
            "timestamp": f"2026-05-18T0{i}:00:00Z",
            "open": 1.0 + i,
            "high": 1.1 + i,
            "low": 0.9 + i,
            "close": 1.05 + i,
            "volume": 100 + i,
        }
        for i in range(8)
    ]
    output = {
        "venue": "byreal_onchain",
        "market": "BYREAL_ONCHAIN:solana:token123",
        "interval": "5m",
        "count": len(rows),
        "candles": rows,
        "coverage": {"bars": len(rows), "complete": True},
        "features": {"rsi_14": 61.2, "macd": {"hist": 0.04}},
        "context": "Momentum is positive while volatility remains bounded.",
    }

    result = tc.compact_tool_result("market_data", output, size_threshold=0)

    assert not result.skipped
    assert result.rule_id == "trading.candles"
    assert result.kept["count"] == 8
    assert result.kept["rows"] == 8
    assert len(result.kept["rows_sample"]) == 5
    assert result.kept["market"] == "BYREAL_ONCHAIN:solana:token123"
    assert result.kept["first_timestamp"] == "2026-05-18T00:00:00Z"
    assert result.kept["last_timestamp"] == "2026-05-18T07:00:00Z"
    assert result.kept["first_timestamp_iso"] == "2026-05-18T00:00:00Z"
    assert result.kept["last_timestamp_iso"] == "2026-05-18T07:00:00Z"
    assert result.kept["coverage"] == {"bars": 8, "complete": True}
    assert result.kept["features"]["rsi_14"] == 61.2
    assert result.kept["features"]["macd"]["hist"] == 0.04
    assert result.kept["context"].startswith("Momentum is positive")
    assert "rows=8" in result.summary


def test_compaction_keeps_persisted_capture_paths() -> None:
    output = {
        "ok": True,
        "query": "market structure",
        "results": [{"title": "Source", "url": "https://example.com"}],
        "saved_path": "state/research_data/2026-08-10/capture.json",
    }

    result = tc.compact_tool_result("web_search", output, size_threshold=0)

    assert result.kept["saved_path"] == output["saved_path"]


def test_market_data_candle_compaction_converts_unix_seconds_to_iso() -> None:
    rows = [
        {"ts": 1779066000, "open": 1, "high": 1, "low": 1, "close": 1},
        {"ts": 1779069600, "open": 2, "high": 2, "low": 2, "close": 2},
    ]
    result = tc.compact_tool_result(
        "market_data",
        {
            "venue": "byreal_onchain",
            "market": "BYREAL_ONCHAIN:solana:token123",
            "interval": "1h",
            "count": 2,
            "candles": rows,
        },
        size_threshold=0,
    )

    assert result.kept["first_timestamp_iso"] == "2026-05-18T01:00:00Z"
    assert result.kept["last_timestamp_iso"] == "2026-05-18T02:00:00Z"
    assert "2026-05-18T01:00:00Z" in result.summary


def test_backtest_compaction_keeps_dashboard_artifact_locator() -> None:
    output = {
        "ok": True,
        "kind": "freeform_backtest",
        "strategy_id": "polymarket_odds_tracker",
        "proposal_id": "prp_625b2c8ffd0d",
        "backtest_ts": "freeform_20260521_050643",
        "out_dir": (
            "C:\\Users\\Ricky\\.nerya\\evolution\\proposals\\prp_625b2c8ffd0d\\"
            "after\\strategies\\polymarket_odds_tracker\\backtests\\"
            "freeform_20260521_050643"
        ),
        "raw_metrics_file": (
            "C:\\Users\\Ricky\\.nerya\\evolution\\proposals\\prp_625b2c8ffd0d\\"
            "after\\strategies\\polymarket_odds_tracker\\backtests\\"
            "freeform_20260521_050643\\metrics.json"
        ),
        "chart_path": (
            "C:\\Users\\Ricky\\.nerya\\evolution\\proposals\\prp_625b2c8ffd0d\\"
            "after\\strategies\\polymarket_odds_tracker\\backtests\\"
            "freeform_20260521_050643\\chart.json"
        ),
        "metrics": {
            "total_return_pct": "12.34%",
            "total_trades": "3",
        },
        "padding": "x" * 8000,
    }

    result = tc.compact_tool_result("strategy_backtest", output, size_threshold=0)

    assert not result.skipped
    assert result.rule_id == "backtest.report"
    assert result.kept["strategy_id"] == "polymarket_odds_tracker"
    assert result.kept["proposal_id"] == "prp_625b2c8ffd0d"
    assert result.kept["backtest_ts"] == "freeform_20260521_050643"
    assert result.kept["raw_metrics_file"].endswith("metrics.json")
    assert result.kept["chart_path"].endswith("chart.json")


def test_backtest_compaction_keeps_bounded_retrospective_decision_proof() -> None:
    output = {
        "ok": True,
        "result_type": "backtest_result",
        "backtest_status": "completed",
        "execution_mode": "script",
        "performance_evidence": False,
        "decision_replay": {
            "label": "current_model_retrospective_replay",
            "decision_receipts_verified": True,
            "model_decisions_verified": True,
            "decision_count": 13,
            "performance_evidence": False,
            "binding": {
                "source_revision": "source-hash",
                "config_hash": "config-hash",
                "data_hash": "data-hash",
                "role_prompt_hash": "role-hash",
                "model_settings_hash": "model-settings-hash",
                "provider": "provider-body-to-drop",
            },
            "tape_sha256": "tape-hash",
            "replay_passes": 1,
            "tape": [{"response": "model-body-to-drop"}],
        },
        "padding": "x" * 8000,
    }

    result = tc.compact_tool_result("strategy_backtest", output, size_threshold=0)

    assert result.kept["decision_replay"] == {
        "label": "current_model_retrospective_replay",
        "decision_receipts_verified": True,
        "model_decisions_verified": True,
        "decision_count": 13,
        "performance_evidence": False,
        "tape_sha256": "tape-hash",
        "replay_passes": 1,
        "binding": {
            "source_revision": "source-hash",
            "config_hash": "config-hash",
            "data_hash": "data-hash",
            "role_prompt_hash": "role-hash",
            "model_settings_hash": "model-settings-hash",
        },
    }
    malformed = tc.compact_tool_result(
        "strategy_backtest",
        {"decision_replay": {
            "label": {"provider_body": "must-drop"},
            "decision_count": "13",
            "binding": {"data_hash": {"provider_body": "must-drop"}},
        }, "padding": "x" * 8000},
        size_threshold=0,
    )
    assert "decision_replay" not in malformed.kept


def test_backtest_compaction_keeps_top_level_outcome_contract() -> None:
    output = {
        "ok": False,
        "verdict": "FAIL",
        "coverage_ok": False,
        "coverage": {"bars": 3, "required_bars": 100, "complete": False},
        "coverage_message": "historical candles are incomplete",
        "modeled_coverage": {"covered_seconds": 7200, "requested_seconds": 10800, "complete": False},
        "gaps": [{"start": "2026-01-01T01:00:00Z", "end": "2026-01-01T02:00:00Z", "seconds": 3600}],
        "observation_coverage": {"leading_gap_seconds": 600, "completeness": "not_asserted"},
        "operator_summary": {
            "headline": "Insufficient coverage",
            "unit_warning": "percent values are display strings",
        },
        "padding": "x" * 10_000,
    }

    result = tc.compact_tool_result("strategy_backtest", output, size_threshold=0)

    assert result.kept["ok"] is False
    assert result.kept["verdict"] == "FAIL"
    assert result.kept["coverage_ok"] is False
    assert result.kept["coverage"]["bars"] == 3
    assert result.kept["operator_summary"]["headline"] == "Insufficient coverage"
    crowded = tc.compact_tool_result("strategy_backtest",
        {**{f"counter_{i}": i for i in range(50)}, **output}, size_threshold=0)
    for key in ("modeled_coverage", "gaps", "observation_coverage"):
        assert result.kept[key] == output[key]
        assert crowded.kept[key] == output[key]
    assert "padding" not in result.kept


def test_generic_validation_compaction_keeps_bounded_blockers_and_warnings() -> None:
    output = {
        "ok": False,
        "strategy_id": "alpha",
        "blockers": [
            {"code": f"blocker_{i}", "message": "fix this"}
            for i in range(12)
        ],
        "warnings": [
            {"code": f"warning_{i}", "message": "review this"}
            for i in range(12)
        ],
        "padding": "x" * 10_000,
    }

    result = tc.compact_tool_result("strategy_validate", output, size_threshold=0)

    assert result.kept["ok"] is False
    assert result.kept["blockers"][0]["code"] == "blocker_0"
    assert result.kept["warnings"][0]["code"] == "warning_0"
    assert result.kept["blockers"][-1]["_truncated_items"] == 4
    assert result.kept["warnings"][-1]["_truncated_items"] == 4
    assert "padding" not in result.kept


def test_generic_draft_compaction_keeps_state_and_next_steps() -> None:
    output = {
        "state": "draft",
        "next_steps": ["edit", "validate", "submit", "promote"] * 4,
        "summary": "draft is awaiting validation",
        "padding": "x" * 10_000,
    }

    result = tc.compact_tool_result("strategy_generate_proposal", output, size_threshold=0)

    assert result.kept["state"] == "draft"
    assert result.kept["next_steps"][:3] == ["edit", "validate", "submit"]
    assert result.kept["next_steps"][-1]["_truncated_items"] == 8


def test_generic_json_compaction_keeps_proposal_locator_fields() -> None:
    output = {
        "strategy_id": "bsc_whale_copycat",
        "strategy_class": "agent",
        "execution_mode": "agent",
        "proposal_id": "prp_625b2c8ffd0d",
        "validation": {"ok": True},
        "proposal_paths": {
            "root": "evolution/proposals/prp_625b2c8ffd0d",
        },
        "next_required_action": {
            "tool": "strategy_backtest",
            "arguments": {"proposal_id": "prp_625b2c8ffd0d"},
        },
        "padding": "x" * 8000,
    }

    result = tc.compact_tool_result(
        "strategy_generate_proposal",
        output,
        size_threshold=0,
    )

    assert result.rule_id == "json.large"
    assert result.kept["strategy_id"] == "bsc_whale_copycat"
    assert result.kept["proposal_id"] == "prp_625b2c8ffd0d"
    assert result.kept["next_required_action"]["tool"] == "strategy_backtest"
