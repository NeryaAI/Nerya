"""CLI entrypoint for the built-in backtest skill."""

from __future__ import annotations

import argparse
import json
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
    now = int(time.time())
    start = now - (cfg.window_days * 86400) - (cfg.warmup_bars * _tf_seconds(cfg.tf))
    cache_root = Path(cfg.cache_root) if cfg.cache_root else config_obj.paths.artifacts / "backtest_cache"
    candles_by_market = {
        market: get_candles(market, cfg.tf, start, now, cache_root, allow_mock=args.allow_mock)
        for market in cfg.markets
    }
    ts_name = time.strftime("%Y%m%d_%H%M%S", time.gmtime())
    out_dir = package.root / "backtests" / ts_name
    out_dir.mkdir(parents=True, exist_ok=True)
    yaml_io.dump(out_dir / "config.yml", cfg.asdict())
    result = run_backtest(package.root, cfg, candles_by_market=candles_by_market, artefacts_dir=out_dir)
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


if __name__ == "__main__":
    raise SystemExit(main())
