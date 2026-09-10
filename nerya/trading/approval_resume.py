"""Durable approval resume.

When an operator approves an escalated trade via the dashboard /
callback, :func:`nerya.api.routes_approvals._publish_approval_resolution`
publishes an ``approval.resolved`` event on the default event bus. This
module provides the subscriber that consumes that event and resumes the
original trade plan — replaying the *same* ``intent_id`` against the
*frozen* market snapshot captured at escalation time.

This closes the largest correctness gap in the canary/live path: before
this module, an approval only flipped a JSONL row to ``approved`` and no
code resumed the intent. A manual retry would generate a new
``intent_id`` and trip the dedupe gate (``duplicate_intent``) because the
dedupe key does not include ``intent_id``.

Resume contract:

* The approval record carries ``frozen_plan`` + ``frozen_market_snapshot``
  (written by :meth:`ApprovalGate.require`).
* ``resume_approved`` rebuilds the :class:`TradePlan`, threads the
  original ``intent_id`` back in, and calls :func:`submit_trade_plan`
  with ``resume=True`` so the Risk Gate skips the dedupe check.
* The approval row is atomically claimed in SQLite before execution and
  moved to a terminal resume state afterward. Duplicate callbacks never
  reach the connector, including across processes.
* The resume passes the approval's own ``risk_reasons`` back into the
  kernel, so a re-escalation is auto-satisfied ONLY for reasons the
  operator actually signed off on (a reason that appeared afterwards —
  reconciliation drift, a tightened threshold — opens a fresh card).
* Expired approvals never resume: once ``expires_at`` has passed the
  record is flipped to ``expired`` and the resume is refused.
* The frozen market snapshot's ``age_s`` is recomputed from the capture
  timestamp at resume time, so the Risk Gate stale-data guard judges the
  TRUE age of the prices the order would execute against, not the age
  frozen at escalation.
"""

from __future__ import annotations

import logging
from typing import Any

from ..core import jsonl
from ..core.config import Config
from ..core.time import now_iso
from .order_intents import (
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


def resume_approved(config: Config, approval_id: str) -> dict[str, Any]:
    """Resume an approved trade plan.

    Reads the approval record (from the approved JSONL or the DB payload),
    rebuilds the frozen :class:`TradePlan`, and re-submits it through
    :func:`submit_trade_plan` with ``resume=True`` so the dedupe gate is
    bypassed and the original ``intent_id`` is preserved.

    Returns the plan-response envelope from the re-submission, or an
    ``{"ok": False, "error": ...}`` dict when the resume cannot proceed
    (record missing, already resumed, etc.). Never raises — a resume
    failure is journalled and surfaced to the operator.
    """
    record = _load_approved_record(config, approval_id)
    if record is None:
        return {"ok": False, "error": "approval_not_found", "approval_id": approval_id}
    if str(record.get("state") or "").lower() not in ("approved",):
        return {
            "ok": False,
            "error": "approval_not_approved",
            "approval_id": approval_id,
            "state": record.get("state"),
        }
    # Guard against double-resume: if we already resumed, skip.
    if record.get("resumed_intent_id"):
        return {
            "ok": True,
            "already_resumed": True,
            "approval_id": approval_id,
            "intent_id": record.get("resumed_intent_id"),
        }

    plan = _rebuild_plan(record)
    if plan is None:
        return {
            "ok": False,
            "error": "frozen_plan_missing",
            "approval_id": approval_id,
        }
    # Expired approvals never resume. ``require()`` stamps ``expires_at``
    # on the escalation record; records without one (legacy rows) keep
    # the historical behavior rather than failing closed on a missing
    # field they never carried.
    expires_at = _approval_expiry(record)
    if expires_at is not None and _now() > expires_at:
        _mark_expired(config, approval_id)
        _journal_resume(
            config, approval_id, record.get("intent_id"),
            ok=False, error="approval_expired",
        )
        return {
            "ok": False,
            "error": "approval_expired",
            "approval_id": approval_id,
            "expires_at": expires_at,
        }
    # Thread the operator's signoff back into the kernel: the approved
    # decision's exact reasons, so ``submit_trade_plan`` auto-satisfies a
    # re-escalation only when every current reason was part of what the
    # operator approved (B3).
    resume_approval = {
        "risk_reasons": _approval_risk_reasons(record),
        "expires_at": expires_at,
    }
    snapshot = _refresh_snapshot_age(record.get("frozen_market_snapshot") or None)

    try:
        claimed, persisted = _claim_resume(config, approval_id)
    except Exception as exc:
        log.exception("approval resume claim failed for %s", approval_id)
        _journal_resume(
            config,
            approval_id,
            record.get("intent_id"),
            ok=False,
            error=f"claim_failed:{exc}",
        )
        return {
            "ok": False,
            "error": "approval_resume_claim_failed",
            "approval_id": approval_id,
        }
    if not claimed:
        state = str(persisted.get("state") or "")
        payload = persisted.get("payload") or {}
        if state in {"resuming", "resumed"}:
            return {
                "ok": True,
                "already_resumed": True,
                "resume_in_progress": state == "resuming",
                "approval_id": approval_id,
                "intent_id": payload.get("resumed_intent_id") or record.get("intent_id"),
            }
        if state == "resume_failed":
            return {
                "ok": False,
                "error": "resume_failed",
                "approval_id": approval_id,
                "intent_id": payload.get("resumed_intent_id") or record.get("intent_id"),
            }
        return {
            "ok": False,
            "error": "approval_resume_not_claimed",
            "approval_id": approval_id,
            "state": state or None,
        }

    # Late import to avoid a circular dependency at module load.
    from .submit import submit_trade_plan

    try:
        response = submit_trade_plan(
            config, plan, market_snapshot=snapshot, resume=True,
            resume_approval=resume_approval,
        )
    except Exception as exc:  # pragma: no cover - defensive
        log.exception("approval resume failed for %s", approval_id)
        _finish_resume(
            config,
            approval_id,
            intent_id=record.get("intent_id"),
            ok=False,
            error=str(exc),
        )
        _journal_resume(config, approval_id, record.get("intent_id"), ok=False, error=str(exc))
        return {"ok": False, "error": f"resume_failed:{exc}", "approval_id": approval_id}

    persisted = _finish_resume(
        config,
        approval_id,
        intent_id=record.get("intent_id"),
        ok=True,
        response_status=response.get("status"),
    )
    _journal_resume(
        config, approval_id, record.get("intent_id"), ok=True, response_status=response.get("status"),
    )
    if not persisted:
        return {
            "ok": False,
            "error": "resume_state_persist_failed",
            "approval_id": approval_id,
            "resume_response": response,
        }
    return {"ok": True, "approval_id": approval_id, "resume_response": response}


def register_approval_resume_subscriber(config: Config) -> Any:
    """Register a bus subscriber that resumes approved trades.

    Subscribes to ``approval.resolved`` on the default
    :class:`StreamingEventBus`. When an approval moves to ``approved``,
    the subscriber calls :func:`resume_approved`. Safe to call once at
    process startup (e.g. from the agent kernel / API bootstrap).

    Idempotent: repeated calls in the same process are no-ops (the first
    registration wins) so multi-turn kernel reboots don't stack handlers.

    Returns the unsubscribe handle so callers (tests) can tear it down.
    """
    global _resume_subscriber_registered
    if _resume_subscriber_registered:
        return None
    from ..agent.streaming import get_default_bus

    def _on_event(event: dict[str, Any]) -> None:
        try:
            if event.get("kind") != "approval.resolved":
                return
            if str(event.get("state") or "").lower() != "approved":
                return
            record = event.get("record")
            record_kind = str(
                (record or {}).get("kind")
                if isinstance(record, dict)
                else event.get("approval_kind") or ""
            ).strip()
            if record_kind and record_kind != "trade_intent":
                return
            approval_id = str(event.get("approval_id") or "")
            if not approval_id:
                return
            # Resolve the config fresh — the event may fire from a
            # different process context (API server vs. agent kernel).
            from ..core.config import load_config
            cfg = config
            try:
                cfg = load_config(config.paths.root)
            except Exception:
                pass
            resume_approved(cfg, approval_id)
        except Exception:
            log.exception("approval.resolved subscriber failed")

    handle = get_default_bus().subscribe(_on_event)
    _resume_subscriber_registered = True
    return handle


_resume_subscriber_registered = False


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------


def _now() -> float:
    import time

    return time.time()


def _approval_expiry(record: dict[str, Any]) -> float | None:
    """Best-effort ``expires_at`` (epoch seconds) from an approval record.

    The full escalation row written by ``ApprovalGate.require`` carries a
    float epoch ``expires_at``. Slim ack rows and legacy records may not
    have one — those return ``None`` and skip the expiry check.
    """
    raw = record.get("expires_at")
    if raw is None:
        return None
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return None
    if value <= 0:
        return None
    return value


def _approval_risk_reasons(record: dict[str, Any]) -> list[str]:
    """The decision reasons the operator signed off on."""
    reasons = record.get("risk_reasons")
    if not reasons and isinstance(record.get("risk"), dict):
        reasons = record["risk"].get("reasons")
    if isinstance(reasons, list):
        return [str(r) for r in reasons]
    return []


def _mark_expired(config: Config, approval_id: str) -> None:
    """Flip an expired approval to a terminal ``expired`` state (best effort).

    R2B10: this used to call ``ApprovalGate.reject(aid, "expired")``,
    which left the DB state showing ``rejected`` — dashboards then
    displayed approvals the operator never declined as rejections. The
    gate now owns an explicit ``expired`` terminal write.
    """
    try:
        from .approval import ApprovalGate

        ApprovalGate(config).mark_expired(approval_id)
    except Exception:
        log.exception("failed to mark approval %s expired", approval_id)


def _refresh_snapshot_age(snapshot: Any) -> Any:
    """Recompute the frozen market snapshot's ``age_s`` at resume time.

    ``ApprovalGate.require`` freezes ``age_s`` at escalation time, so the
    Risk Gate's ``stale_market_data`` guard would judge the snapshot by
    its age THEN — a stale price looks fresh forever. Derive the true age
    from the capture timestamp when one is reachable (envelope ``ts`` /
    ``fetched_at``, or the snapshot's own fields); epoch ms (>1e12) is
    normalized to seconds. Without any timestamp the frozen value is kept
    as-is (best effort — no invented staleness).
    """
    if not isinstance(snapshot, dict):
        return snapshot
    ts: float | None = None
    envelope = snapshot.get("_envelope")
    candidates: list[Any] = []
    if isinstance(envelope, dict):
        candidates.extend((envelope.get("ts"), envelope.get("fetched_at")))
    candidates.extend((snapshot.get("ts"), snapshot.get("fetched_at")))
    for raw in candidates:
        if raw is None:
            continue
        try:
            value = float(raw)
        except (TypeError, ValueError):
            continue
        if value > 1e12:  # epoch ms
            value /= 1000.0
        if value > 1e9:  # plausible epoch seconds
            ts = value
            break
    refreshed = dict(snapshot)
    if ts is not None:
        refreshed["age_s"] = max(0, int(_now() - ts))
    return refreshed


def _approval_payload(row: dict[str, Any] | None) -> dict[str, Any]:
    if not row:
        return {}
    raw = row.get("payload")
    if isinstance(raw, dict):
        return dict(raw)
    if isinstance(raw, str):
        try:
            import json

            decoded = json.loads(raw)
            return dict(decoded) if isinstance(decoded, dict) else {}
        except (TypeError, ValueError):
            return {}
    return {}


def _claim_resume(config: Config, approval_id: str) -> tuple[bool, dict[str, Any]]:
    from ..db.repositories import ApprovalRepository
    from ..db.sqlite import connect

    con = connect(config.paths.db)
    try:
        repo = ApprovalRepository(con)
        if repo.claim_resume(approval_id):
            return True, {}
        row = repo.get(approval_id) or {}
        return False, {**row, "payload": _approval_payload(row)}
    finally:
        con.close()


def _finish_resume(
    config: Config,
    approval_id: str,
    *,
    intent_id: str | None,
    ok: bool,
    response_status: str | None = None,
    error: str | None = None,
) -> bool:
    from ..db.repositories import ApprovalRepository
    from ..db.sqlite import connect

    try:
        con = connect(config.paths.db)
        try:
            completed = ApprovalRepository(con).finish_resume(
                approval_id,
                state="resumed" if ok else "resume_failed",
                intent_id=intent_id,
                response_status=response_status,
                error=error,
            )
        finally:
            con.close()
        if not completed:
            log.error("approval resume terminal state was not persisted for %s", approval_id)
        return completed
    except Exception:
        log.exception("approval resume terminal persistence failed for %s", approval_id)
        return False


def _load_approved_record(config: Config, approval_id: str) -> dict[str, Any] | None:
    """Find an approval record across the approved / pending JSONL + DB.

    The callback path moves the row from ``approvals_pending`` to
    ``approvals_approved`` on approval, so we check the approved journal
    first, then fall back to pending (for auto-approved records that
    stay in the DB only).
    """
    paths = config.paths
    # Collect every matching record across journals; return the richest
    # one (the record that carries ``frozen_plan``). The approval gate
    # writes a slim ``{"state":"approved"}`` ack row in addition to the
    # full escalation record, so a naive first-match would miss the plan.
    candidates: list[dict[str, Any]] = []
    for journal_path in (paths.approvals_approved, paths.approvals_pending):
        if not journal_path.exists():
            continue
        for rec in jsonl.read_all(journal_path):
            if rec.get("approval_id") == approval_id or rec.get("id") == approval_id:
                candidates.append(rec)
    if candidates:
        # Prefer the record that carries the frozen plan; fall back to
        # the first match otherwise.
        for rec in candidates:
            if rec.get("frozen_plan"):
                return rec
        return candidates[0]
    # Fall back to the DB payload (auto-approvals are DB-only).
    try:
        import json as _json
        from ..db.repositories import ApprovalRepository
        from ..db.sqlite import connect

        con = connect(paths.db)
        row = ApprovalRepository(con).get(approval_id)
        con.close()
        if row:
            raw_payload = row.get("payload")
            if isinstance(raw_payload, str):
                try:
                    payload = _json.loads(raw_payload)
                except Exception:
                    payload = {}
            elif isinstance(raw_payload, dict):
                payload = dict(raw_payload)
            else:
                payload = {}
            if payload:
                payload.setdefault("approval_id", approval_id)
                payload.setdefault("state", row.get("state"))
                # The DB row carries the authoritative expiry; the JSON
                # payload written by auto-approve does not.
                if payload.get("expires_at") is None and row.get("expires_at"):
                    payload["expires_at"] = row.get("expires_at")
                return payload
    except Exception:
        pass
    return None


def _rebuild_plan(record: dict[str, Any]) -> TradePlan | None:
    """Reconstruct a :class:`TradePlan` from a frozen approval record."""
    frozen = record.get("frozen_plan")
    if not isinstance(frozen, dict):
        return None
    try:
        sizing_raw = frozen.get("sizing") or {}
        sizing = SizingPolicy(
            method=str(sizing_raw.get("method") or "fixed_usd"),
            fixed_usd=sizing_raw.get("fixed_usd"),
            fixed_base=sizing_raw.get("fixed_base"),
            pct_nav=sizing_raw.get("pct_nav"),
            risk_pct_nav=sizing_raw.get("risk_pct_nav"),
            stop_distance_pct=sizing_raw.get("stop_distance_pct"),
            target_volatility_pct=sizing_raw.get("target_volatility_pct"),
            target_weight=sizing_raw.get("target_weight"),
            reduce_pct=sizing_raw.get("reduce_pct"),
            max_notional_usd=sizing_raw.get("max_notional_usd"),
        )
        entry_raw = frozen.get("entry") or {}
        entry = TradeEntry(
            order_type=str(entry_raw.get("order_type") or "market"),
            limit_price=entry_raw.get("limit_price"),
            stop_price=entry_raw.get("stop_price"),
            max_slippage_bps=int(entry_raw.get("max_slippage_bps") or 25),
            time_in_force=str(entry_raw.get("time_in_force") or "gtc"),
        )
        protection = None
        prot_raw = frozen.get("protection")
        if isinstance(prot_raw, dict):
            protection = _rebuild_protection(prot_raw)
        return TradePlan(
            plan_id=str(frozen.get("plan_id") or ""),
            action=str(frozen.get("action") or "open_position"),  # type: ignore[arg-type]
            strategy_id=str(frozen.get("strategy_id") or ""),
            account_id=str(frozen.get("account_id") or ""),
            market=str(frozen.get("market") or ""),
            side=str(frozen.get("side") or "long"),  # type: ignore[arg-type]
            sizing=sizing,
            entry=entry,
            protection=protection,
            confidence=float(frozen.get("confidence") or 0.0),
            reasoning_ref=str(frozen.get("reasoning_ref") or ""),
            trigger_event_id=frozen.get("trigger_event_id"),
            source=str(frozen.get("source") or "agent"),  # type: ignore[arg-type]
            intent_id=str(frozen.get("intent_id") or record.get("intent_id") or ""),
            meta=dict(frozen.get("meta") or {}),
        )
    except Exception:
        log.exception("failed to rebuild plan from frozen approval record")
        return None


def _rebuild_protection(raw: dict[str, Any]) -> ProtectionRule:
    sl = raw.get("stop_loss")
    tp = raw.get("take_profit")
    trail = raw.get("trailing_stop")
    partials = raw.get("partial_exits") or []
    return ProtectionRule(
        protection_id=str(raw.get("protection_id") or ""),
        position_id=str(raw.get("position_id") or ""),
        executor_id=str(raw.get("executor_id") or ""),
        strategy_id=str(raw.get("strategy_id") or ""),
        account_id=str(raw.get("account_id") or ""),
        market=str(raw.get("market") or ""),
        side=str(raw.get("side") or "long"),  # type: ignore[arg-type]
        mode=str(raw.get("mode") or "soft_runtime"),  # type: ignore[arg-type]
        stop_loss=StopLossSpec(**sl) if isinstance(sl, dict) else None,
        take_profit=TakeProfitSpec(**tp) if isinstance(tp, dict) else None,
        time_limit_sec=raw.get("time_limit_sec"),
        trailing_stop=TrailingStopSpec(**trail) if isinstance(trail, dict) else None,
        partial_exits=[PartialExitSpec(**p) for p in partials if isinstance(p, dict)],
        trigger_source=str(raw.get("trigger_source") or "mark"),  # type: ignore[arg-type]
        status=str(raw.get("status") or "armed"),  # type: ignore[arg-type]
        notes=str(raw.get("notes") or ""),
    )


def _journal_resume(
    config: Config,
    approval_id: str,
    intent_id: str | None,
    *,
    ok: bool,
    response_status: str | None = None,
    error: str | None = None,
) -> None:
    try:
        jsonl.append(config.paths.journal("trading"), {
            "kind": "approval.resumed",
            "ts": now_iso(),
            "approval_id": approval_id,
            "intent_id": intent_id,
            "ok": ok,
            "status": response_status,
            "error": error,
        })
    except Exception:
        pass


__all__ = ["resume_approved", "register_approval_resume_subscriber"]
