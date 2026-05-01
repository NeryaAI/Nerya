"""Self-configuration patches — the agent proposes mutations to its own
runtime configuration (``nerya.yml`` / ``agents.yml`` / ``workspace.yml``)
using the same proposal -> approval -> promotion pipeline that governs
strategies.

Nothing here auto-applies. Protected scopes (see
:data:`nerya.evolution.patch_proposal.PROTECTED_SCOPES`) are rejected
at proposal-creation time so the agent cannot even stage a patch
against risk limits, the kill switch, or credentials.

This module is intentionally small and declarative. It does NOT try
to mutate config in memory — the only legal effect is to write an
evolution proposal for an operator to approve.
"""

from __future__ import annotations

from typing import Any

from ..core import yaml_io
from ..core.errors import ProtectedScopeViolation
from ..core.paths import WorkspacePaths
from .patch_proposal import (
    PROTECTED_SCOPES,
    Proposal,
    create_proposal,
    is_protected,
)

_ALLOWED_TARGETS = frozenset({
    "nerya.yml",
    "agents.yml",
    "workspace.yml",
    "triggers/routes.yml",   # non-protected sections only (handled by is_protected)
    "policies/planner.yml",
    "policies/tier_policy.yml",
})


def _require_non_protected_keys(target: str, config_after: dict[str, Any]) -> None:
    """Reject a proposed patch that would touch a protected *sub*-key.

    Protected scopes can encode sub-keys with ``:`` notation (e.g.
    ``nerya.yml:runtime.live_trading_enabled``). We can't tell exactly
    which keys the operator will end up writing from just the after-file
    alone, but we can reject the obvious case where the proposed yaml
    explicitly includes a protected key path.
    """
    flat = _flatten(config_after)
    for key in flat:
        protected_key = f"{target}:{key}"
        if is_protected(protected_key):
            raise ProtectedScopeViolation(
                f"proposed patch touches protected sub-key {protected_key!r}; "
                f"protected set={sorted(PROTECTED_SCOPES)}"
            )


def _flatten(d: Any, prefix: str = "") -> list[str]:
    out: list[str] = []
    if isinstance(d, dict):
        for k, v in d.items():
            sub = f"{prefix}.{k}" if prefix else str(k)
            out.append(sub)
            out.extend(_flatten(v, sub))
    return out


def propose_core_config_patch(
    paths: WorkspacePaths,
    *,
    target: str,
    summary: str,
    config_after: dict[str, Any],
    rationale: str = "",
) -> Proposal:
    """Propose a mutation to a runtime config file.

    :param target: posix-relative path of the file inside the workspace
        (e.g. ``"nerya.yml"``).
    :param config_after: the full YAML content the operator would end up
        with after applying the patch.
    :raises ProtectedScopeViolation: if ``target`` itself is protected
        or the proposed body touches a protected sub-key.
    :raises ValueError: if ``target`` isn't in :data:`_ALLOWED_TARGETS`.
    """
    if target not in _ALLOWED_TARGETS:
        raise ValueError(
            f"target {target!r} is not allowed for self-config patches; "
            f"allowed={sorted(_ALLOWED_TARGETS)}"
        )
    if is_protected(target):
        raise ProtectedScopeViolation(
            f"target {target!r} is a protected scope and cannot be patched "
            f"through the self-config surface"
        )
    _require_non_protected_keys(target, config_after)

    body = yaml_io.dumps(config_after)
    rationale_md = rationale or f"# Core config patch\n\nTarget: `{target}`\n\n{summary}\n"
    return create_proposal(
        paths,
        kind="core_config_patch",
        summary=summary,
        rationale=rationale_md,
        extra_files={
            f"after/{target}": body,
            "target.yml": yaml_io.dumps({"target": target}),
        },
        target=target,
    )


__all__ = ["propose_core_config_patch"]
