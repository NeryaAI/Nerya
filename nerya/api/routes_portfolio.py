"""Read-only portfolio + strategy endpoints for the dashboard.

These are thin wrappers over existing SDK/skill primitives so the dashboard
can render real data (positions, equity, strategy list) without having to
go through a permission-gated /skills/call payload.
"""

from __future__ import annotations

from typing import Any

from ..core import jsonl
from ..trading import portfolio as portfolio_mod
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


def routes():
    def summary(client, _payload):
        return portfolio_mod.get_portfolio_summary(client.config.paths)

    def positions(client, _payload):
        return {"positions": portfolio_mod.get_positions(client.config.paths)}

    def pnl(client, _payload):
        return portfolio_mod.get_pnl(client.config.paths)

    def strategies_list(client, _payload):
        items = []
        for s in list_strategies(client.config.paths):
            card = _summarize_strategy_ledgers(client.config.paths, s.id)
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
            for r in jsonl.read_all(fp):
                fill = r.get("fill") or {}
                rows.append({
                    "strategy_id": s.id,
                    "ts": fill.get("ts") or r.get("ts"),
                    "market": fill.get("market") or fill.get("symbol"),
                    "side": fill.get("side"),
                    "type": fill.get("type") or fill.get("order_type") or "MARKET",
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
