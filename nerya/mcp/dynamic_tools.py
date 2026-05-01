"""Plan 25 §3 — manifest-driven MCP tool generation.

Hermes' MCP bridge composes its tool surface from the live tool registry
plus plugin contributions, so every newly enabled skill / toolset shows
up in the MCP server without code edits. Nerya's legacy ``NeryaTools``
class hand-codes 17 wrapper methods that mirror specific
``client.skill.call(...)`` invocations, which means:

* a new built-in skill is invisible over MCP until somebody adds another
  hand-written wrapper to ``nerya/mcp/tools.py``;
* the legacy surface deliberately drops mutating actions, so an operator
  who wants to expose, say, ``messages.send`` over MCP cannot — without
  patching the file — even though they may already have a manifest that
  marks the action with the right risk/approval gates;
* the wrapper schemas are not in sync with ``ActionSpec.input_schema``,
  so two sources of truth exist for the same action.

This module replaces that with a generator that walks the live
:class:`SkillRegistry`, applies a :class:`MCPPolicy`, and produces a
list of :class:`MCPTool` descriptors. Each descriptor:

* uses ``ActionSpec.input_schema`` verbatim as the JSON schema the MCP
  client sees, so manifests are the single source of truth;
* preserves the action's ``risk_gate`` / ``approval_gate`` / permissions
  metadata in the descriptor (used by the server to refuse / annotate
  mutating tools);
* dispatches through the same ``client.skill.call(...)`` chokepoint as
  every other caller, so journal / approval / availability / overflow
  spool all keep working transparently;
* respects an optional :class:`OperatorPreset` (``read_only`` / ``dev``
  / ``deploy`` / ``live_trading``) so MCP exposure follows the same
  workspace operator policy as the rest of the runtime.

The legacy ``NeryaTools.registry()`` list keeps working — the dynamic
tools live alongside it under a stable ``nerya_<skill_id>__<action>``
naming convention so they cannot collide with the legacy ``nerya_*``
tools.
"""

from __future__ import annotations

import json
import re
import traceback
from dataclasses import dataclass, field
from typing import Any, Callable, Sequence

from ..agent.operator_presets import (
    OperatorPreset,
    evaluate as evaluate_preset,
    get_preset,
)
from ..sdk.internal_client import InternalClient
from ..skills.manifest import ActionSpec, SkillManifest, action_is_read_only


_NAME_SAFE = re.compile(r"[^A-Za-z0-9_]+")


def _safe_name(value: str) -> str:
    return _NAME_SAFE.sub("_", value).strip("_")


def mcp_tool_name(skill_id: str, action_name: str) -> str:
    """Stable MCP tool name for ``(skill_id, action_name)``.

    Uses double-underscore between skill and action to make the boundary
    visible (and to avoid collision with the legacy ``nerya_<verb>`` flat
    namespace). Both halves are sanitized so we never emit characters
    outside ``[A-Za-z0-9_]``.
    """

    return f"nerya_{_safe_name(skill_id)}__{_safe_name(action_name)}"


def _action_input_schema(spec: ActionSpec) -> dict[str, Any]:
    """Return a deep-copied JSON-schema-shaped dict for the action."""
    schema = dict(spec.input_schema or {})
    if not schema:
        return {"type": "object", "properties": {}, "additionalProperties": True}
    if "type" not in schema:
        schema["type"] = "object"
    return schema


@dataclass(frozen=True)
class MCPPolicy:
    """Policy controls for the dynamic MCP surface.

    * ``preset``: optional operator preset name (``read_only`` / ``dev``
      / ``deploy`` / ``live_trading``). When set, the resolver applies
      :func:`nerya.agent.operator_presets.evaluate` so MCP exposure
      mirrors the workspace operator policy.
    * ``allow_mutating``: when ``False`` (default), every action whose
      *name pattern* does not look like a pure-read query
      (:func:`nerya.skills.manifest.action_is_read_only`) is dropped
      from the surface. ``True`` lets risk-gated mutating actions
      through (they still pass through the runtime's approval / risk
      checks at dispatch time).
    * ``allow_skills``: optional explicit allow-list of skill IDs. When
      present, only listed skills are eligible regardless of preset.
    * ``deny_skills``: explicit skill IDs to drop after preset filtering.
    * ``allow_actions``: optional ``"skill.action"`` allow-list applied
      after ``allow_skills``.
    * ``deny_actions``: ``"skill.action"`` deny-list applied last.
    * ``include_unimplemented``: when ``False`` (default), actions
      flagged ``status="proposal_only_unimplemented"`` are skipped.
    * ``live_trading_enabled``: forwarded to the operator preset
      evaluator so ``live_trading`` mutations only appear when the
      runtime gate is open.
    """

    preset: str | None = None
    allow_mutating: bool = False
    allow_skills: tuple[str, ...] | None = None
    deny_skills: tuple[str, ...] = ()
    allow_actions: tuple[str, ...] | None = None
    deny_actions: tuple[str, ...] = ()
    include_unimplemented: bool = False
    live_trading_enabled: bool = False

    def with_overrides(
        self,
        *,
        extra_deny_skills: Sequence[str] = (),
        extra_deny_actions: Sequence[str] = (),
    ) -> "MCPPolicy":
        return MCPPolicy(
            preset=self.preset,
            allow_mutating=self.allow_mutating,
            allow_skills=self.allow_skills,
            deny_skills=tuple(self.deny_skills) + tuple(extra_deny_skills),
            allow_actions=self.allow_actions,
            deny_actions=tuple(self.deny_actions) + tuple(extra_deny_actions),
            include_unimplemented=self.include_unimplemented,
            live_trading_enabled=self.live_trading_enabled,
        )


def policy_from_config(config: Any) -> MCPPolicy:
    """Build an :class:`MCPPolicy` from a Nerya :class:`Config`.

    Reads the ``mcp.dynamic_tools`` block and falls back to safe
    defaults (read-only style: only actions whose name pattern looks
    like a query are exposed, no mutating skills). When
    ``mcp.dynamic_tools.preset`` is not set, we inherit
    ``agent.operator.preset`` so the MCP surface follows the same
    policy as the workspace operator.
    """

    data = (getattr(config, "data", None) or {}) or {}
    runtime = (data.get("runtime") or {}) if isinstance(data, dict) else {}
    agent = (data.get("agent") or {}) if isinstance(data, dict) else {}
    operator_cfg = (agent.get("operator") or {}) if isinstance(agent, dict) else {}
    mcp_cfg = (data.get("mcp") or {}) if isinstance(data, dict) else {}
    dynamic = (mcp_cfg.get("dynamic_tools") or {}) if isinstance(mcp_cfg, dict) else {}

    explicit_preset = str(dynamic.get("preset") or "").strip() or None
    inherited_preset = str(operator_cfg.get("preset") or "").strip() or None

    return MCPPolicy(
        preset=explicit_preset or inherited_preset,
        allow_mutating=bool(dynamic.get("allow_mutating", False)),
        allow_skills=tuple(dynamic["allow_skills"])
            if isinstance(dynamic.get("allow_skills"), (list, tuple))
            else None,
        deny_skills=tuple(dynamic.get("deny_skills") or ()),
        allow_actions=tuple(dynamic["allow_actions"])
            if isinstance(dynamic.get("allow_actions"), (list, tuple))
            else None,
        deny_actions=tuple(dynamic.get("deny_actions") or ()),
        include_unimplemented=bool(dynamic.get("include_unimplemented", False)),
        live_trading_enabled=bool(runtime.get("live_trading_enabled", False)),
    )


@dataclass
class MCPTool:
    """One generated MCP tool descriptor.

    ``fn`` is a callable suitable for FastMCP registration; it accepts
    keyword payload fields and returns a JSON-serialisable dict.
    """

    name: str
    description: str
    skill_id: str
    action: str
    input_schema: dict[str, Any]
    permissions: list[str]
    risk_gate: str
    approval_gate: str
    read_only: bool
    tags: list[str] = field(default_factory=list)
    status: str = "ready"
    fn: Callable[..., dict[str, Any]] | None = None
    decision: str = "allow"
    decision_reason: str = "ok"

    def asdict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "skill_id": self.skill_id,
            "action": self.action,
            "input_schema": dict(self.input_schema),
            "permissions": list(self.permissions),
            "risk_gate": self.risk_gate,
            "approval_gate": self.approval_gate,
            "read_only": bool(self.read_only),
            "tags": list(self.tags),
            "status": self.status,
            "decision": self.decision,
            "decision_reason": self.decision_reason,
        }


@dataclass
class _DroppedTool:
    """Diagnostic record for an action filtered out of the MCP surface."""

    skill_id: str
    action: str
    reason: str
    detail: str = ""

    def asdict(self) -> dict[str, Any]:
        return {
            "skill_id": self.skill_id,
            "action": self.action,
            "reason": self.reason,
            "detail": self.detail,
        }


def _build_tool(client: InternalClient, manifest: SkillManifest, spec: ActionSpec,
                policy: MCPPolicy, *, decision: str = "allow",
                reason: str = "ok") -> MCPTool:
    name = mcp_tool_name(manifest.id, spec.name)
    description = (
        spec.description
        or spec.title
        or f"{manifest.id}.{spec.name}"
    ).strip()

    skill_id = manifest.id
    action_name = spec.name

    def _fn(**payload: Any) -> dict[str, Any]:
        try:
            return client.skill.call(
                skill_id, action_name,
                payload=dict(payload), caller="mcp",
            )
        except Exception as exc:  # pragma: no cover - defensive
            return {
                "error": {
                    "code": type(exc).__name__,
                    "message": str(exc),
                    "skill": skill_id,
                    "action": action_name,
                    "trace": traceback.format_exc(limit=4),
                }
            }

    return MCPTool(
        name=name,
        description=description,
        skill_id=skill_id,
        action=action_name,
        input_schema=_action_input_schema(spec),
        permissions=list(spec.permissions or manifest.permissions or []),
        risk_gate=spec.risk_gate,
        approval_gate=spec.approval_gate,
        read_only=action_is_read_only(action_name),
        tags=list(spec.tags or []) + list(manifest.tags or []),
        status=spec.status,
        fn=_fn,
        decision=decision,
        decision_reason=reason,
    )


def _matches(value: str, patterns: Sequence[str]) -> bool:
    if not patterns:
        return False
    return value in patterns


def _eval_preset(preset: OperatorPreset | None, *, action_name: str,
                 skill_id: str, read_only: bool, risk_gate: str,
                 approval_gate: str, live_trading_enabled: bool,
                 ) -> tuple[str, str]:
    """Wrapper around :func:`operator_presets.evaluate` for a single row."""

    if preset is None:
        return "allow", "ok"
    row = {
        "alias": f"{skill_id}.{action_name}",
        "skill_id": skill_id,
        "action_name": action_name,
        "query_only": read_only,
        "risk_gate": risk_gate,
        "approval_gate": approval_gate,
    }
    decision = evaluate_preset(
        row, preset,
        runtime_live_enabled=live_trading_enabled,
    )
    return ("allow" if decision.allowed else "deny"), decision.reason


@dataclass
class DynamicMCPRegistry:
    """Resolved MCP-tool view of a Nerya workspace at boot time.

    Construct via :meth:`build`; iterate ``tools`` for the FastMCP
    ``@mcp.tool()`` registration loop, and inspect ``dropped`` for the
    capability matrix / diagnostics.
    """

    tools: list[MCPTool] = field(default_factory=list)
    dropped: list[_DroppedTool] = field(default_factory=list)
    policy: MCPPolicy = field(default_factory=MCPPolicy)

    def names(self) -> list[str]:
        return [t.name for t in self.tools]

    def by_name(self, name: str) -> MCPTool | None:
        for tool in self.tools:
            if tool.name == name:
                return tool
        return None

    def asdict(self) -> dict[str, Any]:
        return {
            "policy": {
                "preset": self.policy.preset,
                "allow_mutating": self.policy.allow_mutating,
                "allow_skills": list(self.policy.allow_skills) if self.policy.allow_skills else None,
                "deny_skills": list(self.policy.deny_skills),
                "allow_actions": list(self.policy.allow_actions) if self.policy.allow_actions else None,
                "deny_actions": list(self.policy.deny_actions),
                "include_unimplemented": self.policy.include_unimplemented,
                "live_trading_enabled": self.policy.live_trading_enabled,
            },
            "tools": [t.asdict() for t in self.tools],
            "dropped": [d.asdict() for d in self.dropped],
            "total": len(self.tools),
        }

    @classmethod
    def build(
        cls,
        client: InternalClient,
        *,
        policy: MCPPolicy | None = None,
    ) -> "DynamicMCPRegistry":
        policy = policy or policy_from_config(client.config)
        preset = get_preset(policy.preset) if policy.preset else None

        tools: list[MCPTool] = []
        dropped: list[_DroppedTool] = []

        for entry in client.skills.registry.list():
            manifest = entry.manifest
            if policy.allow_skills is not None and manifest.id not in policy.allow_skills:
                dropped.append(_DroppedTool(
                    skill_id=manifest.id, action="*",
                    reason="skill_not_allowlisted",
                    detail=f"skill {manifest.id!r} not in allow_skills",
                ))
                continue
            if _matches(manifest.id, policy.deny_skills):
                dropped.append(_DroppedTool(
                    skill_id=manifest.id, action="*",
                    reason="skill_deny_listed",
                    detail=f"skill {manifest.id!r} in deny_skills",
                ))
                continue

            for action_name, spec in manifest.actions.items():
                qualified = f"{manifest.id}.{action_name}"

                if not policy.include_unimplemented and (
                    spec.status == "proposal_only_unimplemented"
                    or manifest.status == "proposal_only_unimplemented"
                ):
                    dropped.append(_DroppedTool(
                        skill_id=manifest.id, action=action_name,
                        reason="unimplemented",
                        detail=f"status={spec.status or manifest.status!r}",
                    ))
                    continue

                if policy.allow_actions is not None and qualified not in policy.allow_actions:
                    dropped.append(_DroppedTool(
                        skill_id=manifest.id, action=action_name,
                        reason="action_not_allowlisted",
                        detail=f"{qualified!r} not in allow_actions",
                    ))
                    continue

                if _matches(qualified, policy.deny_actions):
                    dropped.append(_DroppedTool(
                        skill_id=manifest.id, action=action_name,
                        reason="action_deny_listed",
                        detail=f"{qualified!r} in deny_actions",
                    ))
                    continue

                read_only = action_is_read_only(action_name)
                if not read_only and not policy.allow_mutating:
                    dropped.append(_DroppedTool(
                        skill_id=manifest.id, action=action_name,
                        reason="mutating_action_blocked",
                        detail="action name pattern is not read-only and "
                               "policy.allow_mutating=False",
                    ))
                    continue

                decision, reason = _eval_preset(
                    preset,
                    action_name=action_name,
                    skill_id=manifest.id,
                    read_only=read_only,
                    risk_gate=spec.risk_gate,
                    approval_gate=spec.approval_gate,
                    live_trading_enabled=policy.live_trading_enabled,
                )
                if decision != "allow":
                    dropped.append(_DroppedTool(
                        skill_id=manifest.id, action=action_name,
                        reason=f"preset:{reason}",
                        detail=f"operator preset {policy.preset!r} blocked "
                               f"{qualified!r}",
                    ))
                    continue

                tools.append(_build_tool(client, manifest, spec, policy,
                                         decision=decision, reason=reason))

        tools.sort(key=lambda t: t.name)
        dropped.sort(key=lambda d: (d.skill_id, d.action))
        return cls(tools=tools, dropped=dropped, policy=policy)


def tools_as_json(reg: DynamicMCPRegistry) -> str:
    """Serialise the dynamic registry for documentation / discovery."""
    return json.dumps(reg.asdict(), indent=2, default=str, ensure_ascii=False)
