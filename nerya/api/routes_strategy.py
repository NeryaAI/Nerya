"""HTTP routes for legacy strategy CRUD.

Replaces the dashboard's old ``/skills/call`` ``skill_id="strategy"``
calls. The legacy ``strategy`` skill was archived during the workspace-
native rewrite; the dashboard's surface is now a small REST set sitting
directly on top of :mod:`nerya.trading.strategy_crud`.

Why a dedicated route file instead of routing ``/skills/call`` to
``strategy_crud``: skill calls go through the permission engine, the
journal, and a permissive pipeline tuned for *agent* invocations. Plain
operator CRUD is cleaner as plain REST — same shape as
``/strategies/runtime/*`` and ``/portfolio/*``, no permission-pending
queue, no caller-spoofing surface.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from ..core import jsonl
from ..core.errors import NeryaError, TradingError
from ..core.time import now_iso
from ..skills.builtin.backtest.scripts.render_chart import render_chart
from ..trading.executors.orchestrator import ExecutorOrchestrator
from ..trading.order_intents import SizingPolicy, TradeEntry, TradePlan
from ..trading.order_tracker import OrderTracker
from ..trading.position_book import PositionBook
from ..trading import strategy_crud
from ..trading.submit import submit_trade_plan


def _ok(payload: dict[str, Any]) -> dict[str, Any]:
    if "ok" not in payload:
        payload = {"ok": True, **payload}
    return payload


def _error(message: str, **extra: Any) -> dict[str, Any]:
    return {"ok": False, "error": message, **extra}


def _strategy_blocking_state(client, strategy_id: str) -> dict[str, int]:
    paths = client.config.paths
    positions = PositionBook(paths)
    tracker = OrderTracker(paths)
    orchestrator = ExecutorOrchestrator(client.config)
    open_positions = positions.open_positions(strategy_id=strategy_id)
    active_orders = [
        order for order in tracker.active_orders()
        if order.strategy_id == strategy_id
    ]
    active_executors = [
        run for run in orchestrator.list_active()
        if run.strategy_id == strategy_id
    ]
    return {
        "open_positions": len(open_positions),
        "active_executors": len(active_executors),
        "active_orders": len(active_orders),
    }


def _position_close_row(position, *, share=None) -> dict[str, Any]:
    """Build the close-row payload for a strategy-scoped position.

    When a ``share`` is supplied (post-v6 merged positions) the size /
    side / avg-entry shown to the operator reflect **the strategy's
    own slice**, not the merged net. This matters when one strategy's
    long is offset by another's short on the same (account, market):
    closing s1's slice should sell s1's full long size, not the merged
    net.
    """
    mark_price = (
        float(position.mark_price)
        if position.mark_price is not None
        else float(getattr(share, "avg_entry_share_price", position.avg_entry_price) or 0.0)
    )
    if share is not None:
        size_base = float(getattr(share, "size_share_base", 0.0) or 0.0)
        strategy_id = share.strategy_id
        side = share.side
        avg = float(getattr(share, "avg_entry_share_price", 0.0) or 0.0)
    else:
        size_base = float(position.size_base or 0.0)
        strategy_id = position.strategy_id
        side = position.side
        avg = float(position.avg_entry_price or 0.0)
    unrealized = 0.0
    if avg and mark_price and size_base:
        side_factor = 1.0 if size_base >= 0 else -1.0
        unrealized = (mark_price - avg) * abs(size_base) * side_factor
    return {
        "position_id": position.position_id,
        "account_id": position.account_id,
        "strategy_id": strategy_id,
        "market": position.market,
        "side": side,
        "size_base": abs(size_base),
        "mark_price": mark_price,
        "notional_usd": abs(size_base) * mark_price,
        "unrealized_pnl_usd": unrealized,
    }


def _close_strategy_positions(client, payload: dict[str, Any]) -> dict[str, Any]:
    body = payload or {}
    sid = str(body.get("strategy_id") or "").strip()
    if not sid:
        return _error("strategy_id required")
    dry_run = bool(body.get("dry_run", False))
    operator = str(body.get("operator") or "dashboard")
    book = PositionBook(client.config.paths)
    positions = book.open_positions(strategy_id=sid)
    # Build rows from each strategy's own share so close-quantities
    # reflect what *this* strategy owns, not the merged net.
    shares_by_position: dict[str, Any] = {}
    for pos in positions:
        share = book.get_share(
            strategy_id=sid, account_id=pos.account_id, market=pos.market,
        )
        if share is not None:
            shares_by_position[pos.position_id] = share
    rows = [
        _position_close_row(pos, share=shares_by_position.get(pos.position_id))
        for pos in positions
    ]
    if dry_run:
        return {
            "ok": True,
            "strategy_id": sid,
            "dry_run": True,
            "count": len(rows),
            "positions": rows,
        }

    submitted: list[dict[str, Any]] = []
    for pos, row in zip(positions, rows):
        if row["size_base"] <= 0 or row["mark_price"] <= 0:
            submitted.append({
                "position_id": pos.position_id,
                "status": "skipped",
                "error": "position_missing_size_or_mark_price",
            })
            continue
        # Use the share-derived side/size so we close exactly what
        # this strategy owns, even when the merged position holds the
        # opposite net direction via another strategy.
        plan = TradePlan(
            action="close_position",
            strategy_id=sid,
            account_id=pos.account_id,
            market=pos.market,
            side=row["side"],
            sizing=SizingPolicy(method="fixed_base", fixed_base=row["size_base"]),
            entry=TradeEntry(order_type="market"),
            confidence=1.0,
            reasoning_ref=f"operator {operator} requested close before strategy delete",
            source="operator",
            meta={
                "operator": operator,
                "reason": str(body.get("reason") or "strategy_delete_prepare"),
                "position_id": pos.position_id,
            },
        )
        result = submit_trade_plan(
            client.config,
            plan,
            market_snapshot={"price": row["mark_price"], "age_s": 0},
        )
        submitted.append({
            "position_id": pos.position_id,
            "market": pos.market,
            "side": row["side"],
            "size_base": row["size_base"],
            "status": result.get("status"),
            "result": result,
        })

    state = _strategy_blocking_state(client, sid)
    jsonl.append(client.config.paths.journal("operator"), {
        "kind": "strategy.close_positions",
        "ts": now_iso(),
        "strategy_id": sid,
        "operator": operator,
        "dry_run": False,
        "submitted": len(submitted),
        "remaining_state": state,
    })
    return {
        "ok": True,
        "strategy_id": sid,
        "dry_run": False,
        "count": len(rows),
        "positions": rows,
        "submitted": submitted,
        "remaining_state": state,
    }


def routes():
    def list_strategies(client, payload):
        include_archived = bool((payload or {}).get("include_archived", False))
        try:
            rows = strategy_crud.list_records(
                client.config.paths,
                include_archived=include_archived,
            )
        except Exception as exc:
            return _error(str(exc))
        return {"strategies": rows}

    def get_strategy(client, payload):
        sid = (payload or {}).get("strategy_id") or ""
        if not sid:
            return _error("strategy_id required")
        try:
            return strategy_crud.get_detail(client.config.paths, sid)
        except TradingError as exc:
            return _error(str(exc))
        except Exception as exc:
            return _error(f"{type(exc).__name__}: {exc}")

    def create_strategy(client, payload):
        body = payload or {}
        try:
            req = strategy_crud.CreateRequest(
                strategy_id=str(body.get("strategy_id") or "").strip(),
                title=str(body.get("title") or ""),
                description=str(body.get("description") or ""),
                account_id=str(body.get("account_id") or "paper_main"),
                markets=tuple(str(m) for m in (body.get("markets") or ())),
                trigger_kinds=tuple(
                    str(t) for t in (body.get("trigger_kinds") or ())
                ),
                subagents=tuple(str(s) for s in (body.get("subagents") or ())),
                driver=str(body.get("driver") or "prompt"),
                status=str(body.get("status") or "draft"),
                wallet_id=(
                    str(body.get("wallet_id"))
                    if body.get("wallet_id")
                    else None
                ),
                main_prompt=str(body.get("main_prompt") or ""),
            )
            return strategy_crud.create(client.config.paths, req)
        except TradingError as exc:
            return _error(str(exc))

    def update_strategy(client, payload):
        body = payload or {}
        sid = body.get("strategy_id") or ""
        if not sid:
            return _error("strategy_id required")
        patch = {k: v for k, v in body.items() if k != "strategy_id"}
        reason = str(patch.pop("reason", "") or "dashboard_update")
        try:
            return strategy_crud.update(
                client.config.paths, sid, patch=patch, reason=reason,
            )
        except (TradingError, NeryaError) as exc:
            return _error(str(exc))

    def delete_strategy(client, payload):
        body = payload or {}
        sid = body.get("strategy_id") or ""
        if not sid:
            return _error("strategy_id required")
        force = bool(body.get("force", False))
        try:
            return strategy_crud.delete(
                client.config.paths,
                str(sid),
                force=force,
                blocking_state=_strategy_blocking_state(client, str(sid)),
            )
        except (TradingError, NeryaError) as exc:
            return _error(str(exc))

    def close_positions(client, payload):
        try:
            return _close_strategy_positions(client, payload or {})
        except (TradingError, NeryaError) as exc:
            return _error(str(exc))
        except Exception as exc:
            return _error(f"{type(exc).__name__}: {exc}")

    def set_status(client, payload):
        body = payload or {}
        sid = body.get("strategy_id") or ""
        status = body.get("status") or ""
        if not sid or not status:
            return _error("strategy_id and status are required")
        reason = str(body.get("reason") or "dashboard_update")
        try:
            return strategy_crud.set_status(
                client.config.paths, sid, str(status), reason=reason,
            )
        except (TradingError, NeryaError) as exc:
            return _error(str(exc))

    def bind_wallet(client, payload):
        body = payload or {}
        sid = body.get("strategy_id") or ""
        if not sid:
            return _error("strategy_id required")
        wallet_id = body.get("wallet_id")
        try:
            return strategy_crud.bind_wallet(
                client.config.paths, sid,
                str(wallet_id) if wallet_id else None,
            )
        except TradingError as exc:
            return _error(str(exc))

    def bind_account(client, payload):
        body = payload or {}
        sid = body.get("strategy_id") or ""
        aid = body.get("account_id") or ""
        if not sid or not aid:
            return _error("strategy_id and account_id are required")
        try:
            return strategy_crud.bind_account(
                client.config.paths, sid, str(aid),
            )
        except TradingError as exc:
            return _error(str(exc))

    def resolve_runtime(client, payload):
        body = payload or {}
        sid = body.get("strategy_id") or ""
        if not sid:
            return _error("strategy_id required")
        try:
            return strategy_crud.resolve_runtime(client.config.paths, sid)
        except TradingError as exc:
            return _error(str(exc))

    def versions(client, payload):
        body = payload or {}
        sid = body.get("strategy_id") or ""
        if not sid:
            return _error("strategy_id required")
        try:
            return strategy_crud.versions(client.config.paths, sid)
        except TradingError as exc:
            return _error(str(exc))

    def list_files(client, payload):
        body = payload or {}
        sid = body.get("strategy_id") or ""
        if not sid:
            return _error("strategy_id required")
        try:
            return strategy_crud.list_files(client.config.paths, sid)
        except (TradingError, NeryaError) as exc:
            return _error(str(exc))
        except Exception as exc:
            return _error(f"{type(exc).__name__}: {exc}")

    def write_file(client, payload):
        body = payload or {}
        sid = body.get("strategy_id") or ""
        rel = body.get("rel_path") or ""
        content = body.get("content")
        if not sid or not rel:
            return _error("strategy_id and rel_path are required")
        if not isinstance(content, str):
            return _error("content must be a string")
        reason = str(body.get("reason") or "dashboard_write_file")
        try:
            return strategy_crud.write_file(
                client.config.paths,
                sid,
                rel_path=str(rel),
                content=content,
                reason=reason,
            )
        except (TradingError, NeryaError) as exc:
            return _error(str(exc))
        except Exception as exc:
            return _error(f"{type(exc).__name__}: {exc}")

    def list_backtests(client, payload):
        sid = (payload or {}).get("strategy_id") or ""
        if not sid:
            return _error("strategy_id required")
        root = _backtests_root(client.config.paths.strategy(str(sid)))
        runs = []
        for d in sorted((p for p in root.glob("*") if p.is_dir()), reverse=True):
            metrics = _load_json(d / "metrics.json")
            runs.append({
                "ts": d.name,
                "days": metrics.get("backtest_days"),
                "total_return_pct": metrics.get("total_return_pct"),
                "max_dd_pct": metrics.get("max_drawdown_pct"),
                "sharpe_ratio": metrics.get("sharpe_ratio"),
                "verdict": metrics.get("verdict"),
                "start_utc": metrics.get("start_utc"),
                "end_utc": metrics.get("end_utc"),
            })
        return {"ok": True, "strategy_id": sid, "backtests": runs}

    def backtest_chart(client, payload):
        body = payload or {}
        sid = body.get("strategy_id") or ""
        ts = (payload or {}).get("ts") or ""
        if not sid or not ts:
            return _error("strategy_id and ts are required")
        try:
            run_dir = _safe_backtest_dir_for_payload(client.config.paths, body)
            chart_path = run_dir / "chart.json"
            if not chart_path.exists():
                chart = render_chart(run_dir)
            else:
                chart = _load_json(chart_path)
            return {
                "ok": True,
                "strategy_id": sid,
                "proposal_id": body.get("proposal_id") or None,
                "ts": ts,
                "chart": chart,
            }
        except Exception as exc:
            return _error(f"{type(exc).__name__}: {exc}")

    def backtest_file(client, payload):
        body = payload or {}
        sid = body.get("strategy_id") or ""
        ts = body.get("ts") or ""
        name = body.get("name") or ""
        if not sid or not ts or not name:
            return _error("strategy_id, ts and name are required")
        allowed = {
            "config.yml",
            "ohlcv_indicators_portfolio.csv",
            "trades.csv",
            "analysis_by_reason.csv",
            "rejected_signals.csv",
            "metrics.json",
            "report.md",
            "chart.json",
        }
        if name not in allowed:
            return _error("unsupported backtest file")
        try:
            run_dir = _safe_backtest_dir_for_payload(client.config.paths, body)
            path = (run_dir / str(name)).resolve()
            if run_dir.resolve() not in path.parents and path != run_dir.resolve():
                return _error("invalid path")
            if not path.exists():
                return _error("file not found")
            return {
                "ok": True,
                "strategy_id": sid,
                "proposal_id": body.get("proposal_id") or None,
                "ts": ts,
                "name": name,
                "content": path.read_text(encoding="utf-8"),
            }
        except Exception as exc:
            return _error(f"{type(exc).__name__}: {exc}")

    def performance(client, payload):
        body = payload or {}
        sid = (body.get("strategy_id") or "").strip()
        if not sid:
            return _error("strategy_id required")
        try:
            return _strategy_performance(
                client,
                strategy_id=sid,
                limit_orders=int(body.get("limit_orders") or 50),
                limit_fills=int(body.get("limit_fills") or 50),
                equity_points=int(body.get("equity_points") or 200),
            )
        except Exception as exc:
            return _error(f"{type(exc).__name__}: {exc}")

    return [
        ("POST", "/strategy/list_all", list_strategies),
        ("POST", "/strategy/get", get_strategy),
        ("POST", "/strategy/create", create_strategy),
        ("POST", "/strategy/update", update_strategy),
        ("POST", "/strategy/close_positions", close_positions),
        ("POST", "/strategy/delete", delete_strategy),
        ("POST", "/strategy/set_status", set_status),
        ("POST", "/strategy/bind_wallet", bind_wallet),
        ("POST", "/strategy/bind_account", bind_account),
        ("POST", "/strategy/resolve_runtime", resolve_runtime),
        ("POST", "/strategy/versions", versions),
        ("POST", "/strategy/files_list", list_files),
        ("POST", "/strategy/files_write", write_file),
        ("POST", "/strategy/backtests", list_backtests),
        ("POST", "/strategy/backtests/chart", backtest_chart),
        ("POST", "/strategy/backtests/file", backtest_file),
        ("POST", "/strategy/performance", performance),
    ]


# ---- strategy performance envelope --------------------------------------


def _strategy_performance(
    client,
    *,
    strategy_id: str,
    limit_orders: int = 50,
    limit_fills: int = 50,
    equity_points: int = 200,
) -> dict[str, Any]:
    """Operator-grade performance envelope for one strategy.

    Returns a single payload with:

    * ``kpis``      - aggregate realised/unrealised PnL, fees, funding, trade
                      count, win/loss counts, last fill ts.
    * ``positions`` - per-share view (this strategy's slice of any merged
                      position) with the merged broker-truth context for
                      operator situational awareness.
    * ``orders``    - recent orders from :class:`OrderTracker` filtered to
                      this strategy_id.
    * ``fills``     - recent fills from the ``fills`` SQLite table for this
                      strategy_id.
    * ``equity_curve`` - timeseries of cumulative realised PnL + fees plus
                      a final unrealised marker so the dashboard can spark
                      a per-strategy fund curve without a second round trip.

    Everything reads directly from the per-strategy ``position_shares``
    and ``fills`` tables — never the merged ``positions`` row — so
    every number matches "what this strategy actually did".
    """
    from sqlite3 import Connection
    from ..trading.position_book import PositionBook

    paths = client.config.paths
    book = PositionBook(paths)
    # SQLite connection used directly for cross-table reads. The book
    # exposes ``_con_lazy`` for the position tables; ``fills`` is in
    # the same database so we reuse the same connection.
    con: Connection = book._con_lazy()  # noqa: SLF001 — intentional internal access

    # --- 1. shares (open + closed) ----------------------------------
    open_shares = book.list_shares_history(
        strategy_id=strategy_id, open_only=True, limit=1_000,
    )
    closed_shares = book.list_shares_history(
        strategy_id=strategy_id, open_only=False, limit=2_000,
    )
    closed_only = [s for s in closed_shares if s.closed_at is not None]

    positions_payload: list[dict[str, Any]] = []
    for share in open_shares:
        merged = book.get_open_merged(
            account_id=share.account_id, market=share.market,
        )
        mark = float(
            (merged.mark_price if merged else 0.0)
            or share.avg_entry_share_price
            or 0.0
        )
        size = float(share.size_share_base or 0.0)
        avg = float(share.avg_entry_share_price or 0.0)
        side_factor = 1.0 if size >= 0 else -1.0
        unrealized = (
            (mark - avg) * abs(size) * side_factor if (avg and mark) else 0.0
        )
        merged_payload: dict[str, Any] | None = None
        if merged is not None:
            co_strategies: list[str] = []
            try:
                for sib in book.list_shares(merged.position_id, open_only=True):
                    if sib.strategy_id != strategy_id:
                        co_strategies.append(sib.strategy_id)
            except Exception:
                co_strategies = []
            merged_payload = {
                "position_id": merged.position_id,
                "size_base": float(merged.size_base or 0.0),
                "avg_entry_price": float(merged.avg_entry_price or 0.0),
                "mark_price": float(merged.mark_price or 0.0),
                "unrealized_pnl_usd": float(merged.unrealized_pnl_usd or 0.0),
                "co_strategies": co_strategies,
            }
        positions_payload.append({
            "share_id": share.share_id,
            "market": share.market,
            "venue": share.venue,
            "account_id": share.account_id,
            "side": "long" if size >= 0 else "short",
            "size_share_base": size,
            "avg_entry_price": avg,
            "mark_price": mark,
            "unrealized_pnl_usd": float(unrealized),
            "realized_pnl_usd": float(share.realized_pnl_share_usd or 0.0),
            "fees_usd": float(share.fees_share_usd or 0.0),
            "funding_usd": float(share.funding_share_usd or 0.0),
            "notional_usd": abs(size) * mark,
            "opened_at": float(share.opened_at or 0.0),
            "updated_at": float(share.updated_at or 0.0),
            "merged": merged_payload,
        })

    # --- 2. orders --------------------------------------------------
    tracker = OrderTracker(paths)
    try:
        # No native strategy_id filter on the tracker today; we
        # pull a generous window and filter in Python. The orders
        # page only sees this strategy's history anyway.
        order_rows = list(tracker.active_orders()) + list(tracker.cached_orders())
    except Exception:
        order_rows = []
    orders_payload: list[dict[str, Any]] = []
    for o in order_rows:
        if getattr(o, "strategy_id", None) != strategy_id:
            continue
        orders_payload.append(_order_row_summary(o))
    orders_payload.sort(key=lambda r: r.get("created_at") or 0.0, reverse=True)
    orders_payload = orders_payload[:limit_orders]

    # --- 3. fills + KPI aggregates ----------------------------------
    fills_payload: list[dict[str, Any]] = []
    win_count = 0
    loss_count = 0
    total_fees = 0.0
    total_funding = 0.0
    last_trade_at: float | None = None
    for row in con.execute(
        """
        SELECT fill_id, order_id, account_id, market, side, price, size_base,
               notional_usd, fee_usd, funding_usd, ts
          FROM fills
         WHERE strategy_id = ?
         ORDER BY ts DESC
         LIMIT ?
        """,
        (strategy_id, int(limit_fills)),
    ):
        fills_payload.append({
            "fill_id": row[0],
            "order_id": row[1],
            "account_id": row[2],
            "market": row[3],
            "side": row[4],
            "price": float(row[5] or 0.0),
            "size_base": float(row[6] or 0.0),
            "notional_usd": float(row[7] or 0.0),
            "fee_usd": float(row[8] or 0.0),
            "funding_usd": float(row[9] or 0.0),
            "ts": float(row[10] or 0.0),
        })

    # Wins / losses are decided per CLOSED share — open shares are
    # still in flight and shouldn't be counted as wins or losses yet.
    realized_total = 0.0
    for s in closed_only:
        realized = float(s.realized_pnl_share_usd or 0.0)
        realized_total += realized
        if realized > 0:
            win_count += 1
        elif realized < 0:
            loss_count += 1
    # Include realised + fees + funding for OPEN shares in the
    # cumulative totals too (the user expects "total realised" to
    # include any pre-close realised slices that accumulated when a
    # strategy reduced rather than fully closed).
    for s in open_shares + closed_only:
        total_fees += float(s.fees_share_usd or 0.0)
        total_funding += float(s.funding_share_usd or 0.0)
    realized_total = sum(
        float(s.realized_pnl_share_usd or 0.0)
        for s in open_shares + closed_only
    )

    unrealized_total = sum(p["unrealized_pnl_usd"] for p in positions_payload)

    # Last trade ts comes off the freshest fill row.
    last_fill_row = con.execute(
        "SELECT MAX(ts) FROM fills WHERE strategy_id = ?",
        (strategy_id,),
    ).fetchone()
    if last_fill_row and last_fill_row[0]:
        last_trade_at = float(last_fill_row[0])

    trades_count = con.execute(
        "SELECT COUNT(*) FROM fills WHERE strategy_id = ?",
        (strategy_id,),
    ).fetchone()[0]

    # --- 4. equity curve from fills ---------------------------------
    # We walk every fill in time order, maintaining a per-market
    # (size, avg) state. When a fill is opposite-direction we book
    # realised PnL on the closed slice. The series is downsampled to
    # ``equity_points`` so the dashboard can render quickly even for
    # very chatty scalpers.
    state: dict[str, dict[str, float]] = {}
    realized_running = 0.0
    fees_running = 0.0
    raw_points: list[tuple[float, float, float]] = []  # (ts, realized, fees)
    for row in con.execute(
        """
        SELECT ts, market, side, price, size_base, fee_usd
          FROM fills
         WHERE strategy_id = ?
         ORDER BY ts ASC
        """,
        (strategy_id,),
    ):
        ts, market, side, price, size_base, fee = row
        ts = float(ts or 0.0)
        size = float(size_base or 0.0)
        price = float(price or 0.0)
        fees_running += float(fee or 0.0)
        signed = size if side == "buy" else -size
        st = state.setdefault(market, {"size": 0.0, "avg": 0.0})
        prev = st["size"]
        new = prev + signed
        if prev == 0 or (prev > 0) == (signed > 0):
            if abs(new) > 1e-12:
                st["avg"] = (
                    (st["avg"] * abs(prev) + price * abs(signed)) / abs(new)
                )
            else:
                st["avg"] = 0.0
        else:
            closing = min(abs(prev), abs(signed))
            sign = 1.0 if prev > 0 else -1.0
            realized_running += (price - st["avg"]) * sign * closing
            if abs(new) < 1e-12:
                st["avg"] = 0.0
            elif (new > 0) != (prev > 0):
                st["avg"] = price
        st["size"] = new
        raw_points.append((ts, realized_running, fees_running))

    if raw_points and equity_points > 0 and len(raw_points) > equity_points:
        step = max(1, len(raw_points) // equity_points)
        raw_points = raw_points[::step]
    # Append the live "now" point with the current unrealised so the
    # last datum on the chart is the latest broker-truth equity.
    if open_shares:
        import time as _time
        raw_points.append((
            _time.time(),
            realized_running,
            fees_running,
        ))

    equity_curve = [
        {
            "ts": ts,
            "realized_pnl_usd": rp,
            "fees_paid_usd": fees,
        }
        for (ts, rp, fees) in raw_points
    ]

    kpis = {
        "open_positions": len(positions_payload),
        "closed_shares": len(closed_only),
        "trades_count": int(trades_count or 0),
        "wins": win_count,
        "losses": loss_count,
        "total_realized_usd": float(realized_total),
        "total_unrealized_usd": float(unrealized_total),
        "fees_usd": float(total_fees),
        "funding_usd": float(total_funding),
        "last_trade_at": last_trade_at,
    }

    return {
        "ok": True,
        "strategy_id": strategy_id,
        "kpis": kpis,
        "positions": positions_payload,
        "orders": orders_payload,
        "fills": fills_payload,
        "equity_curve": equity_curve,
    }


def _order_row_summary(order: Any) -> dict[str, Any]:
    """Project an OrderTracker row down to the dashboard-friendly shape."""

    asdict = getattr(order, "asdict", None)
    if callable(asdict):
        d = asdict()
    else:
        d = {
            "order_id": getattr(order, "order_id", None),
            "client_order_id": getattr(order, "client_order_id", None),
            "venue_order_id": getattr(order, "venue_order_id", None),
            "account_id": getattr(order, "account_id", None),
            "strategy_id": getattr(order, "strategy_id", None),
            "market": getattr(order, "market", None),
            "side": getattr(order, "side", None),
            "size_base": getattr(order, "size_base", None),
            "price": getattr(order, "price", None),
            "order_type": getattr(order, "order_type", None),
            "state": getattr(order, "state", None),
            "filled_size": getattr(order, "filled_size", None),
            "avg_price": getattr(order, "avg_price", None),
            "fee_usd": getattr(order, "fee_usd", None),
            "created_at": getattr(order, "created_at", None),
            "updated_at": getattr(order, "updated_at", None),
        }
    return {
        "order_id": d.get("order_id"),
        "venue_order_id": d.get("venue_order_id") or d.get("exchange_order_id"),
        "client_order_id": d.get("client_order_id"),
        "account_id": d.get("account_id"),
        "strategy_id": d.get("strategy_id"),
        "market": d.get("market"),
        "side": d.get("side"),
        "size_base": float(d.get("size_base") or 0.0),
        "price": float(d.get("price") or 0.0) if d.get("price") is not None else None,
        "order_type": d.get("order_type") or d.get("type"),
        "state": d.get("state") or d.get("status"),
        "filled_size": float(d.get("filled_size") or 0.0),
        "avg_price": float(d.get("avg_price") or 0.0)
        if d.get("avg_price") is not None
        else None,
        "fee_usd": float(d.get("fee_usd") or 0.0)
        if d.get("fee_usd") is not None
        else None,
        "created_at": float(d.get("created_at") or 0.0),
        "updated_at": float(d.get("updated_at") or 0.0),
    }


def _backtests_root(strategy_root: Path) -> Path:
    return strategy_root / "backtests"


def _safe_backtest_dir(strategy_root: Path, ts: str) -> Path:
    root = _backtests_root(strategy_root).resolve()
    run_dir = (root / ts).resolve()
    if root not in run_dir.parents and run_dir != root:
        raise TradingError("invalid backtest timestamp")
    if not run_dir.exists() or not run_dir.is_dir():
        raise TradingError("unknown backtest run")
    return run_dir


def _safe_backtest_dir_for_payload(paths, payload: dict[str, Any]) -> Path:
    sid = str(payload.get("strategy_id") or "").strip()
    ts = str(payload.get("ts") or "").strip()
    proposal_id = str(payload.get("proposal_id") or "").strip()
    if not sid or not ts:
        raise TradingError("strategy_id and ts are required")
    if proposal_id:
        proposals_root = paths.proposals.resolve()
        proposal_root = (proposals_root / proposal_id).resolve()
        if proposals_root not in proposal_root.parents and proposal_root != proposals_root:
            raise TradingError("invalid proposal id")
        strategy_root = (proposal_root / "after" / "strategies" / sid).resolve()
        if proposal_root not in strategy_root.parents:
            raise TradingError("invalid proposal strategy path")
        return _safe_backtest_dir(strategy_root, ts)
    return _safe_backtest_dir(paths.strategy(sid), ts)


def _load_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


__all__ = ["routes"]
