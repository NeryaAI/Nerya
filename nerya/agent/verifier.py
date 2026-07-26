"""Verification-agent nudge and 3-tier verifier system.

Tier 1 — Nudge (legacy)
========================
When the model marks several todos completed without running a
verification step (tests, type-check, dry-run), the harness nudges a
verifier so the user does not have to accept "all done" on faith.

The ``after_turn`` reflection hook counts completed todos, compares them
to validation evidence, and attaches a follow-up suggestion to the next
observation.

This module is the *detection* half. The kernel uses
:func:`compute_verifier_nudge` after a turn and, when triggered, emits
an ``agent.verifier.nudge`` journal record + injects a system-level
note into ``memory/global.md`` (already plumbed for cross-turn
context). The next turn's system prompt picks the note up via the
existing memory-recall block, so the model sees:

    [verifier] last turn marked 4 todos completed without running any
    validation tool (no run_shell test invocations, no test_runner
    tools, no diff/grep against the changed paths). Run a verification
    step before declaring done.

Tier 2 — 3-Tier Verifier (new)
===============================
The 3-tier verifier augments the legacy nudge with a structured
outcome that the kernel (or any caller) can use to decide *why* a turn
ended and whether the result is trustworthy.

``verify_hard``  — External truth signal
    Checks whether any test / validation / verification tool ran **and
    succeeded** in the turn blocks. Missing validation is explicit
    ``hard_status="missing"`` rather than a pass, so model prose is
    never mislabeled as hard-verified.

``verify_soft``  — Budget / heuristic check
    Two conditions OR'd:
      a. Token budget over 90 % (pass ``tokens_used`` and
         ``tokens_budget`` as params).
      b. 3 consecutive turns each producing < 500 new tokens of
         assistant text (diminishing returns).
    Default is ``False`` (no budget pressure).

``verify_giveup`` — Model self-stopped
    Model emits no ``tool_use`` AND has assistant text. This is a
    natural completion signal.

``compute_verifier_outcome`` combines all three tiers and returns a
``VerifierOutcome`` with a ``transition_label`` suitable for journal
records and downstream decision-making.

What counts as "validation"
---------------------------
Configurable, but the defaults follow the same basic heuristic:

* a ``run_shell`` call whose command matches one of ``test_patterns``
  (``pytest``/``go test``/``cargo test``/``npm test``/``make test``/…),
* a ``script_run`` whose script name contains ``test`` or ``check``,
* an explicit ``verify_*`` / ``check_*`` native tool invocation,
* an ``llm_classify`` / ``llm_extract_json`` call whose ``task`` tag
  starts with ``verify``,
* a fresh ``read_file`` on a path that ``edit_file`` touched in the
  same turn (re-read after edit).

Anything else doesn't count — including running the *failing* code
path, since the model already knows the change works there.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Iterable, Sequence


# ---------------------------------------------------------------------------
# Heuristic: what shell commands count as "running tests"?
# ---------------------------------------------------------------------------


_TEST_COMMAND_PATTERNS: tuple[re.Pattern[str], ...] = tuple(
    re.compile(p, re.IGNORECASE)
    for p in (
        r"\bpytest\b",
        r"\bpython\s+-m\s+(?:unittest|pytest)\b",
        r"\bgo\s+test\b",
        r"\bcargo\s+test\b",
        r"\bnpm\s+(?:run\s+)?test\b",
        r"\byarn\s+(?:run\s+)?test\b",
        r"\bpnpm\s+(?:run\s+)?test\b",
        r"\bmake\s+(?:test|check|verify)\b",
        r"\bjest\b",
        r"\bvitest\b",
        r"\btox\b",
        r"\bnox\b",
        r"\bmypy\b",
        r"\bruff\s+check\b",
        r"\beslint\b",
        r"\btsc\b",
    )
)

_VERIFY_TOOL_PREFIXES: tuple[str, ...] = ("verify_", "check_", "validate_", "test_")


def is_validation_command(command: str) -> bool:
    return any(pattern.search(command or "") for pattern in _TEST_COMMAND_PATTERNS)


# ---------------------------------------------------------------------------
# Result types
# ---------------------------------------------------------------------------


@dataclass
class VerifierNudge:
    """Detection result for one turn.

    ``triggered`` is True when ``completed_count >= threshold`` AND
    ``validation_count == 0``. The kernel writes the ``message`` to
    ``memory/global.md`` so the next turn's recall block surfaces it.
    """

    triggered: bool
    completed_count: int
    validation_count: int
    threshold: int
    edited_paths: list[str] = field(default_factory=list)
    invoked_validation: list[str] = field(default_factory=list)
    message: str = ""

    def asdict(self) -> dict[str, Any]:
        return {
            "triggered": self.triggered,
            "completed_count": self.completed_count,
            "validation_count": self.validation_count,
            "threshold": self.threshold,
            "edited_paths": list(self.edited_paths),
            "invoked_validation": list(self.invoked_validation),
            "message": self.message,
        }


# ---------------------------------------------------------------------------
# 3-Tier Verifier result type
# ---------------------------------------------------------------------------

_VALID_TRANSITION_LABELS: frozenset[str] = frozenset({
    "verified",
    "model_done",
    "no_more_tools",
    "budget_exceeded",
    "interrupted",
})


@dataclass
class VerifierOutcome:
    """Structured outcome from the 3-tier verifier.

    Attributes
    ----------
    hard_passed:
        ``True`` only when at least one test / validation tool ran
        **and succeeded** in the turn.
    soft_triggered:
        ``True`` when the token budget is over 90 % **or** the last
        3 turns each produced < 500 new tokens of assistant text
        (diminishing returns).  Defaults to ``False``.
    model_done:
        ``True`` when the model emitted no ``tool_use`` blocks but did
        produce assistant text — a natural completion signal.
    transition_label:
        One of ``"verified"``, ``"model_done"``, ``"no_more_tools"``,
        ``"budget_exceeded"``, ``"interrupted"``.  Assigned by
        :func:`compute_verifier_outcome` based on the combined tier
        results.
    details:
        Diagnostic dict with per-tier evidence (test names found,
        budget percentages, token counts, etc.).
    """

    hard_passed: bool
    soft_triggered: bool
    model_done: bool
    transition_label: str
    details: dict[str, Any] = field(default_factory=dict)
    hard_status: str = "missing"
    soft_status: str = "clear"
    lazy_status: str = "no_signal"
    has_hard_evidence: bool = False
    has_validation_attempt: bool = False
    trusted: bool = False

    def __post_init__(self) -> None:
        if self.transition_label not in _VALID_TRANSITION_LABELS:
            raise ValueError(
                f"Invalid transition_label {self.transition_label!r}; "
                f"expected one of {sorted(_VALID_TRANSITION_LABELS)}"
            )

    def asdict(self) -> dict[str, Any]:
        return {
            "hard_passed": self.hard_passed,
            "soft_triggered": self.soft_triggered,
            "model_done": self.model_done,
            "transition_label": self.transition_label,
            "hard_status": self.hard_status,
            "soft_status": self.soft_status,
            "lazy_status": self.lazy_status,
            "has_hard_evidence": self.has_hard_evidence,
            "has_validation_attempt": self.has_validation_attempt,
            "trusted": self.trusted,
            "details": dict(self.details),
        }


# ---------------------------------------------------------------------------
# Tier 1 — Hard verification (external truth signal)
# ---------------------------------------------------------------------------


def verify_hard(
    *,
    blocks: Iterable[dict[str, Any]],
) -> tuple[bool, dict[str, Any]]:
    """Check whether any test/validation tool ran **and succeeded**.

    Scans the turn blocks for ``tool_result`` entries whose action
    matches a known test/verify pattern *and* whose ``ok`` flag is
    ``True``.  Also checks ``tool_use`` entries for the same patterns
    and cross-references with their result (when available).

    Returns ``(passed, details)`` where ``passed`` is ``False`` when
    no validation signal exists. The absence of hard evidence is a
    lazy/model-done fallback, not a hard verifier pass.

    Parameters
    ----------
    blocks:
        Per-turn :class:`BlockEnvelope` dicts — the same shape accepted
        by :func:`compute_verifier_nudge`.
    """

    # Collect tool_result blocks that carry validation evidence.
    # A tool_result envelope has block.kind == "tool_result" with
    # block.ok, block.action, etc.
    successful_validation_actions: list[str] = []
    failed_validation_actions: list[str] = []
    test_commands_seen: list[str] = []
    tool_use_by_call_id: dict[str, dict[str, Any]] = {}
    result_call_ids: set[str] = set()

    # First pass: index tool_use blocks by call_id so we can
    # cross-reference with tool_result blocks.
    for env in blocks or ():
        block = env.get("block") if isinstance(env.get("block"), dict) else env
        if not isinstance(block, dict):
            continue
        kind = block.get("kind")
        if kind == "tool_use":
            call_id = str(block.get("call_id") or "")
            if call_id:
                tool_use_by_call_id[call_id] = block

    # Second pass: scan tool_result blocks for validation success.
    for env in blocks or ():
        block = env.get("block") if isinstance(env.get("block"), dict) else env
        if not isinstance(block, dict):
            continue
        kind = block.get("kind")
        if kind != "tool_result":
            continue

        action = str(block.get("action") or "")
        ok = bool(block.get("ok", False))
        call_id = str(block.get("call_id") or "")
        if call_id:
            result_call_ids.add(call_id)

        # Determine whether this tool_result corresponds to a
        # validation action.  We check both the result's own action
        # field and the original tool_use payload (for run_shell,
        # the validation pattern lives in the command, not the
        # action name).
        is_validation = False

        # Direct prefix match on the action name.
        if any(action.startswith(p) for p in _VERIFY_TOOL_PREFIXES):
            is_validation = True

        # llm_classify / llm_extract_json with verify-prefixed task.
        if action in {"llm_classify", "llm_extract_json"}:
            # Try to get the task from the original tool_use payload.
            orig_use = tool_use_by_call_id.get(call_id)
            if orig_use:
                payload = orig_use.get("payload") or {}
                task = str(payload.get("task") or "").lower()
                if task.startswith("verify") or task.startswith("check"):
                    is_validation = True

        # run_shell with a test command pattern.
        if action == "run_shell":
            # The command is in the original tool_use payload.
            orig_use = tool_use_by_call_id.get(call_id)
            cmd = ""
            if orig_use:
                cmd = str((orig_use.get("payload") or {}).get("command") or "")
            if is_validation_command(cmd):
                is_validation = True
                test_commands_seen.append(cmd[:120])

        # script_run with test/check/verify in the name.
        if action == "script_run":
            orig_use = tool_use_by_call_id.get(call_id)
            name = ""
            if orig_use:
                name = str((orig_use.get("payload") or {}).get("name") or "")
            if any(s in name.lower() for s in ("test", "check", "verify")):
                is_validation = True

        # Also check tool_use-level actions for explicit verify tools
        # that might not appear in the tool_result action (some adapters
        # normalise the action name).
        if not is_validation and call_id:
            orig_use = tool_use_by_call_id.get(call_id)
            if orig_use:
                orig_action = str(orig_use.get("action") or "")
                if any(orig_action.startswith(p) for p in _VERIFY_TOOL_PREFIXES):
                    is_validation = True

        if is_validation:
            if ok:
                successful_validation_actions.append(action)
            else:
                failed_validation_actions.append(action)

    # Third pass: scan tool_use blocks that have no matching
    # tool_result (e.g. the turn ended before results came back).
    # A test was *invoked* but we don't yet know the result — we
    # conservatively treat unknown-result invocations as
    # non-passing by counting them as failed.
    for call_id, use_block in tool_use_by_call_id.items():
        if call_id in result_call_ids:
            continue  # Already handled above.
        action = str(use_block.get("action") or "")
        payload = use_block.get("payload") or {}
        is_validation = False

        if any(action.startswith(p) for p in _VERIFY_TOOL_PREFIXES):
            is_validation = True
        elif action == "run_shell":
            cmd = str(payload.get("command") or "")
            if is_validation_command(cmd):
                is_validation = True
                test_commands_seen.append(cmd[:120])
        elif action == "script_run":
            name = str(payload.get("name") or "")
            if any(s in name.lower() for s in ("test", "check", "verify")):
                is_validation = True
        elif action in {"llm_classify", "llm_extract_json"}:
            task = str(payload.get("task") or "").lower()
            if task.startswith("verify") or task.startswith("check"):
                is_validation = True

        if is_validation:
            # No result yet — conservatively count as failed.
            failed_validation_actions.append(f"{action} (pending_result)")

    if successful_validation_actions:
        passed = True
        hard_status = "passed"
    elif failed_validation_actions:
        passed = False
        hard_status = "failed"
    else:
        passed = False
        hard_status = "missing"

    has_validation_attempt = bool(
        successful_validation_actions or failed_validation_actions
    )
    has_hard_evidence = bool(successful_validation_actions)

    details: dict[str, Any] = {
        "successful_validation_actions": successful_validation_actions,
        "failed_validation_actions": failed_validation_actions,
        "test_commands_seen": test_commands_seen,
        "hard_status": hard_status,
        "has_hard_evidence": has_hard_evidence,
        "has_validation_attempt": has_validation_attempt,
    }
    return passed, details


# ---------------------------------------------------------------------------
# Tier 2 — Soft verification (budget / heuristic check)
# ---------------------------------------------------------------------------

# Minimum number of consecutive low-output turns to trigger the
# diminishing-returns heuristic.
_DIMINISHING_RETURNS_WINDOW: int = 3

# Token threshold below which a turn is considered "low output".
_LOW_OUTPUT_TOKEN_THRESHOLD: int = 500


def verify_soft(
    *,
    tokens_used: int | None = None,
    tokens_budget: int | None = None,
    recent_turn_token_counts: Sequence[int] | None = None,
    budget_threshold_pct: float = 0.90,
    diminishing_returns_window: int = _DIMINISHING_RETURNS_WINDOW,
    low_output_token_threshold: int = _LOW_OUTPUT_TOKEN_THRESHOLD,
) -> tuple[bool, dict[str, Any]]:
    """Budget / heuristic check.

    Two conditions OR'd:

    a. Token budget over ``budget_threshold_pct`` (default 90 %).
       Requires both ``tokens_used`` and ``tokens_budget`` to be
       provided and non-zero.
    b. ``diminishing_returns_window`` consecutive turns (default 3)
       each producing < ``low_output_token_threshold`` new tokens of
       assistant text (default 500).

    Returns ``(triggered, details)`` where ``triggered`` defaults to
    ``False`` (no budget pressure).

    Parameters
    ----------
    tokens_used:
        Tokens consumed so far in the session.
    tokens_budget:
        Total token budget for the session.
    recent_turn_token_counts:
        Sequence of assistant-token counts for the most recent turns,
        ordered oldest-first.  Only the last
        ``diminishing_returns_window`` entries are examined.
    budget_threshold_pct:
        Fraction of budget at which the budget-exceeded trigger fires.
    diminishing_returns_window:
        How many consecutive low-output turns to require.
    low_output_token_threshold:
        Per-turn token count below which a turn is considered
        "low output".
    """

    details: dict[str, Any] = {}
    budget_exceeded = False
    diminishing_returns = False

    # Condition (a): token budget over threshold.
    if (
        tokens_used is not None
        and tokens_budget is not None
        and tokens_budget > 0
    ):
        usage_pct = tokens_used / tokens_budget
        budget_exceeded = usage_pct > budget_threshold_pct
        details["tokens_used"] = tokens_used
        details["tokens_budget"] = tokens_budget
        details["usage_pct"] = round(usage_pct, 4)
        details["budget_threshold_pct"] = budget_threshold_pct
    else:
        details["tokens_used"] = tokens_used
        details["tokens_budget"] = tokens_budget

    # Condition (b): diminishing returns.
    if recent_turn_token_counts is not None:
        window = list(recent_turn_token_counts[-diminishing_returns_window:])
        details["recent_turn_token_counts"] = list(recent_turn_token_counts)
        details["diminishing_returns_window"] = window
        if len(window) >= diminishing_returns_window:
            diminishing_returns = all(
                c < low_output_token_threshold for c in window
            )
        details["low_output_token_threshold"] = low_output_token_threshold
    else:
        details["recent_turn_token_counts"] = None

    triggered = budget_exceeded or diminishing_returns
    details["budget_exceeded"] = budget_exceeded
    details["diminishing_returns"] = diminishing_returns
    return triggered, details


# ---------------------------------------------------------------------------
# Tier 3 — Give-up detection (model self-stopped)
# ---------------------------------------------------------------------------


def verify_giveup(
    *,
    blocks: Iterable[dict[str, Any]],
) -> tuple[bool, dict[str, Any]]:
    """Detect whether the model self-stopped (no tool_use + has text).

    When the model's final iteration produced assistant text but no
    ``tool_use`` blocks, the model has naturally completed its task.
    This is the "give up" / "done talking" signal.

    Returns ``(model_done, details)``.

    Parameters
    ----------
    blocks:
        Per-turn :class:`BlockEnvelope` dicts — the same shape accepted
        by :func:`compute_verifier_nudge`.
    """

    has_text = False
    has_tool_use = False
    text_snippet = ""
    tool_use_actions: list[str] = []

    for env in blocks or ():
        block = env.get("block") if isinstance(env.get("block"), dict) else env
        if not isinstance(block, dict):
            continue
        kind = block.get("kind")
        if kind == "text":
            has_text = True
            text_snippet = str(block.get("text") or "")[:200]
        elif kind == "tool_use":
            has_tool_use = True
            action = str(block.get("action") or "")
            if action:
                tool_use_actions.append(action)

    model_done = has_text and not has_tool_use

    details: dict[str, Any] = {
        "has_text": has_text,
        "has_tool_use": has_tool_use,
        "text_snippet": text_snippet,
        "tool_use_actions": tool_use_actions,
    }
    return model_done, details


# ---------------------------------------------------------------------------
# Combined outcome
# ---------------------------------------------------------------------------


def compute_verifier_outcome(
    *,
    blocks: Iterable[dict[str, Any]],
    tokens_used: int | None = None,
    tokens_budget: int | None = None,
    recent_turn_token_counts: Sequence[int] | None = None,
    budget_threshold_pct: float = 0.90,
    diminishing_returns_window: int = _DIMINISHING_RETURNS_WINDOW,
    low_output_token_threshold: int = _LOW_OUTPUT_TOKEN_THRESHOLD,
    interrupted: bool = False,
) -> VerifierOutcome:
    """Combine all three verifier tiers into a single outcome.

    The ``transition_label`` is assigned by priority:

    1. ``"interrupted"`` — if ``interrupted=True`` (external signal).
    2. ``"budget_exceeded"`` — if :func:`verify_soft` triggered due to
       budget pressure.
    3. ``"verified"`` — if :func:`verify_hard` passed (test/validation
       succeeded).
    4. ``"model_done"`` — if :func:`verify_giveup` detected the model
       self-stopped.
    5. ``"no_more_tools"`` — fallback when the model stopped but hard
       verification did not pass.

    Parameters
    ----------
    blocks:
        Per-turn :class:`BlockEnvelope` dicts.
    tokens_used:
        Tokens consumed so far (for soft check).
    tokens_budget:
        Total token budget (for soft check).
    recent_turn_token_counts:
        Token counts for recent turns (for soft check).
    budget_threshold_pct:
        Fraction of budget at which budget-exceeded fires.
    diminishing_returns_window:
        Consecutive low-output turns for diminishing-returns check.
    low_output_token_threshold:
        Per-turn token threshold for diminishing-returns check.
    interrupted:
        If ``True``, the turn was interrupted externally (cancel,
        timeout, etc.).
    """

    hard_passed, hard_details = verify_hard(blocks=blocks)
    soft_triggered, soft_details = verify_soft(
        tokens_used=tokens_used,
        tokens_budget=tokens_budget,
        recent_turn_token_counts=recent_turn_token_counts,
        budget_threshold_pct=budget_threshold_pct,
        diminishing_returns_window=diminishing_returns_window,
        low_output_token_threshold=low_output_token_threshold,
    )
    model_done, giveup_details = verify_giveup(blocks=blocks)

    hard_status = str(hard_details.get("hard_status") or "missing")
    has_hard_evidence = bool(hard_details.get("has_hard_evidence"))
    has_validation_attempt = bool(hard_details.get("has_validation_attempt"))
    if soft_details.get("budget_exceeded"):
        soft_status = "budget_exceeded"
    elif soft_details.get("diminishing_returns"):
        soft_status = "diminishing_returns"
    else:
        soft_status = "clear"
    if model_done:
        lazy_status = "model_done"
    elif giveup_details.get("has_tool_use"):
        lazy_status = "tooling_active"
    elif giveup_details.get("has_text"):
        lazy_status = "text_only"
    else:
        lazy_status = "no_signal"

    # Determine transition_label by priority.
    if interrupted:
        transition_label = "interrupted"
    elif soft_triggered and soft_details.get("budget_exceeded"):
        transition_label = "budget_exceeded"
    elif hard_passed:
        transition_label = "verified"
    elif model_done:
        transition_label = "model_done"
    else:
        transition_label = "no_more_tools"

    details: dict[str, Any] = {
        "hard": hard_details,
        "soft": soft_details,
        "giveup": giveup_details,
    }

    return VerifierOutcome(
        hard_passed=hard_passed,
        soft_triggered=soft_triggered,
        model_done=model_done,
        transition_label=transition_label,
        hard_status=hard_status,
        soft_status=soft_status,
        lazy_status=lazy_status,
        has_hard_evidence=has_hard_evidence,
        has_validation_attempt=has_validation_attempt,
        trusted=bool(hard_passed and has_hard_evidence and not interrupted),
        details=details,
    )


# ---------------------------------------------------------------------------
# Public API (legacy nudge — unchanged)
# ---------------------------------------------------------------------------


def compute_verifier_nudge(
    *,
    blocks: Iterable[dict[str, Any]],
    todos_before: list[dict[str, Any]],
    todos_after: list[dict[str, Any]],
    threshold: int = 3,
) -> VerifierNudge:
    """Inspect one turn and decide whether to nudge a verifier agent.

    Parameters
    ----------
    blocks:
        Per-turn :class:`BlockEnvelope` dicts (``AgentTurnResult.blocks``).
    todos_before:
        Snapshot of :attr:`TaskState.todos` *before* the turn ran. Pass
        an empty list if the kernel didn't snapshot pre-turn (we'll
        treat every "completed" item as freshly completed).
    todos_after:
        Snapshot taken after the turn finished.
    threshold:
        Minimum number of *newly* completed todos needed to consider
        nudging. Below this the model is doing fine-grained progress
        tracking and a nudge would be noise.
    """

    before_completed: set[str] = {
        str(t.get("id") or t.get("content"))
        for t in (todos_before or [])
        if (t.get("status") == "completed")
    }
    after_completed: list[dict[str, Any]] = [
        t for t in (todos_after or []) if (t.get("status") == "completed")
    ]
    newly_completed = [
        t for t in after_completed
        if str(t.get("id") or t.get("content")) not in before_completed
    ]
    completed_count = len(newly_completed)

    edited_paths: list[str] = []
    validation_invocations: list[str] = []
    test_evidence_seen = 0
    edit_paths_set: set[str] = set()

    for env in blocks or ():
        block = env.get("block") if isinstance(env.get("block"), dict) else env
        if not isinstance(block, dict):
            continue
        kind = block.get("kind")
        if kind == "tool_use":
            action = str(block.get("action") or "")
            payload = block.get("payload") or {}
            if action in {"edit_file", "write_file"}:
                p = str(payload.get("path") or "")
                if p:
                    edit_paths_set.add(p)
                    if p not in edited_paths:
                        edited_paths.append(p)
            elif action == "run_shell":
                cmd = str(payload.get("command") or "")
                if is_validation_command(cmd):
                    test_evidence_seen += 1
                    validation_invocations.append(f"run_shell: {cmd[:80]}")
            elif action == "script_run":
                name = str(payload.get("name") or "")
                if any(s in name.lower() for s in ("test", "check", "verify")):
                    test_evidence_seen += 1
                    validation_invocations.append(f"script_run: {name}")
            elif any(action.startswith(p) for p in _VERIFY_TOOL_PREFIXES):
                test_evidence_seen += 1
                validation_invocations.append(action)
            elif action in {"llm_classify", "llm_extract_json"}:
                task = str((payload.get("task") or "")).lower()
                if task.startswith("verify") or task.startswith("check"):
                    test_evidence_seen += 1
                    validation_invocations.append(f"{action}({task})")

    # A re-read of an edited file also counts as evidence — the model
    # confirmed the new bytes round-trip cleanly.
    if edited_paths:
        for env in blocks or ():
            block = env.get("block") if isinstance(env.get("block"), dict) else env
            if not isinstance(block, dict):
                continue
            if block.get("kind") != "tool_use":
                continue
            if block.get("action") != "read_file":
                continue
            p = str((block.get("payload") or {}).get("path") or "")
            if p in edit_paths_set:
                test_evidence_seen += 1
                validation_invocations.append(f"read_file: {p} (post-edit)")
                break  # one re-read is enough

    triggered = completed_count >= threshold and test_evidence_seen == 0
    if triggered:
        msg_parts = [
            f"[verifier] Last turn marked {completed_count} todo(s) completed",
        ]
        if edited_paths:
            shown = ", ".join(edited_paths[:5])
            extra = "" if len(edited_paths) <= 5 else f" (+{len(edited_paths) - 5} more)"
            msg_parts.append(f"and edited: {shown}{extra}")
        msg_parts.append(
            "without running any validation step (no test runner, "
            "no verify_* tool, no post-edit re-read). Before declaring "
            "the next batch of work done, run a verification: pytest / "
            "go test / npm test / a verify_* tool / re-read a file you "
            "just edited."
        )
        message = " ".join(msg_parts)
    else:
        message = ""

    return VerifierNudge(
        triggered=triggered,
        completed_count=completed_count,
        validation_count=test_evidence_seen,
        threshold=threshold,
        edited_paths=edited_paths,
        invoked_validation=validation_invocations,
        message=message,
    )


__all__ = [
    "VerifierNudge",
    "compute_verifier_nudge",
    "VerifierOutcome",
    "verify_hard",
    "verify_soft",
    "verify_giveup",
    "compute_verifier_outcome",
]
