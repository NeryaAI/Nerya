"""Tool-call execution phase for the provider-native agent loop.

The main loop owns turn-level continuation policy. This module owns the bounded
mechanics between a provider ``tool_use`` response and the next transcript
observation:

* construct typed :class:`ToolCall` objects;
* enforce the per-turn call budget and advertised-tool boundary;
* suppress repeated identical calls without executing them again;
* dispatch the executable subset through :class:`ToolOrchestrator`;
* update the tool evidence / semantic-success ledger.

Keeping this phase independent from ``WorkspaceNativeAgentLoop`` makes the
execution boundary directly testable and reusable by future checkpointed turn
runtimes.
"""

from __future__ import annotations

import json
import logging
from collections import Counter
from dataclasses import dataclass, field
from typing import Any, Callable, Mapping

from ..core.redaction import redact_text
from ..tools.orchestrator import BatchResult, ToolOrchestrator
from ..tools.execution_contracts import (
    execution_unknown_result as _execution_unknown_result,
    is_read_only_tool as _tool_name_is_read_only,
    pair_executed_results as _pair_executed_results,
)
from ..tools.registry import ToolRegistry
from .loop_state import LoopRunState
from ..tools.result_contracts import (
    compacted_kept_data,
    parse_json_text,
    result_counts_as_success,
    tool_json_data,
)
from ..tools.types import (
    ToolCall,
    ToolError,
    ToolErrorKind,
    ToolResult,
)


ToolArgumentsForName = Callable[[str], dict[str, Any]]
_LOG = logging.getLogger(__name__)

@dataclass(frozen=True)
class ToolCallBuildContext:
    turn_id: str
    iteration: int
    caller: str
    session_id: str | None
    strategy_id: str | None
    trigger_event_id: str | None
    original_user_prompt: str
    deadline: float | None
    remaining_wall_seconds: float | None
    wall_time_final_synthesis_seconds: float
    cancel_token: Any = None
    argument_defaults: Mapping[str, dict[str, Any]] = field(default_factory=dict)
    metadata: Mapping[str, Any] = field(default_factory=dict)
    contract_arguments_for_tool: ToolArgumentsForName | None = None


@dataclass(frozen=True)
class ToolBatchPolicy:
    """Per-iteration capability/budget snapshot, separate from the one turn ledger."""

    allowed_tool_names: frozenset[str]
    provider_tool_names: frozenset[str]
    max_total_calls: int | None = None
    repeated_tool_window: int = 5
    repeated_tool_threshold: int = 3
    repeated_tool_stop_after: int = 2

    def __post_init__(self) -> None:
        object.__setattr__(self, "allowed_tool_names", frozenset(self.allowed_tool_names))
        object.__setattr__(self, "provider_tool_names", frozenset(self.provider_tool_names))


@dataclass(frozen=True)
class ToolBatchEffects:
    calls: tuple[ToolCall, ...]
    batch: BatchResult
    repeated_loop_abort: bool = False
    required_next_from_results: frozenset[str] = frozenset()
    semantic_success_names: frozenset[str] = frozenset()
    completed_required_action_names: frozenset[str] = frozenset()
    optional_gap_notes: tuple[str, ...] = ()


def _json_fingerprint(value: Any) -> str:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        )
    except Exception:
        return repr(value)


def tool_call_fingerprint(call: ToolCall) -> str:
    return f"{call.name}:{_json_fingerprint(call.arguments or {})}"


def _explicit_next_required_tools(
    value: Any,
    *,
    provider_tool_names: set[str],
    depth: int = 0,
    active: bool = False,
) -> set[str]:
    if depth >= 8:
        return set()
    if isinstance(value, str):
        candidate = value.strip()
        return {candidate} if active and candidate in provider_tool_names else set()
    if isinstance(value, dict):
        out: set[str] = set()
        for key_raw, nested in value.items():
            key = str(key_raw).lower()
            if (
                active
                and key in {"tool", "tool_name"}
                and isinstance(nested, str)
                and nested.strip() in provider_tool_names
            ):
                out.add(nested.strip())
                continue
            next_active = active or key == "next_required_action"
            if isinstance(nested, (dict, list, str)):
                out.update(
                    _explicit_next_required_tools(
                        nested,
                        provider_tool_names=provider_tool_names,
                        depth=depth + 1,
                        active=next_active,
                    )
                )
        return out
    if isinstance(value, list):
        out: set[str] = set()
        for nested in value:
            out.update(
                _explicit_next_required_tools(
                    nested,
                    provider_tool_names=provider_tool_names,
                    depth=depth + 1,
                    active=active,
                )
            )
        return out
    return set()


def _text_next_required_tools(
    text: str,
    *,
    provider_tool_names: set[str],
) -> set[str]:
    stripped = str(text or "").strip()
    if "next_required_action" not in stripped or not stripped.startswith(("{", "[")):
        return set()
    parsed = parse_json_text(stripped)
    if parsed is None:
        return set()
    return _explicit_next_required_tools(
        parsed,
        provider_tool_names=provider_tool_names,
    )


def extract_next_required_tools(
    results: list[ToolResult],
    *,
    provider_tool_names: set[str],
) -> set[str]:
    """Find advertised tools explicitly named by structured next-action hints."""

    if not provider_tool_names:
        return set()
    required: set[str] = set()
    for result in results:
        if result.is_error:
            if result.error is not None and result.error.recovery_hint:
                required.update(
                    _explicit_next_required_tools(
                        result.error.recovery_hint,
                        provider_tool_names=provider_tool_names,
                    )
                )
            continue
        for part in result.content:
            if part.type == "json" and part.data is not None:
                required.update(
                    _explicit_next_required_tools(
                        part.data,
                        provider_tool_names=provider_tool_names,
                    )
                )
            elif part.type == "text" and part.text:
                required.update(
                    _text_next_required_tools(
                        part.text,
                        provider_tool_names=provider_tool_names,
                    )
                )
    return required


def failed_required_actions(pending: set[str], results: list[ToolResult]) -> set[str]:
    """A failed operation may need another already-authorized tool to repair it.

    Do not discharge the requirement or bypass approvals/unknown effects. Only
    stop forcing the identical failing call. Reconstructed from the persisted
    result ledger so checkpoint continuation has the same recovery surface.
    """
    latest: dict[str, ToolResult] = {}
    for result in reversed(results):
        if result.name in pending and result.name not in latest:
            latest[result.name] = result
    recoverable: set[str] = set()
    for name, result in latest.items():
        if result.is_error:
            error = result.error
            if (error is not None and error.kind == ToolErrorKind.EXECUTION_ERROR
                    and error.detail.get("execution_state") != "unknown"):
                recoverable.add(name)
        elif not result_counts_as_success(result):
            data = tool_json_data(result)
            if isinstance(data, dict) and str(data.get("status") or data.get("state") or "").lower() in {"failed", "error", "invalid"}:
                recoverable.add(name)
    return recoverable


def truncate_tool_loop_text(text: str, *, limit: int = 1200) -> str:
    text = str(text or "").strip()
    if len(text) <= limit:
        return text
    return text[:limit].rstrip() + "\n...[truncated prior result]"


def deduped_tool_loop_result(
    call: ToolCall,
    prior: ToolResult,
    *,
    repeat_count: int,
) -> ToolResult:
    prior_text = prior.text()
    if prior.is_error and prior.error is not None:
        prior_text = prior.error.message or prior_text
    message = (
        "Repeated tool call suppressed: this exact tool and payload already "
        f"ran {repeat_count - 1} time(s) in the current turn. Use the prior "
        "result below, change the arguments, choose a different tool, or "
        "write the final answer. Do not call the same tool with the same "
        "payload again.\n\n"
        f"Prior result:\n{truncate_tool_loop_text(prior_text)}"
    )
    return ToolResult.from_error(
        tool_use_id=call.id,
        name=call.name,
        error=ToolError(
            kind=ToolErrorKind.DEDUPED,
            message=message,
            detail={
                "repeat_count": repeat_count,
                "prior_tool_use_id": prior.tool_use_id,
                "tool": call.name,
                "arguments": dict(call.arguments or {}),
            },
            retryable=False,
            recovery_hint={
                "action": "use_prior_result_or_change_args",
                "prior_tool_use_id": prior.tool_use_id,
            },
        ),
    )


def optional_tool_gap_notes(results: list[ToolResult]) -> list[str]:
    notes: list[str] = []
    for result in results:
        if result_counts_as_success(result):
            continue
        data = tool_json_data(result) or compacted_kept_data(result)
        fields: list[str] = []
        if isinstance(data, dict):
            status = str(data.get("status") or "").strip()
            error = str(data.get("error") or "").strip()
            next_required_action = str(
                data.get("next_required_action") or ""
            ).strip()
            for value in (error, status, next_required_action):
                if value and value not in fields:
                    fields.append(value)
        if not fields and result.is_error and result.error is not None:
            fields.append(result.error.message)
        if not fields:
            fields.append("no new semantic evidence")
        rendered = "; ".join(
            redact_text(value)[:180] for value in fields if value
        )
        if rendered:
            notes.append(f"{result.name or 'tool'}: {rendered}")
    return notes


def build_tool_calls(
    tool_uses: list[dict[str, Any]],
    *,
    context: ToolCallBuildContext,
) -> list[ToolCall]:
    calls: list[ToolCall] = []
    for tool_use in tool_uses:
        name = str(tool_use.get("name") or "")
        arguments = {
            **dict(context.argument_defaults.get(name, {})),
            **dict(tool_use.get("input") or {}),
        }
        if context.contract_arguments_for_tool is not None:
            contract_arguments = context.contract_arguments_for_tool(name)
            if contract_arguments:
                arguments = {**arguments, **contract_arguments}
        calls.append(
            ToolCall(
                name=name,
                arguments=arguments,
                id=str(tool_use.get("id") or ""),
                turn_id=context.turn_id,
                iteration=context.iteration,
                caller=context.caller,
                metadata={
                    **dict(context.metadata),
                    "session_id": context.session_id,
                    "strategy_id": context.strategy_id,
                    "trigger_event_id": context.trigger_event_id,
                    "original_user_prompt": context.original_user_prompt,
                    "turn_deadline_epoch": context.deadline,
                    "remaining_wall_seconds": context.remaining_wall_seconds,
                    "wall_time_final_synthesis_seconds": (
                        context.wall_time_final_synthesis_seconds
                    ),
                    "cancel_token": context.cancel_token,
                },
            )
        )
    return calls


def _budget_result(
    call: ToolCall,
    *,
    limit: int,
    used: int,
) -> ToolResult:
    return ToolResult.from_error(
        tool_use_id=call.id,
        name=call.name,
        error=ToolError(
            kind=ToolErrorKind.ABORTED,
            message=(
                "tool call skipped because the turn tool-call budget is exhausted"
            ),
            detail={"reason": "max_tool_calls", "limit": limit, "used": used},
            retryable=False,
            recovery_hint={"action": "finish_from_existing_evidence"},
        ),
    )


def _unadvertised_result(
    call: ToolCall,
    *,
    allowed_tool_names: set[str],
) -> ToolResult:
    available = sorted(allowed_tool_names)
    return ToolResult.from_error(
        tool_use_id=call.id,
        name=call.name,
        error=ToolError(
            kind=ToolErrorKind.PERMISSION_DENIED,
            message=f"tool {call.name!r} is not available in the current runtime view",
            detail={"advertised": False, "available_tools": available},
            retryable=False,
            recovery_hint={
                "action": "choose_advertised_tool",
                "available_tools": available,
            },
        ),
    )


class ToolBatchPhase:
    """Execute one provider-emitted batch and update the supplied turn ledger."""

    def __init__(
        self,
        *,
        orchestrator: ToolOrchestrator,
        registry: ToolRegistry,
    ) -> None:
        self.orchestrator = orchestrator
        self.registry = registry

    def run(
        self,
        calls: list[ToolCall],
        *,
        state: LoopRunState,
        policy: ToolBatchPolicy,
    ) -> ToolBatchEffects:
        # Identity is a protocol precondition, not a recoverable execution guess.
        id_counts = Counter(call.id for call in calls)
        if any(not call_id or count != 1 for call_id, count in id_counts.items()):
            raise ValueError("tool call ids must be non-empty and unique within a batch")
        required_before_batch = set(state.required_next_tool_names)
        state.attempted_tool_names.update(call.name for call in calls if call.name)
        prepared_results: list[ToolResult | None] = [None] * len(calls)
        executable_calls: list[ToolCall] = []
        executable_indices: list[int] = []
        pending_writes: dict[str, int] = {}
        duplicate_writes: dict[int, int] = {}
        repeated_loop_abort = False
        batch_budget_calls = 0

        for index, call in enumerate(calls):
            if (
                policy.max_total_calls is not None
                and state.total_tool_calls + batch_budget_calls
                >= policy.max_total_calls
            ):
                prepared_results[index] = _budget_result(
                    call,
                    limit=policy.max_total_calls,
                    used=state.total_tool_calls + batch_budget_calls,
                )
                continue
            batch_budget_calls += 1
            if call.name not in policy.allowed_tool_names:
                prepared_results[index] = _unadvertised_result(
                    call,
                    allowed_tool_names=policy.allowed_tool_names,
                )
                continue
            fingerprint = tool_call_fingerprint(call)
            prior_result = state.tool_result_by_fingerprint.get(fingerprint)
            repeat_count = (
                state.recent_tool_fingerprints[
                    -max(1, policy.repeated_tool_window):
                ].count(fingerprint)
                + 1
            )
            checkpoint_replay_is_unsafe = (
                fingerprint in state.checkpointed_fingerprints
                and not _tool_name_is_read_only(call.name, self.registry)
            )
            if prior_result is not None and (
                checkpoint_replay_is_unsafe
                or repeat_count >= max(2, policy.repeated_tool_threshold)
            ):
                prepared_results[index] = deduped_tool_loop_result(
                    call,
                    prior_result,
                    repeat_count=repeat_count,
                )
                deduped_count = (
                    state.deduped_counts_by_fingerprint.get(fingerprint, 0) + 1
                )
                state.deduped_counts_by_fingerprint[fingerprint] = deduped_count
                if deduped_count >= max(1, policy.repeated_tool_stop_after):
                    repeated_loop_abort = True
                continue
            if not _tool_name_is_read_only(call.name, self.registry):
                if fingerprint in pending_writes:
                    duplicate_writes[index] = pending_writes[fingerprint]
                    continue
                pending_writes[fingerprint] = index
            executable_calls.append(call)
            executable_indices.append(index)

        try:
            executed_batch = (
                self.orchestrator.run_batch(executable_calls)
                if executable_calls else BatchResult()
            )
            paired_results = _pair_executed_results(executable_calls, executed_batch.results)
        except Exception:
            _LOG.exception("tool executor failed after dispatch; effects may be incomplete")
            executed_batch = BatchResult()
            paired_results = [
                _execution_unknown_result(call, "executor raised after dispatch")
                for call in executable_calls
            ]
        for index, result in zip(executable_indices, paired_results):
            prepared_results[index] = result
            if result.error and result.error.detail.get("execution_state") == "unknown":
                state.checkpointed_fingerprints.add(tool_call_fingerprint(calls[index]))
        for index, first_index in duplicate_writes.items():
            prior = prepared_results[first_index]
            assert prior is not None
            prepared_results[index] = deduped_tool_loop_result(
                calls[index], prior, repeat_count=2,
            )
        assert all(result is not None for result in prepared_results)
        batch_results = [result for result in prepared_results if result is not None]
        batch = BatchResult(
            results=batch_results,
            total_elapsed_ms=executed_batch.total_elapsed_ms,
            parallel_calls=executed_batch.parallel_calls,
            serial_calls=executed_batch.serial_calls,
            error_count=sum(1 for result in batch_results if result.is_error),
            auto_retries=executed_batch.auto_retries,
        )

        state.completed_tool_results.extend(batch.results)
        required_next_from_results = extract_next_required_tools(
            batch.results,
            provider_tool_names=policy.provider_tool_names,
        )
        state.required_next_tool_names.update(required_next_from_results)
        self_required_next_tools = {
            result.name
            for result in batch.results
            if (
                result.name
                and not result.is_error
                and result.name in required_next_from_results
                and result.name
                in extract_next_required_tools(
                    [result],
                    provider_tool_names={result.name},
                )
            )
        }

        state.total_tool_calls += len(calls)
        state.error_count += batch.error_count
        semantic_success_names: set[str] = set()
        completed_required_action_names: set[str] = set()

        for call, result in zip(calls, batch.results):
            is_deduped = bool(result.error and result.error.kind == ToolErrorKind.DEDUPED)
            if result.name and result.is_error and not is_deduped:
                state.successful_tool_names.discard(result.name)
                semantic_success_names.discard(result.name)
            if result.name and not result.is_error:
                semantic_success = result_counts_as_success(result)
                if result.name in self_required_next_tools or not semantic_success:
                    state.successful_tool_names.discard(result.name)
                else:
                    state.successful_tool_names.add(result.name)
                    semantic_success_names.add(result.name)
                    if (
                        result.name in state.required_next_tool_names
                        and not _tool_name_is_read_only(result.name, self.registry)
                    ):
                        completed_required_action_names.add(result.name)
                    state.required_next_tool_names.discard(result.name)

            fingerprint = tool_call_fingerprint(call)
            state.recent_tool_fingerprints.append(fingerprint)
            max_recent = max(1, policy.repeated_tool_window)
            if len(state.recent_tool_fingerprints) > max_recent:
                del state.recent_tool_fingerprints[:-max_recent]
            if not is_deduped:
                state.tool_result_by_fingerprint[fingerprint] = result

        semantic_success_names.intersection_update(state.successful_tool_names)
        completed_required_action_names.intersection_update(state.successful_tool_names)
        state.required_next_tool_names.update(
            (required_before_batch | required_next_from_results) - state.successful_tool_names
        )
        if completed_required_action_names:
            for pending_name in list(state.required_next_tool_names):
                if _tool_name_is_read_only(pending_name, self.registry):
                    state.required_next_tool_names.discard(pending_name)

        return ToolBatchEffects(
            calls=tuple(calls),
            batch=batch,
            repeated_loop_abort=repeated_loop_abort,
            required_next_from_results=frozenset(required_next_from_results),
            semantic_success_names=frozenset(semantic_success_names),
            completed_required_action_names=frozenset(
                completed_required_action_names
            ),
            optional_gap_notes=tuple(optional_tool_gap_notes(batch.results)),
        )


__all__ = [
    "ToolBatchEffects",
    "ToolBatchPhase",
    "ToolBatchPolicy",
    "ToolCallBuildContext",
    "build_tool_calls",
    "deduped_tool_loop_result",
    "extract_next_required_tools",
    "optional_tool_gap_notes",
    "tool_call_fingerprint",
    "truncate_tool_loop_text",
]
