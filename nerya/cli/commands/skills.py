"""Skill / trigger / trading / strategy / review / portfolio / messages
subcommands.

All of these wrap the in-process ``InternalClient`` facades, so there's
no shared setup or state beyond the one-shot client boot.
"""

from __future__ import annotations

import json
from pathlib import Path

from .._common import _add_ws, _client, _ev_args, _print
from ...core import yaml_io


# ----------------------------------------------------------------- skill
def cmd_skill_list(args) -> int:
    client = _client(args.workspace, getattr(args, "profile", None))
    _print(client.skills.list())
    return 0


def cmd_skill_enable(args) -> int:
    client = _client(args.workspace, getattr(args, "profile", None))
    path = client.config.paths.skills_enabled
    doc = yaml_io.load(path, default={"enabled": []}) or {}
    enabled = list(doc.get("enabled") or [])
    if args.skill_id not in enabled:
        enabled.append(args.skill_id)
    doc["enabled"] = enabled
    yaml_io.dump(path, doc)
    _print({"enabled": enabled})
    return 0


def cmd_skill_install(args) -> int:
    """Install an external skill from a local dir, git URL, or tarball.

    The skill is staged under ``workspace/skills/pending/`` and a
    ``skill_install_request`` proposal is emitted. Nothing is armed
    into the live registry until ``nerya skill promote <skill_id>``
    runs after operator approval.
    """
    from ...skills.installer import install_skill
    client = _client(args.workspace, getattr(args, "profile", None))
    report = install_skill(
        client.config.paths,
        source=args.source,
        kind=getattr(args, "kind", "auto") or "auto",
        subdir=getattr(args, "subdir", None),
        git_ref=getattr(args, "ref", None),
    )
    _print(report.asdict())
    return 0


def cmd_skill_promote(args) -> int:
    from ...skills.installer import promote_installed
    client = _client(args.workspace, getattr(args, "profile", None))
    dst = promote_installed(client.config.paths, args.skill_id)
    _print({"ok": True, "skill_id": args.skill_id, "installed_at": str(dst)})
    return 0


def cmd_skill_installed(args) -> int:
    from ...skills.installer import list_installed
    client = _client(args.workspace, getattr(args, "profile", None))
    _print({"installed": list_installed(client.config.paths)})
    return 0


def cmd_skill_disable(args) -> int:
    """drop a skill id from ``skills/enabled.yml``."""

    client = _client(args.workspace, getattr(args, "profile", None))
    path = client.config.paths.skills_enabled
    doc = yaml_io.load(path, default={"enabled": []}) or {}
    enabled = list(doc.get("enabled") or [])
    if args.skill_id in enabled:
        enabled = [s for s in enabled if s != args.skill_id]
    doc["enabled"] = enabled
    yaml_io.dump(path, doc)
    _print({"enabled": enabled, "removed": args.skill_id})
    return 0


def cmd_skill_view(args) -> int:
    """runtime ``skill view <id>`` showing actions/permissions."""

    client = _client(args.workspace, getattr(args, "profile", None))
    info = client.skills.view(args.skill_id) if hasattr(client, "skills") else None
    # ``client.skills`` here is the SDK facade — re-bind via SkillKernel:
    if info is None:
        from ...skills.kernel import SkillKernel
        info = SkillKernel.boot(client.config).view(args.skill_id)
    if info is None:
        _print({"ok": False, "error": f"unknown skill id: {args.skill_id}"})
        return 1
    _print(info)
    return 0


def cmd_skill_doctor(args) -> int:
    """surface manifest/handler/enabled mismatches."""

    client = _client(args.workspace, getattr(args, "profile", None))
    from ...skills.kernel import SkillKernel
    report = SkillKernel.boot(client.config).doctor()
    _print(report)
    return 0 if not report.get("problems") else 1


def cmd_skill_sync(args) -> int:
    """re-read skill manifests after an install/promote."""

    client = _client(args.workspace, getattr(args, "profile", None))
    from ...skills.kernel import SkillKernel
    count = SkillKernel.boot(client.config).reload()
    _print({"ok": True, "registered": count})
    return 0


# ----------------------------------------------------------------- trigger
def cmd_trigger_emit(args) -> int:
    client = _client(args.workspace, getattr(args, "profile", None))
    ev = json.loads(Path(args.file).read_text(encoding="utf-8"))
    _print(client.triggers.emit(**_ev_args(ev)))
    return 0


def cmd_trigger_dry_run(args) -> int:
    client = _client(args.workspace, getattr(args, "profile", None))
    ev = json.loads(Path(args.file).read_text(encoding="utf-8"))
    ev["dry_run"] = True
    _print(client.triggers.emit(**_ev_args(ev)))
    return 0


# ----------------------------------------------------------------- trading
def cmd_trading_submit(args) -> int:
    client = _client(args.workspace, getattr(args, "profile", None))
    payload = json.loads(Path(args.file).read_text(encoding="utf-8"))
    _print(client.trading.submit_intent(**payload))
    return 0


# ----------------------------------------------------------------- review
def cmd_review_strategy(args) -> int:
    from ...strategy_history import session_writer
    client = _client(args.workspace, getattr(args, "profile", None))
    sid = session_writer.latest_session_id(client.config.paths, args.strategy_id)
    if not sid:
        _print({"error": "no sessions yet for this strategy"})
        return 1
    _print(client.strategy.review(args.strategy_id, sid))
    return 0


# ----------------------------------------------------------------- portfolio
def cmd_portfolio(args) -> int:
    client = _client(args.workspace, getattr(args, "profile", None))
    _print(client.skill.call("portfolio", "get_portfolio_summary", payload={}))
    return 0


# ----------------------------------------------------------------- messages
def cmd_messages_list(args) -> int:
    client = _client(args.workspace, getattr(args, "profile", None))
    _print(client.messages.list(limit=args.limit))
    return 0


def register(sub) -> None:
    # skill
    skill = sub.add_parser("skill").add_subparsers(dest="scmd", required=True)
    p = skill.add_parser("list"); _add_ws(p); p.set_defaults(func=cmd_skill_list)
    p = skill.add_parser("enable"); _add_ws(p)
    p.add_argument("skill_id"); p.set_defaults(func=cmd_skill_enable)
    p = skill.add_parser("install"); _add_ws(p)
    p.add_argument("source", help="local dir, tarball path, or git URL")
    p.add_argument("--kind", default="auto", choices=["auto", "dir", "tar", "git"])
    p.add_argument("--subdir", default=None,
                   help="subdirectory inside tar/git containing SKILL.md")
    p.add_argument("--ref", default=None, help="git branch/tag/sha")
    p.set_defaults(func=cmd_skill_install)
    p = skill.add_parser("promote"); _add_ws(p)
    p.add_argument("skill_id"); p.set_defaults(func=cmd_skill_promote)
    p = skill.add_parser("installed"); _add_ws(p)
    p.set_defaults(func=cmd_skill_installed)
    p = skill.add_parser("disable"); _add_ws(p)
    p.add_argument("skill_id"); p.set_defaults(func=cmd_skill_disable)
    p = skill.add_parser("view"); _add_ws(p)
    p.add_argument("skill_id"); p.set_defaults(func=cmd_skill_view)
    p = skill.add_parser("doctor"); _add_ws(p)
    p.set_defaults(func=cmd_skill_doctor)
    p = skill.add_parser("sync"); _add_ws(p)
    p.set_defaults(func=cmd_skill_sync)

    # trigger
    trig = sub.add_parser("trigger").add_subparsers(dest="tcmd", required=True)
    p = trig.add_parser("emit"); _add_ws(p)
    p.add_argument("--file", required=True); p.set_defaults(func=cmd_trigger_emit)
    p = trig.add_parser("dry-run"); _add_ws(p)
    p.add_argument("--file", required=True); p.set_defaults(func=cmd_trigger_dry_run)

    # trading
    trad = sub.add_parser("trading").add_subparsers(dest="rcmd", required=True)
    p = trad.add_parser("submit"); _add_ws(p)
    p.add_argument("--file", required=True); p.set_defaults(func=cmd_trading_submit)

    # review
    rev = sub.add_parser("review").add_subparsers(dest="rvcmd", required=True)
    p = rev.add_parser("strategy"); _add_ws(p)
    p.add_argument("strategy_id"); p.set_defaults(func=cmd_review_strategy)

    # portfolio
    p = sub.add_parser("portfolio"); _add_ws(p); p.set_defaults(func=cmd_portfolio)

    # messages
    msg = sub.add_parser("messages").add_subparsers(dest="mcmd", required=True)
    lp = msg.add_parser("list"); _add_ws(lp)
    lp.add_argument("--limit", type=int, default=50)
    lp.set_defaults(func=cmd_messages_list)


__all__ = [
    "cmd_skill_list", "cmd_skill_enable", "cmd_skill_disable",
    "cmd_skill_install", "cmd_skill_promote", "cmd_skill_installed",
    "cmd_skill_view", "cmd_skill_doctor", "cmd_skill_sync",
    "cmd_trigger_emit", "cmd_trigger_dry_run",
    "cmd_trading_submit",
    "cmd_review_strategy",
    "cmd_portfolio",
    "cmd_messages_list",
    "register",
]
