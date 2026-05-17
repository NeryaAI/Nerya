"""Diff-overlap report between upstream financial-services and Nerya builtins.

Overlap handling is explicit: skills that already exist in Nerya as
dedicated builtins (``dcf_valuation_skill`` in particular) are not
silently re-imported as duplicate finance skills. ``diff-overlap`` can
still produce a side-by-side report, while ``promote-builtins`` attaches
the upstream method as a lazy reference on the existing builtin.

The output is a human-readable Markdown report for review.
"""
from __future__ import annotations

from dataclasses import dataclass
from difflib import SequenceMatcher, unified_diff
from pathlib import Path

from .frontmatter import extract_upstream_description
from .name_map import NERYA_BUILTIN_OVERLAPS


@dataclass(frozen=True)
class OverlapReport:
    upstream_skill_md: Path
    nerya_skill_md: Path
    upstream_vertical: str
    upstream_skill: str
    nerya_skill_id: str
    similarity: float                 # SequenceMatcher ratio of bodies
    upstream_description: str
    upstream_word_count: int
    nerya_word_count: int
    unified_diff_text: str
    upstream_only_section_titles: tuple[str, ...]
    nerya_only_section_titles: tuple[str, ...]


def find_overlap_pairs(
    upstream_root: Path,
    nerya_builtin_root: Path,
) -> list[tuple[Path, Path, str, str, str]]:
    """Return ``(upstream_md, nerya_md, vertical, upstream_skill, nerya_id)`` tuples.

    Only entries declared in :data:`name_map.NERYA_BUILTIN_OVERLAPS` are
    returned — we deliberately don't compute "anything that looks
    similar". Adding a new overlap requires manual review (add the entry
    to ``NERYA_BUILTIN_OVERLAPS`` after auditing).
    """
    out: list[tuple[Path, Path, str, str, str]] = []
    for (vertical, upstream_skill), nerya_id in NERYA_BUILTIN_OVERLAPS.items():
        upstream_md = (
            upstream_root / "plugins" / "vertical-plugins" / vertical
            / "skills" / upstream_skill / "SKILL.md"
        )
        nerya_md = nerya_builtin_root / nerya_id / "SKILL.md"
        if upstream_md.is_file() and nerya_md.is_file():
            out.append((upstream_md, nerya_md, vertical, upstream_skill, nerya_id))
    return out


def diff_one(
    *,
    upstream_md: Path,
    nerya_md: Path,
    upstream_vertical: str,
    upstream_skill: str,
    nerya_id: str,
) -> OverlapReport:
    """Compute one overlap report between an upstream and a Nerya SKILL.md."""
    upstream_text = upstream_md.read_text(encoding="utf-8")
    nerya_text = nerya_md.read_text(encoding="utf-8")

    upstream_desc = extract_upstream_description(upstream_text)
    upstream_words = len(upstream_text.split())
    nerya_words = len(nerya_text.split())

    similarity = SequenceMatcher(None, upstream_text, nerya_text).ratio()

    diff_lines = list(
        unified_diff(
            upstream_text.splitlines(keepends=False),
            nerya_text.splitlines(keepends=False),
            fromfile=f"upstream/{upstream_vertical}/{upstream_skill}",
            tofile=f"nerya/{nerya_id}",
            lineterm="",
            n=2,
        )
    )

    upstream_titles = _section_titles(upstream_text)
    nerya_titles = _section_titles(nerya_text)
    upstream_only = tuple(t for t in upstream_titles if t not in nerya_titles)
    nerya_only = tuple(t for t in nerya_titles if t not in upstream_titles)

    return OverlapReport(
        upstream_skill_md=upstream_md,
        nerya_skill_md=nerya_md,
        upstream_vertical=upstream_vertical,
        upstream_skill=upstream_skill,
        nerya_skill_id=nerya_id,
        similarity=similarity,
        upstream_description=upstream_desc,
        upstream_word_count=upstream_words,
        nerya_word_count=nerya_words,
        unified_diff_text="\n".join(diff_lines),
        upstream_only_section_titles=upstream_only,
        nerya_only_section_titles=nerya_only,
    )


def render_overlap_report_md(reports: list[OverlapReport]) -> str:
    """Render the operator-facing markdown report."""
    if not reports:
        return (
            "# diff-overlap report\n\n"
            "_No overlap pairs configured. Edit "
            "``name_map.NERYA_BUILTIN_OVERLAPS`` to register a pair._\n"
        )

    lines: list[str] = [
        "# diff-overlap report — upstream financial-services ↔ Nerya builtins",
        "",
        "Each row is a skill that exists in both repos. Decide manually whether",
        "the upstream version adds enough value to merge into Nerya's existing",
        "implementation. The importer never auto-edits ``nerya/skills/builtin/``.",
        "",
        "| upstream | nerya | similarity | upstream-only sections | nerya-only sections |",
        "|---|---|---|---|---|",
    ]
    for r in reports:
        lines.append(
            f"| `{r.upstream_vertical}/{r.upstream_skill}` "
            f"| `{r.nerya_skill_id}` | {r.similarity:.2f} "
            f"| {len(r.upstream_only_section_titles)} | {len(r.nerya_only_section_titles)} |"
        )
    lines.append("")

    for r in reports:
        lines.append(f"## `{r.upstream_vertical}/{r.upstream_skill}` ↔ `{r.nerya_skill_id}`")
        lines.append("")
        lines.append(
            f"- upstream path: `{r.upstream_skill_md}`  ({r.upstream_word_count} words)"
        )
        lines.append(
            f"- nerya path:    `{r.nerya_skill_md}`  ({r.nerya_word_count} words)"
        )
        lines.append(f"- similarity (SequenceMatcher): **{r.similarity:.3f}**")
        if r.upstream_description:
            lines.append("")
            lines.append("> upstream description:")
            lines.append(f"> {r.upstream_description}")
        if r.upstream_only_section_titles:
            lines.append("")
            lines.append("**Sections only in upstream**:")
            for t in r.upstream_only_section_titles:
                lines.append(f"  - {t}")
        if r.nerya_only_section_titles:
            lines.append("")
            lines.append("**Sections only in Nerya**:")
            for t in r.nerya_only_section_titles:
                lines.append(f"  - {t}")
        lines.append("")
        lines.append("<details><summary>unified diff (truncated to 4000 chars)</summary>")
        lines.append("")
        lines.append("```diff")
        diff_text = r.unified_diff_text
        if len(diff_text) > 4000:
            diff_text = diff_text[:4000] + "\n…<diff truncated>…"
        lines.append(diff_text)
        lines.append("```")
        lines.append("</details>")
        lines.append("")

    return "\n".join(lines)


def _section_titles(text: str) -> list[str]:
    """Pull H2/H3 section titles out of a markdown body."""
    out: list[str] = []
    for raw in text.splitlines():
        stripped = raw.strip()
        if stripped.startswith("## ") and not stripped.startswith("### "):
            out.append(stripped[3:].strip())
        elif stripped.startswith("### "):
            out.append(stripped[4:].strip())
    return out


__all__ = [
    "OverlapReport",
    "diff_one",
    "find_overlap_pairs",
    "render_overlap_report_md",
]
