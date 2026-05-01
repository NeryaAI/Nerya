"""Agent task routes.

In the operator-facing model a *task* is the long-lived unit the user
cares about ("plan a strategy", "rebalance portfolio", "answer this
question"). It maps 1:1 onto an :class:`agent.session.SessionState`,
which already aggregates its constituent turns, invoked skills, and
last action.

This module exposes a small, opinionated surface so the dashboard
doesn't have to stitch ``/agent/sessions`` + ``/agent/open_turns`` +
``/agent/trace`` + ``/agent/interrupt`` together itself:

* ``GET  /agent/tasks``               list tasks (sessions) with status
* ``GET  /agent/tasks/timeline``      full trace (events) for one task
* ``GET  /agent/tasks/artifacts``     files / memory / messages produced
* ``POST /agent/tasks/cancel``        cooperative cancel via CancelToken
* ``POST /agent/tasks/resume``        re-run the last open turn

The legacy ``/agent/sessions``, ``/agent/run_turn``, ``/agent/trace``,
``/agent/explain``, ``/agent/interrupt`` endpoints stay live for the
Advanced/Debug surfaces. ``/agent/tasks`` is purely a BFF aggregator.
"""

from __future__ import annotations

from typing import Any, Iterable, Optional

from ._envelope import action, debug_ref, ok, source_ref


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _session_store(client):
    from ..agent.session import SessionStore

    return SessionStore(client.config.paths.root)


def _open_turns(client) -> list[dict[str, Any]]:
    try:
        from ..agent.recovery import list_open_turns

        return [s.asdict() for s in list_open_turns(client.config.paths)]
    except Exception:
        return []


def _open_turn_ids(client) -> set[str]:
    rows = _open_turns(client)
    return {str(r.get("turn_id") or "") for r in rows if r.get("turn_id")}


def _failed_turn_ids(client) -> set[str]:
    rows = _open_turns(client)
    out: set[str] = set()
    for r in rows:
        if r.get("error") or r.get("error_message"):
            tid = r.get("turn_id")
            if tid:
                out.add(str(tid))
    return out


def _task_status(state: Any, *, open_ids: set[str], failed_ids: set[str]) -> str:
    """Derive an operator-facing status from a SessionState.

    * ``failed``     — at least one turn is in ``open_turns`` with an error
    * ``in_progress`` — at least one turn is in ``open_turns`` (no error)
    * ``done``       — every turn closed cleanly
    * ``empty``      — no turns yet
    """

    turns = list(state.turn_ids or [])
    if not turns:
        return "empty"
    failed = [t for t in turns if str(t) in failed_ids]
    if failed:
        return "failed"
    in_flight = [t for t in turns if str(t) in open_ids]
    if in_flight:
        return "in_progress"
    return "done"


def _task_severity(status: str) -> str:
    if status == "failed":
        return "danger"
    if status == "in_progress":
        return "warn"
    return "info"


def _build_task_row(state: Any, *, open_ids: set[str], failed_ids: set[str]) -> dict[str, Any]:
    status = _task_status(state, open_ids=open_ids, failed_ids=failed_ids)
    sid = state.session_id
    last_action = state.last_action or ""
    title = (state.meta.get("title") if isinstance(state.meta, dict) else None) or last_action or sid
    return {
        "id": sid,
        "status": status,
        "severity": _task_severity(status),
        "title": str(title),
        "last_action": str(last_action),
        "strategy_id": state.strategy_id,
        "turn_count": len(state.turn_ids or []),
        "skills_invoked": list(state.invoked_skills or []),
        "created_at": state.created_at,
        "updated_at": state.updated_at,
        "meta": dict(state.meta or {}),
        "active_turn_ids": [t for t in (state.turn_ids or []) if str(t) in open_ids],
        "failed_turn_ids": [t for t in (state.turn_ids or []) if str(t) in failed_ids],
    }


# ---------------------------------------------------------------------------
# Timeline / artifact extraction
# ---------------------------------------------------------------------------


def _extract_artifacts(events: Iterable[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    """Mine a trace for operator-visible artifacts.

    The agent loop emits five categories of artifact-bearing events:

    * file edits     — ``write_file``, ``edit_file``, ``patch`` tool calls
    * messages       — ``send_message`` actions / ``messages`` journal rows
    * memory writes  — ``remember``, ``store_memory``
    * orders / fills — ``order_submitted``, ``order_filled``
    * created assets — ``create_strategy``, ``propose_script``,
                        ``create_subagent``, ``add_schedule``

    We don't try to interpret each one — we just classify and surface
    them so the dashboard can render an artifact tray with deep links.
    """

    files: list[dict[str, Any]] = []
    messages: list[dict[str, Any]] = []
    memory: list[dict[str, Any]] = []
    orders: list[dict[str, Any]] = []
    created: list[dict[str, Any]] = []

    for ev in events:
        rec = (ev or {}).get("record") or {}
        ts = ev.get("ts") if isinstance(ev, dict) else None
        action_name = str(rec.get("action") or rec.get("tool") or "").lower()
        skill_id = str(rec.get("skill_id") or rec.get("skill") or "").lower()
        payload = rec.get("payload") if isinstance(rec.get("payload"), dict) else {}
        result = rec.get("result") if isinstance(rec.get("result"), dict) else {}

        if action_name in ("write_file", "edit_file", "patch", "create_file"):
            path = (
                payload.get("path")
                or result.get("path")
                or rec.get("path")
            )
            if path:
                files.append({"ts": ts, "action": action_name, "path": str(path)})
            continue

        if action_name == "send_message" or skill_id == "message":
            text = (
                (payload.get("text") if isinstance(payload, dict) else None)
                or rec.get("text")
            )
            messages.append({
                "ts": ts,
                "channel": rec.get("channel") or payload.get("channel"),
                "text": str(text or ""),
            })
            continue

        if action_name in ("remember", "store_memory") or skill_id == "memory":
            memory.append({
                "ts": ts,
                "key": payload.get("key") or rec.get("key"),
                "summary": payload.get("summary") or rec.get("summary") or "",
            })
            continue

        if action_name in ("submit_order", "place_order", "order_submitted", "order_filled"):
            orders.append({
                "ts": ts,
                "action": action_name,
                "symbol": payload.get("symbol") or rec.get("symbol"),
                "side": payload.get("side") or rec.get("side"),
                "quantity": payload.get("quantity") or rec.get("quantity"),
                "result": result,
            })
            continue

        if action_name in (
            "create_strategy",
            "set_strategy_status",
            "propose_script",
            "create_subagent",
            "add_schedule",
        ):
            created.append({
                "ts": ts,
                "action": action_name,
                "result": result or payload,
            })
            continue

    return {
        "files": files,
        "messages": messages,
        "memory": memory,
        "orders": orders,
        "created": created,
    }


# ---------------------------------------------------------------------------
# Handlers
# ---------------------------------------------------------------------------


def _list_handler(client, query):
    q = dict(query or {})
    strategy_id = q.get("strategy_id") or None
    limit = int(q.get("limit") or 50)
    status_filter = q.get("status") or None

    store = _session_store(client)
    states = store.list(strategy_id=strategy_id, limit=limit)
    open_ids = _open_turn_ids(client)
    failed_ids = _failed_turn_ids(client)
    rows = [
        _build_task_row(s, open_ids=open_ids, failed_ids=failed_ids)
        for s in states
    ]
    if status_filter:
        wanted = {s.strip() for s in str(status_filter).split(",") if s.strip()}
        rows = [r for r in rows if r["status"] in wanted]

    counts = {"in_progress": 0, "failed": 0, "done": 0, "empty": 0}
    for r in rows:
        counts[r["status"]] = counts.get(r["status"], 0) + 1

    summary = (
        f"{counts.get('in_progress', 0)} in progress, "
        f"{counts.get('failed', 0)} failed, "
        f"{counts.get('done', 0)} done"
    )

    env = ok(
        summary,
        data={"tasks": rows, "counts": counts, "count": len(rows)},
        debug_refs=[
            debug_ref("source", "sessions", href="/agent/sessions"),
            debug_ref("source", "open_turns", href="/agent/open_turns"),
        ],
    )
    return env


def _timeline_handler(client, query):
    q = dict(query or {})
    sid: Optional[str] = q.get("id") or q.get("session_id") or q.get("task_id")
    if not sid:
        return {"ok": False, "error": "id required"}
    try:
        from ..observability.trace import build_trace

        trace = build_trace(client.config.paths, session_id=sid).as_dict()
    except Exception as exc:
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}

    events = list(trace.get("events") or [])
    surfaces = list(trace.get("surfaces") or [])
    summary = f"{len(events)} event(s) across {len(surfaces)} surfaces"

    env = ok(
        summary,
        data={
            "task_id": sid,
            "correlator": trace.get("correlator") or {},
            "events": events,
            "surfaces": surfaces,
        },
        source_refs=[source_ref("session", sid)],
        debug_refs=[debug_ref("source", "trace", href="/agent/trace")],
    )
    return env


def _artifacts_handler(client, query):
    q = dict(query or {})
    sid: Optional[str] = q.get("id") or q.get("session_id") or q.get("task_id")
    if not sid:
        return {"ok": False, "error": "id required"}
    try:
        from ..observability.trace import build_trace

        trace = build_trace(client.config.paths, session_id=sid).as_dict()
    except Exception as exc:
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}

    artifacts = _extract_artifacts(trace.get("events") or [])
    counts = {k: len(v) for k, v in artifacts.items()}
    summary = (
        f"{counts.get('files', 0)} file(s), "
        f"{counts.get('messages', 0)} message(s), "
        f"{counts.get('orders', 0)} order(s)"
    )
    env = ok(
        summary,
        data={"task_id": sid, "artifacts": artifacts, "counts": counts},
        source_refs=[source_ref("session", sid)],
    )
    return env


def _cancel_handler(client, payload):
    p = payload or {}
    sid = p.get("id") or p.get("session_id") or p.get("task_id")
    if not sid:
        return {"ok": False, "error": "id required"}
    try:
        from ..harness.cancellation import signal_cancel

        cancelled = signal_cancel(
            str(sid), reason=str(p.get("reason") or "operator_cancel_task")
        )
    except Exception as exc:
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
    env = ok(
        "cancel signalled" if cancelled else "no in-flight turn matched",
        data={"task_id": str(sid), "cancelled": bool(cancelled)},
    )
    return env


def _resume_handler(client, payload):
    p = payload or {}
    sid = p.get("id") or p.get("session_id") or p.get("task_id")
    if not sid:
        return {"ok": False, "error": "id required"}
    try:
        store = _session_store(client)
        state = store.load(str(sid))
    except Exception as exc:
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
    if state is None:
        return {"ok": False, "error": f"task {sid!r} not found"}

    open_ids = _open_turn_ids(client)
    open_turn_id = next((t for t in (state.turn_ids or []) if str(t) in open_ids), None)
    if not open_turn_id:
        env = ok(
            "no open turn to resume — start a new run",
            data={"task_id": str(sid), "open_turn_id": None},
            primary_action=action(
                id="start_new",
                label="Start new turn",
                method="POST",
                href="/agent/run_turn",
                body={"session_id": str(sid), "strategy_id": state.strategy_id},
            ),
        )
        return env

    try:
        from ..agent.recovery import load_turn_state

        ts = load_turn_state(client.config.paths, str(open_turn_id)).asdict()
    except Exception as exc:
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}

    env = ok(
        f"turn {open_turn_id} ready to resume",
        data={"task_id": str(sid), "open_turn_id": str(open_turn_id), "turn_state": ts},
        primary_action=action(
            id="resume_turn",
            label="Resume",
            method="POST",
            href="/agent/run_turn",
            body={"session_id": str(sid), "turn_id": str(open_turn_id)},
        ),
        next_actions=[
            action(
                id="explain",
                label="Explain failure",
                method="POST",
                href="/agent/explain",
                body={"turn_id": str(open_turn_id)},
            ),
        ],
    )
    return env


def routes():
    return [
        ("GET", "/agent/tasks", _list_handler),
        ("GET", "/agent/tasks/timeline", _timeline_handler),
        ("GET", "/agent/tasks/artifacts", _artifacts_handler),
        ("POST", "/agent/tasks/cancel", _cancel_handler),
        ("POST", "/agent/tasks/resume", _resume_handler),
    ]


__all__ = ["routes"]
