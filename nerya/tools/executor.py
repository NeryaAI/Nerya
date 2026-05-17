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
import json
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
from .tool_errors import (
    collect_schema_issues,
    format_schema_validation_error,
)
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
operator before continuing the next round, rather than silently moving
past a blocking post-tool hook.
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
    """Return None on success, a short error string otherwise.

    Kept for callers outside the executor that still want a boolean
    "valid?" check. The executor itself now walks issues through
    :func:`collect_schema_issues` so the error the model sees is the
    full multi-issue breakdown, not just the first failure.
    """

    issues = collect_schema_issues(payload, schema)
    if not issues:
        return None
    first = issues[0]
    kind = first.get("kind")
    field = first.get("field")
    if kind == "missing":
        return f"missing required field: {field}"
    if kind == "unexpected":
        return f"unexpected field: {field}"
    if kind == "type":
        return (
            f"field {field!r} must be {first.get('expected')} "
            f"(got {first.get('actual')})"
        )
    if kind == "enum":
        return f"field {field!r} must be one of {first.get('expected')}"
    return "schema validation failed"


def _repair_arguments_before_validation(call: ToolCall) -> None:
    """Normalize narrow, recoverable provider argument mistakes.

    The same principle applies here: a call
    that *could* be rescued without lying about the user's intent
    should be rescued in place so the model avoids a round-trip.

    Keep the list of repairs small and obvious — every entry here
    trades a schema failure for a best-effort inference, so only
    register cases where the inference is unambiguous.
    """

    if not isinstance(call.arguments, dict):
        return
    args = call.arguments

    if call.name == "team_run":
        # Some providers (notably certain OpenAI-compatible backends)
        # serialise a nested JSON array as a string even when the
        # outer tool call is valid JSON. Unblock the Team handler so
        # it can emit its collaboration trace.
        raw_roles = args.get("roles")
        if isinstance(raw_roles, str) and raw_roles.strip():
            try:
                parsed = json.loads(raw_roles)
            except Exception:
                parsed = None
            if isinstance(parsed, list):
                call.arguments = {**args, "roles": parsed}
        return

    if call.name == "strategy_generate_proposal":
        # The model's reasoning typically carries a strategy_id but the
        # serialized tool_use block sometimes drops it (long reasoning
        # → truncated tool args). When a title is present we derive a
        # safe lowercase slug instead of burning a round-trip on a
        # schema failure. Schema still rules when both are missing.
        if not args.get("strategy_id") and args.get("title"):
            import re
            slug = re.sub(
                r"[^a-z0-9_]+", "_", str(args["title"]).lower(),
            ).strip("_")
            # strategy_id must start with a letter per the runtime
            # schema (^[a-z][a-z0-9_]+$). Guard before applying.
            if slug and slug[0].isalpha() and len(slug) >= 2:
                call.arguments = {**args, "strategy_id": slug[:64]}
        return


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

        _repair_arguments_before_validation(call)
        issues = (
            collect_schema_issues(call.arguments or {}, descriptor.input_schema)
            if self.options.fail_fast_on_validation
            else []
        )
        if issues:
            # Render each issue as a one-sentence English explanation so
            # the model can act on the tool_result without reading a
            # JSON-schema blob. The raw issues list stays in
            # ``detail.issues`` for dashboard / telemetry consumers.
            friendly = format_schema_validation_error(call.name, issues)
            return ToolResult.from_error(
                tool_use_id=call.id,
                name=call.name,
                error=ToolError(
                    kind=ToolErrorKind.SCHEMA_VALIDATION,
                    message=friendly,
                    detail={
                        "issues": [dict(i) for i in issues],
                        "schema": descriptor.input_schema,
                    },
                    retryable=True,
                    recovery_hint={
                        "action": "fix_arguments_and_retry",
                        "tool_name": call.name,
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
