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

import logging
import time
from dataclasses import replace
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
from ..messaging.trade_notifications import broadcast_trade_event, event_from_order_result
from ..strategy_history import open_session, store as history_store, track_outcome
from .account_snapshots import capture_snapshot, fresh_snapshot
from .accounts import get_account_profile
from .approval import ApprovalGate
from .capital import BudgetChecker, CapitalReservationStore
from .executors.orchestrator import ExecutorOrchestrator
from .intents import TradeIntent
from .order_intents import SizingPolicy, TradePlan
from .position_book import PositionBook
from .risk import RiskGate

log = logging.getLogger(__name__)


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
    if str(getattr(risk, "promotion_state", "") or "").strip().lower() in {
        "canary",
        "live",
    }:
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


def _real_money_execution_blocker(config: Config, profile) -> str | None:
    if not bool(getattr(profile, "is_real_money", False)):
        return None
    if config.kill_switch():
        return "kill_switch_enabled"
    if not config.live_trading_enabled():
        return "live_trading_disabled_runtime"
    if not bool(getattr(profile, "can_place_order", False)):
        return "account_cannot_place_order"
    return None


def submit_trade_intent(
    config: Config,
    *,
    spec: dict[str, Any],
    market_snapshot: Optional[dict[str, Any]] = None,
    default_strategy: str = "manual_agent",
    default_source: str = "agent",
) -> dict[str, Any]:
    """Run the full trade-intent pipeline and return a canonical envelope.

    This is now a thin adapter over :func:`submit_trade_plan`: the
    intent is translated into a :class:`TradePlan` and flows through the
    same unified pipeline (Risk Gate → BudgetChecker → ApprovalGate →
    executor) as the plan path. That means *every* order — legacy intent
    or new plan — now goes through the same guards:

    * account ``place_order`` permission check
      (:func:`_real_money_execution_blocker`)
    * :class:`BudgetChecker` sizing against the live snapshot
    * :class:`CapitalReservationStore` reservation
    * durable executor with atomic fill → PositionBook → protection

    Previously the legacy path jumped straight from the Risk Gate to
    :class:`ExecutionEngine.execute`, bypassing the permission and
    budget checks — an account with ``place_order: false`` could still
    trade via this path. That bypass is now closed.

    The return envelope keeps its historical shape so native tools, the
    SDK, and ``ctx.trading.submit_intent`` callers are unaffected.
    """

    payload = dict(spec or {})
    if "intent_id" in payload:
        intent = TradeIntent(**payload)
    else:
        payload.setdefault("strategy_id", default_strategy)
        payload.setdefault("source", default_source)
        intent = TradeIntent.new(**payload)

    plan = _intent_to_plan(intent)
    plan_response = submit_trade_plan(config, plan, market_snapshot=market_snapshot)

    # Reshape the plan envelope into the legacy intent envelope so
    # existing callers see the keys they expect (``order_id``,
    # ``order``). The plan envelope carries strictly more information,
    # so we only synthesise the legacy ``order`` summary when the
    # executor produced one.
    status = str(plan_response.get("status") or "")
    response: dict[str, Any] = {
        "status": status,
        "order_id": None,
        "session_id": plan_response.get("session_id"),
        "intent": redact_dict(intent.asdict()),
        "risk_decision": plan_response.get("risk_decision") or {},
    }
    if plan_response.get("approval_id"):
        response["approval_id"] = plan_response["approval_id"]
        response["status"] = "pending_approval"
    if plan_response.get("budget_decision"):
        response["budget_decision"] = plan_response["budget_decision"]
    if plan_response.get("execution_blocker"):
        response["execution_blocker"] = plan_response["execution_blocker"]
    if plan_response.get("approval"):
        response["approval"] = plan_response["approval"]
    if plan_response.get("notifications"):
        response["notifications"] = plan_response["notifications"]
    executor = plan_response.get("executor") or {}
    if executor:
        response["executor_id"] = executor.get("executor_id") or plan_response.get("executor_id")
        response["order_ids"] = executor.get("order_ids") or []
        # Synthesise an ``order`` summary from the executor result so
        # legacy callers that read ``response["order"]`` keep working.
        result = executor.get("result") or {}
        response["order"] = {
            "order_id": (executor.get("order_ids") or [None])[0],
            "intent_id": intent.intent_id,
            "status": status,
            "notional_usd": result.get("notional_usd", 0.0),
            "avg_price": result.get("fill_price", 0.0),
            "filled_size": result.get("size_base", 0.0),
        }
        response["order_id"] = response["order"]["order_id"]
    return response


def _intent_to_plan(intent: TradeIntent) -> TradePlan:
    """Translate a legacy :class:`TradeIntent` into a :class:`TradePlan`.

    The plan carries the same information but in the unified control-plane
    schema so it flows through :func:`submit_trade_plan`. Action is inferred
    from ``meta.plan_action`` when present (set by the resume path) or from
    the side / reduce_only hint, defaulting to ``open_position``.
    """
    from .order_intents import SizingPolicy, TradeEntry, TradePlan

    meta = intent.meta or {}
    plan_action = str(meta.get("plan_action") or "").strip()
    reduce_only = bool(meta.get("reduce_only"))
    if plan_action in ("close_position", "reduce_position", "attach_protection"):
        action = plan_action  # type: ignore[assignment]
    elif reduce_only:
        action = "reduce_position"
    else:
        action = "open_position"

    # Side: the intent is already a CEX-native buy/sell. Map back to the
    # directional long/short the plan expects based on the action.
    if action == "open_position":
        side = "long" if intent.side == "buy" else "short"
    else:  # close/reduce — the strategy's position direction is inferred
        side = "short" if intent.side == "buy" else "long"

    # Sizing policy: intent.size is already a concrete number in a known
    # unit, so we hand the BudgetChecker a fixed value.
    if intent.size_unit == "usd":
        sizing = SizingPolicy(method="fixed_usd", fixed_usd=float(intent.size))
    else:
        sizing = SizingPolicy(method="fixed_base", fixed_base=float(intent.size))

    entry = TradeEntry(
        order_type=intent.order_type if intent.order_type in ("market", "limit") else "market",
        limit_price=intent.limit_price,
        time_in_force=intent.time_in_force,
    )

    # Thread the intent_id back so the resume path and dedupe stay stable.
    plan_meta = {k: v for k, v in meta.items() if k not in ("plan_action",)}
    plan_meta["bridged_from_intent"] = True
    # Preserve the original intent source verbatim (e.g. ``agent:native``)
    # so the approval record and audit trail show exactly what the caller
    # declared. TradePlan.source is a restricted Literal, so we normalise
    # to the closest valid value and stash the original in meta.
    valid_plan_sources = (
        "agent", "subagent", "script", "cron",
        "strategy_runtime", "strategy_agent", "strategy_triggered_agent",
    )
    if intent.source in valid_plan_sources:
        plan_source = intent.source  # type: ignore[assignment]
    else:
        plan_source = "agent"
        plan_meta["original_source"] = intent.source
    return TradePlan(
        action=action,
        strategy_id=intent.strategy_id,
        account_id=intent.account_id,
        market=intent.market,
        side=side,  # type: ignore[arg-type]
        sizing=sizing,
        entry=entry,
        confidence=intent.confidence,
        reasoning_ref=intent.reasoning,
        trigger_event_id=intent.trigger_event_id,
        source=plan_source,  # type: ignore[arg-type]
        intent_id=intent.intent_id,
        meta=plan_meta,
    )


def _sync_position_book_after_execution(config: Config, intent: TradeIntent, result) -> None:
    """Mirror legacy execution fills into the account position book."""

    if not result.fills:
        return
    paths = config.paths
    source = "paper" if result.reason == "paper_executed" else "live"
    book = PositionBook(paths)
    marks: dict[str, float] = {}
    try:
        for fill in result.fills:
            price = float(fill.price or 0.0)
            size = float(fill.size or 0.0)
            if price <= 0 or size <= 0:
                continue
            book.apply_fill(
                account_id=intent.account_id,
                strategy_id=intent.strategy_id,
                market=fill.market or intent.market,
                side=intent.side,
                price=price,
                size_base=size,
                fee_usd=float(fill.fee_usd or 0.0),
                venue=_venue_of(fill.market or intent.market),
                leverage=float((intent.meta or {}).get("leverage") or 1.0),
                source=source,
                order_id=fill.order_id,
                fill_id=fill.fill_id,
            )
            marks[fill.market or intent.market] = price
        if marks:
            profile = get_account_profile(paths, intent.account_id)
            capture_snapshot(
                config,
                intent.account_id,
                profile=profile,
                persist=True,
                marks=marks,
            )
    except Exception:
        log.exception("position book sync failed for intent %s", intent.intent_id)
        jsonl.append(paths.journal("trading"), {
            "kind": "position_book.sync_failed",
            "ts": now_iso(),
            "strategy_id": intent.strategy_id,
            "intent_id": intent.intent_id,
            "order_id": result.order_id,
        })


def _safe_broadcast_trade_event(config: Config, event: dict[str, Any]) -> dict[str, Any]:
    try:
        return broadcast_trade_event(config, event)
    except Exception as exc:  # pragma: no cover - notification must not break trading
        log.exception("trade notification fan-out failed")
        try:
            jsonl.append(config.paths.journal("trading"), {
                "kind": "trade.notification_failed",
                "ts": now_iso(),
                "strategy_id": event.get("strategy_id"),
                "session_id": event.get("session_id"),
                "intent_id": event.get("intent_id"),
                "order_id": event.get("order_id"),
                "error": f"{type(exc).__name__}: {exc}",
            })
        except Exception:
            pass
        return {"ok": False, "channels": [], "deliveries": [], "error": f"{type(exc).__name__}: {exc}"}


def _latest_notification_summary(paths, intent_id: str) -> dict[str, Any]:
    """Read the most recent ``trade.notification`` journal row for an intent.

    The executor's :class:`OrderTracker` broadcasts the canonical fill
    notification (so late fills via the poller are covered too). This
    helper surfaces that broadcast's summary on the submit response so
    callers can see which channels were notified without us re-sending.
    """
    try:
        rows = jsonl.read_all(paths.journal("trading"))
        for row in reversed(rows):
            if row.get("kind") == "trade.notification" and row.get("intent_id") == intent_id:
                summary = row.get("summary") or {}
                if isinstance(summary, dict):
                    return {
                        "ok": bool(summary.get("ok")),
                        "channels": list(summary.get("channels") or []),
                        "deliveries": list(summary.get("deliveries") or []),
                    }
        return {"ok": True, "channels": [], "deliveries": []}
    except Exception:
        return {"ok": False, "channels": [], "deliveries": []}


def _venue_of(market: str) -> str:
    return market.split(":", 1)[0].lower() if ":" in str(market or "") else ""


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
    try:
        from ..data.candles import fetch_candles, fetch_public_ticker

        ticker = fetch_public_ticker(
            intent.market,
            allow_mock=False,
            config_like=config,
        )
        if ticker and ticker.get("price") and (ticker.get("_envelope") or {}).get("mode") == "live":
            return ticker

        rows = fetch_candles(
            intent.market,
            count=1,
            interval="1m",
            allow_mock=False,
            config_like=config,
        )
        if rows:
            last = dict(rows[-1])
            envelope = dict(last.get("_envelope") or {})
            if envelope.get("mode") == "live":
                ts = float(last.get("ts") or 0)
                age_s = max(0, int(time.time() - ts)) if ts > 0 else 0
                return {
                    "price": float(last["close"]),
                    "age_s": age_s,
                    "source": envelope.get("source") or venue_hint or "market_data",
                    "_envelope": envelope,
                }
    except Exception:
        pass
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
    resume: bool = False,
) -> dict[str, Any]:
    """Run a :class:`TradePlan` through the new control-plane.

    The full pipeline:

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
    # Resolve ``close_all`` / ``reduce_pct`` sizing against the live
    # PositionBook *before* the BudgetChecker / executor see the plan.
    # Otherwise the BudgetChecker emits an ``OrderCandidate(size_base
    # = None, notional = 0)`` and the MarketOrderExecutor rejects with
    # ``candidate_has_no_size`` — closes never fire. We swap in a
    # concrete ``fixed_base`` sizing equal to the strategy's *share*
    # of the merged position so the close cannot accidentally take
    # out another strategy's slice.
    plan = _resolve_position_sized_plan(paths, plan)
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

    risk = RiskGate(config).evaluate(intent, market_snapshot=snapshot, resume=resume)
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
        if approval_record is None and resume:
            # Resume path: an operator already approved the original
            # intent, so re-escalation (e.g. canary per-trade approval)
            # is auto-satisfied. This closes the loop where a resumed
            # canary order would re-escalate forever.
            approval_record = ApprovalGate(config).auto_approve(
                intent, risk, reason="resumed_from_operator_approval",
            )
        if approval_record is None:
            try:
                ApprovalGate(config).require(
                    intent, risk, market_snapshot=snapshot, plan=plan,
                )
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
    blocker = _real_money_execution_blocker(config, profile)
    if blocker:
        jsonl.append(paths.journal("trading"), {
            "kind": "trade_plan.execution_blocked",
            "ts": now_iso(),
            "strategy_id": plan.strategy_id,
            "session_id": session_id,
            "plan_id": plan.plan_id,
            "intent_id": intent.intent_id,
            "account_id": plan.account_id,
            "reason": blocker,
        })
        return {
            "status": "rejected",
            "session_id": session_id,
            "plan_id": plan.plan_id,
            "intent": redact_dict(intent.asdict()),
            "risk_decision": risk.asdict(),
            "execution_blocker": blocker,
        }
    snap = fresh_snapshot(config, plan.account_id, profile=profile)
    store = CapitalReservationStore(paths)
    checker = BudgetChecker(profile=profile, snapshot=snap, store=store)
    mark_price = (snapshot or {}).get("price") or plan.entry.limit_price
    side = plan.buy_or_sell if plan.action != "attach_protection" else "buy"
    risk_reducing = plan.action in ("close_position", "reduce_position")
    decision = checker.evaluate(
        plan_strategy_id=plan.strategy_id,
        market=plan.market,
        side=side,
        sizing=plan.sizing,
        mark_price=float(mark_price) if mark_price else None,
        order_type=plan.entry.order_type,
        reduce_only=risk_reducing,
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
    candidate.meta.update({
        "plan_action": plan.action,
        **dict(plan.meta or {}),
    })
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
    # Surface the trade-notification summary the executor's OrderTracker
    # broadcast (it owns the canonical fill notification path so we never
    # double-send). We read the most recent ``trade.notification`` journal
    # row for this intent so the response carries the channel list.
    if run.state == "done":
        response["notifications"] = _latest_notification_summary(paths, intent.intent_id)
    if approval_record is not None:
        response["approval"] = _approval_summary(approval_record)
    return response


def _resolve_position_sized_plan(paths: Any, plan: TradePlan) -> TradePlan:
    """Replace position-relative sizing with a concrete ``fixed_base``.

    Reads the originating strategy's *share* of the merged position
    (post-v6 merged-position contract, see :mod:`nerya.trading.position_book`)
    and converts ``SizingPolicy(method="close_all" | "reduce_pct")`` into
    ``SizingPolicy(method="fixed_base", fixed_base=<size>)`` so the
    BudgetChecker and the MarketOrderExecutor each see a concrete size.

    No-op for non-position-relative sizing methods.
    """

    method = plan.sizing.method
    if method not in ("close_all", "reduce_pct"):
        return plan
    try:
        book = PositionBook(paths)
        share = book.get_share(
            strategy_id=plan.strategy_id,
            account_id=plan.account_id,
            market=plan.market,
        )
    except Exception:  # pragma: no cover - defensive only
        return plan
    share_size = abs(float(getattr(share, "size_share_base", 0.0) or 0.0)) if share else 0.0
    if share_size <= 0.0:
        # No share to close — leave the plan alone so the executor's
        # ``candidate_has_no_size`` rejection path produces an honest
        # rejection (rather than us silently building a zero-sized
        # order). Keeps audit trail truthful.
        return plan
    if method == "reduce_pct":
        pct = float(plan.sizing.reduce_pct or 0.0)
        if pct <= 0.0 or pct > 1.0:
            return plan
        new_size = share_size * pct
    else:  # close_all
        new_size = share_size

    new_sizing = SizingPolicy(method="fixed_base", fixed_base=new_size)
    new_meta = {
        **(dict(plan.meta) if plan.meta else {}),
        "resolved_from_method": method,
        "resolved_share_size": share_size,
    }
    return replace(plan, sizing=new_sizing, meta=new_meta)


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
    if plan.sizing.method == "fixed_base":
        intent_size = float(plan.sizing.fixed_base or 0.0)
        intent_unit = "base"
    elif plan.sizing.method == "fixed_usd":
        intent_size = float(plan.sizing.fixed_usd or 0.0)
        intent_unit = "usd"
    elif plan.sizing.method in (
        "close_all",
        "reduce_pct",
        "pct_nav",
        "risk_to_stop",
        "volatility_target",
        "target_weight",
    ):
        # Sizing is resolved downstream by ``BudgetChecker.evaluate``
        # against the current PositionBook share / NAV / risk budget.
        # RiskGate only uses ``size`` to compute the notional estimate
        # for dedupe / cap checks; for risk-reducing or NAV-derived
        # sizing that estimate is irrelevant. We feed a tiny positive
        # placeholder so the :class:`TradeIntent` validator
        # (``size must be positive``) passes.
        intent_size = 1e-9
        intent_unit = "base"
    else:  # pragma: no cover - any unknown method falls through
        intent_size = 1e-9
        intent_unit = "base"
    protection_present = bool(plan.protection)
    payload = {
        "strategy_id": plan.strategy_id,
        "account_id": plan.account_id,
        "market": plan.market,
        "side": side,
        "size": intent_size,
        "size_unit": intent_unit,
        "order_type": plan.entry.order_type,
        "limit_price": plan.entry.limit_price,
        "time_in_force": plan.entry.time_in_force,
        "confidence": plan.confidence,
        "reasoning": plan.reasoning_ref,
        "source": (
            str((plan.meta or {}).get("original_source"))
            if (plan.meta or {}).get("original_source")
            else plan.source if plan.source in (
                "agent",
                "agent:native",
                "subagent",
                "script",
                "cron",
                "strategy_runtime",
                "strategy_agent",
                "strategy_triggered_agent",
            ) else "agent"
        ),
        "trigger_event_id": plan.trigger_event_id,
        "meta": {
            "plan_id": plan.plan_id,
            "plan_action": plan.action,
            "protection_present": protection_present,
            **dict(plan.meta or {}),
        },
    }
    if plan.intent_id:
        payload["intent_id"] = plan.intent_id
        return TradeIntent(**payload)
    return TradeIntent.new(**payload)


class _ExecutorResultAdapter:
    """Adapter that lets :func:`event_from_order_result` read an
    :class:`ExecutorRun` the same way it reads a legacy
    :class:`ExecutionResult`.

    The notification helper calls ``_asdict(result)`` and reads
    ``.status`` / ``.order_id`` / ``.fills`` / ``.notional_usd`` /
    ``.avg_price`` / ``.filled_size`` / ``.fee_usd``. We synthesise
    those from the executor run's ``result_json`` so the unified plan
    path can broadcast trade notifications without resurrecting the
    old ExecutionEngine shape.
    """

    def __init__(self, *, run, intent, response_status: str):
        self._run = run
        self._intent = intent
        self.status = response_status
        rj = run.result_json or {}
        self.order_id = (run.order_ids or [None])[0]
        self.avg_price = float(rj.get("fill_price") or rj.get("avg_price") or 0.0)
        self.filled_size = float(rj.get("size_base") or 0.0)
        self.notional_usd = float(rj.get("notional_usd") or 0.0)
        self.fee_usd = float(rj.get("fee_usd") or 0.0)
        self.fills: list[Any] = []

    def asdict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "order_id": self.order_id,
            "intent_id": self._intent.intent_id,
            "avg_price": self.avg_price,
            "filled_size": self.filled_size,
            "notional_usd": self.notional_usd,
            "fee_usd": self.fee_usd,
            "fills": list(self.fills),
            "reason": getattr(self._run, "close_type", "") or "",
        }


__all__ = ["submit_trade_intent", "submit_trade_plan"]
