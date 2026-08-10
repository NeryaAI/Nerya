"""Evolution proposals. Always written to disk; nothing auto-applies."""

from __future__ import annotations

import fnmatch
import json
import shutil
from dataclasses import asdict, dataclass
from pathlib import Path, PurePosixPath
from typing import Any

from ..core import jsonl, yaml_io
from ..core.atomic_write import atomic_write_text
from ..core.ids import proposal_id
from ..core.paths import WorkspacePaths
from ..core.time import now_iso
from .candidate_bundle import build_candidate_bundle


PROTECTED_SCOPES = {
    # active risk limits
    "strategies/*/limits.yml",
    # accounts / exchanges / secret refs
    "accounts/accounts.yml",
    "accounts/exchanges.yml",
    "accounts/secrets.refs.yml",
    # vault files
    "vault/*",
    # live trading / kill switch
    "nerya.yml:runtime.live_trading_enabled",
    "nerya.yml:runtime.kill_switch",
    # trading/risk posture. These may be discussed as advisory changes,
    # but cannot be staged through the self-config patch surface.
    "nerya.yml:trading.*",
    "nerya.yml:risk",
    "nerya.yml:risk.*",
    "nerya.yml:risk_limits",
    "nerya.yml:risk_limits.*",
    # signer / approval policy
    "approvals/policy.yml",
    "approvals/signer_policy.yml",
    "approvals/llm_high_tier_callers.yml",
    # tiered-autonomy lane (see nerya.evolution.auto_apply) — the agent
    # must never be able to propose widening its own auto-apply
    # whitelist or flipping the opt-in flag.
    "nerya.yml:evolution.auto_apply",
    "nerya.yml:evolution.auto_apply.*",
    # trigger routes (rate limits / payload caps) live here too
    "triggers/routes.yml:*.max_payload_bytes",
    "triggers/routes.yml:*.max_per_minute",
}


@dataclass
class Proposal:
    id: str
    kind: str          # learning_update|prompt_patch|script_proposal|skill_proposal
                       # |trigger_route_patch|strategy_config_patch|risk_limit_suggestion
    state: str         # draft|pending_review|approved|applied|rejected
                       # |rolled_back|superseded
    path: Path
    summary: str
    ts: str
    target: str | None = None
    evidence_refs: list[str] | None = None
    source_event_id: str | None = None
    validation_plan_id: str | None = None
    metadata: dict[str, Any] | None = None

    def asdict(self) -> dict[str, Any]:
        return {**asdict(self), "path": str(self.path)}


ALLOWED_KINDS = {
    "learning_update",
    "prompt_patch",
    "script_proposal",
    "skill_proposal",
    "trigger_route_patch",
    "strategy_config_patch",
    "risk_limit_suggestion",
    "provider_proposal",        # auto-authored exchange/chain provider
    "core_config_patch",        # non-protected parts of nerya.yml / agents.yml
    "skill_install_request",    # external skill imported from dir / git / tar
    "skill_scaffold",           # runnable skill scaffolded in-place
    "gateway_platform_proposal", # messaging gateway adapter / platform support
    "core_feature_proposal",     # non-protected runtime/core feature plan
    "strategy_package_proposal", # agent-generated strategy package; brings
                                 # files under after/strategies/<id>/* that
                                 # promotion.py copies into the workspace.
    "strategy_tuning_proposal",  # patch produced by the per-strategy tuning
                                 # loop; same shape but scoped to
                                 # the tuning subagent's recommendations.
    "evolution_asset_proposal",  # candidate Gene/Capsule changes that still
                                 # require operator review before promotion.
}


def create_proposal(
    paths: WorkspacePaths,
    *,
    kind: str,
    summary: str,
    rationale: str = "",
    diff: str | None = None,
    test_plan: str = "",
    rollback: str = "",
    extra_files: dict[str, str] | None = None,
    initial_state: str = "draft",
    target: str | None = None,
    evidence_refs: list[str] | None = None,
    source_event_id: str | None = None,
    validation_plan_id: str | None = None,
    metadata: dict[str, Any] | None = None,
) -> Proposal:
    if kind not in ALLOWED_KINDS:
        raise ValueError(f"unknown proposal kind: {kind!r}. "
                         f"allowed={sorted(ALLOWED_KINDS)}")
    if target is not None and is_protected(target):
        # A proposal may *mention* a protected scope in its summary, but it
        # must never declare it as its mutation target — we reject here to
        # keep that boundary bright.
        from ..core.errors import ProtectedScopeViolation
        raise ProtectedScopeViolation(
            f"proposal target is in a protected scope: {target}"
        )
    if validation_plan_id and not _safe_validation_id(validation_plan_id):
        raise ValueError(f"invalid validation plan id: {validation_plan_id!r}")
    pid = proposal_id()
    pdir = paths.proposals / pid
    pdir.mkdir(parents=True, exist_ok=True)
    meta = {
        "id": pid, "kind": kind, "state": initial_state,
        "summary": summary, "ts": now_iso(),
    }
    if evidence_refs:
        meta["evidence_refs"] = list(evidence_refs)
    if source_event_id:
        meta["source_event_id"] = source_event_id
    if validation_plan_id:
        meta["validation_plan_id"] = validation_plan_id
    if metadata:
        meta["metadata"] = dict(metadata)
    if target is not None:
        meta["target"] = target
        atomic_write_text(pdir / "target.yml", yaml_io.dumps({"target": target}))
    atomic_write_text(pdir / "proposal.yml", yaml_io.dumps(meta))
    atomic_write_text(pdir / "rationale.md", rationale or f"# {summary}\n")
    if diff is not None:
        atomic_write_text(pdir / "diff.patch", diff)
    atomic_write_text(pdir / "test_plan.md", test_plan or "# Test plan\n\nTBD\n")
    atomic_write_text(pdir / "rollback.md", rollback or "# Rollback\n\nTBD\n")
    staged_files = [
        (_staging_path(pdir, name), content)
        for name, content in (extra_files or {}).items()
    ]
    for path, content in staged_files:
        atomic_write_text(path, content)
    # Freeze the reviewed inputs after all staged files exist.  The bundle is
    # content addressed; apply_proposal rechecks it before any workspace write.
    candidate_context = {}
    if isinstance(metadata, dict) and isinstance(metadata.get("candidate_context"), dict):
        candidate_context = dict(metadata["candidate_context"])
    if isinstance(metadata, dict):
        for key in (
            "base_revision", "model_schema", "model_schema_digest",
            "tool_schema", "tool_schema_digest", "eval_suite",
            "eval_suite_digest",
        ):
            if key in metadata and key not in candidate_context:
                candidate_context[key] = metadata[key]
    candidate_bundle = build_candidate_bundle(
        paths.root,
        pdir,
        context=candidate_context,
    )
    meta["candidate_bundle"] = candidate_bundle
    atomic_write_text(pdir / "proposal.yml", yaml_io.dumps(meta))
    atomic_write_text(
        pdir / "candidate_bundle.json",
        json.dumps(candidate_bundle, indent=2, ensure_ascii=False) + "\n",
    )
    _bind_validation_plan(
        paths,
        validation_plan_id,
        proposal_id=pid,
        candidate_bundle_digest=str(candidate_bundle["digest"]),
    )
    jsonl.append(paths.journal("evolution"), {
        "kind": "proposal.created", "proposal_id": pid,
        "proposal_kind": kind, "state": initial_state, "summary": summary,
        "source_event_id": source_event_id,
        "evidence_refs": list(evidence_refs or []),
    })
    try:
        from .event_store import record_event

        record_event(
            paths,
            parent_id=source_event_id,
            proposal_id=pid,
            mutation_scope=[target] if target else [],
            validation_status="not_run",
            outcome="proposed" if initial_state != "draft" else "candidate",
            summary=summary,
            evidence_refs=list(evidence_refs or []),
            metadata={
                "proposal_kind": kind,
                "validation_plan_id": validation_plan_id,
                "candidate_bundle_digest": candidate_bundle["digest"],
            },
        )
    except Exception:
        pass
    return Proposal(id=pid, kind=kind, state=initial_state, path=pdir,
                    summary=summary, ts=meta["ts"], target=target,
                    evidence_refs=list(evidence_refs or []),
                    source_event_id=source_event_id,
                    validation_plan_id=validation_plan_id,
                    metadata={
                        **dict(metadata or {}),
                        "candidate_bundle": candidate_bundle,
                    })


def _meta_file(pdir: Path) -> Path:
    return pdir / "proposal.yml"


def _staging_path(pdir: Path, name: str) -> Path:
    """Keep generated proposal files inside their staging directory."""

    text = str(name or "").strip().replace("\\", "/")
    relative = PurePosixPath(text)
    if (
        not text
        or relative.is_absolute()
        or relative.as_posix() in {"", "."}
        or any(part == ".." for part in relative.parts)
    ):
        raise ValueError(f"extra file path escapes proposal staging: {name!r}")
    root = pdir.resolve()
    target = (root / Path(*relative.parts)).resolve(strict=False)
    if target != root and root not in target.parents:
        raise ValueError(f"extra file path escapes proposal staging: {name!r}")
    return target


def _bind_validation_plan(
    paths: WorkspacePaths,
    plan_id: str | None,
    *,
    proposal_id: str,
    candidate_bundle_digest: str,
) -> bool:
    """Bind a plan to the frozen candidate without rewriting a prior binding."""

    if not plan_id:
        return True
    plan_text = str(plan_id).strip()
    if not _safe_validation_id(plan_text):
        return False
    path = paths.evolution_validation_plans / f"{plan_text}.json"
    try:
        plan = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return False
    if not isinstance(plan, dict):
        return False
    bound_proposal = str(plan.get("proposal_id") or "").strip()
    bound_digest = str(plan.get("candidate_bundle_digest") or "").strip()
    # A plan can be attached once.  Reusing it for a different proposal or
    # candidate must remain visible to the action gate instead of being silently
    # resealed here.
    if bound_proposal and bound_proposal != proposal_id:
        return False
    if bound_digest and bound_digest != candidate_bundle_digest:
        return False
    plan["proposal_id"] = proposal_id
    plan["candidate_bundle_digest"] = candidate_bundle_digest
    atomic_write_text(path, json.dumps(plan, indent=2, ensure_ascii=False) + "\n")
    return True


def _safe_validation_id(value: Any) -> bool:
    text = str(value or "").strip()
    return (
        bool(text)
        and "/" not in text
        and "\\" not in text
        and Path(text).name == text
        and text not in {".", ".."}
    )


def reseal_candidate_bundle(
    paths: WorkspacePaths,
    pid: str,
    *,
    note: str = "",
) -> dict[str, Any] | None:
    """Refresh a candidate only while it is still in operator review.

    Generators may attach a backtest/replay artifact after the initial draft
    is written.  Re-sealing that pre-approval evidence keeps the apply-time
    CAS strict without treating an approved proposal as mutable.
    """

    proposal = next((p for p in list_proposals(paths) if p.id == pid), None)
    if proposal is None or proposal.state not in {"draft", "pending_review", "proposed"}:
        return None
    existing = yaml_io.load(_meta_file(proposal.path), default={}) or {}
    old_bundle = existing.get("candidate_bundle")
    context = old_bundle.get("context") if isinstance(old_bundle, dict) else None
    bundle = build_candidate_bundle(
        paths.root,
        proposal.path,
        context=context if isinstance(context, dict) else None,
    )
    if not _rebind_validation_plan_for_review(
        paths,
        proposal,
        candidate_bundle_digest=str(bundle["digest"]),
    ):
        return None
    existing["candidate_bundle"] = bundle
    atomic_write_text(_meta_file(proposal.path), yaml_io.dumps(existing))
    atomic_write_text(
        proposal.path / "candidate_bundle.json",
        json.dumps(bundle, indent=2, ensure_ascii=False) + "\n",
    )
    jsonl.append(paths.journal("evolution"), {
        "kind": "proposal.bundle_resealed",
        "proposal_id": pid,
        "bundle_digest": bundle["digest"],
        "note": note,
    })
    return bundle


def _rebind_validation_plan_for_review(
    paths: WorkspacePaths,
    proposal: Proposal,
    *,
    candidate_bundle_digest: str,
) -> bool:
    """Move an attached plan to the new reviewed candidate and invalidate runs."""

    plan_id = str(proposal.validation_plan_id or "").strip()
    if not plan_id:
        return True
    if not _safe_validation_id(plan_id):
        return False
    path = paths.evolution_validation_plans / f"{plan_id}.json"
    try:
        plan = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False
    if not isinstance(plan, dict):
        return False
    bound_proposal = str(plan.get("proposal_id") or "").strip()
    if bound_proposal and bound_proposal != proposal.id:
        return False
    plan["proposal_id"] = proposal.id
    plan["candidate_bundle_digest"] = candidate_bundle_digest
    # Any previous run covered different bytes.  Preserve the step definitions
    # but force a fresh execution before the proposal can be applied.
    plan["status"] = "not_run"
    plan.pop("last_run_id", None)
    plan.pop("last_run_at", None)
    for step in plan.get("steps") or []:
        if not isinstance(step, dict):
            continue
        step["status"] = "not_run"
        step["evidence_ref"] = None
        if step.get("notes"):
            step["notes"] = "candidate bundle resealed; validation must be rerun"
    try:
        atomic_write_text(path, json.dumps(plan, indent=2, ensure_ascii=False) + "\n")
    except OSError:
        return False
    return True


# Proposal states that still sit in the operator's review queue. A recurring
# generator (e.g. the per-strategy tuning cron) should collapse these into
# its newest proposal instead of stacking one per run.
OPEN_STATES = ("draft", "pending_review", "proposed")


def supersede_pending_siblings(
    paths: WorkspacePaths,
    *,
    kind: str,
    target: str | None,
    keep_id: str,
    note: str = "",
) -> list[str]:
    """Mark older open proposals for the same ``kind`` + ``target`` as
    ``superseded``.

    Recurring generators (strategy tuning crons and similar) emit a fresh
    proposal every run; without this, a strategy that misbehaves for weeks
    piles up hundreds of near-identical ``pending_review`` items and the
    operator inbox becomes unreadable. The newest proposal carries the same
    root-cause evidence, so older open siblings are historical noise —
    they stay on disk for the audit trail but leave the review queue.
    """

    if not target:
        return []
    superseded: list[str] = []
    for p in list_proposals(paths):
        if p.id == keep_id:
            continue
        if p.kind != kind or p.target != target:
            continue
        if p.state not in OPEN_STATES:
            continue
        set_state(
            paths, p.id, "superseded",
            note=note or f"superseded_by:{keep_id}",
        )
        superseded.append(p.id)
    return superseded


def list_proposals(paths: WorkspacePaths) -> list[Proposal]:
    root = paths.proposals
    if root.is_symlink() or not root.exists() or not root.is_dir():
        return []
    out: list[Proposal] = []
    for d in sorted(root.iterdir()):
        if d.is_symlink() or not d.is_dir():
            continue
        try:
            resolved = d.resolve()
            resolved.relative_to(root.resolve())
        except (OSError, ValueError):
            continue
        meta = yaml_io.load(_meta_file(d), default={}) or {}
        if not meta.get("id"):
            continue
        proposal_metadata = dict(meta.get("metadata") or {})
        if isinstance(meta.get("candidate_bundle"), dict):
            proposal_metadata["candidate_bundle"] = dict(meta["candidate_bundle"])
        out.append(Proposal(
            id=meta["id"],
            kind=meta.get("kind", "unknown"),
            state=meta.get("state", "draft"),
            path=d,
            summary=meta.get("summary", ""),
            ts=meta.get("ts", ""),
            target=meta.get("target"),
            evidence_refs=list(meta.get("evidence_refs") or []),
            source_event_id=meta.get("source_event_id"),
            validation_plan_id=meta.get("validation_plan_id"),
            metadata=proposal_metadata,
        ))
    return out


def set_state(paths: WorkspacePaths, pid: str, state: str,
              *, note: str = "") -> Proposal | None:
    allowed_states = {
        "draft", "pending_review", "proposed", "approved", "applied",
        "rejected", "rolled_back", "superseded",
    }
    if state not in allowed_states:
        raise ValueError(f"unknown proposal state: {state!r}")
    for p in list_proposals(paths):
        if p.id == pid:
            if p.state in {"rejected", "rolled_back", "superseded"}:
                # Terminal lifecycle records are immutable. Re-approving an
                # applied proposal could replay the same workspace mutation.
                return p if state == p.state else None
            meta = yaml_io.load(_meta_file(p.path), default={}) or {}
            meta["state"] = state
            meta["state_ts"] = now_iso()
            if note:
                meta["state_note"] = note
            atomic_write_text(_meta_file(p.path), yaml_io.dumps(meta))
            jsonl.append(paths.journal("evolution"), {
                "kind": "proposal.state",
                "proposal_id": pid, "state": state, "note": note,
                "source_event_id": meta.get("source_event_id"),
                "evidence_refs": list(meta.get("evidence_refs") or []),
            })
            try:
                from .event_store import record_event

                outcome = (
                    "approved" if state == "approved"
                    else "applied" if state == "applied"
                    else "rejected" if state == "rejected"
                    else "rolled_back" if state == "rolled_back"
                    else "candidate"
                )
                record_event(
                    paths,
                    parent_id=meta.get("source_event_id"),
                    proposal_id=pid,
                    mutation_scope=[p.target] if p.target else [],
                    validation_status=meta.get("validation_status") or "not_run",
                    outcome=outcome,
                    # Approval/application are lifecycle decisions; neither
                    # is evidence that the change improved outcomes.
                    outcome_score=(
                        0.0 if state in {"approved", "applied"}
                        else -0.5 if state in {"rejected", "rolled_back"}
                        else 0.0
                    ),
                    summary=f"Proposal {pid} state changed to {state}.",
                    evidence_refs=list(meta.get("evidence_refs") or []),
                    metadata={
                        "note": note,
                        "proposal_kind": p.kind,
                        **(
                            {
                                "approval_status": "approved",
                                "reward_status": "unevaluated",
                            }
                            if state == "approved"
                            else {
                                "observation_status": "pending",
                                "reward_status": "unevaluated",
                            }
                            if state == "applied"
                            else {}
                        ),
                    },
                )
            except Exception:
                pass
            updated_metadata = dict(meta.get("metadata") or {})
            if isinstance(meta.get("candidate_bundle"), dict):
                updated_metadata["candidate_bundle"] = dict(meta["candidate_bundle"])
            return Proposal(id=pid, kind=p.kind, state=state,
                            path=p.path, summary=p.summary,
                            ts=meta["state_ts"], target=p.target,
                            evidence_refs=list(meta.get("evidence_refs") or []),
                            source_event_id=meta.get("source_event_id"),
                            validation_plan_id=meta.get("validation_plan_id"),
                            metadata=updated_metadata)
    return None


def delete_proposal(
    paths: WorkspacePaths,
    pid: str,
    *,
    force: bool = False,
    note: str = "",
) -> dict[str, Any]:
    """Remove a proposal's on-disk record.

    Pending/draft/rejected/rolled_back proposals (e.g. an agent-generated
    strategy package an operator decides not to keep) can always be deleted.
    An ``applied`` proposal is the audit trail of a change that already landed
    in the workspace, so we refuse to delete it unless ``force`` is set.

    Returns a status dict instead of raising for the not-found / refused
    cases so callers can map them straight to a tool or API response.
    """
    target: Proposal | None = None
    for p in list_proposals(paths):
        if p.id == pid:
            target = p
            break
    if target is None:
        return {
            "ok": False,
            "proposal_id": pid,
            "deleted": False,
            "reason": "not_found",
        }
    if target.state == "applied" and not force:
        return {
            "ok": False,
            "proposal_id": pid,
            "deleted": False,
            "state": target.state,
            "reason": "applied_requires_force",
        }

    meta = yaml_io.load(_meta_file(target.path), default={}) or {}
    try:
        shutil.rmtree(target.path)
    except FileNotFoundError:
        pass
    except Exception as exc:
        return {
            "ok": False,
            "proposal_id": pid,
            "deleted": False,
            "state": target.state,
            "reason": f"delete_failed: {type(exc).__name__}: {exc}",
        }

    jsonl.append(paths.journal("evolution"), {
        "kind": "proposal.deleted",
        "proposal_id": pid,
        "proposal_kind": target.kind,
        "prev_state": target.state,
        "forced": bool(force),
        "note": note,
        "source_event_id": meta.get("source_event_id"),
    })
    try:
        from .event_store import record_event

        record_event(
            paths,
            parent_id=meta.get("source_event_id"),
            proposal_id=pid,
            mutation_scope=[target.target] if target.target else [],
            validation_status=meta.get("validation_status") or "not_run",
            # A deleted pending proposal is, for timeline purposes, a removal
            # of a candidate. We tag the discrete action in metadata.
            outcome="rejected",
            outcome_score=-0.5,
            summary=f"Proposal {pid} deleted.",
            evidence_refs=list(meta.get("evidence_refs") or []),
            metadata={
                "action": "deleted",
                "note": note,
                "proposal_kind": target.kind,
                "prev_state": target.state,
                "forced": bool(force),
            },
        )
    except Exception:
        pass
    return {
        "ok": True,
        "proposal_id": pid,
        "deleted": True,
        "kind": target.kind,
        "prev_state": target.state,
        "summary": target.summary,
    }


def is_protected(target: str) -> bool:
    for rule in PROTECTED_SCOPES:
        if _matches(rule, target):
            return True
    return False


def _matches(rule: str, target: str) -> bool:
    if "*" in rule:
        return fnmatch.fnmatchcase(target, rule)
    return rule == target
