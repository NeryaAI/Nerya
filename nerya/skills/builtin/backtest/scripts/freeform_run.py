"""Run strategy-local freeform research backtests.

This runner is for strategies whose evidence is not naturally expressed as
the stock OHLCV replay: meme smart-money, wallet-flow, event, social, or other
SDK-heavy research. The strategy package owns the research code; Nerya only
executes it and normalises the minimum artifact contract:

* ``equity.csv`` with ``ts``/``time`` and ``equity``/``value`` columns.
* ``trades.csv`` with trade-detail rows, or at least a header when no trades
  were produced.
"""

from __future__ import annotations

import csv
import json
import os
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .....core.config import load_config as load_workspace_config
from .....core.errors import TradingError
from .backtest_run import (
    _load_target_package,
    _metrics_display,
    _operator_summary,
    _operator_summary_text,
)


class NoFreeformBacktestScript(TradingError):
    """Raised when a strategy package has no freeform backtest entrypoint."""


SCRIPT_CANDIDATES = (
    "backtests/research_backtest.py",
    "backtests/freeform_backtest.py",
    "backtests/custom_backtest.py",
    "scripts/research_backtest.py",
    "scripts/freeform_backtest.py",
    "scripts/custom_backtest.py",
    "scripts/custom_replay.py",
)

RESULT_NAMES = (
    "freeform_backtest_result.json",
    "custom_backtest_result.json",
    "research_backtest_result.json",
    "result.json",
)

REPORT_NAMES = (
    "freeform_backtest_report.md",
    "custom_backtest_report.md",
    "research_backtest_report.md",
    "report.md",
)

EQUITY_NAMES = ("equity.csv", "equity_curve.csv", "nav.csv")
TRADES_NAMES = ("trades.csv", "trade_details.csv", "fills.csv")
TIME_KEYS = ("ts", "time", "timestamp", "date", "datetime", "t")
EQUITY_KEYS = ("equity", "value", "balance", "nav")


def has_freeform_backtest_script(strategy_root: str | Path) -> bool:
    return _find_script(Path(strategy_root)) is not None


def run_freeform_backtest(
    *,
    strategy_id: str | None = None,
    proposal_id: str | None = None,
    workspace: str | Path | None = None,
    timeout_seconds: int = 180,
) -> dict[str, Any]:
    """Execute the strategy package's freeform backtest script.

    The script is run from the strategy root with these environment variables:
    ``NERYA_BACKTEST_OUT_DIR``, ``NERYA_STRATEGY_ROOT``, and
    ``NERYA_WORKSPACE``. It can call provider SDKs and write either CSV
    artifacts directly or ``result.json`` containing ``equity_curve`` and
    ``trades`` arrays.
    """

    if bool(strategy_id) == bool(proposal_id):
        raise TradingError("exactly one of strategy_id or proposal_id is required")

    config_obj = load_workspace_config(Path(workspace).expanduser() if workspace else None)
    package = _load_target_package(config_obj.paths, strategy_id, proposal_id)
    script_path = _find_script(package.root)
    if script_path is None:
        raise NoFreeformBacktestScript(
            f"no freeform backtest script found under {package.root}"
        )

    ts_name = time.strftime("freeform_%Y%m%d_%H%M%S", time.gmtime())
    out_dir = package.root / "backtests" / ts_name
    out_dir.mkdir(parents=True, exist_ok=True)

    env = dict(os.environ)
    env["NERYA_BACKTEST_OUT_DIR"] = str(out_dir)
    env["NERYA_STRATEGY_ROOT"] = str(package.root)
    env["NERYA_WORKSPACE"] = str(config_obj.paths.root)
    env["NERYA_FREEFORM_BACKTEST"] = "1"
    env["PYTHONPATH"] = _join_pythonpath(
        [str(config_obj.paths.root), str(Path.cwd())],
        env.get("PYTHONPATH"),
    )

    started_at = time.time()
    proc = subprocess.run(
        [sys.executable, str(script_path)],
        cwd=str(package.root),
        env=env,
        input="",
        text=True,
        capture_output=True,
        timeout=timeout_seconds,
        check=False,
    )
    if proc.returncode != 0:
        raise TradingError(
            "freeform backtest script failed "
            f"({script_path}, exit={proc.returncode}): "
            f"{_tail(proc.stderr or proc.stdout)}"
        )

    fallback_roots = _fallback_artifact_roots(
        script_path=script_path,
        package_root=package.root,
        out_dir=out_dir,
    )
    payload = _read_first_json(out_dir, RESULT_NAMES)
    if not payload:
        for root in fallback_roots:
            payload = _read_first_json(root, RESULT_NAMES, min_mtime=started_at)
            if payload:
                break
    if not payload:
        payload = _read_payload_from_stdout(proc.stdout or "")
    equity_path = _find_artifact(out_dir, EQUITY_NAMES)
    trades_path = _find_artifact(out_dir, TRADES_NAMES)
    if equity_path is None:
        for root in fallback_roots:
            equity_path = _find_artifact(root, EQUITY_NAMES, min_mtime=started_at)
            if equity_path is not None:
                break
    if trades_path is None:
        for root in fallback_roots:
            trades_path = _find_artifact(root, TRADES_NAMES, min_mtime=started_at)
            if trades_path is not None:
                break

    if equity_path is None:
        equity_rows = _payload_rows(payload, ("equity_curve", "equity", "nav_curve"))
        if equity_rows:
            equity_path = out_dir / "equity.csv"
            _write_rows(equity_path, equity_rows, fallback_header=("ts", "equity"))
    if trades_path is None:
        trade_rows = _payload_rows(payload, ("trades", "trade_details", "fills"))
        trades_path = out_dir / "trades.csv"
        _write_rows(
            trades_path,
            trade_rows,
            fallback_header=("ts", "side", "price", "size", "equity", "reason"),
        )

    if equity_path is None or not equity_path.exists():
        raise TradingError(
            "freeform backtest must produce equity.csv, equity_curve.csv, "
            "or result.json with an equity_curve array"
        )
    if trades_path is None or not trades_path.exists():
        raise TradingError(
            "freeform backtest must produce trades.csv, trade_details.csv, "
            "or result.json with a trades array"
        )

    equity_rows = _read_csv(equity_path)
    trade_rows = _read_csv(trades_path)
    if not equity_rows:
        raise TradingError("freeform backtest equity file has no rows")

    equity_path = _canonicalise_csv(
        equity_path,
        out_dir / "equity.csv",
        equity_rows,
        fallback_header=("ts", "equity"),
    )
    trades_path = _canonicalise_csv(
        trades_path,
        out_dir / "trades.csv",
        trade_rows,
        fallback_header=("ts", "side", "price", "size", "equity", "reason"),
    )

    metrics = _build_metrics(
        payload=payload,
        equity_rows=equity_rows,
        trade_rows=trade_rows,
        strategy_id=str(package.manifest.strategy_id or ""),
        proposal_id=proposal_id,
        script_path=script_path,
    )
    metrics_path = out_dir / "metrics.json"
    metrics_path.write_text(
        json.dumps(metrics, ensure_ascii=False, indent=2, default=str),
        encoding="utf-8",
    )

    report_path = _find_artifact(out_dir, REPORT_NAMES)
    if report_path is None:
        report_path = out_dir / "report.md"
        report_path.write_text(_render_report(metrics), encoding="utf-8")

    chart = _render_chart(
        out_dir=out_dir,
        strategy_id=str(package.manifest.strategy_id or ""),
        backtest_ts=ts_name,
        equity_rows=equity_rows,
        trade_rows=trade_rows,
        metrics=metrics,
        workspace_root=config_obj.paths.root,
    )
    chart_path = out_dir / "chart.json"
    chart_path.write_text(
        json.dumps(chart, ensure_ascii=False, indent=2, default=str),
        encoding="utf-8",
    )

    def _ws_rel(p: Path) -> str:
        # Workspace-relative form for reply-visible informational fields so
        # absolute host paths (C:\Users\...) stop leaking into operator
        # replies. Locator fields that tools/tests resolve directly
        # (out_dir, metrics_path, equity/trades/chart paths) stay absolute.
        root = config_obj.paths.root
        return str(p.relative_to(root)) if p.is_relative_to(root) else str(p)

    result_path = out_dir / "freeform_backtest_result.json"
    result_payload = {
        "ok": True,
        "kind": "freeform_backtest",
        "replay_kind": "strategy_sdk_freeform",
        "strategy_id": package.manifest.strategy_id,
        "proposal_id": proposal_id,
        "backtest_ts": ts_name,
        "script_path": str(script_path),
        "out_dir": str(out_dir),
        "metrics_path": str(metrics_path),
        "report_path": _ws_rel(report_path),
        "equity_path": str(equity_path),
        "trades_path": str(trades_path),
        "chart_path": str(chart_path),
        "has_equity_curve": True,
        "has_trade_details": True,
        "equity_points": len(equity_rows),
        "simulated_trades": len(trade_rows),
        "total_trades": len(trade_rows),
        "total_return_pct": metrics.get("total_return_pct"),
        "max_drawdown_pct": metrics.get("max_drawdown_pct"),
        "final_equity_usd": metrics.get("final_equity_usd"),
        "coverage_ok": True,
        "recommended_coverage_ok": None,
        "coverage_message": metrics.get("coverage_message"),
        "limitations": payload.get("limitations") if isinstance(payload, dict) else [],
    }
    result_path.write_text(
        json.dumps(result_payload, ensure_ascii=False, indent=2, default=str),
        encoding="utf-8",
    )

    metrics_display = _metrics_display(metrics)
    operator_summary = _operator_summary(metrics)
    result = {
        **result_payload,
        "result_path": str(result_path),
        "strategy_root": _ws_rel(package.root),
        "strategy_yml_path": _ws_rel(package.root / "strategy.yml"),
        "strategy_md_path": _ws_rel(package.root / "strategy.md"),
        "main_path": _ws_rel(package.root / "main.py"),
        "verdict": metrics.get("verdict"),
        "metrics_display": metrics_display,
        "operator_summary": operator_summary,
        "operator_summary_text": _operator_summary_text(operator_summary),
        "metric_units": {
            "*_pct": "percentage points; display 0.15 as 0.15%, not 15%",
            "*_usd": "US dollars",
            "total_trades": "trade-detail rows emitted by the freeform script",
        },
        "metrics": metrics,
        "chart_panels": len(chart.get("panels", [])),
    }
    if chart.get("chart_blocks"):
        result["chart_blocks"] = chart["chart_blocks"]
    return result


def _find_script(strategy_root: Path) -> Path | None:
    for rel in SCRIPT_CANDIDATES:
        path = strategy_root / rel
        if path.exists() and path.is_file():
            return path
    return None


def _join_pythonpath(prefixes: list[str], existing: str | None) -> str:
    values = [p for p in prefixes if p]
    if existing:
        values.append(existing)
    return os.pathsep.join(dict.fromkeys(values))


def _tail(text: str, *, limit: int = 1200) -> str:
    text = (text or "").strip()
    if len(text) <= limit:
        return text
    return text[-limit:]


def _fallback_artifact_roots(
    *,
    script_path: Path,
    package_root: Path,
    out_dir: Path,
) -> list[Path]:
    """Common script mistakes: writing beside the script or into cwd."""

    candidates = [script_path.parent, package_root]
    out: list[Path] = []
    out_resolved = out_dir.resolve()
    for path in candidates:
        try:
            resolved = path.resolve()
        except Exception:
            continue
        if resolved == out_resolved:
            continue
        if resolved in [p.resolve() for p in out]:
            continue
        out.append(path)
    return out


def _fresh_file(path: Path, *, min_mtime: float | None = None) -> bool:
    if not path.exists() or not path.is_file():
        return False
    if min_mtime is None:
        return True
    try:
        return path.stat().st_mtime >= min_mtime - 1.0
    except OSError:
        return False


def _read_first_json(
    root: Path,
    names: tuple[str, ...],
    *,
    min_mtime: float | None = None,
) -> dict[str, Any]:
    for name in names:
        path = root / name
        if not _fresh_file(path, min_mtime=min_mtime):
            continue
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            continue
        return data if isinstance(data, dict) else {}
    return {}


def _read_payload_from_stdout(stdout: str) -> dict[str, Any]:
    """Extract a freeform result object from script stdout.

    Strategy-local scripts are validated like strategy code, so they should not
    need direct file writes just to hand a replay result back to the runner.
    Accept either a marked JSON line or a final raw JSON object.
    """

    text = str(stdout or "").strip()
    if not text:
        return {}
    marker = "NERYA_FREEFORM_RESULT_JSON="
    for line in reversed(text.splitlines()):
        stripped = line.strip()
        if not stripped:
            continue
        if stripped.startswith(marker):
            return _loads_json_object(stripped[len(marker):].strip())
    parsed = _loads_json_object(text)
    if parsed:
        return parsed
    decoder = json.JSONDecoder()
    candidates: list[dict[str, Any]] = []
    for idx, char in enumerate(text):
        if char != "{":
            continue
        try:
            data, _end = decoder.raw_decode(text[idx:])
        except Exception:
            continue
        if isinstance(data, dict):
            candidates.append(data)
    for data in reversed(candidates):
        if any(key in data for key in ("equity_curve", "equity", "nav_curve")):
            return data
    if candidates:
        return candidates[-1]
    return {}


def _loads_json_object(raw: str) -> dict[str, Any]:
    try:
        data = json.loads(raw)
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


def _find_artifact(
    root: Path,
    names: tuple[str, ...],
    *,
    min_mtime: float | None = None,
) -> Path | None:
    for name in names:
        path = root / name
        if _fresh_file(path, min_mtime=min_mtime):
            return path
    return None


def _payload_rows(payload: dict[str, Any], keys: tuple[str, ...]) -> list[dict[str, Any]]:
    for key in keys:
        value = payload.get(key)
        if isinstance(value, list):
            rows: list[dict[str, Any]] = []
            for idx, row in enumerate(value):
                if isinstance(row, dict):
                    rows.append(dict(row))
                elif key in {"equity_curve", "equity", "nav_curve"}:
                    rows.append({"ts": idx, "equity": row})
            if rows:
                return rows
    return []


def _read_csv(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    with path.open("r", encoding="utf-8", newline="") as fh:
        return list(csv.DictReader(fh))


def _write_rows(
    path: Path,
    rows: list[dict[str, Any]],
    *,
    fallback_header: tuple[str, ...],
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fieldnames = _fieldnames(rows) or list(fallback_header)
    with path.open("w", encoding="utf-8", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=fieldnames)
        writer.writeheader()
        for row in rows:
            writer.writerow({key: row.get(key, "") for key in fieldnames})


def _canonicalise_csv(
    source: Path,
    target: Path,
    rows: list[dict[str, Any]],
    *,
    fallback_header: tuple[str, ...],
) -> Path:
    if source.resolve() == target.resolve():
        return target
    if rows:
        _write_rows(target, rows, fallback_header=fallback_header)
    else:
        target.write_text(source.read_text(encoding="utf-8"), encoding="utf-8")
    return target


def _fieldnames(rows: list[dict[str, Any]]) -> list[str]:
    out: list[str] = []
    for row in rows:
        for key in row:
            if key not in out:
                out.append(key)
    return out


def _build_metrics(
    *,
    payload: dict[str, Any],
    equity_rows: list[dict[str, Any]],
    trade_rows: list[dict[str, Any]],
    strategy_id: str,
    proposal_id: str | None,
    script_path: Path,
) -> dict[str, Any]:
    first_value = _first_float(equity_rows[0], EQUITY_KEYS)
    last_value = _first_float(equity_rows[-1], EQUITY_KEYS)
    initial = _float_or_none(payload.get("initial_capital_usd")) or first_value or 0.0
    final = _float_or_none(payload.get("final_equity_usd")) or last_value or initial
    total_return = (
        _float_or_none(payload.get("total_return_pct"))
        if payload.get("total_return_pct") is not None
        else ((final - initial) / initial * 100.0 if initial else 0.0)
    )
    max_dd = _float_or_none(payload.get("max_drawdown_pct"))
    if max_dd is None:
        max_dd = _max_drawdown_pct(equity_rows)
    start_ts = _first_time(equity_rows[0])
    end_ts = _first_time(equity_rows[-1])
    days = ((end_ts - start_ts) / 86400.0) if start_ts and end_ts and end_ts > start_ts else 0.0
    return {
        "backtest_kind": "freeform_backtest",
        "verdict": str(payload.get("verdict") or "RESEARCH"),
        "strategy_id": strategy_id,
        "proposal_id": proposal_id,
        "script_path": str(script_path),
        "initial_capital_usd": initial,
        "final_equity_usd": final,
        "total_return_pct": total_return,
        "total_return_usd": final - initial,
        "benchmark_buy_hold_return_pct": payload.get("benchmark_buy_hold_return_pct"),
        "alpha_vs_benchmark_pct": payload.get("alpha_vs_benchmark_pct"),
        "max_drawdown_pct": max_dd,
        "sharpe_ratio": payload.get("sharpe_ratio"),
        "profit_factor": payload.get("profit_factor"),
        "win_rate_pct": payload.get("win_rate_pct"),
        "total_trades": len(trade_rows),
        "equity_points": len(equity_rows),
        "backtest_days": round(days, 6),
        "start_utc": _iso_utc(start_ts) if start_ts else None,
        "end_utc": _iso_utc(end_ts) if end_ts else None,
        "coverage_ok": True,
        "recommended_coverage_ok": None,
        "coverage_message": (
            "Freeform SDK backtest accepted: the strategy-local script emitted "
            "a capital curve and trade details; no fixed OHLCV timeframe or "
            "minimum window was enforced."
        ),
        "flags": list(payload.get("flags") or []),
    }


def _float_or_none(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _first_float(row: dict[str, Any], keys: tuple[str, ...]) -> float | None:
    for key in keys:
        value = _float_or_none(row.get(key))
        if value is not None:
            return value
    return None


def _first_time(row: dict[str, Any]) -> int | None:
    for key in TIME_KEYS:
        if key in row:
            return _coerce_unix_seconds(row.get(key))
    return None


def _coerce_unix_seconds(value: Any) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        raw = float(value)
        if raw > 1e12:
            return int(raw / 1000.0)
        if raw > 0:
            return int(raw)
        return None
    text = str(value).strip()
    if not text:
        return None
    try:
        return _coerce_unix_seconds(float(text))
    except ValueError:
        pass
    for fmt in (
        "%Y-%m-%dT%H:%M:%S.%f%z",
        "%Y-%m-%dT%H:%M:%S%z",
        "%Y-%m-%dT%H:%M:%S.%f",
        "%Y-%m-%dT%H:%M:%S",
        "%Y-%m-%d %H:%M:%S",
        "%Y-%m-%d",
    ):
        try:
            dt = datetime.strptime(text.replace("Z", "+0000"), fmt)
        except ValueError:
            continue
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return int(dt.timestamp())
    return None


def _iso_utc(ts: int) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat()


def _max_drawdown_pct(equity_rows: list[dict[str, Any]]) -> float:
    peak: float | None = None
    max_dd = 0.0
    for row in equity_rows:
        value = _first_float(row, EQUITY_KEYS)
        if value is None:
            continue
        peak = value if peak is None else max(peak, value)
        if peak:
            max_dd = max(max_dd, (peak - value) / peak * 100.0)
    return max_dd


def _render_report(metrics: dict[str, Any]) -> str:
    return "\n".join(
        [
            "# Freeform SDK Backtest",
            "",
            f"- verdict: {metrics.get('verdict')}",
            f"- total_return_pct: {metrics.get('total_return_pct')}",
            f"- max_drawdown_pct: {metrics.get('max_drawdown_pct')}",
            f"- total_trades: {metrics.get('total_trades')}",
            f"- coverage: {metrics.get('coverage_message')}",
            "",
            "This is not a stock OHLCV-template replay. The strategy-local "
            "script supplied the capital curve and trade details.",
        ]
    )


def _render_chart(
    *,
    out_dir: Path,
    strategy_id: str,
    backtest_ts: str,
    equity_rows: list[dict[str, Any]],
    trade_rows: list[dict[str, Any]],
    metrics: dict[str, Any],
    workspace_root: Path,
) -> dict[str, Any]:
    equity = [
        {"time": ts, "value": value}
        for row in equity_rows
        if (ts := _first_time(row)) is not None
        if (value := _first_float(row, EQUITY_KEYS)) is not None
    ]
    chart = {
        "schema_version": "1.0",
        "meta": {
            "strategy_id": strategy_id,
            "backtest_ts": backtest_ts,
            "kind": "freeform_backtest",
            "start": metrics.get("start_utc"),
            "end": metrics.get("end_utc"),
            "initial_capital_usd": metrics.get("initial_capital_usd"),
        },
        "panels": [
            {
                "id": "equity",
                "type": "line",
                "title": "Equity",
                "series": [{"kind": "line", "name": "equity", "data": equity}],
            }
        ],
        "summary_cards": [
            {"label": "verdict", "value": metrics.get("verdict"), "tone": "neutral"},
            {"label": "total_return_pct", "value": metrics.get("total_return_pct"), "tone": "neutral"},
            {"label": "max_drawdown_pct", "value": metrics.get("max_drawdown_pct"), "tone": "warning"},
            {"label": "total_trades", "value": metrics.get("total_trades"), "tone": "neutral"},
        ],
        "tables": [_table("trades", trade_rows[:100])],
    }
    block = _build_equity_chart_block(
        workspace_root=workspace_root,
        out_dir=out_dir,
        strategy_id=strategy_id,
        backtest_ts=backtest_ts,
        equity_rows=equity_rows,
        metrics=metrics,
    )
    if block is not None:
        chart["chart_blocks"] = [block]
    return chart


def _build_equity_chart_block(
    *,
    workspace_root: Path,
    out_dir: Path,
    strategy_id: str,
    backtest_ts: str,
    equity_rows: list[dict[str, Any]],
    metrics: dict[str, Any],
) -> dict[str, Any] | None:
    try:
        from .....charting import BulkContext, equity_curve_from_rows
        from .....core.paths import WorkspacePaths
        from .....workspace.artifact_store import ArtifactStore

        ctx = None
        path = "inline"
        if (workspace_root / "nerya.yml").exists():
            ctx = BulkContext(artifact_store=ArtifactStore(WorkspacePaths(root=workspace_root)))
            path = "bulk"
        return equity_curve_from_rows(
            equity_rows,
            title=f"{strategy_id} · Freeform equity · {backtest_ts}",
            skill="backtest",
            action="freeform_run",
            path=path,
            ctx=ctx,
            initial_capital=_float_or_none(metrics.get("initial_capital_usd")),
            insights=[
                "kind: freeform_sdk_backtest",
                f"trades: {metrics.get('total_trades')}",
            ],
            as_of=str(out_dir),
        )
    except Exception:
        return None


def _table(table_id: str, rows: list[dict[str, Any]]) -> dict[str, Any]:
    columns = _fieldnames(rows)
    return {
        "id": table_id,
        "columns": columns,
        "rows": [[row.get(column) for column in columns] for row in rows],
    }


__all__ = [
    "NoFreeformBacktestScript",
    "has_freeform_backtest_script",
    "run_freeform_backtest",
]
