"""Capital management — sizing, budget checking, reservations.

multi-strategy concurrency must not double-spend
the same balance. The two pieces in here cooperate to make that
guarantee:

* :class:`BudgetChecker` translates a strategy's
  :class:`SizingPolicy` into an :class:`OrderCandidate`. It cross
  references the latest :class:`AccountSnapshot` and any *outstanding*
  capital reservations on the same account, then either ``allow``s,
  ``resize``s, or ``reject``s the request. the connector framework's
  ``BudgetChecker`` is the conceptual ancestor; the Nerya version
  drops collateral graphs and deals only in USD-equivalent notional
  for the first pass.

* :class:`CapitalReservationStore` is the persistent ledger of
  outstanding reservations. RiskGate calls
  :meth:`CapitalReservationStore.reserve` *after* allowing an order;
  the executor calls :meth:`consume` on fill or :meth:`release` on
  cancel/expiry. Reservations have an explicit ``state`` and
  ``expires_at`` so a crashed executor can't permanently leak budget.

This is intentionally additive: the legacy paper-only RiskGate keeps
working (it just does its own ad-hoc cash check), and only the new
trade-plan path through ``submit.py`` writes reservations.
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import asdict, dataclass, field
from typing import Any, Literal

from ..core.errors import TradingError
from ..core.ids import reservation_id as _new_reservation_id
from ..core.paths import WorkspacePaths
from ..db.sqlite import connect
from .account_snapshots import (
    AccountSnapshot,
    DEFAULT_MAX_AGE_S,
    free_usd_for_account,
)
from .accounts import AccountProfile
from .order_intents import OrderCandidate, SizingPolicy

log = logging.getLogger(__name__)

ReservationState = Literal[
    "proposed",
    "reserved",
    "consumed",
    "released",
    "expired",
    "rejected",
]


# Default fee buffer applied on top of the snapshot's reported
# free-balance to leave headroom for taker/maker fees + funding
# settlement. Kept tiny on purpose; per-account override comes from
# :attr:`AccountLimits.fee_buffer_bps`.
_DEFAULT_FEE_BUFFER_BPS = 5.0


# ---------------------------------------------------------------------------
# Reservation
# ---------------------------------------------------------------------------


@dataclass
class CapitalReservation:
    reservation_id: str
    account_id: str
    strategy_id: str
    market: str
    side: Literal["buy", "sell"]
    notional_usd: float
    estimated_fee_usd: float
    estimated_margin_usd: float
    state: ReservationState
    risk_evaluation_id: str = ""
    intent_id: str = ""
    plan_id: str = ""
    executor_id: str = ""
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)
    expires_at: float | None = None
    meta: dict[str, Any] = field(default_factory=dict)

    @property
    def total_blocked_usd(self) -> float:
        """Notional + fee buffer + margin draw."""
        return float(self.notional_usd + self.estimated_fee_usd + self.estimated_margin_usd)

    def is_active(self) -> bool:
        return self.state in ("proposed", "reserved")

    def asdict(self) -> dict[str, Any]:
        return asdict(self)


# ---------------------------------------------------------------------------
# Reservation store
# ---------------------------------------------------------------------------


class CapitalReservationStore:
    """Persistent capital-reservation ledger.

    The store is intentionally synchronous and SQLite-backed; the row
    count stays small because reservations are short-lived (lifetime is
    bounded by ``expires_at``, default 5 minutes). Keep the API
    minimal — every transition writes a single row UPDATE so there's
    no opportunity for split-brain state.
    """

    def __init__(self, paths: WorkspacePaths):
        self.paths = paths
        self._con = None

    def _con_lazy(self):
        if self._con is None:
            self._con = connect(self.paths.db)
        return self._con

    # -- create -----------------------------------------------------------------
    def reserve(
        self,
        *,
        account_id: str,
        strategy_id: str,
        market: str,
        side: str,
        notional_usd: float,
        estimated_fee_usd: float = 0.0,
        estimated_margin_usd: float = 0.0,
        risk_evaluation_id: str = "",
        intent_id: str = "",
        plan_id: str = "",
        executor_id: str = "",
        ttl_seconds: float = 300,
        state: ReservationState = "reserved",
        meta: dict[str, Any] | None = None,
    ) -> CapitalReservation:
        if side not in ("buy", "sell"):
            raise TradingError(f"reservation side must be buy|sell, got {side!r}")
        rsv = CapitalReservation(
            reservation_id=_new_reservation_id(),
            account_id=account_id,
            strategy_id=strategy_id,
            market=market,
            side=side,  # type: ignore[arg-type]
            notional_usd=float(max(0.0, notional_usd)),
            estimated_fee_usd=float(max(0.0, estimated_fee_usd)),
            estimated_margin_usd=float(max(0.0, estimated_margin_usd)),
            state=state,
            risk_evaluation_id=risk_evaluation_id,
            intent_id=intent_id,
            plan_id=plan_id,
            executor_id=executor_id,
            created_at=time.time(),
            updated_at=time.time(),
            expires_at=time.time() + float(ttl_seconds) if ttl_seconds else None,
            meta=dict(meta or {}),
        )
        con = self._con_lazy()
        con.execute(
            """
            INSERT INTO capital_reservations (
                reservation_id, account_id, strategy_id, intent_id, plan_id,
                executor_id, market, side, notional_usd, estimated_fee_usd,
                estimated_margin_usd, state, risk_evaluation_id, created_at,
                updated_at, expires_at, meta_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                rsv.reservation_id, rsv.account_id, rsv.strategy_id, rsv.intent_id,
                rsv.plan_id, rsv.executor_id, rsv.market, rsv.side,
                rsv.notional_usd, rsv.estimated_fee_usd, rsv.estimated_margin_usd,
                rsv.state, rsv.risk_evaluation_id, rsv.created_at, rsv.updated_at,
                rsv.expires_at, json.dumps(rsv.meta),
            ),
        )
        return rsv

    # -- transitions ------------------------------------------------------------
    def _set_state(self, reservation_id: str, new_state: ReservationState) -> None:
        con = self._con_lazy()
        con.execute(
            "UPDATE capital_reservations SET state = ?, updated_at = ? WHERE reservation_id = ?",
            (new_state, time.time(), reservation_id),
        )

    def consume(self, reservation_id: str) -> None:
        self._set_state(reservation_id, "consumed")

    def release(self, reservation_id: str) -> None:
        self._set_state(reservation_id, "released")

    def reject(self, reservation_id: str) -> None:
        self._set_state(reservation_id, "rejected")

    def attach_executor(self, reservation_id: str, executor_id: str) -> None:
        con = self._con_lazy()
        con.execute(
            "UPDATE capital_reservations SET executor_id = ?, updated_at = ? WHERE reservation_id = ?",
            (executor_id, time.time(), reservation_id),
        )

    # -- queries ----------------------------------------------------------------
    def expire_due(self, *, now: float | None = None) -> int:
        """Mark expired reservations. Returns the number of rows updated."""
        con = self._con_lazy()
        ts = now if now is not None else time.time()
        cur = con.execute(
            """
            UPDATE capital_reservations
               SET state = 'expired', updated_at = ?
             WHERE state IN ('proposed', 'reserved')
               AND expires_at IS NOT NULL
               AND expires_at <= ?
            """,
            (ts, ts),
        )
        return int(cur.rowcount or 0)

    def get(self, reservation_id: str) -> CapitalReservation | None:
        row = self._con_lazy().execute(
            "SELECT * FROM capital_reservations WHERE reservation_id = ?",
            (reservation_id,),
        ).fetchone()
        return _row_to_reservation(row) if row else None

    def active_for_account(self, account_id: str) -> list[CapitalReservation]:
        self.expire_due()
        rows = self._con_lazy().execute(
            """
            SELECT * FROM capital_reservations
             WHERE account_id = ? AND state IN ('proposed', 'reserved')
             ORDER BY created_at
            """,
            (account_id,),
        ).fetchall()
        return [_row_to_reservation(r) for r in rows]

    def total_blocked_usd(self, account_id: str) -> float:
        return float(sum(r.total_blocked_usd for r in self.active_for_account(account_id)))


def _row_to_reservation(row: Any) -> CapitalReservation:
    return CapitalReservation(
        reservation_id=str(row["reservation_id"]),
        account_id=str(row["account_id"]),
        strategy_id=str(row["strategy_id"]),
        market=str(row["market"]),
        side=str(row["side"]),  # type: ignore[arg-type]
        notional_usd=float(row["notional_usd"] or 0.0),
        estimated_fee_usd=float(row["estimated_fee_usd"] or 0.0),
        estimated_margin_usd=float(row["estimated_margin_usd"] or 0.0),
        state=str(row["state"]),  # type: ignore[arg-type]
        risk_evaluation_id=str(row["risk_evaluation_id"] or ""),
        intent_id=str(row["intent_id"] or ""),
        plan_id=str(row["plan_id"] or ""),
        executor_id=str(row["executor_id"] or ""),
        created_at=float(row["created_at"] or 0.0),
        updated_at=float(row["updated_at"] or 0.0),
        expires_at=float(row["expires_at"]) if row["expires_at"] is not None else None,
        meta=json.loads(str(row["meta_json"] or "{}")),
    )


# ---------------------------------------------------------------------------
# Sizing
# ---------------------------------------------------------------------------


def _resolve_notional(
    sizing: SizingPolicy,
    *,
    nav_usd: float,
    mark_price: float | None,
) -> float:
    """Translate a :class:`SizingPolicy` into a USD notional.

    Returns ``0`` for sizing methods that explicitly opt out of opening
    a new line item (``close_all`` is handled by the executor stack and
    doesn't generate a sizing decision here).
    """
    method = sizing.method
    if method == "fixed_usd":
        return float(sizing.fixed_usd or 0.0)
    if method == "fixed_base":
        if mark_price is None or mark_price <= 0:
            raise TradingError("fixed_base sizing needs a mark price")
        return float(sizing.fixed_base or 0.0) * float(mark_price)
    if method == "pct_nav":
        return float(nav_usd) * float(sizing.pct_nav or 0.0)
    if method == "risk_to_stop":
        if not sizing.risk_pct_nav or not sizing.stop_distance_pct:
            return 0.0
        risk_usd = float(nav_usd) * float(sizing.risk_pct_nav)
        return risk_usd / float(sizing.stop_distance_pct)
    if method == "volatility_target":
        # Without an ATR/vol input we can't safely target volatility.
        # Strategy must supply ``max_notional_usd`` as a hard fallback.
        return float(sizing.max_notional_usd or 0.0)
    if method == "target_weight":
        return float(nav_usd) * abs(float(sizing.target_weight or 0.0))
    if method == "reduce_pct":
        # Reduction is sized by the executor against the current
        # PositionBook entry, not from NAV.
        return 0.0
    if method == "close_all":
        return 0.0
    raise TradingError(f"unsupported sizing method: {method!r}")


# ---------------------------------------------------------------------------
# Budget checker
# ---------------------------------------------------------------------------

BudgetVerdict = Literal["allow", "resize", "reject", "escalate"]


@dataclass
class BudgetDecision:
    verdict: BudgetVerdict
    candidate: OrderCandidate
    reasons: list[str] = field(default_factory=list)

    def asdict(self) -> dict[str, Any]:
        return {
            "verdict": self.verdict,
            "reasons": list(self.reasons),
            "candidate": self.candidate.asdict(),
        }


class BudgetChecker:
    """Check a sizing policy against an account's available budget.

    Inputs:
        * ``profile`` — :class:`AccountProfile` (mode/limits/permissions).
        * ``snapshot`` — current :class:`AccountSnapshot`.
        * ``store`` — :class:`CapitalReservationStore` for outstanding
          reservations on the same account.
        * ``mark_price`` — best available reference price for the
          market in question.

    Outputs a :class:`BudgetDecision` whose ``candidate`` carries the
    final notional, base size, fee estimate, and resize/reject reason
    if any. Callers must *not* call :meth:`CapitalReservationStore.reserve`
    when the verdict is ``reject`` or ``escalate``.
    """

    def __init__(
        self,
        *,
        profile: AccountProfile,
        snapshot: AccountSnapshot,
        store: CapitalReservationStore,
        max_snapshot_age_s: float = DEFAULT_MAX_AGE_S,
    ) -> None:
        self.profile = profile
        self.snapshot = snapshot
        self.store = store
        self.max_snapshot_age_s = max_snapshot_age_s

    def evaluate(
        self,
        *,
        plan_strategy_id: str,
        market: str,
        side: Literal["buy", "sell"],
        sizing: SizingPolicy,
        mark_price: float | None,
        order_price: float | None = None,
        stop_price: float | None = None,
        order_type: Literal["market", "limit", "stop", "stop_limit"] = "market",
        leverage: float = 1.0,
        reduce_only: bool = False,
        time_in_force: Literal["gtc", "ioc", "fok", "post_only"] = "gtc",
        intent_id: str = "",
        plan_id: str = "",
        risk_evaluation_id: str = "",
    ) -> BudgetDecision:
        reasons: list[str] = []
        verdict: BudgetVerdict = "allow"

        # 0. Snapshot freshness
        if self.snapshot.is_stale(max_age_s=self.max_snapshot_age_s):
            reasons.append("snapshot_stale")
            verdict = "reject"

        if self.snapshot.health != "ok":
            reasons.append(f"snapshot_health_{self.snapshot.health}")
            verdict = "reject"

        # 1. Resolve target notional from sizing policy.
        try:
            notional = _resolve_notional(
                sizing,
                nav_usd=self.snapshot.nav_usd or self.profile.initial_balance_usd,
                mark_price=mark_price,
            )
        except TradingError as exc:
            reasons.append(f"sizing_failed:{exc}")
            return self._reject(
                market=market, side=side, plan_strategy_id=plan_strategy_id,
                order_type=order_type, leverage=leverage, reduce_only=reduce_only,
                time_in_force=time_in_force, mark_price=mark_price,
                order_price=order_price, stop_price=stop_price,
                notional=0.0, reasons=reasons, intent_id=intent_id,
                plan_id=plan_id, risk_evaluation_id=risk_evaluation_id,
            )

        # 2. Apply per-account hard caps.
        original_notional = notional
        max_order = float(self.profile.limits.max_order_notional_usd or 0.0)
        if not reduce_only and max_order > 0 and notional > max_order:
            notional = max_order
            verdict = "resize"
            reasons.append(f"resized_to_max_order_notional:{max_order:.2f}")

        # 3. Apply max account NAV cap (don't open more than the
        # account is allowed to manage in total).
        max_nav = float(self.profile.limits.max_account_nav_usd or 0.0)
        if not reduce_only and max_nav > 0 and self.snapshot.nav_usd > max_nav:
            reasons.append(
                f"account_nav_above_cap:{self.snapshot.nav_usd:.2f}>{max_nav:.2f}"
            )
            verdict = "reject"

        # 4. Fee buffer.
        fee_bps = float(self.profile.limits.fee_buffer_bps or _DEFAULT_FEE_BUFFER_BPS)
        estimated_fee = notional * (fee_bps / 10_000.0)

        # 5. Margin estimate (linear). For spot we charge full notional
        # against free balance; for leveraged perp we divide.
        if leverage <= 0:
            leverage = 1.0
        estimated_margin = notional / float(leverage)

        # 6. Cash check against snapshot - blocked.
        free_usd = free_usd_for_account(self.snapshot, self.profile.base_currency)
        blocked = self.store.total_blocked_usd(self.profile.id)
        available = max(0.0, free_usd - blocked)
        required = estimated_margin + estimated_fee
        if side == "buy" and not reduce_only and required > available:
            # Try to resize to fit.
            new_notional = max(0.0, (available - estimated_fee) * leverage)
            if new_notional > 0 and (max_order <= 0 or new_notional <= max_order):
                # Recompute fee on resized notional.
                estimated_fee = new_notional * (fee_bps / 10_000.0)
                estimated_margin = new_notional / leverage
                if estimated_fee + estimated_margin <= available:
                    notional = new_notional
                    verdict = "resize"
                    reasons.append(
                        f"resized_to_available:{notional:.2f}/{available:.2f}"
                    )
            if estimated_margin + estimated_fee > available:
                reasons.append(
                    f"insufficient_free_usd:{available:.2f}<{required:.2f}"
                )
                verdict = "reject"

        # 7. Min free balance pct constraint.
        min_pct = float(self.profile.limits.min_free_balance_pct or 0.0)
        if not reduce_only and min_pct > 0:
            min_free_after = self.snapshot.nav_usd * min_pct
            projected_free = available - (estimated_margin + estimated_fee)
            if projected_free < min_free_after:
                reasons.append(
                    f"would_breach_min_free_pct:{projected_free:.2f}<{min_free_after:.2f}"
                )
                verdict = "reject"

        # 8. Drop notional below epsilon
        if notional <= 0 and sizing.method not in ("close_all", "reduce_pct"):
            reasons.append("notional_zero_after_resize")
            verdict = "reject"

        # 9. Build the candidate.
        size_base: float | None = None
        if mark_price and mark_price > 0 and notional > 0:
            size_base = notional / float(mark_price)

        candidate = OrderCandidate(
            account_id=self.profile.id,
            strategy_id=plan_strategy_id,
            market=market,
            side=side,
            order_type=order_type,
            size_base=size_base,
            notional_usd=notional,
            price=(order_price if order_type in ("limit", "stop_limit") else None),
            stop_price=(stop_price if order_type in ("stop", "stop_limit") else None),
            leverage=leverage,
            reduce_only=reduce_only,
            time_in_force=time_in_force,
            estimated_fee_usd=estimated_fee,
            estimated_slippage_bps=0.0,
            required_collateral={self.profile.base_currency.upper(): estimated_margin},
            expected_returns={},
            resized=(notional != original_notional),
            resize_reason=(reasons[-1] if (notional != original_notional and reasons) else None),
            rejection_reason=(reasons[-1] if verdict == "reject" and reasons else None),
            intent_id=intent_id,
            plan_id=plan_id,
            risk_evaluation_id=risk_evaluation_id,
            # Stash the reference mark price even for market orders so
            # the paper executor can simulate fill price without
            # surfacing a fake "limit price" to a live venue.
            meta={"mark_price": float(mark_price)} if mark_price else {},
        )
        return BudgetDecision(verdict=verdict, candidate=candidate, reasons=reasons)

    def _reject(
        self, *, market, side, plan_strategy_id, order_type, leverage,
        reduce_only, time_in_force, mark_price, order_price, stop_price,
        notional, reasons,
        intent_id, plan_id, risk_evaluation_id,
    ) -> BudgetDecision:
        candidate = OrderCandidate(
            account_id=self.profile.id,
            strategy_id=plan_strategy_id,
            market=market,
            side=side,
            order_type=order_type,
            size_base=None,
            notional_usd=float(notional),
            price=(order_price if order_type in ("limit", "stop_limit") else None),
            stop_price=(stop_price if order_type in ("stop", "stop_limit") else None),
            leverage=leverage,
            reduce_only=reduce_only,
            time_in_force=time_in_force,
            estimated_fee_usd=0.0,
            estimated_slippage_bps=0.0,
            required_collateral={},
            expected_returns={},
            resized=False,
            resize_reason=None,
            rejection_reason=(reasons[-1] if reasons else "rejected"),
            intent_id=intent_id,
            plan_id=plan_id,
            risk_evaluation_id=risk_evaluation_id,
        )
        return BudgetDecision(verdict="reject", candidate=candidate, reasons=list(reasons))


__all__ = [
    "BudgetChecker",
    "BudgetDecision",
    "BudgetVerdict",
    "CapitalReservation",
    "CapitalReservationStore",
    "ReservationState",
]
