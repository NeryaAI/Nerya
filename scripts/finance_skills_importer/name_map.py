"""Name & path conversion between upstream financial-services and Nerya.

Conventions (locked in ``deliverable.md`` after USER decision round 1):

* Upstream uses kebab-case directories: ``investment-banking/``,
  ``ic-memo/``. Nerya uses snake_case throughout.
* USER decision round 1 chose **no ``_skill`` suffix** — finance imports
  are aligned with Nerya core skills (``trading``, ``research``, …) which
  also have no suffix, rather than the newer ``equity_research_skill``
  shape.
* USER decision round 1 chose **deep namespacing**:
  ``workspace/skills/finance/<vertical>/<skill>/``.

This module is pure string/path manipulation — no I/O — so it is easy to
unit-test and to compose with the rest of the importer.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

#: Top-level namespace under ``workspace/skills/`` that finance imports go
#: under. Hard-coded — operators who want a different prefix can fork the
#: importer or land the resulting tree somewhere else themselves.
NAMESPACE_ROOT = "finance"

#: Verticals shipped by the upstream financial-services plugin set as of
#: the date this importer was authored. Only entries enumerated here are
#: importable; everything else falls into ``UNKNOWN_VERTICAL`` so the
#: operator notices the upstream added a vertical the importer has not
#: been audited against.
KNOWN_VERTICALS: frozenset[str] = frozenset(
    {
        "financial-analysis",
        "investment-banking",
        "equity-research",
        "private-equity",
        "wealth-management",
        "fund-admin",
        "operations",
    }
)


class UnknownVerticalError(ValueError):
    """Raised when an upstream vertical is not in the curated allow-list."""


_INVALID_CHARS_RE = re.compile(r"[^a-z0-9_]+")


def to_snake(token: str) -> str:
    """Convert a kebab- or mixed-case token to snake_case.

    Examples
    --------
    >>> to_snake("ic-memo")
    'ic_memo'
    >>> to_snake("3-statement-model")
    '3_statement_model'
    >>> to_snake("KYC-doc-parse")
    'kyc_doc_parse'
    """
    s = token.strip().lower().replace("-", "_")
    s = _INVALID_CHARS_RE.sub("_", s)
    return s.strip("_")


@dataclass(frozen=True)
class SkillTarget:
    """The full set of Nerya-side identifiers + paths for one upstream skill."""

    upstream_vertical: str       # e.g. "private-equity"
    upstream_skill: str          # e.g. "ic-memo"
    nerya_vertical: str          # e.g. "private_equity"
    nerya_skill: str             # e.g. "ic_memo"
    skill_id: str                # e.g. "ic_memo"  (Nerya frontmatter `name`)
    namespace_id: str            # e.g. "finance.private_equity.ic_memo"
    rel_skill_dir: Path          # e.g. Path("skills/finance/private_equity/ic_memo")
    rel_skill_md: Path           # e.g. Path("skills/finance/private_equity/ic_memo/SKILL.md")

    def absolute_skill_md(self, workspace_root: Path) -> Path:
        return workspace_root / self.rel_skill_md

    def absolute_skill_dir(self, workspace_root: Path) -> Path:
        return workspace_root / self.rel_skill_dir


def resolve_target(upstream_vertical: str, upstream_skill: str) -> SkillTarget:
    """Compute the Nerya-side identifiers/paths for one upstream skill.

    Raises
    ------
    UnknownVerticalError
        If ``upstream_vertical`` is not in :data:`KNOWN_VERTICALS`.
    ValueError
        If ``upstream_skill`` is empty / not a clean kebab token.
    """
    if upstream_vertical not in KNOWN_VERTICALS:
        raise UnknownVerticalError(
            f"vertical '{upstream_vertical}' not in importer allow-list "
            f"({sorted(KNOWN_VERTICALS)}). Update name_map.KNOWN_VERTICALS "
            "after auditing the new vertical's SKILL.md files."
        )
    if not upstream_skill or upstream_skill.startswith("-"):
        raise ValueError(f"invalid upstream skill token: {upstream_skill!r}")

    nerya_vertical = to_snake(upstream_vertical)
    nerya_skill = to_snake(upstream_skill)
    if not nerya_skill:
        raise ValueError(
            f"upstream skill {upstream_skill!r} produced empty snake_case id"
        )

    rel_skill_dir = Path("skills") / NAMESPACE_ROOT / nerya_vertical / nerya_skill
    return SkillTarget(
        upstream_vertical=upstream_vertical,
        upstream_skill=upstream_skill,
        nerya_vertical=nerya_vertical,
        nerya_skill=nerya_skill,
        skill_id=nerya_skill,
        namespace_id=f"{NAMESPACE_ROOT}.{nerya_vertical}.{nerya_skill}",
        rel_skill_dir=rel_skill_dir,
        rel_skill_md=rel_skill_dir / "SKILL.md",
    )


#: Skills already implemented natively under ``nerya/skills/builtin/`` whose
#: upstream counterpart should be diff-reviewed (USER decision 5 =
#: diff_and_merge) rather than silently re-imported. Keys are the upstream
#: ``<vertical>/<skill>`` pair; values are the Nerya builtin id to compare
#: against. Importer ``import`` skips these by default and the future
#: ``diff-overlap`` subcommand operates on this map.
NERYA_BUILTIN_OVERLAPS: dict[tuple[str, str], str] = {
    ("financial-analysis", "dcf-model"): "dcf_valuation_skill",
    # ``initiating-coverage`` is the upstream institutional 5-task report
    # workflow (research → model → valuation → charts → DOCX). Nerya's
    # ``equity_research_skill`` is the lighter "fetch financials + memo"
    # playbook from dexter. Scopes overlap on the research/valuation
    # synthesis but the upstream version adds the institutional report
    # formatter Nerya does not yet have — so we surface it through
    # ``diff-overlap`` rather than silently re-import it.
    ("equity-research", "initiating-coverage"): "equity_research_skill",
    # Equity-research ``earnings-analysis`` / ``earnings-preview`` /
    # ``model-update`` / ``catalyst-calendar`` / ``morning-note`` /
    # ``thesis-tracker`` / ``idea-generation`` / ``sector-overview`` are
    # *new* capability Nerya does not have (lifecycle / coverage updates,
    # not the initial deep dive) — they import normally.
}


def overlap_target(upstream_vertical: str, upstream_skill: str) -> str | None:
    """Return the Nerya builtin id that overlaps with an upstream skill,
    or ``None`` if there is no known overlap."""
    return NERYA_BUILTIN_OVERLAPS.get((upstream_vertical, upstream_skill))


#: Upstream skills the importer **never** auto-imports, even with
#: ``--include-overlaps``. These are conceptual conflicts with Nerya's
#: own runtime architecture rather than methodology overlaps:
#:
#: * ``financial-analysis/skill-creator`` — generic Anthropic Skills
#:   meta-skill; Nerya already has its own skill-authoring surface
#:   (``evolve``, ``evolution.skill_generator``, the workspace-first
#:   patch-proposal flow) and importing a parallel one would muddy the
#:   "single source of truth" rule for skill authoring.
DO_NOT_IMPORT: frozenset[tuple[str, str]] = frozenset(
    {
        ("financial-analysis", "skill-creator"),
    }
)


def is_blocked(upstream_vertical: str, upstream_skill: str) -> bool:
    """True when a skill is in the importer's never-import allow-list."""
    return (upstream_vertical, upstream_skill) in DO_NOT_IMPORT


#: Default ``risk_class`` per vertical. The Nerya skill manifest does not
#: enforce risk class semantically — it just persists the field — but the
#: agent loop reads the frontmatter when deciding which approval gate to
#: apply. The compact builtin importer preserves the same field on every
#: promoted finance skill.
#:
#: Tier rationale:
#: * ``low``    — analyst output only, no PII, no client data
#:                (private-equity, equity-research, financial-analysis,
#:                investment-banking).
#: * ``medium`` — touches client PII / account data / tax events
#:                (wealth-management, fund-admin).
#: * ``high``   — KYC / onboarding / regulator-facing surface
#:                (operations).
DEFAULT_RISK_CLASS_BY_VERTICAL: dict[str, str] = {
    "financial-analysis": "low",
    "investment-banking": "low",
    "equity-research": "low",
    "private-equity": "low",
    "wealth-management": "medium",
    "fund-admin": "medium",
    "operations": "high",
}


def default_risk_class(upstream_vertical: str) -> str:
    """Return the default risk_class for an upstream vertical."""
    return DEFAULT_RISK_CLASS_BY_VERTICAL.get(upstream_vertical, "low")
