"""Skill listing — agent-skill style.

A skill is a ``SKILL.md`` (YAML frontmatter + markdown body) — a
*prompt template*, not a bundle of typed actions. The native agent
loop discovers skills by reading SKILL.md frontmatter + body:

* List skills as ``name: description`` lines.
* When the model invokes a skill, return the markdown body verbatim —
  the body teaches the model how to combine native tools.

This module replaces the old ``action_catalog`` (which projected a
non-spec ``actions[]`` array). Anything that was previously a "skill
action" is now either:

* a native tool (registered on :class:`~nerya.tools.registry.ToolRegistry`),
  or
* a markdown procedure inside the skill body.

Permissions / risk gates / concurrency safety / fresh-read invariants
all live on :class:`~nerya.tools.types.ToolDescriptor`, not on skills.
"""

from __future__ import annotations

from .kernel import SkillKernel


def build_skill_listing(skills: SkillKernel | None) -> list[dict[str, str]]:
    """Return a compact listing of installed skills.

    Each row carries only the standard activation contract: ``name`` and
    ``description``. The model decides whether the description matches.
    """

    out: list[dict[str, str]] = []
    if skills is None:
        return out
    try:
        entries = list(skills.registry.list())
    except Exception:  # pragma: no cover - defensive
        return out

    for entry in entries:
        manifest = getattr(entry, "manifest", None)
        if manifest is None:
            continue
        name = getattr(manifest, "id", "") or ""
        if not name:
            continue
        description = (getattr(manifest, "description", "") or "").strip()
        out.append({"name": name, "description": description})

    out.sort(key=lambda e: e.get("name") or "")
    return out


def format_skill_listing(skills: SkillKernel | None) -> str:
    """Format the listing as ``- name: description`` lines.

    The caller owns the global token-budget pass because it knows the
    model's current context window.
    """

    rows = build_skill_listing(skills)
    if not rows:
        return ""
    lines: list[str] = []
    for row in rows:
        desc = row.get("description") or ""
        lines.append(f"- {row['name']}: {desc}")
    return "\n".join(lines)


__all__ = [
    "build_skill_listing",
    "format_skill_listing",
]
