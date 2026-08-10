"""Promote financial-services skills into Nerya builtins.

Workspace imports are useful for local experimentation, but the curated
financial-services set is now part of the shipped Nerya skill catalog.
This module keeps the promotion repeatable:

* normal imports land under ``nerya/skills/builtin/finance/<vertical>/<skill>/``;
* the compact ``SKILL.md`` is the always-loaded routing surface;
* the upstream method lives in ``references/full-playbook.md``;
* known overlaps are merged as extra references on existing builtins.
"""
from __future__ import annotations

import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .frontmatter import (
    FrontmatterMeta,
    compose_nerya_description,
    extract_upstream_description,
    render_frontmatter_block,
)
from .name_map import (
    KNOWN_VERTICALS,
    SkillTarget,
    is_blocked,
    overlap_target,
    resolve_target,
)
from .transform import (
    _DESCRIPTION_LINE_RE,
    _iter_extra_assets,
    _relative_repo_path,
    _rename_reference_dir,
    _rewrite_reference_paths,
    _strip_excess_blank_lines,
)


@dataclass(frozen=True)
class BuiltinPromoteResult:
    operation: str
    upstream_vertical: str
    upstream_skill: str
    skill_id: str
    target_path: Path
    reference_path: Path | None = None
    references_renamed: bool = False
    extra_files_copied: tuple[Path, ...] = ()


_VERTICAL_FLOW: dict[str, str] = {
    "equity_research": (
        "coverage question -> ticker/company scope -> filings/earnings/catalysts "
        "-> thesis/risk/update"
    ),
    "financial_analysis": (
        "model/deck/data request -> source workbook/files -> audit/build/check "
        "-> explain deltas"
    ),
    "fund_admin": (
        "fund accounting issue -> period/entity inputs -> tie/reconcile/trace "
        "-> exception commentary"
    ),
    "investment_banking": (
        "deal-material request -> company/process context -> build/check pack "
        "-> banker-ready output"
    ),
    "operations": (
        "KYC/onboarding request -> document/rule source -> parse/check/escalate "
        "-> structured decision support"
    ),
    "private_equity": (
        "deal/portfolio request -> target facts -> diligence/value creation/IC "
        "-> decision-ready memo"
    ),
    "wealth_management": (
        "client portfolio request -> household/account constraints -> analyze "
        "-> advisor-reviewed output"
    ),
}


def promote_finance_builtins(
    *,
    upstream_root: Path,
    builtin_root: Path,
    apply: bool = False,
    license_: str = "Apache-2.0",
    author: str = "Anthropic",
    version: str = "0.0.1",
) -> dict[str, Any]:
    """Promote every curated financial-services vertical skill into builtins."""

    upstream_root = upstream_root.resolve()
    builtin_root = builtin_root.resolve()
    summary: dict[str, Any] = {
        "subcommand": "promote-builtins",
        "upstream_root": str(upstream_root),
        "builtin_root": str(builtin_root),
        "dry_run": not apply,
        "imports": [],
        "merged_overlaps": [],
        "merged_blocked": [],
        "missing_verticals": [],
        "errors": [],
    }

    for vertical in sorted(KNOWN_VERTICALS):
        skill_dirs = _vertical_skill_dirs(upstream_root, vertical)
        if not skill_dirs:
            summary["missing_verticals"].append(vertical)
            continue

        for skill_dir in skill_dirs:
            upstream_skill = skill_dir.name
            try:
                target = resolve_target(vertical, upstream_skill)
                if is_blocked(vertical, upstream_skill):
                    result = merge_into_existing_builtin(
                        upstream_skill_dir=skill_dir,
                        target=target,
                        builtin_root=builtin_root,
                        builtin_id="evolve",
                        upstream_repo_root=upstream_root,
                        apply=apply,
                    )
                    summary["merged_blocked"].append(_result_payload(result))
                    continue

                overlap_id = overlap_target(vertical, upstream_skill)
                if overlap_id:
                    result = merge_into_existing_builtin(
                        upstream_skill_dir=skill_dir,
                        target=target,
                        builtin_root=builtin_root,
                        builtin_id=overlap_id,
                        upstream_repo_root=upstream_root,
                        apply=apply,
                    )
                    summary["merged_overlaps"].append(_result_payload(result))
                    continue

                result = apply_to_builtin_directory(
                    upstream_skill_dir=skill_dir,
                    target=target,
                    builtin_root=builtin_root,
                    upstream_repo_root=upstream_root,
                    license_=license_,
                    author=author,
                    version=version,
                    apply=apply,
                )
                summary["imports"].append(_result_payload(result))
            except Exception as exc:  # pragma: no cover - surfaced as JSON
                summary["errors"].append(
                    {
                        "upstream_vertical": vertical,
                        "upstream_skill": upstream_skill,
                        "error": str(exc),
                    }
                )

    summary["totals"] = {
        "imports": len(summary["imports"]),
        "merged_overlaps": len(summary["merged_overlaps"]),
        "merged_blocked": len(summary["merged_blocked"]),
        "missing_verticals": len(summary["missing_verticals"]),
        "errors": len(summary["errors"]),
    }
    return summary


def apply_to_builtin_directory(
    *,
    upstream_skill_dir: Path,
    target: SkillTarget,
    builtin_root: Path,
    upstream_repo_root: Path | None = None,
    license_: str = "Apache-2.0",
    author: str = "Anthropic",
    version: str = "0.0.1",
    apply: bool = False,
) -> BuiltinPromoteResult:
    """Materialize one non-overlapping finance skill as a compact builtin."""

    upstream_md = upstream_skill_dir / "SKILL.md"
    upstream_text = upstream_md.read_text(encoding="utf-8")
    upstream_repo_path = _relative_repo_path(upstream_md, upstream_repo_root)
    target_dir = _builtin_skill_dir(builtin_root, target)
    target_md = target_dir / "SKILL.md"
    references_dir = target_dir / "references"
    full_playbook = references_dir / "full-playbook.md"

    skill_text = render_compact_builtin_skill_md(
        upstream_text=upstream_text,
        target=target,
        license_=license_,
        author=author,
        version=version,
    )
    full_text = render_full_playbook_md(
        upstream_text=upstream_text,
        target=target,
        upstream_repo_path=upstream_repo_path,
    )

    extra_files: list[Path] = []
    references_renamed = False
    for path in _iter_extra_assets(upstream_skill_dir):
        rel = path.relative_to(upstream_skill_dir)
        new_rel = _rename_reference_dir(rel)
        if new_rel != rel:
            references_renamed = True
        extra_files.append(target_dir / new_rel)

    if apply:
        references_dir.mkdir(parents=True, exist_ok=True)
        target_md.write_text(skill_text, encoding="utf-8")
        full_playbook.write_text(full_text, encoding="utf-8")
        for path in _iter_extra_assets(upstream_skill_dir):
            rel = path.relative_to(upstream_skill_dir)
            dest = target_dir / _rename_reference_dir(rel)
            if dest == full_playbook:
                continue
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(path, dest)

    return BuiltinPromoteResult(
        operation="import",
        upstream_vertical=target.upstream_vertical,
        upstream_skill=target.upstream_skill,
        skill_id=target.namespace_id,
        target_path=target_md,
        reference_path=full_playbook,
        references_renamed=references_renamed,
        extra_files_copied=tuple(extra_files),
    )


def merge_into_existing_builtin(
    *,
    upstream_skill_dir: Path,
    target: SkillTarget,
    builtin_root: Path,
    builtin_id: str,
    upstream_repo_root: Path | None = None,
    apply: bool = False,
) -> BuiltinPromoteResult:
    """Attach an upstream overlapping skill as a lazy reference."""

    upstream_md = upstream_skill_dir / "SKILL.md"
    upstream_text = upstream_md.read_text(encoding="utf-8")
    upstream_repo_path = _relative_repo_path(upstream_md, upstream_repo_root)
    target_dir = builtin_root / builtin_id
    target_md = target_dir / "SKILL.md"
    reference_name = f"financial-services-{target.nerya_vertical}-{target.nerya_skill}.md"
    reference_path = target_dir / "references" / reference_name
    reference_text = render_full_playbook_md(
        upstream_text=upstream_text,
        target=target,
        upstream_repo_path=upstream_repo_path,
    )

    if apply:
        if not target_md.is_file():
            raise FileNotFoundError(f"existing builtin SKILL.md missing: {target_md}")
        reference_path.parent.mkdir(parents=True, exist_ok=True)
        reference_path.write_text(reference_text, encoding="utf-8")
        _ensure_lazy_reference_link(target_md, reference_name)

    return BuiltinPromoteResult(
        operation="merge_reference",
        upstream_vertical=target.upstream_vertical,
        upstream_skill=target.upstream_skill,
        skill_id=builtin_id,
        target_path=target_md,
        reference_path=reference_path,
    )


def render_compact_builtin_skill_md(
    *,
    upstream_text: str,
    target: SkillTarget,
    license_: str,
    author: str,
    version: str,
) -> str:
    upstream_description = extract_upstream_description(upstream_text)
    nerya_description = compose_nerya_description(
        upstream_description,
        upstream_vertical=target.upstream_vertical,
        upstream_skill=target.upstream_skill,
    )
    meta = FrontmatterMeta(
        name=target.namespace_id,
        description=nerya_description,
        license=license_,
        author=author,
        version=version,
    )
    fm = render_frontmatter_block(meta)
    title = _title_from_skill(target.nerya_skill)
    flow = _VERTICAL_FLOW.get(target.nerya_vertical, "request -> inputs -> analysis -> output")
    return "\n".join(
        [
            fm,
            "",
            f"# {title}",
            "",
            (
                f"Use for `{target.namespace_id}`. Keep this file as the routing "
                "surface; load the full method only when the task matches."
            ),
            "",
            "## Flow",
            "",
            f"MATCH -> {flow}.",
            "VERIFY -> inputs, requested deliverable, data freshness, and review boundary.",
            "LOAD -> `references/full-playbook.md` when method details are needed.",
            "EXECUTE -> reuse Nerya tools and local artifacts before creating new structure.",
            "CHECK -> calculations, citations, assumptions, and unresolved risks.",
            "RETURN -> concise output plus files created or evidence used.",
            "",
            "## Lazy References",
            "",
            "- `references/full-playbook.md` for the upstream detailed workflow.",
            "",
        ]
    )


def render_full_playbook_md(
    *,
    upstream_text: str,
    target: SkillTarget,
    upstream_repo_path: str,
) -> str:
    body = _DESCRIPTION_LINE_RE.sub("", upstream_text, count=1)
    body = _strip_excess_blank_lines(_rewrite_reference_paths(body)).strip()
    return "\n".join(
        [
            "# Full Playbook",
            "",
            f"- Source: `financial-services/{target.upstream_vertical}/{target.upstream_skill}`",
            f"- Upstream path: `{upstream_repo_path}`",
            "",
            body,
            "",
        ]
    )


def _vertical_skill_dirs(upstream_root: Path, vertical: str) -> list[Path]:
    skills_dir = upstream_root / "plugins" / "vertical-plugins" / vertical / "skills"
    if not skills_dir.is_dir():
        return []
    return sorted(
        d for d in skills_dir.iterdir()
        if d.is_dir() and (d / "SKILL.md").is_file()
    )


def _builtin_skill_dir(builtin_root: Path, target: SkillTarget) -> Path:
    return builtin_root / "finance" / target.nerya_vertical / target.nerya_skill


def _ensure_lazy_reference_link(skill_md: Path, reference_name: str) -> None:
    text = skill_md.read_text(encoding="utf-8")
    link = f"- `references/{reference_name}` for the financial-services upstream workflow."
    if reference_name in text:
        return
    if "## Lazy References" in text:
        text = text.rstrip() + "\n" + link + "\n"
    else:
        text = text.rstrip() + "\n\n## Lazy References\n\n" + link + "\n"
    skill_md.write_text(text, encoding="utf-8")


def _title_from_skill(skill: str) -> str:
    return " ".join(part.upper() if part in {"kyc", "lbo"} else part.capitalize() for part in skill.split("_"))


def _result_payload(result: BuiltinPromoteResult) -> dict[str, Any]:
    return {
        "operation": result.operation,
        "upstream": f"{result.upstream_vertical}/{result.upstream_skill}",
        "skill_id": result.skill_id,
        "target_path": str(result.target_path),
        "reference_path": str(result.reference_path) if result.reference_path else None,
        "references_renamed": result.references_renamed,
        "extra_files": [str(p) for p in result.extra_files_copied],
    }


__all__ = [
    "BuiltinPromoteResult",
    "apply_to_builtin_directory",
    "merge_into_existing_builtin",
    "promote_finance_builtins",
    "render_compact_builtin_skill_md",
    "render_full_playbook_md",
]
