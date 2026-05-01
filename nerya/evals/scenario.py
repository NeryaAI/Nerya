"""Eval scenario dataclasses.

A scenario bundles three things:

* A *scripted* model transcript (:class:`TranscriptScript`) — what the
  mock provider should return on each turn.
* The *fixture overrides* the runner should apply before invoking the
  agent loop — eg. seed files in a temp workspace, register custom
  tools, plug a permission engine that denies ``run_shell``.
* The *verdict criteria* — expected tool calls, expected final text,
  expected ``LoopOutcome.stop_reason``, plus a free-form predicate so
  domain-specific assertions can live alongside the data.

These dataclasses are intentionally serialisation-friendly: every
field except the predicates is JSON-encodable so the catalog can be
exported / diffed in CI.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Optional, Sequence

from .transcript_backend import TranscriptScript


# ---------------------------------------------------------------------------
# Expectation primitives
# ---------------------------------------------------------------------------


@dataclass
class ToolCallExpectation:
    """One expected tool invocation.

    ``input_subset`` is matched against the actual ``ToolCall.args``
    via dict containment — only the keys present in ``input_subset``
    are checked, so scenarios don't need to enumerate every default
    argument the loop fills in. Set ``allow_extra_calls=False`` on the
    parent :class:`EvalScenario` to make the runner reject tools you
    didn't list.
    """

    name: str
    input_subset: dict[str, Any] = field(default_factory=dict)
    expected_status: str = "ok"
    """Expected ``ToolResult.status``. ``"any"`` skips the check."""

    def matches(self, actual_name: str, actual_input: dict[str, Any]) -> bool:
        if self.name != actual_name:
            return False
        for key, value in self.input_subset.items():
            if actual_input.get(key) != value:
                return False
        return True


# ---------------------------------------------------------------------------
# Scenario + verdict
# ---------------------------------------------------------------------------


@dataclass
class EvalScenario:
    """Declarative regression scenario.

    Attributes
    ----------
    id, title, summary:
        Catalog metadata. ``id`` should be stable so dashboards can
        track pass-rate over time.
    script:
        :class:`TranscriptScript` driving the mock provider.
    user_message:
        Initial user message (string or content blocks).
    system:
        System prompt the loop should be invoked with. Defaults to a
        minimal harness prompt; override for skill / persona evals.
    expected_tool_calls:
        Ordered list of :class:`ToolCallExpectation`. The runner
        records every dispatched call; ``allow_extra_calls`` controls
        whether unexpected calls fail the scenario.
    expected_stop_reason:
        Required ``LoopOutcome.stop_reason`` (``"end_turn"`` /
        ``"timeout"`` / ``"error"`` / etc.). Empty string skips check.
    expected_final_text_contains:
        Substrings the final assistant text must contain (in order).
    timeout_seconds:
        Wall clock cap for the scenario. The runner sets
        :attr:`LoopConfig.max_wall_seconds` accordingly.
    setup, teardown:
        Optional callables run before/after the scenario. Receive a
        scratch dict so setup can pass state to teardown without
        polluting the scenario itself.
    custom_predicate:
        Optional ``(EvalRunResult) -> Optional[str]``. Returns ``None``
        for "passed" or an error message string. Runs after the built
        in expectations so scenarios can assert custom invariants.
    tags:
        Free-form labels — ``"permissions"``, ``"shell-risk"``,
        ``"compact"``, etc. The runner can filter by tag.
    """

    id: str
    title: str
    summary: str
    script: TranscriptScript
    user_message: str | list[dict[str, Any]] = ""
    system: str = "You are a Nerya regression eval target."
    expected_tool_calls: Sequence[ToolCallExpectation] = field(default_factory=list)
    expected_stop_reason: str = "end_turn"
    expected_final_text_contains: Sequence[str] = field(default_factory=list)
    timeout_seconds: float = 30.0
    allow_extra_calls: bool = True
    setup: Optional[Callable[[dict[str, Any]], None]] = None
    teardown: Optional[Callable[[dict[str, Any]], None]] = None
    custom_predicate: Optional[Callable[["EvalRunResult"], Optional[str]]] = None
    tags: Sequence[str] = field(default_factory=tuple)


# ---------------------------------------------------------------------------
# Result envelope
# ---------------------------------------------------------------------------


@dataclass
class EvalVerdict:
    """High-level pass/fail wrapper."""

    passed: bool
    failures: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)


@dataclass
class EvalRunResult:
    """Single-scenario run result."""

    scenario_id: str
    started_at: str
    finished_at: str
    duration_ms: int
    verdict: EvalVerdict
    iterations: int
    stop_reason: str
    final_text: str
    tool_calls: list[dict[str, Any]] = field(default_factory=list)
    transcript: list[dict[str, Any]] = field(default_factory=list)
    error: Optional[str] = None
    setup_state: dict[str, Any] = field(default_factory=dict)
    metadata: dict[str, Any] = field(default_factory=dict)

    def asdict(self) -> dict[str, Any]:
        return {
            "scenario_id": self.scenario_id,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "duration_ms": self.duration_ms,
            "passed": self.verdict.passed,
            "failures": list(self.verdict.failures),
            "notes": list(self.verdict.notes),
            "iterations": self.iterations,
            "stop_reason": self.stop_reason,
            "final_text": self.final_text,
            "tool_calls": list(self.tool_calls),
            "error": self.error,
            "metadata": dict(self.metadata),
        }


__all__ = [
    "EvalRunResult",
    "EvalScenario",
    "EvalVerdict",
    "ToolCallExpectation",
]
