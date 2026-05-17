"""``Skill`` native tool — load a SKILL.md playbook into the conversation.

This single tool lets the model invoke ``{"skill": "<name>"}``; the
handler:

1. resolves ``<name>`` against the live :class:`SkillIndex`,
2. reads the ``SKILL.md`` file,
3. strips its YAML frontmatter (the listing shows the metadata; the
   body is the playbook),
4. prepends a ``Base directory for this skill: <abs path>`` header so
   the model can reference scripts/assets next to the skill,
5. substitutes ``${CLAUDE_SKILL_DIR}`` placeholders with the same path
   (agent skill runtime convention),
6. returns the resulting text as the tool result — the workspace-native
   loop puts it in a ``tool_result`` block, which the model reads on
   the next turn exactly as if the playbook had been quoted in a user
   message.

The tool is read-only (``RiskLevel.READ``) and auto-approves: loading a
playbook never mutates state. Anything the playbook then *tells* the
model to do still flows through the model -> tool_use loop, so each
underlying tool call still goes through the permission engine.

Why a separate tool when ``skill_view`` already exists?
-------------------------------------------------------
``skill_view`` returns the *raw* SKILL.md (frontmatter included) and
takes ``skill_id`` as its arg name — that's useful for operator
debugging and dashboards, but it's not the shape the model
expects. ``Skill`` takes the ``skill`` field name, strips the frontmatter,
and prepends the base-dir header. Keeping the two tools separate lets
the model pick the right one for the right job: ``Skill`` to *invoke*
a playbook, ``skill_view`` (and ``skill_index``) for discovery /
inspection.
"""

from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Any

from ..registry import ToolRegistry, make_native_descriptor
from ..types import (
    PermissionScope,
    RiskLevel,
    ToolCall,
    ToolError,
    ToolErrorKind,
    ToolResult,
    ToolResultPart,
)
from .skill import SkillIndex


_LOG = logging.getLogger(__name__)

_FRONTMATTER_RE = re.compile(r"^---\s*\n.*?\n---\s*\n", re.DOTALL)
_SKILL_DIR_VAR_RE = re.compile(r"\$\{CLAUDE_SKILL_DIR\}")


SKILL_TOOL_NAME = "Skill"


SKILL_TOOL_DESCRIPTION = (
    "Execute a skill within the main conversation.\n\n"
    "When users ask you to perform tasks, check if any of the available"
    " skills match. Skills provide specialized capabilities and domain"
    " knowledge.\n\n"
    "How to invoke:\n"
    "- Call this tool with the skill name (e.g. `skill: \"commit\"`).\n"
    "- Available skills are listed in the system prompt under \"Skills"
    " available\".\n\n"
    "Important:\n"
    "- When a skill matches the user's request, this is a BLOCKING"
    " REQUIREMENT: invoke the relevant Skill tool BEFORE generating"
    " any other response about the task.\n"
    "- NEVER mention a skill without actually calling this tool.\n"
    "- Do not invoke a skill that is already running."
)


SKILL_TOOL_INPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "skill": {
            "type": "string",
            "description": (
                'The skill name. E.g., "commit", "review-pr", or "pdf".'
            ),
        },
        "args": {
            "type": "string",
            "description": (
                "Optional free-form arguments forwarded to the skill"
                " body via the ``$ARGUMENTS`` placeholder."
            ),
        },
    },
    "required": ["skill"],
}


def _strip_frontmatter(text: str) -> str:
    """Drop a leading YAML frontmatter block, if present.

    The listing already shows the description /
    when_to_use; including the YAML in the body is just noise.
    """

    return _FRONTMATTER_RE.sub("", text, count=1)


def _normalise_dir(path: Path) -> str:
    """Render ``path`` with forward slashes so cross-OS skills work.

    Skills frequently contain
    relative refs like ``./scripts/foo.py`` and the model is far more
    comfortable with POSIX-style paths.
    """

    return str(path).replace("\\", "/")


def _normalise_name(raw: Any) -> str:
    name = str(raw or "").strip()
    if name.startswith("/"):
        name = name[1:]
    return name


def _substitute_args(body: str, args: str) -> str:
    """Replace ``$ARGUMENTS`` markers with the caller-supplied string.

    When ``args`` is empty the marker is dropped so a
    skill written for an arg-less invocation reads cleanly.
    """

    if "$ARGUMENTS" not in body:
        return body
    return body.replace("$ARGUMENTS", args or "")


def skill_tool_handler(
    call: ToolCall,
    *,
    skill_index: SkillIndex,
) -> ToolResult:
    args = call.arguments or {}
    skill_name = _normalise_name(args.get("skill") or args.get("name"))
    if not skill_name:
        return ToolResult.from_error(
            tool_use_id=call.id,
            name=call.name,
            error=ToolError(
                kind=ToolErrorKind.SCHEMA_VALIDATION,
                message='Skill tool requires a non-empty "skill" argument.',
            ),
        )

    record = skill_index.get(skill_name)
    if record is None:
        record = skill_index.get(skill_name, refresh=True)
    if record is None:
        return ToolResult.from_error(
            tool_use_id=call.id,
            name=call.name,
            error=ToolError(
                kind=ToolErrorKind.NOT_FOUND,
                message=f"Unknown skill: {skill_name!r}",
            ),
        )

    skill_md_path = Path(record.path)
    try:
        text = skill_md_path.read_text(encoding="utf-8")
    except OSError as exc:
        return ToolResult.from_error(
            tool_use_id=call.id,
            name=call.name,
            error=ToolError(
                kind=ToolErrorKind.IO_ERROR,
                message=f"failed to read SKILL.md for {skill_name!r}: {exc}",
            ),
        )

    body = _strip_frontmatter(text)

    skill_dir = skill_md_path.parent
    base_dir = _normalise_dir(skill_dir.resolve())
    body = _SKILL_DIR_VAR_RE.sub(base_dir, body)

    extra_args = str(args.get("args") or "").strip()
    body = _substitute_args(body, extra_args)

    rendered = (
        f"Base directory for this skill: {base_dir}\n\n"
        f"{body.strip()}\n"
    )

    return ToolResult(
        tool_use_id=call.id,
        name=call.name,
        content=[
            ToolResultPart.text_part(rendered),
            ToolResultPart.json_part(
                {
                    "skill": skill_name,
                    "skill_id": record.skill_id,
                    "path": str(skill_md_path),
                    "base_dir": base_dir,
                    "scripts": list(record.scripts),
                    "has_scripts": bool(record.has_scripts),
                    "args": extra_args,
                    "status": "inline",
                }
            ),
        ],
    )


def register_skill_tool(
    registry: ToolRegistry,
    *,
    skill_index: SkillIndex,
    replace: bool = False,
) -> None:
    """Register the ``Skill`` tool on ``registry``.

    Kept separate from :func:`register_native_tools` so callers that
    register a slim subset of native tools (for tests or specialised
    runtimes) can opt in or out without touching the bootstrap.
    """

    def _handler(call: ToolCall) -> ToolResult:
        return skill_tool_handler(call, skill_index=skill_index)

    descriptor = make_native_descriptor(
        name=SKILL_TOOL_NAME,
        description=SKILL_TOOL_DESCRIPTION,
        input_schema=SKILL_TOOL_INPUT_SCHEMA,
        handler=_handler,
        risk=RiskLevel.READ,
        permission_scope=PermissionScope.NONE,
        read_only=True,
        is_concurrency_safe=True,
        tags=("skill", "playbook"),
        result_kind="text",
        auto_approve=True,
    )
    registry.register(descriptor, replace=replace)

    # Lowercase alias. Some models emit ``"skill"`` instead of
    # ``"Skill"``. The
    # registry lookup is case-sensitive, so without this alias those
    # calls hit the ``unknown tool: 'skill'`` branch in the executor and
    # the agent loop records a hard error — the turn often recovers by
    # restarting from scratch and loses the model's plan. Register an
    # alias descriptor that shares the same handler / schema.
    alias_descriptor = make_native_descriptor(
        name=SKILL_TOOL_NAME.lower(),
        description=SKILL_TOOL_DESCRIPTION,
        input_schema=SKILL_TOOL_INPUT_SCHEMA,
        handler=_handler,
        risk=RiskLevel.READ,
        permission_scope=PermissionScope.NONE,
        read_only=True,
        is_concurrency_safe=True,
        tags=("skill", "playbook", "alias"),
        result_kind="text",
        auto_approve=True,
    )
    registry.register(alias_descriptor, replace=replace)


__all__ = [
    "SKILL_TOOL_DESCRIPTION",
    "SKILL_TOOL_INPUT_SCHEMA",
    "SKILL_TOOL_NAME",
    "register_skill_tool",
    "skill_tool_handler",
]
