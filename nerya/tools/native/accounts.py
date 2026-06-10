"""Account control-plane native tools.

These tools expose the same account roster path the dashboard/API uses, so
the agent can create safe paper accounts without guessing YAML paths or
mutating workspace files directly.
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any

from ...connectors.provider_spec import get_registry
from ...core.errors import TradingError
from ...trading import accounts as accounts_mod
from ..types import ToolCall, ToolError, ToolErrorKind, ToolResult


ACCOUNT_LIST_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "venue": {
            "type": "string",
            "description": "Optional venue/provider filter, e.g. kraken, okx, ccxt.",
        },
        "include_disabled": {
            "type": "boolean",
            "default": False,
            "description": "Include disabled/quarantined accounts when true.",
        },
    },
}


ACCOUNT_UPSERT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "id": {
            "type": "string",
            "description": "Stable account id, e.g. kraken_paper.",
        },
        "venue": {
            "type": "string",
            "description": (
                "Existing venue/provider id or alias, e.g. kraken, okx, "
                "ccxt:kraken, bsc, self_custody."
            ),
        },
        "kind": {
            "type": "string",
            "enum": ["cex", "dex", "chain"],
            "default": "cex",
        },
        "mode": {
            "type": "string",
            "enum": ["paper", "shadow", "canary", "live"],
            "default": "paper",
        },
        "base_currency": {"type": "string", "default": "USDT"},
        "initial_balance_usd": {"type": "number", "default": 0},
        "provider_config": {
            "type": "object",
            "description": "Non-secret provider settings such as ccxt_id or RPC URL.",
        },
        "credentials": {
            "type": "object",
            "description": "Optional vault:// credential refs only; plaintext is refused.",
        },
        "permissions": {"type": "object"},
        "limits": {"type": "object"},
        "status": {
            "type": "string",
            "enum": ["active", "read_only", "disabled", "quarantined"],
            "default": "active",
        },
        "live_trading_enabled": {
            "type": "boolean",
            "default": False,
            "description": "Must remain false unless the operator explicitly configures live trading.",
        },
    },
    "required": ["id", "venue"],
}


def _public_profile(profile: accounts_mod.AccountProfile) -> dict[str, Any]:
    data = profile.asdict()
    data["account_id"] = profile.id
    return data


def _journal(paths: Any, event: dict[str, Any]) -> None:
    try:
        path = Path(paths.journals) / "operator.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        import json

        record = dict(event)
        record.setdefault("ts", time.time())
        with path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
    except Exception:
        return None


def _normalize_venue(raw: str) -> tuple[str, dict[str, Any]]:
    venue = (raw or "").strip().lower()
    provider_config: dict[str, Any] = {}
    if venue.startswith("ccxt:"):
        ccxt_id = venue.split(":", 1)[1].strip()
        if ccxt_id:
            provider_config["ccxt_id"] = ccxt_id
            direct = get_registry().find(ccxt_id)
            if direct is not None:
                return direct.id, provider_config
        return "ccxt", provider_config
    spec = get_registry().find(venue)
    if spec is not None:
        return spec.id, provider_config
    return venue, provider_config


def _reject_plaintext_credentials(credentials: Any) -> dict[str, str]:
    if credentials in (None, ""):
        return {}
    if not isinstance(credentials, dict):
        raise TradingError("credentials must be a map of vault refs")
    out: dict[str, str] = {}
    for key, value in credentials.items():
        if value in (None, ""):
            continue
        text = str(value)
        if not text.startswith("vault://"):
            raise TradingError(
                f"credential {key!s} must be a vault:// ref; plaintext is not accepted"
            )
        out[str(key)] = text
    return out


def account_list_handler(
    call: ToolCall,
    *,
    config_like: Any | None = None,
) -> ToolResult:
    if config_like is None:
        return ToolResult.from_error(
            tool_use_id=call.id,
            name=call.name,
            error=ToolError(
                kind=ToolErrorKind.EXECUTION_ERROR,
                message="account_list requires a workspace Config",
                retryable=False,
            ),
        )
    args = call.arguments or {}
    venue_filter = str(args.get("venue") or "").strip().lower()
    include_disabled = bool(args.get("include_disabled", False))
    profiles = accounts_mod.load_account_profiles(config_like.paths)
    rows: list[dict[str, Any]] = []
    for profile in profiles.values():
        if venue_filter and profile.venue.lower() != venue_filter:
            continue
        if not include_disabled and profile.status in {"disabled", "quarantined"}:
            continue
        rows.append(_public_profile(profile))
    rows.sort(key=lambda row: str(row.get("id") or ""))
    return ToolResult.from_json(
        tool_use_id=call.id,
        name=call.name,
        data={"ok": True, "count": len(rows), "accounts": rows},
    )


def account_upsert_handler(
    call: ToolCall,
    *,
    config_like: Any | None = None,
) -> ToolResult:
    if config_like is None:
        return ToolResult.from_error(
            tool_use_id=call.id,
            name=call.name,
            error=ToolError(
                kind=ToolErrorKind.EXECUTION_ERROR,
                message="account_upsert requires a workspace Config",
                retryable=False,
            ),
        )
    args = dict(call.arguments or {})
    try:
        mode = str(args.get("mode") or "paper").strip().lower()
        live_trading_enabled = bool(args.get("live_trading_enabled", False))
        if mode != "paper" or live_trading_enabled:
            raise TradingError(
                "account_upsert only applies non-live paper accounts; use the "
                "dashboard intake/proposal flow for live, shadow, or canary accounts"
            )
        venue, provider_config = _normalize_venue(str(args.get("venue") or ""))
        if not venue:
            raise TradingError("venue is required")
        if get_registry().find(venue) is None:
            raise TradingError(f"unknown venue/provider: {venue}")
        provided_config = args.get("provider_config")
        if isinstance(provided_config, dict):
            provider_config.update(provided_config)
        payload = {
            "id": str(args.get("id") or "").strip(),
            "venue": venue,
            "kind": str(args.get("kind") or "cex").strip().lower(),
            "mode": "paper",
            "status": str(args.get("status") or "active").strip().lower(),
            "base_currency": str(args.get("base_currency") or "USDT").upper(),
            "live_trading_enabled": False,
            "initial_balance_usd": float(args.get("initial_balance_usd") or 0.0),
            "credentials": _reject_plaintext_credentials(args.get("credentials")),
            "permissions": dict(args.get("permissions") or {}),
            "limits": dict(args.get("limits") or {}),
        }
        if provider_config:
            payload["provider_config"] = provider_config
        profile = accounts_mod.upsert_account(
            config_like.paths,
            payload,
            operator="agent:native",
        )
    except TradingError as exc:
        return ToolResult.from_error(
            tool_use_id=call.id,
            name=call.name,
            error=ToolError(
                kind=ToolErrorKind.EXECUTION_ERROR,
                message=str(exc),
                retryable=False,
            ),
        )
    except Exception as exc:  # pragma: no cover - defensive tool boundary
        return ToolResult.from_error(
            tool_use_id=call.id,
            name=call.name,
            error=ToolError(
                kind=ToolErrorKind.EXECUTION_ERROR,
                message=f"{type(exc).__name__}: {exc}",
                retryable=None,
            ),
        )
    _journal(
        config_like.paths,
        {
            "kind": "account.upsert",
            "account_id": profile.id,
            "mode": profile.mode,
            "venue": profile.venue,
            "operator": "agent:native",
        },
    )
    return ToolResult.from_json(
        tool_use_id=call.id,
        name=call.name,
        data={
            "ok": True,
            "applied": True,
            "account": _public_profile(profile),
            "completion_signal": {
                "kind": "account_setup",
                "finalizable": True,
                "safety": "paper_only",
            },
            "next": (
                "Verify with account_list or /accounts/list. If the original "
                "operator request was only account, venue, provider, or wallet "
                "setup, write the final answer after verification; do not keep "
                "probing market data, skills, or strategy tools. If the "
                "operator requested additional work, continue only that "
                "remaining work."
            ),
        },
    )


__all__ = [
    "ACCOUNT_LIST_SCHEMA",
    "ACCOUNT_UPSERT_SCHEMA",
    "account_list_handler",
    "account_upsert_handler",
]
