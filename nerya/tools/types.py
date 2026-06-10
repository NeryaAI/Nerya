"""Provider-shaped types for the native tool harness.

These dataclasses are intentionally small, JSON-serialisable, and free
of Nerya-specific imports so they can be consumed by:

* :mod:`nerya.tools.registry`
* :mod:`nerya.agent.executor`
* :mod:`nerya.agent.loop`
* :mod:`nerya.llm.gateway` (``call_messages`` adapters)
* the dashboard ``/api/agent/transcript`` SSE writer
* MCP dynamic_tools
* legacy ``ToolRunner`` adapters

Why not reuse ``nerya/agent/transcript_blocks.py``?
   ``transcript_blocks`` is *streaming UI* shape (with ``block_id``,
   ``index``, ``is_partial``). This module is *executor* shape — the
   things the registry, permission engine, and tool implementations
   actually pass around. The two reconcile at the loop boundary in
   :mod:`nerya.agent.loop`.

Implementation notes:
"""

from __future__ import annotations

import enum
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Optional, Union


# ---------------------------------------------------------------------------
# Risk + permission taxonomy
# ---------------------------------------------------------------------------


class RiskLevel(str, enum.Enum):
    """Coarse risk classification for a tool *as a whole*.

    Per-call risk (e.g. ``rm -rf /`` vs ``ls``) is decided by the tool's
    own ``classify_risk`` hook; this enum is for the descriptor.
    """

    READ = "read"            # cannot mutate state
    WRITE = "write"          # mutates workspace files / DB / state
    EXEC = "exec"            # runs shell / subprocess / network mutation
    DANGEROUS = "dangerous"  # destructive / irreversible / external mutation


class PermissionScope(str, enum.Enum):
    """Where the tool's effects land.

    Used by :class:`PermissionEngine` to apply different policies to
    workspace edits vs system edits vs sandbox-only effects.
    """

    NONE = "none"            # pure compute, no side effects
    WORKSPACE = "workspace"  # under the configured workspace root
    NETWORK = "network"      # outbound HTTP / RPC / chain
    SYSTEM = "system"        # outside the workspace root
    SANDBOX = "sandbox"      # ephemeral container / tempdir
    SECRETS = "secrets"      # touches the SecretVault


# ---------------------------------------------------------------------------
# Tool descriptor (first-class registration)
# ---------------------------------------------------------------------------


# Type alias for the actual callable behind a tool. Tools may return
# either a sync ToolResult or a coroutine producing one. Both forms are
# accepted by the executor.
ToolHandler = Callable[..., Union["ToolResult", Awaitable["ToolResult"]]]


@dataclass(frozen=True)
class ToolDescriptor:
    """First-class metadata for a tool registered in :class:`ToolRegistry`.

    Provider-native tool descriptor kept Pythonic. Every field is
    required for permission / orchestration / UI rendering, so we surface
    them explicitly rather than hiding them behind kwargs.
    """

    name: str
    """Stable tool name. Provider-visible. Lower-snake-case, namespaced
    with a colon for non-native tools (e.g. ``mcp:figma.get_design``)."""

    description: str
    """Short LLM-visible description. First line is the headline."""

    input_schema: dict[str, Any]
    """JSON schema for the tool input. Used both for provider tool
    definitions *and* for schema validation before execution."""

    handler: ToolHandler
    """Callable executed by :class:`NativeToolExecutor`. Receives a
    :class:`ToolCall` and returns / yields a :class:`ToolResult`."""

    risk: RiskLevel = RiskLevel.READ
    """Coarse risk class. ``RiskLevel.READ`` => concurrency-safe."""

    permission_scope: PermissionScope = PermissionScope.NONE
    """Where the tool's effects land. Drives the permission engine."""

    read_only: bool = True
    """Convenience flag — derived from :class:`RiskLevel` but explicit
    so callers can override (e.g. a ``READ`` tool that still touches
    ``readFileState``)."""

    is_concurrency_safe: bool = True
    """Two simultaneous calls of this tool are safe for the workspace
    state. Read-only file/grep/glob tools are safe; edit/write/shell
    are not."""

    requires_fresh_read: bool = False
    """If true the executor verifies that every path argument has a
    fresh read in the FileStateCache before dispatching."""

    mutates_paths: bool = False
    """If true a successful call invalidates the FileStateCache for
    the touched paths."""

    result_kind: str = "json"
    """Coarse result shape — ``"text"`` | ``"json"`` | ``"diff"`` |
    ``"shell"`` | ``"file"`` | ``"image"`` — for UI rendering."""

    max_result_tokens: int = 4_000
    """Soft budget for the tool result. The executor may invoke
    microcompact when the raw result exceeds this budget."""

    namespace: str = "native"
    """Source namespace: ``native`` | ``mcp``. The permission engine and
    UI use this to group tools."""

    tags: tuple[str, ...] = field(default_factory=tuple)
    """Free-form tags (``code``, ``search``, ``shell``, ``planning``).
    Used by the model selection / tool filtering logic."""

    risk_classifier: Optional[Callable[[dict[str, Any]], RiskLevel]] = None
    """Optional per-call risk classifier. ``run_shell`` uses this to
    upgrade ``rm -rf /`` from ``EXEC`` to ``DANGEROUS``."""

    auto_approve: bool = False
    """If true, the permission engine never prompts the user even when
    risk would normally require approval. Used for ``todo_write`` and
    other zero-side-effect helpers."""

    auto_approve_when: Optional[Callable[[dict[str, Any]], bool]] = None
    """Optional per-call auto-approval predicate.

    This keeps broad tools such as ``script_run`` conservative by
    default while allowing a tightly scoped safe lane for a known skill
    script family.
    """

    lazy: bool = False
    """If true, the tool stays in the registry (so it can still be
    dispatched programmatically) but the agent loop's prompt-time
    rendering hides it until the namespace it belongs to has been
    explicitly described in the current session.

    Lazy MCP loading: a server with ``always_eager=False`` in
    ``mcp_servers.yml`` registers all of its tools with ``lazy=True``,
    so 32 MCP tool descriptions don't pollute the model's context until
    the model actively calls ``mcp_describe`` for that namespace.
    """

    # ----- helpers ---------------------------------------------------

    def to_provider_tool(self) -> dict[str, Any]:
        """Render as a provider tool spec (Anthropic-shaped).

        OpenAI / Gemini adapters wrap this further; the registry stays
        provider-agnostic.
        """

        return {
            "name": self.name,
            "description": self.description,
            "input_schema": dict(self.input_schema),
        }

    def per_call_risk(self, payload: dict[str, Any]) -> RiskLevel:
        """Resolve the risk for a single ``payload``.

        Falls back to the descriptor risk when no classifier is set.
        """

        if self.risk_classifier is None:
            return self.risk
        try:
            level = self.risk_classifier(payload)
        except Exception:
            return self.risk
        if isinstance(level, RiskLevel):
            return level
        try:
            return RiskLevel(str(level))
        except Exception:
            return self.risk

    def per_call_auto_approve(self, payload: dict[str, Any]) -> bool:
        """Return whether this exact call should bypass approval prompts."""

        if self.auto_approve:
            return True
        if self.auto_approve_when is None:
            return False
        try:
            return bool(self.auto_approve_when(payload))
        except Exception:
            return False


# ---------------------------------------------------------------------------
# ToolCall (executor input)
# ---------------------------------------------------------------------------


def _new_tool_use_id() -> str:
    return f"toolu_{uuid.uuid4().hex[:24]}"


@dataclass
class ToolCall:
    """A single tool invocation.

    Constructed by the agent loop from a provider ``tool_use`` block.
    The ``id`` is the provider-supplied ``tool_use_id`` and travels
    with the result so the loop can pair them on the next turn.
    """

    name: str
    arguments: dict[str, Any] = field(default_factory=dict)
    id: str = field(default_factory=_new_tool_use_id)

    turn_id: str = ""
    iteration: int = 0
    caller: str = "agent:native"
    started_at: float = field(default_factory=time.time)

    parent_call_id: Optional[str] = None
    """For sub-tool calls (e.g. AgentTool spawning a child loop)."""

    metadata: dict[str, Any] = field(default_factory=dict)
    """Attached by the loop (cancellation token id, permission preview,
    streaming buffer id, ...). Not sent to the model."""

    def asdict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "arguments": dict(self.arguments),
            "turn_id": self.turn_id,
            "iteration": self.iteration,
            "caller": self.caller,
            "started_at": self.started_at,
            "parent_call_id": self.parent_call_id,
            "metadata": dict(self.metadata),
        }


# ---------------------------------------------------------------------------
# ToolError taxonomy
# ---------------------------------------------------------------------------


class ToolErrorKind(str, enum.Enum):
    """Stable error taxonomy. The agent loop and providers use this
    to decide *recovery semantics* (retry, ask, replan, abort)."""

    SCHEMA_VALIDATION = "schema_validation"
    UNKNOWN_TOOL = "tool_not_found"
    PERMISSION_DENIED = "permission_denied"
    PERMISSION_PENDING = "permission_pending"
    TIMEOUT = "timeout"
    ABORTED = "aborted"
    RATE_LIMIT = "rate_limit"
    DEDUPED = "deduped"
    SANDBOX_DENIED = "sandbox_denied"
    STALE_FILE = "stale_file"
    DIFF_CONFLICT = "diff_conflict"
    NOT_FOUND = "not_found"
    CONFLICT = "conflict"
    PROVIDER_ERROR = "provider_error"
    MCP_SESSION_EXPIRED = "mcp_session_expired"
    EXECUTION_ERROR = "execution_error"
    UNKNOWN = "unknown"


@dataclass
class ToolError:
    """Structured tool error. Always wrapped into a ``ToolResult`` with
    ``is_error=True`` when handed back to the model."""

    kind: ToolErrorKind = ToolErrorKind.UNKNOWN
    message: str = ""
    detail: dict[str, Any] = field(default_factory=dict)

    #: Whether the model should retry the same call (``True`` => yes,
    #: ``False`` => switch strategy, ``None`` => model decides).
    retryable: Optional[bool] = None

    #: Optional structured recovery hint for the LLM, e.g.
    #: ``{"action": "read_file_first", "path": "src/foo.py"}``.
    recovery_hint: dict[str, Any] = field(default_factory=dict)

    def asdict(self) -> dict[str, Any]:
        return {
            "kind": self.kind.value,
            "message": self.message,
            "detail": dict(self.detail),
            "retryable": self.retryable,
            "recovery_hint": dict(self.recovery_hint),
        }


# ---------------------------------------------------------------------------
# ToolResult parts (provider-shaped multimodal content)
# ---------------------------------------------------------------------------


@dataclass
class ToolResultPart:
    """One content piece of a ``tool_result`` block.

    Mirrors Anthropic's ``tool_result.content[*]`` and OpenAI's
    ``tool_result.content`` shapes. We keep ``type`` as the string
    instead of an enum because providers add new types over time.
    """

    type: str = "text"  # "text" | "json" | "image" | "diff" | "code"
    text: Optional[str] = None
    data: Optional[Any] = None
    media_type: Optional[str] = None
    metadata: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def text_part(cls, text: str) -> "ToolResultPart":
        return cls(type="text", text=str(text))

    @classmethod
    def json_part(cls, data: Any) -> "ToolResultPart":
        return cls(type="json", data=data)

    @classmethod
    def diff_part(cls, *, diff: str, path: str) -> "ToolResultPart":
        return cls(type="diff", text=diff, metadata={"path": path})

    @classmethod
    def shell_part(
        cls,
        *,
        stdout: str,
        stderr: str,
        exit_code: int,
        duration_ms: int,
        truncated: bool = False,
    ) -> "ToolResultPart":
        return cls(
            type="shell",
            data={
                "stdout": stdout,
                "stderr": stderr,
                "exit_code": exit_code,
                "duration_ms": duration_ms,
                "truncated": truncated,
            },
        )

    def asdict(self) -> dict[str, Any]:
        out: dict[str, Any] = {"type": self.type}
        if self.text is not None:
            out["text"] = self.text
        if self.data is not None:
            out["data"] = self.data
        if self.media_type is not None:
            out["media_type"] = self.media_type
        if self.metadata:
            out["metadata"] = dict(self.metadata)
        return out


# ---------------------------------------------------------------------------
# ContextModifier (post-tool context updates)
# ---------------------------------------------------------------------------


@dataclass
class ContextModifier:
    """Side-effect a tool produces on the agent's *context* state.

    Examples:

    * ``read_file`` updates ``FileStateCache`` and the ArtifactIndex.
    * ``edit_file`` invalidates the cache for the path it just changed.
    * ``todo_write`` updates the session-level todo state.
    * ``run_shell`` records a command artifact.
    * ``enter_plan_mode`` flips the session ``plan_mode`` flag.
    * a tool may return ``hide_in_recompact=True`` so micro-compact
      eagerly drops its output.

    Tools attach modifiers to their result; the executor applies them
    in tool-call order *after* the batch finishes (so a parallel batch
    sees a deterministic post-state).
    """

    kind: str
    """One of ``file_read`` | ``file_mutate`` | ``todo_update`` |
    ``plan_mode`` | ``artifact_index`` | ``invoked_skill`` |
    ``hide_in_recompact`` | ``permission_decision`` | ``custom``."""

    path: Optional[str] = None
    payload: dict[str, Any] = field(default_factory=dict)

    def asdict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "path": self.path,
            "payload": dict(self.payload),
        }


# ---------------------------------------------------------------------------
# ToolResult (executor output)
# ---------------------------------------------------------------------------


@dataclass
class ToolResult:
    """Provider-shaped ``tool_result`` block.

    Returned by every native tool / MCP adapter / legacy adapter and
    handed to the agent loop, which appends it to the transcript with
    ``role="tool"``. ``content`` is always a list (Anthropic shape);
    providers that want a single string get the joined ``content``.
    """

    tool_use_id: str
    """Provider-supplied id from the matching ``tool_use`` block. Must
    be present on every result, even error results — otherwise the next
    turn's ``messages`` is internally invalid."""

    name: str = ""
    is_error: bool = False
    content: list[ToolResultPart] = field(default_factory=list)
    error: Optional[ToolError] = None
    elapsed_ms: int = 0
    completed_at: float = field(default_factory=time.time)
    context_modifiers: list[ContextModifier] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)
    """Free-form telemetry: stdout bytes, file size, line count, etc."""

    # ----- factories -----

    @classmethod
    def from_text(cls, *, tool_use_id: str, name: str, text: str) -> "ToolResult":
        return cls(
            tool_use_id=tool_use_id,
            name=name,
            content=[ToolResultPart.text_part(text)],
        )

    @classmethod
    def from_json(cls, *, tool_use_id: str, name: str, data: Any) -> "ToolResult":
        return cls(
            tool_use_id=tool_use_id,
            name=name,
            content=[ToolResultPart.json_part(data)],
        )

    @classmethod
    def from_error(
        cls,
        *,
        tool_use_id: str,
        name: str,
        error: ToolError,
    ) -> "ToolResult":
        return cls(
            tool_use_id=tool_use_id,
            name=name,
            is_error=True,
            error=error,
            content=[ToolResultPart.text_part(error.message or error.kind.value)],
        )

    # ----- helpers -----

    def asdict(self) -> dict[str, Any]:
        return {
            "type": "tool_result",
            "tool_use_id": self.tool_use_id,
            "name": self.name,
            "is_error": self.is_error,
            "content": [p.asdict() for p in self.content],
            "error": self.error.asdict() if self.error else None,
            "elapsed_ms": self.elapsed_ms,
            "completed_at": self.completed_at,
            "context_modifiers": [m.asdict() for m in self.context_modifiers],
            "metadata": dict(self.metadata),
        }

    def text(self) -> str:
        """Concatenate ``content`` text parts (best-effort)."""

        out: list[str] = []
        for part in self.content:
            if part.type == "text" and part.text is not None:
                out.append(part.text)
            elif part.type == "json" and part.data is not None:
                import json as _json

                out.append(_json.dumps(part.data, ensure_ascii=False, default=str))
            elif part.type == "diff" and part.text is not None:
                out.append(part.text)
            elif part.type == "shell" and part.data is not None:
                stdout = (part.data or {}).get("stdout") or ""
                stderr = (part.data or {}).get("stderr") or ""
                if stdout:
                    out.append(stdout)
                if stderr:
                    out.append(stderr)
        return "\n".join(out)


__all__ = [
    "ContextModifier",
    "PermissionScope",
    "RiskLevel",
    "ToolCall",
    "ToolDescriptor",
    "ToolError",
    "ToolErrorKind",
    "ToolHandler",
    "ToolResult",
    "ToolResultPart",
]
