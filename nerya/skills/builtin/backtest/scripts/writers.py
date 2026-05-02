"""CSV artifact writers for backtest results."""

from __future__ import annotations

import csv
from collections import defaultdict
from pathlib import Path
from typing import Any

from .engine import BacktestResult


def write_csv_artifacts(result: BacktestResult, out_dir: str | Path) -> dict[str, Path]:
    root = Path(out_dir)
    root.mkdir(parents=True, exist_ok=True)
    paths = {
        "ohlcv": root / "ohlcv_indicators_portfolio.csv",
        "trades": root / "trades.csv",
        "analysis": root / "analysis_by_reason.csv",
        "rejected": root / "rejected_signals.csv",
    }
    _write_rows(paths["ohlcv"], result.ohlcv_rows)
    _write_rows(paths["trades"], result.trades)
    _write_rows(paths["rejected"], result.rejected_signals)
    _write_rows(paths["analysis"], _analysis_by_reason(result.trades))
    return paths


def _write_rows(path: Path, rows: list[dict[str, Any]]) -> None:
    keys: list[str] = []
    for row in rows:
        for key in row:
            if key not in keys:
                keys.append(key)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=keys or ["empty"])
        writer.writeheader()
        for row in rows:
            writer.writerow(row)


def _analysis_by_reason(trades: list[dict[str, Any]]) -> list[dict[str, Any]]:
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for trade in trades:
        grouped[str(trade.get("reason") or "unknown")].append(trade)
    out: list[dict[str, Any]] = []
    for reason, rows in sorted(grouped.items()):
        pnl = sum(float(r.get("pnl", 0.0) or 0.0) for r in rows)
        out.append({
            "reason": reason,
            "trades": len(rows),
            "total_notional": sum(float(r.get("notional", 0.0) or 0.0) for r in rows),
            "total_fees": sum(float(r.get("fee", 0.0) or 0.0) for r in rows),
            "total_pnl": pnl,
        })
    return out

