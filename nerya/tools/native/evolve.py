"""Native learning tools: scoped evidence review, memory updates and proposals.

Reflection collects evidence and can retain a justified conclusion in the
built-in store. Code, strategy and policy changes remain separate proposals
subject to the existing validation and approval gates.
"""

from __future__ import annotations

from typing import Any

from ...core.config import Config
from ...core.errors import ProtectedScopeViolation
from ...evolution import runner as evolution_runner
from ...evolution.patch_proposal import create_proposal, list_proposals
from ...evolution.post_apply_observation import record_post_apply_observation
from ...evolution.self_config import propose_core_config_patch
from ...evolution.skill_proposal import _coerce_lines, _render_skill_md
from ...skills import management as skill_management
from ...skills.manifest import _slugify
from ..tool_errors import schema_validation_result
from ..types import (
    ToolCall,
    ToolError,
    ToolErrorKind,
    ToolResult,
)


EVOLVE_REFLECT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "conclusion": {"type": "string", "description": "Optional evidence-backed lesson after reviewing a prior reflection packet. No code or policy is applied."},
        "key": {"type": "string", "description": "Stable fact key; reuse it when correcting an earlier lesson."},
        "evidence_sha256": {"type": "string", "description": "Exact digest returned by the reflection you reviewed. A changed packet requires a new review."},
        "expected_memory_id": {"type": "string", "description": "Current memory id when correcting a recalled lesson."},
    },
}

EVOLVE_PROPOSALS_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "proposal_id": {
            "type": "string",
            "description": (
                "Optional exact proposal id lookup. When set, searches all "
                "proposals and ignores limit."
            ),
        },
        "limit": {
            "type": "integer",
            "minimum": 1,
            "default": 20,
            "description": "Max proposals to enumerate (most recent first).",
        },
    },
}

SKILL_MANAGE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "action": {
            "type": "string",
            "enum": ["save", "enable", "disable", "delete"],
            "default": "save",
            "description": "Direct Skill mutation. save creates/updates a reusable workflow.",
        },
        "skill_id": {
            "type": "string",
            "description": "Exact Skill id for enable, disable, or delete.",
        },
        "name": {
            "type": "string",
            "description": "Human-readable skill name. It is slugified for the target skill id.",
        },
        "description": {
            "type": "string",
            "description": "Short trigger-oriented description for the SKILL.md frontmatter.",
        },
        "workflow": {
            "description": "Captured workflow steps as a string or array of strings.",
            "oneOf": [
                {"type": "string"},
                {"type": "array", "items": {"type": "string"}},
            ],
        },
        "triggers": {
            "description": "When future agents should load this skill.",
            "oneOf": [
                {"type": "string"},
                {"type": "array", "items": {"type": "string"}},
            ],
        },
        "evidence_refs": {
            "description": "Files, commands, tickets, session ids, or logs that justify the workflow.",
            "oneOf": [
                {"type": "string"},
                {"type": "array", "items": {"type": "string"}},
            ],
        },
        "gotchas": {
            "description": "Known pitfalls to include in the generated skill.",
            "oneOf": [
                {"type": "string"},
                {"type": "array", "items": {"type": "string"}},
            ],
        },
        "script_notes": {
            "description": "Helper scripts that should eventually live under scripts/.",
            "oneOf": [
                {"type": "string"},
                {"type": "array", "items": {"type": "string"}},
            ],
        },
        "reference_notes": {
            "description": "Reference docs that should eventually live under references/.",
            "oneOf": [
                {"type": "string"},
                {"type": "array", "items": {"type": "string"}},
            ],
        },
        "update_existing": {
            "type": "boolean",
            "default": False,
            "description": "Allow this direct save to replace an existing workspace skill.",
        },
    },
    "anyOf": [
        {"required": ["name", "description", "workflow"]},
        {
            "required": ["action", "skill_id"],
            "properties": {"action": {"enum": ["enable", "disable", "delete"]}},
        },
    ],
}

# Historical import compatibility only. The active native tool is skill_manage.
EVOLVE_SKILL_PROPOSAL_SCHEMA = SKILL_MANAGE_SCHEMA

EVOLVE_CORE_CONFIG_PATCH_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "target": {
            "type": "string",
            "description": (
                "Workspace config file to propose, for example nerya.yml, "
                "agents.yml, workspace.yml, news_feeds.yml, "
                "messages/channels.yml, triggers/routes.yml, "
                "ui/workspace.yml (or legacy workspace/ui.yml), "
                "policies/planner.yml, or policies/tier_policy.yml. "
                "Skill mutations are not config proposals; use skill_manage."
            ),
        },
        "summary": {
            "type": "string",
            "description": "Concise operator-facing summary of the config change.",
        },
        "config_after": {
            "type": "object",
            "description": (
                "Parsed YAML object after the proposed change. For nerya.yml, "
                "you may provide only the non-protected sections being changed; "
                "omitted top-level sections are preserved from the existing "
                "file, not reset. MCP exposure/OAuth connection management "
                "is separate from Agent chat and never authorized by this tool; "
                "risk/signers/secrets "
                "remain blocked. The live file is not mutated. For messages/channels.yml "
                "or messages/channels.yaml, use the canonical shape "
                "`channels: {<id>: {kind: telegram|discord|webhook, ...}}` plus "
                "top-level `severity_routes: {info: [telegram], critical: "
                "[telegram, discord], silent: []}` for severity-based routing. "
                "For Telegram, store bot tokens as `bot_token_ref`; if the "
                "operator provides a vault-backed chat id, use `chat_id_ref`, "
                "otherwise use plaintext numeric `chat_id`. "
                "Do not use ad-hoc targets such as notifications.routing. For "
                "ui/workspace.yml, send a full declarative manifest with "
                "version: 1, home.widgets, and pages; widgets must use only "
                "the read-only catalog kinds returned by GET /workspace/ui "
                "(kpi, metric, chart, market_ticker, portfolio, "
                "strategy_table, table, attention, markdown, link, skill_panel, "
                "or agent_panel). Never include HTML, JavaScript, iframe, "
                "remote URLs, or executable component fields."
            ),
        },
        "rationale": {
            "type": "string",
            "description": "Optional markdown rationale and review notes.",
        },
    },
    "required": ["target", "summary", "config_after"],
}

EVOLVE_PROVIDER_PROPOSAL_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "venue": {
            "type": "string",
            "description": "Stable provider/venue id, for example aster or hyperliquid_perpetual.",
        },
        "label": {
            "type": "string",
            "description": "Human-readable provider label.",
        },
        "kind": {
            "type": "string",
            "description": "Provider class such as cex, dex, perp, data_source, or wallet.",
        },
        "runtime": {
            "type": "string",
            "description": "Proposed runtime adapter type, for example python, python_ccxt, or custom_http.",
        },
        "base_url": {
            "type": "string",
            "description": "Primary REST/API base URL from the provider docs.",
        },
        "docs_url": {
            "type": "string",
            "description": "Canonical provider API documentation URL.",
        },
        "auth": {
            "type": "string",
            "description": "Authentication/signing model, for example EIP-712 Agent Key.",
        },
        "summary": {
            "type": "string",
            "description": "Operator-facing summary of the provider proposal.",
        },
        "rationale": {
            "type": "string",
            "description": "Markdown rationale and evidence notes.",
        },
        "evidence_refs": {
            "description": "URLs, files, or log refs used as evidence.",
            "oneOf": [
                {"type": "string"},
                {"type": "array", "items": {"type": "string"}},
            ],
        },
        "metadata": {
            "type": "object",
            "description": "Additional non-secret provider metadata to attach to proposal.yml.",
        },
    },
    "required": ["venue"],
}

EVOLVE_POST_APPLY_OBSERVATION_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "proposal_id": {
            "type": "string",
            "description": "Applied proposal id to observe.",
        },
        "status": {
            "type": "string",
            "description": (
                "Observation status: healthy, stable, improved, passed, ok, "
                "regressed, failed, degraded, rollback_recommended, pending, "
                "or observing. If omitted, Nerya derives it from backtest_result "
                "when possible."
            ),
        },
        "summary": {
            "type": "string",
            "description": "Short operator-facing observation summary.",
        },
        "source": {
            "type": "string",
            "description": "Evidence source such as backtest, paper, live, validation, or manual.",
        },
        "observed_at": {
            "type": "string",
            "description": "Optional ISO timestamp for the observation.",
        },
        "evidence_refs": {
            "description": "Evidence refs supporting the observation.",
            "oneOf": [
                {"type": "string"},
                {"type": "array", "items": {"type": "string"}},
            ],
        },
        "metrics": {
            "type": "object",
            "description": "Structured paper/live/backtest metrics observed after apply.",
        },
        "backtest_result": {
            "type": "object",
            "description": "Backtest runner result used for status derivation and audit.",
        },
        "run_id": {
            "type": "string",
            "description": "Optional paper/live/backtest run id.",
        },
        "operator": {
            "type": "string",
            "description": "Optional human or system actor recording the observation.",
        },
        "metadata": {
            "type": "object",
            "description": "Additional non-secret observation metadata.",
        },
    },
    "required": ["proposal_id"],
}


def _string_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [value] if value.strip() else []
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    return [str(value).strip()] if str(value).strip() else []


def _provider_proposal_markdown(args: dict[str, Any]) -> str:
    fields = [
        ("venue", args.get("venue")),
        ("label", args.get("label")),
        ("kind", args.get("kind")),
        ("runtime", args.get("runtime")),
        ("base_url", args.get("base_url")),
        ("docs_url", args.get("docs_url")),
        ("auth", args.get("auth")),
    ]
    lines = ["# Provider Proposal", ""]
    for key, value in fields:
        text = str(value or "").strip()
        if text:
            lines.append(f"- {key}: {text}")
    evidence = _string_list(args.get("evidence_refs"))
    if evidence:
        lines.extend(["", "## Evidence"])
        lines.extend(f"- {item}" for item in evidence)
    rationale = str(args.get("rationale") or "").strip()
    if rationale:
        lines.extend(["", "## Rationale", rationale])
    return "\n".join(lines) + "\n"


def evolve_reflect_handler(call: ToolCall, *, config: Config, strategy_id: str | None = None,
                           workflow_id: str | None = None, session_id: str | None = None,
                           actor_id: str = "default") -> ToolResult:
    """Run a reflection tick and return the new proposal envelope."""

    try:
        result = evolution_runner.evolve(config, strategy_id=strategy_id, isolated=True)
        from ...memory.runtime import MemoryRuntime
        from ...memory.learning import remember_review
        memory = MemoryRuntime(config, actor_id=actor_id or "default", strategy_id=strategy_id or "",
                               workflow_id=workflow_id or "", session_id=session_id or "")
        result["memory_context"] = [
            {"memory_id": hit.memory_id, "key": hit.stable_key, "scope": hit.scope, "content": hit.content}
            for hit in memory.recall("", limit=8)
        ]
        if str((call.arguments or {}).get("conclusion") or "").strip():
            result["learning"] = remember_review(memory, result.get("reflection") or {}, call.arguments or {})
    except Exception as exc:
        return ToolResult.from_error(
            tool_use_id=call.id,
            name=call.name,
            error=ToolError(
                kind=ToolErrorKind.EXECUTION_ERROR,
                message=f"{type(exc).__name__}: {exc}",
            ),
        )
    proposal = result.get("proposal") or {}
    ranked = result.get("ranked") or []
    signals = result.get("signals") or []
    selected_assets = result.get("selected_assets") or {}
    return ToolResult.from_json(
        tool_use_id=call.id,
        name=call.name,
        data={
            "reflection": result.get("reflection"),
            "status": result.get("status"),
            "memory_context": result.get("memory_context", []),
            "learning": result.get("learning"),
            "next_action": result.get("next_action"),
            "proposal": proposal,
            "ranked_seeds": ranked[:10],
            "seed_count": len(ranked),
            "signals": signals,
            "signal_count": len(signals),
            "selected_assets": selected_assets,
            "event": result.get("event"),
            "validation_plan_id": proposal.get("validation_plan_id"),
        },
    )


def evolve_proposals_handler(call: ToolCall, *, config: Config, strategy_id: str | None = None) -> ToolResult:
    """List pending proposals under ``evolution/proposals/``.

    Reads through :func:`nerya.evolution.patch_proposal.list_proposals`
    so the metadata format (``proposal.yml``) stays the single source of
    truth — we only re-render the summary the model needs.
    """

    args = call.arguments or {}
    limit = max(1, int(args.get("limit") or 20))
    try:
        proposals = list_proposals(config.paths)
        if strategy_id:
            proposals = [p for p in proposals if (p.metadata or {}).get("strategy_id") == strategy_id
                         or str(p.target or "").startswith(f"strategies/{strategy_id}/")]
    except Exception as exc:
        return ToolResult.from_error(
            tool_use_id=call.id,
            name=call.name,
            error=ToolError(
                kind=ToolErrorKind.EXECUTION_ERROR,
                message=f"{type(exc).__name__}: {exc}",
            ),
        )
    proposal_id = str(args.get("proposal_id") or "").strip()
    if proposal_id:
        match = next((p for p in proposals if p.id == proposal_id), None)
        if match is None:
            return ToolResult.from_json(
                tool_use_id=call.id,
                name=call.name,
                data={
                    "found": False,
                    "proposal_id": proposal_id,
                    "count": 0,
                    "proposal": None,
                    "proposals": [],
                },
            )
        item = {
            "id": match.id,
            "kind": match.kind,
            "state": match.state,
            "summary": match.summary,
            "ts": match.ts,
            "target": match.target,
            "path": str(match.path),
        }
        return ToolResult.from_json(
            tool_use_id=call.id,
            name=call.name,
            data={
                "found": True,
                "proposal_id": proposal_id,
                "count": 1,
                "proposal": item,
                "proposals": [item],
            },
        )
    proposals = sorted(
        proposals,
        key=lambda p: p.ts or "",
        reverse=True,
    )[:limit]
    out: list[dict[str, Any]] = [
        {
            "id": p.id,
            "kind": p.kind,
            "state": p.state,
            "summary": p.summary,
            "ts": p.ts,
            "target": p.target,
            "path": str(p.path),
        }
        for p in proposals
    ]
    return ToolResult.from_json(
        tool_use_id=call.id,
        name=call.name,
        data={"count": len(out), "proposals": out},
    )


def skill_manage_handler(
    call: ToolCall,
    *,
    config: Config,
    skill_index=None,
    skill_kernel=None,
) -> ToolResult:
    """Create/update/enable/disable/delete Workspace Skills immediately."""
    args = call.arguments or {}
    try:
        requested_action = str(args.get("action") or "save").strip().lower()
        if requested_action in {"enable", "disable", "delete"}:
            skill_id = str(args.get("skill_id") or "").strip()
            if not skill_id:
                raise ValueError("skill_id is required")
            catalog = skill_management.catalog(config, scope="workspace", query=skill_id, limit=200)
            existing = next((row for row in catalog["skills"] if row["id"] == skill_id), None)
            if existing is None:
                raise ValueError(f"workspace skill not found: {skill_id}")
            if requested_action in {"enable", "disable"}:
                result = skill_management.manage(
                    config,
                    requested_action,
                    skill_id,
                    scope="workspace",
                    revision=catalog["enabled_revision"],
                )
            else:
                if existing["enabled"]:
                    skill_management.manage(
                        config,
                        "disable",
                        skill_id,
                        scope="workspace",
                        revision=catalog["enabled_revision"],
                    )
                current = skill_management.read(config, skill_id, scope="workspace")
                result = skill_management.manage(
                    config,
                    "delete",
                    skill_id,
                    scope="workspace",
                    revision=current["revision"],
                )
            if skill_kernel is not None:
                skill_kernel.reload()
            if skill_index is not None:
                skill_index.reload()
            result.update({"tool": "skill_manage", "requested_action": requested_action})
            return ToolResult.from_json(tool_use_id=call.id, name=call.name, data=result)

        if requested_action != "save":
            raise ValueError(f"unsupported skill action: {requested_action}")
        raw_name = str(args.get("name") or "").strip()
        description = str(args.get("description") or "").strip()
        if not raw_name:
            raise ValueError("name is required")
        if len(raw_name) > 120:
            raise ValueError("name is too long")
        if not description:
            raise ValueError("description is required")
        skill_id = _slugify(raw_name)
        if skill_id in {"installed", "pending", "rejected", "enabled", "trust"}:
            raise ValueError(f"reserved skill id: {skill_id}")

        content = _render_skill_md(
            name=raw_name,
            description=description,
            workflow=_coerce_lines(args.get("workflow")),
            triggers=_coerce_lines(args.get("triggers")),
            gotchas=_coerce_lines(args.get("gotchas")),
            script_notes=_coerce_lines(args.get("script_notes")),
            reference_notes=_coerce_lines(args.get("reference_notes")),
        )
        catalog = skill_management.catalog(config, scope="workspace", query=skill_id, limit=200)
        existing = next((row for row in catalog["skills"] if row["id"] == skill_id), None)
        if existing and not bool(args.get("update_existing") or False):
            raise FileExistsError(f"workspace skill already exists: {skill_id}; set update_existing=true")

        if existing:
            current = skill_management.read(config, skill_id, scope="workspace")
            action, revision = "update", current["revision"]
        else:
            action, revision = "create", "missing"
        result = skill_management.manage(
            config,
            action,
            skill_id,
            scope="workspace",
            revision=revision,
            content=content,
        )

        if action == "create":
            catalog = skill_management.catalog(config, scope="workspace", query=skill_id, limit=200)
            created = next((row for row in catalog["skills"] if row["id"] == skill_id), None)
            if created and not created["enabled"]:
                skill_management.manage(
                    config,
                    "enable",
                    skill_id,
                    scope="workspace",
                    revision=catalog["enabled_revision"],
                )
        if skill_kernel is not None:
            skill_kernel.reload()
        if skill_index is not None:
            skill_index.reload()
        refreshed = skill_management.read(config, skill_id, scope="workspace")
        latest_catalog = skill_management.catalog(config, scope="workspace", query=skill_id, limit=200)
        latest = next((row for row in latest_catalog["skills"] if row["id"] == skill_id), None)
        result.update({
            "tool": "skill_manage",
            "requested_action": "save",
            "created": action == "create",
            "updated": action == "update",
            "enabled": bool(latest and latest["enabled"]),
            "revision": refreshed["revision"],
        })
    except Exception as exc:
        return ToolResult.from_error(
            tool_use_id=call.id,
            name=call.name,
            error=ToolError(
                kind=ToolErrorKind.EXECUTION_ERROR,
                message=f"{type(exc).__name__}: {exc}",
            ),
        )
    return ToolResult.from_json(tool_use_id=call.id, name=call.name, data=result)


# Historical Python import compatibility. It is intentionally not registered as
# a native tool anymore, so new Agent turns cannot create Skill proposals.
evolve_skill_proposal_handler = skill_manage_handler


def evolve_core_config_patch_handler(call: ToolCall, *, config: Config) -> ToolResult:
    """Draft a non-protected runtime config patch as a reviewable proposal."""

    args = call.arguments or {}
    config_after = args.get("config_after")
    if not isinstance(config_after, dict):
        return schema_validation_result(
            call, "config_after must be a full parsed YAML object",
        )
    try:
        proposal = propose_core_config_patch(
            config.paths,
            target=str(args.get("target") or ""),
            summary=str(args.get("summary") or "Core config patch"),
            config_after=config_after,
            rationale=str(args.get("rationale") or ""),
            current_config=config.data,
        )
    except ProtectedScopeViolation as exc:
        target = str(args.get("target") or "")
        # A rejected config proposal is not an MCP connection request, nor a
        # native tool ASK. Do not manufacture an OAuth/admin approval action
        # inside an ordinary Agent conversation. The separate MCP setup and
        # connector-use flows own their own credentials and consent.
        return ToolResult.from_error(
            tool_use_id=call.id,
            name=call.name,
            error=ToolError(
                kind=ToolErrorKind.PERMISSION_DENIED,
                message=(
                    "advisory reject: protected scope change refused. "
                    f"{exc}"
                ),
                detail={
                    "reason": "protected_scope",
                    "target": target,
                    "decision": "advisory reject",
                },
                retryable=False,
                recovery_hint={
                    "decision": "advisory reject",
                    "reason": "protected_scope",
                    "target": target,
                },
            ),
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
    return ToolResult.from_json(
        tool_use_id=call.id,
        name=call.name,
        data={"proposal": proposal.asdict()},
    )


def evolve_provider_proposal_handler(call: ToolCall, *, config: Config) -> ToolResult:
    """Draft a missing exchange/data provider as a reviewable proposal."""

    args = dict(call.arguments or {})
    venue = str(args.get("venue") or "").strip().lower().replace(" ", "_")
    if not venue:
        return schema_validation_result(call, "venue is required")
    args["venue"] = venue
    summary = str(args.get("summary") or f"Add provider proposal for {venue}").strip()
    metadata = {
        "venue": venue,
        "label": str(args.get("label") or "").strip(),
        "kind": str(args.get("kind") or "").strip(),
        "runtime": str(args.get("runtime") or "").strip(),
        "base_url": str(args.get("base_url") or "").strip(),
        "docs_url": str(args.get("docs_url") or "").strip(),
        "auth": str(args.get("auth") or "").strip(),
    }
    extra_metadata = args.get("metadata")
    if isinstance(extra_metadata, dict):
        metadata.update({
            str(key): value
            for key, value in extra_metadata.items()
            if value not in (None, "")
        })
    metadata = {key: value for key, value in metadata.items() if value not in (None, "")}
    try:
        proposal = create_proposal(
            config.paths,
            kind="provider_proposal",
            summary=summary,
            rationale=str(args.get("rationale") or summary),
            test_plan=(
                "# Test plan\n\n"
                "- Review the provider spec fields and credential schema.\n"
                "- Add connector/provider implementation in a separate approval step.\n"
                "- Run provider ping and read-only market-data smoke checks before live use.\n"
            ),
            rollback=(
                "# Rollback\n\n"
                "Reject or archive this proposal; no live provider config was mutated.\n"
            ),
            extra_files={
                f"after/providers/{venue}/provider.md": _provider_proposal_markdown(args),
            },
            initial_state="pending_review",
            target=f"providers/{venue}.yml",
            evidence_refs=_string_list(args.get("evidence_refs")),
            metadata=metadata,
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
    return ToolResult.from_json(
        tool_use_id=call.id,
        name=call.name,
        data={
            "ok": True,
            "proposal_id": proposal.id,
            "proposal": proposal.asdict(),
            "metadata": metadata,
            "next_required_action": "review_provider_proposal",
        },
    )


def evolve_post_apply_observation_handler(call: ToolCall, *, config: Config) -> ToolResult:
    """Append an evidence-backed observation for an applied proposal."""

    args = call.arguments or {}
    result = record_post_apply_observation(
        config.paths,
        proposal_id=str(args.get("proposal_id") or ""),
        status=args.get("status"),
        summary=str(args.get("summary") or args.get("note") or ""),
        source=str(args.get("source") or "manual"),
        observed_at=args.get("observed_at"),
        evidence_refs=args.get("evidence_refs"),
        metrics=args.get("metrics"),
        backtest_result=args.get("backtest_result"),
        run_id=args.get("run_id"),
        operator=args.get("operator"),
        metadata=args.get("metadata") if isinstance(args.get("metadata"), dict) else None,
    )
    if result.get("ok"):
        return ToolResult.from_json(tool_use_id=call.id, name=call.name, data=result)

    reason = str(result.get("reason") or "record_failed")
    kind = ToolErrorKind.EXECUTION_ERROR
    if reason in {
        "proposal_id_required",
        "evidence_required",
        "invalid_status",
        "metrics_must_be_object",
        "backtest_result_must_be_object",
    }:
        kind = ToolErrorKind.SCHEMA_VALIDATION
    elif reason == "proposal_not_found":
        kind = ToolErrorKind.NOT_FOUND
    elif reason == "proposal_not_applied":
        kind = ToolErrorKind.CONFLICT
    return ToolResult.from_error(
        tool_use_id=call.id,
        name=call.name,
        error=ToolError(
            kind=kind,
            message=reason,
            detail=result,
            retryable=False,
        ),
    )


__all__ = [
    "EVOLVE_PROVIDER_PROPOSAL_SCHEMA",
    "EVOLVE_POST_APPLY_OBSERVATION_SCHEMA",
    "EVOLVE_PROPOSALS_SCHEMA",
    "EVOLVE_REFLECT_SCHEMA",
    "EVOLVE_CORE_CONFIG_PATCH_SCHEMA",
    "EVOLVE_SKILL_PROPOSAL_SCHEMA",
    "SKILL_MANAGE_SCHEMA",
    "evolve_core_config_patch_handler",
    "evolve_post_apply_observation_handler",
    "evolve_provider_proposal_handler",
    "evolve_proposals_handler",
    "evolve_reflect_handler",
    "evolve_skill_proposal_handler",
    "skill_manage_handler",
]
