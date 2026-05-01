"""``nerya strategy ...`` subcommands.

These commands wrap :class:`nerya.sdk.strategy_api.StrategyAPI` so the
operator can drive every package-lifecycle step from the terminal:

```bash
nerya strategy list
nerya strategy show btc_scalper
nerya strategy generate --strategy-id btc_scalper --markets PAPER:BTCUSDT --accounts paper_main
nerya strategy validate btc_scalper
nerya strategy promote prp_<id>
nerya strategy run btc_scalper [--dry-run] [--trigger-event-id evt_xyz]
nerya strategy schedule btc_scalper [--pause | --resume]
nerya strategy schedule status btc_scalper
nerya strategy kill-switch btc_scalper [--assert "reason" | --clear]
nerya strategy runs btc_scalper [--limit 50]
nerya strategy status btc_scalper
nerya strategy workspace btc_scalper
```

``--dry-run`` on ``run`` flips the runner into ``shadow`` mode so any
trade intent is recorded as a shadow-only run instead of going to the
trading kernel. Use it to sanity-check freshly-promoted packages.
"""

from __future__ import annotations

import json
from pathlib import Path

from .._common import _add_ws, _client, _print
from ...evolution.strategy_code_generator import StrategyGenerationRequest


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------


def cmd_list(args) -> int:
    client = _client(args.workspace, getattr(args, "profile", None))
    _print(client.strategy.list_packages())
    return 0


def cmd_show(args) -> int:
    client = _client(args.workspace, getattr(args, "profile", None))
    _print(client.strategy.get_package(args.strategy_id))
    return 0


def cmd_generate(args) -> int:
    client = _client(args.workspace, getattr(args, "profile", None))
    prompt_text = ""
    if getattr(args, "prompt_file", None):
        prompt_text = Path(args.prompt_file).read_text(encoding="utf-8")
    request = StrategyGenerationRequest(
        strategy_id=args.strategy_id,
        title=getattr(args, "title", "") or "",
        description=getattr(args, "description", "") or "",
        prompt=prompt_text,
        strategy_class=getattr(args, "strategy_class", "scalping"),
        mode=getattr(args, "mode", "paper"),
        markets=tuple(getattr(args, "markets", ()) or ()),
        accounts=tuple(getattr(args, "accounts", ()) or ()),
        schedule_cron=(getattr(args, "cron", "") or ""),
        schedule_every_seconds=(
            int(args.every_seconds) if getattr(args, "every_seconds", None) else None
        ),
        news_sources=tuple(getattr(args, "news_sources", ()) or ()),
        subagents=tuple(getattr(args, "subagents", ()) or ()),
        create_tuning=bool(getattr(args, "create_tuning", False)),
        tuning_prompt=(getattr(args, "tuning_prompt", "") or ""),
        tuning_cron=(getattr(args, "tuning_cron", "0 */6 * * *") or "0 */6 * * *"),
        tuning_objectives=tuple(getattr(args, "tuning_objectives", ()) or ()),
    )
    out = client.strategy.generate_proposal(
        request, validate=not bool(getattr(args, "skip_validate", False))
    )
    _print(out)
    return 0


def cmd_validate(args) -> int:
    client = _client(args.workspace, getattr(args, "profile", None))
    sid = getattr(args, "strategy_id", None)
    pid = getattr(args, "proposal_id", None)
    out = client.strategy.validate(sid, proposal_id=pid)
    _print(out)
    return 0 if out.get("ok") else 1


def cmd_promote(args) -> int:
    client = _client(args.workspace, getattr(args, "profile", None))
    out = client.strategy.promote(
        args.proposal_id, note=getattr(args, "note", "") or ""
    )
    _print(out)
    return 0 if out.get("ok") else 1


def cmd_run(args) -> int:
    client = _client(args.workspace, getattr(args, "profile", None))
    mode_override = "shadow" if bool(getattr(args, "dry_run", False)) else getattr(
        args, "mode_override", None
    )
    payload_text = getattr(args, "payload", None)
    payload: dict | None = None
    if payload_text:
        payload = json.loads(payload_text)
    record = client.strategy.run_tick(
        args.strategy_id,
        trigger_payload=payload,
        trigger_event_id=getattr(args, "trigger_event_id", None),
        operator=getattr(args, "operator", None),
        note=getattr(args, "note", "") or "",
        mode_override=mode_override,
    )
    _print(record)
    return 0 if record.get("status") != "error" else 1


def cmd_schedule(args) -> int:
    client = _client(args.workspace, getattr(args, "profile", None))
    if getattr(args, "pause", False):
        _print(client.strategy.pause(args.strategy_id))
        return 0
    if getattr(args, "resume", False):
        _print(client.strategy.resume(args.strategy_id))
        return 0
    if getattr(args, "remove", False):
        _print(client.strategy.remove_schedules(args.strategy_id))
        return 0
    _print(client.strategy.schedule(args.strategy_id))
    return 0


def cmd_schedule_status(args) -> int:
    client = _client(args.workspace, getattr(args, "profile", None))
    _print(client.strategy.schedule_status(args.strategy_id))
    return 0


def cmd_kill_switch(args) -> int:
    client = _client(args.workspace, getattr(args, "profile", None))
    if getattr(args, "assert_reason", None):
        out = client.strategy.kill_switch(
            args.strategy_id,
            action="assert",
            reason=args.assert_reason,
            by=getattr(args, "by", "operator") or "operator",
        )
    elif getattr(args, "clear", False):
        out = client.strategy.kill_switch(
            args.strategy_id,
            action="clear",
            by=getattr(args, "by", "operator") or "operator",
        )
    else:
        out = client.strategy.kill_switch(args.strategy_id, action="get")
    _print(out)
    return 0


def cmd_runs(args) -> int:
    client = _client(args.workspace, getattr(args, "profile", None))
    _print(
        client.strategy.runs(
            args.strategy_id, limit=int(getattr(args, "limit", 50) or 50)
        )
    )
    return 0


def cmd_status(args) -> int:
    client = _client(args.workspace, getattr(args, "profile", None))
    out = client.strategy.status(args.strategy_id)
    _print(out)
    return 0 if out.get("ok") else 1


def cmd_history(args) -> int:
    client = _client(args.workspace, getattr(args, "profile", None))
    _print(
        client.strategy.history(
            args.strategy_id, limit=int(getattr(args, "limit", 20) or 20)
        )
    )
    return 0


def cmd_explain_trade(args) -> int:
    client = _client(args.workspace, getattr(args, "profile", None))
    _print(client.strategy.explain_trade(args.strategy_id, args.order_id))
    return 0


# ---------------------------------------------------------------------------
# Self-evolution / tuning subcommands
# ---------------------------------------------------------------------------


def cmd_tuning_generate(args) -> int:
    client = _client(args.workspace, getattr(args, "profile", None))
    prompt_text = ""
    if getattr(args, "prompt_file", None):
        prompt_text = Path(args.prompt_file).read_text(encoding="utf-8")
    out = client.strategy.tuning.generate(
        args.strategy_id,
        prompt=prompt_text or getattr(args, "prompt", "") or "",
        cron=getattr(args, "cron", "0 */6 * * *") or "0 */6 * * *",
        every_seconds=(
            int(args.every_seconds) if getattr(args, "every_seconds", None) else None
        ),
        objectives=list(getattr(args, "objectives", ()) or ()),
        require_backtest=bool(getattr(args, "require_backtest", True)),
        require_shadow_run=bool(getattr(args, "require_shadow_run", False)),
    )
    _print(out)
    return 0


def cmd_tuning_schedule(args) -> int:
    client = _client(args.workspace, getattr(args, "profile", None))
    if getattr(args, "pause", False):
        _print(client.strategy.tuning.pause(args.strategy_id))
        return 0
    if getattr(args, "resume", False):
        _print(client.strategy.tuning.resume(args.strategy_id))
        return 0
    _print(client.strategy.tuning.schedule(args.strategy_id))
    return 0


def cmd_tuning_run(args) -> int:
    client = _client(args.workspace, getattr(args, "profile", None))
    out = client.strategy.tuning.run(
        args.strategy_id,
        dry_run=bool(getattr(args, "dry_run", False)),
        operator=getattr(args, "operator", None),
        note=getattr(args, "note", "") or "",
        trigger_event_id=getattr(args, "trigger_event_id", None),
    )
    _print(out)
    return 0 if out.get("status") != "error" else 1


def cmd_tuning_status(args) -> int:
    client = _client(args.workspace, getattr(args, "profile", None))
    out = client.strategy.tuning.status(
        args.strategy_id,
        lookback_runs=int(getattr(args, "lookback_runs", 200) or 200),
    )
    _print(out)
    return 0 if out.get("ok") else 1


def cmd_tuning_snapshot(args) -> int:
    client = _client(args.workspace, getattr(args, "profile", None))
    out = client.strategy.tuning.snapshot(
        args.strategy_id,
        lookback_runs=int(getattr(args, "lookback_runs", 200) or 200),
    )
    _print(out)
    return 0


def cmd_workspace(args) -> int:
    client = _client(args.workspace, getattr(args, "profile", None))
    base = client.strategy.status(args.strategy_id)
    if not base.get("ok"):
        _print(base)
        return 1
    base["runs"] = client.strategy.runs(
        args.strategy_id, limit=int(getattr(args, "runs_limit", 50) or 50)
    )
    base["history"] = client.strategy.history(
        args.strategy_id, limit=int(getattr(args, "history_limit", 50) or 50)
    )
    _print(base)
    return 0


# ---------------------------------------------------------------------------
# Parser registration
# ---------------------------------------------------------------------------


def register(sub) -> None:
    p = sub.add_parser("strategy", help="Strategy runtime control plane")
    sub2 = p.add_subparsers(dest="strategy_cmd", required=True)

    sp = sub2.add_parser("list", help="List promoted strategy packages")
    _add_ws(sp)
    sp.set_defaults(func=cmd_list)

    sp = sub2.add_parser("show", help="Show one strategy package's manifest")
    _add_ws(sp)
    sp.add_argument("strategy_id")
    sp.set_defaults(func=cmd_show)

    sp = sub2.add_parser("generate", help="Generate a strategy package proposal")
    _add_ws(sp)
    sp.add_argument("--strategy-id", dest="strategy_id", required=True)
    sp.add_argument("--title", default="")
    sp.add_argument("--description", default="")
    sp.add_argument("--prompt-file", dest="prompt_file", default=None)
    sp.add_argument(
        "--strategy-class",
        dest="strategy_class",
        choices=["scalping", "trend", "news"],
        default="scalping",
    )
    sp.add_argument(
        "--mode", choices=["paper", "shadow", "live"], default="paper"
    )
    sp.add_argument("--markets", nargs="+", default=())
    sp.add_argument("--accounts", nargs="+", default=())
    sp.add_argument("--cron", default="")
    sp.add_argument(
        "--every-seconds", dest="every_seconds", type=int, default=None
    )
    sp.add_argument("--news-sources", dest="news_sources", nargs="*", default=())
    sp.add_argument("--subagents", nargs="*", default=())
    sp.add_argument(
        "--create-tuning", dest="create_tuning", action="store_true", default=False
    )
    sp.add_argument("--tuning-prompt", dest="tuning_prompt", default="")
    sp.add_argument("--tuning-cron", dest="tuning_cron", default="0 */6 * * *")
    sp.add_argument(
        "--tuning-objectives", dest="tuning_objectives", nargs="*", default=()
    )
    sp.add_argument(
        "--skip-validate", dest="skip_validate", action="store_true", default=False
    )
    sp.set_defaults(func=cmd_generate)

    sp = sub2.add_parser(
        "validate",
        help="Validate a promoted package or in-flight proposal",
    )
    _add_ws(sp)
    sp.add_argument("strategy_id", nargs="?", default=None)
    sp.add_argument("--proposal-id", dest="proposal_id", default=None)
    sp.set_defaults(func=cmd_validate)

    sp = sub2.add_parser("promote", help="Approve + apply a strategy proposal")
    _add_ws(sp)
    sp.add_argument("proposal_id")
    sp.add_argument("--note", default="")
    sp.set_defaults(func=cmd_promote)

    sp = sub2.add_parser("run", help="Run one strategy tick")
    _add_ws(sp)
    sp.add_argument("strategy_id")
    sp.add_argument("--dry-run", action="store_true", default=False)
    sp.add_argument(
        "--mode-override",
        dest="mode_override",
        choices=["paper", "shadow", "live"],
        default=None,
    )
    sp.add_argument("--trigger-event-id", dest="trigger_event_id", default=None)
    sp.add_argument(
        "--payload", default=None, help="Trigger payload as JSON string."
    )
    sp.add_argument("--operator", default=None)
    sp.add_argument("--note", default="")
    sp.set_defaults(func=cmd_run)

    sp = sub2.add_parser(
        "schedule", help="Install / pause / resume / remove schedules"
    )
    _add_ws(sp)
    sp.add_argument("strategy_id")
    grp = sp.add_mutually_exclusive_group()
    grp.add_argument("--pause", action="store_true", default=False)
    grp.add_argument("--resume", action="store_true", default=False)
    grp.add_argument("--remove", action="store_true", default=False)
    sp.set_defaults(func=cmd_schedule)

    sp = sub2.add_parser("schedule-status", help="Show schedule rows")
    _add_ws(sp)
    sp.add_argument("strategy_id")
    sp.set_defaults(func=cmd_schedule_status)

    sp = sub2.add_parser(
        "kill-switch", help="Inspect / set / clear the kill switch"
    )
    _add_ws(sp)
    sp.add_argument("strategy_id")
    grp = sp.add_mutually_exclusive_group()
    grp.add_argument(
        "--assert", dest="assert_reason", default=None, help="Reason for halt"
    )
    grp.add_argument("--clear", action="store_true", default=False)
    sp.add_argument("--by", default="operator")
    sp.set_defaults(func=cmd_kill_switch)

    sp = sub2.add_parser("runs", help="List recent strategy runs")
    _add_ws(sp)
    sp.add_argument("strategy_id")
    sp.add_argument("--limit", type=int, default=50)
    sp.set_defaults(func=cmd_runs)

    sp = sub2.add_parser("status", help="Aggregate manifest+schedule+last run")
    _add_ws(sp)
    sp.add_argument("strategy_id")
    sp.set_defaults(func=cmd_status)

    sp = sub2.add_parser(
        "history", help="Show recent trade history (legacy ledger)"
    )
    _add_ws(sp)
    sp.add_argument("strategy_id")
    sp.add_argument("--limit", type=int, default=20)
    sp.set_defaults(func=cmd_history)

    sp = sub2.add_parser(
        "explain-trade", help="Explain a single submitted order from the ledger"
    )
    _add_ws(sp)
    sp.add_argument("strategy_id")
    sp.add_argument("order_id")
    sp.set_defaults(func=cmd_explain_trade)

    # ----------------------------------- tuning subgroup
    tun = sub2.add_parser(
        "tuning",
        help="Self-evolution loop: generate / schedule / run / status",
    )
    tsub = tun.add_subparsers(dest="tuning_cmd", required=True)

    tp = tsub.add_parser("generate", help="Add a tuning block to a package")
    _add_ws(tp)
    tp.add_argument("strategy_id")
    tp.add_argument("--prompt", default="")
    tp.add_argument("--prompt-file", dest="prompt_file", default=None)
    tp.add_argument("--cron", default="0 */6 * * *")
    tp.add_argument(
        "--every-seconds", dest="every_seconds", type=int, default=None
    )
    tp.add_argument(
        "--objectives", nargs="*", default=("risk_adjusted_return",)
    )
    tp.add_argument(
        "--no-backtest",
        dest="require_backtest",
        action="store_false",
        default=True,
    )
    tp.add_argument(
        "--require-shadow-run",
        dest="require_shadow_run",
        action="store_true",
        default=False,
    )
    tp.set_defaults(func=cmd_tuning_generate)

    tp = tsub.add_parser(
        "schedule", help="Install / pause / resume the tuning schedule"
    )
    _add_ws(tp)
    tp.add_argument("strategy_id")
    grp = tp.add_mutually_exclusive_group()
    grp.add_argument("--pause", action="store_true", default=False)
    grp.add_argument("--resume", action="store_true", default=False)
    tp.set_defaults(func=cmd_tuning_schedule)

    tp = tsub.add_parser("run", help="Run one tuning cycle")
    _add_ws(tp)
    tp.add_argument("strategy_id")
    tp.add_argument("--dry-run", action="store_true", default=False)
    tp.add_argument("--operator", default=None)
    tp.add_argument("--note", default="")
    tp.add_argument("--trigger-event-id", dest="trigger_event_id", default=None)
    tp.set_defaults(func=cmd_tuning_run)

    tp = tsub.add_parser(
        "status",
        help="Aggregate tuning status (config + schedule + snapshot + proposals)",
    )
    _add_ws(tp)
    tp.add_argument("strategy_id")
    tp.add_argument("--lookback-runs", dest="lookback_runs", type=int, default=200)
    tp.set_defaults(func=cmd_tuning_status)

    tp = tsub.add_parser(
        "snapshot",
        help="Print the read-only performance snapshot the tuner consumes",
    )
    _add_ws(tp)
    tp.add_argument("strategy_id")
    tp.add_argument("--lookback-runs", dest="lookback_runs", type=int, default=200)
    tp.set_defaults(func=cmd_tuning_snapshot)

    sp = sub2.add_parser(
        "workspace",
        help="Aggregate workspace endpoint (manifest+schedules+runs+history)",
    )
    _add_ws(sp)
    sp.add_argument("strategy_id")
    sp.add_argument("--runs-limit", dest="runs_limit", type=int, default=50)
    sp.add_argument("--history-limit", dest="history_limit", type=int, default=50)
    sp.set_defaults(func=cmd_workspace)


__all__ = [
    "cmd_explain_trade",
    "cmd_generate",
    "cmd_history",
    "cmd_kill_switch",
    "cmd_list",
    "cmd_promote",
    "cmd_run",
    "cmd_runs",
    "cmd_schedule",
    "cmd_schedule_status",
    "cmd_show",
    "cmd_status",
    "cmd_tuning_generate",
    "cmd_tuning_run",
    "cmd_tuning_schedule",
    "cmd_tuning_snapshot",
    "cmd_tuning_status",
    "cmd_validate",
    "cmd_workspace",
    "register",
]
