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
from dataclasses import dataclass, replace
from typing import Any, Callable, Optional

from .permissions import (
    PermissionContext,
    PermissionDecision,
    PermissionEngine,
    PermissionRequest,
)
from .registry import ToolNotFoundError, ToolRegistry
from .execution_contracts import dispatch_stop_reason, pair_executed_results, skipped_before_dispatch
from .tool_approvals import (
    ToolApprovalResolution,
    ToolApprovalResolver,
)
from .tool_errors import (
    collect_schema_issues,
    format_schema_validation_error,
)
from .types import (
    ToolCall,
    ToolDescriptor,
    ToolError,
    ToolErrorKind,
    ToolResult,
)


_LOG = logging.getLogger(__name__)


def _cancel_requested(token: Any) -> bool:
    if token is None:
        return False
    try:
        value = getattr(token, "is_set", False)
        return bool(value() if callable(value) else value)
    except Exception:
        return True


def _cancel_reason(token: Any) -> str:
    return str(getattr(token, "reason", "") or "cancelled")


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


def _repair_json_string_containers(
    args: dict[str, Any],
    schema: dict[str, Any] | None,
) -> dict[str, Any]:
    """Decode unambiguous JSON-string containers before schema validation."""

    if not isinstance(schema, dict):
        return args
    properties = schema.get("properties")
    if not isinstance(properties, dict):
        return args

    repaired: dict[str, Any] | None = None
    for field, field_schema in properties.items():
        if not isinstance(field_schema, dict):
            continue
        expected = field_schema.get("type")
        if expected not in {"object", "array"}:
            continue
        raw = args.get(field)
        if not isinstance(raw, str) or not raw.strip():
            continue
        try:
            parsed = json.loads(raw)
        except Exception:
            continue
        if expected == "object" and not isinstance(parsed, dict):
            continue
        if expected == "array" and not isinstance(parsed, list):
            continue
        if repaired is None:
            repaired = dict(args)
        repaired[field] = parsed
    return repaired or args


def coerce_json_number_string(raw: Any, *, integer: bool = False) -> Any:
    if not isinstance(raw, str):
        return raw
    text = raw.strip()
    if not text:
        return raw
    try:
        value = float(text)
    except Exception:
        return raw
    if value != value or value in (float("inf"), float("-inf")):
        return raw
    if integer:
        if not value.is_integer():
            return raw
        return int(value)
    if value.is_integer() and all(ch not in text.lower() for ch in (".", "e")):
        return int(value)
    return value


def _repair_schema_numeric_strings(
    args: dict[str, Any],
    schema: dict[str, Any] | None,
) -> dict[str, Any]:
    """Coerce provider-emitted JSON-number strings using the tool schema."""

    if not isinstance(schema, dict):
        return args
    properties = schema.get("properties")
    if not isinstance(properties, dict):
        return args

    repaired: dict[str, Any] | None = None
    for field, field_schema in properties.items():
        if not isinstance(field, str) or not isinstance(field_schema, dict):
            continue
        if field not in args:
            continue
        value = args.get(field)
        expected = field_schema.get("type")
        if expected in {"number", "integer"}:
            normalized = coerce_json_number_string(
                value,
                integer=expected == "integer",
            )
            if normalized is not value:
                if repaired is None:
                    repaired = dict(args)
                repaired[field] = normalized
            continue
        if expected == "object" and isinstance(value, dict):
            nested = _repair_schema_numeric_strings(value, field_schema)
            if nested is not value:
                if repaired is None:
                    repaired = dict(args)
                repaired[field] = nested
            continue
        if expected == "array" and isinstance(value, list):
            item_schema = field_schema.get("items")
            if not isinstance(item_schema, dict):
                continue
            normalized_items: list[Any] = []
            changed = False
            for item in value:
                if isinstance(item, dict):
                    nested = _repair_schema_numeric_strings(item, item_schema)
                    normalized_items.append(nested)
                    changed = changed or nested is not item
                else:
                    normalized_items.append(item)
            if changed:
                if repaired is None:
                    repaired = dict(args)
                repaired[field] = normalized_items
    return repaired if repaired is not None else args


def _repair_raw_json_object_argument(
    args: dict[str, Any],
) -> dict[str, Any]:
    """Decode provider fallback args shaped as ``{"_raw": "{...}"}``.

    Some OpenAI-compatible providers emit a syntactically valid tool call but
    place the whole argument object inside a single ``_raw`` string. When that
    string is a complete JSON object, unwrap it before schema validation. This
    is transport normalization only; malformed or non-object raw payloads stay
    untouched and are rejected by the normal schema path.
    """

    raw = args.get("_raw")
    if not isinstance(raw, str) or not raw.strip().startswith("{"):
        return args
    try:
        parsed = json.loads(raw)
    except Exception:
        return args
    if not isinstance(parsed, dict):
        return args
    repaired = {str(k): v for k, v in parsed.items() if isinstance(k, str)}
    for key, value in args.items():
        if key != "_raw":
            repaired[key] = value
    return repaired


def _repair_schema_array_item_wrappers(
    args: dict[str, Any],
    schema: dict[str, Any] | None,
) -> dict[str, Any]:
    """Unwrap provider-emitted ``{"item": ...}`` objects for array fields.

    Some OpenAI-compatible providers serialize a single array element as an
    object wrapper even when the tool schema says the top-level field is an
    array. This repair is schema-driven and only applies to obvious one-key
    wrappers before validation.
    """

    if not isinstance(schema, dict):
        return args
    properties = schema.get("properties")
    if not isinstance(properties, dict):
        return args
    repaired: dict[str, Any] | None = None
    for key, field_schema in properties.items():
        if not isinstance(key, str) or not isinstance(field_schema, dict):
            continue
        if field_schema.get("type") != "array" or key not in args:
            continue
        value = args.get(key)
        if not isinstance(value, dict) or set(value.keys()) != {"item"}:
            continue
        item = value.get("item")
        normalized = item if isinstance(item, list) else [item]
        if repaired is None:
            repaired = dict(args)
        repaired[key] = normalized
    return repaired if repaired is not None else args


def _repair_arguments_before_validation(
    call: ToolCall,
    schema: dict[str, Any] | None = None,
) -> None:
    """Decode schema-driven provider transport representations."""

    if not isinstance(call.arguments, dict):
        return
    args = _repair_raw_json_object_argument(call.arguments)
    args = _repair_json_string_containers(args, schema)
    if (
        call.name == "evolve_core_config_patch"
        and isinstance(schema, dict)
        and isinstance(schema.get("properties"), dict)
        and schema["properties"].get("config_after", {}).get("type") == "object"
        and isinstance(args.get("config_after"), str)
        and 0 < len(args["config_after"]) <= 64_000
    ):
        # Some OpenAI-compatible models return a valid YAML mapping as a
        # *string*, especially after context compaction. Permit only this
        # schema-owned YAML object. Never guess a truncated JSON/YAML blob or
        # broaden JSON-string coercion for arbitrary executable tools.
        from ..core import yaml_io

        try:
            parsed = yaml_io.loads(args["config_after"])
            seen: set[int] = set()

            def plain(value: Any, depth: int = 0) -> bool:
                if depth > 24:
                    return False
                if type(value) in (str, int, float, bool, type(None)):
                    return True
                if not isinstance(value, (dict, list)) or id(value) in seen:
                    return False
                seen.add(id(value))
                if len(value) > 2000:
                    return False
                if isinstance(value, dict):
                    return all(isinstance(k, str) and plain(v, depth + 1) for k, v in value.items())
                return all(plain(child, depth + 1) for child in value)

            if isinstance(parsed, dict) and plain(parsed):
                args = {**args, "config_after": parsed}
        except Exception:
            # Malformed/incomplete documents remain strings and fail schema
            # validation visibly. A retry must emit the whole mapping.
            pass
    args = _repair_schema_array_item_wrappers(args, schema)
    args = _repair_schema_numeric_strings(args, schema)
    if args is not call.arguments:
        call.arguments = args


def _schema_error(call: ToolCall, descriptor: ToolDescriptor) -> ToolResult | None:
    issues = collect_schema_issues(call.arguments or {}, descriptor.input_schema)
    if not issues:
        return None
    return ToolResult.from_error(
        tool_use_id=call.id, name=call.name,
        error=ToolError(
            kind=ToolErrorKind.SCHEMA_VALIDATION,
            message=format_schema_validation_error(call.name, issues),
            detail={"issues": [dict(i) for i in issues], "schema": descriptor.input_schema},
            retryable=True,
            recovery_hint={"action": "fix_arguments_and_retry", "tool_name": call.name},
        ),
    )


def resolve_execution_target(call: ToolCall, registry: ToolRegistry) -> ToolCall | ToolResult:
    """Resolve a lazy route without executing; share normalization with scheduling."""
    from ..mcp.lazy import META_CALL_TOOL, resolve_mcp_call

    descriptor = registry.find(call.name)
    prepared = replace(call, arguments=dict(call.arguments or {}))
    if descriptor is not None:
        _repair_arguments_before_validation(prepared, descriptor.input_schema)
    if call.name == META_CALL_TOOL and descriptor and "lazy_loader" in descriptor.tags:
        if error := _schema_error(prepared, descriptor):
            return error
        prepared = resolve_mcp_call(prepared, registry=registry)
        if isinstance(prepared, ToolResult):
            return prepared
        target = registry.find(prepared.name)
        if target is not None:
            _repair_arguments_before_validation(prepared, target.input_schema)
    return prepared


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
    * ``approval_timeout_sec`` — retained for compatibility. Approval
      expiry is owned by the durable approval queue.
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
        approval_resolver: Optional[ToolApprovalResolver] = None,
        pre_hooks: Optional[list[PreHook]] = None,
        post_hooks: Optional[list[PostHook]] = None,
        permission_denied_hooks: Optional[list[PermissionDeniedHook]] = None,
        options: Optional[ExecutorOptions] = None,
    ) -> None:
        self.registry = registry
        self.permission_engine = permission_engine
        self.permission_context = permission_context
        self.approval_resolver = approval_resolver
        self.pre_hooks: list[PreHook] = list(pre_hooks or [])
        self.post_hooks: list[PostHook] = list(post_hooks or [])
        self.permission_denied_hooks: list[PermissionDeniedHook] = list(
            permission_denied_hooks or []
        )
        self.options = options or ExecutorOptions()

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

        if reason := dispatch_stop_reason(call, now=time.time()):
            return skipped_before_dispatch(call, reason)
        target = resolve_execution_target(call, self.registry)
        if isinstance(target, ToolResult):
            return target
        if target.name != call.name:
            # A deny on the public routing tool must not disappear either. The
            # route itself only resolves a name; target checks own plan/risk/ASK.
            route = replace(self.registry.get(call.name), read_only=True)
            route_call = replace(call, arguments=dict(call.arguments or {}))
            _repair_arguments_before_validation(route_call, route.input_schema)
            decision = self.permission_engine.evaluate(
                PermissionRequest(
                    descriptor=route, payload=route_call.arguments,
                    caller=call.caller, turn_id=call.turn_id, iteration=call.iteration,
                ), self.permission_context,
            )
            if decision.is_deny():
                for hook in self.permission_denied_hooks:
                    try:
                        hook(route_call, route, decision)
                    except Exception:
                        _LOG.exception("permission-denied hook failed for %s", call.name)
                return ToolResult.from_error(
                    tool_use_id=call.id, name=call.name,
                    error=ToolError(
                        kind=ToolErrorKind.PERMISSION_DENIED,
                        message=decision.reason or "permission denied",
                        detail=decision.asdict(), retryable=False,
                    ),
                )
            # Hooks/approvals see the real target exactly once. Restore the model's
            # call identity only after target hooks so batch/transcript pairing works.
            result = pair_executed_results([target], [self._execute_target(target)])[0]
            return replace(result, name=call.name, metadata={
                **result.metadata,
                "via_mcp_call": True,
                "mcp_namespace": target.metadata["mcp_namespace"],
                "mcp_underlying_tool": target.name,
                "mcp_parent_call_id": call.id,
            })
        return self._execute_target(call)

    async def execute_async(self, call: ToolCall) -> ToolResult:
        """Run the same permission pipeline without nesting an event loop."""
        import asyncio

        return await asyncio.to_thread(self.execute, call)

    def _execute_target(self, call: ToolCall) -> ToolResult:
        started = time.monotonic()
        cancel_token = (call.metadata or {}).get("cancel_token")
        if _cancel_requested(cancel_token):
            return ToolResult.from_error(
                tool_use_id=call.id,
                name=call.name,
                error=ToolError(
                    kind=ToolErrorKind.ABORTED,
                    message="tool call cancelled before execution",
                    detail={"reason": _cancel_reason(cancel_token)},
                    retryable=False,
                    recovery_hint={"action": "cancelled"},
                ),
            )
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

        _repair_arguments_before_validation(call, descriptor.input_schema)
        if self.options.fail_fast_on_validation:
            if error := _schema_error(call, descriptor):
                return error

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
            resolution = self._resolve_approval(call, descriptor, decision)
            if resolution.verdict is False:
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
            if resolution.verdict is None:
                detail = decision.asdict()
                recovery_hint: dict[str, Any] = {
                    "action": "await_approval",
                    "tool_use_id": call.id,
                }
                if resolution.request:
                    approval_id = str(
                        resolution.request.get("approval_id") or ""
                    )
                    if approval_id:
                        detail["approval_id"] = approval_id
                        recovery_hint["approval_id"] = approval_id
                pending = ToolResult.from_error(
                    tool_use_id=call.id,
                    name=call.name,
                    error=ToolError(
                        kind=ToolErrorKind.PERMISSION_PENDING,
                        message=(
                            decision.approval_reason
                            or "approval required before this tool can run"
                        ),
                        detail=detail,
                        retryable=True,
                        recovery_hint=recovery_hint,
                    ),
                )
                if resolution.request:
                    pending.metadata["approval_request"] = dict(
                        resolution.request
                    )
                return pending

        for hook in self.pre_hooks:
            try:
                hook(call, descriptor, decision)
            except Exception:
                _LOG.exception("pre-hook failed for %s", call.name)

        # Approval resolution and pre-hooks can take time or request cancellation.
        # Recheck the inherited deadline/token at the last point before effects.
        if reason := dispatch_stop_reason(call, now=time.time()):
            return skipped_before_dispatch(call, reason)

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
            "tags": tuple(descriptor.tags),
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
    ) -> ToolApprovalResolution:
        if self.approval_resolver is None:
            return ToolApprovalResolution()
        try:
            return self.approval_resolver.resolve(call, descriptor, decision)
        except Exception:
            _LOG.exception("approval resolver failed for %s", call.name)
            return ToolApprovalResolution()


__all__ = [
    "ExecutorOptions",
    "NativeToolExecutor",
    "PermissionDeniedHook",
    "PostHook",
    "PreHook",
    "coerce_json_number_string",
    "resolve_execution_target",
]
