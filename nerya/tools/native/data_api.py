"""Native tool wrapper for provider-specific read-only data APIs."""

from __future__ import annotations

from datetime import datetime, timezone
import json
import re
from typing import Any

from ...harness.cancellation import CancelledError
from ...data_api import (
    DataApiContext,
    DataApiError,
    DataApiRegistry,
    build_data_api_registry,
    compact_data_result,
)
from ..types import ToolCall, ToolError, ToolErrorKind, ToolResult


DATA_API_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "op": {
            "type": "string",
            "enum": ["list", "schema", "call", "snapshots"],
            "description": "Use list to discover actions, schema for inputs, call for read-only acquisition, snapshots to recover this task's saved producer refs without network access.",
        },
        "provider": {
            "type": "string",
            "description": "Provider namespace, e.g. akshare, wallet, onchainos.",
        },
        "action": {
            "type": "string",
            "description": "Exact read-only action from op=list; financial component execution actions are not data routes. Inspect inputs with op=schema, not shell commands or --help.",
        },
        "query": {
            "type": "string",
            "description": "Optional search text for op=list.",
        },
        "tags": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Optional tag filter for op=list.",
        },
        "args": {
            "type": "object",
            "additionalProperties": True,
            "description": "JSON object matching op=schema for provider.action. cli_read requires an allowlisted command string and a params object, not shell text or argv.",
        },
        "limit": {
            "type": "integer",
            "minimum": 1,
            "maximum": 500,
            "default": 50,
            "description": "Max actions for list or max rows returned for call; does not expand upstream history or prove time coverage.",
        },
        "columns": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Optional column projection for table-like call results.",
        },
    },
    "required": ["op"],
}

_CONTROL_ACTIONS = frozenset({"list", "schema", "call"})
_SAFE_PREVIEW_FIELDS = ("provider", "action", "title", "output_kind")


def data_api_handler(
    call: ToolCall,
    *,
    config_like: Any | None = None,
    registry: DataApiRegistry | None = None,
) -> ToolResult:
    args = dict(call.arguments or {})
    op = str(args.get("op") or "").strip().lower()
    reg = registry or build_data_api_registry()
    context = DataApiContext(config_like=config_like, cancel_token=(call.metadata or {}).get("cancel_token"))
    if op in ("call", "schema"):
        missing = [
            field
            for field in ("provider", "action")
            if str(args.get(field) or "").strip() == ""
        ]
        if missing:
            return _missing_route_error(call, reg, op=op, missing=missing)
    try:
        if op == 'snapshots':
            from ...data_api.replay_snapshot import list_snapshots
            rows = list_snapshots(config_like, reg, provider=str(args.get('provider') or ''),
                                  action=str(args.get('action') or ''))
            return ToolResult.from_json(tool_use_id=call.id, name=call.name,
                                        data={'snapshots': rows, 'count': len(rows), 'scope': 'current_task'})
        if op == "list":
            payload = reg.list(
                provider=args.get("provider"),
                query=str(args.get("query") or ""),
                tags=args.get("tags") if isinstance(args.get("tags"), list) else (),
                limit=_limit(args.get("limit"), default=20, maximum=100),
            )
            return ToolResult.from_json(tool_use_id=call.id, name=call.name, data=payload)
        if op == "schema":
            provider = _required(args, "provider")
            action = _required(args, "action")
            payload = reg.schema(provider, action)
            return ToolResult.from_json(tool_use_id=call.id, name=call.name, data=payload)
        if op == "call":
            provider = _required(args, "provider")
            action = _required(args, "action")
            if (
                provider.lower() == "wallet"
                and action.lower() in {"capability_catalog", "meme_strategy_guide"}
                and "limit" in args
            ):
                return _error(
                    call,
                    ToolErrorKind.SCHEMA_VALIDATION,
                    (
                        "limit does not expand wallet catalog/guide object results. "
                        "Use the compact next_required_action and bounded_sequence "
                        "already returned. For read-only lookup, call the selected "
                        "market_data/data_api route and summarize. For strategy "
                        "authoring, read strategy_author with skill_view and move "
                        "to strategy_draft_proposal, then edit the staged proposal "
                        "files with SDK code instead of repeating catalog discovery."
                    ),
                    detail={"provider": provider, "action": action},
                    retryable=False,
                )
            payload_args = args.get("args")
            if payload_args is None:
                payload_args = args.get("payload")
            if payload_args is None:
                payload_args = {}
            if not isinstance(payload_args, dict):
                raise DataApiError("data_api args must be an object", kind="schema_validation", retryable=False)
            from ..executor import _validate_against_schema
            from ..tool_errors import collect_schema_issues
            schema = reg.schema(provider, action).get("input_schema") or {}
            schema_error = _validate_against_schema(payload_args, schema)
            if schema_error:
                issues = collect_schema_issues(payload_args, schema)
                raise DataApiError(schema_error, kind="schema_validation", retryable=False,
                                   detail={"field": issues[0].get("field"),
                                           "input_schema": schema, "issues": issues})
            raw = reg.call(provider, action, payload_args, context=context)
            if reg.resolve_provider(provider) == "defillama":
                from .financial import _lending_pool_identity
                if action.lower() == "yield_pools":
                    raw = {**raw, "rows": [
                        {**row, "canonical_identity": _lending_pool_identity(row)}
                        for row in raw["rows"]
                    ]}
                elif action.lower() == "yield_history":
                    envelope = raw["_envelope"]
                    pool = envelope["pool"]
                    raw = {**raw, "_envelope": {**envelope, "pool": {
                        **pool, "canonical_identity": _lending_pool_identity(pool),
                    }}}
            producer_ref = None
            if reg.schema(provider, action).get('replay_input') and config_like is not None:
                from ...data_api.replay_snapshot import save
                producer_ref = save(config_like, call, reg.resolve_provider(provider),
                                    reg._action_name(provider, action), payload_args, raw)
            payload = compact_data_result(
                provider,
                action,
                raw,
                limit=_limit(args.get("limit"), default=50, maximum=500),
                columns=args.get("columns") if isinstance(args.get("columns"), list) else (),
            )
            _add_time_extent_hint(payload, payload_args)
            if producer_ref:
                payload['producer_ref'] = producer_ref
            return ToolResult.from_json(tool_use_id=call.id, name=call.name, data=payload)
        return _error(
            call,
            ToolErrorKind.SCHEMA_VALIDATION,
            "data_api op must be one of list, schema, call",
            detail={"op": args.get("op")},
            retryable=False,
        )
    except CancelledError:
        raise
    except DataApiError as exc:
        detail = exc.detail if isinstance(exc.detail, dict) else {}
        detail = {
            **detail,
            "op": op,
            "provider": args.get("provider"),
            "action": args.get("action"),
        }
        recovery_hint: dict[str, Any] = {}
        error_message = exc.message
        if (op == 'call' and args.get('provider') == 'uniswap'
                and args.get('action') == 'pool_discover' and exc.kind == 'provider_error'
                and isinstance(detail.get('stage'), str)
                and detail.get('stage') in {'deadline', 'transport', 'request_budget', 'reorg',
                    'identity', 'http_status', 'response_size', 'response_envelope', 'rpc_error'}):
            stage = detail['stage']
            safe = {'stage': stage}
            count = detail.get('request_count')
            if type(count) is int and 0 <= count <= 64:
                safe['request_count'] = count
            method = detail.get('rpc_method')
            if isinstance(method, str) and method in {'eth_chainId', 'eth_blockNumber', 'eth_getBlockByNumber', 'eth_call'}:
                safe['rpc_method'] = method
            message = ('Pool identity discovery did not complete. Do not retry unchanged or change '
                'arguments just to evade duplicate-call protection. Preserve the requested assets and '
                'window; inspect the bounded provider diagnostic before recovery. This is not proof '
                'that historical data is unavailable, and grants no trading permission.')
            recovery_hint = {'action': 'inspect_pool_discovery', 'safe_to_retry': False,
                'preserve_candidate': True, 'preserve_requested_constraints': True, **safe, 'hint': message}
            error_message = message + ' Diagnostic: ' + json.dumps(safe, sort_keys=True)
            detail = {'provider': 'uniswap', 'action': 'pool_discover', 'op': op, **safe}
        elif detail.get('reason') == 'historical_rpc_authorization_required':
            recovery_hint = {
                'action':'inspect_historical_sources', 'reason':'historical_rpc_authorization_required',
                'safe_to_retry':False, 'preserve_candidate':True,
                'preserve_requested_constraints':True,
                'fixed_public_source':True, 'credential_input_supported':False,
                'message':'Historical RPC access was denied; archive access is not established. The fixed '
                    'data action supports only its documented public RPC providers and accepts no API key, vault, '
                    'custom URL or endpoint. Retrying the same request requires changed preconditions. '
                    'You may autonomously select another supported, verified public read-only source '
                    '(including rpc_provider) while preserving the candidate, requested pool/window '
                    'and evidence requirements. Do not seek credentials, bypass access denial or trade.',
            }
            error_message = recovery_hint['message']
        elif detail.get("reason") in ("historical_rpc_evidence_failure", "historical_rpc_rate_limited"):
            from ...data_api.uniswap_history import MAX_REQUESTS, RPC_METHODS

            safe = {}
            stage = detail.get("failure_stage")
            if isinstance(stage, str) and stage in {"http_status", "rpc_error", "response_envelope",
                    "missing_result", "transport", "deadline", "request_budget", "response_size"}:
                safe["failure_stage"] = stage
            method = detail.get("rpc_method")
            if isinstance(method, str) and method in RPC_METHODS:
                safe["rpc_method"] = method
            for field, lower, upper in (("rpc_error_code", -(2**31), 2**31 - 1),
                    ("http_status", 100, 599), ("request_count", 0, MAX_REQUESTS)):
                value = detail.get(field)
                if type(value) is int and lower <= value <= upper:
                    safe[field] = value
            recovery_hint = {
                "action": "inspect_historical_diagnostic",
                "reason": detail["reason"],
                "safe_to_retry": False, "preserve_candidate": True,
                "preserve_requested_constraints": True, **safe,
            }
            message = ('The historical read produced no verified historical state. Do not retry unchanged '
                'failed queries. Preserve the original source receipt and bounded diagnostic, candidate '
                'and requested chain/product/pool/window. Use corrected schema-supported inputs or '
                'investigate compatible public read-only sources to address the specific failure; '
                'report remaining evidence gaps.')
            error_message = message + ' Diagnostic: ' + json.dumps(safe, sort_keys=True)
        elif exc.kind == "schema_validation" and op == "call":
            recovery_hint = {
                "action": "inspect_schema",
                "tool": "data_api",
                "arguments": {"op": "schema", "provider": args.get("provider"), "action": args.get("action")},
                "hint": (
                    "Read this exact action schema and correct the specific invalid fields using its "
                    "types and allowed values; cli_read command is an enum string, not shell text, "
                    "argv or --help. If a corrected call fails, inspect the specific new error before "
                    "further correction. Do not repeat unchanged failed queries."
                ),
            }
            detail["next_required_action"] = recovery_hint
        elif exc.kind == "not_found" and "resolved_provider" in detail:
            confusion = _operation_confusion_diagnostic(
                reg, args, op=op, detail=detail, original_message=exc.message
            )
            if confusion is not None:
                error_message, recovery_hint = confusion
            else:
                known_provider = detail["resolved_provider"] in reg.providers()
                recovery_hint = {
                    "action": "discover_data_actions",
                    "tool": "data_api",
                    "arguments": {"op": "list", **({"provider": args.get("provider")} if known_provider else {})},
                    "hint": (
                        "data_api is read-only. Discover an installed data action, then inspect "
                        "its op=schema before calling. financial_components describes financial "
                        "execution capabilities, not data_api provider/action routes; a swap "
                        "capability supplies no historical data or execution authorization. "
                        "Do not retry an unchanged unknown route."
                    ),
                }
        if not recovery_hint and op == "call" and exc.retryable is False:
            recovery_hint = _access_denial_recovery(detail)
        return _error(
            call,
            _tool_error_kind(exc.kind),
            error_message,
            detail=detail,
            # A rejected read can be corrected; no operation was submitted.
            retryable=True if exc.kind == "schema_validation" and op == "call" else exc.retryable,
            recovery_hint=recovery_hint,
        )
    except Exception as exc:
        from ...wallet.errors import WalletDependencyError, WalletPolicyDenied, WalletProviderNotFound, WalletTransportError
        if isinstance(exc, (WalletPolicyDenied, WalletProviderNotFound)):
            return _error(call, ToolErrorKind.PERMISSION_DENIED if isinstance(exc, WalletPolicyDenied) else ToolErrorKind.NOT_FOUND,
                          str(exc), retryable=False, detail={"provider": args.get("provider"), "action": args.get("action")},
                          recovery_hint={"action": "inspect_configuration", "hint": "Correct the provider, RPC, binding or policy before retrying; do not resend unchanged."})
        if isinstance(exc, WalletDependencyError):
            recovery_action = ("configure_credentials" if any(item.startswith("cred:") for item in exc.missing)
                               else "wallet_auth" if any(item.startswith(("login:", "status:")) for item in exc.missing)
                               else "wallet_install")
            return _error(call, ToolErrorKind.PROVIDER_ERROR, exc.args[0], retryable=False,
                          detail={"provider": exc.provider, "missing": exc.missing, "install_hint": exc.install_hint},
                          recovery_hint={"action": recovery_action, "provider": exc.provider,
                                         "hint": exc.install_hint})
        if isinstance(exc, WalletTransportError) and any(
            marker in str(exc).lower() for marker in ("not logged in", "invalid authority", "login_required", "session expired")
        ):
            return _error(call, ToolErrorKind.PROVIDER_ERROR, "Wallet authentication is required; retrying the same read will not log in.",
                          retryable=False, detail={"provider": provider, "action": action},
                          recovery_hint={"action": "wallet_auth", "hint": "Check status, resume the pending login with verify, or start a login if none exists."})
        return _error(
            call,
            ToolErrorKind.PROVIDER_ERROR,
            str(exc),
            detail={"op": op, "provider": args.get("provider"), "action": args.get("action")},
            retryable=None,
        )


def _operation_confusion_diagnostic(
    reg: DataApiRegistry,
    args: dict[str, Any],
    *,
    op: str,
    detail: dict[str, Any],
    original_message: str,
) -> tuple[str, dict[str, Any]] | None:
    """Explain control-operation/action confusion without dispatching a guess."""
    action = str(args.get("action") or "").strip().lower()
    if op not in {"schema", "call"} or action not in _CONTROL_ACTIONS:
        return None

    resolved_provider = str(detail.get("resolved_provider") or "").strip().lower()
    provider_names = set(reg.providers())
    if resolved_provider not in provider_names:
        return None
    provider = str(args.get("provider") or "").strip()
    known_actions = _installed_action_metadata(reg, resolved_provider)
    action_names = {row["action"] for row in known_actions}

    candidates: list[tuple[str, str]] = []
    nested = args.get("args")
    if isinstance(nested, dict):
        nested_action = _catalog_token(nested.get("action"))
        if nested_action:
            candidates.append(("args.action", nested_action))
    query = _catalog_token(args.get("query"))
    if query:
        candidates.append(("query", query))
    matched = {value for _source, value in candidates if value in action_names}
    if len(matched) == 1:
        candidate = next(iter(matched))
        source = next(source for source, value in candidates if value == candidate)
        route: dict[str, Any] = {
            "op": op,
            "provider": provider,
            "action": candidate,
        }
        if op == "call":
            route["args"] = {}
        hint = {
            "action": "inspect_operation_shape",
            "tool": "data_api",
            "candidate_source": source,
            "candidate": f"{resolved_provider}.{candidate}",
            "arguments": route,
            "safe_to_execute": False,
            "hint": (
                "The request confused data_api's control op with the provider action. "
                "Use the exact route in arguments after checking the action schema; this "
                "diagnostic does not execute or auto-remap the inferred action."
            ),
        }
        message = (
            f"{original_message}. data_api 'op' is the control operation and 'action' is the "
            f"installed provider action; action={action!r} is a control word. The unique "
            f"installed action identified from {source} is {resolved_provider}.{candidate}. "
            f"Do not execute or auto-remap this request. Use this exact shape after inspecting "
            f"the schema: {json.dumps(route, ensure_ascii=False, sort_keys=True)}"
        )
        return message, hint

    preview = known_actions[:8]
    discovery = {
        "op": "list",
        "provider": provider,
    }
    hint = {
        "action": "discover_data_actions",
        "tool": "data_api",
        "arguments": discovery,
        "installed_actions": preview,
        "safe_to_execute": False,
        "hint": (
            "No unique installed action was identified from args.action or query. Use the "
            "explicit op=list shape in arguments, then op=schema for one returned action; "
            "do not guess or auto-remap the control word."
        ),
    }
    message = (
        f"{original_message}. data_api 'op' is the control operation and 'action' is the "
        f"installed provider action; action={action!r} is a control word. No unique known "
        "action was identified from args.action or query. Use op='list' with the provider, "
        f"then op='schema' before op='call'. Installed action preview: "
        f"{json.dumps(preview, ensure_ascii=False, sort_keys=True)}. Do not guess or auto-remap."
    )
    return message, hint


def _installed_action_metadata(reg: DataApiRegistry, provider: str) -> list[dict[str, Any]]:
    actions = getattr(reg, "_actions", {})
    if not isinstance(actions, dict):
        return []
    rows: list[dict[str, Any]] = []
    for (registered_provider, _registered_action), spec in sorted(actions.items()):
        if registered_provider != provider:
            continue
        try:
            preview = spec.preview()
        except Exception:
            continue
        rows.append({key: preview[key] for key in _SAFE_PREVIEW_FIELDS if key in preview})
    return rows


def _catalog_token(value: Any) -> str:
    if not isinstance(value, str):
        return ""
    token = value.strip().lower()
    return token if token and len(token) <= 80 and token.replace("_", "").isalnum() else ""


def _add_time_extent_hint(payload: dict[str, Any], args: dict[str, Any]) -> None:
    """Describe only observed returned rows, never infer requested coverage."""
    if payload.get("kind") != "table":
        return
    rows = payload["rows"]
    timestamps = [
        parsed for row in rows if isinstance(row, dict)
        if (parsed := _timestamp_utc(row.get("timestamp"))) is not None
    ]
    params = args.get("params") if isinstance(args.get("params"), dict) else {}
    requested = args.get("time_range", params.get("time_range"))
    if not timestamps and requested is None:
        return
    start, end = (min(timestamps), max(timestamps)) if timestamps else (None, None)
    # next_required_action already survives tool-result compaction. Keep the
    # extent beside its interpretation so a five-row sample cannot hide it.
    payload["next_required_action"] = {
        "action": "assess_observed_coverage",
        "requested_time_range": requested,
        "observed_time_extent": {
            "scope": "returned_rows",
            "timestamp_count": len(timestamps),
            "row_count": len(rows),
            "start_utc": start.isoformat() if start else None,
            "end_utc": end.isoformat() if end else None,
            "span_seconds": (end - start).total_seconds() if start and end else None,
        },
        "coverage_verified": False,
        "hint": (
            "Compare observed timestamps with the required window. Requested time_range "
            "and row limit do not prove coverage; extent does not prove continuous samples. "
            "Follow schema-supported pagination/history parameters to acquire the requested window; "
            "if exact source cannot supply it, investigate compatible public sources while preserving "
            "chain/product/window. Do not repeat unchanged failed queries or fabricate snapshots."
        ),
    }


def _timestamp_utc(value: Any) -> datetime | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        seconds = float(value)
        if abs(seconds) >= 100_000_000_000:  # Unix milliseconds
            seconds /= 1000
        return datetime.fromtimestamp(seconds, timezone.utc)
    except (TypeError, ValueError, OverflowError, OSError):
        try:
            parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
            return parsed.astimezone(timezone.utc) if parsed.tzinfo else None
        except ValueError:
            return None


def _missing_route_error(
    call: ToolCall,
    reg: DataApiRegistry,
    *,
    op: str,
    missing: list[str],
) -> ToolResult:
    """Self-correcting error for op=call/schema with no provider/action.

    The model frequently emits a bare ``{"op": "call"}`` and then retries
    the identical empty call (which the dedupe guard suppresses), so the
    terse "missing required field" was producing back-to-back failures.
    Return the available providers/aliases and the exact discovery path so
    the next call is valid instead of a repeat.
    """

    try:
        providers = reg.providers()
    except Exception:
        providers = []
    try:
        aliases = reg.aliases()
    except Exception:
        aliases = {}
    example_provider = providers[0] if providers else None
    return _error(
        call,
        ToolErrorKind.SCHEMA_VALIDATION,
        (
            f"data_api op={op!r} needs both 'provider' and 'action' "
            f"(missing: {', '.join(missing)}). There is no default route — "
            "discover one first: call op='list' (optionally with a provider "
            "to narrow), then op='schema' to inspect an action's inputs, then "
            "op='call' with provider + action + args. Do not resend this "
            "exact call with empty arguments."
        ),
        detail={
            "op": op,
            "missing": missing,
            "providers": providers,
            "aliases": aliases,
            "example_next_call": (
                {"op": "list", "provider": example_provider}
                if example_provider
                else {"op": "list"}
            ),
        },
        retryable=False,
        recovery_hint={
            "action": "data_api_list_first",
            "hint": "Run data_api op='list' to discover provider/action, then call.",
            "providers": providers[:40],
        },
    )


def _required(args: dict[str, Any], key: str) -> str:
    value = args.get(key)
    if value is None or str(value).strip() == "":
        raise DataApiError(
            f"missing required field: {key}",
            kind="schema_validation",
            detail={"field": key},
            retryable=False,
        )
    return str(value)


def _limit(value: Any, *, default: int, maximum: int) -> int:
    try:
        return max(1, min(int(value), maximum))
    except (TypeError, ValueError):
        return default


def _tool_error_kind(kind: str) -> ToolErrorKind:
    mapping = {
        "schema_validation": ToolErrorKind.SCHEMA_VALIDATION,
        "not_found": ToolErrorKind.NOT_FOUND,
        "provider_error": ToolErrorKind.PROVIDER_ERROR,
        "execution_error": ToolErrorKind.EXECUTION_ERROR,
    }
    return mapping.get(kind, ToolErrorKind.PROVIDER_ERROR)


def _access_denial_recovery(detail: dict[str, Any]) -> dict[str, Any]:
    status = detail.get("http_status")
    if type(status) is not int or status not in {401, 403}:
        return {}
    diagnostics: dict[str, Any] = {"http_status": status}
    for key in ("provider", "action", "error_category", "error_name"):
        value = detail.get(key)
        if isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9_-]{1,64}", value):
            diagnostics[key] = value
    code = detail.get("error_code")
    if type(code) is int and -(2**31) <= code < 2**31:
        diagnostics["error_code"] = code
    owner_action = detail.get("owner_action_required")
    if type(owner_action) is bool:
        diagnostics["owner_action_required"] = owner_action
    return {"action": "inspect_configuration", "safe_to_retry": False,
            "diagnostics": diagnostics}


def _error(
    call: ToolCall,
    kind: ToolErrorKind,
    message: str,
    *,
    detail: dict[str, Any] | None = None,
    retryable: bool | None = None,
    recovery_hint: dict[str, Any] | None = None,
) -> ToolResult:
    return ToolResult.from_error(
        tool_use_id=call.id,
        name=call.name,
        error=ToolError(
            kind=kind,
            message=message,
            detail=dict(detail or {}),
            retryable=retryable,
            recovery_hint=dict(recovery_hint or {}),
        ),
    )
