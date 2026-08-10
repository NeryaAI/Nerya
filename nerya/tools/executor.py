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
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Optional, Union

from .permissions import (
    PermissionContext,
    PermissionDecision,
    PermissionEngine,
    PermissionRequest,
)
from .registry import ToolNotFoundError, ToolRegistry
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

PermissionPendingHook = Callable[
    [ToolCall, ToolDescriptor, PermissionDecision], None
]
"""Fired when an ASK decision has no resolved operator verdict."""

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


def _coerce_json_number_string(raw: Any, *, integer: bool = False) -> Any:
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
            normalized = _coerce_json_number_string(
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


def _raw_truncated_field_names(
    raw: str,
    schema: dict[str, Any] | None,
    parsed: dict[str, Any],
) -> list[str]:
    properties = (schema or {}).get("properties")
    if not isinstance(properties, dict):
        return []
    missing: list[str] = []
    for key in properties:
        if not isinstance(key, str) or key in parsed:
            continue
        if f'"{key}"' in raw:
            missing.append(key)
    return missing


def _recover_truncated_raw_json_object(
    raw: str,
    schema: dict[str, Any] | None,
) -> dict[str, Any] | None:
    """Recover a provider-truncated raw JSON object when required fields exist."""

    required = tuple(str(key) for key in (schema or {}).get("required") or ())
    if not required:
        return None
    cut = raw.rfind(",")
    attempts = 0
    while cut > 0 and attempts < 20:
        attempts += 1
        candidate = raw[:cut].rstrip() + "}"
        try:
            parsed = json.loads(candidate)
        except Exception:
            cut = raw.rfind(",", 0, cut)
            continue
        if isinstance(parsed, dict) and all(parsed.get(key) for key in required):
            truncated_fields = _raw_truncated_field_names(raw, schema, parsed)
            parsed["_provider_raw_truncated"] = True
            if truncated_fields:
                parsed["_provider_raw_truncated_fields"] = truncated_fields
            return parsed
        cut = raw.rfind(",", 0, cut)
    return None


def _repair_raw_json_object_argument(
    args: dict[str, Any],
    schema: dict[str, Any] | None = None,
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
        parsed = _recover_truncated_raw_json_object(raw, schema)
    if parsed is None:
        return args
    if not isinstance(parsed, dict):
        return args
    repaired = {str(k): v for k, v in parsed.items() if isinstance(k, str)}
    for key, value in args.items():
        if key != "_raw":
            repaired[key] = value
    return repaired


_STRATEGY_PACKAGE_FILE_SUFFIXES = (
    ".py",
    ".md",
    ".yml",
    ".yaml",
    ".json",
    ".toml",
)


def _looks_like_strategy_package_path(key: str) -> bool:
    normalized = str(key or "").strip().replace("\\", "/")
    if not normalized or normalized.startswith("/"):
        return False
    if normalized in {"main.py", "strategy.md", "strategy.yml", "strategy.yaml"}:
        return True
    if normalized.startswith(("tests/", "subagents/", "references/")):
        return normalized.endswith(_STRATEGY_PACKAGE_FILE_SUFFIXES)
    return False


def _strategy_manifest_value(manifest: dict[str, Any], *path: str) -> Any:
    cur: Any = manifest
    for key in path:
        if not isinstance(cur, dict):
            return None
        cur = cur.get(key)
    return cur


def _string_list(value: Any) -> list[str]:
    if isinstance(value, str):
        return [value] if value.strip() else []
    if isinstance(value, list):
        return [str(item) for item in value if str(item).strip()]
    return []


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


def _strategy_id_from_object(value: Any, keys: tuple[str, ...]) -> str | None:
    if not isinstance(value, dict):
        return None
    for key in keys:
        raw = value.get(key)
        if isinstance(raw, str) and raw.strip():
            return raw.strip()
    return None


def _repair_strategy_generate_proposal_string_arrays(
    args: dict[str, Any],
) -> dict[str, Any]:
    """Normalize common provider object items for string-array proposal args."""

    field_keys = {
        "markets": ("market", "symbol", "id", "name"),
        "accounts": ("account_id", "account", "id", "name", "label"),
    }
    repaired: dict[str, Any] | None = None
    for field, keys in field_keys.items():
        raw = args.get(field)
        if not isinstance(raw, list):
            continue
        normalized: list[Any] = []
        changed = False
        for item in raw:
            replacement = _strategy_id_from_object(item, keys)
            if replacement is not None:
                normalized.append(replacement)
                changed = True
            else:
                normalized.append(item)
        if changed:
            if repaired is None:
                repaired = dict(args)
            repaired[field] = normalized
    return repaired if repaired is not None else args


def _strategy_account_venue(value: Any) -> str | None:
    items: list[Any]
    if isinstance(value, dict):
        items = [value]
    elif isinstance(value, list):
        items = value
    else:
        return None
    for item in items:
        if not isinstance(item, dict):
            continue
        raw = item.get("venue") or item.get("provider") or item.get("exchange")
        if isinstance(raw, str) and raw.strip():
            return raw.strip()
    return None


def _strategy_market_needs_account_venue(market: str) -> bool:
    text = str(market or "").strip()
    if not text:
        return False
    if ":" not in text:
        return "/" in text
    prefix = text.split(":", 1)[0]
    return "/" in prefix


def _repair_strategy_generate_proposal_market_venues(
    args: dict[str, Any],
) -> dict[str, Any]:
    """Use an explicit account venue to canonicalize providerless markets."""

    venue = _strategy_account_venue(args.get("accounts"))
    raw_markets = args.get("markets")
    if not venue or not isinstance(raw_markets, list):
        return args

    normalized: list[Any] = []
    changed = False
    for item in raw_markets:
        if isinstance(item, str) and _strategy_market_needs_account_venue(item):
            normalized.append(f"{venue}:{item.strip()}")
            changed = True
        else:
            normalized.append(item)
    if not changed:
        return args
    return {**args, "markets": normalized}


def _repair_strategy_generate_proposal_files(
    args: dict[str, Any],
) -> dict[str, Any]:
    """Normalize provider variants for inline strategy package files."""

    files: dict[str, str] = {}
    raw_files = args.get("files")
    if isinstance(raw_files, dict):
        files.update({
            str(k).replace("\\", "/"): str(v)
            for k, v in raw_files.items()
            if isinstance(k, str) and isinstance(v, str)
        })

    changed = False
    for key, value in list(args.items()):
        if not isinstance(key, str) or not isinstance(value, str):
            continue
        rel = ""
        if key.startswith("files."):
            rel = key[len("files."):]
        elif key.startswith("file."):
            rel = key[len("file."):]
        elif _looks_like_strategy_package_path(key):
            rel = key
        if not rel:
            continue
        rel = rel.strip().replace("\\", "/")
        if not _looks_like_strategy_package_path(rel):
            continue
        files[rel] = value
        changed = True

    if not changed and (
        not isinstance(raw_files, dict) or files == raw_files
    ):
        return args
    return {**args, "files": files}


_STRATEGY_PROPOSAL_WRAPPER_KEYS = (
    "request",
    "input",
    "arguments",
    "payload",
    "proposal",
)
_STRATEGY_PROPOSAL_FIELD_KEYS = frozenset({
    "strategy_id",
    "title",
    "markets",
    "accounts",
    "files",
    "strategy_class",
    "execution_mode",
})


def _repair_strategy_generate_proposal_wrapped_object(
    args: dict[str, Any],
) -> dict[str, Any]:
    """Unwrap provider-emitted object containers for proposal arguments."""

    for wrapper_key in _STRATEGY_PROPOSAL_WRAPPER_KEYS:
        wrapped = args.get(wrapper_key)
        if isinstance(wrapped, str) and wrapped.strip().startswith("{"):
            try:
                wrapped = json.loads(wrapped)
            except Exception:
                wrapped = None
        if not isinstance(wrapped, dict):
            continue
        if not (_STRATEGY_PROPOSAL_FIELD_KEYS & {str(k) for k in wrapped}):
            continue
        repaired = {str(k): v for k, v in wrapped.items() if isinstance(k, str)}
        for key, value in args.items():
            if key != wrapper_key:
                repaired[str(key)] = value
        return repaired
    return args


def _repair_strategy_generate_proposal_manifest_fields(
    args: dict[str, Any],
) -> dict[str, Any]:
    """Backfill top-level proposal args from inline ``strategy.yml``.

    Several OpenAI-compatible providers serialize a package-shaped tool call
    with all metadata inside ``files.strategy.yml``. The handler needs those
    fields at the top level, so copy only exact manifest fields before schema
    validation. This is package normalization, not prompt/category routing.
    """

    files = args.get("files")
    if not isinstance(files, dict):
        return args
    manifest_text = ""
    for name in ("strategy.yml", "strategy.yaml"):
        raw = files.get(name)
        if isinstance(raw, str) and raw.strip():
            manifest_text = raw
            break
    if not manifest_text:
        return args
    try:
        from ..core import yaml_io

        manifest = yaml_io.loads(manifest_text, default={})
    except Exception:
        return args
    if not isinstance(manifest, dict):
        return args

    repaired = dict(args)
    if not repaired.get("strategy_id"):
        sid = manifest.get("strategy_id") or manifest.get("id")
        if sid:
            repaired["strategy_id"] = str(sid)
    if not repaired.get("title") and manifest.get("title"):
        repaired["title"] = str(manifest.get("title"))
    if not repaired.get("description") and manifest.get("description"):
        repaired["description"] = str(manifest.get("description"))
    if not repaired.get("mode") and manifest.get("mode"):
        repaired["mode"] = str(manifest.get("mode"))
    if not repaired.get("markets"):
        markets = _string_list(manifest.get("markets"))
        if markets:
            repaired["markets"] = markets
    if not repaired.get("accounts"):
        accounts = _string_list(manifest.get("accounts"))
        if accounts:
            repaired["accounts"] = accounts
    if not repaired.get("execution_mode"):
        execution_mode = (
            manifest.get("execution_mode")
            or _strategy_manifest_value(manifest, "execution", "execution_mode")
            or _strategy_manifest_value(manifest, "agent_task", "mode")
        )
        if execution_mode:
            repaired["execution_mode"] = str(execution_mode)
    if not repaired.get("strategy_class"):
        strategy_class = manifest.get("strategy_class")
        if strategy_class:
            repaired["strategy_class"] = str(strategy_class)
        elif repaired.get("execution_mode") in {"agent", "agent_task", "agent_team", "team"}:
            repaired["strategy_class"] = "agent"
    return repaired


def _repair_arguments_before_validation(
    call: ToolCall,
    schema: dict[str, Any] | None = None,
) -> None:
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
    args = _repair_raw_json_object_argument(call.arguments, schema)
    args = _repair_json_string_containers(args, schema)
    args = _repair_schema_array_item_wrappers(args, schema)
    args = _repair_schema_numeric_strings(args, schema)
    if args is not call.arguments:
        call.arguments = args

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
        args = _repair_strategy_generate_proposal_wrapped_object(args)
        if args is not call.arguments:
            call.arguments = args
        args = _repair_strategy_generate_proposal_files(args)
        if args is not call.arguments:
            call.arguments = args
        raw_files = args.get("files")
        if isinstance(raw_files, str) and raw_files.strip():
            try:
                parsed_files = json.loads(raw_files)
            except Exception:
                parsed_files = None
            if isinstance(parsed_files, dict) and all(
                isinstance(k, str) and isinstance(v, str)
                for k, v in parsed_files.items()
            ):
                call.arguments = {**args, "files": parsed_files}
                args = call.arguments
        args = _repair_strategy_generate_proposal_market_venues(args)
        if args is not call.arguments:
            call.arguments = args
        args = _repair_strategy_generate_proposal_string_arrays(args)
        if args is not call.arguments:
            call.arguments = args
        args = _repair_strategy_generate_proposal_manifest_fields(args)
        if args is not call.arguments:
            call.arguments = args

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
        approval_cb: Optional[ApprovalCallback] = None,
        pre_hooks: Optional[list[PreHook]] = None,
        post_hooks: Optional[list[PostHook]] = None,
        permission_denied_hooks: Optional[list[PermissionDeniedHook]] = None,
        permission_pending_hooks: Optional[list[PermissionPendingHook]] = None,
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
        self.permission_pending_hooks: list[PermissionPendingHook] = list(
            permission_pending_hooks or []
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

    def add_permission_pending_hook(self, hook: PermissionPendingHook) -> None:
        """Register an observer that persists an unresolved ASK decision."""

        self.permission_pending_hooks.append(hook)

    # ------------------------------------------------------------------ exec

    def execute(self, call: ToolCall) -> ToolResult:
        """Synchronous execute. Awaitable handlers are resolved via
        ``asyncio.run`` when needed (caller must not be in an event
        loop). For async-native callers see :meth:`execute_async`."""

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
                for hook in self.permission_pending_hooks:
                    try:
                        hook(call, descriptor, decision)
                    except Exception:
                        _LOG.exception(
                            "permission-pending hook failed for %s", call.name,
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
    ) -> Optional[bool]:
        if call.id in self.permission_context.approved_calls:
            # Compatibility escape hatch for callers that pre-populate the
            # context. It is deliberately one-shot; durable approvals are
            # resolved by ``approval_cb`` and must not become an in-memory
            # reusable grant.
            self.permission_context.approved_calls.discard(call.id)
            return True
        if call.id in self.permission_context.rejected_calls:
            self.permission_context.rejected_calls.discard(call.id)
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
            return True
        if verdict is False:
            return False
        return None


__all__ = [
    "ApprovalCallback",
    "ExecutorOptions",
    "NativeToolExecutor",
    "PermissionPendingHook",
    "PermissionDeniedHook",
    "PostHook",
    "PreHook",
]
