"""Controlled UI acceptance only; never a market backtest or real model result.

Copies previously Prompt-authored candidates without changing their code. Uses
an injected test kernel through the real strategy task executor. The UI displays
an explicit fixture label in every generated response and reason.
"""
from __future__ import annotations

import json
import os
import shutil
import threading
import time
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace

from nerya.agent.streaming import get_default_bus
from nerya.api.local_server import build_server
from nerya.core import yaml_io
from nerya.core.config import Config, DEFAULT_CONFIG
from nerya.core.paths import WorkspacePaths
from nerya.sdk.strategy_api import StrategyAPI
from nerya.strategies.package import load_package
from nerya.triggers.runtime import TriggerRuntime
from nerya.triggers.strategy_agent_task_executor import StrategyAgentTaskExecutor, TARGET

BASE = Path(__file__).resolve().parents[1]
run_name = os.environ.get("NERYA_USABILITY_RUN", "workflow-usability")
assert run_name.startswith("workflow-usability") and "/" not in run_name and "\\" not in run_name and ".." not in run_name
OUT = BASE / "dashboard/test-results" / run_name
ROOT = OUT / "workspace"
SOURCE = BASE / "dashboard/test-results/autonomy-ui/delivery/candidates"
OUT.mkdir(parents=True, exist_ok=True)
ROOT.mkdir(exist_ok=True)
paths = WorkspacePaths(ROOT)
cfg = Config(paths=paths, data=deepcopy(DEFAULT_CONFIG))
cfg.data["runtime"]["mock_mode"] = False
cfg.data["dashboard"]["port"] = 18381
# Test-only account: no credentials or order permissions.
yaml_io.dump(paths.accounts_file, {"accounts": [{"id": "binance_paper", "venue": "binance", "exchange": "binance", "mode": "paper", "status": "active", "initial_balance_usd": 0, "permissions": {"read_balances": True, "place_order": False, "cancel_order": False}}]})
cases = []
for name in ("script", "macd_agent", "scheduled_agent"):
    src = SOURCE / name
    manifest = yaml_io.load(src / "strategy.yml")
    assert manifest["mode"] == "paper" and manifest["schedule"]["enabled"] is False
    assert manifest["policy"]["allow_direct_order"] is False
    sid = manifest["strategy_id"]
    dest = paths.strategy(sid)
    if not dest.exists():
        shutil.copytree(src, dest, ignore=shutil.ignore_patterns("workflow-view.json", "__pycache__"))
    package = load_package(paths, sid)
    assert (dest / "main.py").read_bytes() == (src / "main.py").read_bytes()
    cases.append({"case": name, "strategy_id": sid, "package_hash": package.content_hash})

api = StrategyAPI(config=cfg, skills=None)
bus = get_default_bus()
class ControlledKernel:
    def __init__(self, fail=False):
        self.fail = fail

    def run_turn(self, **kwargs):
        common = {"turn_id": kwargs["turn_id"], "session_id": kwargs["session_id"], "strategy_id": kwargs["strategy_id"]}
        trace = {"call_id": "fixture_market_read", "action": "market_data", "payload": {"market": "BINANCE:BTCUSDT", "timeframe": "1d", "limit": 30, "acceptance_fixture": True}}
        bus.publish("tool.start", **common, **trace)
        if self.fail:
            bus.publish("tool.complete", **common, **trace, ok=False, error="受控验收：模拟数据源超时", elapsed_ms=1)
            raise RuntimeError("受控验收样例：模拟数据源超时，不代表真实市场服务异常")
        (OUT / "running.json").write_text(json.dumps(api.agent_tasks(kwargs["strategy_id"]), ensure_ascii=False, indent=2))
        deadline = time.monotonic() + 90
        while not (OUT / "continue-run").exists() and time.monotonic() < deadline:
            time.sleep(.2)
        output = {"acceptance_fixture": True, "note": "受控验收返回，未请求真实行情", "rows": 30}
        bus.publish("tool.complete", **common, **trace, ok=True, result=output, elapsed_ms=25)
        answer = "【受控验收样例，不是真实模型分析】\n已验证任务接收、工具步骤和结果展示。\n\n这份样例没有读取真实市场数据，不提供投资结论；没有提交订单或修改账户。"
        bus.publish("message.delta", **common, text=answer, completed=False)
        time.sleep(1)
        return SimpleNamespace(turn_id=kwargs["turn_id"], final_text=answer, decision={}, actions=[],
            tool_trace=[{**trace, "ok": True, "result": output, "elapsed_ms": 25}],
            stopped_reason="end_turn", iterations=2, budget={"tool_calls": 1, "acceptance_fixture": True})

def runtime(fail=False):
    return TriggerRuntime(config=cfg, router=TriggerRuntime.boot(cfg).router,
        agent_task_executor_factory=lambda config: StrategyAgentTaskExecutor(config=config, kernel_factory=lambda _: ControlledKernel(fail)))

def emit(sid, fail=False):
    rt = runtime(fail)
    return rt.emit(rt.from_payload({"source": "ui_acceptance_fixture", "kind": "strategy.acceptance",
        "strategy_id": sid, "target": TARGET, "payload": {"acceptance_fixture": True}}))

def exercise():
    try:
        # No fabricated bars: a missing data source must remain a real skip/error.
        script = cases[0]["strategy_id"]
        try:
            result = api.run_tick(script)
            (OUT / "script-run.json").write_text(json.dumps(result, ensure_ascii=False, indent=2, default=str))
        except Exception as exc:
            (OUT / "script-run-error.txt").write_text(str(exc))
        macd = emit(cases[1]["strategy_id"])
        (OUT / "macd-task.json").write_text(json.dumps(macd.asdict(), ensure_ascii=False, indent=2, default=str))
        deadline = time.monotonic() + 900
        while not (OUT / "start-run").exists() and time.monotonic() < deadline:
            time.sleep(.2)
        if not (OUT / "start-run").exists():
            return
        finished = emit(cases[2]["strategy_id"])
        (OUT / "finished.json").write_text(json.dumps(finished.asdict(), ensure_ascii=False, indent=2, default=str))
        failed = emit(cases[2]["strategy_id"], fail=True)
        (OUT / "failed.json").write_text(json.dumps(failed.asdict(), ensure_ascii=False, indent=2, default=str))
    except Exception as exc:
        (OUT / "fixture-error.txt").write_text(repr(exc))

server = build_server(cfg, host="127.0.0.1", port=0)
port = server.server_address[1]
index = {"api": f"http://127.0.0.1:{port}", "workspace": str(ROOT), "cases": cases,
         "evidence_scope": "Real UI and executor; controlled model fixture, no real market/model or schedule activation."}
(OUT / "fixture-index.json").write_text(json.dumps(index, ensure_ascii=False, indent=2))
threading.Thread(target=exercise, daemon=True).start()
print(json.dumps(index, ensure_ascii=False), flush=True)
try:
    server.serve_forever()
finally:
    server.server_close()
