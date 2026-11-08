"""Bar-by-bar backtest engine."""

from __future__ import annotations

import importlib.util
import asyncio
import bisect
import inspect
import math
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .config import BacktestConfig
from .indicators import compute_indicators
from .mock_ctx import MockCtx, MockPolicy, MockState, SimpleConfigView, append_jsonl
from .portfolio import PortfolioState
from .order_evidence import action_counts
from .slippage import apply_slippage, compute_fee, fee_bps_for, slip_bps_for


def _resolve_strategy_decision(value: Any) -> Any:
    if inspect.isawaitable(value):
        return asyncio.run(value)
    return value


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
    order_attempts: int = 0
    order_events: list[dict[str, Any]] = field(default_factory=list)
    execution_mode: str = "script"
    signals: list[dict[str, Any]] = field(default_factory=list)


def settle(
    pending: list[dict[str, Any]],
    current_bar_by_market: dict[str, dict[str, Any]],
    next_bar_by_market: dict[str, dict[str, Any] | None],
    portfolio: PortfolioState,
    config: BacktestConfig,
    *,
    defer_missing: bool = False,
    policy: Any = None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    fills: list[dict[str, Any]] = []
    rejects: list[dict[str, Any]] = []
    waiting: list[dict[str, Any]] = []
    # Explanations are not scheduling priority. Preserve submission order.
    ordered = list(pending)
    for order in ordered:
        market = str(order.get("market") or "")
        is_exit, order_side = _classify_order(order)
        quantity_intent = order.get("sdk_method") == "submit_intent"
        if quantity_intent:
            is_exit = portfolio.position(market).qty * (1 if order_side == "buy" else -1) < 0
        if config.kill_switch:
            rejects.append(_reject(order, "kill_switch"))
            continue
        if not market or market not in current_bar_by_market:
            rejects.append(_reject(order, "missing_market_bar"))
            continue
        if defer_missing and next_bar_by_market.get(market) is None:
            waiting.append(order)
            continue
        caps = [config.max_open_trades]
        policy_cap = int(getattr(policy,"max_open_positions",0) or 0)
        if policy_cap > 0:
            caps.append(policy_cap)
        if not is_exit and abs(portfolio.position(market).qty) <= 1e-12 and portfolio.open_positions_count() >= min(caps):
            rejects.append(_reject(order, "max_open_trades"))
            continue
        slip_bps = slip_bps_for(market, config.slip_bps_by_venue)
        if config.max_slippage_bps is not None and slip_bps > config.max_slippage_bps:
            rejects.append(_reject(order, "slippage_cap"))
            continue
        bar = current_bar_by_market[market]
        next_bar = next_bar_by_market.get(market)
        # ``order_side`` already encodes the executor leg: long entry -> buy,
        # short entry -> sell, and close_position emits the inverse leg.
        raw_side = order_side
        # Both legs fill at the *next* bar's open: adapters signal off the
        # current bar's close, so filling at that same bar's open is lookahead.
        # The normal replay path never calls settle without a next bar. The
        # fallback below is retained only for isolated low-level callers; the
        # engine rejects end-of-data strategy intents instead of same-close fill.
        if next_bar is not None:
            base_price = float(next_bar.get("open", next_bar.get("close", 0.0)))
        else:
            base_price = float(bar.get("close", bar.get("open", 0.0)))
        forced_close = bool(is_exit and next_bar is None)
        fill_price = apply_slippage(base_price, raw_side, slip_bps)
        size = float(order.get("size") or 0.0)
        size_unit = str(order.get("size_unit") or "usd").lower()
        if quantity_intent:
            # A low-level SDK order specifies quantity, not an implicit full
            # close. The portfolio handles partial reductions and reversals.
            qty = size if size_unit == "base" else size / fill_price if fill_price else 0.0
            if not math.isfinite(qty) or qty <= 0:
                rejects.append(_reject(order, "invalid_size"))
                continue
            notional = qty * fill_price
            after_qty = portfolio.position(market).qty + (qty if raw_side == "buy" else -qty)
            if raw_side == "sell" and after_qty < -1e-12 and not config.allow_short:
                rejects.append(_reject(order, "short_not_allowed"))
                continue
            fee_preview = compute_fee(notional, fee_bps_for(market, config.fee_bps_by_venue))
            if raw_side == "buy" and (portfolio.cash or 0.0) < notional + fee_preview:
                rejects.append(_reject(order, "insufficient_cash"))
                continue
        elif is_exit:
            pos = portfolio.position(market)
            qty = abs(pos.qty)
            if qty <= 1e-12:
                rejects.append(_reject(order, "no_open_position"))
                continue
            if (pos.qty > 0 and raw_side != "sell") or (pos.qty < 0 and raw_side != "buy"):
                rejects.append(_reject(order, "position_side_mismatch"))
                continue
            reduce_pct = order.get("reduce_pct")
            if order.get("fixed_base") is not None:
                qty = min(qty, float(order["fixed_base"]))
            if reduce_pct is not None:
                qty = qty * max(0.0, min(1.0, float(reduce_pct or 0.0)))
                if qty <= 1e-12:
                    rejects.append(_reject(order, "zero_reduce_qty"))
                    continue
        else:
            notional = _stake_notional(size, size_unit, fill_price, portfolio, config)
            if order.get("max_notional_usd") is not None:
                notional = min(notional, float(order["max_notional_usd"]))
            if notional <= 0.0:
                rejects.append(_reject(order, "zero_size"))
                continue
            fee_preview = compute_fee(notional, fee_bps_for(market, config.fee_bps_by_venue))
            if raw_side == "sell":
                # Opening a short books proceeds rather than spending cash, so
                # there is no cash gate — but the strategy must be allowed to
                # short for this to be a faithful simulation.
                if not config.allow_short:
                    rejects.append(_reject(order, "short_not_allowed"))
                    continue
            elif (portfolio.cash or 0.0) < notional + fee_preview:
                rejects.append(_reject(order, "insufficient_cash"))
                continue
            qty = notional / fill_price if fill_price else 0.0
            if qty <= 0.0:
                rejects.append(_reject(order, "zero_size"))
                continue
        notional = qty * fill_price
        if not is_exit and policy is not None:
            single = float(getattr(policy,"max_single_order_usd",0) or 0)
            daily = float(getattr(policy,"max_daily_notional_usd",0) or 0)
            day = int((next_bar or bar).get("ts",0)) // 86400
            if single > 0 and notional > single + 1e-8:
                rejects.append(_reject(order,"max_single_order_usd"))
                continue
            if daily > 0 and portfolio.daily_notional.get(day,0.0) + notional > daily + 1e-8:
                rejects.append(_reject(order,"max_daily_notional_usd"))
                continue
        fee = compute_fee(notional, fee_bps_for(market, config.fee_bps_by_venue))
        fill = {
            "ts": int((next_bar or bar).get("ts", 0)),
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
            "signal_ts": order.get("signal_ts"),
            "order_id": f"{order.get('intent_id')}:fill",
            "forced_close": forced_close,
        }
        portfolio.apply_fill(fill)
        if order.get("protection") and not is_exit:
            from .historical_protection import arm
            arm(portfolio, fill, order["protection"])
        fills.append(fill)
    pending[:] = waiting
    return fills, rejects


def run_backtest(
    strategy_pkg_path: str | Path | None,
    config: BacktestConfig,
    *,
    candles_by_market: dict[str, list[dict[str, Any]]],
    timeframe_candles_by_market: dict[str, dict[str, list[dict[str, Any]]]] | None = None,
    run_fn: Any | None = None,
    artefacts_dir: str | Path | None = None,
    strategy_config: dict[str, Any] | None = None,
    check_cancel: Any | None = None,
    progress: Any | None = None,
) -> BacktestResult:
    config.validate()
    if check_cancel:
        check_cancel()
    deadline = time.monotonic() + config.max_run_seconds
    if not config.markets:
        config.markets = list(candles_by_market.keys())
    strategy_root = Path(strategy_pkg_path) if strategy_pkg_path else None
    strategy_id = strategy_root.name if strategy_root else "in_process"
    strategy_run = run_fn or _load_run_fn(strategy_root)
    row_indexes = _row_indexes(candles_by_market)
    timeframe_indexes = {
        market: {tf: _ts_index(rows) for tf, rows in by_tf.items()}
        for market, by_tf in (timeframe_candles_by_market or {}).items()
    }
    indicators = {
        market: compute_indicators(rows, config.indicators, warmup_bars=config.warmup_bars)
        for market, rows in candles_by_market.items()
    }
    bar_index = sorted({int(row["ts"]) for rows in candles_by_market.values() for row in rows})
    portfolio = PortfolioState(config.initial_capital_usd)
    state = MockState()
    pending: list[dict[str, Any]] = []
    execution_records: dict[str, dict[str, Any]] = {}
    result = BacktestResult(strategy_id=strategy_id, strategy_root=strategy_root, config=config)
    audit_sink = append_jsonl(Path(artefacts_dir) / "logs" / "engine.log") if artefacts_dir else None
    benchmark_initial: dict[str, float] = {}
    last_prices: dict[str, float] = {}
    strategy_view, policy_view = _views_from_strategy_config(strategy_id, config, strategy_config)
    result.execution_mode = "agent" if (strategy_view.extras.get("agent_task") or {}).get("enabled") is True else "script"
    tf_rows = timeframe_candles_by_market or {}
    previous: dict[str, dict[str, Any]] = {}

    for i, ts in enumerate(bar_index):
        if check_cancel:
            check_cancel()
        if time.monotonic() >= deadline:
            raise TimeoutError(f"historical replay exceeded max_run_seconds={config.max_run_seconds} at bar {i}")
        if progress and i % 64 == 0:
            progress({"phase": "replaying", "bars_processed": i, "bars_total": len(bar_index),
                      "bar_ts": ts, "orders_attempted": result.order_attempts, "fills": len(result.trades)})
        bar_fills: list[dict[str, Any]] = []
        current: dict[str, dict[str, Any]] = {}
        next_bars: dict[str, dict[str, Any] | None] = {}
        for market, rows in candles_by_market.items():
            idx = row_indexes[market].get(ts)
            if idx is None:
                continue
            current[market] = rows[idx]
            next_bars[market] = rows[idx + 1] if idx + 1 < len(rows) else None
        # Book the previous decision only when its execution bar arrives.
        # No future opening price or future fill enters the current context.
        if pending:
            fills, rejects = settle(pending, previous, current, portfolio, config, defer_missing=True, policy=policy_view)
            bar_fills = fills
            result.trades.extend(fills)
            result.rejected_signals.extend(rejects)
            # Only settlement establishes terminal execution, never a caller's
            # saved receipt or a coincidentally changed portfolio position.
            for fill in fills:
                record = execution_records.get(str(fill.get("intent_id")))
                if record is not None:
                    record.update(status="filled", order_ids=[fill["order_id"]])
            for rejected in rejects:
                record = execution_records.get(str(rejected.get("intent_id")))
                if record is not None:
                    record.update(status="rejected")
            for fill in fills:
                _sync_state_after_fill(state, portfolio, str(fill["market"]))
        from .historical_protection import settle_protections
        protective_fills = settle_protections(portfolio, current, config)
        result.trades.extend(protective_fills)
        bar_fills.extend(protective_fills)
        for fill in protective_fills:
            _sync_state_after_fill(state, portfolio, str(fill["market"]))
        prices = {m: float(b.get("close", 0.0)) for m, b in current.items()}
        if prices:
            last_prices.update(prices)
            # 预热数据只用于指标，不能提前开始基准收益；否则策略和买入持有
            # 比较的起点不同，且报告会把预热天数算进用户指定的回测窗口。
            if i >= config.warmup_bars:
                portfolio.mark_to_market(ts, last_prices)
                for m, price in prices.items():
                    if price > 0:
                        benchmark_initial.setdefault(m, price)
                ratios = [last_prices[m] / benchmark_initial[m] if m in benchmark_initial else 1.0 for m in config.markets]
                result.benchmark_series.append((ts, config.initial_capital_usd * sum(ratios) / max(1, len(ratios))))
        if len(current) < len(config.markets) and "market_data_gaps" not in result.warnings:
            result.warnings.append("market_data_gaps")
        previous.update(current)
        if i < config.warmup_bars:
            continue
        # Every market callback for this timestamp sees the same historical
        # prefix. Build it once per timestamp, not once per market. The old
        # placement inside the market loop multiplied list slicing/copying by
        # the size of the universe (10 markets => roughly 10x duplicate work).
        rows_so_far = {
            m: _rows_until_ts_indexed(rows, ts, row_indexes[m])
            for m, rows in candles_by_market.items()
        }
        tf_rows_so_far = _timeframe_rows_until_ts(
            tf_rows, rows_so_far, config.tf, ts, timeframe_indexes
        )
        for market in config.markets:
            if market not in current:
                continue
            # Mirror authoritative NAV into MockState so the strategy's
            # ``ctx.portfolio`` NAV accessors (equity_usd / cash_usd /
            # summary / ledger) return real, current values during replay,
            # matching the live StrategyPortfolio contract. Without this,
            # any strategy that reads NAV/equity crashes in backtest.
            state.set(
                "__portfolio_nav__",
                {
                    "equity": float(portfolio.equity() or 0.0),
                    "nav": float(portfolio.equity() or 0.0),
                    "cash": float(portfolio.cash or 0.0),
                    "realized_pnl": float(portfolio.realized_pnl or 0.0),
                },
            )
            ctx = MockCtx(
                strategy_id=strategy_id,
                market_name=market,
                bars_by_market=rows_so_far,
                current_bar=current[market],
                pending_orders=pending,
                config_obj=config,
                state=state,
                audit_sink=audit_sink,
                timeframe_bars_by_market=tf_rows_so_far,
                policy_obj=policy_view,
                config=strategy_view,
                strategy_root=strategy_root,
                execution_records=execution_records,
            )
            pending_before = len(pending)
            try:
                decision = _resolve_strategy_decision(strategy_run(ctx))
                if result.execution_mode == "agent":
                    from .....strategies.agent_task import prepare_agent_task
                    decision = prepare_agent_task(decision, ctx,
                        context_config=strategy_view.extras.get("agent_context", {}),
                        roles=strategy_view.extras.get("subagents", ()))
                if _status_of(decision) == "error":
                    raise RuntimeError(f"strategy returned error at {ts} ({market}): {_reason_of(decision)}")
            except Exception as exc:
                exc.market = market
                exc.bar_ts = ts
                exc.receipt = {"market": market, "bar_ts": ts, "bars_processed": i,
                    "orders_attempted": result.order_attempts + len(ctx.trading.attempts),
                    "recorded_fills": len(result.trades), "performance_evidence": False,
                    "mock_surface_calls": ctx.mock_surface_calls}
                raise
            decision_row = _decision_row(ctx.clock.ts, market, decision)
            decision_row.update(order_attempts=len(ctx.trading.attempts),
                orders_submitted=len(pending) - pending_before, action_counts=action_counts(decision),
                mock_surface_calls=ctx.mock_surface_calls)
            for event in ctx.trading.attempts:
                event["trigger_market"] = market
            result.order_events.extend(ctx.trading.attempts)
            if result.execution_mode == "agent":
                decision_row["input_sources"] = ", ".join((decision.context.get("published") or {}).keys())
                decision_row["selected_roles"] = ", ".join(decision.metadata.get("selected_roles", ()))
                decision_row["agent_execution"] = "not_run"
            result.decisions.append(decision_row)
            # Preserve explicit SDK signals as historical chart evidence. Never
            # infer GBS from an order side or from assistant prose.
            for event in ctx.audit.events():
                if event.get("kind") != "strategy.signal":
                    continue
                signal = event.get("payload") or {}
                payload = signal.get("payload") or {}
                result.signals.append({"ts": ctx.clock.ts, "market": signal.get("market") or market,
                    "signal_kind": signal.get("signal_kind"), "confidence": signal.get("confidence"),
                    "reason": signal.get("reasoning_ref"), "price": payload.get("price"),
                    "label": payload.get("label"), "position": payload.get("position")})
            result.order_attempts += len(ctx.trading.attempts)
            row = dict(current[market])
            row.update({
                "market": market,
                "decision_status": _status_of(decision),
                "decision_reason": _reason_of(decision),
                "fills": sum(1 for fill in bar_fills if fill["market"] == market),
                "cash": portfolio.cash,
                "equity": portfolio.equity(),
                "position_qty": portfolio.position(market).qty,
            })
            for name, values in indicators.get(market, {}).items():
                idx = row_indexes[market].get(ts)
                row[name] = values[idx] if idx is not None and idx < len(values) else None
            result.ohlcv_rows.append(row)

    if bar_index:
        last_ts = bar_index[-1]
        if pending:
            # A decision made at the last candle close cannot fill at that same
            # close. There is no next historical open inside the requested
            # window, so retain the intent as rejected evidence instead of
            # manufacturing an end-of-data fill.
            result.rejected_signals.extend(_reject(order, "end_of_data_no_next_bar") for order in pending)
            pending.clear()
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
                "ts": int(candles_by_market[market][-1]["ts"]),
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
                "engine_generated": True,
            }
            portfolio.apply_fill(fill)
            result.trades.append(fill)
        portfolio.mark_to_market(last_ts, last_prices)
    # The last valuation includes forced-close fees; one authoritative value
    # per timestamp is shared by metrics, CSV and every chart consumer.
    result.equity_series = list(dict(portfolio.equity_series).items())
    result.final_portfolio = portfolio.snapshot()
    return result


def _load_run_fn(strategy_root: Path | None) -> Any:
    if strategy_root is None:
        raise ValueError("strategy_pkg_path or run_fn is required")
    if (strategy_root / "strategy.yml").is_file():
        # Use the same package-local imports and configured entrypoint as live
        # script execution. Replay still receives only the isolated MockCtx.
        from .....strategies.package import load_package_from_dir
        from .....strategies.runner import StrategyRunner
        from .....strategies.agent_task_mode import agent_task_requested
        package = load_package_from_dir(strategy_root)
        return StrategyRunner._load_entrypoint(package, prefer_agent_builder=agent_task_requested(package.manifest))
    main_path = strategy_root / "main.py"
    module_name = f"nerya_backtest_{strategy_root.name}"
    spec = importlib.util.spec_from_file_location(module_name, main_path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load strategy entrypoint: {main_path}")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = mod
    spec.loader.exec_module(mod)
    return getattr(mod, "run")


def _views_from_strategy_config(
    strategy_id: str,
    config: BacktestConfig,
    strategy_config: dict[str, Any] | None,
) -> tuple[SimpleConfigView, MockPolicy]:
    raw = dict(strategy_config or {})
    # Typed manifests serialize extension fields inside `extras`; raw YAML
    # presents them at the top level. Mirror StrategyConfig for both shapes.
    extensions = raw.pop("extras", {})
    if isinstance(extensions, dict):
        raw = {**extensions, **raw}
    llm_raw = raw.get("llm_policy") if isinstance(raw.get("llm_policy"), dict) else {}
    policy_raw = raw.get("policy") if isinstance(raw.get("policy"), dict) else {}
    view = SimpleConfigView(
        strategy_id=str(raw.get("strategy_id") or strategy_id),
        title=str(raw.get("title") or ""),
        mode=str(raw.get("mode") or "backtest"),
        markets=tuple(raw.get("markets") or config.markets),
        accounts=tuple(raw.get("accounts") or ()),
        news_sources=tuple(raw.get("news_sources") or ()),
        extras={
            k: v
            for k, v in raw.items()
            if k not in {"strategy_id", "title", "mode", "markets", "accounts", "news_sources"}
        },
    )
    return view, MockPolicy.from_raw(policy_raw, llm_raw)


def _timeframe_rows_until_ts(
    timeframe_candles_by_market: dict[str, dict[str, list[dict[str, Any]]]],
    primary_rows_by_market: dict[str, list[dict[str, Any]]],
    primary_tf: str,
    ts: int,
    timeframe_indexes: dict[str, dict[str, dict[int, int]]],
) -> dict[str, dict[str, list[dict[str, Any]]]]:
    out: dict[str, dict[str, list[dict[str, Any]]]] = {}
    for market, rows in primary_rows_by_market.items():
        out.setdefault(market, {})[primary_tf] = rows
    for market, by_tf in timeframe_candles_by_market.items():
        target = out.setdefault(market, {})
        for tf, rows in by_tf.items():
            from .data_cache import _tf_seconds
            latest_open = ts + _tf_seconds(primary_tf) - _tf_seconds(tf)
            target[tf] = _rows_until_ts_indexed(rows, latest_open, timeframe_indexes.get(market, {}).get(tf) or _ts_index(rows))
    return out


def _classify_order(order: dict[str, Any]) -> tuple[bool, str]:
    """Return ``(is_exit, fill_side)`` for a pending backtest order.

    The control-plane shim (``open_position`` / ``close_position`` /
    ``reduce_position``) tags each record with ``plan_action`` (mirrored in
    ``raw.method``). Trust that tag to decide open-vs-close so a short *entry*
    — whose executor leg is ``sell`` — is not misread as an exit. Only the
    low-level ``submit_intent`` path lacks an action tag; there we keep the
    historical heuristic where a bare ``sell``/``exit``/``close`` flattens.

    ``fill_side`` is the executor leg actually sent to the book: ``buy`` or
    ``sell``. For closes the shim already emits the inverse leg, so the order's
    own side is correct for both entries and exits.
    """

    raw = order.get("raw") if isinstance(order.get("raw"), dict) else {}
    action = str(order.get("plan_action") or raw.get("method") or "").lower()
    side = str(order.get("side") or raw.get("side") or "buy").lower()
    if side == "long":
        fill_side = "buy"
    elif side == "short":
        fill_side = "sell"
    elif side in {"buy", "sell"}:
        fill_side = side
    else:
        fill_side = "buy"
    if action in {"open_position", "open", "entry"}:
        return False, fill_side
    if action in {"close_position", "close", "exit", "reduce_position", "reduce"}:
        return True, fill_side
    intent_type = str(raw.get("intent_type") or "").lower()
    legacy_exit = side in {"sell", "exit", "close"} or intent_type == "exit"
    return legacy_exit, fill_side


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
    if size_unit in {"pct_nav", "pct", "percent", "nav_pct", "percent_nav", "equity_pct"}:
        pct = float(size or 0.0)
        if pct > 1.0:
            pct = pct / 100.0
        pct = max(0.0, min(1.0, pct))
        nav = float(portfolio.equity() or 0.0)
        if nav <= 0.0:
            nav = float(portfolio.cash or 0.0)
        return max(0.0, nav * pct)
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


def _ts_index(rows: list[dict[str, Any]]) -> dict[int, int]:
    return {int(row.get("ts", 0)): i for i, row in enumerate(rows)}


def _row_indexes(candles_by_market: dict[str, list[dict[str, Any]]]) -> dict[str, dict[int, int]]:
    return {market: _ts_index(rows) for market, rows in candles_by_market.items()}


def _rows_until_ts_indexed(rows: list[dict[str, Any]], ts: int, index: dict[int, int]) -> list[dict[str, Any]]:
    idx = index.get(int(ts))
    if idx is not None:
        return rows[:idx + 1]
    timestamps = [int(row.get("ts", 0)) for row in rows]
    end = bisect.bisect_right(timestamps, int(ts))
    return rows[:end]


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


def _sync_state_after_fill(state: MockState, portfolio: PortfolioState, market: str) -> None:
    """Mirror the authoritative portfolio position into ``MockState``.

    The strategy reads its current share via ``ctx.portfolio.positions``,
    which is backed by this mirror. Deriving open/close from the fill side
    alone is wrong for shorts (a short *entry* is a ``sell``), so we reflect
    the real signed position the book holds after the fill.
    """

    key = f"position:{market}"
    pos = portfolio.position(market)
    if abs(pos.qty) > 1e-12:
        state.set(
            key,
            {"qty": pos.qty, "avg_price": pos.avg_price, "opened_ts": pos.opened_ts},
        )
    else:
        state.delete(key)


def _benchmark_value(candles_by_market: dict[str, list[dict[str, Any]]], markets: list[str], ts: int) -> float:
    vals: list[float] = []
    for market in markets:
        rows = _rows_until_ts(candles_by_market.get(market, []), ts)
        if rows:
            vals.append(float(rows[-1].get("close", 0.0)))
    return sum(vals) / len(vals) if vals else 0.0


def _benchmark_value_from_current(current: dict[str, dict[str, Any]], markets: list[str]) -> float:
    vals: list[float] = []
    for market in markets:
        row = current.get(market)
        if row:
            vals.append(float(row.get("close", 0.0)))
    return sum(vals) / len(vals) if vals else 0.0
