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
import os
import time
from dataclasses import replace
from typing import Any, Optional

from ..core import jsonl
from ..core.config import Config
from ..core.errors import ApprovalPending, IntentValidationError
from ..core.redaction import redact_dict
from ..core.time import now_iso
from ..core.truth import (
    degraded_envelope,
    mock_envelope,
    resolve_allow_mock,
)
from ..messaging.trade_notifications import broadcast_trade_event
from ..strategy_history import open_session, store as history_store, track_outcome
from .account_snapshots import capture_snapshot, fresh_snapshot
from .accounts import get_account_profile
from .approval import ApprovalGate
from .capital import BudgetChecker, CapitalReservationStore
from .executors.orchestrator import ExecutorOrchestrator
from .intents import TradeIntent
from .order_intents import ProtectionRule, SizingPolicy, TradePlan
from .position_book import PositionBook
from .risk import RiskGate, is_usd_stable_quote

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
    # Real-money accounts never auto-approve, regardless of the strategy's
    # promotion state. ``promotion_state`` guards the *strategy* lifecycle;
    # this guards the *account* — a paper-status strategy pointed at a
    # canary/live account must still page an operator. Fail closed when
    # the profile cannot be resolved.
    try:
        profile = get_account_profile(config.paths, intent.account_id)
    except Exception:
        return None
    if bool(getattr(profile, "is_real_money", False)):
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


def _blocking_reasons(reasons: list[str]) -> list[str]:
    """Reasons that actually gate the decision (drop ``ok``/audit-only)."""
    return [
        str(r)
        for r in (reasons or [])
        if str(r) != "ok" and not str(r).endswith("_exempt")
    ]


def _reason_approval_signature(reason: str) -> str:
    """Canonical form of a risk reason for approval matching (R2B2b).

    Parameterized escalation reasons embed volatile numbers. For reasons
    of the form ``key:<observed>>=<threshold>`` (e.g.
    ``approval_required_threshold:5041.23>=5000.00``) the *observed*
    value drifts with NAV between card creation and resume, but the
    *threshold* is policy and stable — an operator who approved "orders
    above $5000 need sign-off" does not need to re-approve the same
    policy because the notional moved $30. Match on ``key`` plus
    everything after ``>=`` and ignore the observed part. Reasons
    without an ``observed>=threshold`` tail compare verbatim, so a
    genuinely new reason family (or a changed threshold) still
    re-escalates.
    """
    text = str(reason)
    key, sep, comparison = text.partition(":")
    if not sep or ">=" not in comparison:
        return text
    _observed, _, threshold = comparison.partition(">=")
    return f"{key}:>={threshold.strip()}"


def _resume_reasons_satisfied(
    reasons: list[str],
    resume_approval: Optional[dict[str, Any]],
) -> bool:
    """Whether every current escalate reason was in the approved decision.

    The approval record carries ``risk_reasons`` — the decision reasons
    the operator signed off on. Comparison is by
    :func:`_reason_approval_signature`: parameterized threshold reasons
    match on key + threshold while ignoring the embedded (NAV-drifted)
    notional, and everything else must match exactly. A raised/lowered
    threshold, a fresh ``reconciliation_action_required:<report>`` or any
    other new reason never inherits an approval granted before it
    appeared. Without a resume record there is nothing to compare
    against, so we fail closed.
    """
    blocking = _blocking_reasons(reasons)
    if not blocking:
        return True
    if not isinstance(resume_approval, dict):
        return False
    approved = {
        _reason_approval_signature(str(r))
        for r in _blocking_reasons(
            list(resume_approval.get("risk_reasons") or []),
        )
    }
    return all(
        _reason_approval_signature(str(reason)) in approved
        for reason in blocking
    )


def _prior_approved_reasons(
    resume_approval: Optional[dict[str, Any]],
) -> list[str]:
    """Approved risk reasons carried by the resume record (R2B2a).

    Used to seed a NEW operator card created later in the same approval
    chain (e.g. a post-budget threshold escalation after the canary card
    was approved). Without the carry-over, the new card's record only
    knows its own reasons, the resume of THAT card fails the
    already-approved check for the earlier reasons, and operators get
    ping-ponged with alternately-typed cards forever.
    """
    if not isinstance(resume_approval, dict):
        return []
    return [
        str(r) for r in (resume_approval.get("risk_reasons") or []) if str(r)
    ]


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
    # Crypto readiness: real-money execution refuses to run while the
    # vault would fall back to the XOR test cipher or the hardcoded
    # default passphrase. Both are fine for paper workspaces but mean
    # exchange credentials are effectively plaintext-at-rest.
    from ..security import encryption as _encryption
    if not _encryption.has_strong_crypto():
        return "vault_crypto_unavailable"
    if not (os.environ.get("NERYA_VAULT_PASSPHRASE") or "").strip():
        return "vault_passphrase_not_set"
    return None


def submit_trade_intent(
    config: Config,
    *,
    spec: dict[str, Any],
    market_snapshot: Optional[dict[str, Any]] = None,
    default_strategy: str = "manual_agent",
    default_source: str = "agent",
) -> dict[str, Any]:
    """Validate an intent and submit it through the one guarded plan executor."""
    payload = dict(spec or {})
    if "intent_id" in payload:
        intent = TradeIntent(**payload)
    else:
        payload.setdefault("strategy_id", default_strategy)
        payload.setdefault("source", default_source)
        intent = TradeIntent.new(**payload)
    return submit_trade_plan(config, _intent_to_plan(intent), market_snapshot=market_snapshot)


def _intent_to_plan(intent: TradeIntent) -> TradePlan:
    """Translate an order intent into the canonical execution plan.

    The plan carries the same information but in the unified control-plane
    schema so it flows through :func:`submit_trade_plan`. Action is inferred
    from ``meta.plan_action`` when present (set by the resume path) or from
    the side / reduce_only hint, defaulting to ``open_position``.

    ``plan_action`` accepts the strategy facade's vocabulary
    (``close`` / ``exit`` / ``flatten`` / ``close_all`` / ``open_long`` /
    ``open_short`` / ``reduce`` / ``partial_exit``) plus the canonical
    plan tokens; unknown values keep the historical open default.
    """
    from .order_intents import SizingPolicy, TradeEntry, TradePlan

    meta = intent.meta or {}
    plan_action = str(meta.get("plan_action") or "").strip().lower()
    reduce_only = bool(meta.get("reduce_only"))
    if plan_action in ("close_position", "reduce_position", "attach_protection"):
        action = plan_action  # type: ignore[assignment]
    elif plan_action in ("close", "exit", "flatten", "close_all"):
        # D1: the strategy facade documents these close aliases, but the
        # recognized-set check above let them fall through to the open
        # branch — a sell+close intent became an OPEN-SHORT plan while
        # the backtest engine and the risk gate treated it as an exit.
        action = "close_position"
    elif plan_action in ("reduce", "partial_exit"):
        action = "reduce_position"
    elif plan_action in ("open_long", "open_short"):
        action = "open_position"
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
    # unit, so we hand the BudgetChecker a fixed value. Quote-unit sizes
    # are USD amounts only when the market's quote asset is a USD stable
    # (C9/D2) — anything else previously hit fixed_base and traded the
    # quote count as base units (100 USDT requested → 100 BTC ordered).
    if intent.size_unit == "usd":
        sizing = SizingPolicy(method="fixed_usd", fixed_usd=float(intent.size))
    elif intent.size_unit == "quote":
        if not is_usd_stable_quote(intent.market):
            raise IntentValidationError(
                f"size_unit='quote' is only supported on USD-stable quote "
                f"assets; market {intent.market!r} does not have one — "
                "resubmit with size_unit='base' or 'usd'"
            )
        sizing = SizingPolicy(method="fixed_usd", fixed_usd=float(intent.size))
    else:
        sizing = SizingPolicy(method="fixed_base", fixed_base=float(intent.size))

    entry = TradeEntry(
        order_type=intent.order_type,
        limit_price=intent.limit_price,
        stop_price=intent.stop_price,
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
            # Untagged caller/model data has no trustworthy provenance. Paper
            # simulation may still use the numeric mark, but real-money Risk
            # Gate requires a live envelope and will fail closed.
            snap["_envelope"] = degraded_envelope(
                "caller_market_snapshot",
                error="missing_provenance_envelope",
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
    resume_approval: Optional[dict[str, Any]] = None,
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
    if plan.action == "attach_protection":
        # R3T5: an ``attach_protection`` plan must never reach the
        # MarketOrderExecutor. It used to fall through as a real BUY
        # market order (``side`` was forced to "buy" only to dodge
        # ``buy_or_sell``'s raise, risk treated it as non-reducing) —
        # a strategy asking to attach SL/TP silently opened an
        # unintended long. Route it to the protection path instead.
        return _attach_protection_for_plan(config, plan)
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

    # A real-money account that cannot place orders must be rejected before
    # Approval Gate.  Otherwise a human could approve a trade that the
    # executor is guaranteed to refuse, leaving a misleading approval card
    # and no executable path.  This is deliberately after Risk Gate so the
    # caller still gets the normal risk reasons when the intent itself is
    # invalid, but before any approval is created.
    try:
        profile = get_account_profile(paths, plan.account_id)
    except Exception as exc:
        profile = None
        execution_blocker = f"account_profile_unavailable:{exc}"
    else:
        execution_blocker = _real_money_execution_blocker(config, profile)
    if execution_blocker:
        jsonl.append(paths.journal("trading"), {
            "kind": "trade_plan.execution_blocked",
            "ts": now_iso(),
            "strategy_id": plan.strategy_id,
            "session_id": session_id,
            "plan_id": plan.plan_id,
            "intent_id": intent.intent_id,
            "account_id": plan.account_id,
            "reason": execution_blocker,
        })
        return {
            "status": "rejected",
            "session_id": session_id,
            "plan_id": plan.plan_id,
            "intent": redact_dict(intent.asdict()),
            "risk_decision": risk.asdict(),
            "execution_blocker": execution_blocker,
        }

    approval_record = None
    if risk.decision == "escalate":
        approval_record = _maybe_auto_approve_strategy_order(config, intent, risk)
        if approval_record is None and resume:
            # Resume path: an operator already approved the original
            # intent. Auto-satisfy the re-escalation ONLY for reasons
            # that were part of the approved decision; a reason that
            # appeared afterwards (reconciliation drift, tightened
            # thresholds, fresh stale guards) must re-escalate to a new
            # card instead of riding on the old approval (B3).
            if _resume_reasons_satisfied(risk.reasons, resume_approval):
                approval_record = ApprovalGate(config).auto_approve(
                    intent, risk, reason="resumed_from_operator_approval",
                )
        if approval_record is None:
            try:
                ApprovalGate(config).require(
                    intent, risk, market_snapshot=snapshot, plan=plan,
                    prior_approved_risk_reasons=_prior_approved_reasons(
                        resume_approval if resume else None,
                    ),
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
    # Re-read the account after Approval Gate. An operator may change the
    # account mode, permissions, or live switch while a card is pending; the
    # approval must never turn that stale pre-approval view into a connector
    # call.
    try:
        profile = get_account_profile(paths, plan.account_id)
    except Exception as exc:
        profile = None
        execution_blocker = f"account_profile_unavailable:{exc}"
    else:
        execution_blocker = _real_money_execution_blocker(config, profile)
    if execution_blocker:
        jsonl.append(paths.journal("trading"), {
            "kind": "trade_plan.execution_blocked",
            "ts": now_iso(),
            "strategy_id": plan.strategy_id,
            "session_id": session_id,
            "plan_id": plan.plan_id,
            "intent_id": intent.intent_id,
            "account_id": plan.account_id,
            "reason": execution_blocker,
        })
        return {
            "status": "rejected",
            "session_id": session_id,
            "plan_id": plan.plan_id,
            "intent": redact_dict(intent.asdict()),
            "risk_decision": risk.asdict(),
            "execution_blocker": execution_blocker,
        }
    snap = fresh_snapshot(config, plan.account_id, profile=profile)
    store = CapitalReservationStore(paths)
    # R2B7: sweep TTL-expired reservations up front so budget blocked by
    # a crashed executor actually frees when the TTL passes, instead of
    # only advancing as a side effect of active_for_account reads.
    try:
        store.expire_due()
    except Exception:
        log.exception("reservation expire_due sweep failed")
    checker = BudgetChecker(profile=profile, snapshot=snap, store=store)
    mark_price = (snapshot or {}).get("price") or plan.entry.limit_price
    # R3T5: ``attach_protection`` plans are intercepted above, so every
    # plan reaching this line is directional and ``buy_or_sell`` is
    # always defined.
    side = plan.buy_or_sell
    risk_reducing = plan.action in ("close_position", "reduce_position")
    decision = checker.evaluate(
        plan_strategy_id=plan.strategy_id,
        market=plan.market,
        side=side,
        sizing=plan.sizing,
        mark_price=float(mark_price) if mark_price else None,
        order_price=plan.entry.limit_price,
        stop_price=plan.entry.stop_price,
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

    # NAV-derived sizing (pct_nav / risk_to_stop / volatility_target /
    # target_weight / position-relative methods) reached RiskGate with a
    # placeholder notional (≈$0) because the real amount only exists once
    # the BudgetChecker resolves it against the account snapshot. Re-run
    # every notional-scaled check — single-order cap, canary cap, total
    # exposure, per-market cap, daily notional, approval threshold —
    # against the resolved amount so a strategy cannot open full-NAV
    # positions while every size gate saw nothing (C1). Risk-reducing
    # plans stay exempt, matching the original gate's carve-out.
    if plan.action not in ("close_position", "reduce_position"):
        resolved_risk = RiskGate(config).evaluate_resolved_notional(
            intent,
            notional_usd=float(
                getattr(decision.candidate, "notional_usd", 0.0) or 0.0
            ),
            mark_price=float(mark_price) if mark_price else None,
        )
        if resolved_risk.decision == "reject":
            return {
                "status": "rejected",
                "session_id": session_id,
                "plan_id": plan.plan_id,
                "intent": redact_dict(intent.asdict()),
                "risk_decision": resolved_risk.asdict(),
                "budget_decision": decision.asdict(),
            }
        if resolved_risk.decision == "escalate":
            # Same approval contract as the pre-budget escalation: policy
            # auto-approval, then resume-reasons matching, then a fresh
            # operator card. Nothing has been reserved yet, so a pending
            # card leaves the account untouched.
            approval_record = _maybe_auto_approve_strategy_order(
                config, intent, resolved_risk,
            )
            if (
                approval_record is None
                and resume
                and _resume_reasons_satisfied(resolved_risk.reasons, resume_approval)
            ):
                approval_record = ApprovalGate(config).auto_approve(
                    intent, resolved_risk,
                    reason="resumed_from_operator_approval",
                )
            if approval_record is None:
                try:
                    ApprovalGate(config).require(
                        intent, resolved_risk, market_snapshot=snapshot, plan=plan,
                        prior_approved_risk_reasons=_prior_approved_reasons(
                            resume_approval if resume else None,
                        ),
                    )
                except ApprovalPending as p:
                    return {
                        "status": "pending_approval",
                        "session_id": session_id,
                        "plan_id": plan.plan_id,
                        "approval_id": p.approval_id,
                        "intent": redact_dict(intent.asdict()),
                        "risk_decision": resolved_risk.asdict(),
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
    # The receipt is an observed ledger record, never inferred from requested size.
    from contextlib import closing
    from .order_tracker import OrderTracker
    with closing(OrderTracker(paths)) as tracker:
        orders = [redact_dict(order.asdict()) for order_id in run.order_ids
                  if (order := tracker.get(order_id)) is not None]
    response["orders"] = orders
    if len(orders) == 1:
        response["order_id"] = orders[0]["order_id"]
        response["order"] = orders[0]
    # Surface the trade-notification summary the executor's OrderTracker
    # broadcast (it owns the canonical fill notification path so we never
    # double-send). We read the most recent ``trade.notification`` journal
    # row for this intent so the response carries the channel list.
    if run.state == "done":
        response["notifications"] = _latest_notification_summary(paths, intent.intent_id)
    if approval_record is not None:
        response["approval"] = _approval_summary(approval_record)
    return response


def _attach_protection_for_plan(config: Config, plan: TradePlan) -> dict[str, Any]:
    """Route an ``attach_protection`` plan to the protection path (R3T5).

    Such plans used to flow into the MarketOrderExecutor as a real BUY
    market order. Instead, the rule carried by the plan is persisted
    against the strategy's open position — mirroring the fill path's
    ``_maybe_attach_protection`` (ProtectionStore upsert + a durable
    position-protection executor) — and a truthful non-order envelope
    is returned. Raises :class:`IntentValidationError` when there is no
    open position to protect or the plan carries no rule; callers
    wanting an arbitrary position should use
    ``TradingAPI.attach_protection`` with an explicit ``position_id``.
    """
    from .protection_store import ProtectionStore

    paths = config.paths
    position = PositionBook(paths).get_open(
        account_id=plan.account_id,
        strategy_id=plan.strategy_id,
        market=plan.market,
    )
    if position is None:
        raise IntentValidationError(
            "attach_protection requires an open position: strategy "
            f"{plan.strategy_id!r} has no share of an open position on "
            f"{plan.market!r} (account {plan.account_id!r}) — open it "
            "first, or call TradingAPI.attach_protection with an "
            "explicit position_id"
        )
    src = plan.protection
    if src is None:
        raise IntentValidationError(
            "attach_protection plan carries no protection rule — declare "
            "stop_loss / take_profit / trailing_stop / partial_exits / "
            "time_limit_sec on plan.protection"
        )
    rule = ProtectionRule(
        position_id=position.position_id,
        strategy_id=plan.strategy_id,
        account_id=plan.account_id,
        market=plan.market,
        side=position.side,
        mode=src.mode,
        stop_loss=src.stop_loss,
        take_profit=src.take_profit,
        time_limit_sec=src.time_limit_sec,
        trailing_stop=src.trailing_stop,
        partial_exits=list(src.partial_exits or []),
        trigger_source=src.trigger_source,
        status="armed",
        notes=src.notes or "attached_via_plan",
    )
    ProtectionStore(paths).upsert(rule)
    PositionBook(paths).attach_protection(position.position_id, rule.protection_id)
    try:
        orch = ExecutorOrchestrator(config)
        orch.create_position_protection(rule=rule, position_id=position.position_id)
        orch.close()
    except Exception:
        # The rule is already persisted and armed — a missing executor
        # row must not fail the attachment; the orchestrator's resume
        # path can recreate it from the rule.
        log.exception(
            "could not persist protection executor for position %s",
            position.position_id,
        )
    jsonl.append(paths.journal("trading"), {
        "kind": "protection.attached",
        "ts": now_iso(),
        "strategy_id": plan.strategy_id,
        "account_id": plan.account_id,
        "plan_id": plan.plan_id,
        "intent_id": plan.intent_id,
        "position_id": position.position_id,
        "protection_id": rule.protection_id,
        "mode": rule.mode,
    })
    return {
        "status": "protection_attached",
        "plan_id": plan.plan_id,
        "protection_id": rule.protection_id,
        "position_id": position.position_id,
        "mode": rule.mode,
    }


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
        "stop_price": plan.entry.stop_price,
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
                "operator",
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
