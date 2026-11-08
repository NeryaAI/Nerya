"""Render dashboard chart.json from backtest artifacts.

Also returns ``chart_blocks`` — interactive ``ChartBlock`` envelopes
the agent kernel splices into the chat. We emit two:

1. Equity curve (with auto-derived drawdown overlay) via
   :func:`nerya.charting.equity_curve_from_rows`.
2. Price + trade markers as a candlestick block, showing the agent's
   entries/exits over the price action.

The legacy ``chart.json`` panel layout is preserved for the existing
backtest detail page; ``chart_blocks`` is purely additive for the
chat-side renderer.
"""

from __future__ import annotations

import csv
import json
from pathlib import Path
from typing import Any
from .chart_artifacts import market_panels, market_indicator_panels, trade_id, VISUALIZATION_REVISION

# ``nerya.charting`` is imported lazily inside ``render_chart`` — eager
# imports would form a cycle (this module is reachable from
# ``nerya.strategies.context`` during CLI bootstrap, and the charting
# package eventually pulls in ``nerya.agent.kernel``, which in turn
# imports back into ``nerya.strategies``).


def render_chart(backtest_dir: str | Path) -> dict[str, Any]:
    root = Path(backtest_dir)
    metrics = json.loads((root / "metrics.json").read_text(encoding="utf-8"))
    rows = _read_csv(root / "ohlcv_indicators_portfolio.csv")
    trades = _read_csv(root / "trades.csv")
    signals = _read_csv(root / "signals.csv")
    instrument_panels = market_panels(rows, trades, metrics, signals)
    equity_rows = _read_csv(root / "equity.csv") if (root / "equity.csv").exists() else rows
    benchmark_rows = _read_csv(root / "benchmark.csv") if (root / "benchmark.csv").exists() else []
    decisions = _read_csv(root / "decisions.csv") if (root / "decisions.csv").exists() else []
    equity = [
        {"time": int(float(r["ts"])), "value": float(r.get("equity") or 0.0)}
        for r in equity_rows if r.get("ts") and r.get("equity") not in (None, "")
    ]
    benchmark = [{"time": int(float(r["ts"])), "value": float(r["equity"])} for r in benchmark_rows if r.get("ts") and r.get("equity")]
    drawdown = _drawdown_points(equity)
    chart = {
        "schema_version": "1.0",
        "meta": {
            "strategy_id": root.parent.parent.name,
            "backtest_ts": root.name,
            "markets": metrics.get("markets", []),
            "tf": metrics.get("tf"),
            "start": metrics.get("start_utc"),
            "end": metrics.get("end_utc"),
            "initial_capital_usd": metrics.get("initial_capital_usd"),
            "evaluation_mode": metrics.get("evaluation_mode", "trading"),
            "execution_mode": metrics.get("execution_mode", "script"),
            "performance_evidence": metrics.get("performance_evidence", True),
            "accounting_currency": metrics.get("accounting_currency"),
            "replay": metrics.get("replay", {}),
            "provenance": metrics.get("provenance", {}),
            "factor_refs": metrics.get("factor_refs", []),
            "coverage_message": metrics.get("coverage_message"),
            "requested_window_days": metrics.get("requested_window_days"),
            "requested_window_complete": metrics.get("requested_window_complete"),
            "data_manifest": metrics.get("data_manifest"),
            "verdict": metrics.get("verdict"),
            "flags": metrics.get("flags", []),
            "engine_version": metrics.get("engine_version"),
            "execution_limits": metrics.get("execution_limits", {}),
            "bias_checks": metrics.get("bias_checks", {}),
            "research_checks": metrics.get("research_checks", {}),
            "visualization_revision": VISUALIZATION_REVISION,
            "market_details_source": "recorded_csv",
            "signals_recorded": (root / "signals.csv").exists(),
            "trade_count": len(trades),
            "trades_displayed": len(trades),
        },
        "panels": [
            *instrument_panels,
            {"id": "equity", "type": "line", "title": "Equity vs B&H", "series": [{"kind": "line", "name": "equity", "data": equity}, *([{"kind": "line", "name": "benchmark", "data": benchmark}] if benchmark else [])]},
            {"id": "drawdown", "type": "area", "title": "Drawdown %", "series": [{"kind": "area", "name": "drawdown", "data": drawdown}], "annotations": metrics.get("drawdown_episodes", [])},
            *market_indicator_panels(rows, instrument_panels),
            {"id": "missed", "type": "overlay_spans", "title": "Missed profit", "series": [], "annotations": metrics.get("missed_profit_episodes", [])},
        ],
        "summary_cards": _summary_cards(metrics),
        "tables": [
            _table("drawdown_episodes", metrics.get("drawdown_episodes", [])[:10]),
            _table("missed_profit_episodes", metrics.get("missed_profit_episodes", [])[:10]),
            # 明细在前端分页，不能先截取最早 500 笔，否则长回测后半段
            # 的成交明细消失，且和图上的完整成交标记不一致。
            _table("trades", [{**trade, "trade_id": trade_id(trade, index)} for index, trade in enumerate(trades)]),
            _table("signals", signals[:500]),
            _table("decisions", decisions[:500]),
            _table("order_events", _read_csv(root / "order_events.csv")[:500]),
            _table("rejected_signals", _read_csv(root / "rejected_signals.csv")[:500]),
        ],
    }
    (root / "chart.json").write_text(json.dumps(chart, ensure_ascii=False, indent=2, default=str), encoding="utf-8")

    # Build interactive chart_blocks the kernel will splice into the
    # chat. We use bulk path because backtest series are easily
    # multi-thousand points; passing them inline would burn the LLM's
    # context for nothing.
    from .....charting import (  # local import — see top-of-file note
        BulkContext,
        candle_chart_from_rows,
        equity_curve_from_rows,
    )
    from .....core.paths import WorkspacePaths
    from .....workspace.artifact_store import ArtifactStore

    workspace_root = _workspace_root_from_backtest_dir(root)
    ctx: BulkContext | None = None
    chart_path: str = "inline"
    if workspace_root is not None:
        try:
            ctx = BulkContext(artifact_store=ArtifactStore(WorkspacePaths(root=workspace_root)))
            chart_path = "bulk"
        except Exception:
            ctx = None
            chart_path = "inline"

    strategy_id = root.parent.parent.name if root.parent and root.parent.parent else "backtest"
    backtest_ts = root.name
    initial_capital = metrics.get("initial_capital_usd")

    blocks: list[dict[str, Any]] = []

    equity_block = equity_curve_from_rows(
        equity,
        title=f"{strategy_id} · Equity vs B&H · {backtest_ts}",
        skill="backtest",
        action="render_chart",
        path=chart_path,
        ctx=ctx,
        initial_capital=float(initial_capital) if initial_capital else None,
        insights=[
            f"verdict: {metrics.get('verdict', '?')}",
            f"max_drawdown_pct: {metrics.get('max_drawdown_pct', '?')}",
            f"sharpe: {metrics.get('sharpe_ratio', '?')}",
        ],
    )
    if equity_block is not None and metrics.get("performance_evidence", True):
        blocks.append(equity_block)

    for instrument_panel in instrument_panels:
        price_series = instrument_panel["series"][0]["data"]
        if not price_series:
            continue
        candle_block = candle_chart_from_rows(
            price_series,
            title=f"{strategy_id} · {instrument_panel['title']} · {backtest_ts}",
            skill="backtest",
            action="render_chart",
            path=chart_path,
            ctx=ctx,
            overlays=[{"type": "marker", "time": marker["time"],
                "position": "below" if marker["position"] == "belowBar" else "inBar" if marker["position"] == "inBar" else "above",
                "shape": {"arrowUp": "arrow_up", "arrowDown": "arrow_down", "circle": "circle"}[marker["shape"]],
                "color": marker["color"], "text": marker["text"], "tooltip": marker["reason"]}
                for marker in instrument_panel["series"][1]["data"]],
            insights=[f"recorded markers: {len(instrument_panel['series'][1]['data'])}"],
        )
        if candle_block is not None:
            candle_block["market"] = instrument_panel["market"]
            candle_block["interval"] = str(metrics.get("tf") or "")
            blocks.append(candle_block)

    if blocks:
        chart["chart_blocks"] = blocks
    return chart


def _workspace_root_from_backtest_dir(backtest_dir: Path) -> Path | None:
    """Climb the artifact path looking for ``nerya.yml``.

    Backtest artifacts live at ``<workspace>/.../backtest/<strategy>/<ts>/``;
    rather than hardcode the depth (it varies between research /
    production layouts) we walk parents looking for the workspace
    config marker. Returns ``None`` if we never find it — the caller
    degrades to inline.
    """

    cur = backtest_dir.resolve()
    parents = [cur, *cur.parents]
    # 候选目录也包含 strategies；必须先找真实 workspace，不能在 after
    # 就停止，否则 bulk 图表写进服务不会读取的伪 artifacts 目录。
    for parent in parents:
        if (parent / "nerya.yml").exists():
            return parent
    for parent in parents:
        if parent.name == "strategies" and parent.parent:
            return parent.parent
    return None


def _read_csv(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    with path.open("r", encoding="utf-8", newline="") as fh:
        return list(csv.DictReader(fh))


def _drawdown_points(equity: list[dict[str, float]]) -> list[dict[str, float]]:
    out = []
    peak = None
    for p in equity:
        value = float(p["value"])
        peak = value if peak is None else max(peak, value)
        dd = (peak - value) / peak * 100.0 if peak else 0.0
        out.append({"time": p["time"], "value": -dd})
    return out


def _summary_cards(metrics: dict[str, Any]) -> list[dict[str, Any]]:
    keys = ["verdict", "total_return_pct", "max_drawdown_pct", "sharpe_ratio", "total_missed_profit_pct"]
    cards = []
    for key in keys:
        value = metrics.get(key)
        tone = "neutral"
        if key == "verdict":
            tone = {"PASS": "positive", "WARN": "warning", "FAIL": "negative"}.get(str(value), "neutral")
        elif isinstance(value, (int, float)):
            tone = "positive" if value > 0 else "neutral"
            if "drawdown" in key or "missed" in key:
                tone = "warning" if value > 0 else "positive"
        cards.append({"label": key, "value": value, "tone": tone})
    return cards


def _table(table_id: str, rows: list[dict[str, Any]]) -> dict[str, Any]:
    columns = list(rows[0].keys()) if rows else []
    return {"id": table_id, "columns": columns, "rows": [[r.get(c) for c in columns] for r in rows]}
