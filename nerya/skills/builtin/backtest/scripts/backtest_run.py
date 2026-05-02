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
from .....strategies.package import load_package
from .config import load_config
from .data_cache import _tf_seconds, get_candles
from .engine import run_backtest
from .metrics import assemble_metrics
from .render_chart import render_chart
from .report import render_report
from .writers import write_csv_artifacts


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run a Nerya strategy backtest")
    parser.add_argument("--strategy-id", required=True)
    parser.add_argument("--preset", default="default")
    parser.add_argument("--config")
    parser.add_argument("--workspace")
    parser.add_argument("--allow-mock", action="store_true")
    args = parser.parse_args(argv)

    config_obj = load_workspace_config(Path(args.workspace).expanduser() if args.workspace else None)
    package = load_package(config_obj.paths, args.strategy_id)
    cfg = load_config(
        preset=args.preset,
        config_path=args.config,
        markets=list(package.manifest.markets),
    )
    discovered_timeframes = _discover_strategy_timeframes(package.root)
    if discovered_timeframes:
        cfg.timeframes = _unique([cfg.tf, *cfg.timeframes, *discovered_timeframes])
        if not args.config:
            cfg.tf = min(cfg.timeframes, key=_tf_seconds)
            cfg.timeframes = _unique([cfg.tf, *cfg.timeframes])
    now = int(time.time())
    start = now - (cfg.window_days * 86400) - (cfg.warmup_bars * _tf_seconds(cfg.tf))
    cache_root = Path(cfg.cache_root) if cfg.cache_root else config_obj.paths.artifacts / "backtest_cache"
    timeframe_candles_by_market = {
        market: {
            tf: get_candles(market, tf, start, now, cache_root, allow_mock=args.allow_mock)
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
    (out_dir / "metrics.json").write_text(json.dumps(metrics, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    outputs: dict[str, Path] = dict(csvs)
    outputs["metrics"] = out_dir / "metrics.json"
    report = render_report(metrics, result, cfg.asdict(), outputs)
    (out_dir / "report.md").write_text(report, encoding="utf-8")
    chart = render_chart(out_dir)
    outputs["chart"] = out_dir / "chart.json"
    print(json.dumps({
        "ok": True,
        "strategy_id": args.strategy_id,
        "backtest_ts": ts_name,
        "out_dir": str(out_dir),
        "verdict": metrics.get("verdict"),
        "total_return_pct": metrics.get("total_return_pct"),
        "max_drawdown_pct": metrics.get("max_drawdown_pct"),
        "sharpe_ratio": metrics.get("sharpe_ratio"),
        "chart_panels": len(chart.get("panels", [])),
    }, ensure_ascii=False, default=str))
    return 0


def _discover_strategy_timeframes(strategy_root: Path) -> list[str]:
    main_path = strategy_root / "main.py"
    if not main_path.exists():
        return []
    text = main_path.read_text(encoding="utf-8", errors="ignore")
    constants: dict[str, str] = {}
    for name, value in re.findall(r"\b([A-Z][A-Z0-9_]*)\s*=\s*[\"'](\d+[mhd])[\"']", text):
        constants[name] = value
    out: list[str] = []
    for value in re.findall(r"timeframe\s*=\s*[\"'](\d+[mhd])[\"']", text):
        out.append(value)
    for name in re.findall(r"timeframe\s*=\s*([A-Z][A-Z0-9_]*)", text):
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
