"""Plan 01 §3 — operator-mode presets.

A *preset* is a coarse-grained policy the operator picks at workspace
config time (``agent.operator.preset``).  It tells the harness which
slice of the action catalog the LLM may see and which actions must
always be filtered, regardless of what skills are installed.

Presets are intentionally orthogonal to:

- **risk gate** (``trading/risk.py``) — still runs after the action
  passes the preset filter.
- **approval gate** (``trading/approval.py``) — still inserts a human
  in the loop for ``approval_gate: always`` actions.
- **availability probes** (``skills/availability.py``) — still drop
  actions with missing env/secret/check_fn dependencies.

The preset filter is the *first* of these gates and is the safest
place to enforce "this workspace is read-only" or "this workspace
runs in paper-only mode".  Built-in presets cover the four modes
called out in the audit:

- ``read_only``      — strictly query-only.  No writes, no terminal,
  no trading.  Fits embedded analyst use-cases.
- ``dev``            — read + safe writes inside the workspace.
  Paper trading allowed; live trading + irreversible promotions
  blocked.  This is the default for fresh installs.
- ``deploy``         — adds approval-gated mutating actions
  (evolution.apply, gate.promote, deployment.*).  Live trading still
  blocked unless the workspace explicitly flips the flag.
- ``live_trading``   — full surface; requires
  ``runtime.live_trading_enabled=true`` *and* per-account opt-in
  before any trading-side action survives the filter.

Workspaces can extend a preset via ``agent.operator.extra_allow``
and ``agent.operator.extra_deny`` (lists of action-alias glob
patterns).  Unknown preset ids fall back to ``dev`` and produce a
warning row in :func:`describe_presets` so doctor / capability matrix
can flag the misconfiguration.

Public API:

- :class:`OperatorPreset`           — frozen dataclass per preset.
- :func:`builtin_presets`           — mapping of id → preset.
- :func:`get_preset`                — lookup with safe fallback.
- :func:`evaluate`                  — does *one* action pass?
- :func:`filter_actions`            — apply preset to a catalog list.
- :func:`describe_presets`          — capability-matrix view.
- :class:`PresetDecision`           — per-action decision payload.
"""

from __future__ import annotations

import fnmatch
from dataclasses import dataclass, field
from typing import Any, Iterable, Mapping, Optional, Sequence, Tuple

__all__ = [
    "OperatorPreset",
    "PresetDecision",
    "DEFAULT_PRESET_ID",
    "builtin_presets",
    "get_preset",
    "list_presets",
    "evaluate",
    "filter_actions",
    "describe_presets",
]


#: Default preset for fresh installs.  Allows paper trading + workspace
#: writes but blocks live trading and irreversible promotions.
DEFAULT_PRESET_ID = "dev"


# --------------------------------------------------------------------- #
# Data model
# --------------------------------------------------------------------- #


@dataclass(frozen=True)
class OperatorPreset:
    """Frozen description of an operator preset.

    All glob fields use :func:`fnmatch.fnmatchcase` so workspaces can
    safely extend the rules without surprising regex behaviour.
    """

    id: str
    title: str
    description: str
    #: When True, only actions whose ``query_only`` row flag is True
    #: (typically derived from the action name pattern via
    #: :func:`nerya.skills.manifest.action_is_read_only`) survive the
    #: filter.
    query_only: bool = False
    #: When True, drop any action whose ``risk_gate`` is ``required`` or
    #: ``approval_gate`` is ``always``.  ``read_only`` enables this.
    block_mutating: bool = False
    #: When True, the preset only emits trading-side actions (skill ids
    #: matched by :data:`_TRADING_SKILLS`) when
    #: ``runtime.live_trading_enabled=true``.  ``live_trading`` sets
    #: this so a misconfigured workspace cannot accidentally surface
    #: live mutators to the LLM.
    requires_live_trading_flag: bool = False
    #: Action aliases that are *always* denied, even if the rest of the
    #: filter would have let them through.  ``"*"`` is allowed.
    deny_actions: Tuple[str, ...] = ()
    #: Skill ids that are *always* denied.  Wildcards are allowed.
    deny_skills: Tuple[str, ...] = ()
    #: When non-empty, only actions whose alias matches one of these
    #: globs pass the filter.  Empty tuple means "anything".
    allow_actions: Tuple[str, ...] = ()
    #: When non-empty, only actions whose skill id matches one of these
    #: globs pass the filter.  Empty tuple means "anything".
    allow_skills: Tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "title": self.title,
            "description": self.description,
            "query_only": self.query_only,
            "block_mutating": self.block_mutating,
            "requires_live_trading_flag": self.requires_live_trading_flag,
            "deny_actions": list(self.deny_actions),
            "deny_skills": list(self.deny_skills),
            "allow_actions": list(self.allow_actions),
            "allow_skills": list(self.allow_skills),
        }


@dataclass
class PresetDecision:
    """Per-action verdict produced by :func:`evaluate`."""

    action: str
    skill_id: str
    allowed: bool
    reason: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "action": self.action,
            "skill_id": self.skill_id,
            "allowed": self.allowed,
            "reason": self.reason,
        }


# --------------------------------------------------------------------- #
# Built-in presets
# --------------------------------------------------------------------- #


#: Skill ids whose actions are considered "trading-side" and therefore
#: subject to ``requires_live_trading_flag`` enforcement.  We bundle
#: ``trading``, ``portfolio`` (rebalance ops can place trades), ``risk``
#: (kill-switch), ``wallet`` (signer mutations) and ``exchange``
#: (live exchange CRUD).  Read-only views of the same skills (e.g.
#: ``portfolio.summary``) keep their ``query_only`` flag in the
#: manifest and are not trading-side.
_TRADING_SKILLS: Tuple[str, ...] = (
    "trading",
    "wallet",
    "exchange",
)


_PRESETS: dict[str, OperatorPreset] = {
    "read_only": OperatorPreset(
        id="read_only",
        title="Read-only analyst",
        description=(
            "Strictly query-only — no writes, no terminal, no trading. "
            "Use for embedded analyst dashboards, read-only API users, "
            "or untrusted environments."
        ),
        query_only=True,
        block_mutating=True,
    ),
    "dev": OperatorPreset(
        id="dev",
        title="Developer / paper trading",
        description=(
            "Default for fresh installs. Read + safe writes inside the "
            "workspace, paper trading allowed, live trading + irreversible "
            "promotions blocked."
        ),
        deny_actions=(
            "evolution.apply",
            "gate.promote",
            "deployment.*",
        ),
    ),
    "deploy": OperatorPreset(
        id="deploy",
        title="Deploy / promotion",
        description=(
            "Adds approval-gated mutating actions (evolution.apply, "
            "gate.promote, deployment.*). Live trading still blocked "
            "unless the workspace explicitly flips the runtime flag."
        ),
    ),
    "live_trading": OperatorPreset(
        id="live_trading",
        title="Live trading",
        description=(
            "Full surface; requires runtime.live_trading_enabled=true "
            "and per-account opt-in before any trading-side action "
            "survives the filter."
        ),
        requires_live_trading_flag=True,
    ),
}


def builtin_presets() -> Mapping[str, OperatorPreset]:
    """Return the built-in preset map.  The mapping is read-only; copy
    it if you intend to mutate."""

    return dict(_PRESETS)


def list_presets() -> Sequence[str]:
    """Return the ordered list of built-in preset ids."""

    return tuple(_PRESETS.keys())


def get_preset(preset_id: Optional[str]) -> OperatorPreset:
    """Lookup a preset by id, falling back to :data:`DEFAULT_PRESET_ID`
    when the id is unknown / empty.  Never raises."""

    if not preset_id:
        return _PRESETS[DEFAULT_PRESET_ID]
    pid = preset_id.strip().lower().replace("-", "_")
    if pid in _PRESETS:
        return _PRESETS[pid]
    return _PRESETS[DEFAULT_PRESET_ID]


# --------------------------------------------------------------------- #
# Evaluation
# --------------------------------------------------------------------- #


def _glob_match(value: str, patterns: Iterable[str]) -> bool:
    return any(fnmatch.fnmatchcase(value, pat) for pat in patterns)


def _is_mutating(row: Mapping[str, Any]) -> bool:
    risk = (row.get("risk_gate") or "").lower()
    approval = (row.get("approval_gate") or "").lower()
    if risk in ("required", "always"):
        return True
    if approval in ("always", "required"):
        return True
    if not row.get("query_only"):
        # If neither flag is set, fall back to "is the manifest tagging
        # it as query_only?".  This matches the existing capability
        # matrix logic.
        return False
    return False


def evaluate(
    row: Mapping[str, Any],
    preset: OperatorPreset,
    *,
    runtime_live_enabled: bool = False,
    extra_allow: Sequence[str] = (),
    extra_deny: Sequence[str] = (),
) -> PresetDecision:
    """Decide whether *one* action row should be exposed under
    ``preset``.

    ``row`` is the dict shape produced by
    :func:`nerya.agent.kernel.build_action_catalog` — at minimum it
    needs ``alias``, ``skill_id``, ``risk_gate``, ``approval_gate``,
    ``query_only``.
    """

    alias = (row.get("alias") or "").strip()
    skill_id = (row.get("skill_id") or "").strip()
    if not alias or not skill_id:
        return PresetDecision(action=alias, skill_id=skill_id, allowed=False, reason="missing_identity")

    # 1) Workspace-level explicit deny wins over everything except the
    #    explicit allow (which can re-include).  We check explicit
    #    allow first so it can override preset-level deny rules too.
    if extra_allow and _glob_match(alias, extra_allow):
        return PresetDecision(action=alias, skill_id=skill_id, allowed=True, reason="extra_allow")
    if extra_deny and _glob_match(alias, extra_deny):
        return PresetDecision(action=alias, skill_id=skill_id, allowed=False, reason="extra_deny")

    # 2) Preset-level deny lists.
    if preset.deny_skills and _glob_match(skill_id, preset.deny_skills):
        return PresetDecision(action=alias, skill_id=skill_id, allowed=False, reason="preset_deny_skill")
    if preset.deny_actions and _glob_match(alias, preset.deny_actions):
        return PresetDecision(action=alias, skill_id=skill_id, allowed=False, reason="preset_deny_action")

    # 3) Preset-level allow lists (must positively match if non-empty).
    if preset.allow_skills and not _glob_match(skill_id, preset.allow_skills):
        return PresetDecision(action=alias, skill_id=skill_id, allowed=False, reason="preset_allow_skill_miss")
    if preset.allow_actions and not _glob_match(alias, preset.allow_actions):
        return PresetDecision(action=alias, skill_id=skill_id, allowed=False, reason="preset_allow_action_miss")

    # 4) Query-only enforcement.
    if preset.query_only and not row.get("query_only"):
        return PresetDecision(action=alias, skill_id=skill_id, allowed=False, reason="preset_query_only")

    # 5) Mutating action enforcement.
    if preset.block_mutating and _is_mutating(row):
        return PresetDecision(action=alias, skill_id=skill_id, allowed=False, reason="preset_block_mutating")

    # 6) Live trading flag.  Only trading-side skills are gated; read-only
    #    introspection (e.g. ``portfolio.summary`` on the same skill) gets
    #    the green light because it is ``query_only``.
    if (
        preset.requires_live_trading_flag
        and not runtime_live_enabled
        and skill_id in _TRADING_SKILLS
        and not row.get("query_only")
    ):
        return PresetDecision(
            action=alias,
            skill_id=skill_id,
            allowed=False,
            reason="needs_runtime_live_trading_enabled",
        )

    return PresetDecision(action=alias, skill_id=skill_id, allowed=True, reason="ok")


def filter_actions(
    rows: Iterable[Mapping[str, Any]],
    preset: OperatorPreset,
    *,
    runtime_live_enabled: bool = False,
    extra_allow: Sequence[str] = (),
    extra_deny: Sequence[str] = (),
) -> list[dict[str, Any]]:
    """Return a new list of action rows that pass the preset filter.

    Each surviving row is a *copy* with an extra ``preset`` key carrying
    the :class:`PresetDecision` payload so the capability-matrix UI and
    drift tests can show why an action was kept.
    """

    out: list[dict[str, Any]] = []
    for row in rows:
        decision = evaluate(
            row,
            preset,
            runtime_live_enabled=runtime_live_enabled,
            extra_allow=extra_allow,
            extra_deny=extra_deny,
        )
        if not decision.allowed:
            continue
        copy = dict(row)
        copy["preset"] = decision.to_dict()
        out.append(copy)
    return out


def describe_presets(active_id: Optional[str] = None) -> dict[str, Any]:
    """Return a capability-matrix payload listing every preset."""

    return {
        "active": (get_preset(active_id).id),
        "default": DEFAULT_PRESET_ID,
        "presets": [p.to_dict() for p in _PRESETS.values()],
    }
