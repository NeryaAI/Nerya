"""Backtest metrics and episode detectors."""

from __future__ import annotations

import math
from collections import defaultdict
from datetime import datetime, timezone
from statistics import mean, pstdev
from typing import Any

from .engine import BacktestResult


ENGINE_VERSION = "backtest_skill_v1"


def assemble_metrics(result: BacktestResult) -> dict[str, Any]:
    cfg = result.config
    equity = result.equity_series or [(0, cfg.initial_capital_usd)]
    initial = float(cfg.initial_capital_usd)
    final = float(equity[-1][1])
    days = _days(equity)
    returns = _period_returns(_daily_equity(equity))
    bench_return = _return_pct(result.benchmark_series, initial)
    trade_pairs = _closed_trade_pairs(result.trades)
    trade_pcts = [p["pnl_pct"] for p in trade_pairs]
    wins = [p for p in trade_pairs if p["pnl_usd"] > 0]
    losses = [p for p in trade_pairs if p["pnl_usd"] <= 0]
    dd_episodes = detect_drawdown_episodes(equity, cfg.thresholds.drawdown_episode_min_pct)
    missed = detect_missed_profit_episodes(
        result.benchmark_series,
        equity,
        cfg.thresholds.missed_profit_min_move_pct,
        cfg.thresholds.missed_profit_max_noise_pct,
    )
    max_dd_pct, max_dd_usd = _max_drawdown(equity)
    annual_factor = 365.25
    ann_return = (((final / initial) ** (365.25 / days) - 1.0) * 100.0) if initial > 0 and days > 0 else 0.0
    vol = (pstdev(returns) * math.sqrt(annual_factor) * 100.0) if len(returns) > 1 else None
    sharpe = _sharpe(returns, cfg.risk_free_daily, annual_factor)
    sortino = _sortino(returns, cfg.risk_free_daily, annual_factor)
    total_win = sum(p["pnl_usd"] for p in wins)
    total_loss = sum(p["pnl_usd"] for p in losses)
    up_cap, down_cap = capture_ratios(result.benchmark_series, equity)
    avg_equity = mean([p[1] for p in equity]) if equity else initial
    metrics = {
        "initial_capital_usd": initial,
        "final_equity_usd": final,
        "total_return_pct": (final - initial) / initial * 100.0 if initial else 0.0,
        "total_return_usd": final - initial,
        "annualized_return_pct": ann_return,
        "benchmark_buy_hold_return_pct": bench_return,
        "alpha_vs_benchmark_pct": ((final - initial) / initial * 100.0 if initial else 0.0) - bench_return,
        "max_drawdown_pct": max_dd_pct,
        "max_drawdown_usd": max_dd_usd,
        "max_drawdown_duration_days": max((e.get("duration_days", 0.0) for e in dd_episodes), default=0.0),
        "volatility_annualized_pct": vol,
        "sharpe_ratio": sharpe,
        "sortino_ratio": sortino,
        "calmar_ratio": ann_return / max_dd_pct if max_dd_pct else None,
        "total_trades": len(trade_pairs),
        "win_trades": len(wins),
        "loss_trades": len(losses),
        "win_rate_pct": len(wins) / len(trade_pairs) * 100.0 if trade_pairs else None,
        "avg_win_pct": mean([p["pnl_pct"] for p in wins]) if wins else None,
        "avg_loss_pct": mean([p["pnl_pct"] for p in losses]) if losses else None,
        "profit_factor": (total_win / abs(total_loss)) if total_loss else (math.inf if total_win else None),
        "expectancy_pct": mean(trade_pcts) if trade_pcts else None,
        "avg_trade_duration_hours": mean([p["duration_hours"] for p in trade_pairs]) if trade_pairs else None,
        "max_consecutive_wins": _max_streak([p["pnl_usd"] > 0 for p in trade_pairs], True),
        "max_consecutive_losses": _max_streak([p["pnl_usd"] > 0 for p in trade_pairs], False),
        "total_fees_usd": sum(float(t.get("fee", 0.0) or 0.0) for t in result.trades),
        "total_slippage_usd": sum(float(t.get("slippage_usd", 0.0) or 0.0) for t in result.trades),
        "exposure_pct": _exposure_pct(result.ohlcv_rows),
        "turnover_ratio": sum(abs(float(t.get("notional", 0.0) or 0.0)) for t in result.trades) / avg_equity if avg_equity else 0.0,
        "drawdown_episodes": dd_episodes,
        "missed_profit_episodes": missed,
        "total_missed_profit_pct": sum(float(e.get("missed_pct", 0.0)) for e in missed),
        "upside_capture_ratio": up_cap,
        "downside_capture_ratio": down_cap,
        "capture_asymmetry": (up_cap - down_cap) if up_cap is not None and down_cap is not None else None,
        "backtest_days": days,
        "bars_total": len(result.ohlcv_rows),
        "bars_traded": sum(1 for r in result.ohlcv_rows if int(r.get("fills", 0) or 0) > 0),
        "markets": list(cfg.markets),
        "tf": cfg.tf,
        "start_utc": _iso(equity[0][0]),
        "end_utc": _iso(equity[-1][0]),
        "engine_version": ENGINE_VERSION,
        "per_market": _per_market(result),
        "verdict": verdict(max_dd_pct, ann_return, bench_return, sum(float(e.get("missed_pct", 0.0)) for e in missed), up_cap, cfg.max_drawdown_pct, final, initial),
    }
    if not trade_pairs:
        metrics["flags"] = ["no_trades"]
    elif max_dd_pct > cfg.max_drawdown_pct:
        metrics["flags"] = ["risk_breach:max_drawdown"]
    else:
        metrics["flags"] = []
    return metrics


def verdict(
    max_dd_pct: float,
    total_return_like: float,
    benchmark_return_pct: float,
    missed_pct: float,
    upside_capture: float | None,
    max_dd_limit: float,
    final: float,
    initial: float,
) -> str:
    total_return_pct = (final - initial) / initial * 100.0 if initial else total_return_like
    if total_return_pct < 0 or max_dd_pct > max_dd_limit:
        return "FAIL"
    if max_dd_pct <= max_dd_limit and missed_pct < 30.0 and (upside_capture or 0.0) > 50.0:
        return "PASS"
    if total_return_pct > benchmark_return_pct:
        return "WARN"
    return "FAIL"


def detect_drawdown_episodes(equity_series: list[tuple[int, float]], min_pct: float) -> list[dict[str, Any]]:
    episodes: list[dict[str, Any]] = []
    if not equity_series:
        return episodes
    peak_ts, peak = equity_series[0]
    current: dict[str, Any] | None = None
    for ts, equity in equity_series:
        if equity >= peak:
            if current and current["dd_pct"] >= min_pct:
                current["recovery_ts"] = _iso(ts)
                current["duration_days"] = (ts - current["peak_unix"]) / 86400.0
                episodes.append(_strip_internal(current))
            peak_ts, peak = ts, equity
            current = None
            continue
        dd_pct = (peak - equity) / peak * 100.0 if peak else 0.0
        if dd_pct >= min_pct:
            if current is None:
                current = {
                    "peak_ts": _iso(peak_ts),
                    "peak_unix": peak_ts,
                    "peak_equity": peak,
                    "trough_ts": _iso(ts),
                    "trough_equity": equity,
                    "recovery_ts": None,
                    "dd_pct": dd_pct,
                    "duration_days": (ts - peak_ts) / 86400.0,
                    "underwater_days": (ts - peak_ts) / 86400.0,
                    "ongoing": True,
                }
            elif equity < current["trough_equity"]:
                current["trough_ts"] = _iso(ts)
                current["trough_equity"] = equity
                current["dd_pct"] = dd_pct
                current["underwater_days"] = (ts - current["peak_unix"]) / 86400.0
    if current and current["dd_pct"] >= min_pct:
        episodes.append(_strip_internal(current))
    return episodes


def detect_missed_profit_episodes(
    benchmark_series: list[tuple[int, float]],
    strategy_series: list[tuple[int, float]],
    min_move_pct: float,
    max_noise_pct: float,
) -> list[dict[str, Any]]:
    del max_noise_pct
    if len(benchmark_series) < 2 or len(strategy_series) < 2:
        return []
    strat_map = dict(strategy_series)
    episodes: list[dict[str, Any]] = []
    start_ts, start_val = benchmark_series[0]
    for ts, val in benchmark_series[1:]:
        if start_val <= 0:
            start_ts, start_val = ts, val
            continue
        move = (val - start_val) / start_val * 100.0
        if move >= min_move_pct:
            strat_start = strat_map.get(start_ts)
            strat_end = strat_map.get(ts)
            if strat_start and strat_end:
                strat_move = (strat_end - strat_start) / strat_start * 100.0
                missed = move - strat_move
                if missed > 0:
                    episodes.append({
                        "start_ts": _iso(start_ts),
                        "end_ts": _iso(ts),
                        "benchmark_pct": move,
                        "strategy_pct": strat_move,
                        "missed_pct": missed,
                        "state": {"flat_time_pct": None, "short_time_pct": None, "long_time_pct": None},
                    })
            start_ts, start_val = ts, val
    return sorted(episodes, key=lambda e: e["missed_pct"], reverse=True)


def capture_ratios(
    benchmark_series: list[tuple[int, float]],
    strategy_series: list[tuple[int, float]],
) -> tuple[float | None, float | None]:
    b = _period_returns(benchmark_series)
    s = _period_returns(strategy_series)
    pairs = list(zip(b, s, strict=False))
    up_b = [x for x, _ in pairs if x > 0]
    up_s = [y for x, y in pairs if x > 0]
    down_b = [x for x, _ in pairs if x < 0]
    down_s = [y for x, y in pairs if x < 0]
    up = (mean(up_s) / mean(up_b) * 100.0) if up_b and mean(up_b) else None
    down = (mean(down_s) / mean(down_b) * 100.0) if down_b and mean(down_b) else None
    return up, down


def _closed_trade_pairs(trades: list[dict[str, Any]]) -> list[dict[str, Any]]:
    opens: dict[str, list[dict[str, Any]]] = defaultdict(list)
    pairs: list[dict[str, Any]] = []
    for t in trades:
        market = str(t.get("market") or "")
        if str(t.get("side")).lower() == "buy" and not t.get("forced_close"):
            opens[market].append(t)
            continue
        if not opens[market]:
            continue
        o = opens[market].pop(0)
        qty = min(float(o.get("qty", 0.0)), float(t.get("qty", 0.0)))
        pnl = (float(t.get("price", 0.0)) - float(o.get("price", 0.0))) * qty
        pnl -= float(o.get("fee", 0.0)) + float(t.get("fee", 0.0))
        entry_notional = float(o.get("price", 0.0)) * qty
        pairs.append({
            "market": market,
            "entry_ts": int(o.get("ts", 0)),
            "exit_ts": int(t.get("ts", 0)),
            "pnl_usd": pnl,
            "pnl_pct": pnl / entry_notional * 100.0 if entry_notional else 0.0,
            "duration_hours": (int(t.get("ts", 0)) - int(o.get("ts", 0))) / 3600.0,
        })
        t["pnl"] = pnl
        t["pnl_pct"] = pairs[-1]["pnl_pct"]
    return pairs


def _period_returns(series: list[tuple[int, float]]) -> list[float]:
    out: list[float] = []
    for (_, prev), (_, cur) in zip(series, series[1:], strict=False):
        if prev:
            out.append((cur - prev) / prev)
    return out


def _daily_equity(series: list[tuple[int, float]]) -> list[tuple[int, float]]:
    by_day: dict[str, tuple[int, float]] = {}
    for ts, value in series:
        by_day[_iso(ts)[:10]] = (ts, value)
    return list(by_day.values())


def _sharpe(returns: list[float], rf_daily: float, annual_factor: float) -> float | None:
    if len(returns) < 2:
        return None
    stdev = pstdev(returns)
    if stdev == 0:
        return None
    return (mean(returns) - rf_daily) / stdev * math.sqrt(annual_factor)


def _sortino(returns: list[float], rf_daily: float, annual_factor: float) -> float | None:
    downs = [r for r in returns if r < rf_daily]
    if not downs:
        return None
    stdev = pstdev(downs)
    if stdev == 0:
        return None
    return (mean(returns) - rf_daily) / stdev * math.sqrt(annual_factor)


def _max_drawdown(series: list[tuple[int, float]]) -> tuple[float, float]:
    peak = None
    max_pct = 0.0
    max_usd = 0.0
    for _, equity in series:
        peak = equity if peak is None else max(peak, equity)
        if peak:
            dd = peak - equity
            max_usd = max(max_usd, dd)
            max_pct = max(max_pct, dd / peak * 100.0)
    return max_pct, max_usd


def _return_pct(series: list[tuple[int, float]], fallback_initial: float) -> float:
    if len(series) < 2:
        return 0.0
    first = series[0][1] or fallback_initial
    return (series[-1][1] - first) / first * 100.0 if first else 0.0


def _days(series: list[tuple[int, float]]) -> float:
    if len(series) < 2:
        return 0.0
    return max(0.0, (series[-1][0] - series[0][0]) / 86400.0)


def _max_streak(values: list[bool], target: bool) -> int:
    best = cur = 0
    for value in values:
        if value is target:
            cur += 1
            best = max(best, cur)
        else:
            cur = 0
    return best


def _exposure_pct(rows: list[dict[str, Any]]) -> float:
    if not rows:
        return 0.0
    return sum(1 for r in rows if int(r.get("fills", 0) or 0) > 0) / len(rows) * 100.0


def _per_market(result: BacktestResult) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    for market in result.config.markets:
        trades = [t for t in result.trades if t.get("market") == market]
        out[market] = {
            "trades": len(trades),
            "fees_usd": sum(float(t.get("fee", 0.0) or 0.0) for t in trades),
            "notional_usd": sum(float(t.get("notional", 0.0) or 0.0) for t in trades),
        }
    return out


def _iso(ts: int) -> str:
    return datetime.fromtimestamp(int(ts), tz=timezone.utc).isoformat().replace("+00:00", "Z")


def _strip_internal(row: dict[str, Any]) -> dict[str, Any]:
    out = dict(row)
    out.pop("peak_unix", None)
    return out

