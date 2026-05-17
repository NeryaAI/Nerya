"""Sandboxed account-intake flow.

The account intake module solves the "let the agent walk an operator
through adding an exchange / wallet without ever seeing the operator's
secrets" problem.

Flow
====

1.  **Agent or operator opens an intake.**
    ``open_intake(paths, venue=..., kind="cex"|"chain", account_id=...)``
    builds an :class:`AccountIntake` describing exactly which fields
    the user must fill in (pulled straight from the venue's
    :class:`CredentialField` schema or the wallet provider catalogue).
    The intake also captures the non-secret context (mode, base
    currency, limits, permissions) so the operator can confirm /
    edit it before submitting.

2.  **Dashboard renders dedicated input boxes.**
    The dashboard reads the intake by id and renders one input per
    field. Secret fields use ``type="password"`` and the values are
    *never* returned in API responses; they only travel from the
    user's browser to ``submit_intake`` and from there into the vault.

3.  **System auto-encrypts and stores credentials.**
    ``submit_intake(paths, intake_id, plaintext_values, ...)``
    derives a deterministic vault name per field, calls
    :class:`SecretVault.put` with the right scope, replaces the field
    value with the resulting ``vault://`` reference, and finally
    delegates to :func:`nerya.trading.accounts.upsert_account` to
    persist the row. The agent only ever observes ``vault://...``
    references.

4.  **Agent confirms.**
    ``get_intake`` returns the public view (no plaintext) and
    ``list_intakes`` lets the agent see what's pending. A submitted
    intake is marked ``applied`` and stays around for audit.

Threat model
============

* The agent cannot read intake plaintext: ``submit_intake`` is the
  only path that touches plaintext, and it stores it in the vault
  before returning. The intake record on disk only carries vault
  references.
* The operator does not have to copy/paste secrets into the chat:
  Nerya separates the message channel (LLM-facing) from the intake
  channel (system-facing).
* If the operator cancels mid-flow, no plaintext was persisted —
  ``cancel_intake`` simply marks the intake ``cancelled``.
"""

from __future__ import annotations

import calendar
import hashlib
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..connectors.provider_spec import CredentialField, get_registry
from ..core import jsonl, yaml_io
from ..core.atomic_write import atomic_write_text
from ..core.errors import NeryaError, TradingError
from ..core.ids import proposal_id
from ..core.paths import WorkspacePaths
from ..core.time import now_iso
from ..security.secrets import SecretVault
from ..security.secret_scanner import SecretBuffer, expand_placeholders
from . import accounts as accounts_mod


INTAKE_KIND = "account_credential_intake"
INTAKE_STATES = ("open", "applied", "cancelled", "expired")
DEFAULT_TTL_SECONDS = 30 * 60  # 30 minutes — operator-friendly default


class AccountIntakeError(NeryaError):
    """Raised when an intake cannot be processed."""


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------


@dataclass
class AccountIntakeField:
    """Per-field schema captured at intake-open time."""

    name: str
    label: str
    kind: str = "secret"  # "secret" | "public" | "url"
    required: bool = True
    description: str = ""
    placeholder: str = ""
    sensitive: bool = True
    vault_scope: str = "exchange"

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "label": self.label,
            "kind": self.kind,
            "required": self.required,
            "description": self.description,
            "placeholder": self.placeholder,
            "sensitive": self.sensitive,
            "vault_scope": self.vault_scope,
        }

    @classmethod
    def from_credential_field(cls, f: CredentialField) -> "AccountIntakeField":
        return cls(
            name=f.name, label=f.label, kind=f.kind, required=f.required,
            description=f.description, placeholder=f.placeholder,
            sensitive=f.sensitive, vault_scope=f.vault_scope,
        )

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "AccountIntakeField":
        return cls(
            name=str(raw.get("name") or ""),
            label=str(raw.get("label") or raw.get("name") or ""),
            kind=str(raw.get("kind") or "secret"),
            required=bool(raw.get("required", True)),
            description=str(raw.get("description") or ""),
            placeholder=str(raw.get("placeholder") or ""),
            sensitive=bool(raw.get("sensitive", True)),
            vault_scope=str(raw.get("vault_scope") or "exchange"),
        )


@dataclass
class AccountIntake:
    """A pending request for the operator to fill in account credentials."""

    id: str
    state: str
    account_kind: str  # "cex" | "dex" | "chain" | "perp" | "futures"
    venue: str
    provider_label: str
    account_id: str
    requested_by: str  # "agent" | "operator" | "<actor>"
    created_at: str
    expires_at: str
    schema: list[AccountIntakeField] = field(default_factory=list)
    # Non-secret defaults the agent or operator already chose. We store
    # these so the dashboard can pre-fill the form and the submit path
    # has everything it needs to call upsert_account.
    profile_defaults: dict[str, Any] = field(default_factory=dict)
    notes: str = ""
    # Once applied, this carries the vault refs the system stored. It
    # never carries plaintext.
    applied_credential_refs: dict[str, str] = field(default_factory=dict)
    applied_at: str = ""
    cancelled_at: str = ""
    cancel_reason: str = ""

    def public_view(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "kind": INTAKE_KIND,
            "state": self.state,
            "account_kind": self.account_kind,
            "venue": self.venue,
            "provider_label": self.provider_label,
            "account_id": self.account_id,
            "requested_by": self.requested_by,
            "created_at": self.created_at,
            "expires_at": self.expires_at,
            "schema": [f.to_dict() for f in self.schema],
            "profile_defaults": dict(self.profile_defaults),
            "notes": self.notes,
            "applied_credential_refs": dict(self.applied_credential_refs),
            "applied_at": self.applied_at,
            "cancelled_at": self.cancelled_at,
            "cancel_reason": self.cancel_reason,
        }

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "AccountIntake":
        return cls(
            id=str(raw["id"]),
            state=str(raw.get("state", "open")),
            account_kind=str(raw.get("account_kind") or "cex"),
            venue=str(raw.get("venue") or ""),
            provider_label=str(raw.get("provider_label") or ""),
            account_id=str(raw.get("account_id") or ""),
            requested_by=str(raw.get("requested_by") or "operator"),
            created_at=str(raw.get("created_at") or now_iso()),
            expires_at=str(raw.get("expires_at") or ""),
            schema=[
                AccountIntakeField.from_dict(f)
                for f in (raw.get("schema") or [])
                if isinstance(f, dict)
            ],
            profile_defaults=dict(raw.get("profile_defaults") or {}),
            notes=str(raw.get("notes") or ""),
            applied_credential_refs=dict(raw.get("applied_credential_refs") or {}),
            applied_at=str(raw.get("applied_at") or ""),
            cancelled_at=str(raw.get("cancelled_at") or ""),
            cancel_reason=str(raw.get("cancel_reason") or ""),
        )


# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------


def _intake_root(paths: WorkspacePaths) -> Path:
    return paths.proposals / "account_intake"


def _intake_path(paths: WorkspacePaths, intake_id: str) -> Path:
    return _intake_root(paths) / intake_id / "intake.yml"


def _save(paths: WorkspacePaths, intake: AccountIntake) -> None:
    p = _intake_path(paths, intake.id)
    p.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_text(p, yaml_io.dumps(intake.public_view()))


def _load(paths: WorkspacePaths, intake_id: str) -> AccountIntake | None:
    p = _intake_path(paths, intake_id)
    if not p.exists():
        return None
    raw = yaml_io.load(p, default={}) or {}
    if not isinstance(raw, dict) or not raw.get("id"):
        return None
    intake = AccountIntake.from_dict(raw)
    # Lazy-expire so a leftover open intake can never silently hang
    # around forever. We persist the expiry in UTC (``...Z``) and parse
    # it as UTC via :func:`calendar.timegm` — ``time.mktime`` would
    # interpret the parsed struct as local time and shift the deadline
    # by the host's UTC offset.
    if intake.state == "open" and intake.expires_at:
        try:
            parsed = time.strptime(intake.expires_at, "%Y-%m-%dT%H:%M:%SZ")
            expiry = calendar.timegm(parsed)
            if expiry < time.time():
                intake.state = "expired"
                _save(paths, intake)
        except Exception:
            pass
    return intake


# ---------------------------------------------------------------------------
# Schema resolution
# ---------------------------------------------------------------------------


def _resolve_schema(account_kind: str, venue: str) -> tuple[list[AccountIntakeField], str]:
    """Return ``(fields, provider_label)`` for an intake target.

    Supports:

    * ``cex`` / ``dex`` / ``perp`` / ``futures`` / ``broker`` /
      ``data_source`` venues — schema comes from
      :class:`ExchangeProviderSpec.credential_fields`. ``broker`` covers
      legacy/desktop-broker integrations like Interactive Brokers,
      MetaTrader 5, and Alpaca; ``data_source`` covers read-only
      providers (Tushare, Polygon.io, CoinGecko, …) that auto-pair with
      a paper account.
    * ``chain`` venues backed by a wallet provider — pulled from
      :data:`nerya.wallet.registry.PROVIDERS`. The wallet ``id`` is
      passed in via ``venue`` (e.g. ``okx_os``, ``bitget``,
      ``binance_agentic``, ``coinbase``, ``self_custody``).
    """

    kind = (account_kind or "cex").lower()
    v = (venue or "").lower()
    if kind == "chain":
        from ..wallet.registry import (
            PROVIDERS as WALLET_PROVIDERS,
            resolve_provider_name as _resolve_wallet_provider_name,
        )

        provider_name = _resolve_wallet_provider_name(v)
        entry = WALLET_PROVIDERS.get(provider_name or "")
        if entry is None:
            raise AccountIntakeError(
                f"unknown wallet provider {venue!r}; "
                f"known: {sorted(WALLET_PROVIDERS)}"
            )
        fields_raw = list(entry.get("credential_fields") or [])
        fields_raw.extend(list(entry.get("advanced_credential_fields") or []))
        fields = [AccountIntakeField.from_dict(dict(f)) for f in fields_raw]
        return fields, str(entry.get("label") or v)
    spec = get_registry().find(v)
    if spec is None:
        raise AccountIntakeError(
            f"unknown venue {venue!r}; ask /exchanges/providers for the "
            "current list or run the exchange_author wizard first."
        )
    fields = [
        AccountIntakeField.from_credential_field(f)
        for f in (spec.credential_fields or ())
    ]
    return fields, spec.label


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def open_intake(
    paths: WorkspacePaths,
    *,
    account_kind: str,
    venue: str,
    account_id: str,
    requested_by: str = "operator",
    profile_defaults: dict[str, Any] | None = None,
    notes: str = "",
    ttl_seconds: int = DEFAULT_TTL_SECONDS,
) -> AccountIntake:
    """Create and persist a new intake record.

    Returns the freshly-created :class:`AccountIntake`. The caller
    should hand the ``id`` to the dashboard so the operator can fill
    it in.
    """

    aid = (account_id or "").strip()
    if not aid:
        raise AccountIntakeError("account_id is required")
    if not aid.replace("-", "").replace("_", "").isalnum():
        raise AccountIntakeError(f"invalid account id {aid!r}")
    schema, label = _resolve_schema(account_kind, venue)
    pid = proposal_id()
    created = now_iso()
    expiry_seconds = max(60, int(ttl_seconds or DEFAULT_TTL_SECONDS))
    expires_at = time.strftime(
        "%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() + expiry_seconds),
    )
    intake = AccountIntake(
        id=pid,
        state="open",
        account_kind=(account_kind or "cex").lower(),
        venue=(venue or "").lower(),
        provider_label=label,
        account_id=aid,
        requested_by=str(requested_by or "operator"),
        created_at=created,
        expires_at=expires_at,
        schema=schema,
        profile_defaults=dict(profile_defaults or {}),
        notes=str(notes or ""),
    )
    _save(paths, intake)
    jsonl.append(
        paths.journal("operator"),
        {
            "kind": "account_intake.opened",
            "intake_id": pid,
            "venue": intake.venue,
            "account_kind": intake.account_kind,
            "account_id": aid,
            "requested_by": intake.requested_by,
            "ts": created,
        },
    )
    return intake


def get_intake(paths: WorkspacePaths, intake_id: str) -> AccountIntake:
    intake = _load(paths, intake_id)
    if intake is None:
        raise AccountIntakeError(f"unknown intake: {intake_id}")
    return intake


def list_intakes(
    paths: WorkspacePaths, *, state: str | None = None,
) -> list[AccountIntake]:
    root = _intake_root(paths)
    if not root.exists():
        return []
    out: list[AccountIntake] = []
    for child in sorted(root.iterdir()):
        if not child.is_dir():
            continue
        intake = _load(paths, child.name)
        if intake is None:
            continue
        if state and intake.state != state:
            continue
        out.append(intake)
    return out


def cancel_intake(
    paths: WorkspacePaths, intake_id: str, *, reason: str = "operator_cancelled",
) -> AccountIntake:
    intake = get_intake(paths, intake_id)
    if intake.state in ("applied", "cancelled", "expired"):
        return intake
    intake.state = "cancelled"
    intake.cancelled_at = now_iso()
    intake.cancel_reason = reason or "cancelled"
    _save(paths, intake)
    jsonl.append(
        paths.journal("operator"),
        {
            "kind": "account_intake.cancelled",
            "intake_id": intake_id,
            "reason": intake.cancel_reason,
            "ts": intake.cancelled_at,
        },
    )
    return intake


# ---------------------------------------------------------------------------
# Submit path — the only function that touches plaintext
# ---------------------------------------------------------------------------


def _vault_secret_name(account_id: str, field_name: str) -> str:
    """Build a deterministic secret name keyed by (account_id, field).

    Using a digest for collision avoidance keeps the on-disk preview
    short while making sure two accounts can't share a slot. The name
    pattern matches :data:`SecretVault` constraints (lowercase a-z0-9
    + ``_-.`` starting with a letter).
    """

    digest = hashlib.sha1(
        f"acct::{account_id}::{field_name}".encode("utf-8")
    ).hexdigest()[:12]
    safe_field = "".join(
        c if c.isalnum() or c in "-_" else "_"
        for c in field_name.lower()
    )
    return f"acct_{safe_field}_{digest}"


def store_credential_values(
    paths: WorkspacePaths,
    *,
    account_id: str,
    account_kind: str,
    credential_values: dict[str, str],
    field_scopes: dict[str, str] | None = None,
    operator: str = "dashboard",
    vault_passphrase: str | None = None,
) -> dict[str, str]:
    """Convert an account credential map into vault refs.

    Callers may pass either existing ``vault://`` references or one-time
    plaintext values. Plaintext is written to :class:`SecretVault` and the
    returned map contains only ``vault://`` references.
    """

    if not account_id:
        raise AccountIntakeError("account_id is required before storing credentials")
    vault = SecretVault.open(paths.vault_enc, passphrase=vault_passphrase)
    out: dict[str, str] = {}
    scopes = field_scopes or {}
    for raw_name, raw_value in (credential_values or {}).items():
        name = str(raw_name or "").strip()
        value = str(raw_value or "").strip()
        if not name or not value:
            continue
        if value.startswith("vault://"):
            out[name] = value
            continue
        secret_name = _vault_secret_name(account_id, name)
        vault.put(
            name=secret_name,
            value=value,
            kind=f"account_{account_kind or 'account'}",
            scope=[scopes.get(name) or "exchange"],
            owner=f"accounts/{account_id}:{operator or 'dashboard'}",
        )
        out[name] = f"vault://{secret_name}"
    return out


def submit_intake(
    paths: WorkspacePaths,
    intake_id: str,
    *,
    plaintext_values: dict[str, str],
    profile_overrides: dict[str, Any] | None = None,
    operator: str = "dashboard",
    vault_passphrase: str | None = None,
    secret_buffer: SecretBuffer | None = None,
) -> tuple[AccountIntake, accounts_mod.AccountProfile]:
    """Encrypt + persist credentials and apply the account row.

    ``plaintext_values`` is the *only* parameter that ever carries raw
    secrets. Values are dropped into the vault before the function
    returns; the agent-facing intake record contains nothing but the
    resulting ``vault://`` references.

    When ``secret_buffer`` is provided, any field value that contains
    ``<<NERYA_SECRET:<tok>>>`` placeholders (produced by
    :func:`nerya.security.secret_scanner.scan_and_redact` when the
    operator pasted plaintext into the gateway chat) is expanded back
    to its original plaintext just-in-time and the buffer entry is
    consumed, so the same secret can never be redeemed twice. Fields
    referencing unknown tokens are rejected outright.
    """

    intake = get_intake(paths, intake_id)
    if intake.state != "open":
        raise AccountIntakeError(
            f"intake {intake_id} is in state {intake.state!r}; refusing to apply"
        )

    cleaned: dict[str, str] = {}
    for raw_field, raw_val in (plaintext_values or {}).items():
        if not isinstance(raw_field, str) or not isinstance(raw_val, str):
            continue
        if raw_val == "":
            continue
        if "<<NERYA_SECRET:" in raw_val:
            if secret_buffer is None:
                raise AccountIntakeError(
                    f"field {raw_field!r} carries placeholder tokens but no "
                    "secret buffer was attached to the submit call"
                )
            expanded, _resolved = expand_placeholders(
                raw_val, buffer=secret_buffer, consume=True,
            )
            if "<<NERYA_SECRET:" in expanded:
                raise AccountIntakeError(
                    f"field {raw_field!r} references an unknown or expired "
                    "secret token; ask the operator to re-enter it"
                )
            cleaned[raw_field] = expanded
        else:
            cleaned[raw_field] = raw_val

    schema_by_name = {f.name: f for f in intake.schema}
    missing_required = [
        f.name for f in intake.schema
        if f.required and f.name not in cleaned
    ]
    if missing_required:
        raise AccountIntakeError(
            f"missing required fields: {', '.join(missing_required)}"
        )
    unknown = sorted(set(cleaned) - set(schema_by_name))
    if unknown:
        raise AccountIntakeError(
            f"unknown fields submitted: {', '.join(unknown)}"
        )

    credential_refs: dict[str, str] = {}
    public_overrides: dict[str, Any] = {}
    sensitive_values: dict[str, str] = {}
    field_scopes: dict[str, str] = {}
    for fname, value in cleaned.items():
        sch = schema_by_name[fname]
        if sch.sensitive:
            sensitive_values[fname] = value
            field_scopes[fname] = sch.vault_scope or "exchange"
        else:
            # Public fields stay in cleartext on the account row — they
            # are URLs / addresses / project ids, not secrets. The
            # ``credentials`` map only ever takes ``vault://`` refs (see
            # :func:`accounts_mod._coerce_credentials_row`), so we
            # store these via ``profile_overrides`` instead.
            public_overrides[fname] = value

    if sensitive_values:
        credential_refs = store_credential_values(
            paths,
            account_id=intake.account_id,
            account_kind=intake.account_kind,
            credential_values=sensitive_values,
            field_scopes=field_scopes,
            operator=operator,
            vault_passphrase=vault_passphrase,
        )

    payload = dict(intake.profile_defaults or {})
    if isinstance(profile_overrides, dict):
        payload.update(profile_overrides)
    payload.update(public_overrides)
    payload.setdefault("id", intake.account_id)
    payload.setdefault("kind", intake.account_kind)
    payload.setdefault("venue", intake.venue)
    # Force paper mode for venues that cannot place orders (data_source
    # providers, the ``yahoo`` connector, …). Operators can still flip
    # to live by editing the row later, but the agent-driven flow
    # should never silently create a "live" account against a venue
    # that has no order-placement support.
    if not payload.get("mode"):
        try:
            spec = get_registry().find(intake.venue)
        except Exception:
            spec = None
        place_order = (spec.supports.get("place_order") if spec else True)
        if intake.account_kind == "data_source" or place_order is False:
            payload["mode"] = "paper"
    if credential_refs:
        existing_creds = payload.get("credentials")
        merged: dict[str, str] = {}
        if isinstance(existing_creds, dict):
            for k, v in existing_creds.items():
                if isinstance(v, str) and v.startswith("vault://"):
                    merged[str(k)] = v
        merged.update(credential_refs)
        payload["credentials"] = merged

    try:
        profile = accounts_mod.upsert_account(paths, payload, operator=operator)
    except TradingError as exc:
        raise AccountIntakeError(f"upsert_account refused: {exc}") from exc

    intake.state = "applied"
    intake.applied_at = now_iso()
    intake.applied_credential_refs = dict(credential_refs)
    _save(paths, intake)
    jsonl.append(
        paths.journal("operator"),
        {
            "kind": "account_intake.applied",
            "intake_id": intake.id,
            "account_id": profile.id,
            "venue": profile.venue,
            "operator": operator,
            "ts": intake.applied_at,
            "credential_refs": list(credential_refs.keys()),
        },
    )
    return intake, profile


__all__ = [
    "AccountIntake",
    "AccountIntakeField",
    "AccountIntakeError",
    "INTAKE_KIND",
    "INTAKE_STATES",
    "open_intake",
    "get_intake",
    "list_intakes",
    "cancel_intake",
    "submit_intake",
    "store_credential_values",
]
