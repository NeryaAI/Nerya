"""nerya.tools — Native tool harness (workspace-native agent layer).

This package implements the *Cursor-style / Claude-Code-style* tool harness
described in:

* ``Nerya/docs/agent-intelligence-gap-and-cursor-refactor-plan.md``
* ``Nerya/docs/agent-harness-comparison-and-refactor-todo.md``

Goal: a provider-native ``messages + tools -> tool_use -> tool_result``
loop. ``nerya.tools`` is the home for:

* :class:`ToolDescriptor` — first-class tool metadata (input_schema,
  read_only, concurrency safety, risk, permission scope, result_kind).
* :class:`ToolCall` / :class:`ToolResult` / :class:`ToolError` —
  provider-shaped dataclasses returned to the agent loop.
* :class:`ToolRegistry` — registers native tools, MCP tools, and
  resource accessors.
* The ``native/`` subpackage — workspace primitives (read_file, grep,
  glob, edit_file, write_file, run_shell, todo_write, plan tools,
  trading / LLM / memory / subagent / recipe / task / resource tools).

Other modules import from here:

* ``nerya.agent.loop`` — main ``WorkspaceNativeAgentLoop``.
* ``nerya.llm.gateway.call_messages`` — provider-native tool-calling.
* ``nerya.mcp.session_adapter`` — external MCP tools register here.
"""

from .types import (
    ContextModifier,
    PermissionScope,
    RiskLevel,
    ToolCall,
    ToolDescriptor,
    ToolError,
    ToolErrorKind,
    ToolResult,
    ToolResultPart,
)
from .registry import ToolRegistry, ToolNotFoundError, make_native_descriptor
from .permissions import (
    PermissionContext,
    PermissionDecision,
    PermissionDecisionKind,
    PermissionEngine,
    PermissionMode,
    PermissionRequest,
    PermissionRule,
)
from .executor import NativeToolExecutor, ExecutorOptions
from .orchestrator import ToolOrchestrator, BatchResult
from .resources import ResourceEntry, ResourceFetcher, ResourceIndex

__all__ = [
    "BatchResult",
    "ContextModifier",
    "ExecutorOptions",
    "NativeToolExecutor",
    "PermissionContext",
    "PermissionDecision",
    "PermissionDecisionKind",
    "PermissionEngine",
    "PermissionMode",
    "PermissionRequest",
    "PermissionRule",
    "PermissionScope",
    "ResourceEntry",
    "ResourceFetcher",
    "ResourceIndex",
    "RiskLevel",
    "ToolCall",
    "ToolDescriptor",
    "ToolError",
    "ToolErrorKind",
    "ToolNotFoundError",
    "ToolOrchestrator",
    "ToolRegistry",
    "ToolResult",
    "ToolResultPart",
    "make_native_descriptor",
]
