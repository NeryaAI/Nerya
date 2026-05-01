"""Reconstruct a single trade's full context."""

from __future__ import annotations

from typing import Any

from ..core.paths import WorkspacePaths
from . import store
from .attribution import attribute_session, execution_quality


def explain_trade(paths: WorkspacePaths, strategy_id: str, order_id: str) -> dict[str, Any]:
    orders = store.read_ledger(paths, strategy_id, "orders")
    match = next(
        (r for r in orders if (r.get("payload") or {}).get("order_id") == order_id),
        None,
    )
    if not match:
        return {"strategy_id": strategy_id, "order_id": order_id,
                "found": False, "reason": "order not found"}
    sid = match.get("session_id")
    intent_id = (match["payload"] or {}).get("intent_id")

    trigger = next((r for r in store.read_ledger(paths, strategy_id, "triggers")
                    if r.get("session_id") == sid), None)
    intent = next((r for r in store.read_ledger(paths, strategy_id, "intents")
                   if (r.get("intent") or {}).get("intent_id") == intent_id), None)
    risk = next((r for r in store.read_ledger(paths, strategy_id, "risk")
                 if (r.get("risk_decision") or {}).get("intent_id") == intent_id), None)
    fills = [r for r in store.read_ledger(paths, strategy_id, "fills")
             if (r.get("fill") or {}).get("order_id") == order_id]
    messages = [r for r in store.read_ledger(paths, strategy_id, "messages")
                if r.get("session_id") == sid]
    reviews = [r for r in store.read_ledger(paths, strategy_id, "reviews")
               if r.get("session_id") == sid]

    try:
        attribution = attribute_session(paths, strategy_id, sid).as_dict() if sid else {}
    except Exception:
        attribution = {}
    try:
        exec_quality = execution_quality(paths, strategy_id, sid) if sid else {}
    except Exception:
        exec_quality = {}
    version_id: str | None = None
    try:
        import json as _json
        pointer = paths.strategy(strategy_id) / "active_version.json"
        if pointer.exists():
            doc = _json.loads(pointer.read_text(encoding="utf-8"))
            v = doc.get("version_id")
            version_id = str(v) if v else None
    except Exception:
        version_id = None

    return {
        "strategy_id": strategy_id,
        "session_id": sid,
        "order_id": order_id,
        "intent_id": intent_id,
        "active_version_id": version_id,
        "trigger": trigger,
        "intent": intent,
        "risk": risk,
        "order": match,
        "fills": fills,
        "messages": messages,
        "reviews": reviews,
        "attribution": attribution,
        "execution_quality": exec_quality,
        "found": True,
    }
