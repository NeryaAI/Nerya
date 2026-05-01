"""Account snapshots — periodic NAV/balance projection per account.

04-29 §3.2 calls this out as the missing layer between the
connector layer (which has live balances on tap) and the rest of the
control plane (RiskGate, BudgetChecker, dashboard, reconciliation). A
:class:`AccountSnapshot` is the persisted "what does this account look
like right now" record. It's stored in the ``account_snapshots`` table
introduced by migration v3.

Three sources are supported:

* ``paper``  — projected from the local :class:`VirtualLedger`.
* ``live``   — fetched from the live venue via :class:`CcxtConnector`
  (or any other :class:`Connector` that implements ``get_balances``).
* ``mock``   — deterministic placeholder for unit tests.

Each :func:`capture_snapshot` call writes a row and returns the
in-memory :class:`AccountSnapshot`. ``latest_snapshot`` and
``latest_snapshots`` give the dashboard / RiskGate cheap read access
without forcing a refetch on every decision.

Health stays explicit: ``ok``, ``degraded``, ``stale``, ``auth_error``,
``rate_limited``. Anything that surfaced an exception goes to
``degraded`` with the redacted error in the meta payload — the snapshot
loop never raises into the caller because we don't want a temporary
exchange hiccup to block paper trading or RiskGate evaluation.
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import asdict, dataclass, field
from typing import Any, Literal

from ..core.config import Config
from ..core.errors import TradingError
from ..core.ids import snapshot_id as _new_snapshot_id
from ..core.paths import WorkspacePaths
from ..core.redaction import redact_dict
from ..db.sqlite import connect
from .accounts import AccountProfile, get_account_profile

log = logging.getLogger(__name__)

SnapshotSource = Literal["paper", "live", "shadow", "mock"]
SnapshotHealth = Literal["ok", "degraded", "stale", "auth_error", "rate_limited"]


# How long we treat a snapshot as fresh by default. says
# RiskGate should reject new opens against stale snapshots; the actual
# threshold per account is read from
# ``trading.snapshot.max_age_seconds`` in nerya.yml.
DEFAULT_MAX_AGE_S = 60


@dataclass
class AccountSnapshot:
    """concrete account state at a point in time."""

    snapshot_id: str
    account_id: str
    ts: float
    source: SnapshotSource
    nav_usd: float
    cash_by_asset: dict[str, float] = field(default_factory=dict)
    free_by_asset: dict[str, float] = field(default_factory=dict)
    locked_by_asset: dict[str, float] = field(default_factory=dict)
    margin_used_usd: float = 0.0
    unrealized_pnl_usd: float = 0.0
    open_order_notional_usd: float = 0.0
    health: SnapshotHealth = "ok"
    latency_ms: int = 0
    raw_ref: str | None = None
    meta: dict[str, Any] = field(default_factory=dict)

    @property
    def free_usd(self) -> float:
        """Best-effort USD free balance.

        Sums any asset that *looks like* a USD stablecoin. The list is
        kept tiny on purpose — wide multi-asset NAV conversion is
        :mod:`portfolio_risk`'s job, not the snapshot itself.
        """
        usd = 0.0
        for asset, amount in self.free_by_asset.items():
            a = (asset or "").upper()
            if a in ("USDT", "USDC", "BUSD", "FDUSD", "USD", "TUSD", "DAI"):
                usd += float(amount or 0)
        return usd

    def asdict(self) -> dict[str, Any]:
        return asdict(self)

    def is_stale(self, *, now: float | None = None, max_age_s: float = DEFAULT_MAX_AGE_S) -> bool:
        now_ts = now if now is not None else time.time()
        return (now_ts - self.ts) > max_age_s


# ---------------------------------------------------------------------------
# Persistence helpers
# ---------------------------------------------------------------------------


def _con(paths: WorkspacePaths):
    return connect(paths.db)


def _row_to_snapshot(row: dict[str, Any] | tuple) -> AccountSnapshot:
    if isinstance(row, dict) or hasattr(row, "keys"):
        get = lambda key, default=None: row[key] if key in row.keys() else default  # noqa: E731
    else:
        # tuple ordering matches SELECT order below
        keys = (
            "snapshot_id", "account_id", "ts", "source", "nav_usd",
            "cash_by_asset_json", "free_by_asset_json", "locked_by_asset_json",
            "margin_used_usd", "unrealized_pnl_usd", "open_order_notional_usd",
            "health", "latency_ms", "raw_ref",
        )
        row = dict(zip(keys, row))
        get = lambda key, default=None: row.get(key, default)  # noqa: E731

    return AccountSnapshot(
        snapshot_id=str(get("snapshot_id")),
        account_id=str(get("account_id")),
        ts=float(get("ts") or 0.0),
        source=str(get("source") or "paper"),  # type: ignore[arg-type]
        nav_usd=float(get("nav_usd") or 0.0),
        cash_by_asset=json.loads(str(get("cash_by_asset_json") or "{}")),
        free_by_asset=json.loads(str(get("free_by_asset_json") or "{}")),
        locked_by_asset=json.loads(str(get("locked_by_asset_json") or "{}")),
        margin_used_usd=float(get("margin_used_usd") or 0.0),
        unrealized_pnl_usd=float(get("unrealized_pnl_usd") or 0.0),
        open_order_notional_usd=float(get("open_order_notional_usd") or 0.0),
        health=str(get("health") or "ok"),  # type: ignore[arg-type]
        latency_ms=int(get("latency_ms") or 0),
        raw_ref=(get("raw_ref") or None),
    )


def _persist(paths: WorkspacePaths, snap: AccountSnapshot) -> None:
    con = _con(paths)
    con.execute(
        """
        INSERT INTO account_snapshots (
            snapshot_id, account_id, ts, source, nav_usd,
            cash_by_asset_json, free_by_asset_json, locked_by_asset_json,
            margin_used_usd, unrealized_pnl_usd, open_order_notional_usd,
            health, latency_ms, raw_ref
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            snap.snapshot_id,
            snap.account_id,
            snap.ts,
            snap.source,
            snap.nav_usd,
            json.dumps(snap.cash_by_asset),
            json.dumps(snap.free_by_asset),
            json.dumps(snap.locked_by_asset),
            snap.margin_used_usd,
            snap.unrealized_pnl_usd,
            snap.open_order_notional_usd,
            snap.health,
            snap.latency_ms,
            snap.raw_ref,
        ),
    )


def latest_snapshot(paths: WorkspacePaths, account_id: str) -> AccountSnapshot | None:
    con = _con(paths)
    row = con.execute(
        """
        SELECT snapshot_id, account_id, ts, source, nav_usd,
               cash_by_asset_json, free_by_asset_json, locked_by_asset_json,
               margin_used_usd, unrealized_pnl_usd, open_order_notional_usd,
               health, latency_ms, raw_ref
        FROM account_snapshots
        WHERE account_id = ?
        ORDER BY ts DESC
        LIMIT 1
        """,
        (account_id,),
    ).fetchone()
    if not row:
        return None
    return _row_to_snapshot(row)


def latest_snapshots(paths: WorkspacePaths) -> list[AccountSnapshot]:
    con = _con(paths)
    rows = con.execute(
        """
        SELECT s.snapshot_id, s.account_id, s.ts, s.source, s.nav_usd,
               s.cash_by_asset_json, s.free_by_asset_json, s.locked_by_asset_json,
               s.margin_used_usd, s.unrealized_pnl_usd, s.open_order_notional_usd,
               s.health, s.latency_ms, s.raw_ref
        FROM account_snapshots s
        JOIN (
            SELECT account_id, MAX(ts) AS max_ts
            FROM account_snapshots
            GROUP BY account_id
        ) latest
          ON latest.account_id = s.account_id
         AND latest.max_ts = s.ts
        """,
    ).fetchall()
    return [_row_to_snapshot(r) for r in rows]


# ---------------------------------------------------------------------------
# Capture path
# ---------------------------------------------------------------------------


def _paper_snapshot(profile: AccountProfile, paths: WorkspacePaths) -> AccountSnapshot:
    """Project a snapshot from the per-account paper ledger."""
    from .virtual_ledger import open_ledger

    ledger = open_ledger(paths, profile.id, profile.initial_balance_usd)
    snap = ledger.snapshot()
    cash_usd = float(snap.get("cash_usd") or 0.0)
    fees = float(snap.get("fees_paid_usd") or 0.0)
    positions = snap.get("positions") or {}
    nav = ledger.equity_estimate()

    free_by_asset = {profile.base_currency.upper(): cash_usd}
    locked_by_asset: dict[str, float] = {}
    open_notional = 0.0
    for market, pos in positions.items():
        size = float((pos or {}).get("size") or 0.0)
        avg = float((pos or {}).get("avg_price") or 0.0)
        if size and avg:
            open_notional += abs(size * avg)

    return AccountSnapshot(
        snapshot_id=_new_snapshot_id(),
        account_id=profile.id,
        ts=time.time(),
        source="paper",
        nav_usd=float(nav),
        cash_by_asset={profile.base_currency.upper(): cash_usd},
        free_by_asset=free_by_asset,
        locked_by_asset=locked_by_asset,
        margin_used_usd=0.0,
        unrealized_pnl_usd=float(nav - cash_usd) if cash_usd else 0.0,
        open_order_notional_usd=open_notional,
        health="ok",
        latency_ms=0,
        meta={"trade_count": int(snap.get("trade_count") or 0), "fees_paid_usd": fees},
    )


def _mock_snapshot(profile: AccountProfile) -> AccountSnapshot:
    return AccountSnapshot(
        snapshot_id=_new_snapshot_id(),
        account_id=profile.id,
        ts=time.time(),
        source="mock",
        nav_usd=float(profile.initial_balance_usd),
        cash_by_asset={profile.base_currency.upper(): profile.initial_balance_usd},
        free_by_asset={profile.base_currency.upper(): profile.initial_balance_usd},
        locked_by_asset={},
        health="ok",
    )


def _live_snapshot(profile: AccountProfile, config: Config) -> AccountSnapshot:
    """Pull a snapshot from the live venue.

    Health is ``ok`` only if we get a complete balance set. Any failure
    surfaces as ``degraded`` (or ``auth_error`` / ``rate_limited`` when
    we can identify the cause) so RiskGate can refuse to size new opens
    against a broken view of the world.
    """
    from ..connectors import ConnectorRegistry

    started = time.perf_counter()
    health: SnapshotHealth = "ok"
    raw_ref: str | None = None
    cash_by_asset: dict[str, float] = {}
    free_by_asset: dict[str, float] = {}
    locked_by_asset: dict[str, float] = {}
    nav_usd = 0.0
    error_meta: dict[str, Any] = {}

    try:
        registry = ConnectorRegistry(workspace=config.paths.root)
        legacy_account = profile.to_account()
        conn = registry.get(profile.id, legacy_account.connector_cfg())
        balances = conn.get_balances()
    except TradingError as exc:
        msg = str(exc).lower()
        if "auth" in msg or "key" in msg or "signature" in msg:
            health = "auth_error"
        elif "rate" in msg:
            health = "rate_limited"
        else:
            health = "degraded"
        error_meta = redact_dict({"error": str(exc)})
        balances = []
    except Exception as exc:  # pragma: no cover — defensive
        log.warning("live snapshot for %s degraded: %s", profile.id, exc)
        health = "degraded"
        error_meta = redact_dict({"error": str(exc)})
        balances = []

    base_ccy = profile.base_currency.upper()
    for bal in balances or []:
        asset = (getattr(bal, "asset", "") or "").upper()
        free = float(getattr(bal, "free", 0) or 0)
        locked = float(getattr(bal, "locked", 0) or 0)
        total = float(getattr(bal, "total", 0) or 0) or (free + locked)
        if total <= 0 and free <= 0 and locked <= 0:
            continue
        cash_by_asset[asset] = total
        free_by_asset[asset] = free
        locked_by_asset[asset] = locked
        # Stablecoins contribute to NAV at par; non-stable assets stay
        # in the asset map and the portfolio-risk layer is responsible
        # for marking them.
        if asset in ("USDT", "USDC", "BUSD", "FDUSD", "USD", "TUSD", "DAI") or asset == base_ccy:
            nav_usd += total

    latency = int((time.perf_counter() - started) * 1000)
    snap = AccountSnapshot(
        snapshot_id=_new_snapshot_id(),
        account_id=profile.id,
        ts=time.time(),
        source="live" if profile.mode != "shadow" else "shadow",
        nav_usd=nav_usd,
        cash_by_asset=cash_by_asset,
        free_by_asset=free_by_asset,
        locked_by_asset=locked_by_asset,
        margin_used_usd=0.0,
        unrealized_pnl_usd=0.0,
        open_order_notional_usd=0.0,
        health=health,
        latency_ms=latency,
        raw_ref=raw_ref,
        meta=error_meta,
    )
    return snap


def _wallet_snapshot(profile: AccountProfile, config: Config) -> AccountSnapshot:
    """Pull a snapshot from an installed on-chain wallet provider.

    04-29 §11 P10 — once an operator (or the agent) installs
    a wallet provider via ``/wallet/install`` and binds it to an
    account by setting ``wallet_id`` plus ``provider_config.balances``
    (a list of ``{chain, address, token, symbol?, decimals?}`` rows),
    the snapshot loop should surface those balances the same way it
    surfaces CEX balances. The wallet's ``readiness()`` gate prevents
    us from hitting a misconfigured provider — if it's not ready we
    drop a ``degraded`` snapshot so the dashboard can flag the issue.
    """

    started = time.perf_counter()
    health: SnapshotHealth = "ok"
    cash_by_asset: dict[str, float] = {}
    free_by_asset: dict[str, float] = {}
    nav_usd = 0.0
    error_meta: dict[str, Any] = {}

    wallet_id = (profile.wallet_id or "").strip().lower()
    provider_cfg = (profile.raw or {}).get("provider_config") or {}
    balance_specs = (
        provider_cfg.get("balances")
        or provider_cfg.get("addresses")
        or []
    )
    if isinstance(balance_specs, dict):
        balance_specs = [balance_specs]
    if not isinstance(balance_specs, list):
        balance_specs = []

    if not wallet_id:
        return AccountSnapshot(
            snapshot_id=_new_snapshot_id(),
            account_id=profile.id,
            ts=time.time(),
            source="live" if profile.mode != "shadow" else "shadow",
            nav_usd=0.0,
            health="degraded",
            meta={"reason": "no_wallet_id"},
        )

    try:
        from ..wallet import build_provider

        wallet_root = (config.data.get("wallet") or {})
        wallet_cfg = dict(wallet_root.get(wallet_id) or {})
        provider = build_provider(
            wallet_id, wallet_cfg, workspace=config.paths.root,
        )
        readiness = provider.readiness()
        if not getattr(readiness, "ready", True):
            health = "degraded"
            error_meta = {
                "reason": "wallet_not_ready",
                "missing": list(getattr(readiness, "missing", []) or []),
                "install_hint": getattr(readiness, "install_hint", "") or "",
            }
        else:
            # If the provider exposes a native portfolio aggregator
            # (e.g. okx_os has a wallet/all-token-balances endpoint),
            # prefer it — it handles per-chain prices, multi-token
            # discovery and stablecoin USD conversion better than our
            # per-row fallback. Providers without it fall back to the
            # explicit ``balances`` map on the account row.
            native_balances: list[Any] = []
            list_fn = getattr(provider, "list_balances", None)
            if callable(list_fn):
                try:
                    native_balances = list(list_fn(specs=balance_specs) or [])
                except TypeError:
                    # Older signature without ``specs`` kwarg.
                    try:
                        native_balances = list(list_fn() or [])
                    except Exception as exc:  # pragma: no cover
                        log.debug("native list_balances failed for %s: %s",
                                  wallet_id, exc)
                        native_balances = []
                except Exception as exc:  # pragma: no cover
                    log.debug("native list_balances failed for %s: %s",
                              wallet_id, exc)
                    native_balances = []

            if native_balances:
                # Each row may be a WalletBalance or a dict; normalise.
                for bal in native_balances:
                    if hasattr(bal, "to_dict"):
                        row = bal.to_dict()
                    elif isinstance(bal, dict):
                        row = bal
                    else:
                        continue
                    symbol = str(row.get("symbol") or "").upper() \
                        or str(row.get("token") or "").upper()
                    amount = float(row.get("balance") or 0.0)
                    if not symbol or amount <= 0:
                        continue
                    cash_by_asset[symbol] = cash_by_asset.get(symbol, 0.0) + amount
                    free_by_asset[symbol] = free_by_asset.get(symbol, 0.0) + amount
                    if symbol in ("USDT", "USDC", "BUSD", "FDUSD",
                                  "USD", "TUSD", "DAI"):
                        nav_usd += amount
            else:
                # Fallback: walk the operator-supplied address map
                # one row at a time.
                for spec in balance_specs:
                    if not isinstance(spec, dict):
                        continue
                    chain = str(spec.get("chain") or "ethereum")
                    address = str(spec.get("address") or "").strip()
                    token = str(spec.get("token") or "").strip()
                    symbol = (
                        str(spec.get("symbol") or "").upper()
                        or token.upper()
                    )
                    if not address:
                        continue
                    try:
                        bal = provider.get_balance(
                            chain=chain, address=address, token=token,
                        )
                    except Exception as exc:  # pragma: no cover
                        error_meta.setdefault("errors", []).append(redact_dict({
                            "chain": chain, "token": token, "error": str(exc),
                        }))
                        health = "degraded"
                        continue
                    amount = float(getattr(bal, "balance", 0.0) or 0.0)
                    key = symbol or chain.upper()
                    cash_by_asset[key] = cash_by_asset.get(key, 0.0) + amount
                    free_by_asset[key] = free_by_asset.get(key, 0.0) + amount
                    # Stablecoins / base ccy contribute to NAV at par;
                    # the portfolio risk layer is responsible for
                    # marking the rest. Mirrors ``_live_snapshot``.
                    if symbol in ("USDT", "USDC", "BUSD", "FDUSD",
                                  "USD", "TUSD", "DAI"):
                        nav_usd += amount
    except Exception as exc:  # pragma: no cover — defensive
        log.warning("wallet snapshot for %s degraded: %s", profile.id, exc)
        health = "degraded"
        error_meta = redact_dict({"error": str(exc)})

    latency = int((time.perf_counter() - started) * 1000)
    return AccountSnapshot(
        snapshot_id=_new_snapshot_id(),
        account_id=profile.id,
        ts=time.time(),
        source="live" if profile.mode != "shadow" else "shadow",
        nav_usd=nav_usd,
        cash_by_asset=cash_by_asset,
        free_by_asset=free_by_asset,
        locked_by_asset={},
        margin_used_usd=0.0,
        unrealized_pnl_usd=0.0,
        open_order_notional_usd=0.0,
        health=health,
        latency_ms=latency,
        meta={"wallet_id": wallet_id, **error_meta},
    )


def capture_snapshot(
    config: Config,
    account_id: str,
    *,
    profile: AccountProfile | None = None,
    persist: bool = True,
) -> AccountSnapshot:
    """Capture a fresh snapshot for ``account_id``.

    The capture path is decided from the resolved
    :class:`AccountProfile`:

    * ``mode=paper`` -> project from :class:`VirtualLedger`.
    * ``mode in {live, canary, shadow}`` and ``kind == "chain"`` (or
      ``wallet_id`` set) -> read the on-chain wallet provider.
    * ``mode in {live, canary, shadow}`` and ``permissions.read_balances``
      -> hit the live venue.
    * Anything else -> deterministic mock snapshot.

    Failures from the live path are *not* raised — they downgrade the
    snapshot's ``health`` so the risk gate can still see something and
    block new opens.
    """
    profile = profile or get_account_profile(config.paths, account_id)

    if profile.mode == "paper":
        snap = _paper_snapshot(profile, config.paths)
    elif profile.wallet_id and profile.kind in ("chain", "dex"):
        snap = _wallet_snapshot(profile, config)
    elif profile.reads_real_balances:
        snap = _live_snapshot(profile, config)
    else:
        snap = _mock_snapshot(profile)

    if persist:
        try:
            _persist(config.paths, snap)
        except Exception:  # pragma: no cover
            log.exception("failed to persist account snapshot")
    return snap


def capture_all(config: Config) -> list[AccountSnapshot]:
    """Refresh snapshots for every account in the registry."""
    from .accounts import load_account_profiles

    out: list[AccountSnapshot] = []
    for profile in load_account_profiles(config.paths).values():
        try:
            out.append(capture_snapshot(config, profile.id, profile=profile))
        except Exception:  # pragma: no cover
            log.exception("failed to capture snapshot for %s", profile.id)
    return out


def fresh_snapshot(
    config: Config,
    account_id: str,
    *,
    max_age_s: float | None = None,
    profile: AccountProfile | None = None,
) -> AccountSnapshot:
    """Return a snapshot that is at most ``max_age_s`` seconds old.

    Reads the latest persisted snapshot; if it's stale (or missing) we
    capture a new one. Use this from RiskGate / BudgetChecker so a
    single decision sees a coherent view without hitting the venue on
    every call.
    """
    threshold = float(
        max_age_s if max_age_s is not None else config.get(
            "trading.snapshot.max_age_seconds", DEFAULT_MAX_AGE_S
        )
    )
    existing = latest_snapshot(config.paths, account_id)
    if existing and not existing.is_stale(max_age_s=threshold):
        return existing
    return capture_snapshot(config, account_id, profile=profile)


def free_usd_for_account(snap: AccountSnapshot, base_currency: str = "USDT") -> float:
    """Convenience helper used by BudgetChecker."""
    base = (base_currency or "USDT").upper()
    if base in snap.free_by_asset:
        return float(snap.free_by_asset[base])
    return snap.free_usd


__all__ = [
    "AccountSnapshot",
    "SnapshotSource",
    "SnapshotHealth",
    "DEFAULT_MAX_AGE_S",
    "capture_snapshot",
    "capture_all",
    "latest_snapshot",
    "latest_snapshots",
    "fresh_snapshot",
    "free_usd_for_account",
]
