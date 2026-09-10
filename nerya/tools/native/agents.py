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
import math
import time
import uuid
from pathlib import Path
from typing import Any

from ...core.config import Config
from ...core.redaction import redact_display_dict, redact_text
from ...skills.kernel import SkillKernel
from ...subagents.dispatcher import SubAgentDispatcher
from ...subagents.registry import (
    DEFAULT_SUBAGENT_SKILLS,
    DEFAULT_TIERS,
    GENERIC_ADHOC_SKILLS,
    build_inline_spec,
    canonical_subagent_name,
    delete_role,
    describe_role,
    generic_role_prompt,
    list_roles,
    load_registry,
    save_role,
)
from ...subagents.result_aggregator import aggregate
from ...teams.orchestrator import TeamOrchestrator, TeamRunRequest
from ...teams.store import TeamStore
from ...teams.templates import get_template
from ..approval_contracts import PERMISSION_PENDING_ERROR_KIND
from ..approval_runtime import nested_approval_pause_from_envelope
from ..result_contracts import TEAM_REPORT_RESULT_PROTOCOL
from ..types import (
    ToolCall,
    ToolError,
    ToolErrorKind,
    ToolResult,
)


def _call_meta(call: ToolCall, key: str) -> Any:
    meta = call.metadata if isinstance(call.metadata, dict) else {}
    return meta.get(key)


def _schema_error(call: ToolCall, message: str) -> ToolResult:
    return ToolResult.from_error(
        tool_use_id=call.id,
        name=call.name,
        error=ToolError(kind=ToolErrorKind.SCHEMA_VALIDATION, message=message),
    )


def _nested_permission_pending(envelope: dict[str, Any]) -> dict[str, Any] | None:
    """Render the shared child-native pause in the established recovery shape."""

    pause = nested_approval_pause_from_envelope(envelope)
    if pause is None:
        return None
    pending = pause.as_nested_dict()
    pending["payload"] = redact_display_dict(dict(pause.payload))
    if pause.recovery_hint:
        pending["recovery_hint"] = redact_display_dict(
            dict(pause.recovery_hint)
        )
    if pause.approval_request:
        pending["approval_request"] = redact_display_dict(
            dict(pause.approval_request)
        )
    return pending


def _build_inline_role_spec(
    config: Config,
    *,
    name: str,
    prompt: Any = None,
    allowed_skills: Any = None,
    tier: Any = None,
    provider: Any = None,
    model: Any = None,
    execution_policy: Any = None,
):
    """Build an ephemeral :class:`SubAgentSpec` from inline tool args, or None.

    Returns ``None`` when the caller supplied no inline role fields — the
    dispatcher then resolves ``name`` through the registry, which already
    synthesises a capable generic role for unknown names. When any inline
    field is present we honour it so the lead agent can define a *temporary*
    role on the fly (no ``role_save`` round-trip, nothing written to disk).
    """

    prompt_str = prompt.strip() if isinstance(prompt, str) else ""
    skills_list = (
        [str(s).strip() for s in allowed_skills if str(s).strip()]
        if isinstance(allowed_skills, list)
        else []
    )
    tier_str = tier.strip() if isinstance(tier, str) else ""
    provider_str = provider.strip() if isinstance(provider, str) else ""
    model_str = model.strip() if isinstance(model, str) else ""
    policy_dict = dict(execution_policy) if isinstance(execution_policy, dict) else {}
    if not (
        prompt_str or skills_list or tier_str or provider_str or model_str or policy_dict
    ):
        return None
    return build_inline_spec(
        config.paths, name=name, prompt=prompt_str or None,
        allowed_skills=skills_list if allowed_skills is not None else None,
        tier=tier_str or None, provider=provider_str or None,
        model=model_str or None, execution_policy=policy_dict or None,
    )


def _publish_team_event(kind: str, **payload: Any) -> None:
    try:
        from ...agent.streaming import get_default_bus

        get_default_bus().publish(kind, **payload)
    except Exception:
        pass


def _native_team_run_report(summary: dict[str, Any]) -> str:
    title = " ".join(str(summary.get("task") or "").split())[:160].strip()
    lines = [
        f"# {title}" if title else "# AgentTeam evidence",
        "",
    ]

    synthesis = _public_team_synthesis(summary)
    if synthesis:
        lines.extend(["", "## Synthesis", synthesis])

    role_lines: list[str] = []
    for row in (summary.get("results") if isinstance(summary.get("results"), list) else [])[:12]:
        if not isinstance(row, dict):
            continue
        role_lines.append(_public_team_role_line(row))
    for row in (summary.get("failures") if isinstance(summary.get("failures"), list) else [])[:12]:
        if not isinstance(row, dict):
            continue
        role_lines.append(_public_team_failure_line(row))
    if role_lines:
        lines.extend(["", "## Role findings", *[line for line in role_lines if line]])
    else:
        lines.extend([
            "",
            "## Role findings",
            "The team returned bounded evidence, but no role-level summary was available for final rendering.",
        ])
    return "\n".join(lines).strip() + "\n"


def _public_team_parse_jsonish(value: Any, *, depth: int = 0) -> Any:
    if depth >= 5:
        return value
    if isinstance(value, str):
        text = value.strip()
        if not text or text[0] not in "{[":
            return value
        try:
            parsed = json.loads(text)
        except Exception:
            return value
        return _public_team_parse_jsonish(parsed, depth=depth + 1)
    if isinstance(value, list):
        return [_public_team_parse_jsonish(item, depth=depth + 1) for item in value]
    if isinstance(value, dict):
        return {str(k): _public_team_parse_jsonish(v, depth=depth + 1) for k, v in value.items()}
    return value


_PUBLIC_TEAM_INTERNAL_KEYS = {
    "analysis_language",
    "call_id",
    "done",
    "error_kind",
    "metrics",
    "ok",
    "output_language",
    "payload",
    "raw",
    "raw_observations",
    "role",
    "skill_calls",
    "status",
    "task_id",
    "team_run_id",
    "tokens",
    "tools_used",
    "truncated",
    "usd",
}
_PUBLIC_TEAM_SUMMARY_KEYS = (
    "executive_summary",
    "summary",
    "conclusion",
    "recommendation",
    "direction",
    "bias",
    "thesis",
    "evidence",
    "narratives",
    "blockers",
    "risks",
    "data_gaps",
    "evidence_gaps",
)


def _public_team_clean(value: Any, *, depth: int = 0) -> Any:
    parsed = _public_team_parse_jsonish(value)
    if depth >= 5:
        return parsed
    if isinstance(parsed, dict):
        cleaned: dict[str, Any] = {}
        for key, child in parsed.items():
            normalized = str(key).lower().replace("-", "_")
            if normalized in _PUBLIC_TEAM_INTERNAL_KEYS:
                continue
            cleaned[str(key)] = _public_team_clean(child, depth=depth + 1)
        return {k: v for k, v in cleaned.items() if v not in (None, "", [], {})}
    if isinstance(parsed, list):
        return [
            child
            for item in parsed[:12]
            if (child := _public_team_clean(item, depth=depth + 1)) not in (None, "", [], {})
        ]
    return parsed


def _public_team_one_line(value: Any, *, limit: int = 700) -> str:
    cleaned = _public_team_clean(value)
    if cleaned in (None, "", [], {}):
        return ""
    if isinstance(cleaned, dict):
        parts: list[str] = []
        for key in _PUBLIC_TEAM_SUMMARY_KEYS:
            if key not in cleaned:
                continue
            rendered = _public_team_one_line(cleaned.get(key), limit=220)
            if rendered:
                parts.append(rendered)
            if len(parts) >= 4:
                break
        if not parts:
            for key, child in cleaned.items():
                rendered = _public_team_one_line(child, limit=220)
                if rendered:
                    parts.append(f"{str(key).replace('_', ' ')}: {rendered}")
                if len(parts) >= 4:
                    break
        text = "; ".join(parts)
    elif isinstance(cleaned, list):
        text = "; ".join(
            part for item in cleaned[:8] if (part := _public_team_one_line(item, limit=220))
        )
    else:
        text = " ".join(str(cleaned).split())
    if len(text) <= limit:
        return text
    return text[:limit].rstrip() + "..."


def _public_team_synthesis(summary: dict[str, Any]) -> str:
    aggregated = summary.get("aggregated")
    if isinstance(aggregated, dict):
        for container_key in ("subagents", "roles"):
            container = aggregated.get(container_key)
            if not isinstance(container, dict):
                continue
            fragments: list[str] = []
            for role, output in list(container.items())[:6]:
                rendered = _public_team_one_line(output, limit=320)
                if rendered:
                    fragments.append(f"- {role}: {rendered}")
            if fragments:
                return "\n".join(fragments)
    return _public_team_one_line(aggregated, limit=1000)


def _public_team_role_line(row: dict[str, Any]) -> str:
    role = str(row.get("subagent") or row.get("role") or "team_member").strip()
    output = row.get("output")
    summary = _public_team_one_line(output, limit=700)
    if not summary:
        summary = str(row.get("summary") or "").strip()
    if not summary:
        summary = "bounded evidence was collected, but this role did not produce a complete narrative"
    return f"### {role}\n{summary}"


def _public_team_failure_line(row: dict[str, Any]) -> str:
    role = str(row.get("subagent") or row.get("role") or "team_member").strip()
    detail = _public_team_one_line(row.get("output"), limit=500)
    if not detail:
        error_text = str(row.get("error") or row.get("summary") or "").strip().lower()
        if "timeout" in error_text:
            detail = "one team member did not complete its conclusion within the turn budget"
        elif error_text:
            detail = "one team member returned degraded output; diagnostic details are available in logs"
        else:
            detail = "one team member did not complete its conclusion in this turn"
    return f"### {role}\n{detail}"


_OUTPUT_LANGUAGE_KEYS = (
    "output_language",
    "target_language",
    "response_language",
    "preferred_language",
)
_ANALYSIS_LANGUAGE_KEYS = (
    "analysis_language",
    "internal_language",
    "working_language",
    "discussion_language",
    "reasoning_language",
)
_ROLE_WORKING_LANGUAGE_KEYS = (
    "language",
    "locale",
)


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
    raw_roles: list[Any],
    shared_payload: dict[str, Any],
) -> str:
    for source in (args, shared_payload):
        for key in _OUTPUT_LANGUAGE_KEYS:
            language = _explicit_output_language(source.get(key))
            if language:
                return language
    for entry in raw_roles:
        if not isinstance(entry, dict):
            continue
        for key in _OUTPUT_LANGUAGE_KEYS:
            language = _explicit_output_language(entry.get(key))
            if language:
                return language
        payload = entry.get("payload") if isinstance(entry.get("payload"), dict) else {}
        for key in _OUTPUT_LANGUAGE_KEYS:
            language = _explicit_output_language(payload.get(key))
            if language:
                return language
    return "the original user prompt language"


def _resolve_team_analysis_language(
    *,
    args: dict[str, Any],
    raw_roles: list[Any],
    shared_payload: dict[str, Any],
    output_language: str,
) -> str:
    for source in (args, shared_payload):
        for key in _ANALYSIS_LANGUAGE_KEYS:
            language = _explicit_output_language(source.get(key))
            if language:
                return language
        for key in _ROLE_WORKING_LANGUAGE_KEYS:
            language = _explicit_output_language(source.get(key))
            if language:
                return language
    for entry in raw_roles:
        if not isinstance(entry, dict):
            continue
        for key in _ANALYSIS_LANGUAGE_KEYS:
            language = _explicit_output_language(entry.get(key))
            if language:
                return language
        for key in _ROLE_WORKING_LANGUAGE_KEYS:
            language = _explicit_output_language(entry.get(key))
            if language:
                return language
        payload = entry.get("payload") if isinstance(entry.get("payload"), dict) else {}
        for key in _ANALYSIS_LANGUAGE_KEYS:
            language = _explicit_output_language(payload.get(key))
            if language:
                return language
        for key in _ROLE_WORKING_LANGUAGE_KEYS:
            language = _explicit_output_language(payload.get(key))
            if language:
                return language
    return output_language


def _seconds(value: Any, name: str) -> float:
    try:
        parsed = float(value)
        if not math.isfinite(parsed) or parsed < 0:
            raise ValueError
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError(f"{name} must be finite and non-negative") from exc
    return parsed


def _parent_remaining_wall_seconds(call: ToolCall) -> float | None:
    remaining = _call_meta(call, "remaining_wall_seconds")
    deadline = _call_meta(call, "turn_deadline_epoch")
    bounds = []
    if remaining is not None:
        bounds.append(_seconds(remaining, "remaining_wall_seconds"))
    if deadline is not None:
        bounds.append(max(0.0, _seconds(deadline, "turn_deadline_epoch") - time.time()))
    return min(bounds) if bounds else None


def _parent_final_reserve_seconds(call: ToolCall) -> float:
    value = _call_meta(call, "wall_time_final_synthesis_seconds")
    return _seconds(value, "wall_time_final_synthesis_seconds") if value is not None else 0.0


def _effective_team_timeout_seconds(
    *, args: dict[str, Any], config: Config,
    parent_remaining_wall_seconds: float | None = None,
    parent_final_reserve_seconds: float = 0.0,
) -> float:
    timeout = args.get("timeout_s")
    if timeout is None:
        timeout = config.get("agent.team_run.timeout_s", 300.0)
    bounds = [_seconds(timeout, "timeout_s"),
              _seconds(config.get("agent.team_run.max_timeout_s", 900.0), "max_timeout_s")]
    if parent_remaining_wall_seconds is not None:
        bounds.append(max(0.0, parent_remaining_wall_seconds - parent_final_reserve_seconds))
    return min(bounds)


def _positive_int(value: Any) -> int | None:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed > 0 else None


def _team_template_parallel_limit(
    team_template: str, paths: Any = None
) -> int | None:
    template_id = str(team_template or "").strip()
    if not template_id:
        return None
    template = get_template(template_id, paths)
    if template is None:
        return None
    return _positive_int(getattr(template, "max_parallel", None))


def _effective_team_workers(
    *,
    args: dict[str, Any],
    config: Config,
    role_count: int,
    team_template: str,
) -> int:
    if role_count <= 0:
        return 1
    requested = _positive_int(args.get("max_parallel"))
    template_limit = _team_template_parallel_limit(
        team_template, getattr(config, "paths", None)
    )
    configured_limit = _positive_int(config.get("agent.team_run.max_parallel"))
    if configured_limit is None:
        configured_limit = _positive_int(
            config.get("agent.subagents.max_parallel"),
        )
    if configured_limit is None:
        configured_limit = 4

    base = requested or template_limit or configured_limit
    cap = min(role_count, configured_limit)
    if template_limit is not None:
        cap = min(cap, template_limit)
    return max(1, min(base, cap))


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
        for key in ("error_kind", "reason", "retryable"):
            if rec.get(key) is not None:
                item[key] = rec.get(key)
        result = rec.get("result") if isinstance(rec.get("result"), dict) else {}
        delegated = result.get("data") if isinstance(result.get("data"), dict) else {}
        if delegated.get("subagent"):
            delegated_summary = {
                key: delegated.get(key)
                for key in (
                    "ok", "subagent", "tier", "provider", "model",
                    "tokens", "usd", "wall_ms", "error", "error_kind",
                    "capture_paths",
                )
                if delegated.get(key) is not None
            }
            delegated_summary["output"] = _compact_json_value(
                delegated.get("output") or {},
                limit=4000,
            )
            item["delegated_run"] = delegated_summary
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
        "provider": entry.get("provider"),
        "model": entry.get("model"),
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
    if entry.get("caveat"):
        out["caveat"] = entry.get("caveat")
    if isinstance(entry.get("permission_pending"), dict):
        out["permission_pending"] = dict(entry["permission_pending"])
    return out


def _member_has_substantive_evidence(output: Any) -> bool:
    """True when a member produced real, sourced findings.

    Used to soften the evidence gate: a member that gathered actual evidence
    (a populated ``evidence`` / ``sources`` list, or successful tool calls in
    ``data_coverage.tools_used``) is a *caveated success*, not a team failure —
    even if an evidence contract flags missing inputs. This is the common
    private-company / estimate-heavy case where some required figures (SEC
    filings, a public market snapshot) legitimately do not exist. Mere
    intermediate ``observations`` or tool *errors* do not count as evidence.
    """

    if not isinstance(output, dict):
        return False
    evidence = output.get("evidence")
    if isinstance(evidence, list):
        for item in evidence:
            if isinstance(item, dict):
                if str(
                    item.get("source")
                    or item.get("claim")
                    or item.get("url")
                    or ""
                ).strip():
                    return True
            elif isinstance(item, str) and item.strip():
                return True
    for key in ("sources", "citations", "references"):
        seq = output.get(key)
        if isinstance(seq, list):
            for s in seq:
                if isinstance(s, dict):
                    if str(s.get("source") or s.get("url") or s.get("title") or "").strip():
                        return True
                elif isinstance(s, str) and s.strip():
                    return True
    coverage = output.get("data_coverage")
    if isinstance(coverage, dict):
        tools_used = coverage.get("tools_used")
        if isinstance(tools_used, list) and any(
            isinstance(t, dict) and t.get("ok") for t in tools_used
        ):
            return True
    return False


def _member_soft_quality_kind(output: dict) -> str | None:
    """Return the degraded/partial/missing-evidence kind, or ``None`` if clean.

    Distilled from the contract + self-report flags. ``status == "failed"`` is
    excluded here because that is a *hard* failure, handled separately.
    """

    contract = output.get("evidence_contract")
    contract_status = ""
    contract_missing = None
    if isinstance(contract, dict):
        contract_status = str(contract.get("status") or "").strip()
        contract_missing = contract.get("missing_evidence")
    quality = str(output.get("quality") or "").strip()
    if contract_status in {"degraded", "partial"} or bool(contract_missing):
        return str(
            (contract.get("error_kind") if isinstance(contract, dict) else None)
            or output.get("error_kind")
            or "insufficient_research_evidence"
        )
    if bool(output.get("degraded")):
        return str(output.get("error_kind") or quality or "degraded_output")
    if quality == "tool_observation_fallback":
        return "tool_observation_fallback"
    if bool(output.get("partial")) or quality == "degraded_missing_evidence":
        return str(output.get("error_kind") or quality or "partial_output")
    return None


def _member_output_failure_kind(output: Any) -> str | None:
    if not isinstance(output, dict):
        return None
    contract = output.get("evidence_contract")
    # Hard failure: the contract explicitly failed -> no usable result, fail
    # regardless of any partial evidence.
    if isinstance(contract, dict) and str(contract.get("status") or "").strip() == "failed":
        return str(
            contract.get("error_kind")
            or output.get("error_kind")
            or "insufficient_research_evidence"
        )
    soft_kind = _member_soft_quality_kind(output)
    if soft_kind is None:
        return None
    # Caveated success: still produced substantive, sourced findings. Keep the
    # quality caveat on the output (so synthesis marks estimates) but do not
    # fail the member.
    if _member_has_substantive_evidence(output):
        return None
    return soft_kind


def _apply_member_evidence_contract(output: Any) -> Any:
    if not isinstance(output, dict):
        return output
    contract = output.get("evidence_contract")
    if not isinstance(contract, dict):
        return output
    missing = contract.get("missing_evidence")
    status = str(contract.get("status") or "").strip()
    if not missing and status not in {"degraded", "failed", "partial"}:
        return output
    merged = dict(output)
    if missing and "missing_evidence" not in merged:
        merged["missing_evidence"] = list(missing) if isinstance(missing, list) else missing
    merged.setdefault("quality", str(contract.get("quality") or "degraded_missing_evidence"))
    merged.setdefault(
        "error_kind",
        str(contract.get("error_kind") or "insufficient_research_evidence"),
    )
    merged.setdefault("partial", True)
    return merged


def _team_assignment_prompt(
    *,
    task: str,
    role_name: str,
    payload: dict[str, Any],
    instructions: str = "",
    output_language: str = "",
    analysis_language: str = "",
) -> str:
    lines = [
        "Agent Team member assignment",
        "",
        f"Team mission: {task}",
        f"Role: {role_name}",
    ]
    if output_language and analysis_language and analysis_language != output_language:
        lines.extend([
            "",
            "Language contract:",
            f"- Role analysis language: {analysis_language}.",
            f"- Final report language: {output_language}.",
            "- Write role analysis, evidence notes, role conclusions, and "
            "natural-language JSON values in the role analysis language.",
            "- The parent turn will synthesize the final user-facing report "
            "in the final report language.",
            "- Preserve JSON keys, enum values required by the role contract, "
            "proper nouns, tickers, source names, code identifiers, URLs, "
            "and numeric metrics in their original form.",
        ])
    elif output_language:
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
        "- Gather role-relevant source data before writing the role conclusion; "
        "if one source fails, try another visible capability or report the "
        "exact remaining gap.",
        "- For tool calls, use explicit fields from the mission, payload, "
        "or prior tool results; do not invent default markets, providers, "
        "wallets, or credentials.",
        "- For provider-specific data, prefer list/schema/capability "
        "discovery first, then call the concrete action returned by that "
        "tool result.",
        "- Do not fabricate data, fill missing evidence with placeholders, "
        "or claim unavailable sources were checked unless a tool result says so.",
        "",
        "Input payload:",
        json.dumps(redact_display_dict(payload), ensure_ascii=False, indent=2, default=str),
    ])
    return redact_text("\n".join(lines))


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
                "'coding_agent'). Prefer a registered spec (workspace "
                "<workspace>/subagents/<name>.agent.md or a "
                "DEFAULT_SUBAGENT_SKILLS key). If none fits, you may invent "
                "a new name and define it inline via the ``prompt`` / "
                "``allowed_skills`` fields below — no role_save needed. Even "
                "with no inline prompt, an unknown name runs as a capable "
                "generic researcher rather than failing."
            ),
        },
        "payload": {
            "type": "object",
            "description": (
                "Arbitrary JSON handed to the child runtime as the task "
                "payload. The child reads it under '=== task payload ==='."
            ),
        },
        "prompt": {
            "type": "string",
            "description": (
                "Optional inline role prompt (Markdown). Supply this to spin "
                "up a temporary ad-hoc role for this run without persisting "
                "it via role_save. Describe the role's expertise, output "
                "schema, and constraints. Ephemeral: not written to disk."
            ),
        },
        "allowed_skills": {
            "type": "array",
            "items": {"type": "string"},
            "description": (
                "Optional skills for an inline ad-hoc role. The dispatcher "
                "denylist still blocks live-trading / wallet surfaces. "
                "Defaults to a safe research/analysis set when omitted."
            ),
        },
        "tier": {
            "type": "string",
            "enum": ["light", "medium", "high"],
            "description": "Optional LLM tier for an inline ad-hoc role.",
        },
        "provider": {
            "type": "string",
            "description": (
                "Optional LLM provider override for this run (e.g. "
                "'openai', 'anthropic', 'deepseek'). Pair with ``model`` "
                "to pin the child to a custom model instead of the tier's "
                "default routing."
            ),
        },
        "model": {
            "type": "string",
            "description": (
                "Optional model id override for this run (e.g. "
                "'gpt-5-mini'). Used with the tier's provider unless "
                "``provider`` is also set."
            ),
        },
        "execution_policy": {
            "type": "object",
            "description": (
                "Optional declarative runtime policy for this ephemeral role: "
                "native_tools allow/deny lists, tool_argument_defaults, "
                "budgets, and an optional locked_tier."
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
    executor: Any = None,
) -> ToolResult:
    """Spawn one subagent and return its envelope."""

    args = call.arguments or {}
    name = (args.get("name") or "").strip()
    if not name:
        return _schema_error(call, "name is required")
    payload = args.get("payload") if isinstance(args.get("payload"), dict) else {}
    strategy_id = args.get("strategy_id") or _call_meta(call, "strategy_id") or None
    session_id = args.get("session_id") or _call_meta(call, "session_id") or None
    trigger_event_id = (
        args.get("trigger_event_id")
        or _call_meta(call, "trigger_event_id")
        or None
    )
    cancel_token = _call_meta(call, "cancel_token") or _call_meta(
        call, "cancellation_token"
    )
    parent_remaining_wall_seconds = _parent_remaining_wall_seconds(call)
    inline_spec = _build_inline_role_spec(
        config,
        name=name,
        prompt=args.get("prompt"),
        allowed_skills=args.get("allowed_skills"),
        tier=args.get("tier"),
        provider=args.get("provider"),
        model=args.get("model"),
        execution_policy=args.get("execution_policy"),
    )
    dispatcher = SubAgentDispatcher(
        config=config, skills=skills, tool_registry=tool_registry, executor=executor,
    )
    try:
        dispatch_kwargs: dict[str, Any] = {
            "trigger_event_id": trigger_event_id,
            "strategy_id": strategy_id,
            "session_id": session_id,
            "turn_id": call.turn_id,
            "parent_call_id": call.id,
            "inline_spec": inline_spec,
            "cancel_token": cancel_token,
        }
        if parent_remaining_wall_seconds is not None:
            dispatch_kwargs["max_wall_seconds"] = parent_remaining_wall_seconds
        envelope = dispatcher.dispatch(
            f"subagent:{name}",
            payload=payload or {},
            **dispatch_kwargs,
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

    nested_pending = _nested_permission_pending(envelope)
    if nested_pending is not None:
        approval_request = nested_pending.get("approval_request")
        recovery_hint = {
            key: value
            for key, value in nested_pending.items()
            if key != "approval_request"
        }
        pending_result = ToolResult.from_error(
            tool_use_id=call.id,
            name=call.name,
            error=ToolError(
                kind=ToolErrorKind.PERMISSION_PENDING,
                message=(
                    "child subagent requested approval before executing "
                    f"{nested_pending.get('tool_name') or 'a native tool'}"
                ),
                detail={"envelope": envelope},
                retryable=True,
                recovery_hint=recovery_hint,
            ),
        )
        if isinstance(approval_request, dict):
            pending_result.metadata["approval_request"] = dict(
                approval_request
            )
        return pending_result

    if not envelope.get("ok", True):
        # Preserve cooperative cancellation at the native-tool boundary.
        # A child runtime already records the canonical ``cancelled`` reason;
        # collapsing it into ``execution_error`` makes the parent loop retry
        # or display a generic failure instead of stopping the turn.
        envelope_error_kind = str(envelope.get("error_kind") or "").strip().lower()
        if envelope_error_kind == "cancelled":
            reason = str(envelope.get("error") or "cancelled")
            return ToolResult.from_error(
                tool_use_id=call.id,
                name=call.name,
                error=ToolError(
                    kind=ToolErrorKind.ABORTED,
                    message=reason,
                    detail={
                        "envelope": envelope,
                        "reason": reason,
                    },
                    retryable=False,
                    recovery_hint={"action": "cancelled", "reason": reason},
                ),
            )
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


RESEARCH_RUN_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "query": {
            "type": "string",
            "description": (
                "What to research on the public web (search-engine style "
                "query or a short mission statement)."
            ),
        },
        "queries": {
            "anyOf": [
                {"type": "string"},
                {
                    "type": "array",
                    "items": {
                        "anyOf": [
                            {"type": "string"},
                            {
                                "type": "object",
                                "properties": {
                                    "query": {"type": "string"},
                                    "type": {"type": "string"},
                                },
                                "required": ["query"],
                            },
                        ],
                    },
                },
            ],
            "description": (
                "Related research questions to combine into one delegated "
                "collection run. Prefer this over several separate calls."
            ),
        },
        "urls": {
            "type": "array",
            "items": {"type": "string"},
            "description": (
                "Specific URLs the researcher must fetch and capture in "
                "full (IR pages, filings, articles, posts)."
            ),
        },
        "instructions": {
            "type": "string",
            "description": (
                "Optional extra guidance: which sources to prefer, date "
                "ranges, fields to extract, output language, etc."
            ),
        },
        "payload": {
            "type": "object",
            "description": (
                "Optional extra payload fields merged into the researcher's "
                "task payload."
            ),
        },
        "strategy_id": {"type": "string"},
        "session_id": {"type": "string"},
        "trigger_event_id": {"type": "string"},
    },
}


def _delegated_capture_paths(
    envelope: dict[str, Any],
    *,
    workspace_root: Path | str | None,
) -> list[str]:
    """Return validated persisted captures produced by one delegated run.

    The child model's prose is not proof that collection happened. Only paths
    emitted by successful tool records count, and every path must resolve to an
    existing file inside the active workspace. This keeps the contract generic:
    any collector tool may emit ``saved_path`` or ``capture_paths`` without the
    delegation handler knowing its concrete tool name.
    """

    if workspace_root is None:
        return []
    root = Path(workspace_root).resolve()
    metrics = envelope.get("metrics") if isinstance(envelope.get("metrics"), dict) else {}
    records = metrics.get("skill_calls") if isinstance(metrics.get("skill_calls"), list) else []
    candidates: list[str] = []

    def _collect(value: Any) -> None:
        if isinstance(value, dict):
            saved = value.get("saved_path")
            if isinstance(saved, str) and saved.strip():
                candidates.append(saved.strip())
            paths = value.get("capture_paths")
            if isinstance(paths, list):
                candidates.extend(
                    str(item).strip()
                    for item in paths
                    if isinstance(item, str) and item.strip()
                )
            for nested in value.values():
                if isinstance(nested, (dict, list)):
                    _collect(nested)
        elif isinstance(value, list):
            for item in value:
                _collect(item)

    for record in records:
        if not isinstance(record, dict) or record.get("ok") is not True:
            continue
        result = record.get("result")
        if isinstance(result, dict):
            data = result.get("data")
            if isinstance(data, dict) and data.get("ok") is False:
                continue
            _collect(data)

    valid: list[str] = []
    for candidate in dict.fromkeys(candidates):
        path = Path(candidate)
        resolved = path.resolve() if path.is_absolute() else (root / path).resolve()
        try:
            relative = resolved.relative_to(root)
        except ValueError:
            continue
        if resolved.is_file():
            valid.append(relative.as_posix())
    return valid


def research_run_handler(
    call: ToolCall,
    *,
    config: Config,
    skills: SkillKernel,
    tool_registry: Any = None,
    executor: Any = None,
) -> ToolResult:
    """Delegate collection to the target declared on this tool descriptor."""

    args = call.arguments or {}
    query = str(args.get("query") or "").strip()

    def _query_text(item: Any) -> str:
        if isinstance(item, (str, bytes)):
            return str(item).strip()
        if isinstance(item, dict):
            for key in ("query", "request", "text"):
                value = item.get(key)
                if isinstance(value, (str, bytes)) and str(value).strip():
                    return str(value).strip()
        return ""

    queries_arg = args.get("queries")
    if isinstance(queries_arg, list):
        queries = [
            text for item in queries_arg if (text := _query_text(item))
        ]
    elif isinstance(queries_arg, (str, bytes)):
        query_text = _query_text(queries_arg)
        queries = [query_text] if query_text else []
    else:
        queries = []
    if queries:
        unique_queries = list(dict.fromkeys(queries))
        if query:
            query = "\n".join([
                query,
                "Related questions:",
                *(f"- {item}" for item in unique_queries if item != query),
            ]).strip()
        else:
            query = "\n".join([
                "Research all of the following in one collection run:",
                *(f"- {item}" for item in unique_queries),
            ])
    urls = [
        str(u).strip() for u in (args.get("urls") or [])
        if isinstance(u, (str, bytes)) and str(u).strip()
    ] if isinstance(args.get("urls"), list) else []
    if not query and not urls:
        return _schema_error(call, "query or urls is required")

    payload: dict[str, Any] = (
        dict(args.get("payload")) if isinstance(args.get("payload"), dict) else {}
    )
    if query:
        payload["query"] = query
    if urls:
        payload["urls"] = urls
    instructions = str(args.get("instructions") or "").strip()
    if instructions:
        payload["instructions"] = instructions
    try:
        descriptor = tool_registry.get(call.name) if tool_registry is not None else None
    except Exception as exc:
        return _schema_error(call, f"delegation descriptor unavailable: {exc}")
    target_name = str(getattr(descriptor, "delegates_to", "") or "").strip()
    if not target_name:
        return _schema_error(call, "delegation target is not configured")
    try:
        delegation_depth = max(0, int(_call_meta(call, "delegation_depth") or 0))
    except (TypeError, ValueError):
        delegation_depth = 0
    child_max_depth = getattr(descriptor, "child_max_depth", None)
    if child_max_depth is not None:
        try:
            if delegation_depth >= int(child_max_depth):
                return _schema_error(call, "delegation depth limit reached")
        except (TypeError, ValueError):
            return _schema_error(call, "delegation depth policy is invalid")

    strategy_id = args.get("strategy_id") or _call_meta(call, "strategy_id") or None
    session_id = args.get("session_id") or _call_meta(call, "session_id") or None
    trigger_event_id = (
        args.get("trigger_event_id")
        or _call_meta(call, "trigger_event_id")
        or None
    )
    cancel_token = _call_meta(call, "cancel_token") or _call_meta(
        call, "cancellation_token"
    )
    parent_remaining_wall_seconds = _parent_remaining_wall_seconds(call)

    dispatcher = SubAgentDispatcher(
        config=config, skills=skills, tool_registry=tool_registry, executor=executor,
    )

    try:
        dispatch_kwargs: dict[str, Any] = {
            "trigger_event_id": trigger_event_id,
            "strategy_id": strategy_id,
            "session_id": session_id,
            "turn_id": call.turn_id,
            "parent_call_id": call.id,
            "delegation_depth": delegation_depth + 1,
            "cancel_token": cancel_token,
        }
        if parent_remaining_wall_seconds is not None:
            dispatch_kwargs["max_wall_seconds"] = parent_remaining_wall_seconds
        envelope = dispatcher.dispatch(
            f"subagent:{target_name}",
            payload=payload,
            **dispatch_kwargs,
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
        envelope_error_kind = str(envelope.get("error_kind") or "").strip().lower()
        if envelope_error_kind == "cancelled":
            reason = str(envelope.get("error") or "cancelled")
            return ToolResult.from_error(
                tool_use_id=call.id,
                name=call.name,
                error=ToolError(
                    kind=ToolErrorKind.ABORTED,
                    message=reason,
                    detail={"envelope": envelope, "reason": reason},
                    retryable=False,
                    recovery_hint={"action": "cancelled", "reason": reason},
                ),
            )
        return ToolResult.from_error(
            tool_use_id=call.id,
            name=call.name,
            error=ToolError(
                kind=ToolErrorKind.EXECUTION_ERROR,
                message=str(envelope.get("error") or "delegated researcher failed"),
                detail={"envelope": envelope},
            ),
        )
    capture_paths = _delegated_capture_paths(
        envelope,
        workspace_root=getattr(getattr(config, "paths", None), "root", None),
    )
    if not capture_paths:
        return ToolResult.from_error(
            tool_use_id=call.id,
            name=call.name,
            error=ToolError(
                kind=ToolErrorKind.EXECUTION_ERROR,
                message="delegated researcher produced no persisted captures",
                detail={
                    "subagent": envelope.get("subagent"),
                    "tier": envelope.get("tier"),
                    "provider": envelope.get("provider"),
                    "model": envelope.get("model"),
                },
                retryable=False,
            ),
        )
    envelope["capture_paths"] = capture_paths
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
                "can orient before reading their own payload. Use only real "
                "operator-provided or tool-observed evidence. If a required "
                "API, webhook, feed, credential, or source body is missing, "
                "state that blocker in the mission and ask roles to report "
                "the evidence gap; do not invent mock, placeholder, synthetic, "
                "or proxy source content."
            ),
        },
        "roles": {
            "type": "array",
            "minItems": 1,
            "description": (
                "List of roles to spawn in parallel. Each entry is "
                "{name: <role>, payload: {...}}. Call ``role_list`` first "
                "when unsure which roles exist: registered personas ship "
                "with their own preloaded skills, tier, and output "
                "contract. When the task centres on a specific named "
                "persona (an investor, creator, or expert the user asked "
                "for by name), staff that persona's registered lane — one "
                "lane per persona — instead of letting a generic analyst "
                "lane impersonate several personas in one context. If no "
                "registered role matches the task, invent a descriptive "
                "name and define the role inline with ``prompt`` (and "
                "optional ``allowed_skills`` / ``tier``). No role_save is "
                "required and nothing is persisted. Even a bare unknown "
                "name runs as a capable generic researcher rather than "
                "failing. ``payload`` is merged on top of the shared "
                "``shared_payload``. A role payload field named "
                "``language`` or ``locale`` means the role's "
                "working/analysis language; use top-level ``output_language`` "
                "for the final user-visible report language. Pass this as a "
                "real JSON array, not a stringified JSON array."
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
                    "prompt": {
                        "type": "string",
                        "description": (
                            "Optional inline role prompt (Markdown). Supply "
                            "this to define a temporary ad-hoc role for this "
                            "run without role_save. Ephemeral: not written "
                            "to disk."
                        ),
                    },
                    "allowed_skills": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": (
                            "Optional skills for an inline ad-hoc role. The "
                            "dispatcher denylist still blocks live-trading / "
                            "wallet surfaces. Defaults to a safe "
                            "research/analysis set when omitted."
                        ),
                    },
                    "tier": {
                        "type": "string",
                        "enum": ["light", "medium", "high"],
                        "description": (
                            "Optional LLM tier for an inline ad-hoc role."
                        ),
                    },
                    "provider": {
                        "type": "string",
                        "description": (
                            "Optional LLM provider override for this role "
                            "(e.g. 'openai', 'anthropic'). Pair with "
                            "``model`` to pin the member to a custom model."
                        ),
                    },
                    "model": {
                        "type": "string",
                        "description": (
                            "Optional model id override for this role (e.g. "
                            "'gpt-5-mini'). Used with the tier's provider "
                            "unless ``provider`` is also set."
                        ),
                    },
                    "execution_policy": {
                        "type": "object",
                        "description": (
                            "Optional declarative runtime policy for this "
                            "ephemeral team member."
                        ),
                    },
                },
                "required": ["name"],
            },
        },
        "team_template": {
            "type": "string",
            "enum": [
                "ad_hoc_parallel_team",
                "market_analysis_team",
                "investment_committee_team",
                "strategy_design_team",
            ],
            "description": (
                "Optional explicit built-in team template. Set this only "
                "when the operator names a template or role_list/role_get "
                "evidence shows the template is the right match. Otherwise "
                "use ad_hoc_parallel_team with explicit roles."
            ),
        },
        "shared_payload": {
            "type": "object",
            "description": "Common payload merged into every role's payload.",
        },
        "output_language": {
            "type": "string",
            "description": (
                "Target language for the final user-visible team synthesis "
                "or report. Default is inferred from the team task and "
                "payload; set this from the latest user prompt when it "
                "explicitly asks for a final/report/output language."
            ),
        },
        "analysis_language": {
            "type": "string",
            "description": (
                "Optional language for team members' internal analysis, "
                "evidence notes, and role conclusions when the operator asks "
                "for a split-language workflow such as Chinese analysis with "
                "an English final report. Defaults to output_language."
            ),
        },
        "max_parallel": {"type": "integer", "minimum": 1},
        "timeout_s": {
            "type": "number", "minimum": 0,
            "description": "Team execution budget in seconds; zero expires immediately. Parent and configured limits still apply.",
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
        "provider": {
            "type": "string",
            "description": (
                "Optional LLM provider override persisted with the role "
                "(e.g. 'openai'). Empty keeps tier-default routing."
            ),
        },
        "model": {
            "type": "string",
            "description": (
                "Optional model id override persisted with the role "
                "(e.g. 'gpt-5-mini'). Empty keeps tier-default routing."
            ),
        },
        "execution_policy": {
            "type": "object",
            "description": (
                "Optional persisted runtime policy: native_tools allow/deny, "
                "tool_argument_defaults, budgets, and locked_tier."
            ),
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
    executor: Any = None,
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
        return _schema_error(call, "task is required (one-line shared mission)")

    raw_roles = args.get("roles")
    if not isinstance(raw_roles, list) or not raw_roles:
        return _schema_error(
            call,
            "roles must be a non-empty array of objects, e.g. "
            "[{\"name\":\"market_analyst\"}, {\"name\":\"risk_critic\"}]",
        )

    shared_payload = args.get("shared_payload") if isinstance(
        args.get("shared_payload"), dict,
    ) else {}
    output_language = _resolve_team_output_language(
        args=args,
        raw_roles=raw_roles,
        shared_payload=shared_payload,
    )
    analysis_language = _resolve_team_analysis_language(
        args=args,
        raw_roles=raw_roles,
        shared_payload=shared_payload,
        output_language=output_language,
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
    team_run_id = str(args.get("team_run_id") or "").strip()
    if not team_run_id:
        team_run_id = f"team-{uuid.uuid4().hex[:10]}"
    team_template = str(args.get("team_template") or "ad_hoc_parallel_team")
    try:
        delegation_depth = max(0, int(_call_meta(call, "delegation_depth") or 0))
    except (TypeError, ValueError):
        delegation_depth = 0
    role_names: list[str] = []
    role_payloads: dict[str, dict[str, Any]] = {}
    role_assignment_prompts: dict[str, str] = {}
    inline_specs: dict[str, Any] = {}
    for entry in raw_roles:
        if not isinstance(entry, dict):
            return _schema_error(call, "roles[*] must be objects")
        role_name = (entry.get("name") or "").strip()
        if not role_name:
            return _schema_error(call, "roles[*].name is required")
        if role_name in role_names:
            return _schema_error(call, f"duplicate role: {role_name!r}")
        role_names.append(role_name)
        merged = dict(shared_payload or {})
        per_role = entry.get("payload") if isinstance(entry.get("payload"), dict) else {}
        merged.update(per_role)
        merged.setdefault("output_language", output_language)
        merged.setdefault("analysis_language", analysis_language)
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
        inline_spec = _build_inline_role_spec(
            config,
            name=role_name,
            prompt=entry.get("prompt"),
            allowed_skills=entry.get("allowed_skills"),
            tier=entry.get("tier"),
            provider=entry.get("provider"),
            model=entry.get("model"),
            execution_policy=entry.get("execution_policy"),
        )
        if inline_spec is not None:
            inline_specs[role_name] = inline_spec
        role_assignment_prompts[role_name] = _team_assignment_prompt(
            task=task,
            role_name=role_name,
            payload=merged,
            instructions=instructions,
            output_language=output_language,
            analysis_language=analysis_language,
        )

    dispatcher = SubAgentDispatcher(
        config=config, skills=skills, tool_registry=tool_registry, executor=executor,
    )

    workers = _effective_team_workers(
        args=args,
        config=config,
        role_count=len(role_names),
        team_template=team_template,
    )
    timeout_args = dict(args)
    timeout_args["max_parallel"] = workers
    timeout_args["team_template"] = team_template
    parent_remaining_wall_seconds = _parent_remaining_wall_seconds(call)
    parent_final_reserve_seconds = _parent_final_reserve_seconds(call)
    uncapped_team_timeout_s = _effective_team_timeout_seconds(args=timeout_args, config=config)
    team_timeout_s = _effective_team_timeout_seconds(
        args=timeout_args,
        config=config,
        parent_remaining_wall_seconds=parent_remaining_wall_seconds,
        parent_final_reserve_seconds=parent_final_reserve_seconds,
    )
    timeout_capped_by_parent = team_timeout_s < uncapped_team_timeout_s
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
        "analysis_language": analysis_language,
        "roles": role_names,
        "max_parallel": workers,
        "timeout_s": team_timeout_s,
        "timeout_uncapped_s": uncapped_team_timeout_s,
        "timeout_capped_by_parent": timeout_capped_by_parent,
        "parent_remaining_wall_seconds": parent_remaining_wall_seconds,
        "parent_final_reserve_seconds": parent_final_reserve_seconds,
        "collaboration_model": (
            "Agent Team run: each member is a subagent runtime with its "
            "own prompt/input/tool loop; team_run aggregates all member "
            "outputs into one committee result."
        ),
    }
    _publish_team_event("team.start", **common_event)
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

    cancel_token = _call_meta(call, "cancel_token") or _call_meta(call, "cancellation_token")
    request = TeamRunRequest(
        task=task,
        template=team_template,
        roles=role_names,
        role_payloads=role_payloads,
        role_specs=inline_specs,
        role_assignment_prompts=role_assignment_prompts,
        shared_payload=shared_payload,
        output_language=output_language,
        analysis_language=analysis_language,
        trigger={"trigger_event_id": trigger_event_id, "payload": shared_payload},
        strategy_id=strategy_id,
        session_id=session_id,
        turn_id=call.turn_id,
        parent_call_id=call.id,
        delegation_depth=delegation_depth,
        run_id=team_run_id,
        max_parallel=workers,
        timeout_s=team_timeout_s,
        cancel_token=cancel_token,
        executor=executor,
    )
    try:
        result = TeamOrchestrator(
            config=config,
            skills=skills,
            dispatcher=dispatcher,
            tool_registry=tool_registry,
            executor=executor,
            cancel_token=cancel_token,
        ).run_request(request)
    except Exception as exc:
        return ToolResult.from_error(
            tool_use_id=call.id,
            name=call.name,
            error=ToolError(
                kind=ToolErrorKind.EXECUTION_ERROR,
                message=f"{type(exc).__name__}: {exc}",
            ),
        )

    results: list[dict[str, Any]] = []
    failures: list[dict[str, Any]] = []
    seen_roles: set[str] = set()
    pending_member: dict[str, Any] | None = None
    pending_approval_request: dict[str, Any] | None = None
    for raw in result.member_results:
        role_name = str(raw.get("subagent") or "team_member")
        seen_roles.add(role_name)
        output = _apply_member_evidence_contract(raw.get("output") or {})
        nested_pending = _nested_permission_pending(raw)
        if nested_pending is not None and pending_member is None:
            pending_approval = nested_pending.get("approval_request")
            if isinstance(pending_approval, dict):
                pending_approval_request = dict(pending_approval)
            pending_member = {
                **{
                    key: value
                    for key, value in nested_pending.items()
                    if key != "approval_request"
                },
                "subagent": role_name,
            }
        failure_kind = (
            PERMISSION_PENDING_ERROR_KIND
            if nested_pending is not None
            else _member_output_failure_kind(output)
        )
        ok = bool(raw.get("ok", True)) and failure_kind is None
        caveat_kind = _member_soft_quality_kind(output) if ok else None
        entry = {
            "subagent": role_name,
            "ok": ok,
            "tier": raw.get("tier"),
            "provider": raw.get("provider"),
            "model": raw.get("model"),
            "tokens": raw.get("tokens", 0),
            "usd": float(raw.get("usd") or 0.0),
            "wall_ms": raw.get("wall_ms", 0),
            "output": output,
            "metrics": raw.get("metrics") or {},
            "steps": raw.get("steps") or [],
            "error": raw.get("error") or (
                "approval required"
                if nested_pending is not None
                else output.get("summary") if failure_kind else None
            ),
            "error_kind": (
                PERMISSION_PENDING_ERROR_KIND
                if nested_pending is not None
                else raw.get("error_kind") or failure_kind
            ),
            "caveat": caveat_kind,
            "permission_pending": nested_pending,
        }
        (results if ok else failures).append(entry)
        _publish_team_event(
            "team.member.end",
            subagent=role_name,
            role=role_name,
            status=(
                "completed"
                if ok
                else "blocked" if nested_pending is not None else "error"
            ),
            ok=ok,
            caveat=caveat_kind,
            error=entry.get("error"),
            error_kind=entry.get("error_kind"),
            tokens=entry.get("tokens"),
            usd=entry.get("usd"),
            wall_ms=entry.get("wall_ms"),
            provider=entry.get("provider"),
            model=entry.get("model"),
            team_task_id=f"role-{role_name}",
            team_task_owner=role_name,
            team_task_subject=task,
            output=redact_display_dict(entry.get("output") or {}),
            metrics=redact_display_dict(entry.get("metrics") or {}),
            recovery=redact_display_dict(nested_pending) if nested_pending else None,
            **common_event,
        )
    for task_row in result.tasks:
        role_name = str(task_row.get("owner") or "")
        if not role_name or role_name in seen_roles:
            continue
        if str(task_row.get("status") or "") in {"failed", "blocked", "cancelled"}:
            failure = {
                "subagent": role_name,
                "ok": False,
                "error": task_row.get("error") or result.error or "team_member_not_completed",
                "error_kind": "cancelled" if task_row.get("status") == "cancelled" else "execution_error",
                "output": {},
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

    # The orchestrator records the raw dispatcher envelope. Apply the native
    # evidence contract to the durable task row as well so dashboard/API
    # consumers do not see a degraded member as successfully completed.
    paths = getattr(config, "paths", None)
    store = TeamStore(paths) if paths is not None else None
    if store is not None and failures:
        failed_by_role = {str(item.get("subagent")): item for item in failures}
        for task_row in store.list_tasks(team_run_id):
            failure = failed_by_role.get(task_row.owner)
            if failure is None or task_row.status != "completed":
                continue
            task_row.status = "failed"
            task_row.error = str(
                failure.get("error") or failure.get("error_kind") or "member_failed"
            )
            task_row.payload = {
                **(task_row.payload or {}),
                "output": failure.get("output") or {},
            }
            store.update_task(task_row)

    compact_results = [_compact_member_entry(r) for r in results]
    compact_failures = [_compact_member_entry(f) for f in failures]
    aggregated = aggregate(
        [{"subagent": r["subagent"], "output": r["output"]} for r in results]
    )
    compact_aggregated = _compact_json_value(aggregated, limit=16000)
    status = (
        "blocked" if pending_member is not None
        else "cancelled" if result.status == "cancelled"
        else "completed" if not failures and result.status == "completed"
        else "completed_with_failures"
    )
    next_action = (
        "This synchronous team call has returned. Use its evidence to decide "
        "the next permitted action required by the task; verify execution outcomes "
        "before declaring success. Report unresolved evidence gaps explicitly."
    )
    summary = {
        "ok": not failures,
        "status": status,
        "team_run_id": team_run_id,
        "team_template": team_template,
        "task": task,
        "output_language": output_language,
        "analysis_language": analysis_language,
        "roles_requested": role_names,
        "roles_succeeded": [r["subagent"] for r in results],
        "roles_failed": [f["subagent"] for f in failures],
        "roles_total": len(role_names),
        "max_parallel": workers,
        "timeout_s": team_timeout_s,
        "timeout_uncapped_s": uncapped_team_timeout_s,
        "timeout_capped_by_parent": timeout_capped_by_parent,
        "parent_remaining_wall_seconds": parent_remaining_wall_seconds,
        "parent_final_reserve_seconds": parent_final_reserve_seconds,
        "tokens_total": sum(int(r.get("tokens") or 0) for r in [*results, *failures]),
        "usd_total": sum(float(r.get("usd") or 0.0) for r in [*results, *failures]),
        "results": compact_results,
        "failures": compact_failures,
        "aggregated": compact_aggregated,
        "next_action": next_action,
        "orchestrator_status": result.status,
    }
    _publish_team_event(
        "team.end",
        ok=summary["ok"],
        status=status,
        roles_succeeded=summary["roles_succeeded"],
        roles_failed=summary["roles_failed"],
        tokens_total=summary["tokens_total"],
        usd_total=summary["usd_total"],
        results=redact_display_dict(compact_results),
        failures=redact_display_dict(compact_failures),
        aggregated=redact_display_dict(compact_aggregated),
        **common_event,
    )
    # The orchestrator already owns durable persistence. The native adapter
    # only updates the compatibility metrics/report shape consumed by older
    # dashboard clients.
    if store is not None:
        try:
            run_row = store.read_run(team_run_id)
            if run_row is not None:
                # Keep the durable lifecycle fail-closed when the native
                # evidence adapter downgrades a member after orchestration.
                # ``completed_with_failures`` remains the legacy tool summary
                # status; TeamRun only admits terminal statuses from its
                # durable state machine.
                failed_roles = {
                    str(item.get("subagent") or "")
                    for item in failures
                    if isinstance(item, dict)
                }
                required_failure = any(
                    str(task_row.get("owner") or "") in failed_roles
                    and bool(task_row.get("required", True))
                    for task_row in result.tasks
                    if isinstance(task_row, dict)
                )
                if failures and not result.tasks:
                    # A failed/blocked orchestrator may return no task
                    # envelope; treat native failures as required by default.
                    required_failure = True
                if result.status in {"failed", "blocked", "cancelled"}:
                    run_row.status = result.status
                elif result.status == "completed" and required_failure:
                    run_row.status = "blocked"
                if run_row.status in {"failed", "blocked", "cancelled"} and not run_row.error:
                    reason = str(result.error or "").strip()
                    if not reason:
                        reason = next(
                            (
                                str(item.get("error_kind") or "").strip()
                                for item in failures
                                if isinstance(item, dict)
                                and str(item.get("error_kind") or "").strip()
                            ),
                            "",
                        )
                    run_row.error = reason or f"team_run_{run_row.status}"
                run_row.metrics.update({
                    "roles_total": len(role_names),
                    "roles_succeeded": len(results),
                    "roles_failed": len(failures),
                    "max_parallel": workers,
                    "timeout_s": team_timeout_s,
                    "timeout_uncapped_s": uncapped_team_timeout_s,
                    "timeout_capped_by_parent": timeout_capped_by_parent,
                    "parent_remaining_wall_seconds": parent_remaining_wall_seconds,
                    "parent_final_reserve_seconds": parent_final_reserve_seconds,
                    "tokens_total": summary["tokens_total"],
                    "usd_total": summary["usd_total"],
                    "output_language": output_language,
                    "analysis_language": analysis_language,
                })
                store.write_synthesis_text(
                    team_run_id,
                    "final_report.md",
                    _native_team_run_report(summary),
                )
                store.update_run(run_row)
        except Exception:
            pass
    if pending_member is not None:
        pending_result = ToolResult.from_error(
            tool_use_id=call.id,
            name=call.name,
            error=ToolError(
                kind=ToolErrorKind.PERMISSION_PENDING,
                message=(
                    "team member requested approval before executing "
                    f"{pending_member.get('tool_name') or 'a native tool'}"
                ),
                detail={"team_run": summary},
                retryable=True,
                recovery_hint=pending_member,
            ),
        )
        if pending_approval_request is not None:
            pending_result.metadata["approval_request"] = dict(
                pending_approval_request
            )
        return pending_result
    return ToolResult.from_json(
        tool_use_id=call.id,
        name=call.name,
        data=summary,
        semantic_success=bool(summary.get("ok")),
        result_protocol=TEAM_REPORT_RESULT_PROTOCOL,
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
                "Role catalog only. Choose roles from their names, prompts, "
                "allowed skills, and the operator's actual request. Do not "
                "treat similarly named workspace roles as implicit routes; "
                "fetch role_get when a role's scope is unclear."
            ),
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
        return _schema_error(call, "name is required")
    record = describe_role(config.paths, name)
    if record is None:
        # No saved/default role matched. Instead of hard-failing with
        # ``not_found`` — which used to push the lead agent into a recovery
        # detour and frequently surfaced a scary "role not found" — synthesise
        # the exact capable generic ad-hoc role the dispatcher would run for
        # this name. ``role_get`` never blocks dispatch: the lead agent can use
        # this as-is, override ``prompt`` / ``allowed_skills`` inline on
        # team_run / subagent_run, or persist a reusable version via role_save.
        try:
            available = [
                str(role.get("name"))
                for role in list_roles(config.paths)
                if role.get("name")
            ]
        except Exception:
            available = []
        try:
            canonical = canonical_subagent_name(name)
        except Exception:
            canonical = name
        record = {
            "name": name,
            "tier": DEFAULT_TIERS.get(canonical, "medium"),
            "allowed_skills": list(GENERIC_ADHOC_SKILLS),
            "persistent": False,
            "source": "generated",
            "generated": True,
            "canonical_name": canonical or name,
            "prompt_path": None,
            "prompt": generic_role_prompt(name),
            "note": (
                f"No role named '{name}' is registered, so this is an "
                "auto-generated generic ad-hoc researcher (read-only research "
                "skills). Dispatch it directly via team_run / subagent_run — "
                "optionally pass an inline ``prompt`` / ``allowed_skills`` to "
                "tailor it, or call role_save to persist a reusable version. "
                "You do NOT need role_get to succeed before dispatching."
            ),
            "available_roles": available[:40],
        }
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
            provider=args.get("provider"),
            model=args.get("model"),
            execution_policy=args.get("execution_policy"),
        )
    except (ValueError, OSError) as exc:
        return _schema_error(call, str(exc))
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
        return _schema_error(call, "name is required")
    try:
        deleted = delete_role(config.paths, name)
    except ValueError as exc:
        return _schema_error(call, str(exc))
    return ToolResult.from_json(
        tool_use_id=call.id,
        name=call.name,
        data={"name": name, "deleted": bool(deleted)},
    )


__all__ = [
    "RESEARCH_RUN_SCHEMA",
    "ROLE_DELETE_SCHEMA",
    "ROLE_GET_SCHEMA",
    "ROLE_LIST_SCHEMA",
    "ROLE_SAVE_SCHEMA",
    "SUBAGENT_LIST_SCHEMA",
    "SUBAGENT_RUN_SCHEMA",
    "TEAM_RUN_SCHEMA",
    "research_run_handler",
    "role_delete_handler",
    "role_get_handler",
    "role_list_handler",
    "role_save_handler",
    "subagent_list_handler",
    "subagent_run_handler",
    "team_run_handler",
]
