"""CLI entrypoint for the built-in backtest skill."""

from __future__ import annotations

import argparse
import json
import math
import re
import time
from pathlib import Path
from typing import Any

from .....core import yaml_io
from .....core.config import load_config as load_workspace_config
from .....core.errors import TradingError
from .....tools.native.paths import to_workspace_relative
from .....data.candles import canonical_venue
from .....evolution.patch_proposal import list_proposals
from .....strategies.package import StrategyPackage, load_package, load_package_from_dir
from .config import BacktestConfig, BacktestConfigError, load_config
from .data_cache import NoHistoricalDataError, _tf_seconds, get_candles
from .engine import run_backtest as run_backtest
from .metrics import assemble_metrics
from .render_chart import render_chart
from .report import render_report
from .writers import create_run_dir, write_csv_artifacts


_REAL_DATA_FALLBACK_TIMEFRAMES = ("5m", "15m", "1m", "30m", "1h", "4h", "1d")
_SHORT_LIVED_MARKET_MARKERS = (
    "meme",
    "memecoin",
    "pump.fun",
    "pumpfun",
    "new pool",
    "new-pool",
    "thin pool",
    "byreal",
    "byreal_onchain",
    "okx_onchain",
    "bitget_onchain",
    "onchain",
    "on-chain",
    "dex",
    "smart money",
    "smart_money",
    "holder concentration",
    "wallet inflow",
    "slippage",
    "solana:",
    "base:",
    "bsc:",
)


def _package_backtest_defaults(package: StrategyPackage) -> dict[str, Any]:
    """Return candidate-owned replay assumptions with safe policy inheritance.

    A strategy's backtest assumptions are part of the candidate under review.
    They must not disappear merely because an Agent follow-up omits a setting.
    Explicit tool settings/config files still override these defaults later.
    """
    raw = package.manifest.extras.get("backtest", {})
    if raw in (None, ""):
        raw = {}
    if not isinstance(raw, dict):
        raise TradingError("strategy.yml backtest must be a mapping")
    defaults = dict(raw)
    if (
        "max_open_trades" not in defaults
        and int(package.manifest.policy.max_open_positions or 0) > 0
    ):
        defaults["max_open_trades"] = int(package.manifest.policy.max_open_positions)
    try:
        # Validate partial candidate defaults with the same schema the replay
        # will use. This catches unknown/invalid values before any history IO.
        BacktestConfig.from_raw(defaults)
    except BacktestConfigError as exc:
        raise TradingError(f"strategy.yml backtest invalid: {exc}") from exc
    return defaults


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run a Nerya strategy backtest")
    target = parser.add_mutually_exclusive_group(required=True)
    target.add_argument("--strategy-id")
    target.add_argument("--proposal-id")
    target.add_argument("--package-dir")
    parser.add_argument("--preset", default="default")
    parser.add_argument("--config")
    parser.add_argument("--workspace")
    parser.add_argument("--allow-mock", action="store_true")
    args = parser.parse_args(argv)

    try:
        result = run_strategy_backtest(
            strategy_id=args.strategy_id,
            proposal_id=args.proposal_id,
            package_dir=args.package_dir,
            preset=args.preset,
            config_path=args.config,
            workspace=args.workspace,
            allow_mock=args.allow_mock,
        )
    except NoHistoricalDataError as exc:
        result = _missing_history_result(
            strategy_id=args.strategy_id,
            proposal_id=args.proposal_id,
            package_dir=args.package_dir,
            message=str(exc),
        )
    print(json.dumps(result, ensure_ascii=False, default=str))
    return 0


def run_strategy_backtest(
    *,
    strategy_id: str | None = None,
    proposal_id: str | None = None,
    package_dir: str | Path | None = None,
    preset: str = "default",
    config_path: str | Path | None = None,
    workspace: str | Path | None = None,
    allow_mock: bool = False,
    preflight_only: bool = False,
    data_mode: str | None = None,
    settings: dict[str, Any] | None = None,
    progress=None,
    cancel_token=None,
    runtime_config=None,
) -> dict[str, Any]:
    from .run_state import RunTracker
    tracker = RunTracker(progress=progress, cancel_token=cancel_token)
    try:
        return _run_strategy_backtest(strategy_id=strategy_id, proposal_id=proposal_id,
            package_dir=package_dir, preset=preset, config_path=config_path,
            workspace=workspace, allow_mock=allow_mock, preflight_only=preflight_only,
            data_mode=data_mode, settings=settings, tracker=tracker, runtime_config=runtime_config)
    except BaseException as exc:
        tracker.fail(exc)
        raise


def _run_strategy_backtest(*, strategy_id, proposal_id, package_dir, preset,
                           config_path, workspace, allow_mock, preflight_only,
                           data_mode, settings, tracker, runtime_config=None) -> dict[str, Any]:
    from .preflight import inspect_package, BacktestPreflightError
    from .history_data import resolve_window, store_root
    from .run_state import atomic_json
    from .....strategies.verification import source_revision
    from .....strategies.workflow_service import _read_files

    tracker.check_cancel()
    target_count = sum(bool(value) for value in (strategy_id, proposal_id, package_dir))
    if target_count != 1:
        raise TradingError("exactly one of strategy_id, proposal_id, or package_dir is required")

    config_obj = load_workspace_config(Path(workspace).expanduser() if workspace else None)
    if config_path:
        supplied = Path(config_path).expanduser()
        if not supplied.is_absolute():
            # Native file tools return workspace-relative paths, while the
            # server process may run from the application installation.
            supplied = (config_obj.paths.root / supplied).resolve()
        supplied = supplied.resolve()
        if not supplied.is_relative_to(config_obj.paths.root.resolve()):
            raise TradingError("backtest config must remain inside the workspace")
        config_path = supplied
    package = _load_target_package(config_obj.paths, strategy_id, proposal_id, package_dir)
    if not package.root.resolve().is_relative_to(config_obj.paths.root.resolve()):
        raise TradingError("backtest package must remain inside the workspace")
    out_dir = create_run_dir(package.root, kind="preflight" if preflight_only else "")
    ts_name = out_dir.name
    tracker.bind(out_dir, strategy_id=package.manifest.strategy_id, proposal_id=proposal_id)
    from .....strategies.continuous_config import is_continuous
    if is_continuous(package.manifest):
        raise TradingError("continuous listener is not an OHLCV tick strategy; test its finite signal function separately and run isolated lifecycle/event-to-paper-order verification")
    candidate_defaults = _package_backtest_defaults(package)
    cfg = load_config(
        preset=preset,
        defaults=candidate_defaults,
        config_path=config_path,
        markets=list(package.manifest.markets),
        overrides=settings,
    )
    evaluation = package.manifest.extras.get("evaluation", {})
    if isinstance(evaluation, dict) and "mode" in evaluation:
        cfg.evaluation_mode = str(evaluation["mode"])
    if data_mode is not None:
        cfg.data_mode = data_mode
    cfg.validate()
    if cfg.agent_replay:
        from .authorized_replay import authorized_policy
        authorized_policy(config_obj)
    if cfg.evaluation_mode == "observation" and package.manifest.policy.allow_direct_order is not False:
        raise TradingError("Observation evaluation requires policy.allow_direct_order: false")
    # 内联参数与文件配置同等权威，不能被旧的周期探测/短窗口默认值覆盖。
    effective_explicit = {**candidate_defaults, **dict(settings or {})}
    explicit_window = bool(config_path) or any(
        key in effective_explicit for key in ("window_days", "start_utc", "end_utc")
    )
    explicit_timeframe = bool(config_path) or "tf" in effective_explicit
    _apply_short_lived_window_policy(cfg, package, explicit_config=explicit_window)
    discovered_timeframes = _discover_strategy_timeframes(package.root)
    if discovered_timeframes:
        if not explicit_timeframe:
            # Prefer the strategy-declared cadence over the generic preset.
            # The previous "smallest timeframe wins" rule forced daily
            # Agent Team strategies onto 1h Yahoo data, which often cannot
            # cover the now-default >1 month replay window.
            cfg.tf = discovered_timeframes[0]
            cfg.timeframes = _unique([cfg.tf, *discovered_timeframes, *cfg.timeframes])
        else:
            cfg.timeframes = _unique([*discovered_timeframes, cfg.tf, *cfg.timeframes])
    _apply_daily_only_venue_policy(cfg)
    cfg.validate()
    now = int(time.time())
    cutoff = now // _tf_seconds(cfg.tf) * _tf_seconds(cfg.tf)
    cfg._window_start, cfg._window_end = resolve_window(start=cfg.start_utc,
        end=cfg.end_utc if cfg.end_utc is not None else cutoff, days=cfg.window_days, now=now)
    if cfg.start_utc is not None:
        cfg.window_days = (cfg._window_end - cfg._window_start) / 86400
    # Validate the selected store before a preflight-only result says ready.
    cache_root = store_root(config_obj, cfg.cache_root)
    preflight, files = inspect_package(package, cfg)
    atomic_json(out_dir / "preflight.json", preflight)
    yaml_io.dump(out_dir / "config.yml", cfg.asdict())
    if not preflight["ok"]:
        raise BacktestPreflightError(preflight)
    from ...factor_library.scripts.library import strategy_factor_snapshots
    factor_snapshots = strategy_factor_snapshots(config_obj, files)
    if preflight_only:
        tracker.complete(preflight_only=True)
        return {"ok": True, "result_type": "backtest_preflight", "strategy_id": package.manifest.strategy_id,
                "proposal_id": proposal_id, "preflight": preflight, "config": cfg.asdict(),
                "run_path": str(out_dir), "message": "Static checks passed; no download or replay was executed."}
    # Freeze the exact files checked, not whatever happens to be on disk after
    # a long download. Local imports resolve inside this snapshot as in runtime.
    snapshot_root = out_dir / "source" / package.manifest.strategy_id
    if factor_snapshots:
        atomic_json(out_dir / "factor_snapshot.json", factor_snapshots)
    for rel, content in files.items():
        destination = snapshot_root / rel
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(content, encoding="utf-8")
    pool_price_requested = bool(package.manifest.extras.get('params', {}).get('price_simulation'))
    if not allow_mock and not pool_price_requested:
        unsupported_markets = _unsupported_explicit_historical_markets(
            cfg.markets,
            config_obj=config_obj,
        )
        if unsupported_markets:
            markets = ", ".join(unsupported_markets)
            raise NoHistoricalDataError(
                f"unsupported historical data venue for {markets}; "
                "configure a provider/data source before running a standard "
                "OHLCV backtest",
                reason='historical_provider_unsupported', retryable=False,
                receipt={'markets': unsupported_markets, 'timeframes': list(cfg.timeframes),
                         'start': cfg._window_start, 'end': cfg._window_end},
            )
    cfg._requested_timeframes = list(cfg.timeframes)
    estimated_bars = sum((cfg._window_end - cfg._window_start) // _tf_seconds(tf) + cfg.warmup_bars
                         for tf in cfg.timeframes) * len(cfg.markets)
    if estimated_bars > cfg.max_bars:
        raise TradingError(f"requested {estimated_bars} input bars exceeds max_bars={cfg.max_bars}; choose a smaller explicit window or increase the configured resource budget")
    tracker.phase("preparing_data")
    requested_tf = cfg.tf
    requested_timeframes = list(cfg.timeframes)
    from .pool_price_inputs import prepare_pool_prices, bind_pool_price_evidence
    pool_prices = prepare_pool_prices(runtime_config or config_obj, package, cfg, out_dir, cancel_token=tracker.cancel_token)
    if pool_prices is not None:
        timeframe_candles_by_market = pool_prices['series']
        attempted_timeframes, missing_timeframes = list(cfg.timeframes), []
    else:
        timeframe_candles_by_market, attempted_timeframes, missing_timeframes = (
            _load_candles_with_timeframe_fallback(
                cfg, now=now, cache_root=cache_root, allow_mock=allow_mock,
                config_obj=config_obj, progress=tracker.data_progress,
                check_cancel=tracker.check_cancel,
            )
        )
    tracker.phase("validating_data")
    data_manifest = _validate_replay_data(cfg, timeframe_candles_by_market, allow_mock=allow_mock)
    atomic_json(out_dir / "data_manifest.json", data_manifest)
    if not data_manifest["ok"]:
        error = BacktestPreflightError(data_manifest)
        error.reason = "backtest_data_incomplete"
        raise error
    current_files, current_omitted = _read_files(package.root)
    if current_omitted or source_revision(current_files) != preflight["source_revision"]:
        raise TradingError("strategy source changed during data preparation; cached data is retained; rerun against the new source revision")
    candles_by_market = {market: by_tf[cfg.tf] for market, by_tf in timeframe_candles_by_market.items()}
    yaml_io.dump(out_dir / "config.yml", cfg.asdict())
    from .....strategies.verification import replay_provenance, source_revision
    from .....strategies.workflow_service import _read_files
    provenance = replay_provenance(package, cfg, timeframe_candles_by_market,
        proposal_id=proposal_id, allow_mock=allow_mock)
    from .historical_protection import MODEL as protection_model
    provenance.setdefault("assumptions", {})["protection_model"] = dict(protection_model)
    provenance["source_revision"] = preflight["source_revision"]
    provenance["source_snapshot"] = str(snapshot_root.relative_to(config_obj.paths.root))
    provenance["data_manifest"] = str((out_dir / "data_manifest.json").relative_to(config_obj.paths.root))
    tracker.phase("replaying")
    from .process_replay import execute_replay
    replay_kwargs = dict(
        candles_by_market=candles_by_market,
        timeframe_candles_by_market=timeframe_candles_by_market,
        artefacts_dir=out_dir, strategy_config=package.manifest.asdict(),
        check_cancel=tracker.check_cancel,
    )
    decision_proof = None
    if cfg.agent_replay:
        from .authorized_replay import execute_authorized_replay
        result, decision_proof = execute_authorized_replay(config_obj, snapshot_root, cfg,
            cancel_token=tracker.cancel_token, **replay_kwargs)
    else:
        result = execute_replay(
        snapshot_root,
        cfg,
        **replay_kwargs,
        progress=tracker.update,
        )
    tracker.phase("reporting")
    metrics = assemble_metrics(result)
    if decision_proof is not None:
        metrics["decision_replay"] = decision_proof
        metrics["performance_evidence"] = False
        metrics["replay"].update(agent_execution="current_model_retrospective_replay",
            scope="current_model_retrospective_replay", decision_count=decision_proof["decision_count"])
        metrics["flags"].append("current_model_retrospective_not_contemporaneous")
    # Closed-trade pairing annotates realized PnL. Persist only after that
    # calculation so CSV details and the reported metrics share one result.
    csvs = write_csv_artifacts(result, out_dir)
    end_files, end_omitted = _read_files(package.root)
    provenance["source_changed_during_run"] = bool(end_omitted) or source_revision(end_files) != provenance["source_revision"]
    metrics["provenance"] = provenance
    if pool_prices is not None:
        bind_pool_price_evidence(metrics, provenance, pool_prices)
    if decision_proof is not None:
        provenance["decision_tape"] = {key:decision_proof[key] for key in ("label","binding","tape_sha256","decision_count","decision_receipts_verified")}
        if (decision_proof.get("decision_receipts_verified") is True
                and decision_proof.get("model_decisions_verified") is True):
            provenance["scope"] = (
                "Historical replay with verified current-model Agent answers; "
                "no contemporaneous decisions, provider model-version attestation, "
                "live fills or out-of-sample claim."
            )
    temporal_warning_codes = {
        "lookahead_negative_shift", "lookahead_backward_fill", "lookahead_centered_window",
        "lookahead_forward_asof", "lookahead_backward_interpolation",
        "lookahead_dynamic_shift", "lookahead_dynamic_center", "lookahead_dynamic_asof",
        "time_alignment_resample", "nondeterministic_wall_clock",
        "nondeterministic_randomness", "external_strategy_data_read",
    }
    temporal_warnings = [
        row for row in preflight.get("warnings", [])
        if isinstance(row, dict) and row.get("code") in temporal_warning_codes
    ]
    metrics["bias_checks"] = {
        "version": 1,
        "static_temporal_scan": "warnings" if temporal_warnings else "passed",
        "static_warnings": temporal_warnings,
        "historical_prefix_only": True,
        "closed_bar_context": True,
        "multi_timeframe_close_aligned": True,
        "strategy_order_execution": "next_bar_open",
        "end_of_data_signal": "rejected_no_next_bar",
        "scope": (
            "Causal replay/runtime checks plus static high-confidence pattern detection. "
            "External point-in-time datasets and dynamic code paths still require independent review."
        ),
    }
    # Describe this invocation, not a global strategy-validation badge. These
    # independent experiments are not performed by an ordinary native replay.
    metrics["research_checks"] = {
        "version": 1,
        "scope": "this_run_only",
        "checks": [{"id": name, "status": "not_run"} for name in (
            "dynamic_lookahead", "warmup_stability", "out_of_sample",
            "walk_forward", "cost_stress", "parameter_sensitivity", "ablation",
        )],
        "note": "A completed replay or static scan is not independent research validation. Separate experiments retain their own run IDs.",
    }
    provenance.setdefault("assumptions", {})["causality_model"] = {
        "historical_prefix_only": True,
        "closed_bar_context": True,
        "multi_timeframe_close_aligned": True,
        "strategy_order_execution": "next_bar_open",
        "end_of_data_signal": "rejected_no_next_bar",
    }
    provenance["assumptions"]["execution_model_limits"] = {
        "order_types": "market_only",
        "maker_taker_fee_split": "not_modeled",
        "funding": "not_modeled",
        "liquidation": "not_modeled",
        "partial_fills": "not_modeled",
        "exchange_precision_and_minimums": "not_modeled",
        "extra_latency_beyond_next_bar": "not_modeled",
    }
    metrics["factor_refs"] = [{key: factor[key] for key in ("factor_id", "version", "name", "definition_hash")} for factor in factor_snapshots]
    metrics["data_manifest"] = data_manifest
    metrics["requested_window_complete"] = data_manifest["requested_window_complete"]
    metrics["tf"] = cfg.tf
    metrics["timeframes"] = list(cfg.timeframes)
    metrics["requested_primary_timeframe"] = requested_tf
    metrics["requested_timeframes"] = requested_timeframes
    metrics["attempted_timeframes"] = attempted_timeframes
    metrics["missing_timeframes"] = missing_timeframes
    if cfg.tf != requested_tf:
        metrics["timeframe_fallback"] = True
        metrics["timeframe_fallback_message"] = (
            f"Requested primary timeframe {requested_tf} had no common "
            f"historical rows; ran the standard OHLCV replay on available "
            f"{cfg.tf} real-data candles instead."
        )
    else:
        metrics["timeframe_fallback"] = False
    _apply_coverage_gate(metrics, cfg)
    if provenance["data_kind"] != "historical":
        metrics["coverage_message"] = (
            f"{provenance['data_kind'].capitalize()} data only: "
            f"{float(metrics.get('backtest_days') or 0):.2f}d loaded; "
            f"{cfg.window_days:.2f}d requested. Not real-history performance evidence."
        )
        if metrics.get("timeframe_fallback"):
            metrics["timeframe_fallback_message"] = f"Requested {requested_tf}; replay used {cfg.tf} {provenance['data_kind']} data."
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
        "requested_primary_timeframe",
        "attempted_timeframes",
        "timeframe_fallback",
        "timeframe_fallback_message",
        "requested_window_days",
        "target_backtest_days",
    )
    metrics_display = _metrics_display(metrics)
    operator_summary = _operator_summary(metrics)

    def _ws_rel(p: Path) -> str:
        # Workspace-relative form for reply-visible informational fields so
        # absolute host paths (C:\Users\...) stop leaking into operator
        # replies. Locator fields that tools/tests resolve directly
        # (out_dir, metrics_path, ...) stay absolute.
        root = config_obj.paths.root
        return str(p.relative_to(root)) if p.is_relative_to(root) else str(p)

    timeline = sorted({int(row["ts"]) for rows in candles_by_market.values() for row in rows})
    tracker.complete(verdict=metrics.get("verdict"), coverage_complete=data_manifest["requested_window_complete"],
                     bars_processed=len(timeline), bars_total=len(timeline),
                     bar_ts=timeline[-1] if timeline else None,
                     orders_attempted=result.order_attempts, fills=len(result.trades))
    return {
        "ok": True,
        "run_path": str(out_dir),
        "run_receipt": to_workspace_relative(out_dir / "run.json", config_obj.paths.root),
        "preflight": preflight,
        "data_manifest": data_manifest,
        "requested_window_complete": data_manifest["requested_window_complete"],
        "requested_start_utc": data_manifest.get("requested_start_utc"),
        "requested_end_utc": data_manifest.get("requested_end_utc"),
        "data_mode": cfg.data_mode,
        "title": package.manifest.title,
        "engine": "native",
        "execution_mode": metrics.get("execution_mode"),
        "performance_evidence": metrics.get("performance_evidence"),
        "accounting_currency": metrics.get("accounting_currency"),
        "decision_replay": metrics.get("decision_replay"),
        "bias_checks": metrics["bias_checks"],
        "research_checks": metrics["research_checks"],
        "equity_preview": ([{"time": ts, "value": value} for ts, value in
            result.equity_series[::max(1, len(result.equity_series) // 60)] + result.equity_series[-1:]]
            if metrics.get("performance_evidence") else []),
        "start_utc": metrics.get("start_utc"),
        "end_utc": metrics.get("end_utc"),
        "operator_summary_text": _operator_summary_text(operator_summary),
        "operator_summary": operator_summary,
        "metrics_display": metrics_display,
        "unit_warning": (
            "Raw *_pct values are already percentage points. Do not multiply "
            "them by 100; e.g. 0.0274 is 0.0274%, not 2.74%."
        ),
        "strategy_id": package.manifest.strategy_id,
        "proposal_id": proposal_id,
        "package_dir": _ws_rel(package.root) if package_dir else None,
        "backtest_ts": ts_name,
        "provenance": provenance,
        "strategy_root": _ws_rel(package.root),
        "strategy_yml_path": _ws_rel(package.root / "strategy.yml"),
        "strategy_md_path": _ws_rel(package.root / "strategy.md"),
        "main_path": _ws_rel(package.root / "main.py"),
        "out_dir": str(out_dir),
        "metrics_path": str(metrics_path),
        "report_path": _ws_rel(report_path),
        "trades_path": str(outputs["trades"]),
        "equity_path": str(outputs["equity"]),
        "decisions_path": str(outputs["decisions"]),
        "config_path": str(out_dir / "config.yml"),
        "verdict": metrics.get("verdict"),
        "evaluation_mode": metrics.get("evaluation_mode"),
        "replay": metrics.get("replay"),
        "flags": metrics.get("flags", []),
        "total_return_pct": metrics.get("total_return_pct"),
        "max_drawdown_pct": metrics.get("max_drawdown_pct"),
        "sharpe_ratio": metrics.get("sharpe_ratio"),
        "coverage_ok": metrics.get("coverage_ok"),
        "recommended_coverage_ok": metrics.get("recommended_coverage_ok"),
        "coverage_message": metrics.get("coverage_message"),
        "requested_window_days": metrics.get("requested_window_days"),
        "target_backtest_days": metrics.get("target_backtest_days"),
        "requested_primary_timeframe": metrics.get("requested_primary_timeframe"),
        "attempted_timeframes": metrics.get("attempted_timeframes"),
        "timeframe_fallback": metrics.get("timeframe_fallback"),
        "timeframe_fallback_message": metrics.get("timeframe_fallback_message"),
        "primary_timeframe": cfg.tf,
        "timeframes": list(cfg.timeframes),
        "metric_units": {
            "*_pct": "percentage points; display 0.15 as 0.15%, not 15%",
            "*_usd": "US dollars",
            "total_trades": "closed trade pairs",
            "backtest_days": "calendar days covered by loaded candles",
        },
        "metrics": {key: metrics.get(key) for key in metric_keys if key in metrics},
        "chart_panels": len(chart.get("panels", [])),
    }


def _missing_history_result(
    *,
    strategy_id: str | None,
    proposal_id: str | None,
    package_dir: str | Path | None = None,
    message: str,
) -> dict[str, Any]:
    return {
        "ok": False,
        "reason": "no_historical_data",
        "strategy_id": strategy_id,
        "proposal_id": proposal_id,
        "package_dir": str(package_dir) if package_dir else None,
        "coverage_ok": False,
        "coverage_message": message,
        "next_required_action": {
            "type": "report_data_gap",
            "message": (
                "No durable historical candles were available for the "
                "requested market/timeframe or fallback timeframes. Do not "
                "retry with mock, "
                "synthetic, random, or placeholder data; either choose a "
                "market with real historical candles, build a real custom "
                "event replay, or request explicit operator approval for "
                "a standard-backtest waiver."
            ),
        },
    }


_DAILY_ONLY_VENUES = {"AKSHARE", "TUSHARE"}
_DAILY_ONLY_MIN_WINDOW_DAYS = 500


def _apply_daily_only_venue_policy(cfg: Any) -> None:
    """Force a daily primary timeframe for end-of-day-only data feeds.

    China A-share open-data feeds (AkShare, Tushare) only publish
    end-of-day candles. A sub-daily primary timeframe silently yields the
    same sparse daily rows mislabelled as intraday, which starves warmup +
    entry logic and produces spurious ``no_trades`` results. When every
    explicit market resolves to such a venue, pin the primary timeframe to
    ``1d`` and guarantee enough daily history for warmup + signals.
    """

    markets = [str(m) for m in (getattr(cfg, "markets", []) or []) if ":" in str(m)]
    if not markets:
        return
    venues = {canonical_venue(m.split(":", 1)[0]) for m in markets}
    if not venues or not venues.issubset(_DAILY_ONLY_VENUES):
        return
    cfg.tf = "1d"
    cfg.timeframes = _unique(["1d", *(getattr(cfg, "timeframes", []) or [])])
    if int(getattr(cfg, "window_days", 0) or 0) < _DAILY_ONLY_MIN_WINDOW_DAYS:
        cfg.window_days = _DAILY_ONLY_MIN_WINDOW_DAYS


def _apply_short_lived_window_policy(cfg: Any, package: StrategyPackage, *, explicit_config: bool) -> None:
    if explicit_config:
        return
    if not _package_looks_short_lived(package):
        return
    short_days = max(1, int(getattr(cfg, "short_lived_window_days", 7) or 7))
    if int(getattr(cfg, "window_days", 0) or 0) <= short_days:
        return
    cfg.window_days = short_days
    setattr(cfg, "window_policy", "short_lived_market")


def _package_looks_short_lived(package: StrategyPackage) -> bool:
    parts = [
        package.manifest.strategy_id,
        package.manifest.title,
        package.manifest.description,
        *package.manifest.markets,
        *package.manifest.news_sources,
        *package.manifest.subagents,
    ]
    for rel in ("strategy.md", "README.md", "main.py"):
        path = package.root / rel
        if path.exists():
            try:
                parts.append(path.read_text(encoding="utf-8", errors="ignore")[:20_000])
            except Exception:
                pass
    body = "\n".join(str(part or "") for part in parts).lower()
    return any(marker in body for marker in _SHORT_LIVED_MARKET_MARKERS)


def _apply_coverage_gate(metrics: dict[str, Any], cfg: Any) -> None:
    manifest = metrics.get("data_manifest")
    if isinstance(manifest, dict):
        complete = manifest.get("requested_window_complete") is True
        metrics.update(coverage_ok=complete, requested_window_days=cfg.window_days,
            target_backtest_days=cfg.min_backtest_days or cfg.window_days,
            recommended_coverage_ok=complete, min_backtest_days=cfg.min_backtest_days)
        metrics["coverage_message"] = (
            "All requested closed candles and warmup rows are present in the verified local datasets."
            if complete else
            "Partial-window research: required historical candles are missing. This is NOT a complete requested-window backtest; see data_manifest.json for each dataset's gaps."
        )
        if not complete:
            metrics["flags"] = list(dict.fromkeys([*metrics.get("flags", []), "incomplete_requested_window"]))
        if metrics.get("timeframe_fallback_message"):
            metrics["coverage_message"] += " " + str(metrics["timeframe_fallback_message"])
        return
    target_days = float(getattr(cfg, "min_backtest_days", 0) or 0)
    requested_window_days = float(getattr(cfg, "window_days", 0) or 0)
    try:
        actual_days = float(metrics.get("backtest_days") or 0)
    except Exception:
        actual_days = 0.0
    recommended_ok = target_days <= 0 or actual_days >= target_days
    fallback_note = str(metrics.get("timeframe_fallback_message") or "").strip()
    # Any non-empty real-data window is acceptable for review. General CEX
    # packages request a broad window, while short-lived meme/on-chain packages
    # use a shorter default and report whatever real coverage the venue can
    # actually provide.
    metrics["coverage_ok"] = actual_days > 0
    metrics["recommended_coverage_ok"] = recommended_ok
    metrics["min_backtest_days"] = target_days
    metrics["target_backtest_days"] = target_days if target_days > 0 else requested_window_days
    metrics["recommended_backtest_days"] = target_days
    metrics["requested_window_days"] = requested_window_days
    policy = str(getattr(cfg, "window_policy", "") or "")
    if target_days <= 0:
        suffix = ""
        if requested_window_days > 0:
            suffix = f" within the requested {requested_window_days:.2f}d window"
        metrics["coverage_message"] = f"Loaded {actual_days:.2f}d of real candle coverage{suffix}."
        if actual_days > 0 and requested_window_days > 0 and actual_days + 0.01 < requested_window_days:
            metrics["coverage_message"] += " Using the maximum real history the source returned."
        if policy == "short_lived_market":
            metrics["coverage_message"] += " Short-lived meme/on-chain window policy applied."
        if fallback_note:
            metrics["coverage_message"] += f" {fallback_note}"
        return
    if recommended_ok:
        metrics["coverage_message"] = (
            f"Loaded {actual_days:.2f}d of real candle coverage against "
            f"target {target_days:.2f}d."
        )
        if fallback_note:
            metrics["coverage_message"] += f" {fallback_note}"
        return

    flags = metrics.get("flags")
    if not isinstance(flags, list):
        flags = []
    if "below_recommended_backtest_window" not in flags:
        flags.append("below_recommended_backtest_window")
    metrics["flags"] = flags
    metrics["coverage_message"] = (
        f"Loaded {actual_days:.2f}d of real candle coverage, below target "
        f"{target_days:.2f}d; treat this as a valid short-window real-data "
        "backtest, not a failed coverage gate."
    )
    if fallback_note:
        metrics["coverage_message"] += f" {fallback_note}"


def _metrics_display(metrics: dict[str, Any]) -> dict[str, str]:
    if metrics.get("performance_evidence") is False:
        replay = metrics.get("replay") or {}
        return {**{key: str(replay.get(key, 0)) for key in ("decisions", "dispatches", "skipped", "errors")},
            **{key: str(metrics[key]) for key in ("start_utc", "end_utc", "backtest_days", "verdict") if metrics.get(key) is not None}}
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
    for key in (
        "total_trades",
        "backtest_days",
        "bars_total",
        "bars_traded",
        "sharpe_ratio",
        "profit_factor",
        "tf",
        "start_utc",
        "end_utc",
        "requested_window_days",
    ):
        value = metrics.get(key)
        if value is not None:
            display[key] = str(value)
    verdict = metrics.get("verdict")
    if verdict is not None:
        display["verdict"] = str(verdict)
    coverage_message = metrics.get("coverage_message")
    if coverage_message:
        display["coverage_message"] = str(coverage_message)
    fallback_message = metrics.get("timeframe_fallback_message")
    if fallback_message:
        display["timeframe_fallback_message"] = str(fallback_message)
    return display


def _operator_summary(metrics: dict[str, Any]) -> dict[str, str]:
    display = _metrics_display(metrics)
    if metrics.get("performance_evidence") is False:
        return {**display, "evidence_scope":"Historical events, branch selection and input collection only. Agent model not executed; no Agent trading performance claim."}
    keys = (
        "verdict",
        "coverage_message",
        "timeframe_fallback_message",
        "tf",
        "start_utc",
        "end_utc",
        "requested_window_days",
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
    alpha = metrics.get("alpha_vs_benchmark_pct")
    try:
        alpha_value = float(alpha) if alpha is not None else None
    except (TypeError, ValueError):
        alpha_value = None
    if alpha_value is not None and math.isfinite(alpha_value):
        delta = display.get("alpha_vs_benchmark_pct", f"{alpha_value:.4f}%").removesuffix("%")
        if alpha_value > 0:
            summary["benchmark_comparison"] = (
                f"Outperformed buy-and-hold benchmark by {delta} percentage points."
            )
        elif alpha_value < 0:
            summary["benchmark_comparison"] = (
                f"Underperformed buy-and-hold benchmark by {delta.lstrip('-')} percentage points."
            )
        else:
            summary["benchmark_comparison"] = "Matched the buy-and-hold benchmark."
    summary["unit_warning"] = (
        "Use these display strings exactly. Raw *_pct values are already "
        "percentage points; never multiply them by 100."
    )
    return summary


def _operator_summary_text(summary: dict[str, str]) -> str:
    """Clean, copy-safe display values for the user-facing summary.

    This must contain ONLY presentable values — no meta-instructions. The
    string is sometimes surfaced to operators verbatim (and models are told
    to reuse these exact numbers), so anything that reads like an internal
    note ("copy these values exactly") would leak into the final reply.
    Unit/formatting guidance for the model lives in
    ``operator_summary['unit_warning']`` and the tool description instead.
    """

    def get(key: str) -> str:
        return str(summary.get(key) or "").strip()

    rows = [
        ("Verdict", get("verdict")),
        ("Coverage", get("coverage_message")),
        ("Timeframe fallback", get("timeframe_fallback_message")),
        ("Primary timeframe", get("tf")),
        ("Actual start UTC", get("start_utc")),
        ("Actual end UTC", get("end_utc")),
        ("Requested window days", get("requested_window_days")),
        ("Backtest days", get("backtest_days")),
        ("Bars total", get("bars_total")),
        ("Total trades", get("total_trades")),
        ("Total return", get("total_return_pct")),
        ("Benchmark (buy & hold)", get("benchmark_buy_hold_return_pct")),
        ("Alpha vs benchmark", get("alpha_vs_benchmark_pct")),
        ("Benchmark comparison", get("benchmark_comparison")),
        ("Max drawdown", get("max_drawdown_pct")),
        ("Sharpe ratio", get("sharpe_ratio")),
        ("Win rate", get("win_rate_pct")),
        ("Profit factor", get("profit_factor")),
        ("Total fees (USD)", get("total_fees_usd")),
        ("Total slippage (USD)", get("total_slippage_usd")),
    ]
    # Always keep the primary-timeframe line so downstream summaries can
    # surface the resolved timeframe even when other fields are empty.
    return "\n".join(
        f"{label}: {value}"
        for label, value in rows
        if value or label == "Primary timeframe"
    )


def _load_target_package(
    paths,
    strategy_id: str | None,
    proposal_id: str | None,
    package_dir: str | Path | None = None,
) -> StrategyPackage:
    from .....strategies.execution_contract import require_implemented_candidate

    def checked(package):
        require_implemented_candidate(package)
        return package

    if package_dir:
        path = Path(package_dir).expanduser()
        if not path.is_absolute():
            path = paths.root / path
        return checked(load_package_from_dir(path))
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
            return checked(load_package_from_dir(candidates[0]))
        raise TradingError(f"unknown proposal: {proposal_id!r}")
    return checked(load_package(paths, str(strategy_id or "")))


_ALWAYS_SUPPORTED_EXPLICIT_VENUES = {
    "MOCK",
    "PAPER",
    "YAHOO",
    "BINANCE",
    "BINANCE_SPOT",
    "BINANCE_PERPETUAL",
    "BINANCE_PERP",
    "BINANCEUSDM",
    "BINANCE_USDM",
    "BINANCE_FUTURES",
    "BINANCE_UM",
    "BINANCE_COINM_PERPETUAL",
    "BINANCE_COINM",
    "BINANCECOINM",
    "BINANCE_CM",
    "BYBIT",
    "BYBIT_PERPETUAL",
    "BYBIT_PERP",
    "BYBIT_LINEAR",
    "BYBIT_SWAP",
    "BYBIT_FUTURES",
    "ONCHAIN",
}


def _canonical_explicit_venue(venue: str) -> str:
    return canonical_venue(str(venue or ""))


def _unsupported_explicit_historical_markets(
    markets: list[str],
    *,
    config_obj: Any,
) -> list[str]:
    """Return explicit ``VENUE:SYMBOL`` markets without configured history.

    Standard OHLCV backtests must not silently substitute another venue for an
    explicit market prefix. Dynamic discovery still works for unprefixed
    markets and for venues present in workspace accounts/exchanges/providers.
    """

    supported = set(_ALWAYS_SUPPORTED_EXPLICIT_VENUES)
    try:
        from .....data.candles import discover_market_data_sources

        for source in discover_market_data_sources(config_obj):
            canon = _canonical_explicit_venue(str(source.get("canonical") or ""))
            if canon:
                supported.add(canon)
    except Exception:
        pass
    try:
        from .....connectors.provider_spec import get_registry

        for spec in get_registry().list_specs():
            info = spec.to_info()
            if not (info.get("supports") or {}).get("klines", False):
                continue
            for venue in [spec.id, *list(spec.aliases or ())]:
                canon = _canonical_explicit_venue(str(venue or ""))
                if canon:
                    supported.add(canon)
    except Exception:
        pass
    out: list[str] = []
    for market in markets:
        if ":" not in str(market):
            continue
        venue = _canonical_explicit_venue(str(market).split(":", 1)[0])
        if not venue:
            continue
        if venue.endswith("_ONCHAIN"):
            continue
        if _ccxt_supports_explicit_venue(venue):
            continue
        if venue not in supported:
            out.append(str(market))
    return out


def _ccxt_supports_explicit_venue(venue: str) -> bool:
    try:
        from .....connectors.ccxt_adapter import supported_exchanges
        from .....data.candles import _ccxt_exchange_id_for_venue

        supported = set(supported_exchanges())
        if not supported:
            return False
        exchange_id = _ccxt_exchange_id_for_venue(venue)
        return bool(exchange_id and exchange_id in supported)
    except Exception:
        return False


def _load_candles_with_timeframe_fallback(
    cfg: Any,
    *,
    now: int,
    cache_root: Path,
    allow_mock: bool,
    config_obj: Any,
    progress=None,
    check_cancel=None,
) -> tuple[dict[str, dict[str, list[dict[str, Any]]]], list[str], dict[str, dict[str, str]]]:
    requested_timeframes = _unique([cfg.tf, *list(cfg.timeframes)])
    fallback_timeframes = _unique([*requested_timeframes, *_REAL_DATA_FALLBACK_TIMEFRAMES]) if cfg.allow_timeframe_fallback else requested_timeframes
    candles_by_market: dict[str, dict[str, list[dict[str, Any]]]] = {}
    missing: dict[str, dict[str, str]] = {}
    attempted: list[str] = []
    cfg._data_receipts = {}

    def load_timeframe(tf: str) -> bool:
        attempted.append(tf)
        complete = True
        for market in cfg.markets:
            if check_cancel:
                check_cancel()
            by_tf = candles_by_market.setdefault(market, {})
            if tf in by_tf:
                continue
            step = _tf_seconds(tf)
            finish = int(getattr(cfg, "_window_end", now)) // step * step
            requested_start = int(getattr(cfg, "_window_start", finish - cfg.window_days * 86400))
            start = ((requested_start + step - 1) // step - cfg.warmup_bars) * step
            try:
                by_tf[tf] = get_candles(
                    market,
                    tf,
                    start,
                    finish - 1,
                    cache_root,
                    allow_mock=allow_mock,
                    config_like=config_obj,
                    offline=cfg.data_mode == "local",
                    progress=progress,
                    check_cancel=check_cancel,
                    timeout_seconds=cfg.download_timeout_seconds,
                )
                cfg._data_receipts.setdefault(market, {})[tf] = getattr(by_tf[tf], "history_receipt", {})
                by_tf[tf] = [bar for bar in by_tf[tf] if int(bar["ts"]) + _tf_seconds(tf) <= now]
                if not by_tf[tf]:
                    del by_tf[tf]
                    raise NoHistoricalDataError(f"no completed historical {tf} candles for {market}")
            except NoHistoricalDataError as exc:
                missing.setdefault(market, {})[tf] = str(exc)
                cfg._data_receipts.setdefault(market, {})[tf] = getattr(exc, "receipt", {})
                complete = False
        return complete

    def timeframe_has_usable_rows(tf: str) -> bool:
        min_rows = max(3, int(getattr(cfg, "warmup_bars", 0) or 0) + 3)
        for market in cfg.markets:
            rows = candles_by_market.get(market, {}).get(tf) or []
            if len(rows) < min_rows:
                return False
        return True

    for tf in requested_timeframes:
        load_timeframe(tf)

    primary_available = all(
        cfg.tf in candles_by_market.get(market, {}) for market in cfg.markets
    )
    if not primary_available or not timeframe_has_usable_rows(cfg.tf):
        for tf in fallback_timeframes:
            if tf in attempted:
                continue
            if load_timeframe(tf) and timeframe_has_usable_rows(tf):
                break

    for market in cfg.markets:
        if not candles_by_market.get(market):
            tried = ", ".join(attempted)
            error = NoHistoricalDataError(
                f"no historical candles for {market}; tried timeframes: {tried}"
            )
            error.receipt = {"market": market, "data_mode": cfg.data_mode,
                "missing_timeframes": missing, "downloads": cfg._data_receipts}
            raise error

    common_timeframes = [
        tf
        for tf in attempted
        if all(tf in candles_by_market.get(market, {}) for market in cfg.markets)
    ]
    if not common_timeframes:
        tried = ", ".join(attempted)
        markets = ", ".join(cfg.markets)
        raise NoHistoricalDataError(
            f"no common historical candle timeframe for {markets}; tried timeframes: {tried}"
        )

    usable_timeframes = [tf for tf in common_timeframes if timeframe_has_usable_rows(tf)]
    selected_tf = (
        cfg.tf
        if not cfg.allow_timeframe_fallback or cfg.tf in usable_timeframes
        else (usable_timeframes[0] if usable_timeframes else common_timeframes[0])
    )
    original_primary = cfg.tf
    cfg.tf = selected_tf
    # An explicitly replaced primary is not an auxiliary frame. Keep only
    # auxiliaries actually requested, rather than retaining an unusable primary
    # and failing its warmup again after a permitted fallback.
    cfg.timeframes = _unique([selected_tf, *[tf for tf in common_timeframes
        if tf != original_primary or selected_tf == original_primary]])
    return candles_by_market, attempted, missing


def _validate_replay_data(cfg: Any, series: dict[str, dict[str, list]], *, allow_mock: bool) -> dict[str, Any]:
    """Validate all inputs before executing a strategy. Never invent missing bars."""
    from .....data.history_store import coverage, normalize_row, is_sample, rows_hash
    datasets: list[dict[str, Any]] = []
    blockers: list[dict[str, Any]] = []
    warnings: list[dict[str, Any]] = []
    total = 0
    complete = True
    requested = getattr(cfg, "_requested_timeframes", cfg.timeframes)
    # Keep missing requested frames visible even when a fallback can execute.
    frames = _unique([cfg.tf, *requested])
    for market in cfg.markets:
        for tf in frames:
            rows = series.get(market, {}).get(tf, [])
            step = _tf_seconds(tf)
            end = cfg._window_end // step * step
            start = ((cfg._window_start + step - 1) // step - cfg.warmup_bars) * step
            stamps = [int(row.get("ts", 0)) for row in rows]
            sample = any(is_sample(market, row) for row in rows)
            if len(stamps) != len(set(stamps)) or any(b <= a for a, b in zip(stamps, stamps[1:])):
                blockers.append({"code": "invalid_timestamps", "message": f"{market} {tf}: duplicate or unordered candles"})
            try:
                for row in rows:
                    normalize_row(row)
            except ValueError as exc:
                blockers.append({"code": "invalid_ohlcv", "message": f"{market} {tf}: {exc}"})
            info = {"market": market, **coverage(rows, start, end, tf),
                    "sha256": rows_hash(rows), "sample": sample,
                    "download": getattr(cfg, "_data_receipts", {}).get(market, {}).get(tf, {})}
            datasets.append(info)
            total += len(rows)
            if not rows:
                (blockers if tf == cfg.tf else warnings).append({"code": "missing_timeframe", "message": f"{market} {tf}: required historical series is missing"})
            elif len(rows) <= cfg.warmup_bars:
                (blockers if tf == cfg.tf else warnings).append({"code": "warmup_insufficient", "message": f"{market} {tf}: {len(rows)} bars cannot satisfy {cfg.warmup_bars} warmup bars"})
            if sample and not allow_mock and market.split(":", 1)[0].upper() not in {"MOCK", "PAPER"}:
                blockers.append({"code": "sample_in_history", "message": f"{market} {tf}: sample candles cannot prove historical performance"})
            if not info["complete"] or sample:
                complete = False
                note = {"code": "incomplete_history", "message": f"{market} {tf}: {info['missing_bars']} of {info['expected_bars']} required closed candles missing; retained local rows can be reused"}
                warnings.append(note)
    if total > cfg.max_bars:
        blockers.append({"code": "input_budget_exceeded", "message": f"{total} rows exceeds max_bars={cfg.max_bars}"})
    return {"version": 1, "ok": not blockers, "phase": "validating_data", "blockers": blockers,
            "warnings": warnings, "datasets": datasets, "total_rows": total,
            "requested_window_complete": complete, "coverage_policy": cfg.coverage_policy,
            "requested_start_utc": datetime_utc(cfg._window_start),
            "requested_end_utc": datetime_utc(cfg._window_end), "end_exclusive": True,
            "calendar_scope": "Continuous 24/7 candles. Session-based markets require a calendar-aware dataset or explicitly partial research; weekends are not fabricated."}


def datetime_utc(stamp: int) -> str:
    from datetime import datetime, timezone
    return datetime.fromtimestamp(stamp, timezone.utc).isoformat()


def _discover_strategy_timeframes(strategy_root: Path) -> list[str]:
    # Workflow cards are runtime configuration, not merely labels. Prefer their
    # ordered timeframes before legacy source-literal discovery.
    manifest = yaml_io.load(strategy_root / "strategy.yml", default={}) or {}
    declared = manifest.get("timeframe") if isinstance(manifest, dict) else None
    out = [str(declared).strip()] if isinstance(declared, str) and declared.strip() else []
    sources = manifest.get("data_sources", []) if isinstance(manifest, dict) else []
    if isinstance(sources, dict):
        sources = list(sources.values())
    from nerya.strategies.source_config import dimensions
    out.extend(frame for source in sources if isinstance(source, dict)
           and source.get("capability", "candles") in {"candles", "features"}
           for frame in dimensions(source, "timeframes", "timeframe", []))
    main_path = strategy_root / "main.py"
    if not main_path.exists():
        return _unique(out)
    text = main_path.read_text(encoding="utf-8", errors="ignore")
    constants: dict[str, str] = {}
    for name, value in re.findall(r"(?<![A-Za-z0-9_])(_?[A-Z][A-Z0-9_]*)\s*=\s*[\"'](\d+[mhd])[\"']", text):
        constants[name] = value
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
