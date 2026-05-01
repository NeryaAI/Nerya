"""Single in-process entrypoint for submitting trade intents.

This module is the *only* place where the full risk → approval →
execution pipeline lives. The native tool
(:func:`nerya.tools.native.trading.trade_intent_submit_handler`),
the Trading SDK (:class:`nerya.sdk.trading_api.TradingAPI`), and the
strategy runtime facade (``ctx.trading.submit_intent`` in
:mod:`nerya.strategies.context`) all delegate here.

The previous implementation duplicated this pipeline across
``trading_skill.submit_trade_intent`` and the native tool handler;
both have been deleted. Everything now flows through:

```text
spec dict
  -> TradeIntent (validate)
  -> open strategy_history session
  -> resolve market snapshot (caller / live / mock / degraded)
  -> RiskGate.evaluate(intent, snapshot)
  -> ApprovalGate.require(intent, risk) for escalations
  -> ExecutionEngine.execute(intent, snapshot)
  -> record orders/fills + journal
```

The function returns a plain ``dict`` envelope so callers can wrap it
in their preferred shape (ToolResult / SDK return / context shim).
"""

from __future__ import annotations

from typing import Any, Optional

from ..core import jsonl
from ..core.config import Config
from ..core.errors import ApprovalPending
from ..core.redaction import redact_dict
from ..core.time import now_iso
from ..core.truth import (
    degraded_envelope,
    live_envelope,
    mock_envelope,
    resolve_allow_mock,
)
from ..strategy_history import open_session, store as history_store, track_outcome
from .account_snapshots import fresh_snapshot
from .accounts import get_account_profile
from .approval import ApprovalGate
from .capital import BudgetChecker, CapitalReservationStore
from .execution import ExecutionEngine
from .executors.orchestrator import ExecutorOrchestrator
from .intents import TradeIntent
from .order_intents import TradePlan
from .risk import RiskGate


_STRATEGY_ORDER_SOURCES = {
    "strategy",
    "strategy_runtime",
    "strategy_agent",
    "strategy_trigger",
    "strategy_triggered_agent",
}

_AUTO_APPROVABLE_STRATEGY_ESCALATIONS = (
    "approval_required_threshold",
    "canary_per_trade_approval_required",
)


def _is_strategy_originated_order(intent: TradeIntent) -> bool:
    source = str(intent.source or "").strip().lower()
    return source in _STRATEGY_ORDER_SOURCES or source.startswith("strategy:")


def _auto_approvable_strategy_escalation(risk) -> bool:
    reasons = [str(r) for r in (getattr(risk, "reasons", None) or []) if str(r) != "ok"]
    if not reasons:
        return False
    return all(
        any(reason.startswith(prefix) for prefix in _AUTO_APPROVABLE_STRATEGY_ESCALATIONS)
        for reason in reasons
    )


def _maybe_auto_approve_strategy_order(
    config: Config,
    intent: TradeIntent,
    risk,
):
    if not bool(config.get("trading.strategy_orders.auto_approve_escalations", True)):
        return None
    if not _is_strategy_originated_order(intent):
        return None
    if not _auto_approvable_strategy_escalation(risk):
        return None
    return ApprovalGate(config).auto_approve(
        intent,
        risk,
        reason="strategy_order_default_auto_approval",
    )


def _approval_summary(record) -> dict[str, Any] | None:
    if record is None:
        return None
    return {
        "approval_id": record.approval_id,
        "state": "auto_approved" if record.state == "approved" else record.state,
        "auto": True,
    }


def submit_trade_intent(
    config: Config,
    *,
    spec: dict[str, Any],
    market_snapshot: Optional[dict[str, Any]] = None,
    default_strategy: str = "manual_agent",
    default_source: str = "agent",
) -> dict[str, Any]:
    """Run the full trade-intent pipeline and return a canonical envelope.

    Parameters
    ----------
    config:
        Workspace ``Config``. Determines paths, accounts, risk policy.
    spec:
        Intent fields (``account_id``, ``market``, ``side``, ``size``,
        ``size_unit``, ``order_type``, …). Either includes an
        ``intent_id`` (for replay) or omits it so a fresh one is
        generated.
    market_snapshot:
        Optional caller-supplied snapshot. When ``None`` we resolve a
        snapshot from the mock exchange (when allowed) or fall back to
        a degraded envelope referencing the intent's ``limit_price``.
    default_strategy:
        Used when the spec does not declare ``strategy_id``.
    default_source:
        Used when the spec does not declare ``source``.

    Returns
    -------
    dict envelope with one of these shapes:

    * ``{"status": "rejected", "order_id": None, "session_id": str,
       "intent": {...}, "risk_decision": {...}}``
    * ``{"status": "pending_approval", "order_id": None,
       "session_id": str, "approval_id": str, "intent": {...},
       "risk_decision": {...}}``
    * ``{"status": "filled" | "partial" | ...,
       "order_id": str, "session_id": str,
       "risk_decision": {...}, "order": {...}}``

    Raises
    ------
    ValueError
        When the intent fails validation. Caller decides whether to
        translate that into a typed error response.
    Exception
        Any exception from :class:`ExecutionEngine`. Caller wraps it.
    """

    payload = dict(spec or {})
    if "intent_id" in payload:
        intent = TradeIntent(**payload)
    else:
        payload.setdefault("strategy_id", default_strategy)
        payload.setdefault("source", default_source)
        intent = TradeIntent.new(**payload)

    paths = config.paths
    session_id = open_session(
        paths,
        intent.strategy_id,
        trigger={
            "intent_id": intent.intent_id,
            "source": intent.source or default_source,
            "trigger_event_id": intent.trigger_event_id,
        },
    )
    history_store.record_trigger(
        paths,
        strategy_id=intent.strategy_id,
        session_id=session_id,
        event={
            "name": "direct_intent",
            "source": intent.source or default_source,
            "payload": {"intent_id": intent.intent_id},
        },
    )
    history_store.record_intent(
        paths,
        strategy_id=intent.strategy_id,
        session_id=session_id,
        intent=intent.asdict(),
    )

    snapshot = _resolve_market_snapshot(
        config, intent, supplied=market_snapshot if isinstance(market_snapshot, dict) else None,
    )

    risk = RiskGate(config).evaluate(intent, market_snapshot=snapshot)
    history_store.record_risk(
        paths,
        strategy_id=intent.strategy_id,
        session_id=session_id,
        decision=risk.asdict(),
    )
    jsonl.append(paths.journal("trading"), {
        "kind": "risk.decision",
        "ts": now_iso(),
        "strategy_id": intent.strategy_id,
        "session_id": session_id,
        "intent_id": intent.intent_id,
        "decision": risk.decision,
        "reasons": risk.reasons,
    })
    history_store.record_decision(
        paths,
        strategy_id=intent.strategy_id,
        session_id=session_id,
        decision={
            "intent_id": intent.intent_id,
            "risk": risk.decision,
            "reasons": risk.reasons,
        },
    )

    approval_record = None
    if risk.decision == "reject":
        return {
            "status": "rejected",
            "order_id": None,
            "session_id": session_id,
            "intent": redact_dict(intent.asdict()),
            "risk_decision": risk.asdict(),
        }

    if risk.decision == "escalate":
        approval_record = _maybe_auto_approve_strategy_order(config, intent, risk)
        if approval_record is None:
            try:
                ApprovalGate(config).require(intent, risk)
            except ApprovalPending as p:
                return {
                    "status": "pending_approval",
                    "order_id": None,
                    "session_id": session_id,
                    "approval_id": p.approval_id,
                    "intent": redact_dict(intent.asdict()),
                    "risk_decision": risk.asdict(),
                }

    # shadow strategies stop here. The intent is fully
    # journaled and the risk decision is persisted, but no order is
    # ever sent (paper or live). This gives operators a side-by-side
    # stream against paper without touching the real-money venue.
    if getattr(risk, "shadow_only", False):
        jsonl.append(paths.journal("trading"), {
            "kind": "shadow.intent",
            "ts": now_iso(),
            "strategy_id": intent.strategy_id,
            "session_id": session_id,
            "intent_id": intent.intent_id,
            "promotion_state": getattr(risk, "promotion_state", "shadow"),
        })
        return {
            "status": "shadow",
            "order_id": None,
            "session_id": session_id,
            "intent": redact_dict(intent.asdict()),
            "risk_decision": risk.asdict(),
            **({"approval": _approval_summary(approval_record)} if approval_record else {}),
        }

    engine = ExecutionEngine(config)
    result = engine.execute(intent, market_snapshot=snapshot)

    history_store.record_order(
        paths,
        strategy_id=intent.strategy_id,
        session_id=session_id,
        payload={
            "order_id": result.order_id,
            "intent_id": intent.intent_id,
            "status": result.status,
            "notional_usd": result.notional_usd,
            "avg_price": result.avg_price,
            "filled_size": result.filled_size,
        },
    )
    jsonl.append(paths.journal("trading"), {
        "kind": "order.placed",
        "ts": now_iso(),
        "strategy_id": intent.strategy_id,
        "session_id": session_id,
        "intent_id": intent.intent_id,
        "order_id": result.order_id,
        "status": result.status,
        "notional_usd": result.notional_usd,
        "avg_price": result.avg_price,
        "filled_size": result.filled_size,
    })
    for f in result.fills:
        history_store.record_fill(
            paths,
            strategy_id=intent.strategy_id,
            session_id=session_id,
            fill={
                "order_id": f.order_id,
                "fill_id": f.fill_id,
                "intent_id": f.intent_id,
                "market": f.market,
                "price": f.price,
                "size": f.size,
                "fee_usd": f.fee_usd,
                "ts": f.ts,
            },
        )
    track_outcome(paths, intent.strategy_id, session_id)

    response = {
        "status": result.status,
        "order_id": result.order_id,
        "session_id": session_id,
        "risk_decision": risk.asdict(),
        "order": result.asdict(),
    }
    if approval_record is not None:
        response["approval"] = _approval_summary(approval_record)
    return response


# --------------------------------------------------------------------------
# Snapshot resolver (mirrors the native-tool helper one-to-one)
# --------------------------------------------------------------------------


def _resolve_market_snapshot(
    config: Config,
    intent: TradeIntent,
    *,
    supplied: Optional[dict[str, Any]],
) -> dict[str, Any]:
    venue_hint = intent.market.split(":", 1)[0].lower() if ":" in intent.market else ""
    if isinstance(supplied, dict) and supplied:
        snap = dict(supplied)
        if "_envelope" not in snap:
            snap["_envelope"] = live_envelope(
                source=str(snap.get("source", venue_hint or "caller")),
                venue=venue_hint,
            ).as_dict()
        return snap
    if resolve_allow_mock(None, config):
        try:
            from ..connectors.mock_exchange import MockExchange

            tk = MockExchange().get_ticker(intent.market)
            return {
                "price": float(tk.mid),
                "age_s": 0,
                "_envelope": mock_envelope(source="mock", venue=venue_hint).as_dict(),
            }
        except Exception:
            pass
    return {
        "price": intent.limit_price or 0.0,
        "age_s": 0,
        "_envelope": degraded_envelope(
            "market_snapshot",
            error="no_live_snapshot_supplied",
            venue=venue_hint,
        ).as_dict(),
    }


def submit_trade_plan(
    config: Config,
    plan: TradePlan,
    *,
    market_snapshot: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    """Run a :class:`TradePlan` through the new control-plane.

    04-29 §3 / §4 / §6 — the full pipeline:

    1. Translate the plan into a legacy :class:`TradeIntent` for the
       existing :class:`RiskGate`. This preserves every existing
       guard (kill switch, account/strategy state, market allow-list,
       confidence floor, dedupe, approvals).
    2. If RiskGate allows, run the new :class:`BudgetChecker` to
       size the order against the latest :class:`AccountSnapshot`
       and outstanding reservations. The verdict is allow / resize /
       reject / escalate.
    3. On allow / resize, write a :class:`CapitalReservation` and
       create a :class:`MarketOrderExecutor` via the orchestrator.
    4. Drive the executor until terminal (paper modes finish in one
       tick; live executors run for as many ticks as the orchestrator
       cap allows, then the orchestrator polling loop continues from
       where we left off).
    """

    paths = config.paths
    intent = _plan_to_intent(plan)
    session_id = open_session(
        paths,
        intent.strategy_id,
        trigger={
            "intent_id": intent.intent_id,
            "plan_id": plan.plan_id,
            "source": intent.source or plan.source,
            "trigger_event_id": intent.trigger_event_id,
        },
    )
    history_store.record_trigger(
        paths,
        strategy_id=intent.strategy_id,
        session_id=session_id,
        event={
            "name": "trade_plan",
            "source": intent.source or plan.source,
            "payload": {"intent_id": intent.intent_id, "plan_id": plan.plan_id},
        },
    )
    history_store.record_intent(
        paths,
        strategy_id=intent.strategy_id,
        session_id=session_id,
        intent={**intent.asdict(), "plan_id": plan.plan_id},
    )

    snapshot = _resolve_market_snapshot(
        config, intent, supplied=market_snapshot if isinstance(market_snapshot, dict) else None,
    )

    risk = RiskGate(config).evaluate(intent, market_snapshot=snapshot)
    history_store.record_risk(
        paths,
        strategy_id=intent.strategy_id,
        session_id=session_id,
        decision=risk.asdict(),
    )

    if risk.decision == "reject":
        return {
            "status": "rejected",
            "session_id": session_id,
            "plan_id": plan.plan_id,
            "intent": redact_dict(intent.asdict()),
            "risk_decision": risk.asdict(),
        }
    approval_record = None
    if risk.decision == "escalate":
        approval_record = _maybe_auto_approve_strategy_order(config, intent, risk)
        if approval_record is None:
            try:
                ApprovalGate(config).require(intent, risk)
            except ApprovalPending as p:
                return {
                    "status": "pending_approval",
                    "session_id": session_id,
                    "plan_id": plan.plan_id,
                    "approval_id": p.approval_id,
                    "intent": redact_dict(intent.asdict()),
                    "risk_decision": risk.asdict(),
                }

    # shadow strategies stop before capital reservation
    # and executor creation. The intent is journalled and the risk
    # decision (built against a real-money snapshot) is persisted so
    # the dashboard can compare shadow intents against the eventual
    # canary execution side-by-side.
    if getattr(risk, "shadow_only", False):
        jsonl.append(paths.journal("trading"), {
            "kind": "shadow.plan",
            "ts": now_iso(),
            "strategy_id": plan.strategy_id,
            "session_id": session_id,
            "plan_id": plan.plan_id,
            "intent_id": intent.intent_id,
            "promotion_state": getattr(risk, "promotion_state", "shadow"),
        })
        return {
            "status": "shadow",
            "session_id": session_id,
            "plan_id": plan.plan_id,
            "intent": redact_dict(intent.asdict()),
            "risk_decision": risk.asdict(),
            **({"approval": _approval_summary(approval_record)} if approval_record else {}),
        }

    # Budget check + reservation.
    profile = get_account_profile(paths, plan.account_id)
    snap = fresh_snapshot(config, plan.account_id, profile=profile)
    store = CapitalReservationStore(paths)
    checker = BudgetChecker(profile=profile, snapshot=snap, store=store)
    mark_price = (snapshot or {}).get("price") or plan.entry.limit_price
    side = plan.buy_or_sell if plan.action != "attach_protection" else "buy"
    decision = checker.evaluate(
        plan_strategy_id=plan.strategy_id,
        market=plan.market,
        side=side,
        sizing=plan.sizing,
        mark_price=float(mark_price) if mark_price else None,
        order_type=plan.entry.order_type,
        time_in_force=plan.entry.time_in_force,
        intent_id=intent.intent_id,
        plan_id=plan.plan_id,
        risk_evaluation_id=risk.risk_evaluation_id,
    )

    if decision.verdict == "reject":
        return {
            "status": "rejected",
            "session_id": session_id,
            "plan_id": plan.plan_id,
            "intent": redact_dict(intent.asdict()),
            "risk_decision": risk.asdict(),
            "budget_decision": decision.asdict(),
        }

    candidate = decision.candidate
    reservation = store.reserve(
        account_id=candidate.account_id,
        strategy_id=candidate.strategy_id,
        market=candidate.market,
        side=candidate.side,
        notional_usd=candidate.notional_usd,
        estimated_fee_usd=candidate.estimated_fee_usd,
        estimated_margin_usd=float(
            (candidate.required_collateral or {}).get(profile.base_currency.upper(), 0.0)
        ),
        risk_evaluation_id=risk.risk_evaluation_id,
        intent_id=intent.intent_id,
        plan_id=plan.plan_id,
    )
    candidate.reservation_id = reservation.reservation_id

    orchestrator = ExecutorOrchestrator(config)
    executor = orchestrator.create_market_order(
        candidate=candidate,
        intent_id=intent.intent_id,
        plan_id=plan.plan_id,
        protection=plan.protection,
    )
    store.attach_executor(reservation.reservation_id, executor.run.executor_id)
    run = orchestrator.run_until_terminal(executor)

    jsonl.append(paths.journal("trading"), {
        "kind": "executor.completed" if run.is_terminal else "executor.in_progress",
        "ts": now_iso(),
        "strategy_id": plan.strategy_id,
        "session_id": session_id,
        "plan_id": plan.plan_id,
        "intent_id": intent.intent_id,
        "executor_id": run.executor_id,
        "state": run.state,
        "close_type": run.close_type,
        "result": run.result_json,
    })
    track_outcome(paths, intent.strategy_id, session_id)

    status_map = {
        "done": "filled",
        "failed": "failed",
        "canceled": "canceled",
        "rejected": "rejected",
    }
    response_status = status_map.get(run.state, run.state)
    response = {
        "status": response_status,
        "session_id": session_id,
        "plan_id": plan.plan_id,
        "executor_id": run.executor_id,
        "intent": redact_dict(intent.asdict()),
        "risk_decision": risk.asdict(),
        "budget_decision": decision.asdict(),
        "reservation_id": reservation.reservation_id,
        "executor": {
            "state": run.state,
            "close_type": run.close_type,
            "result": run.result_json,
            "order_ids": run.order_ids,
        },
    }
    if approval_record is not None:
        response["approval"] = _approval_summary(approval_record)
    return response


def _plan_to_intent(plan: TradePlan) -> TradeIntent:
    """Bridge a :class:`TradePlan` into a legacy :class:`TradeIntent`.

    The intent only needs to be good enough to drive RiskGate. Sizing
    is handled by the new BudgetChecker downstream — we feed RiskGate
    a notional estimate so the dedupe + cap checks still bite.

    We also stamp ``protection_present`` into ``intent.meta`` so the
    canary risk hook can reject opens that ship without
    a protection rule.
    """
    side = plan.buy_or_sell if plan.action in (
        "open_position", "close_position", "reduce_position"
    ) else "buy"
    estimated_notional = (
        float(plan.sizing.fixed_usd or 0.0)
        if plan.sizing.method == "fixed_usd"
        else 0.0
    )
    protection_present = bool(plan.protection)
    payload = {
        "strategy_id": plan.strategy_id,
        "account_id": plan.account_id,
        "market": plan.market,
        "side": side,
        "size": estimated_notional or 0.0,
        "size_unit": "usd",
        "order_type": plan.entry.order_type,
        "limit_price": plan.entry.limit_price,
        "time_in_force": plan.entry.time_in_force,
        "confidence": plan.confidence,
        "reasoning": plan.reasoning_ref,
        "source": plan.source if plan.source in (
            "agent",
            "subagent",
            "script",
            "cron",
            "strategy_runtime",
            "strategy_agent",
            "strategy_triggered_agent",
        ) else "agent",
        "trigger_event_id": plan.trigger_event_id,
        "meta": {
            "plan_id": plan.plan_id,
            "protection_present": protection_present,
            **dict(plan.meta or {}),
        },
    }
    if plan.intent_id:
        payload["intent_id"] = plan.intent_id
        return TradeIntent(**payload)
    return TradeIntent.new(**payload)


__all__ = ["submit_trade_intent", "submit_trade_plan"]
