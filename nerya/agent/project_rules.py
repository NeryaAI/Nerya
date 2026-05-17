"""Loader for project-local agent rules (AGENTS.md / .cursor/rules / CLAUDE.md).


Why
---
IDE integrations and coding agents all converge on the same idea:
a project ships a small set of plain-text "agent rules" that the
runtime injects into the system prompt every turn. Examples:

* ``AGENTS.md`` — top-level conventions
* ``.cursor/rules/*.md`` — IDE-style rule snippets, optionally
  scoped to glob patterns
* ``CLAUDE.md`` — legacy alternate filename
* ``.claude/skills/*/SKILL.md`` — auto-loaded skill instructions

This module discovers and merges all of those into one ordered list
of :class:`ProjectRule` records. The kernel injects them into the
layered prompt (see :mod:`prompt_sections`) under a ``[project rules]``
section so the model sees them on every step.

Path scoping
------------
Rule files can declare a YAML frontmatter with optional ``apply_to``
globs. When the current task touches a file matching one of the
globs, the rule is *prioritised* (rendered first, with a higher
salience). Otherwise it is rendered after the global rules. The
frontmatter is identical to IDE's rule format so existing rule
files just work.
"""

from __future__ import annotations

import fnmatch
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Sequence

from ..core import yaml_io


__all__ = [
    "ProjectRule",
    "load_project_rules",
    "render_rules",
]


_FRONTMATTER_RE = re.compile(r"^---\s*\n(?P<fm>.*?)\n---\s*\n", re.DOTALL)


@dataclass
class ProjectRule:
    """One project-rule snippet."""

    source: Path
    name: str
    body: str
    apply_to: tuple[str, ...] = ()
    priority: int = 50  # 0 = always last, 100 = always first
    description: str = ""
    always_apply: bool = False
    tags: tuple[str, ...] = ()

    def matches(self, paths: Sequence[str]) -> bool:
        if self.always_apply or not self.apply_to:
            return True
        for path in paths:
            for glob in self.apply_to:
                if fnmatch.fnmatch(path, glob):
                    return True
        return False

    def render(self) -> str:
        head = f"### {self.name}"
        if self.description:
            head += f" — {self.description}"
        return f"{head}\n\n{self.body.strip()}"


# ---- discovery --------------------------------------------------------------


_DEFAULT_TOP_LEVEL = (
    "AGENTS.md",
    "CLAUDE.md",
    "AGENT.md",
    ".agents.md",
)

_DEFAULT_DIRS = (
    ".cursor/rules",
    ".claude/rules",
    ".agents/rules",
)


def _parse_rule(path: Path) -> ProjectRule | None:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return None
    if not text.strip():
        return None
    fm: dict[str, object] = {}
    body = text
    m = _FRONTMATTER_RE.match(text)
    if m:
        try:
            fm = yaml_io.loads(m.group("fm")) or {}
        except Exception:
            fm = {}
        if not isinstance(fm, dict):
            fm = {}
        body = text[m.end():]
    name = str(fm.get("name") or path.stem.replace("_", " ").replace("-", " ").title())
    apply_to_raw = fm.get("apply_to") or fm.get("applyTo") or fm.get("globs") or ()
    if isinstance(apply_to_raw, str):
        apply_to = tuple(s.strip() for s in apply_to_raw.split(",") if s.strip())
    elif isinstance(apply_to_raw, (list, tuple)):
        apply_to = tuple(str(s).strip() for s in apply_to_raw if str(s).strip())
    else:
        apply_to = ()
    tags_raw = fm.get("tags") or ()
    if isinstance(tags_raw, str):
        tags = tuple(s.strip() for s in tags_raw.split(",") if s.strip())
    elif isinstance(tags_raw, (list, tuple)):
        tags = tuple(str(s).strip() for s in tags_raw if str(s).strip())
    else:
        tags = ()
    return ProjectRule(
        source=path,
        name=name,
        body=body.strip(),
        apply_to=apply_to,
        priority=int(fm.get("priority") or 50),
        description=str(fm.get("description") or ""),
        always_apply=bool(fm.get("always_apply") or fm.get("alwaysApply") or False),
        tags=tags,
    )


def load_project_rules(
    root: str | Path,
    *,
    top_level: Iterable[str] = _DEFAULT_TOP_LEVEL,
    dirs: Iterable[str] = _DEFAULT_DIRS,
) -> list[ProjectRule]:
    """Discover and parse all rule files under ``root``.

    Returns rules sorted by ``-priority`` (highest first). Caller
    typically filters by ``apply_to`` at render time using the paths
    the agent is currently touching.
    """

    base = Path(root)
    out: list[ProjectRule] = []
    for name in top_level:
        rule = _parse_rule(base / name)
        if rule is not None:
            rule.always_apply = rule.always_apply or True
            rule.priority = max(rule.priority, 80)
            out.append(rule)
    for d in dirs:
        rules_dir = base / d
        if not rules_dir.exists() or not rules_dir.is_dir():
            continue
        for path in sorted(rules_dir.rglob("*")):
            if not path.is_file():
                continue
            if path.suffix.lower() not in {".md", ".mdx", ".markdown", ".txt"}:
                continue
            rule = _parse_rule(path)
            if rule is not None:
                out.append(rule)
    out.sort(key=lambda r: (-r.priority, r.source.as_posix()))
    return out


def render_rules(
    rules: Iterable[ProjectRule],
    *,
    paths: Sequence[str] | None = None,
    max_chars: int = 8000,
) -> str:
    """Render rules to a single string, applying ``apply_to`` filtering.

    Output is bounded by ``max_chars`` so the layered prompt can never
    explode. When the budget would be exceeded we keep the highest-
    priority rules + the matched-by-path rules and drop the tail with
    a visible "(N rules truncated)" marker so the model knows it is
    looking at a partial view.
    """

    paths = list(paths or ())
    selected: list[ProjectRule] = []
    skipped = 0
    used = 0
    rendered_blocks: list[str] = []
    for r in rules:
        block = r.render()
        if not r.matches(paths):
            # Rule has scoping that does not apply right now; skip it
            # silently — the prompt budget is precious.
            skipped += 1
            continue
        if used + len(block) + 2 > max_chars and rendered_blocks:
            skipped += 1
            continue
        rendered_blocks.append(block)
        selected.append(r)
        used += len(block) + 2
    if skipped:
        rendered_blocks.append(f"_({skipped} rule(s) omitted by scope or budget)_")
    return "\n\n".join(rendered_blocks).strip()
