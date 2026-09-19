"""Opt-in real-model authoring check; not a network-free unit test.

Send ordinary user requests through the same public run_turn handler as the
API. Only the main Agent may author proposal files. This script does not
create templates, patch strategies, promote, trade, or start a scheduler.
"""
from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import json
import re
import time
import uuid

from nerya.api.routes_agent import routes
from nerya.core.config import load_config
from nerya.sdk import InternalClient

PROMPTS = {
    "script": "帮我建一个比特币观察策略：每5分钟用脚本检查15分钟K线，收盘价站上20周期均线时记录提醒。整个过程不要用AI，也不要下单。",
    "macd_agent": "帮我盯一下比特币：脚本每5分钟检查15分钟K线，MACD金叉时才请AI分析一下机会和风险，没金叉就别打扰AI。同一根K线别重复分析，不要下单。",
    "scheduled_agent": "帮我建一个定时观察策略：每天北京时间早上9点让AI看看比特币行情，给我一段机会和风险总结。不用脚本先筛选，也不要下单。",
}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workspace", required=True, type=Path)
    parser.add_argument("--out", required=True, type=Path)
    parser.add_argument("--allow-real-model", action="store_true")
    parser.add_argument("--case", choices=list(PROMPTS), action="append")
    parser.add_argument("--dashboard-settings", action="store_true", help="Use the current dashboard's checked-in default run limits, not bare API defaults")
    args = parser.parse_args()
    if not args.allow_real_model:
        parser.error("This opt-in check spends model tokens; pass --allow-real-model explicitly.")
    cfg = load_config(args.workspace)
    if cfg.live_trading_enabled():
        parser.error("Refusing to use a Workspace with live trading enabled.")
    tier = cfg.get("llm.default_tier", "medium")
    if cfg.get(f"llm.tiers.{tier}.provider", "mock") == "mock":
        parser.error("A real configured main-Agent model is required, not a mock.")
    run_settings = {}
    if args.dashboard_settings:
        source = (Path(__file__).resolve().parents[1] / "dashboard/lib/chat.ts").read_text()
        block = re.search(r"export const DEFAULT_CHAT_RUN_SETTINGS[^=]*= \{(.*?)\n\};", source, re.S)
        if not block:
            parser.error("Cannot locate current dashboard defaults; refusing to guess")
        for key in ("max_iterations", "max_total_tool_calls", "max_wall_seconds", "model_context_window"):
            value = re.search(r"\b" + key + r":\s*(\d+)\s*,", block[1])
            if not value:
                parser.error(f"Dashboard setting {key} is not a literal integer")
            run_settings[key] = int(value[1])
    args.out.mkdir(parents=True, exist_ok=True)
    (args.out / "run-settings.json").write_text(json.dumps({"source": "dashboard/lib/chat.ts" if args.dashboard_settings else "Workspace API defaults", **run_settings}, indent=2))
    handler = next(fn for method, path, fn in routes() if method == "POST" and path == "/agent/run_turn")

    def run(key):
        request_file = args.out / f"{key}-request.json"
        if request_file.exists():
            raise RuntimeError(f"{key}: request already recorded; inspect that session instead of resending")
        body = {**run_settings, "source": "user_chat", "kind": "user.chat", "target": "main", "session_id": str(uuid.uuid4()),
                "payload": {"text": PROMPTS[key], "channel": "dashboard"}}
        with request_file.open("x", encoding="utf-8") as stream:
            json.dump(body, stream, ensure_ascii=False, indent=2)
        print(json.dumps({"case": key, "session_id": body["session_id"], "state": "started"}), flush=True)
        start = time.monotonic()
        client = InternalClient.from_config(cfg)
        try:
            response = handler(client, body)
            record = {"request": body, "elapsed_s": round(time.monotonic() - start, 2),
                      "result": {k: response.get(k) for k in ("turn_id", "session_id", "final_text", "actions", "tool_trace", "budget", "stopped_reason", "execution_state", "error", "_status")}}
        except Exception as exc:
            record = {"request": body, "state": "unknown_do_not_resend", "error": f"{type(exc).__name__}: {exc}"}
        (args.out / f"{key}-result.json").write_text(json.dumps(record, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
        result = record.get("result", {})
        print(json.dumps({"case": key, "turn_id": result.get("turn_id"), "stop": result.get("stopped_reason"), "budget": result.get("budget"), "elapsed_s": record.get("elapsed_s"), "error": record.get("error")}), flush=True)

    with ThreadPoolExecutor(max_workers=3) as pool:
        list(pool.map(run, args.case or list(PROMPTS)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
