"""End-to-end verification: generate three strategy classes through the
SDK (the same code path POST /strategies/runtime/generate hits), promote
them, then re-read the generated package files and assert they contain
the v6 hardening (open_position / close_position / bracket TP-SL,
side-aware exit, K-line + indicator + news context for the Agent
prompts, and a usable tuning hook).

Run:
    cd Nerya && python scripts/verify_v6_strategy_generation.py
"""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

# Make sure the in-tree package wins over any installed copy.
HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))

from nerya.core.config import load_config  # noqa: E402
from nerya.evolution.strategy_code_generator import StrategyGenerationRequest  # noqa: E402
from nerya.sdk import InternalClient  # noqa: E402


# ----- The three target prompts -------------------------------------------------

PURE_SCALP = StrategyGenerationRequest(
    strategy_id="v6_scalp_btc_1m",
    title="V6-验证: 纯脚本 BTC 1m 剥头皮",
    description="纯脚本剥头皮策略：1分钟 BTC，2/8 EMA 金叉做多，死叉平多；带 0.5% 止损 / 0.8% 止盈 bracket。",
    prompt=(
        "用 1 分钟 K 线在 binance:BTCUSDT 上做剥头皮：EMA(2)/EMA(8) 金叉做多、死叉平多，"
        "每次开仓固定 50 USDT，必须挂止损 0.5%、止盈 0.8% 的 bracket，"
        "默认 paper 模式，账号 paper_main。"
    ),
    strategy_class="scalping",
    mode="paper",
    markets=("binance:BTCUSDT",),
    accounts=("paper_main",),
    schedule_every_seconds=60,
    create_tuning=True,
    tuning_prompt="每 6 小时根据真实成交、近 200 根 1m K 线、最近新闻，回顾胜率/盈亏比，调整 EMA 周期或 TP/SL 阈值。",
    tuning_objectives=("提高胜率", "提高盈亏比"),
)

SCRIPT_THEN_AGENT = StrategyGenerationRequest(
    strategy_id="v6_macross_then_agent",
    title="V6-验证: 脚本金叉 → Agent 决策",
    description="脚本检测到 EMA 金叉/死叉信号后，触发 Agent 综合 K线、指标 (RSI/ATR)、新闻 来决定是否开/平仓，并附 1% 止损 / 2% 止盈。",
    prompt=(
        "在 binance:BTCUSDT 15 分钟 K 线上检测 EMA(20) / EMA(50) 金叉死叉。"
        "出现信号时让 Agent 拉取最近 100 根 K 线、RSI/ATR 指标和最近 24 小时关于 BTC 的新闻，"
        "综合判断是否真的应该开多/开空，给出 open_position trade_intent (附带 1% stop_loss + 2% take_profit bracket)；"
        "已有反向仓位时优先 close_position 平仓。账号 paper_main，paper 模式。"
    ),
    strategy_class="agent",
    mode="paper",
    markets=("binance:BTCUSDT",),
    accounts=("paper_main",),
    schedule_every_seconds=900,
    news_sources=("crypto",),
    create_tuning=True,
    tuning_prompt="每 6 小时根据真实成交、近 200 根 15m K 线、当前指标、最近新闻，复盘 Agent 判断质量，调整其 system prompt 或开仓阈值。",
    tuning_objectives=("提高 Agent 判断准确率", "降低假信号成本"),
)

AGENT_TEAM_BASKET = StrategyGenerationRequest(
    strategy_id="v6_team_us_basket_daily",
    title="V6-验证: 美股篮子 Agent Team 日检",
    description="每个交易日 10 点定时让 Agent Team 分析 AAPL/MSFT/NVDA/AMZN 四只股票，挑出最佳标的开多 (5% 止盈 / 2% 止损 bracket)，已有持仓但被排除则平仓。",
    prompt=(
        "组建一个 Agent Team，每个交易日 10:00 定时分析 yahoo:AAPL、yahoo:MSFT、yahoo:NVDA、yahoo:AMZN 四个标的，"
        "拉取每只 60 个交易日 K 线、SMA(20)/SMA(50)/RSI(14) 指标，以及关于该公司最近 3 天的新闻。"
        "由 Team 评分选出至多 1 只最优标的开多（账号 paper_main，固定 1000 USD，附 2% 止损 + 5% 止盈 bracket），"
        "对当前持有但被 Team 剔除的标的做 close_position 平仓。"
    ),
    strategy_class="agent_team",
    mode="paper",
    markets=("yahoo:AAPL", "yahoo:MSFT", "yahoo:NVDA", "yahoo:AMZN"),
    accounts=("paper_main",),
    schedule_cron="0 10 * * 1-5",
    news_sources=("equity",),
    create_tuning=True,
    tuning_prompt="每周日 21:00 复盘上周 Team 选股的胜率、平均收益、最大回撤，结合每只股最近 K 线/指标/新闻，调整入选权重或评分模板。",
    tuning_objectives=("提高 Team 选股胜率", "降低换仓频率"),
)


# ----- Per-strategy verification rubric ----------------------------------------

def _read(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return ""


def _verify_main(main_py: str, *, want_open: bool, want_close: bool, want_bracket: bool) -> dict:
    checks = {
        "has_open_position": "open_position(" in main_py,
        "has_close_position": "close_position(" in main_py,
        "has_stop_loss": "stop_loss" in main_py.lower(),
        "has_take_profit": "take_profit" in main_py.lower(),
        "has_protection": "'protection'" in main_py or "\"protection\"" in main_py or "protection=" in main_py,
        "uses_signed_size": "signed" in main_py and ("signed > 0" in main_py or "signed < 0" in main_py or "signed_size" in main_py),
        "lines": main_py.count("\n") + 1,
    }
    expected = {
        "has_open_position": want_open,
        "has_close_position": want_close,
        "has_stop_loss": want_bracket,
        "has_take_profit": want_bracket,
        "has_protection": want_bracket,
    }
    failures = [k for k, v in expected.items() if checks.get(k) is not v]
    checks["passing"] = not failures
    checks["failures"] = failures
    return checks


def _verify_agent_prompt(prompt: str, *, want_kline: bool, want_indicator: bool, want_news: bool, want_team: bool) -> dict:
    """Scan an Agent prompt (which may live inside main.py for agent /
    agent_team templates, since build_agent_task() builds the prompt at
    runtime) for the v6-required hooks.
    """
    text = prompt.lower()
    checks = {
        "len_chars": len(prompt),
        "mentions_kline": any(
            t in text
            for t in ("k线", "k-line", "candle", "ohlc", "k 线", "candles", "k_line", "ctx.market.candles")
        ),
        "mentions_indicator": any(
            t in text
            for t in ("rsi", "macd", "atr", "ema", "sma", "indicator", "指标", "features", "ctx.market.features")
        ),
        "mentions_news": any(t in text for t in ("news", "新闻", "ctx.news.fetch", "news_interpreter")),
        "mentions_team": any(t in text for t in ("team", "agent_team", "篮子", "basket", "subagent", "team_run", "_team_roles")),
        "mentions_protection": any(t in text for t in ("stop_loss", "take_profit", "protection", "止损", "止盈")),
        "mentions_trade_intent": "trade_intent_submit" in text,
        "mentions_close_position": "close_position" in text,
    }
    expected = {
        "mentions_kline": want_kline,
        "mentions_indicator": want_indicator,
        "mentions_news": want_news,
        "mentions_team": want_team,
        "mentions_trade_intent": True,  # All Agent strategies must call this tool
        "mentions_protection": True,    # All Agent strategies must know about bracket
    }
    failures = [k for k, v in expected.items() if checks.get(k) is not v]
    checks["passing"] = not failures
    checks["failures"] = failures
    return checks


def _verify_tuning(prompt: str) -> dict:
    text = prompt.lower()
    checks = {
        "len_chars": len(prompt),
        "mentions_market_context": any(
            t in text for t in (
                "market_context",
                "k线",
                "candle",
                "k-line",
                "k 线",
                "k_line",
                "ohlc",
                "indicator",
                "指标",
            )
        ),
        "mentions_news_context": "news_context" in text or "新闻" in text or "news" in text,
        "mentions_performance": "performance" in text or "复盘" in text or "胜率" in text or "win" in text,
    }
    failures = [
        k for k, v in checks.items()
        if k.startswith("mentions_") and not v
    ]
    checks["passing"] = not failures
    checks["failures"] = failures
    return checks


def main() -> int:
    print("=" * 80)
    print("V6 strategy generation E2E verification")
    print("=" * 80)

    config = load_config()
    client = InternalClient.from_config(config)
    paths = config.paths

    targets = [
        ("scalp", PURE_SCALP, dict(want_open=True, want_close=True, want_bracket=True)),
        ("script_to_agent", SCRIPT_THEN_AGENT, dict(want_open=True, want_close=True, want_bracket=True)),
        ("agent_team", AGENT_TEAM_BASKET, dict(want_open=True, want_close=True, want_bracket=True)),
    ]

    report: dict[str, dict] = {}

    for label, request, main_expect in targets:
        print(f"\n--- [{label}] {request.strategy_id} ({request.strategy_class}) ---")

        # 1) Generate proposal (this is exactly what /strategies/runtime/generate does)
        try:
            gen_t0 = time.time()
            gen_result = client.strategy.generate_proposal(request, validate=True)
            print(f"  generate_proposal: {time.time()-gen_t0:.2f}s, "
                  f"proposal_id={gen_result.get('proposal_id')}, "
                  f"file_count={len(gen_result.get('files') or {})}")
        except Exception as exc:
            print(f"  ! generate_proposal FAILED: {type(exc).__name__}: {exc}")
            report[label] = {"ok": False, "stage": "generate", "error": f"{type(exc).__name__}: {exc}"}
            continue

        proposal_id = gen_result.get("proposal_id")

        # 2) Promote (apply to ~/.nerya/strategies/<sid>/)
        try:
            prom_t0 = time.time()
            prom_result = client.strategy.promote(proposal_id, note="v6 e2e verify")
            print(f"  promote:           {time.time()-prom_t0:.2f}s, "
                  f"strategy_id={prom_result.get('strategy_id')}")
        except Exception as exc:
            print(f"  ! promote FAILED: {type(exc).__name__}: {exc}")
            report[label] = {
                "ok": False, "stage": "promote",
                "error": f"{type(exc).__name__}: {exc}",
                "proposal_id": proposal_id,
            }
            continue

        # 3) Read package files from disk and run verification rubric
        sid = prom_result.get("strategy_id") or request.strategy_id
        sdir = Path(paths.strategies) / sid
        main_py = _read(sdir / "main.py")
        manifest = _read(sdir / "strategy.yml") or _read(sdir / "manifest.json")
        # For agent/agent_team templates the runtime Agent prompt is
        # assembled inside main.py's ``build_agent_task`` (string
        # template) and bundled with subagents/*.agent.md role briefs
        # (only present for agent_team). We scan both.
        subagent_prompt = ""
        for sub_md in (sdir / "subagents").glob("*.agent.md"):
            if "tuner" in sub_md.name:
                continue
            subagent_prompt += _read(sub_md) + "\n\n"
        agent_prompt_combined = main_py + "\n\n" + subagent_prompt
        tuner_prompt = _read(sdir / "subagents" / "strategy_tuner.agent.md")

        is_agent = request.strategy_class in ("agent", "agent_team")
        main_checks = _verify_main(main_py, **main_expect)
        agent_checks = _verify_agent_prompt(
            agent_prompt_combined,
            want_kline=is_agent,
            want_indicator=is_agent,
            want_news=is_agent,
            want_team=request.strategy_class == "agent_team",
        ) if is_agent else None
        tuner_checks = _verify_tuning(tuner_prompt)

        item_ok = main_checks["passing"] and tuner_checks["passing"] and (
            agent_checks is None or agent_checks["passing"]
        )

        print(f"  files: main.py={len(main_py)} chars, "
              f"subagent_briefs={len(subagent_prompt)} chars, "
              f"tuner_prompt={len(tuner_prompt)} chars")
        print(f"  main.py checks: {main_checks}")
        if agent_checks:
            print(f"  agent prompt checks: {agent_checks}")
        print(f"  tuner prompt checks: {tuner_checks}")

        report[label] = {
            "ok": item_ok,
            "strategy_id": sid,
            "main": main_checks,
            "agent": agent_checks,
            "tuner": tuner_checks,
            "files_on_disk": sorted(p.relative_to(sdir).as_posix() for p in sdir.rglob("*") if p.is_file()),
        }

    # ----- summary ----------------------------------------------------------
    print("\n" + "=" * 80)
    print("SUMMARY")
    print("=" * 80)
    overall = True
    for label, item in report.items():
        ok = item.get("ok")
        overall &= bool(ok)
        marker = "PASS" if ok else "FAIL"
        print(f"  {marker:<4}  {label:<20}  strategy_id={item.get('strategy_id') or '?'}")
        if not ok:
            for sub in ("main", "agent", "tuner"):
                v = item.get(sub) or {}
                if v.get("failures"):
                    print(f"        {sub} fails: {v['failures']}")
            if "error" in item:
                print(f"        error: {item['error']}")

    out_path = Path(os.path.expanduser("~/.nerya/journals/v6_e2e_strategy_verify.json"))
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(report, indent=2, ensure_ascii=False))
    print(f"\nReport written: {out_path}")
    return 0 if overall else 1


if __name__ == "__main__":
    sys.exit(main())
