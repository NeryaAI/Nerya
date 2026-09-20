"""Real UI/installed SDK acceptance. All model and market responses are fixtures.
No production strategy generation, scheduler activation or orders. Exercises the extended dispatch selection SDK.
"""
from pathlib import Path
import json
import os
import runpy
import threading
import time
from nerya.api.local_server import build_server
from nerya.strategies.context import StrategyMarket, StrategyClock
from nerya.strategies.package import load_package
from nerya.strategies.evolution import StrategyEvolutionRunner
from nerya.llm.messages import MessagesResponse
from nerya.skills.kernel import SkillKernel
from nerya.triggers.runtime import TriggerRuntime
from nerya.triggers.strategy_agent_task_executor import TARGET

BASE = Path(__file__).resolve().parents[1]
name = os.environ.get("NERYA_BRANCH_REVIEW", "workflow-branches-0917")
assert name.startswith("workflow-branches-") and "/" not in name and ".." not in name
OUT = BASE / "dashboard/test-results" / name
OUT.mkdir(parents=True, exist_ok=True)
ROOT = OUT / "workspace"
assert not ROOT.exists(), "Choose a fresh isolated acceptance directory"
ROOT.mkdir()
fixtures = runpy.run_path(str(BASE / "tests/test_workflow_branch_contract.py"))
cfg, package = runpy.run_path(str(BASE / "tests/test_strategy_dispatch_selection.py"))["seed_selected_branch"](ROOT)
(ROOT / "fixture-evidence.txt").write_text("BRANCH_TOOL_EVIDENCE: 仅验收脚本分支与数据传递，不是真实行情分析。")
case = {"name": "flat", "now": int(time.time())}
models, readers = [], []
lock = threading.Lock()

def candles(self, market=None, *, timeframe="1m", limit=100, **kwargs):
    readers.append({"case": case["name"], "market": market, "timeframe": timeframe, "limit": limit})
    if case["name"] == "failure": raise RuntimeError("受控行情接口失败")
    return fixtures["fixture_rows"](case["name"], case["now"])
StrategyMarket.candles = candles
StrategyClock.now_ms = lambda self: case["now"] * 1000

def message_text(value):
    if isinstance(value, str): return value
    if isinstance(value, dict): return json.dumps(value, ensure_ascii=False) + "\n" + "\n".join(message_text(v) for v in value.values())
    if isinstance(value, list): return "\n".join(message_text(v) for v in value)
    return ""

def supplied_payload(value):
    if isinstance(value, str) and "=== task payload ===" in value:
        tail = value.split("=== task payload ===", 1)[1]
        start = tail.find("{")
        if start >= 0:
            try: return json.JSONDecoder().raw_decode(tail[start:])[0]
            except ValueError: pass
    if isinstance(value, (dict, list)):
        for child in value.values() if isinstance(value, dict) else value:
            found = supplied_payload(child)
            if found is not None: return found
    return None

class Gateway:
    def __init__(self, *args, **kwargs): self.count = 0
    def effective_model_metadata(self, *args, **kwargs): return "controlled", "branch-proof", {}
    def call_messages(self, **kwargs):
        self.count += 1
        caller = str(kwargs.get("caller", ""))
        text = message_text(kwargs.get("messages", []))
        review = case["name"].startswith("review_")
        if review:
            assert "workflow_context" in text and "selected_agent_task_ids" in text
        if not review:
            assert "RAW_ROWS_NOT_FOR_AGENT" not in text and "TRIGGER_NOT_FOR_AGENT" not in text and "OUTPUT_NOT_FOR_AGENT" not in text
            assert '"selected_signal"' in text and '"score": 0' in text
        with lock: models.append({"case": case["name"], "caller": caller, "iteration": self.count, "tool_received": "BRANCH_TOOL_EVIDENCE" in text, "selected_context_verified": not review})
        if self.count <= 2 and not review:
            content = [{"type": "tool_use", "id": f"read_{id(self)}_{self.count}", "name": "read_file", "input": {"path": "fixture-evidence.txt"}}]
            stop = "tool_use"
        else:
            if not review: assert "BRANCH_TOOL_EVIDENCE" in text
            if review:
                output = {"summary": "受控复盘：本轮不需要修改", "proposed_changes": [], "done": True}
                if case["name"] == "review_change":
                    payload = supplied_payload(kwargs.get("messages", []))
                    assert payload is not None and "performance" in payload
                    original = payload["performance"]["package_context"]["files"]["main.py"]["content"]
                    assert "StrategyAgentTask.stop" in original and "read_signal(ctx)" in original
                    output.update(summary="受控复盘：补充停止原因，让用户知道等待下次调度", proposed_changes=[{
                        "file": "main.py", "kind": "full_file",
                        "after_content": original.replace("MACD 未交叉，本轮结束", "MACD 未交叉，本轮结束，等待下次调度"),
                        "rationale": "改善已验证无信号分支的可读性，不改触发规则或交易权限。"}], validation_plan=["manual_review"])
                answer = json.dumps(output, ensure_ascii=False)
            elif caller.startswith("subagent:"):
                answer = json.dumps({"summary": "已核对当前脚本选择的信号摘要与证据文件，没有收到未选择的原始行情或触发内容。受控验收。", "done": True}, ensure_ascii=False)
            else:
                direction = "上穿机会分析" if case["name"] == "up" else "下穿风险分析"
                answer = f"## {direction}\n\n当前路径由 Python 脚本选择。只有当前路径指定的角色核对了信号摘要，另一角色没有被预先派发；协调 Agent 随后又读取证据文件。\n\n收到的数据：市场、15m 周期、已收盘时间、最近两次 MACD 柱值、score = 0 和 trade_allowed = false。\n\n未自动附加整份 K 线与触发内容。未提交订单。\n\n**受控模型与行情验收，不代表真实金融分析。**"
            content = [{"type": "text", "text": answer}]; stop = "end_turn"
        return MessagesResponse(content=content, stop_reason=stop, usage={"input_tokens": 10, "output_tokens": 10}, provider="controlled", model="branch-proof")
import nerya.agent.kernel as kernel_module
import nerya.subagents.dispatcher as dispatcher_module
kernel_module.LLMGateway = Gateway
dispatcher_module.LLMGateway = Gateway


def run_tests():
    try:
        deadline = time.monotonic() + 1800
        while not (OUT / "start-run").exists() and time.monotonic() < deadline: time.sleep(.2)
        if not (OUT / "start-run").exists(): return
        results = []
        runtime = TriggerRuntime.boot(cfg)
        for name, expected in [("flat","skipped"),("up","executed"),("duplicate","skipped"),("down","executed"),("failure","failed")]:
            case["name"] = "up" if name == "duplicate" else name
            if name == "down": case["now"] += 900
            before = len(models)
            event = runtime.from_payload({"source": "test", "kind": "strategy.tick", "strategy_id": package.strategy_id, "target": TARGET,
                "payload": {"excluded": "TRIGGER_NOT_FOR_AGENT", "fixture": True}})
            result = runtime.emit(event).asdict()
            assert result["status"] == expected, result
            calls = len(models) - before
            if expected in {"skipped", "failed"}: assert calls == 0, (name, calls)
            else:
                assert calls == 6, (name, calls)
                chosen = "left" if name == "up" else "right"
                assert result["result"]["metadata"]["selected_roles"] == [chosen]
                assert {m["caller"] for m in models[before:]} == {f"subagent:{chosen}", "agent:loop"}
            results.append({"case": name, "model_calls": calls, "result": result})
            (OUT / "branch-results.json").write_text(json.dumps(results, ensure_ascii=False, indent=2, default=str))
        original = (package.root / "main.py").read_text()
        review_results = []
        for name in ["review_hold", "review_change"]:
            case["name"] = name
            result = StrategyEvolutionRunner(config=cfg, skills=SkillKernel.boot(cfg)).run_once(package.strategy_id).asdict()
            assert len(result["snapshot"]["workflow_context"]["tasks"]) == 5, result["snapshot"].get("workflow_context")
            if name == "review_hold": assert result["status"] == "hold" and not result["proposal_id"], result
            else:
                assert result["proposal_id"], result
                after = cfg.paths.proposals / result["proposal_id"] / "after/strategies" / package.strategy_id / "main.py"
                assert after.is_file() and "等待下次调度" in after.read_text()
                from nerya.strategies.workflow_service import source_files
                from nerya.strategies.validator import validate_proposal_files
                files, _ = source_files(cfg.paths, package.strategy_id, result["proposal_id"])
                validation = validate_proposal_files(strategy_id=package.strategy_id, files=files)
                assert validation.ok, validation.asdict()
                result["actual_static_validation"] = validation.asdict()
            assert (package.root / "main.py").read_text() == original
            review_results.append({"case": name, "result": result})
            (OUT / "review-results.json").write_text(json.dumps(review_results, ensure_ascii=False, indent=2, default=str))
        (OUT / "model-calls.json").write_text(json.dumps(models, ensure_ascii=False, indent=2))
        (OUT / "data-reads.json").write_text(json.dumps(readers, ensure_ascii=False, indent=2))
        (OUT / "finished.json").write_text(json.dumps({"ok": True, "branches": len(results), "reviews": len(review_results)}, indent=2))
    except Exception:
        import traceback
        (OUT / "fixture-error.txt").write_text(traceback.format_exc())

server = build_server(cfg, host="127.0.0.1", port=0)
info = {"api": f"http://127.0.0.1:{server.server_address[1]}", "workspace": str(ROOT), "strategy_id": package.strategy_id,
    "evidence_scope": "Real extended dispatch SDK, role selection, Agent kernel, tools and review candidate validation. Controlled model and market; no live trading."}
(OUT / "fixture-index.json").write_text(json.dumps(info, ensure_ascii=False, indent=2))
threading.Thread(target=run_tests, daemon=True).start()
print(json.dumps(info), flush=True)
try: server.serve_forever()
finally: server.server_close()
