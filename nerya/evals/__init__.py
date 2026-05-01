
"""Eval / regression harness for the workspace-native agent loop.

This package ships the *infrastructure* for tool-level evals (the
machinery used to run scenarios), not the scenarios themselves. The
built-in templates are seed scenarios; operators are expected to
maintain their own catalog under ``evals/`` in the workspace.

Public surface:

* :class:`TranscriptScript` / :class:`ScriptedTurn` — declarative
  multi-turn LLM responses (mock provider).
* :class:`TranscriptMockBackend` — a :class:`MessagesBackend` that
  walks the script and returns ``tool_use`` / ``text`` blocks turn by
  turn.
* :class:`EvalScenario` — a dataclass describing a single regression
  scenario: registry/orchestrator overrides, scripted transcript,
  expected tool calls, and a verdict callable.
* :class:`EvalRunner` — wires the loop, runs scenarios, and aggregates
  ``EvalRunResult`` instances (pass/fail/error per scenario).
* :data:`SCENARIO_TEMPLATES` — built-in scenario templates expressed as data so the catalog is auditable
  without shipping a pytest tree.

The module is import-safe even when the agent loop is not in use; we
defer the loop import until :class:`EvalRunner.run_scenario` is
invoked.
"""

from .transcript_backend import (
    AssistantBlock,
    ScriptedTurn,
    TextBlock,
    ToolUseBlock,
    TranscriptMockBackend,
    TranscriptScript,
)
from .scenario import (
    EvalScenario,
    EvalRunResult,
    EvalVerdict,
    ToolCallExpectation,
)
from .runner import EvalRunner
from .scenarios import SCENARIO_TEMPLATES, scenario_template

__all__ = [
    "AssistantBlock",
    "EvalRunner",
    "EvalRunResult",
    "EvalScenario",
    "EvalVerdict",
    "SCENARIO_TEMPLATES",
    "ScriptedTurn",
    "TextBlock",
    "ToolCallExpectation",
    "ToolUseBlock",
    "TranscriptMockBackend",
    "TranscriptScript",
    "scenario_template",
]
