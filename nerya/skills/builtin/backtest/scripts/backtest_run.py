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
from .....data.candles import canonical_venue
from .....evolution.patch_proposal import list_proposals
from .....strategies.package import StrategyPackage, load_package, load_package_from_dir
from .config import load_config
from .data_cache import NoHistoricalDataError, _tf_seconds, get_candles
from .engine import run_backtest
from .metrics import assemble_metrics
from .render_chart import render_chart
from .report import render_report
from .writers import write_csv_artifacts


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
) -> dict[str, Any]:
    target_count = sum(bool(value) for value in (strategy_id, proposal_id, package_dir))
    if target_count != 1:
        raise TradingError("exactly one of strategy_id, proposal_id, or package_dir is required")

    config_obj = load_workspace_config(Path(workspace).expanduser() if workspace else None)
    package = _load_target_package(config_obj.paths, strategy_id, proposal_id, package_dir)
    cfg = load_config(
        preset=preset,
        config_path=config_path,
        markets=list(package.manifest.markets),
    )
    _apply_short_lived_window_policy(cfg, package, explicit_config=bool(config_path))
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
    if not allow_mock:
        unsupported_markets = _unsupported_explicit_historical_markets(
            cfg.markets,
            config_obj=config_obj,
        )
        if unsupported_markets:
            markets = ", ".join(unsupported_markets)
            raise NoHistoricalDataError(
                f"unsupported historical data venue for {markets}; "
                "configure a provider/data source before running a standard "
                "OHLCV backtest"
            )
    now = int(time.time())
    cache_root = Path(cfg.cache_root) if cfg.cache_root else config_obj.paths.artifacts / "backtest_cache"
    requested_tf = cfg.tf
    requested_timeframes = list(cfg.timeframes)
    timeframe_candles_by_market, attempted_timeframes, missing_timeframes = (
        _load_candles_with_timeframe_fallback(
            cfg,
            now=now,
            cache_root=cache_root,
            allow_mock=allow_mock,
            config_obj=config_obj,
        )
    )
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
        "package_dir": _ws_rel(package.root) if package_dir else None,
        "backtest_ts": ts_name,
        "strategy_root": _ws_rel(package.root),
        "strategy_yml_path": _ws_rel(package.root / "strategy.yml"),
        "strategy_md_path": _ws_rel(package.root / "strategy.md"),
        "main_path": _ws_rel(package.root / "main.py"),
        "out_dir": str(out_dir),
        "metrics_path": str(metrics_path),
        "report_path": _ws_rel(report_path),
        "trades_path": str(outputs["trades"]),
        "config_path": str(out_dir / "config.yml"),
        "verdict": metrics.get("verdict"),
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
    keys = (
        "verdict",
        "coverage_message",
        "timeframe_fallback_message",
        "tf",
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
        ("Backtest days", get("backtest_days")),
        ("Bars total", get("bars_total")),
        ("Total trades", get("total_trades")),
        ("Total return", get("total_return_pct")),
        ("Benchmark (buy & hold)", get("benchmark_buy_hold_return_pct")),
        ("Alpha vs benchmark", get("alpha_vs_benchmark_pct")),
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
    if package_dir:
        path = Path(package_dir).expanduser()
        if not path.is_absolute():
            path = paths.root / path
        return load_package_from_dir(path)
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
) -> tuple[dict[str, dict[str, list[dict[str, Any]]]], list[str], dict[str, dict[str, str]]]:
    requested_timeframes = _unique([cfg.tf, *list(cfg.timeframes)])
    fallback_timeframes = _unique(
        [*requested_timeframes, *_REAL_DATA_FALLBACK_TIMEFRAMES]
    )
    candles_by_market: dict[str, dict[str, list[dict[str, Any]]]] = {}
    missing: dict[str, dict[str, str]] = {}
    attempted: list[str] = []

    def load_timeframe(tf: str) -> bool:
        attempted.append(tf)
        complete = True
        for market in cfg.markets:
            by_tf = candles_by_market.setdefault(market, {})
            if tf in by_tf:
                continue
            start = now - (cfg.window_days * 86400) - (cfg.warmup_bars * _tf_seconds(tf))
            try:
                by_tf[tf] = get_candles(
                    market,
                    tf,
                    start,
                    now,
                    cache_root,
                    allow_mock=allow_mock,
                    config_like=config_obj,
                )
            except NoHistoricalDataError as exc:
                missing.setdefault(market, {})[tf] = str(exc)
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
            raise NoHistoricalDataError(
                f"no historical candles for {market}; tried timeframes: {tried}"
            )

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
        if cfg.tf in usable_timeframes
        else (usable_timeframes[0] if usable_timeframes else common_timeframes[0])
    )
    cfg.tf = selected_tf
    cfg.timeframes = _unique([selected_tf, *common_timeframes])
    return candles_by_market, attempted, missing


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
