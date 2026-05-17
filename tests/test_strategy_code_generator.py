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
from nerya.tools.native.strategy_runtime import strategy_promote_handler
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
