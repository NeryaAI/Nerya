"""Recipe native tools — let the model browse and pull operator-curated runbooks.

Recipes are short "if you see X, run Y" playbooks the operator ships
under ``workspace/recipes/<id>.yml`` (or via the bundled set in
:mod:`nerya.agent.recipes`). They complement the SKILL.md index by
surfacing higher-level *named workflows* that often combine multiple
skills + native tools.

Two tools land here:

* :func:`recipe_list_handler` — compact list of recipes whose
  ``required_skills`` are satisfied by the currently installed skill
  kernel; mirrors what the kernel already renders in the system prompt
  but lets the model fetch the *full* list on demand.
* :func:`recipe_view_handler` — full body + prompt for a specific
  recipe id. The model gets the prompt verbatim so it can either replay
  it inside the current turn (most common) or quote it back to the
  operator for confirmation before acting.

There is intentionally no ``recipe_run`` here: forking a sub-turn that
replays a recipe prompt is a control-flow choice we want the *model*
to make explicit (``recipe_view`` → quote prompt → execute step by
step) rather than something a single tool call hides behind.
"""

from __future__ import annotations

from typing import Any

from ...agent.recipes import all_recipes, is_available
from ...skills.kernel import SkillKernel
from ..tool_errors import schema_validation_result as _usage_error
from ..types import ToolCall, ToolResult


# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------


RECIPE_LIST_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "tag": {
            "type": "string",
            "description": "Optional tag filter (matches Recipe.tags).",
        },
        "category": {
            "type": "string",
            "description": "Optional category filter.",
        },
        "available_only": {
            "type": "boolean",
            "default": True,
            "description": (
                "When true (default), drop recipes whose required_skills "
                "aren't installed in the current workspace."
            ),
        },
    },
}

RECIPE_VIEW_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {"id": {"type": "string"}},
    "required": ["id"],
}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _capability_set(skills: SkillKernel | None) -> tuple[frozenset[str], frozenset[str]]:
    """Mirror :func:`nerya.agent.recipes._capability_set` without needing a
    fully-formed *client* shim.
    """

    skill_ids: set[str] = set()
    action_ids: set[str] = set()
    if skills is None:
        return frozenset(), frozenset()
    try:
        entries = list(skills.registry.list())
    except Exception:
        return frozenset(), frozenset()
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
    return frozenset(skill_ids), frozenset(action_ids)


# ---------------------------------------------------------------------------
# Handlers
# ---------------------------------------------------------------------------


def recipe_list_handler(call: ToolCall, *, skills: SkillKernel, paths) -> ToolResult:
    args = call.arguments or {}
    tag = (args.get("tag") or "").strip().lower()
    category = (args.get("category") or "").strip().lower()
    available_only = args.get("available_only")
    if available_only is None:
        available_only = True

    sf, af = _capability_set(skills) if available_only else (frozenset(), frozenset())
    rows: list[dict[str, Any]] = []
    for recipe in all_recipes(paths):
        if available_only and not is_available(recipe, sf, af):
            continue
        if tag and tag not in (t.lower() for t in recipe.tags):
            continue
        if category and recipe.category.lower() != category:
            continue
        rows.append({
            "id": recipe.id,
            "title": recipe.title,
            "body": recipe.body,
            "category": recipe.category,
            "tags": list(recipe.tags),
            "required_skills": list(recipe.required_skills),
        })
    return ToolResult.from_json(
        tool_use_id=call.id,
        name=call.name,
        data={"count": len(rows), "recipes": rows},
    )


def recipe_view_handler(call: ToolCall, *, skills: SkillKernel, paths) -> ToolResult:
    args = call.arguments or {}
    rid = (args.get("id") or "").strip()
    if not rid:
        return _usage_error(call, "id is required")
    for recipe in all_recipes(paths):
        if recipe.id == rid:
            sf, af = _capability_set(skills)
            return ToolResult.from_json(
                tool_use_id=call.id,
                name=call.name,
                data={
                    "id": recipe.id,
                    "title": recipe.title,
                    "body": recipe.body,
                    "prompt": recipe.prompt,
                    "category": recipe.category,
                    "tags": list(recipe.tags),
                    "required_skills": list(recipe.required_skills),
                    "required_actions": list(recipe.required_actions),
                    "available": is_available(recipe, sf, af),
                },
            )
    return _usage_error(call, f"recipe not found: {rid}")


__all__ = [
    "RECIPE_LIST_SCHEMA",
    "RECIPE_VIEW_SCHEMA",
    "recipe_list_handler",
    "recipe_view_handler",
]
