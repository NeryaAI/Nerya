"""Account roster proposals.

The generic :mod:`nerya.evolution.patch_proposal` machinery refuses to
target ``accounts/accounts.yml`` outright — that file is in
``PROTECTED_SCOPES`` so that auto-generated patches can never sneak a
new account row past an operator. The dashboard, on the other hand,
*does* let an operator add or update accounts; what it needs is a
gating step where the operator stages the change, the diff shows up
on a review surface, and a second click actually writes the YAML.

This module provides exactly that: a side-channel proposal store
just for the account roster. The shape mirrors evolution proposals
(one directory per proposal id, ``proposal.yml`` for metadata, plus
the staged payload), so journals and audit tools see a familiar
structure, but the protected-scope rule for the generic patch system
stays intact.

Lifecycle:

* :func:`propose_upsert` — capture the operator's intent in
  ``proposals/account/<pid>/`` with state ``pending_review``.
* :func:`approve_proposal` — load the staged payload, call
  :func:`nerya.trading.accounts.upsert_account`, mark ``applied``.
* :func:`reject_proposal` — operator drops the proposal without
  touching ``accounts.yml``.
* :func:`list_proposals` / :func:`get_proposal` — read APIs for the
  dashboard.

Protected scopes are still respected: the YAML is only written by
:func:`upsert_account` itself, which is called from
:func:`approve_proposal` and runs the same vault-only credential
guard as the direct upsert path.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..core import jsonl, yaml_io
from ..core.atomic_write import atomic_write_text
from ..core.errors import TradingError
from ..core.ids import proposal_id
from ..core.paths import WorkspacePaths
from ..core.time import now_iso
from . import accounts as accounts_mod


PROPOSAL_KIND = "account_roster_patch"
PROPOSAL_STATES = ("pending_review", "approved", "rejected", "applied")


def _proposals_root(paths: WorkspacePaths) -> Path:
    """Where account proposals live.

    Sits under ``proposals/account/`` so it shares the existing
    ``proposals/`` ancestor with evolution proposals but never clashes
    with them on listing.
    """

    return paths.proposals / "account"


def _meta_path(pdir: Path) -> Path:
    return pdir / "proposal.yml"


def _payload_path(pdir: Path) -> Path:
    return pdir / "account.yml"


@dataclass
class AccountProposal:
    id: str
    state: str
    operator: str
    summary: str
    target_id: str
    operation: str  # "create" | "update"
    ts: str
    payload: dict[str, Any]
    diff: dict[str, Any]
    state_ts: str = ""
    state_note: str = ""
    applied_ts: str = ""

    def asdict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "kind": PROPOSAL_KIND,
            "state": self.state,
            "operator": self.operator,
            "summary": self.summary,
            "target_id": self.target_id,
            "operation": self.operation,
            "ts": self.ts,
            "payload": self.payload,
            "diff": self.diff,
            "state_ts": self.state_ts,
            "state_note": self.state_note,
            "applied_ts": self.applied_ts,
        }


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _diff_account(
    paths: WorkspacePaths, payload: dict[str, Any]
) -> tuple[str, dict[str, Any]]:
    """Return ``(operation, diff)`` for a proposed account row.

    The diff format is intentionally tiny: ``{field: {before, after}}``
    so the dashboard can render it without parsing YAML on the
    frontend. Credential references are normalised to the boolean
    ``"<vault ref>"`` placeholder so the diff never leaks anything
    plaintext (the upsert path already rejects plaintext, but defence
    in depth).
    """

    aid = str(payload.get("id") or "").strip()
    operation = "create"
    before: dict[str, Any] = {}
    try:
        existing = accounts_mod.get_account_profile(paths, aid)
        operation = "update"
        before = existing.asdict()
    except TradingError:
        before = {}

    after = dict(payload)
    if "credentials" in after and isinstance(after["credentials"], dict):
        # Make sure the diff only ever shows the keys, never values
        # (the values are vault refs anyway, but this keeps ASCII diffs
        # narrow and printable).
        after["credentials"] = {
            k: ("<vault ref>" if isinstance(v, str) else "<invalid>")
            for k, v in after["credentials"].items()
        }
    if "credentials" in before:
        before["credentials"] = {
            k: ("<vault ref>" if isinstance(v, str) else "<invalid>")
            for k, v in before["credentials"].items()
        }

    fields: dict[str, dict[str, Any]] = {}
    keys = set(before.keys()) | set(after.keys())
    for key in sorted(keys):
        bv = before.get(key)
        av = after.get(key)
        if bv != av:
            fields[key] = {"before": bv, "after": av}
    return operation, fields


# ---------------------------------------------------------------------------
# Write APIs
# ---------------------------------------------------------------------------


def propose_upsert(
    paths: WorkspacePaths,
    payload: dict[str, Any],
    *,
    operator: str = "dashboard",
    summary: str | None = None,
) -> AccountProposal:
    """Stage an account upsert for operator review.

    The payload is validated up front (``upsert_account`` would refuse
    plaintext credentials etc; we run the same shape check here so the
    proposal never lands in a state where ``approve_proposal`` will
    fail). On success, returns an :class:`AccountProposal` with state
    ``pending_review``.
    """

    if not isinstance(payload, dict):
        raise TradingError("account payload must be a dict")
    aid = str(payload.get("id") or "").strip()
    if not aid:
        raise TradingError("account.id is required")
    # Run the credential normaliser eagerly so an invalid plaintext
    # secret can never reach the proposal store at all.
    accounts_mod._coerce_credentials_row(payload.get("credentials") or {})

    operation, diff = _diff_account(paths, payload)
    pid = proposal_id()
    pdir = _proposals_root(paths) / pid
    pdir.mkdir(parents=True, exist_ok=True)
    ts = now_iso()
    meta = {
        "id": pid,
        "kind": PROPOSAL_KIND,
        "state": "pending_review",
        "operator": operator,
        "summary": summary or f"{operation} account {aid}",
        "target_id": aid,
        "operation": operation,
        "ts": ts,
    }
    atomic_write_text(_meta_path(pdir), yaml_io.dumps(meta))
    atomic_write_text(_payload_path(pdir), yaml_io.dumps(payload))
    atomic_write_text(pdir / "diff.json", json.dumps(diff, indent=2, sort_keys=True))
    jsonl.append(
        paths.journal("evolution"),
        {
            "kind": "account_proposal.created",
            "proposal_id": pid,
            "operator": operator,
            "target_id": aid,
            "operation": operation,
            "ts": ts,
        },
    )
    return AccountProposal(
        id=pid,
        state="pending_review",
        operator=operator,
        summary=meta["summary"],
        target_id=aid,
        operation=operation,
        ts=ts,
        payload=payload,
        diff=diff,
    )


def _load(pdir: Path) -> AccountProposal | None:
    meta = yaml_io.load(_meta_path(pdir), default={}) or {}
    if not meta.get("id"):
        return None
    payload = yaml_io.load(_payload_path(pdir), default={}) or {}
    diff_path = pdir / "diff.json"
    diff: dict[str, Any] = {}
    if diff_path.exists():
        try:
            diff = json.loads(diff_path.read_text(encoding="utf-8"))
        except Exception:
            diff = {}
    return AccountProposal(
        id=str(meta["id"]),
        state=str(meta.get("state", "pending_review")),
        operator=str(meta.get("operator", "")),
        summary=str(meta.get("summary", "")),
        target_id=str(meta.get("target_id", "")),
        operation=str(meta.get("operation", "update")),
        ts=str(meta.get("ts", "")),
        payload=payload if isinstance(payload, dict) else {},
        diff=diff if isinstance(diff, dict) else {},
        state_ts=str(meta.get("state_ts", "")),
        state_note=str(meta.get("state_note", "")),
        applied_ts=str(meta.get("applied_ts", "")),
    )


def list_proposals(
    paths: WorkspacePaths, *, state: str | None = None
) -> list[AccountProposal]:
    root = _proposals_root(paths)
    if not root.exists():
        return []
    out: list[AccountProposal] = []
    for d in sorted(root.iterdir()):
        if not d.is_dir():
            continue
        proposal = _load(d)
        if proposal is None:
            continue
        if state and proposal.state != state:
            continue
        out.append(proposal)
    return out


def get_proposal(paths: WorkspacePaths, pid: str) -> AccountProposal:
    pdir = _proposals_root(paths) / pid
    proposal = _load(pdir) if pdir.exists() else None
    if proposal is None:
        raise TradingError(f"unknown account proposal: {pid}")
    return proposal


def _set_state(
    paths: WorkspacePaths,
    proposal: AccountProposal,
    new_state: str,
    *,
    note: str = "",
    applied: bool = False,
) -> AccountProposal:
    if new_state not in PROPOSAL_STATES:
        raise TradingError(f"invalid proposal state: {new_state}")
    pdir = _proposals_root(paths) / proposal.id
    meta = yaml_io.load(_meta_path(pdir), default={}) or {}
    meta["state"] = new_state
    meta["state_ts"] = now_iso()
    if note:
        meta["state_note"] = note
    if applied:
        meta["applied_ts"] = meta["state_ts"]
    atomic_write_text(_meta_path(pdir), yaml_io.dumps(meta))
    jsonl.append(
        paths.journal("evolution"),
        {
            "kind": "account_proposal.state",
            "proposal_id": proposal.id,
            "state": new_state,
            "note": note,
        },
    )
    return _load(pdir) or proposal


def approve_proposal(
    paths: WorkspacePaths,
    pid: str,
    *,
    operator: str = "dashboard",
    note: str = "",
) -> tuple[AccountProposal, accounts_mod.AccountProfile]:
    """Apply a pending account proposal.

    Calls :func:`nerya.trading.accounts.upsert_account` with the staged
    payload, then marks the proposal ``applied``. ``upsert_account``
    runs the same vault-only credential guard so anything malformed is
    still refused; on failure the proposal stays in ``pending_review``
    so the operator can correct it.
    """

    proposal = get_proposal(paths, pid)
    if proposal.state in ("applied", "rejected"):
        raise TradingError(f"proposal {pid} already in terminal state: {proposal.state}")
    profile = accounts_mod.upsert_account(
        paths,
        proposal.payload,
        operator=operator,
    )
    updated = _set_state(paths, proposal, "applied", note=note, applied=True)
    return updated, profile


def reject_proposal(
    paths: WorkspacePaths,
    pid: str,
    *,
    operator: str = "dashboard",
    note: str = "",
) -> AccountProposal:
    proposal = get_proposal(paths, pid)
    if proposal.state == "applied":
        raise TradingError(f"proposal {pid} already applied; cannot reject")
    return _set_state(paths, proposal, "rejected", note=note or operator)


__all__ = [
    "AccountProposal",
    "PROPOSAL_KIND",
    "PROPOSAL_STATES",
    "propose_upsert",
    "approve_proposal",
    "reject_proposal",
    "list_proposals",
    "get_proposal",
]
