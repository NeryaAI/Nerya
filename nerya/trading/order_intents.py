"""Trading control-plane schemas (Plan 2026-04-29 §3.2/§4/§7).

This module is the canonical home for the *intent-side* dataclasses that
sit between an Agent / strategy SDK call and the rest of the trading
stack. They are deliberately framework-agnostic — pure dataclasses with
``asdict`` round-trip — so the new control plane can build on them
without dragging in pydantic at the kernel layer.

The shapes here are stable contracts referenced from:

* Agent structured output (``TradePlan``)
* :mod:`nerya.trading.capital` (``OrderCandidate``, ``SizingPolicy``)
* Executors (``ProtectionRule``)
* Risk gate / dashboard rendering

If you change a field, treat it as you would a database migration: add
new optional fields, never silently rename or drop existing ones.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Literal

from ..core.errors import IntentValidationError
from ..core.ids import (
    protection_id as _new_protection_id,
    trade_plan_id as _new_trade_plan_id,
)
from ..core.time import now_iso


# ---------------------------------------------------------------------------
# Sizing policy
# ---------------------------------------------------------------------------

SizingMethod = Literal[
    "fixed_usd",
    "fixed_base",
    "pct_nav",
    "risk_to_stop",
    "volatility_target",
    "target_weight",
    "reduce_pct",
    "close_all",
]


@dataclass
class SizingPolicy:
    """Strategy-declared sizing intent.

    Strategies must not pre-compute a base/quote/usd amount and bury the
    units in a free-form ``size`` field. Instead they describe *how much
    risk to allocate* with one of these methods; the BudgetChecker is
    the only component allowed to translate that into an
    :class:`OrderCandidate` with concrete numbers.
    """

    method: SizingMethod = "fixed_usd"
    fixed_usd: float | None = None
    fixed_base: float | None = None
    pct_nav: float | None = None
    risk_pct_nav: float | None = None
    stop_distance_pct: float | None = None
    target_volatility_pct: float | None = None
    target_weight: float | None = None
    reduce_pct: float | None = None
    max_notional_usd: float | None = None

    def asdict(self) -> dict[str, Any]:
        return {k: v for k, v in asdict(self).items() if v is not None or k == "method"}

    def __post_init__(self) -> None:
        m = self.method
        if m == "fixed_usd" and (self.fixed_usd is None or self.fixed_usd <= 0):
            raise IntentValidationError("fixed_usd sizing requires positive fixed_usd")
        if m == "fixed_base" and (self.fixed_base is None or self.fixed_base <= 0):
            raise IntentValidationError("fixed_base sizing requires positive fixed_base")
        if m == "pct_nav" and (self.pct_nav is None or not 0 < self.pct_nav <= 1):
            raise IntentValidationError("pct_nav sizing requires 0<pct_nav<=1")
        if m == "risk_to_stop":
            if self.risk_pct_nav is None or not 0 < self.risk_pct_nav <= 0.1:
                raise IntentValidationError(
                    "risk_to_stop sizing requires 0<risk_pct_nav<=0.1"
                )
            if self.stop_distance_pct is None or self.stop_distance_pct <= 0:
                raise IntentValidationError(
                    "risk_to_stop sizing requires positive stop_distance_pct"
                )
        if m == "reduce_pct" and (self.reduce_pct is None or not 0 < self.reduce_pct <= 1):
            raise IntentValidationError("reduce_pct sizing requires 0<reduce_pct<=1")
        if m == "target_weight" and (
            self.target_weight is None or not -1 <= self.target_weight <= 1
        ):
            raise IntentValidationError(
                "target_weight sizing requires -1<=target_weight<=1"
            )


# ---------------------------------------------------------------------------
# Protection rule (TP/SL/trailing/time-limit)
# ---------------------------------------------------------------------------

ProtectionMode = Literal["hard_exchange", "soft_runtime", "hybrid"]
ProtectionStatus = Literal[
    "pending",
    "armed",
    "exchange_armed",
    "triggered",
    "released",
    "failed",
]


@dataclass
class StopLossSpec:
    type: Literal["pct", "price", "atr", "pnl_usd"] = "pct"
    value: float = 0.0

    def __post_init__(self) -> None:
        if self.type == "pct" and not 0 < self.value < 1:
            raise IntentValidationError("stop_loss pct must be in (0,1)")
        if self.type in ("price", "atr", "pnl_usd") and self.value <= 0:
            raise IntentValidationError(f"stop_loss {self.type} must be > 0")


@dataclass
class TakeProfitSpec:
    type: Literal["pct", "price", "r_multiple", "pnl_usd"] = "pct"
    value: float = 0.0

    def __post_init__(self) -> None:
        if self.type == "pct" and self.value <= 0:
            raise IntentValidationError("take_profit pct must be > 0")
        if self.type in ("price", "r_multiple", "pnl_usd") and self.value <= 0:
            raise IntentValidationError(f"take_profit {self.type} must be > 0")


@dataclass
class TrailingStopSpec:
    activation_pct: float = 0.0
    trail_pct: float = 0.0

    def __post_init__(self) -> None:
        if self.activation_pct < 0 or self.trail_pct <= 0:
            raise IntentValidationError(
                "trailing stop requires activation_pct>=0 and trail_pct>0"
            )


@dataclass
class PartialExitSpec:
    trigger_pct: float
    close_pct: float

    def __post_init__(self) -> None:
        if not 0 < self.trigger_pct:
            raise IntentValidationError("partial exit trigger_pct must be > 0")
        if not 0 < self.close_pct <= 1:
            raise IntentValidationError("partial exit close_pct must be in (0,1]")


@dataclass
class ProtectionRule:
    """Protection plan attached to an open position.

    Each open position must have at most one active rule. The rule is
    interpreted by the :class:`PositionProtectionExecutor`; ``mode``
    decides whether the executor pushes the orders to the exchange,
    monitors them in-process, or both.
    """

    protection_id: str = field(default_factory=_new_protection_id)
    position_id: str = ""
    executor_id: str = ""
    strategy_id: str = ""
    account_id: str = ""
    market: str = ""
    side: Literal["long", "short"] = "long"
    mode: ProtectionMode = "soft_runtime"
    stop_loss: StopLossSpec | None = None
    take_profit: TakeProfitSpec | None = None
    time_limit_sec: int | None = None
    trailing_stop: TrailingStopSpec | None = None
    partial_exits: list[PartialExitSpec] = field(default_factory=list)
    status: ProtectionStatus = "pending"
    trigger_source: Literal["mark", "last", "bid_ask", "candle_close"] = "mark"
    exchange_order_ids: dict[str, str] = field(default_factory=dict)
    created_at: str = field(default_factory=now_iso)
    updated_at: str = field(default_factory=now_iso)
    triggered_at: str | None = None
    triggered_kind: str | None = None  # take_profit | stop_loss | trailing_stop | time_limit
    notes: str = ""

    def __post_init__(self) -> None:
        if self.side not in ("long", "short"):
            raise IntentValidationError("protection.side must be long|short")
        if (
            self.stop_loss is None
            and self.take_profit is None
            and self.time_limit_sec is None
            and self.trailing_stop is None
            and not self.partial_exits
        ):
            raise IntentValidationError(
                "protection rule must declare at least one of stop_loss/take_profit/"
                "time_limit_sec/trailing_stop/partial_exits"
            )
        if self.time_limit_sec is not None and self.time_limit_sec <= 0:
            raise IntentValidationError("time_limit_sec must be > 0")

    def asdict(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "protection_id": self.protection_id,
            "position_id": self.position_id,
            "executor_id": self.executor_id,
            "strategy_id": self.strategy_id,
            "account_id": self.account_id,
            "market": self.market,
            "side": self.side,
            "mode": self.mode,
            "status": self.status,
            "trigger_source": self.trigger_source,
            "time_limit_sec": self.time_limit_sec,
            "exchange_order_ids": dict(self.exchange_order_ids),
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "triggered_at": self.triggered_at,
            "triggered_kind": self.triggered_kind,
            "notes": self.notes,
        }
        if self.stop_loss is not None:
            out["stop_loss"] = asdict(self.stop_loss)
        if self.take_profit is not None:
            out["take_profit"] = asdict(self.take_profit)
        if self.trailing_stop is not None:
            out["trailing_stop"] = asdict(self.trailing_stop)
        if self.partial_exits:
            out["partial_exits"] = [asdict(p) for p in self.partial_exits]
        return out


# ---------------------------------------------------------------------------
# Order candidate (post BudgetChecker)
# ---------------------------------------------------------------------------


@dataclass
class OrderCandidate:
    """A concrete, sized order plan that BudgetChecker has already vetted.

    This is the bridge object between sizing policy and the executor.
    Once an :class:`OrderCandidate` exists it carries enough information
    to:

    * Place the order with a CCXT/native connector.
    * Drive the durable :class:`OrderTracker`.
    * Produce a ``CapitalReservation`` with the right collateral.
    * Be replayed in paper / shadow / canary modes deterministically.
    """

    account_id: str
    strategy_id: str
    market: str
    side: Literal["buy", "sell"]
    order_type: Literal["market", "limit", "stop", "stop_limit"] = "market"
    size_base: float | None = None
    notional_usd: float = 0.0
    price: float | None = None
    leverage: float = 1.0
    reduce_only: bool = False
    time_in_force: Literal["gtc", "ioc", "fok", "post_only"] = "gtc"
    estimated_fee_usd: float = 0.0
    estimated_slippage_bps: float = 0.0
    required_collateral: dict[str, float] = field(default_factory=dict)
    expected_returns: dict[str, float] = field(default_factory=dict)
    resized: bool = False
    resize_reason: str | None = None
    rejection_reason: str | None = None
    intent_id: str = ""
    plan_id: str = ""
    risk_evaluation_id: str = ""
    reservation_id: str = ""
    executor_id: str = ""
    client_order_id: str = ""
    meta: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.side not in ("buy", "sell"):
            raise IntentValidationError(f"bad side: {self.side!r}")
        if self.notional_usd < 0:
            raise IntentValidationError("notional_usd must be >= 0")
        if self.size_base is not None and self.size_base < 0:
            raise IntentValidationError("size_base must be >= 0")
        if self.order_type in ("limit", "stop_limit") and self.price is None:
            raise IntentValidationError(
                f"{self.order_type} candidate requires a price"
            )
        if self.leverage <= 0:
            raise IntentValidationError("leverage must be > 0")

    def asdict(self) -> dict[str, Any]:
        return asdict(self)


# ---------------------------------------------------------------------------
# Trade plan (Agent / SDK -> Nerya)
# ---------------------------------------------------------------------------


PlanAction = Literal[
    "open_position",
    "close_position",
    "reduce_position",
    "attach_protection",
    "cancel_executor",
    "rebalance",
]


@dataclass
class TradeEntry:
    order_type: Literal["market", "limit"] = "market"
    limit_price: float | None = None
    max_slippage_bps: int = 25
    time_in_force: Literal["gtc", "ioc", "fok", "post_only"] = "gtc"

    def asdict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class TradePlan:
    """Standardised trade plan emitted by Agents and SDK helpers.

    A :class:`TradePlan` is the *only* shape the new control-plane
    accepts at the top of the pipeline. ``submit.py`` translates legacy
    :class:`TradeIntent` payloads into a plan internally so the rest of
    the stack (BudgetChecker, executors, dashboards) can speak a single
    language.
    """

    plan_id: str = field(default_factory=_new_trade_plan_id)
    action: PlanAction = "open_position"
    strategy_id: str = ""
    account_id: str = ""
    market: str = ""
    side: Literal["long", "short", "flat"] = "long"
    sizing: SizingPolicy = field(default_factory=lambda: SizingPolicy(method="fixed_usd", fixed_usd=0.0))
    entry: TradeEntry = field(default_factory=TradeEntry)
    protection: ProtectionRule | None = None
    confidence: float = 0.0
    reasoning_ref: str = ""
    trigger_event_id: str | None = None
    source: Literal[
        "agent",
        "subagent",
        "script",
        "cron",
        "operator",
        "sdk",
        "strategy_runtime",
        "strategy_agent",
        "strategy_triggered_agent",
    ] = "agent"
    intent_id: str = ""  # legacy bridge — set when promoted from a TradeIntent
    created_at: str = field(default_factory=now_iso)
    meta: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.action not in (
            "open_position",
            "close_position",
            "reduce_position",
            "attach_protection",
            "cancel_executor",
            "rebalance",
        ):
            raise IntentValidationError(f"bad action: {self.action!r}")
        if self.action in ("open_position", "reduce_position", "close_position"):
            if not self.market or ":" not in self.market:
                raise IntentValidationError("market must be '<venue>:<symbol>'")
            if self.side not in ("long", "short", "flat"):
                raise IntentValidationError(f"bad side: {self.side!r}")
        if not 0 <= self.confidence <= 1:
            raise IntentValidationError("confidence must be in [0,1]")

    @property
    def buy_or_sell(self) -> Literal["buy", "sell"]:
        """Map a directional plan to a CEX-native ``buy``/``sell``.

        ``open long`` and ``close short`` reduce to ``buy``; ``open
        short`` and ``close long`` reduce to ``sell``. Plans that don't
        carry a direction (e.g. attach_protection) raise — callers must
        not look this up for non-trading actions.
        """
        if self.action in ("open_position",):
            return "buy" if self.side == "long" else "sell"
        if self.action in ("close_position", "reduce_position"):
            return "sell" if self.side == "long" else "buy"
        raise IntentValidationError(
            f"buy_or_sell undefined for action={self.action}"
        )

    def asdict(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "plan_id": self.plan_id,
            "action": self.action,
            "strategy_id": self.strategy_id,
            "account_id": self.account_id,
            "market": self.market,
            "side": self.side,
            "sizing": self.sizing.asdict(),
            "entry": self.entry.asdict(),
            "confidence": self.confidence,
            "reasoning_ref": self.reasoning_ref,
            "trigger_event_id": self.trigger_event_id,
            "source": self.source,
            "intent_id": self.intent_id,
            "created_at": self.created_at,
            "meta": dict(self.meta),
        }
        if self.protection is not None:
            out["protection"] = self.protection.asdict()
        return out


__all__ = [
    "SizingMethod",
    "SizingPolicy",
    "ProtectionMode",
    "ProtectionStatus",
    "StopLossSpec",
    "TakeProfitSpec",
    "TrailingStopSpec",
    "PartialExitSpec",
    "ProtectionRule",
    "OrderCandidate",
    "TradeEntry",
    "TradePlan",
    "PlanAction",
]
