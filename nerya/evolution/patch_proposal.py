"""Evolution proposals. Always written to disk; nothing auto-applies."""

from __future__ import annotations

import json
import fnmatch
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from ..core import jsonl, yaml_io
from ..core.atomic_write import atomic_write_text
from ..core.ids import proposal_id
from ..core.paths import WorkspacePaths
from ..core.time import now_iso


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
    # trigger routes (rate limits / payload caps) live here too
    "triggers/routes.yml:*.max_payload_bytes",
    "triggers/routes.yml:*.max_per_minute",
}


@dataclass
class Proposal:
    id: str
    kind: str          # learning_update|prompt_patch|script_proposal|skill_proposal
                       # |trigger_route_patch|strategy_config_patch|risk_limit_suggestion
    state: str         # draft|pending_review|approved|applied|rejected|rolled_back
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
    for name, content in (extra_files or {}).items():
        atomic_write_text(pdir / name, content)
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
            metadata={"proposal_kind": kind, "validation_plan_id": validation_plan_id},
        )
    except Exception:
        pass
    return Proposal(id=pid, kind=kind, state=initial_state, path=pdir,
                    summary=summary, ts=meta["ts"], target=target,
                    evidence_refs=list(evidence_refs or []),
                    source_event_id=source_event_id,
                    validation_plan_id=validation_plan_id,
                    metadata=dict(metadata or {}))


def _meta_file(pdir: Path) -> Path:
    return pdir / "proposal.yml"


def list_proposals(paths: WorkspacePaths) -> list[Proposal]:
    root = paths.proposals
    if not root.exists():
        return []
    out: list[Proposal] = []
    for d in sorted(root.iterdir()):
        if not d.is_dir():
            continue
        meta = yaml_io.load(_meta_file(d), default={}) or {}
        if not meta.get("id"):
            continue
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
            metadata=dict(meta.get("metadata") or {}),
        ))
    return out


def set_state(paths: WorkspacePaths, pid: str, state: str,
              *, note: str = "") -> Proposal | None:
    for p in list_proposals(paths):
        if p.id == pid:
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
                    outcome_score=1.0 if state in {"approved", "applied"} else -0.5 if state in {"rejected", "rolled_back"} else 0.0,
                    summary=f"Proposal {pid} state changed to {state}.",
                    evidence_refs=list(meta.get("evidence_refs") or []),
                    metadata={"note": note, "proposal_kind": p.kind},
                )
            except Exception:
                pass
            return Proposal(id=pid, kind=p.kind, state=state,
                            path=p.path, summary=p.summary,
                            ts=meta["state_ts"], target=p.target,
                            evidence_refs=list(meta.get("evidence_refs") or []),
                            source_event_id=meta.get("source_event_id"),
                            validation_plan_id=meta.get("validation_plan_id"),
                            metadata=dict(meta.get("metadata") or {}))
    return None


def is_protected(target: str) -> bool:
    for rule in PROTECTED_SCOPES:
        if _matches(rule, target):
            return True
    return False


def _matches(rule: str, target: str) -> bool:
    if "*" in rule:
        return fnmatch.fnmatchcase(target, rule)
    return rule == target
