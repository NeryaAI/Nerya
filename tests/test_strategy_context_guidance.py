from __future__ import annotations

import json
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace

import pytest

from nerya.agent.kernel import AgentKernel, AgentTurnResult
from nerya.agent.session import SessionStore
from nerya.api import routes_agent
from nerya.core.config import Config, DEFAULT_CONFIG
from nerya.core.paths import WorkspacePaths
from nerya.tools.permissions import PermissionMode
from nerya.tools.native.strategy_runtime import strategy_generate_proposal_handler
from nerya.tools.native.bootstrap import build_native_tool_deps, register_native_tools
from nerya.tools.native.agents import TEAM_RUN_SCHEMA
from nerya.tools.registry import ToolRegistry
from nerya.tools.types import ToolCall, ToolErrorKind


pytestmark = pytest.mark.smoke


def _config(tmp_path) -> Config:
    return Config(paths=WorkspacePaths(root=tmp_path), data=deepcopy(DEFAULT_CONFIG))


def test_system_prompt_surfaces_session_market_context_as_advisory(tmp_path) -> None:
    cfg = _config(tmp_path)
    store = SessionStore(cfg.paths.root)
    store.ensure("sess_cn")
    store.update_meta(
        "sess_cn",
        {
            "market_context": {
                "market_domain": "cn_equity",
                "asset_class": "equity",
                "confidence": 0.86,
                "evidence": ["中国大模型", "A股"],
                "source": "prior_user_turns",
            }
        },
    )

    kernel = AgentKernel(config=cfg, skills=None)  # type: ignore[arg-type]
    prompt = kernel._build_system_prompt(kernel._ensure_registry(), session_id="sess_cn")

    assert "Session market context (advisory)" in prompt
    assert "market_domain: cn_equity" in prompt
    assert "generic follow-up strategy requests should inherit this context" in prompt
    assert "do not substitute unrelated example markets" in prompt
    assert "not a keyword router or hard tool gate" in prompt
    assert "strategy_draft_proposal then edit the staged files" in prompt
    assert "strategy_validate / strategy_submit_proposal /" in prompt
    assert "Turn execution policy:" in prompt
    assert "Choose tools from their schemas" in prompt
    assert "hardcoded workflows" in prompt
    assert "Wallet/provider requests" not in prompt
    assert "Read it with skill_view before connector_list" not in prompt
    assert "generic wallet universe route" not in prompt


def test_yolo_system_prompt_reduces_preemptive_confirmation(tmp_path) -> None:
    cfg = _config(tmp_path)
    kernel = AgentKernel(
        config=cfg,
        skills=None,  # type: ignore[arg-type]
        permission_mode=PermissionMode.YOLO,
    )

    prompt = kernel._build_system_prompt(
        kernel._ensure_registry(),
        user_text="修复测试并直接跑完",
    )

    assert "Permission mode: yolo" in prompt
    assert "unattended execution" in prompt
    assert "safe, reversible" in prompt
    assert "approval or permission blocker" in prompt
    assert "live trading" in prompt


def test_system_prompt_does_not_hard_route_chat_orders(tmp_path) -> None:
    cfg = _config(tmp_path)
    kernel = AgentKernel(
        config=cfg,
        skills=None,  # type: ignore[arg-type]
        permission_mode=PermissionMode.YOLO,
    )

    prompt = kernel._build_system_prompt(
        kernel._ensure_registry(),
        user_text="place the order if risk allows",
    )

    assert "trade_intent_submit" in prompt  # tool list only
    assert "call trade_intent_submit" not in prompt
    assert "confirmation window" not in prompt
    assert "do not ask for chat confirmation first" not in prompt


def test_system_prompt_does_not_encode_routing_and_recurring_workflows(tmp_path) -> None:
    cfg = _config(tmp_path)
    kernel = AgentKernel(
        config=cfg,
        skills=None,  # type: ignore[arg-type]
        permission_mode=PermissionMode.YOLO,
    )

    prompt = kernel._build_system_prompt(
        kernel._ensure_registry(),
        user_text="配置告警分级路由，并每小时让团队判断 ETH 仓位",
    )

    assert "schemas, tool descriptions, loaded skills, and observed state" in prompt
    assert "severity-based message routing" not in prompt
    assert "blocked-but-durable" not in prompt
    assert "committee workflows" not in prompt


def test_ambiguous_targetful_operations_do_not_become_skill_proposals(tmp_path) -> None:
    cfg = _config(tmp_path)
    kernel = AgentKernel(
        config=cfg,
        skills=None,  # type: ignore[arg-type]
        permission_mode=PermissionMode.YOLO,
    )

    prompt = kernel._build_system_prompt(
        kernel._ensure_registry(),
        user_text="两次同 ID promote",
    )

    assert "Choose tools from their schemas" in prompt
    assert "hardcoded workflows" in prompt
    assert "Do not turn a terse or ambiguous operation report" not in prompt
    assert "ask for the missing target instead of creating an unrelated proposal" not in prompt


def test_strategy_author_skill_contains_soft_context_rules() -> None:
    text = (
        __import__("pathlib")
        .Path("nerya/skills/builtin/strategy_author/SKILL.md")
        .read_text(encoding="utf-8")
    )

    assert "Market context inheritance" in text
    assert "Use to author SDK strategy code" in text
    assert "- meme" in text
    assert "- wallet" in text
    assert "- onchain" in text
    assert "- polymarket" in text
    assert "- prediction-market" in text
    assert "advisory context for your judgment, not a hard router" in text
    assert "Preserve the market scope that the session has already established" in text
    assert "Examples are examples, not defaults" in text
    assert "Market scope assumption" in text
    # New draft -> edit -> validate -> submit lane.
    assert "SCAFFOLD the package as a draft proposal with `strategy_draft_proposal`" in text
    assert "AUTHOR the strategy by editing the staged files" in text
    assert "SUBMIT with `strategy_submit_proposal(" in text
    assert "strategy_draft_proposal" in text
    assert "strategy_submit_proposal" in text
    assert "strategy_generate_proposal" not in text
    assert "strategy_backtest({\"proposal_id\"" in text
    assert "--proposal-id <proposal_id>" in text
    assert "only when the action is still proposal validation" in text
    assert "Promotion changes the workspace" in text
    assert "proposal_paths" in text
    assert "When the operator names a `prp_*` proposal id" in text
    assert "Resolve and operate on the exact proposal first" in text
    assert "`proposal_id` into validation/backtest calls" in text
    assert "Do not substitute a promoted `strategy_id`" in text
    assert "recommended_coverage_ok" in text
    assert "attempted short-window real-data backtest" in text
    assert "Do not call the standard backtest unavailable" in text
    assert "do not rewrite the thesis into trend/scalping" in text
    assert "Paper review can continue" in text
    assert "Shadow/live progression still requires explicit operator approval" in text
    assert "`paper_review_allowed` or a `review_gate`" in text
    assert "Do not override it with a manual" in text
    assert "FAIL/no_trades rejection" in text
    assert "If `strategy_backtest` returns `ok:true`" in text
    assert "completed standard OHLCV" in text
    assert "reason:no_historical_data" in text
    assert "regenerate a strategy only because" in text
    assert "promoted strategy path is absent" in text
    assert "do not reply with a questionnaire" in text
    assert "non-live mode, modest sizing" in text
    assert "do not edit `main.py` away from the requested thesis" in text
    assert "draft the package files yourself with the Nerya strategy SDK" in text
    assert (
        "from nerya.strategies import StrategyContext, StrategyResult, StrategyAgentTask"
        in text
    )
    assert "do not import from nerya.sdk" in text
    assert "do not import from nerya.strategy" in text
    assert "Do not call StrategyResult.order" in text
    assert "Do not call StrategyResult.dispatch" in text
    assert "prediction-market/Polymarket evidence" in text
    assert "`strategy_submit_proposal` only validates and queues the package" in text
    assert "For custom strategies, author `main.py` by editing the staged file" in text
    assert "Do not call shell, glob, or raw file reads once the efficient evidence boundary is met" in text
    assert "Never pass `context=`" in text
    assert "`session_key` must be a" in text
    assert "not a string" in text
    assert "Never wrap the task with `ctx.result.agent_task" in text
    assert "StrategyAgentTask" in text
    assert "Never multiply raw `_pct` fields by 100" in text
    assert "For on-chain meme, news, social" in text
    assert "do not request shell just to" in text
    assert "stop discovery and write the SDK proposal" in text
    assert "Do not call shell, glob, or raw file reads" in text
    assert "the efficient evidence boundary is" in text
    assert "author the SDK strategy package immediately by editing the staged files" in text
    assert "Continue until a `strategy_submit_proposal` call for a validated SDK package exists" in text
    assert "preferred_provider" in text
    assert "not ready instead of silently substituting another provider" in text
    assert "Wallet Meme Quick Path" in text
    assert "selection.mode` is `wallet_binding`" in text
    assert "market_data` already returned" in text
    assert "exact chain:token" in text
    assert "do not install a fallback" in text
    assert "runtime scanner" in text
    assert "execution_mode: \"agent_task\"" in text
    assert "do not satisfy\nthat request with CEX proxies" in text
    assert "not a valid on-chain\nbacktest" in text
    assert "Do not copy those low-level action names into" in text
    assert "StrategyAgentTask` prompts or operator-facing strategy docs" in text
    assert "Do not call\n`strategy_promote` during an ordinary" in text
    assert "do not set `operator_approved: true` yourself" in text


def test_strategy_draft_and_submit_descriptions_describe_the_lane(tmp_path) -> None:
    cfg = _config(tmp_path)
    registry = ToolRegistry()
    deps = build_native_tool_deps(
        workspace_root=cfg.paths.root,
        skill_roots=[],
        paths=cfg.paths,
        config=cfg,
        skills=None,
    )
    register_native_tools(registry, deps)

    # The deprecated single-shot tool is no longer exposed to the agent.
    assert registry.find("strategy_generate_proposal") is None

    draft_desc = registry.get("strategy_draft_proposal").description
    assert "DRAFT proposal" in draft_desc
    assert "from_strategy_id to iterate on it" in draft_desc
    assert "proposal_paths" in draft_desc
    assert "next_steps" in draft_desc
    assert "does NOT enter the pending-review queue and writes NO inline code" in draft_desc
    assert "editing the staged files with read_file + edit_file / write_file" in draft_desc
    assert "run strategy_validate" in draft_desc
    assert "finish with strategy_submit_proposal" in draft_desc
    assert "skill_view" in draft_desc
    # SDK contract reminders survive on the scaffold tool.
    assert (
        "from nerya.strategies import StrategyContext, StrategyResult, StrategyAgentTask"
        in draft_desc
    )
    assert "ctx.portfolio.positions(market)" in draft_desc
    assert "ctx.config.accounts[0]" in draft_desc
    assert "there is no ctx.account_id" in draft_desc
    assert "Preserve explicit session/tool-evidence market scope" in draft_desc

    submit_desc = registry.get("strategy_submit_proposal").description
    assert "re-validate the edited" in submit_desc
    assert "draft -> pending_review" in submit_desc
    assert "only if validation passes" in submit_desc
    assert "the blockers are returned so you can edit the files and submit again" in submit_desc
    assert "backtest_required" in submit_desc
    assert "next_required_action" in submit_desc

    write_desc = registry.get("write_file").description
    assert "first call strategy_draft_proposal to scaffold a draft" in write_desc
    assert "after/strategies/<id>/" in write_desc
    assert "never write directly into the live" in write_desc
    assert "proposal-only" in write_desc

    promote_desc = registry.get("strategy_promote").description
    assert "ordinary create/review strategy request" in promote_desc
    assert "explicitly asks to promote" in promote_desc


def test_strategy_generate_proposal_requires_sdk_files_for_onchain_wallet_scope(tmp_path) -> None:
    cfg = _config(tmp_path)

    result = strategy_generate_proposal_handler(
        ToolCall(
            name="strategy_generate_proposal",
            arguments={
                "strategy_id": "solana_meme_smart_money",
                "title": "Solana meme smart money",
                "description": "Use wallet/on-chain smart money evidence.",
                "markets": [
                    "BYREAL_ONCHAIN:solana:4pMsh7JF5wXjkx8sK6gJgv14xkBy1kUoMv4ixN8npump"
                ],
                "accounts": ["paper"],
            },
        ),
        config=cfg,
    )

    assert result.is_error is True
    assert "must include `files.main.py`" in result.text()
    assert not list(cfg.paths.evolution.glob("proposals/prp_*"))


def test_strategy_generate_proposal_allows_cex_cash_carry_perp_swap_without_inline_files(
    tmp_path,
) -> None:
    cfg = _config(tmp_path)

    result = strategy_generate_proposal_handler(
        ToolCall(
            name="strategy_generate_proposal",
            arguments={
                "strategy_id": "binance_spot_aster_perp_carry",
                "title": "Binance Spot + Aster Perpetual Cash-and-Carry",
                "description": (
                    "Delta-neutral cash-and-carry arbitrage. Long spot on "
                    "Binance, short an equal-notional perpetual swap. Runtime "
                    "perp leg uses Binance USDT-M as fallback while the Aster "
                    "connector proposal is pending review."
                ),
                "prompt": (
                    "Build a cash-and-carry basis strategy using Binance spot "
                    "and a perpetual swap leg, with funding evidence and an "
                    "Aster migration note."
                ),
                "strategy_class": "trend",
                "execution_mode": "script",
                "mode": "paper",
                "markets": ["binance:BTCUSDT", "binance:BTCUSDT-PERP"],
                "accounts": ["binance_paper"],
            },
            metadata={
                "original_user_prompt": (
                    "做一个 Binance 现货 + Aster 永续的 cash-and-carry 套利策略，回测 30 天"
                )
            },
        ),
        config=cfg,
    )

    assert result.is_error is False, result.text()
    data = result.content[0].data
    proposal_root = cfg.paths.evolution / "proposals" / data["proposal_id"]
    proposal_text = (proposal_root / "proposal.yml").read_text(encoding="utf-8")
    strategy_text = (
        proposal_root
        / "after"
        / "strategies"
        / "binance_spot_aster_perp_carry"
        / "strategy.yml"
    ).read_text(encoding="utf-8")
    combined = f"{proposal_text}\n{strategy_text}".lower()
    for needle in ("cash", "carry", "aster", "binance"):
        assert needle in combined


def test_strategy_generate_proposal_requires_main_for_named_custom_signals(tmp_path) -> None:
    cfg = _config(tmp_path)

    result = strategy_generate_proposal_handler(
        ToolCall(
            name="strategy_generate_proposal",
            arguments={
                "strategy_id": "btc_macd_agent_4h",
                "title": "BTC MACD Agent",
                "description": "MACD 12/26/9 cross dispatches to an Agent.",
                "strategy_class": "agent",
                "execution_mode": "agent",
                "markets": ["binance:BTCUSDT"],
                "accounts": ["paper"],
            },
        ),
        config=cfg,
    )

    assert result.is_error is True
    assert "named custom signal logic" in result.text()
    assert "`files.main.py`" in result.text()
    assert not list(cfg.paths.evolution.glob("proposals/prp_*"))


def test_strategy_generate_proposal_rejects_main_py_missing_requested_signal_terms(
    tmp_path,
) -> None:
    cfg = _config(tmp_path)

    result = strategy_generate_proposal_handler(
        ToolCall(
            name="strategy_generate_proposal",
            arguments={
                "strategy_id": "eth_rsi_agent_breakout",
                "title": "ETH RSI Agent breakout",
                "description": "Agent judges false breakout with market context.",
                "prompt": "Agent judges whether an ETH move is a real reversal.",
                "strategy_class": "agent",
                "execution_mode": "agent",
                "markets": ["BINANCE:ETHUSDT"],
                "accounts": ["paper"],
                "files": {
                    "main.py": (
                        "from nerya.strategies import StrategyAgentTask, StrategyContext\n\n"
                        "def run(ctx: StrategyContext):\n"
                        "    return StrategyAgentTask.dispatch(\n"
                        "        prompt='Judge whether this is a real reversal or false breakout.'\n"
                        "    )\n"
                    ),
                    "strategy.md": (
                        "ETH 1h strategy where RSI(14), funding, and order flow "
                        "should trigger the Agent."
                    ),
                },
            },
            metadata={
                "original_user_prompt": (
                    "ETH 1h，RSI(14) 低于 30 或高于 70 时触发 Agent，"
                    "让它结合资金费率和大单流向判断是真反转还是假突破"
                )
            },
        ),
        config=cfg,
    )

    assert result.is_error is True
    text = result.text().lower()
    assert "files.main.py" in text
    assert "rsi" in text
    assert not list(cfg.paths.evolution.glob("proposals/prp_*"))


def test_strategy_generate_proposal_ignores_model_invented_agent_custom_scope(
    tmp_path,
) -> None:
    cfg = _config(tmp_path)

    result = strategy_generate_proposal_handler(
        ToolCall(
            name="strategy_generate_proposal",
            arguments={
                "strategy_id": "btc_momentum_1h",
                "title": "BTC MACD Agent",
                "description": "MACD 12/26/9 cross dispatches to an Agent.",
                "prompt": "MACD + RSI agent momentum strategy",
                "strategy_class": "agent",
                "execution_mode": "script",
                "markets": ["BINANCE:BTCUSDT"],
                "accounts": ["paper"],
            },
            metadata={
                "original_user_prompt": "帮我做一个 1 小时级别的 BTC 现货动量策略，回测一下",
            },
        ),
        config=cfg,
    )

    assert result.is_error is False
    data = result.content[0].data
    assert data["strategy_id"] == "btc_momentum_1h"
    assert data["proposal_id"]
    assert data["validation"]["ok"] is True


def test_strategy_generate_proposal_rejects_script_mode_for_agent_decision(tmp_path) -> None:
    cfg = _config(tmp_path)

    result = strategy_generate_proposal_handler(
        ToolCall(
            name="strategy_generate_proposal",
            arguments={
                "strategy_id": "btc_confluence_script",
                "title": "BTC confluence script",
                "prompt": (
                    "MACD + RSI + volume confluence; let Agent decide. "
                    "If any precondition fails, skip."
                ),
                "strategy_class": "scalping",
                "execution_mode": "script",
                "markets": ["BINANCE:BTCUSDT"],
                "accounts": ["paper"],
                "files": {
                    "main.py": (
                        "from nerya.strategies import StrategyContext\n\n"
                        "def run(ctx: StrategyContext):\n"
                        "    return ctx.result.hold(reason='skip')\n"
                    ),
                    "strategy.md": "Agent should decide only after all filters pass.",
                },
            },
        ),
        config=cfg,
    )

    assert result.is_error is True
    assert "Agent-task strategy" in result.text()
    assert "StrategyAgentTask.skip" in result.text()
    assert not list(cfg.paths.evolution.glob("proposals/prp_*"))


def test_agent_task_validator_rejects_structural_no_dispatch_branch(tmp_path) -> None:
    cfg = _config(tmp_path)

    result = strategy_generate_proposal_handler(
        ToolCall(
            name="strategy_generate_proposal",
            arguments={
                "strategy_id": "btc_confluence_agent",
                "title": "BTC confluence agent",
                "description": (
                    "Only dispatch to the Agent when all indicator "
                    "preconditions pass; otherwise skip."
                ),
                "strategy_class": "agent",
                "execution_mode": "agent",
                "markets": ["binance:BTCUSDT"],
                "accounts": ["paper"],
                    "files": {
                        "main.py": (
                            "from nerya.strategies import StrategyAgentTask, StrategyContext\n\n"
                            "def run(ctx: StrategyContext):\n"
                            "    if not ctx.market.candles('binance:BTCUSDT', timeframe='1h', limit=20):\n"
                            "        return ctx.result.hold(reason='no candles')\n"
                            "    return StrategyAgentTask.dispatch(prompt='conditions passed')\n"
                        ),
                        "strategy.md": "Agent task with explicit precondition skip path.",
                    },
            },
        ),
        config=cfg,
    )

    # Validation now gates creation: a package with blockers does not enter
    # the pending-review queue. The handler hands the blockers back to the
    # agent as a schema-validation error (with a fix-and-retry hint) instead.
    assert result.is_error is True
    assert result.error is not None
    assert result.error.kind == ToolErrorKind.SCHEMA_VALIDATION
    validation = result.error.recovery_hint["validation"]
    assert validation["ok"] is False
    assert any(
        issue["code"] == "agent_task_skip_status"
        for issue in validation["blockers"]
    )
    assert "StrategyAgentTask.skip" in json.dumps(validation)
    # No pending proposal should have been written for the invalid package.
    assert not list(cfg.paths.evolution.glob("proposals/prp_*"))


def test_strategy_generate_proposal_requires_sdk_files_for_polymarket_scope(tmp_path) -> None:
    cfg = _config(tmp_path)

    result = strategy_generate_proposal_handler(
        ToolCall(
            name="strategy_generate_proposal",
            arguments={
                "strategy_id": "polymarket_headline_edge",
                "title": "Polymarket headline edge",
                "description": "Use Polymarket CLOB and headline evidence.",
                "strategy_class": "news",
                "markets": ["POLYMARKET:event-slug"],
                "accounts": ["paper_polymarket"],
            },
        ),
        config=cfg,
    )

    assert result.is_error is True
    assert "prediction-market strategy proposals must include" in result.text()
    assert "must include `files.main.py`" in result.text()
    assert not list(cfg.paths.evolution.glob("proposals/prp_*"))


def test_strategy_generate_proposal_rejects_placeholder_onchain_backtest(tmp_path) -> None:
    cfg = _config(tmp_path)

    result = strategy_generate_proposal_handler(
        ToolCall(
            name="strategy_generate_proposal",
            arguments={
                "strategy_id": "solana_meme_smart_money",
                "title": "Solana meme smart money",
                "description": "Use wallet/on-chain smart money evidence.",
                "markets": [
                    "BYREAL_ONCHAIN:solana:4pMsh7JF5wXjkx8sK6gJgv14xkBy1kUoMv4ixN8npump"
                ],
                "accounts": ["paper"],
                "files": {
                    "main.py": "from nerya.strategies import StrategyContext\n",
                    "strategy.md": "# smart money\n",
                    "backtests/research_backtest.py": (
                        "# 模拟回测结果\n"
                        "print('NERYA_FREEFORM_RESULT_JSON={\"equity_curve\": [], \"trades\": []}')\n"
                    ),
                },
            },
        ),
        config=cfg,
    )

    assert result.is_error is True
    assert "placeholder or simulated examples" in result.text()
    assert not list(cfg.paths.evolution.glob("proposals/prp_*"))


def test_run_shell_description_defers_strategy_authoring_to_native_tools(tmp_path) -> None:
    cfg = _config(tmp_path)
    registry = ToolRegistry()
    deps = build_native_tool_deps(
        workspace_root=cfg.paths.root,
        skill_roots=[],
        paths=cfg.paths,
        config=cfg,
        skills=None,
    )
    register_native_tools(registry, deps)

    desc = registry.get("run_shell").description
    assert "Do not use this for strategy authoring" in desc
    assert "connector/data-source discovery" in desc
    assert "wallet/on-chain provider inspection" in desc
    # Strategy authoring is the file-editing lane: draft scaffold, then
    # edit_file / write_file on the staged proposal files.
    assert "strategy_draft_proposal followed" in desc
    assert "edit_file / write_file on the staged proposal files" in desc
    assert "to author SDK code" in desc
    assert "reserve shell for explicit operator commands" in desc


def test_data_api_description_uses_structured_next_action_for_sdk_authoring(tmp_path) -> None:
    cfg = _config(tmp_path)
    registry = ToolRegistry()
    deps = build_native_tool_deps(
        workspace_root=cfg.paths.root,
        skill_roots=[],
        paths=cfg.paths,
        config=cfg,
        skills=None,
    )
    register_native_tools(registry, deps)

    desc = registry.get("data_api").description
    assert "next_required_action" in desc
    assert "bounded_sequence" in desc
    assert "authoritative structured continuation" in desc
    assert "operator-named provider preferences" in desc
    assert "silently substituting another route" in desc


def test_strategy_generate_proposal_rejects_file_directory_collision(tmp_path) -> None:
    cfg = _config(tmp_path)

    result = strategy_generate_proposal_handler(
        ToolCall(
            name="strategy_generate_proposal",
            arguments={
                "strategy_id": "btc_directory_collision",
                "title": "BTC Directory Collision",
                "description": "BTC strategy with an invalid file override.",
                "prompt": "Create a BTC paper strategy.",
                "strategy_class": "trend",
                "execution_mode": "script",
                "mode": "paper",
                "markets": ["binance:BTCUSDT"],
                "accounts": ["paper-main"],
                "files": {
                    "tests": "test_main.py",
                },
            },
        ),
        config=cfg,
    )

    assert result.is_error is True
    text = result.text().lower()
    assert "file path collides" in text
    assert "tests" in text
    assert "permissionerror" not in text
    assert not list(cfg.paths.evolution.glob("proposals/prp_*"))


def test_evolve_proposals_description_supports_exact_lookup(tmp_path) -> None:
    cfg = _config(tmp_path)
    registry = ToolRegistry()
    deps = build_native_tool_deps(
        workspace_root=cfg.paths.root,
        skill_roots=[],
        paths=cfg.paths,
        config=cfg,
        skills=None,
    )
    register_native_tools(registry, deps)

    desc = registry.get("evolve_proposals").description
    assert "proposal_id" in desc
    assert "exact read-only lookup" in desc
    assert "instead of using shell" in desc


def test_strategy_backtest_tool_supports_proposal_targets(tmp_path) -> None:
    cfg = _config(tmp_path)
    registry = ToolRegistry()
    deps = build_native_tool_deps(
        workspace_root=cfg.paths.root,
        skill_roots=[],
        paths=cfg.paths,
        config=cfg,
        skills=None,
    )
    register_native_tools(registry, deps)

    desc = registry.get("strategy_backtest").description
    assert "proposal_id" in desc
    assert "do not substitute a similarly named promoted strategy_id" in desc
    assert "before promotion" in desc
    assert "45-day window" in desc
    assert "recommended_coverage_ok" in desc
    assert "short-window backtest" in desc
    assert "Do not call the standard backtest unavailable" in desc
    assert "custom/event-driven packages" in desc
    assert "review_gate" in desc
    assert "paper_review_allowed" in desc
    assert "manual FAIL/no_trades rejection" in desc
    assert "completed standard OHLCV replay" in desc
    assert "zero trades means no simulated OHLCV fills" in desc
    assert "different template" in desc
    assert "explicit operator approval" in desc
    assert "no_historical_data" in desc
    assert "strategy_root" in desc
    assert "metrics_display" in desc
    assert "percentage points" in desc
    assert "Never multiply them by 100" in desc
    assert "operator_summary_text" in desc
    assert "raw_metrics_file" in desc


def test_team_run_tool_keeps_template_choice_catalog_based() -> None:
    source = Path("nerya/tools/native/bootstrap.py").read_text(encoding="utf-8")
    schema_text = str(TEAM_RUN_SCHEMA)

    assert "several independent securities" not in source
    assert "several tickers" not in source
    assert "hidden template" in source
    assert "market_analysis_team" in schema_text
    assert "max_parallel" in schema_text


def test_team_run_tool_does_not_force_committee_debate_route() -> None:
    source = Path("nerya/tools/native/bootstrap.py").read_text(encoding="utf-8")
    schema_text = str(TEAM_RUN_SCHEMA)

    assert "bull/bear" not in source
    assert "do not simulate" not in source.lower()
    assert "investment_committee_team" in schema_text


def test_market_context_module_does_not_keyword_infer_domains() -> None:
    text = (
        __import__("pathlib")
        .Path("nerya/agent/market_context.py")
        .read_text(encoding="utf-8")
    )

    assert "infer_market_context" not in text
    assert "update_session_market_context_from_text" not in text
    assert "TERMS" not in text
    assert "for term in" not in text


def test_run_turn_reuses_requested_turn_id_for_resume(monkeypatch, tmp_path) -> None:
    cfg = _config(tmp_path)
    calls: list[dict] = []

    def fake_run_turn(self, **kwargs):  # noqa: ANN001
        calls.append(kwargs)
        return AgentTurnResult(
            trigger_event_id="evt_resume",
            strategy_id=kwargs.get("strategy_id"),
            session_id=kwargs.get("session_id"),
            turn_id=kwargs.get("turn_id") or "new_turn",
            decision={"action": "send_message", "text": "resumed"},
            actions=[{"action": "send_message", "ok": True, "text": "resumed"}],
            tool_trace=[],
            stopped_reason="end_turn",
            final_text="resumed",
        )

    monkeypatch.setattr(AgentKernel, "run_turn", fake_run_turn)
    client = SimpleNamespace(config=cfg, skills=None)
    run_turn = next(
        handler
        for method, path, handler in routes_agent.routes()
        if method == "POST" and path == "/agent/run_turn"
    )

    response = run_turn(
        client,
        {
            "session_id": "sess_resume",
            "turn_id": "turn_pending",
            "strategy_id": "s1",
            "trigger": {
                "id": "evt_resume",
                "source": "approval_continue",
                "payload": {"text": "continue"},
            },
        },
    )

    assert calls[0]["turn_id"] == "turn_pending"
    assert response["turn_id"] == "turn_pending"
