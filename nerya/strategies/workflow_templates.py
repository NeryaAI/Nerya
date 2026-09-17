"""Three executable-contract examples. Paper drafts only, never auto-started.

These illustrate architecture, not profitable trading systems. Market data is
requested through the real SDK; no synthetic candles or performance claims.
"""
from __future__ import annotations

from dataclasses import replace
from typing import Any
from uuid import uuid4

from ..core import yaml_io
from ..core.paths import WorkspacePaths
from ..evolution.strategy_code_generator import StrategyCodeGenerator, StrategyGenerationRequest
from .workflow_graph import WorkflowError
from .workflow_service import view_workflow

TEMPLATES = {
    "multi_script": {"title": "多脚本 · 趋势观察", "description": "双周期数据 → 因子脚本 → 风险检查 → 观察结果。示例只返回 HOLD，不下单。"},
    "script_agent": {"title": "脚本驱动 · Agent 研判", "description": "脚本采集真实市场数据并计算信号，再向策略 Agent 派发结构化研判任务。"},
    "scheduler_agent": {"title": "调度驱动 · Agent 巡检", "description": "独立调度器唤醒策略 Agent，协同市场分析和风险复核；默认不启用调度。"},
}

_INPUTS = '''"""Read configured data-source presets through the strategy SDK."""
def collect(ctx):
    presets = ctx.config.extras.get("data_sources") or []
    snapshots = {}
    for preset in presets:
        if preset.get("capability") != "candles":
            continue
        timeframe = str(preset.get("timeframe", "5m"))
        limit = min(500, max(2, int(preset.get("limit", 40))))
        for market in ctx.config.markets:
            rows = ctx.market.candles(market, timeframe=timeframe, limit=limit)
            snapshots[f"{market}:{timeframe}"] = rows
    return snapshots
'''
_SIGNALS = '''"""Deterministic features; missing observations do not become signals."""
def momentum(snapshots):
    signals = {}
    for key, rows in snapshots.items():
        if len(rows) < 2:
            continue
        first, last = float(rows[0]["close"]), float(rows[-1]["close"])
        if first > 0:
            signals[key] = last / first - 1.0
    return signals
'''
_RISK = '''"""An illustrative prefilter, additional to the SDK's mandatory risk gate."""
def review(signals):
    return {key: value for key, value in signals.items() if abs(value) <= 0.05}
'''
_MULTI = '''from nerya.strategies import StrategyContext, StrategyResult
from market_inputs import collect
from signals import momentum
from risk_rules import review


def run(ctx: StrategyContext) -> StrategyResult:
    snapshots = collect(ctx)
    signals = momentum(snapshots)
    reviewed = review(signals)
    return ctx.result.hold(
        "Architecture example: observation only, no orders",
        metadata={"signals": signals, "reviewed": reviewed, "sources": list(snapshots)},
    )
'''
_SCRIPT_AGENT = '''from nerya.strategies import StrategyContext, StrategyAgentTask
from market_inputs import collect
from signals import momentum


def run(ctx: StrategyContext) -> StrategyAgentTask:
    signals = momentum(collect(ctx))
    if not signals:
        return StrategyAgentTask.skip("No complete market observations; do not invent evidence")
    return StrategyAgentTask.dispatch(
        prompt=("Review these observed momentum features: " + str(signals)
                + ". Ask market_analyst and risk_critic for independent reviews. "
                "Report evidence, uncertainty and risk only; this example must not place orders."),
        session_key={"market": ctx.config.markets[0]},
        metadata={"signals": signals, "workflow_template": "script_agent"},
        reason="script observations ready",
    )
'''
_SCHEDULER_AGENT = '''from nerya.strategies import StrategyContext, StrategyAgentTask


def run(ctx: StrategyContext) -> StrategyAgentTask:
    return StrategyAgentTask.dispatch(
        prompt=("Scheduled market and account review for " + ", ".join(ctx.config.markets)
                + ". Use configured data capabilities. Ask market_analyst to summarize "
                "market evidence and risk_critic to review risks. Record missing data honestly. "
                "This architecture example is review-only: do not place orders or change live settings."),
        session_key={"market": ctx.config.markets[0]},
        metadata={"workflow_template": "scheduler_agent"},
        reason="scheduled review",
    )
'''


def create_workflow_template(paths: WorkspacePaths, payload: dict[str, Any]) -> dict[str, Any]:
    template = str(payload.get("template") or "")
    if template not in TEMPLATES:
        raise WorkflowError("Unknown workflow template")
    accounts, markets = payload.get("accounts"), payload.get("markets")
    if not isinstance(accounts, list) or not accounts or not all(isinstance(a, str) and a.strip() for a in accounts):
        raise WorkflowError("Select at least one paper account reference")
    if not isinstance(markets, list) or not markets or not all(isinstance(m, str) and m.strip() for m in markets):
        raise WorkflowError("At least one concrete market is required")
    info = TEMPLATES[template]
    strategy_id = str(payload.get("strategy_id") or f"wf_{template}_{uuid4().hex[:8]}")
    is_agent = template != "multi_script"
    req = StrategyGenerationRequest(
        strategy_id=strategy_id, title=str(payload.get("title") or info["title"]),
        description=info["description"], prompt=info["description"],
        strategy_class="agent" if is_agent else "trend", execution_mode="agent" if is_agent else "script",
        mode="paper", markets=tuple(markets), accounts=tuple(accounts),
        schedule_every_seconds=900 if template == "scheduler_agent" else 300,
        subagents=("market_analyst", "risk_critic") if is_agent else (),
        policy_overrides={"allow_direct_order": False, "max_single_order_usd": 25, "max_daily_notional_usd": 100},
        create_tuning=True,
        extra_subagent_prompts={
            "market_analyst": "# Market analyst\nReview observed market data. Cite timestamps and missing evidence. Do not place orders.\n",
            "risk_critic": "# Risk critic\nIndependently challenge the thesis, data quality and risk budget. Do not place orders or relax approvals.\n",
        },
    )
    generator = StrategyCodeGenerator(paths)
    files = generator.generate(req, validate=False, create_proposal_record=False).files
    manifest = yaml_io.loads(files["strategy.yml"])
    manifest["schedule"]["enabled"] = False
    manifest["tuning"]["schedule"]["enabled"] = False
    manifest["workflow_template"] = template
    manifest["data_sources"] = [
        {"id": "candles_5m", "title": "5m 行情 · 快周期", "provider": "runtime.market", "capability": "candles", "timeframe": "5m", "limit": 40, "consumers": ["market_inputs.py"] if template != "scheduler_agent" else []},
        {"id": "candles_1h", "title": "1h 行情 · 大周期", "provider": "runtime.market", "capability": "candles", "timeframe": "1h", "limit": 40, "consumers": ["market_inputs.py"] if template != "scheduler_agent" else []},
    ]
    files["strategy.yml"] = yaml_io.dumps(manifest)
    files["main.py"] = {"multi_script": _MULTI, "script_agent": _SCRIPT_AGENT, "scheduler_agent": _SCHEDULER_AGENT}[template]
    if template != "scheduler_agent":
        files.update({"market_inputs.py": _INPUTS, "signals.py": _SIGNALS})
    if template == "multi_script":
        files["risk_rules.py"] = _RISK
    result = generator.generate(replace(req, files=files), require_valid=True, initial_state="draft")
    if not result.proposal:
        return {"ok": False, "error": "template_validation_failed", "validation": result.validation.asdict() if result.validation else None}
    return {"ok": True, "strategy_id": strategy_id, "proposal_id": result.proposal.id,
            "state": "draft", "validation": result.validation.asdict() if result.validation else None,
            "workflow": view_workflow(paths, strategy_id, result.proposal.id)}
