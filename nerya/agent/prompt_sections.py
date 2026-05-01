"""Layered prompt assembly for the workspace-native agent loop.

- Mirrors coding-agent's "system message is a stack of named sections"
  pattern. The kernel composes the prompt from explicit, ordered
  sections so individual layers (project rules, skill catalog, tool
  catalog, context budget header) can be re-rendered/truncated
  independently.

The existing ``context_builder.py`` already produces a rich rules
block; this module is *complementary* — it provides the building
blocks that ``WorkspaceNativeAgent`` and the streaming SSE writer use
when they need to emit/inspect prompt layers as data instead of one
opaque string.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable, Sequence

from .project_rules import ProjectRule, render_rules


__all__ = [
    "PromptSection",
    "PromptComposer",
]


@dataclass
class PromptSection:
    """One named layer of the system prompt.

    ``priority`` controls render order (high = top). ``budget_chars``
    is the soft cap; the composer may still drop sections to fit the
    overall prompt budget but tries to preserve high-priority sections
    first.
    """

    name: str
    body: str
    priority: int = 50
    budget_chars: int = 4000
    pinned: bool = False

    def render(self) -> str:
        head = f"## {self.name}"
        return f"{head}\n{self.body.strip()}"


class PromptComposer:
    """Assembles a list of :class:`PromptSection` into a single string."""

    def __init__(
        self,
        *,
        max_chars: int = 24000,
    ) -> None:
        self.max_chars = int(max_chars)
        self._sections: list[PromptSection] = []

    def add(self, section: PromptSection) -> None:
        self._sections.append(section)

    def add_text(self, name: str, body: str, *, priority: int = 50,
                 pinned: bool = False, budget_chars: int = 4000) -> None:
        if not body or not body.strip():
            return
        self.add(PromptSection(
            name=name, body=body, priority=priority,
            pinned=pinned, budget_chars=budget_chars,
        ))

    def add_project_rules(
        self,
        rules: Iterable[ProjectRule],
        *,
        paths: Sequence[str] | None = None,
        budget_chars: int = 8000,
    ) -> None:
        text = render_rules(rules, paths=paths, max_chars=budget_chars)
        self.add_text(
            "Project Rules",
            text,
            priority=85,
            pinned=True,
            budget_chars=budget_chars,
        )

    def render(self) -> str:
        ordered = sorted(
            self._sections,
            key=lambda s: (-int(s.pinned), -s.priority),
        )
        out: list[str] = []
        used = 0
        dropped = 0
        for s in ordered:
            block = s.render()
            cost = len(block) + 2
            if not s.pinned and used + cost > self.max_chars:
                dropped += 1
                continue
            out.append(block)
            used += cost
        if dropped:
            out.append(f"_({dropped} prompt section(s) dropped to fit budget)_")
        return "\n\n".join(out).strip()
