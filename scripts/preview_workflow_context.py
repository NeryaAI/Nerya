"""Opt-in UI acceptance fixture; never production strategy generation.

Real kernel + parallel team + native read_file execution. Only the model
provider and source data are controlled. No credentials, orders or schedules.
"""
from __future__ import annotations
from copy import deepcopy
import json
import os
from pathlib import Path
import threading
import time

from nerya.core import yaml_io
from nerya.api.local_server import build_server
from nerya.llm.messages import MessagesResponse
from nerya.strategies.package import load_package
from nerya.triggers.runtime import TriggerRuntime
from nerya.triggers.strategy_agent_task_executor import TARGET
import runpy

BASE = Path(__file__).resolve().parents[1]
seed = runpy.run_path(str(BASE / "tests/test_strategy_agent_context.py"))["seed"]
run_name = os.environ.get("NERYA_CONTEXT_REVIEW", "workflow-context-review")
assert run_name.startswith("workflow-context-") and "/" not in run_name and ".." not in run_name
OUT = BASE / "dashboard/test-results" / run_name
OUT.mkdir(parents=True, exist_ok=True)
ROOT = OUT / "workspace"
assert not ROOT.exists(), "Use a fresh review directory; never overwrite a previous run"
ROOT.mkdir()
cfg, pkg, raw = seed(ROOT, team=True)
raw.update(title="行情研判 · 并行协作", description="先整理输入，再由趋势与风险 Agent 并行分析，最后汇总。受控验收示例，不交易。")
raw["agent_profile"].update(title="分析协调 Agent", role="基于前置脚本与数据源快照，汇总趋势和风险分析。可以读取证据、补充核验并多轮决策；保留不确定性。只观察，不下单。")
raw["data_sources"][0].update(title="行情数据 · 验收快照", timeframe="15m", limit=3)
raw["agent_execution"]["team"].update(max_parallel=2)
yaml_io.dump(pkg.root / "strategy.yml", raw)
(pkg.root / "prepare.py").write_text('''"""整理输入快照，并发布前置脚本计算结果。"""
def prepare(ctx):
    bars = [{"close": 123.45, "fixture_marker": "WORKFLOW_EVIDENCE_765"}]
    ctx.inputs.publish("source:bars", bars, source="受控数据源，仅用于验收")
    prepared = {"rows": len(bars), "latest_close": bars[-1]["close"], "score": 0, "confirmed": False}
    ctx.inputs.publish("preflight", prepared, source="prepare.py")
    return prepared
''', encoding="utf-8")
(pkg.root / "main.py").write_text('''"""接收脚本结果，交给并行 Agent 分析与汇总。"""
from nerya.strategies.agent_task import StrategyAgentTask
from prepare import prepare

def run(ctx):
    result = prepare(ctx)
    return StrategyAgentTask.dispatch(prompt="这是受控链路验收。核对前置脚本、数据源及证据文件，输出核验结论和仍有的缺口，不提供真实行情判断，不下单。", context={"entry_result": result})
''', encoding="utf-8")
meta = {"version": 1, "nodes": {
    "script:main.py": {"title": "分析入口"},
    "script:prepare.py": {"title": "整理数据与信号"},
    "source:data_sources/bars": {"title": "行情输入快照"},
    "agent:role/left": {"title": "趋势分析 Agent"},
    "agent:role/right": {"title": "风险分析 Agent"},
}, "edges": []}
(pkg.root / "workflow.json").write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
pkg = load_package(cfg.paths, pkg.strategy_id)
(ROOT / "fixture-evidence.txt").write_text("FILE_EVIDENCE_876：受控证据文件；指标样例不代表真实行情。", encoding="utf-8")
barrier = threading.Barrier(2)
records, lock = [], threading.Lock()
fail_role = False

class Gateway:
    def __init__(self, *_args, **_kwargs):
        self.calls = 0
    def effective_model_metadata(self, *_args, **_kwargs):
        return "controlled", "workflow-review", {}
    def call_messages(self, **kwargs):
        self.calls += 1
        child = str(kwargs.get("caller", "")).startswith("subagent:")
        caller = str(kwargs.get("caller", "coordinator"))
        content = json.dumps(kwargs.get("messages", []), ensure_ascii=False)
        assert "WORKFLOW_EVIDENCE_765" in content, "Source snapshot missing from real model request"
        with lock:
            records.append({"provider_instance": id(self), "caller": caller, "iteration": self.calls,
                "has_source": "WORKFLOW_EVIDENCE_765" in content, "has_script_output": 'score' in content,
                "has_tool_evidence": "FILE_EVIDENCE_876" in content, "at": time.time()})
        if child and self.calls == 1:
            barrier.wait(timeout=15)
            deadline = time.monotonic() + 180
            while not (OUT / "continue-run").exists() and time.monotonic() < deadline:
                time.sleep(.15)
        if fail_role and child and "right" in caller:
            raise RuntimeError("受控成员失败：用于验证部分失败不会被标为全部成功")
        if self.calls <= 2:
            value = [{"type": "tool_use", "id": f"read_{id(self)}_{self.calls}", "name": "read_file", "input": {"path": "fixture-evidence.txt"}}]
            reason = "tool_use"
        else:
            assert "FILE_EVIDENCE_876" in content, "Actual read_file output missing on the next model round"
            if child:
                text = json.dumps({"summary": "已读取共享快照与证据文件，并完成第二轮核对。受控验收，非真实行情判断。", "done": True,
                    "source_marker": "WORKFLOW_EVIDENCE_765", "latest_close": 123.45, "script_score": 0, "confirmed": False}, ensure_ascii=False)
            else:
                text = "## 核验结果\n\n两个角色已并行返回。我又读取证据文件做了复核，前置脚本与数据源快照一致。\n\n| 输入 | 已收到的值 |\n| --- | --- |\n| 数据源收盘价 | 123.45 |\n| 脚本 score | 0 |\n| 脚本 confirmed | false |\n\n这是受控执行验收，不是真实模型行情分析；没有提交订单。"
                if fail_role:
                    text = "## 部分完成\n\n风险角色出现受控失败，趋势角色已返回。保留失败记录，不将本次团队分析视为全部成功。\n\n这是受控验收，不是真实行情分析，没有提交订单。"
            value, reason = [{"type": "text", "text": text}], "end_turn"
        return MessagesResponse(content=value, stop_reason=reason, usage={"input_tokens": 10, "output_tokens": 5}, provider="controlled", model="workflow-review")

import nerya.agent.kernel as kernel_module
import nerya.subagents.dispatcher as dispatcher_module
kernel_module.LLMGateway = Gateway
dispatcher_module.LLMGateway = Gateway

def run():
    global fail_role
    try:
        deadline = time.monotonic() + 1800
        while not (OUT / "start-run").exists() and time.monotonic() < deadline:
            time.sleep(.15)
        if not (OUT / "start-run").exists():
            return
        runtime = TriggerRuntime.boot(cfg)
        payload = {"source": "test", "kind": "strategy.tick", "strategy_id": pkg.strategy_id, "target": TARGET, "payload": {"acceptance_fixture": True}}
        outcome = runtime.emit(runtime.from_payload(payload))
        (OUT / "finished.json").write_text(json.dumps(outcome.asdict(), ensure_ascii=False, indent=2, default=str))
        (OUT / "provider-checks.json").write_text(json.dumps(records, ensure_ascii=False, indent=2))
        deadline = time.monotonic() + 900
        while not (OUT / "start-failure").exists() and time.monotonic() < deadline:
            time.sleep(.2)
        if (OUT / "start-failure").exists():
            fail_role = True
            outcome = runtime.emit(runtime.from_payload(payload))
            (OUT / "partial-failure.json").write_text(json.dumps(outcome.asdict(), ensure_ascii=False, indent=2, default=str))
    except Exception as exc:
        import traceback
        (OUT / "fixture-error.txt").write_text(traceback.format_exc())

server = build_server(cfg, host="127.0.0.1", port=0)
index = {"api": f"http://127.0.0.1:{server.server_address[1]}", "workspace": str(ROOT), "strategy_id": pkg.strategy_id,
         "evidence_scope": "Real kernel, tools and parallel runtime; controlled model provider and source data only. No live market analysis, no orders, no schedules."}
(OUT / "fixture-index.json").write_text(json.dumps(index, ensure_ascii=False, indent=2))
threading.Thread(target=run, daemon=True).start()
print(json.dumps(index), flush=True)
try:
    server.serve_forever()
finally:
    server.server_close()
