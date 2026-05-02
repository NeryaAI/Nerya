"""Bar-by-bar backtest engine."""

from __future__ import annotations

import importlib.util
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .config import BacktestConfig
from .indicators import compute_indicators
from .mock_ctx import MockCtx, MockState, append_jsonl
from .portfolio import PortfolioState
from .slippage import apply_slippage, compute_fee, fee_bps_for, slip_bps_for


@dataclass
class BacktestResult:
    strategy_id: str
    strategy_root: Path | None
    config: BacktestConfig
    ohlcv_rows: list[dict[str, Any]] = field(default_factory=list)
    trades: list[dict[str, Any]] = field(default_factory=list)
    rejected_signals: list[dict[str, Any]] = field(default_factory=list)
    decisions: list[dict[str, Any]] = field(default_factory=list)
    equity_series: list[tuple[int, float]] = field(default_factory=list)
    benchmark_series: list[tuple[int, float]] = field(default_factory=list)
    final_portfolio: dict[str, Any] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)


def settle(
    pending: list[dict[str, Any]],
    current_bar_by_market: dict[str, dict[str, Any]],
    next_bar_by_market: dict[str, dict[str, Any] | None],
    portfolio: PortfolioState,
    config: BacktestConfig,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    fills: list[dict[str, Any]] = []
    rejects: list[dict[str, Any]] = []
    ordered = sorted(pending, key=lambda p: str(p.get("reason") or ""))
    for order in ordered:
        market = str(order.get("market") or "")
        side = str(order.get("side") or "buy").lower()
        is_exit = side in {"sell", "exit", "close"} or str(order.get("raw", {}).get("intent_type", "")).lower() == "exit"
        if config.kill_switch:
            rejects.append(_reject(order, "kill_switch"))
            continue
        if not market or market not in current_bar_by_market:
            rejects.append(_reject(order, "missing_market_bar"))
            continue
        if not is_exit and portfolio.open_positions_count() >= config.max_open_trades:
            rejects.append(_reject(order, "max_open_trades"))
            continue
        slip_bps = slip_bps_for(market, config.slip_bps_by_venue)
        if config.max_slippage_bps is not None and slip_bps > config.max_slippage_bps:
            rejects.append(_reject(order, "slippage_cap"))
            continue
        bar = current_bar_by_market[market]
        next_bar = next_bar_by_market.get(market)
        raw_side = "sell" if is_exit else ("sell" if side in {"short"} else "buy")
        base_price = float((next_bar if is_exit and next_bar else bar).get("open" if next_bar else "close", bar.get("open", 0.0)))
        if not is_exit:
            base_price = float(bar.get("open", bar.get("close", 0.0)))
        forced_close = bool(is_exit and next_bar is None)
        fill_price = apply_slippage(base_price, raw_side, slip_bps)
        size = float(order.get("size") or 0.0)
        size_unit = str(order.get("size_unit") or "usd").lower()
        if is_exit:
            pos = portfolio.position(market)
            qty = abs(pos.qty)
            if qty <= 1e-12:
                rejects.append(_reject(order, "no_open_position"))
                continue
        else:
            notional = _stake_notional(size, size_unit, fill_price, portfolio, config)
            fee_preview = compute_fee(notional, fee_bps_for(market, config.fee_bps_by_venue))
            if (portfolio.cash or 0.0) < notional + fee_preview and not config.allow_short:
                rejects.append(_reject(order, "insufficient_cash"))
                continue
            qty = notional / fill_price if fill_price else 0.0
        notional = qty * fill_price
        fee = compute_fee(notional, fee_bps_for(market, config.fee_bps_by_venue))
        fill = {
            "ts": int(bar.get("ts", 0)),
            "market": market,
            "side": raw_side,
            "qty": qty,
            "price": fill_price,
            "ideal_price": base_price,
            "notional": notional,
            "fee": fee,
            "slippage_bps": slip_bps,
            "slippage_usd": abs(fill_price - base_price) * qty,
            "reason": str(order.get("reason") or ""),
            "intent_id": order.get("intent_id"),
            "forced_close": forced_close,
        }
        portfolio.apply_fill(fill)
        fills.append(fill)
    pending.clear()
    return fills, rejects


def run_backtest(
    strategy_pkg_path: str | Path | None,
    config: BacktestConfig,
    *,
    candles_by_market: dict[str, list[dict[str, Any]]],
    run_fn: Any | None = None,
    artefacts_dir: str | Path | None = None,
) -> BacktestResult:
    if not config.markets:
        config.markets = list(candles_by_market.keys())
    strategy_root = Path(strategy_pkg_path) if strategy_pkg_path else None
    strategy_id = strategy_root.name if strategy_root else "in_process"
    strategy_run = run_fn or _load_run_fn(strategy_root)
    indicators = {
        market: compute_indicators(rows, config.indicators, warmup_bars=config.warmup_bars)
        for market, rows in candles_by_market.items()
    }
    bar_index = sorted({int(row["ts"]) for rows in candles_by_market.values() for row in rows})
    portfolio = PortfolioState(config.initial_capital_usd)
    state = MockState()
    pending: list[dict[str, Any]] = []
    result = BacktestResult(strategy_id=strategy_id, strategy_root=strategy_root, config=config)
    audit_sink = append_jsonl(Path(artefacts_dir) / "logs" / "engine.log") if artefacts_dir else None
    benchmark_start = _benchmark_value(candles_by_market, config.markets, bar_index[0]) if bar_index else 0.0

    for i, ts in enumerate(bar_index):
        current: dict[str, dict[str, Any]] = {}
        next_bars: dict[str, dict[str, Any] | None] = {}
        for market, rows in candles_by_market.items():
            idx = _index_for_ts(rows, ts)
            if idx is None:
                continue
            current[market] = rows[idx]
            next_bars[market] = rows[idx + 1] if idx + 1 < len(rows) else None
        if i < config.warmup_bars:
            prices = {m: float(b.get("close", 0.0)) for m, b in current.items()}
            if prices:
                portfolio.mark_to_market(ts, prices)
            continue
        for market in config.markets:
            if market not in current:
                continue
            rows_so_far = {
                m: _rows_until_ts(rows, ts)
                for m, rows in candles_by_market.items()
            }
            ctx = MockCtx(
                strategy_id=strategy_id,
                market_name=market,
                bars_by_market=rows_so_far,
                current_bar=current[market],
                pending_orders=pending,
                config_obj=config,
                state=state,
                audit_sink=audit_sink,
            )
            decision = strategy_run(ctx)
            result.decisions.append(_decision_row(ts, market, decision))
            fills, rejects = settle(pending, current, next_bars, portfolio, config)
            result.trades.extend(fills)
            result.rejected_signals.extend(rejects)
            for fill in fills:
                _sync_state_after_fill(state, fill)
            row = dict(current[market])
            row.update({
                "market": market,
                "decision_status": _status_of(decision),
                "decision_reason": _reason_of(decision),
                "fills": len(fills),
                "cash": portfolio.cash,
                "equity": portfolio.equity(),
            })
            for name, values in indicators.get(market, {}).items():
                idx = _index_for_ts(candles_by_market[market], ts)
                row[name] = values[idx] if idx is not None and idx < len(values) else None
            result.ohlcv_rows.append(row)
        prices = {m: float(b.get("close", 0.0)) for m, b in current.items()}
        if prices:
            equity = portfolio.mark_to_market(ts, prices)
            result.equity_series.append((ts, equity))
            bench_now = _benchmark_value(candles_by_market, config.markets, ts)
            bench_equity = config.initial_capital_usd
            if benchmark_start:
                bench_equity = config.initial_capital_usd * bench_now / benchmark_start
            result.benchmark_series.append((ts, bench_equity))

    if bar_index:
        last_ts = bar_index[-1]
        last_prices = {
            market: float(rows[-1].get("close", 0.0))
            for market, rows in candles_by_market.items()
            if rows
        }
        for market, pos in list(portfolio.positions.items()):
            if abs(pos.qty) <= 1e-12:
                continue
            side = "sell" if pos.qty > 0 else "buy"
            price = last_prices.get(market, pos.avg_price)
            fill = {
                "ts": last_ts,
                "market": market,
                "side": side,
                "qty": abs(pos.qty),
                "price": price,
                "ideal_price": price,
                "notional": abs(pos.qty) * price,
                "fee": compute_fee(abs(pos.qty) * price, fee_bps_for(market, config.fee_bps_by_venue)),
                "slippage_bps": 0.0,
                "slippage_usd": 0.0,
                "reason": "forced_close",
                "intent_id": "forced_close",
                "forced_close": True,
            }
            portfolio.apply_fill(fill)
            result.trades.append(fill)
        portfolio.mark_to_market(last_ts, last_prices)
    result.equity_series = list(portfolio.equity_series)
    result.final_portfolio = portfolio.snapshot()
    return result


def _load_run_fn(strategy_root: Path | None) -> Any:
    if strategy_root is None:
        raise ValueError("strategy_pkg_path or run_fn is required")
    main_path = strategy_root / "main.py"
    spec = importlib.util.spec_from_file_location(f"nerya_backtest_{strategy_root.name}", main_path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load strategy entrypoint: {main_path}")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return getattr(mod, "run")


def _stake_notional(
    size: float,
    size_unit: str,
    price: float,
    portfolio: PortfolioState,
    config: BacktestConfig,
) -> float:
    if config.stake_amount.mode == "fixed" and config.stake_amount.fixed_usd:
        return float(config.stake_amount.fixed_usd)
    if size_unit in {"base", "qty", "quantity"}:
        return size * price
    if size > 0:
        return size
    return max(0.0, float(portfolio.cash or 0.0) / max(1, config.max_open_trades))


def _reject(order: dict[str, Any], reason: str) -> dict[str, Any]:
    out = dict(order)
    out["reject_reason"] = reason
    return out


def _index_for_ts(rows: list[dict[str, Any]], ts: int) -> int | None:
    for i, row in enumerate(rows):
        if int(row.get("ts", 0)) == int(ts):
            return i
    return None


def _rows_until_ts(rows: list[dict[str, Any]], ts: int) -> list[dict[str, Any]]:
    return [row for row in rows if int(row.get("ts", 0)) <= int(ts)]


def _status_of(decision: Any) -> str:
    if hasattr(decision, "status"):
        status = getattr(decision, "status")
        return getattr(status, "value", str(status))
    if isinstance(decision, dict):
        return str(decision.get("status") or "ok")
    return "ok"


def _reason_of(decision: Any) -> str:
    if hasattr(decision, "reason"):
        return str(getattr(decision, "reason") or "")
    if isinstance(decision, dict):
        return str(decision.get("reason") or "")
    return ""


def _decision_row(ts: int, market: str, decision: Any) -> dict[str, Any]:
    return {"ts": ts, "market": market, "status": _status_of(decision), "reason": _reason_of(decision)}


def _sync_state_after_fill(state: MockState, fill: dict[str, Any]) -> None:
    key = f"position:{fill['market']}"
    if fill["forced_close"] or fill["side"] == "sell":
        state.delete(key)
    else:
        state.set(key, {"qty": fill["qty"], "avg_price": fill["price"], "opened_ts": fill["ts"]})


def _benchmark_value(candles_by_market: dict[str, list[dict[str, Any]]], markets: list[str], ts: int) -> float:
    vals: list[float] = []
    for market in markets:
        rows = _rows_until_ts(candles_by_market.get(market, []), ts)
        if rows:
            vals.append(float(rows[-1].get("close", 0.0)))
    return sum(vals) / len(vals) if vals else 0.0

