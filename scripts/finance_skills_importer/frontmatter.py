"""Frontmatter synthesis for upstream financial-services SKILL.md files.

The upstream files do *not* carry YAML frontmatter; they put ``description:``
inline as a markdown paragraph and rely on the surrounding plugin framework
to expose them. Nerya, on the other hand, requires a strict YAML frontmatter
block (see ``nerya/skills/manifest.py``). This module extracts the
upstream description, normalises it into a single triggers-aware sentence
the Nerya skill kernel can use to fire the skill, and emits a frontmatter
block that respects both the spec and the marker convention from
``nerya/skills/builtin/*/SKILL.md``.

We deliberately stay dependency-free (no PyYAML at runtime) — the YAML we
emit is hand-rolled and intentionally simple so future upgrade /
three-way-merge passes can diff it textually without round-tripping
through a full parser.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Mapping

from . import FRONTMATTER_END_MARKER, FRONTMATTER_START_MARKER

#: Regex that matches the upstream ``description:`` line. Upstream uses
#: a markdown paragraph that *starts* with the literal word "description:"
#: rather than YAML frontmatter — see e.g.
#: ``private-equity/skills/ic-memo/SKILL.md`` line 3.
_UPSTREAM_DESC_RE = re.compile(
    r"^\s*description:\s*(?P<body>.+?)\s*$",
    re.IGNORECASE | re.MULTILINE,
)

#: Regex that splits an upstream description into ``<thesis> Triggers on
#: "<phrase>", "<phrase>", …`` so we can rewrap it Nerya-style.
_TRIGGERS_SPLIT_RE = re.compile(
    r"^(?P<thesis>.*?)\s*Triggers?\s+on\s+(?P<triggers>.+?)\s*$",
    re.IGNORECASE | re.DOTALL,
)


@dataclass
class FrontmatterMeta:
    """Inputs needed to build a Nerya frontmatter for one imported skill."""

    name: str                   # snake_case skill id (frontmatter `name`)
    description: str            # composed Nerya-style description
    upstream_path: str          # relative path inside upstream repo
    upstream_repo: str = "financial-services"
    upstream_commit: str = ""   # git sha, optional
    license: str = "Apache-2.0"
    author: str = "Anthropic"
    version: str = "0.0.1"
    risk_class: str = "low"     # low | medium | high
    requires_integration: str = ""
    extra: Mapping[str, str] = field(default_factory=dict)


def extract_upstream_description(upstream_skill_md: str) -> str:
    """Pull the ``description:`` line out of an upstream SKILL.md body."""
    match = _UPSTREAM_DESC_RE.search(upstream_skill_md)
    if not match:
        return ""
    return _collapse_whitespace(match.group("body"))


def compose_nerya_description(
    upstream_description: str,
    *,
    upstream_vertical: str,
    upstream_skill: str,
) -> str:
    """Reshape an upstream description into a Nerya-spec one.

    Nerya kernel descriptions canonically open with ``"Use whenever …"``
    and surface explicit ``Triggers on …`` keywords so the kernel can
    decide when to load the skill. We try to reuse the upstream wording
    as much as possible — the upstream copywriting is itself good and
    should be honoured.
    """
    if not upstream_description:
        return (
            f"Adapted from {_provenance_blurb(upstream_vertical, upstream_skill)}. "
            "(Upstream description was missing — operator should rewrite this "
            "before relying on automatic skill triggering.)"
        )

    split = _TRIGGERS_SPLIT_RE.match(upstream_description)
    if split:
        thesis = _collapse_whitespace(split.group("thesis"))
        triggers = _collapse_whitespace(split.group("triggers")).rstrip(".")
        opener = _ensure_use_whenever(thesis)
        return (
            f'{opener} Triggers on {triggers}. '
            f"Adapted from {_provenance_blurb(upstream_vertical, upstream_skill)}."
        )

    opener = _ensure_use_whenever(upstream_description)
    return (
        f'{opener} '
        f"Adapted from {_provenance_blurb(upstream_vertical, upstream_skill)}."
    )


def render_frontmatter_block(meta: FrontmatterMeta) -> str:
    """Render a Nerya-spec frontmatter block (markers + YAML)."""
    lines: list[str] = [FRONTMATTER_START_MARKER, "---"]
    lines.append(f"name: {meta.name}")
    lines.append(f"description: {_yaml_double_quote(meta.description)}")
    lines.append(f"version: {meta.version}")
    lines.append(f"license: {meta.license}")
    lines.append(f"author: {meta.author}")
    if meta.requires_integration:
        lines.append(f"requires_integration: {meta.requires_integration}")
    lines.append(f"risk_class: {meta.risk_class}")
    lines.append("adapted_from:")
    lines.append(f"  upstream: {meta.upstream_repo}")
    lines.append(f"  upstream_path: {meta.upstream_path}")
    if meta.upstream_commit:
        lines.append(f"  upstream_commit: {meta.upstream_commit}")
    lines.append(f"  imported_at: {_now_iso()}")
    lines.append("  imported_by: finance_skills_importer/0.0.1")
    for key, value in sorted(meta.extra.items()):
        lines.append(f"{key}: {_yaml_double_quote(str(value))}")
    lines.append("---")
    lines.append(FRONTMATTER_END_MARKER)
    return "\n".join(lines)


# ---- helpers ---------------------------------------------------------------


def _provenance_blurb(upstream_vertical: str, upstream_skill: str) -> str:
    return (
        f"financial-services/{upstream_vertical}/{upstream_skill} "
        "(Apache-2.0)"
    )


def _ensure_use_whenever(thesis: str) -> str:
    """Make sure the thesis sentence opens with the canonical Nerya 'Use whenever' / 'Use for' form."""
    body = thesis.strip().rstrip(".")
    if not body:
        return "Use whenever the operator asks about this workflow."
    lower = body[:5].lower()
    if lower.startswith("use "):
        return body + "."
    if lower.startswith(("draft", "write", "build", "produce", "analyze", "audit")):
        # Imperative upstream description (e.g. "Draft a structured IC memo …")
        # — wrap it as Nerya-style trigger thesis.
        return f"Use whenever the operator asks to {_decap(body)}."
    return f"Use for: {body}."


def _decap(text: str) -> str:
    return text[0].lower() + text[1:] if text else text


def _collapse_whitespace(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def _yaml_double_quote(text: str) -> str:
    """Render *text* as a YAML double-quoted scalar.

    PyYAML round-trips most things, but we deliberately hand-roll this so
    the importer has zero runtime YAML dependencies. Inside double-quoted
    YAML scalars only ``\\`` and ``"`` need escaping — newlines are
    expanded ``\\n``-style.
    """
    escaped = (
        text.replace("\\", "\\\\")
        .replace('"', '\\"')
        .replace("\r\n", "\n")
        .replace("\n", "\\n")
    )
    return f'"{escaped}"'


def _now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


__all__ = [
    "FrontmatterMeta",
    "compose_nerya_description",
    "extract_upstream_description",
    "render_frontmatter_block",
]
