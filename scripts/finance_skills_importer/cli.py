"""Operator-facing CLI for ``finance_skills_importer``.

Subcommands
-----------
``import``        Convert one upstream vertical into Nerya skills (dry-run by default)
``import-routes`` Render side-car ``finance.<vertical>.routes.yml`` from upstream commands
``import-agent``  Convert one upstream agent-plugin into a Nerya subagent
``diff-overlap``  Produce a markdown report comparing upstream skills with Nerya builtins
``promote-builtins`` Promote curated finance skills into shipped Nerya builtins
``promote``       Run import + import-routes + import-agent for every known vertical
                  and the curated agent-plugin set in one shot. Default target is
                  ``~/.nerya/<profile>/``; pass ``--workspace`` to override.

Every subcommand prints a JSON summary to stdout so it composes with
``jq`` and the future Nerya operator dashboard. Filesystem writes are
gated by ``--apply``; without ``--apply`` the command prints what it
would do without touching the workspace.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Iterable

from .agent_to_subagent import derive_subagent, write_subagent
from .builtin_promoter import promote_finance_builtins
from .command_to_route import CommandRoute, derive_route, render_routes_yml
from .diff_overlap import (
    diff_one,
    find_overlap_pairs,
    render_overlap_report_md,
)
from .name_map import (
    KNOWN_VERTICALS,
    NERYA_BUILTIN_OVERLAPS,
    UnknownVerticalError,
    is_blocked,
    overlap_target,
    resolve_target,
)
from .transform import apply_to_directory


# ---------- helpers ---------------------------------------------------------


def _vertical_skill_dirs(upstream_root: Path, vertical: str) -> list[Path]:
    skills_dir = (
        upstream_root / "plugins" / "vertical-plugins" / vertical / "skills"
    )
    if not skills_dir.is_dir():
        return []
    return sorted(d for d in skills_dir.iterdir() if d.is_dir() and (d / "SKILL.md").is_file())


def _vertical_command_files(upstream_root: Path, vertical: str) -> list[Path]:
    cmd_dir = (
        upstream_root / "plugins" / "vertical-plugins" / vertical / "commands"
    )
    if not cmd_dir.is_dir():
        return []
    return sorted(p for p in cmd_dir.iterdir() if p.suffix == ".md")


def _emit_json(payload: dict[str, Any]) -> None:
    sys.stdout.write(json.dumps(payload, ensure_ascii=False, indent=2, default=str))
    sys.stdout.write("\n")


# ---------- subcommands -----------------------------------------------------


def cmd_import(args: argparse.Namespace) -> int:
    upstream_root = args.upstream_root.resolve()
    workspace = args.workspace.resolve()

    try:
        skill_dirs = _vertical_skill_dirs(upstream_root, args.vertical)
    except UnknownVerticalError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    if not skill_dirs:
        print(
            f"error: no skills found at "
            f"{upstream_root}/plugins/vertical-plugins/{args.vertical}/skills/",
            file=sys.stderr,
        )
        return 2

    if args.vertical not in KNOWN_VERTICALS:
        print(
            f"error: vertical {args.vertical!r} not in importer allow-list "
            f"({sorted(KNOWN_VERTICALS)})",
            file=sys.stderr,
        )
        return 2

    summary: dict[str, Any] = {
        "subcommand": "import",
        "vertical": args.vertical,
        "upstream_root": str(upstream_root),
        "workspace_root": str(workspace),
        "dry_run": not args.apply,
        "imports": [],
        "skipped_overlaps": [],
        "skipped_blocked": [],
        "errors": [],
    }

    for skill_dir in skill_dirs:
        upstream_skill = skill_dir.name
        if is_blocked(args.vertical, upstream_skill):
            summary["skipped_blocked"].append(
                {
                    "upstream_skill": upstream_skill,
                    "reason": "in DO_NOT_IMPORT allow-list "
                              "(conceptual conflict with Nerya runtime)",
                }
            )
            continue
        overlap_id = overlap_target(args.vertical, upstream_skill)
        if overlap_id and not args.include_overlaps:
            summary["skipped_overlaps"].append(
                {
                    "upstream_skill": upstream_skill,
                    "nerya_builtin_id": overlap_id,
                    "reason": "diff-overlap candidate; rerun with --include-overlaps to import anyway",
                }
            )
            continue

        try:
            target = resolve_target(args.vertical, upstream_skill)
        except (UnknownVerticalError, ValueError) as exc:
            summary["errors"].append({"upstream_skill": upstream_skill, "error": str(exc)})
            continue

        try:
            result = apply_to_directory(
                upstream_skill_dir=skill_dir,
                target=target,
                workspace_root=workspace,
                dry_run=not args.apply,
                license_=args.license,
                author=args.author,
            )
        except Exception as exc:  # pragma: no cover — surfaced as JSON
            summary["errors"].append({"upstream_skill": upstream_skill, "error": str(exc)})
            continue

        summary["imports"].append(
            {
                "upstream_skill": upstream_skill,
                "nerya_skill_id": target.skill_id,
                "namespace_id": target.namespace_id,
                "rel_skill_md": str(target.rel_skill_md),
                "references_renamed": result.references_singular_to_plural,
                "extra_files": [str(p) for p in result.extra_files_copied],
                "upstream_description_chars": len(result.upstream_description),
                "nerya_description_chars": len(result.nerya_description),
            }
        )

    _emit_json(summary)
    return 0 if not summary["errors"] else 1


def cmd_import_routes(args: argparse.Namespace) -> int:
    upstream_root = args.upstream_root.resolve()
    workspace = args.workspace.resolve()

    cmd_files = _vertical_command_files(upstream_root, args.vertical)
    if not cmd_files:
        # Some verticals (fund-admin, operations) ship no slash-commands
        # upstream — they expose only skills + agent-plugins. Soft-skip
        # so batch imports keep going.
        _emit_json(
            {
                "subcommand": "import-routes",
                "vertical": args.vertical,
                "dry_run": not args.apply,
                "routes_count": 0,
                "out_path": None,
                "skipped": [],
                "routes": [],
                "note": (
                    f"upstream vertical '{args.vertical}' has no commands/ "
                    "directory; nothing to render"
                ),
            }
        )
        return 0

    routes: list[CommandRoute] = []
    skipped: list[dict[str, Any]] = []
    for cmd_md in cmd_files:
        upstream_token = cmd_md.stem
        try:
            target = resolve_target(args.vertical, upstream_token)
        except (UnknownVerticalError, ValueError) as exc:
            skipped.append({"command": upstream_token, "reason": str(exc)})
            continue
        routes.append(derive_route(upstream_command_md=cmd_md, skill_target=target))

    vertical_snake = args.vertical.replace("-", "_")
    out_path = workspace / "triggers" / f"finance.{vertical_snake}.routes.yml"
    rendered = render_routes_yml(routes)

    if args.apply:
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(rendered, encoding="utf-8")

    _emit_json(
        {
            "subcommand": "import-routes",
            "vertical": args.vertical,
            "dry_run": not args.apply,
            "routes_count": len(routes),
            "out_path": str(out_path),
            "skipped": skipped,
            "routes": [
                {
                    "id": r.route_id,
                    "namespace_id": r.namespace_id,
                    "argument_hint": r.argument_hint,
                    "description": r.description,
                }
                for r in routes
            ],
        }
    )
    if not args.apply:
        sys.stderr.write(
            f"\n# Preview of finance.{vertical_snake}.routes.yml "
            "(use --apply to write):\n"
        )
        sys.stderr.write(rendered)
        sys.stderr.write("\n")
    return 0


def cmd_import_agent(args: argparse.Namespace) -> int:
    upstream_root = args.upstream_root.resolve()
    workspace = args.workspace.resolve()

    upstream_md = (
        upstream_root / "plugins" / "agent-plugins" / args.agent / "agents" / f"{args.agent}.md"
    )
    if not upstream_md.is_file():
        print(f"error: upstream agent prompt not found: {upstream_md}", file=sys.stderr)
        return 2

    sub = derive_subagent(upstream_md, vertical_namespace=args.vertical_namespace)
    agent_md_path, role_yaml_path = write_subagent(
        sub=sub, workspace_root=workspace, dry_run=not args.apply
    )

    _emit_json(
        {
            "subcommand": "import-agent",
            "agent": args.agent,
            "nerya_subagent_name": sub.nerya_name,
            "tier": sub.tier,
            "dry_run": not args.apply,
            "agent_md_path": str(agent_md_path),
            "role_yaml_path": str(role_yaml_path),
            "allowed_skills": list(sub.allowed_skills),
            "referenced_finance_skills": list(sub.referenced_skill_ids),
            "upstream_tools": list(sub.upstream_tools),
        }
    )
    return 0


#: Curated agent-plugin → vertical-namespace mapping. Each entry results
#: in one ``<workspace>/subagents/<snake>.agent.md`` + ``<snake>.role.yaml``.
#: ``market-researcher`` is intentionally namespaced under
#: ``financial_analysis`` because that is the vertical whose comps /
#: sector-overview / pptx-author skills it actually invokes (per the
#: upstream agent prompt body).
_PROMOTE_AGENT_BUNDLE: list[tuple[str, str]] = [
    ("market-researcher", "financial_analysis"),
    ("earnings-reviewer", "equity_research"),
    ("pitch-agent", "investment_banking"),
    ("meeting-prep-agent", "private_equity"),
    ("valuation-reviewer", "financial_analysis"),
    ("model-builder", "financial_analysis"),
    ("statement-auditor", "fund_admin"),
    ("month-end-closer", "fund_admin"),
    ("gl-reconciler", "fund_admin"),
    ("kyc-screener", "operations"),
]


def cmd_promote(args: argparse.Namespace) -> int:
    """Run the full PE+ER+WM+IB+FA+fund-admin+ops + 10 subagents bundle.

    Designed to be the one operator-facing command after a fresh
    ``nerya init``: pass ``--workspace ~/.nerya/<profile>`` and you get
    a workspace that already contains every importable upstream skill,
    a per-vertical staging routes file, every agent-plugin lifted to a
    persistent role, and a fresh diff-overlap report.
    """
    upstream_root = args.upstream_root.resolve()
    workspace = args.workspace.resolve()

    summary: dict[str, Any] = {
        "subcommand": "promote",
        "workspace": str(workspace),
        "upstream_root": str(upstream_root),
        "dry_run": not args.apply,
        "verticals": [],
        "agents": [],
        "diff_overlap_path": None,
        "totals": {},
    }

    total_imports = 0
    total_overlap_skips = 0
    total_blocked_skips = 0
    total_errors = 0
    total_routes = 0

    for vertical in sorted(KNOWN_VERTICALS):
        skill_dirs = _vertical_skill_dirs(upstream_root, vertical)
        if not skill_dirs:
            summary["verticals"].append(
                {"vertical": vertical, "skipped": True,
                 "reason": "vertical missing from upstream checkout"}
            )
            continue

        imports: list[dict[str, Any]] = []
        overlap_skips: list[dict[str, Any]] = []
        blocked_skips: list[dict[str, Any]] = []
        errors: list[dict[str, Any]] = []
        for skill_dir in skill_dirs:
            upstream_skill = skill_dir.name
            if is_blocked(vertical, upstream_skill):
                blocked_skips.append(
                    {"upstream_skill": upstream_skill,
                     "reason": "DO_NOT_IMPORT"}
                )
                continue
            overlap_id = overlap_target(vertical, upstream_skill)
            if overlap_id and not args.include_overlaps:
                overlap_skips.append(
                    {"upstream_skill": upstream_skill,
                     "nerya_builtin_id": overlap_id}
                )
                continue
            try:
                target = resolve_target(vertical, upstream_skill)
            except (UnknownVerticalError, ValueError) as exc:
                errors.append(
                    {"upstream_skill": upstream_skill, "error": str(exc)}
                )
                continue
            try:
                result = apply_to_directory(
                    upstream_skill_dir=skill_dir,
                    target=target,
                    workspace_root=workspace,
                    dry_run=not args.apply,
                    license_=args.license,
                    author=args.author,
                )
            except Exception as exc:  # pragma: no cover
                errors.append(
                    {"upstream_skill": upstream_skill, "error": str(exc)}
                )
                continue
            imports.append(
                {
                    "upstream_skill": upstream_skill,
                    "nerya_skill_id": target.skill_id,
                    "rel_skill_md": str(target.rel_skill_md),
                    "references_renamed": result.references_singular_to_plural,
                }
            )

        # Render routes (best-effort: missing commands/ → soft-skip).
        cmd_files = _vertical_command_files(upstream_root, vertical)
        routes: list[CommandRoute] = []
        for cmd_md in cmd_files:
            try:
                target = resolve_target(vertical, cmd_md.stem)
            except (UnknownVerticalError, ValueError):
                continue
            routes.append(derive_route(upstream_command_md=cmd_md, skill_target=target))
        if routes:
            vertical_snake = vertical.replace("-", "_")
            out_path = workspace / "triggers" / f"finance.{vertical_snake}.routes.yml"
            if args.apply:
                out_path.parent.mkdir(parents=True, exist_ok=True)
                out_path.write_text(render_routes_yml(routes), encoding="utf-8")

        summary["verticals"].append(
            {
                "vertical": vertical,
                "imports": len(imports),
                "overlap_skips": len(overlap_skips),
                "blocked_skips": len(blocked_skips),
                "errors": len(errors),
                "routes": len(routes),
                "overlap_skip_detail": overlap_skips,
                "blocked_skip_detail": blocked_skips,
                "error_detail": errors,
            }
        )
        total_imports += len(imports)
        total_overlap_skips += len(overlap_skips)
        total_blocked_skips += len(blocked_skips)
        total_errors += len(errors)
        total_routes += len(routes)

    # Subagent bundle.
    for agent_slug, vertical_namespace in _PROMOTE_AGENT_BUNDLE:
        upstream_md = (
            upstream_root / "plugins" / "agent-plugins" / agent_slug
            / "agents" / f"{agent_slug}.md"
        )
        if not upstream_md.is_file():
            summary["agents"].append(
                {"agent": agent_slug, "skipped": True,
                 "reason": "upstream agent prompt not found"}
            )
            continue
        sub = derive_subagent(upstream_md, vertical_namespace=vertical_namespace)
        agent_md, role_yaml = write_subagent(
            sub=sub, workspace_root=workspace, dry_run=not args.apply,
        )
        summary["agents"].append(
            {
                "agent": agent_slug,
                "nerya_subagent_name": sub.nerya_name,
                "tier": sub.tier,
                "vertical_namespace": vertical_namespace,
                "agent_md": str(agent_md),
                "role_yaml": str(role_yaml),
            }
        )

    # Diff-overlap report.
    pairs = find_overlap_pairs(upstream_root, args.nerya_builtin_root.resolve())
    reports = [
        diff_one(
            upstream_md=u, nerya_md=n,
            upstream_vertical=v, upstream_skill=s, nerya_id=nid,
        )
        for u, n, v, s, nid in pairs
    ]
    diff_path = workspace / "diff_overlap.md"
    if args.apply and reports:
        diff_path.parent.mkdir(parents=True, exist_ok=True)
        diff_path.write_text(render_overlap_report_md(reports), encoding="utf-8")
    summary["diff_overlap_path"] = str(diff_path) if reports else None
    summary["diff_overlap_pairs"] = len(reports)

    summary["totals"] = {
        "imports": total_imports,
        "overlap_skips": total_overlap_skips,
        "blocked_skips": total_blocked_skips,
        "routes": total_routes,
        "subagents": sum(1 for a in summary["agents"] if not a.get("skipped")),
        "errors": total_errors,
    }
    _emit_json(summary)
    return 0 if total_errors == 0 else 1


def cmd_diff_overlap(args: argparse.Namespace) -> int:
    upstream_root = args.upstream_root.resolve()
    nerya_builtin_root = args.nerya_builtin_root.resolve()

    pairs = find_overlap_pairs(upstream_root, nerya_builtin_root)
    if not pairs:
        sys.stderr.write(
            "warning: no overlap pairs found. Verify "
            "name_map.NERYA_BUILTIN_OVERLAPS, upstream_root, and "
            "nerya_builtin_root.\n"
        )
    reports = [diff_one(
        upstream_md=u, nerya_md=n,
        upstream_vertical=v, upstream_skill=s, nerya_id=nid,
    ) for u, n, v, s, nid in pairs]

    md = render_overlap_report_md(reports)
    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(md, encoding="utf-8")
    else:
        sys.stdout.write(md)

    _emit_json(
        {
            "subcommand": "diff-overlap",
            "pairs_known": len(NERYA_BUILTIN_OVERLAPS),
            "pairs_resolved": len(pairs),
            "out_path": str(args.out) if args.out else None,
            "reports": [
                {
                    "upstream": f"{r.upstream_vertical}/{r.upstream_skill}",
                    "nerya_id": r.nerya_skill_id,
                    "similarity": round(r.similarity, 3),
                }
                for r in reports
            ],
        }
    )
    return 0


def cmd_promote_builtins(args: argparse.Namespace) -> int:
    summary = promote_finance_builtins(
        upstream_root=args.upstream_root,
        builtin_root=args.nerya_builtin_root,
        apply=args.apply,
        license_=args.license,
        author=args.author,
    )
    _emit_json(summary)
    return 0 if not summary["errors"] else 1


# ---------- argparse glue ---------------------------------------------------


def _default_upstream_root() -> Path:
    return Path(__file__).resolve().parents[3] / "financial-services"


def _default_nerya_builtin_root() -> Path:
    return Path(__file__).resolve().parents[2] / "nerya" / "skills" / "builtin"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="finance_skills_importer",
        description=(
            "Curated import of upstream financial-services SKILL.md files "
            "into a Nerya operator workspace."
        ),
    )
    sub = parser.add_subparsers(dest="subcommand", required=True)

    common_paths = argparse.ArgumentParser(add_help=False)
    common_paths.add_argument(
        "--upstream-root", type=Path, default=_default_upstream_root(),
        help="Path to a checkout of anthropics/financial-services.",
    )
    common_paths.add_argument(
        "--workspace", type=Path,
        default=Path.home() / ".nerya",
        help="Path to the Nerya workspace root (default: ~/.nerya).",
    )

    p_import = sub.add_parser(
        "import", parents=[common_paths],
        help="Convert one upstream vertical's skills into Nerya workspace skills.",
    )
    p_import.add_argument("--vertical", required=True, choices=sorted(KNOWN_VERTICALS))
    p_import.add_argument("--apply", action="store_true",
                          help="Actually write to the workspace (default: dry-run).")
    p_import.add_argument("--include-overlaps", action="store_true",
                          help="Also import skills that overlap a Nerya builtin.")
    p_import.add_argument("--license", default="Apache-2.0")
    p_import.add_argument("--author", default="Anthropic")
    p_import.set_defaults(func=cmd_import)

    p_routes = sub.add_parser(
        "import-routes", parents=[common_paths],
        help="Generate triggers/finance.<vertical>.routes.yml from upstream slash-commands.",
    )
    p_routes.add_argument("--vertical", required=True, choices=sorted(KNOWN_VERTICALS))
    p_routes.add_argument("--apply", action="store_true")
    p_routes.set_defaults(func=cmd_import_routes)

    p_agent = sub.add_parser(
        "import-agent", parents=[common_paths],
        help="Convert one upstream agent-plugin into a Nerya workspace subagent.",
    )
    p_agent.add_argument("--agent", required=True,
                         help="Upstream agent slug, e.g. 'market-researcher'.")
    p_agent.add_argument("--vertical-namespace", default=None,
                         help="If set, finance-skill ids referenced by the agent are "
                              "namespaced as finance.<vertical>.<skill>.")
    p_agent.add_argument("--apply", action="store_true")
    p_agent.set_defaults(func=cmd_import_agent)

    p_promote = sub.add_parser(
        "promote", parents=[common_paths],
        help=(
            "One-shot: import every known vertical + every curated agent-plugin "
            "+ refresh diff-overlap report into one workspace."
        ),
    )
    p_promote.add_argument(
        "--apply", action="store_true",
        help="Actually write to the workspace (default: dry-run).",
    )
    p_promote.add_argument(
        "--include-overlaps", action="store_true",
        help="Also import skills that overlap a Nerya builtin.",
    )
    p_promote.add_argument(
        "--license", default="Apache-2.0",
        help="Default license stamp for imported skills.",
    )
    p_promote.add_argument(
        "--author", default="Anthropic",
        help="Default author stamp for imported skills.",
    )
    p_promote.add_argument(
        "--nerya-builtin-root", type=Path,
        default=_default_nerya_builtin_root(),
        help="Path to nerya/skills/builtin/ for the diff-overlap step.",
    )
    p_promote.set_defaults(func=cmd_promote)

    p_builtin = sub.add_parser(
        "promote-builtins",
        help=(
            "Promote all curated financial-services skills into "
            "nerya/skills/builtin/finance and merge known overlaps as references."
        ),
    )
    p_builtin.add_argument(
        "--upstream-root", type=Path, default=_default_upstream_root(),
        help="Path to a checkout of anthropics/financial-services.",
    )
    p_builtin.add_argument(
        "--nerya-builtin-root", type=Path,
        default=_default_nerya_builtin_root(),
        help="Path to nerya/skills/builtin/.",
    )
    p_builtin.add_argument(
        "--apply", action="store_true",
        help="Actually write builtins (default: dry-run).",
    )
    p_builtin.add_argument("--license", default="Apache-2.0")
    p_builtin.add_argument("--author", default="Anthropic")
    p_builtin.set_defaults(func=cmd_promote_builtins)

    p_diff = sub.add_parser(
        "diff-overlap",
        help="Render markdown report comparing upstream skills with Nerya builtins.",
    )
    p_diff.add_argument(
        "--upstream-root", type=Path, default=_default_upstream_root(),
    )
    p_diff.add_argument(
        "--nerya-builtin-root", type=Path, default=_default_nerya_builtin_root(),
    )
    p_diff.add_argument("--out", type=Path, default=None,
                        help="Write the report to this file instead of stdout.")
    p_diff.set_defaults(func=cmd_diff_overlap)

    return parser


def main(argv: Iterable[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(list(argv) if argv is not None else None)
    return args.func(args)


__all__ = ["build_parser", "main"]


if __name__ == "__main__":  # pragma: no cover - CLI entrypoint
    raise SystemExit(main())
