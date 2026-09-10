"""Programmatic bridge to the built-in backtest engine."""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any, Callable

from ..data.candles import mock_candles
from ..skills.builtin.backtest.scripts.config import load_config
from ..skills.builtin.backtest.scripts.data_cache import _tf_seconds
from ..skills.builtin.backtest.scripts.engine import run_backtest
from ..skills.builtin.backtest.scripts.metrics import assemble_metrics
from ..skills.builtin.backtest.scripts.render_chart import render_chart
from ..skills.builtin.backtest.scripts.report import render_report
from ..skills.builtin.backtest.scripts.slippage import venue_of
from ..skills.builtin.backtest.scripts.writers import write_csv_artifacts
from ..core import yaml_io
import json


def backtest_replay(
    run_fn: Callable[[Any], Any],
    *,
    markets: list[str] | tuple[str, ...] | None = None,
    window_days: int = 180,
    tf: str = "1h",
    fee_bps: float | None = None,
    slippage_bps: float | None = None,
    artefacts_dir: str | Path | None = None,
    candles_by_market: dict[str, list[dict[str, Any]]] | None = None,
    **engine_kwargs: Any,
) -> dict[str, Any]:
    chosen_markets = list(markets or engine_kwargs.pop("markets", None) or ["MOCK:BTCUSDT"])
    overrides: dict[str, Any] = {"window_days": window_days, "tf": tf}
    # Key the per-venue overrides by the venues actually present in the
    # markets list (same ``VENUE:SYMBOL`` prefix convention the engine's
    # fee/slippage lookup uses), so BYBIT/other-venue strategies get the
    # requested fees too. The legacy three stay included for callers
    # that swap in MOCK/PAPER/BINANCE markets downstream.
    venues = {venue_of(m) for m in chosen_markets} | {"MOCK", "PAPER", "BINANCE"}
    if fee_bps is not None:
        overrides["fee_bps_by_venue"] = {venue: fee_bps for venue in sorted(venues)}
    if slippage_bps is not None:
        overrides["slip_bps_by_venue"] = {venue: slippage_bps for venue in sorted(venues)}
    overrides.update(engine_kwargs)
    cfg = load_config(preset="default", markets=chosen_markets, overrides=overrides)
    if candles_by_market is None:
        count = max(60, int((window_days * 86400) / _tf_seconds(tf)) + cfg.warmup_bars)
        candles_by_market = {m: mock_candles(m, count=count, interval_s=_tf_seconds(tf)) for m in chosen_markets}
    result = run_backtest(None, cfg, candles_by_market=candles_by_market, run_fn=run_fn, artefacts_dir=artefacts_dir)
    metrics = assemble_metrics(result)
    if artefacts_dir is not None:
        root = Path(artefacts_dir)
        root.mkdir(parents=True, exist_ok=True)
        yaml_io.dump(root / "config.yml", cfg.asdict())
        outputs = write_csv_artifacts(result, root)
        (root / "metrics.json").write_text(json.dumps(metrics, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
        outputs["metrics"] = root / "metrics.json"
        (root / "report.md").write_text(render_report(metrics, result, cfg.asdict(), outputs), encoding="utf-8")
        render_chart(root)
    return metrics
