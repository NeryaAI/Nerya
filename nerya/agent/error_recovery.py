"""Error taxonomy + recovery hints for the workspace-native agent loop.

Plan refs:
- ``docs/coding-agent-and-file-tools-improvement-plan.md`` §4.7
- Mirrors Claude Code's principle that *every* error the model sees
  must be (a) classified and (b) accompanied by an actionable next
  step. Without this, models loop on the same error forever; with it,
  they self-recover ~80% of the time.

Why
---
The existing :mod:`nerya.core.error_classifier` produces an internal
category (``timeout``, ``rate_limit``, ``validation``, ``internal``,
…). That is enough for retry policy, but not enough for the LLM to
know *what to do next*.

This module adds a second, model-facing taxonomy:

* ``stale_file_read``  → "call ``read_file`` again on this exact path"
* ``patch_context_mismatch``  → "the find string is no longer present;
   re-read the file and re-anchor your edit"
* ``approval_pending``  → "the operator must approve this destructive
   step; either wait or restate the request as read-only"
* ``permission_denied``  → "this skill is not allowed in the current
   lane; pick a different action or escalate"
* ``budget_exceeded``  → "the wall-clock / call budget is exhausted;
   stop calling tools and wrap up"
* ``ambiguous_input``  → "the action requires X but the payload was
   missing/wrong; ask the user or default and retry"
* ``transient`` (timeout / rate_limit / 5xx)  → "wait + retry"
* ``unrecoverable``  → "report the failure to the user; do not retry"

Each entry carries a ``recovery_hint`` string that the kernel injects
into the synthetic observation so the next planner step has an
explicit instruction. The dashboard renders the same taxonomy as a
visible status badge ("read-stale", "approval needed", …).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional

from .file_state import StaleFileReadError


__all__ = [
    "ErrorVerdict",
    "RecoveryAction",
    "RetryPolicy",
    "classify_for_recovery",
    "policy_for_kind",
    "RECOVERY_HINTS",
    "RETRY_POLICY",
]


# ---- canonical hint strings -------------------------------------------------

RECOVERY_HINTS: dict[str, str] = {
    "stale_file_read": (
        "The file changed (or has not been read) since you last looked. "
        "Call operator.read_file on this exact path BEFORE retrying any "
        "edit so your find/replace anchors against the current bytes."
    ),
    "patch_context_mismatch": (
        "The find/context string is no longer present in the file. "
        "Re-read the file with operator.read_file (no slicing) and "
        "re-anchor your edit on a string that still exists; do not "
        "guess the new contents."
    ),
    "approval_pending": (
        "This action requires operator approval. Either wait for the "
        "approval to land in your observation feed, restate the request "
        "as a read-only/dry-run action, or send a message to the user "
        "explaining what approval you need and why."
    ),
    "permission_denied": (
        "The current lane does not permit this skill/action. Pick a "
        "different action that is in the allow-list, or send a message "
        "to the user asking them to switch lanes."
    ),
    "budget_exceeded": (
        "The per-turn budget is exhausted. STOP calling tools — send a "
        "single message.send_message summarising what you did so far, "
        "what is still pending, and how the user should resume."
    ),
    "ambiguous_input": (
        "The action rejected the payload because a required field was "
        "missing or wrongly typed. Re-read the action description, fix "
        "the payload, and retry once. Do not loop."
    ),
    "not_query_only": (
        "This action's name pattern is not read-only so it cannot join "
        "a parallel batch. Call it serially via the normal action "
        "channel."
    ),
    "deduped": (
        "You have already called this exact action+payload in this "
        "turn; the prior result is attached. Do NOT re-issue the same "
        "call — use the result you already have."
    ),
    "transient": (
        "Transient infrastructure failure (timeout / rate_limit / 5xx). "
        "Wait briefly and retry the same action up to one more time. If "
        "it fails again, report the issue to the user and stop."
    ),
    "unrecoverable": (
        "This failure is unrecoverable from the agent loop. Send a "
        "message.send_message to the user with the error and stop."
    ),
    "stale_file_required_read": (
        "operator.edit_file refused because you have not read this "
        "file in the current session. Run operator.read_file on the "
        "exact path first."
    ),
    "destructive_refused": (
        "Command was refused because it matched a destructive pattern "
        "(rm -rf, dd, mkfs, sudo, fork-bomb, raw block-device write). "
        "Re-issue a narrower command or escalate to the operator."
    ),
    "tool_not_found": (
        "The named tool is not registered. Call resource_list / "
        "tool listings (or re-read the available-tools header) and "
        "pick one that exists."
    ),
    "aborted": (
        "The call was aborted (operator cancellation, kill switch, "
        "or signal). Do not retry the same call; either send a "
        "send_message explaining the cancellation or wait for the "
        "next operator instruction."
    ),
    "sandbox_denied": (
        "The sandbox layer refused this command. Either narrow the "
        "command (no sudo, no destructive flags) or escalate to the "
        "operator with send_message."
    ),
    "provider_error": (
        "The downstream provider/API returned an error. Wait briefly "
        "and retry once. If the same error recurs, send_message to "
        "the user with the provider's error text and stop."
    ),
    "mcp_session_expired": (
        "The MCP server session expired. The harness will reconnect "
        "and retry once automatically; if you see this in your tool "
        "result, the retry already happened — pick a different tool "
        "or wait for the next turn."
    ),
    "diff_conflict": (
        "The diff/patch could not be applied because the surrounding "
        "context drifted. Re-read the file with operator.read_file "
        "and re-author the patch against the current bytes."
    ),
}


@dataclass
class ErrorVerdict:
    """Recovery-flavoured classification of a tool-call failure."""

    category: str
    recoverable: bool
    recovery_hint: str
    retry_after_s: float = 0.0
    raw_kind: str = ""
    payload: dict[str, Any] | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "category": self.category,
            "recoverable": self.recoverable,
            "recovery_hint": self.recovery_hint,
            "retry_after_s": self.retry_after_s,
            "raw_kind": self.raw_kind,
            **({"payload": self.payload} if self.payload else {}),
        }


def _hint(category: str) -> str:
    return RECOVERY_HINTS.get(category) or RECOVERY_HINTS["unrecoverable"]


def classify_for_recovery(
    *,
    error_kind: str | None,
    error_message: str | None = None,
    exception: BaseException | None = None,
    extra: dict[str, Any] | None = None,
) -> ErrorVerdict:
    """Map a tool-call failure to a recovery verdict.

    ``error_kind`` is the harness-level classification produced by
    :class:`ToolRunner` (``timeout``, ``rate_limit``, ``validation``,
    ``budget``, ``deduped``, …). ``exception`` is optional and lets us
    pull richer context out of well-known exceptions like
    :class:`StaleFileReadError`.
    """

    extra = extra or {}
    msg = (error_message or "").lower()

    # 0) StaleFileReadError carries everything we need to construct a
    #    precise hint with the path embedded. Check first because the
    #    raw kind may have been classified as "validation".
    if isinstance(exception, StaleFileReadError):
        payload = exception.as_dict()
        return ErrorVerdict(
            category="stale_file_read",
            recoverable=True,
            recovery_hint=(
                f"{_hint('stale_file_read')} Path: {exception.path}."
            ),
            raw_kind=error_kind or "stale_file_read",
            payload=payload,
        )

    # 1) Direct mappings on harness kind. Keys cover both the legacy
    #    error_classifier strings ("timeout", "validation", …) and the
    #    new ``ToolErrorKind`` enum values ("schema_validation",
    #    "mcp_session_expired", …). Anything missing falls through to
    #    the message-pattern sniff and ultimately ``unrecoverable``.
    direct = {
        # transient infrastructure
        "timeout": "transient",
        "rate_limit": "transient",
        "retryable": "transient",
        "provider_error": "provider_error",
        "mcp_session_expired": "mcp_session_expired",
        # budget / dedup / batch shape
        "budget": "budget_exceeded",
        "deduped": "deduped",
        "not_query_only": "not_query_only",
        # input / schema
        "validation": "ambiguous_input",
        "schema_validation": "ambiguous_input",
        # approval / permissions
        "approval_pending": "approval_pending",
        "permission_pending": "approval_pending",
        "approval_denied": "unrecoverable",
        "permission_denied": "permission_denied",
        # discovery
        "tool_not_found": "tool_not_found",
        "not_found": "tool_not_found",
        # sandbox / abort
        "sandbox_denied": "sandbox_denied",
        "aborted": "aborted",
        # file / diff
        "stale_file": "stale_file_read",
        "diff_conflict": "diff_conflict",
        # generic execution failure
        "execution_error": "unrecoverable",
    }
    if error_kind in direct:
        cat = direct[error_kind]
        retry_after = 0.0
        if cat == "transient":
            retry_after = 0.5
        elif cat == "mcp_session_expired":
            retry_after = 0.25
        elif cat == "provider_error":
            retry_after = 0.5
        return ErrorVerdict(
            category=cat,
            recoverable=cat
            not in {"unrecoverable", "tool_not_found", "aborted",
                    "sandbox_denied", "destructive_refused"},
            recovery_hint=_hint(cat),
            retry_after_s=retry_after,
            raw_kind=error_kind,
        )

    # 2) Pattern sniff on the message — covers cases where the kind
    #    came back as "internal" but the text reveals a known shape.
    if msg:
        if "context mismatch" in msg or "find pattern not present" in msg:
            return ErrorVerdict(
                category="patch_context_mismatch",
                recoverable=True,
                recovery_hint=_hint("patch_context_mismatch"),
                raw_kind=error_kind or "internal",
            )
        if "destructive" in msg and "refus" in msg:
            return ErrorVerdict(
                category="destructive_refused",
                recoverable=False,
                recovery_hint=_hint("destructive_refused"),
                raw_kind=error_kind or "internal",
            )
        if "stale" in msg and "read" in msg:
            return ErrorVerdict(
                category="stale_file_read",
                recoverable=True,
                recovery_hint=_hint("stale_file_read"),
                raw_kind=error_kind or "internal",
            )

    # 3) Catch-all.
    return ErrorVerdict(
        category="unrecoverable",
        recoverable=False,
        recovery_hint=_hint("unrecoverable"),
        raw_kind=error_kind or "internal",
    )


# ---------------------------------------------------------------------------
# Retry policy (Phase 13)
# ---------------------------------------------------------------------------


class RecoveryAction(str):
    """How the harness should act on a given error category.

    ``auto_retry``      — re-issue the same call with backoff, no model round-trip.
    ``ask_model``       — surface the error + recovery hint, let the model decide
                          (usually because the *payload* needs fixing).
    ``ask_user``        — surface to the operator (approval / fatal config issue).
    ``stop_loop``       — abort the current turn; further retries are pointless.
    """

    AUTO_RETRY = "auto_retry"
    ASK_MODEL = "ask_model"
    ASK_USER = "ask_user"
    STOP_LOOP = "stop_loop"


@dataclass
class RetryPolicy:
    """Per-error retry configuration.

    The agent loop reads ``policy_for_kind`` once it sees a failed
    ``tool_result`` and decides between (a) replaying the same call
    transparently with backoff, (b) appending the result + hint and
    asking the model, (c) escalating to the operator, (d) halting.
    """

    action: str
    """One of :class:`RecoveryAction` constants."""

    max_retries: int = 0
    """Maximum *automatic* retries the loop performs before falling
    back to ``ask_model``. ``0`` means no auto retries — the model
    sees the failure on the very next round-trip."""

    initial_backoff_s: float = 0.5
    backoff_multiplier: float = 2.0
    max_backoff_s: float = 8.0

    notes: str = ""

    def backoff_for_attempt(self, attempt: int) -> float:
        """Return the wait time before retry attempt ``attempt`` (1-indexed)."""

        if attempt < 1:
            return 0.0
        delay = self.initial_backoff_s * (self.backoff_multiplier ** (attempt - 1))
        return min(delay, self.max_backoff_s)


# Per-category retry policy. The agent loop falls back to a *no-retry,
# ask-model* default when the kind is missing — anything not in the
# table behaves like the old "render a hint" path so existing callers
# stay backwards compatible.
RETRY_POLICY: dict[str, RetryPolicy] = {
    "transient": RetryPolicy(
        action=RecoveryAction.AUTO_RETRY,
        max_retries=2,
        initial_backoff_s=0.5,
        backoff_multiplier=2.0,
        max_backoff_s=4.0,
        notes="timeout / rate_limit / 5xx — retry up to 2× with 0.5/1/2s backoff",
    ),
    "stale_file_read": RetryPolicy(
        action=RecoveryAction.ASK_MODEL,
        max_retries=0,
        notes="model must re-read the file before retrying the edit",
    ),
    "patch_context_mismatch": RetryPolicy(
        action=RecoveryAction.ASK_MODEL,
        max_retries=0,
        notes="model must re-anchor the find string against current bytes",
    ),
    "ambiguous_input": RetryPolicy(
        action=RecoveryAction.ASK_MODEL,
        max_retries=0,
        notes="schema validation failed — model must fix the payload",
    ),
    "approval_pending": RetryPolicy(
        action=RecoveryAction.ASK_MODEL,
        max_retries=0,
        notes=(
            "approval is owed by the operator; surface the hint and let the "
            "model send_message or call plan_status / approval_status"
        ),
    ),
    "permission_denied": RetryPolicy(
        action=RecoveryAction.ASK_MODEL,
        max_retries=0,
        notes=(
            "policy denied this lane; model should pick another action or ask "
            "the operator to switch lanes"
        ),
    ),
    "destructive_refused": RetryPolicy(
        action=RecoveryAction.ASK_USER,
        max_retries=0,
        notes="destructive command was refused; only the operator can authorise",
    ),
    "budget_exceeded": RetryPolicy(
        action=RecoveryAction.STOP_LOOP,
        max_retries=0,
        notes="per-turn budget gone — wrap up rather than thrashing",
    ),
    "deduped": RetryPolicy(
        action=RecoveryAction.ASK_MODEL,
        max_retries=0,
        notes="model already issued this exact call; do NOT retry",
    ),
    "not_query_only": RetryPolicy(
        action=RecoveryAction.ASK_MODEL,
        max_retries=0,
        notes="parallel batch rejected a non-read tool; serialise instead",
    ),
    "tool_not_found": RetryPolicy(
        action=RecoveryAction.ASK_MODEL,
        max_retries=0,
        notes="tool name unknown — model must pick a registered tool",
    ),
    "aborted": RetryPolicy(
        action=RecoveryAction.STOP_LOOP,
        max_retries=0,
        notes="operator/kernel cancelled the call; do not retry",
    ),
    "sandbox_denied": RetryPolicy(
        action=RecoveryAction.ASK_USER,
        max_retries=0,
        notes="sandbox refused the command; only the operator can override",
    ),
    "provider_error": RetryPolicy(
        action=RecoveryAction.AUTO_RETRY,
        max_retries=1,
        initial_backoff_s=0.5,
        backoff_multiplier=2.0,
        max_backoff_s=2.0,
        notes="downstream provider hiccup — retry once then surface to model",
    ),
    "mcp_session_expired": RetryPolicy(
        action=RecoveryAction.AUTO_RETRY,
        max_retries=1,
        initial_backoff_s=0.25,
        backoff_multiplier=2.0,
        max_backoff_s=1.0,
        notes="reconnect handled by MCPSessionAdapter; one extra harness-side retry",
    ),
    "diff_conflict": RetryPolicy(
        action=RecoveryAction.ASK_MODEL,
        max_retries=0,
        notes="patch context drifted; re-read file and re-author the patch",
    ),
    "unrecoverable": RetryPolicy(
        action=RecoveryAction.STOP_LOOP,
        max_retries=0,
        notes="report and stop — further retries cannot help",
    ),
}


def policy_for_kind(category: str | None) -> RetryPolicy:
    """Look up the retry policy for an :class:`ErrorVerdict` category.

    Unknown categories fall back to ``ask_model`` with no retries —
    that's the safest default: surface the failure, let the next
    round-trip decide what to do.
    """

    if category and category in RETRY_POLICY:
        return RETRY_POLICY[category]
    return RetryPolicy(
        action=RecoveryAction.ASK_MODEL,
        max_retries=0,
        notes="unknown category — surface to model",
    )
