"""Strategy performance snapshots.

The self-evolution loop needs a deterministic, schema-stable view of
*how the strategy is actually doing* — derived from the same ledgers
the dashboard already reads — so the tuning subagent can ground its
proposals in numbers instead of vibes.

Inputs (read-only)
------------------
* ``runs/<run_id>.json`` written by :class:`StrategyRunStore` (statuses,
  durations, error kinds, per-tick mode).
* ``strategy_history/<strategy_id>/{orders,fills,risk,pnl,decisions,
  subagents}.jsonl`` written by :mod:`nerya.strategy_history.store`.

Outputs
-------
:class:`StrategyPerformanceSnapshot` — a single typed dict-like object
with three groups of metrics:

* **Run metrics** — total ticks, success rate, hold rate, error rate,
  median duration, last run timestamp.
* **Trade metrics** — submitted intents, filled orders, cumulative PnL,
  drawdown floor, win rate, average slippage.
* **Cost metrics** — risk rejects, subagent counts, last review timestamp.

The snapshot is intentionally *flat* and JSON-serialisable so a tuning
subagent can be prompted with ``json.dumps(snapshot.asdict(), …)``
directly.

Boundaries
----------
This module never writes anything and never calls into the runtime.
It is safe to import from the validator, dashboard, evolution loop,
or CLI.
"""

from __future__ import annotations

import logging
import math
from dataclasses import asdict, dataclass, field
from typing import Any, Iterable, Optional

from ..core.paths import WorkspacePaths
from ..core.time import now_iso
from ..strategy_history import store as history_store
from .package import StrategyPackage, load_package
from .state import StrategyRunRecord, StrategyRunStore


_LOG = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Snapshot
# ---------------------------------------------------------------------------


@dataclass
class StrategyPerformanceSnapshot:
    """Read-only metrics bundle the tuning loop / dashboard consumes."""

    strategy_id: str
    package_hash: str
    generated_at: str
    lookback_runs: int
    runs_considered: int
    run_metrics: dict[str, Any] = field(default_factory=dict)
    trade_metrics: dict[str, Any] = field(default_factory=dict)
    cost_metrics: dict[str, Any] = field(default_factory=dict)
    risk_metrics: dict[str, Any] = field(default_factory=dict)
    last_run_at: Optional[str] = None
    last_review_at: Optional[str] = None
    notes: list[str] = field(default_factory=list)

    def asdict(self) -> dict[str, Any]:
        return asdict(self)


# ---------------------------------------------------------------------------
# Builder
# ---------------------------------------------------------------------------


def build_snapshot(
    paths: WorkspacePaths,
    strategy_id: str,
    *,
    lookback_runs: int = 200,
    package: Optional[StrategyPackage] = None,
) -> StrategyPerformanceSnapshot:
    """Compose a :class:`StrategyPerformanceSnapshot` from on-disk data."""

    pkg = package
    if pkg is None:
        try:
            pkg = load_package(paths, strategy_id)
        except Exception:
            pkg = None

    runs = StrategyRunStore(paths, strategy_id).list(limit=lookback_runs)
    notes: list[str] = []

    run_metrics = _summarise_runs(runs)
    last_run_at = runs[0].finished_at if runs else None

    intents = _read(paths, strategy_id, "intents")
    orders = _read(paths, strategy_id, "orders")
    fills = _read(paths, strategy_id, "fills")
    pnls = _read(paths, strategy_id, "pnl")
    risk_rows = _read(paths, strategy_id, "risk")
    decisions = _read(paths, strategy_id, "decisions")
    reviews = _read(paths, strategy_id, "reviews")
    subagent_rows = _read(paths, strategy_id, "subagents")

    trade_metrics = _summarise_trades(intents, orders, fills, pnls)
    risk_metrics = _summarise_risk(risk_rows, decisions)
    cost_metrics = _summarise_costs(subagent_rows)

    last_review_at = None
    if reviews:
        last_review_at = (
            reviews[-1].get("ts") if isinstance(reviews[-1], dict) else None
        )

    if not runs:
        notes.append("no runs recorded yet")
    if not orders:
        notes.append("no orders recorded yet")

    return StrategyPerformanceSnapshot(
        strategy_id=strategy_id,
        package_hash=(pkg.content_hash if pkg is not None else ""),
        generated_at=now_iso(),
        lookback_runs=lookback_runs,
        runs_considered=len(runs),
        run_metrics=run_metrics,
        trade_metrics=trade_metrics,
        cost_metrics=cost_metrics,
        risk_metrics=risk_metrics,
        last_run_at=last_run_at,
        last_review_at=last_review_at,
        notes=notes,
    )


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------


def _read(
    paths: WorkspacePaths, strategy_id: str, name: str
) -> list[dict[str, Any]]:
    try:
        return list(history_store.read_ledger(paths, strategy_id, name))
    except Exception:
        _LOG.exception("read_ledger %s failed for %s", name, strategy_id)
        return []


def _summarise_runs(runs: Iterable[StrategyRunRecord]) -> dict[str, Any]:
    runs = list(runs)
    total = len(runs)
    if total == 0:
        return {
            "total": 0,
            "ok": 0,
            "hold": 0,
            "error": 0,
            "submitted": 0,
            "ok_rate": 0.0,
            "hold_rate": 0.0,
            "error_rate": 0.0,
            "median_duration_ms": 0,
            "p95_duration_ms": 0,
            "modes": {},
        }
    ok = sum(1 for r in runs if r.status == "ok")
    hold = sum(1 for r in runs if r.status == "hold")
    submitted = sum(1 for r in runs if r.status == "submitted")
    err = sum(1 for r in runs if r.status == "error")
    durations = sorted(int(r.duration_ms or 0) for r in runs)
    modes: dict[str, int] = {}
    for r in runs:
        modes[r.mode] = modes.get(r.mode, 0) + 1
    return {
        "total": total,
        "ok": ok,
        "hold": hold,
        "submitted": submitted,
        "error": err,
        "ok_rate": _safe_rate(ok, total),
        "hold_rate": _safe_rate(hold, total),
        "error_rate": _safe_rate(err, total),
        "median_duration_ms": _percentile(durations, 0.5),
        "p95_duration_ms": _percentile(durations, 0.95),
        "modes": modes,
    }


def _summarise_trades(
    intents: list[dict[str, Any]],
    orders: list[dict[str, Any]],
    fills: list[dict[str, Any]],
    pnls: list[dict[str, Any]],
) -> dict[str, Any]:
    submitted_orders = len(orders)
    filled = len(fills)
    fill_rate = _safe_rate(filled, max(1, submitted_orders))

    pnl_total = 0.0
    pnl_series: list[float] = []
    wins = losses = 0
    current_win_streak = current_loss_streak = 0
    max_win_streak = max_loss_streak = 0
    for row in pnls:
        amt = _coerce_float(_get_nested(row, "pnl", "realized_usd"))
        if amt is None:
            amt = _coerce_float(_get_nested(row, "pnl", "pnl_usd"))
        if amt is None:
            amt = _coerce_float(_get_nested(row, "pnl", "value"))
        if amt is None:
            continue
        pnl_total += amt
        pnl_series.append(pnl_total)
        if amt > 0:
            wins += 1
            current_win_streak += 1
            current_loss_streak = 0
        elif amt < 0:
            losses += 1
            current_loss_streak += 1
            current_win_streak = 0
        else:
            current_win_streak = 0
            current_loss_streak = 0
        max_win_streak = max(max_win_streak, current_win_streak)
        max_loss_streak = max(max_loss_streak, current_loss_streak)
    drawdown = _max_drawdown(pnl_series) if pnl_series else 0.0

    slippages: list[float] = []
    for row in fills:
        s = _coerce_float(_get_nested(row, "fill", "slippage_bps"))
        if s is None:
            s = _coerce_float(_get_nested(row, "fill", "slippage"))
        if s is not None:
            slippages.append(float(s))
    avg_slip = sum(slippages) / len(slippages) if slippages else 0.0

    closed = wins + losses
    win_rate = _safe_rate(wins, max(1, closed))

    return {
        "intents": len(intents),
        "orders": submitted_orders,
        "fills": filled,
        "fill_rate": fill_rate,
        "pnl_total_usd": pnl_total,
        "max_drawdown_usd": drawdown,
        "wins": wins,
        "losses": losses,
        "closed": closed,
        "win_rate": win_rate,
        "current_win_streak": current_win_streak,
        "current_loss_streak": current_loss_streak,
        "max_win_streak": max_win_streak,
        "max_loss_streak": max_loss_streak,
        "avg_slippage": avg_slip,
        "slippage_samples": len(slippages),
        "paper_live_divergence_bps": 0.0,
        "paper_live_divergence_samples": 0,
    }


def _summarise_risk(
    risk_rows: list[dict[str, Any]],
    decisions: list[dict[str, Any]],
) -> dict[str, Any]:
    rejects = 0
    blocks = 0
    for row in risk_rows:
        verdict = _get_nested(row, "risk_decision", "verdict")
        if not isinstance(verdict, str):
            continue
        v = verdict.lower()
        if v in ("reject", "rejected", "block", "blocked"):
            rejects += 1
        if v == "blocked":
            blocks += 1
    holds = 0
    for row in decisions:
        action = _get_nested(row, "decision", "action")
        if isinstance(action, str) and action.lower() == "hold":
            holds += 1
    return {
        "risk_rows": len(risk_rows),
        "risk_rejects": rejects,
        "risk_blocks": blocks,
        "decision_rows": len(decisions),
        "decision_holds": holds,
    }


def _summarise_costs(subagent_rows: list[dict[str, Any]]) -> dict[str, Any]:
    by_name: dict[str, int] = {}
    for row in subagent_rows:
        name = str(row.get("name") or "")
        if name:
            by_name[name] = by_name.get(name, 0) + 1
    return {
        "subagent_invocations": len(subagent_rows),
        "subagent_by_name": by_name,
    }


# ---------------------------------------------------------------------------
# Tiny stats helpers
# ---------------------------------------------------------------------------


def _safe_rate(num: float, den: float) -> float:
    if not den:
        return 0.0
    try:
        return round(float(num) / float(den), 4)
    except Exception:
        return 0.0


def _percentile(sorted_values: list[int], p: float) -> int:
    if not sorted_values:
        return 0
    if p <= 0:
        return sorted_values[0]
    if p >= 1:
        return sorted_values[-1]
    idx = int(math.floor(p * (len(sorted_values) - 1)))
    return int(sorted_values[idx])


def _max_drawdown(series: list[float]) -> float:
    if not series:
        return 0.0
    peak = series[0]
    worst = 0.0
    for v in series:
        if v > peak:
            peak = v
        dd = v - peak
        if dd < worst:
            worst = dd
    return float(worst)


def _coerce_float(v: Any) -> Optional[float]:
    if v is None:
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _get_nested(row: Any, *path: str) -> Any:
    cur = row
    for k in path:
        if not isinstance(cur, dict):
            return None
        cur = cur.get(k)
    return cur


__all__ = [
    "StrategyPerformanceSnapshot",
    "build_snapshot",
]
