"""Markdown report renderer."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from .....core import yaml_io
from .engine import BacktestResult


def render_report(metrics: dict[str, Any], result: BacktestResult, config_snapshot: dict[str, Any], outputs: dict[str, Path] | None = None) -> str:
    outputs = outputs or {}
    # The public report needs modes, not potentially private stub responses.
    config_snapshot = {**config_snapshot, "mock_surfaces": {
        name: {**surface, "payload": "[omitted]"} if "payload" in surface else surface
        for name, surface in config_snapshot.get("mock_surfaces", {}).items()
    }}
    pool_simulation = (metrics.get("replay") or {}).get("scope") == "historical_pool_price_simulation"
    currency = (metrics.get("accounting_currency") or "quote unit not recorded") if pool_simulation else None
    initial = f"{metrics.get('initial_capital_usd'):,.2f} {currency}" if pool_simulation else f"${metrics.get('initial_capital_usd'):,.2f}"
    lines = [
        f"# Backtest - {result.strategy_id} - {metrics.get('start_utc')} -> {metrics.get('end_utc')}",
        f"> {metrics.get('backtest_days', 0):.2f} days · {', '.join(metrics.get('markets', []))} · {metrics.get('tf')} · initial {initial}",
        "",
        "## Verdict",
        str(metrics.get("verdict") or "UNKNOWN"),
        "",
        "Assumption: signals use closed bars; orders are queued and settle at the next available bar open. At data end, strategy intents without a next bar are rejected; existing positions are liquidated separately at the last close. Unsupported order types are rejected, never silently converted. Missing market marks carry the last known close, not zero. The benchmark uses fixed equal-weight allocations.",
        "",
        "## Key metrics",
        "| Metric | Value |",
        "|---|---:|",
    ]
    if pool_simulation:
        lines[8:8] = [
            "## Pool-price simulation provenance",
            f"Accounting currency: {currency}. Legacy `*_usd` engineering fields contain quote-unit amounts; no USD conversion is implied. "
            "The execution target and observed pool/token/quote source are distinct. Fills and costs are modeled assumptions, not historical target-venue routes, fees or fills.",
            "Producer `provenance.price_simulation` (target, source quote and assumptions as recorded):",
            "```yaml",
            yaml_io.dumps((metrics.get("provenance") or {}).get("price_simulation") or {"status": "not recorded"}).strip(),
            "```",
            "",
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
    if metrics.get("performance_evidence") is False:
        lines[8:8] = [
            "## Evidence limitation",
            "`performance_evidence: false`. This run is not trading-performance evidence. "
            "Returns, fills and verdict below describe the replay simulation only; "
            "they do not establish real AI performance or authorize bypassing strategy gates.",
            "",
        ]
    if metrics.get("decision_replay"):
        proof = metrics["decision_replay"]
        lines[8:8] = ["## Current-model retrospective replay",
            f"Host-verified decision receipts: {proof.get('decision_count', 0)}. "
            "The current model was evaluated on historical prefixes; these are not decisions recorded at that historical time.",
            *["- " + str(value) for value in proof.get("limitations", [])], ""]
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
    evidence = metrics.get("replay") or {}
    if evidence.get("mock_surface_calls"):
        lines.extend(["", "## Mock surface calls",
                      "Only actual calls are counted. Stub payloads are simulated inputs, not historical model/news evidence.",
                      "| Surface | Mode | Count |", "|---|---|---:|"])
        for call in evidence["mock_surface_calls"]:
            lines.append(f"| {call['surface']} | {call['mode']} | {call['count']} |")
    lines.extend(["", "## Order execution evidence", "A strategy status of ok is not an order or a fill.",
                  "| Stage | Count |", "|---|---:|"])
    for key in ("order_attempts", "orders_submitted", "orders_filled", "orders_rejected", "sdk_errors", "forced_closes", "ok_without_orders"):
        lines.append(f"| {key} | {evidence.get(key, 'not recorded')} |")
    for reason, count in evidence.get("rejection_reasons", {}).items():
        lines.append(f"- rejection {reason}: {count}")
    bias = metrics.get("bias_checks") or {}
    research = metrics.get("research_checks") or {}
    if bias or research:
        lines.extend(["", "## Research evidence (this run only)",
                      "Static scanning is not a dynamic causality proof. Economic PASS is not out-of-sample validation.",
                      "| Check | Recorded status |", "|---|---|"])
        lines.append(f"| Static temporal scan | {bias.get('static_temporal_scan', 'not recorded')} |")
        for check in research.get("checks", []):
            lines.append(f"| {check['id']} | {check['status']} |")
        for warning in bias.get("static_warnings", []):
            lines.append(f"- Review {warning.get('file', '')}:{warning.get('line', '')}: {warning.get('message', warning.get('code', ''))}")
    limits = ((metrics.get("provenance") or {}).get("assumptions") or {}).get("execution_model_limits") or {}
    if limits:
        lines.extend(["", "## Execution-model limitations", "Unmodeled is not zero cost or verified execution."])
        for key, value in limits.items():
            lines.append(f"- {key}: {value}")
    lines.extend(["", "## Trades by reason", "| Reason | N | Total notional | Fees |", "|---|---:|---:|---:|"])
    by_reason: dict[str, dict[str, float]] = {}
    for t in result.trades:
        reason = str(t.get("reason") or "unknown")
        row = by_reason.setdefault(reason, {"n": 0, "notional": 0.0, "fees": 0.0})
        row["n"] += 1
        row["notional"] += float(t.get("notional", 0.0) or 0.0)
        row["fees"] += float(t.get("fee", 0.0) or 0.0)
    for reason, row in by_reason.items():
        lines.append(f"| {reason} | {int(row['n'])} | {_usd(row['notional'], currency)} | {_usd(row['fees'], currency)} |")
    if not by_reason:
        lines.append("| no_trades | 0 | 0 | 0 |")
    lines.extend(["", "## Top 5 winners / losers"])
    closed = [t for t in result.trades if "pnl" in t]
    for t in sorted(closed, key=lambda r: float(r.get("pnl", 0.0)), reverse=True)[:5]:
        lines.append(f"- winner {t.get('market')} {t.get('ts')}: {_usd(t.get('pnl'), currency)} ({_pct(t.get('pnl_pct'))})")
    for t in sorted(closed, key=lambda r: float(r.get("pnl", 0.0)))[:5]:
        lines.append(f"- loser {t.get('market')} {t.get('ts')}: {_usd(t.get('pnl'), currency)} ({_pct(t.get('pnl_pct'))})")
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


def _usd(value: Any, currency: str | None = None) -> str:
    if value is None:
        return "null"
    try:
        amount = f"{float(value):.4f}"
    except Exception:
        amount = str(value)
    return f"{amount} {currency}" if currency else f"${amount}"


def _fmt(value: Any) -> str:
    if value is None:
        return "null"
    if isinstance(value, float):
        if value == float("inf"):
            return "inf"
        return f"{value:.4f}"
    return str(value)
