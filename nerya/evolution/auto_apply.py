"""Tiered autonomy for evolution proposals (auto-apply tier).

A narrow, hard-coded lane in which the agent's own proposals can land
without a human click — everything outside the lane keeps the normal
Inbox approval flow, and :data:`~nerya.evolution.patch_proposal.
PROTECTED_SCOPES` still rejects risk / credential / kill-switch
mutations long before this module ever sees them.

The lane is deliberately restrictive and *not* extensible from config:

* kind must be in :data:`AUTO_APPLY_KINDS` (prose-level prompt patches
  and non-protected core-config patches);
* every staged write and declared deletion must match
  :data:`AUTO_APPLY_PATH_ALLOWLIST`;
* at most :data:`MAX_AUTO_APPLY_FILES` touched files and a unified diff
  of at most :data:`MAX_AUTO_APPLY_DIFF_LINES` changed lines;
* the proposal's validation plan must exist and be fully *passed*
  (required steps green with evidence refs), regardless of kind;
* the workspace opt-in flag ``evolution.auto_apply.enabled`` must be
  true. That key is itself a protected scope, so the agent cannot
  propose widening its own autonomy.

Applied proposals enter a 24h observation window. ``review`` rolls a
proposal back automatically when a post-apply observation lands with a
failing status inside the window.
"""

from __future__ import annotations

import difflib
import fnmatch
import time
from typing import Any

from ..core import jsonl
from ..core.config import Config
from ..core.paths import WorkspacePaths
from ..core.time import now_iso
from .candidate_bundle import proposal_deleted_paths
from .patch_proposal import Proposal, list_proposals, set_state
from .post_apply_observation import post_apply_observations_by_proposal
from .promotion import _proposal_validation_gate, apply_proposal
from .rollback import rollback_proposal

#: Proposal kinds allowed into the auto-apply lane. Hard-coded on
#: purpose — do not read this from workspace config.
AUTO_APPLY_KINDS: frozenset[str] = frozenset({
    "prompt_patch",
    "core_config_patch",
})

#: Workspace-relative paths an auto-applied proposal may touch.
#: Everything else (strategy code, skills, scripts, triggers, …)
#: requires a human in the loop.
AUTO_APPLY_PATH_ALLOWLIST: tuple[str, ...] = (
    "agents/*.md",
    "subagents/*.agent.md",
    "strategies/*/prompts/*.agent.md",
    "news_feeds.yml",
    "news_feeds.yaml",
    "policies/planner.yml",
    "policies/tier_policy.yml",
)

MAX_AUTO_APPLY_FILES = 4
MAX_AUTO_APPLY_DIFF_LINES = 200
OBSERVATION_WINDOW_SECONDS = 24 * 3600

#: Post-apply observation statuses that trigger an automatic rollback
#: while the observation window is open.
FAILING_OBSERVATION_STATUSES: frozenset[str] = frozenset({
    "failed",
    "regressed",
    "degraded",
})


def auto_apply_enabled(config: Config) -> bool:
    return bool(config.get("evolution.auto_apply.enabled", False))


def evaluate_auto_apply(
    paths: WorkspacePaths,
    config: Config,
    prop: Proposal,
) -> dict[str, Any]:
    """Return the eligibility verdict for one pending proposal."""

    reasons: list[str] = []
    if not auto_apply_enabled(config):
        reasons.append("auto_apply_disabled")
    if prop.state != "pending_review":
        reasons.append(f"state_{prop.state}")
    if prop.kind not in AUTO_APPLY_KINDS:
        reasons.append(f"kind_not_eligible:{prop.kind}")

    after_dir = prop.path / "after"
    after_files: list[Any] = []
    if after_dir.is_symlink():
        reasons.append("candidate_bundle_symlink")
    elif after_dir.is_dir():
        for path in sorted(after_dir.rglob("*")):
            if path.is_symlink():
                reasons.append("candidate_bundle_symlink")
            elif path.is_file():
                after_files.append(path)
    try:
        deleted_files = proposal_deleted_paths(prop.path)
    except (OSError, ValueError):
        deleted_files = []
        reasons.append("invalid_deletion_declaration")
    if not after_files and not deleted_files:
        reasons.append("no_materialized_changes")
    file_count = len(after_files) + len(deleted_files)
    if file_count > MAX_AUTO_APPLY_FILES:
        reasons.append(f"too_many_files:{file_count}>{MAX_AUTO_APPLY_FILES}")

    diff_lines = 0
    for src in after_files:
        rel = src.relative_to(after_dir).as_posix()
        if not _path_allowed(rel):
            reasons.append(f"path_not_allowed:{rel}")
            continue
        diff_lines += _diff_line_count(paths, rel, src)
    for rel in deleted_files:
        if not _path_allowed(rel):
            reasons.append(f"path_not_allowed:{rel}")
            continue
        diff_lines += _diff_line_count(paths, rel)
    if diff_lines > MAX_AUTO_APPLY_DIFF_LINES:
        reasons.append(f"diff_too_large:{diff_lines}>{MAX_AUTO_APPLY_DIFF_LINES}")

    validation = _proposal_validation_gate(paths, prop, required=True)
    if not validation.get("ok"):
        reasons.append(str(validation.get("reason") or "validation_not_passed"))

    return {
        "proposal_id": prop.id,
        "eligible": not reasons,
        "reasons": reasons,
        "kind": prop.kind,
        "file_count": file_count,
        "after_file_count": len(after_files),
        "deleted_file_count": len(deleted_files),
        "diff_lines": diff_lines,
        "validation": validation,
    }


def auto_apply_tick(paths: WorkspacePaths, config: Config) -> dict[str, Any]:
    """One pass of the tier: apply eligible proposals, review applied ones.

    Safe to call from a schedule or from the API; every action is
    journalled with ``actor=auto_apply_tier`` so the Inbox / replay UI
    can distinguish it from operator clicks.
    """

    applied: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    if auto_apply_enabled(config):
        for prop in list_proposals(paths):
            if prop.state != "pending_review":
                continue
            verdict = evaluate_auto_apply(paths, config, prop)
            if not verdict["eligible"]:
                if prop.kind in AUTO_APPLY_KINDS:
                    skipped.append({
                        "proposal_id": prop.id,
                        "reasons": verdict["reasons"],
                    })
                continue
            set_state(paths, prop.id, "approved", note="auto_apply_tier")
            result = apply_proposal(paths, prop.id)
            row = {
                "kind": "proposal.auto_applied",
                "ts": now_iso(),
                "proposal_id": prop.id,
                "actor": "auto_apply_tier",
                "ok": bool(result.get("ok")),
                "observe_until_epoch": time.time() + OBSERVATION_WINDOW_SECONDS,
                "verdict": {
                    "diff_lines": verdict["diff_lines"],
                    "file_count": verdict["file_count"],
                    "after_file_count": verdict["after_file_count"],
                    "deleted_file_count": verdict["deleted_file_count"],
                },
            }
            if not result.get("ok"):
                # Apply gate refused after approval — put the proposal
                # back in the queue for a human instead of leaving it
                # silently approved.
                set_state(
                    paths, prop.id, "pending_review",
                    note=f"auto_apply_gate_blocked:{result.get('reason')}",
                )
                row["reason"] = result.get("reason")
            jsonl.append(paths.journal("evolution"), row)
            applied.append({
                "proposal_id": prop.id,
                "ok": bool(result.get("ok")),
                "reason": result.get("reason"),
            })

    reviewed = review_auto_applied(paths)
    return {
        "ok": True,
        "enabled": auto_apply_enabled(config),
        "applied": applied,
        "skipped": skipped,
        "rolled_back": reviewed["rolled_back"],
    }


def review_auto_applied(paths: WorkspacePaths) -> dict[str, Any]:
    """Roll back auto-applied proposals with failing observations.

    Rollback triggers are hard-coded (:data:`FAILING_OBSERVATION_
    STATUSES` inside :data:`OBSERVATION_WINDOW_SECONDS`) — they are a
    safety property, not a tunable.
    """

    windows = _open_observation_windows(paths)
    if not windows:
        return {"ok": True, "rolled_back": []}
    rolled_back: list[dict[str, Any]] = []
    states = {p.id: p.state for p in list_proposals(paths)}
    for pid, observe_until in windows.items():
        if states.get(pid) != "applied":
            continue
        observations = post_apply_observations_by_proposal(
            paths, proposal_id=pid,
        ).get(pid) or []
        failing = [
            obs for obs in observations
            if str(obs.get("status") or "").lower() in FAILING_OBSERVATION_STATUSES
        ]
        if not failing:
            continue
        result = rollback_proposal(paths, pid)
        jsonl.append(paths.journal("evolution"), {
            "kind": "proposal.auto_rolled_back",
            "ts": now_iso(),
            "proposal_id": pid,
            "actor": "auto_apply_tier",
            "ok": bool(result.get("ok")),
            "failing_observations": [
                {
                    "status": obs.get("status"),
                    "journal_ref": obs.get("journal_ref"),
                }
                for obs in failing[:5]
            ],
        })
        rolled_back.append({
            "proposal_id": pid,
            "ok": bool(result.get("ok")),
            "reason": result.get("reason"),
        })
        _ = observe_until  # window bound already filtered below
    return {"ok": True, "rolled_back": rolled_back}


# ------------------------------------------------------------------ helpers


def _path_allowed(rel_posix: str) -> bool:
    return any(
        fnmatch.fnmatchcase(rel_posix, pattern)
        for pattern in AUTO_APPLY_PATH_ALLOWLIST
    )


def _diff_line_count(paths: WorkspacePaths, rel_posix: str, after_file=None) -> int:
    if after_file is None:
        after_text = ""
    else:
        try:
            after_text = after_file.read_text(encoding="utf-8")
        except Exception:
            return MAX_AUTO_APPLY_DIFF_LINES + 1  # unreadable — force ineligible
    current = paths.root / rel_posix
    try:
        before_text = current.read_text(encoding="utf-8") if current.exists() else ""
    except Exception:
        return MAX_AUTO_APPLY_DIFF_LINES + 1
    changed = 0
    for line in difflib.unified_diff(
        before_text.splitlines(), after_text.splitlines(), lineterm="",
    ):
        if line.startswith(("+", "-")) and not line.startswith(("+++", "---")):
            changed += 1
    return changed


def _open_observation_windows(paths: WorkspacePaths) -> dict[str, float]:
    """Map proposal_id -> observe_until for windows still open."""

    now = time.time()
    windows: dict[str, float] = {}
    try:
        rows = jsonl.read_all(paths.journal("evolution"))
    except Exception:
        return {}
    for row in rows:
        if row.get("kind") != "proposal.auto_applied" or not row.get("ok"):
            continue
        pid = str(row.get("proposal_id") or "")
        until = float(row.get("observe_until_epoch") or 0.0)
        if pid and until > now:
            windows[pid] = until
    return windows


__all__ = [
    "AUTO_APPLY_KINDS",
    "AUTO_APPLY_PATH_ALLOWLIST",
    "auto_apply_enabled",
    "auto_apply_tick",
    "evaluate_auto_apply",
    "review_auto_applied",
]
