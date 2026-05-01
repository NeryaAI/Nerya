#!/usr/bin/env python3
"""Deep conversational smoke test against a running Nerya service.

Runs the seven scripted prompts the user asked for through a single
session_id (so the agent remembers context across turns) — half in
English, half in Mandarin. Prints a turn-by-turn summary and a final
hallucination/expectation report based on what actually showed up
in the trade/strategies/journals layer.

Usage:
    python tools/smoke_chat_test.py [--port 18317] [--workspace ~/.nerya]
"""
from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.request
from pathlib import Path
from typing import Any


PROMPTS: list[dict[str, str]] = [
    {"id": "p1_market_overview_en", "lang": "en",
     "text": "Give me a quick overall market overview — BTC, ETH, "
             "and the broader risk-on / risk-off mood for the next 24h."},
    {"id": "p2_scalping_strategy_zh", "lang": "zh",
     "text": "请帮我创建一个新的剥头皮（scalping）策略，标的是 BINANCE:BTCUSDT，"
             "止盈 0.4%，止损 0.25%，单笔仓位 200 USDT，使用 paper_main 账户，"
             "策略 ID 命名为 smoke_btc_scalper。"},
    {"id": "p3_trend_strategy_en", "lang": "en",
     "text": "Now create a separate trend-following strategy on the same "
             "paper_main account, market BINANCE:ETHUSDT, 4h timeframe, "
             "with a 200-period SMA filter and ATR-based trailing stop. "
             "Strategy id: smoke_eth_trend."},
    {"id": "p4_polymarket_strategy_zh", "lang": "zh",
     "text": "再帮我创建一个 polymarket 策略，市场名 POLYMARKET:ELECTION_2028，"
             "属于事件驱动，仓位上限 50 USDC，使用 paper_main 账户，"
             "策略 ID 取名 smoke_polymarket_event。"},
    {"id": "p5_akshare_module_en", "lang": "en",
     "text": "I want a small Python module under tools/ that pulls daily "
             "A-share data via akshare (use ak.stock_zh_a_hist) and exposes "
             "a fetch_daily(symbol, start, end) function. Then create a "
             "simple A-share paper-trading strategy on paper_main called "
             "smoke_ashare_paper that uses MA20 mean-reversion on 600519."},
    {"id": "p6_explain_principles_zh", "lang": "zh",
     "text": "请用中文，逐一解释你刚才创建的四个策略（剥头皮、趋势、polymarket、A 股 MA20）"
             "背后的原理，分别说明为什么这些参数对它们的目标 alpha 有效，并指出每个策略最大的失效场景。"},
    {"id": "p7_team_research_en", "lang": "en",
     "text": "Spin up an agent team to do a deep research pass on Nvidia "
             "(NVDA) — fundamentals, AI/datacenter demand, competitive moat, "
             "supply chain risk, and a trade idea for the next 4 weeks. "
             "Use as many subagents as needed and summarize the consensus."},
]


def post(url: str, body: dict, timeout: float = 240.0) -> dict[str, Any]:
    req = urllib.request.Request(
        url,
        data=json.dumps(body).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def get(url: str, timeout: float = 30.0) -> dict[str, Any]:
    with urllib.request.urlopen(url, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def run_turn(base: str, session_id: str, prompt: dict[str, str]) -> dict[str, Any]:
    body = {
        "session_id": session_id,
        "trigger": {
            "source": "smoke_chat_test",
            "kind": "manual",
            "id": f"evt_{prompt['id']}",
            "payload": {"text": prompt["text"], "lang": prompt["lang"]},
        },
    }
    t0 = time.time()
    try:
        resp = post(f"{base}/agent/run_turn", body, timeout=240)
        dt = round(time.time() - t0, 1)
        return {"ok": True, "elapsed_s": dt, "resp": resp}
    except Exception as exc:  # noqa: BLE001
        dt = round(time.time() - t0, 1)
        return {"ok": False, "elapsed_s": dt, "error": f"{type(exc).__name__}: {exc}"}


def summarise_turn(prompt: dict[str, str], outcome: dict[str, Any]) -> str:
    if not outcome["ok"]:
        return f"  ❌ ERROR ({outcome['elapsed_s']}s): {outcome['error']}"
    r = outcome["resp"]
    decision = r.get("decision", {})
    plan = r.get("plan", {})
    actions = r.get("actions", []) or []
    subagents = r.get("subagents", {}) or {}
    reply = r.get("reply_text", "") or ""
    reply_short = (reply[:160] + "…") if len(reply) > 160 else reply
    bits = [
        f"  ✓ {outcome['elapsed_s']}s · plan={plan.get('kind')}/{plan.get('tier')} "
        f"· decision={decision.get('action')} · actions={len(actions)} "
        f"· subagents={list(subagents.keys())}",
        f"    turn_id={r.get('turn_id')}  stopped={r.get('stopped_reason')}",
        f"    reply: {reply_short!r}",
    ]
    for a in actions[:8]:
        name = a.get("action")
        if a.get("error"):
            # Surface action errors loudly — they used to hide behind an
            # optimistic ``send_message`` reply and look like success.
            err = str(a.get("error"))[:160]
            kind = a.get("error_kind") or "?"
            bits.append(f"      - {name} ✗ [{kind}] {err}")
            continue
        result = a.get("result") if isinstance(a.get("result"), dict) else {}
        bits.append(f"      - {name}: {json.dumps(result, default=str)[:140]}")
    return "\n".join(bits)


def post_audit(base: str, ws: Path) -> str:
    """Look at the file-system after the conversation completes."""
    lines: list[str] = []
    strat_dir = ws / "strategies"
    found = sorted(p.name for p in strat_dir.iterdir()) if strat_dir.exists() else []
    lines.append(f"strategies on disk: {found}")
    expected = {"smoke_btc_scalper", "smoke_eth_trend",
                "smoke_polymarket_event", "smoke_ashare_paper"}
    missing = sorted(expected - set(found))
    extra = sorted(set(found) - expected - {"manual_agent"})
    lines.append(f"  missing strategies: {missing}")
    lines.append(f"  extra/unexpected:    {extra}")
    # The agent saves authored code under the workspace ``scripts/``
    # tree (operator scripts) — not the repo's ``tools/`` directory.
    # Look for any python file mentioning ``stock_zh_a_hist`` so we
    # detect the akshare module regardless of the file name the LLM
    # picked.
    scripts_dir = ws / "scripts"
    akshare_hits: list[str] = []
    if scripts_dir.exists():
        for path in scripts_dir.rglob("*.py"):
            try:
                txt = path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            if "stock_zh_a_hist" in txt or "akshare" in txt:
                akshare_hits.append(
                    str(path.relative_to(ws)).replace("\\", "/")
                )
    lines.append(
        "akshare module(s) under scripts/: "
        f"{akshare_hits if akshare_hits else 'none'}"
    )
    journal_lines = 0
    j = ws / "journals" / "agent.jsonl"
    if j.exists():
        with j.open("r", encoding="utf-8", errors="replace") as f:
            journal_lines = sum(1 for _ in f)
    lines.append(f"agent journal lines: {journal_lines}")
    return "\n".join(lines)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=18317)
    ap.add_argument("--workspace", default=str(Path.home() / ".nerya"))
    ap.add_argument("--session-id", default="smoke_session_2026_04_26")
    ap.add_argument("--out", default="-")
    args = ap.parse_args()

    base = f"http://{args.host}:{args.port}"
    ws = Path(args.workspace).expanduser()
    out_file = sys.stdout if args.out == "-" else open(args.out, "w", encoding="utf-8")

    print(f"=== Nerya deep chat smoke ===  base={base}  ws={ws}", file=out_file)
    print(f"session_id={args.session_id}", file=out_file)
    try:
        h = get(f"{base}/health", timeout=5)
        print(f"health: {h}", file=out_file)
    except Exception as exc:
        print(f"health check failed: {exc}", file=out_file)
        return 2

    results: list[dict[str, Any]] = []
    for i, prompt in enumerate(PROMPTS, 1):
        print(f"\n--- [{i}/{len(PROMPTS)}] {prompt['id']} ({prompt['lang']}) ---",
              file=out_file)
        print(f"prompt: {prompt['text']}", file=out_file)
        outcome = run_turn(base, args.session_id, prompt)
        print(summarise_turn(prompt, outcome), file=out_file)
        results.append({"prompt": prompt, "outcome": outcome})
        if outcome.get("ok"):
            time.sleep(0.5)

    print("\n=== Post-conversation audit ===", file=out_file)
    print(post_audit(base, ws), file=out_file)

    failed = [r for r in results if not r["outcome"]["ok"]]
    print(f"\n=== Done. {len(results) - len(failed)}/{len(results)} prompts ok. ===",
          file=out_file)
    if out_file is not sys.stdout:
        out_file.close()
    return 0 if not failed else 1


if __name__ == "__main__":
    raise SystemExit(main())
