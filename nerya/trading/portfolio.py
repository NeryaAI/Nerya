"""Portfolio read model — aggregates ledgers + open positions + pnl."""

from __future__ import annotations

from typing import Any
from contextlib import closing
import json
import sqlite3

from ..core.paths import WorkspacePaths
from .accounts import load_accounts, load_account_profiles
from .account_snapshots import latest_snapshot, usd_cash_value, position_instruments, display_snapshot, DEFAULT_MAX_AGE_S
from .position_book import Position, PositionBook
from .virtual_ledger import open_ledger


def get_portfolio_summary(paths: WorkspacePaths) -> dict[str, Any]:
    accts = load_account_profiles(paths)
    out: dict[str, Any] = {"accounts": [], "totals": {"cash_usd": 0, "equity_usd": 0}}
    for a in accts.values():
        from ..connectors.provider_spec import get_registry
        spec = get_registry(paths.root).find(a.venue)
        strategy_scope = bool(spec and spec.supports.get('strategy_pnl_scope'))
        ledger = open_ledger(paths, a.id, a.initial_balance_usd)
        snap = ledger.snapshot()
        book_positions = _book_positions(paths, a.id)
        marks = _marks_by_market(book_positions)
        eq = ledger.equity_estimate(marks)
        latest = display_snapshot(paths, a, latest_snapshot(paths, a.id))
        marks = _marks_by_market(book_positions, latest)
        cash_usd = float(snap["cash_usd"])
        available_usd = locked_usd = None
        if latest is not None:
            eq = float(latest.nav_usd)
            cash_usd = _snapshot_cash_usd(latest.cash_by_asset)
            available_usd = usd_cash_value(latest.free_by_asset)
            locked_usd = usd_cash_value(latest.locked_by_asset)
            if latest.health not in ('ok', 'stale'):
                cash_usd = available_usd = locked_usd = eq = None
        elif strategy_scope and a.mode != 'paper':
            cash_usd = eq = None
        positions = _positions_for_account(
            snap["positions"], book_positions, marks, paths=paths,
        )
        instruments = position_instruments(paths, a, latest)
        for row in positions.values():
            decorate_instrument(row, latest, defaults=spec.instrument_defaults if spec else None, instruments=instruments)
        fees = (_prediction_fees(paths, a.id, snap['trade_count'] or len(book_positions))
                if strategy_scope and a.mode != 'paper' else snap['fees_paid_usd'])
        out["accounts"].append({
            "id": a.id, "mode": a.mode,
            "kind": a.kind,
            'pnl_scope': 'strategy' if strategy_scope else 'account',
            "live_trading_enabled": a.is_real_money and a.live_trading_enabled,
            "cash_usd": cash_usd,
            "available_usd": available_usd, "locked_usd": locked_usd,
            "equity_usd": eq,
            "positions": positions,
            "trade_count": snap["trade_count"],
            "fees_paid_usd": fees,
            "fees_status": "unknown" if fees is None else "known",
            "snapshot": latest.asdict() if latest else None,
        })
        for name, value in (('cash_usd', cash_usd), ('equity_usd', eq)):
            total = out['totals'][name]
            out['totals'][name] = total + value if total is not None and value is not None else None
    return out


def get_positions(paths: WorkspacePaths) -> list[dict]:
    summary = get_portfolio_summary(paths)
    out = []
    for a in summary["accounts"]:
        for market, pos in a["positions"].items():
            if pos.get("size"):
                out.append({"account_id": a["id"], **pos})
    return out


def get_pnl(paths: WorkspacePaths) -> dict:
    summary = get_portfolio_summary(paths)
    initial_equity = _initial_equity_usd(paths)
    equity = summary["totals"]["equity_usd"]
    unrealized = _summary_unrealized_usd(summary)
    total_pnl = equity - initial_equity if equity is not None else None
    realized_net = total_pnl - unrealized if total_pnl is not None and unrealized is not None else None
    gross_realized, fees, funding = _position_book_pnl_components(paths, summary)
    if any(a.get('pnl_scope') == 'strategy' for a in summary['accounts']):
        profiles = load_accounts(paths)
        parts = []
        for account in summary['accounts']:
            if account.get('pnl_scope') != 'strategy':
                value = account['equity_usd']
                parts.append(value - profiles[account['id']].initial_balance_usd if value is not None else None)
                continue
            # Whole-account NAV can include deposits and external holdings;
            # prediction performance belongs to the local strategy book only.
            values = [row.get('unrealized_pnl_usd') for row in account['positions'].values()]
            fee = account['fees_paid_usd']
            if fee is None or any(value is None for value in values):
                parts.append(None)
                continue
            try:
                history = PositionBook(paths).history(account_id=account['id'], limit=1_000_000)
                realized = sum(float(p.realized_pnl_usd or 0) for p in history)
                parts.append(realized - fee + sum(values))
            except Exception:
                parts.append(None)
        total_pnl = sum(parts) if all(value is not None for value in parts) else None
        realized_net = total_pnl - unrealized if total_pnl is not None and unrealized is not None else None
        if any(a.get('fees_status') == 'unknown' for a in summary['accounts']):
            fees = None
    return {
        "initial_equity_usd": initial_equity,
        "equity_usd": equity,
        "realized_usd": realized_net,
        "realized_net_usd": realized_net,
        "realized_gross_usd": gross_realized,
        "unrealized_usd": unrealized,
        "fees_usd": fees,
        "funding_usd": funding,
        "total_pnl_usd": total_pnl,
    }


def _book_positions(paths: WorkspacePaths, account_id: str) -> list[Position]:
    try:
        return PositionBook(paths).open_positions(account_id=account_id)
    except Exception:
        return []


def _marks_by_market(positions: list[Position], snapshot=None) -> dict[str, float]:
    marks: dict[str, float] = {}
    for pos in positions:
        if pos.mark_price is not None:
            marks[pos.market] = float(pos.mark_price)
    if snapshot and snapshot.meta.get('display_projection'):
        marks.update({market: float(mark) for market, mark in (snapshot.meta.get('marks') or {}).items()
                      if mark is not None})
    return marks


def _positions_for_account(
    ledger_positions: dict[str, Any],
    book_positions: list[Position],
    marks: dict[str, float],
    *,
    paths: WorkspacePaths | None = None,
) -> dict[str, dict[str, Any]]:
    if not book_positions:
        return _ledger_positions_with_marks(ledger_positions, marks)

    out = _position_book_rows(book_positions, marks, paths=paths)
    out.update(_ledger_only_positions(ledger_positions, book_positions, marks))
    return out


def _position_book_rows(
    book_positions: list[Position],
    marks: dict[str, float],
    *,
    paths: WorkspacePaths | None = None,
) -> dict[str, dict[str, Any]]:
    """Emit one row per merged (account, market) position.

    Each row carries:
    - the merged size / avg / mark / unrealized PnL (broker-truth view)
    - an embedded ``shares`` list with each strategy's individual
      slice (size, avg, realized PnL, fees, funding, mark-derived
      unrealized PnL). The UI uses this to render an expandable
      per-strategy breakdown without making a second round trip.

    ``paths`` is optional so callers that don't care about the share
    breakdown (e.g. legacy unit tests) skip the lookup.
    """
    out: dict[str, dict[str, Any]] = {}
    book = PositionBook(paths) if paths is not None else None
    for pos in book_positions:
        mark = marks.get(pos.market, pos.mark_price)
        # Post-v6 there's at most ONE merged row per (account, market).
        # The legacy fallback key (market:strategy:position_id) is kept
        # for the synthesised non-book ledger path only.
        key = pos.market
        row = {
            **pos.asdict(),
            "size": pos.size_base,
            "avg_price": pos.avg_entry_price,
            "mark_price": mark,
            "market_value_usd": abs(pos.size_base * mark) if mark is not None else None,
            "notional_usd": abs(pos.size_base * mark) if mark is not None else None,
            "unrealized_pnl_usd": _position_unrealized_usd(pos, mark),
        }
        row["shares"] = _shares_for_position(book, pos, mark) if book is not None else []
        out[key] = row
    return out


def _shares_for_position(
    book: PositionBook,
    pos: Position,
    mark: float | None,
) -> list[dict[str, Any]]:
    """Per-strategy share rows for one merged position.

    Unrealized PnL on a share is pro-rated from the merged unrealized
    using ``share.size_share_base / pos.size_base`` so the sum of share
    unrealized always equals the merged unrealized (avoids the UI
    showing a "ghost" PnL that doesn't reconcile to the merged row).
    """
    try:
        shares = book.list_shares(pos.position_id)
    except Exception:
        return []
    if not shares:
        return []
    merged_size = float(pos.size_base or 0.0)
    merged_unrealized = _position_unrealized_usd(pos, mark)
    out: list[dict[str, Any]] = []
    for share in shares:
        share_size = float(share.size_share_base or 0.0)
        if merged_unrealized is None:
            pro_rata = None
        elif merged_size:
            pro_rata = (share_size / merged_size) * merged_unrealized
        else:
            pro_rata = 0.0
        market_value = abs(share_size * mark) if mark is not None else None
        out.append({
            "strategy_id": share.strategy_id,
            "size_base": share_size,
            "avg_entry_price": float(share.avg_entry_share_price or 0.0),
            "realized_pnl_usd": float(share.realized_pnl_share_usd or 0.0),
            "fees_usd": float(share.fees_share_usd or 0.0),
            "funding_usd": float(share.funding_share_usd or 0.0),
            "market_value_usd": market_value,
            "notional_usd": market_value,
            "unrealized_pnl_usd": pro_rata,
            "opened_at": share.opened_at,
            "updated_at": share.updated_at,
        })
    return out


def _ledger_only_positions(
    ledger_positions: dict[str, Any],
    book_positions: list[Position],
    marks: dict[str, float],
) -> dict[str, dict[str, Any]]:
    book_markets = {pos.market for pos in book_positions}
    legacy_only = {
        market: pos
        for market, pos in (ledger_positions or {}).items()
        if str(market) not in book_markets
    }
    return _ledger_positions_with_marks(legacy_only, marks)


def _ledger_positions_with_marks(
    positions: dict[str, Any],
    marks: dict[str, float],
) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    for market, pos in (positions or {}).items():
        row = dict(pos or {})
        size = float(row.get("size") or 0.0)
        avg = float(row.get("avg_price") or 0.0)
        mark = marks.get(market, row.get('mark_price'))
        row.setdefault("market", market)
        row["mark_price"] = mark
        row["market_value_usd"] = abs(size * mark) if mark is not None else None
        row["notional_usd"] = row['market_value_usd']
        row["unrealized_pnl_usd"] = (mark - avg) * size if mark is not None else None
        out[str(market)] = row
    return out


def _snapshot_cash_usd(cash_by_asset: dict[str, float]) -> float | None:
    return usd_cash_value(cash_by_asset or {})


def _position_unrealized_usd(pos: Position, mark: float | None = None) -> float | None:
    size = float(pos.size_base or 0.0)
    avg = float(pos.avg_entry_price or 0.0)
    mark_value = mark if mark is not None else pos.mark_price
    if mark_value is None:
        return None
    side_factor = -1.0 if pos.side == "short" or size < 0 else 1.0
    return (mark_value - avg) * abs(size) * side_factor


def decorate_instrument(row, snapshot, *, defaults=None, instruments=None):
    metadata = (instruments or {}).get(row.get('market'))
    if metadata is None and snapshot:
        metadata = (snapshot.meta.get('instruments') or {}).get(row.get('market'))
    if metadata:
        row['instrument'] = dict(metadata)
        import time
        observed = metadata.get('as_of')
        stale = (time.time() - observed >= DEFAULT_MAX_AGE_S if observed is not None
                 else bool(snapshot and snapshot.is_stale()))
        if stale and row['instrument'].get('data_status') == 'available':
            row['instrument']['data_status'] = 'stale'
    elif defaults:
        row['instrument'] = {**defaults, 'data_status': 'unavailable'}
    return row


def _prediction_fees(paths, account_id, trade_count):
    """Read recorded strategy fees without turning unverified fees into zero."""
    from ..db.sqlite import connect_preview
    try:
        with closing(connect_preview(paths.db)) as con:
            rows = con.execute('SELECT fee_usd, meta_json FROM fills WHERE account_id = ?', (account_id,)).fetchall()
        if not rows:
            return None if trade_count else 0.0
        metadata = [json.loads(row['meta_json'] or '{}') for row in rows]
        if any(meta.get('fee_unknown') is not False and meta.get('fee_status') != 'chain_verified' for meta in metadata):
            return None
        return sum(float(row['fee_usd']) for row in rows)
    except (ValueError, KeyError, TypeError, sqlite3.Error):
        return None


def _initial_equity_usd(paths: WorkspacePaths) -> float:
    try:
        return sum(float(a.initial_balance_usd or 0.0) for a in load_accounts(paths).values())
    except Exception:
        return 0.0


def _summary_unrealized_usd(summary: dict[str, Any]) -> float | None:
    total = 0.0
    for account in summary.get("accounts") or []:
        snapshot = account.get("snapshot") or {}
        if account.get('pnl_scope') != 'strategy' and "unrealized_pnl_usd" in snapshot:
            total += float(snapshot.get("unrealized_pnl_usd") or 0.0)
            continue
        for pos in (account.get("positions") or {}).values():
            value = (pos or {}).get("unrealized_pnl_usd")
            if value is None:
                return None
            total += float(value)
    return total


def _position_book_pnl_components(
    paths: WorkspacePaths,
    summary: dict[str, Any],
) -> tuple[float, float, float]:
    try:
        positions = PositionBook(paths).history(limit=1_000_000)
    except Exception:
        positions = []
    if positions:
        gross_realized = sum(float(p.realized_pnl_usd or 0.0) for p in positions)
        fees = sum(float(p.fees_usd or 0.0) for p in positions)
        funding = sum(float(p.funding_usd or 0.0) for p in positions)
        return gross_realized, fees, funding

    gross_realized = 0.0
    fees = 0.0
    funding = 0.0
    for account in summary.get("accounts") or []:
        fees += float(account.get("fees_paid_usd") or 0.0)
        for pos in (account.get("positions") or {}).values():
            row = pos or {}
            gross_realized += float(row.get("realized_pnl_usd") or 0.0)
            funding += float(row.get("funding_usd") or 0.0)
    return gross_realized, fees, funding
