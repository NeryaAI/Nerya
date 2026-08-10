"""Small, trusted fixtures used only by the built-in eval catalog.

The production registry stays unchanged.  These helpers seed a scratch
workspace and register deterministic adapters through the normal registry /
executor path so the catalog can exercise recovery branches offline.
"""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from typing import Any

from ..tools.permissions import PermissionDecisionKind, PermissionRule
from ..tools.registry import ToolRegistry, make_native_descriptor
from ..tools.types import (
    PermissionScope,
    RiskLevel,
    ToolCall,
    ToolError,
    ToolErrorKind,
    ToolResult,
)


def _root(scratch: dict[str, Any]) -> Path:
    root = Path(scratch["workspace_root"]).resolve()
    root.mkdir(parents=True, exist_ok=True)
    return root


def _write(scratch: dict[str, Any], relative: str, contents: str) -> None:
    path = _root(scratch) / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(contents, encoding="utf-8")
    scratch.setdefault("_fixture_files", []).append(path)


def seed_coding_workspace(scratch: dict[str, Any]) -> None:
    _write(
        scratch,
        "src/util.py",
        "def calculate_total(values):\n    return sum(values)\n",
    )


def seed_schema_workspace(scratch: dict[str, Any]) -> None:
    _write(scratch, "README.md", "# Eval workspace\n\nFixture README.\n")


def seed_permission_workspace(scratch: dict[str, Any]) -> None:
    _write(
        scratch,
        "ops/runbook.md",
        "# Reset runbook\n\nRun the database reset manually after approval.\n",
    )
    executor = scratch.get("executor")
    context = getattr(executor, "permission_context", None)
    if context is None:
        return
    rule = PermissionRule(
        tool="run_shell",
        payload_regex=r"TRUNCATE\s+accounts",
        decision=PermissionDecisionKind.DENY,
        reason="eval fixture denies destructive database reset",
    )
    context.session_rules.append(rule)
    scratch.setdefault("_fixture_rules", []).append((context, rule))


def seed_compact_workspace(scratch: dict[str, Any]) -> None:
    _write(
        scratch,
        "src/auth/main.py",
        "from .helpers import verify_token\n\ndef authenticate(token):\n    return verify_token(token)\n",
    )
    _write(
        scratch,
        "src/auth/helpers.py",
        "def verify_token(token):\n    return bool(token)\n\ndef dead_helper():\n    return None\n",
    )


def _register(scratch: dict[str, Any], descriptor: Any) -> None:
    registry: ToolRegistry = scratch["registry"]
    registry.register(descriptor)
    scratch.setdefault("_fixture_tools", []).append(descriptor.name)


def register_grep_compat(scratch: dict[str, Any]) -> None:
    """Keep the old eval vocabulary without changing production tools."""

    registry: ToolRegistry = scratch["registry"]
    if registry.has("grep_search"):
        return
    _register(scratch, replace(registry.get("grep"), name="grep_search"))


def seed_skill_workspace(scratch: dict[str, Any]) -> None:
    _write(
        scratch,
        "skills/installed/workspace_janitor/SKILL.md",
        "---\n"
        "name: workspace_janitor\n"
        "description: Deterministic read-only workspace janitor eval skill.\n"
        "version: 0.1.0\n"
        "permissions:\n"
        "  - read\n"
        "---\n\n"
        "# Workspace Janitor\n\n"
        "Inspect the workspace and report what would be removed.\n",
    )
    _write(
        scratch,
        "skills/installed/workspace_janitor/scripts/janitor.py",
        "import json\nprint(json.dumps({'ok': True, 'removed': 0}))\n",
    )

    # SkillKernel is booted before scenario setup. Refresh it after adding the
    # fixture, then rebuild the native index from the refreshed registry.
    skills = scratch.get("skills")
    deps = scratch.get("deps")
    if skills is not None and hasattr(skills, "reload"):
        skills.reload()
    if deps is not None and skills is not None:
        files = [
            Path(entry.manifest.path) / "SKILL.md"
            for entry in skills.registry.list()
            if entry.manifest.path is not None
        ]
        index = deps.skill_index
        index._skill_files = files  # eval refresh; production index is immutable
        index.reload()

    executor = scratch.get("executor")
    context = getattr(executor, "permission_context", None)
    if context is not None:
        rule = PermissionRule(
            tool="script_run",
            payload_regex=r"workspace_janitor",
            decision=PermissionDecisionKind.ALLOW,
            reason="eval fixture is a deterministic read-only script",
        )
        context.session_rules.append(rule)
        scratch.setdefault("_fixture_rules", []).append((context, rule))


def register_mcp_fixture(scratch: dict[str, Any]) -> None:
    state = scratch.setdefault("_mcp_state", {"connected": False, "calls": 0})

    def mcp_call(call: ToolCall) -> ToolResult:
        state["calls"] += 1
        if not state["connected"]:
            return ToolResult.from_error(
                tool_use_id=call.id,
                name=call.name,
                error=ToolError(
                    kind=ToolErrorKind.MCP_SESSION_EXPIRED,
                    message="MCP session_expired; reconnect before retrying.",
                    retryable=True,
                    recovery_hint={"action": "mcp_reconnect", "server": "github"},
                ),
            )
        return ToolResult.from_json(
            tool_use_id=call.id,
            name=call.name,
            data={"ok": True, "server": "github", "tool": "list_issues", "count": 5},
        )

    def mcp_reconnect(call: ToolCall) -> ToolResult:
        state["connected"] = True
        return ToolResult.from_json(
            tool_use_id=call.id,
            name=call.name,
            data={"ok": True, "server": "github", "status": "reconnected"},
        )

    _register(
        scratch,
        make_native_descriptor(
            name="mcp_call",
            description="Eval-only deterministic MCP call adapter.",
            input_schema={
                "type": "object",
                "properties": {
                    "server": {"type": "string"},
                    "tool": {"type": "string"},
                    "args": {"type": "object"},
                },
                "required": ["server", "tool"],
                "additionalProperties": False,
            },
            handler=mcp_call,
            risk=RiskLevel.READ,
            permission_scope=PermissionScope.NONE,
            auto_approve=True,
            tags=("eval", "mcp"),
        ),
    )
    _register(
        scratch,
        make_native_descriptor(
            name="mcp_reconnect",
            description="Eval-only deterministic MCP reconnect adapter.",
            input_schema={
                "type": "object",
                "properties": {"server": {"type": "string"}},
                "required": ["server"],
                "additionalProperties": False,
            },
            handler=mcp_reconnect,
            risk=RiskLevel.READ,
            permission_scope=PermissionScope.NONE,
            auto_approve=True,
            tags=("eval", "mcp"),
        ),
    )


def register_subagent_fixture(scratch: dict[str, Any]) -> None:
    def dispatch(call: ToolCall) -> ToolResult:
        args = call.arguments or {}
        name = str(args.get("name") or "market_analyst")
        return ToolResult.from_json(
            tool_use_id=call.id,
            name=call.name,
            data={
                "ok": True,
                "subagent": name,
                "output": {"summary": "BTC realised vol up 12% over 24h."},
                "context_scope": "isolated",
            },
        )

    _register(
        scratch,
        make_native_descriptor(
            name="dispatch_subagent",
            description="Eval-only deterministic subagent completion adapter.",
            input_schema={
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "prompt": {"type": "string"},
                },
                "required": ["name", "prompt"],
                "additionalProperties": False,
            },
            handler=dispatch,
            risk=RiskLevel.EXEC,
            permission_scope=PermissionScope.WORKSPACE,
            auto_approve=True,
            tags=("eval", "subagent"),
        ),
    )


def cleanup(scratch: dict[str, Any]) -> None:
    """Remove only files/rules/tools this scenario created."""

    for context, rule in reversed(scratch.get("_fixture_rules", [])):
        try:
            context.session_rules.remove(rule)
        except (ValueError, AttributeError):
            pass
    registry = scratch.get("registry")
    for name in reversed(scratch.get("_fixture_tools", [])):
        try:
            registry.unregister(name)
        except Exception:
            pass
    for path in reversed(scratch.get("_fixture_files", [])):
        try:
            path.unlink(missing_ok=True)
        except OSError:
            pass
    # Remove only now-empty fixture directories, from deepest to shallowest.
    roots = {Path(path).parent for path in scratch.get("_fixture_files", [])}
    for directory in sorted(roots, key=lambda p: len(p.parts), reverse=True):
        try:
            directory.rmdir()
        except OSError:
            pass


__all__ = [
    "cleanup",
    "register_grep_compat",
    "register_mcp_fixture",
    "register_subagent_fixture",
    "seed_coding_workspace",
    "seed_compact_workspace",
    "seed_permission_workspace",
    "seed_schema_workspace",
    "seed_skill_workspace",
]
