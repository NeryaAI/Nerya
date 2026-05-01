"""bridge a native :class:`ToolRegistry` to the MCP server.

The legacy MCP wiring projects ``ActionSpec`` (declarative action
metadata on each ``SkillManifest``) into MCP tool descriptors. That
duplicates the source of truth: every action lives once on the manifest
and once again on the MCP wrapper.

In the workspace-native architecture there is *one* registry — the
:class:`nerya.tools.registry.ToolRegistry` — that the agent loop, the
permission engine, and (now) the MCP server consume directly. This
module renders the registry as MCP tools and dispatches calls through
:class:`NativeToolExecutor` so MCP gets the same validate / permission
/ hooks pipeline as the agent.

Two consumers:

* :func:`build_native_mcp_registry` — produces a list of dataclasses
  that the MCP server iterates to register FastMCP tools.
* :func:`mcp_dispatch` — single chokepoint used by the FastMCP wrapper
  to invoke a tool by name (used by the bridge above).

The bridge respects the same operator preset / mode that the agent
loop uses. When :class:`PermissionContext.mode` is ``BYPASS`` every
mutating tool is allowed; when it is ``RESTRICTED`` only auto-approve
read-only tools surface; the default mode keeps the executor's
ASK/ALLOW/DENY semantics.
"""

from __future__ import annotations

import json
import logging
import re
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable, Optional

from ..tools import (
    NativeToolExecutor,
    PermissionContext,
    PermissionDecisionKind,
    PermissionEngine,
    PermissionMode,
    PermissionRequest,
    ToolRegistry,
)
from ..tools.types import (
    PermissionScope,
    RiskLevel,
    ToolCall,
    ToolDescriptor,
    ToolResult,
)


_LOG = logging.getLogger(__name__)
_NAME_SAFE = re.compile(r"[^A-Za-z0-9_]+")


def _safe(value: str) -> str:
    return _NAME_SAFE.sub("_", value).strip("_")


def native_mcp_tool_name(tool_name: str) -> str:
    """Render the public MCP name for a native tool.

    We prefix every native tool with ``nerya_native_`` so the MCP
    surface clearly separates them from the legacy ``nerya_*`` and
    skill-projected ``nerya_<skill>__<action>`` names.
    """

    return f"nerya_native_{_safe(tool_name)}"


# ---------------------------------------------------------------------------
# Public dataclasses
# ---------------------------------------------------------------------------


@dataclass
class MCPNativeTool:
    """One registered native tool exposed over MCP."""

    name: str
    tool_name: str
    description: str
    input_schema: dict[str, Any]
    risk: str
    permission_scope: str
    read_only: bool
    is_concurrency_safe: bool
    requires_fresh_read: bool
    mutates_paths: bool
    tags: list[str] = field(default_factory=list)
    auto_approve: bool = False
    fn: Optional[Callable[..., dict[str, Any]]] = None
    decision: str = "allow"
    decision_reason: str = "ok"

    def asdict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "tool_name": self.tool_name,
            "description": self.description,
            "input_schema": dict(self.input_schema),
            "risk": self.risk,
            "permission_scope": self.permission_scope,
            "read_only": self.read_only,
            "is_concurrency_safe": self.is_concurrency_safe,
            "requires_fresh_read": self.requires_fresh_read,
            "mutates_paths": self.mutates_paths,
            "tags": list(self.tags),
            "auto_approve": self.auto_approve,
            "decision": self.decision,
            "decision_reason": self.decision_reason,
        }


@dataclass
class NativeMCPRegistry:
    """Resolved view of the native ToolRegistry as MCP tools."""

    tools: list[MCPNativeTool] = field(default_factory=list)
    dropped: list[dict[str, Any]] = field(default_factory=list)
    mode: str = PermissionMode.DEFAULT.value

    def names(self) -> list[str]:
        return [t.name for t in self.tools]

    def by_name(self, name: str) -> Optional[MCPNativeTool]:
        for tool in self.tools:
            if tool.name == name:
                return tool
        return None

    def asdict(self) -> dict[str, Any]:
        return {
            "mode": self.mode,
            "tools": [t.asdict() for t in self.tools],
            "dropped": list(self.dropped),
            "total": len(self.tools),
        }


# ---------------------------------------------------------------------------
# Filter policy
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class NativeMCPPolicy:
    """Filter / preset settings for the native MCP layer.

    * ``mode`` — :class:`PermissionMode` to seed the executor's
      :class:`PermissionContext` with. ``RESTRICTED`` is recommended for
      remote MCP clients; ``DEFAULT`` for local CLI access.
    * ``allow_mutating`` — when ``False`` (default) only ``read_only``
      tools are exposed; mutating tools are dropped from the surface
      regardless of the runtime mode.
    * ``allow_exec`` — gate for ``RiskLevel.EXEC`` (run_shell,
      script_run). Off by default so a misconfigured MCP client can't
      ask the workspace to run arbitrary code.
    * ``deny_tools`` — explicit names to drop.
    * ``allow_tools`` — when present, only these names are exposed.
    """

    mode: PermissionMode = PermissionMode.RESTRICTED
    allow_mutating: bool = False
    allow_exec: bool = False
    deny_tools: tuple[str, ...] = ()
    allow_tools: Optional[tuple[str, ...]] = None


def policy_from_config(config: Any) -> NativeMCPPolicy:
    """Read ``mcp.native_tools.*`` and ``runtime.mode`` to build a policy."""

    data = (getattr(config, "data", None) or {}) or {}
    mcp_cfg = (data.get("mcp") or {}) if isinstance(data, dict) else {}
    native_cfg = (mcp_cfg.get("native_tools") or {}) if isinstance(mcp_cfg, dict) else {}
    runtime_cfg = (data.get("runtime") or {}) if isinstance(data, dict) else {}

    mode_str = str(native_cfg.get("mode") or "restricted").lower()
    try:
        mode = PermissionMode(mode_str)
    except ValueError:
        mode = PermissionMode.RESTRICTED

    return NativeMCPPolicy(
        mode=mode,
        allow_mutating=bool(native_cfg.get("allow_mutating", False)),
        allow_exec=bool(native_cfg.get("allow_exec", False))
        or bool(runtime_cfg.get("live_trading_enabled", False))
        and bool(native_cfg.get("inherit_live_trading", False)),
        deny_tools=tuple(native_cfg.get("deny_tools") or ()),
        allow_tools=tuple(native_cfg["allow_tools"])
        if isinstance(native_cfg.get("allow_tools"), (list, tuple))
        else None,
    )


# ---------------------------------------------------------------------------
# Build / dispatch
# ---------------------------------------------------------------------------


def _make_dispatcher(
    descriptor: ToolDescriptor,
    *,
    executor: NativeToolExecutor,
    caller: str = "mcp",
) -> Callable[..., dict[str, Any]]:
    """Return an MCP-ready callable that drives ``executor.execute`` for
    ``descriptor``."""

    def _fn(**payload: Any) -> dict[str, Any]:
        call = ToolCall(
            name=descriptor.name,
            arguments=dict(payload),
            id=f"toolu_mcp_{uuid.uuid4().hex[:10]}",
            turn_id="mcp",
            iteration=0,
            caller=caller,
        )
        try:
            result: ToolResult = executor.execute(call)
        except Exception as exc:  # pragma: no cover - defensive
            _LOG.exception("MCP dispatch crashed for %s", descriptor.name)
            return {
                "ok": False,
                "error": {
                    "code": type(exc).__name__,
                    "message": str(exc),
                    "tool": descriptor.name,
                },
            }
        return _result_as_mcp_dict(result, descriptor=descriptor)

    _fn.__name__ = native_mcp_tool_name(descriptor.name)
    _fn.__doc__ = descriptor.description
    return _fn


def _result_as_mcp_dict(result: ToolResult, *, descriptor: ToolDescriptor) -> dict[str, Any]:
    """Render :class:`ToolResult` into the dict shape MCP clients expect."""

    body: dict[str, Any] = {
        "ok": not result.is_error,
        "name": result.name,
        "tool_use_id": result.tool_use_id,
        "elapsed_ms": result.elapsed_ms,
        "metadata": dict(result.metadata or {}),
    }
    text = result.text()
    if text:
        body["text"] = text[:50_000]
    parts: list[dict[str, Any]] = []
    for part in result.content:
        if part.type == "text" and part.text is not None:
            parts.append({"type": "text", "text": part.text})
        elif part.type == "json" and part.data is not None:
            parts.append({"type": "json", "data": part.data})
        elif part.type == "diff" and part.text is not None:
            parts.append({"type": "diff", "text": part.text})
        elif part.type == "shell" and part.data is not None:
            parts.append({"type": "shell", "data": part.data})
    if parts:
        body["content"] = parts
    if result.error is not None:
        body["error"] = {
            "code": result.error.kind.value,
            "message": result.error.message,
            "detail": result.error.detail,
            "retryable": result.error.retryable,
        }
    body["descriptor"] = {
        "risk": descriptor.permission_scope.value,
        "result_kind": descriptor.result_kind,
    }
    return body


def build_native_mcp_registry(
    *,
    registry: ToolRegistry,
    config: Any,
    policy: Optional[NativeMCPPolicy] = None,
) -> tuple[NativeMCPRegistry, NativeToolExecutor]:
    """Render ``registry`` as MCP tools and return a paired executor.

    Returning the executor lets the MCP server reuse the same instance
    across every dispatched call (so permission decisions about
    "remember this approval for the rest of the session" persist).
    """

    policy = policy or policy_from_config(config)
    permission_engine = PermissionEngine()
    permission_context = PermissionContext(mode=policy.mode)
    executor = NativeToolExecutor(
        registry=registry,
        permission_engine=permission_engine,
        permission_context=permission_context,
    )

    descriptors: Iterable[ToolDescriptor] = registry.list_tools()
    tools: list[MCPNativeTool] = []
    dropped: list[dict[str, Any]] = []

    for d in descriptors:
        public_name = native_mcp_tool_name(d.name)
        if policy.allow_tools is not None and d.name not in policy.allow_tools:
            dropped.append(
                {"tool": d.name, "reason": "not_allowlisted"}
            )
            continue
        if d.name in policy.deny_tools:
            dropped.append({"tool": d.name, "reason": "deny_listed"})
            continue
        if not policy.allow_mutating and not d.read_only:
            dropped.append(
                {"tool": d.name, "reason": "mutating_blocked"}
            )
            continue
        if not policy.allow_exec and d.risk == RiskLevel.EXEC:
            dropped.append(
                {"tool": d.name, "reason": "exec_blocked"}
            )
            continue

        decision = permission_engine.evaluate(
            PermissionRequest(
                descriptor=d,
                payload={},
                caller="mcp",
                turn_id="mcp",
                iteration=0,
            ),
            permission_context,
        )
        if decision.kind == PermissionDecisionKind.DENY:
            dropped.append(
                {
                    "tool": d.name,
                    "reason": f"permission_engine:{decision.reason or 'deny'}",
                }
            )
            continue

        tools.append(
            MCPNativeTool(
                name=public_name,
                tool_name=d.name,
                description=d.description,
                input_schema=dict(d.input_schema or {}),
                risk=d.risk.value,
                permission_scope=d.permission_scope.value,
                read_only=d.read_only,
                is_concurrency_safe=d.is_concurrency_safe,
                requires_fresh_read=d.requires_fresh_read,
                mutates_paths=d.mutates_paths,
                tags=list(d.tags or []),
                auto_approve=bool(d.auto_approve),
                fn=_make_dispatcher(d, executor=executor),
                decision=decision.kind.value,
                decision_reason=decision.reason or "ok",
            )
        )

    tools.sort(key=lambda t: t.name)
    return (
        NativeMCPRegistry(tools=tools, dropped=dropped, mode=policy.mode.value),
        executor,
    )


__all__ = [
    "MCPNativeTool",
    "NativeMCPPolicy",
    "NativeMCPRegistry",
    "build_native_mcp_registry",
    "native_mcp_tool_name",
    "policy_from_config",
]
