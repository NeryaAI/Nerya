"""AgentKernel — single canonical entry point for one agent turn.

Each turn materialises a fresh :class:`WorkspaceNativeAgentLoop` and runs
the provider-native ``messages + tools`` loop until the model emits
``stop_reason == end_turn`` (or the iteration budget is exhausted).

This kernel intentionally owns no planning, parsing, or action-dispatch
logic; those concerns live behind native :class:`ToolDescriptor`\\ s and
the model decides which tool to call. The kernel only:

* binds per-turn lifecycle (hooks, sessions, cancel tokens, journals),
* builds the tool registry (native + legacy-skill bridge),
* renders the system prompt (charter + memory recap + skill / recipe
  listing),
* attaches long-lived helpers — :class:`~nerya.agent.memory.Memory`,
  :class:`~nerya.subagents.dispatcher.SubAgentDispatcher` — so callers
  (and the kernel's own end-of-turn hook) can use them without
  re-instantiating per turn,
* delegates the conversation to :class:`WorkspaceNativeAgentLoop`,
* runs end-of-turn auto-evolution (``maybe_propose_from_turn``) and an
  optional memory-write tick so durable lessons aren't dropped between
  turns,
* projects the loop outcome onto :class:`AgentTurnResult` for HTTP/SDK
  consumers.
"""

from __future__ import annotations

import logging
import hashlib
import json
import re
import time
from datetime import timezone
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

from ..core import jsonl
from ..core.config import Config
from ..core.ids import turn_id as new_turn_id
from ..core.time import now, now_iso
from ..llm.gateway import LLMGateway
from ..skills.kernel import SkillKernel
from ..tools import (
    NativeToolExecutor,
    PermissionContext,
    PermissionDecisionKind,
    PermissionEngine,
    PermissionMode,
    PermissionRule,
    ToolOrchestrator,
    ToolRegistry,
)
from ..tools.native import (
    NativeToolDeps,
    build_native_tool_deps,
    register_native_tools,
)
from .file_state import FileStateCache
from .hooks import HookContext, HookRegistry, _bind_config, _unbind_config
from .loop import LoopConfig, LoopOutcome, WorkspaceNativeAgentLoop
from .attachments import (
    prepare_user_message,
    public_attachment_blocks_from_envelopes,
)
from .artifact_index import build_artifact_index, render_final_report
from .market_context import (
    load_session_market_context,
    render_session_market_context_block,
)
from .memory import Memory
from .self_improvement import maybe_propose_from_turn
from .session import SessionStore
from .session_compaction import (
    SESSION_COMPACTION_META_KEY,
    SessionCompactionPolicy,
    checkpoint_from_session_meta,
    compact_session_history,
)
from .session_restore import apply_to_task_state, restore_from_journal
from .verifier import compute_verifier_nudge
from .streaming import get_default_bus
from .transcript_blocks import BlockEnvelope
from .chart_hook import extract_chart_blocks, extract_chart_marker_ids
# ``..charting`` and ``..workspace.artifact_store`` are imported lazily
# inside the marker-resolution branch below — eager imports would
# create a circular cycle (nerya.agent.kernel → nerya.charting →
# nerya.agent.chart_block → nerya.agent.__init__ → kernel) that breaks
# CLI bootstrap because ``nerya.strategies`` already loads through
# ``backtest_bridge → render_chart → nerya.charting`` during startup.
from ..evolution.hooks import EvolutionHookBus


_LOG = logging.getLogger(__name__)


# Per-turn meta cap. Chat transcripts can stack up thousands of turns;
# each assistant row stores its full ``blocks`` / ``tool_trace`` so the
# dashboard can rehydrate the tool_use timeline after a reload. The cap
# keeps a single pathological tool_result (say, a 5 MB JSON dump) from
# blowing up the SQLite row. 256 KB comfortably holds a normal
# multi-tool turn but clips anything exotic — the UI still renders the
# truncation because the envelope is dropped intact, just flagged.
_ASSISTANT_TURN_META_CAP = 256 * 1024

_TURN_ACTIVITY_EVENT_KINDS = frozenset(
    {
        "team.start",
        "team.event",
        "team.member.start",
        "team.member.end",
        "team.member.skip",
        "team.member.timeout",
        "team.end",
        "team.duplicate",
        "team.subagent_duplicate",
        "subagent.start",
        "subagent.step",
        "subagent.end",
    }
)

_TURN_ACTIVITY_TEXT_LIMITS = {
    "prompt": 12_000,
    "assignment_prompt": 12_000,
    "role_prompt": 12_000,
    "reasoning": 4_000,
    "output": 16_000,
    "aggregated": 16_000,
    "error": 2_000,
}

_FAILED_ASSISTANT_HISTORY_MARKERS = (
    "Turn stopped before a complete model-written final answer was produced",
    "abort_reason: max_iterations",
    "abort_reason=max_iterations",
)


def _json_obj(raw: Any) -> dict[str, Any]:
    if isinstance(raw, dict):
        return raw
    try:
        parsed = json.loads(str(raw or "{}"))
    except Exception:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _compact_resume_value(value: Any, *, limit: int = 900) -> str:
    if value is None or value == "":
        return ""
    try:
        if isinstance(value, str):
            text = value
        else:
            text = json.dumps(value, ensure_ascii=False, default=str)
    except Exception:
        text = str(value)
    text = " ".join(text.split())
    if len(text) <= limit:
        return text
    return text[:limit] + f"...[truncated {len(text) - limit} chars]"


def _tool_name_for_resume(item: dict[str, Any]) -> str:
    skill = str(item.get("skill_id") or item.get("skill") or "").strip()
    action = str(item.get("action") or item.get("tool") or "").strip()
    if skill and action and not action.startswith(f"{skill}."):
        return f"{skill}.{action}"
    return action or skill or "tool"


def _tool_resume_lines_from_trace(
    tool_trace: Any,
    *,
    max_items: int = 8,
) -> list[str]:
    if not isinstance(tool_trace, list):
        return []
    lines: list[str] = []
    for item in tool_trace:
        if not isinstance(item, dict):
            continue
        name = _tool_name_for_resume(item)
        ok = bool(item.get("ok"))
        status = "ok" if ok else "blocked/error"
        payload = _compact_resume_value(item.get("payload"), limit=360)
        result = _compact_resume_value(item.get("result"), limit=780)
        error_kind = _compact_resume_value(item.get("error_kind"), limit=160)
        error = _compact_resume_value(item.get("error"), limit=360)
        details: list[str] = [f"{name} status={status}"]
        if payload:
            details.append(f"input={payload}")
        if result:
            details.append(f"result={result}")
        if error_kind:
            details.append(f"error_kind={error_kind}")
        if error:
            details.append(f"error={error}")
        lines.append("- " + "; ".join(details))
        if len(lines) >= max_items:
            break
    if len(tool_trace) > len(lines):
        lines.append(f"- ... {len(tool_trace) - len(lines)} more tool result(s) omitted")
    return lines


def _tool_trace_from_blocks(blocks: Any) -> list[dict[str, Any]]:
    if not isinstance(blocks, list):
        return []
    payload_by_call_id: dict[str, Any] = {}
    trace: list[dict[str, Any]] = []
    for env in blocks:
        block = env.get("block") if isinstance(env, dict) and isinstance(env.get("block"), dict) else env
        if not isinstance(block, dict):
            continue
        kind = str(block.get("kind") or "")
        call_id = str(block.get("call_id") or "")
        if kind == "tool_use":
            payload_by_call_id[call_id] = block.get("payload") or {}
            continue
        if kind != "tool_result":
            continue
        trace.append(
            {
                "call_id": call_id,
                "skill_id": block.get("skill_id") or "native",
                "action": block.get("action") or "",
                "payload": payload_by_call_id.get(call_id, {}),
                "ok": bool(block.get("ok")),
                "result": block.get("result"),
                "error": block.get("error"),
                "error_kind": block.get("error_kind"),
            }
        )
    return trace


def _tool_trace_from_event_rows(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    payload_by_call_id: dict[str, Any] = {}
    trace: list[dict[str, Any]] = []
    for event in events:
        if not isinstance(event, dict):
            continue
        payload = _json_obj(event.get("payload_json"))
        call_id = str(event.get("call_id") or "")
        tool = str(event.get("tool") or "")
        skill, _, action = tool.partition(".")
        if not action:
            action = skill
            skill = "native"
        phase = str(event.get("phase") or "")
        if phase == "tool_use":
            payload_by_call_id[call_id] = payload.get("payload") or {}
            continue
        if phase != "tool_result":
            continue
        ok_raw = event.get("ok")
        ok = bool(ok_raw) if ok_raw is not None else not bool(payload.get("error"))
        trace.append(
            {
                "call_id": call_id,
                "skill_id": skill or "native",
                "action": action,
                "payload": payload_by_call_id.get(call_id, {}),
                "ok": ok,
                "result": payload.get("result"),
                "error": payload.get("error"),
                "error_kind": payload.get("error_kind"),
            }
        )
    return trace


def _trace_paused_for_approval(tool_trace: Any) -> bool:
    if not isinstance(tool_trace, list):
        return False
    approval_markers = {"permission_pending", "approval_pending"}
    for item in tool_trace:
        if not isinstance(item, dict):
            continue
        kind = str(item.get("error_kind") or "").strip().lower()
        if kind in approval_markers:
            return True
        error_text = str(item.get("error") or "").lower()
        if any(marker in error_text for marker in approval_markers):
            return True
    return False


def _turn_paused_for_approval(turn: dict[str, Any]) -> bool:
    budget = turn.get("budget") if isinstance(turn.get("budget"), dict) else {}
    reason = str(
        turn.get("abort_reason")
        or budget.get("abort_reason")
        or turn.get("stopped_reason")
        or turn.get("stop_reason")
        or ""
    ).strip().lower()
    if reason in {"approval_pending", "permission_pending"}:
        return True
    trace = turn.get("tool_trace")
    if _trace_paused_for_approval(trace):
        return True
    return _trace_paused_for_approval(_tool_trace_from_blocks(turn.get("blocks")))


def _approval_resume_context_from_trace(tool_trace: list[dict[str, Any]]) -> str:
    lines = _tool_resume_lines_from_trace(tool_trace)
    if not lines:
        return ""
    body = "\n".join(lines)
    return (
        "[interrupted turn context]\n"
        "The previous assistant turn paused for operator approval before a final answer was produced. "
        "Continue the prior user task using these already observed tool results. Do not treat the approval notice as a new task, and do not repeat completed discovery unless the evidence is missing.\n"
        "Observed tool results:\n"
        f"{body}"
    )[:6_000]


def _approval_resume_context_from_assistant_row(row: dict[str, Any]) -> str:
    meta = _json_obj(row.get("meta_json"))
    turn = meta.get("turn") if isinstance(meta.get("turn"), dict) else {}
    if not turn or not _turn_paused_for_approval(turn):
        return ""
    trace = turn.get("tool_trace")
    if not isinstance(trace, list) or not trace:
        trace = _tool_trace_from_blocks(turn.get("blocks"))
    return _approval_resume_context_from_trace(trace)


def _approval_resume_context_from_events(events: list[dict[str, Any]]) -> str:
    trace = _tool_trace_from_event_rows(events)
    if not trace or not _trace_paused_for_approval(trace):
        return ""
    return _approval_resume_context_from_trace(trace)


def _assistant_history_turn_failed(row: dict[str, Any], content: str) -> bool:
    """Return true when a persisted assistant row is retry noise.

    A max-iteration assistant summary is useful for the dashboard, but
    harmful as model history: on retry the model tends to quote the old
    failure report instead of solving the latest user request.
    """

    if any(marker in content for marker in _FAILED_ASSISTANT_HISTORY_MARKERS):
        return True
    meta = _json_obj(row.get("meta_json"))
    turn = meta.get("turn") if isinstance(meta.get("turn"), dict) else {}
    budget = turn.get("budget") if isinstance(turn.get("budget"), dict) else {}
    aborted = bool(turn.get("aborted") or budget.get("aborted"))
    abort_reason = str(
        turn.get("abort_reason")
        or budget.get("abort_reason")
        or turn.get("stopped_reason")
        or ""
    ).strip().lower()
    if aborted and abort_reason in {"max_iterations", "max_tool_calls", "needs_summarisation"}:
        return True
    return aborted and not content.strip()


def _filter_failed_history_rows(
    rows: list[dict[str, Any]],
    *,
    tool_events_by_turn: dict[str, list[dict[str, Any]]] | None = None,
    preserve_approval_pauses: bool = False,
) -> list[dict[str, Any]]:
    skipped_turn_ids: set[str] = set()
    resume_context_by_turn: dict[str, str] = {}
    failed_assistant_message_ids: set[str] = set()
    for row in rows:
        if row.get("role") != "assistant":
            continue
        content = row.get("content")
        if not isinstance(content, str):
            continue
        if _assistant_history_turn_failed(row, content):
            tid = str(row.get("turn_id") or "").strip()
            if tid:
                resume_context = (
                    _approval_resume_context_from_assistant_row(row)
                    if preserve_approval_pauses
                    else ""
                )
                if resume_context:
                    resume_context_by_turn[tid] = resume_context
                    failed_assistant_message_ids.add(str(row.get("message_id") or ""))
                else:
                    skipped_turn_ids.add(tid)
    if preserve_approval_pauses:
        for tid, events in (tool_events_by_turn or {}).items():
            key = str(tid or "").strip()
            if not key or key in skipped_turn_ids or key in resume_context_by_turn:
                continue
            resume_context = _approval_resume_context_from_events(events)
            if resume_context:
                resume_context_by_turn[key] = resume_context
    if not skipped_turn_ids and not resume_context_by_turn:
        return rows
    out: list[dict[str, Any]] = []
    row_turn_ids = {str(row.get("turn_id") or "").strip() for row in rows}
    inserted_resume_turn_ids: set[str] = set()
    for row in rows:
        tid = str(row.get("turn_id") or "").strip()
        if tid in skipped_turn_ids:
            continue
        is_failed_assistant = str(row.get("message_id") or "") in failed_assistant_message_ids
        if is_failed_assistant and tid in resume_context_by_turn:
            next_row = dict(row)
            next_row["content"] = resume_context_by_turn[tid]
            next_row["meta_json"] = "{}"
            out.append(next_row)
            inserted_resume_turn_ids.add(tid)
            continue
        out.append(row)
        if (
            row.get("role") == "user"
            and tid in resume_context_by_turn
            and tid not in inserted_resume_turn_ids
        ):
            out.append(
                {
                    "message_id": f"{tid}:approval_resume_context",
                    "session_id": row.get("session_id"),
                    "turn_id": tid,
                    "role": "assistant",
                    "content": resume_context_by_turn[tid],
                    "ts": row.get("ts"),
                    "meta_json": "{}",
                }
            )
            inserted_resume_turn_ids.add(tid)
    for tid, resume_context in resume_context_by_turn.items():
        if tid in inserted_resume_turn_ids or tid in row_turn_ids:
            continue
        out.append(
            {
                "message_id": f"{tid}:approval_resume_context",
                "turn_id": tid,
                "role": "assistant",
                "content": resume_context,
                "meta_json": "{}",
            }
        )
    return out


def _compact_activity_text(value: Any, *, limit: int = 4_000) -> str:
    text = str(value or "")
    if len(text) <= limit:
        return text
    return text[:limit] + f"\n...[truncated {len(text) - limit} chars]"


def _compact_activity_value(value: Any, *, limit: int = 8_000, depth: int = 0) -> Any:
    if isinstance(value, str):
        return _compact_activity_text(value, limit=limit)
    if isinstance(value, (int, float, bool)) or value is None:
        return value
    if depth >= 4:
        try:
            return _compact_activity_text(
                json.dumps(value, ensure_ascii=False, default=str),
                limit=limit,
            )
        except Exception:
            return _compact_activity_text(value, limit=limit)
    if isinstance(value, list):
        items = [
            _compact_activity_value(
                item,
                limit=max(1_000, limit // 4),
                depth=depth + 1,
            )
            for item in value[:40]
        ]
        if len(value) > 40:
            items.append({"truncated_items": len(value) - 40})
        return items
    if isinstance(value, dict):
        out: dict[str, Any] = {}
        for key, item in value.items():
            skey = str(key)
            if skey in {"raw", "trace", "stack"}:
                continue
            out[skey] = _compact_activity_value(
                item,
                limit=max(1_000, limit // 3),
                depth=depth + 1,
            )
        try:
            rendered = json.dumps(out, ensure_ascii=False, default=str)
        except Exception:
            rendered = str(out)
        if len(rendered) > limit:
            return {
                "summary": _compact_activity_text(rendered, limit=limit),
                "truncated": True,
            }
        return out
    return _compact_activity_text(value, limit=limit)


def _compact_turn_activity_event(event: dict[str, Any]) -> dict[str, Any]:
    """Persist the human-auditable Team/SubAgent stream without raw bloat."""

    keep = {
        "kind",
        "seq",
        "event_id",
        "ts",
        "turn_id",
        "session_id",
        "strategy_id",
        "trigger_event_id",
        "call_id",
        "tool_call_id",
        "team_call_id",
        "team_run_id",
        "team_template",
        "team_task_id",
        "team_task_owner",
        "team_task_subject",
        "subagent",
        "name",
        "role",
        "owner",
        "tier",
        "status",
        "phase",
        "ok",
        "error",
        "error_kind",
        "message",
        "text",
        "task",
        "goal",
        "roles",
        "max_parallel",
        "timeout_s",
        "collaboration_model",
        "step_kind",
        "iteration",
        "skill",
        "action",
        "payload_keys",
        "payload",
        "input_payload",
        "assignment_prompt",
        "role_prompt",
        "prompt_path",
        "allowed_skills",
        "callable_skills",
        "native_tools",
        "context_chars",
        "prompt",
        "prompt_chars",
        "parsed_keys",
        "reasoning",
        "reasoning_tokens",
        "reasoning_effort",
        "provider",
        "model",
        "output",
        "metrics",
        "iterations",
        "skill_calls",
        "skill_calls_n",
        "rejected",
        "rejected_actions_n",
        "tokens",
        "usd",
        "wall_ms",
        "close_reason",
        "roles_succeeded",
        "roles_failed",
        "tokens_total",
        "usd_total",
        "results",
        "failures",
        "aggregated",
    }
    out: dict[str, Any] = {}
    for key in keep:
        if key not in event:
            continue
        limit = _TURN_ACTIVITY_TEXT_LIMITS.get(key, 8_000)
        out[key] = _compact_activity_value(event.get(key), limit=limit)
    return out


def _compact_turn_activity_events(
    events: list[dict[str, Any]],
    *,
    max_events: int = 300,
) -> list[dict[str, Any]]:
    if len(events) <= max_events:
        return [_compact_turn_activity_event(e) for e in events]
    head = [_compact_turn_activity_event(e) for e in events[:40]]
    tail = [_compact_turn_activity_event(e) for e in events[-(max_events - 41):]]
    return [
        *head,
        {
            "kind": "activity.truncated",
            "truncated_events": len(events) - len(head) - len(tail),
        },
        *tail,
    ]


def _compact_turn_payload(
    *,
    turn_id: str,
    blocks: list[dict[str, Any]],
    actions: list[dict[str, Any]],
    tool_trace: list[dict[str, Any]],
    iterations: int | None,
    tool_calls_count: int | None,
    stop_reason: str | None,
    aborted: bool | None,
    abort_reason: str | None,
    error_count: int | None,
    final_text: str,
) -> dict[str, Any]:
    """Shape the turn payload persisted on the assistant message row.

    Mirrors :class:`AgentTurnResult` fields the dashboard consumes when
    rendering ``TurnBlocks`` so an imported session reconstructs with
    the same block stream the live turn produced. If the serialised
    payload exceeds :data:`_ASSISTANT_TURN_META_CAP`, progressively drop
    the heaviest lists (``blocks`` first, then ``tool_trace``) so at
    least the summary fields survive.
    """

    def _serialised_size(obj: Any) -> int:
        try:
            return len(
                json.dumps(obj, ensure_ascii=False, default=str)
            )
        except Exception:
            return _ASSISTANT_TURN_META_CAP + 1

    payload: dict[str, Any] = {
        "turn_id": turn_id,
        "harness": "native",
        "reply_text": final_text,
        "final_text": final_text,
        "blocks": blocks or [],
        "actions": actions or [],
        "tool_trace": tool_trace or [],
        "budget": {
            "iterations": iterations,
            "tool_calls": tool_calls_count,
            "errors": error_count,
            "aborted": aborted,
            "abort_reason": abort_reason,
        },
        "stopped_reason": stop_reason,
    }
    if _serialised_size(payload) <= _ASSISTANT_TURN_META_CAP:
        return payload
    # Shed the heaviest fields in order. The summary (actions + budget)
    # is what the dashboard falls back on today, so preserve it.
    payload["blocks_truncated"] = True
    payload["blocks"] = []
    if _serialised_size(payload) <= _ASSISTANT_TURN_META_CAP:
        return payload
    payload["tool_trace_truncated"] = True
    payload["tool_trace"] = []
    return payload


_STRATEGY_TRIGGER_SOURCES = {
    "scheduled_session",
    "schedule",
    "cron",
    "price",
    "news",
    "social",
    "onchain",
    "trigger",
    "strategy",
    "strategy_runtime",
}

_MANUAL_TRIGGER_SOURCES = {
    "dashboard",
    "telegram",
    "discord",
    "slack",
    "feishu",
    "mcp",
    "sdk",
}

_MANUAL_TRIGGER_KINDS = {
    "user.chat",
    "agent.user_message",
    "manual.chat",
    "manual.order",
}


def _render_temporal_context_block() -> str:
    """Render the per-turn date and freshness rules.

    The runtime injects a small ``currentDate`` context block into each
    conversation. Nerya needs the same always-on anchor because trading
    and research questions often depend on what "current" means.
    """

    current = now()
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    local = current.astimezone()
    utc = current.astimezone(timezone.utc)
    utc_iso = utc.isoformat(timespec="seconds").replace("+00:00", "Z")
    local_date = local.date().isoformat()
    return (
        f"Temporal context: Today's date is {local_date} (local time). "
        f"Current UTC time is {utc_iso}.\n"
        "Current and recent fact rule: for facts that could have changed "
        "recently, including markets, news, regulations, model/provider "
        "availability, public/company figures, prices, schedules, and anything "
        "the user calls current/latest/recent/today/this year, use live tools "
        "such as web_search_fetch, web_fetch, connector_list/connector_view, "
        "portfolio/strategy tools, or journals before answering. Report the "
        "evidence date/source you used. If current tools fail or are "
        "unavailable, say the current status is unverified instead of "
        "presenting model-memory facts as current. Do not describe 2024-2025 "
        "as the current environment when the date above is 2026 unless the "
        "evidence explicitly says that period is the relevant historical "
        "context."
    )


_BROWSER_TASK_RE = re.compile(
    r"https?://|www\.|browser|webpage|page|dom|click|screenshot|"
    r"viewport|form|captcha|浏览器|网页|页面|点击|截图|做题|打开|按钮|登录|验证码",
    re.IGNORECASE,
)

_TRADING_TASK_RE = re.compile(
    r"\b(btc|eth|sol|usdt|trade|trading|order|position|portfolio|"
    r"risk|market|signal|price|candle|ohlcv)\b|"
    r"交易|买卖|下单|仓位|持仓|组合|资产|风控|行情|市场|信号|策略|止损|止盈|K线|币价",
    re.IGNORECASE,
)

_FULL_BROWSER_SCOPE_RE = re.compile(
    r"\b(all|every|complete|finish|entire|full|end-to-end|until done)\b|"
    r"全部|所有|完整|全量|做完|完成全部|每个|每一|通关|全流程",
    re.IGNORECASE,
)

def _looks_like_browser_task(user_text: str) -> bool:
    return bool(_BROWSER_TASK_RE.search(user_text or ""))


def _looks_like_trading_task(user_text: str) -> bool:
    return bool(_TRADING_TASK_RE.search(user_text or ""))


def _has_explicit_full_browser_scope(user_text: str) -> bool:
    return bool(_FULL_BROWSER_SCOPE_RE.search(user_text or ""))


def _render_output_language_block(user_text: str | None) -> str:
    return (
        "Output language:\n"
        "- Write the final answer and any user-visible conclusion in the "
        "same natural language as the latest user request. Infer that "
        "language from the user prompt itself instead of relying on fixed "
        "language-name mappings.\n"
        "- Translate or synthesize tool/sub-agent outputs into that same "
        "user-facing language; do not leave a mixed-language report just "
        "because evidence, tool fields, or team member outputs used another "
        "language. "
        "Translate headings, labels, and natural-language field names too.\n"
        "- Preserve proper nouns, ticker symbols, company names, model/provider "
        "names, API names, code identifiers, file paths, URLs, numeric metrics, "
        "and direct source titles in their original form."
    )


def _model_user_text_with_scope_boundary(user_text: str) -> str:
    if not _looks_like_browser_task(user_text):
        return user_text
    if _looks_like_trading_task(user_text):
        return user_text
    if _has_explicit_full_browser_scope(user_text):
        return user_text
    return (
        user_text
        + "\n\nBrowser scope boundary: the latest message does not explicitly "
        "request completing every item, page, question, or a continuous workflow. "
        "Perform the smallest representative interaction needed to answer or "
        "verify the browser task, then stop and report concise evidence. Do not "
        "advance into additional items after that representative interaction."
    )


def _is_approval_continue_trigger(
    trigger: dict[str, Any],
    payload: dict[str, Any] | None = None,
) -> bool:
    payload = payload if isinstance(payload, dict) else {}
    values = (
        trigger.get("source"),
        trigger.get("kind"),
        payload.get("source"),
        payload.get("channel"),
    )
    return any(
        str(value or "").strip().lower() in {"approval_continue", "approval.continue"}
        for value in values
    )


def _model_user_text_for_trigger(user_text: str, trigger: dict[str, Any]) -> str:
    payload = trigger.get("payload") if isinstance(trigger.get("payload"), dict) else {}
    if _is_approval_continue_trigger(trigger, payload):
        return (
            "An operator approved a pending permission request from the previous turn. "
            "Continue the prior user task from the available conversation context and observed tool results. "
            "Do not treat this approval notice as a new task. "
            "Do not repeat already completed discovery unless the prior evidence is missing. "
            "If the approved action still cannot run, report the blocker and the evidence already collected."
        )
    return _model_user_text_with_scope_boundary(user_text)


def _latest_prior_user_text(messages: list[dict[str, Any]]) -> str | None:
    for message in reversed(messages or []):
        if message.get("role") != "user":
            continue
        content = message.get("content")
        if isinstance(content, str) and content.strip():
            return content
    return None


def _render_turn_focus_block(
    *,
    user_text: str | None,
    attached_skills: Optional[list[str]] = None,
) -> str:
    """Keep broad native tool exposure aligned with the latest user ask."""

    text = user_text or ""
    skill_names = {str(skill).strip().lower() for skill in attached_skills or []}
    browser_focused = "browser" in skill_names or _looks_like_browser_task(text)
    trading_focused = _looks_like_trading_task(text)

    if browser_focused and not trading_focused:
        return (
            "Turn focus: browser/web interaction.\n"
            "The latest user message is authoritative. Use the browser skill/script "
            "for page interaction, DOM state, screenshots, console/network evidence, "
            "or form actions. Keep the task scoped to the page or prompt. "
            "Browser scope rule: infer the requested completion scope from the "
            "latest message. If it does not explicitly ask to complete every "
            "item, page, question, or a continuous workflow, perform the "
            "smallest representative interaction needed to answer or verify, "
            "then stop with evidence. "
            "Do not introduce trading, portfolio, market-signal, strategy, order, "
            "or risk workflows unless the latest user message explicitly asks for "
            "them. When the requested page task reaches a clear answer or submitted "
            "state, stop and report the result instead of continuing to unrelated "
            "workflows.\n"
            "Browser script payload reminder: use script_run with skill_id=\"browser\", "
            "name=\"browser_session.py\", args=[\"--json\", "
            "\"{\\\"operation\\\":\\\"open|snapshot|click|eval|screenshot|console|network\\\", ...}\"]; "
            "eval JavaScript belongs in \"expression\"."
        )
    if not trading_focused:
        return (
            "Turn focus: general operator task.\n"
            "The latest user message is authoritative. Do not introduce trading, "
            "portfolio, market-signal, strategy, order, or risk workflows unless "
            "the latest user message explicitly asks for them."
        )
    return (
        "Turn focus: trading/market task.\n"
        "Trading and market-data tools are in scope, but answer only the latest "
        "user request and do not drift into browser, coding, or unrelated "
        "workflows unless asked."
    )


def _strategy_triggered_order_turn(
    strategy_id: Optional[str],
    trigger: dict[str, Any],
) -> bool:
    if not strategy_id:
        return False
    source = str((trigger or {}).get("source") or "").strip().lower()
    kind = str((trigger or {}).get("kind") or "").strip().lower()
    payload = (trigger or {}).get("payload")
    payload = payload if isinstance(payload, dict) else {}
    if bool((trigger or {}).get("strategy_triggered") or payload.get("strategy_triggered")):
        return True
    if str((trigger or {}).get("origin") or payload.get("origin") or "").lower() == "strategy":
        return True
    if source in _STRATEGY_TRIGGER_SOURCES or source.startswith("strategy"):
        return True
    if kind.startswith(("price.", "news.", "social.", "onchain.", "schedule.")):
        return True
    if source in _MANUAL_TRIGGER_SOURCES or kind in _MANUAL_TRIGGER_KINDS:
        return False
    return False


# ---------------------------------------------------------------------------
# Public turn result
# ---------------------------------------------------------------------------


@dataclass
class AgentTurnResult:
    """One agent turn's output, surfaced to the API/SDK.

    Field shape mirrors what ``api/routes_agent.py``,
    ``api/gateway_events.py``, and ``sdk/agent_api.py`` consume today —
    minus the legacy planner/subagent artefacts (``plan_kind``,
    ``plan_tier``, ``subagent_outputs``) which the workspace-native
    loop no longer produces. The model decides what to do; the kernel
    just records what happened.

    * ``decision`` — derived envelope ``{"action": "send_message",
      "text": <final assistant text>}``. Kept for legacy callers that
      branch on ``decision["action"]``; new callers should read
      ``final_text`` and ``actions`` directly.
    * ``actions`` — one entry per ``tool_use`` block, plus a synthetic
      ``send_message`` entry carrying ``final_text`` for chat surfaces.
    * ``tool_trace`` — one entry per ``tool_result`` block; mirrors the
      legacy harness shape (``ok`` / ``error`` / ``elapsed_ms``).
    * ``steps`` / ``blocks`` — both contain
      :class:`~nerya.agent.transcript_blocks.BlockEnvelope` dicts, the
      provider-native transcript. ``steps`` is kept for clients still
      pointed at the old field name.
    """

    trigger_event_id: Optional[str]
    strategy_id: Optional[str]
    session_id: Optional[str]
    turn_id: str
    decision: dict[str, Any]
    actions: list[dict[str, Any]] = field(default_factory=list)
    tool_trace: list[dict[str, Any]] = field(default_factory=list)
    budget: dict[str, Any] = field(default_factory=dict)
    steps: list[dict[str, Any]] = field(default_factory=list)
    blocks: list[dict[str, Any]] = field(default_factory=list)
    stopped_reason: Optional[str] = None
    final_text: str = ""
    iterations: int = 0
    harness: str = "native"
    activity_events: list[dict[str, Any]] = field(default_factory=list)
    artifact_index: dict[str, Any] = field(default_factory=dict)
    final_report: dict[str, Any] = field(default_factory=dict)
    attachments: list[dict[str, Any]] = field(default_factory=list)


def _loop_config_from_config(
    config: Config,
    *,
    llm_tier: str | None = None,
    reasoning_effort: str | None = None,
    reasoning_summary: str | None = None,
    model_provider: str | None = None,
    model_id: str | None = None,
    session_id: str | None = None,
    strategy_id: str | None = None,
    trigger_event_id: str | None = None,
    compact_preservation_cb: Any = None,
) -> LoopConfig:
    """Build native loop limits from config, preserving legacy harness knobs."""

    return LoopConfig(
        max_iterations=int(
            config.get(
                "agent.native.max_iterations",
                config.get("agent.harness.max_iterations", 48),
            )
        ),
        compact_threshold=int(config.get("agent.native.compact_threshold", 60)),
        keep_tail_messages=int(config.get("agent.native.keep_tail_messages", 24)),
        max_tokens=int(config.get("agent.native.max_tokens", 4096)),
        tier=llm_tier or config.get("agent.native.tier"),
        max_wall_seconds=(
            float(
                config.get(
                    "agent.native.max_wall_seconds",
                    config.get("agent.harness.max_wall_seconds", 0.0),
                )
            )
            or None
        ),
        max_total_tool_calls=(
            int(
                config.get(
                    "agent.native.max_total_tool_calls",
                    config.get("agent.harness.max_tool_calls", 0),
                )
            )
            or None
        ),
        reasoning_effort=reasoning_effort,
        reasoning_summary=reasoning_summary,
        model_provider=model_provider,
        model_id=model_id,
        session_id=session_id,
        strategy_id=strategy_id,
        trigger_event_id=trigger_event_id,
        compact_preservation_cb=compact_preservation_cb,
    )


# ---------------------------------------------------------------------------
# Kernel
# ---------------------------------------------------------------------------


@dataclass
class AgentKernel:
    """Run one agent turn through the workspace-native loop."""

    config: Config
    skills: SkillKernel
    permission_mode: PermissionMode = PermissionMode.DEFAULT
    reasoning_effort: str | None = None
    reasoning_summary: str | None = None
    llm_tier: str | None = None
    model_provider: str | None = None
    model_id: str | None = None

    def __post_init__(self) -> None:
        self._hooks = HookRegistry(self.config)
        self._sessions = SessionStore(self.config.paths.root)
        self._registry = ToolRegistry()
        self._deps: Optional[NativeToolDeps] = None
        self._memory: Optional[Memory] = None
        self._subagents: Optional[Any] = None
        self._evolution_hooks = EvolutionHookBus(self.config)
        # Per-kernel turn counter feeds the periodic memory compaction
        # tick so we don't run a full filesystem walk after every
        # single turn — only every Nth.
        self._turn_count: int = 0

    # --------------------------------------------------------------- props

    @property
    def hooks(self) -> HookRegistry:
        return self._hooks

    @property
    def sessions(self) -> SessionStore:
        return self._sessions

    @property
    def tool_registry(self) -> ToolRegistry:
        """Return the tool registry, building it lazily on first access."""
        self._ensure_registry()
        return self._registry

    @property
    def memory(self) -> Memory:
        """Long-term memory (workspace ``memory/*.md`` + per-strategy
        ``learnings.md``).

        Lazily attached so unit tests that build a kernel without a
        real workspace don't pay the I/O cost. Mirrors
        :class:`agent_runtime.MemoryManager` in spirit: the kernel keeps
        a single instance for the whole session, the native
        ``memory_recall`` / ``memory_remember`` tools read/write through
        it, and end-of-turn hooks (see :meth:`_after_turn_memory`) call
        :meth:`Memory.append_global` for durable lessons.
        """

        if self._memory is None:
            self._memory = Memory(paths=self.config.paths)
        return self._memory

    @property
    def subagents(self):
        """Workspace-level :class:`SubAgentDispatcher`.

        Held on the kernel so the parent runtime can dispatch children
        without rebuilding the dispatcher per turn (it caches the
        :class:`LLMGateway` and the subagent registry). The native
        ``subagent_run`` tool reads from the same registry — exposing
        it on the kernel makes recipes and hooks reusable too.
        """

        if self._subagents is None:
            from ..subagents.dispatcher import SubAgentDispatcher

            self._subagents = SubAgentDispatcher(
                config=self.config, skills=self.skills,
                # Subagents inherit the parent's full native-tool surface
                # (connector_list / connector_view, memory, search, file
                # primitives, …). Accessing the property here guarantees
                # the native registry is built before children look up tools.
                tool_registry=self.tool_registry,
            )
        return self._subagents

    def refresh_action_map(self) -> dict[str, Any]:
        """Force the tool registry to be rebuilt on the next turn.

        Compatibility shim — the legacy ``ACTION_MAP`` is gone; the tool
        registry is rebuilt on the next call to :meth:`run_turn` so any
        new native tools or hot-loaded user skills come into view. The
        method preserves the old name so callers that refresh after a
        skill install/uninstall keep working.
        """

        self._deps = None
        return {}

    # ------------------------------------------------------------- run_turn

    def run_turn(
        self,
        *,
        trigger: dict[str, Any],
        strategy_id: Optional[str] = None,
        session_id: Optional[str] = None,
        turn_id: Optional[str] = None,
        attached_skills: Optional[list[str]] = None,
        cancel_token: Any = None,
    ) -> AgentTurnResult:
        """Run a single agent turn for ``trigger``.

        ``attached_skills`` lets a scheduled session pin a per-turn
        skill whitelist; it is rendered into the system prompt so the
        model prefers those skills. ``cancel_token`` is a
        :class:`~nerya.harness.cancellation.CancelToken` — when
        provided, the registry-side hooks honour it between iterations.
        """

        trigger_event_id = trigger.get("id") or trigger.get("event_id")
        turn_id = str(turn_id or "").strip() or new_turn_id()

        session_existed = False
        if session_id:
            try:
                session_existed = self._sessions.exists(session_id)
                self._sessions.ensure(session_id, strategy_id=strategy_id)
            except Exception:
                _LOG.debug("session ensure failed", exc_info=True)

        if cancel_token is not None:
            try:
                from ..harness.cancellation import register_token as _reg_token

                if session_id:
                    _reg_token(session_id, cancel_token)
                _reg_token(turn_id, cancel_token)
            except Exception:
                pass

        _bind_config(turn_id, self.config)
        self._hooks.fire(
            "before_turn",
            HookContext(
                phase="before_turn",
                turn_id=turn_id,
                trigger_event_id=trigger_event_id,
                strategy_id=strategy_id,
                session_id=session_id,
                data={"trigger": trigger},
            ),
        )

        result: Optional[AgentTurnResult] = None
        try:
            result = self._run(
                turn_id=turn_id,
                trigger=trigger,
                trigger_event_id=trigger_event_id,
                strategy_id=strategy_id,
                session_id=session_id,
                attached_skills=attached_skills,
                cancel_token=cancel_token,
                session_existed=session_existed,
            )
            return result
        finally:
            if session_id and result is not None:
                try:
                    invoked: list[str] = []
                    for rec in (result.tool_trace or []):
                        sid = str(rec.get("skill_id") or rec.get("skill") or "")
                        if sid and sid not in invoked:
                            invoked.append(sid)
                    top_action = (
                        (result.actions[0].get("action") if result.actions else None)
                        or (
                            result.decision.get("action")
                            if isinstance(result.decision, dict)
                            else None
                        )
                        or "noop"
                    )
                    self._sessions.append_turn(
                        session_id,
                        turn_id,
                        invoked_skills=invoked,
                        last_action=top_action,
                        strategy_id=strategy_id,
                    )
                except Exception:
                    pass
            if result is not None:
                # Long-running invariants the legacy planner used to handle
                # before the workspace-native rewrite — restored here so
                # they still fire on every turn:
                #
                # 1. ``maybe_propose_from_turn`` scans the journal for
                #    consecutive no-ops / error spikes and files a
                #    learning_update proposal *without* mutating live
                #    config. Cheap to run; gated by the journal tail it
                #    reads, not by an LLM call.
                # 2. ``_after_turn_memory`` appends a one-line summary to
                #    ``memory/global.md`` so the next turn's
                #    ``memory_recall`` block has the freshest lesson. Off
                #    by default — operator turns it on per workspace.
                self._after_turn_evolve(turn_id=turn_id, result=result)
                self._after_turn_memory(
                    turn_id=turn_id,
                    result=result,
                    strategy_id=strategy_id,
                )
                try:
                    self._evolution_hooks.after_turn(
                        turn_id=turn_id,
                        result=result,
                    )
                except Exception:
                    _LOG.debug("evolution after_turn hook failed", exc_info=True)
                self._turn_count += 1
                self._maybe_compact_memory()
            self._hooks.fire(
                "after_turn",
                HookContext(
                    phase="after_turn",
                    turn_id=turn_id,
                    trigger_event_id=trigger_event_id,
                    strategy_id=strategy_id,
                    session_id=session_id,
                    data={
                        "ok": result is not None,
                        "stopped_reason": getattr(result, "stopped_reason", None),
                        "actions_count": len(getattr(result, "actions", []) or []),
                    },
                ),
            )
            _unbind_config(turn_id)
            if cancel_token is not None:
                try:
                    from ..harness.cancellation import unregister_token as _unreg

                    if session_id:
                        _unreg(session_id)
                    _unreg(turn_id)
                except Exception:
                    pass

    # -------------------------------------------------------------- _run

    def _run(
        self,
        *,
        turn_id: str,
        trigger: dict[str, Any],
        trigger_event_id: Optional[str],
        strategy_id: Optional[str],
        session_id: Optional[str],
        attached_skills: Optional[list[str]],
        cancel_token: Any = None,
        session_existed: bool = False,
    ) -> AgentTurnResult:
        deps = self._ensure_registry()
        strategy_order_auto_approve = _strategy_triggered_order_turn(
            strategy_id,
            trigger,
        )
        deps.active_strategy_id = strategy_id
        deps.active_session_id = session_id
        deps.active_trigger_event_id = trigger_event_id
        deps.active_trigger_source = str((trigger or {}).get("source") or "")
        deps.active_trigger_kind = str((trigger or {}).get("kind") or "")
        deps.strategy_order_auto_approve = strategy_order_auto_approve
        deps.permission_mode = self.permission_mode.value
        # Replay todos / plan-mode flag for resumed sessions so the
        # model picks up where it left off instead of starting from an
        # empty :class:`TaskState`. We snapshot the live todos *before*
        # the turn so the verifier nudge can compute "newly completed"
        # accurately at the end.
        if session_existed and session_id:
            try:
                restored = restore_from_journal(self.config.paths, session_id=session_id)
                apply_to_task_state(restored, task_state=deps.task_state)
            except Exception:
                _LOG.debug("session restore application failed", exc_info=True)
        # In ``auto`` / ``yolo`` permission modes we run unattended, so any
        # plan the previous turn submitted via ``exit_plan_mode`` would
        # otherwise sit forever waiting for an operator that isn't there.
        # Auto-resolve pending plans the same way an operator would tap
        # "Approve" in the dashboard — the next turn's ``plan_status``
        # poll then returns ``approved`` and the model proceeds with
        # mutating tools. Plan mode itself stays on until the model exits
        # it explicitly so progress remains in the audit trail.
        if self.permission_mode in (PermissionMode.AUTO, PermissionMode.YOLO):
            if (
                deps.task_state.pending_plan_id is not None
                and deps.task_state.plan_decision is None
            ):
                try:
                    deps.task_state.resolve_plan(approved=True)
                except Exception:
                    _LOG.debug("auto plan approval failed", exc_info=True)
        todos_before = deps.task_state.snapshot_todos()
        gw = LLMGateway(self.config)

        permission_context = PermissionContext(mode=self.permission_mode)
        if strategy_order_auto_approve:
            permission_context.session_rules.append(
                PermissionRule(
                    tool="trade_intent_submit",
                    namespace="native",
                    decision=PermissionDecisionKind.ALLOW,
                    reason="strategy-triggered order auto approval",
                )
            )
        engine = PermissionEngine()

        def _approval_cb(call, _descriptor, _decision):
            return self._lookup_tool_permission_decision(
                session_id=session_id,
                tool_name=str(getattr(call, "name", "") or ""),
                payload=dict(getattr(call, "arguments", {}) or {}),
                call_id=str(getattr(call, "id", "") or ""),
            )

        executor = NativeToolExecutor(
            registry=self._registry,
            permission_engine=engine,
            permission_context=permission_context,
            approval_cb=_approval_cb,
        )
        orchestrator = ToolOrchestrator(
            registry=self._registry,
            executor=executor,
            max_parallel=int(self.config.get("agent.native.max_parallel", 4)),
        )
        def _compact_preservation_cb(
            transcript: list[dict[str, Any]],
        ) -> list[dict[str, Any]]:
            """Re-attach file-state breadcrumbs after a compact pass.

            ``compact_transcript`` already preserves invoked-skill
            envelopes and pinned messages; the file-state cache lives
            outside the transcript and survives compact untouched.
            But the model loses sight of "I previously read X.py" if
            the underlying ``read_file`` tool_use was dropped. Inject
            a single system-style breadcrumb listing the most recent
            paths the cache knows about so the model can recover.
            """

            try:
                snapshot = deps.file_state.snapshot()
            except Exception:
                return transcript
            if not snapshot:
                return transcript
            # Take up to 16 most recently touched entries.
            ordered = sorted(
                snapshot,
                key=lambda e: max(
                    int(e.get("last_read_seq") or 0),
                    int(e.get("last_write_seq") or 0),
                ),
                reverse=True,
            )[:16]
            if not ordered:
                return transcript
            bullets = []
            for e in ordered:
                p = e.get("path")
                if not p:
                    continue
                marker = "edited" if e.get("last_write_seq") else "read"
                bullets.append(f"- {marker}: {p}")
            if not bullets:
                return transcript
            attachment = {
                "role": "system",
                "kind": "transcript.compact.attachments",
                "content": (
                    "[after compact] You previously interacted with "
                    "the following workspace files this turn — re-read "
                    "the ones you still need before issuing edits "
                    "(file-state cache survives compact, but the "
                    "original read/edit blocks were dropped):\n"
                    + "\n".join(bullets)
                ),
                "pinned": True,
            }
            # Skip if we already injected an identical attachment in a
            # prior compact pass — keeps the transcript idempotent.
            for m in transcript:
                if (
                    isinstance(m, dict)
                    and m.get("kind") == "transcript.compact.attachments"
                    and m.get("content") == attachment["content"]
                ):
                    return transcript
            # Insert right after the compact breadcrumb (or at the
            # head of the user-visible region if no breadcrumb).
            insert_at = 0
            for i, m in enumerate(transcript):
                if (
                    isinstance(m, dict)
                    and m.get("kind") == "transcript.compact.breadcrumb"
                ):
                    insert_at = i + 1
                    break
            new_transcript = list(transcript)
            new_transcript.insert(insert_at, attachment)
            return new_transcript

        loop_config = _loop_config_from_config(
            self.config,
            llm_tier=self.llm_tier,
            reasoning_effort=self.reasoning_effort,
            reasoning_summary=self.reasoning_summary,
            model_provider=self.model_provider,
            model_id=self.model_id,
            session_id=session_id,
            strategy_id=strategy_id,
            trigger_event_id=trigger_event_id,
            compact_preservation_cb=_compact_preservation_cb,
        )
        bus = get_default_bus()
        tool_payloads: dict[str, dict[str, Any]] = {}
        captured_activity_events: list[dict[str, Any]] = []
        captured_team_run_ids: set[str] = set()
        captured_team_call_ids: set[str] = set()
        activity_min_seq = bus.latest_seq()
        # Captured during ``permission_pending`` tool results so we can
        # splice an ``approval_request`` block into ``outcome.blocks``
        # after the loop returns. Without this, the approval card lives
        # only on the in-memory event bus and disappears the moment the
        # dashboard switches from the live stream to ``msg.turn.blocks``
        # (page reload, session re-open, follow-up turn).
        captured_approvals: list[tuple[str, dict[str, Any]]] = []
        # Captured during ``tool_result`` events that surface a
        # ``chart_blocks`` field. We splice these into ``outcome.blocks``
        # after the loop returns so the dashboard renders the chart in
        # ``msg.turn.blocks`` (post-turn / reload), and we publish them
        # on the streaming bus so the live activity panel can paint the
        # chart immediately. Without the post-loop splice the chart
        # vanishes the moment the chat re-renders from persisted blocks.
        captured_charts: list[tuple[str, dict[str, Any]]] = []

        def _activity_event_sink(event: dict[str, Any]) -> None:
            try:
                seq = int(event.get("seq") or 0)
            except Exception:
                seq = 0
            if seq <= activity_min_seq:
                return
            kind = str(event.get("kind") or "")
            if kind not in _TURN_ACTIVITY_EVENT_KINDS:
                return
            event_turn_id = str(event.get("turn_id") or "")
            team_run_id = str(event.get("team_run_id") or "")
            team_call_id = str(
                event.get("team_call_id")
                or event.get("tool_call_id")
                or event.get("call_id")
                or ""
            )
            matches_turn = event_turn_id == turn_id
            if matches_turn:
                if team_run_id:
                    captured_team_run_ids.add(team_run_id)
                if team_call_id:
                    captured_team_call_ids.add(team_call_id)
            elif team_run_id and team_run_id in captured_team_run_ids:
                matches_turn = True
            elif team_call_id and team_call_id in captured_team_call_ids:
                matches_turn = True
            if not matches_turn:
                return
            item = dict(event)
            item.setdefault("turn_id", turn_id)
            if session_id:
                item.setdefault("session_id", session_id)
            if strategy_id:
                item.setdefault("strategy_id", strategy_id)
            captured_activity_events.append(item)

        try:
            _unsubscribe_activity_events = bus.subscribe(_activity_event_sink)
        except Exception:
            _unsubscribe_activity_events = None

        def _event_sink(env: BlockEnvelope) -> None:
            """Translate native block envelopes onto the streaming bus.

            The dashboard's live activity panel polls
            ``/agent/stream/events`` for ``tool.start`` /
            ``tool.complete`` / ``message.delta`` shapes; mapping the
            native blocks onto that vocabulary keeps the existing UI
            working with no client-side change.
            """

            block = env.block or {}
            kind = block.get("kind")
            common = {
                "turn_id": turn_id,
                "session_id": session_id,
                "strategy_id": strategy_id,
                "trigger_event_id": trigger_event_id,
            }
            try:
                if kind == "text":
                    bus.publish(
                        "message.delta",
                        text=str(block.get("text") or ""),
                        completed=False,
                        **common,
                    )
                elif kind == "thinking":
                    bus.publish(
                        "turn.step",
                        step={
                            "kind": "thinking",
                            "status": "ok",
                            "wall_ms": 0,
                            "detail": {
                                "text": (str(block.get("text") or ""))[:4096],
                                "summary": str(block.get("summary") or ""),
                            },
                        },
                        **common,
                    )
                elif kind == "tool_use":
                    call_id = str(block.get("call_id") or "")
                    tool_payloads[call_id] = dict(block.get("payload") or {})
                    bus.publish(
                        "tool.start",
                        tool_call_id=call_id,
                        call_id=call_id,
                        skill_id=str(block.get("skill_id") or "native"),
                        action=str(block.get("action") or ""),
                        payload=dict(block.get("payload") or {}),
                        caller="agent:loop",
                        **common,
                    )
                elif kind == "tool_result":
                    tool_name = str(block.get("action") or block.get("skill_id") or "")
                    bus.publish(
                        "tool.complete",
                        tool_call_id=str(block.get("call_id") or ""),
                        call_id=str(block.get("call_id") or ""),
                        skill_id=str(block.get("skill_id") or "native"),
                        action=str(block.get("action") or ""),
                        ok=bool(block.get("ok")),
                        error=block.get("error"),
                        error_kind=block.get("error_kind"),
                        elapsed_ms=block.get("elapsed_ms"),
                        result=block.get("result"),
                        **common,
                    )
                    try:
                        self._evolution_hooks.after_tool_result(
                            turn_id=turn_id,
                            session_id=session_id,
                            strategy_id=strategy_id,
                            tool=tool_name,
                            ok=bool(block.get("ok")),
                            error=(
                                str(block.get("error"))
                                if block.get("error") is not None
                                else None
                            ),
                            error_kind=(
                                str(block.get("error_kind"))
                                if block.get("error_kind") is not None
                                else None
                            ),
                        )
                    except Exception:
                        _LOG.debug("evolution tool hook failed", exc_info=True)
                    if bool(block.get("ok")):
                        result_payload = block.get("result")
                        # Inline path: skill returned ``chart_blocks: [...]``
                        # in its JSON output. Most static skills
                        # land here.
                        try:
                            chart_blocks = extract_chart_blocks(result_payload)
                        except Exception:
                            chart_blocks = []
                            _LOG.debug("chart extract failed", exc_info=True)
                        # Marker path: dynamic-code script printed
                        # ``@@nerya:chart@@ <id>`` after publishing the
                        # artifact via ``client.charts.publish``. We
                        # rebuild a minimal ChartBlock dict from the
                        # persisted payload so the splice still gets a
                        # valid envelope without requiring the script
                        # to dump the whole block to stdout.
                        try:
                            marker_ids = extract_chart_marker_ids(result_payload)
                        except Exception:
                            marker_ids = []
                            _LOG.debug("chart marker extract failed", exc_info=True)
                        if marker_ids:
                            # Lazy imports: see top-of-file note about
                            # the kernel ↔ charting import cycle.
                            try:
                                from ..charting import load_chart_artifact as _load_chart_artifact
                                from ..workspace.artifact_store import ArtifactStore as _ArtifactStore

                                store = _ArtifactStore(self.config.paths)
                            except Exception:
                                store = None
                                _load_chart_artifact = None  # type: ignore[assignment]
                                _LOG.debug(
                                    "chart marker artifact store init failed",
                                    exc_info=True,
                                )
                            seen_ids = {
                                str(c.get("chart_id") or "")
                                for c in chart_blocks
                            }
                            for mid in marker_ids:
                                if not mid or mid in seen_ids or store is None:
                                    continue
                                try:
                                    payload = _load_chart_artifact(store, mid)  # type: ignore[misc]
                                except Exception:
                                    payload = None
                                    _LOG.debug(
                                        "chart marker load failed",
                                        exc_info=True,
                                    )
                                if not isinstance(payload, dict):
                                    continue
                                marker_block = {
                                    "kind": "chart",
                                    "version": "v1",
                                    "chart_id": mid,
                                    "chart_kind": payload.get("chart_kind") or "line",
                                    "title": payload.get("title") or mid,
                                    "series": [
                                        {
                                            "name": s.get("name") or "series",
                                            "type": s.get("type") or "line",
                                            "data_uri": f"nerya://chart/{mid}#series/{s.get('name') or 'series'}",
                                        }
                                        for s in (payload.get("series") or [])
                                    ],
                                    "source": payload.get("source")
                                    or {
                                        "skill": "agent",
                                        "action": "publish",
                                        "as_of": payload.get("as_of") or "",
                                    },
                                    "path": "bulk",
                                    "bulk_data_uri": f"nerya://chart/{mid}",
                                }
                                chart_blocks.append(marker_block)
                                seen_ids.add(mid)
                        for cb in chart_blocks:
                            cb_call_id = str(block.get("call_id") or "")
                            captured_charts.append((cb_call_id, dict(cb)))
                            try:
                                bus.publish(
                                    "chart.block",
                                    chart_id=str(cb.get("chart_id") or ""),
                                    chart_block=dict(cb),
                                    call_id=cb_call_id,
                                    skill_id=str(block.get("skill_id") or "native"),
                                    action=str(block.get("action") or ""),
                                    **common,
                                )
                            except Exception:
                                _LOG.debug("chart.block publish failed", exc_info=True)
                    # surface approval requests as their
                    # own event so the dashboard can render an
                    # "approval pending" pill instead of just a
                    # ``tool.complete`` carrying ``error_kind=
                    # permission_pending``. Same shape, dedicated
                    # channel: subscribers can listen for either.
                    if (
                        not bool(block.get("ok"))
                        and str(block.get("error_kind") or "") == "permission_pending"
                    ):
                        call_id = str(block.get("call_id") or "")
                        if call_id and not block.get("payload"):
                            block = {
                                **block,
                                "payload": tool_payloads.get(call_id) or {},
                            }
                        approval_payload = self._record_tool_permission_request(
                            turn_id=turn_id,
                            session_id=session_id,
                            strategy_id=strategy_id,
                            block=block,
                        )
                        bus.publish(
                            "approval.request",
                            **approval_payload,
                            tool_call_id=str(block.get("call_id") or ""),
                            call_id=str(block.get("call_id") or ""),
                            skill_id=str(block.get("skill_id") or "native"),
                            action=str(block.get("action") or ""),
                            reason=block.get("error"),
                            **common,
                        )
                        captured_approvals.append((
                            call_id,
                            {
                                "kind": "approval_request",
                                "approval_id": str(approval_payload.get("approval_id") or ""),
                                "call_id": call_id,
                                "skill_id": str(block.get("skill_id") or "native"),
                                "action": str(block.get("action") or ""),
                                "prompt": approval_payload.get("prompt"),
                                "record": approval_payload.get("record"),
                                "reason": block.get("error"),
                            },
                        ))
                elif kind == "system":
                    sub_kind = str(block.get("kind_detail") or block.get("event_kind") or "")
                    if sub_kind in {"compact.start", "compact.complete"}:
                        bus.publish(
                            sub_kind,
                            **{
                                k: v
                                for k, v in block.items()
                                if k not in {"kind", "kind_detail", "event_kind"}
                            },
                            **common,
                        )
            except Exception:
                _LOG.debug("event_sink publish failed", exc_info=True)

        loop = WorkspaceNativeAgentLoop(
            gateway=gw,
            registry=self._registry,
            orchestrator=orchestrator,
            config=loop_config,
            event_sink=_event_sink,
        )

        user_payload = trigger.get("payload") or {}
        user_text = (
            user_payload.get("text")
            or user_payload.get("message")
            or user_payload.get("prompt")
            or trigger.get("raw")
            or trigger.get("text")
            or "(no user message provided)"
        )
        if not isinstance(user_text, str):
            user_text = str(user_text)
        approval_continue = _is_approval_continue_trigger(trigger, user_payload)
        model_user_text = _model_user_text_for_trigger(user_text, trigger)

        # Replay prior user/assistant exchanges before rendering the
        # system prompt so approval continuations inherit the interrupted
        # task's market/scope/language instead of the control message.
        prior_messages: list[dict[str, Any]] = []
        if session_existed and session_id:
            try:
                prior_messages = self._load_prior_chat_messages(
                    session_id=session_id,
                    exclude_turn_id=None if approval_continue else turn_id,
                    include_interrupted_resume_context=approval_continue,
                )
            except Exception:
                _LOG.debug(
                    "prior chat history load failed", exc_info=True,
                )
        system_user_text = (
            _latest_prior_user_text(prior_messages)
            if approval_continue
            else user_text
        )
        system_prompt = self._build_system_prompt(
            deps,
            attached_skills=attached_skills,
            strategy_id=strategy_id,
            session_id=session_id,
            user_text=system_user_text,
        )
        effective_provider, _effective_model, effective_meta = gw.effective_model_metadata(
            self.llm_tier or self.config.get("agent.native.tier"),
            provider_override=self.model_provider,
            model_override=self.model_id,
        )
        prepared_attachments = prepare_user_message(
            model_user_text,
            user_payload.get("attachments"),
            paths=self.config.paths,
            turn_id=turn_id,
            provider=effective_provider,
            model_metadata=effective_meta,
        )
        user_message = prepared_attachments.message
        user_attachment_meta = prepared_attachments.attachments

        # Persist the actual prompt text (truncated) so subsequent
        # turns in the same session can replay the conversation back
        # into the loop transcript instead of starting from a blank
        # slate every time.
        _USER_TEXT_JOURNAL_CAP = 16_000
        if session_id and not approval_continue:
            self._record_session_db_message(
                session_id=session_id,
                strategy_id=strategy_id,
                turn_id=turn_id,
                role="user",
                content=user_text[:_USER_TEXT_JOURNAL_CAP],
                source=str(
                    user_payload.get("channel")
                    or trigger.get("source")
                    or trigger.get("kind")
                    or ""
                ),
            )
        jsonl.append(
            self.config.paths.journal("agent"),
            {
                "kind": "agent.turn.start",
                "trigger_event_id": trigger_event_id,
                "turn_id": turn_id,
                "strategy_id": strategy_id,
                "session_id": session_id,
                "user_text_len": len(user_text),
                "user_text": user_text[:_USER_TEXT_JOURNAL_CAP],
                "user_text_truncated": len(user_text) > _USER_TEXT_JOURNAL_CAP,
                "attachments": user_attachment_meta,
            },
        )

        # Pull the MCP per-session cache. Each chat handler
        # builds a fresh ``AgentKernel`` + ``ToolRegistry`` +
        # ``LazyMcpState``, so without this lookup the model would
        # have to re-call ``mcp_describe`` on every turn just to see
        # the same yahoo / edgar / coingecko tools it described one
        # turn earlier. ``pull_session_cache_into`` is a no-op when
        # ``session_id`` is falsy or the registry has no lazy state
        # attached (e.g. MCP connectors disabled).
        try:
            from ..mcp.lazy import (
                LazyMcpState as _LazyMcpState,
                pull_session_cache_into as _pull_session_cache_into,
            )
            _live_state = getattr(self._registry, "lazy_mcp_state", None)
            if isinstance(_live_state, _LazyMcpState):
                _promoted = _pull_session_cache_into(
                    _live_state,
                    workspace_root=self.config.paths.root,
                    session_id=session_id,
                )
                if _promoted:
                    _LOG.debug(
                        "phase_o pull: promoted %d namespaces from "
                        "session cache for session_id=%s",
                        _promoted, session_id,
                    )
        except Exception:
            _LOG.debug("phase_o pull failed", exc_info=True)

        try:
            outcome: LoopOutcome = loop.run(
                system=system_prompt,
                user_message=user_message,
                prior_messages=prior_messages or None,
                cancel_token=cancel_token,
            )
        finally:
            if _unsubscribe_activity_events is not None:
                try:
                    _unsubscribe_activity_events()
                except Exception:
                    pass

        # Push the MCP per-session cache. Mirror anything the
        # loop newly described or cached back to the long-lived
        # session-scoped state so the next turn's pull can see it.
        try:
            from ..mcp.lazy import (
                LazyMcpState as _LazyMcpState,
                push_state_into_session_cache as _push_state_into_session_cache,
            )
            _live_state = getattr(self._registry, "lazy_mcp_state", None)
            if isinstance(_live_state, _LazyMcpState):
                _persisted = _push_state_into_session_cache(
                    _live_state,
                    workspace_root=self.config.paths.root,
                    session_id=session_id,
                )
                if _persisted:
                    _LOG.debug(
                        "phase_o push: persisted %d namespaces to "
                        "session cache for session_id=%s",
                        _persisted, session_id,
                    )
        except Exception:
            _LOG.debug("phase_o push failed", exc_info=True)

        # Persist any permission_pending approvals as native blocks so
        # the dashboard's ``msg.turn.blocks`` view (post-turn, after
        # reload, in re-imported sessions) keeps showing the approval
        # card. Without this the card is only present in the in-memory
        # event bus and vanishes the moment the chat re-renders from
        # ``turn.blocks``.
        if captured_approvals:
            self._splice_approval_blocks(outcome, captured_approvals, turn_id)

        # Splice captured chart blocks alongside their tool_results so
        # the chat (post-turn, after reload) keeps showing the K-line
        # the agent generated. Same shape as the approval splice — we
        # just mirror that pattern keyed on ``call_id``.
        if captured_charts:
            self._splice_chart_blocks(outcome, captured_charts, turn_id)

        actions, tool_trace = self._project_blocks(outcome)

        if outcome.final_text:
            actions.append(
                {
                    "action": "send_message",
                    "skill_id": "native",
                    "ok": True,
                    "text": outcome.final_text,
                    "result": {"text": outcome.final_text},
                }
            )

        _FINAL_TEXT_JOURNAL_CAP = 16_000
        _final_text = outcome.final_text or ""
        jsonl.append(
            self.config.paths.journal("agent"),
            {
                "kind": "agent.turn.end",
                "trigger_event_id": trigger_event_id,
                "turn_id": turn_id,
                "strategy_id": strategy_id,
                "session_id": session_id,
                "iterations": outcome.iterations,
                "tool_calls": outcome.tool_calls,
                "stop_reason": outcome.stop_reason,
                "aborted": outcome.aborted,
                "abort_reason": outcome.abort_reason or None,
                "final_text_len": len(_final_text),
                "final_text": _final_text[:_FINAL_TEXT_JOURNAL_CAP],
                "final_text_truncated": len(_final_text) > _FINAL_TEXT_JOURNAL_CAP,
            },
        )

        try:
            bus.publish(
                "turn.complete",
                turn_id=turn_id,
                trigger_event_id=trigger_event_id,
                session_id=session_id,
                strategy_id=strategy_id,
                stop_reason=outcome.stop_reason,
                iterations=outcome.iterations,
                tool_calls=outcome.tool_calls,
                final_text=outcome.final_text,
                harness="native",
            )
        except Exception:
            _LOG.debug("turn.complete publish failed", exc_info=True)

        block_dicts = [env.as_dict() for env in outcome.blocks]
        output_attachments = public_attachment_blocks_from_envelopes(block_dicts)
        all_attachments = [*user_attachment_meta, *output_attachments]
        if session_id:
            compact_activity_events = _compact_turn_activity_events(captured_activity_events)
            self._record_session_db_turn(
                session_id=session_id,
                strategy_id=strategy_id,
                turn_id=turn_id,
                user_text=user_text,
                final_text=outcome.final_text or "",
                blocks=block_dicts,
                actions=actions,
                tool_trace=tool_trace,
                activity_events=compact_activity_events,
                iterations=outcome.iterations,
                tool_calls_count=outcome.tool_calls,
                stop_reason=outcome.stop_reason,
                aborted=outcome.aborted,
                abort_reason=outcome.abort_reason or None,
                error_count=outcome.error_count,
            )
            self._maybe_auto_title_session(
                session_id=session_id,
                strategy_id=strategy_id,
                user_text=user_text,
                final_text=outcome.final_text or "",
            )
        # Verifier nudge: compare pre-turn vs post-turn todo state and
        # the tool calls that happened in between. When the model
        # marked >= threshold todos done without running any test /
        # verify tool / re-read, drop a one-line note into
        # ``memory/global.md`` so the next turn's recall block picks
        # it up. We don't *force* the model to act on it — just make
        # sure it's visible.
        try:
            self._fire_verifier_nudge(
                turn_id=turn_id,
                strategy_id=strategy_id,
                blocks=block_dicts,
                todos_before=todos_before,
                todos_after=deps.task_state.snapshot_todos(),
            )
        except Exception:
            _LOG.debug("verifier nudge failed", exc_info=True)

        # autonomous artifact index. We collect what
        # actually changed on disk / what commands ran / what errors
        # came back so dashboards + CI gates have a structured record
        # independent of the model's narrative final_text. Failure
        # here is best-effort: a malformed block list shouldn't break
        # the turn return path.
        artifact_payload: dict[str, Any] = {}
        final_report_payload: dict[str, Any] = {}
        try:
            ai = build_artifact_index(block_dicts)
            artifact_payload = ai.asdict()
            final_report_payload = render_final_report(ai)
            jsonl.append(
                self.config.paths.journal("agent"),
                {
                    "kind": "agent.turn.summary",
                    "turn_id": turn_id,
                    "session_id": session_id,
                    "strategy_id": strategy_id,
                    "final_report": final_report_payload,
                    **artifact_payload,
                },
            )
            try:
                bus.publish(
                    "agent.turn.final_report",
                    turn_id=turn_id,
                    session_id=session_id,
                    strategy_id=strategy_id,
                    **final_report_payload,
                )
            except Exception:
                _LOG.debug("final_report publish failed", exc_info=True)
        except Exception:
            _LOG.debug("artifact index build failed", exc_info=True)

        return AgentTurnResult(
            trigger_event_id=trigger_event_id,
            strategy_id=strategy_id,
            session_id=session_id,
            turn_id=turn_id,
            decision={"action": "send_message", "text": outcome.final_text},
            actions=actions,
            tool_trace=tool_trace,
            budget={
                "iterations": outcome.iterations,
                "tool_calls": outcome.tool_calls,
                "errors": outcome.error_count,
                "aborted": outcome.aborted,
                "abort_reason": outcome.abort_reason or None,
            },
            steps=block_dicts,
            blocks=block_dicts,
            stopped_reason=outcome.stop_reason,
            final_text=outcome.final_text,
            iterations=outcome.iterations,
            harness="native",
            activity_events=_compact_turn_activity_events(captured_activity_events),
            artifact_index=artifact_payload,
            final_report=final_report_payload,
            attachments=all_attachments,
        )

    @staticmethod
    def _tool_permission_fingerprint(tool_name: str, payload: dict[str, Any]) -> str:
        try:
            body = json.dumps(
                {"tool": tool_name, "payload": payload or {}},
                sort_keys=True,
                ensure_ascii=False,
                default=str,
            )
        except Exception:
            body = f"{tool_name}:{payload}"
        return hashlib.sha256(body.encode("utf-8")).hexdigest()

    def _iter_approval_rows(self, path) -> list[dict[str, Any]]:
        if not path.exists():
            return []
        rows: list[dict[str, Any]] = []
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            return rows
        for line in text.splitlines():
            if not line.strip():
                continue
            try:
                row = json.loads(line)
            except Exception:
                continue
            if isinstance(row, dict):
                rows.append(row)
        return rows

    def _lookup_tool_permission_decision(
        self,
        *,
        session_id: str | None,
        tool_name: str,
        payload: dict[str, Any],
        call_id: str,
    ) -> bool | None:
        """Return a persisted operator verdict for this exact tool call.

        Approval cards are resolved out-of-band by the dashboard or a
        gateway. The next time the model retries the same tool with the
        same arguments, this callback lets the executor proceed without
        requiring an in-memory UI callback.
        """

        fp = self._tool_permission_fingerprint(tool_name, payload)

        def _session_matches(row: dict[str, Any]) -> bool:
            return not (
                session_id
                and row.get("session_id")
                and row.get("session_id") != session_id
            )

        def _tool_payload(row: dict[str, Any]) -> dict[str, Any]:
            payload = row.get("payload") if isinstance(row.get("payload"), dict) else {}
            tool = row.get("tool") if isinstance(row.get("tool"), dict) else {}
            payload_tool = payload.get("tool") if isinstance(payload.get("tool"), dict) else {}
            return {**payload_tool, **tool}

        def _item_matches(row: dict[str, Any]) -> bool:
            if not _session_matches(row):
                return False
            tool = _tool_payload(row)
            row_call_id = str(
                row.get("tool_use_id")
                or row.get("call_id")
                or tool.get("call_id")
                or ""
            )
            if call_id and row_call_id == call_id:
                return True
            if str(row.get("fingerprint") or "") == fp:
                return True
            return str(tool.get("fingerprint") or "") == fp

        def _matches(row: dict[str, Any]) -> bool:
            kind = str(row.get("kind") or "")
            if kind == "tool_permission":
                return _item_matches(row)
            if kind != "tool_permission_batch":
                return False
            if not _session_matches(row):
                return False
            fingerprints = row.get("fingerprints")
            if isinstance(fingerprints, list) and fp in {str(x) for x in fingerprints}:
                return True
            tool_use_ids = row.get("tool_use_ids")
            if (
                call_id
                and isinstance(tool_use_ids, list)
                and call_id in {str(x) for x in tool_use_ids}
            ):
                return True
            payload = row.get("payload") if isinstance(row.get("payload"), dict) else {}
            items = row.get("items") or payload.get("items") or []
            if not isinstance(items, list):
                return False
            return any(_item_matches(item) for item in items if isinstance(item, dict))

        paths = self.config.paths
        for row in self._iter_approval_rows(paths.approvals_rejected):
            if _matches(row):
                return False
        for row in self._iter_approval_rows(paths.approvals_approved):
            if _matches(row):
                return True
        return None

    def _record_session_db_message(
        self,
        *,
        session_id: str,
        strategy_id: str | None,
        turn_id: str,
        role: str,
        content: str,
        source: str = "",
    ) -> None:
        try:
            from ..db.repositories import AgentSessionRepository
            from ..db.sqlite import connect

            con = connect(self.config.paths.db)
            repo = AgentSessionRepository(con)
            repo.upsert_session(
                session_id=session_id,
                strategy_id=strategy_id,
                source=source,
                meta={"last_turn_id": turn_id},
            )
            repo.record_message(
                message_id=f"{turn_id}:{role}",
                session_id=session_id,
                turn_id=turn_id,
                role=role,
                content=content,
                meta={"source": source} if source else None,
            )
            con.close()
        except Exception:
            _LOG.debug("session db message record failed", exc_info=True)

    def _record_session_db_turn(
        self,
        *,
        session_id: str,
        strategy_id: str | None,
        turn_id: str,
        user_text: str,
        final_text: str,
        blocks: list[dict[str, Any]],
        actions: list[dict[str, Any]] | None = None,
        tool_trace: list[dict[str, Any]] | None = None,
        activity_events: list[dict[str, Any]] | None = None,
        iterations: int | None = None,
        tool_calls_count: int | None = None,
        stop_reason: str | None = None,
        aborted: bool | None = None,
        abort_reason: str | None = None,
        error_count: int | None = None,
    ) -> None:
        try:
            from ..db.repositories import AgentSessionRepository
            from ..db.sqlite import connect

            con = connect(self.config.paths.db)
            repo = AgentSessionRepository(con)
            repo.upsert_session(
                session_id=session_id,
                strategy_id=strategy_id,
                meta={"last_turn_id": turn_id},
            )
            if final_text.strip():
                # May-01 2026 — persist the full turn payload so the
                # dashboard can rehydrate imported sessions with the
                # original tool_use / tool_result stream. Without this
                # the transcript route only returns ``content`` and the
                # chat view degrades to the ``actions applied`` summary
                # after a refresh or visibilitychange-triggered
                # reimport. Cap each nested list at a conservative
                # budget so a pathological tool_result payload can't
                # blow up the meta_json cell.
                assistant_meta = {
                    "turn": _compact_turn_payload(
                        turn_id=turn_id,
                        blocks=blocks,
                        actions=actions or [],
                        tool_trace=tool_trace or [],
                        iterations=iterations,
                        tool_calls_count=tool_calls_count,
                        stop_reason=stop_reason,
                        aborted=aborted,
                        abort_reason=abort_reason,
                        error_count=error_count,
                        final_text=final_text,
                    ),
                }
                repo.record_message(
                    message_id=f"{turn_id}:assistant",
                    session_id=session_id,
                    turn_id=turn_id,
                    role="assistant",
                    content=final_text[:16_000],
                    meta=assistant_meta,
                )
            for i, env in enumerate(blocks or []):
                block = env.get("block") if isinstance(env.get("block"), dict) else env
                if not isinstance(block, dict):
                    continue
                kind = str(block.get("kind") or "")
                if kind not in {"tool_use", "tool_result"}:
                    continue
                call_id = str(block.get("call_id") or "")
                action = str(block.get("action") or "")
                skill = str(block.get("skill_id") or "native")
                repo.record_tool_event(
                    event_id=f"{turn_id}:{i}:{kind}:{call_id or action}",
                    session_id=session_id,
                    turn_id=turn_id,
                    call_id=call_id or None,
                    tool=f"{skill}.{action}" if action else skill,
                    phase=kind,
                    ok=(
                        bool(block.get("ok"))
                        if kind == "tool_result"
                        else None
                    ),
                    payload={
                        k: block.get(k)
                        for k in (
                            "payload",
                            "result",
                            "error",
                            "error_kind",
                            "elapsed_ms",
                        )
                        if k in block
                    },
                )
            for i, event in enumerate(activity_events or []):
                if not isinstance(event, dict):
                    continue
                kind = str(event.get("kind") or "").strip()
                if kind not in _TURN_ACTIVITY_EVENT_KINDS:
                    continue
                call_id = str(
                    event.get("team_call_id")
                    or event.get("tool_call_id")
                    or event.get("call_id")
                    or event.get("team_run_id")
                    or ""
                )
                event_id = str(event.get("event_id") or event.get("seq") or i)
                repo.record_tool_event(
                    event_id=f"{turn_id}:activity:{i}:{kind}:{event_id}",
                    session_id=session_id,
                    turn_id=turn_id,
                    call_id=call_id or None,
                    tool="native.agent_activity",
                    phase=kind,
                    ok=(
                        bool(event.get("ok"))
                        if "ok" in event and event.get("ok") is not None
                        else None
                    ),
                    payload={"event": event},
                    ts=(
                        float(event.get("ts"))
                        if isinstance(event.get("ts"), (int, float))
                        else None
                    ),
                )
            con.close()
        except Exception:
            _LOG.debug("session db turn record failed", exc_info=True)

    def _fallback_session_title(self, text: str) -> str:
        clean = re.sub(r"\s+", " ", (text or "").strip())
        clean = clean.strip(" #`\"'")
        if not clean:
            return "Nerya session"
        return clean[:48].rstrip() or "Nerya session"

    def _maybe_auto_title_session(
        self,
        *,
        session_id: str,
        strategy_id: str | None,
        user_text: str,
        final_text: str,
    ) -> None:
        try:
            state = self._sessions.load(session_id)
            meta = dict(state.meta if state else {})
            current = str(meta.get("title") or "").strip()
            if current and meta.get("title_source") != "fallback":
                return
            if state and len(state.turn_ids) > 1 and current:
                return
            prompt = (
                "Return JSON only: {\"title\":\"...\"}.\n"
                "Create a short chat title, 3 to 8 words, no quotes, no punctuation at the end.\n"
                f"User: {user_text[:1200]}\n"
                f"Assistant: {final_text[:1200]}"
            )
            title = ""
            source = "light"
            try:
                call = LLMGateway(self.config).call(
                    task="auto_session_title",
                    caller="agent:session_title",
                    tier="light",
                    prompt=prompt,
                    schema={
                        "type": "object",
                        "required": ["title"],
                        "properties": {"title": {"type": "string"}},
                    },
                )
                parsed = call.parsed if isinstance(call.parsed, dict) else {}
                title = str(parsed.get("title") or "").strip()
            except Exception:
                source = "fallback"
            if not title:
                title = self._fallback_session_title(user_text)
                source = "fallback"
            title = re.sub(r"\s+", " ", title).strip(" #`\"'.")
            if len(title) > 60:
                title = title[:57].rstrip() + "..."
            if not title:
                return
            self._sessions.update_meta(
                session_id,
                {
                    "title": title,
                    "title_source": source,
                    "title_updated_at": now_iso(),
                },
                strategy_id=strategy_id,
            )
            try:
                from ..db.repositories import AgentSessionRepository
                from ..db.sqlite import connect

                con = connect(self.config.paths.db)
                AgentSessionRepository(con).set_title(session_id, title)
                con.close()
            except Exception:
                pass
        except Exception:
            _LOG.debug("auto session title failed", exc_info=True)

    def _record_tool_permission_request(
        self,
        *,
        turn_id: str,
        session_id: str | None,
        strategy_id: str | None,
        block: dict[str, Any],
    ) -> dict[str, Any]:
        """Persist a tool permission prompt in the shared approval queue.

        Multiple permission-pending tool calls in the same agent turn are
        represented by one batch approval. The dashboard can then show one
        chronological card, while the persisted verdict still resolves each
        individual tool call by call id or fingerprint on the continuation
        turn.
        """

        call_id = str(block.get("call_id") or block.get("tool_use_id") or "")
        safe_call_id = re.sub(r"[^A-Za-z0-9_.-]+", "_", call_id)[:80]
        safe_turn_id = re.sub(r"[^A-Za-z0-9_.-]+", "_", turn_id or "")[:80]
        aid = f"tool_batch_{safe_turn_id or safe_call_id or 'pending'}"
        paths = self.config.paths

        reason = str(block.get("error") or "approval required before this tool can run")
        action = str(block.get("action") or "")
        skill_id = str(block.get("skill_id") or "native")
        tool_payload = dict(block.get("payload") or {})
        fingerprint = self._tool_permission_fingerprint(action, tool_payload)
        item_payload = {
            "tool": {
                "name": action,
                "skill_id": skill_id,
                "call_id": call_id,
                "fingerprint": fingerprint,
            },
            "risk": {
                "reasons": [reason],
            },
            "arguments": tool_payload,
        }
        item = {
            "approval_id": f"tool_{safe_call_id or fingerprint[:12]}",
            "id": f"tool_{safe_call_id or fingerprint[:12]}",
            "kind": "tool_permission",
            "state": "pending",
            "turn_id": turn_id,
            "session_id": session_id,
            "strategy_id": strategy_id,
            "tool_use_id": call_id,
            "tool": item_payload["tool"],
            "reason": reason,
            "fingerprint": fingerprint,
            "payload": item_payload,
        }

        def _item_matches(row: dict[str, Any]) -> bool:
            payload = row.get("payload") if isinstance(row.get("payload"), dict) else {}
            tool = row.get("tool") if isinstance(row.get("tool"), dict) else {}
            payload_tool = payload.get("tool") if isinstance(payload.get("tool"), dict) else {}
            merged_tool = {**payload_tool, **tool}
            return bool(
                (call_id and str(row.get("tool_use_id") or merged_tool.get("call_id") or "") == call_id)
                or str(row.get("fingerprint") or merged_tool.get("fingerprint") or "") == fingerprint
            )

        def _row_has_item(row: dict[str, Any]) -> bool:
            kind = str(row.get("kind") or "")
            if kind == "tool_permission":
                return _item_matches(row)
            if kind != "tool_permission_batch":
                return False
            if call_id and call_id in {str(x) for x in row.get("tool_use_ids") or []}:
                return True
            if fingerprint in {str(x) for x in row.get("fingerprints") or []}:
                return True
            payload = row.get("payload") if isinstance(row.get("payload"), dict) else {}
            items = row.get("items") or payload.get("items") or []
            return any(_item_matches(x) for x in items if isinstance(x, dict))

        def _existing_terminal(path) -> dict[str, Any] | None:
            if not path.exists():
                return None
            try:
                for line in path.read_text(encoding="utf-8").splitlines():
                    if not line.strip():
                        continue
                    import json as _json

                    rec = _json.loads(line)
                    if (
                        rec.get("approval_id") == aid
                        or rec.get("id") == aid
                        or _row_has_item(rec)
                    ):
                        return rec
            except Exception:
                return None
            return None

        def _merge_record(record: dict[str, Any]) -> dict[str, Any]:
            existing_items = record.get("items")
            payload = record.get("payload") if isinstance(record.get("payload"), dict) else {}
            if not isinstance(existing_items, list):
                existing_items = payload.get("items") if isinstance(payload.get("items"), list) else []
            items = [x for x in existing_items if isinstance(x, dict)]
            if not any(_item_matches(x) for x in items):
                items.append(item)
            def _tool_for(x: dict[str, Any]) -> dict[str, Any]:
                tool = x.get("tool")
                return tool if isinstance(tool, dict) else {}

            reasons: list[str] = []
            for x in items:
                text = str(x.get("reason") or "")
                if text and text not in reasons:
                    reasons.append(text)
            tool_use_ids = [
                str(x.get("tool_use_id") or _tool_for(x).get("call_id") or "")
                for x in items
            ]
            tool_use_ids = [x for x in dict.fromkeys(tool_use_ids) if x]
            fingerprints = [
                str(x.get("fingerprint") or _tool_for(x).get("fingerprint") or "")
                for x in items
            ]
            fingerprints = [x for x in dict.fromkeys(fingerprints) if x]
            first_tool = items[0].get("tool") if items and isinstance(items[0].get("tool"), dict) else {}
            record = {
                **record,
                "approval_id": aid,
                "id": aid,
                "kind": "tool_permission_batch",
                "state": str(record.get("state") or "pending"),
                "updated_at": time.time(),
                "updated_at_iso": now_iso(),
                "turn_id": turn_id,
                "session_id": session_id,
                "strategy_id": strategy_id,
                "tool_use_ids": tool_use_ids,
                "fingerprints": fingerprints,
                "tool": first_tool,
                "reason": (
                    f"{len(items)} tool calls require permission"
                    if len(items) != 1
                    else reasons[0] if reasons else reason
                ),
                "items": items,
                "payload": {
                    "kind": "tool_permission_batch",
                    "items": items,
                    "risk": {"reasons": reasons},
                },
            }
            record.setdefault("created_at", time.time())
            record.setdefault("created_at_iso", now_iso())
            return record

        terminal = (
            _existing_terminal(paths.approvals_approved)
            or _existing_terminal(paths.approvals_rejected)
        )
        if terminal is not None:
            record = terminal
        else:
            import json as _json

            record: dict[str, Any] | None = None
            pending = paths.approvals_pending
            pending.parent.mkdir(parents=True, exist_ok=True)
            lines = pending.read_text(encoding="utf-8").splitlines() if pending.exists() else []
            out_lines: list[str] = []
            for line in lines:
                if not line.strip():
                    continue
                try:
                    rec = _json.loads(line)
                except Exception:
                    out_lines.append(line)
                    continue
                if (
                    record is None
                    and (
                        rec.get("approval_id") == aid
                        or rec.get("id") == aid
                        or _row_has_item(rec)
                    )
                ):
                    record = _merge_record(rec)
                    out_lines.append(_json.dumps(record, ensure_ascii=False, default=str))
                    continue
                out_lines.append(line)
            if record is None:
                record = _merge_record({
                    "approval_id": aid,
                    "id": aid,
                    "kind": "tool_permission_batch",
                    "state": "pending",
                    "created_at": time.time(),
                    "created_at_iso": now_iso(),
                    "turn_id": turn_id,
                    "session_id": session_id,
                    "strategy_id": strategy_id,
                    "items": [],
                })
                out_lines.append(_json.dumps(record, ensure_ascii=False, default=str))
            pending.write_text(
                "\n".join(out_lines) + ("\n" if out_lines else ""),
                encoding="utf-8",
            )
            try:
                from ..trading.approval import _broadcast_approval

                _broadcast_approval(self.config, record)
            except Exception:
                pass
        try:
            from ..messaging.approval_prompts import build_prompt

            prompt = build_prompt(record).as_dict()
        except Exception:
            prompt = {"approval_id": aid, "text": reason, "buttons": []}
        return {
            "approval_id": aid,
            "record": record,
            "prompt": prompt,
        }

    @staticmethod
    def _splice_approval_blocks(
        outcome: LoopOutcome,
        captured: list[tuple[str, dict[str, Any]]],
        turn_id: str,
    ) -> None:
        """Insert ``approval_request`` block envelopes into the outcome.

        Each captured entry pairs a ``call_id`` with the block payload
        the dashboard's ``ApprovalRequestCard`` expects. We splice the
        envelope right after the matching ``tool_result`` so the chat
        renders the card adjacent to the call that triggered it; if the
        tool_result can't be located we append at the end as a fallback.
        """

        if not captured or not outcome.blocks:
            return

        # Avoid duplicating an envelope when the loop is re-entered for
        # the same turn (defensive — current loop builds outcome.blocks
        # fresh each run).
        existing_ids = {
            str((env.block or {}).get("approval_id") or "")
            for env in outcome.blocks
            if (env.block or {}).get("kind") == "approval_request"
        }

        message_id = outcome.blocks[-1].message_id if outcome.blocks else turn_id
        next_seq = max((env.seq for env in outcome.blocks), default=0) + 1

        for call_id, block in captured:
            approval_id = str(block.get("approval_id") or "")
            if approval_id and approval_id in existing_ids:
                continue
            envelope = BlockEnvelope(
                seq=next_seq,
                turn_id=turn_id,
                message_id=message_id,
                role="tool",
                block=dict(block),
            )
            next_seq += 1
            insert_at: int | None = None
            for idx in range(len(outcome.blocks) - 1, -1, -1):
                candidate = outcome.blocks[idx].block or {}
                if (
                    candidate.get("kind") == "tool_result"
                    and str(candidate.get("call_id") or "") == call_id
                ):
                    insert_at = idx + 1
                    break
            if insert_at is None:
                outcome.blocks.append(envelope)
            else:
                outcome.blocks.insert(insert_at, envelope)
            if approval_id:
                existing_ids.add(approval_id)

    @staticmethod
    def _splice_chart_blocks(
        outcome: LoopOutcome,
        captured: list[tuple[str, dict[str, Any]]],
        turn_id: str,
    ) -> None:
        """Insert ``chart`` block envelopes into the outcome.

        For each captured ``(call_id, chart_block_dict)`` we emit a
        :class:`BlockEnvelope` whose ``block`` is the chart payload
        plus a back-reference to the originating ``call_id``. We try
        to insert immediately after the matching ``tool_result`` so
        the chat renders the K-line right where the user expects it
        (next to the ``markets.get_candles`` call); if that anchor is
        missing we append at the end as a degraded but non-fatal
        fallback.

        Idempotent on ``chart_id``: re-entering the splice for the
        same turn won't duplicate envelopes — useful when the loop is
        retried after a transient failure.
        """

        if not captured or outcome.blocks is None:
            return

        existing_ids: set[str] = set()
        for env in outcome.blocks:
            block = env.block or {}
            if block.get("kind") == "chart":
                cid = str(block.get("chart_id") or "")
                if cid:
                    existing_ids.add(cid)

        message_id = outcome.blocks[-1].message_id if outcome.blocks else turn_id
        next_seq = max((env.seq for env in outcome.blocks), default=0) + 1

        for call_id, chart_block in captured:
            chart_id = str(chart_block.get("chart_id") or "")
            if chart_id and chart_id in existing_ids:
                continue
            block_payload: dict[str, Any] = dict(chart_block)
            block_payload["kind"] = "chart"
            if call_id:
                block_payload.setdefault("call_id", call_id)
            envelope = BlockEnvelope(
                seq=next_seq,
                turn_id=turn_id,
                message_id=message_id,
                role="tool",
                block=block_payload,
            )
            next_seq += 1
            insert_at: int | None = None
            for idx in range(len(outcome.blocks) - 1, -1, -1):
                candidate = outcome.blocks[idx].block or {}
                if (
                    candidate.get("kind") == "tool_result"
                    and str(candidate.get("call_id") or "") == call_id
                ):
                    # Walk forward past any chart envelopes already
                    # spliced for the same call so the order matches
                    # capture order (chart 1, chart 2, ...) instead of
                    # being reversed by stacking inserts at the same
                    # anchor.
                    insert_at = idx + 1
                    while (
                        insert_at < len(outcome.blocks)
                        and (outcome.blocks[insert_at].block or {}).get("kind") == "chart"
                        and str((outcome.blocks[insert_at].block or {}).get("call_id") or "")
                        == call_id
                    ):
                        insert_at += 1
                    break
            if insert_at is None:
                outcome.blocks.append(envelope)
            else:
                outcome.blocks.insert(insert_at, envelope)
            if chart_id:
                existing_ids.add(chart_id)

    # ----------------------------------------------------------- helpers

    @staticmethod
    def _project_blocks(
        outcome: LoopOutcome,
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        """Project block envelopes onto legacy ``actions`` / ``tool_trace``.

        Old API consumers branch on ``actions[*].action`` and read
        ``tool_trace[*].ok`` — this projection keeps those code paths
        working while the canonical transcript lives in
        :attr:`AgentTurnResult.blocks`.
        """

        actions: list[dict[str, Any]] = []
        tool_trace: list[dict[str, Any]] = []
        payload_by_call_id: dict[str, Any] = {}
        for env in outcome.blocks:
            block = env.block or {}
            kind = block.get("kind")
            if kind == "tool_use":
                call_id = block.get("call_id")
                if call_id:
                    payload_by_call_id[str(call_id)] = block.get("payload") or {}
                actions.append(
                    {
                        "action": block.get("action"),
                        "skill_id": block.get("skill_id") or "native",
                        "payload": block.get("payload") or {},
                        "call_id": block.get("call_id"),
                    }
                )
            elif kind == "tool_result":
                call_id = block.get("call_id")
                tool_trace.append(
                    {
                        "call_id": call_id,
                        "skill_id": block.get("skill_id") or "native",
                        "action": block.get("action"),
                        "payload": payload_by_call_id.get(str(call_id), {}),
                        "ok": bool(block.get("ok")),
                        "result": block.get("result"),
                        "error": block.get("error"),
                        "error_kind": block.get("error_kind"),
                        "elapsed_ms": block.get("elapsed_ms") or 0,
                    }
                )
        return actions, tool_trace

    def _render_recipe_block(self, *, max_chars: int = 800) -> str:
        """Render a compact 'recipes you can run' block.

        Recipes are operator-curated runbooks ("write a monitoring
        script", "schedule a portfolio heartbeat", …). We surface only
        the ones whose ``required_skills`` are satisfied by the
        currently installed skill kernel — otherwise the agent gets
        encouraged to pick a recipe it can't actually finish.

        Output is bounded by ``max_chars`` so the system prompt stays
        lean; rendered shape is intentionally similar to
        ``deps.skill_index.render_for_prompt()``.
        """

        try:
            from .recipes import all_recipes, is_available
        except Exception:
            return ""

        skill_ids: set[str] = set()
        action_ids: set[str] = set()
        try:
            entries = list(self.skills.registry.list())
        except Exception:
            entries = []
        for entry in entries:
            manifest = getattr(entry, "manifest", None)
            if manifest is None:
                continue
            sid = getattr(manifest, "id", "")
            if sid:
                skill_ids.add(sid)
            actions = getattr(manifest, "actions", {}) or {}
            for name in actions.keys():
                action_ids.add(f"{sid}.{name}")
        sf = frozenset(skill_ids)
        af = frozenset(action_ids)

        recipes = [
            r for r in all_recipes(self.config.paths)
            if is_available(r, sf, af)
        ]
        if not recipes:
            return ""

        lines = [
            "Recipes (operator-curated runbooks you can offer; "
            "use recipe_list / recipe_view for the full body):"
        ]
        used = len(lines[0]) + 1
        for r in recipes:
            row = f"- {r.id} — {r.title}: {r.body}"
            if used + len(row) + 1 > max_chars:
                lines.append(
                    "- … (more recipes available; call recipe_list "
                    "for the full list, recipe_view <id> for the body + prompt)"
                )
                break
            lines.append(row)
            used += len(row) + 1
        return "\n".join(lines)

    # ------------------------------------------------------- after-turn hooks

    def _after_turn_evolve(self, *, turn_id: str, result: AgentTurnResult) -> None:
        """Best-effort end-of-turn auto-evolution tick.

        :func:`maybe_propose_from_turn` only fires when a clear signal
        is in the journal (e.g. ≥9/10 consecutive no-ops); it never
        mutates live config and always emits a *proposal* the operator
        must approve. We swallow exceptions here because a journal-write
        flake on this hook must not poison a successful turn.
        """

        if not bool(self.config.get("agent.native.evolve_after_turn", True)):
            return
        try:
            top_action = (
                (result.actions[0].get("action") if result.actions else None)
                or "noop"
            )
            maybe_propose_from_turn(
                self.config,
                turn=({
                    "turn_id": turn_id,
                    "action": top_action,
                    "stop_reason": result.stopped_reason,
                    "iterations": result.iterations,
                }),
            )
        except Exception:
            _LOG.debug("after-turn evolve hook failed", exc_info=True)

    def _load_prior_chat_messages(
        self,
        *,
        session_id: str,
        exclude_turn_id: Optional[str] = None,
        max_pairs: int = 12,
        per_msg_cap: int = 12_000,
        include_interrupted_resume_context: bool = False,
    ) -> list[dict[str, Any]]:
        """Reconstruct prior user/assistant exchanges for a chat session.

        Walks the agent journal and pairs every ``agent.turn.start``
        with the matching ``agent.turn.end`` for the same ``turn_id``
        and ``session_id``. Returns the most recent ``max_pairs`` pairs
        as a flat ``[{role, content}, ...]`` list ready to seed a
        :class:`WorkspaceNativeAgentLoop` transcript.

        Skips ``exclude_turn_id`` (typically the in-flight turn whose
        own ``turn.start`` is already in the journal but whose
        ``turn.end`` has not been written yet).
        """
        try:
            from ..db.repositories import AgentSessionRepository
            from ..db.sqlite import connect

            con = connect(self.config.paths.db)
            repo = AgentSessionRepository(con)
            session_row = repo.get_session(session_id) or {}
            session_meta = self._parse_json_dict(session_row.get("meta_json"))
            session_compact_enabled = bool(
                self.config.get("agent.native.session_autocompact_enabled", True)
            )
            if session_compact_enabled:
                rows = repo.transcript(session_id, limit=0)
            else:
                rows = repo.transcript(
                    session_id,
                    limit=max(2, max_pairs * 2 + 4),
                )
            tool_events_by_turn: dict[str, list[dict[str, Any]]] = {}
            if include_interrupted_resume_context:
                turn_ids = [
                    str(row.get("turn_id") or "").strip()
                    for row in rows
                    if str(row.get("turn_id") or "").strip()
                ]
            else:
                turn_ids = []
            if turn_ids:
                try:
                    for event in repo.tool_events(session_id, turn_ids=set(turn_ids)):
                        tid = str(event.get("turn_id") or "").strip()
                        if tid:
                            tool_events_by_turn.setdefault(tid, []).append(event)
                except Exception:
                    _LOG.debug("db tool-event history load failed", exc_info=True)
            rows = _filter_failed_history_rows(
                rows,
                tool_events_by_turn=tool_events_by_turn,
                preserve_approval_pauses=include_interrupted_resume_context,
            )
            if session_compact_enabled:
                policy = SessionCompactionPolicy(
                    keep_recent_pairs=int(
                        self.config.get(
                            "agent.native.session_compact_keep_recent_pairs",
                            max_pairs,
                        )
                    ),
                    trigger_pairs=int(
                        self.config.get(
                            "agent.native.session_compact_trigger_pairs",
                            max_pairs,
                        )
                    ),
                    per_message_chars=int(
                        self.config.get(
                            "agent.native.session_compact_per_message_chars",
                            per_msg_cap,
                        )
                    ),
                    max_bullets_per_section=int(
                        self.config.get(
                            "agent.native.session_compact_max_bullets",
                            12,
                        )
                    ),
                    max_render_chars=int(
                        self.config.get(
                            "agent.native.session_compact_max_render_chars",
                            18_000,
                        )
                    ),
                )
                compacted = compact_session_history(
                    rows,
                    existing_checkpoint=checkpoint_from_session_meta(session_meta),
                    policy=policy,
                    exclude_turn_id=exclude_turn_id,
                )
                if compacted.checkpoint is not None:
                    existing_checkpoint = checkpoint_from_session_meta(session_meta)
                    if compacted.checkpoint != existing_checkpoint:
                        session_meta[SESSION_COMPACTION_META_KEY] = compacted.checkpoint
                        repo.update_session_meta(session_id, session_meta)
                con.close()
                if compacted.messages:
                    return compacted.messages
                return []
            con.close()
            out: list[dict[str, Any]] = []
            for row in rows:
                if exclude_turn_id and str(row.get("turn_id") or "") == str(exclude_turn_id):
                    continue
                role = row.get("role")
                content = row.get("content")
                if role not in ("user", "assistant"):
                    continue
                if isinstance(content, str) and content.strip():
                    out.append({"role": role, "content": content[:per_msg_cap]})
            if out:
                return out[-max_pairs * 2:] if max_pairs > 0 else out
        except Exception:
            _LOG.debug("db prior chat history load failed", exc_info=True)

        journal = self.config.paths.journal("agent")
        if not journal.exists():
            return []
        starts: dict[str, str] = {}
        ends: dict[str, str] = {}
        skipped_turn_ids: set[str] = set()
        order: list[str] = []
        for row in jsonl.read_all(journal):
            if not isinstance(row, dict):
                continue
            if str(row.get("session_id") or "") != session_id:
                continue
            tid = row.get("turn_id")
            if not tid:
                continue
            tid = str(tid)
            if exclude_turn_id and tid == str(exclude_turn_id):
                continue
            kind = row.get("kind")
            if kind == "agent.turn.start":
                user_text = row.get("user_text")
                if isinstance(user_text, str) and user_text:
                    starts[tid] = user_text[:per_msg_cap]
                    if tid not in order:
                        order.append(tid)
            elif kind == "agent.turn.end":
                final_text = row.get("final_text")
                if _assistant_history_turn_failed(
                    row,
                    final_text if isinstance(final_text, str) else "",
                ):
                    skipped_turn_ids.add(tid)
                    continue
                if isinstance(final_text, str) and final_text:
                    ends[tid] = final_text[:per_msg_cap]
        # keep journal order (chronological) but only the last N pairs
        ordered = [t for t in order if t in starts and t not in skipped_turn_ids]
        if max_pairs > 0 and len(ordered) > max_pairs:
            ordered = ordered[-max_pairs:]
        out: list[dict[str, Any]] = []
        for tid in ordered:
            out.append({"role": "user", "content": starts[tid]})
            assistant = ends.get(tid)
            if assistant:
                out.append({"role": "assistant", "content": assistant})
        return out

    @staticmethod
    def _parse_json_dict(raw: Any) -> dict[str, Any]:
        if isinstance(raw, dict):
            return dict(raw)
        if not isinstance(raw, str) or not raw.strip():
            return {}
        try:
            parsed = json.loads(raw)
        except Exception:
            return {}
        return dict(parsed) if isinstance(parsed, dict) else {}

    def _after_turn_memory(
        self,
        *,
        turn_id: str,
        result: AgentTurnResult,
        strategy_id: Optional[str],
    ) -> None:
        """Optional: append a one-line summary of the turn to memory.

        Disabled by default — the agent already has
        :func:`memory_remember` for explicit writes, and writing every
        turn would bloat ``memory/global.md`` and crowd out the durable
        lessons we actually want to keep. Operators turn this on for
        long-horizon reasoning workflows where the next turn's
        ``memory_recall`` block needs to know what the previous one
        concluded.
        """

        if not bool(self.config.get("agent.native.memory_write_on_turn", False)):
            return
        text = (result.final_text or "").strip()
        if not text:
            return
        try:
            preview = text.splitlines()[0][:200]
            note = (
                f"turn={turn_id} action={result.actions[0].get('action') if result.actions else 'noop'}"
                f" stopped={result.stopped_reason} :: {preview}"
            )
            if strategy_id:
                self._evolution_hooks.on_memory_write(
                    target=f"strategy:{strategy_id}",
                    content=note,
                    source="after_turn_memory",
                    evidence_refs=[f"turn:{turn_id}"],
                    strategy_id=strategy_id,
                )
                self.memory.append_strategy_learning(strategy_id, note)
            else:
                self._evolution_hooks.on_memory_write(
                    target="global.md",
                    content=note,
                    source="after_turn_memory",
                    evidence_refs=[f"turn:{turn_id}"],
                )
                self.memory.append_global("global.md", note)
        except Exception:
            _LOG.debug("after-turn memory hook failed", exc_info=True)

    def _fire_verifier_nudge(
        self,
        *,
        turn_id: str,
        strategy_id: Optional[str],
        blocks: list[dict[str, Any]],
        todos_before: list[dict[str, Any]],
        todos_after: list[dict[str, Any]],
    ) -> None:
        """Emit a verifier nudge if many todos completed without verification.

        Off by default — operators turn it on per workspace via
        ``agent.native.verifier_nudge_enabled``. When enabled the
        kernel checks the heuristic and, if triggered, both journals
        the event *and* writes a short note to ``memory/global.md``
        so the **next** turn's system prompt sees it (the memory
        block is rendered fresh on every turn).
        """

        if not bool(self.config.get("agent.native.verifier_nudge_enabled", True)):
            return
        threshold = int(
            self.config.get("agent.native.verifier_nudge_threshold", 3)
        )
        nudge = compute_verifier_nudge(
            blocks=blocks,
            todos_before=todos_before,
            todos_after=todos_after,
            threshold=threshold,
        )
        if not nudge.triggered:
            return
        try:
            jsonl.append(
                self.config.paths.journal("agent"),
                {
                    "kind": "agent.verifier.nudge",
                    "turn_id": turn_id,
                    "strategy_id": strategy_id,
                    **nudge.asdict(),
                },
            )
        except Exception:
            pass
        try:
            self.memory.append_global("global.md", nudge.message)
        except Exception:
            _LOG.debug("verifier nudge memory write failed", exc_info=True)

    def _maybe_compact_memory(self) -> None:
        """Periodic memory compaction tick.

        Drives :meth:`Memory.compact_all` every ``every_n`` turns
        (default 50) so ``memory/global.md`` and friends don't grow
        forever. The compaction itself is TTL-based — sections whose
        ``## <timestamp>`` header is older than ``max_age_days`` are
        dropped, everything else (including handwritten notes without
        a parseable timestamp) survives.

        Disabled when ``agent.native.memory_compact_every_n_turns``
        is 0 or unset, so workspaces that don't want auto-compaction
        keep the full history.
        """

        every_n = int(
            self.config.get("agent.native.memory_compact_every_n_turns", 0)
        )
        if every_n <= 0:
            return
        if (self._turn_count % every_n) != 0:
            return
        max_age_days = float(
            self.config.get("agent.native.memory_max_age_days", 30.0)
        )
        try:
            report = self.memory.compact_all(max_age_days=max_age_days)
        except Exception:
            _LOG.debug("memory compaction tick failed", exc_info=True)
            return
        if any(report.values()):
            try:
                jsonl.append(
                    self.config.paths.journal("agent"),
                    {
                        "kind": "agent.memory.compacted",
                        "turn_count": self._turn_count,
                        "max_age_days": max_age_days,
                        "report": report,
                    },
                )
            except Exception:
                pass

    def on_session_end(
        self,
        *,
        session_id: str,
        reason: str = "session_end",
    ) -> dict[str, Any]:
        """End-of-session lifecycle hook.

        End-of-session cleanup:

        * fires the registry-level ``after_session`` hook so operator
          extensions can run cleanup,
        * compacts the global memory files (TTL pass) so a long chat
          doesn't snowball ``global.md`` indefinitely,
        * appends an ``agent.session.end`` record to the journal so
          the dashboard can show "session closed at ..." with the
          per-skill invocation counts the session collected.

        Safe to call multiple times — each component swallows its own
        exceptions so a missing file or unloaded subagent doesn't
        block the rest of the cleanup.
        """

        report: dict[str, Any] = {"session_id": session_id, "reason": reason}
        try:
            state = self._sessions.load(session_id)
        except Exception:
            state = None
        if state is not None:
            report["turn_ids"] = list(state.turn_ids)
            report["invoked_skills"] = list(state.invoked_skills)
            report["last_action"] = state.last_action

        # 1. Memory compaction — guarded by the same knobs as the
        #    periodic tick. Always do at least the global pass so
        #    closing a long session is the natural moment to GC.
        try:
            max_age_days = float(
                self.config.get("agent.native.memory_max_age_days", 30.0)
            )
            report["compacted"] = self.memory.compact_all(max_age_days=max_age_days)
        except Exception as exc:
            report["compact_error"] = repr(exc)

        # 2. Lifecycle hook — operators can register extra cleanup
        #    (flush caches, ack queues, …) without touching the kernel.
        try:
            self._hooks.fire(
                "after_session",
                HookContext(
                    phase="after_session",
                    turn_id="",
                    trigger_event_id=None,
                    strategy_id=(state.strategy_id if state else None),
                    session_id=session_id,
                    data={"reason": reason, "report": report},
                ),
            )
        except Exception as exc:
            report["hook_error"] = repr(exc)

        # 3. Journal — single record so the dashboard can render
        #    "session closed (3 skills invoked, 12 turns)" without
        #    tailing the entire session file.
        try:
            jsonl.append(
                self.config.paths.journal("agent"),
                {
                    "kind": "agent.session.end",
                    "session_id": session_id,
                    "reason": reason,
                    "turns": len(report.get("turn_ids", []) or []),
                    "invoked_skills": list(report.get("invoked_skills", []) or []),
                },
            )
        except Exception:
            pass
        try:
            self._evolution_hooks.on_session_end(
                session_id=session_id,
                report=report,
            )
        except Exception:
            _LOG.debug("evolution session hook failed", exc_info=True)
        return report

    def _ensure_registry(self) -> NativeToolDeps:
        if self._deps is not None:
            return self._deps
        skill_roots = self._skill_roots()
        deps = build_native_tool_deps(
            workspace_root=Path(self.config.paths.root),
            skill_roots=skill_roots,
            file_state=FileStateCache(),
            paths=self.config.paths,
            config=self.config,
            skills=self.skills,
        )
        register_native_tools(self._registry, deps)
        # Bootstrap any operator-declared MCP connectors so
        # external server tools (sec_edgar, yahoo_finance, coingecko,
        # …) land on the same ToolRegistry the agent loop drives.
        # Default-off so existing tests / operators don't pay the
        # subprocess + network cost without explicit opt-in.
        self._maybe_attach_mcp_connectors()
        self._deps = deps
        return deps

    def _maybe_attach_mcp_connectors(self) -> None:
        """Attach MCP connectors when ``mcp.connectors.enabled`` is true.

        Defaults to False so the kernel keeps booting cleanly in any
        environment (including offline CI). Operators flip it on via:

        .. code-block:: yaml

            # ~/.nerya/<profile>/nerya.yml
            mcp:
              connectors:
                enabled: true
                auto_seed: true        # default; writes the 17-server stub
                                       # on first boot if the file is missing

        Failures here are *non-fatal* — they go to the debug log and the
        kernel continues with native tools only. Per-server failures
        already get swallowed inside ``bootstrap_mcp_connectors`` and
        surfaced as ``BootstrapDiagnostics.failures()`` rows.
        """

        try:
            cfg_data = (getattr(self.config, "data", None) or {}) or {}
            mcp_cfg = (cfg_data.get("mcp") or {}) if isinstance(cfg_data, dict) else {}
            connectors_cfg = (mcp_cfg.get("connectors") or {}) if isinstance(mcp_cfg, dict) else {}
        except Exception:
            connectors_cfg = {}

        if not bool(connectors_cfg.get("enabled", False)):
            return

        try:
            from ..mcp.connectors import bootstrap_mcp_connectors
        except Exception:
            _LOG.debug("MCP connectors module not importable; skipping attach")
            return

        try:
            diagnostics = bootstrap_mcp_connectors(
                paths=self.config.paths,
                registry=self._registry,
                executor=None,  # post-tool hook is per-executor; per-turn
                                # executors share this registry, so MCP tools
                                # are visible regardless. Hook installation
                                # belongs to a separate Phase if needed.
                resource_index=None,
                vault_passphrase=connectors_cfg.get("vault_passphrase"),
                auto_seed=bool(connectors_cfg.get("auto_seed", True)),
            )
        except Exception:
            _LOG.exception("MCP connector bootstrap failed; continuing without MCP")
            return

        success = len(diagnostics.successes())
        failed = len(diagnostics.failures())
        skipped = diagnostics.total_declared - diagnostics.total_enabled
        _LOG.info(
            "mcp connectors: %d attached, %d failed, %d disabled-by-config",
            success, failed, skipped,
        )

    def _skill_roots(self) -> list[Path]:
        roots: list[Path] = []
        try:
            installed = Path(self.config.paths.skills_installed)
            if installed.exists():
                roots.append(installed)
        except Exception:
            pass
        try:
            from .. import skills as _skills_pkg

            builtin = Path(_skills_pkg.__file__).parent / "builtin"
            if builtin.exists():
                roots.append(builtin)
        except Exception:
            pass
        return roots

    def _build_system_prompt(
        self,
        deps: NativeToolDeps,
        *,
        attached_skills: Optional[list[str]] = None,
        strategy_id: Optional[str] = None,
        session_id: Optional[str] = None,
        user_text: Optional[str] = None,
    ) -> str:
        """Render the workspace-native system prompt.

        Build a compact system prompt from a one-paragraph charter, the
        workspace root, a skill listing, and a memory recap block (see
        :func:`nerya.tools.native.memory.build_system_prompt_block`).
        Tool docs come from the provider tool list rendered by the
        loop, not the system prompt — that's how Anthropic / OpenAI /
        Gemini surface tool metadata in the modern API.
        """

        try:
            skill_block = deps.skill_index.render_for_prompt()
        except Exception:
            skill_block = ""

        memory_block = ""
        if deps.paths is not None:
            try:
                from ..tools.native.memory import build_system_prompt_block

                memory_block = build_system_prompt_block(
                    deps.paths,
                    config=self.config,
                    session_id=session_id,
                    strategy_id=strategy_id,
                    max_chars=int(
                        self.config.get("agent.native.memory_block_chars", 1200)
                    ),
                )
            except Exception:
                memory_block = ""

        profile_block = ""
        if deps.paths is not None and session_id:
            try:
                from .session_profile import (
                    load_strategy_agent_profile,
                    render_strategy_agent_profile_block,
                )

                profile_block = render_strategy_agent_profile_block(
                    load_strategy_agent_profile(deps.paths, session_id)
                )
            except Exception:
                profile_block = ""

        market_context_block = ""
        if session_id:
            try:
                market_context_block = render_session_market_context_block(
                    load_session_market_context(self._sessions, session_id)
                )
            except Exception:
                market_context_block = ""

        # Recipe digest — restored from the legacy planner. Recipes are
        # short, vetted "if you see X, run Y" runbooks the operator
        # ships in ``workspace/recipes/`` (or via builtin packs). They
        # complement the skill index by surfacing higher-level *named
        # workflows* that often combine multiple skills + tools.
        recipe_block = ""
        if bool(self.config.get("agent.native.expose_recipes", True)):
            try:
                recipe_block = self._render_recipe_block(max_chars=int(
                    self.config.get("agent.native.recipe_block_chars", 800)
                ))
            except Exception:
                recipe_block = ""

        sections: list[str] = []
        sections.append(
            "You are Nerya, an autonomous coding + trading agent. You have"
            " native tools for filesystem, search, shell, planning,"
            " memory, sub-agents, self-evolution, skill discovery,"
            " connector / venue discovery (connector_list,"
            " connector_view), trading (portfolio_summary, risk_check,"
            " kill_switch_set, trade_intent_submit, strategy_list /"
            " strategy_view / strategy_history), strategy package"
            " lifecycle (strategy_generate_proposal / strategy_validate /"
            " strategy_backtest / strategy_promote / strategy_run_tick),"
            " and LLM delegation"
            " (llm_complete / llm_classify / llm_extract_json /"
            " llm_compress for cheap classification + schema-bound"
            " extraction). Prefer the smallest tool that gets the job"
            " done; read files before editing them; run short commands"
            " before long ones; dry-run trade intents through risk_check"
            " before trade_intent_submit."
        )

        sections.append(f"Workspace root: {deps.workspace_root}")
        sections.append(_render_temporal_context_block())
        sections.append(
            _render_turn_focus_block(
                user_text=user_text,
                attached_skills=attached_skills,
            )
        )
        sections.append(_render_output_language_block(user_text))
        if attached_skills:
            sections.append(
                "Attached skills (preferred for this turn): "
                + ", ".join(attached_skills)
            )
        if memory_block:
            sections.append(memory_block)
        if profile_block:
            sections.append(profile_block)
        if market_context_block:
            sections.append(market_context_block)
        if recipe_block:
            sections.append(recipe_block)
        if skill_block:
            sections.append(skill_block)
        sections.append(
            "Workflow:\n"
            "1. Plan in your head. For multi-step work, use todo_write"
            " to show progress and continue. Use enter_plan_mode +"
            " exit_plan_mode only when the user explicitly asks for plan"
            " approval or before high-risk actions such as destructive"
            " deletes, sensitive config changes, strategy promotion,"
            " kill-switch changes, or live trading.\n"
            "2. Read before edit. edit_file refuses stale reads.\n"
            "3. Use memory_recall before re-deriving things you've"
            " already learned; use memory_remember sparingly for durable"
            " lessons. If you discover a reusable multi-step workflow,"
            " or the same correction/process recurs, use"
            " evolve_skill_proposal to draft a lazy-loaded SKILL.md"
            " proposal instead of putting procedure into memory.\n"
            "4. If the user asks for Agent Team, team, committee,"
            " multi-role, or deep-research collaboration, call role_list"
            " then team_run so the UI shows one coordinated team. Use"
            " subagent_run only for a single bounded child task;"
            " live-trading writes stay on the parent. For public-company"
            " stock research or 研报 requests, prefer default"
            " investment-research roles such as fundamentals_analyst,"
            " technical_analyst, sentiment_analyst, bull_researcher,"
            " bear_researcher, risk_critic, and research_manager; do"
            " not pick finance-ops workspace roles like"
            " valuation_reviewer unless the user asks for fund/GP"
            " valuation or accounting workflows. team_run is"
            " synchronous: when it returns, synthesize the final report"
            " from results/failures now. For prompts like"
            " `启动AgentTeam分析<company>` or stock 研报 requests, the"
            " expected output is the complete research report in the"
            " same reply, not a launch/status acknowledgement or a"
            " follow-up question asking whether the user wants details;"
            " include the concrete subject identifiers in team_run"
            " shared_payload / role payloads (for example"
            " asset_class='public_equity', company='NVIDIA',"
            " ticker='NVDA', venue='yahoo') so child roles do not infer"
            " unrelated default markets;"
            " do not say the team is still running, call task_get,"
            " task_output, task_list, or run team_run again for the"
            " same task unless you used an async subagent tool. If"
            " required roles fail or return degraded"
            " output, retry the missing analysis or state the evidence"
            " gap instead of claiming a complete report.\n"
            "5. Trading discipline: portfolio_summary / strategy_history"
            " before forming an opinion; risk_check before"
            " trade_intent_submit; kill_switch_set is DANGEROUS and"
            " requires operator confirmation.\n"
            "6. NEVER claim a venue / data source is missing without"
            " calling connector_list (and connector_view, when you"
            " want to inspect the existing implementation) first. If"
            " the venue is in the registry (binance, bybit, okx,"
            " hyperliquid, polymarket, bsc, evm, solana, mock, ccxt:*"
            " …) wire the strategy /"
            " script to it directly via the existing Connector"
            " subclass — do not re-author it. If it really is missing,"
            " follow the coding skill's extending-nerya.md playbook to"
            " write a *real* Connector under workspace/providers/"
            "<id>/provider.py exposing a top-level SPEC:"
            " ExchangeProviderSpec, then run scripts/reload_subsystem.py"
            " providers and confirm with connector_list. This"
            " workspace track is the default for agent-authored"
            " adapters because nerya/connectors/<vendor>.py is"
            " outside the agent's writable workspace root in any"
            " normal session — the kernel will refuse the write."
            " Mock data, one-shot scripts, and"
            " nerya/markets/*_provider.py throwaways are forbidden."
            " See skill `coding` references/extending-nerya.md.\n"
            "7. Strategy authoring: when the user asks to create,"
            " implement, or backtest a strategy, load/use"
            " `strategy_author`, draft real package files in context,"
            " and do not stop to ask for missing market, timeframe,"
            " account, schedule, or risk parameters when the request is a"
            " low-risk proposal/paper/backtest workflow. In that case,"
            " choose conservative defaults yourself: inherit session market"
            " context when present, otherwise use connector discovery and"
            " pick a liquid market with real historical candles; use a"
            " paper account and non-live execution mode; document the"
            " assumptions in the proposal rationale. Ask the operator only"
            " when the missing choice would make the action live,"
            " destructive, irreversible, or honestly impossible."
            " For ordinary or generic strategy prompts, do not override"
            " files.main.py; let strategy_generate_proposal use its stock"
            " StrategyContext-compatible template. Only provide inline file"
            " overrides when the user asks for behavior that the stock"
            " archetype cannot express. Requests involving custom data,"
            " provider-specific reads, chain-native/wallet evidence,"
            " non-standard replay rules, or an explicit rejection of a"
            " stock archetype are custom strategy-authoring tasks: draft"
            " package-rooted SDK files yourself using StrategyContext,"
            " StrategyResult, and when needed StrategyAgentTask, then use"
            " strategy_generate_proposal only as the proposal packager."
            " Call strategy_generate_proposal with inline files, then"
            " strategy_validate, then strategy_backtest using proposal_id"
            " before any promotion. A successful strategy_generate_proposal"
            " response is not terminal when it returns backtest_required or"
            " next_required_action; immediately run the indicated"
            " strategy_backtest unless the operator explicitly skips it."
            " If the generated/validated proposal contradicts the"
            " requested strategy class or an excluded stock archetype,"
            " repair it in the same turn by supplying corrected SDK files;"
            " do not end with a promise to repair it later."
            " Use returned proposal_paths / strategy_root / metrics_path"
            " for reads; do not probe promoted strategy paths before"
            " promotion. If the operator asks for an on-chain or DEX"
            " strategy, do not satisfy that request with CEX symbols such"
            " as binance:DOGEUSDT unless the operator explicitly accepts"
            " a non-on-chain proxy. On-chain means an on-chain/DEX data"
            " source with durable historical replay data; if that is not"
            " available, stop and report the gap instead of silently"
            " substituting CEX candles. If real OHLCV/history is"
            " unavailable or coverage_ok is false for Polymarket,"
            " on-chain meme coins,"
            " news, or other hard markets, stop probing after bounded"
            " evidence and report the coverage/data-source gap. For"
            " meme/on-chain strategies, use a custom/event replay when"
            " durable wallet, DEX, holder, top-trader, or swap history"
            " exists; otherwise promotion/live progression requires an"
            " explicit operator-approved standard-backtest waiver and"
            " must not be described as a passed standard backtest. Do not"
            " inspect connector source files or docs repeatedly just to"
            " keep trying; connector_list, connector_view, data_api"
            " provider catalogs/schemas, and one bounded"
            " market_data/backtest attempt are enough evidence. Once a"
            " provider route is visible, stop discovery and write the SDK"
            " proposal. Do not call shell, glob, or raw file reads to"
            " enumerate local connector source or workspace files just to"
            " learn data-source names. Do not"
            " fabricate synthetic/random/placeholder replay data. When"
            " summarising backtests, copy metrics_display/operator_summary"
            " values exactly. Raw *_pct values are already percentage"
            " points; never multiply them by 100 or move the decimal.\n"
            "8. After every batch of tool calls, summarise progress for"
            " the user in plain text. End the turn with a clear final"
            " text answer."
        )
        return "\n\n".join(sections)


__all__ = ["AgentKernel", "AgentTurnResult"]
