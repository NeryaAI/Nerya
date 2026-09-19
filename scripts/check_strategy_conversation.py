"""Opt-in real main-Agent conversation acceptance (network and model costs).

This harness sends requests to the public run_turn handler. It never authors
strategy code, manufactures replay results, changes permissions, or activates
candidates. Every retry is a separate recorded conversation, not a hidden retry.
"""
from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
from pathlib import Path
import re
import time
import uuid

from nerya.api.routes_agent import routes
from nerya.core.config import load_config
from nerya.sdk import InternalClient

PROMPTS = {
    "paper_trade": "帮我创建一个比特币双均线策略：用1小时K线，20均线上穿50均线买入、下穿卖出，每次模拟投入100美元，不加杠杆。先用最近30天真实历史数据回测，检查能否运行，有问题就修好，给我结果。不要启用自动调度，也不要向账户下单，只允许回测里的模拟成交。",
    "signal_agent": "帮我创建一个比特币观察策略：脚本每5分钟检查15分钟K线，MACD金叉时才请AI分析机会和风险；没金叉就不要调用AI，同一根K线不要重复分析。请真正验证有信号、无信号和重复信号这些情况，再用真实历史数据回放，有错误直接修复。不要下单，也不要启用自动调度。",
    "scheduled_agent": "帮我创建一个每天北京时间早上9点看比特币行情的策略，让AI输出机会和风险总结，不要先用脚本筛选。创建后检查代码，并实际验证能运行，不要只是给我一个方案。只观察，不下单，暂时不要启用自动调度。",
}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workspace", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--case", choices=list(PROMPTS), action="append")
    parser.add_argument("--continue-from", type=Path)
    parser.add_argument("--message", type=str)
    parser.add_argument("--allow-real-model", action="store_true")
    args = parser.parse_args()
    if not args.allow_real_model:
        parser.error("Real model calls spend tokens. Pass --allow-real-model to opt in.")
    cfg = load_config(args.workspace)
    tier = cfg.get("llm.default_tier", "medium")
    provider = cfg.get(f"llm.tiers.{tier}.provider", "mock")
    if cfg.live_trading_enabled() or cfg.get("runtime.mock_mode") or provider == "mock":
        parser.error("Requires a real model/data configuration with live trading disabled.")
    base = Path(__file__).resolve().parents[1]
    source = (base / "dashboard/lib/chat.ts").read_text()
    defaults = re.search(r"export const DEFAULT_CHAT_RUN_SETTINGS[^=]*= \{(.*?)\n\};", source, re.S)
    if not defaults:
        raise ValueError("Cannot find actual dashboard defaults")
    limits = {}
    for key in ("max_iterations", "max_total_tool_calls", "max_wall_seconds", "model_context_window"):
        match = re.search(r"\b" + key + r":\s*(\d+)\s*,", defaults[1])
        if not match:
            raise ValueError(f"Cannot read dashboard limit {key}")
        limits[key] = int(match[1])
    args.out.mkdir(parents=True, exist_ok=True)
    skill = base / "nerya/skills/builtin/strategy_author/SKILL.md"
    meta = {"provider": provider, "model": cfg.get(f"llm.tiers.{tier}.model"),
            "workspace": str(cfg.paths.root), "permission_mode": cfg.get("runtime.permission_mode"),
            "limits": limits, "skill_sha256": hashlib.sha256(skill.read_bytes()).hexdigest(),
            "scope": "Actual main Agent and tools; no mocked model, data, strategy or result."}
    (args.out / "run-settings.json").write_text(json.dumps(meta, ensure_ascii=False, indent=2))
    handler = next(fn for method, path, fn in routes() if (method, path) == ("POST", "/agent/run_turn"))

    def run(key: str) -> bool:
        prior = json.loads((args.continue_from / f"{key}-request.json").read_text()) if args.continue_from else None
        if prior and not args.message:
            raise ValueError("A continuation must record its actual user feedback")
        body = {**limits, "source": "user_chat", "kind": "user.chat", "target": "main",
                "session_id": prior["session_id"] if prior else str(uuid.uuid4()),
                "payload": {"text": args.message or PROMPTS[key], "channel": "dashboard"}}
        with (args.out / f"{key}-request.json").open("x", encoding="utf-8") as stream:
            json.dump(body, stream, ensure_ascii=False, indent=2)
        print(json.dumps({"case": key, "session_id": body["session_id"], "state": "started"}), flush=True)
        client = InternalClient.from_config(cfg)
        started = time.monotonic()
        try:
            response = handler(client, body)
            keys = ("turn_id", "session_id", "final_text", "actions", "tool_trace", "budget", "stopped_reason", "execution_state", "error", "_status")
            record = {"request": body, "elapsed_s": round(time.monotonic() - started, 2), "result": {k: response.get(k) for k in keys}}
            ok = bool(response.get("final_text")) and not response.get("error")
        except Exception as exc:
            record = {"request": body, "elapsed_s": round(time.monotonic() - started, 2),
                      "state": "unknown_do_not_resend", "error": f"{type(exc).__name__}: {exc}"}
            ok = False
        (args.out / f"{key}-result.json").write_text(json.dumps(record, ensure_ascii=False, indent=2, default=str))
        result = record.get("result", {})
        print(json.dumps({"case": key, "turn_id": result.get("turn_id"), "elapsed_s": record["elapsed_s"],
                          "stop": result.get("stopped_reason"), "error": record.get("error") or result.get("error"),
                          "budget": result.get("budget")}, ensure_ascii=False), flush=True)
        return ok

    with ThreadPoolExecutor(max_workers=3) as pool:
        results = list(pool.map(run, args.case or list(PROMPTS)))
    # Process success is not strategy success. Inspect tool receipts/artifacts.
    return 0 if all(results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
