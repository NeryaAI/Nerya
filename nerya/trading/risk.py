"""Risk Gate — pure function of (intent, strategy, account, market snapshot, ledger).

Every decision now carries a stable
``risk_evaluation_id`` (matching the new ``risk_evaluations`` table)
so reservations, executors, and journal entries can pin themselves to
exactly which decision allowed them.
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import asdict, dataclass, field
from typing import Any, Literal

from ..core.config import Config
from ..core.ids import risk_evaluation_id as _new_risk_evaluation_id
from ..core.time import now_iso
from ..db import DedupeRepository
from ..db.sqlite import connect
from .account_snapshots import fresh_snapshot
from .accounts import Account, get_account_profile, load_accounts
from .capital import CapitalReservationStore
from .intents import TradeIntent
from .position_book import PositionBook
from .reconciliation import ReconciliationStore
from .risk_hints import derive_fix_hints
from .strategies import Strategy, load_strategy
from .virtual_ledger import open_ledger

log = logging.getLogger(__name__)


# USD-stable quote assets. A ``size_unit="quote"`` intent is only a USD
# notional when the market's quote asset is one of these (C9/D2).
USD_STABLE_QUOTES: tuple[str, ...] = ("USDT", "USDC", "BUSD", "TUSD", "DAI", "USD")

# Venue-family suffixes stripped when comparing a market's venue prefix
# against an account's venue (C7): ``bybit_perpetual`` ≡ ``bybit``.
_VENUE_FAMILY_SUFFIXES = (
    "_perpetual", "_futures", "_coinm", "_linear", "_swap", "_perp", "_spot",
)


def market_quote_asset(market: str) -> str:
    """Return the quote asset of ``'``<venue>:``<symbol>'`` (upper-cased).

    Handles both ``mock:BTC/USDT`` and compact ``BINANCE:BTCUSDT`` shapes
    (plus a perp settlement suffix like ``SOL/USDT:USDT``). Returns ``""``
    when the quote asset cannot be determined from the string alone.
    """
    raw = str(market or "").strip()
    symbol = raw.split(":", 1)[1] if ":" in raw else raw
    # Drop a perp settlement suffix ("SOL/USDT:USDT" -> "SOL/USDT").
    symbol = symbol.split(":", 1)[0].strip().upper()
    if "/" in symbol:
        return symbol.rsplit("/", 1)[-1].strip()
    # Longest-first so "USDT" wins over its "USD" suffix.
    for quote in sorted(USD_STABLE_QUOTES, key=len, reverse=True):
        if symbol.endswith(quote):
            return quote
    return ""


def is_usd_stable_quote(market: str) -> bool:
    """True iff ``market``'s quote asset is a USD stablecoin."""
    return market_quote_asset(market) in USD_STABLE_QUOTES


def _venue_family(venue: str) -> str:
    """Canonicalize a venue token for mismatch comparison."""
    raw = str(venue or "").strip()
    if not raw:
        return ""
    try:
        from ..data.candles import canonical_venue

        raw = canonical_venue(raw)
    except Exception:
        pass
    family = raw.lower()
    for suffix in _VENUE_FAMILY_SUFFIXES:
        if family.endswith(suffix):
            return family[: -len(suffix)]
    return family


def _venues_compatible(market: str, account_venue: str) -> bool:
    """Whether a market's venue prefix may execute on ``account_venue``.

    The connector strips everything before ':' from the market string, so
    the prefix is a promise about *which venue* will see the order. Two
    tokens are compatible when they normalize to the same venue family
    or resolve to the same provider spec (``paper`` ≡ ``mock`` via the
    registry, ``BYBIT_PERP`` ≡ ``bybit`` via family stripping). Unknown
    or empty venues produce no mismatch evidence.
    """
    raw = str(market or "").strip()
    market_venue = ""
    if ":" in raw:
        head, _tail = raw.split(":", 1)
        # "SOL/USDT:USDT" is a unified contract symbol with NO venue
        # prefix — the text before the first ":" is the base asset, not
        # a venue promise (same split rule as the connector's
        # _normalise_symbol). Only a slash-free head is a venue prefix.
        if "/" not in head:
            market_venue = head
    a = _venue_family(market_venue)
    b = _venue_family(account_venue)
    if not a or not b:
        return True
    if a == b:
        return True
    try:
        from ..connectors.provider_spec import get_registry

        registry = get_registry()
        spec_a = registry.find(a)
        spec_b = registry.find(b)
        if spec_a is not None and spec_b is not None and spec_a.id == spec_b.id:
            return True
    except Exception:
        pass
    return False


# Instrument-type tokens a provider spec can advertise (R2C3) that mean
# its connectors can trade contract-suffixed symbols.
_DERIVATIVE_INSTRUMENT_TYPES = frozenset({
    "perpetual", "perp", "swap", "future", "futures",
    "inverse", "linear", "derivatives",
})

# R2C3 fallback: provider specs that publish no ``instrument_types``
# metadata but whose builtin factory is unambiguously spot-only — an
# explicit contract symbol ("SOL/USDT:USDT") forwarded verbatim fails at
# the venue with a late, confusing BadSymbol. Unified venues whose spot
# spec genuinely loads + routes contract markets (bybit, okx, gate,
# hyperliquid, …) are deliberately absent: they pass here, which is a
# documented false-negative, not a safety hole — the venue still fails
# closed on any symbol it cannot trade.
_KNOWN_SPOT_ONLY_PROVIDER_IDS = frozenset({
    "binance",           # derivatives live on binance_perpetual(_coinm)
    "ccxt",              # unified default factory → binance spot connector
    "kraken",            # futures are a separate ccxt class (krakenfutures)
    "kucoin",            # futures live on kucoin_perpetual (kucoinfutures)
    "coinbase",
    "coinbase_exchange",
    "bitstamp",
    "bitmart",
    "btcmarkets",
    "bitrue",
    "ndax",
    "backpack",
})


def _market_has_contract_suffix(market: str) -> bool:
    """True when the market carries an explicit unified contract suffix
    — a ":" after the ``BASE/QUOTE`` part, e.g. ``SOL/USDT:USDT`` or
    ``BYBIT:SOL/USDT:USDT``."""
    raw = str(market or "").strip()
    if not raw:
        return False
    symbol = raw
    if ":" in raw:
        head, tail = raw.split(":", 1)
        # A venue prefix ("BYBIT:SOLUSDT") sits before the symbol and
        # never contains a slash — same split rule as _venues_compatible.
        if "/" not in head:
            symbol = tail
    return "/" in symbol and ":" in symbol


def _venue_supports_derivatives(account_venue: str) -> bool:
    """Whether ``account_venue`` can trade contract-suffixed symbols.

    Resolution order (R2C3):

    1. Empty / mock venues pass (same exemption as the mismatch gate).
    2. Registry spec with ``instrument_types`` metadata: any derivative
       token → True; metadata without one → False.
    3. No metadata: pass unless the spec id is in the known-spot-only
       set. LIMITATION: this is a static allowlist, not a markets
       snapshot — a user-authored spot-only provider without
       ``instrument_types`` passes here and only fails at the venue.
    4. Unknown venue (not in the registry, not mock) → False: the
       connector would forward the symbol verbatim and the venue would
       reject it late, so we fail closed at the gate instead.
    """
    raw = str(account_venue or "").strip()
    if not raw or _venue_family(raw) == "mock":
        return True
    try:
        from ..connectors.provider_spec import get_registry

        spec = get_registry().find(raw)
    except Exception:
        spec = None
    if spec is None:
        return False
    instrument_types = {
        str(t).strip().lower() for t in (spec.instrument_types or ())
    }
    if instrument_types:
        return bool(instrument_types & _DERIVATIVE_INSTRUMENT_TYPES)
    return spec.id not in _KNOWN_SPOT_ONLY_PROVIDER_IDS


@dataclass
class RiskDecision:
    intent_id: str
    decision: Literal["allow", "reject", "escalate"]
    reasons: list[str]
    limits_snapshot: dict[str, Any] = field(default_factory=dict)
    virtual_ledger_snapshot: dict[str, Any] = field(default_factory=dict)
    estimated_notional_usd: float = 0.0
    risk_evaluation_id: str = ""
    account_snapshot: dict[str, Any] = field(default_factory=dict)
    reservation_blocked_usd: float = 0.0
    ts: str = ""
    # promotion-state-aware flags consumed by submit.py.
    # ``shadow_only=True`` means the gate accepted the intent but the
    # submit pipeline must skip the executor (intent is journalled
    # against a real-money account snapshot, never sent to the venue).
    shadow_only: bool = False
    promotion_state: str = ""
    # operator-facing remediation hints. Each entry maps
    # one of the strings in ``reasons`` to a human-readable fix and a
    # deep-link target the dashboard can render as a button.
    fix_hints: list[dict[str, Any]] = field(default_factory=list)

    def __post_init__(self):
        if not self.ts:
            self.ts = now_iso()
        if not self.risk_evaluation_id:
            self.risk_evaluation_id = _new_risk_evaluation_id()
        # Always recompute hints from reasons so callers that mutate
        # ``reasons`` post-construction still see a consistent surface.
        if self.reasons and not self.fix_hints:
            self.fix_hints = derive_fix_hints(self.reasons)

    def asdict(self) -> dict[str, Any]:
        return asdict(self)


class RiskGate:
    def __init__(self, config: Config):
        self.config = config
        self._con = None

    def _con_lazy(self):
        if self._con is None:
            self._con = connect(self.config.paths.db)
        return self._con

    def evaluate(
        self,
        intent: TradeIntent,
        *,
        market_snapshot: dict[str, Any] | None = None,
        resume: bool = False,
    ) -> RiskDecision:
        paths = self.config.paths
        reasons: list[str] = []
        decision: str = "allow"

        # 1. Kill switch
        if self.config.kill_switch():
            reasons.append("kill_switch_enabled")
            decision = "reject"

        # load strategy + account
        try:
            strategy: Strategy = load_strategy(paths, intent.strategy_id)
        except Exception as exc:
            reasons.append(f"strategy_unknown:{exc}")
            return RiskDecision(
                intent.intent_id,
                "reject",
                reasons,
                estimated_notional_usd=intent.notional_usd_estimate,
                fix_hints=derive_fix_hints(reasons, intent=intent),
            )
        accounts = load_accounts(paths)
        if intent.account_id not in accounts:
            reasons.append("account_unknown")
            return RiskDecision(
                intent.intent_id,
                "reject",
                reasons,
                estimated_notional_usd=intent.notional_usd_estimate,
                fix_hints=derive_fix_hints(reasons, intent=intent),
            )
        account: Account = accounts[intent.account_id]
        try:
            account_profile = get_account_profile(paths, intent.account_id)
        except Exception as exc:
            reasons.append(f"account_profile_unavailable:{exc}")
            return RiskDecision(
                intent.intent_id,
                "reject",
                reasons,
                estimated_notional_usd=intent.notional_usd_estimate,
                fix_hints=derive_fix_hints(reasons, intent=intent),
            )

        # 1b. Strategy ↔ account binding integrity.
        #
        # Risk evaluates against `intent.account_id` (snapshots, PositionBook,
        # ledger, reservations). If a caller routes an intent at a *different*
        # account than the one the strategy is bound to in `strategy.yml`,
        # every downstream check silently moves to the wrong book and the
        # strategy can effectively trade on an account it was never approved
        # for. Reject early and short-circuit so we never touch the wrong
        # snapshot/ledger.
        bound_account_id = (strategy.account_id or "").strip()
        if bound_account_id and bound_account_id != intent.account_id:
            reasons.append(
                f"account_binding_mismatch:strategy={bound_account_id}!=intent={intent.account_id}"
            )
            return RiskDecision(
                intent.intent_id,
                "reject",
                reasons,
                limits_snapshot=asdict(strategy.limits),
                estimated_notional_usd=intent.notional_usd_estimate,
                fix_hints=derive_fix_hints(reasons, intent=intent),
            )

        # 1c. Market venue ↔ account venue consistency.
        #
        # The connector strips everything before ':' from a market
        # string, so an intent for ``BINANCE:DOGEUSDT`` or
        # ``PAPER:BTCUSDT`` would silently execute on a Bybit account —
        # every cap, snapshot, and position then refers to the wrong
        # venue. Reject when the market's venue prefix does not resolve
        # to the account's venue (alias/family aware — see
        # :func:`_venues_compatible`).
        #
        # A simulated account venue (mock) intentionally executes any
        # market prefix — paper accounts exist precisely so strategies
        # configured with BYBIT:/BINANCE: markets can rehearse without a
        # real venue. The mismatch gate is only evidence on real venues;
        # the dangerous direction (fake prefix on a live account) stays
        # fully enforced.
        if (
            _venue_family(str(account_profile.venue or "")) != "mock"
            and not _venues_compatible(intent.market, str(account_profile.venue or ""))
        ):
            reasons.append(
                f"market_venue_account_mismatch:{intent.market}"
                f"!={account_profile.venue}"
            )
            return RiskDecision(
                intent.intent_id,
                "reject",
                reasons,
                limits_snapshot=asdict(strategy.limits),
                estimated_notional_usd=intent.notional_usd_estimate,
                fix_hints=derive_fix_hints(reasons, intent=intent),
            )

        # 1d. Contract-suffixed markets need a derivatives-capable venue.
        #
        # An explicit unified contract symbol ("SOL/USDT:USDT") stays
        # verbatim in the connector, so an account whose venue cannot
        # trade derivatives (spot-only specs, unknown venues) would only
        # fail at the venue with a late, confusing BadSymbol. When the
        # market carries an explicit contract suffix, require the
        # account's venue to support derivatives (mock venues stay
        # exempt, same as the mismatch gate above).
        if (
            _market_has_contract_suffix(intent.market)
            and _venue_family(str(account_profile.venue or "")) != "mock"
            and not _venue_supports_derivatives(str(account_profile.venue or ""))
        ):
            reasons.append(f"market_instrument_unsupported:{intent.market}")
            return RiskDecision(
                intent.intent_id,
                "reject",
                reasons,
                limits_snapshot=asdict(strategy.limits),
                estimated_notional_usd=intent.notional_usd_estimate,
                fix_hints=derive_fix_hints(reasons, intent=intent),
            )

        # 2. Execution-mode gates. Use AccountProfile rather than the
        # compatibility Account view: load_accounts intentionally collapses
        # canary to paper, which previously made canary orders pass paper-cash
        # checks and skip real-money fail-closed branches.
        if account_profile.is_real_money and not self.config.live_trading_enabled():
            reasons.append("live_trading_disabled_runtime")
            decision = "reject"
        if account_profile.is_real_money and not account_profile.live_trading_enabled:
            reasons.append("live_trading_disabled_account")
            decision = "reject"
        if account_profile.mode == "paper" and not self.config.paper_trading_enabled():
            reasons.append("paper_trading_disabled_runtime")
            decision = "reject"
        # A paper-lifecycle strategy must never reach a real-money account even
        # if a caller supplies that account id and all global live flags are on.
        if account_profile.is_real_money and strategy.status not in {
            "shadow", "canary", "live",
        }:
            reasons.append(
                f"strategy_status_{strategy.status}_cannot_use_real_money_account"
            )
            decision = "reject"
        if strategy.limits.kill_switch:
            reasons.append("strategy_kill_switch_enabled")
            decision = "reject"

        plan_action = str((intent.meta or {}).get("plan_action") or "").strip()
        risk_reducing = plan_action in {"close_position", "reduce_position"}

        # 3. Strategy status. Operators still need to flatten exposure
        # after a pause/quarantine/archive, so risk-reducing close/reduce
        # plans are allowed to continue through the safety checks.
        if not strategy.is_tradable and not (
            risk_reducing and strategy.status in {"paused", "quarantined", "archived"}
        ):
            reasons.append(f"strategy_status_{strategy.status}")
            decision = "reject"

        # 3b. promotion-state guards.
        #     - ``shadow``  must run against a real-money account but
        #       must NOT place orders. We mark the decision so the
        #       submit pipeline can short-circuit without losing the
        #       audit trail.
        #     - ``canary`` must have a protection rule attached (the
        #       caller threads this through via ``intent.meta``) and
        #       the per-trade approval threshold is forced down so an
        #       operator clicks every fill.
        #     - ``live``  cannot be reached from anything other than
        #       ``canary`` or ``paused`` per the lifecycle graph; that
        #       is enforced at promotion time, not here.
        promotion_state = strategy.status
        if strategy.status == "shadow":
            # Shadow positions do not touch the venue; mark the
            # decision and continue evaluating so we still check
            # confidence, dedupe, snapshots, etc.
            pass

        # 4. Account status
        if account.status != "active":
            reasons.append(f"account_status_{account.status}")
            decision = "reject"

        # 5. Market allow-list
        if strategy.limits.allowed_markets and intent.market not in strategy.limits.allowed_markets:
            reasons.append(f"market_not_allowed:{intent.market}")
            decision = "reject"

        # notional
        mark = (market_snapshot or {}).get("price") or intent.limit_price or 0.0
        market_envelope = (
            (market_snapshot or {}).get("_envelope")
            if isinstance((market_snapshot or {}).get("_envelope"), dict)
            else {}
        )
        if (
            account_profile.is_real_money
            and not risk_reducing
            and str((market_envelope or {}).get("mode") or "").lower() != "live"
        ):
            reasons.append("real_money_requires_live_market_snapshot")
            decision = "reject"
        if intent.size_unit == "usd":
            notional = float(intent.size)
        elif intent.size_unit == "base":
            notional = float(intent.size) * float(mark or 0)
        elif intent.size_unit == "quote":
            # A quote-unit size is a USD notional only when the market's
            # quote asset is a USD stable. Anything else previously fell
            # through as "quote == usd" (1 unit of a BTC-quoted pair
            # counted as 1 dollar) — fail closed instead of silently
            # miscalculating every notional-scaled cap.
            if is_usd_stable_quote(intent.market):
                notional = float(intent.size)
            else:
                reasons.append(f"unsupported_quote_size_unit:{intent.market}")
                decision = "reject"
                notional = 0.0
        else:
            notional = float(intent.size)

        # 6. Per-single-order cap
        cap = strategy.limits.max_single_order_usd
        if not risk_reducing and cap > 0 and notional > cap:
            reasons.append(f"max_single_order_exceeded:{notional:.2f}>{cap:.2f}")
            decision = "reject"

        # 6b. canary forces a stricter cap regardless of
        # what the strategy's own ``limits.yml`` says, so a too-loose
        # YAML cannot bypass the canary safety net.
        if not risk_reducing and strategy.status == "canary":
            canary_cap = float(self.config.get("trading.canary.max_single_order_usd", 250.0))
            if canary_cap > 0 and notional > canary_cap:
                reasons.append(
                    f"canary_max_single_order_exceeded:{notional:.2f}>{canary_cap:.2f}"
                )
                decision = "reject"

        # 7. Total exposure cap.
        #
        # PositionBook is the source of truth for both live and paper
        # accounts after v6 — the merged ``size_base * mark`` per market
        # gives the same gross figure regardless of how many strategies
        # contributed. Fall back to the virtual ledger when the book is
        # empty (e.g. a fresh account before any fill has landed) so
        # paper-only tests that pre-populated the ledger keep passing.
        ledger = open_ledger(paths, account.id, account.initial_balance_usd)
        ledger_snapshot = ledger.snapshot()
        book = PositionBook(paths)
        try:
            book_open = book.open_positions(account_id=account.id)
        except Exception:
            book_open = []
        if book_open:
            current_exposure = 0.0
            current_merged_size_by_market: dict[str, float] = {}
            current_merged_avg_by_market: dict[str, float] = {}
            for pos in book_open:
                size = float(pos.size_base or 0.0)
                avg = float(pos.avg_entry_price or 0.0)
                mark = float(pos.mark_price or avg or 0.0)
                current_exposure += abs(size * mark)
                current_merged_size_by_market[pos.market] = size
                current_merged_avg_by_market[pos.market] = avg
        else:
            current_exposure = sum(
                abs(p.get("size", 0) * p.get("avg_price", 0))
                for p in ledger_snapshot["positions"].values()
            )
            current_merged_size_by_market = {
                m: float((p or {}).get("size") or 0.0)
                for m, p in (ledger_snapshot["positions"] or {}).items()
            }
            current_merged_avg_by_market = {
                m: float((p or {}).get("avg_price") or 0.0)
                for m, p in (ledger_snapshot["positions"] or {}).items()
            }
        total_cap = strategy.limits.max_total_exposure_usd
        if not risk_reducing and total_cap > 0 and current_exposure + notional > total_cap:
            reasons.append(
                f"max_total_exposure_exceeded:{current_exposure:.2f}+{notional:.2f}>{total_cap:.2f}"
            )
            decision = "reject"

        # 7b. Per-(account, market) merged-position size cap.
        #
        # ``max_position_size_usd`` protects the *merged* position from
        # runaway aggregation when several strategies hit the same
        # exchange + symbol. We project the new merged size after this
        # fill and compare to the cap in USD. Risk-reducing intents are
        # exempt — a close/reduce can only ever shrink the merged
        # position so it never blows the cap.
        current_size = current_merged_size_by_market.get(intent.market, 0.0)
        current_avg = current_merged_avg_by_market.get(intent.market, 0.0)
        # New size delta in base units. Used by the per-market cap and
        # by the snapshot-freshness exemption below.
        base_size = (
            float(intent.size)
            if intent.size_unit == "base"
            else (float(intent.size) / float(mark or 1.0) if mark else 0.0)
        )
        signed_delta = base_size if intent.side == "buy" else -base_size
        projected_size = current_size + signed_delta
        # An intent is "position-reducing" if it shrinks the absolute
        # merged size — that covers explicit close/reduce plan actions
        # AND ad-hoc operator sells that mathematically de-risk the
        # book (e.g. a sell against an existing long, even without a
        # `plan_action` tag). We use this to relax gates that exist to
        # protect against *adding* exposure (snapshot freshness, etc.).
        position_reducing = bool(
            risk_reducing
            or (abs(current_size) > 0 and abs(projected_size) < abs(current_size))
        )

        per_market_cap = strategy.limits.max_position_size_usd
        if not risk_reducing and per_market_cap > 0 and notional > 0:
            projected_notional = abs(projected_size) * float(mark or current_avg or 0.0)
            if projected_notional > per_market_cap:
                reasons.append(
                    f"max_position_size_exceeded:{intent.market}:"
                    f"{projected_notional:.2f}>{per_market_cap:.2f}"
                )
                decision = "reject"

        # 8. Virtual ledger balance (paper mode only). Canary is real money
        # even though the legacy Account compatibility view calls it paper.
        if (
            not risk_reducing
            and account_profile.mode == "paper"
            and intent.side == "buy"
            and ledger_snapshot["cash_usd"] < notional
        ):
            reasons.append("insufficient_paper_cash")
            decision = "reject"

        # 8b. Account snapshot freshness + reservation overlay.
        # Refresh on demand before risk evaluation so scheduled strategies
        # are not rejected just because the background loop cadence is wider
        # than the risk freshness threshold.
        snapshot_payload: dict[str, Any] = {}
        reservation_blocked_usd = 0.0
        max_age_s = float(self.config.get("trading.snapshot.max_age_seconds", 60))
        snap = fresh_snapshot(self.config, intent.account_id, max_age_s=max_age_s)
        if snap is not None:
            snapshot_payload = snap.asdict()
            # Position-reducing intents (close/reduce plan actions OR
            # sells that mathematically shrink the merged position)
            # bypass the freshness/health rejection: the operator needs
            # an exit valve even when the balance loop has stalled or
            # the venue is sneezing. The reason is still appended for
            # audit, with an ``_exempt`` suffix so the dashboard can
            # show the warning without coloring the intent red.
            stale_age = int(time.time() - snap.ts)
            if snap.is_stale(max_age_s=max_age_s):
                if position_reducing:
                    reasons.append(
                        f"account_snapshot_stale_exempt:{stale_age}s>{int(max_age_s)}s"
                    )
                else:
                    reasons.append(
                        f"account_snapshot_stale:{stale_age}s>{int(max_age_s)}s"
                    )
                    decision = "reject"
            if snap.health != "ok":
                if position_reducing:
                    reasons.append(f"account_snapshot_health_exempt:{snap.health}")
                else:
                    reasons.append(f"account_snapshot_health_{snap.health}")
                    decision = "reject"
            try:
                reservation_blocked_usd = CapitalReservationStore(paths).total_blocked_usd(
                    intent.account_id
                )
            except Exception:
                reservation_blocked_usd = 0.0
            # On real-money modes, blocked reservations must not exceed
            # the snapshot's free balance + the new notional.
            if not risk_reducing and account_profile.is_real_money and intent.side == "buy":
                free_usd = snap.free_usd
                if reservation_blocked_usd + notional > free_usd:
                    reasons.append(
                        f"reservation_overcommit:"
                        f"reserved={reservation_blocked_usd:.2f}+new={notional:.2f}>"
                        f"free={free_usd:.2f}"
                    )
                    decision = "reject"

        # 8c. Reconciliation halt. If the most recent
        # reconciliation pass for this account left a ``trading_halted``
        # severity unresolved (within the lookback window), every new
        # open is rejected until an operator runs a clean pass.
        try:
            recon_lookback = float(self.config.get("trading.reconciliation.halt_window_s", 1800))
            worst = ReconciliationStore(paths).worst_recent(
                account_id=intent.account_id,
                within_seconds=recon_lookback,
            )
        except Exception:
            worst = None
        if not risk_reducing and worst is not None:
            if worst.severity == "trading_halted":
                reasons.append(f"reconciliation_halt:{worst.report_id}")
                decision = "reject"
            elif worst.severity == "action_required" and account.is_live:
                # Soft escalate so an operator approves any open while
                # an unresolved drift exists on a live account.
                if decision != "reject":
                    reasons.append(f"reconciliation_action_required:{worst.report_id}")
                    decision = "escalate"

        # 11. Confidence floor
        if intent.confidence < strategy.limits.min_confidence:
            reasons.append(
                f"confidence_below_floor:{intent.confidence:.2f}<{strategy.limits.min_confidence:.2f}"
            )
            decision = "reject"

        # 13. Stale data guard
        if market_snapshot and "age_s" in market_snapshot:
            if int(market_snapshot["age_s"]) > strategy.limits.max_stale_seconds:
                reasons.append(
                    f"stale_market_data:{market_snapshot['age_s']}s>{strategy.limits.max_stale_seconds}s"
                )
                decision = "reject"

        # 14. Duplicate / dedupe
        # Skip on resume — an approved intent is replayed with the same
        # strategy/market/side/notional, which would otherwise trip the
        # dedupe key and reject the resumed order. The executor's
        # ``client_order_id`` idempotency is the real double-submit guard.
        if not risk_reducing and not resume:
            dedupe_key = f"{intent.strategy_id}:{intent.market}:{intent.side}:{round(notional, 2)}"
            dedupe = DedupeRepository(self._con_lazy())
            window = float(self.config.get("trading.dedupe_window_seconds", 300))
            if dedupe.seen("trade_intent", dedupe_key, window_s=window):
                reasons.append("duplicate_intent")
                decision = "reject"

        # 14b. Manifest policy caps enforced as hard gates.
        # ``max_daily_notional_usd`` sums today's executed order
        # notionals from the strategy history; ``max_open_positions``
        # counts the strategy's currently-open PositionBook entries.
        # Both only apply to risk-*adding* intents (opens); a close can
        # always proceed so an operator can de-risk when over-limit.
        if not risk_reducing:
            daily_cap = strategy.limits.max_daily_notional_usd
            if daily_cap > 0:
                spent_today = _strategy_daily_notional(paths, intent.strategy_id)
                if spent_today is None:
                    # Ledger unreadable. Fail closed for real-money
                    # accounts — an unenforceable daily cap must not
                    # silently become no cap. Paper stays permissive.
                    if account_profile.is_real_money:
                        reasons.append("daily_notional_ledger_unreadable")
                        decision = "reject"
                    spent_today = 0.0
                if spent_today + notional > daily_cap:
                    reasons.append(
                        f"max_daily_notional_exceeded:{spent_today:.2f}+{notional:.2f}>"
                        f"{daily_cap:.2f}"
                    )
                    decision = "reject"
            pos_cap = strategy.limits.max_open_positions
            if pos_cap > 0:
                open_count = 0
                try:
                    open_count = sum(
                        1 for p in book.open_positions(account_id=account.id)
                        if p.strategy_id == intent.strategy_id
                    )
                except Exception:
                    open_count = 0
                # A new open on a market the strategy already holds does
                # not increase the position count (it adds to an existing
                # merged position), so only count it when the strategy
                # has no current exposure on this market.
                already_open_here = any(
                    p.market == intent.market and p.strategy_id == intent.strategy_id
                    for p in (book_open or [])
                )
                if not already_open_here and open_count >= pos_cap:
                    reasons.append(
                        f"max_open_positions_exceeded:{open_count}>={pos_cap}"
                    )
                    decision = "reject"

        # 12 & 15 & 16. Slippage / conflicts / approval threshold (advisory)
        if (
            not risk_reducing
            and strategy.limits.approval_threshold_usd > 0
            and notional >= strategy.limits.approval_threshold_usd
        ):
            if decision != "reject":
                reasons.append(
                    f"approval_required_threshold:{notional:.2f}>={strategy.limits.approval_threshold_usd:.2f}"
                )
                decision = "escalate"

        # 12b. canary state forces approval on every
        # opening side regardless of notional, and rejects opens that
        # don't carry a protection rule. ``intent.meta['protection_present']``
        # is set by ``submit_trade_plan`` when a TradePlan declares one;
        # legacy ``submit_trade_intent`` callers can opt in by passing
        # ``meta={'protection_present': True}``.
        if not risk_reducing and strategy.status == "canary":
            meta = intent.meta or {}
            protection_present = bool(
                meta.get("protection_present")
                or meta.get("plan_protection_attached")
            )
            # Both open sides need protection: a canary short without
            # SL/TP is exactly as naked as a canary long.
            if not protection_present:
                reasons.append("canary_requires_protection_rule")
                decision = "reject"
            if decision != "reject":
                reasons.append("canary_per_trade_approval_required")
                decision = "escalate"

        # Human/operator-driven Agent turns always stop at Approval Gate,
        # independent of notional and account mode. Strategy automation uses
        # the dedicated strategy_* sources and may proceed unattended subject
        # to its own lifecycle/risk policy. This source is trusted only after
        # the native-tool wrapper overwrites model-supplied provenance.
        operator_agent_sources = {"agent", "agent:native", "subagent", "operator"}
        source_key = str(intent.source or "").strip().lower()
        # A dashboard operator's emergency flatten path is intentionally
        # allowed to reduce risk without another card. Conversational Agent
        # proposals—including closes/reductions—still require Approval Gate.
        if source_key in operator_agent_sources and not (
            source_key == "operator" and risk_reducing
        ):
            if decision != "reject":
                reasons.append("operator_agent_trade_approval_required")
                decision = "escalate"

        if not reasons:
            reasons = ["ok"]

        # 12c. shadow strategies pass risk but never
        # actually place orders. Tag the decision so the submit
        # pipeline can short-circuit cleanly. Risk is still evaluated
        # against the real-money snapshot above so a broken paper
        # mock cannot mask issues that would surface in shadow.
        shadow_only = (strategy.status == "shadow") and decision != "reject"

        decision_obj = RiskDecision(
            intent_id=intent.intent_id,
            decision=decision,  # type: ignore[arg-type]
            reasons=reasons,
            limits_snapshot=asdict(strategy.limits),
            virtual_ledger_snapshot=ledger_snapshot,
            estimated_notional_usd=notional,
            account_snapshot=snapshot_payload,
            reservation_blocked_usd=reservation_blocked_usd,
            shadow_only=shadow_only,
            promotion_state=promotion_state,
            fix_hints=derive_fix_hints(reasons, intent=intent),
        )
        try:
            self._persist(decision_obj, intent=intent)
        except Exception:
            # For paper accounts persistence stays advisory — a
            # bookkeeping hiccup must not block simulated trading. For
            # real-money accounts the audit row is part of the safety
            # contract (reservations / executors pin themselves to the
            # risk_evaluation_id), so fail closed instead of trading
            # without a persisted decision.
            log.exception(
                "risk decision persistence failed for intent %s",
                intent.intent_id,
            )
            if account_profile.is_real_money and decision_obj.decision != "reject":
                decision_obj.decision = "reject"
                decision_obj.reasons = [
                    r for r in decision_obj.reasons if r != "ok"
                ] + ["risk_persistence_failed"]
                decision_obj.fix_hints = derive_fix_hints(
                    decision_obj.reasons, intent=intent,
                )
        return decision_obj

    # --------------------------------------------------------------- persistence

    def evaluate_resolved_notional(
        self,
        intent: TradeIntent,
        *,
        notional_usd: float,
        mark_price: float | None = None,
    ) -> RiskDecision:
        """Re-run the notional-dependent checks against a *resolved* notional.

        NAV-derived sizing (``pct_nav`` / ``risk_to_stop`` /
        ``volatility_target`` / ``target_weight`` / position-relative
        methods) reaches :meth:`evaluate` through a placeholder notional
        because the real amount only exists once
        :class:`~nerya.trading.capital.BudgetChecker` resolves it against
        the account snapshot. This method re-validates that resolved
        amount against every cap that scales with order size — the
        single-order cap, the canary cap, total exposure, the per-market
        position cap, the daily notional cap, and the approval-threshold
        escalation — so a strategy cannot open full-NAV positions while
        every notional gate sees ≈$0.

        Callers must skip this for risk-reducing plans (close/reduce),
        which stay exempt from the caps by design. The returned decision
        is always at least as strict as the original: it only ever adds
        reject/escalate reasons.
        """

        paths = self.config.paths
        notional = float(max(0.0, notional_usd))
        reasons: list[str] = []
        decision: str = "allow"

        try:
            strategy: Strategy = load_strategy(paths, intent.strategy_id)
        except Exception as exc:
            return RiskDecision(
                intent.intent_id,
                "reject",
                [f"strategy_unknown:{exc}"],
                estimated_notional_usd=notional,
                fix_hints=derive_fix_hints([f"strategy_unknown:{exc}"], intent=intent),
            )
        try:
            account_profile = get_account_profile(paths, intent.account_id)
        except Exception as exc:
            return RiskDecision(
                intent.intent_id,
                "reject",
                [f"account_profile_unavailable:{exc}"],
                estimated_notional_usd=notional,
                fix_hints=derive_fix_hints(
                    [f"account_profile_unavailable:{exc}"], intent=intent,
                ),
            )

        # Single-order cap (check 6) + canary cap (6b).
        cap = strategy.limits.max_single_order_usd
        if cap > 0 and notional > cap:
            reasons.append(f"max_single_order_exceeded:{notional:.2f}>{cap:.2f}")
            decision = "reject"
        if strategy.status == "canary":
            canary_cap = float(self.config.get("trading.canary.max_single_order_usd", 250.0))
            if canary_cap > 0 and notional > canary_cap:
                reasons.append(
                    f"canary_max_single_order_exceeded:{notional:.2f}>{canary_cap:.2f}"
                )
                decision = "reject"

        # Total exposure cap (check 7) + per-market merged-position cap
        # (7b) — same reads as evaluate().
        ledger = open_ledger(paths, intent.account_id, account_profile.initial_balance_usd)
        ledger_snapshot = ledger.snapshot()
        book = PositionBook(paths)
        try:
            book_open = book.open_positions(account_id=intent.account_id)
        except Exception:
            book_open = []
        if book_open:
            current_exposure = 0.0
            current_merged_size_by_market: dict[str, float] = {}
            current_merged_avg_by_market: dict[str, float] = {}
            for pos in book_open:
                size = float(pos.size_base or 0.0)
                current_exposure += abs(size * float(pos.mark_price or pos.avg_entry_price or 0.0))
                current_merged_size_by_market[pos.market] = size
                current_merged_avg_by_market[pos.market] = float(pos.avg_entry_price or 0.0)
        else:
            current_exposure = sum(
                abs(p.get("size", 0) * p.get("avg_price", 0))
                for p in ledger_snapshot["positions"].values()
            )
            current_merged_size_by_market = {
                m: float((p or {}).get("size") or 0.0)
                for m, p in (ledger_snapshot["positions"] or {}).items()
            }
            current_merged_avg_by_market = {
                m: float((p or {}).get("avg_price") or 0.0)
                for m, p in (ledger_snapshot["positions"] or {}).items()
            }
        total_cap = strategy.limits.max_total_exposure_usd
        if total_cap > 0 and current_exposure + notional > total_cap:
            reasons.append(
                f"max_total_exposure_exceeded:{current_exposure:.2f}+{notional:.2f}>"
                f"{total_cap:.2f}"
            )
            decision = "reject"

        per_market_cap = strategy.limits.max_position_size_usd
        if per_market_cap > 0 and notional > 0 and mark_price and mark_price > 0:
            current_size = current_merged_size_by_market.get(intent.market, 0.0)
            current_avg = current_merged_avg_by_market.get(intent.market, 0.0)
            base_size = notional / float(mark_price)
            signed_delta = base_size if intent.side == "buy" else -base_size
            projected_notional = abs(current_size + signed_delta) * float(
                mark_price or current_avg or 0.0
            )
            if projected_notional > per_market_cap:
                reasons.append(
                    f"max_position_size_exceeded:{intent.market}:"
                    f"{projected_notional:.2f}>{per_market_cap:.2f}"
                )
                decision = "reject"

        # Daily notional cap (14b) — fail closed on real money when the
        # ledger is unreadable, exactly like evaluate().
        daily_cap = strategy.limits.max_daily_notional_usd
        if daily_cap > 0:
            spent_today = _strategy_daily_notional(paths, intent.strategy_id)
            if spent_today is None:
                if account_profile.is_real_money:
                    reasons.append("daily_notional_ledger_unreadable")
                    decision = "reject"
                spent_today = 0.0
            if spent_today + notional > daily_cap:
                reasons.append(
                    f"max_daily_notional_exceeded:{spent_today:.2f}+{notional:.2f}>"
                    f"{daily_cap:.2f}"
                )
                decision = "reject"

        # Approval-threshold escalation (12) — only fires when the
        # resolved notional crosses the threshold.
        if (
            decision != "reject"
            and strategy.limits.approval_threshold_usd > 0
            and notional >= strategy.limits.approval_threshold_usd
        ):
            reasons.append(
                f"approval_required_threshold:{notional:.2f}>="
                f"{strategy.limits.approval_threshold_usd:.2f}"
            )
            decision = "escalate"

        if not reasons:
            reasons = ["ok"]

        resolved = RiskDecision(
            intent_id=intent.intent_id,
            decision=decision,  # type: ignore[arg-type]
            reasons=reasons,
            limits_snapshot=asdict(strategy.limits),
            estimated_notional_usd=notional,
            promotion_state=strategy.status,
            fix_hints=derive_fix_hints(reasons, intent=intent),
        )
        if resolved.decision != "allow":
            # Persist enforcement events so reservations / operators can
            # pin themselves to the binding decision. An allow just
            # mirrors the original evaluation and needs no second row.
            try:
                self._persist(resolved, intent=intent)
            except Exception:
                log.exception(
                    "resolved-notional risk persistence failed for intent %s",
                    intent.intent_id,
                )
                if account_profile.is_real_money:
                    resolved.decision = "reject"
                    resolved.reasons = [
                        r for r in resolved.reasons if r != "ok"
                    ] + ["risk_persistence_failed"]
                    resolved.fix_hints = derive_fix_hints(
                        resolved.reasons, intent=intent,
                    )
        return resolved

    def _persist(self, decision: RiskDecision, *, intent: TradeIntent) -> None:
        con = self._con_lazy()
        # Embed fix_hints under the snapshot blob so the existing
        # ``risk_evaluations`` table doesn't need a schema migration —
        # the dashboard reads them out by key.
        snapshot_blob = dict(decision.account_snapshot)
        if decision.fix_hints:
            snapshot_blob["_fix_hints"] = decision.fix_hints
        con.execute(
            """
            INSERT OR IGNORE INTO risk_evaluations (
                risk_evaluation_id, intent_id, plan_id, strategy_id, account_id,
                decision, notional_usd, reasons_json, snapshot_json, ts
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                decision.risk_evaluation_id,
                intent.intent_id,
                None,
                intent.strategy_id,
                intent.account_id,
                decision.decision,
                decision.estimated_notional_usd,
                json.dumps(decision.reasons),
                json.dumps(snapshot_blob),
                time.time(),
            ),
        )


def _strategy_daily_notional(paths, strategy_id: str) -> float | None:
    """Sum the notional of every order the strategy placed today.

    Reads the strategy's ``orders.jsonl`` history and totals
    ``payload.notional_usd`` for entries whose session opened in the
    last 24h. A missing log legitimately means "nothing spent" and
    returns ``0.0``; a *read failure* returns ``None`` so the caller
    can fail closed on real-money accounts instead of treating a
    corrupt ledger as an unlimited budget.
    """
    try:
        from ..strategy_history import store as history_store

        log_path = paths.strategy(strategy_id) / "orders.jsonl"
        if not log_path.exists():
            return 0.0
        cutoff = time.time() - 86_400.0
        total = 0.0
        for line in log_path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                entry = json.loads(line)
            except Exception:
                continue
            ts = entry.get("ts")
            # ``ts`` is an ISO string; parse defensively and skip if old.
            try:
                from datetime import datetime
                dt = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
                if dt.timestamp() < cutoff:
                    continue
            except Exception:
                pass
            payload = entry.get("payload") or {}
            total += float(payload.get("notional_usd") or 0.0)
        return total
    except Exception:
        log.exception(
            "daily notional ledger read failed for strategy %s", strategy_id,
        )
        return None
