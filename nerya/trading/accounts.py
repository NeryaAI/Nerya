"""Account registry — reads ``workspace/accounts/accounts.yml``.

The original :class:`Account` shape (``id``, ``mode``, ``initial_balance_usd``,
``status``, ``venue``/``kind``/``raw``) is kept verbatim so existing call
sites continue to work. This module introduces a richer
:class:`AccountProfile` view layered on top — operators get
mode/permission/limits/credentials in one place, while existing code
keeps using the slim :class:`Account` dataclass.

``AccountProfile`` is intentionally derived (parsed from the same YAML
row) rather than replacing :class:`Account`. That means:

* The trading kernel's risk gate can keep importing ``Account``.
* New code (capital reservation, executor orchestrator, dashboard) can
  ask for the profile without forcing every legacy site to migrate at
  once.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

from ..core import yaml_io
from ..core.errors import TradingError
from ..core.paths import WorkspacePaths


# ---------------------------------------------------------------------------
# AccountMode and AccountStatus literals.
# ---------------------------------------------------------------------------

AccountMode = Literal["paper", "shadow", "canary", "live"]
AccountStatus = Literal["active", "read_only", "disabled", "quarantined"]


@dataclass
class AccountPermissions:
    read_balances: bool = True
    place_order: bool = False
    cancel_order: bool = False
    withdraw: bool = False  # always false — Nerya never touches withdrawals

    def asdict(self) -> dict[str, Any]:
        return {
            "read_balances": self.read_balances,
            "place_order": self.place_order,
            "cancel_order": self.cancel_order,
            # Force False on the wire so a misconfigured YAML row can't
            # silently advertise withdraw capability.
            "withdraw": False,
        }


@dataclass
class AccountLimits:
    """Per-account guard rails. Strategies still have their own limits.

    The two layers stack: a 100 USDT order goes through if both the
    account *and* the strategy say it's allowed.
    """

    max_account_nav_usd: float = 0.0
    max_strategy_allocation_pct: float = 0.0
    max_order_notional_usd: float = 0.0
    max_daily_loss_usd: float = 0.0
    max_drawdown_pct: float = 0.0
    max_leverage: float = 1.0
    fee_buffer_bps: float = 5.0
    min_free_balance_pct: float = 0.0

    def asdict(self) -> dict[str, Any]:
        return {
            "max_account_nav_usd": self.max_account_nav_usd,
            "max_strategy_allocation_pct": self.max_strategy_allocation_pct,
            "max_order_notional_usd": self.max_order_notional_usd,
            "max_daily_loss_usd": self.max_daily_loss_usd,
            "max_drawdown_pct": self.max_drawdown_pct,
            "max_leverage": self.max_leverage,
            "fee_buffer_bps": self.fee_buffer_bps,
            "min_free_balance_pct": self.min_free_balance_pct,
        }


@dataclass
class Account:
    """Legacy slim account shape used by the existing risk path.

    Kept stable so :mod:`nerya.trading.risk`, the execution engine and
    every CLI/dashboard call site continues to work without churn.
    """

    id: str
    exchange: str
    mode: str  # paper | shadow | canary | live
    live_trading_enabled: bool
    initial_balance_usd: float
    status: str = "active"
    venue: str = ""
    kind: str = "cex"  # cex | dex | chain
    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def is_live(self) -> bool:
        # ``live_trading_enabled`` only takes effect when the account is
        # in ``live`` mode. Shadow and canary modes still resolve as
        # "not live" for the legacy path — the new control plane reads
        # the AccountProfile directly to make finer-grained decisions.
        return self.mode == "live" and self.live_trading_enabled

    @property
    def is_real_money(self) -> bool:
        """True if any path could result in real funds moving.

        Mirrors :attr:`AccountProfile.is_real_money` so the legacy risk
        path can make the same fail-closed decisions as the new control
        plane.
        """
        return self.mode in ("canary", "live")

    def connector_cfg(self) -> dict[str, Any]:
        """Flattened config for ``build_connector``."""
        cfg = dict(self.raw or {})
        cfg.setdefault("venue", self.venue or self.exchange)
        cfg.setdefault("kind", self.kind)
        cfg["live"] = self.is_live
        return cfg


@dataclass
class AccountProfile:
    """Rich account view used by the new trading control plane.

    Built from the same YAML row that produces :class:`Account`. The
    profile carries explicit permissions, limits, base currency and
    provider spec so the BudgetChecker / Orchestrator can reason about
    "what is this account allowed to do, and how much".
    """

    id: str
    mode: AccountMode
    venue: str
    kind: str
    provider_spec: str
    base_currency: str
    subaccount: str
    status: AccountStatus
    live_trading_enabled: bool
    initial_balance_usd: float
    permissions: AccountPermissions = field(default_factory=AccountPermissions)
    limits: AccountLimits = field(default_factory=AccountLimits)
    credentials: dict[str, str] = field(default_factory=dict)
    # Every account can pin a specific wallet
    # provider id (declared under ``wallet.providers.<id>`` in
    # ``nerya.yml``). Empty falls back to the legacy single
    # ``wallet.provider`` selection so existing fixtures stay green.
    wallet_id: str = ""
    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def is_real_money(self) -> bool:
        """True if any path could result in real funds moving."""
        return self.mode in ("canary", "live")

    @property
    def can_place_order(self) -> bool:
        """``place_order`` permission combined with mode/status guards."""
        if self.status != "active":
            return False
        if self.mode == "shadow":
            return False
        if self.is_real_money and not self.live_trading_enabled:
            return False
        return bool(self.permissions.place_order)

    @property
    def reads_real_balances(self) -> bool:
        """Whether the account snapshot loop should hit a real venue."""
        if self.mode in ("live", "canary", "shadow"):
            return bool(self.permissions.read_balances)
        return False

    def to_connector_account(self, *, live: bool | None = None) -> Account:
        """Bridge into :class:`Account` for connector-facing code.

        Canary/live are real-money connector modes; paper/shadow stay
        paper by default. Private balance reads can pass ``live=True``
        without granting order permission.
        """
        legacy_mode = self.mode if self.mode in ("paper", "live") else (
            "live" if self.is_real_money else "paper"
        )
        live_enabled = self.live_trading_enabled
        if live is not None:
            live_enabled = bool(live)
            if live_enabled:
                legacy_mode = "live"
        return Account(
            id=self.id,
            exchange=self.venue or self.provider_spec or "mock",
            mode=legacy_mode,
            live_trading_enabled=live_enabled,
            initial_balance_usd=self.initial_balance_usd,
            status="active" if self.status == "active" else "disabled",
            venue=self.venue,
            kind=self.kind,
            raw=dict(self.raw),
        )

    def to_account(self) -> Account:
        """Compatibility alias for older connector call sites."""
        return self.to_connector_account()

    def asdict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "mode": self.mode,
            "venue": self.venue,
            "kind": self.kind,
            "provider_spec": self.provider_spec,
            "base_currency": self.base_currency,
            "subaccount": self.subaccount,
            "status": self.status,
            "live_trading_enabled": self.live_trading_enabled,
            "initial_balance_usd": self.initial_balance_usd,
            "permissions": self.permissions.asdict(),
            "limits": self.limits.asdict(),
            "wallet_id": self.wallet_id,
            # Credentials are vault refs only; never plaintext on the
            # wire. Strip anything that doesn't look like a ref so a
            # mistakenly-pasted secret can't escape via /accounts.
            "credentials": {
                k: v for k, v in self.credentials.items()
                if isinstance(v, str) and v.startswith("vault://")
            },
        }


def _parse_permissions(row: dict[str, Any]) -> AccountPermissions:
    raw = row.get("permissions") or {}
    if not isinstance(raw, dict):
        raw = {}
    return AccountPermissions(
        read_balances=bool(raw.get("read_balances", True)),
        place_order=bool(raw.get("place_order", False)),
        cancel_order=bool(raw.get("cancel_order", False)),
        withdraw=False,  # hard-coded — Nerya never opts in to withdraw.
    )


def _parse_limits(row: dict[str, Any]) -> AccountLimits:
    raw = row.get("limits") or {}
    if not isinstance(raw, dict):
        raw = {}
    return AccountLimits(
        max_account_nav_usd=float(raw.get("max_account_nav_usd", 0)),
        max_strategy_allocation_pct=float(raw.get("max_strategy_allocation_pct", 0)),
        max_order_notional_usd=float(raw.get("max_order_notional_usd", 0)),
        max_daily_loss_usd=float(raw.get("max_daily_loss_usd", 0)),
        max_drawdown_pct=float(raw.get("max_drawdown_pct", 0)),
        max_leverage=float(raw.get("max_leverage", 1)),
        fee_buffer_bps=float(raw.get("fee_buffer_bps", 5)),
        min_free_balance_pct=float(raw.get("min_free_balance_pct", 0)),
    )


def _parse_credentials(row: dict[str, Any]) -> dict[str, str]:
    raw = row.get("credentials") or {}
    if not isinstance(raw, dict):
        return {}
    out: dict[str, str] = {}
    for k, v in raw.items():
        if isinstance(v, str) and v.startswith("vault://"):
            out[str(k)] = v
    return out


def _normalise_mode(raw_mode: str) -> AccountMode:
    m = (raw_mode or "paper").strip().lower()
    if m not in ("paper", "shadow", "canary", "live"):
        return "paper"
    return m  # type: ignore[return-value]


def _normalise_status(raw_status: str) -> AccountStatus:
    s = (raw_status or "active").strip().lower()
    if s not in ("active", "read_only", "disabled", "quarantined"):
        return "active"
    return s  # type: ignore[return-value]


def _legacy_mode(profile_mode: AccountMode, live_enabled: bool) -> str:
    """Collapse the rich mode into the legacy paper/live binary.

    Existing risk-gate logic only knows ``paper`` vs ``live``. Shadow
    and canary should *not* hit a real ``place_order`` from the legacy
    path — they go through the new orchestrator instead — so we map
    them to ``paper`` for the legacy view. Code paths that need the
    full mode use :class:`AccountProfile` directly.
    """
    if profile_mode == "live" and live_enabled:
        return "live"
    if profile_mode == "live" and not live_enabled:
        return "paper"
    return "paper"


def load_accounts(paths: WorkspacePaths) -> dict[str, Account]:
    doc = yaml_io.load(paths.accounts_file, default={"accounts": []}) or {}
    out: dict[str, Account] = {}
    for row in doc.get("accounts", []):
        profile_mode = _normalise_mode(row.get("mode", "paper"))
        live_enabled = bool(row.get("live_trading_enabled", False))
        legacy_mode = _legacy_mode(profile_mode, live_enabled)
        acc = Account(
            id=row["id"],
            exchange=row.get("exchange", row.get("venue", "mock")),
            mode=legacy_mode,
            live_trading_enabled=live_enabled,
            initial_balance_usd=float(row.get("initial_balance_usd", 0)),
            status=_normalise_status(row.get("status", "active")),
            venue=(row.get("venue") or row.get("exchange") or "mock"),
            kind=row.get("kind", "cex"),
            raw=dict(row),
        )
        out[acc.id] = acc
    return out


def load_account_profiles(paths: WorkspacePaths) -> dict[str, AccountProfile]:
    """rich profile view of the account roster."""
    doc = yaml_io.load(paths.accounts_file, default={"accounts": []}) or {}
    out: dict[str, AccountProfile] = {}
    for row in doc.get("accounts", []):
        if not isinstance(row, dict) or "id" not in row:
            continue
        profile = AccountProfile(
            id=str(row["id"]),
            mode=_normalise_mode(row.get("mode", "paper")),
            venue=str(row.get("venue") or row.get("exchange") or "mock"),
            kind=str(row.get("kind", "cex")),
            provider_spec=str(row.get("provider_spec") or row.get("venue") or row.get("exchange") or ""),
            base_currency=str(row.get("base_currency", "USDT")),
            subaccount=str(row.get("subaccount", "")),
            status=_normalise_status(row.get("status", "active")),
            live_trading_enabled=bool(row.get("live_trading_enabled", False)),
            initial_balance_usd=float(row.get("initial_balance_usd", 0)),
            permissions=_parse_permissions(row),
            limits=_parse_limits(row),
            credentials=_parse_credentials(row),
            wallet_id=str(row.get("wallet_id") or ""),
            raw=dict(row),
        )
        out[profile.id] = profile
    return out


def get_account(paths: WorkspacePaths, account_id: str) -> Account:
    accts = load_accounts(paths)
    if account_id not in accts:
        raise TradingError(f"unknown account_id: {account_id}")
    return accts[account_id]


def get_account_profile(paths: WorkspacePaths, account_id: str) -> AccountProfile:
    """Resolve a :class:`AccountProfile`; falls back to a default
    paper-mode profile when the YAML row only carries the legacy slim
    shape, so existing fixtures keep working.
    """
    profiles = load_account_profiles(paths)
    if account_id in profiles:
        return profiles[account_id]
    accts = load_accounts(paths)
    if account_id not in accts:
        raise TradingError(f"unknown account_id: {account_id}")
    legacy = accts[account_id]
    return AccountProfile(
        id=legacy.id,
        mode="live" if legacy.is_live else ("paper" if legacy.mode == "paper" else "live"),
        venue=legacy.venue or legacy.exchange,
        kind=legacy.kind,
        provider_spec=legacy.exchange,
        base_currency="USDT",
        subaccount="",
        status="active" if legacy.status == "active" else "disabled",
        live_trading_enabled=legacy.live_trading_enabled,
        initial_balance_usd=legacy.initial_balance_usd,
        permissions=AccountPermissions(
            read_balances=True,
            place_order=legacy.is_live,
            cancel_order=legacy.is_live,
            withdraw=False,
        ),
        limits=AccountLimits(),
        credentials={},
        raw=dict(legacy.raw),
    )


def _atomic_write_accounts_doc(paths: WorkspacePaths, doc: dict[str, Any]) -> None:
    """Persist the accounts roster back to ``accounts/accounts.yml``.

    Every mutation goes through this helper so
    we (a) deduplicate writes, (b) keep a single source of truth, and
    (c) make sure the directory exists for fresh workspaces.
    """

    paths.accounts.mkdir(parents=True, exist_ok=True)
    yaml_io.dump(paths.accounts_file, doc)


def _normalise_kind(raw_kind: str) -> str:
    k = (raw_kind or "cex").strip().lower()
    if k not in ("cex", "dex", "chain", "perp", "futures"):
        return "cex"
    return k


def _coerce_credentials_row(raw: Any) -> dict[str, str]:
    """Reject anything that isn't a ``vault://`` reference.

    The HTTP surface refuses plaintext keys outright — the only
    acceptable value is a vault:// reference produced by an earlier
    ``/security/secrets/put`` call. Operators that need to introduce a
    new key first store it via the secrets vault, *then* reference it
    here.
    """

    if not isinstance(raw, dict):
        return {}
    out: dict[str, str] = {}
    for k, v in raw.items():
        if not isinstance(v, str):
            continue
        if not v.startswith("vault://"):
            raise TradingError(
                f"credentials.{k} must be a vault:// reference, refusing plaintext"
            )
        out[str(k)] = v
    return out


_DEDUP_CREDENTIAL_PRIORITY = (
    "api_key",
    "apiKey",
    "key",
    "access_key",
    "accessKey",
    "user_id",
    "userId",
    "wallet_id",
)


def _credential_fingerprint(credentials: dict[str, str]) -> str:
    """Stable fingerprint of an account's primary credential reference.

    We deliberately keep this rough — just the ``vault://`` ref of the
    first known "primary" credential field. That's enough to spot
    "you're about to register the same Binance API key twice"
    without leaking anything sensitive, and the comparison happens
    against pre-existing ``vault://`` refs only.
    """

    if not credentials:
        return ""
    for key in _DEDUP_CREDENTIAL_PRIORITY:
        value = credentials.get(key)
        if isinstance(value, str) and value.startswith("vault://"):
            return value
    for value in credentials.values():
        if isinstance(value, str) and value.startswith("vault://"):
            return value
    return ""


def find_duplicate_account(
    paths: WorkspacePaths,
    *,
    venue: str,
    kind: str,
    credentials: dict[str, str],
    ignore_id: str | None = None,
) -> AccountProfile | None:
    """Look for an existing account with the same venue + primary cred.

    Used by the HTTP upsert flow to surface a "did you mean to update
    `<existing_id>` instead of registering a new one?" hint. Returning
    a profile does **not** block the operator — the call site decides
    whether to warn or hard-fail. The fingerprint comparison only
    checks ``vault://`` references (never plaintext) so this is safe
    to expose to the dashboard.
    """

    venue_l = (venue or "").strip().lower()
    kind_l = (kind or "").strip().lower()
    fingerprint = _credential_fingerprint(credentials)
    if not venue_l or not kind_l or not fingerprint:
        return None
    for profile in load_account_profiles(paths).values():
        if ignore_id and profile.id == ignore_id:
            continue
        if (profile.venue or "").strip().lower() != venue_l:
            continue
        if (profile.kind or "").strip().lower() != kind_l:
            continue
        if _credential_fingerprint(profile.credentials) != fingerprint:
            continue
        return profile
    return None


def _connection_field_present(
    field_name: str,
    *,
    credentials: dict[str, str],
    provider_config: dict[str, Any],
    raw_account: dict[str, Any],
) -> bool:
    value = credentials.get(field_name)
    if isinstance(value, str) and value.strip():
        return True
    value = provider_config.get(field_name)
    if isinstance(value, (str, int, float)) and str(value).strip():
        return True
    value = raw_account.get(field_name)
    if isinstance(value, (str, int, float)) and str(value).strip():
        return True
    # Backward compatibility for pre-schema Hyperliquid accounts that
    # used api_key/api_secret for wallet_address/private_key.
    legacy_aliases = {
        "wallet_address": ("api_key", "address"),
        "private_key": ("api_secret", "wallet_private_key"),
    }
    for alias in legacy_aliases.get(field_name, ()):
        if _connection_field_present(
            alias,
            credentials=credentials,
            provider_config=provider_config,
            raw_account=raw_account,
        ):
            return True
    return False


def _missing_required_connection_fields(
    *,
    venue: str,
    mode: AccountMode,
    permissions: dict[str, Any],
    credentials: dict[str, str],
    provider_config: dict[str, Any],
    raw_account: dict[str, Any],
    wallet_bound_account: bool,
) -> list[str]:
    """Return provider-required fields missing from a real account row."""

    if wallet_bound_account:
        return []
    reads_real_balances = bool(permissions.get("read_balances", True))
    requires_connection = mode in ("live", "canary") or (
        mode == "shadow" and reads_real_balances
    )
    if not requires_connection:
        return []
    try:
        from ..connectors.provider_spec import get_registry

        spec = get_registry().find(venue)
    except Exception:  # pragma: no cover - provider registry is best effort here
        spec = None
    if spec is None:
        return []
    missing: list[str] = []
    for credential_field in spec.credential_fields or ():
        if not credential_field.required:
            continue
        if _connection_field_present(
            credential_field.name,
            credentials=credentials,
            provider_config=provider_config,
            raw_account=raw_account,
        ):
            continue
        missing.append(credential_field.name)
    return missing


def upsert_account(
    paths: WorkspacePaths,
    account: dict[str, Any],
    *,
    operator: str | None = None,
) -> AccountProfile:
    """Insert or update a single account row.

    The shape mirrors :class:`AccountProfile` but the YAML stays
    human-friendly: keys are flat at the row level. ``credentials``
    must be a ``vault://`` map; any plaintext is rejected.
    """

    if not isinstance(account, dict):
        raise TradingError("account payload must be a dict")
    aid = str(account.get("id") or "").strip()
    if not aid:
        raise TradingError("account.id is required")
    if not aid.replace("-", "").replace("_", "").isalnum():
        raise TradingError(f"invalid account id {aid!r}")

    mode = _normalise_mode(account.get("mode", "paper"))
    status = _normalise_status(account.get("status", "active"))
    kind = _normalise_kind(account.get("kind", "cex"))
    venue = str(account.get("venue") or account.get("exchange") or "").strip().lower()
    if not venue:
        raise TradingError("account.venue is required")
    wallet_id = str(account.get("wallet_id") or "").strip()
    if kind in ("chain", "dex") and wallet_id:
        try:
            from ..wallet.registry import resolve_provider_name

            wallet_provider = resolve_provider_name(venue)
        except Exception:  # pragma: no cover - wallet registry is optional here
            wallet_provider = None
        if wallet_provider:
            venue = wallet_provider
    live_enabled = bool(account.get("live_trading_enabled", False))

    credentials = _coerce_credentials_row(account.get("credentials"))
    wallet_bound_account = bool(wallet_id) and kind in ("chain", "dex")

    row: dict[str, Any] = {
        "id": aid,
        "venue": venue,
        "exchange": venue,
        "kind": kind,
        "mode": mode,
        "status": status,
        "base_currency": str(account.get("base_currency") or "USDT").upper(),
        "subaccount": str(account.get("subaccount") or ""),
        "live_trading_enabled": live_enabled,
        "initial_balance_usd": float(account.get("initial_balance_usd") or 0.0),
        "permissions": dict(account.get("permissions") or {}),
        "limits": dict(account.get("limits") or {}),
        "credentials": credentials,
    }
    # Non-secret per-venue connector config — host/port for IBKR, server
    # name + login for MT5, RPC URLs / chain ids for DEX, region hints
    # for ccxt, etc. Stored separately from ``credentials`` so it never
    # gets coerced through the vault-only validator. Bare scalar
    # overrides at the top level (host/port/client_id/account_id/…) are
    # also harvested here so legacy callers that don't know about
    # ``provider_config`` still produce a working row.
    provider_config: dict[str, Any] = {}
    raw_pc = account.get("provider_config")
    if isinstance(raw_pc, dict):
        for k, v in raw_pc.items():
            if isinstance(v, (str, int, float, bool)):
                provider_config[str(k)] = v
            elif isinstance(v, dict):
                provider_config[str(k)] = dict(v)
            elif isinstance(v, list):
                # Wallet bindings stash ``balances`` (an address/token
                # list) here so the snapshot loop can pull live
                # on-chain numbers. Preserve list-of-dicts shapes
                # verbatim — the snapshot helper validates each row.
                provider_config[str(k)] = [
                    dict(row) if isinstance(row, dict) else row
                    for row in v
                ]
    _PUBLIC_OVERRIDE_KEYS = (
        "host", "port", "client_id", "account_id",  # IBKR
        "server", "login", "path", "deviation",      # MT5
        "rpc_url", "chain_id", "router",             # DEX
        "ccxt_id", "category", "options",            # ccxt
        "uid", "wallet_address", "address",          # ccxt non-secret auth
        "paper",                                      # Alpaca / generic
        "base_url", "clob_url", "gamma_url", "data_url",  # REST overrides
    )
    for k in _PUBLIC_OVERRIDE_KEYS:
        if k in account and account[k] not in (None, ""):
            v = account[k]
            if isinstance(v, (str, int, float, bool, dict)):
                provider_config.setdefault(str(k), v)
    missing_connection_fields = _missing_required_connection_fields(
        venue=venue,
        mode=mode,
        permissions=row["permissions"],
        credentials=credentials,
        provider_config=provider_config,
        raw_account=account,
        wallet_bound_account=wallet_bound_account,
    )
    if missing_connection_fields:
        raise TradingError(
            f"account {aid}: {mode} account for venue {venue!r} is missing "
            "required connection fields: "
            + ", ".join(missing_connection_fields)
        )
    if provider_config:
        row["provider_config"] = provider_config
    if wallet_id:
        row["wallet_id"] = str(wallet_id)
    provider_spec = account.get("provider_spec")
    if provider_spec:
        row["provider_spec"] = str(provider_spec)
    if operator:
        row["last_modified_by"] = str(operator)

    doc = yaml_io.load(paths.accounts_file, default={"accounts": []}) or {}
    rows = list(doc.get("accounts") or [])
    replaced = False
    for idx, existing in enumerate(rows):
        if isinstance(existing, dict) and str(existing.get("id")) == aid:
            merged = dict(existing)
            merged.update(row)
            rows[idx] = merged
            replaced = True
            break
    if not replaced:
        rows.append(row)
    doc["accounts"] = rows
    _atomic_write_accounts_doc(paths, doc)
    return get_account_profile(paths, aid)


def set_account_status(
    paths: WorkspacePaths,
    account_id: str,
    *,
    status: AccountStatus,
    reason: str | None = None,
    operator: str | None = None,
) -> AccountProfile:
    """Mark an account as ``active`` / ``read_only`` / ``disabled`` /
    ``quarantined``.

    Used by the per-account kill-switch surface (P8). The risk gate
    treats any non-``active`` status as "no new trades", and
    ``quarantined`` additionally signals the operator that the account
    needs investigation before recovery.
    """

    new_status = _normalise_status(status)
    if new_status == "active":
        normalised_status: AccountStatus = "active"
    else:
        normalised_status = new_status
    doc = yaml_io.load(paths.accounts_file, default={"accounts": []}) or {}
    rows = list(doc.get("accounts") or [])
    found = False
    for row in rows:
        if isinstance(row, dict) and str(row.get("id")) == str(account_id):
            row["status"] = normalised_status
            row["last_status_reason"] = reason or row.get("last_status_reason", "")
            if operator:
                row["last_modified_by"] = str(operator)
            found = True
            break
    if not found:
        raise TradingError(f"unknown account_id: {account_id}")
    doc["accounts"] = rows
    _atomic_write_accounts_doc(paths, doc)
    return get_account_profile(paths, str(account_id))


def reset_paper_account(
    paths: WorkspacePaths,
    account_id: str,
    *,
    initial_balance_usd: float | None = None,
    operator: str | None = None,
) -> AccountProfile:
    """Wipe all paper-mode trading state for ``account_id``.

    Operators iterate fast on paper. This
    helper resets the paper sandbox without touching live or canary
    accounts:

    * Refuses anything that isn't ``mode == "paper"`` so an accidental
      click on a live row is impossible.
    * Removes the per-account virtual ledger JSON.
    * Drops the SQLite-backed runtime state limited to the account:
      orders, fills, positions, capital reservations, account
      snapshots, executor runs, protections.
    * Updates ``initial_balance_usd`` if a value is supplied.

    Strategy bindings, limits, and the YAML row itself stay
    untouched. Anything else (open executors etc.) was already
    pre-checked at the HTTP layer.
    """

    profile = get_account_profile(paths, account_id)
    if profile.mode != "paper":
        raise TradingError(
            f"reset_paper_account refused: account {account_id!r} is mode={profile.mode!r}; "
            "only paper accounts may be reset"
        )

    # 1. Virtual ledger JSON ---------------------------------------------------
    try:
        ledger_path = paths.virtual_ledgers / f"{account_id}.json"
        if ledger_path.exists():
            ledger_path.unlink()
    except Exception:
        # Ledger removal is best-effort; missing/locked files must not
        # block the rest of the reset.
        pass

    # 2. SQLite-backed runtime state ------------------------------------------
    try:
        from ..db.sqlite import connect

        con = connect(paths.db)
        try:
            con.execute("BEGIN")
            con.execute("DELETE FROM fills WHERE account_id = ?", (account_id,))
            con.execute("DELETE FROM order_events WHERE order_id IN ("
                        "SELECT order_id FROM orders WHERE account_id = ?)",
                        (account_id,))
            con.execute("DELETE FROM orders WHERE account_id = ?", (account_id,))
            con.execute(
                "DELETE FROM position_events WHERE position_id IN ("
                "SELECT position_id FROM positions WHERE account_id = ?)",
                (account_id,),
            )
            con.execute("DELETE FROM positions WHERE account_id = ?", (account_id,))
            con.execute(
                "DELETE FROM capital_reservations WHERE account_id = ?",
                (account_id,),
            )
            con.execute(
                "DELETE FROM account_snapshots WHERE account_id = ?",
                (account_id,),
            )
            con.execute(
                "DELETE FROM executor_runs WHERE account_id = ?",
                (account_id,),
            )
            con.execute("COMMIT")
        except Exception:
            con.execute("ROLLBACK")
            raise

        # Protection rules — schema may pre-date this method (older
        # workspaces still on the legacy reconciliation tables). Best
        # effort delete keeps reset usable in those cases too.
        for table in ("protection_rules", "protection_states"):
            try:
                con.execute(f"DELETE FROM {table} WHERE account_id = ?", (account_id,))
                con.execute("COMMIT")
            except Exception:
                try:
                    con.execute("ROLLBACK")
                except Exception:
                    pass
    except Exception as exc:
        raise TradingError(f"failed to reset paper sqlite state: {exc}") from exc

    # 3. Optionally bump initial balance --------------------------------------
    if initial_balance_usd is not None:
        doc = yaml_io.load(paths.accounts_file, default={"accounts": []}) or {}
        rows = list(doc.get("accounts") or [])
        for row in rows:
            if isinstance(row, dict) and str(row.get("id")) == str(account_id):
                row["initial_balance_usd"] = float(initial_balance_usd)
                if operator:
                    row["last_modified_by"] = str(operator)
                break
        doc["accounts"] = rows
        _atomic_write_accounts_doc(paths, doc)

    return get_account_profile(paths, account_id)


def delete_account(
    paths: WorkspacePaths,
    account_id: str,
    *,
    require_empty: bool = True,
) -> None:
    """Remove an account row.

    The HTTP surface defaults to ``require_empty=True`` so the operator
    can't drop an account that still has open positions or active
    executors. Strategy bindings to the deleted account remain in the
    strategy YAML; the operator must rebind those before re-enabling
    live trading.
    """

    doc = yaml_io.load(paths.accounts_file, default={"accounts": []}) or {}
    rows = [r for r in (doc.get("accounts") or []) if isinstance(r, dict)]
    new_rows = [r for r in rows if str(r.get("id")) != str(account_id)]
    if len(new_rows) == len(rows):
        raise TradingError(f"unknown account_id: {account_id}")
    doc["accounts"] = new_rows
    _atomic_write_accounts_doc(paths, doc)


__all__ = [
    "Account",
    "AccountProfile",
    "AccountPermissions",
    "AccountLimits",
    "AccountMode",
    "AccountStatus",
    "load_accounts",
    "load_account_profiles",
    "get_account",
    "get_account_profile",
    "upsert_account",
    "set_account_status",
    "delete_account",
    "reset_paper_account",
    "find_duplicate_account",
]
