"""Account snapshots — periodic NAV/balance projection per account.

This is the missing layer between the
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
_USD_STABLES = {"USDT", "USDC", "BUSD", "FDUSD", "USD", "TUSD", "DAI"}


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

    @property
    def total_usd(self) -> float:
        return float(self.nav_usd)

    @property
    def equity_usd(self) -> float:
        return float(self.nav_usd)

    @property
    def available_usd(self) -> float:
        return float(self.free_usd)

    @property
    def positions_value_usd(self) -> float:
        return float(self.open_order_notional_usd)

    def asdict(self) -> dict[str, Any]:
        data = asdict(self)
        data.update({
            "total_usd": self.total_usd,
            "equity_usd": self.equity_usd,
            "free_usd": self.free_usd,
            "available_usd": self.available_usd,
            "positions_value_usd": self.positions_value_usd,
        })
        return data

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


def equity_curve(
    paths: WorkspacePaths,
    account_id: str,
    *,
    since_ts: float | None = None,
    limit: int = 500,
    bucket_seconds: float | None = None,
) -> list[dict[str, Any]]:
    """Return the per-account NAV curve from persisted snapshots.

    Each point carries ``ts``, ``nav_usd``, ``unrealized_pnl_usd``,
    ``source`` and ``health`` so the dashboard can render both the
    equity line and per-point health indicators (so a stretch of
    ``degraded`` snapshots stands out instead of silently mixing in).

    ``bucket_seconds`` optionally downsamples by keeping the latest
    point inside each bucket — useful for long live histories where
    we'd otherwise return thousands of rows.
    """

    if not account_id:
        return []
    limit = max(1, min(int(limit or 500), 5000))
    con = _con(paths)
    params: list[Any] = [account_id]
    where = "WHERE account_id = ?"
    if since_ts is not None:
        where += " AND ts >= ?"
        params.append(float(since_ts))
    rows = con.execute(
        f"""
        SELECT ts, nav_usd, unrealized_pnl_usd, open_order_notional_usd,
               source, health
        FROM account_snapshots
        {where}
        ORDER BY ts ASC
        """,
        params,
    ).fetchall()
    bucket = float(bucket_seconds or 0.0)
    points: list[dict[str, Any]] = []
    last_bucket_key: int | None = None
    for row in rows:
        ts = float(row["ts"] if isinstance(row, dict) or hasattr(row, "keys") else row[0])
        nav = float(row["nav_usd"] if isinstance(row, dict) or hasattr(row, "keys") else row[1])
        unr = float(row["unrealized_pnl_usd"] if isinstance(row, dict) or hasattr(row, "keys") else row[2])
        notional = float(row["open_order_notional_usd"] if isinstance(row, dict) or hasattr(row, "keys") else row[3])
        source = str(row["source"] if isinstance(row, dict) or hasattr(row, "keys") else row[4])
        health = str(row["health"] if isinstance(row, dict) or hasattr(row, "keys") else row[5])
        point = {
            "ts": ts,
            "nav_usd": nav,
            "unrealized_pnl_usd": unr,
            "open_order_notional_usd": notional,
            "source": source,
            "health": health,
        }
        if bucket > 0:
            key = int(ts // bucket)
            if last_bucket_key == key and points:
                points[-1] = point
                continue
            last_bucket_key = key
        points.append(point)
    if len(points) > limit:
        # Even after bucketing the operator can pin a hard limit. Trim
        # from the head so the chart always shows the most recent
        # window.
        points = points[-limit:]
    return points


def _position_book_marks(paths: WorkspacePaths, account_id: str) -> dict[str, float]:
    """Return latest persisted marks for this account's open positions."""

    try:
        from .position_book import PositionBook

        marks: dict[str, float] = {}
        for pos in PositionBook(paths).open_positions(account_id=account_id):
            mark = float(pos.mark_price or 0.0)
            if mark > 0:
                marks[pos.market] = mark
        return marks
    except Exception:  # pragma: no cover - read-model best effort
        log.debug("position mark lookup failed for account %s", account_id, exc_info=True)
        return {}


def _paper_position_totals(
    positions: dict[str, Any],
    marks: dict[str, float],
) -> tuple[float, float]:
    market_value = 0.0
    unrealized = 0.0
    for market, pos in positions.items():
        size = float((pos or {}).get("size") or 0.0)
        avg = float((pos or {}).get("avg_price") or 0.0)
        mark = float(marks.get(market, avg) or avg or 0.0)
        if size and mark:
            market_value += abs(size * mark)
        if size and avg and mark:
            unrealized += (mark - avg) * size
    return market_value, unrealized


def _position_book_open(paths: WorkspacePaths, account_id: str):
    try:
        from .position_book import PositionBook

        return PositionBook(paths).open_positions(account_id=account_id)
    except Exception:  # pragma: no cover - read-model best effort
        log.debug("position book lookup failed for account %s", account_id, exc_info=True)
        return []


def _paper_book_position_totals(
    positions: list[Any],
    marks: dict[str, float],
    *,
    shares_by_position: dict[str, list[Any]] | None = None,
) -> tuple[float, float, float]:
    """Sum NAV-relevant signals over a list of merged ``Position`` rows.

    Returns ``(signed_value, gross_value, unrealized)``.

    * ``signed_value`` is the merged signed market value — feeds NAV.
    * ``gross_value`` is **share-level** gross exposure when shares are
      supplied: each strategy could independently be told to flatten,
      so risk wants the sum of |share_size * mark|, not |merged_size *
      mark|. Without shares (legacy callers) we fall back to merged
      gross.
    * ``unrealized`` derives from the merged size/avg/mark and so is
      identical to the pre-v6 number for single-strategy positions.
    """
    signed_value = 0.0
    gross_value = 0.0
    unrealized = 0.0
    for pos in positions:
        size = float(getattr(pos, "size_base", 0.0) or 0.0)
        avg = float(getattr(pos, "avg_entry_price", 0.0) or 0.0)
        market = str(getattr(pos, "market", "") or "")
        position_id = str(getattr(pos, "position_id", "") or "")
        mark = float(
            marks.get(market)
            or getattr(pos, "mark_price", None)
            or avg
            or 0.0
        )
        if size and mark:
            signed_value += size * mark
        if shares_by_position is not None and position_id in shares_by_position:
            for sh in shares_by_position[position_id]:
                sh_size = float(getattr(sh, "size_share_base", 0.0) or 0.0)
                if sh_size and mark:
                    gross_value += abs(sh_size * mark)
        elif size and mark:
            gross_value += abs(size * mark)
        if size and avg and mark:
            side = str(getattr(pos, "side", "") or "").lower()
            side_factor = -1.0 if side == "short" or size < 0 else 1.0
            unrealized += (mark - avg) * abs(size) * side_factor
        else:
            unrealized += float(getattr(pos, "unrealized_pnl_usd", 0.0) or 0.0)
    return signed_value, gross_value, unrealized


def _asset_usd_price(asset: str, *, profile: AccountProfile, config: Config) -> float | None:
    """Best-effort public USD mark for non-stable balances."""

    symbol = (asset or "").upper().strip()
    if not symbol:
        return None
    if symbol in _USD_STABLES:
        return 1.0
    base = (profile.base_currency or "USDT").upper()
    quote_candidates = []
    for quote in (base, "USDT", "USDC", "USD"):
        if quote and quote not in quote_candidates and quote != symbol and quote in _USD_STABLES:
            quote_candidates.append(quote)
    venue = (profile.venue or profile.provider_spec or "").strip()
    try:
        from ..data.candles import fetch_public_ticker

        for quote in quote_candidates:
            market = f"{symbol}{quote}"
            market_id = f"{venue}:{market}" if venue else market
            snap = fetch_public_ticker(market_id, allow_mock=False, config_like=config)
            if float(snap.get("price") or 0.0) > 0:
                env = snap.get("_envelope") or {}
                if env.get("mode") == "live":
                    return float(snap["price"])
    except Exception:  # pragma: no cover - external market-data best effort
        log.debug("asset USD mark failed for %s", symbol, exc_info=True)
    return None


# ---------------------------------------------------------------------------
# Capture path
# ---------------------------------------------------------------------------


def _paper_snapshot(
    profile: AccountProfile,
    paths: WorkspacePaths,
    *,
    marks: dict[str, float] | None = None,
) -> AccountSnapshot:
    """Project a snapshot from the per-account paper ledger."""
    from .virtual_ledger import open_ledger

    ledger = open_ledger(paths, profile.id, profile.initial_balance_usd)
    snap = ledger.snapshot()
    cash_usd = float(snap.get("cash_usd") or 0.0)
    fees = float(snap.get("fees_paid_usd") or 0.0)
    positions = snap.get("positions") or {}
    effective_marks = dict(_position_book_marks(paths, profile.id))
    for key, value in (marks or {}).items():
        try:
            mark = float(value or 0.0)
        except (TypeError, ValueError):
            continue
        if mark > 0:
            effective_marks[str(key)] = mark
    book_positions = _position_book_open(paths, profile.id)
    if book_positions:
        book_markets = {str(pos.market) for pos in book_positions}
        shares_by_position: dict[str, list[Any]] = {}
        try:
            from .position_book import PositionBook

            book = PositionBook(paths)
            for pos in book_positions:
                shares_by_position[pos.position_id] = book.list_shares(pos.position_id)
        except Exception:  # pragma: no cover — defensive against schema drift
            shares_by_position = {}
        signed_value, open_notional, unrealized = _paper_book_position_totals(
            book_positions, effective_marks,
            shares_by_position=shares_by_position,
        )
        legacy_only = {
            market: pos
            for market, pos in positions.items()
            if str(market) not in book_markets
        }
        legacy_notional, legacy_unrealized = _paper_position_totals(
            legacy_only, effective_marks
        )
        nav = cash_usd + signed_value
        for market, pos in legacy_only.items():
            size = float((pos or {}).get("size") or 0.0)
            avg = float((pos or {}).get("avg_price") or 0.0)
            mark = float(effective_marks.get(market, avg) or avg or 0.0)
            nav += size * mark
        open_notional += legacy_notional
        unrealized += legacy_unrealized
    else:
        nav = ledger.equity_estimate(effective_marks)
        open_notional, unrealized = _paper_position_totals(positions, effective_marks)

    free_by_asset = {profile.base_currency.upper(): cash_usd}
    locked_by_asset: dict[str, float] = {}

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
        unrealized_pnl_usd=float(unrealized),
        open_order_notional_usd=open_notional,
        health="ok",
        latency_ms=0,
        meta={
            "trade_count": int(snap.get("trade_count") or 0),
            "fees_paid_usd": fees,
            "marks": effective_marks,
        },
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
        connector_cfg = legacy_account.connector_cfg()
        # Balance reads are private API calls but not trading actions.
        # Keep AccountProfile.to_account() conservative for the order
        # path, and opt into a live connector only inside this
        # snapshot-read context.
        connector_cfg["live"] = True
        conn = registry.get(profile.id, connector_cfg)
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
        # Stablecoins contribute at par. Non-stable balances are marked
        # through public market data when possible so live snapshots do
        # not understate total account value after spot fills.
        if asset in _USD_STABLES or (asset == base_ccy and base_ccy in _USD_STABLES):
            nav_usd += total
        else:
            price = _asset_usd_price(asset, profile=profile, config=config)
            if price is not None:
                nav_usd += total * price

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

    Once an operator (or the agent) installs
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
        from ..wallet import resolve_for_account

        resolved_wallet_id, provider, source = resolve_for_account(
            config.data,
            profile.id,
            wallet_id,
            workspace=config.paths.root,
        )
        if provider is None:
            raise RuntimeError(
                f"wallet binding {wallet_id!r} did not resolve"
            )
        readiness = provider.readiness()
        if not getattr(readiness, "ready", True):
            health = "degraded"
            error_meta = {
                "reason": "wallet_not_ready",
                "wallet_id": resolved_wallet_id,
                "wallet_source": source,
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
                    if symbol in _USD_STABLES:
                        nav_usd += amount
                    else:
                        price = _asset_usd_price(symbol, profile=profile, config=config)
                        if price is not None:
                            nav_usd += amount * price
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
                    if symbol in _USD_STABLES:
                        nav_usd += amount
                    else:
                        price = _asset_usd_price(symbol, profile=profile, config=config)
                        if price is not None:
                            nav_usd += amount * price
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
    marks: dict[str, float] | None = None,
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
        snap = _paper_snapshot(profile, config.paths, marks=marks)
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
        # Auto-ingest the snapshot as an Evidence Vault row so the operator
        # always has a citeable audit trail for NAV/equity history. Honors
        # ``runtime.evidence_vault`` and never raises.
        try:
            from ..evidence import autoingest as _evidence_autoingest
            import json as _json

            class _ConfigClient:
                __slots__ = ("config",)

                def __init__(self, cfg) -> None:
                    self.config = cfg

            _evidence_autoingest.on_account_snapshot(
                _ConfigClient(config),
                account_id=str(snap.account_id),
                snapshot_id=str(snap.snapshot_id),
                body=_json.dumps(snap.asdict(), default=str, ensure_ascii=False)[:8000],
            )
        except Exception:  # pragma: no cover - defensive
            pass
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
    "equity_curve",
    "fresh_snapshot",
    "free_usd_for_account",
]
