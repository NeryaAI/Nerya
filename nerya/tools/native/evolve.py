"""Self-evolution native tools.

Hermes parity: the agent can ask for a reflection cycle directly,
without the legacy ``runtime.call("evolve", "tick", ...)`` bridge. Two
tools are exposed:

* ``evolve_reflect`` — run :func:`nerya.agent.self_improvement.evolve`
  to summarise journals + risk + ranked seeds and write a
  ``learning_update`` proposal under ``evolution/proposals/``.
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


__all__ = [
    "EVOLVE_PROPOSALS_SCHEMA",
    "EVOLVE_REFLECT_SCHEMA",
    "evolve_proposals_handler",
    "evolve_reflect_handler",
]
