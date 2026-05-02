"""Render dashboard chart.json from backtest artifacts."""

from __future__ import annotations

import csv
import json
from pathlib import Path
from typing import Any


def render_chart(backtest_dir: str | Path) -> dict[str, Any]:
    root = Path(backtest_dir)
    metrics = json.loads((root / "metrics.json").read_text(encoding="utf-8"))
    rows = _read_csv(root / "ohlcv_indicators_portfolio.csv")
    trades = _read_csv(root / "trades.csv")
    price_series = [
        {
            "time": int(float(r["ts"])),
            "open": float(r["open"]),
            "high": float(r["high"]),
            "low": float(r["low"]),
            "close": float(r["close"]),
        }
        for r in rows if r.get("ts") and r.get("open")
    ]
    equity = [
        {"time": int(float(r["ts"])), "value": float(r.get("equity") or 0.0)}
        for r in rows if r.get("ts") and r.get("equity") not in (None, "")
    ]
    drawdown = _drawdown_points(equity)
    rsi_key = next((k for k in rows[0].keys() if k.startswith("rsi_")), None) if rows else None
    rsi = [
        {"time": int(float(r["ts"])), "value": float(r[rsi_key])}
        for r in rows if rsi_key and r.get(rsi_key) not in (None, "")
    ]
    markers = [
        {
            "time": int(float(t.get("ts") or 0)),
            "position": "belowBar" if t.get("side") == "buy" else "aboveBar",
            "color": "#22c55e" if t.get("side") == "buy" else "#ef4444",
            "shape": "arrowUp" if t.get("side") == "buy" else "arrowDown",
            "text": str(t.get("reason") or t.get("side") or ""),
        }
        for t in trades if t.get("ts")
    ]
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
        },
        "panels": [
            {"id": "price", "type": "candlestick", "title": "Price", "series": [{"kind": "candles", "data": price_series}, {"kind": "markers", "data": markers}]},
            {"id": "equity", "type": "line", "title": "Equity vs B&H", "series": [{"kind": "line", "name": "equity", "data": equity}]},
            {"id": "drawdown", "type": "area", "title": "Drawdown %", "series": [{"kind": "area", "name": "drawdown", "data": drawdown}], "annotations": metrics.get("drawdown_episodes", [])},
            {"id": "rsi", "type": "line", "title": "RSI(14)", "series": [{"kind": "line", "name": "rsi", "data": rsi}], "guides": [{"value": 30}, {"value": 70}]},
            {"id": "missed", "type": "overlay_spans", "title": "Missed profit", "series": [], "annotations": metrics.get("missed_profit_episodes", [])},
        ],
        "summary_cards": _summary_cards(metrics),
        "tables": [
            _table("drawdown_episodes", metrics.get("drawdown_episodes", [])[:10]),
            _table("missed_profit_episodes", metrics.get("missed_profit_episodes", [])[:10]),
            _table("trades_top10", trades[:10]),
        ],
    }
    (root / "chart.json").write_text(json.dumps(chart, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    return chart


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

