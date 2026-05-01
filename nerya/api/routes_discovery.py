"""Runtime-driven discovery endpoints.

Audit finding 4.10 / 6.1 / 7.1 / 7.2: the operator dashboard shipped
with hardcoded wallet / account / market / status / driver lists that
drifted from the runtime truth. These endpoints return a single
snapshot the dashboard can render without any hardcoded enums.

They are intentionally read-only and side-effect free — they just
glue existing registries together so every surface sees the same
catalog.
"""

from __future__ import annotations

from typing import Any

from .. import wallet as wallet_mod
from ..connectors.provider_spec import get_registry
from ..trading import accounts as accounts_mod
from ..trading.strategy_lifecycle import STATES as STRATEGY_STATES


_STRATEGY_DRIVERS: tuple[str, ...] = ("prompt", "script", "manual")
_STRATEGY_STATUSES: tuple[str, ...] = tuple(STRATEGY_STATES)


def _accounts_snapshot(client) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    accts = accounts_mod.load_accounts(client.config.paths)
    for acc in accts.values():
        out.append({
            "id": acc.id,
            "exchange": acc.exchange,
            "venue": acc.venue or acc.exchange,
            "kind": acc.kind,
            "mode": acc.mode,
            "status": acc.status,
            "live_trading_enabled": bool(acc.live_trading_enabled),
            "initial_balance_usd": float(acc.initial_balance_usd),
        })
    out.sort(key=lambda a: a["id"])
    return out


def _wallets_snapshot(client) -> dict[str, Any]:
    report = wallet_mod.readiness_report(
        client.config.data, workspace=client.config.paths.root,
    )
    active = ((client.config.data.get("wallet") or {}).get("provider") or "") or None
    ids: list[dict[str, Any]] = []
    for entry in report:
        pid = str(entry.get("id") or "")
        readiness = entry.get("readiness") or {}
        ids.append({
            "id": pid,
            "label": entry.get("label") or pid,
            "runtime": entry.get("runtime") or "",
            "ready": bool(readiness.get("ready")),
            "reason": readiness.get("reason") or "",
            "active": pid == active,
        })
    ids.sort(key=lambda x: x["id"])
    return {"providers": ids, "active": active}


def _venues_snapshot(_client) -> list[dict[str, Any]]:
    """Real venue catalog as seen by the runtime connector registry."""
    from .routes_market import _MOCK_REGISTRY_IDS  # type: ignore[attr-defined]

    specs = get_registry().list_specs()
    out: list[dict[str, Any]] = []
    for spec in specs:
        info = spec.to_info()
        vid = str(info.get("id") or "")
        if not vid or vid in _MOCK_REGISTRY_IDS:
            continue
        out.append({
            "id": vid,
            "kind": info.get("kind"),
            "venue": info.get("venue") or vid,
            "instrument_types": list(info.get("instrument_types") or ()),
        })
    out.sort(key=lambda v: v["id"])
    return out


def _markets_snapshot(client) -> list[str]:
    """Union of allowed markets across configured accounts.

    Dashboard create-strategy dialogs can prefill this instead of
    shipping ``MOCK:BTCUSDT`` as a default.
    """
    seen: set[str] = set()
    for acc in accounts_mod.load_accounts(client.config.paths).values():
        raw = acc.raw or {}
        for key in ("allowed_markets", "markets", "default_markets"):
            for sym in raw.get(key) or ():
                if isinstance(sym, str) and sym.strip():
                    seen.add(sym.strip())
    return sorted(seen)


def routes():
    def discovery_snapshot(client, _query):
        """All-in-one snapshot used by the dashboard bootstrap."""
        return {
            "accounts": _accounts_snapshot(client),
            "wallets": _wallets_snapshot(client),
            "venues": _venues_snapshot(client),
            "markets": _markets_snapshot(client),
            "strategy_statuses": list(_STRATEGY_STATUSES),
            "strategy_drivers": list(_STRATEGY_DRIVERS),
        }

    def accounts_list(client, _query):
        return {"accounts": _accounts_snapshot(client)}

    def wallets_list(client, _query):
        return _wallets_snapshot(client)

    def venues_list(client, _query):
        return {"venues": _venues_snapshot(client)}

    def markets_list(client, _query):
        return {"markets": _markets_snapshot(client)}

    def lifecycle_enums(_client, _query):
        """Canonical strategy status / driver enums (safety policy)."""
        return {
            "statuses": list(_STRATEGY_STATUSES),
            "drivers": list(_STRATEGY_DRIVERS),
        }

    return [
        ("GET", "/discovery", discovery_snapshot),
        ("GET", "/discovery/accounts", accounts_list),
        ("GET", "/discovery/wallets", wallets_list),
        ("GET", "/discovery/venues", venues_list),
        ("GET", "/discovery/markets", markets_list),
        ("GET", "/discovery/lifecycle", lifecycle_enums),
    ]
