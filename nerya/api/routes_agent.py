from __future__ import annotations

import copy
import json
import re
import threading
import traceback
from contextlib import contextmanager
from typing import Any

from ..agent.attachments import upload_chat_attachments
from ..agent.history_mutations import deleted_session_ids, has_message_history, is_session_deleted


def _history_session_deleted(paths, sid) -> bool:
    from ..db.sqlite import connect
    con = connect(paths.db)
    try:
        return is_session_deleted(con, str(sid))
    finally:
        con.close()

from ..agent.kernel import AgentKernel, AgentTurnResult
from ..agent.loop_state import (
    TurnCheckpointResumeError,
    validate_turn_checkpoint_resume_request,
)
from ..agent.recovery import list_open_turns, load_turn_state
from ..agent.session import (
    SessionStore,
    db_session_asdict,
    file_session_asdict,
    hydrate_db_session_counts,
    merge_session_dict,
    session_updated_ts,
)
from ..core import jsonl
from ..observability.trace import build_trace, explain_trace
from .gateway_commands import CommandContext, DEFAULT_REGISTRY
from .gateway_events import turn_events


_RUN_TURN_LOCK_GUARD = threading.RLock()
_RUN_TURN_LOCKS: dict[str, threading.Lock] = {}
def _run_turn_lock_key(client: Any, session_id: str | None) -> str:
    sid = str(session_id or "").strip()
    if not sid:
        return ""
    try:
        root = str(client.config.paths.root.resolve())
    except Exception:
        root = ""
    return f"{root}:{sid}"


@contextmanager
def _claim_run_turn_session(client: Any, session_id: str | None):
    key = _run_turn_lock_key(client, session_id)
    if not key:
        yield True
        return
    with _RUN_TURN_LOCK_GUARD:
        lock = _RUN_TURN_LOCKS.get(key)
        if lock is None:
            lock = threading.Lock()
            _RUN_TURN_LOCKS[key] = lock
    acquired = lock.acquire(blocking=False)
    try:
        yield acquired
    finally:
        if acquired:
            lock.release()
            with _RUN_TURN_LOCK_GUARD:
                if not lock.locked():
                    _RUN_TURN_LOCKS.pop(key, None)


def _payload_number(payload: dict[str, Any], key: str, *, minimum: float, maximum: float) -> float | None:
    raw = payload.get(key)
    if raw is None:
        limits = payload.get("runtime_limits")
        if isinstance(limits, dict):
            raw = limits.get(key)
    if raw in (None, ""):
        return None
    try:
        value = float(raw)
    except Exception:
        return None
    import math
    if not math.isfinite(value):
        return None
    if not (minimum <= value <= maximum):
        value = max(minimum, min(maximum, value))
    return value


def _payload_context_window(payload: dict[str, Any]) -> int | None:
    raw = payload.get("model_context_window") or payload.get("context_window")
    if raw is None:
        limits = payload.get("runtime_limits")
        if isinstance(limits, dict):
            raw = limits.get("model_context_window") or limits.get("context_window")
    if raw in (None, ""):
        return None
    try:
        value = int(str(raw).strip().replace("_", ""))
    except Exception:
        return None
    if value <= 0:
        return None
    return min(16_777_216, value)


def _with_turn_limit_overrides(config, payload: dict[str, Any]):
    """Return a per-request config clone with safe agent-loop overrides."""

    if not isinstance(payload, dict):
        return config
    max_iterations = _payload_number(payload, "max_iterations", minimum=0, maximum=float("inf"))
    max_tool_calls = _payload_number(payload, "max_total_tool_calls", minimum=0, maximum=float("inf"))
    max_wall_seconds = _payload_number(payload, "max_wall_seconds", minimum=0, maximum=float("inf"))
    model_context_window = _payload_context_window(payload)
    if (
        max_iterations is None
        and max_tool_calls is None
        and max_wall_seconds is None
        and model_context_window is None
    ):
        return config

    data = copy.deepcopy(getattr(config, "data", {}) or {})
    native = data.setdefault("agent", {}).setdefault("native", {})
    if max_iterations is not None:
        native["max_iterations"] = int(max_iterations)
    if max_tool_calls is not None:
        native["max_total_tool_calls"] = int(max_tool_calls)
    if max_wall_seconds is not None:
        native["max_wall_seconds"] = float(max_wall_seconds)
    if model_context_window is not None:
        native["model_context_window"] = int(model_context_window)
    return config.__class__(paths=config.paths, data=data)


def _run_turn_user_text(payload: dict[str, Any]) -> str:
    if not isinstance(payload, dict):
        return ""
    body = payload.get("payload")
    if isinstance(body, dict):
        text = body.get("text") or body.get("content") or body.get("message")
        if isinstance(text, str):
            return text
    trigger = payload.get("trigger")
    if isinstance(trigger, dict):
        body = trigger.get("payload")
        if isinstance(body, dict):
            text = body.get("text") or body.get("content") or body.get("message")
            if isinstance(text, str):
                return text
    return ""


def _run_turn_command_response(client: Any, payload: dict[str, Any], user_text: str) -> dict[str, Any] | None:
    """Handle registered slash commands before they enter the LLM loop."""

    text = str(user_text or "").strip()
    if not text.startswith("/"):
        return None
    trigger = normalise_trigger_payload(payload)
    trigger_payload = trigger.get("payload") if isinstance(trigger.get("payload"), dict) else {}
    outcome = DEFAULT_REGISTRY.handle(
        text,
        CommandContext(
            client=client,
            platform=str(trigger_payload.get("platform") or trigger.get("source") or "dashboard"),
            chat_id=str(trigger_payload.get("chat_id") or payload.get("session_id") or "dashboard"),
            session_id=str(payload.get("session_id") or ""),
            raw_text=text,
        ),
    )
    if not outcome.handled:
        return None
    reply = outcome.reply_text
    result = AgentTurnResult(
        trigger_event_id=trigger.get("id") or trigger.get("event_id"),
        strategy_id=payload.get("strategy_id"),
        session_id=payload.get("session_id"),
        turn_id=str(payload.get("turn_id") or ""),
        decision={"action": "send_message", "text": reply, "command": outcome.command},
        actions=[{
            "action": "send_message",
            "payload": {"text": reply, "command": outcome.command},
        }],
        budget={
            "iterations": 0,
            "tool_calls": 0,
            "errors": 0,
            "aborted": False,
            "transition_reason": "slash_command",
        },
        stopped_reason="command",
        transition_reason="slash_command",
        final_text=reply,
        harness="command",
    )
    return {**result.asdict(), "command": outcome.command}


def _parse_meta_json(raw: Any) -> dict[str, Any]:
    try:
        meta = json.loads(str(raw or "{}"))
    except (TypeError, ValueError):
        return {}
    return meta if isinstance(meta, dict) else {}


def _truthy_query(value: Any) -> bool:
    return str(value or "").strip().lower() in {"1", "true", "yes", "on", "full"}


def _split_tool_name(tool: Any) -> tuple[str, str]:
    text = str(tool or "").strip()
    if "." in text:
        skill, action = text.split(".", 1)
        return skill or "native", action
    return "native", text


def _blocks_from_tool_events(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    blocks: list[dict[str, Any]] = []
    for row in events:
        phase = str(row.get("phase") or "").strip()
        if phase not in {"tool_use", "tool_result"}:
            continue
        payload = _parse_meta_json(row.get("payload_json"))
        skill_id, action = _split_tool_name(row.get("tool"))
        block: dict[str, Any] = {
            "kind": phase,
            "call_id": row.get("call_id") or "",
            "skill_id": skill_id,
            "action": action,
            "index": len(blocks),
        }
        if phase == "tool_use":
            block["payload"] = payload.get("payload") if isinstance(payload.get("payload"), dict) else {}
        else:
            ok = row.get("ok")
            block["ok"] = bool(ok) if ok is not None else not bool(payload.get("error"))
            for key in ("result", "error", "error_kind", "elapsed_ms"):
                if key in payload:
                    block[key] = payload.get(key)
        blocks.append(
            {
                "kind": phase,
                "block": block,
                "ts": row.get("ts"),
                "index": len(blocks),
            }
        )
    return blocks


_ACTIVITY_EVENT_PHASES = {
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


def _activity_events_from_tool_events(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for row in events:
        phase = str(row.get("phase") or "").strip()
        if phase not in _ACTIVITY_EVENT_PHASES:
            continue
        payload = _parse_meta_json(row.get("payload_json"))
        event = payload.get("event") if isinstance(payload.get("event"), dict) else payload
        if not isinstance(event, dict):
            continue
        item = dict(event)
        item.setdefault("kind", phase)
        item.setdefault("turn_id", row.get("turn_id"))
        item.setdefault("session_id", row.get("session_id"))
        item.setdefault("ts", row.get("ts"))
        out.append(item)
    return out


def _project_tool_trace_from_blocks(blocks: list[dict[str, Any]]) -> list[dict[str, Any]]:
    payload_by_call_id: dict[str, Any] = {}
    trace: list[dict[str, Any]] = []
    for env in blocks:
        block = env.get("block") if isinstance(env.get("block"), dict) else env
        if not isinstance(block, dict):
            continue
        kind = block.get("kind")
        call_id = str(block.get("call_id") or "")
        if kind == "tool_use":
            payload_by_call_id[call_id] = block.get("payload") or {}
        elif kind == "tool_result":
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
                    "elapsed_ms": block.get("elapsed_ms") or 0,
                }
            )
    return trace


_BACKTEST_LOCATOR_KEYS = (
    "strategy_id",
    "proposal_id",
    "backtest_ts",
    "out_dir",
    "backtest_dir",
    "metrics_path",
    "raw_metrics_file",
    "report_path",
    "chart_path",
    "equity_path",
    "trades_path",
    "result_path",
)


def _json_object_from_compacted_result(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if not isinstance(value, str):
        return {}
    text = value.strip()
    marker = "[compacted_kept]"
    if marker in text:
        text = text.split(marker, 1)[1].strip()
    if not text.startswith("{"):
        return {}
    try:
        parsed = json.loads(text)
    except Exception:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _backtest_locator_from_payload(payload: Any) -> dict[str, Any]:
    if not isinstance(payload, dict):
        return {}
    locator = {
        key: payload.get(key)
        for key in _BACKTEST_LOCATOR_KEYS
        if payload.get(key) not in (None, "", [])
    }
    if not locator.get("raw_metrics_file") and payload.get("metrics_path"):
        locator["raw_metrics_file"] = payload.get("metrics_path")
    return locator


def _augment_turn_backtest_locators(
    turn_payload: dict[str, Any] | None,
    *,
    raw_payload_by_ref: dict[str, Any] | None = None,
    client: Any | None = None,
) -> dict[str, Any] | None:
    if not isinstance(turn_payload, dict):
        return turn_payload
    raw_cache: dict[str, Any] = dict(raw_payload_by_ref or {})

    def raw_payload(ref: str) -> Any:
        if not ref:
            return None
        if ref in raw_cache:
            return raw_cache[ref]
        if client is None:
            return None
        try:
            from ..llm.tool_raw_store import open_store

            rec = open_store(client).read(ref)
        except Exception:
            rec = None
        raw_cache[ref] = rec.payload if rec is not None else None
        return raw_cache[ref]

    def locator_for(block: dict[str, Any]) -> dict[str, Any]:
        action = str(block.get("action") or "")
        compaction = block.get("compaction") if isinstance(block.get("compaction"), dict) else {}
        if "backtest" not in action and compaction.get("rule_id") != "backtest.report":
            return {}
        locator = _backtest_locator_from_payload(block)
        locator.update(_backtest_locator_from_payload(_json_object_from_compacted_result(block.get("result"))))
        if locator.get("strategy_id") and locator.get("backtest_ts"):
            return locator
        ref = str(compaction.get("raw_ref") or "").strip()
        locator.update(_backtest_locator_from_payload(raw_payload(ref)))
        return locator

    changed = False
    next_payload = dict(turn_payload)
    blocks = next_payload.get("blocks")
    if isinstance(blocks, list):
        next_blocks: list[Any] = []
        for env in blocks:
            if not isinstance(env, dict):
                next_blocks.append(env)
                continue
            block = env.get("block") if isinstance(env.get("block"), dict) else env
            if not isinstance(block, dict):
                next_blocks.append(env)
                continue
            locator = locator_for(block)
            if not locator:
                next_blocks.append(env)
                continue
            changed = True
            next_block = {**block, **locator}
            if env.get("block") is block:
                next_blocks.append({**env, "block": next_block})
            else:
                next_blocks.append(next_block)
        if changed:
            next_payload["blocks"] = next_blocks

    traces = next_payload.get("tool_trace")
    if isinstance(traces, list):
        next_traces: list[Any] = []
        for item in traces:
            if not isinstance(item, dict):
                next_traces.append(item)
                continue
            locator = locator_for(item)
            if locator:
                changed = True
                next_traces.append({**item, **locator})
            else:
                next_traces.append(item)
        if changed:
            next_payload["tool_trace"] = next_traces

    return next_payload if changed else turn_payload


def _rehydrate_turn_tool_events(
    turn_payload: dict[str, Any] | None,
    events: list[dict[str, Any]],
) -> dict[str, Any] | None:
    if not isinstance(turn_payload, dict) or not events:
        return turn_payload
    needs_blocks = not isinstance(turn_payload.get("blocks"), list) or not turn_payload.get("blocks")
    needs_trace = (
        not isinstance(turn_payload.get("tool_trace"), list)
        or not turn_payload.get("tool_trace")
    )
    needs_activity = (
        not isinstance(turn_payload.get("activity_events"), list)
        or not turn_payload.get("activity_events")
    )
    if not needs_blocks and not needs_trace and not needs_activity:
        return turn_payload
    next_payload = dict(turn_payload)
    blocks = _blocks_from_tool_events(events)
    if blocks and needs_blocks:
        next_payload["blocks"] = blocks
        next_payload["blocks_rehydrated"] = True
    if blocks and needs_trace:
        next_payload["tool_trace"] = _project_tool_trace_from_blocks(blocks)
        next_payload["tool_trace_rehydrated"] = True
    if needs_activity:
        activity_events = _activity_events_from_tool_events(events)
        if activity_events:
            next_payload["activity_events"] = activity_events
            next_payload["activity_events_rehydrated"] = True
    return next_payload


def normalise_trigger_payload(payload):
    """Accept both {trigger: {...}} and a bare trigger object.

    The dashboard posts the bare trigger shape. Older callers use the
    wrapper shape. Treat both as the same API contract.
    """
    if isinstance(payload.get("trigger"), dict):
        return payload["trigger"]
    keys = {"source", "kind", "target", "payload", "id", "event_id"}
    if any(k in payload for k in keys):
        return payload
    return {}


def _inject_trusted_actor(trigger: dict[str, Any], payload: dict[str, Any]) -> dict[str, Any]:
    """Override model-controlled actor fields with the HTTP auth identity."""

    actor_id = str(payload.get("_auth_actor_id") or "").strip()
    if not actor_id:
        return trigger
    stamped = dict(trigger or {})
    for key in ("_auth_actor_id", "_auth_scope", "_auth_scopes"):
        stamped.pop(key, None)
    trigger_payload = stamped.get("payload")
    trigger_payload = dict(trigger_payload) if isinstance(trigger_payload, dict) else {}
    for key in ("_auth_actor_id", "_auth_scope", "_auth_scopes"):
        trigger_payload.pop(key, None)
    trigger_payload["actor_id"] = actor_id
    stamped["actor_id"] = actor_id
    stamped["payload"] = trigger_payload
    return stamped


def agent_reply_text(result: AgentTurnResult) -> str:
    """Return the canonical workspace-native assistant response."""

    return result.final_text.strip()


def routes():
    def _normalise_reasoning_effort(value: object) -> str | None:
        from ..llm.messages import normalise_reasoning_effort
        raw = normalise_reasoning_effort(value)
        if raw in {"none", "minimal", "low", "medium", "high", "xhigh", "max"}:
            return raw
        return None

    def _normalise_llm_tier(value: object, client) -> str | None:
        if value is None:
            return None
        raw = str(value).strip()
        if not raw or raw.lower() in {"default", "auto"}:
            return None
        tiers = client.config.get("llm.tiers") or {}
        return raw if raw in tiers else None

    def _normalise_model_provider(value: object) -> str | None:
        if value is None:
            return None
        raw = str(value).strip().lower()
        if not raw or raw in {"default", "auto"}:
            return None
        if not re.match(r"^[a-z0-9_.-]{1,64}$", raw):
            return None
        return raw

    def _normalise_model_id(value: object) -> str | None:
        if value is None:
            return None
        raw = str(value).strip()
        if not raw or raw.lower() in {"default", "auto"}:
            return None
        return raw[:160]

    def run_turn(client, payload, *, enforce_public_gate: bool = True):
        payload = payload if isinstance(payload, dict) else {}
        from ..agent.assistants import AssistantError, prepare_request
        try:
            payload = prepare_request(client.config, payload)
        except AssistantError as exc:
            return {"ok": False, "error": exc.code, "_status": exc.status}
        # Permission mode resolution order (most → least specific):
        # explicit payload field, env override, default. Env override
        # ``NERYA_PERMISSION_MODE`` exists so headless / unattended runs
        # (eval harness, integration tests, dev sessions) can flip the
        # kernel into ``auto`` / ``yolo`` without round-tripping every
        # tool through the dashboard approval pane.
        import os as _os
        import time as _time
        from ..tools.permissions import PermissionMode as _PM
        _turn_started_at = _time.time()
        raw = (
            payload.get("permission_mode")
            or _os.environ.get("NERYA_PERMISSION_MODE")
            or client.config.get("runtime.permission_mode")
            or "default"
        )
        try:
            pmode = _PM(str(raw).strip().lower())
        except Exception:
            pmode = _PM.DEFAULT
        reasoning_effort = _normalise_reasoning_effort(
            payload.get("reasoning_effort")
        )
        reasoning_summary_raw = payload.get("reasoning_summary")
        reasoning_summary = (
            str(reasoning_summary_raw).strip().lower()
            if reasoning_summary_raw is not None
            else None
        )
        if reasoning_summary not in {"auto", "concise", "detailed"}:
            reasoning_summary = None
        llm_tier = _normalise_llm_tier(payload.get("model_tier"), client)
        model_provider = _normalise_model_provider(payload.get("model_provider"))
        model_id = _normalise_model_id(payload.get("model_id") or payload.get("model"))
        run_config = _with_turn_limit_overrides(client.config, payload)
        from ..agent.assistants import AssistantStore, assistant_config
        run_config = assistant_config(run_config, AssistantStore(run_config.paths).for_session(payload.get("session_id")), session_id=payload.get("session_id"))
        from copy import deepcopy
        from ..core.config import Config
        run_config = Config(paths=run_config.paths,data=deepcopy(run_config.data))
        mode = payload.get("work_mode", "execute")
        if payload.get("resume_turn_id") and payload.get("session_id"):
            from ..db.sqlite import connect as _plan_connect
            con=_plan_connect(client.config.paths.db)
            try:
                original=con.execute("SELECT request_json FROM agent_commands WHERE session_id=? AND turn_id=? ORDER BY created_at LIMIT 1",(payload["session_id"],payload["resume_turn_id"])).fetchone()
                if original:
                    import json as _plan_json
                    mode=_plan_json.loads(original[0]).get("work_mode","execute")
            finally:
                con.close()
        if mode not in ("execute", "plan", "goal"):
            return {"ok":False,"_status":400,"error":"invalid_work_mode"}
        run_config.data.setdefault("agent",{}).setdefault("native",{})["plan_only"] = mode == "plan"
        if mode == "plan" and not payload.get("resume_turn_id"):
            payload = deepcopy(payload)
            user_payload = payload.setdefault("payload",{})
            user_payload["text"] = str(user_payload.get("text") or "") + "\nWork mode: plan. Investigate with read-only tools, then use propose_plan. Do not modify external state or execute the plan."
        elif mode == "goal" and not payload.get("resume_turn_id"):
            payload = deepcopy(payload)
            user_payload = payload.setdefault("payload", {})
            user_payload["text"] = str(user_payload.get("text") or "") + (
                "\nWork mode: goal. Define concrete acceptance criteria, carry out the task, "
                "and verify the result with evidence. Keep iterating within this turn's limits "
                "while useful work remains. Preserve permission, approval, and proposal rules. "
                "If blocked, ask for the missing input. If the limits are reached or verification "
                "is incomplete, report the remaining work without claiming completion."
            )

        trigger = _inject_trusted_actor(
            normalise_trigger_payload(payload),
            payload,
        )
        requested_session_id = payload.get("session_id")
        if requested_session_id:
            from ..db.sqlite import connect as _wb_connect
            con=_wb_connect(client.config.paths.db)
            try:
                pending=con.execute("SELECT interaction_id,state,actor_id FROM agent_interactions WHERE session_id=? AND state IN ('pending','deferred','answered')",(requested_session_id,)).fetchall()
                if pending and (len(pending)!=1 or pending[0]["state"]!="answered" or pending[0]["interaction_id"]!=payload.get("interaction_id") or pending[0]["actor_id"]!=str(payload.get("_auth_actor_id") or "local")):
                    return {"ok":False,"_status":409,"error":"interaction_response_required"}
            finally:
                con.close()
        if requested_session_id and _history_session_deleted(client.config.paths, requested_session_id):
            return {"ok": False, "status": "error", "error": "session_deleted", "reason": "session_deleted"}
        trigger_payload = (
            trigger.get("payload")
            if isinstance(trigger.get("payload"), dict)
            else {}
        )
        try:
            resume_turn_id, continuation_feedback = (
                validate_turn_checkpoint_resume_request(
                    resume_turn_id=payload.get("resume_turn_id"),
                    continuation_feedback=payload.get("continuation_feedback"),
                    session_id=requested_session_id,
                    turn_id=payload.get("turn_id"),
                    has_attachments=bool(
                        payload.get("attachments")
                        or trigger_payload.get("attachments")
                    ),
                )
            )
        except TurnCheckpointResumeError as exc:
            response = exc.asdict()
            response["_status"] = response.pop("status")
            return response
        _user_text = (
            continuation_feedback
            if resume_turn_id
            else _run_turn_user_text(payload)
        )

        # Public research-only boundary. This must precede prompt-guard,
        # slash-command dispatch, and AgentKernel; every continuation is
        # classified again and missing text is isolated.
        _intent = None
        if enforce_public_gate and client.config.get("runtime.intent_gate.enabled", False):
            from ..agent.intent_gate import classify_request, extract_user_text as _intent_text
            _user_text = _intent_text(trigger) or _user_text
            if resume_turn_id and not str(_user_text or "").strip():
                _intent = {"status": "isolated", "allowed": False, "reason_code": "resume_missing_continuation"}
            else:
                _intent = classify_request(client, text=_user_text, source="POST /agent/run_turn")
            if not _intent.get("allowed"):
                return {"_status": 403, "ok": False, "error": "intent_gate_blocked", "isolated": True, "intent_gate": _intent, "message": "This public agent endpoint accepts research and market-analysis requests only. The request was isolated before execution."}

        # Auto-classify the incoming operator/user/channel text against the
        # prompt-guard policy. ``review`` and ``block`` verdicts auto-enqueue
        # into the review queue so the Action Inbox renders them; ``block``
        # short-circuits the turn so the LLM never sees the hostile prompt.
        _pg = None
        try:
            from ..agent.prompt_firewall import classify_user_input, extract_user_text
            _user_text = extract_user_text(trigger) or _user_text
            if _user_text:
                _channel = (
                    (trigger.get("payload") or {}).get("channel")
                    if isinstance(trigger.get("payload"), dict)
                    else None
                ) or trigger.get("source") or "chat"
                _pg = classify_user_input(
                    client,
                    text=_user_text,
                    source_route="POST /agent/run_turn",
                    source_channel=str(_channel),
                )
                if _pg.get("verdict") == "block" and _pg.get("flag_enabled"):
                    return {
                        "_status": 403,
                        "ok": False,
                        "error": "prompt_guard_blocked",
                        "blocked": True,
                        "prompt_guard": _pg,
                        "message": (
                            "I cannot fulfill this request. For security reasons, "
                            "this input was blocked by the prompt guard. Review the "
                            "matched patterns and resolve the queue item from the "
                            "Action Inbox."
                        ),
                    }
        except Exception:  # pragma: no cover - defensive, never block on guard error
            _pg = None
        command_response = (
            None
            if resume_turn_id
            else _run_turn_command_response(client, payload, _user_text)
        )
        if command_response is not None:
            return command_response
        with _claim_run_turn_session(client, requested_session_id) as claimed:
            if not claimed:
                return {
                    "_status": 409,
                    "ok": False,
                    "error": "session_turn_in_progress",
                    "session_id": requested_session_id,
                    "message": (
                        "Another agent turn is already running for this session. "
                        "Wait for it to finish or interrupt it before sending a new turn."
                    ),
                }
            # Persist the candidate reference server-side, after validating its
            # strategy ownership. Future turns cannot silently lose the reference.
            proposal_id = payload.get("strategy_proposal_id")
            if proposal_id:
                if not requested_session_id:
                    return {"ok": False, "error": "session_id required for a candidate reference", "_status": 400}
                from ..strategies.workflow_service import source_files
                from ..strategies.workflow_graph import WorkflowError
                try:
                    store = SessionStore(client.config.paths.root)
                    state = store.load(requested_session_id)
                    bound_strategy = (state.strategy_id if state else None) or payload.get("strategy_id")
                    if state and state.strategy_id and payload.get("strategy_id") not in (None, state.strategy_id):
                        raise ValueError("Strategy binding cannot change")
                    source_files(client.config.paths, str(bound_strategy or ""), str(proposal_id))
                    store.update_meta(requested_session_id, {"strategy_proposal_id": str(proposal_id)}, strategy_id=bound_strategy)
                except (ValueError, OSError, WorkflowError) as exc:
                    return {"ok": False, "error": str(exc), "_status": 400}
            kernel = AgentKernel(
                config=run_config,
                skills=client.skills,
                permission_mode=pmode,
                reasoning_effort=reasoning_effort,
                reasoning_summary=reasoning_summary,
                llm_tier=llm_tier,
                model_provider=model_provider,
                model_id=model_id,
            )
            try:
                from ..harness.cancellation import CancelToken

                cancel_token = CancelToken()
                result = kernel.run_turn(
                    trigger=trigger,
                    strategy_id=payload.get("strategy_id"),
                    session_id=requested_session_id,
                    turn_id=payload.get("turn_id"),
                    cancel_token=cancel_token,
                    evidence_contract=(
                        payload.get("evidence_contract")
                        if isinstance(payload.get("evidence_contract"), dict)
                        else None
                    ),
                    resume_turn_id=resume_turn_id or None,
                    continuation_feedback=continuation_feedback,
                )
            except TurnCheckpointResumeError as exc:
                response = exc.asdict()
                response["_status"] = response.pop("status")
                return response
            except Exception as exc:
                tb = traceback.format_exc()
                jsonl.append(client.config.paths.journal("errors"), {
                    "kind": "api.run_turn.error",
                    "trigger_event_id": trigger.get("id") or trigger.get("event_id"),
                    "error": f"{type(exc).__name__}: {exc}",
                    "trace": tb.splitlines()[-20:],
                })
                raise
        response = result.asdict(events=turn_events(result))
        # Continue editing the latest candidate produced in this bound chat.
        if requested_session_id and payload.get("strategy_id"):
            from ..strategies.workflow_service import source_files
            from ..strategies.workflow_graph import WorkflowError
            def candidate_refs(value, depth=0):
                if depth > 8:
                    return
                if isinstance(value, dict):
                    if value.get("proposal_id") and value.get("strategy_id") == payload["strategy_id"] and value.get("ok") is not False:
                        yield str(value["proposal_id"])
                    for key in ("result", "data", "output", "block", "blocks", "tool_trace", "actions", "content", "text"):
                        yield from candidate_refs(value.get(key), depth + 1)
                elif isinstance(value, list):
                    for item in value:
                        yield from candidate_refs(item, depth + 1)
                elif isinstance(value, str) and value.lstrip().startswith(("{", "[")):
                    try:
                        yield from candidate_refs(json.loads(value), depth + 1)
                    except (ValueError, TypeError):
                        pass
            for candidate in dict.fromkeys(candidate_refs(response)):
                try:
                    _, candidate_source = source_files(client.config.paths, payload["strategy_id"], candidate)
                    if candidate_source["state"] in {"draft", "pending_review", "proposed", "approved"}:
                        SessionStore(client.config.paths.root).update_meta(requested_session_id, {"strategy_proposal_id": candidate}, strategy_id=payload["strategy_id"])
                except (WorkflowError, OSError, ValueError):
                    continue
        # Surface the prompt-guard verdict on review (block already short-
        # circuited above). Operators see this in the dashboard turn detail.
        if _pg and _pg.get("verdict") in ("review", "block"):
            response["prompt_guard"] = _pg
        if _intent is not None:
            response["intent_gate"] = _intent

        # Operator profile self-learning capture — propose facts after
        # stable patterns are observed. Never blocks the turn; failures
        # are swallowed.
        if not resume_turn_id and not (response.get("strategy_id") or payload.get("strategy_id")) and run_config.get("agent.native.assistant_profile.memory_allowed", True):
            try:
                from ..agent.profile_capture import observe_turn as _observe_turn
                _channel_for_capture = (
                    (trigger.get("payload") or {}).get("channel")
                    if isinstance(trigger.get("payload"), dict)
                    else None
                ) or trigger.get("source") or "chat"
                _capture = _observe_turn(
                    client,
                    user_text=_user_text or "",
                    channel=str(_channel_for_capture),
                )
                if _capture and _capture.get("proposed"):
                    response["profile_capture"] = _capture
            except Exception:  # pragma: no cover - defensive
                pass

        # E2E auto-capture — opt-in via NERYA_E2E_AUTO_CAPTURE_RUN_TURN=1.
        # When enabled, each turn's request/response is captured as a
        # one-shot artifact run so the operator has citeable evidence for
        # the conversation.
        try:
            from ..ops.auto_capture import maybe_capture_run_turn
            _capture_meta = maybe_capture_run_turn(
                client,
                request_payload=payload,
                response=response,
                started_at_ms=_turn_started_at,
            )
            if _capture_meta:
                response["e2e_artifact"] = {
                    "run_id": _capture_meta.get("run_id"),
                    "status": _capture_meta.get("status"),
                }
        except Exception:  # pragma: no cover - defensive
            pass

        return response

    def attachments_upload(client, payload):
        upload_id = str(
            payload.get("upload_id")
            or payload.get("session_id")
            or payload.get("turn_id")
            or ""
        )
        attachments = upload_chat_attachments(
            payload.get("attachments"),
            paths=client.config.paths,
            upload_id=upload_id,
        )
        return {
            "ok": True,
            "upload_id": upload_id,
            "attachments": attachments,
        }

    def get_trace(client, payload):
        """POST /agent/trace — rebuild the end-to-end trace for a correlator.

        operators can hand in any of ``trigger_id``/``turn_id``/
        ``session_id`` (optionally scoped to ``strategy_id``) and get
        back a time-ordered list of every runtime event that touched
        the request. Zero state: re-built from the journals each time.
        """
        trace = build_trace(
            client.config.paths,
            trigger_id=payload.get("trigger_id"),
            turn_id=payload.get("turn_id"),
            session_id=payload.get("session_id"),
            strategy_id=payload.get("strategy_id"),
        )
        return trace.as_dict()

    def explain(client, payload):
        """POST /agent/explain — operator-oriented explain surface.

        Returns the same trace plus stage counts, detected degradation
        rows attribution, and the active strategy version —
        everything an operator needs to answer "what happened and why".
        """
        return explain_trace(
            client.config.paths,
            trigger_id=payload.get("trigger_id"),
            turn_id=payload.get("turn_id"),
            session_id=payload.get("session_id"),
            strategy_id=payload.get("strategy_id"),
        )

    def open_turns(client, _params):
        """GET /agent/open_turns — resumable / halted turn inventory. operator surface: renders every turn the journal
        knows about that never emitted a ``close`` step, so the on-call
        can decide which ones to resume and which to abandon.
        """
        return {
            "open_turns": [s.asdict() for s in list_open_turns(client.config.paths)],
        }

    def turn_state(client, payload):
        """POST /agent/turn_state — full recovery view for one turn."""
        tid = payload.get("turn_id")
        if not tid:
            return {"error": "turn_id required"}
        try:
            return load_turn_state(client.config.paths, tid).asdict()
        except KeyError as exc:
            return {
                "_status": 404,
                "ok": False,
                "error": "turn_state_not_found",
                "message": str(exc),
                "turn_id": str(tid),
            }

    # Session sources that represent a human-initiated chat thread — these
    # are the only ones the dashboard chat sidebar should display by default.
    # Anything else (strategy triggers, scheduled heartbeats, price/news
    # event handlers, sub-agent journals, etc.) lives in the same DB but is
    # not a conversation the operator started from the chat input.
    _CHAT_SOURCES: frozenset[str] = frozenset({
        "",
        "dashboard",
        "user_chat",
        "user.chat",
        "agent.user_message",
        "manual.chat",
        "manual",
        "mcp",
        "tunnel",
        "approval_continue",
    })

    # Prefixes that unambiguously identify a non-chat origin. These are used
    # in addition to the allowlist: the frontend only wants user chats in
    # the sidebar, so the filter is deny-by-default when ``include`` is not
    # ``all``. Matching is case-insensitive, checked against the raw
    # ``source`` column of ``agent_sessions``.
    _NON_CHAT_SOURCE_PREFIXES: tuple[str, ...] = (
        "strategy",
        "schedule",
        "scheduled",
        "cron",
        "trigger",
        "price.",
        "news.",
        "social.",
        "onchain.",
        "subagent",
        "team",
        "webhook",
    )

    def _is_chat_session(row: dict[str, Any]) -> bool:
        """Return True if ``row`` looks like a user-started chat thread.

        A session is treated as "chat" when:
        * it has no ``strategy_id`` (strategy-scoped runs belong to the
          strategies UI, not the chat sidebar), AND
        * its ``source`` is either empty, in the explicit allowlist, or
          does not start with any known non-chat prefix.

        File-backed sessions (``workspace/sessions/*.json``) created before
        the source column was enforced show up with ``source == ""`` — we
        keep those so older dashboards don't suddenly lose history.
        """
        if row.get("strategy_id"):
            return False
        source = str(row.get("source") or "").strip().lower()
        if source in _CHAT_SOURCES:
            return True
        if any(source.startswith(p) for p in _NON_CHAT_SOURCE_PREFIXES):
            return False
        # Unknown non-empty source — err on the side of showing it so a
        # custom integration (CLI, tests) still surfaces in the sidebar
        # unless the operator explicitly tags it as non-chat.
        return True

    def sessions_list(client, query):
        q = dict(query or {})
        store = SessionStore(client.config.paths.root)
        strategy_id = q.get("strategy_id") or None
        try:
            limit = max(1, min(int(q.get("limit") or 50), 100))
        except Exception:
            limit = 50
        try:
            offset = max(0, int(q.get("offset") or 0))
        except Exception:
            offset = 0
        fetch_limit = min(max(limit + offset + 1, 250), 1000)
        # ``include=all`` bypasses the chat-only filter so the strategies /
        # observability surfaces can still see every session. Default path
        # (what the dashboard chat sidebar calls) drops strategy-scoped +
        # trigger-driven runs so one chat thread = one sidebar entry.
        include_mode = str(q.get("include") or "").strip().lower()
        chat_only = include_mode != "all" and not strategy_id
        states = [
            file_session_asdict(s)
            for s in store.list(strategy_id=strategy_id, limit=fetch_limit)
        ]
        by_id = {str(s.get("session_id") or ""): dict(s) for s in states}
        try:
            from ..db.repositories import AgentSessionRepository
            from ..db.sqlite import connect

            con = connect(client.config.paths.db)
            rows = AgentSessionRepository(con).list_sessions(
                limit=fetch_limit,
            )
            deleted = deleted_session_ids(con)
            by_id = {sid: row for sid, row in by_id.items() if sid not in deleted}
            rows = [row for row in rows if row.get("session_id") not in deleted]
            con.close()
            rows = hydrate_db_session_counts(client.config.paths, rows)
            for row in rows:
                if strategy_id and row.get("strategy_id") != strategy_id:
                    continue
                sid = str(row.get("session_id") or "")
                if not sid:
                    continue
                if sid in by_id:
                    by_id[sid] = merge_session_dict(by_id[sid], row)
                else:
                    by_id[sid] = db_session_asdict(row)
        except Exception:
            pass
        sessions = list(by_id.values())
        if chat_only:
            sessions = [s for s in sessions if _is_chat_session(s)]
        sessions.sort(
            key=session_updated_ts,
            reverse=True,
        )
        if q.get("view") == "workbench" or q.get("q") or q.get("state"):
            from ..agent.workbench import filter_sessions
            sessions = filter_sessions(client.config, sessions, q)
        page = sessions[offset:offset + limit]
        return {
            "sessions": page,
            "limit": limit,
            "offset": offset,
            "next_offset": offset + len(page),
            "has_more": len(sessions) > offset + len(page),
        }

    def session_get(client, query):
        q = dict(query or {})
        sid = q.get("session_id") or q.get("id")
        if not sid:
            return {"error": "session_id required"}
        if _history_session_deleted(client.config.paths, sid):
            return {"error": "session not found", "code": "session_deleted", "session_id": sid}
        store = SessionStore(client.config.paths.root)
        state = store.load(sid)
        try:
            from ..db.repositories import AgentSessionRepository
            from ..db.sqlite import connect

            con = connect(client.config.paths.db)
            db_row = AgentSessionRepository(con).get_session(str(sid))
            con.close()
            if db_row:
                db_row = hydrate_db_session_counts(client.config.paths, [db_row])[0]
        except Exception:
            db_row = None
        if state is None:
            if db_row:
                return db_session_asdict(db_row)
            return {"error": "session not found", "session_id": sid}
        return merge_session_dict(state, db_row)

    def session_delete(client, payload):
        from ..agent.history_mutations import delete_session, history_response
        return history_response(delete_session, client.config.paths, payload)

    def session_rename(client, payload):
        from ..agent.history_mutations import rename_session, history_response
        return history_response(rename_session, client.config.paths, payload)

    def session_message_edit(client, payload):
        from ..agent.history_mutations import mutate_message, history_response
        return history_response(mutate_message, client.config.paths, payload)

    def session_message_delete(client, payload):
        from functools import partial
        from ..agent.history_mutations import mutate_message, history_response
        return history_response(partial(mutate_message, delete=True), client.config.paths, payload)

    def session_record_skill_state(client, payload):
        p = payload or {}
        sid = p.get("session_id")
        skill_id = p.get("skill_id")
        if not sid or not skill_id:
            return {"ok": False, "error": "session_id + skill_id required"}
        store = SessionStore(client.config.paths.root)
        state = store.record_skill_state(sid, skill_id, p.get("state"))
        return {"ok": True, "session": state.asdict()}

    def session_search(client, payload):
        """POST /agent/session/search — full-journal search.

        Body: ``{query, session_id?, strategy_id?, limit?, journals?}``.
        Returns matching events newest-first with payload + preview.
        """
        from ..agent.session_search import search as _search
        p = payload or {}
        q = str(p.get("query") or "").strip()
        if not q:
            return {"ok": False, "error": "query required"}
        rows = _search(
            client.config.paths,
            q,
            session_id=p.get("session_id"),
            strategy_id=p.get("strategy_id"),
            limit=int(p.get("limit") or 50),
            journals=tuple(p.get("journals") or ("turn_steps", "agent_decisions", "skills", "messages")),
            case_sensitive=bool(p.get("case_sensitive", False)),
        )
        return {"ok": True, "matches": rows, "count": len(rows)}

    def session_recent_events(client, query):
        """GET /agent/session/events — recent journal events for a session."""
        from ..agent.session_search import recent_events as _recent
        q = dict(query or {})
        rows = _recent(
            client.config.paths,
            session_id=q.get("session_id"),
            strategy_id=q.get("strategy_id"),
            limit=int(q.get("limit") or 50),
        )
        return {"events": rows, "count": len(rows)}

    def session_transcript_handler(client, query):
        """GET /agent/session/transcript — chat-shaped transcript.

        Returns user/assistant pairs reconstructed from the agent
        journal so the dashboard chat can fold in conversations that
        were started outside the dashboard (curl, gateway, scripts).
        """
        from ..agent.session_search import session_transcript as _txn
        q = dict(query or {})
        sid = q.get("session_id") or q.get("id")
        if not sid:
            return {"ok": False, "error": "session_id required"}
        if _history_session_deleted(client.config.paths, sid):
            return {"ok": False, "error": "session_deleted", "code": "session_deleted", "session_id": str(sid)}
        messages: list[dict] = []
        canonical_history = False
        has_more = False
        full = _truthy_query(q.get("full")) or _truthy_query(q.get("all"))
        try:
            max_pairs = int(q.get("max_pairs") or 200)
        except Exception:
            max_pairs = 200
        try:
            per_msg_cap = int(q.get("per_msg_cap") or 12_000)
        except Exception:
            per_msg_cap = 12_000
        try:
            import json as _json

            from ..db.repositories import AgentSessionRepository
            from ..db.sqlite import connect

            con = connect(client.config.paths.db)
            repo = AgentSessionRepository(con)
            session_row = repo.get_session(str(sid))
            if not full and (
                (session_row and session_row.get("source") in {"mcp", "tunnel"})
                or (not session_row and str(sid).startswith(("ext_mcp_", "ext_tunnel_")))
            ):
                from ..agent.external_history import external_transcript_page
                try:
                    return external_transcript_page(con, str(sid), q)
                finally:
                    con.close()
            canonical_history = has_message_history(con, str(sid))
            anchor=q.get("anchor_message_id")
            if anchor and not full:
                found=con.execute("SELECT ts FROM agent_messages WHERE session_id=? AND message_id=? AND deleted=0",(sid,anchor)).fetchone()
                if found:
                    count=con.execute("SELECT count(*) FROM agent_messages WHERE session_id=? AND deleted=0 AND ts>=?",(sid,found["ts"])).fetchone()[0]
                    max_pairs=max(max_pairs,min(1000,(count+1)//2+2))
            rows = repo.transcript(str(sid), limit=0 if full else max_pairs * 2 + 1)
            if not full and len(rows)>max_pairs*2:
                has_more=True
                rows=rows[-max_pairs*2:]
            turn_ids = {
                str(r.get("turn_id") or "")
                for r in rows
                if r.get("role") == "assistant" and r.get("turn_id")
            }
            tool_events_by_turn: dict[str, list[dict[str, Any]]] = {}
            if turn_ids:
                for ev in repo.tool_events(str(sid), turn_ids=turn_ids):
                    tid = str(ev.get("turn_id") or "")
                    if tid:
                        tool_events_by_turn.setdefault(tid, []).append(ev)
            con.close()
            for r in rows:
                if r.get("role") not in {"user", "assistant"}:
                    continue
                meta = {}
                try:
                    meta = _json.loads(r.get("meta_json") or "{}")
                except Exception:
                    meta = {}
                if not isinstance(meta, dict):
                    meta = {}
                # May-01 2026 — assistant rows now persist the full
                # turn payload (blocks / tool_trace / actions / budget)
                # under ``meta.turn`` so the dashboard can rebuild the
                # chronological tool timeline on import. Surface it as
                # a first-class field and strip it from the meta blob
                # to keep ``meta`` a lightweight catch-all.
                turn_payload = None
                if r.get("role") == "assistant":
                    turn_candidate = meta.pop("turn", None)
                    if isinstance(turn_candidate, dict):
                        turn_payload = turn_candidate if turn_candidate.get("harness") == "external" else _rehydrate_turn_tool_events(
                            turn_candidate,
                            tool_events_by_turn.get(str(r.get("turn_id") or ""), []),
                        )
                        turn_payload = _augment_turn_backtest_locators(
                            turn_payload,
                            client=client,
                        )
                messages.append(
                    {
                        "message_id": r.get("message_id"),
                        "role": r.get("role"),
                        "content": r.get("content"),
                        "turn_id": r.get("turn_id"),
                        "ts": r.get("ts"),
                        "meta": meta,
                        "turn": turn_payload,
                    }
                )
        except Exception:
            if "con" in locals():
                con.close()
            return {"ok": False, "error": "history_read_failed", "code": "history_read_failed"}
        if not messages and not canonical_history:
            try:
                messages = _txn(
                    client.config.paths,
                    session_id=str(sid),
                    per_msg_cap=0 if full else per_msg_cap,
                    max_pairs=0 if full else max_pairs,
                )
            except Exception as exc:
                return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
        store = SessionStore(client.config.paths.root)
        state = store.load(str(sid))
        try:
            from ..db.repositories import AgentSessionRepository
            from ..db.sqlite import connect

            con = connect(client.config.paths.db)
            db_row = AgentSessionRepository(con).get_session(str(sid))
            con.close()
        except Exception:
            db_row = None
        db_state = db_session_asdict(db_row) if db_row else {}
        db_meta = db_state.get("meta") if isinstance(db_state.get("meta"), dict) else {}
        merged_state = merge_session_dict(state, db_row) if state else db_state
        state_meta = merged_state.get("meta") or {}
        external = db_state.get("source") in {"mcp", "tunnel"}
        return {
            "ok": True,
            "session_id": str(sid),
            "source": db_state.get("source", ""),
            "strategy_id": (
                state.strategy_id if state else db_state.get("strategy_id")
            ),
            "title": (state_meta.get("title") or db_meta.get("title") or ""),
            "strategy_proposal_id": state_meta.get("strategy_proposal_id") or db_meta.get("strategy_proposal_id"),
            "created_at": (state.created_at if state else db_state.get("created_at", "")),
            "updated_at": merged_state.get("updated_at", ""),
            "messages": messages,
            "count": len(messages),
            "has_more": has_more,
        }

    def stream_events(client, query):
        """GET /agent/stream/events — streaming bus replay.

        Returns recently-published events from the process-wide
        :class:`StreamingEventBus`. The dashboard polls this endpoint
        (or any other subscriber that cannot hold a long-lived
        socket) to render assistant deltas, tool start/progress, and
        approval cards in real time. Replays the last N events so a
        new client never misses the just-finished turn.

        Query parameters
        ----------------
        ``session_id`` (optional)
            Restrict the response to events whose payload is tagged
            with this session.
        ``after_seq`` (optional)
            Return only events with ``seq > after_seq``. Implements
            the reconnect contract: a client persists the
            largest seq it has rendered and replays from there on
            reconnect, so it never duplicates or drops events.
        ``limit`` (optional)
            Cap the number of events returned (newest-N).

        Response
        --------
        ``{events, count, cursor, latest_seq}``

        ``cursor`` is the largest seq in the returned slice (or the
        bus's ``latest_seq`` if no events match) — clients pass it
        back as ``after_seq`` on the next poll. ``latest_seq`` is the
        bus-wide cursor regardless of filtering, useful for clients
        that filter by ``session_id`` and still want a global pointer.
        """

        from ..agent.streaming import get_default_bus
        bus = get_default_bus()
        q = query or {}
        try:
            after_seq_raw = q.get("after_seq")
            after_seq = int(after_seq_raw) if after_seq_raw not in (None, "") else None
        except (TypeError, ValueError):
            after_seq = None
        try:
            limit = int(q.get("limit") or 500)
        except (TypeError, ValueError):
            limit = 500
        return bus.page(after_seq=after_seq, epoch=q.get("epoch"),
                        session_id=q.get("session_id"), limit=limit)

    def interrupt(client, payload):
        """POST /agent/interrupt — stop control.

        Best-effort cooperative cancellation of the current turn(s)
        for ``session_id``. The kernel's :class:`CancelToken` is
        consulted at every step boundary, so signalling here causes
        the in-flight turn to return on the next checkpoint.
        """

        from ..harness.cancellation import signal_cancel
        p = payload or {}
        sid = p.get("session_id") or p.get("turn_id")
        if not sid:
            return {"ok": False, "error": "session_id required"}
        cancelled = signal_cancel(str(sid), reason=str(p.get("reason") or "operator_interrupt"))
        return {"ok": True, "cancelled": cancelled, "session_id": str(sid)}

    def steer(client, payload):
        """POST /agent/steer — mid-turn redirect.

        Queues an operator message for the *running* turn of
        ``session_id`` (or ``turn_id``). The agent loop drains the
        queue between iterations and appends each message to the live
        transcript as a pinned user message, so the model
        course-corrects on its next round without aborting the turn or
        losing the tool work already done. Returns ``steered=False``
        when no turn is currently running under that id — the caller
        should then send a normal new-turn message instead.
        """

        from ..harness.cancellation import signal_steer
        p = payload or {}
        sid = p.get("session_id") or p.get("turn_id")
        message = str(p.get("message") or p.get("text") or "").strip()
        if not sid:
            return {"ok": False, "error": "session_id required"}
        if not message:
            return {"ok": False, "error": "message required"}
        steered = signal_steer(str(sid), message)
        return {"ok": True, "steered": steered, "session_id": str(sid)}

    def session_message_append(client, payload):
        from ..mcp.operator_messages import append_message
        try:
            return append_message(client.config, payload)
        except ValueError as exc:
            return {"ok": False, "error": str(exc)}

    def tool_registry(client, _payload):
        return client.agent.list_tools()

    def run_turn_internal(client, payload):
        """Dashboard-only loopback entry point.

        The HTTP dispatcher enforces the loopback boundary for this route.
        Keeping it as a distinct route (rather than trusting a payload
        ``source`` field) prevents a public caller from self-declaring an
        internal origin. Nerya's prompt firewall and normal permission/Risk
        gates still run; only the public research-only gate is skipped.
        """
        return run_turn(client, payload, enforce_public_gate=False)

    from .routes_conversation_commands import command_routes
    return [
        ("POST", "/agent/run_turn", run_turn),
        ("POST", "/agent/run_turn_internal", run_turn_internal),
        ("POST", "/agent/attachments/upload", attachments_upload),
        ("POST", "/agent/trace", get_trace),
        ("POST", "/agent/explain", explain),
        ("GET",  "/agent/open_turns", open_turns),
        ("POST", "/agent/turn_state", turn_state),
        ("GET",  "/agent/sessions", sessions_list),
        ("GET",  "/agent/session", session_get),
        ("POST", "/agent/session/delete", session_delete),
        ("POST", "/agent/session/rename", session_rename),
        ("POST", "/agent/session/message/append", session_message_append),
        ("POST", "/agent/session/message/edit", session_message_edit),
        ("POST", "/agent/session/message/delete", session_message_delete),
        ("POST", "/agent/session/skill_state", session_record_skill_state),
        ("POST", "/agent/session/search", session_search),
        ("GET",  "/agent/session/events", session_recent_events),
        ("GET",  "/agent/session/transcript", session_transcript_handler),
        ("GET",  "/agent/stream/events", stream_events),
        ("POST", "/agent/interrupt", interrupt),
        ("POST", "/agent/steer", steer),
        ("GET",  "/agent/tools", tool_registry),
    ] + command_routes(run_turn)
