"""Least-privilege access checks for operator-facing trade actions.

The internal strategy runtime is trusted application code and calls the
trading SDK directly. Public HTTP routes, however, carry dispatcher-stamped
identity fields (``_auth_actor_id`` / ``_auth_scopes``).  Those fields let us
bind the caller's grant to the *resolved account mode* instead of trusting a
request body or a static route scope:

* paper/shadow account actions require ``trade:paper``;
* canary/live account actions require ``trade:live``;
* ``api:all`` remains the owner/loopback wildcard.

Unstamped calls are treated as in-process calls. The local HTTP dispatcher
always overwrites the stamp on registered sensitive routes, so request JSON
cannot manufacture this trust context.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from ..core.config import Config
from ..core.errors import TradingError
from .accounts import get_account_profile


def _parse_scopes(raw: object) -> frozenset[str]:
    if isinstance(raw, str):
        return frozenset(
            part.strip()
            for part in raw.replace(",", " ").split()
            if part.strip()
        )
    if isinstance(raw, (list, tuple, set, frozenset)):
        return frozenset(
            str(part).strip()
            for part in raw
            if str(part).strip()
        )
    return frozenset()


def trusted_http_actor(payload: Mapping[str, Any] | None) -> str:
    """Return the dispatcher-authenticated actor, never a body actor field."""

    if not isinstance(payload, Mapping):
        return ""
    return str(payload.get("_auth_actor_id") or "").strip()


def trusted_http_scopes(payload: Mapping[str, Any] | None) -> frozenset[str] | None:
    """Return trusted HTTP grants, or ``None`` for an in-process call."""

    if not trusted_http_actor(payload):
        return None
    return _parse_scopes((payload or {}).get("_auth_scopes"))


def required_account_trade_scope(config: Config, account_id: str) -> tuple[str, str]:
    """Resolve ``(required_scope, account_mode)`` from persisted account state."""

    profile = get_account_profile(config.paths, account_id)
    required = "trade:live" if profile.is_real_money else "trade:paper"
    return required, str(profile.mode)


def guard_http_trade_scope(
    config: Config | None,
    payload: Mapping[str, Any] | None,
    *,
    account_id: str,
    action: str,
) -> dict[str, Any] | None:
    """Return a rejection envelope when an HTTP grant mismatches account mode.

    Unknown accounts are left to the canonical trading pipeline, which emits
    its normal validation envelope and cannot produce a venue side effect.
    """

    scopes = trusted_http_scopes(payload)
    if config is None or scopes is None or not str(account_id or "").strip():
        return None
    account_id = str(account_id).strip()
    try:
        required_scope, account_mode = required_account_trade_scope(
            config, account_id
        )
    except TradingError:
        return None
    if "api:all" in scopes or required_scope in scopes:
        return None
    actor_id = trusted_http_actor(payload)
    return {
        "ok": False,
        "status": "rejected",
        "error": "insufficient_trade_scope",
        "detail": (
            f"{action} on account {account_id!r} (mode={account_mode}) "
            f"requires {required_scope}"
        ),
        "action": str(action),
        "actor_id": actor_id,
        "account_id": account_id,
        "account_mode": account_mode,
        "required_scope": required_scope,
        "granted_scopes": sorted(scopes),
    }


__all__ = [
    "guard_http_trade_scope",
    "required_account_trade_scope",
    "trusted_http_actor",
    "trusted_http_scopes",
]
