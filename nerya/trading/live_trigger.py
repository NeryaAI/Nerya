"""Fail-closed admission for live strategy Trigger events.

This guard only admits a strategy tick to the existing runner. It does not
approve an order and does not call a connector write method. Trade intents
still pass through RiskGate and ApprovalGate in the normal submit path.
"""
from __future__ import annotations

from typing import Any

from ..strategies.package import load_package
from .accounts import get_account_profile
from .account_snapshots import fresh_snapshot
from .reconciliation import ReconciliationStore


def admit_live_trigger(config, strategy_id: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
    payload = dict(payload or {})
    blockers: list[str] = []
    if not config.live_trading_enabled():
        blockers.append("runtime_live_trading_disabled")
    if config.kill_switch():
        blockers.append("kill_switch_active")
    try:
        package = load_package(config.paths, strategy_id)
    except Exception as exc:
        return {"allowed": False, "status": "blocked", "blockers": [f"strategy_unavailable:{type(exc).__name__}"]}
    if package.manifest.mode != "live":
        blockers.append("strategy_mode_not_live")
    account_ids = list(package.manifest.accounts)
    requested = str(payload.get("account_id") or "").strip()
    if requested:
        account_ids = [requested] if requested in account_ids else []
    if len(account_ids) != 1:
        blockers.append("live_trigger_requires_one_declared_account")
        return {"allowed": False, "status": "blocked", "strategy_id": strategy_id, "blockers": blockers, "account_id": None}
    account_id = account_ids[0]
    try:
        profile = get_account_profile(config.paths, account_id)
        if profile.mode != "live":
            blockers.append("account_mode_not_live")
        if not profile.live_trading_enabled:
            blockers.append("account_live_trading_disabled")
        if not profile.can_place_order:
            blockers.append("account_place_order_permission_missing")
        snapshot = fresh_snapshot(config, account_id, profile=profile, persist=True)
        if snapshot.source != "live":
            blockers.append("account_snapshot_not_live")
        if snapshot.health != "ok":
            blockers.append(f"account_snapshot_{snapshot.health}")
    except Exception as exc:
        snapshot = None
        blockers.append(f"account_state_unavailable:{type(exc).__name__}")
    try:
        worst = ReconciliationStore(config.paths, readonly=True).worst_recent(account_id=account_id, within_seconds=1800)
        if worst is not None and worst.severity == "trading_halted":
            blockers.append("reconciliation_trading_halted")
        elif worst is not None and worst.severity == "action_required":
            blockers.append("reconciliation_action_required")
    except Exception:
        blockers.append("reconciliation_state_unavailable")
    return {"allowed": not blockers, "status": "allowed" if not blockers else "blocked",
            "strategy_id": strategy_id, "account_id": account_id, "blockers": blockers,
            "snapshot_id": snapshot.snapshot_id if snapshot is not None else None,
            "approval": "trade_intent_approval_gate"}


__all__ = ["admit_live_trigger"]
