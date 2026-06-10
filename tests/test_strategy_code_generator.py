from __future__ import annotations

import json
from pathlib import Path

import pytest

from nerya.api.routes_strategies_runtime import _request_from_payload
from nerya.core import yaml_io
from nerya.core.config import Config
from nerya.core.paths import WorkspacePaths
from nerya.evolution.strategy_code_generator import (
    StrategyCodeGenerator,
    StrategyGenerationRequest,
)
from nerya.strategies import performance as performance_mod
from nerya.strategies.package import load_package
from nerya.strategies.performance import build_snapshot
from nerya.strategies.agent_task import StrategyAgentTask
from nerya.tools.native.strategy_runtime import (
    STRATEGY_GENERATE_PROPOSAL_SCHEMA,
    _request_from_args,
    _with_inferred_news_sources,
    strategy_promote_handler,
)
from nerya.tools.types import ToolCall

pytestmark = pytest.mark.smoke


def test_strategy_generator_normalizes_natural_language_tuning_objectives(tmp_path):
    req = StrategyGenerationRequest(
        strategy_id="amzn_daily_team_long",
        title="AMZN daily team long",
        strategy_class="trend",
        markets=("yahoo:AMZN",),
        accounts=("paper_main",),
        tuning_objectives=(
            "Increase risk-adjusted returns without raising drawdown above 20%",
        ),
    )

    out = StrategyCodeGenerator(WorkspacePaths(root=tmp_path)).generate(
        req,
        validate=True,
        create_proposal_record=False,
    )

    manifest = yaml_io.loads(out.files["strategy.yml"])
    assert out.validation is not None
    assert out.validation.ok
    assert manifest["tuning"]["objectives"] == [
        "risk_adjusted_return",
        "drawdown",
        "return",
    ]


def test_strategy_generator_routes_tuning_to_medium_tier(tmp_path):
    req = StrategyGenerationRequest(
        strategy_id="btc_tuning_medium",
        strategy_class="scalping",
        markets=("binance:BTCUSDT",),
        accounts=("paper_main",),
    )

    out = StrategyCodeGenerator(WorkspacePaths(root=tmp_path)).generate(
        req,
        validate=True,
        create_proposal_record=False,
    )

    manifest = yaml_io.loads(out.files["strategy.yml"])
    assert manifest["tuning"]["subagent"]["tier"] == "medium"


def test_strategy_generator_news_template_gates_news_surface_in_backtest(tmp_path):
    req = StrategyGenerationRequest(
        strategy_id="tsla_news_alpha",
        strategy_class="news",
        execution_mode="script",
        markets=("yahoo:TSLA",),
        accounts=("paper_main",),
    )

    out = StrategyCodeGenerator(WorkspacePaths(root=tmp_path)).generate(
        req,
        validate=True,
        create_proposal_record=False,
    )

    main_py = out.files["main.py"]
    gate_pos = main_py.index("if getattr(ctx, 'runmode', '') == 'backtest':")
    news_pos = main_py.index("ctx.news.fetch")
    assert gate_pos < news_pos
    assert "def _backtest_news_result" in main_py
    assert "news surface disabled in OHLCV backtest" in main_py


def test_strategy_generator_normalizes_agent_authored_overrides(tmp_path):
    req = StrategyGenerationRequest(
        strategy_id="solana_smartmoney_meme",
        strategy_class="agent",
        execution_mode="agent_task",
        markets=("SOLANA",),
        accounts=("paper",),
        files={
            "strategy.yml": """
strategy_id: solana_smartmoney_meme
title: Solana Smart Money Meme Alpha
version: 1
markets:
  - SOLANA
accounts:
  - paper
mode: paper
schedule:
  cron: "*/30 * * * *"
llm_policy:
  tier: core
tuning:
  enabled: true
""",
            "main.py": """
from nerya.strategies.context import StrategyContext
from nerya.strategies.result import StrategyResult, StrategyAgentTask

def run(ctx: StrategyContext) -> StrategyResult:
    default_order = ctx.config.policy.get("default_order_usd", 50)
    return ctx.result.hold(reason="no qualifying signal")
""",
        },
    )

    out = StrategyCodeGenerator(WorkspacePaths(root=tmp_path)).generate(
        req,
        validate=True,
        create_proposal_record=False,
    )

    manifest = yaml_io.loads(out.files["strategy.yml"])
    assert out.validation is not None
    assert out.validation.ok
    assert manifest["schedule"] == {
        "cron": "*/30 * * * *",
        "enabled": True,
        "type": "cron",
    }
    assert manifest["tuning"]["schedule"] == {
        "cron": "0 */6 * * *",
        "enabled": True,
        "type": "cron",
    }
    assert manifest["llm_policy"]["default_tier"] == "medium"
    assert "from nerya.strategies import StrategyResult, StrategyAgentTask" in out.files["main.py"]
    assert "ctx.config.policy" not in out.files["main.py"]
    assert "ctx.policy.get" in out.files["main.py"]


def test_strategy_generator_normalizes_common_agent_task_sdk_mistakes(tmp_path):
    req = StrategyGenerationRequest(
        strategy_id="solana_smartmoney_meme",
        strategy_class="agent",
        execution_mode="agent_task",
        markets=("XAGT_ONCHAIN:solana",),
        accounts=("paper",),
        files={
            "main.py": """
from nerya.strategies import StrategyAgentTask, StrategyContext

def run(ctx: StrategyContext):
    task = StrategyAgentTask.dispatch(
        prompt="inspect wallet flow",
        context={"route": "XAGT_ONCHAIN:solana"},
        reason="smart money scan",
    )
    return ctx.result.agent_task(task)
""",
        },
    )

    out = StrategyCodeGenerator(WorkspacePaths(root=tmp_path)).generate(
        req,
        validate=True,
        create_proposal_record=False,
    )

    main_py = out.files["main.py"]
    assert "context=" not in main_py
    assert "metadata=" in main_py
    assert "ctx.result.agent_task" not in main_py
    assert "return task" in main_py


def test_strategy_generator_replaces_legacy_bar_main_for_agent_team(tmp_path):
    req = StrategyGenerationRequest(
        strategy_id="sol_short_term_scalping_v1",
        strategy_class="scalping",
        execution_mode="agent_team",
        markets=("binance:SOLUSDT",),
        accounts=("paper",),
        files={
            "main.py": """
from nerya.strategies import StrategyContext, StrategyResult

class SolShortTermStrategy:
    def on_bar(self, bar):
        result = StrategyResult()
        result.hold()
        return result

def run(context: StrategyContext):
    bar = context.get_current_bar()
    if bar is None:
        context.result.hold()
        return
    return SolShortTermStrategy().on_bar(bar)
""",
        },
    )

    out = StrategyCodeGenerator(WorkspacePaths(root=tmp_path)).generate(
        req,
        validate=True,
        create_proposal_record=False,
    )

    main_py = out.files["main.py"]
    assert out.validation is not None
    assert out.validation.ok
    assert "get_current_bar" not in main_py
    assert "StrategyResult()" not in main_py
    assert "StrategyAgentTask.dispatch" in main_py
    assert "team_run" in main_py


def test_strategy_generator_normalizes_agent_task_constructor_kwargs(tmp_path):
    req = StrategyGenerationRequest(
        strategy_id="btc_bb_squeeze_agent",
        strategy_class="agent",
        execution_mode="agent_task",
        markets=("BINANCE:BTCUSDT",),
        accounts=("paper_main",),
        files={
            "main.py": """
from nerya.strategies import StrategyAgentTask, StrategyContext

def run(ctx: StrategyContext):
    return StrategyAgentTask(
        prompt="Assess the Bollinger squeeze breakout before any paper order.",
        symbol="BTCUSDT",
        timeframe="15m",
        indicators=["bollinger_bandwidth", "breakout_direction"],
        reason="squeeze_detected",
    )
""",
        },
    )

    out = StrategyCodeGenerator(WorkspacePaths(root=tmp_path)).generate(
        req,
        validate=True,
        create_proposal_record=False,
    )

    main_py = out.files["main.py"]
    assert out.validation is not None
    assert out.validation.ok
    assert "StrategyAgentTask.dispatch(" in main_py
    assert "metadata={" in main_py
    assert "symbol=" not in main_py
    assert "timeframe=" not in main_py
    assert "indicators=" not in main_py


def test_strategy_generator_normalizes_agent_dispatch_skip_to_agent_task_skip(tmp_path):
    req = StrategyGenerationRequest(
        strategy_id="consensus_4filter_agent",
        strategy_class="agent",
        execution_mode="agent",
        markets=("binance:BTCUSDT",),
        accounts=("paper_main",),
        files={
            "main.py": """
from nerya.strategies.result import StrategyResult

def run(ctx):
    if not ctx.trigger.get("all_pass"):
        return StrategyResult.skip(
            reason="filters_failed",
            metadata={"failed_filters": ["macd"]},
        )
    from nerya.strategies import StrategyAgentTask

    return StrategyAgentTask.dispatch(
        prompt="all filters passed; decide sizing",
        metadata={"gate": "confluence"},
    )
""",
        },
    )

    out = StrategyCodeGenerator(WorkspacePaths(root=tmp_path)).generate(
        req,
        validate=True,
        create_proposal_record=False,
    )

    main_py = out.files["main.py"]
    assert "return StrategyAgentTask.skip(" in main_py
    assert "StrategyResult.skip" not in main_py
    assert "from nerya.strategies import StrategyAgentTask" in main_py
    assert out.validation is not None
    assert out.validation.ok


def test_strategy_generator_normalizes_agent_candle_facade_aliases(tmp_path):
    req = StrategyGenerationRequest(
        strategy_id="spy_confluence_agent",
        strategy_class="agent",
        execution_mode="agent",
        markets=("yahoo:SPY",),
        accounts=("paper_main",),
        files={
            "main.py": """
from __future__ import annotations

import numpy as np

from nerya.strategies import StrategyAgentTask, StrategyContext

RESISTANCE_LOOKBACK = 60


def run(ctx: StrategyContext):
    candles = ctx.market.candles(interval="1d", count=RESISTANCE_LOOKBACK + 50)
    if not candles:
        return StrategyAgentTask.skip(reason="no_data")
    closes = np.array([c.close for c in candles], dtype=float)
    volumes = np.array([c.volume for c in candles], dtype=float)
    if float(closes[-1]) <= 0 or float(volumes[-1]) <= 0:
        return StrategyAgentTask.skip(reason="bad_data")
    return StrategyAgentTask.dispatch(prompt="decide", metadata={"close": float(closes[-1])})
""",
        },
    )

    out = StrategyCodeGenerator(WorkspacePaths(root=tmp_path)).generate(
        req,
        validate=True,
        create_proposal_record=False,
    )

    main_py = out.files["main.py"]
    assert 'ctx.market.candles(ctx.config.markets[0], timeframe="1d", limit=RESISTANCE_LOOKBACK + 50)' in main_py
    assert "interval=" not in main_py
    assert "count=RESISTANCE_LOOKBACK" not in main_py
    assert 'c["close"]' in main_py
    assert 'c["volume"]' in main_py
    assert ".close" not in main_py
    assert ".volume" not in main_py
    assert out.validation is not None
    assert out.validation.ok


def test_strategy_generator_normalizes_indexed_candle_row_aliases(tmp_path):
    req = StrategyGenerationRequest(
        strategy_id="tsla_pullback",
        strategy_class="trend",
        execution_mode="script",
        markets=("yahoo:TSLA",),
        accounts=("paper_main",),
        files={
            "main.py": """
from nerya.strategies import StrategyContext, StrategyResult


def run(ctx: StrategyContext) -> StrategyResult:
    candles = ctx.market.candles(interval="1d", count=120)
    last = candles[-1]
    prev = candles[-2]
    if last.close < prev.close:
        return ctx.result.hold(reason="pullback")
    return ctx.result.hold(reason="no_signal")
""",
        },
    )

    out = StrategyCodeGenerator(WorkspacePaths(root=tmp_path)).generate(
        req,
        validate=True,
        create_proposal_record=False,
    )

    main_py = out.files["main.py"]
    assert 'ctx.market.candles(ctx.config.markets[0], timeframe="1d", limit=120)' in main_py
    assert 'last["close"]' in main_py
    assert 'prev["close"]' in main_py
    assert ".close" not in main_py
    assert out.validation is not None
    assert out.validation.ok


def test_strategy_generator_normalizes_result_builder_positional_reasons(tmp_path):
    req = StrategyGenerationRequest(
        strategy_id="tsla_result_contract",
        strategy_class="trend",
        execution_mode="script",
        markets=("yahoo:TSLA",),
        accounts=("paper_main",),
        files={
            "main.py": """
from nerya.strategies import StrategyContext, StrategyResult


def run(ctx: StrategyContext) -> StrategyResult:
    candles = ctx.market.candles(ctx.config.markets[0], timeframe="1d", limit=120)
    if not candles:
        return ctx.result.skip("insufficient_history")
    if ctx.trigger.get("bookkeeping"):
        return ctx.result.ok("bookkeeping_done")
    return ctx.result.hold("no_signal", {"bars": len(candles)})
""",
        },
    )

    out = StrategyCodeGenerator(WorkspacePaths(root=tmp_path)).generate(
        req,
        validate=True,
        create_proposal_record=False,
    )

    main_py = out.files["main.py"]
    assert 'ctx.result.skip(reason="insufficient_history")' in main_py
    assert 'ctx.result.ok(reason="bookkeeping_done")' in main_py
    assert 'ctx.result.hold(reason="no_signal", metadata={"bars": len(candles)})' in main_py
    assert out.validation is not None
    assert out.validation.ok


def test_strategy_generator_normalizes_agent_task_factory_positional_metadata(tmp_path):
    req = StrategyGenerationRequest(
        strategy_id="btc_mtf_agent_task",
        strategy_class="agent",
        execution_mode="agent",
        markets=("BINANCE:BTCUSDT",),
        accounts=("paper_main",),
        files={
            "main.py": """
from nerya.strategies import StrategyAgentTask, StrategyContext


def run(ctx: StrategyContext):
    candles = ctx.market.candles(ctx.config.markets[0], timeframe="1h", limit=120)
    if len(candles) < 30:
        return StrategyAgentTask.error("insufficient_history", {"bars": len(candles)})
    if candles[-1]["close"] < candles[-2]["close"]:
        return StrategyAgentTask.skip("trend_filter_failed", {"close": candles[-1]["close"]})
    return StrategyAgentTask.dispatch(prompt="Review confluence before entry.")
""",
        },
    )

    out = StrategyCodeGenerator(WorkspacePaths(root=tmp_path)).generate(
        req,
        validate=True,
        create_proposal_record=False,
    )

    main_py = out.files["main.py"]
    assert 'StrategyAgentTask.error(reason="insufficient_history", metadata={"bars": len(candles)})' in main_py
    assert 'StrategyAgentTask.skip(reason="trend_filter_failed", metadata={"close": candles[-1]["close"]})' in main_py
    assert out.validation is not None
    assert out.validation.ok


def test_strategy_generator_normalizes_strategy_sdk_imports(tmp_path):
    req = StrategyGenerationRequest(
        strategy_id="btc_funding_short_arb",
        strategy_class="agent",
        execution_mode="agent",
        markets=("binance_perpetual:BTC/USDT:USDT",),
        accounts=("paper_main",),
        files={
            "main.py": """
from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from strategy_sdk import StrategyContext


def run(ctx: "StrategyContext") -> Any:
    from strategy_sdk import StrategyAgentTask

    candles = ctx.market.candles(ctx.config.markets[0], timeframe="1h", limit=50)
    if not candles:
        return StrategyAgentTask.skip(reason="no_data")
    return StrategyAgentTask.dispatch(prompt="decide funding arb")
""",
        },
    )

    out = StrategyCodeGenerator(WorkspacePaths(root=tmp_path)).generate(
        req,
        validate=True,
        create_proposal_record=False,
    )

    main_py = out.files["main.py"]
    assert "strategy_sdk" not in main_py
    assert "from nerya.strategies import StrategyContext" in main_py
    assert "from nerya.strategies import StrategyAgentTask" in main_py
    assert out.validation is not None
    assert out.validation.ok


def test_strategy_generator_normalizes_market_facade_aliases(tmp_path):
    req = StrategyGenerationRequest(
        strategy_id="btc_funding_short_arb",
        strategy_class="agent",
        execution_mode="agent",
        markets=("binance_perpetual:BTC/USDT:USDT",),
        accounts=("paper_main",),
        files={
            "main.py": """
from nerya.strategies import StrategyAgentTask, StrategyContext


def run(ctx: StrategyContext):
    market = ctx.market
    ticker = market.ticker(symbol="BTCUSDT")
    candles_1h = market.candles("1h", limit=24)
    closes = [c.close for c in candles_1h]
    features = market.features()
    funding_history = market.features(
        ctx.config.markets[0],
        symbol="BTCUSDT",
        feature="funding_history",
        lookback=50,
    )
    positions = ctx.portfolio.positions or []
    return StrategyAgentTask.dispatch(
        prompt="decide",
        metadata={
            "close": closes[-1] if closes else 0,
            "features": features,
            "funding_history": funding_history,
            "positions": positions,
            "ticker": ticker,
        },
    )
""",
        },
    )

    out = StrategyCodeGenerator(WorkspacePaths(root=tmp_path)).generate(
        req,
        validate=True,
        create_proposal_record=False,
    )

    main_py = out.files["main.py"]
    assert 'market.ticker(ctx.config.markets[0])' in main_py
    assert 'market.candles(ctx.config.markets[0], timeframe="1h", limit=24)' in main_py
    assert "market.features(ctx.config.markets[0])" in main_py
    assert "market.features(ctx.config.markets[0], lookback=50)" in main_py
    assert "symbol=" not in main_py
    assert "feature=" not in main_py
    assert "ctx.portfolio.positions(ctx.config.markets[0]) or []" in main_py
    assert 'c["close"]' in main_py
    assert ".close" not in main_py
    assert out.validation is not None
    assert out.validation.ok


def test_strategy_agent_task_accepts_string_session_key_for_generated_code() -> None:
    task = StrategyAgentTask.dispatch(
        prompt="inspect wallet flow",
        session_key="smartmoney_XAGT_ONCHAIN_solana",
        metadata={"market": "XAGT_ONCHAIN:solana"},
    )

    assert task.session_key == {"key": "smartmoney_XAGT_ONCHAIN_solana"}
    assert task.metadata == {"market": "XAGT_ONCHAIN:solana"}


def test_strategy_generator_normalizes_legacy_execution_schedule(tmp_path):
    req = StrategyGenerationRequest(
        strategy_id="solana_smartmoney_meme",
        strategy_class="agent",
        execution_mode="agent_task",
        markets=("XAGT_ONCHAIN:solana:token",),
        accounts=("paper_main",),
        files={
            "strategy.yml": """
id: solana_smartmoney_meme
title: Solana Smart Money Meme Alpha
version: 1
markets:
  - XAGT_ONCHAIN:solana:token
accounts:
  - paper_main
mode: paper
execution:
  execution_mode: agent_task
  schedule:
    cron: "*/30 * * * *"
""",
            "main.py": "def run(ctx):\n    return ctx.result.hold(reason='no signal')\n",
        },
    )

    out = StrategyCodeGenerator(WorkspacePaths(root=tmp_path)).generate(
        req,
        validate=True,
        create_proposal_record=False,
    )

    manifest = yaml_io.loads(out.files["strategy.yml"])
    assert out.validation is not None
    assert out.validation.ok
    assert manifest["schedule"] == {
        "cron": "*/30 * * * *",
        "enabled": True,
        "type": "cron",
    }


def test_strategy_generate_request_accepts_agent_task_class_alias() -> None:
    req = _request_from_args({
        "strategy_id": "solana_smartmoney_meme",
        "strategy_class": "agent_task",
        "markets": ["XAGT_ONCHAIN:solana:token"],
        "accounts": ["paper_main"],
    })

    enum = STRATEGY_GENERATE_PROPOSAL_SCHEMA["properties"]["strategy_class"]["enum"]
    assert "agent_task" in enum
    assert req.strategy_class == "agent"
    assert req.execution_mode == "agent_task"


def test_strategy_generator_agent_team_uses_agent_task_runtime(tmp_path):
    req = StrategyGenerationRequest(
        strategy_id="amzn_daily_team_long",
        title="AMZN daily Agent Team long",
        description="Use Agent Team research before AMZN orders.",
        prompt=(
            "Run an Agent Team to analyze AMZN technicals, fundamentals, "
            "macro/news, and risk before buy/sell/hold."
        ),
        strategy_class="trend",
        markets=("yahoo:AMZN",),
        accounts=("paper_main",),
        schedule_cron="0 14 * * 1-5",
        subagents=(
            "technical_analyst",
            "fundamentals_analyst",
            "news_interpreter",
            "risk_critic",
        ),
    )

    out = StrategyCodeGenerator(WorkspacePaths(root=tmp_path)).generate(
        req,
        validate=True,
        create_proposal_record=False,
    )

    manifest = yaml_io.loads(out.files["strategy.yml"])
    assert out.validation is not None
    assert out.validation.ok
    assert manifest["agent_task"] == {"enabled": True, "mode": "agent_team"}
    assert manifest["agent_profile"]["allowed_tools"][:2] == [
        "team_run",
        "role_list",
    ]
    assert "trade_intent_submit" in manifest["agent_profile"]["allowed_tools"]
    assert manifest["policy"]["max_run_seconds"] >= 600
    assert manifest["agent_session"]["policy"] == "per_signal"
    main_py = out.files["main.py"]
    assert "def build_agent_task" in main_py
    assert "StrategyAgentTask.dispatch" in main_py
    assert "team_run" in main_py
    assert "Call team_run first" in main_py
    assert "ctx.subagents.run" not in main_py


def test_strategy_generator_explicit_agent_mode_builds_script_to_agent_task(tmp_path):
    req = StrategyGenerationRequest(
        strategy_id="btc_macd_agent",
        title="BTC MACD Agent",
        description="Script computes indicators, then Agent decides whether to trade.",
        strategy_class="agent",
        execution_mode="agent",
        markets=("mock:BTC/USDT",),
        accounts=("paper_main",),
        schedule_every_seconds=300,
    )

    out = StrategyCodeGenerator(WorkspacePaths(root=tmp_path)).generate(
        req,
        validate=True,
        create_proposal_record=False,
    )

    manifest = yaml_io.loads(out.files["strategy.yml"])
    assert out.validation is not None
    assert out.validation.ok
    assert not any(i.code == "orphan_no_trade_path" for i in out.validation.issues)
    assert manifest["execution_mode"] == "agent"
    assert manifest["agent_task"] == {"enabled": True, "mode": "agent"}
    assert "trade_intent_submit" in manifest["agent_profile"]["allowed_tools"]
    assert "team_run" not in manifest["agent_profile"]["allowed_tools"]
    main_py = out.files["main.py"]
    assert "if getattr(ctx, 'runmode', '') == 'backtest':" in main_py
    assert "backtest_script_signal" in main_py
    assert "def build_agent_task" in main_py
    assert "StrategyAgentTask.dispatch" in main_py
    assert "Recent K-line tail JSON" in main_py
    assert "team_run" not in main_py


def test_strategy_generator_agent_mode_preserves_news_social_context(tmp_path):
    req = StrategyGenerationRequest(
        strategy_id="eth_macd_agent",
        title="ETH MACD News Agent",
        description="Agent reviews technical signal plus recent news/social context.",
        strategy_class="agent",
        execution_mode="agent",
        markets=("binance:ETHUSDT",),
        accounts=("paper_main",),
        news_sources=("crypto", "social"),
    )

    out = StrategyCodeGenerator(WorkspacePaths(root=tmp_path)).generate(
        req,
        validate=False,
        create_proposal_record=False,
    )

    manifest = yaml_io.loads(out.files["strategy.yml"])
    assert "news_social" in manifest["agent_profile"]["attached_skills"]
    main_py = out.files["main.py"]
    assert "news_social" in main_py
    assert "_NEWS_SOCIAL_SOURCES" in main_py
    assert "ctx.news.fetch(sources=_NEWS_SOCIAL_SOURCES" in main_py


def test_strategy_request_infers_news_social_context_from_operator_prompt(tmp_path):
    req = _request_from_args({
        "strategy_id": "eth_macd_news_agent",
        "title": "ETH MACD Agent",
        "strategy_class": "agent",
        "execution_mode": "agent",
        "markets": ["binance:ETHUSDT"],
        "accounts": ["paper_main"],
    })

    inferred = _with_inferred_news_sources(
        req,
        operator_prompt="ETH 1h MACD 金叉触发时先查最近 6 小时新闻和大宗事件再决策",
    )
    out = StrategyCodeGenerator(WorkspacePaths(root=tmp_path)).generate(
        inferred,
        validate=False,
        create_proposal_record=False,
    )

    manifest = yaml_io.loads(out.files["strategy.yml"])
    assert inferred.news_sources == ("crypto",)
    assert "news_social" in manifest["agent_profile"]["attached_skills"]
    assert "news_social" in out.files["main.py"]


def test_strategy_generator_news_sources_add_hook_to_inline_main_override(tmp_path):
    req = StrategyGenerationRequest(
        strategy_id="eth_custom_agent",
        strategy_class="agent",
        execution_mode="agent",
        markets=("binance:ETHUSDT",),
        accounts=("paper_main",),
        news_sources=("crypto",),
        files={
            "main.py": (
                "from nerya.strategies import StrategyContext\n\n"
                "def run(ctx: StrategyContext):\n"
                "    return ctx.result.hold(reason='custom draft')\n"
            ),
        },
    )

    out = StrategyCodeGenerator(WorkspacePaths(root=tmp_path)).generate(
        req,
        validate=False,
        create_proposal_record=False,
    )

    main_py = out.files["main.py"]
    assert "custom draft" in main_py
    assert "news_social: generated audit hook" in main_py
    assert "_NEWS_SOCIAL_SOURCES = [\"crypto\"]" in main_py
    assert "def _news_social_sources" in main_py


def test_strategy_generator_records_confluence_semantic_tag(tmp_path):
    req = StrategyGenerationRequest(
        strategy_id="btc_four_gate_agent",
        title="BTC 四条件 Agent 仲裁",
        description="MACD, RSI, volume, and resistance gates before Agent dispatch.",
        strategy_class="agent",
        execution_mode="agent",
        markets=("binance:BTCUSDT",),
        accounts=("paper_main",),
        files={
            "main.py": (
                "from nerya.strategies import StrategyAgentTask, StrategyContext\n\n"
                "def run(ctx: StrategyContext):\n"
                "    macd_ok = True\n"
                "    rsi_ok = True\n"
                "    volume_ok = True\n"
                "    resistance_clear = True\n"
                "    if not (macd_ok and rsi_ok and volume_ok and resistance_clear):\n"
                "        return StrategyAgentTask.skip('gate failed')\n"
                "    return StrategyAgentTask.dispatch(prompt='review signal')\n"
            ),
        },
    )

    out = StrategyCodeGenerator(WorkspacePaths(root=tmp_path)).generate(
        req,
        validate=False,
        create_proposal_record=True,
    )

    assert out.proposal is not None
    assert "confluence" in (out.proposal.metadata or {}).get("semantic_tags", [])


def test_strategy_generator_records_mtf_semantic_tag(tmp_path):
    req = StrategyGenerationRequest(
        strategy_id="btc_long_agent",
        title="BTC 保守长线 Agent",
        description="1d MACD, 4h RSI, and 1h EMA200 must align before dispatch.",
        strategy_class="agent",
        execution_mode="agent",
        markets=("binance:BTCUSDT",),
        accounts=("paper_main",),
    )

    out = StrategyCodeGenerator(WorkspacePaths(root=tmp_path)).generate(
        req,
        validate=False,
        create_proposal_record=True,
    )

    assert out.proposal is not None
    assert "mtf" in (out.proposal.metadata or {}).get("semantic_tags", [])


def test_strategy_generator_inline_manifest_preserves_agent_execution_defaults(tmp_path):
    req = StrategyGenerationRequest(
        strategy_id="btc_bollinger_agent",
        title="BTC Bollinger Agent",
        description="Custom script detects Bollinger squeeze, then Agent decides.",
        strategy_class="agent",
        execution_mode="agent",
        markets=("binance:BTCUSDT",),
        accounts=("paper",),
        files={
            "strategy.yml": """
strategy_id: btc_bollinger_agent
title: BTC Bollinger Agent
markets:
  - binance:BTCUSDT
accounts:
  - paper
mode: paper
policy:
  default_order_usd: 25
""",
            "main.py": """
from nerya.strategies import StrategyAgentTask, StrategyContext

def run(ctx: StrategyContext):
    return StrategyAgentTask.dispatch(
        prompt="Bollinger squeeze breakout detected; decide whether to chase.",
        metadata={"indicator": "bollinger"},
    )
""",
        },
    )

    out = StrategyCodeGenerator(WorkspacePaths(root=tmp_path)).generate(
        req,
        validate=False,
        create_proposal_record=False,
    )

    manifest = yaml_io.loads(out.files["strategy.yml"])
    assert manifest["execution_mode"] == "agent"
    assert manifest["agent_task"] == {"enabled": True, "mode": "agent"}
    assert manifest["agent_profile"]["allowed_tools"]
    assert manifest["policy"]["allow_direct_order"] is False
    assert manifest["policy"]["default_order_usd"] == 25


def test_strategy_generator_trend_template_uses_ma_crosses(tmp_path):
    req = StrategyGenerationRequest(
        strategy_id="btc_ma_cross",
        strategy_class="trend",
        markets=("mock:BTC/USDT",),
        accounts=("paper_main",),
    )

    out = StrategyCodeGenerator(WorkspacePaths(root=tmp_path)).generate(
        req,
        validate=True,
        create_proposal_record=False,
    )

    main_py = out.files["main.py"]
    assert out.validation is not None
    assert out.validation.ok
    assert "golden_cross" in main_py
    assert "death_cross" in main_py
    # v6 contract: trend template chooses position side (long/short)
    # from the cross, not order side (buy/sell). Entry goes through
    # the open_position pipeline with bracket TP/SL; opposing-cross
    # exit goes through close_position.
    assert 'cross_side = "long"' in main_py
    assert "ctx.trading.open_position" in main_py
    assert "ctx.trading.close_position" in main_py
    assert '"stop_loss"' in main_py
    assert '"take_profit"' in main_py


def test_strategy_generator_scalping_template_has_exit_path(tmp_path):
    req = StrategyGenerationRequest(
        strategy_id="btc_scalp_replay",
        strategy_class="scalping",
        markets=("mock:BTC/USDT",),
        accounts=("paper_main",),
    )

    out = StrategyCodeGenerator(WorkspacePaths(root=tmp_path)).generate(
        req,
        validate=True,
        create_proposal_record=False,
    )

    main_py = out.files["main.py"]
    assert "ctx.portfolio.positions(market)" in main_py
    assert "scalp_exit" in main_py
    # v6 contract: entry ships bracket TP/SL via open_position; the
    # tactical exit goes through close_position so the position side
    # (long/short) is what the SDK sees, not the order side.
    assert "ctx.trading.open_position" in main_py
    assert "ctx.trading.close_position" in main_py
    assert '_STOP_LOSS_PCT' in main_py
    assert '_TAKE_PROFIT_PCT' in main_py
    # The runaway-bug fix: short shares MUST close via side='short',
    # never via a bare sell. Search for the side-mapping idiom.
    assert "'long' if signed > 0 else 'short'" in main_py


def test_strategy_generator_agent_team_has_backtest_fallback(tmp_path):
    req = StrategyGenerationRequest(
        strategy_id="stock_team_replay",
        strategy_class="agent_team",
        execution_mode="agent_team",
        markets=("yahoo:AAPL", "yahoo:MSFT"),
        accounts=("paper_main",),
    )

    out = StrategyCodeGenerator(WorkspacePaths(root=tmp_path)).generate(
        req,
        validate=True,
        create_proposal_record=False,
    )

    main_py = out.files["main.py"]
    assert "if getattr(ctx, 'runmode', '') == 'backtest':" in main_py
    assert "def _backtest_basket_result" in main_py
    assert "backtest_team_rank" in main_py


def test_strategy_promote_requires_backtest_by_default(tmp_path):
    paths = WorkspacePaths(root=tmp_path)
    cfg = Config(paths=paths, data={})
    generated = StrategyCodeGenerator(paths).generate(
        StrategyGenerationRequest(
            strategy_id="needs_backtest_before_promote",
            strategy_class="scalping",
            markets=("mock:BTC/USDT",),
            accounts=("paper_main",),
        ),
        validate=True,
        create_proposal_record=True,
    )

    result = strategy_promote_handler(
        ToolCall(
            name="strategy_promote",
            arguments={"proposal_id": generated.proposal.id},
        ),
        config=cfg,
    )

    data = result.content[0].data
    assert data["ok"] is False
    assert data["reason"] == "backtest_required"
    assert data["next_required_action"]["tool"] == "strategy_backtest"


def test_strategy_promote_flexible_meme_requires_operator_approval_without_replay(tmp_path):
    paths = WorkspacePaths(root=tmp_path)
    cfg = Config(paths=paths, data={})
    generated = StrategyCodeGenerator(paths).generate(
        StrategyGenerationRequest(
            strategy_id="meme_smart_money_gap",
            strategy_class="agent",
            execution_mode="agent",
            prompt="Use on-chain meme smart money wallet-flow data.",
            markets=("OKX_ONCHAIN:solana:Token111111111111111111111111111111111",),
            accounts=("paper_main",),
        ),
        validate=True,
        create_proposal_record=True,
    )

    result = strategy_promote_handler(
        ToolCall(
            name="strategy_promote",
            arguments={
                "proposal_id": generated.proposal.id,
                "backtest_policy": "flexible_meme",
            },
        ),
        config=cfg,
    )

    data = result.content[0].data
    assert data["ok"] is False
    assert data["reason"] == "operator_approval_required_for_backtest_waiver"
    assert data["backtest_status"]["is_meme_or_onchain"] is True
    assert data["next_required_action"]["arguments"]["operator_approved"] is True


def test_strategy_promote_flexible_meme_accepts_custom_replay(tmp_path):
    paths = WorkspacePaths(root=tmp_path)
    cfg = Config(paths=paths, data={})
    generated = StrategyCodeGenerator(paths).generate(
        StrategyGenerationRequest(
            strategy_id="meme_custom_replay",
            strategy_class="agent",
            execution_mode="agent",
            prompt="Use on-chain meme top-trader and swap history data.",
            markets=("OKX_ONCHAIN:solana:Token222222222222222222222222222222222",),
            accounts=("paper_main",),
        ),
        validate=True,
        create_proposal_record=True,
    )
    replay_dir = (
        paths.evolution
        / "proposals"
        / generated.proposal.id
        / "after"
        / "strategies"
        / "meme_custom_replay"
        / "backtests"
    )
    replay_dir.mkdir(parents=True, exist_ok=True)
    (replay_dir / "custom_replay_result.json").write_text(
        json.dumps(
            {
                "ok": True,
                "strategy_id": "meme_custom_replay",
                "replay_kind": "swap_history",
                "window": {"start": "2026-05-01", "end": "2026-05-17"},
                "events_seen": 120,
                "signals": 4,
                "simulated_trades": 2,
                "limitations": ["no full historical orderbook"],
            }
        ),
        encoding="utf-8",
    )

    result = strategy_promote_handler(
        ToolCall(
            name="strategy_promote",
            arguments={
                "proposal_id": generated.proposal.id,
                "backtest_policy": "flexible_meme",
            },
        ),
        config=cfg,
    )

    data = result.content[0].data
    assert data["ok"] is True
    assert data["backtest_status"]["accepted_kind"] == "custom_replay"
    assert data["evidence_record"]["kind"] == "custom_replay"


def test_strategy_promote_flexible_meme_records_operator_waiver(tmp_path):
    paths = WorkspacePaths(root=tmp_path)
    cfg = Config(paths=paths, data={})
    generated = StrategyCodeGenerator(paths).generate(
        StrategyGenerationRequest(
            strategy_id="meme_backtest_waiver",
            strategy_class="agent",
            execution_mode="agent",
            prompt="Use on-chain meme holder and smart money data.",
            markets=("OKX_ONCHAIN:solana:Token333333333333333333333333333333333",),
            accounts=("paper_main",),
        ),
        validate=True,
        create_proposal_record=True,
    )

    result = strategy_promote_handler(
        ToolCall(
            name="strategy_promote",
            arguments={
                "proposal_id": generated.proposal.id,
                "backtest_policy": "flexible_meme",
                "operator_approved": True,
                "approval_note": "Operator accepts no standard OHLCV backtest for this meme token.",
                "operator": "alice",
            },
        ),
        config=cfg,
    )

    data = result.content[0].data
    assert data["ok"] is True
    assert data["backtest_status"]["accepted_kind"] == "backtest_waiver"
    assert data["evidence_record"]["kind"] == "backtest_waiver"
    assert data["evidence_record"]["operator"] == "alice"


def test_http_strategy_generation_defaults_to_tuning_and_accepts_files():
    req = _request_from_payload(
        {
            "strategy_id": "btc_agent_http",
            "strategy_class": "agent",
            "execution_mode": "agent",
            "markets": ["mock:BTC/USDT"],
            "accounts": ["paper_main"],
            "files": {"tests/test_main.py": "def test_ok():\n    assert True\n"},
        }
    )

    assert req.create_tuning is True
    assert req.execution_mode == "agent"
    assert req.files["tests/test_main.py"].startswith("def test_ok")


def test_tuning_snapshot_includes_kline_indicators_and_news_context(tmp_path):
    paths = WorkspacePaths(root=tmp_path)
    cfg = Config(paths=paths, data={"runtime": {"mock_mode": True}})
    req = StrategyGenerationRequest(
        strategy_id="btc_review_context",
        strategy_class="trend",
        markets=("mock:BTC/USDT",),
        accounts=("paper_main",),
    )
    out = StrategyCodeGenerator(paths).generate(
        req,
        validate=True,
        create_proposal_record=False,
    )
    root = paths.strategy(req.strategy_id)
    for rel, body in out.files.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body, encoding="utf-8")

    pkg = load_package(paths, req.strategy_id)
    snap = build_snapshot(paths, req.strategy_id, package=pkg, config_like=cfg)

    item = snap.market_context["items"][0]
    assert item["candles_count"] > 0
    assert item["recent_candles"]
    assert "rsi_14" in item["features"]
    assert snap.news_context["count"] >= 1
    assert snap.news_context["items"][0]["matched_tickers"] == ["BTC"]


def test_tuning_snapshot_uses_equity_prices_and_news_for_stock_baskets(
    monkeypatch,
    tmp_path,
):
    paths = WorkspacePaths(root=tmp_path)
    cfg = Config(paths=paths, data={"runtime": {"mock_mode": False}})
    req = StrategyGenerationRequest(
        strategy_id="stock_basket_review",
        strategy_class="agent_team",
        execution_mode="agent_team",
        markets=("yahoo:AAPL", "yahoo:MSFT"),
        accounts=("paper_main",),
        subagents=("technical_analyst", "fundamentals_analyst", "news_interpreter"),
    )
    out = StrategyCodeGenerator(paths).generate(
        req,
        validate=True,
        create_proposal_record=False,
    )
    root = paths.strategy(req.strategy_id)
    for rel, body in out.files.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body, encoding="utf-8")

    def fake_candles(market, *, count, interval, allow_mock, config_like):
        return [
            {
                "ts": i,
                "open": 100 + i,
                "high": 101 + i,
                "low": 99 + i,
                "close": 100.5 + i,
                "volume": 1_000 + i,
                "_envelope": {"mode": "live", "source": "yahoo"},
            }
            for i in range(count)
        ]

    class FakeEquitiesClient:
        def news(self, ticker: str, *, limit: int = 12):
            return {
                "data": {
                    "news": [
                        {
                            "title": f"{ticker} product cycle update",
                            "summary": "Company-specific equity news.",
                            "published_at": "2026-05-10",
                            "url": f"https://example.test/{ticker.lower()}",
                            "tickers": [ticker],
                        }
                    ]
                },
                "_envelope": {"mode": "live", "source": "financial_datasets"},
            }

    monkeypatch.setattr(performance_mod, "fetch_candles", fake_candles)
    monkeypatch.setattr(
        performance_mod,
        "EquitiesClient",
        lambda: FakeEquitiesClient(),
    )

    snap = build_snapshot(
        paths,
        req.strategy_id,
        package=load_package(paths, req.strategy_id),
        config_like=cfg,
    )

    assert snap.market_context["timeframe"] == "1d"
    assert snap.market_context["items"][0]["candles_count"] == 96
    assert snap.market_context["items"][0]["features"]["rsi_14"] is not None
    assert snap.news_context["symbols"] == ["AAPL", "MSFT"]
    assert snap.news_context["count"] == 2
    assert snap.news_context["items"][0]["matched_tickers"] == ["AAPL"]
    assert "product cycle" in snap.news_context["items"][0]["title"]


def test_equity_news_falls_back_to_yahoo_rss_when_fd_key_missing(monkeypatch, tmp_path):
    cfg = Config(paths=WorkspacePaths(root=tmp_path), data={"runtime": {"mock_mode": False}})

    class FakeEquitiesClient:
        def news(self, ticker: str, *, limit: int = 12):
            return {
                "data": {},
                "_envelope": {
                    "mode": "unavailable",
                    "source": "financial_datasets",
                    "error": "no api key configured",
                },
            }

    class FakeHttp:
        def __init__(self, *args, **kwargs):
            pass

        def request(self, method, url, *, timeout=15.0, **kwargs):
            ticker = "AAPL" if "AAPL" in url else "MSFT"
            return 200, {
                "raw": (
                    "<rss><channel><item>"
                    f"<title>{ticker} supplier update</title>"
                    "<description>Company-specific news.</description>"
                    "<link>https://finance.yahoo.com/news/demo</link>"
                    "<pubDate>Mon, 11 May 2026 10:00:00 GMT</pubDate>"
                    "</item></channel></rss>"
                )
            }

    monkeypatch.setattr(performance_mod, "EquitiesClient", lambda: FakeEquitiesClient())
    monkeypatch.setattr(performance_mod, "UrllibHttp", FakeHttp)

    context = performance_mod._build_equity_news_context(
        ["AAPL", "MSFT"],
        config_like=cfg,
    )

    assert context["count"] == 2
    assert {item["matched_tickers"][0] for item in context["items"]} == {"AAPL", "MSFT"}
    assert context["items"][0]["source"] == "yahoo_finance_rss"
    assert context["items"][0]["_envelope"]["mode"] == "live"


def test_strategy_generate_tool_and_handler_share_tuning_default():
    """Guard against schema/default drift without importing native bootstrap."""

    root = Path(__file__).resolve().parents[1]
    source = (root / "nerya" / "tools" / "native" / "strategy_runtime.py").read_text(
        encoding="utf-8",
    )

    assert '"default": True' in source
    assert 'create_tuning=bool(args.get("create_tuning", True))' in source
