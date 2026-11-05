"""Durable SuperAgent-style control records for assistant task families.

The existing command store owns execution and leases. This module owns the
small coordination vocabulary around it: frozen mandate, assignment, worker
reports, input receipts, and explicit handback. Model-authored values are
accepted as reports, never as proof of completion or permission.
"""
from __future__ import annotations

import json
import hashlib
import time
import uuid
from contextlib import closing
from typing import Any, Mapping

from ..db.sqlite import connect

REPORT_STATES = frozenset({"progress", "ready", "waiting", "blocked", "failed"})
INPUT_STATES = frozenset({"delivered", "read", "replay_pending", "reconciled"})
HANDBACK_STATES = frozenset({"open", "routed", "asked", "deferred"})
TRIGGER_DELIVERY_STATES = frozenset({"delivered", "deduped", "blocked", "failed"})
DEFAULT_TRIGGER_MAX_ATTEMPTS = 3
_TEXT_LIMIT = 4000


def role_plan_for(task_kind: str) -> list[dict[str, str]]:
    if task_kind in {"research", "strategy"}:
        return [
            {"role": "researcher", "persona": "market_analyst"},
            {"role": "skeptic", "persona": "risk_critic"},
            {"role": "strategy_author", "persona": "strategy_reviewer"},
            {"role": "validator", "persona": "quant_researcher"},
            {"role": "coordinator", "persona": "research_manager"},
        ]
    return [{"role": "coordinator", "persona": "research_manager"}]


def coordination_instruction(task_kind: str) -> str:
    roles = role_plan_for(task_kind)
    roster = ", ".join(f"{row['role']}={row['persona']}" for row in roles)
    if task_kind not in {"research", "strategy"}:
        return ""
    return (
        "\n\nDurable task coordination contract: you are the Coordinator. "
        "For this research/strategy task, use the existing team_run tool once "
        "with the declared roles below and synthesize only after their reports "
        "are available: " + roster + ". Keep the task paper/proposal-only. "
        "A ready report is not completion; cite producer evidence, use "
        "worker_report for progress or waiting, and use task_handback when a "
        "request exceeds this mandate. Live trading remains behind Risk Gate "
        "and Approval Gate."
    )


def _text(value: Any, limit: int = _TEXT_LIMIT) -> str:
    return str(value or "").strip()[:limit]


def _list(value: Any, limit: int = 32) -> list[str]:
    if not isinstance(value, list):
        return []
    return [_text(item, 512) for item in value[:limit] if _text(item, 512)]


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _load(value: Any, default: Any) -> Any:
    try:
        parsed = json.loads(value or "")
    except (TypeError, ValueError):
        return default
    return parsed


def ensure_schema(paths) -> None:
    """Create task-control tables for workspaces initialized before migration v31."""
    with closing(connect(paths.db)) as con:
        con.executescript("""
        CREATE TABLE IF NOT EXISTS task_mandates (
            task_id TEXT PRIMARY KEY, parent_task_id TEXT NOT NULL DEFAULT '',
            command_id TEXT NOT NULL, session_id TEXT NOT NULL, actor_id TEXT NOT NULL,
            task_kind TEXT NOT NULL, objective TEXT NOT NULL, scope_json TEXT NOT NULL,
            allowed_tools_json TEXT NOT NULL, execution_mode TEXT NOT NULL,
            policy_revision TEXT NOT NULL, state TEXT NOT NULL DEFAULT 'active',
            created_at REAL NOT NULL, updated_at REAL NOT NULL
        );
        CREATE TABLE IF NOT EXISTS task_assignments (
            assignment_id TEXT PRIMARY KEY, task_id TEXT NOT NULL, session_id TEXT NOT NULL,
            command_id TEXT NOT NULL, attempt INTEGER NOT NULL DEFAULT 1,
            state TEXT NOT NULL DEFAULT 'pending', worker_owner TEXT NOT NULL DEFAULT '',
            started_at REAL, ended_at REAL, last_report_id TEXT NOT NULL DEFAULT '',
            created_at REAL NOT NULL, updated_at REAL NOT NULL
        );
        CREATE TABLE IF NOT EXISTS task_worker_reports (
            report_id TEXT PRIMARY KEY, task_id TEXT NOT NULL, assignment_id TEXT NOT NULL,
            command_id TEXT NOT NULL, state TEXT NOT NULL, summary TEXT NOT NULL,
            human_action TEXT NOT NULL DEFAULT '', artifacts_json TEXT NOT NULL,
            evidence_refs_json TEXT NOT NULL, next_action TEXT NOT NULL DEFAULT '',
            safe_to_retry INTEGER NOT NULL DEFAULT 0, in_scope INTEGER NOT NULL DEFAULT 1,
            created_at REAL NOT NULL
        );
        CREATE TABLE IF NOT EXISTS task_inputs (
            input_id TEXT PRIMARY KEY, task_id TEXT NOT NULL, assignment_id TEXT NOT NULL,
            command_id TEXT NOT NULL, sender_session_id TEXT NOT NULL, body TEXT NOT NULL,
            state TEXT NOT NULL, delivered_at REAL NOT NULL, read_at REAL, replay_count INTEGER NOT NULL DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS task_handbacks (
            handback_id TEXT PRIMARY KEY, task_id TEXT NOT NULL, assignment_id TEXT NOT NULL,
            command_id TEXT NOT NULL, request TEXT NOT NULL, reason TEXT NOT NULL,
            state TEXT NOT NULL DEFAULT 'open', target_task_id TEXT NOT NULL DEFAULT '',
            created_at REAL NOT NULL, updated_at REAL NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_task_reports_task ON task_worker_reports(task_id, created_at);
        CREATE INDEX IF NOT EXISTS idx_task_inputs_state ON task_inputs(task_id, state);
        CREATE TABLE IF NOT EXISTS task_trigger_subscriptions (
            subscription_id TEXT PRIMARY KEY, task_id TEXT NOT NULL, source TEXT NOT NULL,
            kind TEXT NOT NULL DEFAULT '', match_json TEXT NOT NULL, enabled INTEGER NOT NULL DEFAULT 1,
            last_cursor TEXT NOT NULL DEFAULT '', created_at REAL NOT NULL, updated_at REAL NOT NULL
        );
        CREATE UNIQUE INDEX IF NOT EXISTS idx_task_subscription_unique
            ON task_trigger_subscriptions(task_id, source, kind, match_json);
        CREATE TABLE IF NOT EXISTS task_trigger_events (
            event_id TEXT PRIMARY KEY, source TEXT NOT NULL, kind TEXT NOT NULL,
            source_cursor TEXT NOT NULL DEFAULT '', idempotency_key TEXT NOT NULL DEFAULT '',
            occurred_at TEXT NOT NULL, payload_json TEXT NOT NULL, state TEXT NOT NULL DEFAULT 'recorded',
            created_at REAL NOT NULL
        );
        CREATE UNIQUE INDEX IF NOT EXISTS idx_task_trigger_idempotency
            ON task_trigger_events(source, idempotency_key) WHERE idempotency_key <> '';
        CREATE TABLE IF NOT EXISTS task_trigger_deliveries (
            delivery_id TEXT PRIMARY KEY, event_id TEXT NOT NULL, task_id TEXT NOT NULL,
            subscription_id TEXT NOT NULL, state TEXT NOT NULL, input_id TEXT NOT NULL DEFAULT '',
            command_id TEXT NOT NULL DEFAULT '', error TEXT NOT NULL DEFAULT '',
            created_at REAL NOT NULL, updated_at REAL NOT NULL,
            attempt_count INTEGER NOT NULL DEFAULT 0, max_attempts INTEGER NOT NULL DEFAULT 3,
            next_retry_at REAL, last_attempt_at REAL
        );
        CREATE UNIQUE INDEX IF NOT EXISTS idx_task_trigger_delivery_unique
            ON task_trigger_deliveries(event_id, task_id);
        CREATE TABLE IF NOT EXISTS task_trigger_sources (
            source TEXT PRIMARY KEY, cursor TEXT NOT NULL DEFAULT '', state TEXT NOT NULL DEFAULT 'ready',
            consecutive_failures INTEGER NOT NULL DEFAULT 0, last_error TEXT NOT NULL DEFAULT '',
            updated_at REAL NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_task_trigger_event_source ON task_trigger_events(source, created_at);
        CREATE INDEX IF NOT EXISTS idx_task_trigger_delivery_task ON task_trigger_deliveries(task_id, created_at);
        CREATE TABLE IF NOT EXISTS market_watch_state (
            watch_id TEXT PRIMARY KEY, market TEXT NOT NULL, last_sample_ts REAL,
            last_price REAL, last_funding_rate REAL, last_volatility_pct REAL,
            last_alerts_json TEXT NOT NULL DEFAULT '{}', last_cursor TEXT NOT NULL DEFAULT '',
            updated_at REAL NOT NULL
        );
        CREATE TABLE IF NOT EXISTS task_trigger_dead_letters (
            dead_letter_id TEXT PRIMARY KEY, delivery_id TEXT NOT NULL, event_id TEXT NOT NULL,
            task_id TEXT NOT NULL, source TEXT NOT NULL, kind TEXT NOT NULL, error TEXT NOT NULL,
            attempts INTEGER NOT NULL, state TEXT NOT NULL DEFAULT 'open', replay_count INTEGER NOT NULL DEFAULT 0,
            created_at REAL NOT NULL, updated_at REAL NOT NULL
        );
        CREATE UNIQUE INDEX IF NOT EXISTS idx_task_trigger_dead_delivery ON task_trigger_dead_letters(delivery_id);
        CREATE INDEX IF NOT EXISTS idx_task_trigger_dead_state ON task_trigger_dead_letters(state, created_at);
        """)
        columns = {row[1] for row in con.execute("PRAGMA table_info(task_trigger_deliveries)").fetchall()}
        for name, definition in (("attempt_count", "INTEGER NOT NULL DEFAULT 0"), ("max_attempts", "INTEGER NOT NULL DEFAULT 3"), ("next_retry_at", "REAL"), ("last_attempt_at", "REAL")):
            if name not in columns:
                con.execute(f"ALTER TABLE task_trigger_deliveries ADD COLUMN {name} {definition}")
        con.commit()


def create_mandate(paths, *, task_id: str, command_id: str, session_id: str, actor_id: str,
                   task_kind: str, objective: str, scope: Mapping[str, Any] | None = None,
                   allowed_tools: list[str] | None = None, execution_mode: str = "paper",
                   policy_revision: str = "v1", parent_task_id: str = "") -> dict[str, Any]:
    ensure_schema(paths)
    now = time.time()
    row = {
        "task_id": _text(task_id, 128), "parent_task_id": _text(parent_task_id, 128),
        "command_id": _text(command_id, 128), "session_id": _text(session_id, 128), "actor_id": _text(actor_id, 256),
        "task_kind": _text(task_kind, 64), "objective": _text(objective, 16000), "scope": dict(scope or {}),
        "allowed_tools": _list(allowed_tools, 128), "execution_mode": _text(execution_mode, 32),
        "policy_revision": _text(policy_revision, 128), "state": "active", "created_at": now, "updated_at": now,
    }
    with closing(connect(paths.db)) as con:
        con.execute("""INSERT OR IGNORE INTO task_mandates
            (task_id,parent_task_id,command_id,session_id,actor_id,task_kind,objective,scope_json,
             allowed_tools_json,execution_mode,policy_revision,state,created_at,updated_at)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""", (
                row["task_id"], row["parent_task_id"], row["command_id"], row["session_id"], row["actor_id"],
                row["task_kind"], row["objective"], _json(row["scope"]), _json(row["allowed_tools"]),
                row["execution_mode"], row["policy_revision"], row["state"], now, now))
        con.commit()
    return row


def assignment_for(paths, *, task_id: str, session_id: str, command_id: str, attempt: int = 1) -> dict[str, Any]:
    ensure_schema(paths)
    now = time.time()
    assignment = {
        "assignment_id": "asg_" + uuid.uuid4().hex[:24], "task_id": _text(task_id, 128),
        "session_id": _text(session_id, 128), "command_id": _text(command_id, 128),
        "attempt": max(1, int(attempt)), "state": "pending", "worker_owner": "",
        "started_at": None, "ended_at": None, "last_report_id": "", "created_at": now, "updated_at": now,
    }
    with closing(connect(paths.db)) as con:
        con.execute("""INSERT INTO task_assignments
            (assignment_id,task_id,session_id,command_id,attempt,state,worker_owner,started_at,ended_at,last_report_id,created_at,updated_at)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""", tuple(assignment.values()))
        con.commit()
    return assignment


def assignment_for_command(paths, command_id: str) -> dict[str, Any] | None:
    ensure_schema(paths)
    with closing(connect(paths.db)) as con:
        row = con.execute("SELECT * FROM task_assignments WHERE command_id=? ORDER BY created_at DESC LIMIT 1", (command_id,)).fetchone()
    return dict(row) if row else None


def assignment_started(paths, assignment_id: str, *, worker_owner: str = "") -> dict[str, Any]:
    ensure_schema(paths)
    now = time.time()
    with closing(connect(paths.db)) as con:
        con.execute("UPDATE task_assignments SET state='running',worker_owner=?,started_at=COALESCE(started_at,?),updated_at=? WHERE assignment_id=?",
                    (_text(worker_owner, 128), now, now, assignment_id))
        con.commit()
        row = con.execute("SELECT * FROM task_assignments WHERE assignment_id=?", (assignment_id,)).fetchone()
    if not row:
        raise ValueError("task_assignment_not_found")
    return dict(row)


def assignment_ended(paths, assignment_id: str, *, state: str) -> dict[str, Any]:
    ensure_schema(paths)
    if state not in {"ended", "failed", "blocked", "waiting"}:
        raise ValueError("invalid_task_assignment_state")
    now = time.time()
    with closing(connect(paths.db)) as con:
        con.execute("UPDATE task_assignments SET state=?,ended_at=?,updated_at=? WHERE assignment_id=?",
                    (state, now, now, assignment_id))
        con.commit()
        row = con.execute("SELECT * FROM task_assignments WHERE assignment_id=?", (assignment_id,)).fetchone()
    if not row:
        raise ValueError("task_assignment_not_found")
    return dict(row)


def input_receipt(paths, *, task_id: str, assignment_id: str, command_id: str, sender_session_id: str,
                  body: str, state: str = "delivered", input_id: str | None = None) -> dict[str, Any]:
    if state not in INPUT_STATES:
        raise ValueError("invalid_task_input_state")
    ensure_schema(paths)
    now = time.time()
    row = {"input_id": input_id or "input_" + uuid.uuid4().hex[:24], "task_id": _text(task_id, 128),
           "assignment_id": _text(assignment_id, 128), "command_id": _text(command_id, 128),
           "sender_session_id": _text(sender_session_id, 128), "body": _text(body, 12000),
           "state": state, "delivered_at": now, "read_at": now if state == "read" else None, "replay_count": 0}
    with closing(connect(paths.db)) as con:
        con.execute("""INSERT OR IGNORE INTO task_inputs
            (input_id,task_id,assignment_id,command_id,sender_session_id,body,state,delivered_at,read_at,replay_count)
            VALUES (?,?,?,?,?,?,?,?,?,?)""", tuple(row.values()))
        con.commit()
    return row


def mark_input(paths, input_id: str, *, state: str = "read", replay: bool = False) -> dict[str, Any]:
    if state not in INPUT_STATES:
        raise ValueError("invalid_task_input_state")
    ensure_schema(paths)
    with closing(connect(paths.db)) as con:
        row = con.execute("SELECT * FROM task_inputs WHERE input_id=?", (input_id,)).fetchone()
        if not row:
            raise ValueError("task_input_not_found")
        count = int(row["replay_count"]) + (1 if replay else 0)
        read_at = time.time() if state == "read" else row["read_at"]
        con.execute("UPDATE task_inputs SET state=?,read_at=?,replay_count=? WHERE input_id=?", (state, read_at, count, input_id))
        con.commit()
        updated = dict(con.execute("SELECT * FROM task_inputs WHERE input_id=?", (input_id,)).fetchone())
    return updated


def worker_report(paths, *, task_id: str, assignment_id: str, command_id: str, payload: Mapping[str, Any]) -> dict[str, Any]:
    state = _text(payload.get("status") or payload.get("state"), 32)
    if state not in REPORT_STATES:
        raise ValueError("invalid_worker_report_state")
    human_action = _text(payload.get("human_action"), 2000)
    if state == "waiting" and not human_action:
        raise ValueError("waiting_report_requires_human_action")
    if state == "blocked" and not _text(payload.get("summary")):
        raise ValueError("blocked_report_requires_summary")
    ensure_schema(paths)
    now = time.time()
    report = {"report_id": "rpt_" + uuid.uuid4().hex[:24], "task_id": _text(task_id, 128),
              "assignment_id": _text(assignment_id, 128), "command_id": _text(command_id, 128),
              "state": state, "summary": _text(payload.get("summary"), _TEXT_LIMIT),
              "human_action": human_action, "artifacts": _list(payload.get("artifacts")),
              "evidence_refs": _list(payload.get("evidence_refs")), "next_action": _text(payload.get("next_action"), 2000),
              "safe_to_retry": bool(payload.get("safe_to_retry", False)),
              "in_scope": bool(payload.get("scope", payload.get("in_scope", True))), "created_at": now}
    with closing(connect(paths.db)) as con:
        con.execute("""INSERT INTO task_worker_reports
            (report_id,task_id,assignment_id,command_id,state,summary,human_action,artifacts_json,evidence_refs_json,next_action,safe_to_retry,in_scope,created_at)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""", (
                report["report_id"], report["task_id"], report["assignment_id"], report["command_id"], report["state"], report["summary"],
                report["human_action"], _json(report["artifacts"]), _json(report["evidence_refs"]), report["next_action"],
                int(report["safe_to_retry"]), int(report["in_scope"]), now))
        con.execute("UPDATE task_assignments SET last_report_id=?,updated_at=? WHERE assignment_id=?", (report["report_id"], now, assignment_id))
        con.commit()
    return report


def handback(paths, *, task_id: str, assignment_id: str, command_id: str, request: str, reason: str) -> dict[str, Any]:
    ensure_schema(paths)
    now = time.time()
    row = {"handback_id": "hb_" + uuid.uuid4().hex[:24], "task_id": _text(task_id, 128),
           "assignment_id": _text(assignment_id, 128), "command_id": _text(command_id, 128),
           "request": _text(request, 12000), "reason": _text(reason, 4000), "state": "open",
           "target_task_id": "", "created_at": now, "updated_at": now}
    if not row["request"] or not row["reason"]:
        raise ValueError("handback_requires_request_and_reason")
    with closing(connect(paths.db)) as con:
        con.execute("""INSERT INTO task_handbacks
            (handback_id,task_id,assignment_id,command_id,request,reason,state,target_task_id,created_at,updated_at)
            VALUES (?,?,?,?,?,?,?,?,?,?)""", tuple(row.values()))
        con.commit()
    return row


def public_control(paths, task_id: str) -> dict[str, Any]:
    ensure_schema(paths)
    with closing(connect(paths.db)) as con:
        mandate = con.execute("SELECT * FROM task_mandates WHERE task_id=?", (task_id,)).fetchone()
        assignment = con.execute("SELECT * FROM task_assignments WHERE task_id=? ORDER BY created_at DESC LIMIT 1", (task_id,)).fetchone()
        report = con.execute("SELECT * FROM task_worker_reports WHERE task_id=? ORDER BY created_at DESC LIMIT 1", (task_id,)).fetchone()
        inputs = con.execute("SELECT * FROM task_inputs WHERE task_id=? AND state IN ('delivered','replay_pending') ORDER BY delivered_at", (task_id,)).fetchall()
        handbacks = con.execute("SELECT * FROM task_handbacks WHERE task_id=? AND state='open' ORDER BY created_at", (task_id,)).fetchall()
        subscriptions = con.execute("SELECT * FROM task_trigger_subscriptions WHERE task_id=? ORDER BY created_at", (task_id,)).fetchall()
        deliveries = con.execute("SELECT * FROM task_trigger_deliveries WHERE task_id=? ORDER BY created_at DESC LIMIT 20", (task_id,)).fetchall()
        dead_letters = con.execute("SELECT * FROM task_trigger_dead_letters WHERE task_id=? ORDER BY created_at DESC LIMIT 20", (task_id,)).fetchall()
    def unpack(row: Any) -> dict[str, Any] | None:
        return dict(row) if row else None
    value = {"mandate": unpack(mandate), "assignment": unpack(assignment), "report": unpack(report),
             "pending_inputs": [dict(row) for row in inputs], "open_handbacks": [dict(row) for row in handbacks],
             "subscriptions": [dict(row) for row in subscriptions], "trigger_deliveries": [dict(row) for row in deliveries],
             "dead_letters": [dict(row) for row in dead_letters]}
    if value["mandate"]:
        value["mandate"]["scope"] = _load(value["mandate"].pop("scope_json", "{}"), {})
        value["mandate"]["allowed_tools"] = _load(value["mandate"].pop("allowed_tools_json", "[]"), [])
    if value["report"]:
        value["report"]["artifacts"] = _load(value["report"].pop("artifacts_json", "[]"), [])
        value["report"]["evidence_refs"] = _load(value["report"].pop("evidence_refs_json", "[]"), [])
    return value


def _safe_match(value: Any) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        return {}
    out: dict[str, Any] = {}
    for key, item in list(value.items())[:32]:
        key = _text(key, 128)
        if not key or any(ch not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_.:-" for ch in key):
            raise ValueError("invalid_task_subscription_match")
        if isinstance(item, (str, int, float, bool)) or item is None:
            out[key] = item
        else:
            raise ValueError("invalid_task_subscription_match_value")
    return out


def subscribe_task(paths, *, task_id: str, source: str, kind: str = "", match: Mapping[str, Any] | None = None,
                   subscription_id: str | None = None) -> dict[str, Any]:
    source = _text(source, 64)
    kind = _text(kind, 128)
    if not source or any(ch not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_.:-" for ch in source):
        raise ValueError("invalid_task_subscription_source")
    match_value = _safe_match(match)
    ensure_schema(paths)
    now = time.time()
    row = {"subscription_id": subscription_id or "sub_" + uuid.uuid4().hex[:24], "task_id": _text(task_id, 128),
           "source": source, "kind": kind, "match": match_value, "enabled": True, "last_cursor": "",
           "created_at": now, "updated_at": now}
    with closing(connect(paths.db)) as con:
        con.execute("""INSERT OR IGNORE INTO task_trigger_subscriptions
            (subscription_id,task_id,source,kind,match_json,enabled,last_cursor,created_at,updated_at)
            VALUES (?,?,?,?,?,?,?,?,?)""", (row["subscription_id"], row["task_id"], row["source"], row["kind"],
                                               _json(match_value), 1, "", now, now))
        con.commit()
        saved = con.execute("SELECT * FROM task_trigger_subscriptions WHERE subscription_id=?", (row["subscription_id"],)).fetchone()
    return {**dict(saved), "match": _load(saved["match_json"], {})}


def unsubscribe_task(paths, subscription_id: str) -> bool:
    ensure_schema(paths)
    with closing(connect(paths.db)) as con:
        changed = con.execute("UPDATE task_trigger_subscriptions SET enabled=0,updated_at=? WHERE subscription_id=?", (time.time(), subscription_id)).rowcount
        con.commit()
    return bool(changed)


def _event_match(event: Mapping[str, Any], subscription: Mapping[str, Any]) -> bool:
    if subscription["source"] != event.get("source"):
        return False
    if subscription["kind"] and subscription["kind"] != event.get("kind"):
        return False
    payload = event.get("payload") if isinstance(event.get("payload"), Mapping) else {}
    values = {**event, **{f"payload.{key}": value for key, value in payload.items()}}
    for key, expected in _load(subscription["match_json"], {}).items():
        if key not in values and key.startswith("payload."):
            continue
        if values.get(key) != expected:
            return False
    return True


def _stable_event_id(event: Mapping[str, Any]) -> str:
    explicit = _text(event.get("event_id"), 160)
    if explicit:
        return explicit
    basis = [event.get("source"), event.get("kind"), event.get("source_cursor"), event.get("idempotency_key"), event.get("occurred_at"), event.get("payload")]
    return "evt_" + hashlib.sha256(_json(basis).encode()).hexdigest()[:32]


def dispatch_trigger(paths, event: Mapping[str, Any], *, config: Any | None = None) -> dict[str, Any]:
    """Record one Trigger and deliver it once to every matching task subscription."""
    ensure_schema(paths)
    now = time.time()
    event_id = _stable_event_id(event)
    source = _text(event.get("source"), 64)
    kind = _text(event.get("kind"), 128)
    cursor = _text(event.get("source_cursor") or (event.get("payload") or {}).get("scheduled_at"), 160)
    idem = _text(event.get("idempotency_key"), 200)
    occurred = _text(event.get("occurred_at"), 80) or str(now)
    event_row = {"event_id": event_id, "source": source, "kind": kind, "source_cursor": cursor,
                 "idempotency_key": idem, "occurred_at": occurred, "payload": dict(event.get("payload") or {})}
    with closing(connect(paths.db)) as con:
        con.execute("""INSERT OR IGNORE INTO task_trigger_events
            (event_id,source,kind,source_cursor,idempotency_key,occurred_at,payload_json,state,created_at)
            VALUES (?,?,?,?,?,?,?,?,?)""", (event_id, source, kind, cursor, idem, occurred, _json(event_row["payload"]), "recorded", now))
        subscriptions = con.execute("SELECT * FROM task_trigger_subscriptions WHERE enabled=1 AND source=?", (source,)).fetchall()
        con.commit()
    delivered: list[dict[str, Any]] = []
    for subscription in subscriptions:
        if not _event_match({**event_row, "payload": event_row["payload"]}, subscription):
            continue
        delivery = _deliver_to_task(paths, subscription, event_row, config=config)
        delivered.append(delivery)
    states = {row["state"] for row in delivered}
    state = ("dead_letter" if "dead_letter" in states else
             "retry_scheduled" if "retry_scheduled" in states else
             "delivered" if "delivered" in states else "recorded")
    with closing(connect(paths.db)) as con:
        con.execute("UPDATE task_trigger_events SET state=? WHERE event_id=?", (state, event_id))
        con.commit()
    return {"event_id": event_id, "state": state, "matched": len(delivered), "deliveries": delivered}


def _deliver_to_task(paths, subscription: Mapping[str, Any], event: Mapping[str, Any], *, config: Any | None) -> dict[str, Any]:
    event_id, task_id, sub_id = event["event_id"], subscription["task_id"], subscription["subscription_id"]
    now = time.time()
    with closing(connect(paths.db)) as con:
        existing = con.execute("SELECT * FROM task_trigger_deliveries WHERE event_id=? AND task_id=?", (event_id, task_id)).fetchone()
        if existing:
            if existing["state"] in {"failed", "retry_scheduled"} and existing["next_retry_at"] is not None and float(existing["next_retry_at"]) <= time.time():
                return _attempt_delivery(paths, subscription, event, delivery_id=existing["delivery_id"], attempt=int(existing["attempt_count"] or 0) + 1, max_attempts=int(existing["max_attempts"] or DEFAULT_TRIGGER_MAX_ATTEMPTS), config=config)
            return {"event_id": event_id, "task_id": task_id, "state": "deduped", "delivery_id": existing["delivery_id"]}
    return _attempt_delivery(paths, subscription, event, delivery_id=None, attempt=1, max_attempts=DEFAULT_TRIGGER_MAX_ATTEMPTS, config=config)


def _attempt_delivery(paths, subscription: Mapping[str, Any], event: Mapping[str, Any], *, delivery_id: str | None,
                      attempt: int, max_attempts: int, config: Any | None) -> dict[str, Any]:
    event_id, task_id, sub_id = event["event_id"], subscription["task_id"], subscription["subscription_id"]
    now = time.time()
    try:
        if config is None:
            raise ValueError("task_trigger_config_missing")
        from .assistant_work import AssistantWork
        result = AssistantWork(paths).enqueue_trigger(config, task_id, event)
        state = str(result.get("state") or "delivered")
        input_id, command_id = str(result.get("input_id") or ""), str(result.get("command_id") or "")
        error = ""
    except Exception as exc:  # record a visible delivery failure; do not hide it
        state, input_id, command_id, error = "failed", "", "", f"{type(exc).__name__}: {exc}"[:1000]
    delivery_id = delivery_id or "tdl_" + uuid.uuid4().hex[:24]
    state = state if not error else "failed"
    next_retry_at = None
    if error:
        if attempt >= max_attempts:
            state = "dead_letter"
        else:
            state = "retry_scheduled"
            next_retry_at = now + min(3600.0, 10.0 * (2 ** max(0, attempt - 1)))
    with closing(connect(paths.db)) as con:
        con.execute("""INSERT INTO task_trigger_deliveries
            (delivery_id,event_id,task_id,subscription_id,state,input_id,command_id,error,created_at,updated_at,attempt_count,max_attempts,next_retry_at,last_attempt_at)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(delivery_id) DO UPDATE SET state=excluded.state,
            input_id=excluded.input_id,command_id=excluded.command_id,error=excluded.error,updated_at=excluded.updated_at,
            attempt_count=excluded.attempt_count,max_attempts=excluded.max_attempts,next_retry_at=excluded.next_retry_at,last_attempt_at=excluded.last_attempt_at""",
                     (delivery_id, event_id, task_id, sub_id, state, input_id, command_id, error, now, now, attempt, max_attempts, next_retry_at, now))
        if state == "dead_letter":
            con.execute("""INSERT OR IGNORE INTO task_trigger_dead_letters
                (dead_letter_id,delivery_id,event_id,task_id,source,kind,error,attempts,state,replay_count,created_at,updated_at)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""", ("dl_" + uuid.uuid4().hex[:24], delivery_id, event_id, task_id, event["source"], event["kind"], error, attempt, "open", 0, now, now))
        con.execute("UPDATE task_trigger_subscriptions SET last_cursor=?,updated_at=? WHERE subscription_id=?", (event.get("source_cursor", ""), now, sub_id))
        con.execute("""INSERT INTO task_trigger_sources(source,cursor,state,consecutive_failures,last_error,updated_at)
            VALUES (?,?,?,?,?,?) ON CONFLICT(source) DO UPDATE SET cursor=excluded.cursor,state=excluded.state,
            consecutive_failures=excluded.consecutive_failures,last_error=excluded.last_error,updated_at=excluded.updated_at""",
                     (event["source"], event.get("source_cursor", ""), "degraded" if error else "ready", 1 if error else 0, error, now))
        con.commit()
    return {"event_id": event_id, "task_id": task_id, "state": state, "delivery_id": delivery_id, "error": error,
            "attempt_count": attempt, "max_attempts": max_attempts, "next_retry_at": next_retry_at}


def retry_due_trigger_deliveries(paths, *, config: Any, now: float | None = None, limit: int = 50) -> dict[str, Any]:
    ensure_schema(paths)
    now = float(now if now is not None else time.time())
    with closing(connect(paths.db)) as con:
        rows = con.execute("""SELECT d.*, e.source, e.kind, e.source_cursor, e.occurred_at, e.payload_json, s.*
            FROM task_trigger_deliveries d JOIN task_trigger_events e ON e.event_id=d.event_id
            JOIN task_trigger_subscriptions s ON s.subscription_id=d.subscription_id
            WHERE d.state='retry_scheduled' AND d.next_retry_at IS NOT NULL AND d.next_retry_at<=? ORDER BY d.next_retry_at LIMIT ?""", (now, limit)).fetchall()
    results = []
    for row in rows:
        event = {"event_id": row["event_id"], "source": row["source"], "kind": row["kind"], "source_cursor": row["source_cursor"],
                 "occurred_at": row["occurred_at"], "payload": _load(row["payload_json"], {})}
        results.append(_attempt_delivery(paths, row, event, delivery_id=row["delivery_id"], attempt=int(row["attempt_count"] or 0) + 1,
                                         max_attempts=int(row["max_attempts"] or DEFAULT_TRIGGER_MAX_ATTEMPTS), config=config))
    return {"processed": len(results), "results": results}


def list_dead_letters(paths, *, state: str = "open", limit: int = 100) -> list[dict[str, Any]]:
    ensure_schema(paths)
    with closing(connect(paths.db)) as con:
        rows = con.execute("SELECT * FROM task_trigger_dead_letters WHERE state=? ORDER BY created_at DESC LIMIT ?", (state, limit)).fetchall()
    return [dict(row) for row in rows]


def replay_dead_letter(paths, *, dead_letter_id: str, config: Any) -> dict[str, Any]:
    ensure_schema(paths)
    with closing(connect(paths.db)) as con:
        row = con.execute("SELECT * FROM task_trigger_dead_letters WHERE dead_letter_id=?", (dead_letter_id,)).fetchone()
        if not row:
            raise ValueError("trigger_dead_letter_not_found")
        delivery = con.execute("SELECT * FROM task_trigger_deliveries WHERE delivery_id=?", (row["delivery_id"],)).fetchone()
        event = con.execute("SELECT * FROM task_trigger_events WHERE event_id=?", (row["event_id"],)).fetchone()
        subscription = con.execute("SELECT * FROM task_trigger_subscriptions WHERE subscription_id=?", (delivery["subscription_id"],)).fetchone()
    event_data = {"event_id": event["event_id"], "source": event["source"], "kind": event["kind"], "source_cursor": event["source_cursor"],
                  "occurred_at": event["occurred_at"], "payload": _load(event["payload_json"], {})}
    result = _attempt_delivery(paths, subscription, event_data, delivery_id=delivery["delivery_id"], attempt=1,
                               max_attempts=int(delivery["max_attempts"] or DEFAULT_TRIGGER_MAX_ATTEMPTS), config=config)
    with closing(connect(paths.db)) as con:
        con.execute("UPDATE task_trigger_dead_letters SET replay_count=replay_count+1,state=?,updated_at=? WHERE dead_letter_id=?",
                    ("resolved" if result["state"] == "delivered" else "open", time.time(), dead_letter_id))
        con.commit()
    return {"dead_letter_id": dead_letter_id, "replay": result}


def trigger_health(paths, source: str | None = None) -> list[dict[str, Any]]:
    ensure_schema(paths)
    with closing(connect(paths.db)) as con:
        rows = con.execute("SELECT * FROM task_trigger_sources WHERE (? IS NULL OR source=?) ORDER BY source", (source, source)).fetchall()
    return [dict(row) for row in rows]


def record_trigger_source_health(paths, *, source: str, cursor: str = "", state: str = "ready",
                                 error: str = "", increment_failure: bool = False) -> dict[str, Any]:
    """Persist source health without fabricating a successful event."""
    ensure_schema(paths)
    now = time.time()
    with closing(connect(paths.db)) as con:
        prior = con.execute("SELECT consecutive_failures FROM task_trigger_sources WHERE source=?", (source,)).fetchone()
        failures = int(prior[0]) if prior else 0
        failures = failures + 1 if increment_failure else 0
        con.execute("""INSERT INTO task_trigger_sources(source,cursor,state,consecutive_failures,last_error,updated_at)
            VALUES (?,?,?,?,?,?) ON CONFLICT(source) DO UPDATE SET cursor=excluded.cursor,state=excluded.state,
            consecutive_failures=excluded.consecutive_failures,last_error=excluded.last_error,updated_at=excluded.updated_at""",
                     (source, cursor, state, failures, _text(error, 1000), now))
        con.commit()
        row = con.execute("SELECT * FROM task_trigger_sources WHERE source=?", (source,)).fetchone()
    return dict(row)


__all__ = ["REPORT_STATES", "INPUT_STATES", "HANDBACK_STATES", "TRIGGER_DELIVERY_STATES", "role_plan_for", "coordination_instruction", "ensure_schema", "create_mandate", "assignment_for", "assignment_for_command", "assignment_started", "assignment_ended", "input_receipt", "mark_input", "worker_report", "handback", "public_control", "subscribe_task", "unsubscribe_task", "dispatch_trigger", "retry_due_trigger_deliveries", "list_dead_letters", "replay_dead_letter", "trigger_health", "record_trigger_source_health"]
