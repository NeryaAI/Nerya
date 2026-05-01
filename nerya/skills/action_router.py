"""Thin helper for tools that receive a 'skill:<id>.<action>' name and need
to dispatch."""

from __future__ import annotations

from typing import Any

from ..core.errors import SkillNotFoundError
from .kernel import SkillKernel


def route(kernel: SkillKernel, tool_name: str, payload: dict[str, Any], **ctx) -> dict[str, Any]:
    if not tool_name.startswith("skill:"):
        raise SkillNotFoundError(f"expected skill:<id>.<action>, got {tool_name}")
    rest = tool_name[len("skill:"):]
    if "." not in rest:
        raise SkillNotFoundError(tool_name)
    skill_id, action = rest.split(".", 1)
    return kernel.call(skill_id, action, payload=payload, **ctx)
