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


def _team_assignment_prompt(
    *,
    task: str,
    role_name: str,
    payload: dict[str, Any],
    instructions: str = "",
) -> str:
    lines = [
        "Agent Team member assignment",
        "",
        f"Team mission: {task}",
        f"Role: {role_name}",
    ]
    if instructions:
        lines.extend(["", "Role-specific instructions:", instructions])
    lines.extend([
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

    dispatcher = SubAgentDispatcher(config=config, skills=skills)
    try:
        envelope = dispatcher.dispatch(
            f"subagent:{name}",
            payload=payload or {},
            trigger_event_id=trigger_event_id,
            strategy_id=strategy_id,
            session_id=session_id,
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
        "max_parallel": {"type": "integer", "minimum": 1},
        "usd_budget": {
            "type": "number",
            "description": (
                "Optional shared budget. Once spent, no further roles "
                "start; in-flight roles finish."
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
    usd_budget = args.get("usd_budget")
    if usd_budget is not None:
        try:
            usd_budget = float(usd_budget)
        except Exception:
            usd_budget = None

    team_run_id = str(args.get("team_run_id") or "").strip()
    if not team_run_id:
        team_run_id = f"team-{uuid.uuid4().hex[:10]}"
    team_template = str(args.get("team_template") or "ad_hoc_parallel_team")
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
        )

    dispatcher = SubAgentDispatcher(config=config, skills=skills)

    # ``dispatch_many`` takes one shared payload — we approximate the
    # per-role payload by issuing the run individually but with the
    # bounded parallelism the dispatcher already implements. Keeping
    # the loop in-process so a single failure doesn't abort the whole
    # team.
    from concurrent.futures import ThreadPoolExecutor, as_completed

    results: list[dict[str, Any]] = []
    failures: list[dict[str, Any]] = []
    spent = 0.0

    workers = max(1, min(int(max_parallel or 4), len(role_names)))
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
        "roles": role_names,
        "max_parallel": workers,
        "collaboration_model": (
            "Agent Team run: each member is a subagent runtime with its "
            "own prompt/input/tool loop; team_run aggregates all member "
            "outputs into one committee result."
        ),
    }
    _publish_team_event("team.start", **common_event)
    with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="team") as pool:
        futs = {
            pool.submit(
                dispatcher.dispatch,
                f"subagent:{r}",
                payload=role_payloads[r],
                trigger_event_id=trigger_event_id,
                strategy_id=strategy_id,
                session_id=session_id,
            ): r
            for r in role_names
        }
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
        for fut in as_completed(futs):
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
            ok = bool(envelope.get("ok", True))
            usd = float(envelope.get("usd") or 0.0)
            spent += usd
            entry = {
                "subagent": role_name,
                "ok": ok,
                "tier": envelope.get("tier"),
                "tokens": envelope.get("tokens", 0),
                "usd": usd,
                "wall_ms": envelope.get("wall_ms", 0),
                "output": envelope.get("output") or {},
                "metrics": envelope.get("metrics") or {},
                "steps": envelope.get("steps") or [],
                "error": envelope.get("error"),
                "error_kind": envelope.get("error_kind"),
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
            if usd_budget is not None and spent >= usd_budget:
                # Cancel any pending roles; we still surface them as
                # budget skips so the caller sees the full plan.
                for pending_fut, pending_name in list(futs.items()):
                    if pending_fut.done() or pending_fut is fut:
                        continue
                    if pending_fut.cancel():
                        failure = {
                            "subagent": pending_name,
                            "error_kind": "budget",
                            "error": (
                                f"team usd budget {usd_budget} reached; "
                                "skipped this role"
                            ),
                        }
                        failures.append(failure)
                        _publish_team_event(
                            "team.member.skip",
                            subagent=pending_name,
                            role=pending_name,
                            status="skipped",
                            ok=False,
                            error=failure["error"],
                            error_kind=failure["error_kind"],
                            **common_event,
                        )

    aggregated = aggregate(
        [{"subagent": r["subagent"], "output": r["output"]} for r in results]
    )
    summary = {
        "task": task,
        "roles_requested": role_names,
        "roles_succeeded": [r["subagent"] for r in results],
        "roles_failed": [f["subagent"] for f in failures],
        "tokens_total": sum(int(r.get("tokens") or 0) for r in results),
        "usd_total": round(spent, 4),
        "results": results,
        "failures": failures,
        "aggregated": aggregated,
    }
    _publish_team_event(
        "team.end",
        ok=not failures,
        status="completed" if not failures else "completed_with_failures",
        roles_succeeded=summary["roles_succeeded"],
        roles_failed=summary["roles_failed"],
        tokens_total=summary["tokens_total"],
        usd_total=summary["usd_total"],
        results=redact_display_dict(results),
        failures=redact_display_dict(failures),
        aggregated=redact_display_dict(aggregated),
        **common_event,
    )
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
        data={"roles": list_roles(config.paths)},
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
    "role_delete_handler",
    "role_get_handler",
    "role_list_handler",
    "role_save_handler",
    "subagent_list_handler",
    "subagent_run_handler",
    "team_run_handler",
]
