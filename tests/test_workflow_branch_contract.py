"""Acceptance fixtures for the INSTALLED SDK; not a production strategy generator."""
from __future__ import annotations
from pathlib import Path
from types import SimpleNamespace
from copy import deepcopy
import importlib.util
import runpy
import sys
import time
from unittest.mock import patch
import pytest
from nerya.core import yaml_io
from nerya.strategies.context import build_strategy_context, StrategyClock
from nerya.strategies.input_context import collect_task_context
from nerya.strategies.package import load_package
from nerya.strategies.validator import validate_proposal_files

pytestmark = pytest.mark.smoke

FILES = {
    "main.py": '''"""先判断，再选择一条路径。无交叉立即结束，不调用 Agent。"""
from nerya.strategies import StrategyAgentTask
from wfbc_signal import read_signal
from wfbc_up import upward_task
from wfbc_down import downward_task


def run(ctx):
    try:
        signal = read_signal(ctx)
        if not signal["direction"]:
            return StrategyAgentTask.skip(
                reason="MACD 未交叉，本轮结束",
                metadata={"path": "stop", "bar_ts": signal["bar_ts"]},
            )
        key = "selected_bar:" + signal["market"] + ":" + signal["timeframe"]
        previous = ctx.state.get(key)
        if previous is not None and previous >= signal["bar_ts"]:
            return StrategyAgentTask.skip(reason="同一根已收盘 K 线已处理", metadata={"path": "duplicate"})
        if not ctx.state.compare_and_set(key, expect=previous, new_value=signal["bar_ts"]):
            return StrategyAgentTask.skip(reason="已有运行选中该 K 线", metadata={"path": "duplicate"})
        # 这里只记录已选中，不声称 Agent 已完成或保证恰好一次交付。
        if signal["direction"] == "up":
            return upward_task(ctx, signal)
        return downward_task(ctx, signal)
    except Exception as exc:
        return StrategyAgentTask.error(reason=str(exc), metadata={"path": "input_error"})
''',
    "wfbc_signal.py": '''"""读取已收盘数据，计算真实 MACD 交叉；参数来自数据源配置。"""
import math


def ema(values, period):
    result = [values[0]]
    alpha = 2.0 / (period + 1)
    for value in values[1:]:
        result.append(alpha * value + (1.0 - alpha) * result[-1])
    return result


def read_signal(ctx):
    preset = next(s for s in ctx.config.extras["data_sources"] if s["id"] == "bars")
    params = preset["parameters"]
    fast, slow, signal = [int(params[k]) for k in ("fast", "slow", "signal")]
    if not 0 < fast < slow or signal < 1:
        raise ValueError("MACD 参数无效")
    frame = preset["timeframe"]
    seconds = int(frame[:-1]) * {"m": 60, "h": 3600, "d": 86400}[frame[-1]]
    now = ctx.clock.now_ms() / 1000
    rows = ctx.inputs.source("bars")
    closed = [r for r in rows if float(r["ts"]) + seconds <= now]
    if len(closed) < 3 * (slow + signal):
        raise ValueError("已收盘数据不足")
    if now - float(closed[-1]["ts"]) > seconds * 2:
        raise ValueError("数据已过期")
    prices = [float(r["close"]) for r in closed]
    if any(not math.isfinite(v) or v <= 0 for v in prices):
        raise ValueError("价格数据无效")
    dif = [a - b for a, b in zip(ema(prices, fast), ema(prices, slow))]
    hist = [a - b for a, b in zip(dif, ema(dif, signal))]
    direction = "up" if hist[-2] <= 0 < hist[-1] else "down" if hist[-2] >= 0 > hist[-1] else ""
    return {"direction": direction, "market": ctx.config.markets[0], "timeframe": frame,
            "bar_ts": closed[-1]["ts"], "histogram": hist[-2:], "score": 0, "trade_allowed": False}
''',
    "wfbc_up.py": '''"""上穿路径：只传本次信号摘要，不自动附加原始行情或触发内容。"""
from nerya.strategies import StrategyAgentTask


def upward_task(ctx, signal):
    return StrategyAgentTask.dispatch(
        prompt="按分析配置核对上穿机会与风险。受控验收，只观察，不下单。",
        context={"path": "opportunity", "selected_signal": signal},
        metadata={"path": "opportunity"}, reason="上穿：进入机会分析路径",
        session_key={"market": signal["market"], "timeframe": signal["timeframe"], "path": "opportunity"},
    )
''',
    "wfbc_down.py": '''"""下穿路径：只传本次风险证据，不执行上穿分析脚本。"""
from nerya.strategies import StrategyAgentTask


def downward_task(ctx, signal):
    return StrategyAgentTask.dispatch(
        prompt="按分析配置核对下穿风险与不确定性。受控验收，只观察，不下单。",
        context={"path": "risk", "selected_signal": signal},
        metadata={"path": "risk"}, reason="下穿：进入风险分析路径",
        session_key={"market": signal["market"], "timeframe": signal["timeframe"], "path": "risk"},
    )
''',
}


def seed_branch(root: Path, team=False):
    seed = runpy.run_path(str(Path(__file__).with_name("test_strategy_agent_context.py")))["seed"]
    config, package, raw = seed(root, team=team)
    raw.update(title="MACD 条件分支", description="无交叉结束；上穿分析机会，下穿分析风险。受控测试，不交易。")
    raw["data_sources"] = [{"id": "bars", "title": "行情数据", "provider": "runtime.market", "capability": "candles",
        "timeframe": "15m", "limit": 160, "consumers": ["wfbc_signal.py"], "parameters": {"fast": 12, "slow": 26, "signal": 9}}]
    raw["agent_context"] = {"sources": [], "include_script_outputs": True, "include_trigger": False}
    raw["agent_session"] = {"include_prior_messages": False}
    raw["agent_profile"].update(title="信号分析 Agent", role="只基于本次脚本选择的数据分析，必要时读取证据并继续核验。只观察，不下单。")
    raw["tuning"] = {"enabled": True, "schedule": {"type": "cron", "cron": "0 */6 * * *", "enabled": False},
        "objectives": ["execution_quality"], "lookback": {"min_closed_trades": 0},
        "proposal_policy": {"allowed_targets": ["main.py", "wfbc_signal.py", "wfbc_up.py", "wfbc_down.py", "strategy.yml"]},
        "guardrails": {"require_backtest": False},
        "tuning_prompt": "复盘真实运行路径和输入证据；仅在有依据时提出完整文件修改，验证没有信号仍零调用。只创建待审核提案。"}
    yaml_io.dump(package.root / "strategy.yml", raw)
    for name, content in FILES.items(): (package.root / name).write_text(content)
    (package.root / "workflow.json").write_text(__import__("json").dumps({"version": 1, "nodes": {
        "script:main.py": {"title": "判断与分流"}, "script:wfbc_signal.py": {"title": "计算已收盘 MACD"},
        "script:wfbc_up.py": {"title": "上穿：机会分析"}, "script:wfbc_down.py": {"title": "下穿：风险分析"}}, "edges": []}, ensure_ascii=False))
    return config, load_package(config.paths, package.strategy_id)


def fixture_rows(case: str, now: int):
    base = int(now // 900) * 900
    rows = [{"ts": base - (160-i)*900, "close": 100.0, "excluded": "RAW_ROWS_NOT_FOR_AGENT"} for i in range(160)]
    if case == "up": rows[-1]["close"] = 130.0
    if case == "down": rows[-1]["close"] = 70.0
    if case == "open": rows.append({"ts": base, "close": 130.0})
    if case == "stale":
        for row in rows: row["ts"] -= 9000
    if case == "insufficient": rows = rows[-2:]
    return rows


def load_main(package):
    spec = importlib.util.spec_from_file_location("wfbc_main", package.root / "main.py")
    module = importlib.util.module_from_spec(spec)
    sys.path.insert(0, str(package.root))
    try:
        for key in ("wfbc_signal", "wfbc_up", "wfbc_down"): sys.modules.pop(key, None)
        spec.loader.exec_module(module)
    finally: sys.path.remove(str(package.root))
    return module


@pytest.mark.parametrize("case,path,status", [("flat","stop","skip"),("open","stop","skip"),("up","opportunity","dispatch"),("down","risk","dispatch"),("stale","input_error","error"),("insufficient","input_error","error"),("failure","input_error","error")])
def test_actual_macd_branches_and_explicit_context(tmp_path, case, path, status):
    config, package = seed_branch(tmp_path)
    ctx = build_strategy_context(config=config, package=package, run_id="branch", session_id=None)
    now = int(time.time()); ctx.clock.freeze(iso="2026-09-17T00:00:00Z", ms=now*1000)
    rows = fixture_rows(case, now)
    reader = SimpleNamespace(candles=lambda *a, **kw: deepcopy(rows))
    if case == "failure":
        def fail(*a, **kw): raise RuntimeError("行情接口失败")
        reader.candles = fail
    ctx.inputs.market = reader
    ctx.trigger.payload["excluded"] = "TRIGGER_NOT_FOR_AGENT"
    main = load_main(package)
    with patch.object(main, "upward_task", wraps=main.upward_task) as up, patch.object(main, "downward_task", wraps=main.downward_task) as down:
        task = main.run(ctx)
        assert task.status == status
        assert task.metadata["path"] == path
        assert up.call_count == int(case == "up") and down.call_count == int(case == "down")
        collect_task_context(task, ctx, package.manifest.extras["agent_context"])
        if status == "dispatch":
            assert task.context["published"] == {} and task.context["trigger"] == {}
            assert task.context["script_outputs"]["selected_signal"]["score"] == 0
            assert task.context["script_outputs"]["selected_signal"]["trade_allowed"] is False
            assert "RAW_ROWS_NOT_FOR_AGENT" not in str(task.context)
            assert "TRIGGER_NOT_FOR_AGENT" not in str(task.context)
            assert main.run(ctx).metadata["path"] == "duplicate"


def test_fixture_files_validate_against_current_sdk(tmp_path):
    _, package = seed_branch(tmp_path)
    files = {p: (package.root / p).read_text() for p in package.files}
    assert validate_proposal_files(strategy_id=package.strategy_id, files=files).ok
