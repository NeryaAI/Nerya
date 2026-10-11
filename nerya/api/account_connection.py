"""Operator connection flow: validate in memory, then save; never place orders."""
from __future__ import annotations

from copy import deepcopy
import hashlib
import json
import math
import time

from ..connectors.provider_spec import get_registry
from ..core.errors import SecretAccessDenied, SecretNotFoundError
from ..security.credential_probe import credential_probe
from ..security.secrets import SecretVault
from ..trading import accounts, account_snapshots
from ..trading.locks import trading_lock


class ConnectionFailure(Exception):
    def __init__(self, code: str, *, fields: list[str] | None = None):
        self.code = code
        self.fields = fields or []
        super().__init__(code)


def failure(code: str, *, fields: list[str] | None = None) -> dict:
    # Deliberately do not return arbitrary provider exception text. It can
    # contain a signed request, a key in a URL, or even a short passphrase.
    return {"ok": False, "error": code, "fields": fields or []}


def _error_code(text: str) -> str:
    text = text.lower()
    if any(word in text for word in ("vault", "secretaccessdenied", "secretnotfound")):
        return "vault_unavailable"
    if any(word in text for word in ("not installed", "no module named")):
        return "connector_unavailable"
    if any(word in text for word in ("rate", "429", "too many")):
        return "rate_limited"
    if any(word in text for word in ("wallet", "binding", "address_missing")):
        return "wallet_not_ready"
    if any(word in text for word in ("auth", "key", "signature", "permission", "401", "403", "passphrase")):
        return "auth_error"
    if any(word in text for word in ("timeout", "timed out", "network", "connect", "resolve", "unavailable")):
        return "network_error"
    return "connection_failed"


def _schema(config, row: dict):
    registry = get_registry(config.paths.root)
    registry.reload_workspace(config.paths.root)
    spec = registry.find(str(row.get("venue") or ""))
    if row.get("wallet_id") and row.get("kind") in ("chain", "dex"):
        return None
    if spec is None or spec.id in ("mock", "mock_chain") or not spec.supports.get("balances"):
        raise ConnectionFailure("unsupported_provider")
    return spec


def probe(config, row: dict):
    """Return a read-only snapshot, using expiring refs for unsaved credentials."""
    from .routes_accounts import _balance_test_profile

    body = deepcopy(row)
    if body.get("mode") == "paper":
        return account_snapshots.capture_snapshot(
            config, str(body.get("id") or "balance_probe"),
            profile=_balance_test_profile(body), persist=False,
        )
    spec = _schema(config, body)
    credentials = {str(k): str(v).strip() for k, v in (body.get("credentials") or {}).items() if v}
    scopes = {f.name: f.vault_scope or "exchange" for f in spec.credential_fields} if spec else {}
    missing = accounts._missing_required_connection_fields(
        venue=str(body.get("venue") or ""), mode="shadow",
        permissions={"read_balances": True}, credentials=credentials,
        provider_config=body.get("provider_config") or {}, raw_account=body,
        wallet_bound_account=bool(body.get("wallet_id")) and body.get("kind") in ("chain", "dex"),
    )
    if missing:
        raise ConnectionFailure("missing_fields", fields=missing)
    # Catch a missing/unreadable existing vault before the connector silently
    # drops its refs and reports a misleading exchange authentication failure.
    stored = [(k, v) for k, v in credentials.items() if v.startswith("vault://")]
    if stored:
        vault = SecretVault.open(config.paths.vault_enc)
        for key, ref in stored:
            credentials[key] = vault.resolve(ref.removeprefix("vault://"), required_scope=scopes.get(key, "exchange"))
    with credential_probe(config.paths.root, credentials, scopes) as refs:
        body["credentials"] = refs
        body["mode"] = "shadow"
        body["live_trading_enabled"] = False
        body["permissions"] = {"read_balances": True, "place_order": False, "cancel_order": False, "withdraw": False}
        profile = _balance_test_profile(body)
        snap = account_snapshots.capture_snapshot(config, profile.id, profile=profile, persist=False)
        if snap.health != "ok" or snap.source not in ("live", "shadow"):
            code = snap.health if snap.health in ("auth_error", "rate_limited") else _error_code(str(snap.meta))
            raise ConnectionFailure(code)
        if not math.isfinite(snap.nav_usd):
            raise ConnectionFailure("connection_failed")
        return snap


def test_connection(client, payload):
    if not isinstance(payload, dict):
        return failure("payload_required")
    try:
        row = dict(payload)
        aid = str(row.get("account_id") or "")
        if aid and not any(k in row for k in ("venue", "credentials", "wallet_id")):
            row = deepcopy(accounts.get_account_profile(client.config.paths, aid).raw)
        snap = probe(client.config, row)
        return {"ok": True, "account_id": snap.account_id, "snapshot": snap.asdict(),
                "verified": snap.source in ("live", "shadow"), "mode": row.get("mode", "paper")}
    except ConnectionFailure as exc:
        return failure(exc.code, fields=exc.fields)
    except (SecretAccessDenied, SecretNotFoundError):
        return failure("vault_unavailable")
    except Exception as exc:
        return failure(_error_code(str(exc)))


def _prepare(config, payload: dict, existing):
    if existing:
        if payload.get("expected_revision") != accounts.account_revision(existing):
            raise ConnectionFailure("account_changed")
        if str(payload.get("venue") or existing.venue) != existing.venue:
            raise ConnectionFailure("provider_change_requires_new_account")
        row = deepcopy(existing.raw)
        policy = payload.get("policy")
        if policy is not None:
            if not isinstance(policy, dict) or policy.get("mode") not in ("paper", "shadow", "canary", "live"):
                raise ConnectionFailure("invalid_account")
            limits = policy.get("limits", {})
            permissions = policy.get("permissions", {})
            if not isinstance(limits, dict) or not isinstance(permissions, dict):
                raise ConnectionFailure("invalid_account")
            known_limits = accounts.AccountLimits.__dataclass_fields__
            if any(key not in known_limits or isinstance(value, bool) or not isinstance(value, (int, float))
                   or not math.isfinite(value) or value < 0 for key, value in limits.items()):
                raise ConnectionFailure("invalid_account")
            if any(key not in ("read_balances", "place_order", "cancel_order", "withdraw") or not isinstance(value, bool)
                   for key, value in permissions.items()):
                raise ConnectionFailure("invalid_account")
            enabled = policy.get("live_trading_enabled", existing.live_trading_enabled)
            if not isinstance(enabled, bool):
                raise ConnectionFailure("invalid_account")
            row.update(mode=policy["mode"], live_trading_enabled=enabled,
                       limits={**(row.get("limits") or {}), **limits},
                       permissions={**(row.get("permissions") or {}), **permissions, "withdraw": False})
    else:
        row = {"id": str(payload.get("id") or ""), "venue": str(payload.get("venue") or "").strip().lower(),
               "kind": str(payload.get("kind") or "cex"),
               "mode": "paper" if payload.get("mode") == "paper" else "shadow",
               "live_trading_enabled": False, "status": "active",
               "permissions": {"read_balances": True, "place_order": payload.get("mode") == "paper",
                               "cancel_order": payload.get("mode") == "paper", "withdraw": False}}
    for key in ("label", "base_currency", "subaccount"):
        if key in payload:
            row[key] = str(payload[key]).strip()
    if not existing and "wallet_id" in payload:
        row["wallet_id"] = str(payload["wallet_id"])
    if row.get("mode") == "paper":
        balance = float(payload.get("initial_balance_usd", row.get("initial_balance_usd", 10000)))
        if not math.isfinite(balance) or balance < 0:
            raise ConnectionFailure("invalid_balance")
        row["initial_balance_usd"] = balance
    else:
        spec = _schema(config, row)
        allowed = {f.name: f for f in spec.credential_fields} if spec else {}
        for key in ("credentials", "provider_config"):
            values = payload.get(key, {})
            if not isinstance(values, dict):
                raise ConnectionFailure("invalid_account")
            updates = {}
            for name, value in values.items():
                field = allowed.get(name)
                if not field or bool(field.sensitive) != (key == "credentials"):
                    raise ConnectionFailure("invalid_account")
                if not isinstance(value, (str, int, float, bool)):
                    raise ConnectionFailure("invalid_account")
                # Empty secret inputs mean keep the stored credential while
                # editing. An empty public option deliberately clears it.
                if key == "credentials" and not str(value).strip():
                    continue
                updates[name] = str(value).strip()
            row[key] = {**(row.get(key) or {}), **updates}
    return row


def connect(client, payload):
    """Create/reconnect an account. Retries are idempotent; edits use a revision."""
    from .routes_accounts import _account_summary, _journal_operator, _vaultify_plaintext_credentials

    if not isinstance(payload, dict):
        return failure("payload_required")
    aid = str(payload.get("id") or "").strip()
    if not aid or len(aid) > 80 or not aid.replace("-", "").replace("_", "").isalnum():
        return failure("invalid_account_id")
    request_id = str(payload.get("request_id") or "")
    if not request_id or len(request_id) > 128:
        return failure("request_id_required")
    digest = hashlib.sha256(json.dumps(payload, sort_keys=True, default=str).encode()).hexdigest()
    paths = client.config.paths
    try:
        with trading_lock(paths, "account-connections") as acquired:
            if not acquired:
                return failure("connection_busy")
            existing = accounts.load_account_profiles(paths).get(aid)
            receipt = existing.raw.get("connection_receipt", {}) if existing else {}
            if receipt.get("request_id") == request_id and receipt.get("digest") == digest:
                summary = _account_summary(client, existing)
                # Replay the accepted verification, not a later background
                # refresh (or a missing optional snapshot DB write).
                if isinstance(receipt.get("snapshot"), dict):
                    summary["snapshot"] = deepcopy(receipt["snapshot"])
                return {"ok": True, "account": summary, "replayed": True,
                        "verified": bool(receipt.get("verified_at"))}
            if payload.get("operation", "create") == "update":
                if not existing:
                    return failure("unknown_account")
            elif existing:
                return failure("account_exists")
            row = _prepare(client.config, payload, existing)
            if not row.get("venue"):
                return failure("missing_fields", fields=["venue"])
            snap = probe(client.config, row) if row.get("mode") != "paper" else None
            current = accounts.load_account_profiles(paths).get(aid)
            if (existing is None) != (current is None) or (
                existing and current and accounts.account_revision(existing) != accounts.account_revision(current)
            ):
                return failure("account_changed")
            # Credential material is persisted only after the real private
            # balance read succeeds. Preserve all unseen configuration on edit.
            stored = _vaultify_plaintext_credentials(client, row, operator="dashboard")
            profile = accounts.upsert_account(paths, stored, operator="dashboard", connection_receipt={
                "request_id": request_id, "digest": digest, "verified_at": time.time() if snap else None,
                "snapshot": snap.asdict() if snap else None,
            })
            if snap:
                try:
                    account_snapshots._persist(paths, snap)
                except Exception:
                    # The account is saved. A transient snapshot storage error
                    # must not be misrepresented as a failed account creation.
                    pass
            result = _account_summary(client, profile)
            if snap:
                result["snapshot"] = snap.asdict()
            _journal_operator(client, {"kind": "account.connected", "account_id": aid,
                                       "mode": profile.mode, "verified": bool(snap)})
            return {"ok": True, "account": result, "verified": bool(snap)}
    except ConnectionFailure as exc:
        return failure(exc.code, fields=exc.fields)
    except (SecretAccessDenied, SecretNotFoundError):
        return failure("vault_unavailable")
    except (ValueError, TypeError):
        return failure("invalid_account")
    except Exception as exc:
        return failure(_error_code(str(exc)))
