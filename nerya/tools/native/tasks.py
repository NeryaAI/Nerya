"""Async / background subagent tools — .

The parent spawns a subagent, gets a ``task_id`` back immediately, and
uses companion tools to inspect / pull / stop the work later.

Tools registered here:

* ``subagent_run_async`` — spawn a child subagent in a daemon
  thread; returns the ``task_id`` immediately. Output lands in
  ``<workspace>/agent_tasks/<task_id>.json`` and on the streaming
  event bus as ``agent.task.progress`` / ``agent.task.finished``.
* ``task_list`` — list known tasks (filter by state / session id).
* ``task_get`` — full record for one task: state, progress notes,
  output (when terminal), error / token / wall_ms.
* ``task_output`` — return *just* the output blob; convenience for
  the model so it doesn't have to navigate around progress notes.
* ``task_stop`` — cooperatively cancel a running task.

Important: live-trading is still off-limits to subagents. The
underlying :class:`SubAgentDispatcher` already enforces the
denylist; we don't relax it here.
"""

from __future__ import annotations

import time
from typing import Any, Optional

from ...core.errors import TriggerValidationError
from ...core.config import Config
from ...skills.builtin.tasks.scripts import create_task
from ...skills.kernel import SkillKernel
from ...subagents.dispatcher import SubAgentDispatcher
from ...subagents.tasks import TaskStore, run_in_thread
from ..types import (
    ToolCall,
    ToolError,
    ToolErrorKind,
    ToolResult,
)


# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------


SUBAGENT_RUN_ASYNC_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "name": {
            "type": "string",
            "description": (
                "Subagent name — same registry as ``subagent_run``. "
                "Live-trading subagents are denied at the dispatcher "
                "boundary; pick a research / monitoring / analysis "
                "subagent for background work."
            ),
        },
        "payload": {
            "type": "object",
            "description": "JSON payload handed to the child runtime.",
        },
        "strategy_id": {"type": "string"},
        "session_id": {"type": "string"},
        "trigger_event_id": {"type": "string"},
    },
    "required": ["name"],
}

TASK_CREATE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "id": {"type": "string", "description": "Stable schedule/task id."},
        "title": {"type": "string"},
        "task_type": {
            "type": "string",
            "enum": ["agent", "script"],
            "description": "Use agent for recurring agent sessions; script for approved scripts.",
        },
        "source_request": {
            "type": "string",
            "description": "The operator's original request for auditability.",
        },
        "generated_prompt": {
            "type": "string",
            "description": (
                "Durable prompt executed by recurring agent tasks. It must "
                "perform the scheduled business work directly, not create "
                "more recurring schedules/tasks or clone itself on each tick."
            ),
        },
        "script_id": {
            "type": "string",
            "description": "Required for script tasks unless target is script:<id>.",
        },
        "script_args": {"type": "object"},
        "cron": {
            "type": "string",
            "description": "Five-field cron expression such as 0 9 * * *.",
        },
        "every_seconds": {
            "type": "integer",
            "minimum": 1,
            "description": "Interval schedule in seconds.",
        },
        "timezone": {"type": "string"},
        "session_mode": {"type": "string", "enum": ["ephemeral", "reuse"]},
        "session_id": {"type": "string"},
        "delivery_targets": {
            "oneOf": [
                {"type": "string"},
                {"type": "object"},
                {"type": "array", "items": {"type": ["object", "string"]}},
            ],
            "description": (
                "Output routing targets. Use dashboard/local as safe defaults. "
                "Only include external gateways such as telegram, discord, or "
                "slack when the operator's original source_request explicitly "
                "asked for that output channel; do not add unrelated channels."
            ),
        },
        "payload": {"type": "object"},
        "enabled": {"type": "boolean"},
    },
    "required": ["task_type"],
}

TASK_LIST_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "state": {
            "type": "string",
            "enum": ["queued", "running", "succeeded", "failed", "cancelled"],
            "description": "Optional state filter.",
        },
        "session_id": {
            "type": "string",
            "description": "Optional parent-session filter.",
        },
        "limit": {"type": "integer", "minimum": 1, "default": 25},
    },
}

TASK_GET_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {"task_id": {"type": "string"}},
    "required": ["task_id"],
}

TASK_OUTPUT_SCHEMA: dict[str, Any] = TASK_GET_SCHEMA
TASK_STOP_SCHEMA: dict[str, Any] = TASK_GET_SCHEMA
TASK_SUMMARY_SCHEMA: dict[str, Any] = TASK_GET_SCHEMA

TASK_UPDATE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "task_id": {"type": "string"},
        "note": {
            "type": "string",
            "description": (
                "Short progress note (one line). Appended to the task's "
                "progress journal and surfaced on the dashboard."
            ),
        },
        "payload": {
            "type": "object",
            "description": (
                "Optional structured payload accompanying the note "
                "(``step``, ``percent``, free-form metrics)."
            ),
        },
    },
    "required": ["task_id", "note"],
}


# ---------------------------------------------------------------------------
# Worker
# ---------------------------------------------------------------------------


def _worker(
    *,
    config: Config,
    skills: SkillKernel,
    store: TaskStore,
    task_id: str,
    name: str,
    payload: dict[str, Any],
    trigger_event_id: Optional[str],
    strategy_id: Optional[str],
    session_id: Optional[str],
) -> None:
    """Run one subagent in a daemon thread and persist its outcome.

    Cooperative cancellation: the worker checks the store's cancel
    event before/after the dispatch. We can't kill a running
    :class:`SubAgentRuntime` mid-step today, but the next iteration
    of the child's own observe→think→act loop will see the cancel
    flag (the runtime threads it through ``cancel_token``).
    """

    store.update_state(task_id, "running")
    cancel_event = store.cancel_event(task_id)
    started = time.monotonic()
    try:
        if cancel_event is not None and cancel_event.is_set():
            store.finish(
                task_id,
                error="cancelled before start",
                error_kind="cancelled",
                wall_ms=int((time.monotonic() - started) * 1000),
            )
            return
        dispatcher = SubAgentDispatcher(config=config, skills=skills)
        envelope = dispatcher.dispatch(
            f"subagent:{name}",
            payload=payload or {},
            trigger_event_id=trigger_event_id,
            strategy_id=strategy_id,
            session_id=session_id,
        )
        wall_ms = int((time.monotonic() - started) * 1000)
        if cancel_event is not None and cancel_event.is_set():
            store.finish(
                task_id,
                error="cancelled mid-flight",
                error_kind="cancelled",
                wall_ms=wall_ms,
                output=envelope.get("output") or {},
                tokens=int(envelope.get("tokens") or 0),
                usd=float(envelope.get("usd") or 0.0),
            )
            return
        if not envelope.get("ok", True):
            store.finish(
                task_id,
                error=str(envelope.get("error") or "subagent failed"),
                error_kind=envelope.get("error_kind") or "unknown",
                wall_ms=wall_ms,
                output=envelope,
            )
            return
        store.finish(
            task_id,
            output=envelope,
            tokens=int(envelope.get("tokens") or 0),
            usd=float(envelope.get("usd") or 0.0),
            wall_ms=wall_ms,
        )
    except Exception as exc:
        wall_ms = int((time.monotonic() - started) * 1000)
        store.finish(
            task_id,
            error=f"{type(exc).__name__}: {exc}",
            error_kind="execution_error",
            wall_ms=wall_ms,
        )


# ---------------------------------------------------------------------------
# Handlers
# ---------------------------------------------------------------------------


def _usage_error(call: ToolCall, message: str) -> ToolResult:
    return ToolResult.from_error(
        tool_use_id=call.id,
        name=call.name,
        error=ToolError(kind=ToolErrorKind.SCHEMA_VALIDATION, message=message),
    )


def _cached_team_summary(call: ToolCall, args: dict[str, Any]) -> dict[str, Any] | None:
    try:
        from .agents import cached_team_run_summary_for_call

        return cached_team_run_summary_for_call(call, args)
    except Exception:
        return None


def _team_task_view(summary: dict[str, Any]) -> dict[str, Any]:
    return {
        "task_id": summary.get("team_run_id"),
        "name": "team_run",
        "state": summary.get("status") or "completed",
        "started_at": None,
        "finished_at": None,
        "tokens": summary.get("tokens_total", 0),
        "usd": summary.get("usd_total", 0.0),
        "wall_ms": None,
        "progress_count": 0,
    }


def _team_task_hint(summary: dict[str, Any]) -> str:
    output_language = str(
        summary.get("output_language") or "the original user prompt language"
    )
    return (
        "The latest team_run in this turn was synchronous and already "
        "returned this result. Synthesize the final answer from "
        "team_summary now in the original user prompt language "
        f"({output_language}), translating team member outputs as needed "
        "including headings, labels, and natural-language field names while "
        "preserving proper nouns, tickers, "
        "source names, code identifiers, and URLs. team_run_id is not an async "
        "task_id; do not inspect task_list/task_get/task_output again for this "
        "team_run."
    )


def subagent_run_async_handler(
    call: ToolCall,
    *,
    config: Config,
    skills: SkillKernel,
    store: TaskStore,
) -> ToolResult:
    args = call.arguments or {}
    name = (args.get("name") or "").strip()
    if not name:
        return _usage_error(call, "name is required")
    payload = args.get("payload") if isinstance(args.get("payload"), dict) else {}
    team_summary = _cached_team_summary(call, args)
    if team_summary is not None:
        return ToolResult.from_json(
            tool_use_id=call.id,
            name=call.name,
            data={
                "task_id": team_summary.get("team_run_id"),
                "name": "team_run",
                "state": team_summary.get("status") or "completed",
                "status": "team_already_completed",
                "skipped": True,
                "team_summary": team_summary,
                "next_action": _team_task_hint(team_summary),
            },
        )
    meta = call.metadata if isinstance(call.metadata, dict) else {}
    strategy_id = args.get("strategy_id") or meta.get("strategy_id") or None
    session_id = args.get("session_id") or meta.get("session_id") or None
    trigger_event_id = (
        args.get("trigger_event_id")
        or meta.get("trigger_event_id")
        or None
    )

    record = store.create(
        name=name,
        payload=payload or {},
        parent_turn_id=call.turn_id,
        parent_session_id=session_id,
        strategy_id=strategy_id,
    )
    run_in_thread(
        _worker,
        name=f"subagent-async-{record.task_id}",
        kwargs={
            "config": config,
            "skills": skills,
            "store": store,
            "task_id": record.task_id,
            "name": name,
            "payload": payload or {},
            "trigger_event_id": trigger_event_id,
            "strategy_id": strategy_id,
            "session_id": session_id,
        },
    )
    return ToolResult.from_json(
        tool_use_id=call.id,
        name=call.name,
        data={
            "task_id": record.task_id,
            "name": name,
            "state": "queued",
            "started_at": record.started_at,
            "hint": (
                "Use task_get / task_output to fetch results once the "
                "task reaches state='succeeded'. Use task_stop to "
                "cooperatively cancel."
            ),
        },
    )


def task_list_handler(
    call: ToolCall,
    *,
    store: TaskStore,
) -> ToolResult:
    args = call.arguments or {}
    state = args.get("state") or None
    session_id = args.get("session_id") or None
    limit = max(1, int(args.get("limit") or 25))
    team_summary = _cached_team_summary(call, args)
    if team_summary is not None:
        return ToolResult.from_json(
            tool_use_id=call.id,
            name=call.name,
            data={
                "count": 1,
                "tasks": [_team_task_view(team_summary)],
                "team_summary": team_summary,
                "next_action": _team_task_hint(team_summary),
            },
        )
    rows = store.list(state=state, parent_session_id=session_id, limit=limit)
    return ToolResult.from_json(
        tool_use_id=call.id,
        name=call.name,
        data={
            "count": len(rows),
            "tasks": [
                {
                    "task_id": r.task_id,
                    "name": r.name,
                    "state": r.state,
                    "started_at": r.started_at,
                    "finished_at": r.finished_at,
                    "tokens": r.tokens,
                    "usd": r.usd,
                    "wall_ms": r.wall_ms,
                    "progress_count": len(r.progress),
                }
                for r in rows
            ],
        },
    )


def task_create_handler(
    call: ToolCall,
    *,
    workspace: str | "Path",
) -> ToolResult:
    try:
        result = create_task.run(dict(call.arguments or {}), workspace=workspace)
    except TriggerValidationError as exc:
        return ToolResult.from_error(
            tool_use_id=call.id,
            name=call.name,
            error=ToolError(
                kind=ToolErrorKind.SCHEMA_VALIDATION,
                message=f"{type(exc).__name__}: {exc}",
            ),
        )
    except Exception as exc:  # noqa: BLE001 - surface concrete tool failure.
        return ToolResult.from_error(
            tool_use_id=call.id,
            name=call.name,
            error=ToolError(
                kind=ToolErrorKind.EXECUTION_ERROR,
                message=f"{type(exc).__name__}: {exc}",
            ),
        )
    if not result.get("ok"):
        return ToolResult.from_error(
            tool_use_id=call.id,
            name=call.name,
            error=ToolError(
                kind=ToolErrorKind.SCHEMA_VALIDATION,
                message=str(result.get("error") or "task_create failed"),
            ),
        )
    return ToolResult.from_json(tool_use_id=call.id, name=call.name, data=result)


def task_get_handler(
    call: ToolCall,
    *,
    store: TaskStore,
) -> ToolResult:
    args = call.arguments or {}
    task_id = (args.get("task_id") or "").strip()
    if not task_id:
        return _usage_error(call, "task_id is required")
    rec = store.load(task_id)
    if rec is None:
        team_summary = _cached_team_summary(call, args)
        if team_summary is not None:
            return ToolResult.from_json(
                tool_use_id=call.id,
                name=call.name,
                data={
                    **_team_task_view(team_summary),
                    "requested_task_id": task_id,
                    "output": team_summary,
                    "team_summary": team_summary,
                    "next_action": _team_task_hint(team_summary),
                },
            )
        return _usage_error(call, f"task not found: {task_id}")
    return ToolResult.from_json(
        tool_use_id=call.id,
        name=call.name,
        data=rec.asdict(),
    )


def task_output_handler(
    call: ToolCall,
    *,
    store: TaskStore,
) -> ToolResult:
    args = call.arguments or {}
    task_id = (args.get("task_id") or "").strip()
    if not task_id:
        return _usage_error(call, "task_id is required")
    rec = store.load(task_id)
    if rec is None:
        team_summary = _cached_team_summary(call, args)
        if team_summary is not None:
            return ToolResult.from_json(
                tool_use_id=call.id,
                name=call.name,
                data={
                    "task_id": team_summary.get("team_run_id"),
                    "requested_task_id": task_id,
                    "state": team_summary.get("status") or "completed",
                    "output": team_summary,
                    "error": None,
                    "error_kind": None,
                    "next_action": _team_task_hint(team_summary),
                },
            )
        return _usage_error(call, f"task not found: {task_id}")
    return ToolResult.from_json(
        tool_use_id=call.id,
        name=call.name,
        data={
            "task_id": rec.task_id,
            "state": rec.state,
            "output": rec.output,
            "error": rec.error,
            "error_kind": rec.error_kind,
        },
    )


def task_stop_handler(
    call: ToolCall,
    *,
    store: TaskStore,
) -> ToolResult:
    args = call.arguments or {}
    task_id = (args.get("task_id") or "").strip()
    if not task_id:
        return _usage_error(call, "task_id is required")
    found = store.request_stop(task_id)
    return ToolResult.from_json(
        tool_use_id=call.id,
        name=call.name,
        data={
            "task_id": task_id,
            "stop_requested": True,
            "live_worker_found": found,
            "hint": (
                "Cancellation is cooperative — the worker checks "
                "between iterations. Use task_get to confirm the "
                "task reached state='cancelled'."
            ),
        },
    )


def task_update_handler(
    call: ToolCall,
    *,
    store: TaskStore,
) -> ToolResult:
    """Append a progress note to a running task.

    Used by the parent agent (or, more usefully, a worker subagent
    threading its progress back to the parent) to surface partial
    findings without polluting the parent's context with a full
    output read. This keeps partial findings visible without flooding
    the parent with full output.
    """

    args = call.arguments or {}
    task_id = (args.get("task_id") or "").strip()
    note = (args.get("note") or "").strip()
    payload = args.get("payload") if isinstance(args.get("payload"), dict) else None
    if not task_id:
        return _usage_error(call, "task_id is required")
    if not note:
        return _usage_error(call, "note is required (one-line progress text)")
    rec = store.append_progress(task_id, note=note, payload=payload)
    if rec is None:
        return _usage_error(call, f"task not found: {task_id}")
    return ToolResult.from_json(
        tool_use_id=call.id,
        name=call.name,
        data={
            "task_id": task_id,
            "state": rec.state,
            "progress_count": len(rec.progress),
            "appended": {"note": note, "payload": payload or {}},
        },
    )


def task_summary_handler(
    call: ToolCall,
    *,
    store: TaskStore,
) -> ToolResult:
    """Return a *summary* of a task — state, recent progress notes,
    final outcome — without dumping the (potentially large) full
    output body. The model uses this to monitor a long-running task
    without pulling its full body into context.
    """

    args = call.arguments or {}
    task_id = (args.get("task_id") or "").strip()
    if not task_id:
        return _usage_error(call, "task_id is required")
    rec = store.load(task_id)
    if rec is None:
        team_summary = _cached_team_summary(call, args)
        if team_summary is not None:
            return ToolResult.from_json(
                tool_use_id=call.id,
                name=call.name,
                data={
                    **_team_task_view(team_summary),
                    "requested_task_id": task_id,
                    "summary": team_summary.get("next_action"),
                    "team_summary": team_summary,
                    "next_action": _team_task_hint(team_summary),
                },
            )
        return _usage_error(call, f"task not found: {task_id}")
    progress_recent = list(rec.progress[-5:])
    summary_text: str | None = None
    if isinstance(rec.output, dict):
        for key in ("summary", "final", "headline", "result"):
            v = rec.output.get(key)
            if isinstance(v, str) and v.strip():
                summary_text = v.strip()[:1200]
                break
    return ToolResult.from_json(
        tool_use_id=call.id,
        name=call.name,
        data={
            "task_id": task_id,
            "name": rec.name,
            "state": rec.state,
            "started_at": rec.started_at,
            "finished_at": rec.finished_at,
            "wall_ms": rec.wall_ms,
            "tokens": rec.tokens,
            "usd": rec.usd,
            "error": rec.error,
            "error_kind": rec.error_kind,
            "progress_count": len(rec.progress),
            "progress_recent": progress_recent,
            "summary": summary_text,
            "hint": (
                "call task_output(task_id) to inspect the full output "
                "body when you actually need it; this summary keeps the "
                "parent context small while the worker runs"
            ),
        },
    )


__all__ = [
    "SUBAGENT_RUN_ASYNC_SCHEMA",
    "TASK_CREATE_SCHEMA",
    "TASK_GET_SCHEMA",
    "TASK_LIST_SCHEMA",
    "TASK_OUTPUT_SCHEMA",
    "TASK_STOP_SCHEMA",
    "TASK_SUMMARY_SCHEMA",
    "TASK_UPDATE_SCHEMA",
    "subagent_run_async_handler",
    "task_create_handler",
    "task_get_handler",
    "task_list_handler",
    "task_output_handler",
    "task_stop_handler",
    "task_summary_handler",
    "task_update_handler",
]
