"""Track outcome of an open session."""

from __future__ import annotations

from typing import Any

from ..core.paths import WorkspacePaths
from . import store
from .session_writer import close_session


def track_outcome(paths: WorkspacePaths, strategy_id: str, session_id: str) -> dict[str, Any]:
    fills = [r for r in store.read_ledger(paths, strategy_id, "fills")
             if r.get("session_id") == session_id]
    notional = sum((f.get("fill", {}).get("price", 0) * f.get("fill", {}).get("size", 0))
                   for f in fills)
    fee = sum(f.get("fill", {}).get("fee_usd", 0) for f in fills)
    outcome = {
        "fills_count": len(fills),
        "gross_notional_usd": round(notional, 2),
        "fees_usd": round(fee, 4),
    }
    close_session(paths, strategy_id, session_id, outcome=outcome)
    return outcome
