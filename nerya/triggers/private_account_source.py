"""Private account observation Source.

It reads the existing persisted live/canary/shadow snapshots and order tracker
state. The source never places orders and never exposes credentials. It emits
only changes from the last fingerprinted observation.
"""
from __future__ import annotations

import hashlib
import json
import time
from typing import Any

from ..agent.task_control import dispatch_trigger, record_trigger_source_health
from ..trading.accounts import load_account_profiles
from ..trading.account_snapshots import latest_snapshot
from ..trading.order_tracker import OrderTracker
from ..trading.reconciliation import ReconciliationStore


def _fingerprint(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, default=str, separators=(",", ":")).encode()).hexdigest()


def _watch_state(paths, account_id: str) -> dict[str, Any] | None:
    from ..db.sqlite import connect
    with connect(paths.db) as con:
        row = con.execute("SELECT * FROM private_account_watch_state WHERE account_id=?", (account_id,)).fetchone()
    return dict(row) if row else None


def _save_state(paths, account_id: str, snapshot_id: str, snapshot_ts: float, fingerprint: str, order_cursor: str, event_ts: float) -> None:
    from ..db.sqlite import connect
    with connect(paths.db) as con:
        con.execute("""INSERT INTO private_account_watch_state
            (account_id,last_snapshot_id,last_snapshot_ts,last_fingerprint,last_order_cursor,last_event_ts,updated_at)
            VALUES (?,?,?,?,?,?,?) ON CONFLICT(account_id) DO UPDATE SET last_snapshot_id=excluded.last_snapshot_id,
            last_snapshot_ts=excluded.last_snapshot_ts,last_fingerprint=excluded.last_fingerprint,last_order_cursor=excluded.last_order_cursor,
            last_event_ts=excluded.last_event_ts,updated_at=excluded.updated_at""",
                     (account_id, snapshot_id, snapshot_ts, fingerprint, order_cursor, event_ts, event_ts))
        con.commit()


def _event_for_change(account_id: str, kind: str, payload: dict[str, Any], cursor: str, now: float) -> dict[str, Any]:
    event_id = "evt_account_" + hashlib.sha256(f"{account_id}:{kind}:{cursor}".encode()).hexdigest()[:32]
    return {"event_id": event_id, "source": "private_account", "kind": kind,
            "source_cursor": cursor, "idempotency_key": event_id,
            "occurred_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now)),
            "payload": {"account_id": account_id, "mode": "private_read", **payload}}


def poll(config, *, account_id: str | None = None, now: float | None = None) -> dict[str, Any]:
    paths, now = config.paths, float(now if now is not None else time.time())
    emitted, errors = [], []
    profiles = load_account_profiles(paths)
    for aid, profile in profiles.items():
        if account_id and aid != account_id:
            continue
        if profile.mode not in {"live", "canary", "shadow"} or not profile.permissions.read_balances:
            continue
        try:
            snapshot = latest_snapshot(paths, aid, readonly=True)
            if snapshot is None:
                raise ValueError("private_account_snapshot_missing")
            tracker = OrderTracker(paths)
            orders = [order.asdict() for order in tracker.active_orders(account_id=aid)]
            orders.extend(order.asdict() for order in tracker.cached_orders(account_id=aid, now=now))
            tracker.close()
            state = _watch_state(paths, aid)
            snapshot_payload = {
                "snapshot_id": snapshot.snapshot_id, "snapshot_ts": snapshot.ts,
                "health": snapshot.health, "nav_usd": snapshot.nav_usd,
                "free_by_asset": snapshot.free_by_asset, "positions": snapshot.meta.get("positions", []),
                "open_orders": snapshot.meta.get("open_orders", []),
            }
            snapshot_fp = _fingerprint(snapshot_payload)
            orders_fp = _fingerprint(orders)
            if not state or state.get("last_snapshot_id") != snapshot.snapshot_id:
                if state and state.get("last_snapshot_id"):
                    emitted.append(dispatch_trigger(paths, _event_for_change(aid, "account.snapshot_changed", snapshot_payload, snapshot.snapshot_id, now), config=config))
            if state and state.get("last_order_cursor") != orders_fp:
                emitted.append(dispatch_trigger(paths, _event_for_change(aid, "order.updated", {"orders": orders}, orders_fp, now), config=config))
                for order in orders:
                    emitted.append(dispatch_trigger(paths, _event_for_change(aid, "order.updated", order, str(order.get("order_id") or orders_fp), now), config=config))
            worst = ReconciliationStore(paths, readonly=True).worst_recent(account_id=aid, within_seconds=300)
            if worst is not None and (not state or state.get("last_event_ts") is None or float(state.get("last_event_ts") or 0) < float(worst.ts)):
                emitted.append(dispatch_trigger(paths, _event_for_change(aid, "reconciliation.alert", worst.as_dict(), worst.report_id, now), config=config))
            _save_state(paths, aid, snapshot.snapshot_id, snapshot.ts, snapshot_fp, orders_fp, now)
            record_trigger_source_health(paths, source="private_account", cursor=f"{aid}:{snapshot.snapshot_id}", state="ready")
        except Exception as exc:
            error = f"{type(exc).__name__}: {exc}"
            errors.append({"account_id": aid, "error": error})
            record_trigger_source_health(paths, source="private_account", cursor=aid, state="degraded", error=error, increment_failure=True)
    return {"ok": not errors, "source": "private_account", "emitted": emitted, "errors": errors}


__all__ = ["poll"]
