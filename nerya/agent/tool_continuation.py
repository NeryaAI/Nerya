"""Typed continuation decisions around provider-emitted tool calls.

The provider may emit a tool that was not exposed in the current iteration.
Those calls must remain paired with a structured denial for auditability, but
the decision to retry, finalize, or stop a required-action branch is pure turn
policy and does not belong in the execution phase.
"""

from __future__ import annotations

from dataclasses import dataclass

from ..core.redaction import redact_text
from ..tools.registry import ToolRegistry
from ..tools.types import RiskLevel
from .loop_state import ProviderToolSelection


@dataclass(frozen=True)
class ToolContinuationDecision:
    diagnostic: str = ""
    retry_prompt: str = ""
    final_text: str = ""
    transition_reason: str = ""


def _tool_use_is_read_only(tool_use: dict, registry: ToolRegistry) -> bool:
    descriptor = registry.find(str(tool_use.get("name") or ""))
    return bool(
        descriptor is not None
        and descriptor.read_only
        and descriptor.risk == RiskLevel.READ
    )


def provider_unoffered_tool_retry_prompt(
    *,
    allowed_tool_names: set[str],
    rejected_tool_names: list[str],
) -> str:
    allowed = ", ".join(sorted(allowed_tool_names)) or "none"
    rejected = ", ".join(name for name in rejected_tool_names if name) or "unknown"
    return (
        "Required action tool boundary: the provider returned tool call(s) "
        f"that were not exposed in this iteration: {rejected}. Available "
        f"tools for this iteration: {allowed}. Ignore the unexposed call(s), "
        "call only an available tool, or answer in text if none are available."
    )


def provider_unoffered_tool_blocked_final_text(
    *,
    allowed_tool_names: set[str],
    rejected_tool_names: list[str],
) -> str:
    allowed = ", ".join(sorted(allowed_tool_names)) or "none"
    rejected = ", ".join(name for name in rejected_tool_names if name) or "unknown"
    return (
        "The provider returned only tool call(s) that were not exposed in this "
        f"iteration: {rejected}. Available tools were: {allowed}. I did not "
        "execute the unexposed call(s); retry or continue from existing evidence."
    )


def required_action_read_only_retry_prompt(
    pending_tool_names: tuple[str, ...],
    skipped_tool_names: list[str],
) -> str:
    pending = ", ".join(pending_tool_names) or "the required action tool"
    skipped = ", ".join(name for name in skipped_tool_names if name) or "read-only tools"
    return (
        f"Required action tool(s) are still pending: {pending}. The previous "
        f"response attempted only read-only discovery tool(s): {skipped}. Stop "
        f"open-ended discovery and call {pending}, or report that it remains "
        "incomplete if it cannot be called safely."
    )


def required_action_read_only_blocked_final_text(
    pending_tool_names: tuple[str, ...],
    skipped_tool_names: list[str],
) -> str:
    pending = ", ".join(pending_tool_names) or "the required action tool"
    skipped = ", ".join(name for name in skipped_tool_names if name) or "read-only tools"
    return (
        "Stopped before more open-ended read-only discovery because required "
        f"action tool(s) remain pending: {pending}.\n\n"
        f"Skipped read-only tool(s): {skipped}.\n"
        "No additional action was completed in this turn."
    )


def required_action_wrong_tool_retry_prompt(
    pending_tool_names: tuple[str, ...],
    skipped_tool_names: list[str],
) -> str:
    pending = ", ".join(pending_tool_names) or "the required action tool"
    skipped = ", ".join(name for name in skipped_tool_names if name) or "other tools"
    return (
        f"Required action tool(s) are still pending: {pending}. The previous "
        f"response attempted different tool(s): {skipped}. Call {pending} next, "
        "or report that it remains incomplete if it cannot be called safely."
    )


def required_action_wrong_tool_blocked_final_text(
    pending_tool_names: tuple[str, ...],
    skipped_tool_names: list[str],
) -> str:
    pending = ", ".join(pending_tool_names) or "the required action tool"
    skipped = ", ".join(name for name in skipped_tool_names if name) or "other tools"
    return (
        "Stopped before unrelated tools because required action tool(s) remain "
        f"pending: {pending}.\n\nSkipped tool(s): {skipped}.\n"
        "No additional action was completed in this turn."
    )


def wall_time_late_tool_abort_text(
    tool_names: list[str],
    *,
    original_user_text: str = "",
    pending_required_tool_names: tuple[str, ...] = (),
) -> str:
    names = ", ".join(name for name in tool_names if name) or "the remaining step"
    lines = [
        "I ran out of time before the last step, so I stopped instead of "
        "starting it with too little time left — nothing was changed or saved.",
        f"Unfinished: {names}",
    ]
    request = redact_text(str(original_user_text or "").strip())
    if request:
        lines.append(f"Your request: {request}")
    pending = ", ".join(
        redact_text(str(name))
        for name in pending_required_tool_names
        if str(name).strip()
    )
    if pending:
        lines.append(f"Still needed: {pending}")
    lines.append("Ask me to continue and I'll finish from here.")
    return "\n".join(lines)


def decide_unoffered_tool_calls(
    selection: ProviderToolSelection,
    *,
    allowed_tool_names: set[str],
    registry: ToolRegistry,
    remaining_seconds: float | None,
    action_tool_reserve_seconds: float,
    total_tool_calls: int,
    has_tool_result_evidence: bool,
    pending_required_action_tools: set[str],
    pending_required_tool_names: tuple[str, ...],
    iteration: int,
    max_iterations: int,
    original_user_text: str,
) -> ToolContinuationDecision:
    """Return deferred continuation policy after structured denials are emitted."""

    if not selection.rejected:
        return ToolContinuationDecision()
    rejected_uses = list(selection.rejected)
    rejected_names = list(selection.rejected_names)
    diagnostic = (
        "Rejected provider tool call(s) not exposed in this iteration: "
        + (", ".join(name for name in rejected_names if name) or "unknown")
    )
    if not selection.only_rejected:
        return ToolContinuationDecision(diagnostic=diagnostic)

    rejected_actions = {
        str(tool_use.get("name") or "")
        for tool_use in rejected_uses
        if not _tool_use_is_read_only(tool_use, registry)
    }
    if (
        rejected_actions
        and total_tool_calls > 0
        and has_tool_result_evidence
        and remaining_seconds is not None
        and 0 < remaining_seconds <= action_tool_reserve_seconds
    ):
        return ToolContinuationDecision(
            diagnostic=diagnostic,
            final_text=wall_time_late_tool_abort_text(
                rejected_names,
                original_user_text=original_user_text,
                pending_required_tool_names=pending_required_tool_names,
            ),
            transition_reason="wall_time_final_synthesis",
        )

    if pending_required_action_tools:
        retry_key = tuple(sorted(pending_required_action_tools))
        skipped = sorted(name for name in rejected_names if name)
        read_only = all(
            _tool_use_is_read_only(tool_use, registry)
            for tool_use in rejected_uses
        )
        if iteration < max_iterations:
            return ToolContinuationDecision(
                diagnostic=diagnostic,
                retry_prompt=(
                    required_action_read_only_retry_prompt(retry_key, skipped)
                    if read_only
                    else required_action_wrong_tool_retry_prompt(retry_key, skipped)
                ),
                transition_reason=(
                    "next_required_action_read_only_retry"
                    if read_only
                    else "next_required_action_wrong_tool_retry"
                ),
            )
        return ToolContinuationDecision(
            diagnostic=diagnostic,
            final_text=(
                required_action_read_only_blocked_final_text(retry_key, skipped)
                if read_only
                else required_action_wrong_tool_blocked_final_text(retry_key, skipped)
            ),
            transition_reason=(
                "next_required_action_read_only_blocked"
                if read_only
                else "next_required_action_wrong_tool_blocked"
            ),
        )

    if iteration < max_iterations:
        return ToolContinuationDecision(
            diagnostic=diagnostic,
            retry_prompt=provider_unoffered_tool_retry_prompt(
                allowed_tool_names=allowed_tool_names,
                rejected_tool_names=rejected_names,
            ),
            transition_reason="provider_unoffered_tool_retry",
        )
    return ToolContinuationDecision(
        diagnostic=diagnostic,
        final_text=provider_unoffered_tool_blocked_final_text(
            allowed_tool_names=allowed_tool_names,
            rejected_tool_names=rejected_names,
        ),
        transition_reason="provider_unoffered_tool_blocked",
    )


__all__ = [
    "ToolContinuationDecision",
    "decide_unoffered_tool_calls",
    "provider_unoffered_tool_blocked_final_text",
    "provider_unoffered_tool_retry_prompt",
    "required_action_read_only_blocked_final_text",
    "required_action_read_only_retry_prompt",
    "required_action_wrong_tool_blocked_final_text",
    "required_action_wrong_tool_retry_prompt",
    "wall_time_late_tool_abort_text",
]
