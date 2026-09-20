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

One read-only discovery surface
-------------------------------
``Skill`` also lists primary workflows, reads bounded reference pages, and
inspects scripts without executing them. ``skill_index``, ``skill_view`` and
``script_inspect`` remain callable compatibility entries. The provider catalog
folds them only after policy filtering and only when ``Skill`` is available.
``script_run`` remains a separate EXEC tool with its original approval policy.
"""

from __future__ import annotations

import logging
import re
from dataclasses import replace
from pathlib import Path
from typing import Any

from ..registry import ToolRegistry, make_native_descriptor
from ..tool_errors import schema_validation_result
from ..types import (
    PermissionScope,
    RiskLevel,
    ToolCall,
    ToolError,
    ToolErrorKind,
    ToolResult,
    ToolResultPart,
)
from .skill import (
    SkillIndex, script_inspect_handler, skill_index_handler, skill_view_handler,
)


_LOG = logging.getLogger(__name__)

_FRONTMATTER_RE = re.compile(
    r"^\s*(?:<!--.*?-->\s*)*---\s*\n.*?\n---\s*\n",
    re.DOTALL,
)
_SKILL_DIR_VAR_RE = re.compile(r"\$\{CLAUDE_SKILL_DIR\}")


SKILL_TOOL_NAME = "Skill"


SKILL_TOOL_DESCRIPTION = (
    "Read-only skill discovery: action=list lists primary workflows; "
    "skill=<name> loads a playbook; file=<relative path> reads a reference "
    "page (use returned next_offset to continue); action=inspect with script "
    "reads a helper without executing it. Catalog skill names are not callable tools: "
    "load them through this exact Skill tool. Scripts execute only through script_run."
)


SKILL_TOOL_INPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "action": {
            "type": "string", "enum": ["list", "load", "read", "inspect"],
            "description": "Defaults to load, or read when file is supplied.",
        },
        "skill": {"type": "string", "description": "Exact catalog or compatibility skill name."},
        "file": {"type": "string", "description": "Asset path confined to the skill directory."},
        "script": {"type": "string", "description": "Helper filename for action=inspect."},
        "offset": {"type": "integer", "minimum": 0},
        "limit": {"type": "integer", "minimum": 1, "maximum": 400},
        "refresh": {"type": "boolean"},
        "args": {"type": "string", "description": "Optional $ARGUMENTS substitution on load."},
    },
    "anyOf": [
        {"required": ["skill"]},
        {"required": ["action"], "properties": {"action": {"const": "list"}}},
    ],
}


def _strip_frontmatter(text: str) -> str:
    """Drop a leading YAML frontmatter block, if present.

    The listing already shows the description; including YAML in the body is
    just noise.
    """

    return _FRONTMATTER_RE.sub("", text, count=1)


def _normalise_dir(path: Path) -> str:
    """Render ``path`` with forward slashes so cross-OS skills work.

    Skills frequently contain
    relative refs like ``./scripts/foo.py`` and the model is far more
    comfortable with POSIX-style paths.
    """

    return str(path).replace("\\", "/")


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
    action = args.get("action") or ("read" if args.get("file") else "load")
    if action not in ("list", "load", "read", "inspect"):
        return schema_validation_result(call, "Skill supports list, load, read or inspect; never execution.")
    if action == "list":
        return skill_index_handler(call, skill_index=skill_index)
    raw_skill_name = args.get("skill")
    skill_name = raw_skill_name.strip() if isinstance(raw_skill_name, str) else ""
    if not skill_name:
        return schema_validation_result(
            call, 'Skill tool requires a non-empty "skill" argument.',
        )

    if action in ("read", "inspect"):
        if args.get("refresh") or skill_index.get(skill_name) is None:
            skill_index.reload()
        mapped = dict(args, skill_id=skill_name)
        if action == "read":
            if not isinstance(args.get("file"), str) or not args["file"].strip():
                return schema_validation_result(call, "Skill read requires a relative file path.")
            mapped.setdefault("limit", 200)
            return skill_view_handler(replace(call, arguments=mapped), skill_index=skill_index)
        return script_inspect_handler(replace(call, arguments=mapped), skill_index=skill_index)

    record = skill_index.get(skill_name, refresh=bool(args.get("refresh")))
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
    except (OSError, UnicodeError) as exc:
        return ToolResult.from_error(
            tool_use_id=call.id,
            name=call.name,
            error=ToolError(
                kind=ToolErrorKind.EXECUTION_ERROR,
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

    The advertised name is exact: ``Skill``.
    """

    def _base_handler(call: ToolCall) -> ToolResult:
        return skill_tool_handler(call, skill_index=skill_index)

    descriptor = make_native_descriptor(
        name=SKILL_TOOL_NAME,
        description=SKILL_TOOL_DESCRIPTION,
        input_schema=SKILL_TOOL_INPUT_SCHEMA,
        handler=_base_handler,
        risk=RiskLevel.READ,
        permission_scope=PermissionScope.NONE,
        read_only=True,
        is_concurrency_safe=True,
        tags=("skill", "playbook"),
        result_kind="text",
        auto_approve=True,
    )
    registry.register(descriptor, replace=replace)


__all__ = [
    "SKILL_TOOL_DESCRIPTION",
    "SKILL_TOOL_INPUT_SCHEMA",
    "SKILL_TOOL_NAME",
    "register_skill_tool",
    "skill_tool_handler",
]
