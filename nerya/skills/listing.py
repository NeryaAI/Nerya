"""Skill listing — agent-skill style.

A skill is a ``SKILL.md`` (YAML frontmatter + markdown body) — a
*prompt template*, not a bundle of typed actions. The native agent
loop discovers skills the same way coding-agent does
(:file:`coding-agent/src/skills/loadSkillsDir.ts`,
:file:`coding-agent/src/tools/SkillTool/prompt.ts`) and the runtime does
(:file:`agent-runtime/agent/skill_utils.py`):

* List skills as ``name: description - when_to_use`` lines, truncated
  to a per-entry character cap and overall token budget.
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

from typing import Any

from .kernel import SkillKernel


# Per-entry character cap for the listing — matches coding-agent's
# ``MAX_LISTING_DESC_CHARS`` (250). Wide descriptions waste turn-1
# cache_creation tokens without improving match rate.
MAX_LISTING_DESC_CHARS = 250


def _truncate(text: str, limit: int = MAX_LISTING_DESC_CHARS) -> str:
    text = (text or "").strip()
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 1)].rstrip() + "\u2026"


def _frontmatter(manifest: Any) -> dict[str, Any]:
    meta = getattr(manifest, "instructions_meta", None)
    return dict(meta) if isinstance(meta, dict) else {}


def _when_to_use(manifest: Any) -> str:
    fm = _frontmatter(manifest)
    raw = fm.get("when_to_use") or fm.get("whenToUse") or ""
    return str(raw).strip()


def build_skill_listing(skills: SkillKernel | None) -> list[dict[str, Any]]:
    """Return a coding-agent-style listing of installed skills.

    Each row carries only what the model needs to decide whether to
    invoke a skill: ``name``, ``description`` (truncated), and
    ``when_to_use`` (when the manifest declares one).
    """

    out: list[dict[str, Any]] = []
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
        description = _truncate(getattr(manifest, "description", "") or "")
        when_to_use = _truncate(_when_to_use(manifest))
        row: dict[str, Any] = {"name": name, "description": description}
        if when_to_use:
            row["when_to_use"] = when_to_use
        out.append(row)

    out.sort(key=lambda e: e.get("name") or "")
    return out


def format_skill_listing(skills: SkillKernel | None) -> str:
    """Format the listing as ``- name: description - when_to_use`` lines.

    Mirrors :func:`coding-agent/src/tools/SkillTool/prompt.ts:formatCommandsWithinBudget`
    minus the global token-budget pass — that lives on the caller (the
    native loop's prompt assembler) which knows the model's context
    window.
    """

    rows = build_skill_listing(skills)
    if not rows:
        return ""
    lines: list[str] = []
    for row in rows:
        desc = row.get("description") or ""
        wtu = row.get("when_to_use") or ""
        if desc and wtu:
            payload = f"{desc} - {wtu}"
        else:
            payload = desc or wtu
        lines.append(f"- {row['name']}: {payload}")
    return "\n".join(lines)


__all__ = [
    "MAX_LISTING_DESC_CHARS",
    "build_skill_listing",
    "format_skill_listing",
]
