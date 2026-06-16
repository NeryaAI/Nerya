"""Backtest before/after summaries for evolution proposals.

This module intentionally reads existing backtest artifacts instead of running
new backtests. Execution belongs to the backtest skill / validation harness;
the evolution UI needs a small, deterministic reducer that can tell operators
whether a proposal has comparable before/after evidence.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from ..core.paths import WorkspacePaths


_NUMERIC_METRICS = (
    "total_return_pct",
    "benchmark_buy_hold_return_pct",
    "alpha_vs_benchmark_pct",
    "max_drawdown_pct",
    "sharpe_ratio",
    "profit_factor",
    "win_rate_pct",
    "exposure_pct",
    "total_trades",
    "backtest_days",
    "bars_total",
    "bars_traded",
    "total_fees_usd",
    "total_slippage_usd",
)
_LOWER_IS_BETTER = {
    "max_drawdown_pct",
    "total_fees_usd",
    "total_slippage_usd",
}
_DISPLAY_METRICS = (
    "verdict",
    "total_return_pct",
    "alpha_vs_benchmark_pct",
    "max_drawdown_pct",
    "sharpe_ratio",
    "profit_factor",
    "win_rate_pct",
    "total_trades",
    "backtest_days",
    "tf",
    "coverage_ok",
    "coverage_message",
)


def proposal_backtest_comparison(
    paths: WorkspacePaths,
    proposal: dict[str, Any],
) -> dict[str, Any] | None:
    """Return latest workspace-vs-proposal backtest comparison, if relevant."""

    proposal_path = Path(str(proposal.get("path") or ""))
    strategy_id = _proposal_strategy_id(proposal_path, proposal)
    if not strategy_id:
        return None

    before = _latest_backtest(paths.strategy(strategy_id) / "backtests")
    after = _latest_backtest(
        proposal_path / "after" / "strategies" / strategy_id / "backtests"
    )
    if after is None:
        # Some older strategy package proposals may not carry metadata and may
        # have one strategy dir under after/strategies. Use it as a fallback.
        after = _latest_backtest(_first_after_strategy_backtests(proposal_path))

    status = _comparison_status(before, after)
    deltas = _metric_deltas(before.get("metrics") if before else None, after.get("metrics") if after else None)
    summary = _summary(status, before, after, deltas)
    return {
        "strategy_id": strategy_id,
        "status": status,
        "summary": summary,
        "before": before,
        "after": after,
        "metrics_delta": deltas,
        "evidence_refs": _evidence_refs(proposal, before, after),
    }


def _proposal_strategy_id(proposal_path: Path, proposal: dict[str, Any]) -> str | None:
    metadata = proposal.get("metadata") if isinstance(proposal.get("metadata"), dict) else {}
    direct = proposal.get("strategy_id") or metadata.get("strategy_id")
    if direct:
        return str(direct)
    target = str(proposal.get("target") or "")
    parts = target.replace("\\", "/").split("/")
    if "strategies" in parts:
        idx = parts.index("strategies")
        if idx + 1 < len(parts):
            return parts[idx + 1]
    after_strategies = proposal_path / "after" / "strategies"
    if after_strategies.exists():
        candidates = sorted(p.name for p in after_strategies.iterdir() if p.is_dir())
        if len(candidates) == 1:
            return candidates[0]
    return None


def _first_after_strategy_backtests(proposal_path: Path) -> Path:
    after_strategies = proposal_path / "after" / "strategies"
    if not after_strategies.exists():
        return proposal_path / "__missing_backtests__"
    candidates = sorted(p for p in after_strategies.iterdir() if p.is_dir())
    if not candidates:
        return proposal_path / "__missing_backtests__"
    return candidates[0] / "backtests"


def _latest_backtest(root: Path) -> dict[str, Any] | None:
    if not root.exists() or not root.is_dir():
        return None
    metrics_paths = sorted(
        (p for p in root.glob("*/metrics.json") if p.is_file()),
        key=lambda p: (p.parent.name, str(p)),
    )
    if not metrics_paths:
        return None
    metrics_path = metrics_paths[-1]
    try:
        metrics = json.loads(metrics_path.read_text(encoding="utf-8"))
    except Exception:
        return None
    report_path = metrics_path.parent / "report.md"
    chart_path = metrics_path.parent / "chart.json"
    return {
        "backtest_id": metrics_path.parent.name,
        "metrics_path": str(metrics_path),
        "report_path": str(report_path) if report_path.exists() else None,
        "chart_path": str(chart_path) if chart_path.exists() else None,
        "metrics": _display_metrics(metrics),
    }


def _display_metrics(metrics: dict[str, Any]) -> dict[str, Any]:
    return {key: metrics.get(key) for key in _DISPLAY_METRICS if key in metrics}


def _comparison_status(before: dict[str, Any] | None, after: dict[str, Any] | None) -> str:
    if before and after:
        return "complete"
    if before:
        return "missing_after"
    if after:
        return "missing_before"
    return "missing_both"


def _metric_deltas(
    before: dict[str, Any] | None,
    after: dict[str, Any] | None,
) -> list[dict[str, Any]]:
    if not isinstance(before, dict) or not isinstance(after, dict):
        return []
    rows: list[dict[str, Any]] = []
    for key in _NUMERIC_METRICS:
        if key not in before or key not in after:
            continue
        b = _number(before.get(key))
        a = _number(after.get(key))
        if b is None or a is None:
            continue
        delta = a - b
        rows.append({
            "key": key,
            "before": b,
            "after": a,
            "delta": delta,
            "direction": _direction(key, delta),
        })
    return rows


def _number(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _direction(key: str, delta: float) -> str:
    if abs(delta) < 1e-12:
        return "flat"
    improved = delta < 0 if key in _LOWER_IS_BETTER else delta > 0
    return "improved" if improved else "regressed"


def _summary(
    status: str,
    before: dict[str, Any] | None,
    after: dict[str, Any] | None,
    deltas: list[dict[str, Any]],
) -> str:
    if status == "missing_both":
        return "No before or after backtest artifacts were found."
    if status == "missing_before":
        return "After backtest exists, but no workspace baseline backtest was found."
    if status == "missing_after":
        return "Workspace baseline backtest exists, but no proposal backtest was found."
    verdict_before = str(((before or {}).get("metrics") or {}).get("verdict") or "unknown")
    verdict_after = str(((after or {}).get("metrics") or {}).get("verdict") or "unknown")
    improved = sum(1 for row in deltas if row.get("direction") == "improved")
    regressed = sum(1 for row in deltas if row.get("direction") == "regressed")
    return (
        f"Backtest comparison available: verdict {verdict_before} -> {verdict_after}; "
        f"{improved} metric(s) improved, {regressed} regressed."
    )


def _evidence_refs(
    proposal: dict[str, Any],
    before: dict[str, Any] | None,
    after: dict[str, Any] | None,
) -> list[str]:
    refs = [f"proposal:{proposal.get('id')}"] if proposal.get("id") else []
    for row in (before, after):
        path = str((row or {}).get("metrics_path") or "")
        if path:
            refs.append(f"file:{path}")
    return refs


__all__ = ["proposal_backtest_comparison"]
