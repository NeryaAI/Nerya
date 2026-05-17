"""CLI entrypoint for the built-in backtest skill."""

from __future__ import annotations

import argparse
import json
import re
import time
from pathlib import Path
from typing import Any

from .....core import yaml_io
from .....core.config import load_config as load_workspace_config
from .....core.errors import TradingError
from .....evolution.patch_proposal import list_proposals
from .....strategies.package import StrategyPackage, load_package, load_package_from_dir
from .config import load_config
from .data_cache import _tf_seconds, get_candles
from .engine import run_backtest
from .metrics import assemble_metrics
from .render_chart import render_chart
from .report import render_report
from .writers import write_csv_artifacts


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run a Nerya strategy backtest")
    target = parser.add_mutually_exclusive_group(required=True)
    target.add_argument("--strategy-id")
    target.add_argument("--proposal-id")
    parser.add_argument("--preset", default="default")
    parser.add_argument("--config")
    parser.add_argument("--workspace")
    parser.add_argument("--allow-mock", action="store_true")
    args = parser.parse_args(argv)

    print(json.dumps(run_strategy_backtest(
        strategy_id=args.strategy_id,
        proposal_id=args.proposal_id,
        preset=args.preset,
        config_path=args.config,
        workspace=args.workspace,
        allow_mock=args.allow_mock,
    ), ensure_ascii=False, default=str))
    return 0


def run_strategy_backtest(
    *,
    strategy_id: str | None = None,
    proposal_id: str | None = None,
    preset: str = "default",
    config_path: str | Path | None = None,
    workspace: str | Path | None = None,
    allow_mock: bool = False,
) -> dict[str, Any]:
    if bool(strategy_id) == bool(proposal_id):
        raise TradingError("exactly one of strategy_id or proposal_id is required")

    config_obj = load_workspace_config(Path(workspace).expanduser() if workspace else None)
    package = _load_target_package(config_obj.paths, strategy_id, proposal_id)
    cfg = load_config(
        preset=preset,
        config_path=config_path,
        markets=list(package.manifest.markets),
    )
    discovered_timeframes = _discover_strategy_timeframes(package.root)
    if discovered_timeframes:
        if not config_path:
            # Prefer the strategy-declared cadence over the generic preset.
            # The previous "smallest timeframe wins" rule forced daily
            # Agent Team strategies onto 1h Yahoo data, which often cannot
            # cover the now-default >1 month replay window.
            cfg.tf = discovered_timeframes[0]
            cfg.timeframes = _unique([cfg.tf, *discovered_timeframes])
        else:
            cfg.timeframes = _unique([*discovered_timeframes, cfg.tf, *cfg.timeframes])
    now = int(time.time())
    start = now - (cfg.window_days * 86400) - (cfg.warmup_bars * _tf_seconds(cfg.tf))
    cache_root = Path(cfg.cache_root) if cfg.cache_root else config_obj.paths.artifacts / "backtest_cache"
    timeframe_candles_by_market = {
        market: {
            tf: get_candles(market, tf, start, now, cache_root, allow_mock=allow_mock)
            for tf in cfg.timeframes
        }
        for market in cfg.markets
    }
    candles_by_market = {market: by_tf[cfg.tf] for market, by_tf in timeframe_candles_by_market.items()}
    ts_name = time.strftime("%Y%m%d_%H%M%S", time.gmtime())
    out_dir = package.root / "backtests" / ts_name
    out_dir.mkdir(parents=True, exist_ok=True)
    yaml_io.dump(out_dir / "config.yml", cfg.asdict())
    result = run_backtest(
        package.root,
        cfg,
        candles_by_market=candles_by_market,
        timeframe_candles_by_market=timeframe_candles_by_market,
        artefacts_dir=out_dir,
        strategy_config=package.manifest.asdict(),
    )
    csvs = write_csv_artifacts(result, out_dir)
    metrics = assemble_metrics(result)
    _apply_coverage_gate(metrics, cfg)
    metrics_path = out_dir / "metrics.json"
    metrics_path.write_text(json.dumps(metrics, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    outputs: dict[str, Path] = dict(csvs)
    outputs["metrics"] = metrics_path
    report = render_report(metrics, result, cfg.asdict(), outputs)
    report_path = out_dir / "report.md"
    report_path.write_text(report, encoding="utf-8")
    chart = render_chart(out_dir)
    outputs["chart"] = out_dir / "chart.json"
    # Auto-ingest a Trading Evidence Vault row so the operator can cite
    # this backtest later. Honors ``runtime.evidence_vault`` and never
    # raises.
    try:
        from .....evidence import autoingest as _evidence_autoingest

        class _ConfigClient:
            __slots__ = ("config",)

            def __init__(self, cfg) -> None:
                self.config = cfg

        _evidence_autoingest.on_backtest_finalize(
            _ConfigClient(config_obj),
            strategy_id=str(package.manifest.strategy_id or ""),
            backtest_id=str(ts_name),
            metrics=metrics or {},
            window=str(metrics.get("start_utc") or "")
            + ".."
            + str(metrics.get("end_utc") or ""),
            symbols=list(cfg.markets) if getattr(cfg, "markets", None) else None,
            artifact_refs=[
                str(metrics_path.relative_to(config_obj.paths.root))
                if metrics_path.is_relative_to(config_obj.paths.root)
                else str(metrics_path),
                str(report_path.relative_to(config_obj.paths.root))
                if report_path.is_relative_to(config_obj.paths.root)
                else str(report_path),
            ],
        )
    except Exception:  # pragma: no cover - defensive
        pass
    metric_keys = (
        "total_trades",
        "backtest_days",
        "bars_total",
        "bars_traded",
        "total_fees_usd",
        "total_slippage_usd",
        "total_return_pct",
        "benchmark_buy_hold_return_pct",
        "alpha_vs_benchmark_pct",
        "max_drawdown_pct",
        "sharpe_ratio",
        "profit_factor",
        "win_rate_pct",
        "exposure_pct",
        "tf",
        "markets",
        "start_utc",
        "end_utc",
        "verdict",
    )
    metrics_display = _metrics_display(metrics)
    operator_summary = _operator_summary(metrics)
    return {
        "ok": True,
        "operator_summary_text": _operator_summary_text(operator_summary),
        "operator_summary": operator_summary,
        "metrics_display": metrics_display,
        "unit_warning": (
            "Raw *_pct values are already percentage points. Do not multiply "
            "them by 100; e.g. 0.0274 is 0.0274%, not 2.74%."
        ),
        "strategy_id": package.manifest.strategy_id,
        "proposal_id": proposal_id,
        "backtest_ts": ts_name,
        "strategy_root": str(package.root),
        "strategy_yml_path": str(package.root / "strategy.yml"),
        "strategy_md_path": str(package.root / "strategy.md"),
        "main_path": str(package.root / "main.py"),
        "out_dir": str(out_dir),
        "metrics_path": str(metrics_path),
        "report_path": str(report_path),
        "trades_path": str(outputs["trades"]),
        "config_path": str(out_dir / "config.yml"),
        "verdict": metrics.get("verdict"),
        "total_return_pct": metrics.get("total_return_pct"),
        "max_drawdown_pct": metrics.get("max_drawdown_pct"),
        "sharpe_ratio": metrics.get("sharpe_ratio"),
        "coverage_ok": metrics.get("coverage_ok"),
        "coverage_message": metrics.get("coverage_message"),
        "metric_units": {
            "*_pct": "percentage points; display 0.15 as 0.15%, not 15%",
            "*_usd": "US dollars",
            "total_trades": "closed trade pairs",
            "backtest_days": "calendar days covered by loaded candles",
        },
        "metrics": {key: metrics.get(key) for key in metric_keys if key in metrics},
        "chart_panels": len(chart.get("panels", [])),
    }


def _apply_coverage_gate(metrics: dict[str, Any], cfg: Any) -> None:
    min_days = float(getattr(cfg, "min_backtest_days", 30) or 0)
    try:
        actual_days = float(metrics.get("backtest_days") or 0)
    except Exception:
        actual_days = 0.0
    coverage_ok = actual_days >= min_days
    metrics["coverage_ok"] = coverage_ok
    metrics["min_backtest_days"] = min_days
    if coverage_ok:
        metrics["coverage_message"] = (
            f"Loaded candle coverage {actual_days:.2f}d meets "
            f"minimum {min_days:.2f}d."
        )
        return

    flags = metrics.get("flags")
    if not isinstance(flags, list):
        flags = []
    if "insufficient_backtest_window" not in flags:
        flags.append("insufficient_backtest_window")
    metrics["flags"] = flags
    metrics["verdict"] = "FAIL"
    metrics["coverage_message"] = (
        f"Loaded candle coverage {actual_days:.2f}d is below "
        f"minimum {min_days:.2f}d; do not treat this as a valid "
        "one-month-plus backtest."
    )


def _metrics_display(metrics: dict[str, Any]) -> dict[str, str]:
    def number(key: str) -> float | None:
        try:
            value = metrics.get(key)
            if value is None:
                return None
            return float(value)
        except Exception:
            return None

    def pct(key: str, digits: int = 4) -> str | None:
        value = number(key)
        if value is None:
            return None
        return f"{value:.{digits}f}%"

    def usd(key: str, digits: int = 4) -> str | None:
        value = number(key)
        if value is None:
            return None
        return f"${value:.{digits}f}"

    display: dict[str, str] = {}
    for key, value in (
        ("total_return_pct", pct("total_return_pct")),
        ("benchmark_buy_hold_return_pct", pct("benchmark_buy_hold_return_pct")),
        ("alpha_vs_benchmark_pct", pct("alpha_vs_benchmark_pct")),
        ("max_drawdown_pct", pct("max_drawdown_pct")),
        ("win_rate_pct", pct("win_rate_pct", digits=2)),
        ("exposure_pct", pct("exposure_pct", digits=2)),
        ("total_fees_usd", usd("total_fees_usd")),
        ("total_slippage_usd", usd("total_slippage_usd")),
    ):
        if value is not None:
            display[key] = value
    for key in ("total_trades", "backtest_days", "bars_total", "bars_traded", "sharpe_ratio", "profit_factor"):
        value = metrics.get(key)
        if value is not None:
            display[key] = str(value)
    verdict = metrics.get("verdict")
    if verdict is not None:
        display["verdict"] = str(verdict)
    coverage_message = metrics.get("coverage_message")
    if coverage_message:
        display["coverage_message"] = str(coverage_message)
    return display


def _operator_summary(metrics: dict[str, Any]) -> dict[str, str]:
    display = _metrics_display(metrics)
    keys = (
        "verdict",
        "coverage_message",
        "backtest_days",
        "bars_total",
        "total_trades",
        "total_return_pct",
        "benchmark_buy_hold_return_pct",
        "alpha_vs_benchmark_pct",
        "max_drawdown_pct",
        "sharpe_ratio",
        "win_rate_pct",
        "profit_factor",
        "total_fees_usd",
        "total_slippage_usd",
    )
    summary = {key: display[key] for key in keys if key in display}
    summary["unit_warning"] = (
        "Use these display strings exactly. Raw *_pct values are already "
        "percentage points; never multiply them by 100."
    )
    return summary


def _operator_summary_text(summary: dict[str, str]) -> str:
    def get(key: str) -> str:
        return str(summary.get(key) or "n/a")

    lines = [
        "Operator-facing backtest summary. Copy these display values exactly.",
        "Raw *_pct values are already percentage points; never multiply by 100.",
        "Example: total_return_pct 0.0274 means 0.0274%, not 2.74%.",
        f"verdict: {get('verdict')}",
        f"coverage: {get('coverage_message')}",
        f"backtest_days: {get('backtest_days')}",
        f"bars_total: {get('bars_total')}",
        f"total_trades: {get('total_trades')}",
        f"total_return_pct: {get('total_return_pct')}",
        f"benchmark_buy_hold_return_pct: {get('benchmark_buy_hold_return_pct')}",
        f"alpha_vs_benchmark_pct: {get('alpha_vs_benchmark_pct')}",
        f"max_drawdown_pct: {get('max_drawdown_pct')}",
        f"sharpe_ratio: {get('sharpe_ratio')}",
        f"win_rate_pct: {get('win_rate_pct')}",
        f"profit_factor: {get('profit_factor')}",
        f"total_fees_usd: {get('total_fees_usd')}",
        f"total_slippage_usd: {get('total_slippage_usd')}",
    ]
    return "\n".join(lines)


def _load_target_package(
    paths,
    strategy_id: str | None,
    proposal_id: str | None,
) -> StrategyPackage:
    if proposal_id:
        for proposal in list_proposals(paths):
            if proposal.id != proposal_id:
                continue
            strategies_dir = proposal.path / "after" / "strategies"
            if not strategies_dir.exists():
                raise TradingError(
                    f"proposal {proposal_id!r} has no after/strategies tree"
                )
            candidates = sorted(p for p in strategies_dir.iterdir() if p.is_dir())
            if not candidates:
                raise TradingError(
                    f"proposal {proposal_id!r} has no strategy package"
                )
            return load_package_from_dir(candidates[0])
        raise TradingError(f"unknown proposal: {proposal_id!r}")
    return load_package(paths, str(strategy_id or ""))


def _discover_strategy_timeframes(strategy_root: Path) -> list[str]:
    main_path = strategy_root / "main.py"
    if not main_path.exists():
        return []
    text = main_path.read_text(encoding="utf-8", errors="ignore")
    constants: dict[str, str] = {}
    for name, value in re.findall(r"(?<![A-Za-z0-9_])(_?[A-Z][A-Z0-9_]*)\s*=\s*[\"'](\d+[mhd])[\"']", text):
        constants[name] = value
    out: list[str] = []
    for name, value in constants.items():
        if "TIMEFRAME" in name:
            out.append(value)
    for value in re.findall(r"timeframe\s*=\s*[\"'](\d+[mhd])[\"']", text):
        out.append(value)
    for name in re.findall(r"timeframe\s*=\s*(_?[A-Z][A-Z0-9_]*)", text):
        if name in constants:
            out.append(constants[name])
    return _unique(out)


def _unique(values: list[str]) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for value in values:
        if value in seen:
            continue
        seen.add(value)
        out.append(value)
    return out


if __name__ == "__main__":
    raise SystemExit(main())
