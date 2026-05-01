"""Strategy history ledger writers."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from ..core import jsonl
from ..core.paths import WorkspacePaths
from ..core.time import now_iso


def _append(paths: WorkspacePaths, strategy_id: str, ledger: str, record: dict[str, Any]) -> None:
    path = paths.strategy_history(strategy_id) / f"{ledger}.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    jsonl.append(path, record)


def record_trigger(paths: WorkspacePaths, *, strategy_id: str, session_id: str | None,
                   event: dict[str, Any]) -> None:
    _append(paths, strategy_id, "triggers", {
        "kind": "trigger", "session_id": session_id, "event": event,
    })


def record_skill_call(paths: WorkspacePaths, *, strategy_id: str, session_id: str | None,
                      skill_id: str, action: str, caller: str,
                      payload_keys: list[str], result_summary: dict,
                      ts: str | None = None) -> None:
    _append(paths, strategy_id, "skill_calls", {
        "ts": ts or now_iso(),
        "session_id": session_id,
        "skill_id": skill_id, "action": action,
        "caller": caller,
        "payload_keys": payload_keys,
        "result_summary": result_summary,
    })


def record_subagent(paths: WorkspacePaths, *, strategy_id: str, session_id: str | None,
                    name: str, output: dict[str, Any]) -> None:
    _append(paths, strategy_id, "subagents", {
        "session_id": session_id, "name": name, "output": output,
    })


def record_decision(paths: WorkspacePaths, *, strategy_id: str, session_id: str | None,
                    decision: dict[str, Any]) -> None:
    _append(paths, strategy_id, "decisions", {
        "session_id": session_id, "decision": decision,
    })


def record_intent(paths: WorkspacePaths, *, strategy_id: str, session_id: str | None,
                  intent: dict[str, Any]) -> None:
    _append(paths, strategy_id, "intents", {
        "session_id": session_id, "intent": intent,
    })


def record_risk(paths: WorkspacePaths, *, strategy_id: str, session_id: str | None,
                decision: dict[str, Any]) -> None:
    _append(paths, strategy_id, "risk", {
        "session_id": session_id, "risk_decision": decision,
    })


def record_order(paths: WorkspacePaths, *, strategy_id: str, session_id: str | None,
                 payload: dict[str, Any]) -> None:
    _append(paths, strategy_id, "orders", {
        "session_id": session_id, "payload": payload,
    })


def record_fill(paths: WorkspacePaths, *, strategy_id: str, session_id: str | None,
                fill: dict[str, Any]) -> None:
    _append(paths, strategy_id, "fills", {
        "session_id": session_id, "fill": fill,
    })


def record_pnl(paths: WorkspacePaths, *, strategy_id: str, session_id: str | None,
               pnl: dict[str, Any]) -> None:
    _append(paths, strategy_id, "pnl", {
        "session_id": session_id, "pnl": pnl,
    })


def record_message(paths: WorkspacePaths, *, strategy_id: str, session_id: str | None,
                   message: dict[str, Any]) -> None:
    _append(paths, strategy_id, "messages", {
        "session_id": session_id, "message": message,
    })


def record_agent_task(paths: WorkspacePaths, *, strategy_id: str, session_id: str | None,
                      task: dict[str, Any]) -> None:
    _append(paths, strategy_id, "agent_tasks", {
        "session_id": session_id, "task": task,
    })


def record_review(paths: WorkspacePaths, *, strategy_id: str, session_id: str | None,
                  review: dict[str, Any]) -> None:
    _append(paths, strategy_id, "reviews", {
        "session_id": session_id, "review": review,
    })


def read_ledger(paths: WorkspacePaths, strategy_id: str, name: str) -> list[dict]:
    return jsonl.read_all(paths.strategy_history(strategy_id) / f"{name}.jsonl")
