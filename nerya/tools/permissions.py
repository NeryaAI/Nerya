"""PermissionEngine — allow / ask / deny for native tool calls.

Replaces the mix of ``ACP approve``, ``manifest approval_gate``, and
``risk_gate`` that lives across :mod:`nerya.skills.permissions`,
:mod:`nerya.acp.protocol`, and ad-hoc dashboard prompts with a single
in-process engine.

Inputs:
* ``descriptor`` — :class:`ToolDescriptor` (carries default risk +
  permission scope + auto_approve flag).
* ``payload``   — concrete arguments. The descriptor's risk_classifier
  may upgrade the call's risk level.
* ``context``   — :class:`PermissionContext` (mode, session-allowed,
  permanent rules, deny rules, caller).

Outputs:
* :class:`PermissionDecision` — ``ALLOW`` | ``ASK`` | ``DENY``.

Decisions are written to the transcript by the executor (Phase 5);
the engine itself is pure-data and easily unit-testable.

References:
* docs/agent-harness-comparison-and-refactor-todo.md Phase 4
* docs/agent-intelligence-gap-and-cursor-refactor-plan.md §3.4 (Plan/
  approval boundary)
"""

from __future__ import annotations

import enum
import re
from dataclasses import dataclass, field
from typing import Any, Iterable, Optional

from .types import PermissionScope, RiskLevel, ToolDescriptor


class PermissionMode(str, enum.Enum):
    """Top-level operator policy.

    * ``DEFAULT``    — risk-based: READ/WRITE auto-allowed, EXEC asks,
      DANGEROUS blocks unless explicitly approved.
    * ``PLAN``       — approval-planning mode. Mutating tools are blocked,
      but auto-approved research/delegation helpers may still run.
    * ``AUTO``       — unattended for low/medium-risk work (used by eval /
      cron); DANGEROUS operations still ask and the engine still enforces
      deny rules and sandboxed scopes.
    * ``YOLO``       — unattended for low/medium-risk work, but still
      escalates dangerous operations. Reserved for explicitly-acknowledged
      power users.
    """

    DEFAULT = "default"
    PLAN = "plan"
    AUTO = "auto"
    YOLO = "yolo"


class PermissionDecisionKind(str, enum.Enum):
    ALLOW = "allow"
    ASK = "ask"
    DENY = "deny"


@dataclass
class PermissionRule:
    """One allow/deny rule.

    Rules can match by ``tool`` (exact or wildcard), ``namespace`` (e.g.
    ``mcp``), or by a compiled ``payload_regex`` against the rendered
    payload (used for ``run_shell command="git push *"``).
    """

    tool: Optional[str] = None
    namespace: Optional[str] = None
    payload_regex: Optional[str] = None
    decision: PermissionDecisionKind = PermissionDecisionKind.ALLOW
    reason: str = ""

    def matches(self, descriptor: ToolDescriptor, payload: dict[str, Any]) -> bool:
        if self.tool is not None and self.tool != "*":
            if not _glob_match(self.tool, descriptor.name):
                return False
        if self.namespace is not None and descriptor.namespace != self.namespace:
            return False
        if self.payload_regex:
            try:
                rendered = _payload_text(payload)
            except Exception:
                rendered = ""
            if not re.search(self.payload_regex, rendered):
                return False
        return True


def _glob_match(pattern: str, name: str) -> bool:
    """Tiny glob: ``*`` matches anything, ``foo:*`` matches namespace,
    ``run_shell:git push *`` matches command prefix."""

    if pattern == "*":
        return True
    if "*" not in pattern:
        return pattern == name
    rx = re.escape(pattern).replace(r"\*", ".*")
    return re.fullmatch(rx, name) is not None


def _payload_text(payload: dict[str, Any]) -> str:
    import json as _json

    try:
        return _json.dumps(payload, ensure_ascii=False, default=str)
    except Exception:
        return str(payload)


# ---------------------------------------------------------------------------
# Request / Decision dataclasses
# ---------------------------------------------------------------------------


@dataclass
class PermissionRequest:
    """Carries everything the engine needs to decide a single call."""

    descriptor: ToolDescriptor
    payload: dict[str, Any] = field(default_factory=dict)
    caller: str = "agent:native"
    turn_id: str = ""
    iteration: int = 0


@dataclass
class PermissionDecision:
    """Engine output. The executor writes this to the transcript."""

    kind: PermissionDecisionKind
    reason: str = ""
    rule: Optional[PermissionRule] = None
    risk: RiskLevel = RiskLevel.READ
    scope: PermissionScope = PermissionScope.NONE
    requires_approval: bool = False
    approval_reason: str = ""
    approval_id: str = ""

    def is_allow(self) -> bool:
        return self.kind is PermissionDecisionKind.ALLOW

    def is_ask(self) -> bool:
        return self.kind is PermissionDecisionKind.ASK

    def is_deny(self) -> bool:
        return self.kind is PermissionDecisionKind.DENY

    def asdict(self) -> dict[str, Any]:
        return {
            "kind": self.kind.value,
            "reason": self.reason,
            "rule": (
                {
                    "tool": self.rule.tool,
                    "namespace": self.rule.namespace,
                    "payload_regex": self.rule.payload_regex,
                    "decision": self.rule.decision.value,
                    "reason": self.rule.reason,
                }
                if self.rule
                else None
            ),
            "risk": self.risk.value,
            "scope": self.scope.value,
            "requires_approval": self.requires_approval,
            "approval_reason": self.approval_reason,
            "approval_id": self.approval_id,
        }


# ---------------------------------------------------------------------------
# Engine
# ---------------------------------------------------------------------------


@dataclass
class PermissionContext:
    """Session/operator state the engine reads from.

    The agent kernel constructs this once per turn; the executor passes
    it into :meth:`PermissionEngine.evaluate` for every tool call.
    """

    mode: PermissionMode = PermissionMode.DEFAULT
    permanent_rules: list[PermissionRule] = field(default_factory=list)
    session_rules: list[PermissionRule] = field(default_factory=list)
    deny_rules: list[PermissionRule] = field(default_factory=list)
    approved_calls: set[str] = field(default_factory=set)
    """``{tool_use_id}`` set of pre-approved calls (UI confirmed)."""

    rejected_calls: set[str] = field(default_factory=set)
    """``{tool_use_id}`` set of explicitly rejected calls."""


class PermissionEngine:
    """Stateless engine evaluating a :class:`PermissionRequest`.

    The state lives in :class:`PermissionContext`; the engine itself
    is reusable across turns / sessions.
    """

    def evaluate(
        self,
        request: PermissionRequest,
        context: PermissionContext,
    ) -> PermissionDecision:
        descriptor = request.descriptor
        payload = request.payload

        risk = descriptor.per_call_risk(payload)
        scope = descriptor.permission_scope

        if context.mode is PermissionMode.YOLO:
            for rule in context.deny_rules:
                if rule.matches(descriptor, payload):
                    return PermissionDecision(
                        kind=PermissionDecisionKind.DENY,
                        reason=rule.reason or "denied by deny rule",
                        rule=rule,
                        risk=risk,
                        scope=scope,
                    )
            if risk is RiskLevel.DANGEROUS:
                return PermissionDecision(
                    kind=PermissionDecisionKind.ASK,
                    reason="yolo mode escalates DANGEROUS to ask",
                    risk=risk,
                    scope=scope,
                    requires_approval=True,
                    approval_reason="dangerous tool requires explicit approval",
                )
            return PermissionDecision(
                kind=PermissionDecisionKind.ALLOW,
                reason="yolo mode",
                risk=risk,
                scope=scope,
            )

        for rule in context.deny_rules:
            if rule.matches(descriptor, payload):
                return PermissionDecision(
                    kind=PermissionDecisionKind.DENY,
                    reason=rule.reason or "denied by deny rule",
                    rule=rule,
                    risk=risk,
                    scope=scope,
                )

        if context.mode is PermissionMode.PLAN:
            if (
                risk is RiskLevel.DANGEROUS
                or (
                    (risk is RiskLevel.WRITE or descriptor.mutates_paths)
                    and not descriptor.auto_approve
                )
                or (risk is RiskLevel.EXEC and not descriptor.auto_approve)
            ):
                return PermissionDecision(
                    kind=PermissionDecisionKind.DENY,
                    reason="plan mode forbids mutating tools",
                    risk=risk,
                    scope=scope,
                )

        if descriptor.auto_approve:
            return PermissionDecision(
                kind=PermissionDecisionKind.ALLOW,
                reason="auto_approve descriptor",
                risk=risk,
                scope=scope,
            )

        for rules in (context.session_rules, context.permanent_rules):
            for rule in rules:
                if rule.matches(descriptor, payload):
                    if rule.decision is PermissionDecisionKind.ALLOW:
                        return PermissionDecision(
                            kind=PermissionDecisionKind.ALLOW,
                            reason=rule.reason or "matched allow rule",
                            rule=rule,
                            risk=risk,
                            scope=scope,
                        )
                    if rule.decision is PermissionDecisionKind.DENY:
                        return PermissionDecision(
                            kind=PermissionDecisionKind.DENY,
                            reason=rule.reason or "matched deny rule",
                            rule=rule,
                            risk=risk,
                            scope=scope,
                        )

        if context.mode is PermissionMode.AUTO:
            if risk is RiskLevel.DANGEROUS:
                return PermissionDecision(
                    kind=PermissionDecisionKind.ASK,
                    reason="auto mode escalates DANGEROUS to ask",
                    risk=risk,
                    scope=scope,
                    requires_approval=True,
                    approval_reason="dangerous tool requires explicit approval",
                )
            return PermissionDecision(
                kind=PermissionDecisionKind.ALLOW,
                reason="auto mode",
                risk=risk,
                scope=scope,
            )

        if risk is RiskLevel.READ:
            return PermissionDecision(
                kind=PermissionDecisionKind.ALLOW,
                reason="read-only tool",
                risk=risk,
                scope=scope,
            )
        if risk is RiskLevel.WRITE:
            if scope in (PermissionScope.WORKSPACE, PermissionScope.SANDBOX):
                return PermissionDecision(
                    kind=PermissionDecisionKind.ALLOW,
                    reason="workspace write within scope",
                    risk=risk,
                    scope=scope,
                )
            return PermissionDecision(
                kind=PermissionDecisionKind.ASK,
                reason="write outside workspace requires approval",
                risk=risk,
                scope=scope,
                requires_approval=True,
                approval_reason=f"write to {scope.value}",
            )
        if risk is RiskLevel.EXEC:
            return PermissionDecision(
                kind=PermissionDecisionKind.ASK,
                reason="shell / external execution",
                risk=risk,
                scope=scope,
                requires_approval=True,
                approval_reason="shell or network execution",
            )
        return PermissionDecision(
            kind=PermissionDecisionKind.ASK,
            reason="dangerous tool",
            risk=risk,
            scope=scope,
            requires_approval=True,
            approval_reason="dangerous classification",
        )


__all__ = [
    "PermissionContext",
    "PermissionDecision",
    "PermissionDecisionKind",
    "PermissionEngine",
    "PermissionMode",
    "PermissionRequest",
    "PermissionRule",
]
