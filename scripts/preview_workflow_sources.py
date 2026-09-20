"""Opt-in source UI acceptance. Controlled readers/model; real context and kernel.
Never used for production strategy generation or trading.
"""
from pathlib import Path
import json
import os
import runpy
import threading
import time
from nerya.core import yaml_io
from nerya.api.local_server import build_server
from nerya.strategies.package import load_package
from nerya.strategies.context import StrategyMarket, StrategyNews
from nerya.llm.messages import MessagesResponse
from nerya.triggers.runtime import TriggerRuntime
from nerya.triggers.strategy_agent_task_executor import TARGET

BASE = Path(__file__).resolve().parents[1]
name = os.environ.get("NERYA_SOURCE_REVIEW", "workflow-sources-release")
assert name.startswith("workflow-sources-") and "/" not in name and ".." not in name
OUT = BASE / "dashboard/test-results" / name
OUT.mkdir(parents=True, exist_ok=True)
ROOT = OUT / "workspace"
assert not ROOT.exists(), "Use a fresh isolated review name"
ROOT.mkdir()
seed = runpy.run_path(str(BASE / "tests/test_strategy_agent_context.py"))["seed"]
cfg, package, raw = seed(ROOT, team=True)
raw.update(title="多品种行情观察", description="一个行情源读取多个品种和周期，趋势与风险 Agent 并行分析。受控验收，不交易。", markets=["mock:BTC/USDT", "mock:ETH/USDT"])
raw["data_sources"] = [
    {"id":"bars", "title":"多周期K线", "provider":"runtime.market", "capability":"candles", "markets":raw["markets"], "timeframes":["15m","1h","4h"], "limit":120, "consumers":["prepare.py"], "parameters":{"threshold":0,"strict":False}},
    {"id":"daily", "title":"长期参考", "provider":"runtime.market", "capability":"candles", "timeframe":"1d", "limit":30, "consumers":[]},
    {"id":"news", "title":"新闻快讯", "provider":"runtime.news", "capability":"news", "sources":["market_feed","project_feed"], "limit":20, "consumers":[]},
]
raw["agent_context"].update(sources=["bars","daily","news"])
raw["agent_execution"]["team"]["max_parallel"] = 2
raw["agent_profile"].update(title="分析协调 Agent", role="综合多周期数据、脚本结果与并行角色意见，必要时继续核验。只观察，不下单。")
yaml_io.dump(package.root / "strategy.yml", raw)
(package.root / "prepare.py").write_text('''"""整理各品种与周期的输入，保留序列标识。"""
def prepare(ctx):
    snapshot = ctx.inputs.source("bars")
    result = {"series_count": len(snapshot["series"]), "score": 0, "confirmed": False}
    ctx.inputs.publish("preflight", result, source="prepare.py")
    return result
''')
(package.root / "main.py").write_text('''"""派发已整理的数据，交给并行角色分析。"""
from nerya.strategies.agent_task import StrategyAgentTask
from prepare import prepare

def run(ctx):
    result = prepare(ctx)
    return StrategyAgentTask.dispatch(prompt="核对多周期输入、脚本结果与证据文件。这是受控验收，只报告收到的实际数据，不生成交易建议。", context={"prepared": result})
''')
(package.root / "workflow.json").write_text(json.dumps({"version":1,"nodes":{"script:main.py":{"title":"派发分析"},"script:prepare.py":{"title":"整理数据"},"agent:role/left":{"title":"趋势分析 Agent"},"agent:role/right":{"title":"风险分析 Agent"}},"edges":[]}, ensure_ascii=False))
package = load_package(cfg.paths, package.strategy_id)
(ROOT / "fixture-evidence.txt").write_text("SERIES_REVIEW_TOOL: 受控数据核验记录，不是真实行情。")
calls, model_calls, lock = [], [], threading.Lock()

def candles(self, market=None, *, timeframe="1m", limit=100, **kwargs):
    with lock: calls.append({"market":market,"timeframe":timeframe,"limit":limit})
    return [{"ts":1700000000+i*60,"open":100+i,"high":103+i,"low":99+i,"close":101+i,"volume":0,"fixture":"SOURCE_MATRIX_TEST"} for i in range(3)]
def news(self, **kwargs):
    with lock: calls.append({"news":kwargs})
    return [{"title":"受控新闻样例","source":"market_feed","fixture":"SOURCE_NEWS_TEST"}]
StrategyMarket.candles = candles
StrategyNews.fetch = news

class Gateway:
    def __init__(self,*args,**kwargs): self.count=0
    def effective_model_metadata(self,*args,**kwargs): return "controlled","source-test",{}
    def call_messages(self,**kwargs):
        self.count += 1
        content=json.dumps(kwargs.get("messages",[]),ensure_ascii=False)
        assert all(s in content for s in ["SOURCE_MATRIX_TEST","SOURCE_NEWS_TEST","mock:BTC/USDT","mock:ETH/USDT","15m","1h","4h"])
        with lock: model_calls.append({"caller":kwargs.get("caller"),"iteration":self.count,"upstream_received":True,"tool_received":"SERIES_REVIEW_TOOL" in content})
        if self.count <= 2:
            body=[{"type":"tool_use","id":f"read_{id(self)}_{self.count}","name":"read_file","input":{"path":"fixture-evidence.txt"}}];reason="tool_use"
        else:
            assert "SERIES_REVIEW_TOOL" in content
            text=json.dumps({"summary":"已收到6组多周期数据、日线参考与新闻，脚本score为0、confirmed为false。受控验收。","done":True},ensure_ascii=False)
            if not str(kwargs.get("caller","")).startswith("subagent:"):
                text="## 输入核验完成\n\n已收到 2 个品种 × 3 个周期的 6 组输入，以及日线参考和新闻。两个角色分别完成多轮核验。\n\n前置脚本输出：score = 0，confirmed = false。\n\n受控模型与数据验收，不代表真实行情分析；未下单。"
            body=[{"type":"text","text":text}];reason="end_turn"
        return MessagesResponse(content=body,stop_reason=reason,usage={"input_tokens":10,"output_tokens":5},provider="controlled",model="source-test")
import nerya.agent.kernel as kernel_module
import nerya.subagents.dispatcher as dispatcher_module
kernel_module.LLMGateway=Gateway
dispatcher_module.LLMGateway=Gateway

def run():
    try:
        deadline=time.monotonic()+1800
        while not (OUT/"start-run").exists() and time.monotonic()<deadline: time.sleep(.2)
        if not (OUT/"start-run").exists(): return
        runtime=TriggerRuntime.boot(cfg)
        event=runtime.from_payload({"source":"test","kind":"strategy.tick","strategy_id":package.strategy_id,"target":TARGET,"payload":{"acceptance_fixture":True}})
        out=runtime.emit(event)
        (OUT/"finished.json").write_text(json.dumps(out.asdict(),ensure_ascii=False,indent=2,default=str))
        (OUT/"source-calls.json").write_text(json.dumps(calls,ensure_ascii=False,indent=2))
        (OUT/"model-checks.json").write_text(json.dumps(model_calls,ensure_ascii=False,indent=2))
    except Exception:
        import traceback
        (OUT/"fixture-error.txt").write_text(traceback.format_exc())
server=build_server(cfg,host="127.0.0.1",port=0)
info={"api":f"http://127.0.0.1:{server.server_address[1]}","workspace":str(ROOT),"strategy_id":package.strategy_id,"evidence_scope":"Real context, tools and Agent kernel; controlled model, market and news. No live execution."}
(OUT/"fixture-index.json").write_text(json.dumps(info,ensure_ascii=False,indent=2))
threading.Thread(target=run,daemon=True).start()
print(json.dumps(info),flush=True)
try: server.serve_forever()
finally: server.server_close()
