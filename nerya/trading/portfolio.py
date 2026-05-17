"""Portfolio read model — aggregates ledgers + open positions + pnl."""

from __future__ import annotations

from typing import Any

from ..core.paths import WorkspacePaths
from .accounts import load_accounts
from .account_snapshots import latest_snapshot
from .position_book import Position, PositionBook
from .virtual_ledger import open_ledger


_USD_STABLES = {"USDT", "USDC", "BUSD", "FDUSD", "USD", "TUSD", "DAI"}


def get_portfolio_summary(paths: WorkspacePaths) -> dict[str, Any]:
    accts = load_accounts(paths)
    out: dict[str, Any] = {"accounts": [], "totals": {"cash_usd": 0, "equity_usd": 0}}
    for a in accts.values():
        ledger = open_ledger(paths, a.id, a.initial_balance_usd)
        snap = ledger.snapshot()
        book_positions = _book_positions(paths, a.id)
        marks = _marks_by_market(book_positions)
        eq = ledger.equity_estimate(marks)
        latest = latest_snapshot(paths, a.id)
        cash_usd = float(snap["cash_usd"])
        if latest is not None:
            eq = float(latest.nav_usd)
            cash_usd = _snapshot_cash_usd(latest.cash_by_asset, fallback=cash_usd)
        positions = _positions_for_account(
            snap["positions"], book_positions, marks, paths=paths,
        )
        out["accounts"].append({
            "id": a.id, "mode": a.mode,
            "live_trading_enabled": a.is_live,
            "cash_usd": cash_usd,
            "equity_usd": eq,
            "positions": positions,
            "trade_count": snap["trade_count"],
            "fees_paid_usd": snap["fees_paid_usd"],
            "snapshot": latest.asdict() if latest else None,
        })
        out["totals"]["cash_usd"] += cash_usd
        out["totals"]["equity_usd"] += eq
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
    equity = float(summary["totals"]["equity_usd"])
    unrealized = _summary_unrealized_usd(summary)
    total_pnl = equity - initial_equity
    realized_net = total_pnl - unrealized
    gross_realized, fees, funding = _position_book_pnl_components(paths, summary)
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


def _marks_by_market(positions: list[Position]) -> dict[str, float]:
    marks: dict[str, float] = {}
    for pos in positions:
        mark = float(pos.mark_price or 0.0)
        if mark > 0:
            marks[pos.market] = mark
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
        mark = float(marks.get(pos.market) or pos.mark_price or pos.avg_entry_price or 0.0)
        # Post-v6 there's at most ONE merged row per (account, market).
        # The legacy fallback key (market:strategy:position_id) is kept
        # for the synthesised non-book ledger path only.
        key = pos.market
        row = {
            **pos.asdict(),
            "size": pos.size_base,
            "avg_price": pos.avg_entry_price,
            "mark_price": mark,
            "market_value_usd": abs(pos.size_base * mark),
            "notional_usd": abs(pos.size_base * mark),
            "unrealized_pnl_usd": _position_unrealized_usd(pos, mark),
        }
        row["shares"] = _shares_for_position(book, pos, mark) if book is not None else []
        out[key] = row
    return out


def _shares_for_position(
    book: PositionBook,
    pos: Position,
    mark: float,
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
        if merged_size:
            pro_rata = (share_size / merged_size) * merged_unrealized
        else:
            pro_rata = 0.0
        out.append({
            "strategy_id": share.strategy_id,
            "size_base": share_size,
            "avg_entry_price": float(share.avg_entry_share_price or 0.0),
            "realized_pnl_usd": float(share.realized_pnl_share_usd or 0.0),
            "fees_usd": float(share.fees_share_usd or 0.0),
            "funding_usd": float(share.funding_share_usd or 0.0),
            "notional_usd": abs(share_size * mark),
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
        mark = float(marks.get(market, avg) or avg or 0.0)
        row.setdefault("market", market)
        row["mark_price"] = mark
        row["market_value_usd"] = abs(size * mark)
        row["notional_usd"] = abs(size * mark)
        row["unrealized_pnl_usd"] = (mark - avg) * size if avg and mark else 0.0
        out[str(market)] = row
    return out


def _snapshot_cash_usd(cash_by_asset: dict[str, float], *, fallback: float) -> float:
    total = 0.0
    for asset, amount in (cash_by_asset or {}).items():
        if str(asset).upper() in _USD_STABLES:
            total += float(amount or 0.0)
    return total if total or not cash_by_asset else float(fallback)


def _position_unrealized_usd(pos: Position, mark: float | None = None) -> float:
    size = float(pos.size_base or 0.0)
    avg = float(pos.avg_entry_price or 0.0)
    mark_value = float(mark or pos.mark_price or avg or 0.0)
    if not size or not avg or not mark_value:
        return float(pos.unrealized_pnl_usd or 0.0)
    side_factor = -1.0 if pos.side == "short" or size < 0 else 1.0
    return (mark_value - avg) * abs(size) * side_factor


def _initial_equity_usd(paths: WorkspacePaths) -> float:
    try:
        return sum(float(a.initial_balance_usd or 0.0) for a in load_accounts(paths).values())
    except Exception:
        return 0.0


def _summary_unrealized_usd(summary: dict[str, Any]) -> float:
    total = 0.0
    for account in summary.get("accounts") or []:
        snapshot = account.get("snapshot") or {}
        if "unrealized_pnl_usd" in snapshot:
            total += float(snapshot.get("unrealized_pnl_usd") or 0.0)
            continue
        for pos in (account.get("positions") or {}).values():
            total += float((pos or {}).get("unrealized_pnl_usd") or 0.0)
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
