"""Verification of recorded checks, not universal completion or authority.

Host receipts bind checks to source versions. All required recorded checks must
pass; pending, missing, failed and stale remain distinct. A later revision
invalidates older checks. Shell compatibility evidence is explicitly turn-scoped.
Model self-review, tool names and quoted checker commands are not hard evidence.
The legacy todo nudge is advisory only; it never changes a verification verdict.
"""

from __future__ import annotations

import re
import json
import shlex
from dataclasses import dataclass, field
from typing import Any, Iterable, Sequence

from ..tools.verification import read_verification_receipt


# ---------------------------------------------------------------------------
# Heuristic: what shell commands count as "running tests"?
# ---------------------------------------------------------------------------


_VERIFY_TOOL_PREFIXES: tuple[str, ...] = ("verify_", "check_", "validate_", "test_")


def is_validation_command(command: str) -> bool:
    """Recognize an invoked checker, not quoted words or masked shell exits.

    Deliberately conservative: complex shell programs need a host receipt. A
    pipeline's last process or ``|| true`` cannot prove the checker succeeded.
    This is evidence classification, never a shell authorization policy.
    """
    if not command or any(token in command for token in (";", "|", "\n", "`")):
        return False
    if "$(" in command or "&" in command.replace("&&", ""):
        return False
    try:
        segments = [shlex.split(segment) for segment in command.split("&&")]
    except ValueError:
        return False
    checked = False
    for words in segments:
        if not words:
            return False
        if words[0] == "cd" and len(words) == 2 and not checked:
            continue
        # A leading environment assignment does not change which command runs.
        while words and re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*=.*", words[0]):
            words = words[1:]
        if not words:
            return False
        executable = words[0].replace("\\", "/").rsplit("/", 1)[-1]
        args = words[1:]
        no_check = {"-h", "--help", "--version", "--collect-only", "--co", "--listTests",
                    "--list-tests", "--showConfig", "--show-config", "--listFilesOnly", "--show-settings"}
        if any(arg.split("=", 1)[0] in no_check for arg in args):
            return False
        if re.fullmatch(r"python(?:\d+(?:\.\d+)*)?(?:\.exe)?", executable):
            if len(args) < 2 or args[0] != "-m" or args[1] not in {"pytest", "unittest"}:
                return False
        elif executable in {"go", "cargo"}:
            if not args or args[0] != "test":
                return False
        elif executable in {"npm", "pnpm", "yarn"}:
            if args[:1] != ["test"] and args[:2] != ["run", "test"]:
                return False
        elif executable == "make":
            if not args or args[0] not in {"test", "check", "verify"}:
                return False
        elif executable == "ruff":
            if args[:1] != ["check"]:
                return False
        elif executable not in {"pytest", "jest", "vitest", "tox", "nox", "mypy", "eslint", "tsc"}:
            return False
        checked = True
    return checked


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
        **and succeeded**, with no unresolved required check in the turn.
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
    """Reduce recorded checks, with failure/missing/stale precedence.

    A new receipt supersedes the same check only, for the same artifact. Other
    checks on an older revision become stale. Legacy shell checks are explicitly
    turn-scoped; named tools without host receipts cannot self-certify a version.
    """
    rows = [env.get("block", env) for env in blocks or () if isinstance(env, dict)]
    rows = [row for row in rows if isinstance(row, dict)]
    uses = {str(row["call_id"]): row for row in rows if row.get("kind") == "tool_use" and row.get("call_id")}
    checks: dict[tuple[str, str], dict[str, Any]] = {}
    completed: set[str] = set()
    test_commands: list[str] = []
    unverified: list[str] = []
    for row in rows:
        if row.get("kind") != "tool_result":
            continue
        call_id = str(row.get("call_id") or "")
        completed.add(call_id)
        use = uses.get(call_id, {})
        action = str(row.get("action") or use.get("action") or "")
        if row.get("modified_paths") or (row.get("ok") is True and action in {"write_file", "edit_file"}):
            # Without a complete dependency graph, a source write conservatively
            # invalidates earlier successes. Rechecking restores that check only.
            for check in checks.values():
                if check["status"] == "passed":
                    check["status"] = "stale"
        receipt = read_verification_receipt(row.get("verification"))
        if receipt is not None:
            artifact, revision = receipt["artifact_id"], receipt["revision"]
            for (owner, _), previous in checks.items():
                if owner == artifact and previous["revision"] != revision:
                    previous["status"] = "stale"
            for item in receipt["checks"]:
                check = {**item, "artifact_id": artifact, "revision": revision,
                         "scope": receipt["scope"], "action": action, "call_id": call_id, "binding": "source_revision"}
                checks[(artifact, item["check_id"])] = check
            if row.get("ok") is not True or row.get("semantic_success") is False:
                checks[(artifact, f"{action}:operation")] = {
                    "check_id": f"{action}:operation", "label": "Tool operation", "artifact_id": artifact,
                    "revision": revision, "action": action, "call_id": call_id, "binding": "source_revision",
                    "status": "failed", "required": True,
                }
            else:
                checks.pop((artifact, f"{action}:operation"), None)
            unverified.extend(receipt["unverified"])
            continue
        payload = use.get("payload") or {}
        command = str(payload.get("command") or "") if isinstance(payload, dict) else ""
        shell_check = action == "run_shell" and bool(use) and is_validation_command(command)
        named_check = action.startswith(_VERIFY_TOOL_PREFIXES)
        if not shell_check and not named_check:
            continue
        status = _legacy_check_status(row)
        if named_check and status == "passed":
            status = "missing"  # a tool name is not a verification contract
        key = command if shell_check else action
        checks[("turn", key)] = {"check_id": key, "label": key, "action": action, "call_id": call_id,
                                 "artifact_id": "", "revision": "", "scope": "Recorded command only",
                                 "binding": "turn", "status": status, "required": True}
        if shell_check:
            test_commands.append(command[:240])
    for call_id, use in uses.items():
        if call_id in completed:
            continue
        action = str(use.get("action") or "")
        command = str((use.get("payload") or {}).get("command") or "")
        if action == "run_shell" and is_validation_command(command) or action.startswith(_VERIFY_TOOL_PREFIXES):
            checks[("pending", call_id)] = {"check_id": command or action, "label": command or action,
                "action": action, "call_id": call_id, "artifact_id": "", "revision": "",
                "binding": "turn", "status": "pending", "required": True}
    items = list(checks.values())
    required = [item for item in items if item["required"]]
    statuses = {item["status"] for item in required}
    hard_status = next((status for status in ("failed", "pending", "stale", "missing") if status in statuses),
                       "passed" if required else "missing")
    successful = [item["action"] for item in items if item["status"] == "passed"]
    failed = [item["action"] for item in items if item["status"] == "failed"]
    return hard_status == "passed", {
        "successful_validation_actions": successful, "failed_validation_actions": failed,
        "test_commands_seen": test_commands, "hard_status": hard_status,
        "has_hard_evidence": bool(successful), "has_validation_attempt": bool(items),
        "checks": items, "unverified": list(dict.fromkeys(unverified)),
        "scope": "Recorded checks only; not universal task completion or live authorization.",
    }


def _legacy_check_status(row: dict[str, Any]) -> str:
    value = row.get("result")
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except ValueError:
            value = {}
    data = value if isinstance(value, dict) else {}
    if (data.get("terminal") is False or data.get("complete") is False
            or data.get("execution_state") == "unknown"
            or data.get("status") in {"pending", "submitted", "confirming", "awaiting_approval"}):
        return "pending"
    if (row.get("ok") is not True or row.get("semantic_success") is False
            or data.get("ok") is False or data.get("success") is False
            or data.get("exit_code", 0) != 0 or data.get("errors") or data.get("error")
            or data.get("status") in {"failed", "error", "blocked", "cancelled"}):
        return "failed"
    return "passed"


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

    blocks = list(blocks or ())
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
