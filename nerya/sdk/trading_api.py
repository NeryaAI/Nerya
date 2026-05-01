"""Trading SDK surface.

04-29 §11 P6 — Agents and strategies should express trading
intent in *typed plans*, not bespoke order dicts. This module is the
canonical SDK contract: every helper here translates the caller's
intent into a :class:`TradePlan` (or related schema) and forwards it
through exactly the same risk/approval/budget pipeline as a manual CLI
submit.

The high-level methods cover the full strategy lifecycle:

| Method                | Purpose                                       |
|-----------------------|-----------------------------------------------|
| :meth:`signal`        | Journal an analysis signal (no orders).       |
| :meth:`open_position` | Open a new position with sizing + protection. |
| :meth:`close_position`| Close (fully) an existing position.           |
| :meth:`reduce_position`| Trim an open position by % or absolute size. |
| :meth:`attach_protection` | Bind a protection rule to a position.     |
| :meth:`cancel_executor`| Cancel a running executor.                   |
| :meth:`portfolio_snapshot` | Operator-readable account+position view. |
| :meth:`risk_preview`  | Dry-run RiskGate on a plan/intent without commit. |

Every method routes through ``nerya.trading.submit`` (same module that
backs the legacy :func:`submit_trade_intent`) so:

* Risk gate, approval gate, capital reservation, executor orchestrator
  are *always* applied.
* Live trading switch + kill switch + dedupe still bite.
* Audit trail is identical to a manual CLI submit.

The legacy :meth:`submit_intent` is preserved for old callers — it
delegates to the same skill kernel route it always has.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Literal

from ..core.config import Config
from ..core.errors import ApprovalPending
from ..skills.kernel import SkillKernel
from ..trading.order_intents import (
    PartialExitSpec,
    ProtectionRule,
    SizingPolicy,
    StopLossSpec,
    TakeProfitSpec,
    TradeEntry,
    TradePlan,
    TrailingStopSpec,
)

log = logging.getLogger(__name__)


@dataclass
class TradingAPI:
    """High-level SDK wrapper around the trading control plane."""

    config: Config
    skills: SkillKernel

    # ------------------------------------------------------------------
    # Legacy entry points (kept stable for existing callers)
    # ------------------------------------------------------------------

    def submit_intent(self, **payload: Any) -> dict[str, Any]:
        """Legacy raw-intent submit.

        Most callers should prefer :meth:`open_position` /
        :meth:`close_position` instead — they produce typed
        :class:`TradePlan` objects with explicit sizing + protection,
        which the new control plane can reason about.
        """
        return self.skills.call(
            "trading", "submit_trade_intent",
            payload=payload,
            caller=payload.pop("_caller", "sdk"),
            strategy_id=payload.get("strategy_id"),
            session_id=payload.pop("_session_id", None),
            trigger_event_id=payload.pop("_trigger_event_id", None),
        )

    def cancel_order(self, *, strategy_id: str, order_id: str,
                     caller: str = "sdk") -> dict[str, Any]:
        return self.skills.call(
            "trading", "cancel_order",
            payload={"strategy_id": strategy_id, "order_id": order_id},
            caller=caller, strategy_id=strategy_id,
        )

    def get_order_status(self, *, strategy_id: str, order_id: str) -> dict[str, Any]:
        return self.skills.call(
            "trading", "get_order_status",
            payload={"strategy_id": strategy_id, "order_id": order_id},
            caller="sdk", strategy_id=strategy_id,
        )

    def get_strategy_history(self, *, strategy_id: str, limit: int = 20) -> dict[str, Any]:
        return self.skills.call(
            "trading", "get_strategy_history",
            payload={"strategy_id": strategy_id, "limit": limit},
            caller="sdk", strategy_id=strategy_id,
        )

    # ------------------------------------------------------------------
    # New control-plane methods
    # ------------------------------------------------------------------

    def signal(
        self,
        *,
        strategy_id: str,
        market: str,
        signal_kind: str,
        confidence: float,
        reasoning_ref: str = "",
        payload: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Journal a non-trading analysis signal.

        Useful for "I see X, but I'm not sizing yet" moments — the
        signal is recorded in the strategy's session journal and shows
        up on the dashboard alongside the actual orders, so the
        reasoning trail is intact even when no trade is placed.
        """
        from ..core import jsonl
        from ..core.time import now_iso

        record = {
            "kind": "agent.signal",
            "ts": now_iso(),
            "strategy_id": strategy_id,
            "market": market,
            "signal_kind": str(signal_kind),
            "confidence": float(confidence),
            "reasoning_ref": str(reasoning_ref or ""),
            "payload": dict(payload or {}),
        }
        jsonl.append(self.config.paths.journal("trading"), record)
        return {"status": "recorded", **record}

    def open_position(
        self,
        *,
        strategy_id: str,
        account_id: str,
        market: str,
        side: Literal["long", "short"],
        sizing: SizingPolicy | dict[str, Any],
        entry: TradeEntry | dict[str, Any] | None = None,
        protection: ProtectionRule | dict[str, Any] | None = None,
        confidence: float = 0.0,
        reasoning_ref: str = "",
        trigger_event_id: str | None = None,
        source: Literal["agent", "subagent", "script", "cron", "operator", "sdk"] = "sdk",
        market_snapshot: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Open a new position via the control-plane TradePlan pipeline."""

        plan = self._build_plan(
            action="open_position",
            strategy_id=strategy_id,
            account_id=account_id,
            market=market,
            side=side,
            sizing=sizing,
            entry=entry,
            protection=protection,
            confidence=confidence,
            reasoning_ref=reasoning_ref,
            trigger_event_id=trigger_event_id,
            source=source,
        )
        return self._submit(plan, market_snapshot=market_snapshot)

    def close_position(
        self,
        *,
        strategy_id: str,
        account_id: str,
        market: str,
        side: Literal["long", "short"],
        entry: TradeEntry | dict[str, Any] | None = None,
        confidence: float = 0.0,
        reasoning_ref: str = "",
        source: Literal["agent", "subagent", "script", "cron", "operator", "sdk"] = "sdk",
        market_snapshot: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Fully close an existing position.

        Sizing is fixed to ``close_all`` so callers cannot accidentally
        leave a partial dust position behind. Use :meth:`reduce_position`
        for partial trims.
        """
        plan = self._build_plan(
            action="close_position",
            strategy_id=strategy_id,
            account_id=account_id,
            market=market,
            side=side,
            sizing=SizingPolicy(method="close_all"),
            entry=entry,
            confidence=confidence,
            reasoning_ref=reasoning_ref,
            source=source,
        )
        return self._submit(plan, market_snapshot=market_snapshot)

    def reduce_position(
        self,
        *,
        strategy_id: str,
        account_id: str,
        market: str,
        side: Literal["long", "short"],
        reduce_pct: float | None = None,
        fixed_base: float | None = None,
        entry: TradeEntry | dict[str, Any] | None = None,
        confidence: float = 0.0,
        reasoning_ref: str = "",
        source: Literal["agent", "subagent", "script", "cron", "operator", "sdk"] = "sdk",
        market_snapshot: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Reduce an existing position by a percentage or absolute base size."""

        if reduce_pct is None and fixed_base is None:
            raise ValueError("reduce_position requires reduce_pct or fixed_base")
        if reduce_pct is not None:
            sizing = SizingPolicy(method="reduce_pct", reduce_pct=float(reduce_pct))
        else:
            sizing = SizingPolicy(method="fixed_base", fixed_base=float(fixed_base or 0.0))
        plan = self._build_plan(
            action="reduce_position",
            strategy_id=strategy_id,
            account_id=account_id,
            market=market,
            side=side,
            sizing=sizing,
            entry=entry,
            confidence=confidence,
            reasoning_ref=reasoning_ref,
            source=source,
        )
        return self._submit(plan, market_snapshot=market_snapshot)

    def attach_protection(
        self,
        *,
        strategy_id: str,
        account_id: str,
        position_id: str,
        market: str,
        side: Literal["buy", "sell"],
        stop_loss: StopLossSpec | dict[str, Any] | None = None,
        take_profit: TakeProfitSpec | dict[str, Any] | None = None,
        trailing_stop: TrailingStopSpec | dict[str, Any] | None = None,
        partial_exits: list[PartialExitSpec | dict[str, Any]] | None = None,
        time_limit_sec: int | None = None,
        mode: Literal["soft", "hard", "advisory"] = "soft",
    ) -> dict[str, Any]:
        """Attach (or replace) a protection rule on an open position."""

        rule = ProtectionRule(
            position_id=position_id,
            strategy_id=strategy_id,
            account_id=account_id,
            market=market,
            side=side,
            mode=mode,
            stop_loss=_coerce_stop_loss(stop_loss),
            take_profit=_coerce_take_profit(take_profit),
            trailing_stop=_coerce_trailing_stop(trailing_stop),
            partial_exits=[_coerce_partial(p) for p in (partial_exits or [])],
            time_limit_sec=time_limit_sec,
        )
        from ..trading.protection_store import ProtectionStore

        store = ProtectionStore(self.config.paths)
        store.upsert(rule)
        return {
            "status": "attached",
            "protection_id": rule.protection_id,
            "rule": rule.asdict(),
        }

    def cancel_executor(self, *, executor_id: str) -> dict[str, Any]:
        """Cancel a running executor (best-effort)."""
        from ..trading.executors import ExecutorOrchestrator

        orchestrator = ExecutorOrchestrator(self.config)
        result = orchestrator.cancel(executor_id)
        return {"status": "canceled" if result else "not_found", "executor_id": executor_id}

    def portfolio_snapshot(
        self,
        *,
        account_id: str | None = None,
    ) -> dict[str, Any]:
        """Return account-level health + open positions + reservations."""
        from ..trading.account_snapshots import latest_snapshot, latest_snapshots
        from ..trading.accounts import (
            get_account_profile,
            load_account_profiles,
        )
        from ..trading.capital import CapitalReservationStore
        from ..trading.executors import ExecutorOrchestrator
        from ..trading.position_book import PositionBook
        from ..trading.protection_store import ProtectionStore

        if account_id:
            profiles = {account_id: get_account_profile(self.config.paths, account_id)}
        else:
            profiles = load_account_profiles(self.config.paths)

        out_accounts = []
        for aid, profile in profiles.items():
            snap = latest_snapshot(self.config.paths, aid) if account_id else \
                   latest_snapshots(self.config.paths).get(aid)
            entry: dict[str, Any] = {
                "account_id": aid,
                "mode": profile.mode,
                "venue": profile.venue,
                "snapshot": snap.asdict() if snap is not None else None,
            }
            try:
                reservations = CapitalReservationStore(self.config.paths).total_blocked_usd(aid)
                entry["reserved_usd"] = float(reservations)
            except Exception:
                entry["reserved_usd"] = 0.0
            out_accounts.append(entry)

        positions = []
        try:
            book = PositionBook(self.config.paths)
            for p in book.open_positions(account_id=account_id):
                positions.append(p.asdict() if hasattr(p, "asdict") else {
                    "position_id": p.position_id,
                    "market": p.market,
                    "side": p.side,
                    "size_base": p.size_base,
                    "avg_entry_price": p.avg_entry_price,
                    "account_id": p.account_id,
                    "strategy_id": p.strategy_id,
                })
        except Exception:
            pass

        protections = []
        try:
            for r in ProtectionStore(self.config.paths).list_active(account_id=account_id):
                protections.append(r.asdict() if hasattr(r, "asdict") else {})
        except Exception:
            pass

        executors = []
        try:
            for run in ExecutorOrchestrator(self.config).list_active():
                executors.append({
                    "executor_id": run.executor_id,
                    "kind": run.kind,
                    "state": run.state,
                    "account_id": run.account_id,
                    "strategy_id": run.strategy_id,
                    "market": run.market,
                })
        except Exception:
            pass

        return {
            "accounts": out_accounts,
            "open_positions": positions,
            "protections": protections,
            "active_executors": executors,
        }

    def risk_preview(
        self,
        *,
        plan: TradePlan | dict[str, Any] | None = None,
        intent: dict[str, Any] | None = None,
        market_snapshot: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Run RiskGate against a plan/intent without committing.

        acceptance #4 — SDK and direct ``submit_trade_intent``
        produce the same RiskGate verdict for equivalent payloads. This
        method exposes that verdict to the caller for "what-if" UX
        without persisting the intent or sending an order.
        """
        if plan is None and intent is None:
            raise ValueError("risk_preview requires plan or intent")

        from ..trading.intents import TradeIntent
        from ..trading.risk import RiskGate

        if plan is not None:
            from ..trading.submit import _plan_to_intent
            tp = plan if isinstance(plan, TradePlan) else _plan_from_dict(plan)
            ti = _plan_to_intent(tp)
        else:
            ti = TradeIntent(**dict(intent or {})) if "intent_id" in (intent or {}) \
                 else TradeIntent.new(**dict(intent or {}))

        decision = RiskGate(self.config).evaluate(ti, market_snapshot=market_snapshot)
        return {
            "intent": ti.asdict(),
            "risk_decision": decision.asdict(),
        }

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _submit(
        self,
        plan: TradePlan,
        *,
        market_snapshot: dict[str, Any] | None,
    ) -> dict[str, Any]:
        """Run a TradePlan through the canonical submit pipeline."""
        from ..trading.submit import submit_trade_plan

        try:
            envelope = submit_trade_plan(
                self.config,
                plan,
                market_snapshot=market_snapshot,
            )
        except ApprovalPending as p:
            envelope = {
                "status": "pending_approval",
                "approval_id": p.approval_id,
                "plan_id": plan.plan_id,
            }
        return envelope

    def _build_plan(
        self,
        *,
        action: str,
        strategy_id: str,
        account_id: str,
        market: str,
        side: Literal["long", "short"],
        sizing: SizingPolicy | dict[str, Any],
        entry: TradeEntry | dict[str, Any] | None,
        protection: ProtectionRule | dict[str, Any] | None = None,
        confidence: float = 0.0,
        reasoning_ref: str = "",
        trigger_event_id: str | None = None,
        source: str = "sdk",
    ) -> TradePlan:
        plan = TradePlan(
            action=action,  # type: ignore[arg-type]
            strategy_id=strategy_id,
            account_id=account_id,
            market=market,
            side=side,
            sizing=sizing if isinstance(sizing, SizingPolicy) else SizingPolicy(**dict(sizing)),
            entry=entry if isinstance(entry, TradeEntry) else (
                TradeEntry(**dict(entry)) if entry else TradeEntry()
            ),
            protection=_coerce_protection(protection),
            confidence=float(confidence),
            reasoning_ref=str(reasoning_ref or ""),
            trigger_event_id=trigger_event_id,
            source=source,  # type: ignore[arg-type]
        )
        return plan


# ---------------------------------------------------------------------------
# Coercion helpers — accept dicts so non-Python callers (LLM tool
# arguments, JSON CLI input) don't have to import the dataclasses.
# ---------------------------------------------------------------------------


def _coerce_protection(p: ProtectionRule | dict[str, Any] | None) -> ProtectionRule | None:
    if p is None or isinstance(p, ProtectionRule):
        return p
    d = dict(p)
    return ProtectionRule(
        position_id=str(d.get("position_id") or ""),
        strategy_id=str(d.get("strategy_id") or ""),
        account_id=str(d.get("account_id") or ""),
        market=str(d.get("market") or ""),
        side=d.get("side") or "buy",  # type: ignore[arg-type]
        mode=d.get("mode") or "soft",  # type: ignore[arg-type]
        stop_loss=_coerce_stop_loss(d.get("stop_loss")),
        take_profit=_coerce_take_profit(d.get("take_profit")),
        trailing_stop=_coerce_trailing_stop(d.get("trailing_stop")),
        partial_exits=[_coerce_partial(x) for x in (d.get("partial_exits") or [])],
        time_limit_sec=d.get("time_limit_sec"),
    )


def _coerce_stop_loss(x: StopLossSpec | dict[str, Any] | None) -> StopLossSpec | None:
    if x is None or isinstance(x, StopLossSpec):
        return x
    return StopLossSpec(**dict(x))


def _coerce_take_profit(x: TakeProfitSpec | dict[str, Any] | None) -> TakeProfitSpec | None:
    if x is None or isinstance(x, TakeProfitSpec):
        return x
    return TakeProfitSpec(**dict(x))


def _coerce_trailing_stop(x: TrailingStopSpec | dict[str, Any] | None) -> TrailingStopSpec | None:
    if x is None or isinstance(x, TrailingStopSpec):
        return x
    return TrailingStopSpec(**dict(x))


def _coerce_partial(x: PartialExitSpec | dict[str, Any]) -> PartialExitSpec:
    if isinstance(x, PartialExitSpec):
        return x
    return PartialExitSpec(**dict(x))


def _plan_from_dict(d: dict[str, Any]) -> TradePlan:
    """Best-effort reconstruction of a :class:`TradePlan` from a dict."""
    sizing_raw = d.get("sizing") or {}
    entry_raw = d.get("entry") or {}
    return TradePlan(
        plan_id=str(d.get("plan_id") or "") or TradePlan().plan_id,
        action=d.get("action") or "open_position",  # type: ignore[arg-type]
        strategy_id=str(d.get("strategy_id") or ""),
        account_id=str(d.get("account_id") or ""),
        market=str(d.get("market") or ""),
        side=d.get("side") or "long",  # type: ignore[arg-type]
        sizing=SizingPolicy(**dict(sizing_raw)),
        entry=TradeEntry(**dict(entry_raw)),
        protection=_coerce_protection(d.get("protection")),
        confidence=float(d.get("confidence") or 0.0),
        reasoning_ref=str(d.get("reasoning_ref") or ""),
        trigger_event_id=d.get("trigger_event_id"),
        source=d.get("source") or "sdk",  # type: ignore[arg-type]
    )
