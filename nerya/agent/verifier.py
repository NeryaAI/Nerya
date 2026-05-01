"""Verification-agent nudge — .

Agent runtime reference:

* ``coding-agent/.../src/tools/TodoWriteTool/TodoWriteTool.ts:72`` — when
  the model marks several todos completed without running a verification
  step (tests, type-check, dry-run), the harness *nudges* a verifier
  agent so the user doesn't accept "all done" on faith.

compatibility:

* The runtime' ``after_turn`` reflection hook fires the same shape — count
  completed todos this turn, compare to validation evidence, attach a
  follow-up suggestion to the next observation.

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

What counts as "validation"
---------------------------
Configurable, but the defaults match coding-agent's heuristic:

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
from typing import Any, Iterable


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
# Public API
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
                if any(p.search(cmd) for p in _TEST_COMMAND_PATTERNS):
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
]
