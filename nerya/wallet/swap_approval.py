"""Durable, one-shot operator approval for live wallet swaps.

A wallet swap is a real-money side effect even though it is not a CEX
``TradeIntent``. Public requests therefore follow the same safety shape as
interactive orders:

1. validate and quote the exact swap request;
2. persist a frozen ``wallet_swap`` approval record in SQLite + JSONL;
3. show the record through the shared approval UI;
4. after approval, atomically claim the record and execute it once;
5. re-check runtime live controls and the approved quote floor immediately
   before calling the provider.

The approval id is the idempotency boundary. A duplicate callback or resume
attempt cannot invoke the provider again, including across API processes.
"""

from __future__ import annotations

import json
import logging
import math
from pathlib import Path
import time
from typing import Any, Mapping

from ..core import jsonl
from ..core.config import Config
from ..core.ids import approval_id
from ..core.time import now_iso
from ..db.repositories import ApprovalRepository
from ..db.sqlite import connect
from .errors import WalletDependencyError, WalletPolicyDenied
from .registry import build_provider, list_configured_providers

log = logging.getLogger(__name__)


def _meaningful_wallet_cfg(cfg: Mapping[str, Any]) -> bool:
    for key, value in dict(cfg or {}).items():
        if value in (None, "", [], {}):
            continue
        if key == "entry" and value in {
            "dist/nerya.js",
            "dist/index.js",
            "scripts/bitget-wallet-agent-api.py",
        }:
            continue
        return True
    return False


def _wallet_cfg(config: Config, name: str) -> dict[str, Any]:
    wallet_cfg = config.data.get("wallet") or {}
    direct = dict(wallet_cfg.get(name) or {})
    if _meaningful_wallet_cfg(direct):
        return direct
    for binding in list_configured_providers(config.data):
        if str(binding.get("provider") or "").strip().lower() == name:
            return dict(binding.get("config") or {})
    return direct


def normalize_swap_request(
    config: Config,
    payload: Mapping[str, Any] | None,
) -> dict[str, Any]:
    """Return the allow-listed, serialisable wallet swap request."""

    body = dict(payload or {})
    configured = config.data.get("wallet") or {}
    provider = str(
        body.get("provider") or configured.get("provider") or ""
    ).strip().lower()
    if not provider:
        raise ValueError("no wallet provider selected")
    chain = str(body.get("chain") or "ethereum").strip().lower()
    token_in = str(body.get("token_in") or "").strip()
    token_out = str(body.get("token_out") or "").strip()
    if not chain:
        raise ValueError("chain is required")
    if not token_in or not token_out:
        raise ValueError("token_in and token_out are required")
    if token_in.lower() == token_out.lower():
        raise ValueError("token_in and token_out must differ")
    try:
        amount_in = float(body.get("amount_in") or 0.0)
    except (TypeError, ValueError) as exc:
        raise ValueError("amount_in must be numeric") from exc
    if not math.isfinite(amount_in) or amount_in <= 0:
        raise ValueError("amount_in must be a finite positive number")
    try:
        slippage_bps = int(body.get("slippage_bps") or 50)
    except (TypeError, ValueError) as exc:
        raise ValueError("slippage_bps must be an integer") from exc
    if not 0 <= slippage_bps <= 5_000:
        raise ValueError("slippage_bps must be between 0 and 5000")
    receiver = str(body.get("receiver") or "").strip()
    return {
        "provider": provider,
        "chain": chain,
        "token_in": token_in,
        "token_out": token_out,
        "amount_in": amount_in,
        "slippage_bps": slippage_bps,
        "receiver": receiver,
    }


def _provider(config: Config, request: Mapping[str, Any]):
    name = str(request.get("provider") or "").strip().lower()
    return build_provider(
        name,
        _wallet_cfg(config, name),
        workspace=Path(config.paths.root),
    )


def quote_swap(config: Config, request: Mapping[str, Any]) -> dict[str, Any]:
    """Fetch the quote that the operator will approve."""

    provider = _provider(config, request)
    result = provider.quote(
        chain=str(request["chain"]),
        token_in=str(request["token_in"]),
        token_out=str(request["token_out"]),
        amount_in=float(request["amount_in"]),
        slippage_bps=int(request["slippage_bps"]),
    )
    return result.to_dict()


def prepare_swap(
    config: Config,
    payload: Mapping[str, Any] | None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    request = normalize_swap_request(config, payload)
    return request, quote_swap(config, request)


def request_approval(
    config: Config,
    *,
    request: Mapping[str, Any],
    quote: Mapping[str, Any],
    actor_id: str,
    session_id: str = "",
    turn_id: str = "",
    tool_call_id: str = "",
) -> dict[str, Any]:
    """Persist and broadcast one frozen wallet swap approval."""

    expires_s = max(1.0, float(config.get("approvals.expire_seconds", 600)))
    aid = approval_id()
    created_at = time.time()
    actor = str(actor_id or "").strip() or "operator:http"
    frozen_request = dict(request)
    frozen_quote = dict(quote)
    record: dict[str, Any] = {
        "approval_id": aid,
        "kind": "wallet_swap",
        "state": "pending",
        "created_at": created_at,
        "expires_at": created_at + expires_s,
        "actor_id": actor,
        "approval_actor_id": actor,
        "source": "operator_http",
        "execution_mode": "live",
        "provider": frozen_request["provider"],
        "chain": frozen_request["chain"],
        "token_in": frozen_request["token_in"],
        "token_out": frozen_request["token_out"],
        "amount_in": frozen_request["amount_in"],
        "slippage_bps": frozen_request["slippage_bps"],
        "receiver": frozen_request.get("receiver") or "",
        "wallet_swap": frozen_request,
        "quote": frozen_quote,
        "risk": {
            "decision": "escalate",
            "reasons": ["wallet_live_side_effect_requires_operator_approval"],
        },
    }
    if session_id:
        record["session_id"] = str(session_id)
    if turn_id:
        record["turn_id"] = str(turn_id)
    if tool_call_id:
        record["tool_call_id"] = str(tool_call_id)

    con = connect(config.paths.db)
    try:
        ApprovalRepository(con).insert(
            id=aid,
            kind="wallet_swap",
            expires_s=expires_s,
            payload=record,
        )
    finally:
        con.close()
    jsonl.append(config.paths.approvals_pending, record)
    jsonl.append(
        config.paths.journal("wallet"),
        {
            "kind": "wallet.swap.approval_requested",
            "ts": now_iso(),
            "approval_id": aid,
            "actor_id": actor,
            "wallet_swap": frozen_request,
            "quote": frozen_quote,
        },
    )
    try:
        from ..trading.approval import _broadcast_approval

        _broadcast_approval(config, record)
    except Exception:
        pass
    return {
        "ok": True,
        "status": "pending_approval",
        "approval_id": aid,
        "execution_mode": "live",
        "wallet_swap": frozen_request,
        "quote": frozen_quote,
    }


def _float(value: object) -> float:
    try:
        number = float(value or 0.0)
    except (TypeError, ValueError):
        return 0.0
    return number if math.isfinite(number) else 0.0


def execute_frozen_swap(
    config: Config,
    *,
    request: Mapping[str, Any],
    approved_quote: Mapping[str, Any],
    approval_id_value: str,
) -> dict[str, Any]:
    """Execute a claimed swap after revalidating all live controls."""

    if not config.live_trading_enabled():
        return {
            "ok": False,
            "error": "live_trading_disabled",
            "reason": "enable runtime.live_trading_enabled to run swaps",
        }
    if config.kill_switch():
        return {"ok": False, "error": "kill_switch_enabled"}

    frozen = normalize_swap_request(config, request)
    provider = _provider(config, frozen)
    current_quote = provider.quote(
        chain=frozen["chain"],
        token_in=frozen["token_in"],
        token_out=frozen["token_out"],
        amount_in=frozen["amount_in"],
        slippage_bps=frozen["slippage_bps"],
    ).to_dict()
    approved_min_out = _float(approved_quote.get("min_out"))
    current_expected_out = _float(current_quote.get("expected_out"))
    if approved_min_out > 0 and current_expected_out < approved_min_out:
        return {
            "ok": False,
            "error": "quote_moved_requires_reapproval",
            "approval_id": approval_id_value,
            "approved_min_out": approved_min_out,
            "current_quote": current_quote,
        }

    audit = {
        "kind": "wallet.swap.requested",
        "ts": now_iso(),
        "approval_id": approval_id_value,
        **frozen,
        "approved_quote": dict(approved_quote),
        "execution_quote": current_quote,
    }
    jsonl.append(config.paths.journal("wallet"), audit)
    kwargs: dict[str, Any] = {
        "chain": frozen["chain"],
        "token_in": frozen["token_in"],
        "token_out": frozen["token_out"],
        "amount_in": frozen["amount_in"],
        "slippage_bps": frozen["slippage_bps"],
        "receiver": frozen.get("receiver") or None,
        "live": True,
    }
    if approved_min_out > 0:
        kwargs["min_out"] = approved_min_out
    result = provider.swap(**kwargs)
    result_doc = result.to_dict()
    amount_out = _float(result_doc.get("amount_out"))
    below_floor = (
        bool(result_doc.get("ok"))
        and approved_min_out > 0
        and amount_out > 0
        and amount_out < approved_min_out
    )
    if below_floor:
        result_doc["approval_policy_breach"] = {
            "approved_min_out": approved_min_out,
            "reported_amount_out": amount_out,
        }
    effective_ok = bool(result_doc.get("ok")) and not below_floor
    jsonl.append(
        config.paths.journal("wallet"),
        {
            **audit,
            "kind": "wallet.swap.result",
            "ok": effective_ok,
            "tx_hash": str(result_doc.get("tx_hash") or ""),
            "result": result_doc,
        },
    )
    return {
        "ok": effective_ok,
        "approval_id": approval_id_value,
        "result": result_doc,
        "quote": current_quote,
        **(
            {"error": "execution_below_approved_min_out"}
            if below_floor
            else {}
        ),
    }


def _approval_payload(row: Mapping[str, Any] | None) -> dict[str, Any]:
    if not row:
        return {}
    raw = row.get("payload")
    if isinstance(raw, dict):
        return dict(raw)
    if isinstance(raw, str):
        try:
            decoded = json.loads(raw)
        except (TypeError, ValueError):
            return {}
        return dict(decoded) if isinstance(decoded, dict) else {}
    return {}


def _load_approved_record(config: Config, aid: str) -> dict[str, Any] | None:
    candidates: list[dict[str, Any]] = []
    for path in (config.paths.approvals_approved, config.paths.approvals_pending):
        if not path.exists():
            continue
        for record in jsonl.read_all(path):
            if record.get("approval_id") == aid or record.get("id") == aid:
                candidates.append(record)
    for record in candidates:
        if str(record.get("kind") or "") == "wallet_swap" and isinstance(
            record.get("wallet_swap"), dict
        ):
            return record
    con = connect(config.paths.db)
    try:
        row = ApprovalRepository(con).get(aid)
    finally:
        con.close()
    if not row:
        return None
    payload = _approval_payload(row)
    if not payload:
        return None
    payload["approval_id"] = aid
    payload["kind"] = str(row.get("kind") or payload.get("kind") or "")
    payload["state"] = str(row.get("state") or payload.get("state") or "")
    return payload


def _claim_resume(config: Config, aid: str) -> tuple[bool, dict[str, Any]]:
    con = connect(config.paths.db)
    try:
        repo = ApprovalRepository(con)
        if repo.claim_resume(aid):
            return True, {}
        row = repo.get(aid) or {}
        return False, {**row, "payload": _approval_payload(row)}
    finally:
        con.close()


def _finish_resume(
    config: Config,
    aid: str,
    *,
    ok: bool,
    response_status: str | None,
    error: str | None = None,
) -> bool:
    con = connect(config.paths.db)
    try:
        return ApprovalRepository(con).finish_resume(
            aid,
            state="resumed" if ok else "resume_failed",
            intent_id=None,
            response_status=response_status,
            error=error,
        )
    finally:
        con.close()


def resume_approved(config: Config, aid: str) -> dict[str, Any]:
    """Atomically execute one approved wallet swap exactly once."""

    record = _load_approved_record(config, aid)
    if record is None:
        return {"ok": False, "error": "approval_not_found", "approval_id": aid}
    if str(record.get("kind") or "") != "wallet_swap":
        return {"ok": False, "error": "approval_kind_mismatch", "approval_id": aid}
    if str(record.get("state") or "").lower() != "approved":
        return {
            "ok": False,
            "error": "approval_not_approved",
            "approval_id": aid,
            "state": record.get("state"),
        }
    try:
        claimed, persisted = _claim_resume(config, aid)
    except Exception as exc:
        log.exception("wallet swap resume claim failed for %s", aid)
        return {
            "ok": False,
            "error": f"approval_resume_claim_failed:{exc}",
            "approval_id": aid,
        }
    if not claimed:
        state = str(persisted.get("state") or "")
        payload = persisted.get("payload") or {}
        if state in {"resuming", "resumed"}:
            return {
                "ok": True,
                "already_resumed": True,
                "resume_in_progress": state == "resuming",
                "approval_id": aid,
                "resume_status": payload.get("resume_status"),
            }
        return {
            "ok": False,
            "error": "resume_failed" if state == "resume_failed" else "approval_resume_not_claimed",
            "approval_id": aid,
            "state": state or None,
        }

    try:
        response = execute_frozen_swap(
            config,
            request=dict(record.get("wallet_swap") or {}),
            approved_quote=dict(record.get("quote") or {}),
            approval_id_value=aid,
        )
    except (WalletDependencyError, WalletPolicyDenied) as exc:
        response = {
            "ok": False,
            "error": (
                "dependency_missing"
                if isinstance(exc, WalletDependencyError)
                else "policy_denied"
            ),
            "reason": str(exc),
        }
    except Exception as exc:  # pragma: no cover - provider failure boundary
        log.exception("wallet swap approval resume failed for %s", aid)
        _finish_resume(
            config,
            aid,
            ok=False,
            response_status="exception",
            error=str(exc),
        )
        jsonl.append(
            config.paths.journal("wallet"),
            {
                "kind": "wallet.swap.approval_resumed",
                "ts": now_iso(),
                "approval_id": aid,
                "ok": False,
                "error": str(exc),
            },
        )
        return {"ok": False, "error": f"resume_failed:{exc}", "approval_id": aid}

    # A returned response consumes the one-shot approval even when a late
    # safety check rejects execution. Retrying silently after an ambiguous
    # provider response is more dangerous than requiring a fresh approval.
    status = str(response.get("error") or ("executed" if response.get("ok") else "rejected"))
    persisted = _finish_resume(
        config,
        aid,
        ok=True,
        response_status=status,
        error=None if response.get("ok") else status,
    )
    jsonl.append(
        config.paths.journal("wallet"),
        {
            "kind": "wallet.swap.approval_resumed",
            "ts": now_iso(),
            "approval_id": aid,
            "ok": bool(response.get("ok")),
            "status": status,
            "persisted": persisted,
        },
    )
    if not persisted:
        return {
            "ok": False,
            "error": "resume_state_persist_failed",
            "approval_id": aid,
            "resume_response": response,
        }
    return {
        "ok": bool(response.get("ok")),
        "approval_id": aid,
        "resume_response": response,
    }


__all__ = [
    "execute_frozen_swap",
    "normalize_swap_request",
    "prepare_swap",
    "quote_swap",
    "request_approval",
    "resume_approved",
]
