"""Procedural ``SKILL.md`` skills (Plan 02 P0 §4).

Hermes' user skill ecosystem accepts plain markdown skills with a YAML
frontmatter (``---``) describing ``id``, ``description``, ``tags``,
``allowed_tools``, and so on. The body is the operator-facing playbook
the agent should follow. There is no Python action module — the skill
is *procedural*, i.e. a recipe.

Nerya has historically only supported typed action skills
(``skill.yml`` + ``actions.py``). Without a procedural loader, every
agentskills.io / OpenClaw / Hermes skill on disk is invisible to the
runtime, so operators cannot drag-and-drop a SKILL.md into
``workspace/skills/`` and use it from chat.

This module builds a synthetic :class:`SkillManifest` for each
SKILL.md it finds, exposing a single ``run`` action that returns the
markdown body so the planner / chat surface can splice the playbook
into context. Mutation actions are intentionally *not* generated —
procedural skills declare what the agent should consider, the agent
still routes side effects through the existing typed skills.

Plan ref:
``docs/plans/2026-04-25-nerya-hermes-capability-gap-audit/02-skill-loading-and-execution.md``
P0 §4.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .manifest import ActionSpec, SkillManifest


_FRONTMATTER_RE = re.compile(r"^---\s*\n(?P<body>.*?)\n---\s*\n", re.DOTALL)


@dataclass
class ProceduralSkill:
    manifest: SkillManifest
    body: str


def _parse_frontmatter(text: str) -> tuple[dict[str, Any], str]:
    """Split frontmatter ``---`` block from body. Returns ``({}, full_text)``
    when no frontmatter is present so unannotated SKILL.md still loads.
    """

    m = _FRONTMATTER_RE.match(text)
    if not m:
        return {}, text
    raw = m.group("body")
    try:
        # local import keeps yaml optional in environments without it.
        from ..core import yaml_io
        meta = yaml_io.loads(raw) or {}
    except Exception:
        meta = {}
    if not isinstance(meta, dict):
        meta = {}
    body = text[m.end():]
    return meta, body


def load_procedural_skill(path: Path) -> ProceduralSkill | None:
    """Load ``path`` (a SKILL.md file) into a :class:`ProceduralSkill`.

    Returns ``None`` if the file is missing or has no usable id (we
    refuse to silently make one up so a typo does not shadow a real
    skill).
    """

    if not path.exists() or not path.is_file():
        return None
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return None
    meta, body = _parse_frontmatter(text)
    sid = str(meta.get("id") or meta.get("name") or "").strip()
    if not sid:
        sid = path.parent.name.strip()
    if not sid:
        return None
    sid = re.sub(r"[^A-Za-z0-9_.-]+", "_", sid)
    title = str(meta.get("title") or sid).strip()
    description = str(meta.get("description") or "").strip()
    version = str(meta.get("version") or "0.1.0")
    tags = list(meta.get("tags") or [])
    permissions = list(meta.get("permissions") or [])
    allowed_tools = list(meta.get("allowed_tools") or [])
    if allowed_tools and "tools" not in tags:
        tags.append("tools:" + ",".join(allowed_tools))

    actions = {
        "run": ActionSpec(
            name="run",
            title=f"Run procedural skill: {title}",
            description=(
                description
                or "Return the SKILL.md playbook so the agent can follow it."
            ),
            permissions=permissions,
            risk_gate="n/a",
            approval_gate="n/a",
            context_policy="scoped_strategy",
            journal=True,
            input_schema={"type": "object"},
            output_schema={
                "type": "object",
                "properties": {
                    "skill_id": {"type": "string"},
                    "title": {"type": "string"},
                    "body": {"type": "string"},
                    "tags": {"type": "array"},
                },
            },
            tags=tags + ["procedural"],
        )
    }
    manifest = SkillManifest(
        id=sid,
        version=version,
        title=title or sid,
        description=description,
        permissions=permissions,
        actions=actions,
        source="procedural",
        path=path.parent,
        status="ready",
        tags=tags + ["procedural"],
    )
    return ProceduralSkill(manifest=manifest, body=body)


def make_run_handler(body: str, sid: str, title: str, tags: list[str]):
    """Return a stable callable suitable for ``SkillEntry.actions['run']``.

    The handler ignores its kwargs (apart from ``ctx``) and returns the
    procedural body. We do not embed ``body`` as a default arg because
    that would let a caller accidentally override it via payload.
    """

    captured_body = body
    captured_id = sid
    captured_title = title
    captured_tags = list(tags)

    def run(ctx, **payload) -> dict[str, Any]:  # noqa: D401 - thin wrapper
        return {
            "skill_id": captured_id,
            "title": captured_title,
            "body": captured_body,
            "tags": captured_tags,
            "source": "procedural",
        }

    return run


__all__ = [
    "ProceduralSkill",
    "load_procedural_skill",
    "make_run_handler",
]
