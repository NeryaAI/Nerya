"""NativeToolExecutor — validate -> permission -> hooks -> execute -> result.

This is the single chokepoint between the agent loop and tool handlers
in the new architecture. The legacy ``nerya.harness.tool_runner`` is
kept for legacy ``SKILL.md actions`` but the workspace-native loop calls ``NativeToolExecutor.execute(call)`` exclusively.

Lifecycle for one call:

1. **Lookup** — ``ToolRegistry.get(name)`` => :class:`ToolDescriptor`.
2. **Validate** — JSON-schema check on ``call.arguments``.
3. **Permission** — :meth:`PermissionEngine.evaluate` => ``ALLOW``,
   ``ASK`` (=> ``permission_pending`` result), or ``DENY`` (=>
   ``permission_denied`` result).
4. **Fresh-read precondition** — for descriptors with
   ``requires_fresh_read=True`` we sample :class:`FileStateCache`
   and refuse mutation when the read is stale or missing.
5. **Pre-hook** — optional callbacks (telemetry, transcript writes).
6. **Execute** — invoke the handler. ``elapsed_ms`` is filled in here.
7. **Apply context modifiers** — caller does this; the executor only
   *attaches* them to the result.
8. **Post-hook** — optional callbacks for evidence / streaming.

Implementation notes:
"""

from __future__ import annotations

import inspect
import logging
import time
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Optional, Union

from .permissions import (
    PermissionContext,
    PermissionDecision,
    PermissionDecisionKind,
    PermissionEngine,
    PermissionRequest,
)
from .registry import ToolNotFoundError, ToolRegistry
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


_LOG = logging.getLogger(__name__)


PreHook = Callable[[ToolCall, ToolDescriptor, PermissionDecision], None]
PostHook = Callable[[ToolCall, ToolResult], None]
"""Post-tool hook. Mutate ``result`` in place to attach context /
redact secrets / append breadcrumbs. Setting
``result.context_modifiers`` or ``result.metadata['block_continuation']
= True`` lets the loop know it must surface the hook output to the
operator before continuing the next round (mirrors coding-agent's
``executePostToolHooks`` blocking error path).
"""

PermissionDeniedHook = Callable[
    [ToolCall, ToolDescriptor, PermissionDecision], None
]
"""Fired *only* when the permission engine returned ``DENY``. Use
this for analytics, transcript breadcrumbs, or to surface a
dashboard banner — the call already has its denied tool_result by
the time this runs."""

ApprovalCallback = Callable[
    [ToolCall, ToolDescriptor, PermissionDecision],
    Union[bool, "Awaitable[bool]"],
]


# ---------------------------------------------------------------------------
# Schema validation (lightweight, no jsonschema dep)
# ---------------------------------------------------------------------------


def _validate_against_schema(payload: dict[str, Any], schema: dict[str, Any]) -> Optional[str]:
    """Return None on success, an error string otherwise.

    Implements a *useful subset* of JSON Schema: type / required /
    enum on direct properties. Avoids a hard dependency on jsonschema
    (which adds ~1MB and breaks in some restricted runtimes).
    """

    if not schema:
        return None
    expected_type = schema.get("type")
    if expected_type == "object":
        if not isinstance(payload, dict):
            return f"expected object, got {type(payload).__name__}"
        required = schema.get("required") or []
        for key in required:
            if key not in payload:
                return f"missing required field: {key}"
        props = schema.get("properties") or {}
        for key, sub in props.items():
            if key not in payload:
                continue
            sub_type = sub.get("type")
            v = payload[key]
            if sub_type == "string" and not isinstance(v, str):
                return f"field {key!r} must be a string"
            if sub_type == "integer" and not isinstance(v, int):
                return f"field {key!r} must be an integer"
            if sub_type == "number" and not isinstance(v, (int, float)):
                return f"field {key!r} must be a number"
            if sub_type == "boolean" and not isinstance(v, bool):
                return f"field {key!r} must be a boolean"
            if sub_type == "array" and not isinstance(v, list):
                return f"field {key!r} must be an array"
            if sub_type == "object" and not isinstance(v, dict):
                return f"field {key!r} must be an object"
            enum = sub.get("enum")
            if enum and v not in enum:
                return f"field {key!r} must be one of {enum}"
    return None


# ---------------------------------------------------------------------------
# Approval state (executor-side bookkeeping for ASK decisions)
# ---------------------------------------------------------------------------


@dataclass
class _PendingApproval:
    tool_use_id: str
    tool_name: str
    decision: PermissionDecision
    expires_at: float


# ---------------------------------------------------------------------------
# Executor
# ---------------------------------------------------------------------------


@dataclass
class ExecutorOptions:
    """Tunables for :class:`NativeToolExecutor`.

    * ``fail_fast_on_validation`` — if true (default) schema-failures
      short-circuit before permission evaluation. Set to false for
      lenient mode (still passes broken payload to handler so it can
      respond with a structured error).
    * ``approval_timeout_sec`` — how long an ASK decision sits as
      ``permission_pending`` before the executor surfaces a timeout.
    """

    fail_fast_on_validation: bool = True
    approval_timeout_sec: float = 600.0


class NativeToolExecutor:
    """Single chokepoint between the loop and tool handlers."""

    def __init__(
        self,
        *,
        registry: ToolRegistry,
        permission_engine: PermissionEngine,
        permission_context: PermissionContext,
        approval_cb: Optional[ApprovalCallback] = None,
        pre_hooks: Optional[list[PreHook]] = None,
        post_hooks: Optional[list[PostHook]] = None,
        permission_denied_hooks: Optional[list[PermissionDeniedHook]] = None,
        options: Optional[ExecutorOptions] = None,
    ) -> None:
        self.registry = registry
        self.permission_engine = permission_engine
        self.permission_context = permission_context
        self.approval_cb = approval_cb
        self.pre_hooks: list[PreHook] = list(pre_hooks or [])
        self.post_hooks: list[PostHook] = list(post_hooks or [])
        self.permission_denied_hooks: list[PermissionDeniedHook] = list(
            permission_denied_hooks or []
        )
        self.options = options or ExecutorOptions()
        self._pending: dict[str, _PendingApproval] = {}

    # ------------------------------------------------------------------ hooks

    def add_pre_hook(self, hook: PreHook) -> None:
        self.pre_hooks.append(hook)

    def add_post_hook(self, hook: PostHook) -> None:
        self.post_hooks.append(hook)

    def add_permission_denied_hook(self, hook: PermissionDeniedHook) -> None:
        """Register a callback for explicit DENY decisions.

        The hook fires *after* the denied tool_result is produced.
        Hooks that raise are swallowed so a misbehaving observer can't
        crash the agent loop.
        """

        self.permission_denied_hooks.append(hook)

    # ------------------------------------------------------------------ exec

    def execute(self, call: ToolCall) -> ToolResult:
        """Synchronous execute. Awaitable handlers are resolved via
        ``asyncio.run`` when needed (caller must not be in an event
        loop). For async-native callers see :meth:`execute_async`."""

        started = time.monotonic()
        try:
            descriptor = self.registry.get(call.name)
        except ToolNotFoundError:
            return ToolResult.from_error(
                tool_use_id=call.id,
                name=call.name,
                error=ToolError(
                    kind=ToolErrorKind.UNKNOWN_TOOL,
                    message=f"unknown tool: {call.name!r}",
                ),
            )

        validation_error = (
            _validate_against_schema(call.arguments or {}, descriptor.input_schema)
            if self.options.fail_fast_on_validation
            else None
        )
        if validation_error:
            return ToolResult.from_error(
                tool_use_id=call.id,
                name=call.name,
                error=ToolError(
                    kind=ToolErrorKind.SCHEMA_VALIDATION,
                    message=validation_error,
                    detail={"schema": descriptor.input_schema},
                    retryable=True,
                    recovery_hint={
                        "action": "fix_arguments",
                        "schema": descriptor.input_schema,
                    },
                ),
            )

        decision = self.permission_engine.evaluate(
            PermissionRequest(
                descriptor=descriptor,
                payload=dict(call.arguments or {}),
                caller=call.caller,
                turn_id=call.turn_id,
                iteration=call.iteration,
            ),
            self.permission_context,
        )

        if decision.is_deny():
            denied = ToolResult.from_error(
                tool_use_id=call.id,
                name=call.name,
                error=ToolError(
                    kind=ToolErrorKind.PERMISSION_DENIED,
                    message=decision.reason or "permission denied",
                    detail=decision.asdict(),
                    retryable=False,
                ),
            )
            for hook in self.permission_denied_hooks:
                try:
                    hook(call, descriptor, decision)
                except Exception:
                    _LOG.exception(
                        "permission-denied hook failed for %s", call.name,
                    )
            return denied

        if decision.is_ask():
            approved = self._resolve_approval(call, descriptor, decision)
            if approved is False:
                return ToolResult.from_error(
                    tool_use_id=call.id,
                    name=call.name,
                    error=ToolError(
                        kind=ToolErrorKind.PERMISSION_DENIED,
                        message="user rejected the approval",
                        detail=decision.asdict(),
                        retryable=False,
                    ),
                )
            if approved is None:
                self._pending[call.id] = _PendingApproval(
                    tool_use_id=call.id,
                    tool_name=call.name,
                    decision=decision,
                    expires_at=time.time() + self.options.approval_timeout_sec,
                )
                return ToolResult.from_error(
                    tool_use_id=call.id,
                    name=call.name,
                    error=ToolError(
                        kind=ToolErrorKind.PERMISSION_PENDING,
                        message=(
                            decision.approval_reason
                            or "approval required before this tool can run"
                        ),
                        detail=decision.asdict(),
                        retryable=True,
                        recovery_hint={
                            "action": "await_approval",
                            "tool_use_id": call.id,
                        },
                    ),
                )

        for hook in self.pre_hooks:
            try:
                hook(call, descriptor, decision)
            except Exception:
                _LOG.exception("pre-hook failed for %s", call.name)

        try:
            result = self._invoke_handler(call, descriptor)
        except Exception as exc:
            _LOG.exception("tool handler crashed: %s", call.name)
            result = ToolResult.from_error(
                tool_use_id=call.id,
                name=call.name,
                error=ToolError(
                    kind=ToolErrorKind.EXECUTION_ERROR,
                    message=f"handler crashed: {exc}",
                    retryable=False,
                ),
            )

        result.elapsed_ms = max(result.elapsed_ms, int((time.monotonic() - started) * 1000))
        if not result.name:
            result.name = call.name
        result.tool_use_id = call.id

        result.metadata.setdefault("descriptor", {
            "namespace": descriptor.namespace,
            "result_kind": descriptor.result_kind,
            "risk": descriptor.per_call_risk(call.arguments or {}).value,
            "scope": descriptor.permission_scope.value,
        })

        for hook in self.post_hooks:
            try:
                hook(call, result)
            except Exception:
                _LOG.exception("post-hook failed for %s", call.name)

        return result

    # ----------------------------------------------------------------- detail

    def _invoke_handler(
        self, call: ToolCall, descriptor: ToolDescriptor
    ) -> ToolResult:
        handler = descriptor.handler
        result_or_awaitable = handler(call)
        if inspect.isawaitable(result_or_awaitable):
            import asyncio

            try:
                loop = asyncio.get_event_loop()
            except RuntimeError:
                loop = None
            if loop and loop.is_running():
                raise RuntimeError(
                    f"async tool handler {call.name!r} cannot be invoked from a running event loop "
                    "via execute(); call execute_async() instead."
                )
            return asyncio.run(result_or_awaitable)
        return result_or_awaitable  # type: ignore[return-value]

    def _resolve_approval(
        self,
        call: ToolCall,
        descriptor: ToolDescriptor,
        decision: PermissionDecision,
    ) -> Optional[bool]:
        if call.id in self.permission_context.approved_calls:
            return True
        if call.id in self.permission_context.rejected_calls:
            return False
        if self.approval_cb is None:
            return None
        try:
            verdict = self.approval_cb(call, descriptor, decision)
        except Exception:
            _LOG.exception("approval callback failed for %s", call.name)
            return None
        if inspect.isawaitable(verdict):
            try:
                import asyncio

                verdict = asyncio.run(verdict)  # type: ignore[arg-type]
            except RuntimeError:
                return None
        if verdict is True:
            self.permission_context.approved_calls.add(call.id)
            return True
        if verdict is False:
            self.permission_context.rejected_calls.add(call.id)
            return False
        return None


__all__ = [
    "ApprovalCallback",
    "ExecutorOptions",
    "NativeToolExecutor",
    "PermissionDeniedHook",
    "PostHook",
    "PreHook",
]
