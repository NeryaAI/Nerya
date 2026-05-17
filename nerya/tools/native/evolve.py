"""Self-evolution native tools.

compatibility: the agent can ask for a reflection cycle directly,
without the legacy ``runtime.call("evolve", "tick", ...)`` bridge. Two
tools are exposed:

* ``evolve_reflect`` — run :func:`nerya.agent.self_improvement.evolve`
  to summarise journals + risk + ranked seeds and write a
  ``learning_update`` proposal under ``evolution/proposals/``.
* ``evolve_skill_proposal`` — capture a repeatable workflow as a
  reviewable ``skill_proposal`` with ``after/skills/<id>/SKILL.md``.
* ``evolve_proposals`` — read-only enumeration of pending proposals
  (id + kind + summary + path) so the model can decide whether to
  trigger a fresh reflection or summarise an existing one.

Both are write-light: ``evolve_reflect`` only ever creates a
*proposal* (never mutates live config), matching the safety contract
in ``self_improvement.evolve``'s docstring.
"""

from __future__ import annotations

from typing import Any

from ...agent.self_improvement import evolve
from ...core.config import Config
from ...evolution.patch_proposal import list_proposals
from ...evolution.skill_proposal import propose_skill_from_workflow
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


def evolve_reflect_handler(call: ToolCall, *, config: Config) -> ToolResult:
    """Run a reflection tick and return the new proposal envelope."""

    try:
        result = evolve(config)
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
    return ToolResult.from_json(
        tool_use_id=call.id,
        name=call.name,
        data={
            "proposal": proposal,
            "ranked_seeds": ranked[:10],
            "seed_count": len(ranked),
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


__all__ = [
    "EVOLVE_PROPOSALS_SCHEMA",
    "EVOLVE_REFLECT_SCHEMA",
    "EVOLVE_SKILL_PROPOSAL_SCHEMA",
    "evolve_proposals_handler",
    "evolve_reflect_handler",
    "evolve_skill_proposal_handler",
]
