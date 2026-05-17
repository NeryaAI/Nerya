"""Control-plane operator endpoints.

Exposes the new account-aware control plane to the dashboard and any
HTTP-driven operator tooling. Every handler is read-only or
operator-explicit (no risk-bypassing endpoints).

Routes:

* ``/portfolio/health``      — account snapshot + reservations + positions + executors + protections.
* ``/portfolio/refresh``     — refresh open-position marks, executor ticks and snapshots.
* ``/orders/list``           — orders from :class:`OrderTracker` (filterable).
* ``/orders/cancel``         — request a cancel.
* ``/executors/list``        — executors from :class:`ExecutorOrchestrator`.
* ``/executors/cancel``      — operator-driven executor cancel.
* ``/reconciliation/reports``— reports from :class:`ReconciliationStore`.
* ``/reconciliation/run``    — operator-triggered :func:`reconcile`.
* ``/protections/list``      — active :class:`ProtectionRule` rows.
* ``/strategy/promotions/list``    — promotion records.
* ``/strategy/promotions/request`` — request a promotion (writes audit row).
* ``/strategy/promotions/apply``   — flip strategy state for an approved promotion.
* ``/strategy/evidence/record``    — record an evidence row.
* ``/incidents``             — combined incident feed (lost orders, drift, auth, stale).
* ``/kill_switch/get`` / ``/kill_switch/set`` — runtime kill switch.

The dashboard typecheck (``cd Nerya/dashboard && npx tsc --noEmit``) is
left to the user per the workflow rules; the dashboard itself can
consume these endpoints incrementally — every payload is a plain JSON
dict.
"""

from __future__ import annotations

import logging
import time
from datetime import datetime
from typing import Any

from ..core import jsonl
from ..core.time import now_iso

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Route handlers
# ---------------------------------------------------------------------------


def _portfolio_health(client, _payload):
    from ..trading.account_snapshots import latest_snapshot
    from ..trading.accounts import load_account_profiles
    from ..trading.capital import CapitalReservationStore
    from ..trading.executors import ExecutorOrchestrator
    from ..trading.position_book import PositionBook
    from ..trading.protection_store import ProtectionStore

    paths = client.config.paths
    profiles = load_account_profiles(paths)

    res_store = CapitalReservationStore(paths)
    book = PositionBook(paths)
    protections = ProtectionStore(paths)
    orchestrator = ExecutorOrchestrator(client.config)
    legacy_positions = {
        aid: _legacy_ledger_position_rows(paths, profile)
        for aid, profile in profiles.items()
    }

    # Active executors keyed by account.
    exec_index: dict[str, list[dict[str, Any]]] = {}
    for run in orchestrator.list_active():
        exec_index.setdefault(run.account_id, []).append({
            "executor_id": run.executor_id,
            "kind": run.kind,
            "state": run.state,
            "market": run.market,
            "strategy_id": run.strategy_id,
            "created_at": run.created_at,
            "last_heartbeat": run.last_heartbeat,
        })

    accounts: list[dict[str, Any]] = []
    for aid, profile in profiles.items():
        snap = latest_snapshot(paths, aid)
        try:
            blocked_usd = float(res_store.total_blocked_usd(aid))
        except Exception:
            blocked_usd = 0.0
        book_open = [p.asdict() for p in book.open_positions(account_id=aid)]
        ledger_open = legacy_positions.get(aid, [])
        open_pos = book_open or ledger_open
        prot_rules = protections.list_active(account_id=aid)
        accounts.append({
            "account_id": aid,
            "mode": profile.mode,
            "venue": profile.venue,
            "kind": profile.kind,
            "live_trading_enabled": profile.live_trading_enabled,
            "snapshot": snap.asdict() if snap else None,
            "reserved_usd": blocked_usd,
            "open_position_count": len(open_pos),
            "open_positions": open_pos,
            "position_book_open_positions": book_open,
            "legacy_ledger_positions": ledger_open,
            "protection_count": len(prot_rules),
            "protections": [r.asdict() for r in prot_rules],
            "active_executors": exec_index.get(aid, []),
        })

    # Aggregate totals.
    totals = {
        "accounts": len(accounts),
        "live_accounts": sum(1 for a in accounts if a.get("live_trading_enabled")),
        "open_positions": sum(a["open_position_count"] for a in accounts),
        "active_protections": sum(a["protection_count"] for a in accounts),
        "active_executors": sum(len(a["active_executors"]) for a in accounts),
        "reserved_usd": round(sum(a["reserved_usd"] for a in accounts), 2),
    }

    return {"accounts": accounts, "totals": totals, "ts": now_iso()}


def _portfolio_refresh(client, payload):
    from ..trading.account_refresh import refresh_account_marks

    account_id = payload.get("account_id") or None
    run_executors = bool(payload.get("run_executors", True))
    return refresh_account_marks(
        client.config,
        account_id=account_id,
        persist_snapshot=True,
        run_executors=run_executors,
    )


def _orders_list(client, payload):
    from ..trading.order_tracker import OrderTracker

    paths = client.config.paths
    tracker = OrderTracker(paths)
    account_id = payload.get("account_id") or None
    state = (payload.get("state") or "").strip()
    limit = int(payload.get("limit") or 200)

    if state == "active":
        rows = tracker.active_orders(account_id=account_id)
    elif state == "lost":
        rows = tracker.lost_orders(account_id=account_id)
    elif state == "cached":
        rows = tracker.cached_orders(account_id=account_id)
    else:
        # "Recent" = active + cached (recently terminal). # asks for active/cached/lost grouping in one view; merging
        # active and cached is the closest single-call surface.
        active = tracker.active_orders(account_id=account_id)
        cached = tracker.cached_orders(account_id=account_id)
        rows = list(active) + list(cached)
    order_rows = [_order_summary(r) for r in rows]
    if state in ("", "recent", "cached"):
        order_rows.extend(
            _strategy_history_orders(
                paths,
                account_id=account_id,
                limit=max(limit, 500),
            )
        )
    order_rows = _dedupe_order_rows(order_rows)
    order_rows.sort(key=lambda r: float(r.get("created_at") or 0.0), reverse=True)
    return {
        "orders": order_rows[:limit],
        "filter": {"account_id": account_id, "state": state or "recent"},
    }


def _orders_cancel(client, payload):
    from ..trading.order_tracker import OrderTracker

    order_id = str(payload.get("order_id") or "")
    if not order_id:
        return {"ok": False, "error": "order_id required"}
    tracker = OrderTracker(client.config.paths)
    tracker.request_cancel(order_id)
    jsonl.append(client.config.paths.journal("operator"), {
        "kind": "order.cancel_requested",
        "ts": now_iso(),
        "order_id": order_id,
        "operator": payload.get("operator"),
        "reason": payload.get("reason") or "operator",
    })
    return {"ok": True, "order_id": order_id, "state": "cancel_requested"}


def _executors_list(client, payload):
    from ..trading.executors import ExecutorOrchestrator

    orchestrator = ExecutorOrchestrator(client.config)
    state = (payload.get("state") or "").strip()
    account_id = payload.get("account_id") or None
    limit = int(payload.get("limit") or 100)
    if state == "active":
        runs = orchestrator.list_active(account_id=account_id)
    else:
        runs = orchestrator.list_recent(limit=limit)
        if account_id:
            runs = [r for r in runs if r.account_id == account_id]
    return {
        "executors": [_executor_summary(r) for r in runs[:limit]],
        "filter": {"account_id": account_id, "state": state or "recent"},
    }


def _executors_cancel(client, payload):
    from ..trading.executors import ExecutorOrchestrator

    executor_id = str(payload.get("executor_id") or "")
    if not executor_id:
        return {"ok": False, "error": "executor_id required"}
    orchestrator = ExecutorOrchestrator(client.config)
    run = orchestrator.cancel(executor_id, reason=str(payload.get("reason") or "operator"))
    jsonl.append(client.config.paths.journal("operator"), {
        "kind": "executor.cancel_requested",
        "ts": now_iso(),
        "executor_id": executor_id,
        "operator": payload.get("operator"),
        "reason": payload.get("reason") or "operator",
    })
    return {
        "ok": run is not None,
        "executor_id": executor_id,
        "state": run.state if run else "not_found",
    }


def _reconciliation_reports(client, payload):
    from ..trading.reconciliation import ReconciliationStore

    store = ReconciliationStore(client.config.paths)
    account_id = payload.get("account_id") or None
    scope = payload.get("scope") or None
    limit = int(payload.get("limit") or 50)
    rows = store.recent(account_id=account_id, scope=scope, limit=limit)
    worst = store.worst_recent(
        account_id=account_id,
        within_seconds=float(payload.get("worst_window_s") or 1800),
    )
    return {
        "reports": [r.asdict() for r in rows],
        "worst_recent": worst.asdict() if worst else None,
        "filter": {"account_id": account_id, "scope": scope},
    }


def _reconciliation_run(client, payload):
    from ..trading.reconciliation import reconcile

    account_id = payload.get("account_id") or None
    rep = reconcile(client.config, account_id=account_id, persist=True)
    return {"report": rep.asdict()}


def _protections_list(client, payload):
    from ..trading.protection_store import ProtectionStore

    store = ProtectionStore(client.config.paths)
    account_id = payload.get("account_id") or None
    rules = store.list_active(account_id=account_id)
    return {"protections": [r.asdict() for r in rules]}


def _strategy_promotions_list(client, payload):
    from ..trading.promotion import PromotionStore

    strategy_id = str(payload.get("strategy_id") or "")
    if not strategy_id:
        return {"ok": False, "error": "strategy_id required"}
    rows = PromotionStore(client.config.paths).list_for(
        strategy_id, limit=int(payload.get("limit") or 50)
    )
    return {"promotions": [r.asdict() for r in rows]}


def _strategy_promotions_request(client, payload):
    from ..trading.promotion import request_promotion

    strategy_id = str(payload.get("strategy_id") or "")
    if not strategy_id:
        return {"ok": False, "error": "strategy_id required"}
    rec = request_promotion(
        client.config,
        strategy_id=strategy_id,
        target=payload.get("target"),
        operator=payload.get("operator"),
        notes=payload.get("notes"),
    )
    return {"ok": True, "promotion": rec.asdict()}


def _strategy_promotions_apply(client, payload):
    from ..trading.promotion import apply_promotion

    promotion = str(payload.get("promotion_id") or "")
    if not promotion:
        return {"ok": False, "error": "promotion_id required"}
    rec = apply_promotion(client.config, promotion)
    return {"ok": True, "promotion": rec.asdict()}


def _strategy_evidence_record(client, payload):
    from ..trading.promotion import EvidenceStore

    strategy_id = str(payload.get("strategy_id") or "")
    kind = str(payload.get("kind") or "")
    if not strategy_id or not kind:
        return {"ok": False, "error": "strategy_id and kind required"}
    ev = EvidenceStore(client.config.paths).record(
        strategy_id=strategy_id,
        kind=kind,  # type: ignore[arg-type]
        passed=bool(payload.get("passed", True)),
        payload=payload.get("payload") or {},
        artifact_ref=payload.get("artifact_ref"),
        operator=payload.get("operator"),
        ttl_seconds=payload.get("ttl_seconds"),
    )
    return {"ok": True, "evidence": ev.asdict()}


def _incidents(client, payload):
    """Aggregate the unresolved incidents the operator should see.

    Pulls from:

    * ``ReconciliationStore`` for ``action_required`` / ``trading_halted``
      reports inside the lookback window.
    * ``OrderTracker.lost_orders`` for orders the venue lost track of.
    * Recent journal entries with ``kind`` matching incident keys.
    """
    from ..trading.order_tracker import OrderTracker
    from ..trading.reconciliation import ReconciliationStore

    paths = client.config.paths
    window_s = float(payload.get("window_s") or 3600)
    cutoff = time.time() - window_s

    incidents: list[dict[str, Any]] = []
    for rep in ReconciliationStore(paths).recent(limit=200):
        if rep.severity in ("action_required", "trading_halted") and rep.ts >= cutoff:
            incidents.append({
                "kind": "reconcile_drift",
                "severity": rep.severity,
                "report_id": rep.report_id,
                "account_id": rep.account_id,
                "ts": rep.ts,
                "summary": rep.summary,
                "issues": rep.issues,
            })

    for o in OrderTracker(paths).lost_orders():
        incidents.append({
            "kind": "lost_order",
            "severity": "action_required",
            "order_id": o.order_id,
            "account_id": o.account_id,
            "market": o.market,
            "client_order_id": o.client_order_id,
            "ts": o.created_at,
        })

    # Tail the trading journal for max-loss / auth incidents written by
    # other layers (the journal is already append-only, so this is a
    # best-effort read — older incidents fall off as the lookback shrinks).
    journal_path = paths.journal("trading")
    if journal_path.exists():
        try:
            for r in jsonl.read_all(journal_path)[-500:]:
                kind = str(r.get("kind") or "")
                if kind in ("auth.error", "max_loss.breach", "snapshot.unhealthy"):
                    ts_raw = r.get("ts") or ""
                    incidents.append({
                        "kind": kind,
                        "severity": r.get("severity") or "warning",
                        "ts": ts_raw,
                        "payload": r,
                    })
        except Exception:
            pass

    incidents.sort(key=lambda x: str(x.get("ts") or ""), reverse=True)
    return {"incidents": incidents, "ts": now_iso()}


def _kill_switch_get(client, _payload):
    return {
        "kill_switch": bool(client.config.kill_switch()),
        "live_trading_enabled": bool(client.config.live_trading_enabled()),
        "ts": now_iso(),
    }


def _kill_switch_set(client, payload):
    """Toggle the runtime kill switch.

    Persists into the workspace config so the change survives restart.
    Operators that need a transient override can still set
    ``NERYA_KILL_SWITCH=1`` in the environment.
    """
    from ..core import yaml_io

    enabled = bool(payload.get("enabled"))
    cfg_path = client.config.paths.config
    doc = yaml_io.load(cfg_path, default={}) or {}
    runtime = doc.setdefault("runtime", {})
    runtime["kill_switch"] = enabled
    cfg_path.write_text(__import__("yaml").safe_dump(doc), encoding="utf-8")
    # Reflect the change on the in-memory Config so subsequent calls in
    # the same process pick it up without a restart.
    client.config.data.setdefault("runtime", {})["kill_switch"] = enabled
    jsonl.append(client.config.paths.journal("operator"), {
        "kind": "kill_switch.set",
        "ts": now_iso(),
        "enabled": enabled,
        "operator": payload.get("operator"),
    })
    return {"ok": True, "kill_switch": enabled}


def _risk_evaluations(client, payload):
    """Return recent rejected / escalated risk evaluations.

    Surfaces ``RiskGate`` rejections in the
    dashboard so operators see *why* an intent was blocked and which
    button to click. ``fix_hints`` is decoded from ``snapshot_json``
    (where ``RiskGate._persist`` stows it under ``_fix_hints``); if the
    embed is missing (older rows / hand-edited DBs) we re-derive on
    the fly so the field is always populated.
    """

    from ..db.sqlite import connect
    from ..trading.risk import derive_fix_hints

    body = payload or {}
    paths = client.config.paths
    strategy_id = str(body.get("strategy_id") or "").strip()
    account_id = str(body.get("account_id") or "").strip()
    decisions = body.get("decisions") or ["reject", "escalate"]
    if not isinstance(decisions, (list, tuple)):
        decisions = [str(decisions)]
    decisions = [str(d) for d in decisions if d]
    limit = int(body.get("limit") or 50)
    limit = max(1, min(limit, 500))
    since_seconds = body.get("since_seconds")
    since_ts: float | None = None
    if since_seconds is not None and since_seconds != "":
        try:
            since_ts = time.time() - float(since_seconds)
        except Exception:
            since_ts = None
    con = connect(paths.db)
    where: list[str] = []
    params: list[Any] = []
    if decisions:
        placeholders = ",".join("?" * len(decisions))
        where.append(f"decision IN ({placeholders})")
        params.extend(decisions)
    if strategy_id:
        where.append("strategy_id = ?")
        params.append(strategy_id)
    if account_id:
        where.append("account_id = ?")
        params.append(account_id)
    if since_ts is not None:
        where.append("ts >= ?")
        params.append(since_ts)
    sql = "SELECT * FROM risk_evaluations"
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY ts DESC LIMIT ?"
    params.append(limit)
    try:
        rows = con.execute(sql, tuple(params)).fetchall()
    except Exception as exc:
        log.warning("/risk/evaluations failed: %s", exc)
        return {"evaluations": [], "ts": time.time(), "error": str(exc)}
    out: list[dict[str, Any]] = []
    import json as _json

    for row in rows:
        try:
            reasons = _json.loads(row["reasons_json"] or "[]")
        except Exception:
            reasons = []
        try:
            snapshot_blob = _json.loads(row["snapshot_json"] or "{}")
        except Exception:
            snapshot_blob = {}
        fix_hints = (
            snapshot_blob.pop("_fix_hints", None)
            if isinstance(snapshot_blob, dict)
            else None
        )
        if not fix_hints:
            # Best-effort fallback: derive from reasons. Without an
            # ``intent`` we still substitute strategy_id/account_id so
            # deep-links work.
            fix_hints = derive_fix_hints(reasons)
            for hint in fix_hints:
                href = hint.get("href")
                if isinstance(href, str) and "{strategy_id}" in href:
                    hint["href"] = href.replace(
                        "{strategy_id}", row["strategy_id"] or ""
                    ).replace("{account_id}", row["account_id"] or "")
        out.append(
            {
                "risk_evaluation_id": row["risk_evaluation_id"],
                "intent_id": row["intent_id"],
                "plan_id": row["plan_id"],
                "strategy_id": row["strategy_id"],
                "account_id": row["account_id"],
                "decision": row["decision"],
                "notional_usd": row["notional_usd"],
                "reasons": reasons,
                "fix_hints": fix_hints,
                "snapshot": snapshot_blob,
                "ts": row["ts"],
            }
        )
    return {"evaluations": out, "ts": time.time(), "count": len(out)}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _legacy_ledger_position_rows(paths, profile) -> list[dict[str, Any]]:
    try:
        from ..trading.position_book import PositionBook
        from ..trading.virtual_ledger import open_ledger

        ledger = open_ledger(paths, profile.id, profile.initial_balance_usd)
        snap = ledger.snapshot()
        marks = {
            pos.market: float(pos.mark_price or pos.avg_entry_price or 0.0)
            for pos in PositionBook(paths).open_positions(account_id=profile.id)
            if float(pos.mark_price or pos.avg_entry_price or 0.0) > 0
        }
        rows = []
        for market, pos in (snap.get("positions") or {}).items():
            size = float((pos or {}).get("size") or 0.0)
            if not size:
                continue
            avg = float((pos or {}).get("avg_price") or 0.0)
            mark = float(marks.get(str(market)) or avg or 0.0)
            rows.append({
                "account_id": profile.id,
                "market": str(market),
                "size": size,
                "avg_price": avg,
                "realized_pnl_usd": float((pos or {}).get("realized_pnl_usd") or 0.0),
                "mark_price": mark,
                "market_value_usd": abs(size * mark),
                "notional_usd": abs(size * mark),
                "unrealized_pnl_usd": (mark - avg) * size if avg and mark else 0.0,
            })
        return rows
    except Exception:
        return []


def _strategy_history_orders(
    paths,
    *,
    account_id: str | None,
    limit: int,
) -> list[dict[str, Any]]:
    from ..trading.strategies import list_strategies

    out: dict[str, dict[str, Any]] = {}
    for strategy in list_strategies(paths):
        intents = _strategy_history_intents(paths, strategy.id)
        orders_path = paths.strategy_history(strategy.id) / "orders.jsonl"
        if not orders_path.exists():
            continue
        for record in jsonl.read_all(orders_path):
            payload = record.get("payload") or {}
            if not isinstance(payload, dict):
                continue
            order_id = str(payload.get("order_id") or "").strip()
            if not order_id:
                continue
            intent_id = str(payload.get("intent_id") or "").strip()
            intent = intents.get(intent_id, {})
            fills = payload.get("fills") if isinstance(payload.get("fills"), list) else []
            first_fill = fills[0] if fills and isinstance(fills[0], dict) else {}
            row_account_id = str(
                payload.get("account_id")
                or intent.get("account_id")
                or ""
            )
            if account_id and row_account_id != account_id:
                continue
            ts = _history_ts(record.get("ts") or first_fill.get("ts") or intent.get("created_at"))
            state = _history_order_state(payload)
            row = {
                "order_id": order_id,
                "client_order_id": payload.get("client_order_id"),
                "exchange_order_id": payload.get("exchange_order_id"),
                "account_id": row_account_id,
                "strategy_id": str(payload.get("strategy_id") or strategy.id),
                "market": (
                    payload.get("market")
                    or first_fill.get("market")
                    or intent.get("market")
                    or ""
                ),
                "side": payload.get("side") or intent.get("side") or "",
                "order_type": payload.get("order_type") or intent.get("order_type") or "market",
                "size_base": payload.get("size_base") or payload.get("filled_size"),
                "filled_size": payload.get("filled_size"),
                "avg_price": payload.get("avg_price"),
                "state": state,
                "created_at": ts,
                "submitted_at": ts,
                "last_seen_at": ts,
                "terminal_at": ts if state in _TERMINAL_ORDER_STATES else None,
                "executor_id": payload.get("executor_id"),
                "intent_id": intent_id or None,
                "plan_id": payload.get("plan_id"),
                "source": "strategy_history",
            }
            existing = out.get(order_id)
            if existing is None or _history_order_prefer(row, existing):
                out[order_id] = row
    rows = list(out.values())
    rows.sort(key=lambda r: float(r.get("created_at") or 0.0), reverse=True)
    return rows[:limit]


def _strategy_history_intents(paths, strategy_id: str) -> dict[str, dict[str, Any]]:
    intents_path = paths.strategy_history(strategy_id) / "intents.jsonl"
    if not intents_path.exists():
        return {}
    intents: dict[str, dict[str, Any]] = {}
    for record in jsonl.read_all(intents_path):
        intent = record.get("intent") or {}
        if not isinstance(intent, dict):
            continue
        intent_id = str(intent.get("intent_id") or "").strip()
        if intent_id and intent_id not in intents:
            intents[intent_id] = intent
    return intents


_TERMINAL_ORDER_STATES = {"filled", "canceled", "rejected", "expired", "failed"}


def _history_order_state(payload: dict[str, Any]) -> str:
    raw = str(payload.get("state") or payload.get("status") or "").strip().lower()
    if raw == "cancelled":
        raw = "canceled"
    if raw:
        return raw
    if float(payload.get("filled_size") or 0.0) > 0:
        return "filled"
    return "unknown"


def _history_order_prefer(candidate: dict[str, Any], existing: dict[str, Any]) -> bool:
    candidate_ts = float(candidate.get("created_at") or 0.0)
    existing_ts = float(existing.get("created_at") or 0.0)
    if candidate_ts != existing_ts:
        return candidate_ts > existing_ts
    candidate_has_fill = float(candidate.get("filled_size") or 0.0) > 0
    existing_has_fill = float(existing.get("filled_size") or 0.0) > 0
    return candidate_has_fill and not existing_has_fill


def _history_ts(raw: Any) -> float:
    if raw is None:
        return 0.0
    if isinstance(raw, (int, float)):
        value = float(raw)
        return value / 1000.0 if value > 1e12 else value
    text = str(raw).strip()
    if not text:
        return 0.0
    try:
        return _history_ts(float(text))
    except ValueError:
        pass
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return 0.0


def _dedupe_order_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    for row in rows:
        order_id = str(row.get("order_id") or "").strip()
        if not order_id or order_id in out:
            continue
        out[order_id] = row
    return list(out.values())


def _order_summary(order) -> dict[str, Any]:
    return {
        "order_id": order.order_id,
        "client_order_id": order.client_order_id,
        "exchange_order_id": order.exchange_order_id,
        "account_id": order.account_id,
        "strategy_id": order.strategy_id,
        "market": order.market,
        "side": order.side,
        "order_type": order.order_type,
        "size_base": order.size_base,
        "filled_size": order.filled_size,
        "avg_price": order.avg_price,
        "state": order.state,
        "created_at": order.created_at,
        "submitted_at": order.submitted_at,
        "last_seen_at": order.last_seen_at,
        "terminal_at": order.terminal_at,
        "executor_id": order.executor_id,
        "intent_id": order.intent_id,
        "plan_id": order.plan_id,
    }


def _executor_summary(run) -> dict[str, Any]:
    return {
        "executor_id": run.executor_id,
        "kind": run.kind,
        "state": run.state,
        "account_id": run.account_id,
        "strategy_id": run.strategy_id,
        "market": run.market,
        "created_at": run.created_at,
        "last_heartbeat": run.last_heartbeat,
        "terminal_at": run.terminal_at,
        "close_type": run.close_type,
        "result_json": run.result_json,
        "order_ids": list(run.order_ids or []),
        "reservation_ids": list(run.reservation_ids or []),
        "position_id": run.position_id,
        "intent_id": run.intent_id,
        "plan_id": run.plan_id,
    }


# ---------------------------------------------------------------------------
# Route registration (POST + matching GET aliases for curl-friendly use)
# ---------------------------------------------------------------------------


def routes() -> list[tuple[str, str, Any]]:
    out: list[tuple[str, str, Any]] = []
    spec: list[tuple[str, Any]] = [
        ("/portfolio/health", _portfolio_health),
        ("/portfolio/refresh", _portfolio_refresh),
        ("/orders/list", _orders_list),
        ("/orders/cancel", _orders_cancel),
        ("/executors/list", _executors_list),
        ("/executors/cancel", _executors_cancel),
        ("/reconciliation/reports", _reconciliation_reports),
        ("/reconciliation/run", _reconciliation_run),
        ("/protections/list", _protections_list),
        ("/strategy/promotions/list", _strategy_promotions_list),
        ("/strategy/promotions/request", _strategy_promotions_request),
        ("/strategy/promotions/apply", _strategy_promotions_apply),
        ("/strategy/evidence/record", _strategy_evidence_record),
        ("/incidents", _incidents),
        ("/kill_switch/get", _kill_switch_get),
        ("/kill_switch/set", _kill_switch_set),
        ("/risk/evaluations", _risk_evaluations),
    ]
    for path, handler in spec:
        out.append(("POST", path, handler))
        # Accept GET on the read-only endpoints so the plan's
        # ``GET /portfolio/health`` smoke target works against the
        # local server without needing a JSON body.
        if handler.__name__ in (
            "_portfolio_health", "_orders_list", "_executors_list",
            "_reconciliation_reports", "_protections_list",
            "_strategy_promotions_list", "_incidents",
            "_kill_switch_get", "_risk_evaluations",
        ):
            out.append(("GET", path, handler))
    return out
