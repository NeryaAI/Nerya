"""Sub-agent native tools — wrap :mod:`nerya.subagents` for the kernel.

compatibility: the parent kernel exposes a small handful of native tools
that let the model spawn a child runtime instead of reaching into
``runtime.call("subagents", "spawn", ...)`` through the legacy bridge.

The two tools are intentionally thin:

* ``subagent_list`` — read-only enumeration of registered specs (what
  the model is allowed to spawn). Always available.
* ``subagent_run`` — actually run one spec with a typed payload. Routes
  through :class:`SubAgentDispatcher` so the existing denylist + journal
  hooks stay authoritative.

The dispatcher already enforces the live-trading denylist
(``trading``/``wallet``/``script_runtime``), so we don't re-enforce it
here. Errors come back inside a ``SubAgentResult.ok=false`` envelope and
the handlers surface them as ``ToolResult`` JSON, never as an exception
that crashes the agent loop.
"""

from __future__ import annotations

import json
import uuid
from threading import Lock
from typing import Any

from ...core.config import Config
from ...core.redaction import redact_display_dict, redact_text
from ...skills.kernel import SkillKernel
from ...subagents.dispatcher import SubAgentDispatcher
from ...subagents.registry import (
    DEFAULT_SUBAGENT_SKILLS,
    DEFAULT_TIERS,
    delete_role,
    describe_role,
    list_roles,
    load_registry,
    save_role,
)
from ...subagents.result_aggregator import aggregate
from ..types import (
    ToolCall,
    ToolError,
    ToolErrorKind,
    ToolResult,
)


def _call_meta(call: ToolCall, key: str) -> Any:
    meta = call.metadata if isinstance(call.metadata, dict) else {}
    return meta.get(key)


def _publish_team_event(kind: str, **payload: Any) -> None:
    try:
        from ...agent.streaming import get_default_bus

        get_default_bus().publish(kind, **payload)
    except Exception:
        pass


_TEAM_RUN_TURN_CACHE_MAX = 128
_TEAM_RUN_TURN_CACHE: dict[tuple[str, str], dict[str, Any]] = {}
_TEAM_RUN_TURN_CACHE_ORDER: list[tuple[str, str]] = []
_TEAM_RUN_TURN_CACHE_LOCK = Lock()
_LANGUAGE_KEYS = (
    "output_language",
    "target_language",
    "response_language",
    "preferred_language",
    "language",
    "locale",
)


def _json_clone(value: Any) -> Any:
    return json.loads(json.dumps(value, ensure_ascii=False, default=str))


def _team_run_turn_key(call: ToolCall, args: dict[str, Any]) -> tuple[str, str]:
    session_key = (
        args.get("session_id")
        or _call_meta(call, "session_id")
        or args.get("trigger_event_id")
        or _call_meta(call, "trigger_event_id")
        or "no-session"
    )
    turn_key = (
        call.turn_id
        or args.get("turn_id")
        or _call_meta(call, "turn_id")
        or args.get("trigger_event_id")
        or _call_meta(call, "trigger_event_id")
        or call.id
    )
    return (str(session_key), str(turn_key))


def _allow_additional_team_run(call: ToolCall, args: dict[str, Any]) -> bool:
    return (
        args.get("allow_additional_team_run") is True
        and _call_meta(call, "allow_additional_team_run") is True
    )


def _get_cached_team_run_summary(key: tuple[str, str]) -> dict[str, Any] | None:
    with _TEAM_RUN_TURN_CACHE_LOCK:
        cached = _TEAM_RUN_TURN_CACHE.get(key)
        if cached is None:
            return None
        return _json_clone(cached)


def cached_team_run_summary_for_call(
    call: ToolCall,
    args: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    return _get_cached_team_run_summary(_team_run_turn_key(call, args or {}))


def _remember_team_run_summary(
    key: tuple[str, str],
    summary: dict[str, Any],
) -> None:
    with _TEAM_RUN_TURN_CACHE_LOCK:
        if key not in _TEAM_RUN_TURN_CACHE:
            _TEAM_RUN_TURN_CACHE_ORDER.append(key)
        _TEAM_RUN_TURN_CACHE[key] = _json_clone(summary)
        while len(_TEAM_RUN_TURN_CACHE_ORDER) > _TEAM_RUN_TURN_CACHE_MAX:
            old_key = _TEAM_RUN_TURN_CACHE_ORDER.pop(0)
            _TEAM_RUN_TURN_CACHE.pop(old_key, None)


def _duplicate_team_run_summary(
    cached: dict[str, Any],
    *,
    requested_team_run_id: str,
    task: str,
    role_names: list[str],
) -> dict[str, Any]:
    duplicate = _json_clone(cached)
    duplicate["duplicate_suppressed"] = True
    duplicate["duplicate_status"] = "duplicate_suppressed"
    duplicate["duplicate_of_team_run_id"] = cached.get("team_run_id")
    duplicate["duplicate_request_team_run_id"] = requested_team_run_id
    duplicate["duplicate_request_task"] = task
    duplicate["duplicate_request_roles"] = list(role_names)
    output_language = str(
        cached.get("output_language") or "the original user prompt language"
    )
    duplicate["next_action"] = (
        "A team_run already completed in this same turn. Use the cached "
        "team_run results to write the complete requested answer now. For "
        "research-report tasks, include the full report in this reply; do "
        "not ask whether the user wants details. Write the user-visible final "
        f"answer in the original user prompt language ({output_language}), "
        "translating team member outputs, headings, labels, and "
        "natural-language field names as needed while preserving proper "
        "nouns, tickers, source "
        "names, code identifiers, and URLs. team_run_id is not an async task_id; "
        "do not call task_get, task_output, task_list, or run team_run again "
        "for this turn."
    )
    return duplicate


def _compact_text(value: Any, *, limit: int = 8000) -> str:
    text = str(value or "")
    if len(text) <= limit:
        return text
    return text[:limit] + f"\n...[truncated {len(text) - limit} chars]"


def _explicit_output_language(value: Any) -> str | None:
    text = str(value or "").strip()
    if not text:
        return None
    return text[:80]


def _resolve_team_output_language(
    *,
    args: dict[str, Any],
    task: str,
    raw_roles: list[Any],
    shared_payload: dict[str, Any],
) -> str:
    for source in (args, shared_payload):
        for key in _LANGUAGE_KEYS:
            language = _explicit_output_language(source.get(key))
            if language:
                return language
    for entry in raw_roles:
        if not isinstance(entry, dict):
            continue
        for key in _LANGUAGE_KEYS:
            language = _explicit_output_language(entry.get(key))
            if language:
                return language
        payload = entry.get("payload") if isinstance(entry.get("payload"), dict) else {}
        for key in _LANGUAGE_KEYS:
            language = _explicit_output_language(payload.get(key))
            if language:
                return language
    return "the original user prompt language"


def _compact_json_value(value: Any, *, limit: int = 12000, depth: int = 0) -> Any:
    if isinstance(value, str):
        return _compact_text(value, limit=limit)
    if isinstance(value, (int, float, bool)) or value is None:
        return value
    if depth >= 4:
        return _compact_text(json.dumps(value, ensure_ascii=False, default=str), limit=limit)
    if isinstance(value, list):
        items = [
            _compact_json_value(item, limit=max(1000, limit // 4), depth=depth + 1)
            for item in value[:20]
        ]
        if len(value) > 20:
            items.append({"_truncated_items": len(value) - 20})
        return items
    if isinstance(value, dict):
        skip = {"steps", "audit", "prompt_records"}
        out: dict[str, Any] = {}
        for key, item in value.items():
            if key in skip:
                continue
            out[str(key)] = _compact_json_value(
                item,
                limit=max(1000, limit // 3),
                depth=depth + 1,
            )
        rendered = json.dumps(out, ensure_ascii=False, default=str)
        if len(rendered) > limit:
            return {
                "summary": _compact_text(rendered, limit=limit),
                "truncated": True,
            }
        return out
    return _compact_text(value, limit=limit)


def _compact_tool_records(records: Any, *, limit: int = 16) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    if not isinstance(records, list):
        return out
    for rec in records[:limit]:
        if not isinstance(rec, dict):
            continue
        item = {
            "ok": bool(rec.get("ok")),
            "skill": rec.get("skill"),
            "action": rec.get("action"),
        }
        if rec.get("error"):
            item["error"] = _compact_text(rec.get("error"), limit=500)
        out.append(item)
    if len(records) > limit:
        out.append({"truncated_records": len(records) - limit})
    return out


def _compact_member_entry(entry: dict[str, Any]) -> dict[str, Any]:
    metrics = entry.get("metrics") if isinstance(entry.get("metrics"), dict) else {}
    compact_metrics = {
        "iterations": metrics.get("iterations"),
        "signals_used": _compact_json_value(metrics.get("signals_used") or [], limit=2000),
        "evidence": _compact_json_value(metrics.get("evidence") or [], limit=4000),
        "uncertainty": metrics.get("uncertainty"),
        "skill_calls_count": len(metrics.get("skill_calls") or []),
        "rejected_actions_count": len(metrics.get("rejected_actions") or []),
        "skill_calls": _compact_tool_records(metrics.get("skill_calls")),
        "rejected_actions": _compact_tool_records(metrics.get("rejected_actions"), limit=8),
    }
    out = {
        "subagent": entry.get("subagent"),
        "ok": bool(entry.get("ok")),
        "tier": entry.get("tier"),
        "tokens": entry.get("tokens", 0),
        "usd": entry.get("usd", 0.0),
        "wall_ms": entry.get("wall_ms", 0),
        "output": _compact_json_value(entry.get("output") or {}, limit=12000),
        "metrics": compact_metrics,
    }
    if entry.get("error"):
        out["error"] = _compact_text(entry.get("error"), limit=1000)
    if entry.get("error_kind"):
        out["error_kind"] = entry.get("error_kind")
    return out


def _team_assignment_prompt(
    *,
    task: str,
    role_name: str,
    payload: dict[str, Any],
    instructions: str = "",
    output_language: str = "",
) -> str:
    lines = [
        "Agent Team member assignment",
        "",
        f"Team mission: {task}",
        f"Role: {role_name}",
    ]
    if output_language:
        lines.extend([
            "",
            "Output language:",
            f"- Target user-visible language: {output_language}.",
            "- Write all natural-language JSON values and role conclusions "
            "in this language.",
            "- Preserve JSON keys, enum values required by the role contract, "
            "proper nouns, tickers, source names, code identifiers, URLs, "
            "and numeric metrics in their original form.",
        ])
    if instructions:
        lines.extend(["", "Role-specific instructions:", instructions])
    lines.extend([
        "",
        "Data discipline:",
        "- Stay on the team mission and payload subject only.",
        "- For market, company, research, or risk tasks, gather "
        "role-relevant source data before writing the role conclusion; "
        "if one data source fails, try another visible data/search/provider "
        "tool and report the exact remaining gap instead of returning only "
        "a query, plan, or unavailable-data claim.",
        "- When using market_data, always pass an explicit market/symbol from the mission or payload; never call it with empty arguments.",
        "- For on-chain/meme/DEX data beyond OHLCV, inspect data_api provider='wallet' and provider='onchainos' before claiming a source is unavailable.",
        "- For wallet-backed meme strategies, call data_api wallet.capability_catalog or wallet.meme_strategy_guide before authoring rules, use selection.selected_route.call for the installed/logged-in wallet, and follow GOAT/self_custody fallback plus install recommendations when no wallet is ready.",
        "",
        "Input payload:",
        json.dumps(redact_display_dict(payload), ensure_ascii=False, indent=2, default=str),
    ])
    return redact_text("\n".join(lines))


def _coerce_roles_arg(raw_roles: Any) -> list[Any]:
    if isinstance(raw_roles, list):
        return raw_roles
    if isinstance(raw_roles, str) and raw_roles.strip():
        try:
            parsed = json.loads(raw_roles)
        except Exception:
            return []
        if isinstance(parsed, list):
            return parsed
        if isinstance(parsed, dict) and isinstance(parsed.get("roles"), list):
            return list(parsed["roles"])
    return []


SUBAGENT_LIST_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {},
}

SUBAGENT_RUN_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "name": {
            "type": "string",
            "description": (
                "Subagent name (e.g. 'market_analyst', 'risk_critic', "
                "'coding_agent'). Must match a spec on disk under "
                "<workspace>/subagents/<name>.agent.md or one of the "
                "DEFAULT_SUBAGENT_SKILLS keys."
            ),
        },
        "payload": {
            "type": "object",
            "description": (
                "Arbitrary JSON handed to the child runtime as the task "
                "payload. The child reads it under '=== task payload ==='."
            ),
        },
        "strategy_id": {
            "type": "string",
            "description": "Optional strategy scope (forwarded to the child).",
        },
        "session_id": {
            "type": "string",
            "description": "Optional session id (forwarded to the child).",
        },
        "trigger_event_id": {
            "type": "string",
            "description": "Optional trigger event id for journal correlation.",
        },
    },
    "required": ["name"],
}


def subagent_list_handler(
    call: ToolCall,
    *,
    config: Config,
) -> ToolResult:
    """Enumerate registered subagent specs (workspace + defaults)."""

    out: list[dict[str, Any]] = []
    seen: set[str] = set()

    try:
        on_disk = load_registry(config.paths)
    except Exception:
        on_disk = {}

    for name, spec in sorted(on_disk.items()):
        out.append({
            "name": name,
            "tier": spec.tier,
            "allowed_skills": list(spec.allowed_skills),
            "source": "workspace",
            "prompt_path": str(spec.prompt_path),
        })
        seen.add(name)

    for name, allowed in sorted(DEFAULT_SUBAGENT_SKILLS.items()):
        if name in seen:
            continue
        out.append({
            "name": name,
            "tier": DEFAULT_TIERS.get(name, "medium"),
            "allowed_skills": list(allowed),
            "source": "default",
            "prompt_path": None,
        })

    return ToolResult.from_json(
        tool_use_id=call.id,
        name=call.name,
        data={"count": len(out), "subagents": out},
    )


def subagent_run_handler(
    call: ToolCall,
    *,
    config: Config,
    skills: SkillKernel,
    tool_registry: Any = None,
) -> ToolResult:
    """Spawn one subagent and return its envelope."""

    args = call.arguments or {}
    name = (args.get("name") or "").strip()
    if not name:
        return ToolResult.from_error(
            tool_use_id=call.id,
            name=call.name,
            error=ToolError(
                kind=ToolErrorKind.SCHEMA_VALIDATION,
                message="name is required",
            ),
        )
    payload = args.get("payload") if isinstance(args.get("payload"), dict) else {}
    strategy_id = args.get("strategy_id") or _call_meta(call, "strategy_id") or None
    session_id = args.get("session_id") or _call_meta(call, "session_id") or None
    trigger_event_id = (
        args.get("trigger_event_id")
        or _call_meta(call, "trigger_event_id")
        or None
    )
    cached_team = _get_cached_team_run_summary(_team_run_turn_key(call, args))
    if cached_team is not None and cached_team.get("ok") is True:
        _publish_team_event(
            "team.subagent_duplicate",
            call_id=call.id,
            tool_call_id=call.id,
            turn_id=call.turn_id,
            session_id=session_id,
            strategy_id=strategy_id,
            trigger_event_id=trigger_event_id,
            subagent=name,
            duplicate_of_team_run_id=cached_team.get("team_run_id"),
            status="team_already_completed",
            ok=True,
        )
        return ToolResult.from_json(
            tool_use_id=call.id,
            name=call.name,
            data={
                "ok": True,
                "status": "team_already_completed",
                "subagent": name,
                "skipped": True,
                "duplicate_of_team_run_id": cached_team.get("team_run_id"),
                "team_summary": cached_team,
                "next_action": (
                    "A successful team_run already completed in this same "
                    "turn. Use the cached team results to synthesize the "
                    "final answer now in the original user prompt language "
                    f"({cached_team.get('output_language') or 'the original user prompt language'}); "
                    "translate headings, labels, and natural-language field names; "
                    "do not launch more subagents for this turn."
                ),
            },
        )

    dispatcher_kwargs = {"config": config, "skills": skills}
    if tool_registry is not None:
        dispatcher_kwargs["tool_registry"] = tool_registry
    dispatcher = SubAgentDispatcher(**dispatcher_kwargs)
    try:
        envelope = dispatcher.dispatch(
            f"subagent:{name}",
            payload=payload or {},
            trigger_event_id=trigger_event_id,
            strategy_id=strategy_id,
            session_id=session_id,
            turn_id=call.turn_id,
            parent_call_id=call.id,
        )
    except Exception as exc:
        return ToolResult.from_error(
            tool_use_id=call.id,
            name=call.name,
            error=ToolError(
                kind=ToolErrorKind.EXECUTION_ERROR,
                message=f"{type(exc).__name__}: {exc}",
            ),
        )

    if not envelope.get("ok", True):
        return ToolResult.from_error(
            tool_use_id=call.id,
            name=call.name,
            error=ToolError(
                kind=ToolErrorKind.EXECUTION_ERROR,
                message=str(envelope.get("error") or "subagent failed"),
                detail={"envelope": envelope},
            ),
        )
    return ToolResult.from_json(
        tool_use_id=call.id,
        name=call.name,
        data=envelope,
    )


TEAM_RUN_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "task": {
            "type": "string",
            "description": (
                "One-line shared mission for the whole team. Each role "
                "sees this verbatim under '=== team task ===' so they "
                "can orient before reading their own payload."
            ),
        },
        "roles": {
            "type": "array",
            "minItems": 1,
            "description": (
                "List of roles to spawn in parallel. Each entry is "
                "{name: <role>, payload: {...}}. ``name`` must match a "
                "registered subagent (workspace or default). ``payload`` "
                "is merged on top of the shared ``shared_payload``. Pass "
                "this as a real JSON array, not a stringified JSON array."
            ),
            "items": {
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "payload": {"type": "object"},
                    "instructions": {
                        "type": "string",
                        "description": (
                            "Optional per-role instruction prepended to "
                            "the role's prompt for this run."
                        ),
                    },
                },
                "required": ["name"],
            },
        },
        "shared_payload": {
            "type": "object",
            "description": "Common payload merged into every role's payload.",
        },
        "output_language": {
            "type": "string",
            "description": (
                "Target language for user-visible team member outputs and "
                "the final synthesis. Default is inferred from the team task "
                "and payload; set this from the latest user prompt when known."
            ),
        },
        "max_parallel": {"type": "integer", "minimum": 1},
        "timeout_s": {
            "type": "number",
            "minimum": 30,
            "description": (
                "Hard wall-clock budget for the whole team_run. Pending "
                "members are returned as timeout failures instead of "
                "blocking the parent turn forever."
            ),
        },
        "allow_additional_team_run": {
            "type": "boolean",
            "description": (
                "Internal escape hatch for orchestrators that intentionally "
                "need more than one independent team_run in the same turn. "
                "Ignored unless tool metadata also permits it."
            ),
        },
        "strategy_id": {"type": "string"},
        "session_id": {"type": "string"},
        "trigger_event_id": {"type": "string"},
    },
    "required": ["task", "roles"],
}

ROLE_LIST_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {},
}

ROLE_GET_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {"name": {"type": "string"}},
    "required": ["name"],
}

ROLE_SAVE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "name": {
            "type": "string",
            "description": "Role identifier ([A-Za-z0-9_]+).",
        },
        "prompt": {
            "type": "string",
            "description": (
                "Markdown body for ``<workspace>/subagents/<name>.agent.md``. "
                "Should describe the role's expertise, output schema, and "
                "any constraints (read-only, language, etc.)."
            ),
        },
        "allowed_skills": {
            "type": "array",
            "items": {"type": "string"},
            "description": (
                "Skills this role may invoke. The dispatcher denylist "
                "still blocks live-trading surfaces regardless."
            ),
        },
        "tier": {
            "type": "string",
            "enum": ["light", "medium", "high"],
            "description": "LLM tier the runtime should use.",
        },
    },
    "required": ["name", "prompt"],
}

ROLE_DELETE_SCHEMA: dict[str, Any] = ROLE_GET_SCHEMA


def team_run_handler(
    call: ToolCall,
    *,
    config: Config,
    skills: SkillKernel,
    tool_registry: Any = None,
) -> ToolResult:
    """Run a multi-role Agent Team in parallel and return aggregated findings.

    Mirrors the dispatcher's ``dispatch_many`` but exposes it as a
    single tool call so the model can write
    ``team_run({task: "...", roles: [{name: "market_analyst"}, ...]})``
    instead of orchestrating each subagent itself.
    """

    args = call.arguments or {}
    task = (args.get("task") or "").strip()
    if not task:
        return ToolResult.from_error(
            tool_use_id=call.id,
            name=call.name,
            error=ToolError(
                kind=ToolErrorKind.SCHEMA_VALIDATION,
                message="task is required (one-line shared mission)",
            ),
        )

    raw_roles = _coerce_roles_arg(args.get("roles"))
    if not isinstance(raw_roles, list) or not raw_roles:
        return ToolResult.from_error(
            tool_use_id=call.id,
            name=call.name,
            error=ToolError(
                kind=ToolErrorKind.SCHEMA_VALIDATION,
                message=(
                    "roles must be a non-empty array of objects, e.g. "
                    "[{\"name\":\"market_analyst\"}, {\"name\":\"risk_critic\"}]"
                ),
            ),
        )

    shared_payload = args.get("shared_payload") if isinstance(
        args.get("shared_payload"), dict,
    ) else {}
    output_language = _resolve_team_output_language(
        args=args,
        task=task,
        raw_roles=raw_roles,
        shared_payload=shared_payload,
    )
    original_user_prompt = str(
        args.get("original_user_prompt")
        or _call_meta(call, "original_user_prompt")
        or ""
    ).strip()
    strategy_id = args.get("strategy_id") or _call_meta(call, "strategy_id") or None
    session_id = args.get("session_id") or _call_meta(call, "session_id") or None
    trigger_event_id = (
        args.get("trigger_event_id")
        or _call_meta(call, "trigger_event_id")
        or None
    )
    max_parallel = args.get("max_parallel")
    if max_parallel is not None:
        try:
            max_parallel = max(1, int(max_parallel))
        except Exception:
            max_parallel = None
    team_run_id = str(args.get("team_run_id") or "").strip()
    if not team_run_id:
        team_run_id = f"team-{uuid.uuid4().hex[:10]}"
    team_template = str(args.get("team_template") or "ad_hoc_parallel_team")
    guard_key = _team_run_turn_key(call, args)
    allow_additional_team_run = _allow_additional_team_run(call, args)
    role_names: list[str] = []
    role_payloads: dict[str, dict[str, Any]] = {}
    role_assignment_prompts: dict[str, str] = {}
    for entry in raw_roles:
        if not isinstance(entry, dict):
            return ToolResult.from_error(
                tool_use_id=call.id,
                name=call.name,
                error=ToolError(
                    kind=ToolErrorKind.SCHEMA_VALIDATION,
                    message="roles[*] must be objects",
                ),
            )
        role_name = (entry.get("name") or "").strip()
        if not role_name:
            return ToolResult.from_error(
                tool_use_id=call.id,
                name=call.name,
                error=ToolError(
                    kind=ToolErrorKind.SCHEMA_VALIDATION,
                    message="roles[*].name is required",
                ),
            )
        if role_name in role_names:
            return ToolResult.from_error(
                tool_use_id=call.id,
                name=call.name,
                error=ToolError(
                    kind=ToolErrorKind.SCHEMA_VALIDATION,
                    message=f"duplicate role: {role_name!r}",
                ),
            )
        role_names.append(role_name)
        merged = dict(shared_payload or {})
        per_role = entry.get("payload") if isinstance(entry.get("payload"), dict) else {}
        merged.update(per_role)
        merged.setdefault("output_language", output_language)
        if original_user_prompt:
            merged.setdefault("original_user_prompt", original_user_prompt)
        merged["__team_task"] = task
        merged["team_run_id"] = team_run_id
        merged["team_template"] = team_template
        merged["team_call_id"] = call.id
        merged["task_id"] = f"role-{role_name}"
        merged["task_owner"] = role_name
        merged["task_subject"] = task
        instructions = (entry.get("instructions") or "").strip()
        if instructions:
            merged["__team_instructions"] = instructions
        role_payloads[role_name] = merged
        role_assignment_prompts[role_name] = _team_assignment_prompt(
            task=task,
            role_name=role_name,
            payload=merged,
            instructions=instructions,
            output_language=output_language,
        )

    if not allow_additional_team_run:
        cached_summary = _get_cached_team_run_summary(guard_key)
        if cached_summary is not None:
            duplicate_summary = _duplicate_team_run_summary(
                cached_summary,
                requested_team_run_id=team_run_id,
                task=task,
                role_names=role_names,
            )
            _publish_team_event(
                "team.duplicate",
                call_id=call.id,
                tool_call_id=call.id,
                turn_id=call.turn_id,
                team_run_id=team_run_id,
                duplicate_of_team_run_id=cached_summary.get("team_run_id"),
                session_id=session_id,
                strategy_id=strategy_id,
                trigger_event_id=trigger_event_id,
                task=task,
                roles=role_names,
                status="already_completed",
                ok=bool(cached_summary.get("ok")),
            )
            return ToolResult.from_json(
                tool_use_id=call.id,
                name=call.name,
                data=duplicate_summary,
            )

    dispatcher_kwargs = {"config": config, "skills": skills}
    if tool_registry is not None:
        dispatcher_kwargs["tool_registry"] = tool_registry
    dispatcher = SubAgentDispatcher(**dispatcher_kwargs)

    # ``dispatch_many`` takes one shared payload — we approximate the
    # per-role payload by issuing the run individually but with the
    # bounded parallelism the dispatcher already implements. Keeping
    # the loop in-process so a single failure doesn't abort the whole
    # team.
    from concurrent.futures import ThreadPoolExecutor, TimeoutError, as_completed

    results: list[dict[str, Any]] = []
    failures: list[dict[str, Any]] = []
    workers = max(1, min(int(max_parallel or 4), len(role_names)))
    timeout_raw = args.get("timeout_s") or args.get("max_wall_seconds")
    if timeout_raw is None:
        try:
            timeout_raw = config.get("agent.team_run.timeout_s", 300)
        except Exception:
            timeout_raw = 300
    try:
        team_timeout_s = max(30.0, float(timeout_raw or 300))
    except Exception:
        team_timeout_s = 300.0
    common_event = {
        "call_id": call.id,
        "tool_call_id": call.id,
        "turn_id": call.turn_id,
        "team_run_id": team_run_id,
        "team_template": team_template,
        "session_id": session_id,
        "strategy_id": strategy_id,
        "trigger_event_id": trigger_event_id,
        "task": task,
        "goal": task,
        "output_language": output_language,
        "roles": role_names,
        "max_parallel": workers,
        "timeout_s": team_timeout_s,
        "collaboration_model": (
            "Agent Team run: each member is a subagent runtime with its "
            "own prompt/input/tool loop; team_run aggregates all member "
            "outputs into one committee result."
        ),
    }
    _publish_team_event("team.start", **common_event)
    pool = ThreadPoolExecutor(max_workers=workers, thread_name_prefix="team")
    futs = {
        pool.submit(
            dispatcher.dispatch,
            f"subagent:{r}",
            payload=role_payloads[r],
            trigger_event_id=trigger_event_id,
            strategy_id=strategy_id,
            session_id=session_id,
            turn_id=call.turn_id,
            parent_call_id=call.id,
        ): r
        for r in role_names
    }
    try:
        for role_name in role_names:
            _publish_team_event(
                "team.member.start",
                subagent=role_name,
                role=role_name,
                status="running",
                team_task_id=f"role-{role_name}",
                team_task_owner=role_name,
                team_task_subject=task,
                payload=redact_display_dict(role_payloads[role_name]),
                assignment_prompt=role_assignment_prompts[role_name],
                **common_event,
            )
        for fut in as_completed(futs, timeout=team_timeout_s):
            role_name = futs[fut]
            try:
                envelope = fut.result()
            except Exception as exc:
                failure = {
                    "subagent": role_name,
                    "error_kind": "execution_error",
                    "error": f"{type(exc).__name__}: {exc}",
                }
                failures.append(failure)
                _publish_team_event(
                    "team.member.end",
                    subagent=role_name,
                    role=role_name,
                    status="error",
                    ok=False,
                    error=failure["error"],
                    error_kind=failure["error_kind"],
                    **common_event,
                )
                continue
            output = envelope.get("output") or {}
            degraded = isinstance(output, dict) and bool(output.get("degraded"))
            ok = bool(envelope.get("ok", True)) and not degraded
            usd = float(envelope.get("usd") or 0.0)
            entry = {
                "subagent": role_name,
                "ok": ok,
                "tier": envelope.get("tier"),
                "tokens": envelope.get("tokens", 0),
                "usd": usd,
                "wall_ms": envelope.get("wall_ms", 0),
                "output": output,
                "metrics": envelope.get("metrics") or {},
                "steps": envelope.get("steps") or [],
                "error": envelope.get("error")
                or (output.get("summary") if degraded else None),
                "error_kind": envelope.get("error_kind")
                or (output.get("error_kind") if degraded else None),
            }
            if ok:
                results.append(entry)
            else:
                failures.append(entry)
            _publish_team_event(
                "team.member.end",
                subagent=role_name,
                role=role_name,
                status="completed" if ok else "error",
                ok=ok,
                error=entry.get("error"),
                error_kind=entry.get("error_kind"),
                tokens=entry.get("tokens"),
                usd=entry.get("usd"),
                wall_ms=entry.get("wall_ms"),
                team_task_id=f"role-{role_name}",
                team_task_owner=role_name,
                team_task_subject=task,
                output=redact_display_dict(entry.get("output") or {}),
                metrics=redact_display_dict(entry.get("metrics") or {}),
                **common_event,
            )
    except TimeoutError:
        for pending_fut, pending_name in list(futs.items()):
            if pending_fut.done():
                continue
            pending_fut.cancel()
            failure = {
                "subagent": pending_name,
                "error_kind": "timeout",
                "error": f"team_run timeout after {team_timeout_s:.0f}s",
            }
            failures.append(failure)
            _publish_team_event(
                "team.member.timeout",
                subagent=pending_name,
                role=pending_name,
                status="timeout",
                ok=False,
                error=failure["error"],
                error_kind=failure["error_kind"],
                **common_event,
            )
    finally:
        pool.shutdown(wait=False, cancel_futures=True)

    compact_results = [_compact_member_entry(r) for r in results]
    compact_failures = [_compact_member_entry(f) for f in failures]
    aggregated = aggregate(
        [{"subagent": r["subagent"], "output": r["output"]} for r in results]
    )
    compact_aggregated = _compact_json_value(aggregated, limit=16000)
    status = "completed" if not failures else "completed_with_failures"
    next_action = (
        "team_run is synchronous and already finished. Write the complete "
        "requested answer from results now. For research-report tasks, "
        "include the full report in this reply; do not ask whether the user "
        "wants details. Write the user-visible final answer in the original "
        f"user prompt language ({output_language}), translating team member "
        "outputs as needed. Translate headings, labels, and natural-language "
        "field names too "
        "while preserving proper nouns, tickers, source names, code "
        "identifiers, and URLs. team_run_id is not an async task_id; do not "
        "call task_get, task_output, task_list, or run team_run again for "
        "the same task unless you used an async subagent tool."
    )
    if failures:
        next_action = (
            "team_run is synchronous and already finished with failed or "
            "degraded members. Retry only the missing required analysis or "
            "state the evidence gap, then write the best possible report in "
            "this reply in the original user prompt language "
            f"({output_language}). Do not ask whether the user wants details. "
            "Translate team member outputs, headings, labels, and "
            "natural-language field names as needed while preserving proper "
            "nouns, tickers, source names, code identifiers, and URLs. "
            "team_run_id is not an async task_id; "
            "do not call task_get, task_output, task_list, or rerun the whole "
            "team."
        )
    summary = {
        "ok": not failures,
        "status": status,
        "team_run_id": team_run_id,
        "task": task,
        "output_language": output_language,
        "roles_requested": role_names,
        "roles_succeeded": [r["subagent"] for r in results],
        "roles_failed": [f["subagent"] for f in failures],
        "tokens_total": sum(int(r.get("tokens") or 0) for r in results),
        "usd_total": round(sum(float(r.get("usd") or 0.0) for r in results), 4),
        "results": compact_results,
        "failures": compact_failures,
        "aggregated": compact_aggregated,
        "next_action": next_action,
    }
    _publish_team_event(
        "team.end",
        ok=not failures,
        status="completed" if not failures else "completed_with_failures",
        roles_succeeded=summary["roles_succeeded"],
        roles_failed=summary["roles_failed"],
        tokens_total=summary["tokens_total"],
        usd_total=summary["usd_total"],
        results=redact_display_dict(compact_results),
        failures=redact_display_dict(compact_failures),
        aggregated=redact_display_dict(compact_aggregated),
        **common_event,
    )
    if not allow_additional_team_run:
        _remember_team_run_summary(guard_key, summary)
    return ToolResult.from_json(
        tool_use_id=call.id,
        name=call.name,
        data=summary,
    )


def role_list_handler(
    call: ToolCall,
    *,
    config: Config,
) -> ToolResult:
    return ToolResult.from_json(
        tool_use_id=call.id,
        name=call.name,
        data={
            "guidance": (
                "For public-company or stock research reports, prefer the "
                "default investment-research roles such as "
                "fundamentals_analyst, technical_analyst, sentiment_analyst, "
                "bull_researcher, bear_researcher, risk_critic, and "
                "research_manager. Workspace finance-ops roles like "
                "valuation_reviewer are domain-specific; use them only when "
                "the user asks for fund/GP valuation, accounting, KYC, "
                "pitch/deck, or model-building work."
            ),
            "recommended_stock_research_roles": [
                "fundamentals_analyst",
                "technical_analyst",
                "sentiment_analyst",
                "bull_researcher",
                "bear_researcher",
                "risk_critic",
                "research_manager",
            ],
            "roles": list_roles(config.paths),
        },
    )


def role_get_handler(
    call: ToolCall,
    *,
    config: Config,
) -> ToolResult:
    args = call.arguments or {}
    name = (args.get("name") or "").strip()
    if not name:
        return ToolResult.from_error(
            tool_use_id=call.id,
            name=call.name,
            error=ToolError(
                kind=ToolErrorKind.SCHEMA_VALIDATION,
                message="name is required",
            ),
        )
    record = describe_role(config.paths, name)
    if record is None:
        return ToolResult.from_error(
            tool_use_id=call.id,
            name=call.name,
            error=ToolError(
                kind=ToolErrorKind.NOT_FOUND,
                message=f"role not found: {name}",
                retryable=False,
            ),
        )
    return ToolResult.from_json(
        tool_use_id=call.id,
        name=call.name,
        data=record,
    )


def role_save_handler(
    call: ToolCall,
    *,
    config: Config,
) -> ToolResult:
    args = call.arguments or {}
    try:
        record = save_role(
            config.paths,
            name=(args.get("name") or "").strip(),
            prompt=args.get("prompt") or "",
            allowed_skills=list(args.get("allowed_skills") or []) or None,
            tier=args.get("tier"),
        )
    except (ValueError, OSError) as exc:
        return ToolResult.from_error(
            tool_use_id=call.id,
            name=call.name,
            error=ToolError(
                kind=ToolErrorKind.SCHEMA_VALIDATION,
                message=str(exc),
            ),
        )
    return ToolResult.from_json(
        tool_use_id=call.id,
        name=call.name,
        data=record,
    )


def role_delete_handler(
    call: ToolCall,
    *,
    config: Config,
) -> ToolResult:
    args = call.arguments or {}
    name = (args.get("name") or "").strip()
    if not name:
        return ToolResult.from_error(
            tool_use_id=call.id,
            name=call.name,
            error=ToolError(
                kind=ToolErrorKind.SCHEMA_VALIDATION,
                message="name is required",
            ),
        )
    try:
        deleted = delete_role(config.paths, name)
    except ValueError as exc:
        return ToolResult.from_error(
            tool_use_id=call.id,
            name=call.name,
            error=ToolError(
                kind=ToolErrorKind.SCHEMA_VALIDATION,
                message=str(exc),
            ),
        )
    return ToolResult.from_json(
        tool_use_id=call.id,
        name=call.name,
        data={"name": name, "deleted": bool(deleted)},
    )


__all__ = [
    "ROLE_DELETE_SCHEMA",
    "ROLE_GET_SCHEMA",
    "ROLE_LIST_SCHEMA",
    "ROLE_SAVE_SCHEMA",
    "SUBAGENT_LIST_SCHEMA",
    "SUBAGENT_RUN_SCHEMA",
    "TEAM_RUN_SCHEMA",
    "cached_team_run_summary_for_call",
    "role_delete_handler",
    "role_get_handler",
    "role_list_handler",
    "role_save_handler",
    "subagent_list_handler",
    "subagent_run_handler",
    "team_run_handler",
]
