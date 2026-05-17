"""Read-only portfolio + strategy endpoints for the dashboard.

These are thin wrappers over existing SDK/skill primitives so the dashboard
can render real data (positions, equity, strategy list) without having to
go through a permission-gated /skills/call payload.
"""

from __future__ import annotations

from typing import Any

from ..core import jsonl
from ..trading import portfolio as portfolio_mod
from ..trading.position_book import PositionBook
from ..trading.strategies import list_strategies


def _summarize_strategy_ledgers(paths, strategy_id: str) -> dict[str, Any]:
    """Compute a light strategy scorecard from on-disk ledgers."""
    sh_root = paths.strategy_history(strategy_id)
    fills_path = sh_root / "fills.jsonl"
    pnl_path = sh_root / "pnl.jsonl"
    intents_path = sh_root / "intents.jsonl"

    fills = jsonl.read_all(fills_path) if fills_path.exists() else []
    pnl_rows = jsonl.read_all(pnl_path) if pnl_path.exists() else []
    intents = jsonl.read_all(intents_path) if intents_path.exists() else []

    realized = 0.0
    fees = 0.0
    wins = 0
    losses = 0
    for row in pnl_rows:
        p = (row.get("pnl") or {})
        realized += float(p.get("realized_usd") or p.get("realized") or 0.0)
        fees += float(p.get("fees_usd") or 0.0)
        r = float(p.get("realized_usd") or p.get("realized") or 0.0)
        if r > 0:
            wins += 1
        elif r < 0:
            losses += 1

    decided = max(1, wins + losses)
    win_rate = 100.0 * wins / decided if (wins + losses) else 0.0

    return {
        "strategy_id": strategy_id,
        "fills_count": len(fills),
        "intents_count": len(intents),
        "realized_pnl_usd": round(realized, 4),
        "fees_usd": round(fees, 4),
        "wins": wins,
        "losses": losses,
        "win_rate_pct": round(win_rate, 2),
    }


def _summarize_positions_by_strategy(
    positions: list[dict[str, Any]],
) -> dict[str, dict[str, float | int]]:
    """Roll open-position metrics up by strategy id."""
    totals: dict[str, dict[str, float | int]] = {}
    for row in positions:
        strategy_id = str(row.get("strategy_id") or "").strip()
        if not strategy_id:
            continue
        item = totals.setdefault(
            strategy_id,
            {"unrealized_pnl_usd": 0.0, "open_positions_count": 0},
        )
        item["unrealized_pnl_usd"] = float(item["unrealized_pnl_usd"]) + float(
            row.get("unrealized_pnl_usd") or 0.0
        )
        item["open_positions_count"] = int(item["open_positions_count"]) + 1
    for item in totals.values():
        item["unrealized_pnl_usd"] = round(float(item["unrealized_pnl_usd"]), 4)
    return totals


def _summarize_position_book_by_strategy(paths) -> dict[str, dict[str, float | int]]:
    """Roll durable PositionBook PnL up by strategy id.

    Strategy cards need per-strategy attribution; after v6 the
    merged ``positions`` row no longer carries a strategy_id, so we
    drive the rollup from the ``position_shares`` table (each row is
    a strategy's slice of a merged position).

    Unrealized PnL on an open share is allocated *pro-rata*: each
    share gets its size's share of the merged unrealized — which
    matches what the strategy's own avg-entry vs mark would compute,
    because the merged unrealized was itself derived from the same
    cost-basis-weighted average.
    """
    totals: dict[str, dict[str, float | int]] = {}
    try:
        book = PositionBook(paths)
        shares = book.list_shares_history(limit=100_000)
    except Exception:
        return totals

    # Cache merged position lookup so we don't hit the DB per share.
    merged_by_id: dict[str, Any] = {}

    def _merged(pos_id: str):
        cached = merged_by_id.get(pos_id)
        if cached is not None:
            return cached
        m = book.get_by_id(pos_id)
        merged_by_id[pos_id] = m
        return m

    for sh in shares:
        strategy_id = str(sh.strategy_id or "").strip()
        if not strategy_id:
            continue
        item = totals.setdefault(
            strategy_id,
            {
                "realized_pnl_usd": 0.0,
                "unrealized_pnl_usd": 0.0,
                "fees_usd": 0.0,
                "wins": 0,
                "losses": 0,
                "open_positions_count": 0,
            },
        )
        realized = float(sh.realized_pnl_share_usd or 0.0)
        item["realized_pnl_usd"] = float(item["realized_pnl_usd"]) + realized
        item["fees_usd"] = float(item["fees_usd"]) + float(sh.fees_share_usd or 0.0)
        if realized > 0:
            item["wins"] = int(item["wins"]) + 1
        elif realized < 0:
            item["losses"] = int(item["losses"]) + 1
        if sh.is_open:
            merged = _merged(sh.position_id)
            if merged is not None and merged.is_open:
                mark = float(merged.mark_price or merged.avg_entry_price or 0.0)
                avg = float(sh.avg_entry_share_price or 0.0)
                size = float(sh.size_share_base or 0.0)
                if mark and avg and size:
                    side_factor = 1.0 if size >= 0 else -1.0
                    unreal = (mark - avg) * abs(size) * side_factor
                    item["unrealized_pnl_usd"] = (
                        float(item["unrealized_pnl_usd"]) + unreal
                    )
            item["open_positions_count"] = int(item["open_positions_count"]) + 1

    for item in totals.values():
        item["realized_pnl_usd"] = round(float(item["realized_pnl_usd"]), 4)
        item["unrealized_pnl_usd"] = round(float(item["unrealized_pnl_usd"]), 4)
        item["fees_usd"] = round(float(item["fees_usd"]), 4)
    return totals


def _position_unrealized_usd(pos) -> float:
    size = float(getattr(pos, "size_base", 0.0) or 0.0)
    avg = float(getattr(pos, "avg_entry_price", 0.0) or 0.0)
    mark = float(getattr(pos, "mark_price", None) or avg or 0.0)
    if not size or not avg or not mark:
        return float(getattr(pos, "unrealized_pnl_usd", 0.0) or 0.0)
    side = str(getattr(pos, "side", "") or "").lower()
    side_factor = -1.0 if side == "short" or size < 0 else 1.0
    return (mark - avg) * abs(size) * side_factor


def _strategy_intent_index(paths, strategy_id: str) -> dict[str, dict[str, Any]]:
    intents_path = paths.strategy_history(strategy_id) / "intents.jsonl"
    if not intents_path.exists():
        return {}
    out: dict[str, dict[str, Any]] = {}
    for row in jsonl.read_all(intents_path):
        intent = row.get("intent") or {}
        if not isinstance(intent, dict):
            continue
        intent_id = str(intent.get("intent_id") or "").strip()
        if intent_id and intent_id not in out:
            out[intent_id] = intent
    return out


def routes():
    def summary(client, _payload):
        return portfolio_mod.get_portfolio_summary(client.config.paths)

    def positions(client, _payload):
        return {"positions": portfolio_mod.get_positions(client.config.paths)}

    def pnl(client, _payload):
        return portfolio_mod.get_pnl(client.config.paths)

    def strategies_list(client, _payload):
        items = []
        try:
            position_totals = _summarize_position_book_by_strategy(client.config.paths)
        except Exception:
            position_totals = {}
        for s in list_strategies(client.config.paths):
            card = _summarize_strategy_ledgers(client.config.paths, s.id)
            positions = position_totals.get(
                s.id,
                {
                    "realized_pnl_usd": card.get("realized_pnl_usd") or 0.0,
                    "unrealized_pnl_usd": 0.0,
                    "fees_usd": card.get("fees_usd") or 0.0,
                    "wins": card.get("wins") or 0,
                    "losses": card.get("losses") or 0,
                    "open_positions_count": 0,
                },
            )
            realized = float(positions.get("realized_pnl_usd") or 0.0)
            unrealized = float(positions.get("unrealized_pnl_usd") or 0.0)
            wins = int(positions.get("wins") or 0)
            losses = int(positions.get("losses") or 0)
            if not wins and not losses:
                wins = int(card.get("wins") or 0)
                losses = int(card.get("losses") or 0)
            win_rate = round(100.0 * wins / (wins + losses), 2) if (wins + losses) else 0.0
            items.append({
                "id": s.id,
                "title": s.title,
                "status": s.status,
                "account_id": s.account_id,
                "markets": s.markets,
                "paper_trading_enabled": s.paper_trading_enabled,
                "live_trading_enabled": s.live_trading_enabled,
                "trigger_kinds": s.trigger_kinds,
                **card,
                "realized_pnl_usd": round(realized, 4),
                "fees_usd": round(float(positions.get("fees_usd") or 0.0), 4),
                "wins": wins,
                "losses": losses,
                "win_rate_pct": win_rate,
                "unrealized_pnl_usd": round(unrealized, 4),
                "total_pnl_usd": round(realized + unrealized, 4),
                "open_positions_count": int(
                    positions.get("open_positions_count") or 0
                ),
            })
        return {"strategies": items}

    def recent_trades(client, payload):
        limit = int(payload.get("limit", 25))
        paths = client.config.paths
        rows: list[dict[str, Any]] = []
        for s in list_strategies(paths):
            fp = paths.strategy_history(s.id) / "fills.jsonl"
            if not fp.exists():
                continue
            intents = _strategy_intent_index(paths, s.id)
            for r in jsonl.read_all(fp):
                fill = r.get("fill") or {}
                intent = intents.get(str(fill.get("intent_id") or ""), {})
                rows.append({
                    "strategy_id": s.id,
                    "ts": fill.get("ts") or r.get("ts"),
                    "market": fill.get("market") or fill.get("symbol") or intent.get("market"),
                    "side": fill.get("side") or intent.get("side"),
                    "type": (
                        fill.get("type")
                        or fill.get("order_type")
                        or intent.get("order_type")
                        or "MARKET"
                    ),
                    "size": fill.get("size"),
                    "price": fill.get("price"),
                    "fee_usd": fill.get("fee_usd"),
                    "status": fill.get("status") or "FILLED",
                    "order_id": fill.get("order_id"),
                })
        # newest first
        rows.sort(key=lambda x: str(x.get("ts") or ""), reverse=True)
        return {"trades": rows[:limit]}

    def equity_curve(client, payload):
        """Build an equity curve from pnl ledgers.

        The curve is the cumulative realized PnL across all strategies
        plus the current cash per portfolio summary as the final point.
        Good enough for the dashboard chart; more sophisticated ones
        can be layered on top later.
        """
        limit = int(payload.get("limit", 120))
        paths = client.config.paths
        all_rows: list[tuple[str, float]] = []
        for s in list_strategies(paths):
            fp = paths.strategy_history(s.id) / "pnl.jsonl"
            if not fp.exists():
                continue
            for r in jsonl.read_all(fp):
                p = r.get("pnl") or {}
                ts = r.get("ts") or ""
                realized = float(p.get("realized_usd") or p.get("realized") or 0.0)
                all_rows.append((str(ts), realized))
        all_rows.sort(key=lambda x: x[0])

        # cumulative
        base = float(portfolio_mod.get_portfolio_summary(paths)
                     .get("totals", {}).get("equity_usd", 0.0))
        equity = base
        points: list[dict[str, Any]] = []
        # Start from the earliest point and walk forward so the final value
        # matches `base`.
        running = base - sum(v for _, v in all_rows)
        for ts, realized in all_rows[-limit:]:
            running += realized
            points.append({"ts": ts, "equity_usd": round(running, 4)})
        if not points:
            points.append({"ts": "", "equity_usd": round(equity, 4)})
        return {"points": points, "equity_usd": round(equity, 4)}

    return [
        ("POST", "/portfolio/summary", summary),
        ("POST", "/portfolio/positions", positions),
        ("POST", "/portfolio/pnl", pnl),
        ("POST", "/portfolio/equity_curve", equity_curve),
        ("POST", "/strategy/list", strategies_list),
        ("POST", "/trading/recent_trades", recent_trades),
    ]
