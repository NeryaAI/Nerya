"""finance_skills_importer — curated import of upstream financial-services
SKILL.md files into Nerya.

This package is *not* part of the Nerya runtime. It is an operator-level
tooling that reads
``../financial-services/plugins/vertical-plugins/<vertical>/skills/``
(treated as read-only upstream) and can land compact ``SKILL.md`` files
either under an operator workspace or under shipped builtins at
``nerya/skills/builtin/finance/<vertical>/<skill>/SKILL.md``.

Design notes are tracked in
``NeryaProject/tmp/finance_services_integration/deliverable.md``.

Phase A scope (current): ``transform`` + ``name_map`` + ``frontmatter``
+ ``cli import`` of one vertical at a time. Slash-command, agent-plugin,
diff-overlap, and upgrade subcommands ship in later phases.
"""
from __future__ import annotations

__all__ = [
    "FRONTMATTER_START_MARKER",
    "FRONTMATTER_END_MARKER",
]

#: HTML-comment markers that wrap the YAML frontmatter of every imported
#: SKILL.md. Mirrors what Nerya's own builtin skills use so Markdown
#: autoformatters in editors do not collapse the leading ``---`` into a
#: setext H2 heading. See ``nerya/skills/manifest.py::_FRONTMATTER_RE``.
FRONTMATTER_START_MARKER = "<!-- nerya-skill-frontmatter-start -->"
FRONTMATTER_END_MARKER = "<!-- nerya-skill-frontmatter-end -->"
