"""Self-evolution native tools.

compatibility: the agent can ask for a reflection cycle directly,
without the legacy ``runtime.call("evolve", "tick", ...)`` bridge. Two
tools are exposed:

* ``evolve_reflect`` — run :func:`nerya.evolution.runner.evolve`
  to summarise journals + risk + ranked seeds, collect structured
  evolution signals/assets/events, and write a ``learning_update``
  proposal under ``evolution/proposals/``.
* ``evolve_skill_proposal`` — capture a repeatable workflow as a
  reviewable ``skill_proposal`` with ``after/skills/<id>/SKILL.md``.
* ``evolve_proposals`` — read-only enumeration of pending proposals
  (id + kind + summary + path) so the model can decide whether to
  trigger a fresh reflection or summarise an existing one.
* ``evolve_post_apply_observation`` — append evidence-backed post-apply
  observations for an applied proposal.

Both are write-light: ``evolve_reflect`` only ever creates a
*proposal* (never mutates live config), matching the safety contract
in ``evolution.runner.evolve``'s docstring.
"""

from __future__ import annotations

from typing import Any

from ...core.config import Config
from ...core.errors import ProtectedScopeViolation
from ...evolution import runner as evolution_runner
from ...evolution.patch_proposal import create_proposal, list_proposals
from ...evolution.post_apply_observation import record_post_apply_observation
from ...evolution.self_config import propose_core_config_patch
from ...evolution.skill_proposal import propose_skill_from_workflow
from ..tool_errors import schema_validation_result
from ..types import (
    ToolCall,
    ToolError,
    ToolErrorKind,
    ToolResult,
)


EVOLVE_REFLECT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {},
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

EVOLVE_SKILL_PROPOSAL_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
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
            "description": "Allow the proposal to replace an existing workspace skill.",
        },
    },
    "required": ["name", "description", "workflow"],
}

EVOLVE_CORE_CONFIG_PATCH_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "target": {
            "type": "string",
            "description": (
                "Workspace config file to propose, for example nerya.yml, "
                "agents.yml, workspace.yml, news_feeds.yml, "
                "messages/channels.yml, triggers/routes.yml, "
                "policies/planner.yml, or "
                "policies/tier_policy.yml."
            ),
        },
        "summary": {
            "type": "string",
            "description": "Concise operator-facing summary of the config change.",
        },
        "config_after": {
            "type": "object",
            "description": (
                "Full parsed YAML object for the target file after the proposed "
                "change. The live file is not mutated. For messages/channels.yml "
                "or messages/channels.yaml, use the canonical shape "
                "`channels: {<id>: {kind: telegram|discord|webhook, ...}}` plus "
                "top-level `severity_routes: {info: [telegram], critical: "
                "[telegram, discord], silent: []}` for severity-based routing. "
                "For Telegram, store bot tokens as `bot_token_ref`; if the "
                "operator provides a vault-backed chat id, use `chat_id_ref`, "
                "otherwise use plaintext numeric `chat_id`. "
                "Do not use ad-hoc targets such as notifications.routing."
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


def evolve_reflect_handler(call: ToolCall, *, config: Config) -> ToolResult:
    """Run a reflection tick and return the new proposal envelope."""

    try:
        result = evolution_runner.evolve(config)
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


def evolve_proposals_handler(call: ToolCall, *, config: Config) -> ToolResult:
    """List pending proposals under ``evolution/proposals/``.

    Reads through :func:`nerya.evolution.patch_proposal.list_proposals`
    so the metadata format (``proposal.yml``) stays the single source of
    truth — we only re-render the summary the model needs.
    """

    args = call.arguments or {}
    limit = max(1, int(args.get("limit") or 20))
    try:
        proposals = list_proposals(config.paths)
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


def evolve_skill_proposal_handler(call: ToolCall, *, config: Config) -> ToolResult:
    """Draft a workflow-to-skill proposal without mutating live skills."""

    args = call.arguments or {}
    try:
        result = propose_skill_from_workflow(
            config.paths,
            name=str(args.get("name") or ""),
            description=str(args.get("description") or ""),
            workflow=args.get("workflow"),
            triggers=args.get("triggers"),
            evidence_refs=args.get("evidence_refs"),
            gotchas=args.get("gotchas"),
            script_notes=args.get("script_notes"),
            reference_notes=args.get("reference_notes"),
            update_existing=bool(args.get("update_existing") or False),
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
    return ToolResult.from_json(tool_use_id=call.id, name=call.name, data=result)


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
    "evolve_core_config_patch_handler",
    "evolve_post_apply_observation_handler",
    "evolve_provider_proposal_handler",
    "evolve_proposals_handler",
    "evolve_reflect_handler",
    "evolve_skill_proposal_handler",
]
