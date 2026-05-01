"""Account control-plane HTTP endpoints (Plan 2026-04-29 §11 P8).

The trading control plane already exposed a per-portfolio aggregate via
``/portfolio/health``. This module fills in the operator-grade CRUD
surface for individual accounts:

- ``/accounts/list``     — every configured account with the latest
  snapshot, reservation total, open positions, protections and active
  executors. (The legacy ``/discovery/accounts`` is kept for the very
  old "is anything configured?" check.)
- ``/accounts/get``      — single account detail used by the new
  ``/accounts/[id]`` driver page on the dashboard.
- ``/accounts/upsert``   — create or update a row in
  ``accounts/accounts.yml``. Plaintext credentials are refused outright;
  callers must first store the secret via ``/security/secrets/put`` and
  then pass the resulting ``vault://`` reference here.
- ``/accounts/delete``   — remove an account row (refuses if there are
  active orders/positions/executors).
- ``/accounts/quarantine`` — flip ``status`` between ``active``,
  ``read_only``, ``disabled`` and ``quarantined``. Anything other than
  ``active`` blocks new orders via the existing risk gate. Operator
  actions are journaled to ``journals/operator.jsonl``.

The Settings-page wallet/exchange surfaces stay where they are; this
module only changes the *account roster* on top of them.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

from ..connectors import http_auth
from ..connectors.provider_spec import get_registry
from ..core.errors import TradingError
from ..trading import account_intake as account_intake_mod
from ..trading import accounts as accounts_mod
from ..trading import account_proposals as account_proposals_mod
from ..trading.account_snapshots import latest_snapshot
from ..trading.capital import CapitalReservationStore
from ..trading.executors.orchestrator import ExecutorOrchestrator
from ..trading.order_tracker import OrderTracker
from ..trading.position_book import PositionBook
from ..trading.protection_store import ProtectionStore


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _journal_operator(client, event: dict[str, Any]) -> None:
    """Append a structured event to ``journals/operator.jsonl``.

    Same shape used elsewhere in P7. Failures are intentionally swallowed
    — observability must not block the operator action itself.
    """

    try:
        path = Path(client.config.paths.journals) / "operator.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        record = dict(event)
        record.setdefault("ts", time.time())
        with path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
    except Exception:
        return None


def _account_summary(
    client,
    profile: accounts_mod.AccountProfile,
) -> dict[str, Any]:
    """Build the per-account driver-page payload.

    Combines the rich :class:`AccountProfile` shape with a snapshot,
    reservation total, open-position count, protection count and the
    list of currently-running executors so the dashboard renders one
    consistent surface.
    """

    paths = client.config.paths
    reservations = CapitalReservationStore(paths)
    positions = PositionBook(paths)
    protections = ProtectionStore(paths)
    orchestrator = ExecutorOrchestrator(client.config)
    snapshot = latest_snapshot(paths, profile.id)
    open_positions = positions.open_positions(account_id=profile.id)
    open_protections = protections.list_active(account_id=profile.id)
    active_executors = orchestrator.list_active(account_id=profile.id)
    try:
        reserved_usd = float(reservations.total_blocked_usd(profile.id))
    except Exception:
        reserved_usd = 0.0
    return {
        "profile": profile.asdict(),
        "snapshot": snapshot.asdict() if snapshot else None,
        "reserved_usd": reserved_usd,
        "open_positions": [p.asdict() for p in open_positions],
        "open_position_count": len(open_positions),
        "protections": [pr.asdict() for pr in open_protections],
        "protection_count": len(open_protections),
        "active_executors": [
            {
                "executor_id": run.executor_id,
                "kind": run.kind,
                "state": run.state,
                "market": run.market,
                "strategy_id": run.strategy_id,
                "created_at": run.created_at,
                "last_heartbeat": run.last_heartbeat,
            }
            for run in active_executors
        ],
    }


def _has_blocking_state(client, account_id: str) -> dict[str, Any]:
    """Return a description of any state that would block a delete.

    The dashboard needs to render *why* a delete is refused, not just
    "no". We surface the counts directly so the user can take action
    (cancel orders, close positions, drain executors) before retrying.
    """

    paths = client.config.paths
    positions = PositionBook(paths)
    orchestrator = ExecutorOrchestrator(client.config)
    tracker = OrderTracker(paths)
    open_positions = positions.open_positions(account_id=account_id)
    active_executors = orchestrator.list_active(account_id=account_id)
    active_orders = tracker.active_orders(account_id=account_id)
    return {
        "open_positions": len(open_positions),
        "active_executors": len(active_executors),
        "active_orders": len(active_orders),
    }


def _vaultify_plaintext_credentials(
    client,
    payload: dict[str, Any],
    *,
    operator: str,
) -> dict[str, Any]:
    """Store one-time plaintext credentials and return a vault-only payload.

    ``accounts.upsert_account`` intentionally refuses plaintext. The HTTP
    control plane accepts it only at this edge, writes it to SecretVault, and
    then continues with the same vault-only account path used everywhere else.
    """

    raw_credentials = payload.get("credentials")
    if not isinstance(raw_credentials, dict) or not raw_credentials:
        return payload

    account_id = str(payload.get("id") or "").strip()
    if not account_id:
        raise TradingError("account.id is required before storing credentials")
    account_kind = str(payload.get("kind") or "cex").strip().lower()
    venue = str(payload.get("venue") or payload.get("exchange") or "").strip().lower()
    field_scopes: dict[str, str] = {}
    if venue:
        try:
            spec = get_registry().find(venue)
        except Exception:
            spec = None
        if spec is not None:
            for field in spec.credential_fields:
                if field.sensitive:
                    field_scopes[field.name] = field.vault_scope or "exchange"

    refs = account_intake_mod.store_credential_values(
        client.config.paths,
        account_id=account_id,
        account_kind=account_kind,
        credential_values={
            str(k): str(v)
            for k, v in raw_credentials.items()
            if isinstance(v, (str, int, float))
        },
        field_scopes=field_scopes,
        operator=operator,
    )
    next_payload = dict(payload)
    next_payload["credentials"] = refs
    return next_payload


# ---------------------------------------------------------------------------
# HTTP handlers
# ---------------------------------------------------------------------------


def routes():
    def list_accounts(client, _payload):
        profiles = accounts_mod.load_account_profiles(client.config.paths)
        return {
            "accounts": [
                _account_summary(client, profile)
                for profile in profiles.values()
            ],
            "ts": time.time(),
        }

    def get_account(client, payload):
        aid = str((payload or {}).get("account_id") or "").strip()
        if not aid:
            return {"ok": False, "error": "account_id_required"}
        try:
            profile = accounts_mod.get_account_profile(client.config.paths, aid)
        except TradingError as exc:
            return {"ok": False, "error": "unknown_account", "detail": str(exc)}
        return {"ok": True, "account": _account_summary(client, profile)}

    def upsert_account(client, payload):
        # Plan 2026-04-29 §11 P9 — accounts.upsert can stage a
        # proposal instead of writing immediately. ``apply: true``
        # (default) preserves the prior direct-write behaviour;
        # ``apply: false`` returns an :class:`AccountProposal` the
        # operator must approve via ``/accounts/proposals/apply``.
        if not isinstance(payload, dict):
            return {"ok": False, "error": "payload_required"}
        operator = str(payload.get("operator") or "dashboard")
        apply_now = bool(payload.get("apply", True))
        # Strip the meta fields (apply/operator) before passing the
        # payload further so the schema stays clean.
        scrubbed = {k: v for k, v in payload.items() if k not in ("apply", "operator")}
        try:
            scrubbed = _vaultify_plaintext_credentials(
                client, scrubbed, operator=operator,
            )
        except TradingError as exc:
            return {"ok": False, "error": "invalid_account", "detail": str(exc)}
        if not apply_now:
            try:
                proposal = account_proposals_mod.propose_upsert(
                    client.config.paths,
                    scrubbed,
                    operator=operator,
                )
            except TradingError as exc:
                return {"ok": False, "error": "invalid_account", "detail": str(exc)}
            _journal_operator(
                client,
                {
                    "kind": "account.upsert.proposed",
                    "proposal_id": proposal.id,
                    "account_id": proposal.target_id,
                    "operation": proposal.operation,
                    "operator": operator,
                },
            )
            return {
                "ok": True,
                "applied": False,
                "proposal": proposal.asdict(),
            }
        try:
            profile = accounts_mod.upsert_account(
                client.config.paths,
                scrubbed,
                operator=operator,
            )
        except TradingError as exc:
            return {"ok": False, "error": "invalid_account", "detail": str(exc)}
        _journal_operator(
            client,
            {
                "kind": "account.upsert",
                "account_id": profile.id,
                "mode": profile.mode,
                "venue": profile.venue,
                "operator": operator,
            },
        )
        return {
            "ok": True,
            "applied": True,
            "account": _account_summary(client, profile),
        }

    def list_account_proposals(client, payload):
        body = payload or {}
        state = (str(body.get("state") or "").strip().lower() or None)
        proposals = account_proposals_mod.list_proposals(
            client.config.paths, state=state,
        )
        return {
            "ok": True,
            "proposals": [p.asdict() for p in proposals],
            "count": len(proposals),
        }

    def get_account_proposal(client, payload):
        body = payload or {}
        pid = str(body.get("proposal_id") or "").strip()
        if not pid:
            return {"ok": False, "error": "proposal_id_required"}
        try:
            proposal = account_proposals_mod.get_proposal(client.config.paths, pid)
        except TradingError as exc:
            return {"ok": False, "error": "unknown_proposal", "detail": str(exc)}
        return {"ok": True, "proposal": proposal.asdict()}

    def apply_account_proposal(client, payload):
        body = payload or {}
        pid = str(body.get("proposal_id") or "").strip()
        if not pid:
            return {"ok": False, "error": "proposal_id_required"}
        operator = str(body.get("operator") or "dashboard")
        note = str(body.get("note") or "")
        try:
            proposal, profile = account_proposals_mod.approve_proposal(
                client.config.paths, pid, operator=operator, note=note,
            )
        except TradingError as exc:
            return {"ok": False, "error": "apply_failed", "detail": str(exc)}
        _journal_operator(
            client,
            {
                "kind": "account.upsert.applied",
                "proposal_id": pid,
                "account_id": profile.id,
                "operator": operator,
                "note": note,
            },
        )
        return {
            "ok": True,
            "proposal": proposal.asdict(),
            "account": _account_summary(client, profile),
        }

    def reject_account_proposal(client, payload):
        body = payload or {}
        pid = str(body.get("proposal_id") or "").strip()
        if not pid:
            return {"ok": False, "error": "proposal_id_required"}
        operator = str(body.get("operator") or "dashboard")
        note = str(body.get("note") or "")
        try:
            proposal = account_proposals_mod.reject_proposal(
                client.config.paths, pid, operator=operator, note=note,
            )
        except TradingError as exc:
            return {"ok": False, "error": "reject_failed", "detail": str(exc)}
        _journal_operator(
            client,
            {
                "kind": "account.upsert.rejected",
                "proposal_id": pid,
                "operator": operator,
                "note": note,
            },
        )
        return {"ok": True, "proposal": proposal.asdict()}

    def delete_account(client, payload):
        aid = str((payload or {}).get("account_id") or "").strip()
        if not aid:
            return {"ok": False, "error": "account_id_required"}
        force = bool((payload or {}).get("force"))
        blocking = _has_blocking_state(client, aid)
        if not force and any(blocking.values()):
            return {
                "ok": False,
                "error": "account_busy",
                "detail": (
                    "account has active executors / orders / positions; close them "
                    "or pass force=true"
                ),
                "state": blocking,
            }
        try:
            accounts_mod.delete_account(client.config.paths, aid, require_empty=False)
        except TradingError as exc:
            return {"ok": False, "error": "delete_failed", "detail": str(exc)}
        _journal_operator(
            client,
            {
                "kind": "account.delete",
                "account_id": aid,
                "operator": str((payload or {}).get("operator") or "dashboard"),
                "force": force,
                "blocking": blocking,
            },
        )
        return {"ok": True, "account_id": aid}

    def reset_paper(client, payload):
        body = payload or {}
        aid = str(body.get("account_id") or "").strip()
        if not aid:
            return {"ok": False, "error": "account_id_required"}
        operator = str(body.get("operator") or "dashboard")
        # Same blocking-state check as delete: the operator gets a clear
        # readout instead of "no", and ``force=true`` overrides for the
        # rare case of a stuck executor that we're explicitly purging.
        force = bool(body.get("force"))
        blocking = _has_blocking_state(client, aid)
        if not force and any(blocking.values()):
            return {
                "ok": False,
                "error": "account_busy",
                "detail": (
                    "paper reset refused while orders/positions/executors are "
                    "active; cancel them or pass force=true"
                ),
                "state": blocking,
            }
        initial_balance = body.get("initial_balance_usd")
        try:
            profile = accounts_mod.reset_paper_account(
                client.config.paths,
                aid,
                initial_balance_usd=(
                    float(initial_balance)
                    if initial_balance is not None and initial_balance != ""
                    else None
                ),
                operator=operator,
            )
        except TradingError as exc:
            return {"ok": False, "error": "reset_refused", "detail": str(exc)}
        _journal_operator(
            client,
            {
                "kind": "account.reset_paper",
                "account_id": aid,
                "operator": operator,
                "force": force,
                "initial_balance_usd": initial_balance,
                "blocking": blocking,
            },
        )
        return {"ok": True, "account": _account_summary(client, profile)}

    def quarantine_account(client, payload):
        aid = str((payload or {}).get("account_id") or "").strip()
        new_status = str((payload or {}).get("status") or "").strip().lower()
        if not aid:
            return {"ok": False, "error": "account_id_required"}
        if new_status not in ("active", "read_only", "disabled", "quarantined"):
            return {"ok": False, "error": "invalid_status"}
        reason = str((payload or {}).get("reason") or "")
        operator = str((payload or {}).get("operator") or "dashboard")
        try:
            profile = accounts_mod.set_account_status(
                client.config.paths,
                aid,
                status=new_status,
                reason=reason,
                operator=operator,
            )
        except TradingError as exc:
            return {"ok": False, "error": "status_update_failed", "detail": str(exc)}
        _journal_operator(
            client,
            {
                "kind": "account.status",
                "account_id": aid,
                "status": new_status,
                "reason": reason,
                "operator": operator,
            },
        )
        return {"ok": True, "account": _account_summary(client, profile)}

    def list_headers(client, payload):
        """Show masked auth headers for an account.

        Plan 2026-04-29 §11 P8 — data-source providers (and any REST
        connector) can hold extra HTTP auth headers under
        ``provider_config.headers``. Operators viewing the dashboard or
        agents inspecting an account must never see plaintext secrets,
        so this endpoint returns the metadata view from
        :func:`http_auth.headers_metadata` (vault refs are surfaced as
        ``vault://<name>``, raw values are masked).
        """
        body = payload or {}
        aid = str(body.get("account_id") or "").strip()
        if not aid:
            return {"ok": False, "error": "account_id_required"}
        try:
            profile = accounts_mod.get_account_profile(client.config.paths, aid)
        except TradingError as exc:
            return {"ok": False, "error": "unknown_account", "detail": str(exc)}
        provider_config = (profile.raw or {}).get("provider_config") or {}
        raw_headers = provider_config.get("headers") if isinstance(provider_config, dict) else None
        try:
            headers = http_auth.normalize_headers_payload(raw_headers)
        except Exception:
            headers = {}
        return {
            "ok": True,
            "account_id": aid,
            "headers": http_auth.headers_metadata(headers),
        }

    def patch_headers(client, payload):
        """Merge / remove HTTP auth headers on a data-source account.

        Body schema::

            {
              "account_id": "cmc_paper",
              "operator": "agent",
              "headers": {
                "X-CMC_PRO_API_KEY": "vault://cmc_pro_key",
                "Authorization": "Bearer vault://cmc_pro_key",
                "X-Old-Header": null   // remove
              }
            }

        Values must already be ``vault://<ref>`` references (or plain
        strings for non-secret hints). Plaintext secrets are *refused*
        — store them via ``/security/secrets/put`` first, then pass
        the returned ref here. Setting a value to ``None`` removes the
        header from ``provider_config.headers``.
        """
        body = payload or {}
        aid = str(body.get("account_id") or "").strip()
        if not aid:
            return {"ok": False, "error": "account_id_required"}
        operator = str(body.get("operator") or "dashboard")
        patch = body.get("headers")
        if not isinstance(patch, dict) or not patch:
            return {"ok": False, "error": "headers_required"}
        try:
            profile = accounts_mod.get_account_profile(client.config.paths, aid)
        except TradingError as exc:
            return {"ok": False, "error": "unknown_account", "detail": str(exc)}

        existing_pc = dict((profile.raw or {}).get("provider_config") or {})
        try:
            existing_headers = dict(http_auth.normalize_headers_payload(
                existing_pc.get("headers")
            ))
        except Exception:
            existing_headers = {}

        # Refuse anything that looks like a plaintext secret. We accept
        # bare strings (non-secret hints), strings that *contain* a
        # ``vault://`` reference, and ``None`` (delete).
        for key, value in patch.items():
            if value is None:
                continue
            if not isinstance(value, str):
                return {
                    "ok": False,
                    "error": "invalid_header",
                    "detail": f"header {key!r} must be a string or null",
                }
            stripped = value.strip()
            if not stripped:
                continue
            looks_secret = any(
                token in stripped.lower()
                for token in ("apikey", "api_key", "secret", "private")
            ) and "vault://" not in stripped.lower()
            if looks_secret:
                return {
                    "ok": False,
                    "error": "plaintext_secret_refused",
                    "detail": (
                        f"header {key!r} looks like a plaintext secret; "
                        "store it via /security/secrets/put and use a vault:// reference"
                    ),
                }

        merged = dict(existing_headers)
        for key, value in patch.items():
            k = str(key).strip()
            if not k:
                continue
            if value is None:
                merged.pop(k, None)
            else:
                merged[k] = str(value).strip()

        # Build a fresh upsert payload that preserves the existing row
        # while overwriting just the headers. ``upsert_account`` requires
        # vault:// references for the credentials map, so we hand back
        # the same raw values we read.
        new_pc = dict(existing_pc)
        if merged:
            new_pc["headers"] = merged
        else:
            new_pc.pop("headers", None)

        upsert_payload = dict(profile.raw or {})
        upsert_payload["id"] = profile.id
        upsert_payload["provider_config"] = new_pc

        try:
            new_profile = accounts_mod.upsert_account(
                client.config.paths,
                upsert_payload,
                operator=operator,
            )
        except TradingError as exc:
            return {"ok": False, "error": "patch_failed", "detail": str(exc)}

        _journal_operator(
            client,
            {
                "kind": "account.headers.patch",
                "account_id": aid,
                "operator": operator,
                "added_or_updated": [k for k, v in patch.items() if v is not None],
                "removed": [k for k, v in patch.items() if v is None],
            },
        )
        return {
            "ok": True,
            "account": _account_summary(client, new_profile),
            "headers": http_auth.headers_metadata(merged),
        }

    return [
        ("GET", "/accounts/list", list_accounts),
        ("POST", "/accounts/list", list_accounts),
        ("POST", "/accounts/get", get_account),
        ("POST", "/accounts/upsert", upsert_account),
        ("POST", "/accounts/delete", delete_account),
        ("POST", "/accounts/quarantine", quarantine_account),
        ("POST", "/accounts/reset_paper", reset_paper),
        ("POST", "/accounts/headers/list", list_headers),
        ("POST", "/accounts/headers/patch", patch_headers),
        ("GET", "/accounts/proposals/list", list_account_proposals),
        ("POST", "/accounts/proposals/list", list_account_proposals),
        ("POST", "/accounts/proposals/get", get_account_proposal),
        ("POST", "/accounts/proposals/apply", apply_account_proposal),
        ("POST", "/accounts/proposals/reject", reject_account_proposal),
    ]
