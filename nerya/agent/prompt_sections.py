"""Layered prompt assembly for the workspace-native agent loop.

- The kernel composes the prompt from explicit, ordered sections so
  individual layers (project rules, skill catalog, tool catalog,
  context budget header) can be re-rendered or truncated independently.

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
    "CACHE_BOUNDARY_LAYER",
    "CACHE_BOUNDARY_MARKER",
    "PromptSection",
    "PromptComposer",
]

# ---------------------------------------------------------------------------
# Cache boundary — layers above this index are byte-identical across turns
# (eligible for provider-side prompt caching); layers below change every
# turn and must be re-sent.
#
#   Layer 0 – Identity            (cached)
#   Layer 1 – Tool behavior       (cached)
#   Layer 2 – Skills index        (cached)
#   Layer 3 – Frozen memory       (cached until the snapshot changes)
#   ---- CACHE_BOUNDARY ----
#   Layer 4 – Timestamp           (uncached)
#   Layer 5 – Task progress       (uncached)
#   Layer 6 – Transcript          (uncached)
# ---------------------------------------------------------------------------
CACHE_BOUNDARY_LAYER: int = 3
CACHE_BOUNDARY_MARKER: str = "--- CACHE_BOUNDARY ---"


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
    cache_boundary: bool = False

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

    def add_cache_boundary(self) -> None:
        """Insert a cache-boundary marker section.

        Everything rendered *above* this section (layers 0-2) is
        byte-identical across turns and eligible for prompt caching.
        Everything *below* (layers 3-6) changes every turn.

        The marker line is what downstream logs/tests use to verify
        where the stable prefix ends.
        """
        self.add(PromptSection(
            name="Cache Boundary",
            body=CACHE_BOUNDARY_MARKER,
            priority=0,
            budget_chars=0,
            pinned=True,
            cache_boundary=True,
        ))

    def render(self) -> str:
        ordered = sorted(
            self._sections,
            key=lambda s: (-int(s.pinned), -s.priority),
        )
        out: list[str] = []
        used = 0
        dropped = 0
        boundary_inserted = False
        for s in ordered:
            if s.cache_boundary:
                # Record the position — the marker will be emitted after
                # all cached (pinned + high-priority) sections that appear
                # before it in the ordered list.
                boundary_inserted = True
                out.append(s.body)
                used += len(s.body) + 2
                continue
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
