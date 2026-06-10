#!/usr/bin/env python3
"""Reset a Nerya workspace for E2E test runs.

Safe by default: deletes only ephemeral test artifacts; keeps credentials,
the encrypted vault, and any account/skill data the operator created
manually. Use ``--full`` to wipe the entire workspace directory tree
(useful for a fresh test workspace under /tmp).

Examples
--------
    # Wipe ephemeral state in the test workspace (default safe mode)
    python tools/reset_workspace.py --workspace ~/.nerya/test-workspace

    # Dry-run to see what would be deleted
    python tools/reset_workspace.py --workspace ~/.nerya/test-workspace --dry-run

    # Nuke everything (intended only for isolated test workspaces!)
    python tools/reset_workspace.py --workspace ~/.nerya/test-workspace --full

Exit codes
----------
    0  success
    1  workspace path looks unsafe (refuses to delete prod-like paths)
    2  unrecognised arguments

The default reset path is dependency-free (stdlib only) so it can run
before Nerya itself is imported. The optional prompt-bundle sync mode
imports Nerya lazily and is intended for isolated E2E workspaces.
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import time
from pathlib import Path

# Directories considered "ephemeral test output" — deleted in safe mode.
# Order matters: deeper paths first so a parent can be left intact when
# only some children are wiped.
EPHEMERAL_DIRS: tuple[str, ...] = (
    "evolution/proposals",
    "evolution/assets/candidates.jsonl",  # treated as file via _wipe_path
    "evolution/events.jsonl",
    "evolution/signals.jsonl",
    "journals",
    "inbox",
    "outbox",
    "approvals/pending.jsonl",
    "teams",
    "strategies",          # promoted-strategy state; reset between test runs
    "agent_tasks",
    "triggers/schedules.yml",
    "triggers/dead_letter",
    "subagents/runs",      # keep custom roles under subagents/<name>.agent.md
    "scripts/pending",
    "scripts/rejected",
    "skills/pending",
    "skills/rejected",
    "memory/sessions",
    "tmp",
    "state/dispatch",
    "state/cron",
    "state/reflection",
)

# Subset of EPHEMERAL_DIRS that is safe to wipe while *other* concurrent
# test cases are mid-flight. Everything else in EPHEMERAL_DIRS is durable
# cross-case evidence (proposals, team runs, strategies, schedules, tasks)
# that parallel workers read back via API checks after their turn finished;
# wiping those mid-run erases another worker's freshly created artifacts.
PER_CASE_SAFE_DIRS: tuple[str, ...] = (
    "memory/sessions",
    "tmp",
    "state/reflection",
)

# Agent-authored recall/profile files are durable in a normal workspace, but
# isolated E2E runs need a clean model memory slate at suite start and before
# CSV rows marked reset_before=1.
MEMORY_STATE_PATHS: tuple[str, ...] = (
    "memory/global.md",
    "memory/mistakes.md",
    "memory/market_regimes.md",
    "memory/skill_learnings.md",
    "memory/operator_profile.jsonl",
    "memory/operator_profile.cache.json",
    "memory/profile_capture_state.json",
)

# Directories that must NEVER be deleted in safe mode (credentials!).
KEEP_DIRS: tuple[str, ...] = (
    "vault",
    "accounts",
    "skills/installed",          # accepted skills survive
    "scripts/approved",          # accepted scripts survive
    "subagents",                 # custom roles preserved by default
    "memory/notebook",           # long-term notes
    "state/init.json",
    "nerya.yml",
    "agents.yml",
)

# Safety net: refuse to operate on a workspace path that doesn't look
# like a Nerya workspace OR sits on top of common dangerous roots.
UNSAFE_PATH_PREFIXES: tuple[str, ...] = (
    "/", "/etc", "/home", "/root", "/usr", "/var",
    "C:\\Users", "C:\\Windows", "C:\\",
)


def _looks_like_workspace(p: Path) -> bool:
    """A workspace must already exist OR have at least one Nerya marker."""
    if not p.exists():
        return True  # we'll create the structure on first use
    if (p / "state").exists():
        return True
    if (p / "nerya.yml").exists():
        return True
    if (p / "accounts").exists():
        return True
    if (p / "evolution").exists():
        return True
    # An empty directory we just made — also OK.
    try:
        return not any(p.iterdir())
    except OSError:
        return False


def _is_unsafe(p: Path) -> bool:
    s = str(p.resolve())
    if s in {"/", "C:\\"}:
        return True
    if any(s == prefix for prefix in UNSAFE_PATH_PREFIXES):
        return True
    return False


def _wipe_path(target: Path, *, dry_run: bool, log: list[dict]) -> None:
    if not target.exists():
        log.append({"path": str(target), "action": "skip", "reason": "missing"})
        return
    if target.is_file():
        if not dry_run:
            try:
                target.unlink()
            except OSError as exc:
                # Best-effort — file may be locked by a running Nerya
                # runtime (common on Windows). Try to truncate instead so
                # we still leave a clean slate for the next test.
                try:
                    target.write_text("", encoding="utf-8")
                    log.append({"path": str(target), "action": "truncate",
                                "reason": str(exc)})
                    return
                except OSError as exc2:
                    log.append({"path": str(target), "action": "skip_locked",
                                "reason": str(exc2)})
                    return
        log.append({"path": str(target), "action": "unlink"})
        return
    # Directory — remove recursively. On Windows the runtime may hold
    # file handles open under e.g. teams/ or journals/; in that case
    # delete what we can and report the rest as warnings rather than
    # crashing the whole reset (which would also break the test run).
    if not dry_run:
        errors: list[str] = []
        def _onerror(func, path, excinfo):
            errors.append(f"{path}: {excinfo[1]}")
            try:
                # Try to delete contents one by one (keeps the directory
                # itself, but at least removes its files).
                Path(path).unlink(missing_ok=True)
            except OSError:
                pass
        shutil.rmtree(target, onerror=_onerror)
        if errors:
            log.append({"path": str(target), "action": "rmtree_partial",
                        "errors": errors[:5], "error_count": len(errors)})
            return
    log.append({"path": str(target), "action": "rmtree"})


def _safe_wipe(
    workspace: Path,
    *,
    dry_run: bool,
    clear_memory: bool = False,
    sync_prompt_bundle: bool = False,
    keep_evidence: bool = False,
) -> list[dict]:
    log: list[dict] = []
    wipe_dirs = PER_CASE_SAFE_DIRS if keep_evidence else EPHEMERAL_DIRS
    for rel in wipe_dirs:
        _wipe_path(workspace / rel, dry_run=dry_run, log=log)
    if clear_memory:
        for rel in MEMORY_STATE_PATHS:
            _wipe_path(workspace / rel, dry_run=dry_run, log=log)
    # Recreate the bare layout so subsequent runs find directories they
    # expect — but only after wiping. Idempotent.
    if not dry_run:
        for d in ("evolution/proposals", "journals", "inbox", "outbox",
                  "teams", "strategies", "agent_tasks", "triggers",
                  "tmp", "state"):
            (workspace / d).mkdir(parents=True, exist_ok=True)
        _seed_manual_agent_strategy(workspace, log=log)
    if sync_prompt_bundle:
        _sync_default_prompt_bundle(workspace, dry_run=dry_run, log=log)
    return log


def _sync_default_prompt_bundle(workspace: Path, *, dry_run: bool, log: list[dict]) -> None:
    """Refresh bundled prompt slots while preserving extra custom roles.

    Normal workspaces deliberately preserve operator-edited prompt files.
    Isolated E2E workspaces need a stronger reset boundary: stale default
    role overrides can otherwise leak from one CSV row into the next and
    change unrelated task domains. This explicit mode overwrites only slots
    declared by the shipped prompt bundle; additional custom role files stay
    in place.
    """

    if dry_run:
        log.append({
            "path": str(workspace / "agents" / "_provenance.yml"),
            "action": "sync_prompt_bundle_dry_run",
        })
        return
    try:
        from nerya.core.paths import WorkspacePaths
        from nerya.workspace.prompt_bundles import DEFAULT_BUNDLE_ID, load_bundle, seed_bundle

        paths = WorkspacePaths(root=workspace)
        summary = seed_bundle(
            paths,
            bundle=load_bundle(DEFAULT_BUNDLE_ID),
            overwrite_operator_edits=True,
        )
        log.append({
            "path": str(workspace / "agents" / "_provenance.yml"),
            "action": "sync_prompt_bundle",
            "written": list(summary.get("written") or []),
            "skipped_existing": list(summary.get("skipped_existing") or []),
        })
    except Exception as exc:
        log.append({
            "path": str(workspace / "agents" / "_provenance.yml"),
            "action": "sync_prompt_bundle_failed",
            "reason": str(exc),
        })
        raise


def _seed_manual_agent_strategy(workspace: Path, *, log: list[dict]) -> None:
    """Recreate the paper-only strategy used by direct chat order intents."""

    root = workspace / "strategies" / "manual_agent"
    history = workspace / "strategy_history" / "manual_agent"
    sessions = workspace / "strategy_sessions" / "manual_agent"
    try:
        root.mkdir(parents=True, exist_ok=True)
        history.mkdir(parents=True, exist_ok=True)
        sessions.mkdir(parents=True, exist_ok=True)
        (root / "prompts").mkdir(exist_ok=True)
        (root / "strategy.yml").write_text(
            "\n".join([
                "id: manual_agent",
                "title: Manual / agent-initiated paper trades",
                "status: paper",
                "account_id: paper_main",
                "markets:",
                "- PAPER:BTCUSDT",
                "- PAPER:ETHUSDT",
                "- PAPER:SOLUSDT",
                "paper_trading_enabled: true",
                "live_trading_enabled: false",
                "subagents: []",
                "trigger_kinds:",
                "- manual.intent",
                "driver: manual",
                "notes: Fallback strategy id for ad-hoc chat/order intents. Keep paper-only.",
                "",
            ]),
            encoding="utf-8",
        )
        (root / "config.yml").write_text(
            "\n".join([
                "min_confidence: 0.0",
                "position_size_usd: 500.0",
                "",
            ]),
            encoding="utf-8",
        )
        (root / "limits.yml").write_text(
            "\n".join([
                "allowed_markets:",
                "- PAPER:BTCUSDT",
                "- PAPER:ETHUSDT",
                "- PAPER:SOLUSDT",
                "max_single_order_usd: 1000.0",
                "max_total_exposure_usd: 2500.0",
                "daily_loss_usd: 500.0",
                "max_drawdown_pct: 0.10",
                "min_confidence: 0.0",
                "max_slippage_bps: 50",
                "max_stale_seconds: 60",
                "approval_threshold_usd: 1500.0",
                "",
            ]),
            encoding="utf-8",
        )
        learnings = root / "learnings.md"
        if not learnings.exists():
            learnings.write_text("# Learnings - manual_agent\n\n- (empty)\n", encoding="utf-8")
        for name in (
            "triggers", "skill_calls", "subagents", "decisions", "intents",
            "risk", "orders", "fills", "pnl", "messages", "reviews",
        ):
            (history / f"{name}.jsonl").touch(exist_ok=True)
        log.append({"path": str(root), "action": "seed_manual_agent"})
    except OSError as exc:
        log.append({"path": str(root), "action": "seed_manual_agent_failed", "reason": str(exc)})


def _full_wipe(workspace: Path, *, dry_run: bool) -> list[dict]:
    log: list[dict] = []
    if workspace.exists():
        if not dry_run:
            shutil.rmtree(workspace, ignore_errors=False)
        log.append({"path": str(workspace), "action": "rmtree_full"})
    if not dry_run:
        workspace.mkdir(parents=True, exist_ok=True)
    return log


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n", 1)[0])
    ap.add_argument(
        "--workspace",
        default=os.environ.get("NERYA_WORKSPACE"),
        help="Workspace path. Defaults to $NERYA_WORKSPACE.",
    )
    ap.add_argument(
        "--full",
        action="store_true",
        help="Wipe entire workspace directory (use only for isolated test dirs).",
    )
    ap.add_argument(
        "--dry-run",
        action="store_true",
        help="Print what would be deleted without changing anything.",
    )
    ap.add_argument(
        "--clear-memory",
        action="store_true",
        help=(
            "Also clear agent-authored memory/profile recall files. Intended "
            "for isolated E2E workspaces; preserves vault/accounts."
        ),
    )
    ap.add_argument(
        "--sync-prompt-bundle",
        action="store_true",
        help=(
            "Refresh shipped default agent/subagent prompt bundle slots. "
            "Intended for isolated E2E workspaces; preserves extra custom roles."
        ),
    )
    ap.add_argument(
        "--keep-evidence",
        action="store_true",
        help=(
            "Per-case reset for PARALLEL runs: clear only per-session memory "
            "state and keep durable cross-case evidence (proposals, teams, "
            "strategies, schedules) that sibling workers still verify."
        ),
    )
    ap.add_argument(
        "--log",
        default=None,
        help="Path to write a JSON log of actions taken.",
    )
    ap.add_argument(
        "--quiet",
        action="store_true",
        help="Suppress stdout summary.",
    )
    args = ap.parse_args(argv)

    if not args.workspace:
        print(
            "error: --workspace is required (or set $NERYA_WORKSPACE)",
            file=sys.stderr,
        )
        return 2

    ws = Path(args.workspace).expanduser().resolve()
    if _is_unsafe(ws):
        print(
            f"error: refusing to reset unsafe path: {ws}",
            file=sys.stderr,
        )
        return 1
    if not _looks_like_workspace(ws):
        print(
            f"error: {ws} does not look like a Nerya workspace "
            "(no nerya.yml/state/accounts/evolution). Refusing.",
            file=sys.stderr,
        )
        return 1

    started = time.time()
    if args.full:
        log = _full_wipe(ws, dry_run=args.dry_run)
        if args.sync_prompt_bundle:
            _sync_default_prompt_bundle(ws, dry_run=args.dry_run, log=log)
    else:
        log = _safe_wipe(
            ws,
            dry_run=args.dry_run,
            clear_memory=args.clear_memory,
            sync_prompt_bundle=args.sync_prompt_bundle,
            keep_evidence=args.keep_evidence,
        )
    elapsed_ms = int((time.time() - started) * 1000)

    summary = {
        "workspace": str(ws),
        "mode": "full" if args.full else ("safe-keep-evidence" if args.keep_evidence else "safe"),
        "clear_memory": bool(args.clear_memory and not args.full),
        "sync_prompt_bundle": bool(args.sync_prompt_bundle),
        "dry_run": args.dry_run,
        "elapsed_ms": elapsed_ms,
        "actions": log,
    }
    if args.log:
        Path(args.log).parent.mkdir(parents=True, exist_ok=True)
        Path(args.log).write_text(
            json.dumps(summary, indent=2),
            encoding="utf-8",
        )
    if not args.quiet:
        print(
            f"reset_workspace: {summary['mode']} mode, "
            f"{len(log)} action(s), {elapsed_ms} ms"
            + (" (dry-run)" if args.dry_run else "")
        )
        for entry in log:
            print(f"  {entry['action']:>12s}  {entry['path']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
