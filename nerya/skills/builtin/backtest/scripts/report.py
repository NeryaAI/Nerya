"""Markdown report renderer."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from .....core import yaml_io
from .engine import BacktestResult


def render_report(metrics: dict[str, Any], result: BacktestResult, config_snapshot: dict[str, Any], outputs: dict[str, Path] | None = None) -> str:
    outputs = outputs or {}
    lines = [
        f"# Backtest - {result.strategy_id} - {metrics.get('start_utc')} -> {metrics.get('end_utc')}",
        f"> {metrics.get('backtest_days', 0):.2f} days · {', '.join(metrics.get('markets', []))} · {metrics.get('tf')} · initial ${metrics.get('initial_capital_usd'):,.2f}",
        "",
        "## Verdict",
        str(metrics.get("verdict") or "UNKNOWN"),
        "",
        "Assumption: v1 fills entries at current bar open and exits at next bar open; limit/stop style intents are degraded to market simulation.",
        "",
        "## Key metrics",
        "| Metric | Value |",
        "|---|---:|",
    ]
    if metrics.get("recommended_coverage_ok") is False:
        lines[8:8] = [
            "## Coverage note",
            str(
                metrics.get("coverage_message")
                or "Loaded candle coverage is below the recommended window."
            ),
            "",
        ]
    for key in [
        "total_return_pct",
        "annualized_return_pct",
        "sharpe_ratio",
        "sortino_ratio",
        "calmar_ratio",
        "max_drawdown_pct",
        "win_rate_pct",
        "profit_factor",
        "exposure_pct",
        "total_missed_profit_pct",
    ]:
        lines.append(f"| {key} | {_fmt_metric(key, metrics.get(key))} |")
    lines.extend(["", "## Trades by reason", "| Reason | N | Total notional | Fees |", "|---|---:|---:|---:|"])
    by_reason: dict[str, dict[str, float]] = {}
    for t in result.trades:
        reason = str(t.get("reason") or "unknown")
        row = by_reason.setdefault(reason, {"n": 0, "notional": 0.0, "fees": 0.0})
        row["n"] += 1
        row["notional"] += float(t.get("notional", 0.0) or 0.0)
        row["fees"] += float(t.get("fee", 0.0) or 0.0)
    for reason, row in by_reason.items():
        lines.append(f"| {reason} | {int(row['n'])} | {_usd(row['notional'])} | {_usd(row['fees'])} |")
    if not by_reason:
        lines.append("| no_trades | 0 | 0 | 0 |")
    lines.extend(["", "## Top 5 winners / losers"])
    closed = [t for t in result.trades if "pnl" in t]
    for t in sorted(closed, key=lambda r: float(r.get("pnl", 0.0)), reverse=True)[:5]:
        lines.append(f"- winner {t.get('market')} {t.get('ts')}: {_usd(t.get('pnl'))} ({_pct(t.get('pnl_pct'))})")
    for t in sorted(closed, key=lambda r: float(r.get("pnl", 0.0)))[:5]:
        lines.append(f"- loser {t.get('market')} {t.get('ts')}: {_usd(t.get('pnl'))} ({_pct(t.get('pnl_pct'))})")
    lines.extend(["", "## Drawdown episodes (>3%)"])
    for ep in metrics.get("drawdown_episodes", [])[:10]:
        lines.append(f"- {ep.get('peak_ts')} -> {ep.get('trough_ts')} dd={_pct(ep.get('dd_pct'))} recovery={ep.get('recovery_ts')}")
    if not metrics.get("drawdown_episodes"):
        lines.append("- none")
    lines.extend(["", "## Missed profit episodes"])
    for ep in metrics.get("missed_profit_episodes", [])[:5]:
        lines.append(f"- {ep.get('start_ts')} -> {ep.get('end_ts')} bench={_pct(ep.get('benchmark_pct'))} strat={_pct(ep.get('strategy_pct'))} missed={_pct(ep.get('missed_pct'))}")
    if not metrics.get("missed_profit_episodes"):
        lines.append("- none")
    lines.extend(["", "## Config snapshot", "```yaml", yaml_io.dumps(config_snapshot).strip(), "```", "", "## Outputs"])
    for name, path in outputs.items():
        lines.append(f"- {name}: `{path.name}`")
    return "\n".join(lines) + "\n"


def _fmt_metric(key: str, value: Any) -> str:
    if key.endswith("_pct"):
        return _pct(value)
    if key.endswith("_usd"):
        return _usd(value)
    return _fmt(value)


def _pct(value: Any) -> str:
    if value is None:
        return "null"
    try:
        return f"{float(value):.4f}%"
    except Exception:
        return f"{value}%"


def _usd(value: Any) -> str:
    if value is None:
        return "null"
    try:
        return f"${float(value):.4f}"
    except Exception:
        return f"${value}"


def _fmt(value: Any) -> str:
    if value is None:
        return "null"
    if isinstance(value, float):
        if value == float("inf"):
            return "inf"
        return f"{value:.4f}"
    return str(value)
