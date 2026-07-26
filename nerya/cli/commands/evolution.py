"""Self-evolution commands: ``reflect``, ``evolve``, ``proposals``,
``scripts``.

These are the only subcommands that can *mutate* agent-authored
artifacts, so they always go through the ``PatchProposal`` or
``scripts.proposal.promote`` pipeline — never a direct write.
"""

from __future__ import annotations

from .._common import _add_ws, _client, _print
from ...core import yaml_io
from ...evolution import patch_proposal, promotion, rollback as rollback_mod


# ----------------------------------------------------------------- reflect / evolve
def cmd_reflect(args) -> int:
    from ...evolution.reflection_engine import run_reflection
    client = _client(args.workspace, getattr(args, "profile", None))
    _print(run_reflection(client.config.paths, config=client.config))
    return 0


def cmd_evolve(args) -> int:
    from ...evolution.runner import evolve
    client = _client(args.workspace, getattr(args, "profile", None))
    _print(evolve(client.config))
    return 0


def cmd_signals(args) -> int:
    from ...evolution.event_store import list_signals
    from ...evolution.signals import collect_signals

    client = _client(args.workspace, getattr(args, "profile", None))
    if getattr(args, "refresh", False):
        collect_signals(
            client.config.paths,
            strategy_id=getattr(args, "strategy", None),
            persist=True,
            limit=int(getattr(args, "limit", 100) or 100),
        )
    _print(
        {
            "signals": list_signals(
                client.config.paths,
                strategy_id=getattr(args, "strategy", None),
                source=getattr(args, "source", None),
                severity=getattr(args, "severity", None),
                kind=getattr(args, "kind", None),
                limit=int(getattr(args, "limit", 100) or 100),
            )
        }
    )
    return 0


def cmd_events(args) -> int:
    from ...evolution.event_store import list_events

    client = _client(args.workspace, getattr(args, "profile", None))
    _print(
        {
            "events": list_events(
                client.config.paths,
                strategy_id=getattr(args, "strategy", None),
                proposal_id=getattr(args, "proposal", None),
                outcome=getattr(args, "outcome", None),
                limit=int(getattr(args, "limit", 100) or 100),
            )
        }
    )
    return 0


def cmd_assets_search(args) -> int:
    from ...evolution.assets import list_candidates, search_assets

    client = _client(args.workspace, getattr(args, "profile", None))
    _print(
        {
            "assets": search_assets(
                client.config.paths,
                kind=getattr(args, "kind", None),
                query=getattr(args, "query", None),
                strategy_id=getattr(args, "strategy", None),
                limit=int(getattr(args, "limit", 100) or 100),
            ),
            "candidates": list_candidates(client.config.paths),
        }
    )
    return 0


def cmd_assets_promote(args) -> int:
    from ...evolution.assets import promote_candidate

    client = _client(args.workspace, getattr(args, "profile", None))
    out = promote_candidate(
        client.config.paths,
        args.candidate_id,
        operator=getattr(args, "operator", None),
    )
    _print(out)
    return 0 if out.get("ok") else 1


def cmd_assets_reject(args) -> int:
    from ...evolution.assets import reject_candidate

    client = _client(args.workspace, getattr(args, "profile", None))
    out = reject_candidate(
        client.config.paths,
        args.candidate_id,
        reason=getattr(args, "reason", ""),
        operator=getattr(args, "operator", None),
    )
    _print(out)
    return 0 if out.get("ok") else 1


def cmd_validate(args) -> int:
    from ...evolution.validation_plan import run_validation_plan

    client = _client(args.workspace, getattr(args, "profile", None))
    if not getattr(args, "plan_id", None) and not getattr(args, "proposal_id", None):
        _print({"ok": False, "error": "plan_id or proposal_id required"})
        return 1
    out = run_validation_plan(
        client.config.paths,
        plan_id=getattr(args, "plan_id", None),
        proposal_id=getattr(args, "proposal_id", None),
        dry_run=getattr(args, "dry_run", True),
    )
    _print(out)
    return 0 if out.get("ok") else 1


def cmd_export(args) -> int:
    from ...evolution.assets import list_capsules, list_genes
    from ...evolution.event_store import list_events

    client = _client(args.workspace, getattr(args, "profile", None))
    payload = {
        "format": getattr(args, "format", "nerya"),
        "dry_run": bool(getattr(args, "dry_run", False)),
        "genes": list_genes(client.config.paths),
        "capsules": list_capsules(client.config.paths, limit=500),
        "events": list_events(client.config.paths, limit=500),
    }
    _print(payload)
    return 0


# ----------------------------------------------------------------- proposals
def cmd_proposals_list(args) -> int:
    client = _client(args.workspace, getattr(args, "profile", None))
    props = patch_proposal.list_proposals(client.config.paths)
    _print([p.asdict() for p in props])
    return 0


def cmd_proposals_show(args) -> int:
    client = _client(args.workspace, getattr(args, "profile", None))
    d = client.config.paths.proposals / args.proposal_id
    if not d.exists():
        _print({"error": f"no proposal {args.proposal_id}"})
        return 1
    out = {"id": args.proposal_id, "files": [p.name for p in d.iterdir()]}
    manifest = d / "proposal.yml"
    if manifest.exists():
        out["proposal"] = yaml_io.load(manifest)
    rationale = d / "rationale.md"
    if rationale.exists():
        out["rationale"] = rationale.read_text(encoding="utf-8")
    _print(out)
    return 0


def cmd_proposals_approve(args) -> int:
    client = _client(args.workspace, getattr(args, "profile", None))
    patch_proposal.set_state(client.config.paths, args.proposal_id, "approved")
    _print({"proposal_id": args.proposal_id, "state": "approved"})
    return 0


def cmd_proposals_apply(args) -> int:
    client = _client(args.workspace, getattr(args, "profile", None))
    result = promotion.apply_proposal(client.config.paths, args.proposal_id)
    _print(result if isinstance(result, dict) else result.asdict())
    return 0 if (isinstance(result, dict) and result.get("ok")) else 1


def cmd_proposals_rollback(args) -> int:
    client = _client(args.workspace, getattr(args, "profile", None))
    result = rollback_mod.rollback_proposal(client.config.paths, args.proposal_id)
    _print(result if isinstance(result, dict) else result.asdict())
    return 0 if (isinstance(result, dict) and result.get("ok")) else 1


# ----------------------------------------------------------------- scripts
def cmd_scripts_list(args) -> int:
    client = _client(args.workspace, getattr(args, "profile", None))
    paths = client.config.paths
    _print({
        "pending": [p.name for p in paths.scripts_pending.iterdir()
                    if p.is_dir()] if paths.scripts_pending.exists() else [],
        "approved": [p.name for p in paths.scripts_approved.iterdir()
                     if p.is_dir()] if paths.scripts_approved.exists() else [],
    })
    return 0


def cmd_scripts_approve(args) -> int:
    from ...scripts.proposal import promote
    client = _client(args.workspace, getattr(args, "profile", None))
    out = promote(client.config.paths, args.script_id)
    _print({"script_id": args.script_id, "approved_path": str(out)})
    return 0


def register(sub) -> None:
    p = sub.add_parser("reflect"); _add_ws(p); p.set_defaults(func=cmd_reflect)
    p = sub.add_parser("evolve"); _add_ws(p); p.set_defaults(func=cmd_evolve)
    p = sub.add_parser("signals"); _add_ws(p)
    p.add_argument("--strategy")
    p.add_argument("--source")
    p.add_argument("--severity")
    p.add_argument("--kind")
    p.add_argument("--limit", type=int, default=100)
    p.add_argument("--refresh", action="store_true")
    p.set_defaults(func=cmd_signals)

    p = sub.add_parser("events"); _add_ws(p)
    p.add_argument("--strategy")
    p.add_argument("--proposal")
    p.add_argument("--outcome")
    p.add_argument("--limit", type=int, default=100)
    p.set_defaults(func=cmd_events)

    p = sub.add_parser("validate"); _add_ws(p)
    p.add_argument("proposal_id", nargs="?")
    p.add_argument("--plan-id")
    p.add_argument("--run", dest="dry_run", action="store_false")
    p.set_defaults(func=cmd_validate, dry_run=True)

    p = sub.add_parser("export"); _add_ws(p)
    p.add_argument("--format", choices=["nerya", "gep"], default="nerya")
    p.add_argument("--dry-run", action="store_true")
    p.set_defaults(func=cmd_export)

    # proposals
    prop = sub.add_parser("proposals").add_subparsers(dest="pcmd", required=True)
    p = prop.add_parser("list"); _add_ws(p); p.set_defaults(func=cmd_proposals_list)
    p = prop.add_parser("show"); _add_ws(p)
    p.add_argument("proposal_id"); p.set_defaults(func=cmd_proposals_show)
    p = prop.add_parser("approve"); _add_ws(p)
    p.add_argument("proposal_id"); p.set_defaults(func=cmd_proposals_approve)
    p = prop.add_parser("apply"); _add_ws(p)
    p.add_argument("proposal_id"); p.set_defaults(func=cmd_proposals_apply)
    p = prop.add_parser("rollback"); _add_ws(p)
    p.add_argument("proposal_id"); p.set_defaults(func=cmd_proposals_rollback)

    # assets
    assets = sub.add_parser("assets").add_subparsers(dest="acmd", required=True)
    p = assets.add_parser("search"); _add_ws(p)
    p.add_argument("query", nargs="?")
    p.add_argument("--kind", choices=["gene", "capsule"])
    p.add_argument("--strategy")
    p.add_argument("--limit", type=int, default=100)
    p.set_defaults(func=cmd_assets_search)
    p = assets.add_parser("promote"); _add_ws(p)
    p.add_argument("candidate_id")
    p.add_argument("--operator")
    p.set_defaults(func=cmd_assets_promote)
    p = assets.add_parser("reject"); _add_ws(p)
    p.add_argument("candidate_id")
    p.add_argument("--reason", default="")
    p.add_argument("--operator")
    p.set_defaults(func=cmd_assets_reject)

    # scripts
    scripts = sub.add_parser("scripts").add_subparsers(dest="scmd", required=True)
    p = scripts.add_parser("list"); _add_ws(p); p.set_defaults(func=cmd_scripts_list)
    p = scripts.add_parser("approve"); _add_ws(p)
    p.add_argument("script_id"); p.set_defaults(func=cmd_scripts_approve)

    # Compatibility namespace for docs and operator muscle memory:
    # `nerya evolution signals ...` mirrors the historical top-level
    # `nerya signals ...` commands registered above.
    ns = sub.add_parser("evolution")
    nsub = ns.add_subparsers(dest="evolution_cmd", required=True)
    p = nsub.add_parser("reflect"); _add_ws(p); p.set_defaults(func=cmd_reflect)
    p = nsub.add_parser("evolve"); _add_ws(p); p.set_defaults(func=cmd_evolve)
    p = nsub.add_parser("signals"); _add_ws(p)
    p.add_argument("--strategy")
    p.add_argument("--source")
    p.add_argument("--severity")
    p.add_argument("--kind")
    p.add_argument("--limit", type=int, default=100)
    p.add_argument("--refresh", action="store_true")
    p.set_defaults(func=cmd_signals)
    p = nsub.add_parser("events"); _add_ws(p)
    p.add_argument("--strategy")
    p.add_argument("--proposal")
    p.add_argument("--outcome")
    p.add_argument("--limit", type=int, default=100)
    p.set_defaults(func=cmd_events)
    p = nsub.add_parser("validate"); _add_ws(p)
    p.add_argument("proposal_id", nargs="?")
    p.add_argument("--plan-id")
    p.add_argument("--run", dest="dry_run", action="store_false")
    p.set_defaults(func=cmd_validate, dry_run=True)
    p = nsub.add_parser("export"); _add_ws(p)
    p.add_argument("--format", choices=["nerya", "gep"], default="nerya")
    p.add_argument("--dry-run", action="store_true")
    p.set_defaults(func=cmd_export)
    assets_ns = nsub.add_parser("assets").add_subparsers(dest="acmd", required=True)
    p = assets_ns.add_parser("search"); _add_ws(p)
    p.add_argument("query", nargs="?")
    p.add_argument("--kind", choices=["gene", "capsule"])
    p.add_argument("--strategy")
    p.add_argument("--limit", type=int, default=100)
    p.set_defaults(func=cmd_assets_search)
    p = assets_ns.add_parser("promote"); _add_ws(p)
    p.add_argument("candidate_id")
    p.add_argument("--operator")
    p.set_defaults(func=cmd_assets_promote)
    p = assets_ns.add_parser("reject"); _add_ws(p)
    p.add_argument("candidate_id")
    p.add_argument("--reason", default="")
    p.add_argument("--operator")
    p.set_defaults(func=cmd_assets_reject)


__all__ = [
    "cmd_reflect", "cmd_evolve",
    "cmd_signals", "cmd_events", "cmd_validate", "cmd_export",
    "cmd_proposals_list", "cmd_proposals_show", "cmd_proposals_approve",
    "cmd_proposals_apply", "cmd_proposals_rollback",
    "cmd_assets_search", "cmd_assets_promote", "cmd_assets_reject",
    "cmd_scripts_list", "cmd_scripts_approve",
    "register",
]
