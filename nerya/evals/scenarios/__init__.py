"""Reference eval scenarios.

These are *templates*, not pytest tests. Each function here builds a
fresh :class:`EvalScenario` so callers (CI scripts, dashboards, MCP
probes) can mutate the user prompt or expected tool calls without
mutating shared state.

The 10 scenarios mirror Phase 15 of
``docs/agent-harness-comparison-and-refactor-todo.md``:

1. ``read_grep_edit_shell_final``    — happy-path coding task.
2. ``tool_input_schema_correction``  — tool error -> retry.
3. ``permission_ask_approve``        — permission engine grants.
4. ``permission_deny_alternative``   — permission engine denies, agent reroutes.
5. ``shell_dangerous_blocked``       — risky shell intercepted.
6. ``compact_then_continue``         — transcript compaction across turns.
7. ``skill_index_view_run``          — skill discovery -> view -> run.
8. ``mcp_session_expired_retry``     — MCP recovery path.
9. ``subagent_async_completion``     — subagent results don't leak into parent ctx.
10. ``interrupt_during_tool_use``    — interrupt + resume preserves transcript.

The catalog deliberately does *not* execute these scenarios; that's
:class:`nerya.evals.runner.EvalRunner`'s job. Operators register
their own scenarios alongside these by calling
:func:`scenario_template` for one of the IDs above (which constructs
a fresh scenario) or by composing one from scratch.
"""

from __future__ import annotations

from typing import Callable, Mapping

from ..scenario import EvalScenario, ToolCallExpectation
from ..transcript_backend import (
    ScriptedTurn,
    TextBlock,
    ToolUseBlock,
    TranscriptScript,
)


# ---------------------------------------------------------------------------
# Scenario builders (each returns a *fresh* EvalScenario)
# ---------------------------------------------------------------------------


def _scn_read_grep_edit_shell_final() -> EvalScenario:
    return EvalScenario(
        id="read_grep_edit_shell_final",
        title="Happy-path: read → grep → edit → shell → final",
        summary=(
            "Drives the agent through the canonical coding loop: "
            "read a file, locate a symbol with grep, propose an edit, "
            "verify with run_shell, finish with a text summary."
        ),
        user_message="Refactor calculate_total in src/util.py to use Decimal.",
        script=TranscriptScript(
            turns=[
                ScriptedTurn(
                    blocks=[
                        TextBlock("Reading the target module first."),
                        ToolUseBlock(
                            name="read_file",
                            input={"path": "src/util.py"},
                        ),
                    ],
                    stop_reason="tool_use",
                    label="read_target",
                ),
                ScriptedTurn(
                    blocks=[
                        ToolUseBlock(
                            name="grep_search",
                            input={"pattern": "calculate_total", "path": "src"},
                        ),
                    ],
                    stop_reason="tool_use",
                    label="locate_symbol",
                ),
                ScriptedTurn(
                    blocks=[
                        ToolUseBlock(
                            name="edit_file",
                            input={
                                "path": "src/util.py",
                                "patch": "diff_placeholder",
                            },
                        ),
                    ],
                    stop_reason="tool_use",
                    label="apply_edit",
                ),
                ScriptedTurn(
                    blocks=[
                        ToolUseBlock(
                            name="run_shell",
                            input={"command": "pytest -k calculate_total"},
                        ),
                    ],
                    stop_reason="tool_use",
                    label="verify",
                ),
                ScriptedTurn(
                    blocks=[
                        TextBlock(
                            "Refactor complete; calculate_total now uses Decimal."
                        ),
                    ],
                    stop_reason="end_turn",
                    label="final",
                ),
            ]
        ),
        expected_tool_calls=[
            ToolCallExpectation(name="read_file"),
            ToolCallExpectation(name="grep_search"),
            ToolCallExpectation(name="edit_file"),
            ToolCallExpectation(name="run_shell"),
        ],
        expected_final_text_contains=["calculate_total", "Decimal"],
        tags=("happy-path", "coding"),
    )


def _scn_tool_input_schema_correction() -> EvalScenario:
    return EvalScenario(
        id="tool_input_schema_correction",
        title="Tool input schema error → tool_result error → model corrects",
        summary=(
            "First call uses an unknown argument and the orchestrator "
            "surfaces a schema error. The model corrects the input "
            "and the second call succeeds."
        ),
        user_message="Read README.md.",
        script=TranscriptScript(
            turns=[
                ScriptedTurn(
                    blocks=[
                        ToolUseBlock(
                            name="read_file",
                            input={"file_path": "README.md"},
                        ),
                    ],
                    stop_reason="tool_use",
                    label="bad_arg",
                ),
                ScriptedTurn(
                    blocks=[
                        ToolUseBlock(
                            name="read_file",
                            input={"path": "README.md"},
                        ),
                    ],
                    stop_reason="tool_use",
                    label="corrected",
                ),
                ScriptedTurn(
                    blocks=[TextBlock("Read complete.")],
                    stop_reason="end_turn",
                ),
            ]
        ),
        expected_tool_calls=[
            ToolCallExpectation(
                name="read_file",
                input_subset={"file_path": "README.md"},
                expected_status="error",
            ),
            ToolCallExpectation(
                name="read_file",
                input_subset={"path": "README.md"},
                expected_status="ok",
            ),
        ],
        tags=("schema", "recovery"),
    )


def _scn_permission_ask_approve() -> EvalScenario:
    return EvalScenario(
        id="permission_ask_approve",
        title="Permission ask → approve → tool runs",
        summary=(
            "Tool requires operator approval; harness fixture grants it; "
            "tool runs to completion and result feeds the next turn."
        ),
        user_message="Delete tmp/ scratch files.",
        script=TranscriptScript(
            turns=[
                ScriptedTurn(
                    blocks=[
                        ToolUseBlock(
                            name="run_shell",
                            input={"command": "rm -rf tmp"},
                        ),
                    ],
                    stop_reason="tool_use",
                    label="ask",
                ),
                ScriptedTurn(
                    blocks=[TextBlock("Cleanup completed.")],
                    stop_reason="end_turn",
                ),
            ]
        ),
        expected_tool_calls=[
            ToolCallExpectation(
                name="run_shell",
                input_subset={"command": "rm -rf tmp"},
                expected_status="ok",
            )
        ],
        expected_final_text_contains=["completed"],
        tags=("permissions",),
    )


def _scn_permission_deny_alternative() -> EvalScenario:
    return EvalScenario(
        id="permission_deny_alternative",
        title="Permission denied → model picks an alternative path",
        summary=(
            "Risky shell is denied. The model recovers by reading the "
            "denial, switching to a read-only inspection, and ending."
        ),
        user_message="Reset the database.",
        script=TranscriptScript(
            turns=[
                ScriptedTurn(
                    blocks=[
                        ToolUseBlock(
                            name="run_shell",
                            input={"command": "psql -c 'TRUNCATE accounts'"},
                        ),
                    ],
                    stop_reason="tool_use",
                    label="risky_attempt",
                ),
                ScriptedTurn(
                    blocks=[
                        ToolUseBlock(
                            name="read_file",
                            input={"path": "ops/runbook.md"},
                        ),
                    ],
                    stop_reason="tool_use",
                    label="rerouted",
                ),
                ScriptedTurn(
                    blocks=[
                        TextBlock(
                            "Operator must run the reset manually; here's the runbook."
                        ),
                    ],
                    stop_reason="end_turn",
                ),
            ]
        ),
        expected_tool_calls=[
            ToolCallExpectation(name="run_shell", expected_status="error"),
            ToolCallExpectation(name="read_file"),
        ],
        expected_final_text_contains=["runbook"],
        tags=("permissions", "recovery"),
    )


def _scn_shell_dangerous_blocked() -> EvalScenario:
    return EvalScenario(
        id="shell_dangerous_blocked",
        title="Dangerous shell intercepted by risk filter",
        summary=(
            "BashTool risk classifier rejects an obviously destructive "
            "command. The model surfaces the rejection text instead of "
            "retrying."
        ),
        user_message="Free up disk space.",
        script=TranscriptScript(
            turns=[
                ScriptedTurn(
                    blocks=[
                        ToolUseBlock(
                            name="run_shell",
                            input={"command": "rm -rf /"},
                        ),
                    ],
                    stop_reason="tool_use",
                    label="dangerous",
                ),
                ScriptedTurn(
                    blocks=[
                        TextBlock(
                            "Refusing dangerous command. Please specify a path."
                        ),
                    ],
                    stop_reason="end_turn",
                ),
            ]
        ),
        expected_tool_calls=[
            ToolCallExpectation(
                name="run_shell",
                input_subset={"command": "rm -rf /"},
                expected_status="error",
            )
        ],
        expected_final_text_contains=["dangerous"],
        tags=("permissions", "shell-risk"),
    )


def _scn_compact_then_continue() -> EvalScenario:
    return EvalScenario(
        id="compact_then_continue",
        title="Compaction → still aware of files already read/modified",
        summary=(
            "Generates enough turns to trip macro+microcompaction, then "
            "continues editing. Custom predicate asserts the post-compact "
            "transcript still references the files touched earlier."
        ),
        user_message="Trace the auth flow and clean up dead helpers.",
        script=TranscriptScript(
            turns=[
                ScriptedTurn(
                    blocks=[
                        ToolUseBlock(
                            name="read_file",
                            input={"path": "src/auth/main.py"},
                        ),
                    ],
                    stop_reason="tool_use",
                    label="initial_read",
                ),
                ScriptedTurn(
                    blocks=[
                        ToolUseBlock(
                            name="grep_search",
                            input={"pattern": "verify_token"},
                        ),
                    ],
                    stop_reason="tool_use",
                    label="grep",
                ),
                ScriptedTurn(
                    blocks=[
                        ToolUseBlock(
                            name="read_file",
                            input={"path": "src/auth/helpers.py"},
                        ),
                    ],
                    stop_reason="tool_use",
                    label="post_compact_read",
                ),
                ScriptedTurn(
                    blocks=[
                        ToolUseBlock(
                            name="edit_file",
                            input={
                                "path": "src/auth/helpers.py",
                                "patch": "remove_dead_code",
                            },
                        ),
                    ],
                    stop_reason="tool_use",
                    label="post_compact_edit",
                ),
                ScriptedTurn(
                    blocks=[
                        TextBlock(
                            "Removed dead helpers in src/auth/helpers.py."
                        ),
                    ],
                    stop_reason="end_turn",
                ),
            ]
        ),
        expected_tool_calls=[
            ToolCallExpectation(name="read_file"),
            ToolCallExpectation(name="grep_search"),
            ToolCallExpectation(name="edit_file"),
        ],
        expected_final_text_contains=["src/auth/helpers.py"],
        tags=("compact", "long-context"),
    )


def _scn_skill_index_view_run() -> EvalScenario:
    return EvalScenario(
        id="skill_index_view_run",
        title="skill_index → skill_view → script_inspect → script_run",
        summary=(
            "Exercises the skill-discovery family: list skills, view a "
            "playbook, inspect a script proposal, run it."
        ),
        user_message="Run the daily workspace janitor.",
        script=TranscriptScript(
            turns=[
                ScriptedTurn(
                    blocks=[
                        ToolUseBlock(name="skill_index", input={}),
                    ],
                    stop_reason="tool_use",
                ),
                ScriptedTurn(
                    blocks=[
                        ToolUseBlock(
                            name="skill_view",
                            input={"skill_id": "workspace_janitor"},
                        ),
                    ],
                    stop_reason="tool_use",
                ),
                ScriptedTurn(
                    blocks=[
                        ToolUseBlock(
                            name="script_inspect",
                            input={"script_id": "workspace_janitor"},
                        ),
                    ],
                    stop_reason="tool_use",
                ),
                ScriptedTurn(
                    blocks=[
                        ToolUseBlock(
                            name="script_run",
                            input={"script_id": "workspace_janitor"},
                        ),
                    ],
                    stop_reason="tool_use",
                ),
                ScriptedTurn(
                    blocks=[
                        TextBlock("Janitor finished."),
                    ],
                    stop_reason="end_turn",
                ),
            ]
        ),
        expected_tool_calls=[
            ToolCallExpectation(name="skill_index"),
            ToolCallExpectation(name="skill_view"),
            ToolCallExpectation(name="script_inspect"),
            ToolCallExpectation(name="script_run"),
        ],
        tags=("skills",),
    )


def _scn_mcp_session_expired_retry() -> EvalScenario:
    return EvalScenario(
        id="mcp_session_expired_retry",
        title="MCP session expired → reconnect → retry",
        summary=(
            "First MCP tool call returns ``session_expired``; the model "
            "calls a reconnect tool, then retries the original action."
        ),
        user_message="Fetch the latest GH issues via MCP.",
        script=TranscriptScript(
            turns=[
                ScriptedTurn(
                    blocks=[
                        ToolUseBlock(
                            name="mcp_call",
                            input={"server": "github", "tool": "list_issues"},
                        ),
                    ],
                    stop_reason="tool_use",
                    label="initial_attempt",
                ),
                ScriptedTurn(
                    blocks=[
                        ToolUseBlock(
                            name="mcp_reconnect",
                            input={"server": "github"},
                        ),
                    ],
                    stop_reason="tool_use",
                    label="reconnect",
                ),
                ScriptedTurn(
                    blocks=[
                        ToolUseBlock(
                            name="mcp_call",
                            input={"server": "github", "tool": "list_issues"},
                        ),
                    ],
                    stop_reason="tool_use",
                    label="retry",
                ),
                ScriptedTurn(
                    blocks=[TextBlock("Pulled 5 open issues.")],
                    stop_reason="end_turn",
                ),
            ]
        ),
        expected_tool_calls=[
            ToolCallExpectation(name="mcp_call", expected_status="error"),
            ToolCallExpectation(name="mcp_reconnect"),
            ToolCallExpectation(name="mcp_call", expected_status="ok"),
        ],
        tags=("mcp", "recovery"),
    )


def _scn_subagent_async_completion() -> EvalScenario:
    return EvalScenario(
        id="subagent_async_completion",
        title="Subagent async completion → final report unaffected",
        summary=(
            "Parent dispatches a subagent task; subagent completes "
            "asynchronously. Parent transcript should *not* contain the "
            "subagent's intermediate steps, only the final summary."
        ),
        user_message="Have the market_analyst subagent summarise BTC volatility.",
        script=TranscriptScript(
            turns=[
                ScriptedTurn(
                    blocks=[
                        ToolUseBlock(
                            name="dispatch_subagent",
                            input={
                                "name": "market_analyst",
                                "prompt": "BTC volatility 24h",
                            },
                        ),
                    ],
                    stop_reason="tool_use",
                    label="dispatch",
                ),
                ScriptedTurn(
                    blocks=[
                        TextBlock(
                            "BTC realised vol up 12% over 24h."
                        ),
                    ],
                    stop_reason="end_turn",
                    label="parent_final",
                ),
            ]
        ),
        expected_tool_calls=[
            ToolCallExpectation(name="dispatch_subagent"),
        ],
        expected_final_text_contains=["BTC", "vol"],
        tags=("subagent",),
    )


def _scn_interrupt_during_tool_use() -> EvalScenario:
    return EvalScenario(
        id="interrupt_during_tool_use",
        title="Operator interrupt mid-turn → resume preserves transcript",
        summary=(
            "Operator signals cancel before the next tool batch. The "
            "loop emits an ``interrupted`` tool_result and exits with "
            "``stop_reason='cancelled'``. A follow-up resume should be "
            "able to pick up from the same transcript."
        ),
        user_message="Run the long_running batch.",
        script=TranscriptScript(
            turns=[
                ScriptedTurn(
                    blocks=[
                        ToolUseBlock(
                            name="run_shell",
                            input={"command": "long_running_batch"},
                        ),
                    ],
                    stop_reason="tool_use",
                ),
                ScriptedTurn(
                    blocks=[
                        TextBlock("Cancelled before final summary."),
                    ],
                    stop_reason="end_turn",
                ),
            ]
        ),
        expected_stop_reason="end_turn",
        expected_tool_calls=[
            ToolCallExpectation(name="run_shell"),
        ],
        tags=("interrupt", "resume"),
    )


# ---------------------------------------------------------------------------
# Catalog
# ---------------------------------------------------------------------------


SCENARIO_TEMPLATES: Mapping[str, Callable[[], EvalScenario]] = {
    "read_grep_edit_shell_final": _scn_read_grep_edit_shell_final,
    "tool_input_schema_correction": _scn_tool_input_schema_correction,
    "permission_ask_approve": _scn_permission_ask_approve,
    "permission_deny_alternative": _scn_permission_deny_alternative,
    "shell_dangerous_blocked": _scn_shell_dangerous_blocked,
    "compact_then_continue": _scn_compact_then_continue,
    "skill_index_view_run": _scn_skill_index_view_run,
    "mcp_session_expired_retry": _scn_mcp_session_expired_retry,
    "subagent_async_completion": _scn_subagent_async_completion,
    "interrupt_during_tool_use": _scn_interrupt_during_tool_use,
}


def scenario_template(scenario_id: str) -> EvalScenario:
    """Return a fresh :class:`EvalScenario` from the catalog.

    Raises :class:`KeyError` if ``scenario_id`` isn't registered.
    Operators are expected to layer their own builders on top — this
    catalog only seeds the canonical Phase 15 reference cases.
    """

    builder = SCENARIO_TEMPLATES[scenario_id]
    return builder()


__all__ = ["SCENARIO_TEMPLATES", "scenario_template"]
