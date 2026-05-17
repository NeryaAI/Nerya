"""Native workspace-primitive tools for the workspace-native agent loop.

These are the *first-class coding tools* that every coding-capable
agent ships with. They are
deliberately decoupled from the legacy ``operator_skill`` so the agent
can use them without going through the legacy planner / skill selector
allowlist.

Modules:

* :mod:`paths`         — workspace path resolver (re-uses the operator
  helpers but lives on its own so we can drop the operator skill later).
* :mod:`file_ops`      — ``read_file``, ``list_dir``, ``edit_file``,
  ``write_file``, ``apply_patch``.
* :mod:`search`        — ``glob`` / ``grep``.
* :mod:`shell`         — ``run_shell`` with risk classifier + timeout.
* :mod:`task`          — ``todo_write``, ``enter_plan_mode``,
  ``exit_plan_mode``.
* :mod:`skill`         — ``skill_index`` / ``skill_view`` /
  ``script_inspect`` / ``script_run``.
* :mod:`skill_tool`    — ``Skill`` (playbook loader).
* :mod:`memory`        — ``memory_recall`` / ``memory_remember`` /
  ``journal_search`` (compatibility for long-term recall).
* :mod:`agents`        — ``subagent_list`` / ``subagent_run`` (parent
  kernel summons child runtimes).
* :mod:`evolve`        — ``evolve_reflect`` / ``evolve_skill_proposal`` /
  ``evolve_proposals`` (self-improvement reflection and workflow-to-skill
  proposals).
* :mod:`recipes`       — ``recipe_list`` / ``recipe_view`` (operator-curated
  named runbooks, complement to the SKILL.md index).
* :mod:`connectors`    — ``connector_list`` / ``connector_view``: enumerate
  the in-process ``ExchangeProviderRegistry`` so the agent can authoritatively
  answer "is venue X integrated?" before claiming a venue is missing /
  authoring a placeholder strategy.
* :mod:`data_api`      — ``data_api``: discover schemas and call bounded
  read-only provider-specific data actions beyond standard ticker / OHLCV
  reads (AkShare tables, wallet provider data, allowlisted OnchainOS reads).
* :mod:`trading`       — ``portfolio_summary`` / ``portfolio_positions`` /
  ``portfolio_pnl`` / ``virtual_ledger`` / ``risk_check`` /
  ``strategy_list`` / ``strategy_view`` / ``strategy_history`` /
  ``kill_switch_set`` / ``trade_intent_submit`` (safety-critical
  trading actions promoted out of the legacy bridge).
* :mod:`llm`           — ``llm_complete`` / ``llm_classify`` /
  ``llm_extract_json`` / ``llm_compress`` (delegate to the workspace's
  ``LLMGateway`` for cheap classification, schema-bound extraction,
  and compression).
* :mod:`bootstrap`     — ``register_native_tools(registry, deps)``.

Implementation notes:
  coding primitives).
"""

from .bootstrap import (
    NativeToolDeps,
    build_native_tool_deps,
    register_native_tools,
)
from .memory import build_system_prompt_block as memory_system_prompt_block
from .skill import SkillIndex, SkillRecord
from .skill_tool import (
    SKILL_TOOL_NAME,
    register_skill_tool,
    skill_tool_handler,
)
from .task import TaskState, TodoItem

__all__ = [
    "NativeToolDeps",
    "SKILL_TOOL_NAME",
    "SkillIndex",
    "SkillRecord",
    "TaskState",
    "TodoItem",
    "build_native_tool_deps",
    "memory_system_prompt_block",
    "register_native_tools",
    "register_skill_tool",
    "skill_tool_handler",
]
