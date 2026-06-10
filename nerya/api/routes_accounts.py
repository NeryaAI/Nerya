"""Account control-plane HTTP endpoints.

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

import hashlib
import re

from ..connectors import http_auth
from ..connectors.provider_spec import get_registry
from ..core.errors import TradingError
from ..core.redaction import redact_dict
from ..security.secrets import SecretVault
from ..trading import account_intake as account_intake_mod
from ..trading import accounts as accounts_mod
from ..trading import account_proposals as account_proposals_mod
from ..trading.account_snapshots import equity_curve as _account_equity_curve, latest_snapshot
from ..trading.capital import CapitalReservationStore
from ..trading.executors.orchestrator import ExecutorOrchestrator
from ..trading.order_tracker import OrderTracker
from ..trading.position_book import PositionBook
from ..trading.protection_store import ProtectionStore


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


# Vault names follow the same charset SecretVault validates against, see
# ``routes_security._NAME_OK``. We mirror that here so the auto-generated
# header refs always pass SecretVault.put without surprise.
_VAULT_NAME_CHARSET_RE = re.compile(r"[^a-z0-9_\-.]+")


def _looks_like_secret(value: str) -> bool:
    """Heuristic: does ``value`` look like a plaintext API secret?

    Used by ``patch_headers`` to either reject (strict mode) or
    auto-vault (``auto_vault=true``) the value. We treat it as a
    secret whenever the value is *long enough to be a token* AND
    doesn't already contain a ``vault://`` ref. Short hints
    (``application/json``, ``en-US``, etc.) pass through unchanged.
    """

    v = value.strip()
    if not v:
        return False
    if "vault://" in v.lower():
        return False
    # ``Bearer <token>`` — strip the prefix before measuring length so
    # ``Bearer xyz123longtoken`` is treated as a secret even though the
    # full string includes the bearer keyword.
    probe = v[7:].strip() if v.lower().startswith("bearer ") else v
    if not probe:
        return False
    if any(
        token in v.lower()
        for token in ("apikey", "api_key", "secret", "private")
    ):
        return True
    # Any opaque token >= 16 chars without whitespace is treated as
    # secret material. CMC keys are 36 chars, OKX keys 32, exchange
    # secrets typically 40+ — well above this floor. Header values
    # like ``application/json`` (16) survive because of the slash.
    if len(probe) >= 16 and not any(ch.isspace() for ch in probe) and "/" not in probe and "=" not in probe[:1]:
        return True
    return False


def _auto_vault_name(account_id: str, header_key: str, token: str) -> str:
    """Derive a deterministic vault entry name for a header secret.

    Re-vaulting the same ``(account_id, header_key, token)`` triple
    overwrites the same vault row, so resaving an unchanged header
    doesn't bloat the vault. Different tokens generate different
    names so historical rotations remain auditable via the journal.

    The returned name is always lower-cased and conforms to
    ``[a-z][a-z0-9_-.]{1,80}``.
    """

    def _slug(text: str, *, fallback: str) -> str:
        out = _VAULT_NAME_CHARSET_RE.sub("_", text.strip().lower())
        out = out.strip("_-.")
        return out[:24] if out else fallback

    aid_slug = _slug(account_id, fallback="acct")
    key_slug = _slug(header_key, fallback="hdr")
    fp = hashlib.sha256(token.encode("utf-8")).hexdigest()[:8]
    name = f"hdr_{aid_slug}_{key_slug}_{fp}"
    name = name[:81]
    # Defensive: SecretVault requires the leading char to be a-z.
    if not name or not name[0].isalpha():
        name = f"h{name}"[:81]
    return name


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


def _bound_strategies(paths, account_id: str) -> list[dict[str, Any]]:
    """List non-archived strategies bound to ``account_id``.

    Used by the dashboard to render a 1:N indicator and trigger the
    soft-warning when an operator tries to bind a second strategy onto
    the same account (operators are encouraged to use exchange
    sub-accounts instead — see ``strategy_crud._account_in_use``).
    """

    try:
        from ..trading.strategies import list_strategies as _list_strategies
    except Exception:  # pragma: no cover - defensive
        return []
    out: list[dict[str, Any]] = []
    for s in _list_strategies(paths):
        if str(s.account_id or "") != str(account_id):
            continue
        if s.status == "archived":
            continue
        out.append({
            "strategy_id": s.id,
            "title": s.title,
            "status": s.status,
        })
    return out


def _balance_test_profile(body: dict[str, Any]) -> accounts_mod.AccountProfile:
    """Build a non-persisted profile for a read-only balance probe."""

    aid = str(body.get("id") or body.get("account_id") or "balance_probe").strip()
    if not aid:
        aid = "balance_probe"
    raw_permissions = (
        body.get("permissions") if isinstance(body.get("permissions"), dict) else {}
    )
    raw_limits = body.get("limits") if isinstance(body.get("limits"), dict) else {}
    credentials = {
        str(k): str(v)
        for k, v in (body.get("credentials") or {}).items()
        if isinstance(v, (str, int, float))
    } if isinstance(body.get("credentials"), dict) else {}
    provider_config = (
        dict(body.get("provider_config"))
        if isinstance(body.get("provider_config"), dict)
        else {}
    )
    raw = dict(body)
    raw["credentials"] = credentials
    raw["provider_config"] = provider_config
    kind = str(body.get("kind") or "cex").strip().lower()
    venue = str(body.get("venue") or body.get("exchange") or "mock").strip().lower()
    wallet_id = str(body.get("wallet_id") or "").strip()
    if kind in ("chain", "dex") and wallet_id:
        try:
            from ..wallet.registry import resolve_provider_name

            venue = resolve_provider_name(venue) or venue
        except Exception:  # pragma: no cover - optional wallet registry
            pass
    return accounts_mod.AccountProfile(
        id=aid,
        mode=str(body.get("mode") or "paper").strip().lower(),  # type: ignore[arg-type]
        venue=venue,
        kind=kind,
        provider_spec=str(body.get("provider_spec") or venue),
        base_currency=str(body.get("base_currency") or "USDT").upper(),
        subaccount=str(body.get("subaccount") or ""),
        status=str(body.get("status") or "active").strip().lower(),  # type: ignore[arg-type]
        live_trading_enabled=bool(body.get("live_trading_enabled", False)),
        initial_balance_usd=float(body.get("initial_balance_usd") or 0.0),
        permissions=accounts_mod.AccountPermissions(
            read_balances=bool(raw_permissions.get("read_balances", True)),
            place_order=bool(raw_permissions.get("place_order", False)),
            cancel_order=bool(raw_permissions.get("cancel_order", False)),
            withdraw=False,
        ),
        limits=accounts_mod.AccountLimits(
            max_account_nav_usd=float(raw_limits.get("max_account_nav_usd") or 0.0),
            max_strategy_allocation_pct=float(raw_limits.get("max_strategy_allocation_pct") or 0.0),
            max_order_notional_usd=float(raw_limits.get("max_order_notional_usd") or 0.0),
            max_daily_loss_usd=float(raw_limits.get("max_daily_loss_usd") or 0.0),
            max_drawdown_pct=float(raw_limits.get("max_drawdown_pct") or 0.0),
            max_leverage=float(raw_limits.get("max_leverage") or 1.0),
            fee_buffer_bps=float(raw_limits.get("fee_buffer_bps") or 5.0),
            min_free_balance_pct=float(raw_limits.get("min_free_balance_pct") or 0.0),
        ),
        credentials=credentials,
        wallet_id=wallet_id,
        raw=raw,
    )


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
    # Post-v6 the open_positions list is one merged row per
    # (account, market). Each row carries a ``shares`` array so the
    # dashboard can render the merged top-line *and* the per-strategy
    # breakdown without a second round-trip.
    open_positions_payload: list[dict] = []
    for pos in open_positions:
        row = pos.asdict()
        try:
            shares = positions.list_shares(pos.position_id)
        except Exception:
            shares = []
        merged_size = float(pos.size_base or 0.0)
        merged_unrealized = float(pos.unrealized_pnl_usd or 0.0)
        mark_value = float(pos.mark_price or pos.avg_entry_price or 0.0)
        share_rows: list[dict] = []
        for share in shares:
            share_size = float(share.size_share_base or 0.0)
            # Pro-rata so sum-of-shares reconciles to the merged
            # unrealized — see ``trading.portfolio._shares_for_position``
            # for the same invariant.
            pro_rata = (
                (share_size / merged_size) * merged_unrealized
                if merged_size else 0.0
            )
            share_rows.append({
                "strategy_id": share.strategy_id,
                "size_base": share_size,
                "avg_entry_price": float(share.avg_entry_share_price or 0.0),
                "realized_pnl_usd": float(share.realized_pnl_share_usd or 0.0),
                "fees_usd": float(share.fees_share_usd or 0.0),
                "funding_usd": float(share.funding_share_usd or 0.0),
                "notional_usd": abs(share_size * mark_value),
                "unrealized_pnl_usd": pro_rata,
                "opened_at": share.opened_at,
                "updated_at": share.updated_at,
            })
        row["shares"] = share_rows
        open_positions_payload.append(row)
    open_protections = protections.list_active(account_id=profile.id)
    active_executors = orchestrator.list_active(account_id=profile.id)
    try:
        reserved_usd = float(reservations.total_blocked_usd(profile.id))
    except Exception:
        reserved_usd = 0.0
    bound = _bound_strategies(paths, profile.id)
    try:
        from ..trading.account_refresh import next_refresh_ts_for_profile

        next_refresh_ts = next_refresh_ts_for_profile(
            client.config, profile, snapshot=snapshot,
        )
    except Exception:  # pragma: no cover - defensive
        next_refresh_ts = None
    return {
        "profile": profile.asdict(),
        "snapshot": snapshot.asdict() if snapshot else None,
        "reserved_usd": reserved_usd,
        "open_positions": open_positions_payload,
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
        "bound_strategies": bound,
        "bound_strategy_count": len(bound),
        "next_refresh_ts": next_refresh_ts,
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
                {
                    "id": profile.id,
                    **_account_summary(client, profile),
                }
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
        # accounts.upsert can stage a
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
        meta_keys = ("apply", "operator", "force", "acknowledge_duplicate")
        scrubbed = {k: v for k, v in payload.items() if k not in meta_keys}
        try:
            scrubbed = _vaultify_plaintext_credentials(
                client, scrubbed, operator=operator,
            )
        except TradingError as exc:
            return {"ok": False, "error": "invalid_account", "detail": str(exc)}
        force = bool(payload.get("force") or payload.get("acknowledge_duplicate"))
        # CEX dedup hint: same venue + same primary credential ref
        # almost always means the operator double-clicked the wizard
        # or is re-importing keys they already registered. We warn
        # (not block) and only short-circuit when the dashboard
        # forwards an explicit acknowledge.
        try:
            venue_for_check = str(scrubbed.get("venue") or scrubbed.get("exchange") or "")
            kind_for_check = str(scrubbed.get("kind") or "cex")
            cred_for_check = scrubbed.get("credentials")
            if isinstance(cred_for_check, dict):
                dupe = accounts_mod.find_duplicate_account(
                    client.config.paths,
                    venue=venue_for_check,
                    kind=kind_for_check,
                    credentials={
                        str(k): str(v)
                        for k, v in cred_for_check.items()
                        if isinstance(v, str)
                    },
                    ignore_id=str(scrubbed.get("id") or "") or None,
                )
            else:
                dupe = None
        except Exception:  # pragma: no cover - dedup must never block
            dupe = None
        if dupe is not None and not force:
            return {
                "ok": False,
                "error": "duplicate_candidate",
                "detail": (
                    f"venue {dupe.venue!r} already has an account "
                    f"({dupe.id}) with the same primary credential. "
                    "Pass force=true (or acknowledge_duplicate=true) to "
                    "register anyway, e.g. as a sub-account."
                ),
                "duplicate": {
                    "account_id": dupe.id,
                    "venue": dupe.venue,
                    "kind": dupe.kind,
                    "mode": dupe.mode,
                    "subaccount": dupe.subaccount,
                    "status": dupe.status,
                },
            }
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

    def test_balance(client, payload):
        body = payload or {}
        if not isinstance(body, dict):
            return {"ok": False, "error": "payload_required"}
        aid = str(body.get("account_id") or "").strip()
        profile = None
        if aid and not any(k in body for k in ("venue", "credentials", "wallet_id")):
            try:
                profile = accounts_mod.get_account_profile(client.config.paths, aid)
            except TradingError as exc:
                return {"ok": False, "error": "unknown_account", "detail": str(exc)}
        if profile is None:
            try:
                profile = _balance_test_profile(body)
            except Exception as exc:
                return {
                    "ok": False,
                    "error": "invalid_account",
                    "detail": redact_dict({"error": str(exc)}).get("error", str(exc)),
                }
        try:
            from ..trading.account_snapshots import capture_snapshot

            snap = capture_snapshot(
                client.config,
                profile.id,
                profile=profile,
                persist=False,
            )
        except Exception as exc:
            return {
                "ok": False,
                "error": "balance_test_failed",
                "detail": redact_dict({"error": str(exc)}).get("error", str(exc)),
            }
        return {
            "ok": True,
            "account_id": profile.id,
            "mode": profile.mode,
            "venue": profile.venue,
            "kind": profile.kind,
            "wallet_id": profile.wallet_id,
            "snapshot": snap.asdict(),
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

        Data-source providers (and any REST
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
              "auto_vault": true,         // optional, default false
              "headers": {
                "X-CMC_PRO_API_KEY": "vault://cmc_pro_key",
                "Authorization": "Bearer vault://cmc_pro_key",
                "X-Plain-Key": "raw-token-here",  // auto-vaulted if auto_vault=true
                "X-Old-Header": null   // remove
              }
            }

        Two ingestion modes:

        * ``auto_vault: false`` (default, programmatic safety) — values
          must already be ``vault://<ref>`` references (or plain
          strings for non-secret hints). Plaintext that looks like a
          secret is refused; store it via ``/security/secrets/put``
          first.
        * ``auto_vault: true`` (dashboard ergonomic UX) — plaintext
          values are stashed in :class:`SecretVault` under a
          deterministic auto-generated name (``hdr_<acct>_<key>``)
          and rewritten to ``vault://<name>``. ``Bearer <plaintext>``
          is preserved as ``Bearer vault://<name>``. The generated
          refs are echoed back in the response so the operator can
          see what got stored.

        Setting a value to ``None`` removes the header from
        ``provider_config.headers``.
        """
        body = payload or {}
        aid = str(body.get("account_id") or "").strip()
        if not aid:
            return {"ok": False, "error": "account_id_required"}
        operator = str(body.get("operator") or "dashboard")
        auto_vault = bool(body.get("auto_vault"))
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

        # Optional auto-vault step: rewrite plaintext-looking values
        # into vault refs before the strict validator runs. When
        # ``auto_vault`` is false we keep the historic strict behavior
        # so programmatic callers don't silently leak secrets.
        vaulted_refs: dict[str, str] = {}
        if auto_vault:
            vault = SecretVault.open(client.config.paths.vault_enc)
            for key, value in list(patch.items()):
                if value is None or not isinstance(value, str):
                    continue
                stripped = value.strip()
                if not stripped or "vault://" in stripped.lower():
                    continue
                # Tokens that pass through unchanged: short hints or
                # well-known constants like ``application/json`` that
                # operators might paste into a non-auth header.
                if not _looks_like_secret(stripped):
                    continue
                bearer_prefix = ""
                token = stripped
                lower = stripped.lower()
                if lower.startswith("bearer "):
                    bearer_prefix = stripped[:7]  # preserve original casing
                    token = stripped[7:].strip()
                    if not token or "vault://" in token.lower():
                        continue
                vault_name = _auto_vault_name(aid, key, token)
                vault.put(
                    name=vault_name,
                    value=token,
                    kind="header_token",
                    scope=["exchange"],
                    owner=f"dashboard:{operator}",
                )
                ref_value = f"{bearer_prefix}vault://{vault_name}" if bearer_prefix else f"vault://{vault_name}"
                patch[key] = ref_value
                vaulted_refs[key] = f"vault://{vault_name}"

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
            if "vault://" not in stripped.lower() and _looks_like_secret(stripped):
                return {
                    "ok": False,
                    "error": "plaintext_secret_refused",
                    "detail": (
                        f"header {key!r} looks like a plaintext secret; "
                        "set auto_vault=true to stash it automatically, "
                        "or store it via /security/secrets/put and pass a vault:// reference"
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
                "vaulted": sorted(vaulted_refs.keys()),
            },
        )
        return {
            "ok": True,
            "account": _account_summary(client, new_profile),
            "headers": http_auth.headers_metadata(merged),
            "vaulted_refs": vaulted_refs,
        }

    def equity_curve(client, payload):
        """Return the per-account NAV history sourced from snapshots.

        The dashboard's `/accounts/[id]` page renders this as a real
        fund curve. We pull from ``account_snapshots`` (which the
        background refresh loop already populates) rather than
        recomputing from trade history — that way paper, live (CEX)
        and wallet-chain accounts all use the same code path and any
        ``degraded``/``stale`` snapshot health surfaces naturally to
        the operator.
        """

        body = payload or {}
        aid = str(body.get("account_id") or "").strip()
        if not aid:
            return {"ok": False, "error": "account_id_required"}
        try:
            limit = int(body.get("limit") or 500)
        except (TypeError, ValueError):
            limit = 500
        since_raw = body.get("since_ts")
        try:
            since = float(since_raw) if since_raw not in (None, "") else None
        except (TypeError, ValueError):
            since = None
        bucket_raw = body.get("bucket_seconds")
        try:
            bucket = (
                float(bucket_raw)
                if bucket_raw not in (None, "")
                else None
            )
        except (TypeError, ValueError):
            bucket = None
        try:
            profile = accounts_mod.get_account_profile(client.config.paths, aid)
        except TradingError as exc:
            return {"ok": False, "error": "unknown_account", "detail": str(exc)}
        points = _account_equity_curve(
            client.config.paths,
            aid,
            since_ts=since,
            limit=limit,
            bucket_seconds=bucket,
        )
        initial = float(profile.initial_balance_usd or 0.0)
        latest = points[-1]["nav_usd"] if points else initial
        first = points[0]["nav_usd"] if points else initial
        try:
            return_pct = (
                ((latest - first) / first) * 100.0 if first not in (0, 0.0) else 0.0
            )
        except Exception:
            return_pct = 0.0
        return {
            "ok": True,
            "account_id": aid,
            "base_currency": profile.base_currency or "USDT",
            "initial_balance_usd": initial,
            "points": points,
            "count": len(points),
            "latest_nav_usd": float(latest),
            "first_nav_usd": float(first),
            "return_pct": return_pct,
        }

    return [
        ("GET", "/accounts/list", list_accounts),
        ("POST", "/accounts/list", list_accounts),
        ("POST", "/accounts/get", get_account),
        ("POST", "/accounts/upsert", upsert_account),
        ("POST", "/accounts/test_balance", test_balance),
        ("POST", "/accounts/delete", delete_account),
        ("POST", "/accounts/quarantine", quarantine_account),
        ("POST", "/accounts/reset_paper", reset_paper),
        ("POST", "/accounts/equity_curve", equity_curve),
        ("GET", "/accounts/equity_curve", equity_curve),
        ("POST", "/accounts/headers/list", list_headers),
        ("POST", "/accounts/headers/patch", patch_headers),
        ("GET", "/accounts/proposals/list", list_account_proposals),
        ("POST", "/accounts/proposals/list", list_account_proposals),
        ("POST", "/accounts/proposals/get", get_account_proposal),
        ("POST", "/accounts/proposals/apply", apply_account_proposal),
        ("POST", "/accounts/proposals/reject", reject_account_proposal),
    ]
