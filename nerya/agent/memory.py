"""Memory accessors for global + strategy-specific learnings.

Previews are *read-only* text summaries of `memory/*.md` and
`strategies/<id>/learnings.md`. Writes are append-only and are meant to be
called from the reflection / self-improvement path (or by operator via CLI)
— never by an untrusted script. unifies the memory semantics:

* ``Memory.append_*`` stays the canonical write path (whitelist-enforced).
* ``Memory.compact_file`` implements TTL-based compaction so persisted
  notes do not grow forever. Sections older than ``max_age_days`` are
  dropped; the remaining sections are preserved in timestamp order.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from ..core.paths import WorkspacePaths


_GLOBAL_WHITELIST = frozenset({
    "global.md", "mistakes.md", "market_regimes.md", "skill_learnings.md",
})
_HEADER_RE = re.compile(r"^##\s+(?P<ts>\S+)\s*$", re.MULTILINE)


@dataclass
class Memory:
    paths: WorkspacePaths

    # ------------------------------------------------------------------ read
    def global_preview(self, *, max_chars: int = 1200) -> str:
        out: list[str] = []
        for name in sorted(_GLOBAL_WHITELIST):
            p: Path = self.paths.memory / name
            if p.exists():
                text = p.read_text(encoding="utf-8")
                out.append(f"### memory/{name}\n{text[-max_chars:]}")
        return "\n\n".join(out)

    def strategy_preview(self, strategy_id: str, *, max_chars: int = 1200) -> str:
        p = self.paths.strategies / strategy_id / "learnings.md"
        if not p.exists():
            return ""
        return (
            f"### strategies/{strategy_id}/learnings.md\n"
            + p.read_text(encoding="utf-8")[-max_chars:]
        )

    # ------------------------------------------------------------------ write
    def _now(self) -> str:
        return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    def append_global(self, name: str, note: str) -> Path:
        assert name in _GLOBAL_WHITELIST, f"unknown memory file: {name}"
        p: Path = self.paths.memory / name
        p.parent.mkdir(parents=True, exist_ok=True)
        header = f"\n\n## {self._now()}\n"
        existing = p.read_text(encoding="utf-8") if p.exists() else ""
        p.write_text(existing + header + note.strip() + "\n", encoding="utf-8")
        return p

    def append_strategy_learning(self, strategy_id: str, note: str) -> Path:
        p = self.paths.strategies / strategy_id / "learnings.md"
        p.parent.mkdir(parents=True, exist_ok=True)
        header = f"\n\n## {self._now()}\n"
        existing = p.read_text(encoding="utf-8") if p.exists() else ""
        p.write_text(existing + header + note.strip() + "\n", encoding="utf-8")
        return p

    # -------------------------------------------------------- compaction
    def compact_file(
        self, path: Path, *,
        max_age_days: float,
        now: datetime | None = None,
    ) -> int:
        """Drop memory sections older than ``max_age_days`` from ``path``.

        Returns the number of sections removed. Sections without a
        parseable ``## <timestamp>`` header are always kept so handwritten
        notes survive the compaction pass.
        """
        if not path.exists():
            return 0
        now = now or datetime.now(timezone.utc)
        text = path.read_text(encoding="utf-8")
        matches = list(_HEADER_RE.finditer(text))
        if not matches:
            return 0
        cutoff = max_age_days * 86400.0
        kept: list[str] = []
        dropped = 0
        # Preamble (before the first header) is always preserved.
        if matches[0].start() > 0:
            preamble = text[:matches[0].start()].rstrip()
            if preamble:
                kept.append(preamble)
        for i, m in enumerate(matches):
            start = m.start()
            end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
            section = text[start:end].rstrip()
            header_ts = m.group("ts")
            try:
                ts = datetime.fromisoformat(header_ts.replace("Z", "+00:00"))
            except Exception:
                kept.append(section)
                continue
            age = (now - ts).total_seconds()
            if age > cutoff:
                dropped += 1
                continue
            kept.append(section)
        new_text = ("\n\n".join(kept)).rstrip() + "\n"
        path.write_text(new_text, encoding="utf-8")
        return dropped

    def compact_all(self, *, max_age_days: float) -> dict[str, int]:
        """Apply :meth:`compact_file` to every global memory file.

        Strategy-scoped ``learnings.md`` files are intentionally *not*
        auto-compacted here — they are compacted only when the owning
        strategy asks for it, so they remain the single source of truth
        for that strategy's history.
        """
        report: dict[str, int] = {}
        for name in sorted(_GLOBAL_WHITELIST):
            p: Path = self.paths.memory / name
            report[name] = self.compact_file(p, max_age_days=max_age_days)
        return report

    def compact_strategy(self, strategy_id: str, *,
                         max_age_days: float) -> int:
        p = self.paths.strategies / strategy_id / "learnings.md"
        return self.compact_file(p, max_age_days=max_age_days)
