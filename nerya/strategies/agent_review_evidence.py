"""Read-only, version-attributed Agent task evidence for strategy reviews.

Uses the owning strategy ledger, not global chat/memory. Kept separate from
script/trade metrics: a completed Agent turn is not business success.
"""
from __future__ import annotations
from collections import Counter
from datetime import datetime, timezone
from typing import Any
from ..core.redaction import redact_display_dict
from ..strategy_history.store import read_ledger


def _time(value: Any) -> datetime | None:
    try:
        stamp = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return stamp.replace(tzinfo=timezone.utc) if stamp.tzinfo is None else stamp.astimezone(timezone.utc)
    except (ValueError, TypeError):
        return None


def agent_review_evidence(paths, *, strategy_id: str, package_hash: str,
                          execution_mode: str, package_mode: str,
                          cutoff: datetime, anchor: datetime, limit: int,
                          requested_run_ids=frozenset(), requested_session_ids=frozenset()) -> dict[str, Any]:
    excluded: Counter[str] = Counter()
    newest: dict[str, tuple[datetime, dict[str, Any]]] = {}
    try:
        entries = read_ledger(paths, strategy_id, "agent_tasks")
    except Exception as exc:
        return {"available": False, "reason": f"Task ledger unreadable: {type(exc).__name__}", "tasks": []}
    # Deduplicate lifecycle records before filtering attribution. Otherwise
    # a missing-hash terminal event could resurrect a stale running event.
    last: dict[str, tuple[datetime, dict[str, Any]]] = {}
    for entry in entries:
        task = entry.get("task") if isinstance(entry, dict) else None
        if not isinstance(task, dict) or not isinstance(task.get("task_id"), str):
            excluded["invalid_record"] += 1; continue
        stamp = _time(task.get("finished_at") or task.get("ts"))
        if stamp is None:
            excluded["timestamp"] += 1; continue
        key = task["task_id"]
        if key not in last or stamp >= last[key][0]: last[key] = (stamp, entry)
    for _, entry in last.values():
        if not isinstance(entry, dict) or not isinstance(entry.get("task"), dict):
            excluded["invalid_record"] += 1; continue
        task = entry["task"]
        task_id = task.get("task_id")
        if not isinstance(task_id, str) or not task_id:
            excluded["missing_task_id"] += 1; continue
        if task.get("strategy_id") != strategy_id or entry.get("strategy_id", strategy_id) != strategy_id:
            excluded["strategy_id"] += 1; continue
        # Version hash binds the mode even for legacy tasks without a mode field.
        if task.get("package_hash") != package_hash:
            excluded["package_hash"] += 1; continue
        if execution_mode != package_mode or task.get("mode", package_mode) != execution_mode:
            excluded["execution_mode"] += 1; continue
        stamp = _time(task.get("finished_at") or task.get("ts"))
        if stamp is None or not cutoff <= stamp <= anchor:
            excluded["timestamp"] += 1; continue
        session = task.get("session_id")
        if entry.get("session_id") and entry["session_id"] != session:
            excluded["session_conflict"] += 1; continue
        if requested_run_ids and task_id not in requested_run_ids:
            excluded["requested_run_ids"] += 1; continue
        if requested_session_ids and session not in requested_session_ids:
            excluded["requested_session_ids"] += 1; continue
        previous = newest.get(task_id)
        if previous is None or stamp >= previous[0]: newest[task_id] = (stamp, task)
    selected = sorted(newest.values(), key=lambda value: (value[0], value[1]["task_id"]), reverse=True)
    if len(selected) > limit: excluded["lookback_limit"] += len(selected) - limit
    tasks = []
    for stamp, task in selected[:limit]:
        metadata = task.get("metadata") if isinstance(task.get("metadata"), dict) else {}
        row = {key: task[key] for key in ("task_id", "strategy_id", "package_hash", "session_id", "turn_id", "status", "reason", "duration_ms", "iterations", "stopped_reason") if key in task}
        row["observed_at"] = stamp.isoformat()
        row["path"] = metadata.get("path")
        # No raw tool trace, hidden reasoning, unrelated conversation or secrets.
        for key in ("input_context", "input_selection", "selected_roles", "execution_policy", "required_team_run_id", "required_team_run_ok"):
            if key in metadata: row[key] = metadata[key]
        final = task.get("final_text")
        if isinstance(final, str):
            row["final_text"] = final[:4000]
            row["final_text_truncated"] = len(final) > 4000
        if task.get("error"):
            error = task["error"]
            row["error"] = {k: error[k] for k in ("code", "message") if k in error} if isinstance(error, dict) else str(error)[:1000]
        tasks.append(redact_display_dict(row))
    return {"available": True, "source": "strategy_history/agent_tasks", "tasks": tasks,
            "selected_task_ids": [t["task_id"] for t in tasks],
            "by_status": dict(Counter(t.get("status", "unknown") for t in tasks)),
            "excluded": dict(excluded),
            "notes": ["One row per recorded task; stops are evidence, not missing runs.",
                      "Agent completion is not proof of business success or profitability.",
                      "Input references identify captured artifacts; their full content is not loaded here."]}
