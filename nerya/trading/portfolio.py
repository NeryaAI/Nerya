"""Portfolio read model — aggregates ledgers + open positions + pnl."""

from __future__ import annotations

from typing import Any

from ..core.paths import WorkspacePaths
from .accounts import load_accounts
from .virtual_ledger import open_ledger


def get_portfolio_summary(paths: WorkspacePaths) -> dict[str, Any]:
    accts = load_accounts(paths)
    out: dict[str, Any] = {"accounts": [], "totals": {"cash_usd": 0, "equity_usd": 0}}
    for a in accts.values():
        ledger = open_ledger(paths, a.id, a.initial_balance_usd)
        snap = ledger.snapshot()
        eq = ledger.equity_estimate()
        out["accounts"].append({
            "id": a.id, "mode": a.mode,
            "live_trading_enabled": a.is_live,
            "cash_usd": snap["cash_usd"],
            "equity_usd": eq,
            "positions": snap["positions"],
            "trade_count": snap["trade_count"],
            "fees_paid_usd": snap["fees_paid_usd"],
        })
        out["totals"]["cash_usd"] += snap["cash_usd"]
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
    realized = 0.0
    for a in summary["accounts"]:
        for pos in a["positions"].values():
            realized += float(pos.get("realized_pnl_usd", 0))
    return {"realized_usd": realized, "equity_usd": summary["totals"]["equity_usd"]}
