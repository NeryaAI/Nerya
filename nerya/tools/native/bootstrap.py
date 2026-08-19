"""Native tool bootstrap — wires file/search/shell/task/skill handlers into a
:class:`ToolRegistry`.

Used by:

* :class:`nerya.agent.executor.NativeToolExecutor`
* :class:`nerya.agent.loop.WorkspaceNativeAgentLoop`
* the dashboard ``/api/tools`` endpoint that previews the live registry

The bootstrap function returns a :class:`NativeToolDeps` bundle so the
caller can later install MCP / legacy adapters on the same registry
without re-creating the dependency objects.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Optional

from ...agent.file_state import FileStateCache
from ...core.config import Config
from ...core.paths import WorkspacePaths
from ...skills.kernel import SkillKernel
from ..registry import ToolRegistry, make_native_descriptor
from ..types import (
    PermissionScope,
    RiskLevel,
    ToolCall,
    ToolError,
    ToolErrorKind,
    ToolResult,
)
from .accounts import (
    ACCOUNT_LIST_SCHEMA,
    ACCOUNT_UPSERT_SCHEMA,
    account_list_handler,
    account_upsert_handler,
)
from .agents import (
    RESEARCH_RUN_SCHEMA,
    ROLE_DELETE_SCHEMA,
    ROLE_GET_SCHEMA,
    ROLE_LIST_SCHEMA,
    ROLE_SAVE_SCHEMA,
    SUBAGENT_LIST_SCHEMA,
    SUBAGENT_RUN_SCHEMA,
    TEAM_RUN_SCHEMA,
    research_run_handler,
    role_delete_handler,
    role_get_handler,
    role_list_handler,
    role_save_handler,
    subagent_list_handler,
    subagent_run_handler,
    team_run_handler,
)
from .connectors import (
    CONNECTOR_LIST_SCHEMA,
    CONNECTOR_VIEW_SCHEMA,
    MARKET_DATA_SCHEMA,
    connector_list_handler,
    connector_view_handler,
    market_data_handler,
)
from .data_api import DATA_API_SCHEMA, data_api_handler
from .data_sources import (
    DATA_SOURCE_STATUS_SCHEMA,
    DATA_SOURCE_SYNC_NOW_SCHEMA,
    data_source_status_handler,
    data_source_sync_now_handler,
)
from .evolve import (
    EVOLVE_CORE_CONFIG_PATCH_SCHEMA,
    EVOLVE_POST_APPLY_OBSERVATION_SCHEMA,
    EVOLVE_PROVIDER_PROPOSAL_SCHEMA,
    EVOLVE_PROPOSALS_SCHEMA,
    EVOLVE_REFLECT_SCHEMA,
    EVOLVE_SKILL_PROPOSAL_SCHEMA,
    evolve_core_config_patch_handler,
    evolve_post_apply_observation_handler,
    evolve_provider_proposal_handler,
    evolve_proposals_handler,
    evolve_reflect_handler,
    evolve_skill_proposal_handler,
)
from .workspace_ui import (
    WORKSPACE_UI_INSPECT_SCHEMA,
    WORKSPACE_UI_PROPOSE_SCHEMA,
    workspace_ui_inspect_handler,
    workspace_ui_propose_handler,
)
from .file_ops import (
    classify_file_mutation_risk,
    edit_file_handler,
    list_dir_handler,
    read_file_handler,
    write_file_handler,
)
from .gateway import GATEWAY_DIAGNOSE_SCHEMA, gateway_diagnose_handler
from .llm import (
    LLM_CLASSIFY_SCHEMA,
    LLM_COMPLETE_SCHEMA,
    LLM_COMPRESS_SCHEMA,
    LLM_EXTRACT_JSON_SCHEMA,
    llm_classify_handler,
    llm_complete_handler,
    llm_compress_handler,
    llm_extract_json_handler,
)
from .memory import (
    JOURNAL_SEARCH_SCHEMA,
    MEMORY_RECALL_SCHEMA,
    MEMORY_REMEMBER_SCHEMA,
    journal_search_handler,
    memory_recall_handler,
    memory_remember_handler,
)
from .recipes import (
    RECIPE_LIST_SCHEMA,
    RECIPE_VIEW_SCHEMA,
    recipe_list_handler,
    recipe_view_handler,
)
from .resources import (
    RESOURCE_LIST_SCHEMA,
    RESOURCE_READ_SCHEMA,
    resource_list_handler,
    resource_read_handler,
)
from .search import glob_handler, grep_handler
from .tasks import (
    SUBAGENT_RUN_ASYNC_SCHEMA,
    TASK_CREATE_SCHEMA,
    TASK_GET_SCHEMA,
    TASK_LIST_SCHEMA,
    TASK_OUTPUT_SCHEMA,
    TASK_STOP_SCHEMA,
    TASK_SUMMARY_SCHEMA,
    TASK_UPDATE_SCHEMA,
    subagent_run_async_handler,
    task_create_handler,
    task_get_handler,
    task_list_handler,
    task_output_handler,
    task_stop_handler,
    task_summary_handler,
    task_update_handler,
)
from .shell import classify_shell_risk, run_shell_handler
from .skill import (
    SkillIndex,
    is_browser_skill_script_run,
    script_inspect_handler,
    script_run_handler,
    skill_index_handler,
    skill_view_handler,
)
from .skill_tool import register_skill_tool
from .task import (
    TaskState,
    enter_plan_mode_handler,
    exit_plan_mode_handler,
    plan_status_handler,
    todo_write_handler,
)
from .trading import (
    KILL_SWITCH_SET_SCHEMA,
    PORTFOLIO_PNL_SCHEMA,
    PORTFOLIO_POSITIONS_SCHEMA,
    PORTFOLIO_SUMMARY_SCHEMA,
    RISK_CHECK_SCHEMA,
    STRATEGY_HISTORY_SCHEMA,
    STRATEGY_LIST_SCHEMA,
    STRATEGY_VIEW_SCHEMA,
    TRADE_INTENT_SUBMIT_SCHEMA,
    VIRTUAL_LEDGER_SCHEMA,
    kill_switch_set_handler,
    portfolio_pnl_handler,
    portfolio_positions_handler,
    portfolio_summary_handler,
    risk_check_handler,
    strategy_history_handler,
    strategy_list_handler,
    strategy_view_handler,
    trade_intent_submit_handler,
    virtual_ledger_handler,
)
from .web import (
    WEB_FETCH_SCHEMA,
    WEB_SEARCH_FETCH_SCHEMA,
    WEB_SEARCH_SCHEMA,
    web_fetch_handler,
    web_search_fetch_handler,
    web_search_handler,
)
from .strategy_runtime import (
    STRATEGY_BACKTEST_SCHEMA,
    STRATEGY_DELETE_PROPOSAL_SCHEMA,
    STRATEGY_DRAFT_PROPOSAL_SCHEMA,
    STRATEGY_KILL_SWITCH_SCHEMA,
    STRATEGY_PROMOTE_SCHEMA,
    STRATEGY_RUN_HISTORY_SCHEMA,
    STRATEGY_RUN_TICK_SCHEMA,
    STRATEGY_SUBMIT_PROPOSAL_SCHEMA,
    STRATEGY_TUNING_GENERATE_SCHEMA,
    STRATEGY_TUNING_RUN_SCHEMA,
    STRATEGY_TUNING_SNAPSHOT_SCHEMA,
    STRATEGY_TUNING_STATUS_SCHEMA,
    STRATEGY_VALIDATE_SCHEMA,
    strategy_backtest_handler,
    strategy_delete_proposal_handler,
    strategy_draft_proposal_handler,
    strategy_kill_switch_handler,
    strategy_promote_handler,
    strategy_run_history_handler,
    strategy_run_tick_handler,
    strategy_submit_proposal_handler,
    strategy_tuning_generate_handler,
    strategy_tuning_run_handler,
    strategy_tuning_snapshot_handler,
    strategy_tuning_status_handler,
    strategy_validate_handler,
)


# ---------------------------------------------------------------------------
# Dependency bundle
# ---------------------------------------------------------------------------


@dataclass
class NativeToolDeps:
    """Shared dependencies for native tools.

    Held by :class:`NativeToolExecutor` and threaded into each handler
    via partial application during bootstrap. Mutable so the executor
    can swap them on workspace switch (e.g. CLI ``cd`` between
    workspaces).
    """

    workspace_root: Path
    file_state: FileStateCache
    task_state: TaskState
    skill_index: SkillIndex
    skill_roots: list[Path] = field(default_factory=list)
    shell_default_timeout_sec: float = 60.0
    shell_max_output_bytes: int = 64_000
    background_processes: dict[str, dict] = field(default_factory=dict)
    task_store: Optional["Any"] = None
    """Async subagent task store. Held on deps so every
    ``subagent_run_async`` / ``task_*`` call shares the same on-disk
    registry. Built lazily from ``paths`` when missing."""

    resource_index: Optional["Any"] = None
    """Workspace :class:`ResourceIndex`. Holds MCP-published
    resources alongside any local read-only documents. ``resource_list``
    / ``resource_read`` tools dispatch through it."""

    paths: Optional[WorkspacePaths] = None
    """Workspace path layout used by tools that read/write the long-term
    memory + journals (see :mod:`nerya.tools.native.memory`). Optional so
    older callers that only want file/shell tools keep working — the
    memory tools simply skip registration when ``paths`` is ``None``."""

    config: Optional[Config] = None
    """Workspace ``Config`` — required by the subagent + evolve tools
    (they read journals, dispatch child runtimes, file proposals).
    Tools that need it skip registration when ``None``."""

    skills: Optional[SkillKernel] = None
    """Boot-time skill kernel — only the ``subagent_run`` tool needs it
    (the child runtime dispatches skills through the parent kernel).
    Optional so the CLI ad-hoc bootstrap path stays minimal."""

    tool_registry: Optional[ToolRegistry] = None
    """Native tool registry exposed to child subagent runtimes."""

    executor: Optional[Any] = None
    """Parent-owned :class:`NativeToolExecutor` for delegated calls.

    The dependency bundle is created before the per-turn permission context,
    so the kernel fills this slot once it has built that turn's executor.
    Child runtimes must use this object instead of invoking descriptors
    directly; keeping it optional preserves ad-hoc bootstrap compatibility.
    """

    active_strategy_id: Optional[str] = None
    """Strategy scoped to the current Agent turn, if any."""

    active_session_id: Optional[str] = None
    """Agent session scoped to the current turn, if any."""

    active_conversation_id: Optional[str] = None
    """File-placement scope for the current turn (session id or turn id)."""

    active_actor_id: str = "default"
    """Trusted operator/gateway actor scoped to the current turn."""

    active_trigger_event_id: Optional[str] = None
    """Trigger event currently driving the Agent turn, if any."""

    active_trigger_source: str = ""
    """Source of the current Agent trigger, for handler defaults."""

    strategy_order_auto_approve: bool = False
    """True only for strategy-triggered turns allowed to run strategy
    runtime tools / submit strategy orders without the native tool
    permission card. Domain gates still enforce trading invariants."""

    permission_mode: str = "default"
    """Current agent turn permission mode, threaded to plan tools."""


# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------


_READ_FILE_SCHEMA = {
    "type": "object",
    "properties": {
        "path": {"type": "string", "description": "Workspace-relative path."},
        "offset": {"type": "integer", "minimum": 1, "description": "1-based start line."},
        "limit": {"type": "integer", "minimum": 1, "description": "Max lines to return."},
    },
    "required": ["path"],
}

_LIST_DIR_SCHEMA = {
    "type": "object",
    "properties": {
        "path": {"type": "string"},
        "recursive": {"type": "boolean", "default": False},
        "max_entries": {"type": "integer", "minimum": 1, "default": 200},
    },
    "required": ["path"],
}

_GLOB_SCHEMA = {
    "type": "object",
    "properties": {
        "pattern": {"type": "string"},
        "path": {"type": "string", "description": "Workspace-relative root."},
        "max_entries": {"type": "integer", "minimum": 1, "default": 200},
    },
    "required": ["pattern"],
}

_GREP_SCHEMA = {
    "type": "object",
    "properties": {
        "pattern": {"type": "string", "description": "Regex pattern."},
        "path": {"type": "string"},
        "glob": {"type": "string"},
        "type": {"type": "string", "description": "rg --type alias."},
        "case_insensitive": {"type": "boolean", "default": False},
        "max_results": {"type": "integer", "minimum": 1, "default": 200},
    },
    "required": ["pattern"],
}

_EDIT_FILE_SCHEMA = {
    "type": "object",
    "properties": {
        "path": {"type": "string"},
        "old_string": {"type": "string"},
        "new_string": {"type": "string"},
        "replace_all": {"type": "boolean", "default": False},
    },
    "required": ["path", "old_string", "new_string"],
}

_WRITE_FILE_SCHEMA = {
    "type": "object",
    "properties": {
        "path": {"type": "string"},
        "contents": {"type": "string"},
        "allow_outside_conversation": {
            "type": "boolean",
            "default": False,
            "description": (
                "Exceptional: create a new file outside the current "
                "conversation directory. Requires outside_conversation_reason."
            ),
        },
        "outside_conversation_reason": {
            "type": "string",
            "description": "Why this new file needs its requested canonical path.",
        },
    },
    "required": ["path", "contents"],
}

_RUN_SHELL_SCHEMA = {
    "type": "object",
    "properties": {
        "command": {"type": "string"},
        "cwd": {"type": "string"},
        "timeout_sec": {"type": "number", "minimum": 0.1},
        "background": {"type": "boolean", "default": False},
        "description": {"type": "string"},
        "allow_outside_conversation": {
            "type": "boolean",
            "default": False,
            "description": (
                "Exceptional: let a file-mutating command run outside the "
                "current conversation directory. Requires "
                "outside_conversation_reason."
            ),
        },
        "outside_conversation_reason": {
            "type": "string",
            "description": "Why the command must write at its requested path.",
        },
    },
    "required": ["command"],
}

_WALLET_INSTALL_SCHEMA = {
    "type": "object",
    "properties": {
        "provider": {
            "type": "string",
            "default": "self_custody",
            "description": "Wallet provider id from the wallet catalog.",
        },
        "mode": {
            "type": "string",
            "enum": ["default", "goat"],
            "default": "default",
            "description": "Use goat for the self_custody GOAT SDK bootstrap path.",
        },
        "command": {
            "type": "string",
            "description": "Optional catalog-listed install command override.",
        },
        "approve": {
            "type": "boolean",
            "default": False,
            "description": "Operator approval flag; installs are still gated by runtime policy.",
        },
    },
}

_TODO_WRITE_SCHEMA = {
    "type": "object",
    "properties": {
        "todos": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "id": {"type": "string"},
                    "content": {"type": "string"},
                    "activeForm": {"type": "string"},
                    "status": {
                        "type": "string",
                        "enum": ["pending", "in_progress", "completed", "cancelled"],
                    },
                },
                "required": ["content"],
            },
        },
    },
    "required": ["todos"],
}

_ENTER_PLAN_MODE_SCHEMA = {
    "type": "object",
    "properties": {},
}

_EXIT_PLAN_MODE_SCHEMA = {
    "type": "object",
    "properties": {
        "plan": {"type": "string", "description": "Markdown plan body."},
    },
    "required": ["plan"],
}

_PLAN_STATUS_SCHEMA = {
    "type": "object",
    "properties": {
        "plan_id": {
            "type": "string",
            "description": (
                "Optional plan id to inspect (returned by exit_plan_mode). "
                "Omit to inspect the most recent submission."
            ),
        },
    },
}

_SKILL_INDEX_SCHEMA = {
    "type": "object",
    "properties": {
        "refresh": {"type": "boolean", "default": False},
    },
}

_SKILL_VIEW_SCHEMA = {
    "type": "object",
    "properties": {
        "skill_id": {"type": "string"},
        "file": {
            "type": "string",
            "description": (
                "Optional path relative to the skill directory (e.g. "
                "'references/full-playbook.md') to read a skill asset "
                "instead of SKILL.md. Builtin skill assets are not "
                "reachable via read_file, so use this."
            ),
        },
    },
    "required": ["skill_id"],
}

_SCRIPT_INSPECT_SCHEMA = {
    "type": "object",
    "properties": {
        "skill_id": {"type": "string"},
        "name": {"type": "string"},
    },
    "required": ["skill_id", "name"],
}

_SCRIPT_RUN_SCHEMA = {
    "type": "object",
    "properties": {
        "skill_id": {"type": "string"},
        "name": {"type": "string"},
        "args": {"type": "array", "items": {"type": "string"}},
        "timeout_sec": {"type": "number", "minimum": 0.1},
    },
    "required": ["skill_id", "name"],
}


# ---------------------------------------------------------------------------
# Adapters (close over deps)
# ---------------------------------------------------------------------------


def _wrap_read_file(deps: NativeToolDeps):
    def handler(call: ToolCall):
        return read_file_handler(call, root=deps.workspace_root, file_state=deps.file_state)

    return handler


def _wrap_list_dir(deps: NativeToolDeps):
    def handler(call: ToolCall):
        return list_dir_handler(call, root=deps.workspace_root)

    return handler


def _wrap_edit_file(deps: NativeToolDeps):
    def handler(call: ToolCall):
        return edit_file_handler(call, root=deps.workspace_root, file_state=deps.file_state)

    return handler


def _wrap_write_file(deps: NativeToolDeps):
    def handler(call: ToolCall):
        return write_file_handler(
            call,
            root=deps.workspace_root,
            file_state=deps.file_state,
            session_id=deps.active_conversation_id or deps.active_session_id,
        )

    return handler


def _wrap_glob(deps: NativeToolDeps):
    def handler(call: ToolCall):
        return glob_handler(call, root=deps.workspace_root)

    return handler


def _wrap_grep(deps: NativeToolDeps):
    def handler(call: ToolCall):
        return grep_handler(call, root=deps.workspace_root)

    return handler


def _wrap_run_shell(deps: NativeToolDeps):
    def handler(call: ToolCall):
        return run_shell_handler(
            call,
            root=deps.workspace_root,
            session_id=deps.active_conversation_id or deps.active_session_id,
        )

    return handler


def _wrap_todo_write(deps: NativeToolDeps):
    def handler(call: ToolCall):
        return todo_write_handler(call, task_state=deps.task_state)

    return handler


def _wrap_enter_plan(deps: NativeToolDeps):
    def handler(call: ToolCall):
        return enter_plan_mode_handler(call, task_state=deps.task_state)

    return handler


def _wrap_exit_plan(deps: NativeToolDeps):
    def handler(call: ToolCall):
        return exit_plan_mode_handler(
            call,
            task_state=deps.task_state,
            permission_mode=deps.permission_mode,
        )

    return handler


def _wrap_plan_status(deps: NativeToolDeps):
    def handler(call: ToolCall):
        return plan_status_handler(call, task_state=deps.task_state)

    return handler


def _wrap_skill_index(deps: NativeToolDeps):
    def handler(call: ToolCall):
        return skill_index_handler(call, skill_index=deps.skill_index)

    return handler


def _wrap_skill_view(deps: NativeToolDeps):
    def handler(call: ToolCall):
        return skill_view_handler(call, skill_index=deps.skill_index)

    return handler


def _wrap_market_data(deps: NativeToolDeps):
    def handler(call: ToolCall):
        return market_data_handler(call, config_like=deps.config)

    return handler


def _wrap_connector_view(deps: NativeToolDeps):
    def handler(call: ToolCall):
        return connector_view_handler(call, config_like=deps.config)

    return handler


def _wrap_data_api(deps: NativeToolDeps):
    def handler(call: ToolCall):
        return data_api_handler(call, config_like=deps.config)

    return handler


def _wrap_data_source_status(deps: NativeToolDeps):
    def handler(call: ToolCall):
        return data_source_status_handler(call, config=deps.config)

    return handler


def _wrap_data_source_sync_now(deps: NativeToolDeps):
    def handler(call: ToolCall):
        return data_source_sync_now_handler(call, config=deps.config)

    return handler


def _wrap_account_list(deps: NativeToolDeps):
    def handler(call: ToolCall):
        return account_list_handler(call, config_like=deps.config)

    return handler


def _wrap_account_upsert(deps: NativeToolDeps):
    def handler(call: ToolCall):
        return account_upsert_handler(call, config_like=deps.config)

    return handler


def _wrap_wallet_install(deps: NativeToolDeps):
    def handler(call: ToolCall):
        if deps.config is None:
            return ToolResult.from_error(
                tool_use_id=call.id,
                name=call.name,
                error=ToolError(
                    kind=ToolErrorKind.EXECUTION_ERROR,
                    message="wallet_install requires a workspace Config",
                    retryable=False,
                ),
            )
        args = dict(call.arguments or {})
        provider = str(args.get("provider") or "self_custody").strip().lower()
        mode = str(args.get("mode") or "default").strip().lower()
        override = str(args.get("command") or "").strip()
        approve = bool(args.get("approve", False)) or deps.permission_mode == "yolo"
        try:
            from ...install.dep_installer import install as run_install
            from ...wallet import PROVIDERS

            entry = PROVIDERS.get(provider)
            if not entry:
                return ToolResult.from_error(
                    tool_use_id=call.id,
                    name=call.name,
                    error=ToolError(
                        kind=ToolErrorKind.NOT_FOUND,
                        message=f"unknown wallet provider: {provider}",
                        detail={"known": sorted(PROVIDERS)},
                        retryable=False,
                    ),
                )
            allowed = {str(entry.get("install_command") or "").strip()}
            for alt in entry.get("install_alternatives") or []:
                if isinstance(alt, dict):
                    allowed.add(str(alt.get("command") or "").strip())
            allowed.discard("")
            if override:
                if override not in allowed:
                    return ToolResult.from_error(
                        tool_use_id=call.id,
                        name=call.name,
                        error=ToolError(
                            kind=ToolErrorKind.PERMISSION_DENIED,
                            message="install command is not in the wallet provider catalog",
                            detail={"provider": provider, "allowed": sorted(allowed)},
                            retryable=False,
                        ),
                    )
                commands = [override]
            elif provider == "self_custody" and mode == "goat":
                commands = [
                    "npm:@goat-sdk/core",
                    "npm:@goat-sdk/wallet-viem",
                    str(entry.get("install_command") or "").strip(),
                ]
            else:
                commands = [str(entry.get("install_command") or "").strip()]
            commands = [cmd for cmd in commands if cmd]
            if not commands:
                return ToolResult.from_json(
                    tool_use_id=call.id,
                    name=call.name,
                    data={"ok": True, "provider": provider, "skipped": True, "reason": "no_install_command"},
                )
            results = []
            for command in commands:
                result = run_install(
                    deps.config.paths,
                    command,
                    config_data=deps.config.data,
                    approve=approve,
                )
                results.append(result.asdict())
                if result.skipped or not result.ok:
                    break
            return ToolResult.from_json(
                tool_use_id=call.id,
                name=call.name,
                data={
                    "ok": bool(results) and all(row.get("ok") for row in results),
                    "provider": provider,
                    "mode": mode,
                    "results": results,
                    "next": (
                        "refresh provider capability discovery and follow "
                        "the concrete next_required_action or call shape "
                        "returned by that tool result"
                    ),
                },
            )
        except Exception as exc:  # noqa: BLE001 - native tool should return structured errors.
            return ToolResult.from_error(
                tool_use_id=call.id,
                name=call.name,
                error=ToolError(
                    kind=ToolErrorKind.EXECUTION_ERROR,
                    message=str(exc),
                    detail={"provider": provider, "mode": mode},
                    retryable=None,
                ),
            )

    return handler


def _wrap_script_inspect(deps: NativeToolDeps):
    def handler(call: ToolCall):
        return script_inspect_handler(call, skill_index=deps.skill_index)

    return handler


def _wrap_script_run(deps: NativeToolDeps):
    def handler(call: ToolCall):
        return script_run_handler(call, skill_index=deps.skill_index, cwd=deps.workspace_root)

    return handler


def _auto_approve_skill_script_run(deps: NativeToolDeps, payload: dict[str, Any]) -> bool:
    # Browser automation is the only built-in script lane with a dedicated
    # low-risk policy. All other scripts use the normal EXEC approval gate.
    return is_browser_skill_script_run(payload)


def _wrap_memory_recall(deps: NativeToolDeps):
    def handler(call: ToolCall):
        from ...memory.runtime import MemoryRuntime

        config = deps.config or Config(paths=deps.paths, data={})
        runtime = MemoryRuntime(
            config,
            actor_id=deps.active_actor_id or "default",
            session_id=deps.active_session_id or "",
            strategy_id=deps.active_strategy_id or "",
        )
        return memory_recall_handler(call, runtime=runtime)

    return handler


def _wrap_memory_remember(deps: NativeToolDeps):
    def handler(call: ToolCall):
        from ...memory.runtime import MemoryRuntime

        config = deps.config or Config(paths=deps.paths, data={})
        runtime = MemoryRuntime(
            config,
            actor_id=deps.active_actor_id or "default",
            session_id=deps.active_session_id or "",
            strategy_id=deps.active_strategy_id or "",
        )
        return memory_remember_handler(call, runtime=runtime)

    return handler


def _wrap_journal_search(deps: NativeToolDeps):
    def handler(call: ToolCall):
        return journal_search_handler(call, paths=deps.paths)

    return handler


def _wrap_recipe_list(deps: NativeToolDeps):
    def handler(call: ToolCall):
        return recipe_list_handler(call, skills=deps.skills, paths=deps.paths)

    return handler


def _wrap_recipe_view(deps: NativeToolDeps):
    def handler(call: ToolCall):
        return recipe_view_handler(call, skills=deps.skills, paths=deps.paths)

    return handler


def _wrap_subagent_run_async(deps: NativeToolDeps):
    def handler(call: ToolCall):
        return subagent_run_async_handler(
            call,
            config=deps.config,
            skills=deps.skills,
            store=deps.task_store,
        )

    return handler


def _wrap_task_create(deps: NativeToolDeps):
    def handler(call: ToolCall):
        return task_create_handler(call, workspace=deps.workspace_root)

    return handler


def _wrap_task_list(deps: NativeToolDeps):
    def handler(call: ToolCall):
        return task_list_handler(call, store=deps.task_store)

    return handler


def _wrap_task_get(deps: NativeToolDeps):
    def handler(call: ToolCall):
        return task_get_handler(call, store=deps.task_store)

    return handler


def _wrap_task_output(deps: NativeToolDeps):
    def handler(call: ToolCall):
        return task_output_handler(call, store=deps.task_store)

    return handler


def _wrap_task_stop(deps: NativeToolDeps):
    def handler(call: ToolCall):
        return task_stop_handler(call, store=deps.task_store)

    return handler


def _wrap_task_update(deps: NativeToolDeps):
    def handler(call: ToolCall):
        return task_update_handler(call, store=deps.task_store)

    return handler


def _wrap_task_summary(deps: NativeToolDeps):
    def handler(call: ToolCall):
        return task_summary_handler(call, store=deps.task_store)

    return handler


def _wrap_resource_list(deps: NativeToolDeps):
    def handler(call: ToolCall):
        return resource_list_handler(call, index=deps.resource_index)

    return handler


def _wrap_resource_read(deps: NativeToolDeps):
    def handler(call: ToolCall):
        return resource_read_handler(call, index=deps.resource_index)

    return handler


def _wrap_subagent_list(deps: NativeToolDeps):
    def handler(call: ToolCall):
        return subagent_list_handler(call, config=deps.config)

    return handler


def _wrap_subagent_run(deps: NativeToolDeps):
    def handler(call: ToolCall):
        return subagent_run_handler(
            call,
            config=deps.config,
            skills=deps.skills,
            tool_registry=deps.tool_registry,
            executor=deps.executor,
        )

    return handler


def _wrap_team_run(deps: NativeToolDeps):
    def handler(call: ToolCall):
        return team_run_handler(
            call,
            config=deps.config,
            skills=deps.skills,
            tool_registry=deps.tool_registry,
            executor=deps.executor,
        )

    return handler


def _wrap_research_run(deps: NativeToolDeps):
    def handler(call: ToolCall):
        return research_run_handler(
            call,
            config=deps.config,
            skills=deps.skills,
            tool_registry=deps.tool_registry,
            executor=deps.executor,
        )

    return handler


def _wrap_web_search(deps: NativeToolDeps):
    def handler(call: ToolCall):
        return web_search_handler(call, workspace_root=deps.workspace_root)

    return handler


def _wrap_web_fetch(deps: NativeToolDeps):
    def handler(call: ToolCall):
        return web_fetch_handler(call, workspace_root=deps.workspace_root)

    return handler


def _wrap_web_search_fetch(deps: NativeToolDeps):
    def handler(call: ToolCall):
        return web_search_fetch_handler(call, workspace_root=deps.workspace_root)

    return handler


def _wrap_role_list(deps: NativeToolDeps):
    def handler(call: ToolCall):
        return role_list_handler(call, config=deps.config)

    return handler


def _wrap_role_get(deps: NativeToolDeps):
    def handler(call: ToolCall):
        return role_get_handler(call, config=deps.config)

    return handler


def _wrap_role_save(deps: NativeToolDeps):
    def handler(call: ToolCall):
        return role_save_handler(call, config=deps.config)

    return handler


def _wrap_role_delete(deps: NativeToolDeps):
    def handler(call: ToolCall):
        return role_delete_handler(call, config=deps.config)

    return handler


def _wrap_workspace_ui_inspect(deps: NativeToolDeps):
    def handler(call: ToolCall):
        return workspace_ui_inspect_handler(call, config=deps.config)

    return handler


def _wrap_workspace_ui_propose(deps: NativeToolDeps):
    def handler(call: ToolCall):
        return workspace_ui_propose_handler(call, config=deps.config)

    return handler


def _wrap_evolve_reflect(deps: NativeToolDeps):
    def handler(call: ToolCall):
        return evolve_reflect_handler(call, config=deps.config)

    return handler


def _wrap_evolve_proposals(deps: NativeToolDeps):
    def handler(call: ToolCall):
        return evolve_proposals_handler(call, config=deps.config)

    return handler


def _wrap_evolve_skill_proposal(deps: NativeToolDeps):
    def handler(call: ToolCall):
        return evolve_skill_proposal_handler(call, config=deps.config)

    return handler


def _wrap_evolve_core_config_patch(deps: NativeToolDeps):
    def handler(call: ToolCall):
        return evolve_core_config_patch_handler(call, config=deps.config)

    return handler


def _wrap_evolve_provider_proposal(deps: NativeToolDeps):
    def handler(call: ToolCall):
        return evolve_provider_proposal_handler(call, config=deps.config)

    return handler


def _wrap_evolve_post_apply_observation(deps: NativeToolDeps):
    def handler(call: ToolCall):
        return evolve_post_apply_observation_handler(call, config=deps.config)

    return handler


# Trading domain — every wrapper closes over ``config`` so the underlying
# RiskGate / ExecutionEngine / StateStore see the same workspace layout
# the rest of the runtime is using.


def _wrap_portfolio_summary(deps: NativeToolDeps):
    def handler(call: ToolCall):
        return portfolio_summary_handler(call, config=deps.config)

    return handler


def _wrap_portfolio_positions(deps: NativeToolDeps):
    def handler(call: ToolCall):
        return portfolio_positions_handler(call, config=deps.config)

    return handler


def _wrap_portfolio_pnl(deps: NativeToolDeps):
    def handler(call: ToolCall):
        return portfolio_pnl_handler(call, config=deps.config)

    return handler


def _wrap_virtual_ledger(deps: NativeToolDeps):
    def handler(call: ToolCall):
        return virtual_ledger_handler(call, config=deps.config)

    return handler


def _wrap_risk_check(deps: NativeToolDeps):
    def handler(call: ToolCall):
        return risk_check_handler(call, config=deps.config)

    return handler


def _wrap_strategy_list(deps: NativeToolDeps):
    def handler(call: ToolCall):
        return strategy_list_handler(call, config=deps.config)

    return handler


def _wrap_strategy_view(deps: NativeToolDeps):
    def handler(call: ToolCall):
        return strategy_view_handler(call, config=deps.config)

    return handler


def _wrap_strategy_history(deps: NativeToolDeps):
    def handler(call: ToolCall):
        return strategy_history_handler(call, config=deps.config)

    return handler


def _wrap_kill_switch_set(deps: NativeToolDeps):
    def handler(call: ToolCall):
        return kill_switch_set_handler(call, config=deps.config)

    return handler


def _tool_permission_denied(
    call: ToolCall,
    message: str,
    *,
    detail: dict[str, Any] | None = None,
) -> ToolResult:
    return ToolResult.from_error(
        tool_use_id=call.id,
        name=call.name,
        error=ToolError(
            kind=ToolErrorKind.PERMISSION_DENIED,
            message=message,
            detail=dict(detail or {}),
            retryable=False,
        ),
    )


def _as_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _tool_allowed(allowed_tools: Any, tool_name: str) -> bool:
    tools = [str(t).strip() for t in (allowed_tools or []) if str(t).strip()]
    if not tools:
        return True
    for tool in tools:
        if tool == tool_name:
            return True
        if tool.endswith(f":{tool_name}") or tool.endswith(f".{tool_name}"):
            return True
    return False


def _estimate_notional_usd(args: dict[str, Any], snapshot: Any) -> float:
    size = _as_float(args.get("size"))
    unit = str(args.get("size_unit") or "").strip().lower()
    if unit in {"usd", "quote"}:
        return size
    price = _as_float(args.get("limit_price")) or _as_float(args.get("stop_price"))
    if not price and isinstance(snapshot, dict):
        price = _as_float(snapshot.get("price") or snapshot.get("mark_price") or snapshot.get("mid"))
    return size * price if price else size


def _load_strategy_agent_profile(deps: NativeToolDeps) -> dict[str, Any] | None:
    if deps.paths is None or not deps.active_session_id:
        return None
    try:
        from ...agent.session_profile import load_strategy_agent_profile

        return load_strategy_agent_profile(deps.paths, deps.active_session_id)
    except Exception:
        return None


def _strategy_agent_trade_call(
    deps: NativeToolDeps,
    call: ToolCall,
) -> ToolCall | ToolResult:
    """Bind a strategy-triggered Agent order to its session profile.

    PermissionEngine may allow ``trade_intent_submit`` for unattended
    strategy turns, but this adapter still pins the actual intent to the
    active strategy/session and enforces the profile's cheap pre-checks.
    The canonical RiskGate / ApprovalGate pipeline still runs after this.
    """

    if not (deps.strategy_order_auto_approve and deps.active_strategy_id):
        return call

    args = dict(call.arguments or {})
    requested_strategy = str(args.get("strategy_id") or "").strip()
    if requested_strategy and requested_strategy != deps.active_strategy_id:
        return _tool_permission_denied(
            call,
            "strategy Agent session cannot submit an order for a different strategy",
            detail={
                "active_strategy_id": deps.active_strategy_id,
                "requested_strategy_id": requested_strategy,
            },
        )

    args["strategy_id"] = deps.active_strategy_id
    if deps.active_trigger_event_id and not args.get("trigger_event_id"):
        args["trigger_event_id"] = deps.active_trigger_event_id

    meta = dict(args.get("meta") or {})
    if deps.active_session_id:
        meta.setdefault("agent_session_id", deps.active_session_id)
    if deps.active_trigger_event_id:
        meta.setdefault("trigger_event_id", deps.active_trigger_event_id)
    meta.setdefault("order_origin", "strategy_agent")
    args["meta"] = meta

    profile_record = _load_strategy_agent_profile(deps)
    profile = dict((profile_record or {}).get("profile") or {})
    risk_limits = dict(profile.get("risk_limits") or {})

    if not _tool_allowed(profile.get("allowed_tools"), "trade_intent_submit"):
        return _tool_permission_denied(
            call,
            "strategy Agent session profile does not allow trade_intent_submit",
            detail={"session_id": deps.active_session_id, "strategy_id": deps.active_strategy_id},
        )

    allowed_accounts = (
        profile.get("allowed_accounts")
        or profile.get("accounts")
        or risk_limits.get("allowed_accounts")
    )
    if allowed_accounts:
        account_id = str(args.get("account_id") or "").strip()
        allowed = {str(a).strip() for a in allowed_accounts if str(a).strip()}
        if account_id and account_id not in allowed:
            return _tool_permission_denied(
                call,
                "strategy Agent session profile blocks this account_id",
                detail={"account_id": account_id, "allowed_accounts": sorted(allowed)},
            )

    allowed_markets = (
        profile.get("allowed_markets")
        or profile.get("markets")
        or risk_limits.get("allowed_markets")
    )
    if allowed_markets:
        market = str(args.get("market") or "").strip()
        allowed = {str(m).strip() for m in allowed_markets if str(m).strip()}
        if market and market not in allowed:
            return _tool_permission_denied(
                call,
                "strategy Agent session profile blocks this market",
                detail={"market": market, "allowed_markets": sorted(allowed)},
            )

    min_confidence = max(
        _as_float(profile.get("min_confidence_to_trade")),
        _as_float(risk_limits.get("min_confidence")),
    )
    confidence = _as_float(args.get("confidence"))
    if min_confidence > 0 and confidence < min_confidence:
        return _tool_permission_denied(
            call,
            "strategy Agent order confidence is below the session profile floor",
            detail={"confidence": confidence, "min_confidence": min_confidence},
        )

    max_single_order = _as_float(risk_limits.get("max_single_order_usd"))
    notional = _estimate_notional_usd(args, args.get("market_snapshot"))
    if max_single_order > 0 and notional > max_single_order:
        return _tool_permission_denied(
            call,
            "strategy Agent order exceeds the session profile max_single_order_usd",
            detail={
                "estimated_notional_usd": notional,
                "max_single_order_usd": max_single_order,
            },
        )

    default_source = str(profile.get("default_trade_source") or "strategy_agent").strip()
    if default_source not in {"strategy_agent", "strategy_triggered_agent", "strategy_runtime"}:
        default_source = "strategy_agent"
    args["source"] = default_source

    return ToolCall(
        name=call.name,
        arguments=args,
        id=call.id,
        turn_id=call.turn_id,
        iteration=call.iteration,
        caller=call.caller,
        started_at=call.started_at,
        parent_call_id=call.parent_call_id,
        metadata=dict(call.metadata or {}),
    )


def _wrap_trade_intent_submit(deps: NativeToolDeps):
    def handler(call: ToolCall):
        guarded = _strategy_agent_trade_call(deps, call)
        if isinstance(guarded, ToolResult):
            return guarded
        if not (deps.strategy_order_auto_approve and deps.active_strategy_id):
            # Provenance is runtime-owned. A model/user must not be able to
            # submit ``source=strategy_runtime`` from an interactive chat and
            # inherit the unattended strategy approval lane.
            args = dict(guarded.arguments or {})
            requested_source = str(args.get("source") or "").strip()
            args["source"] = "agent:native"
            meta = dict(args.get("meta") or {})
            meta["order_origin"] = "operator_agent"
            if requested_source and requested_source != "agent:native":
                meta["requested_source"] = requested_source
            if deps.active_session_id:
                meta["agent_session_id"] = deps.active_session_id
            if deps.active_conversation_id:
                meta["conversation_id"] = deps.active_conversation_id
            if deps.active_actor_id:
                meta["actor_id"] = deps.active_actor_id
            if call.turn_id:
                meta["turn_id"] = call.turn_id
            if call.id:
                meta["tool_call_id"] = call.id
            args["meta"] = meta
            guarded = ToolCall(
                name=guarded.name,
                arguments=args,
                id=guarded.id,
                turn_id=guarded.turn_id,
                iteration=guarded.iteration,
                caller=guarded.caller,
                started_at=guarded.started_at,
                parent_call_id=guarded.parent_call_id,
                metadata=dict(guarded.metadata or {}),
            )
        return trade_intent_submit_handler(
            guarded,
            config=deps.config,
            default_strategy=deps.active_strategy_id or "manual_agent",
            default_source=(
                "strategy_agent"
                if deps.strategy_order_auto_approve and deps.active_strategy_id
                else "agent:native"
            ),
        )

    return handler


# LLM domain — every wrapper closes over ``config`` so calls go through
# the workspace's tier policy + budget gates.


def _wrap_llm_complete(deps: NativeToolDeps):
    def handler(call: ToolCall):
        return llm_complete_handler(call, config=deps.config)

    return handler


def _wrap_llm_classify(deps: NativeToolDeps):
    def handler(call: ToolCall):
        return llm_classify_handler(call, config=deps.config)

    return handler


def _wrap_llm_extract_json(deps: NativeToolDeps):
    def handler(call: ToolCall):
        return llm_extract_json_handler(call, config=deps.config)

    return handler


def _wrap_llm_compress(deps: NativeToolDeps):
    def handler(call: ToolCall):
        return llm_compress_handler(call, config=deps.config)

    return handler


# ----- strategy runtime ----------------------------------------------------
# of the strategy-runtime refactor exposes the full lifecycle
# of agent-generated strategy packages (generate / validate / promote /
# run / kill switch / history) as native tools so the agent can author
# and operate them without leaving the loop.


def _wrap_strategy_draft_proposal(deps: NativeToolDeps):
    def handler(call: ToolCall):
        return strategy_draft_proposal_handler(call, config=deps.config)

    return handler


def _wrap_strategy_submit_proposal(deps: NativeToolDeps):
    def handler(call: ToolCall):
        return strategy_submit_proposal_handler(call, config=deps.config)

    return handler


def _wrap_strategy_validate(deps: NativeToolDeps):
    def handler(call: ToolCall):
        return strategy_validate_handler(call, config=deps.config)

    return handler


def _wrap_strategy_delete_proposal(deps: NativeToolDeps):
    def handler(call: ToolCall):
        return strategy_delete_proposal_handler(call, config=deps.config)

    return handler


def _wrap_strategy_backtest(deps: NativeToolDeps):
    def handler(call: ToolCall):
        return strategy_backtest_handler(call, config=deps.config)

    return handler


def _wrap_strategy_promote(deps: NativeToolDeps):
    def handler(call: ToolCall):
        return strategy_promote_handler(call, config=deps.config)

    return handler


def _wrap_strategy_run_tick(deps: NativeToolDeps):
    def handler(call: ToolCall):
        guarded = _strategy_agent_run_tick_call(deps, call)
        if isinstance(guarded, ToolResult):
            return guarded
        return strategy_run_tick_handler(
            guarded,
            config=deps.config,
            skills=deps.skills,
            tool_registry=deps.tool_registry,
            executor=deps.executor,
        )

    return handler


_AUTO_APPROVED_STRATEGY_TICK_MODES = frozenset({"paper", "shadow"})
_STRATEGY_TICK_MODE_KEYS = ("mode_override", "mode", "execution_mode")


def _strategy_tick_mode(payload: dict[str, Any]) -> str:
    for key in _STRATEGY_TICK_MODE_KEYS:
        value = payload.get(key)
        if value:
            return str(value).strip().lower()
    return ""


def _strategy_tick_payload_id(payload: dict[str, Any]) -> str:
    return str(payload.get("strategy_id") or "").strip()


def _is_active_strategy_runtime_tick(payload: dict[str, Any], deps: NativeToolDeps) -> bool:
    if not (deps.strategy_order_auto_approve and deps.active_strategy_id):
        return False
    requested = _strategy_tick_payload_id(payload)
    return not requested or requested == deps.active_strategy_id


def _configured_strategy_tick_mode(payload: dict[str, Any], deps: NativeToolDeps) -> str:
    strategy_id = _strategy_tick_payload_id(payload)
    if not strategy_id:
        return ""
    paths = (deps.config.paths if deps.config is not None else None) or deps.paths
    if paths is None:
        return ""
    try:
        from ...strategies.package import load_package

        return str(load_package(paths, strategy_id).manifest.mode or "").strip().lower()
    except Exception:
        return ""


def is_strategy_run_tick_auto_approved(
    payload: dict[str, Any],
    deps: NativeToolDeps,
) -> bool:
    """Allow unattended strategy ticks at the tool layer.

    ``strategy_run_tick`` is the runtime entrypoint, not the final
    trade approval boundary. Live execution is still constrained by
    ``StrategyRunner`` and by ``trade_intent_submit`` / RiskGate.
    """

    mode = _strategy_tick_mode(payload)
    if mode in _AUTO_APPROVED_STRATEGY_TICK_MODES:
        return True
    active_strategy_tick = _is_active_strategy_runtime_tick(payload, deps)
    if mode == "live":
        return active_strategy_tick
    if active_strategy_tick:
        return True
    configured_mode = _configured_strategy_tick_mode(payload, deps)
    return configured_mode in _AUTO_APPROVED_STRATEGY_TICK_MODES


def _strategy_agent_run_tick_call(
    deps: NativeToolDeps,
    call: ToolCall,
) -> ToolCall | ToolResult:
    if not (deps.strategy_order_auto_approve and deps.active_strategy_id):
        return call

    args = dict(call.arguments or {})
    requested_strategy = _strategy_tick_payload_id(args)
    if requested_strategy and requested_strategy != deps.active_strategy_id:
        return _tool_permission_denied(
            call,
            "strategy Agent session cannot run a tick for a different strategy",
            detail={
                "active_strategy_id": deps.active_strategy_id,
                "requested_strategy_id": requested_strategy,
            },
        )

    args["strategy_id"] = deps.active_strategy_id
    if deps.active_trigger_event_id and not args.get("trigger_event_id"):
        args["trigger_event_id"] = deps.active_trigger_event_id
    if deps.active_trigger_source and not args.get("operator"):
        args["operator"] = deps.active_trigger_source

    return ToolCall(
        name=call.name,
        arguments=args,
        id=call.id,
        turn_id=call.turn_id,
        iteration=call.iteration,
        caller=call.caller,
        started_at=call.started_at,
        parent_call_id=call.parent_call_id,
        metadata=dict(call.metadata or {}),
    )


def _wrap_strategy_kill_switch(deps: NativeToolDeps):
    def handler(call: ToolCall):
        return strategy_kill_switch_handler(call, config=deps.config)

    return handler


def _wrap_strategy_run_history(deps: NativeToolDeps):
    def handler(call: ToolCall):
        return strategy_run_history_handler(call, config=deps.config)

    return handler


def _wrap_strategy_tuning_generate(deps: NativeToolDeps):
    def handler(call: ToolCall):
        return strategy_tuning_generate_handler(call, config=deps.config)

    return handler


def _wrap_strategy_tuning_run(deps: NativeToolDeps):
    def handler(call: ToolCall):
        return strategy_tuning_run_handler(
            call, config=deps.config, skills=deps.skills
        )

    return handler


def _wrap_strategy_tuning_status(deps: NativeToolDeps):
    def handler(call: ToolCall):
        return strategy_tuning_status_handler(call, config=deps.config)

    return handler


def _wrap_strategy_tuning_snapshot(deps: NativeToolDeps):
    def handler(call: ToolCall):
        return strategy_tuning_snapshot_handler(call, config=deps.config)

    return handler


def _wrap_gateway_diagnose(deps: NativeToolDeps):
    def handler(call: ToolCall):
        return gateway_diagnose_handler(call, paths=deps.paths)

    return handler


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def build_native_tool_deps(
    *,
    workspace_root: Path,
    skill_roots: Iterable[Path],
    file_state: Optional[FileStateCache] = None,
    task_state: Optional[TaskState] = None,
    shell_default_timeout_sec: float = 60.0,
    shell_max_output_bytes: int = 64_000,
    paths: Optional[WorkspacePaths] = None,
    config: Optional[Config] = None,
    skills: Optional[SkillKernel] = None,
) -> NativeToolDeps:
    """Construct the dependency bundle (without registering tools)."""

    fs = file_state if file_state is not None else FileStateCache()
    ts = task_state if task_state is not None else TaskState()
    skill_roots_list = [Path(r) for r in skill_roots]
    skill_files = None
    if skills is not None:
        skill_files = [
            Path(entry.manifest.path) / "SKILL.md"
            for entry in skills.registry.list()
            if entry.manifest.path is not None
        ]
    si = SkillIndex(skill_roots_list, skill_files=skill_files)
    # Default to a paths layout rooted at the workspace so memory tools
    # have somewhere to land even when the caller hasn't passed an
    # explicit ``paths`` (e.g. CLI ad-hoc invocation).
    resolved_paths = paths if paths is not None else WorkspacePaths(root=Path(workspace_root))
    # Async-task store is workspace-scoped — every workspace gets its
    # own ``agent_tasks/`` directory so a CLI ``cd`` between workspaces
    # doesn't accidentally surface the previous workspace's tasks.
    from ...subagents.tasks import TaskStore as _TaskStore
    from ..resources import ResourceIndex as _ResourceIndex

    task_store = _TaskStore(resolved_paths)
    resource_index = _ResourceIndex()
    return NativeToolDeps(
        workspace_root=Path(workspace_root),
        file_state=fs,
        task_state=ts,
        skill_index=si,
        skill_roots=skill_roots_list,
        shell_default_timeout_sec=shell_default_timeout_sec,
        shell_max_output_bytes=shell_max_output_bytes,
        paths=resolved_paths,
        config=config,
        skills=skills,
        task_store=task_store,
        resource_index=resource_index,
    )


def register_native_tools(
    registry: ToolRegistry,
    deps: NativeToolDeps,
    *,
    replace: bool = False,
) -> NativeToolDeps:
    """Register every native tool on ``registry`` and return ``deps``.

    Idempotent when ``replace=True``. Returns ``deps`` so callers can
    chain ``register_native_tools(reg, build_native_tool_deps(...))``.
    """

    deps.tool_registry = registry
    descriptors = [
        # ----- file ops -----
        make_native_descriptor(
            name="read_file",
            description=(
                "Read a workspace text file. Returns the content with optional "
                "line offset/limit. Updates FileStateCache for fresh-read checks."
            ),
            input_schema=_READ_FILE_SCHEMA,
            handler=_wrap_read_file(deps),
            risk=RiskLevel.READ,
            permission_scope=PermissionScope.WORKSPACE,
            read_only=True,
            is_concurrency_safe=True,
            tags=("file", "read"),
            result_kind="file",
            auto_approve=True,
        ),
        make_native_descriptor(
            name="list_dir",
            description="List entries under a workspace directory.",
            input_schema=_LIST_DIR_SCHEMA,
            handler=_wrap_list_dir(deps),
            risk=RiskLevel.READ,
            permission_scope=PermissionScope.WORKSPACE,
            tags=("file", "read"),
            result_kind="json",
            auto_approve=True,
        ),
        make_native_descriptor(
            name="glob",
            description="Match files in the workspace by a glob pattern.",
            input_schema=_GLOB_SCHEMA,
            handler=_wrap_glob(deps),
            risk=RiskLevel.READ,
            permission_scope=PermissionScope.WORKSPACE,
            tags=("file", "search"),
            auto_approve=True,
        ),
        make_native_descriptor(
            name="grep",
            description="Regex search across the workspace via ripgrep when available.",
            input_schema=_GREP_SCHEMA,
            handler=_wrap_grep(deps),
            risk=RiskLevel.READ,
            permission_scope=PermissionScope.WORKSPACE,
            tags=("file", "search"),
            auto_approve=True,
        ),
        make_native_descriptor(
            name="edit_file",
            description=(
                "Apply a single string replacement to a workspace file. Requires "
                "a fresh read; falls back to multi-occurrence with replace_all=true."
            ),
            input_schema=_EDIT_FILE_SCHEMA,
            handler=_wrap_edit_file(deps),
            risk=RiskLevel.WRITE,
            permission_scope=PermissionScope.WORKSPACE,
            read_only=False,
            is_concurrency_safe=False,
            requires_fresh_read=True,
            mutates_paths=True,
            tags=("file", "edit"),
            result_kind="diff",
            risk_classifier=classify_file_mutation_risk,
        ),
        make_native_descriptor(
            name="write_file",
            description=(
                "Create or overwrite a workspace file with the given contents. "
                "New free-form files are automatically placed under the active "
                "conversation directory. Creating elsewhere requires "
                "allow_outside_conversation=true plus "
                "outside_conversation_reason. Existing files keep their path. "
                "For strategy authoring, first call strategy_draft_proposal to "
                "scaffold a draft, then use write_file / edit_file on the "
                "returned proposal_paths (the after/strategies/<id>/ files "
                "under evolution/proposals/<id>/) to author the package; never "
                "write directly into the live workspace/strategies/<id>/ tree "
                "(that is proposal-only) and do not stage packages under "
                "~/.nerya or temporary directories."
            ),
            input_schema=_WRITE_FILE_SCHEMA,
            handler=_wrap_write_file(deps),
            risk=RiskLevel.WRITE,
            permission_scope=PermissionScope.WORKSPACE,
            read_only=False,
            is_concurrency_safe=False,
            mutates_paths=True,
            tags=("file", "edit"),
            result_kind="diff",
            risk_classifier=classify_file_mutation_risk,
        ),
        # ----- shell -----
        make_native_descriptor(
            name="run_shell",
            description=(
                "Run a shell command (tests, builds, git, one-off scripts). "
                "Risk is classified per-call: rm -rf, sudo, git push --force, "
                "etc. are flagged as DANGEROUS. "
                "During an Agent conversation, common file-mutating commands "
                "default to its artifacts/conversations/<session>/ directory. "
                "Standard tests and builds retain the project cwd. "
                "Writing elsewhere requires allow_outside_conversation=true "
                "plus outside_conversation_reason. "
                "Do not use this for strategy authoring, connector/data-source "
                "discovery, or wallet/on-chain provider inspection. "
                "Prefer the native tool first — the sandbox BLOCKS run_shell "
                "for work native tools already cover, so reaching for shell "
                "wastes a turn on permission_denied. Use instead: "
                "list/find files or directories -> list_dir or glob (not ls / "
                "find); read a file -> read_file (not cat / head / tail); "
                "search file contents -> grep; discover roles or subagents -> "
                "role_list or subagent_list (not find subagents); run a "
                "backtest -> strategy_backtest (not python -m ...backtest_run); "
                "strategy / connector / wallet / on-chain / market data -> "
                "skill_view (strategy_author), connector_list / connector_view, "
                "data_api, market_data; author strategy code -> "
                "strategy_draft_proposal followed by edit_file / write_file "
                "on the staged proposal files to author SDK code. "
                "Reserve run_shell for explicit operator commands; reserve shell "
                "for explicit operator commands, running "
                "tests / builds (pytest, ruff, npm test), or cases where no "
                "native tool exists. If the sandbox refuses a command "
                "(permission_denied / workspace_sandbox_escape), report the "
                "refusal plainly by quoting the permission_denied reason "
                "(权限不足/拒绝) before any workaround suggestions — never "
                "soften a security refusal into a generic apology, and never "
                "offer an escape hatch that does not exist: there is no "
                "operator mode, flag, or approval that lets run_shell read "
                "host/system files outside the workspace."
            ),
            input_schema=_RUN_SHELL_SCHEMA,
            handler=_wrap_run_shell(deps),
            risk=RiskLevel.EXEC,
            permission_scope=PermissionScope.WORKSPACE,
            read_only=False,
            is_concurrency_safe=False,
            mutates_paths=True,
            tags=("shell", "exec"),
            result_kind="shell",
            risk_classifier=classify_shell_risk,
        ),
        # ----- task / plan -----
        make_native_descriptor(
            name="todo_write",
            description=(
                "Set the entire todo list for the current session. Use to track "
                "multi-step work; only one item may be in_progress at a time."
            ),
            input_schema=_TODO_WRITE_SCHEMA,
            handler=_wrap_todo_write(deps),
            risk=RiskLevel.READ,
            permission_scope=PermissionScope.NONE,
            read_only=True,
            is_concurrency_safe=False,
            tags=("planning",),
            result_kind="json",
            auto_approve=True,
        ),
        make_native_descriptor(
            name="enter_plan_mode",
            description=(
                "Enter plan mode. Mutating tools are blocked until exit_plan_mode "
                "submits a plan and the user approves it."
            ),
            input_schema=_ENTER_PLAN_MODE_SCHEMA,
            handler=_wrap_enter_plan(deps),
            risk=RiskLevel.READ,
            permission_scope=PermissionScope.NONE,
            tags=("planning",),
            result_kind="json",
            auto_approve=True,
        ),
        make_native_descriptor(
            name="exit_plan_mode",
            description=(
                "Submit a markdown plan body for user approval and exit plan mode "
                "on accept."
            ),
            input_schema=_EXIT_PLAN_MODE_SCHEMA,
            handler=_wrap_exit_plan(deps),
            risk=RiskLevel.READ,
            permission_scope=PermissionScope.NONE,
            tags=("planning",),
            result_kind="json",
            auto_approve=True,
        ),
        make_native_descriptor(
            name="plan_status",
            description=(
                "Poll the resolution of a plan submitted via exit_plan_mode. "
                "Returns approved / rejected / pending_approval / stale / "
                "no_pending_plan, plus a hint for what the model should do next."
            ),
            input_schema=_PLAN_STATUS_SCHEMA,
            handler=_wrap_plan_status(deps),
            risk=RiskLevel.READ,
            permission_scope=PermissionScope.NONE,
            tags=("planning",),
            result_kind="json",
            auto_approve=True,
        ),
        # ----- skills -----
        make_native_descriptor(
            name="skill_index",
            description=(
                "List installed SKILL.md playbooks (id and description). "
                "Read a playbook with skill_view or Skill."
            ),
            input_schema=_SKILL_INDEX_SCHEMA,
            handler=_wrap_skill_index(deps),
            risk=RiskLevel.READ,
            permission_scope=PermissionScope.NONE,
            tags=("skill", "discovery"),
            auto_approve=True,
        ),
        make_native_descriptor(
            name="skill_view",
            description="Fetch the full body of a SKILL.md playbook by id.",
            input_schema=_SKILL_VIEW_SCHEMA,
            handler=_wrap_skill_view(deps),
            risk=RiskLevel.READ,
            permission_scope=PermissionScope.NONE,
            tags=("skill", "discovery"),
            auto_approve=True,
            result_kind="text",
        ),
        make_native_descriptor(
            name="script_inspect",
            description="Read the head of a script under a skill's scripts/ directory.",
            input_schema=_SCRIPT_INSPECT_SCHEMA,
            handler=_wrap_script_inspect(deps),
            risk=RiskLevel.READ,
            permission_scope=PermissionScope.WORKSPACE,
            tags=("skill", "script"),
            auto_approve=True,
        ),
        make_native_descriptor(
            name="script_run",
            description="Run a script under a skill's scripts/ directory.",
            input_schema=_SCRIPT_RUN_SCHEMA,
            handler=_wrap_script_run(deps),
            risk=RiskLevel.EXEC,
            permission_scope=PermissionScope.WORKSPACE,
            read_only=False,
            is_concurrency_safe=False,
            tags=("skill", "script", "exec"),
            result_kind="shell",
            auto_approve_when=lambda payload: _auto_approve_skill_script_run(deps, payload),
        ),
        # ----- web research -----
        make_native_descriptor(
            name="web_search",
            description=(
                "Search the public web and return ranked result URLs/snippets. "
                "Walks a configurable engine chain (Exa → Tavily → Perplexity → "
                "LangSearch → Brave → Serper → Firecrawl → SearXNG → Bing → "
                "DuckDuckGo) with per-engine multi-key rotation; falls through "
                "automatically when an engine errors or runs out of keys. "
                "Override with ``engines`` (ordered list) or ``engine`` (single). "
                "Pair with web_fetch for a chosen page or web_search_fetch for "
                "top-N pages in one bounded pass."
            ),
            input_schema=WEB_SEARCH_SCHEMA,
            handler=_wrap_web_search(deps),
            risk=RiskLevel.READ,
            permission_scope=PermissionScope.NETWORK,
            read_only=True,
            is_concurrency_safe=True,
            tags=("web", "research", "search", "external_content"),
            result_kind="json",
            auto_approve=True,
        ),
        make_native_descriptor(
            name="web_fetch",
            description=(
                "Fetch one HTTP(S) URL as readable markdown/text. Applies "
                "Nerya web-safety checks plus a progressive fallback chain: "
                "direct fetch + local HTML extraction → Jina Reader → "
                "configured headless browser engine (Lightpanda / "
                "CloakBrowser / Obscura) → Scrapling stealth fetcher. Each "
                "tier can be disabled with use_jina_fallback / "
                "use_browser_fallback / use_scrapling_fallback."
            ),
            input_schema=WEB_FETCH_SCHEMA,
            handler=_wrap_web_fetch(deps),
            risk=RiskLevel.READ,
            permission_scope=PermissionScope.NETWORK,
            read_only=True,
            is_concurrency_safe=True,
            tags=("web", "research", "fetch", "external_content"),
            result_kind="json",
            auto_approve=True,
        ),
        make_native_descriptor(
            name="web_search_fetch",
            description=(
                "Search the public web and fetch the top N result pages as "
                "markdown documents. Inherits the multi-engine search chain "
                "(set via ``engines``) and the progressive fetch fallback "
                "chain (Jina → browser → Scrapling). Use for research briefs "
                "that need source content, not just snippets."
            ),
            input_schema=WEB_SEARCH_FETCH_SCHEMA,
            handler=_wrap_web_search_fetch(deps),
            risk=RiskLevel.READ,
            permission_scope=PermissionScope.NETWORK,
            read_only=True,
            is_concurrency_safe=False,
            tags=("web", "research", "search", "fetch", "external_content"),
            result_kind="json",
            auto_approve=True,
        ),
        # ----- connector / venue discovery -----
        # Surfaces the in-process ``ExchangeProviderRegistry`` so the
        # agent can authoritatively answer "is X integrated?" before
        # claiming a venue is missing. Closes the gap that caused the
        # model to ship a placeholder ``polymarket_edge`` strategy
        # while ``nerya/connectors/polymarket.py`` was already wired up.
        make_native_descriptor(
            name="connector_list",
            description=(
                "List every venue / data-source provider already "
                "integrated into Nerya (CEX, DEX, chain, prediction "
                "market). Pass query='polymarket' (or any substring) "
                "to check whether a specific exchange is available "
                "before authoring a new connector. Returns id, label, "
                "kind, aliases, runtime, install_hint, supports matrix "
                "and doc links. When you report wallets, balances, or "
                "readiness to the operator, label every entry with the exact "
                "`provider=<id>` form (for example provider=evm, provider=okx, "
                "provider=solana) — keep the literal English word `provider` "
                "and the raw id; never translate either into prose."
            ),
            input_schema=CONNECTOR_LIST_SCHEMA,
            handler=connector_list_handler,
            risk=RiskLevel.READ,
            permission_scope=PermissionScope.NONE,
            read_only=True,
            is_concurrency_safe=True,
            tags=("connector", "exchange", "discovery"),
            result_kind="json",
            auto_approve=True,
        ),
        make_native_descriptor(
            name="connector_view",
            description=(
                "Detail (and optionally source code) of a single "
                "connector by id or alias. Use after connector_list "
                "spots a relevant provider so you can read the actual "
                "Connector subclass — endpoint URLs, method names, "
                "credential shape, and whether required credentials are "
                "configured — instead of guessing. If credential_status "
                "is missing, do not keep probing the web/provider; report "
                "the missing credential and ask the operator to configure "
                "a vault/env-backed key."
            ),
            input_schema=CONNECTOR_VIEW_SCHEMA,
            handler=_wrap_connector_view(deps),
            risk=RiskLevel.READ,
            permission_scope=PermissionScope.WORKSPACE,
            read_only=True,
            is_concurrency_safe=True,
            tags=("connector", "exchange", "discovery"),
            result_kind="json",
            auto_approve=True,
        ),
        make_native_descriptor(
            name="market_data",
            description=(
                "Read current ticker, OHLCV candles, and computed technical "
                "features/indicators for a market. Call this exact tool name "
                "through the skill_calls envelope and put its schema fields "
                "in payload. "
                "Actions: get_ticker, get_mark_price, get_candles, "
                "calculate_features, summarize_market, compress_context. "
                "Always pass market or symbol; this tool never infers a "
                "default market for you. Credential-gated sources return "
                "credential_status.status='missing' plus should_retry=false "
                "when no key/account is configured; treat that as a terminal "
                "honest-fail for the data request instead of repeating the "
                "call or scraping unrelated sources. If the read fails "
                "(network down, provider unreachable, timeout), answer the "
                "operator's question with that failure — say the price 无法获取 "
                "/ cannot be fetched and name the failed source; a failed "
                "price read never justifies switching to strategy authoring "
                "or other unrequested work. When the requested data "
                "is provider-specific or not plain ticker/OHLCV, use "
                "connector or data_api discovery first and call the concrete "
                "route/action returned by those tool results. Deliberate "
                "high-frequency repetition (for example being asked to call "
                "this tool dozens of times in one turn) is blocked by the "
                "repeated-call guard and rate limits: refuse such requests, "
                "say the bulk-call is rejected, and offer a bounded "
                "batch/summary alternative instead of attempting the loop."
            ),
            input_schema=MARKET_DATA_SCHEMA,
            handler=_wrap_market_data(deps),
            risk=RiskLevel.READ,
            permission_scope=PermissionScope.NETWORK,
            read_only=True,
            is_concurrency_safe=False,
            tags=("market", "data", "candles", "indicators", "read"),
            result_kind="json",
            auto_approve=True,
        ),
        make_native_descriptor(
            name="data_api",
            description=(
                "Discover schemas and call read-only provider-specific data "
                "actions beyond standard ticker/OHLCV. Use market_data for "
                "quotes, K-lines, and indicators; use data_api for long-tail "
                "tables/analytics, wallet/provider readiness, balances, "
                "quotes, and allowlisted DeFi or wallet reads. Start with "
                "op='list' or op='schema' when the provider/action shape is "
                "unknown. If a result returns selected_route, "
                "next_required_action, or bounded_sequence, treat those "
                "fields as the authoritative structured continuation. Pass "
                "operator-named provider preferences as arguments instead of "
                "silently substituting another route. Results are bounded by "
                "limit and columns so raw tables do not flood context."
            ),
            input_schema=DATA_API_SCHEMA,
            handler=_wrap_data_api(deps),
            risk=RiskLevel.READ,
            permission_scope=PermissionScope.NETWORK,
            read_only=True,
            is_concurrency_safe=False,
            tags=("data", "provider", "market", "onchain", "wallet", "read"),
            result_kind="json",
            max_result_tokens=6000,
            auto_approve=True,
        ),
        make_native_descriptor(
            name="gateway_diagnose",
            description=(
                "Read-only diagnostics for messaging gateway connectivity. "
                "Use when the operator asks why a Telegram, Slack, Discord, "
                "webhook, or other gateway channel cannot connect, send, or "
                "reply. For Telegram this checks live messages/channels.yml "
                "configuration, vault-backed token/chat refs, Telegram getMe/"
                "getChat probes when configured, and returns concrete hints. "
                "Does not send messages or mutate config."
            ),
            input_schema=GATEWAY_DIAGNOSE_SCHEMA,
            handler=_wrap_gateway_diagnose(deps),
            risk=RiskLevel.READ,
            permission_scope=PermissionScope.NONE,
            read_only=True,
            is_concurrency_safe=True,
            tags=("gateway", "messaging", "diagnose", "read"),
            result_kind="json",
            auto_approve=True,
        ),
        make_native_descriptor(
            name="data_source_status",
            description=(
                "Read Nerya's unified data-source sync ledger. Use this for "
                "operator requests asking for all data-source sync status, "
                "freshness, stale sources, or recent sync events instead of "
                "probing unrelated market/account tools."
            ),
            input_schema=DATA_SOURCE_STATUS_SCHEMA,
            handler=_wrap_data_source_status(deps),
            risk=RiskLevel.READ,
            permission_scope=PermissionScope.WORKSPACE,
            read_only=True,
            is_concurrency_safe=True,
            tags=("data", "sync", "status", "read"),
            result_kind="json",
            auto_approve=True,
        ),
        make_native_descriptor(
            name="data_source_sync_now",
            description=(
                "Force one registered data-source contributor to sync now "
                "and return the updated row plus recent events. Use this when "
                "the operator names a source_id such as account:paper_main or "
                "market:public_ccxt and asks to sync/refresh it immediately. "
                "This updates only the sync ledger/freshness state; it does "
                "not place trades or mutate live trading gates."
            ),
            input_schema=DATA_SOURCE_SYNC_NOW_SCHEMA,
            handler=_wrap_data_source_sync_now(deps),
            risk=RiskLevel.WRITE,
            permission_scope=PermissionScope.WORKSPACE,
            read_only=False,
            is_concurrency_safe=False,
            tags=("data", "sync", "status", "write"),
            result_kind="json",
            auto_approve=True,
        ),
        make_native_descriptor(
            name="account_list",
            description=(
                "List configured trading accounts from the workspace account "
                "control plane. Use this before claiming an account is missing "
                "or after account_upsert to verify the created paper account."
            ),
            input_schema=ACCOUNT_LIST_SCHEMA,
            handler=_wrap_account_list(deps),
            risk=RiskLevel.READ,
            permission_scope=PermissionScope.WORKSPACE,
            read_only=True,
            is_concurrency_safe=True,
            tags=("account", "trading", "read"),
            result_kind="json",
            auto_approve=True,
        ),
        make_native_descriptor(
            name="account_upsert",
            description=(
                "Create or update a non-live paper account through Nerya's "
                "official account registry. Use when the operator asks to add "
                "a paper/shadowless exchange, broker, DEX, or chain account. "
                "For venues handled by ccxt, pass the concrete provider id "
                "when it exists (for example venue='kraken') or venue='ccxt:kraken'. "
                "Paper accounts do not need credentials: when the operator "
                "asks for a paper account, call this immediately with venue + "
                "mode='paper' instead of asking for API keys first; vault "
                "credential refs are optional and only needed for "
                "credentialed reads. "
                "Account creation is preparation, not the goal: if the "
                "operator actually asked for a strategy, backtest, or trade, "
                "continue to strategy_draft_proposal / the trading tools "
                "in the same turn after the account exists — do not end the "
                "turn with only accounts created. "
                "Plaintext credentials are refused; live/canary/shadow accounts "
                "must go through the dashboard intake/proposal flow."
            ),
            input_schema=ACCOUNT_UPSERT_SCHEMA,
            handler=_wrap_account_upsert(deps),
            risk=RiskLevel.WRITE,
            permission_scope=PermissionScope.WORKSPACE,
            read_only=False,
            is_concurrency_safe=False,
            mutates_paths=True,
            tags=("account", "trading", "write"),
            result_kind="json",
            auto_approve_when=lambda payload: (
                str(payload.get("mode") or "paper").strip().lower() == "paper"
                and not bool(payload.get("live_trading_enabled", False))
            ),
        ),
        make_native_descriptor(
            name="wallet_install",
            description=(
                "Install a catalog-listed wallet provider dependency through "
                "Nerya's dependency installer. Use only after "
                "provider capability discovery reports a missing dependency "
                "or returns an explicit install recommendation. After a "
                "successful install, refresh capability discovery and follow "
                "the concrete call shape returned by that result. "
                "Installs obey runtime.allow_auto_install, "
                "NERYA_ALLOW_AUTO_INSTALL, or explicit operator approval."
            ),
            input_schema=_WALLET_INSTALL_SCHEMA,
            handler=_wrap_wallet_install(deps),
            risk=RiskLevel.EXEC,
            permission_scope=PermissionScope.NETWORK,
            read_only=False,
            is_concurrency_safe=False,
            tags=("wallet", "install", "goat", "dependency"),
            result_kind="json",
            max_result_tokens=4000,
        ),
    ]
    if deps.paths is not None:
        descriptors.extend([
            make_native_descriptor(
                name="memory_recall",
                description=(
                    "Recall query-relevant long-term memory visible to this "
                    "session and its active strategy. Strategy and session "
                    "identifiers are enforced by the runtime."
                ),
                input_schema=MEMORY_RECALL_SCHEMA,
                handler=_wrap_memory_recall(deps),
                risk=RiskLevel.READ,
                permission_scope=PermissionScope.WORKSPACE,
                tags=("memory", "read"),
                result_kind="json",
                auto_approve=True,
            ),
            make_native_descriptor(
                name="memory_remember",
                description=(
                    "Append a timestamped note to the agent's long-term memory. "
                    "Use sparingly — durable lessons only, not turn-by-turn chatter."
                ),
                input_schema=MEMORY_REMEMBER_SCHEMA,
                handler=_wrap_memory_remember(deps),
                risk=RiskLevel.WRITE,
                permission_scope=PermissionScope.WORKSPACE,
                read_only=False,
                is_concurrency_safe=False,
                mutates_paths=True,
                tags=("memory", "write"),
                result_kind="json",
            ),
            make_native_descriptor(
                name="journal_search",
                description=(
                    "Tail and filter a workspace journal (jsonl). Use to recall "
                    "recent agent / risk / orders / triggers events without "
                    "loading the whole file."
                ),
                input_schema=JOURNAL_SEARCH_SCHEMA,
                handler=_wrap_journal_search(deps),
                risk=RiskLevel.READ,
                permission_scope=PermissionScope.WORKSPACE,
                tags=("memory", "journal", "read"),
                result_kind="json",
                auto_approve=True,
            ),
            # ----- recipes -----
            # Recipes are short operator-curated workflow records. The
            # native tools let the model fetch the full list / body on
            # demand without bloating the always-on prompt.
            make_native_descriptor(
                name="recipe_list",
                description=(
                    "List operator-curated recipes (named runbooks) "
                    "whose required skills are installed. Filter by tag "
                    "or category; pass available_only=false to see "
                    "every recipe, including ones gated on skills the "
                    "workspace doesn't have."
                ),
                input_schema=RECIPE_LIST_SCHEMA,
                handler=_wrap_recipe_list(deps),
                risk=RiskLevel.READ,
                permission_scope=PermissionScope.NONE,
                tags=("recipe", "discovery"),
                result_kind="json",
                auto_approve=True,
            ),
            make_native_descriptor(
                name="recipe_view",
                description=(
                    "Fetch a recipe's full body + verbatim prompt by id. "
                    "Use after recipe_list spots a relevant runbook — "
                    "follow the steps in body, then either replay the "
                    "prompt directly or quote it back to the operator "
                    "for confirmation before acting."
                ),
                input_schema=RECIPE_VIEW_SCHEMA,
                handler=_wrap_recipe_view(deps),
                risk=RiskLevel.READ,
                permission_scope=PermissionScope.NONE,
                tags=("recipe", "discovery"),
                result_kind="json",
                auto_approve=True,
            ),
        ])
        if deps.resource_index is not None:
            descriptors.extend([
                make_native_descriptor(
                    name="resource_list",
                    description=(
                        "Enumerate workspace resources (MCP-published "
                        "documents + local read-only assets). Optional "
                        "'source' filter ('mcp', 'workspace', 'local')."
                    ),
                    input_schema=RESOURCE_LIST_SCHEMA,
                    handler=_wrap_resource_list(deps),
                    risk=RiskLevel.READ,
                    permission_scope=PermissionScope.NETWORK,
                    tags=("resource", "discovery", "mcp"),
                    result_kind="json",
                    auto_approve=True,
                ),
                make_native_descriptor(
                    name="resource_read",
                    description=(
                        "Fetch one resource by URI. The fetcher decides "
                        "how to materialise the body (in-memory text, "
                        "lazy HTTP, MCP resources/read RPC, …)."
                    ),
                    input_schema=RESOURCE_READ_SCHEMA,
                    handler=_wrap_resource_read(deps),
                    risk=RiskLevel.READ,
                    permission_scope=PermissionScope.NETWORK,
                    tags=("resource", "mcp"),
                    result_kind="json",
                    auto_approve=True,
                ),
            ])
    if deps.config is not None:
        # ----- conversational workspace customization -----
        descriptors.extend([
            make_native_descriptor(
                name="workspace_ui_inspect",
                description=(
                    "Read the current declarative dashboard layout, revision, "
                    "page/widget inventory, and finite allow-listed widget catalog. "
                    "Use this before changing the home dashboard, adding a widget, "
                    "creating a menu page, or editing page navigation. This tool is "
                    "read-only and is the authoritative alternative to reading or "
                    "reconstructing ui/workspace.yml manually."
                ),
                input_schema=WORKSPACE_UI_INSPECT_SCHEMA,
                handler=_wrap_workspace_ui_inspect(deps),
                risk=RiskLevel.READ,
                permission_scope=PermissionScope.WORKSPACE,
                tags=("workspace", "dashboard", "ui", "customization", "read"),
                result_kind="json",
                auto_approve=True,
            ),
            make_native_descriptor(
                name="workspace_ui_propose",
                description=(
                    "Create a reviewable dashboard/page/menu proposal from small "
                    "structured operations. First call workspace_ui_inspect, then "
                    "use upsert_widget or upsert_page with stable ids whenever "
                    "possible. The tool validates the read-only widget catalog, "
                    "captures revision/digest guards, and writes only a pending "
                    "core_config_patch proposal; it never changes the live dashboard. "
                    "Do not edit React/TSX or rewrite the full YAML for normal UI "
                    "customization requests."
                ),
                input_schema=WORKSPACE_UI_PROPOSE_SCHEMA,
                handler=_wrap_workspace_ui_propose(deps),
                risk=RiskLevel.WRITE,
                permission_scope=PermissionScope.WORKSPACE,
                read_only=False,
                is_concurrency_safe=False,
                mutates_paths=True,
                tags=("workspace", "dashboard", "ui", "customization", "proposal"),
                result_kind="json",
                max_result_tokens=6_000,
            ),
        ])
        # ----- self-evolution -----
        descriptors.extend([
            make_native_descriptor(
                name="evolve_reflect",
                description=(
                    "Run a reflection tick over recent journals + risk + "
                    "subagent telemetry and write a 'learning_update' "
                    "proposal under evolution/proposals/. Use this for "
                    "requests to review performance, reflect on failures, "
                    "apply a lesson/experience, or find problems. Never "
                    "mutates live config — proposals require operator "
                    "approval."
                ),
                input_schema=EVOLVE_REFLECT_SCHEMA,
                handler=_wrap_evolve_reflect(deps),
                risk=RiskLevel.WRITE,
                permission_scope=PermissionScope.WORKSPACE,
                read_only=False,
                is_concurrency_safe=False,
                mutates_paths=True,
                tags=("evolve", "reflection"),
                result_kind="json",
            ),
            make_native_descriptor(
                name="evolve_proposals",
                description=(
                    "List recent self-improvement proposals with id, kind, "
                    "state, summary, target. When the user or tool output "
                    "already names a proposal id, pass proposal_id for an "
                    "exact read-only lookup instead of using shell, glob, "
                    "or workspace directory searches. This tool is read-only "
                    "and cannot create or apply a proposal; do not use it as "
                    "a substitute for evolve_reflect, evolve_skill_proposal, "
                    "workspace_ui_propose, evolve_core_config_patch, "
                    "strategy_draft_proposal, or "
                    "strategy_tuning_generate when the task asks for new "
                    "reflection, learning, skill, config, or strategy changes."
                ),
                input_schema=EVOLVE_PROPOSALS_SCHEMA,
                handler=_wrap_evolve_proposals(deps),
                risk=RiskLevel.READ,
                permission_scope=PermissionScope.WORKSPACE,
                tags=("evolve", "proposals", "read"),
                result_kind="json",
                auto_approve=True,
            ),
            make_native_descriptor(
                name="evolve_skill_proposal",
                description=(
                    "Capture a repeated or newly discovered workflow as a "
                    "reviewable SKILL.md proposal. Writes only under "
                    "evolution/proposals/<id>/after/skills/<skill_id>/; "
                    "does not activate or mutate live skills. If the operator "
                    "asks Nerya to learn, add, or create a reusable skill or "
                    "workflow, call this tool after the minimum necessary "
                    "research instead of ending with a prose promise. Do not "
                    "use this for ambiguous bug reports or targetful operations "
                    "like promote/apply/approve unless the operator explicitly "
                    "asks to turn that workflow into a reusable skill."
                ),
                input_schema=EVOLVE_SKILL_PROPOSAL_SCHEMA,
                handler=_wrap_evolve_skill_proposal(deps),
                risk=RiskLevel.WRITE,
                permission_scope=PermissionScope.WORKSPACE,
                read_only=False,
                is_concurrency_safe=False,
                mutates_paths=True,
                tags=("evolve", "skills", "workflow"),
                result_kind="json",
            ),
            make_native_descriptor(
                name="evolve_core_config_patch",
                description=(
                    "Propose a non-protected runtime config change to "
                    "nerya.yml / agents.yml / workspace.yml / "
                    "ui/workspace.yml / "
                    "news_feeds.yml / messages/channels.yml / policy files. "
                    "This never mutates the live config; it writes a "
                    "core_config_patch proposal under evolution/proposals/ "
                    "for operator review. Use this instead of edit_file, "
                    "write_file, or run_shell when changing default LLM "
                    "routing, reasoning_effort, max_parallel, custom RSS "
                    "feeds, outbound channel/webhook config, severity-based "
                    "notification routing, trade notification fan-out, or "
                    "other agent/runtime defaults. Dashboard, page, widget, and "
                    "menu requests must use workspace_ui_inspect followed by "
                    "workspace_ui_propose instead of this full-document tool. "
                    "For messages/channels.yml "
                    "severity routing, propose top-level severity_routes such "
                    "as {info: [telegram], critical: [telegram, discord], "
                    "silent: []}; do not invent targets like "
                    "notifications.routing. For Telegram channels, use "
                    "bot_token_ref plus numeric chat_id or vault-backed "
                    "chat_id_ref. Never include HTML, JavaScript, iframe, remote "
                    "URL, or executable component fields in any declarative config. "
                    "Do not use this for protected "
                    "risk exposure, signer, secret, approval, or live-trading "
                    "limits; those direct changes must be refused with "
                    "`advisory reject`. When you report such a reject to the "
                    "operator, name only the protected scope that was hit in "
                    "one line (for example nerya.yml:risk) — do not paste the "
                    "full protected-set list from the tool error."
                ),
                input_schema=EVOLVE_CORE_CONFIG_PATCH_SCHEMA,
                handler=_wrap_evolve_core_config_patch(deps),
                risk=RiskLevel.WRITE,
                permission_scope=PermissionScope.WORKSPACE,
                read_only=False,
                is_concurrency_safe=False,
                mutates_paths=True,
                tags=("evolve", "config", "proposal"),
                result_kind="json",
            ),
            make_native_descriptor(
                name="evolve_provider_proposal",
                description=(
                    "Create a reviewable provider_proposal for a missing "
                    "exchange, DEX, perpetual venue, wallet data source, or "
                    "external market-data provider. Use this after checking "
                    "connector_list/connector_view/data_api and confirming "
                    "the venue is not already integrated. Do not use this "
                    "just because a strategy/backtest request mentions a "
                    "venue; for trading strategy construction use "
                    "strategy_draft_proposal and report provider/data "
                    "readiness gaps from tool evidence if needed. Include venue, "
                    "docs_url, base_url, auth/signing model, and evidence. "
                    "This writes only under evolution/proposals/<id>/ and "
                    "never mutates live provider registry, credentials, or "
                    "accounts."
                ),
                input_schema=EVOLVE_PROVIDER_PROPOSAL_SCHEMA,
                handler=_wrap_evolve_provider_proposal(deps),
                risk=RiskLevel.WRITE,
                permission_scope=PermissionScope.WORKSPACE,
                read_only=False,
                is_concurrency_safe=False,
                mutates_paths=True,
                tags=("evolve", "provider", "proposal"),
                result_kind="json",
            ),
            make_native_descriptor(
                name="evolve_post_apply_observation",
                description=(
                    "Record evidence-backed post-apply observations for an "
                    "already applied evolution proposal. Use this after a "
                    "paper run, live/shadow observation, or backtest produces "
                    "new outcome evidence for an applied change. It appends "
                    "only to the evolution journal and never applies, rolls "
                    "back, or mutates strategy files."
                ),
                input_schema=EVOLVE_POST_APPLY_OBSERVATION_SCHEMA,
                handler=_wrap_evolve_post_apply_observation(deps),
                risk=RiskLevel.WRITE,
                permission_scope=PermissionScope.WORKSPACE,
                read_only=False,
                is_concurrency_safe=False,
                mutates_paths=True,
                tags=("evolve", "observation", "post_apply"),
                result_kind="json",
            ),
        ])
        if deps.skills is not None:
            # ----- subagents -----
            descriptors.extend([
                make_native_descriptor(
                    name="subagent_list",
                    description=(
                        "List registered subagents (workspace specs + "
                        "default lanes). Read-only."
                    ),
                    input_schema=SUBAGENT_LIST_SCHEMA,
                    handler=_wrap_subagent_list(deps),
                    risk=RiskLevel.READ,
                    permission_scope=PermissionScope.WORKSPACE,
                    tags=("subagent", "discovery"),
                    result_kind="json",
                    auto_approve=True,
                ),
                make_native_descriptor(
                    name="subagent_run",
                    description=(
                        "Spawn a child subagent with a JSON payload and "
                        "return its envelope. The child runs its own "
                        "observe → think → act loop with a bounded "
                        "skill allowlist; live-trading skills are "
                        "denied at the dispatcher boundary. ``name`` need "
                        "not be a registered role: if none fits, invent a "
                        "name and pass an inline ``prompt`` (and optional "
                        "``allowed_skills`` / ``tier``) to run a temporary "
                        "ad-hoc role — no role_save first, nothing persisted; "
                        "an unknown name with no prompt still runs as a "
                        "capable generic researcher. Do not use "
                        "this for user-requested Agent Team / committee "
                        "work; use team_run so the dashboard can show one "
                        "coordinated team with member lanes."
                    ),
                    input_schema=SUBAGENT_RUN_SCHEMA,
                    handler=_wrap_subagent_run(deps),
                    risk=RiskLevel.EXEC,
                    permission_scope=PermissionScope.WORKSPACE,
                    read_only=False,
                    is_concurrency_safe=False,
                    tags=("subagent", "exec"),
                    result_kind="json",
                    auto_approve=True,
                ),
                make_native_descriptor(
                    name="team_run",
                    description=(
                        "Spawn an Agent Team in parallel: multiple roles "
                        "share one mission and run concurrently under "
                        "a bounded budget. Returns aggregated findings "
                        "(per-role output + cross-role aggregate). Use this "
                        "when the operator explicitly asks for multiple "
                        "parallel roles or when the request is already split "
                        "into independent analysis lanes. For an explicit "
                        "Agent Team request, call this tool directly with the "
                        "task and clear role objects; use role_list or role_get "
                        "only when a requested role is ambiguous. Do not "
                        "prefetch market or research data before this call; "
                        "put those requirements in the team task or role "
                        "prompts so members gather the evidence. Build roles "
                        "from the task itself; do not infer a hidden template "
                        "from prompt keywords. "
                        "You are not limited to registered roles: when none "
                        "fits, give a role entry a fresh ``name`` plus an "
                        "inline ``prompt`` (and optional ``allowed_skills`` / "
                        "``tier``) to spin up a temporary ad-hoc role for "
                        "this run — no role_save needed, nothing persisted; "
                        "a bare unknown name still runs as a capable generic "
                        "researcher. Prefer building the team the task needs "
                        "over forcing it onto ill-fitting registered roles. "
                        "The roles argument must be an actual JSON array of "
                        "role objects, not a quoted JSON string. This tool is "
                        "synchronous: after it returns, answer from its "
                        "results/failures instead of giving only a launch "
                        "acknowledgement or running another team_run for the "
                        "same task. If an input feed/API/credential is "
                        "missing, make that blocker explicit and do not "
                        "substitute mock, placeholder, synthetic, or proxy "
                        "source content."
                    ),
                    input_schema=TEAM_RUN_SCHEMA,
                    handler=_wrap_team_run(deps),
                    risk=RiskLevel.EXEC,
                    permission_scope=PermissionScope.WORKSPACE,
                    read_only=False,
                    is_concurrency_safe=False,
                    tags=("subagent", "team", "exec"),
                    result_kind="json",
                    auto_approve=True,
                ),
                make_native_descriptor(
                    name="research_run",
                    description=(
                        "Delegate web-data collection to the dedicated "
                        "``web_researcher`` lane (light-tier by default). It "
                        "drives the built-in search-engine chain + headless "
                        "browser fallback, persists complete raw captures "
                        "under state/research_data/ and returns capture "
                        "paths + key facts for downstream analysis. Unlike "
                        "subagent_run this tool is also callable from inside "
                        "team members, so an expert lane can pull fresh web "
                        "data mid-run; the researcher itself cannot nest "
                        "further. Provide ``query`` and/or explicit "
                        "``urls``. For a normal one-turn brief, consume one "
                        "successful result instead of repeating equivalent "
                        "raw web searches."
                    ),
                    input_schema=RESEARCH_RUN_SCHEMA,
                    handler=_wrap_research_run(deps),
                    risk=RiskLevel.EXEC,
                    permission_scope=PermissionScope.WORKSPACE,
                    read_only=False,
                    is_concurrency_safe=True,
                    tags=("subagent", "research", "web", "exec"),
                    result_kind="json",
                    auto_approve=True,
                    child_max_depth=1,
                    delegates_to="web_researcher",
                ),
                make_native_descriptor(
                    name="role_list",
                    description=(
                        "List every Agent Team role (workspace + "
                        "defaults). Workspace roles override defaults "
                        "with the same name. This is a catalog, not a route "
                        "selector; inspect role_get when names overlap or "
                        "scope is unclear. For an explicit Agent Team request, "
                        "call team_run directly unless a requested role is "
                        "ambiguous."
                    ),
                    input_schema=ROLE_LIST_SCHEMA,
                    handler=_wrap_role_list(deps),
                    risk=RiskLevel.READ,
                    permission_scope=PermissionScope.WORKSPACE,
                    tags=("subagent", "team", "discovery"),
                    result_kind="json",
                    auto_approve=True,
                ),
                make_native_descriptor(
                    name="role_get",
                    description=(
                        "Fetch a role's full record (prompt + allowed_skills "
                        "+ tier + persistent flag). Use before role_save to "
                        "clone an existing role. Never fails on an unknown "
                        "name: if no role is registered it returns an "
                        "auto-generated generic ad-hoc researcher "
                        "(source=generated) you can dispatch as-is — so you "
                        "don't need role_get to succeed before team_run / "
                        "subagent_run, and shouldn't treat a missing role as a "
                        "blocker."
                    ),
                    input_schema=ROLE_GET_SCHEMA,
                    handler=_wrap_role_get(deps),
                    risk=RiskLevel.READ,
                    permission_scope=PermissionScope.WORKSPACE,
                    tags=("subagent", "team", "discovery"),
                    result_kind="json",
                    auto_approve=True,
                ),
                make_native_descriptor(
                    name="role_save",
                    description=(
                        "Upsert a *persistent* Agent Team role. Writes "
                        "<workspace>/subagents/<name>.agent.md (markdown "
                        "prompt) and <name>.role.yaml (allowed_skills + "
                        "tier). Existing files are overwritten. The "
                        "dispatcher denylist still blocks live-trading "
                        "skills regardless of allowed_skills. Use this ONLY "
                        "to persist a reusable role across turns — for a "
                        "one-off temporary role, skip role_save and pass an "
                        "inline ``prompt`` directly to team_run / "
                        "subagent_run instead."
                    ),
                    input_schema=ROLE_SAVE_SCHEMA,
                    handler=_wrap_role_save(deps),
                    risk=RiskLevel.WRITE,
                    permission_scope=PermissionScope.WORKSPACE,
                    read_only=False,
                    is_concurrency_safe=True,
                    tags=("subagent", "team", "configuration"),
                    result_kind="json",
                ),
                make_native_descriptor(
                    name="role_delete",
                    description=(
                        "Delete a persistent Agent Team role from the "
                        "workspace. Default roles cannot be deleted; "
                        "calling this on a default name is a no-op."
                    ),
                    input_schema=ROLE_DELETE_SCHEMA,
                    handler=_wrap_role_delete(deps),
                    risk=RiskLevel.WRITE,
                    permission_scope=PermissionScope.WORKSPACE,
                    read_only=False,
                    is_concurrency_safe=True,
                    tags=("subagent", "team", "configuration"),
                    result_kind="json",
                ),
                make_native_descriptor(
                    name="subagent_run_async",
                    description=(
                        "Spawn a one-off subagent in the background when the "
                        "operator asks for a background/后台 task or async "
                        "work. Return the task_id instead of doing the work "
                        "synchronously in the parent turn; use task_get / "
                        "task_output to fetch results when the task "
                        "reports state='succeeded'. Live-trading "
                        "skills remain denied. Cancellation is "
                        "cooperative (task_stop)."
                    ),
                    input_schema=SUBAGENT_RUN_ASYNC_SCHEMA,
                    handler=_wrap_subagent_run_async(deps),
                    risk=RiskLevel.EXEC,
                    permission_scope=PermissionScope.WORKSPACE,
                    read_only=False,
                    is_concurrency_safe=True,
                    tags=("subagent", "exec", "async"),
                    result_kind="json",
                    auto_approve=True,
                ),
                make_native_descriptor(
                    name="task_create",
                    description=(
                        "Create or update a recurring non-strategy task "
                        "schedule. Use this for hourly/daily/cron/every-N "
                        "agent or approved-script jobs. Default output "
                        "delivery to dashboard/local unless the operator's "
                        "original request explicitly names an external "
                        "delivery channel. For one-off background work use "
                        "subagent_run_async instead."
                    ),
                    input_schema=TASK_CREATE_SCHEMA,
                    handler=_wrap_task_create(deps),
                    risk=RiskLevel.WRITE,
                    permission_scope=PermissionScope.WORKSPACE,
                    read_only=False,
                    is_concurrency_safe=False,
                    tags=("task", "schedule", "automation"),
                    result_kind="json",
                    auto_approve=True,
                ),
                make_native_descriptor(
                    name="task_list",
                    description=(
                        "List background subagent tasks. Optionally "
                        "filter by state and parent session_id."
                    ),
                    input_schema=TASK_LIST_SCHEMA,
                    handler=_wrap_task_list(deps),
                    risk=RiskLevel.READ,
                    permission_scope=PermissionScope.WORKSPACE,
                    tags=("subagent", "task", "discovery"),
                    result_kind="json",
                    auto_approve=True,
                ),
                make_native_descriptor(
                    name="task_get",
                    description=(
                        "Full task record (state, progress notes, "
                        "output, error, tokens, wall_ms)."
                    ),
                    input_schema=TASK_GET_SCHEMA,
                    handler=_wrap_task_get(deps),
                    risk=RiskLevel.READ,
                    permission_scope=PermissionScope.WORKSPACE,
                    tags=("subagent", "task"),
                    result_kind="json",
                    auto_approve=True,
                ),
                make_native_descriptor(
                    name="task_output",
                    description=(
                        "Just the output blob of a finished task — "
                        "convenience over task_get."
                    ),
                    input_schema=TASK_OUTPUT_SCHEMA,
                    handler=_wrap_task_output(deps),
                    risk=RiskLevel.READ,
                    permission_scope=PermissionScope.WORKSPACE,
                    tags=("subagent", "task"),
                    result_kind="json",
                    auto_approve=True,
                ),
                make_native_descriptor(
                    name="task_stop",
                    description=(
                        "Cooperatively cancel a running task. The "
                        "worker checks the cancel flag between "
                        "iterations; the task transitions to "
                        "state='cancelled' once the next loop "
                        "iteration sees it."
                    ),
                    input_schema=TASK_STOP_SCHEMA,
                    handler=_wrap_task_stop(deps),
                    risk=RiskLevel.WRITE,
                    permission_scope=PermissionScope.WORKSPACE,
                    read_only=False,
                    tags=("subagent", "task"),
                    result_kind="json",
                ),
                make_native_descriptor(
                    name="task_update",
                    description=(
                        "Append a progress note (one line + optional "
                        "structured payload) to a running task. Used "
                        "by the parent (or a worker subagent reporting "
                        "back) to surface partial findings without "
                        "pulling the full output body into context."
                    ),
                    input_schema=TASK_UPDATE_SCHEMA,
                    handler=_wrap_task_update(deps),
                    risk=RiskLevel.WRITE,
                    permission_scope=PermissionScope.WORKSPACE,
                    read_only=False,
                    is_concurrency_safe=True,
                    tags=("subagent", "task", "progress"),
                    result_kind="json",
                    auto_approve=True,
                ),
                make_native_descriptor(
                    name="task_summary",
                    description=(
                        "Compact summary of a task: state, recent "
                        "progress notes, headline summary if the "
                        "worker emitted one. Use this to monitor a "
                        "long-running task without dumping its full "
                        "output blob into context."
                    ),
                    input_schema=TASK_SUMMARY_SCHEMA,
                    handler=_wrap_task_summary(deps),
                    risk=RiskLevel.READ,
                    permission_scope=PermissionScope.WORKSPACE,
                    tags=("subagent", "task"),
                    result_kind="json",
                    auto_approve=True,
                ),
            ])
        # ----- trading domain -----
        # All trading tools require ``config`` (RiskGate / ExecutionEngine /
        # StateStore + the workspace paths). They're registered together so
        # the agent gets the full surface (read summaries + risk_check +
        # kill switch + intent submission) atomically.
        descriptors.extend([
            make_native_descriptor(
                name="portfolio_summary",
                description=(
                    "Workspace-wide portfolio snapshot: account balances, "
                    "open positions, virtual ledger marks. Read-only."
                ),
                input_schema=PORTFOLIO_SUMMARY_SCHEMA,
                handler=_wrap_portfolio_summary(deps),
                risk=RiskLevel.READ,
                permission_scope=PermissionScope.WORKSPACE,
                tags=("trading", "portfolio", "read"),
                result_kind="json",
                auto_approve=True,
            ),
            make_native_descriptor(
                name="portfolio_positions",
                description="List open positions across the workspace.",
                input_schema=PORTFOLIO_POSITIONS_SCHEMA,
                handler=_wrap_portfolio_positions(deps),
                risk=RiskLevel.READ,
                permission_scope=PermissionScope.WORKSPACE,
                tags=("trading", "portfolio", "read"),
                result_kind="json",
                auto_approve=True,
            ),
            make_native_descriptor(
                name="portfolio_pnl",
                description="Realised + unrealised PnL across the workspace.",
                input_schema=PORTFOLIO_PNL_SCHEMA,
                handler=_wrap_portfolio_pnl(deps),
                risk=RiskLevel.READ,
                permission_scope=PermissionScope.WORKSPACE,
                tags=("trading", "portfolio", "read"),
                result_kind="json",
                auto_approve=True,
            ),
            make_native_descriptor(
                name="virtual_ledger",
                description=(
                    "Per-account virtual ledger snapshot (cash, equity, "
                    "exposure). Returns found=False for unknown accounts."
                ),
                input_schema=VIRTUAL_LEDGER_SCHEMA,
                handler=_wrap_virtual_ledger(deps),
                risk=RiskLevel.READ,
                permission_scope=PermissionScope.WORKSPACE,
                tags=("trading", "ledger", "read"),
                result_kind="json",
                auto_approve=True,
            ),
            make_native_descriptor(
                name="risk_check",
                description=(
                    "Dry-run a trade intent through RiskGate without "
                    "sending an order. Returns the decision (allow / "
                    "escalate / reject) plus the reasons + estimated "
                    "notional. Use this before trade_intent_submit when "
                    "in doubt — it never mutates state. For direct "
                    "one-shot order requests, this is the risk evidence "
                    "path; do not replace it with a strategy proposal."
                ),
                input_schema=RISK_CHECK_SCHEMA,
                handler=_wrap_risk_check(deps),
                risk=RiskLevel.READ,
                permission_scope=PermissionScope.WORKSPACE,
                tags=("trading", "risk", "read"),
                result_kind="json",
                auto_approve=True,
            ),
            make_native_descriptor(
                name="strategy_list",
                description=(
                    "List configured strategies (id, title, status, "
                    "markets, paper/live flags, limits)."
                ),
                input_schema=STRATEGY_LIST_SCHEMA,
                handler=_wrap_strategy_list(deps),
                risk=RiskLevel.READ,
                permission_scope=PermissionScope.WORKSPACE,
                tags=("trading", "strategy", "read"),
                result_kind="json",
                auto_approve=True,
            ),
            make_native_descriptor(
                name="strategy_view",
                description=(
                    "View a promoted strategy's spec + limits by id. For "
                    "in-flight proposals, use strategy_draft_proposal "
                    "`proposal_paths` or strategy_backtest `strategy_root` "
                    "and artifact paths instead; a proposal is not visible "
                    "to strategy_view until promoted."
                ),
                input_schema=STRATEGY_VIEW_SCHEMA,
                handler=_wrap_strategy_view(deps),
                risk=RiskLevel.READ,
                permission_scope=PermissionScope.WORKSPACE,
                tags=("trading", "strategy", "read"),
                result_kind="json",
                auto_approve=True,
            ),
            make_native_descriptor(
                name="strategy_history",
                description=(
                    "Tail recent ledger rows for a strategy across "
                    "triggers / intents / risk / orders / fills / "
                    "messages / reviews. Use to recall what a strategy "
                    "actually did before forming an opinion."
                ),
                input_schema=STRATEGY_HISTORY_SCHEMA,
                handler=_wrap_strategy_history(deps),
                risk=RiskLevel.READ,
                permission_scope=PermissionScope.WORKSPACE,
                tags=("trading", "strategy", "history", "read"),
                result_kind="json",
                auto_approve=True,
            ),
            make_native_descriptor(
                name="kill_switch_set",
                description=(
                    "Engage or release the runtime kill switch. Engaging "
                    "blocks every live order until released. Always "
                    "DANGEROUS — operator confirmation required."
                ),
                input_schema=KILL_SWITCH_SET_SCHEMA,
                handler=_wrap_kill_switch_set(deps),
                risk=RiskLevel.DANGEROUS,
                permission_scope=PermissionScope.WORKSPACE,
                read_only=False,
                is_concurrency_safe=False,
                mutates_paths=True,
                tags=("trading", "risk", "kill_switch"),
                result_kind="json",
            ),
            make_native_descriptor(
                name="trade_intent_submit",
                description=(
                    "Submit a trade intent through risk → approval → "
                    "execution. Returns rejected / pending_approval / "
                    "filled with the full risk_decision. DANGEROUS: "
                    "every successful call writes order/fill ledgers and "
                    "may move real money on live accounts. The native tool "
                    "permission layer lets this call reach the domain gate; "
                    "RiskGate and ApprovalGate remain authoritative for "
                    "rejected / pending_approval / filled outcomes. Direct "
                    "Call this tool after risk_check when the structured "
                    "intent is actionable; pending_approval means the "
                    "gateway/UI approval gate is now authoritative."
                ),
                input_schema=TRADE_INTENT_SUBMIT_SCHEMA,
                handler=_wrap_trade_intent_submit(deps),
                risk=RiskLevel.DANGEROUS,
                permission_scope=PermissionScope.WORKSPACE,
                read_only=False,
                is_concurrency_safe=False,
                mutates_paths=True,
                tags=("trading", "execute"),
                result_kind="json",
                auto_approve=True,
            ),
        ])
        # ----- LLM domain -----
        # All four route through ``LLMGateway(config)`` so they share the
        # workspace's tier policy, capability matrix, and budget gates.
        # Caller identity is fixed to ``agent:native`` inside the
        # handler module so the dashboard can tell kernel-initiated
        # calls apart from script-borrowed ones.
        descriptors.extend([
            make_native_descriptor(
                name="llm_complete",
                description=(
                    "Generic prompt → completion via the workspace "
                    "LLMGateway. Pass a task tag (for tier routing + "
                    "telemetry), a prompt, and an optional schema for "
                    "structured output."
                ),
                input_schema=LLM_COMPLETE_SCHEMA,
                handler=_wrap_llm_complete(deps),
                risk=RiskLevel.EXEC,
                permission_scope=PermissionScope.NETWORK,
                tags=("llm", "exec"),
                result_kind="json",
                auto_approve=True,
            ),
            make_native_descriptor(
                name="llm_classify",
                description=(
                    "Pick one label from a list (defaults to the light "
                    "tier). Cheap, deterministic, ideal for routing "
                    "decisions and quick triage."
                ),
                input_schema=LLM_CLASSIFY_SCHEMA,
                handler=_wrap_llm_classify(deps),
                risk=RiskLevel.EXEC,
                permission_scope=PermissionScope.NETWORK,
                tags=("llm", "classify"),
                result_kind="json",
                auto_approve=True,
            ),
            make_native_descriptor(
                name="llm_extract_json",
                description=(
                    "Schema-bound JSON extraction from text. Providers "
                    "without schema_json_mode will refuse — pick a "
                    "supported tier."
                ),
                input_schema=LLM_EXTRACT_JSON_SCHEMA,
                handler=_wrap_llm_extract_json(deps),
                risk=RiskLevel.EXEC,
                permission_scope=PermissionScope.NETWORK,
                tags=("llm", "extract"),
                result_kind="json",
                auto_approve=True,
            ),
            make_native_descriptor(
                name="llm_compress",
                description=(
                    "Compress a long blob of text into <max_tokens "
                    "tokens at the light tier. Use before passing big "
                    "context windows to the high tier."
                ),
                input_schema=LLM_COMPRESS_SCHEMA,
                handler=_wrap_llm_compress(deps),
                risk=RiskLevel.EXEC,
                permission_scope=PermissionScope.NETWORK,
                tags=("llm", "compress"),
                result_kind="json",
                auto_approve=True,
            ),
        ])
        # ----- strategy runtime -----
        # Strategy authoring is a file-editing lane: the agent scaffolds a
        # *draft* proposal with strategy_draft_proposal, edits the staged
        # after/strategies/<id>/ files in place with read_file/edit_file/
        # write_file, validates with strategy_validate, and submits with
        # strategy_submit_proposal (which only enters the pending-review
        # queue once validation passes). Operators promote it, and the agent
        # (or a cron-driven bridge) calls run_tick. Every order still goes
        # through the trading kernel via the in-strategy
        # ``ctx.trading.submit_intent`` facade.
        descriptors.extend([
            make_native_descriptor(
                name="strategy_draft_proposal",
                description=(
                    "Scaffold a NEW strategy package as a DRAFT proposal, or "
                    "seed a draft from an existing promoted strategy via "
                    "from_strategy_id to iterate on it. Returns the "
                    "proposal_id plus `proposal_paths` (the "
                    "after/strategies/<id>/ files: strategy.yml, strategy.md, "
                    "main.py, tests/...) and `next_steps`. This does NOT enter "
                        "the pending-review queue and writes NO inline code — you "
                        "then author the logic by editing the staged files with "
                        "read_file + edit_file / write_file (they live under "
                        "evolution/proposals/<id>/ which the workspace mutation "
                        "guard allows), run strategy_validate, and finish with "
                        "strategy_submit_proposal. If the operator explicitly "
                        "asks for a draft/proposal scaffold only or says not to "
                        "edit, submit, promote, run, or trade, return this "
                        "scaffold result and stop; do not follow these next "
                        "steps in the same turn. Read the strategy_author skill "
                    "(skill_view) for the exact per-file format. Only for "
                    "requests that actually ask for a strategy: never reroute "
                    "'enable live trading' or an immediate buy/sell order into "
                    "a strategy package. Author main.py inside the "
                    "StrategyContext contract: `from nerya.strategies import "
                    "StrategyContext, StrategyResult, StrategyAgentTask` (do "
                    "not import from nerya.sdk / nerya.strategy), read candles "
                    "via ctx.market.candles/features, read positions via "
                    "ctx.portfolio.positions(market) (a list — iterate or "
                    "select a row, never .get), use the configured account via "
                    "ctx.config.accounts[0] (there is no ctx.account_id), place "
                    "orders via ctx.trading.submit_intent/open_position/"
                    "close_position, and return ctx.result.hold/skip/ok/error "
                    "or StrategyAgentTask.dispatch/skip/error. Preserve "
                    "explicit session/tool-evidence market scope; do not copy "
                    "example markets."
                ),
                input_schema=STRATEGY_DRAFT_PROPOSAL_SCHEMA,
                handler=_wrap_strategy_draft_proposal(deps),
                risk=RiskLevel.WRITE,
                permission_scope=PermissionScope.WORKSPACE,
                read_only=False,
                is_concurrency_safe=False,
                mutates_paths=True,
                tags=("strategy", "evolve", "write"),
                result_kind="json",
            ),
            make_native_descriptor(
                name="strategy_submit_proposal",
                description=(
                    "Finish a DRAFT strategy proposal: re-validate the edited "
                    "after/strategies/<id>/ files and, only if validation "
                    "passes, move the proposal into the pending-review queue "
                    "(draft -> pending_review) so the operator can approve it. "
                    "If validation still has blockers the proposal stays a "
                    "draft and the blockers are returned so you can edit the "
                    "files and submit again. On success the result includes "
                    "`backtest_required` and `next_required_action`; call "
                    "strategy_backtest with that proposal_id before asking to "
                    "promote. Call this after strategy_draft_proposal + your "
                    "edits + strategy_validate, never before the files are "
                    "authored."
                ),
                input_schema=STRATEGY_SUBMIT_PROPOSAL_SCHEMA,
                handler=_wrap_strategy_submit_proposal(deps),
                risk=RiskLevel.WRITE,
                permission_scope=PermissionScope.WORKSPACE,
                read_only=False,
                is_concurrency_safe=False,
                mutates_paths=True,
                tags=("strategy", "evolve", "write"),
                result_kind="json",
            ),
            make_native_descriptor(
                name="strategy_validate",
                description=(
                    "Run schema + static-policy + import-smoke "
                    "validation against a promoted strategy package "
                    "(strategy_id) or against an in-flight proposal's "
                    "after/strategies/<id>/ files (proposal_id). If the "
                    "operator gives a prp_* proposal id, pass proposal_id; "
                    "do not validate a similarly named promoted strategy "
                    "package instead."
                ),
                input_schema=STRATEGY_VALIDATE_SCHEMA,
                handler=_wrap_strategy_validate(deps),
                risk=RiskLevel.READ,
                permission_scope=PermissionScope.WORKSPACE,
                tags=("strategy", "validate", "read"),
                result_kind="json",
                auto_approve=True,
            ),
            make_native_descriptor(
                name="strategy_delete_proposal",
                description=(
                    "Delete a pending strategy package/tuning proposal "
                    "(prp_* id) from the pending-review queue. Use this when "
                    "the operator decides not to keep a generated proposal "
                    "instead of leaving it pending. Deleting a pending "
                    "proposal does not touch any promoted strategy. An "
                    "already-applied proposal is an audit record of a change "
                    "that already landed; deleting it requires force=true and "
                    "does NOT roll the change back (use the rollback flow for "
                    "that)."
                ),
                input_schema=STRATEGY_DELETE_PROPOSAL_SCHEMA,
                handler=_wrap_strategy_delete_proposal(deps),
                risk=RiskLevel.WRITE,
                permission_scope=PermissionScope.WORKSPACE,
                read_only=False,
                is_concurrency_safe=False,
                mutates_paths=True,
                tags=("strategy", "evolve", "write"),
                result_kind="json",
            ),
            make_native_descriptor(
                name="strategy_backtest",
                description=(
                    "Run the built-in backtest engine for a strategy package, "
                    "or a strategy-local freeform SDK backtest when the "
                    "package provides one. "
                    "Accepts either strategy_id for promoted packages or "
                    "proposal_id for in-flight strategy proposals, so the "
                    "agent can validate and backtest before promotion. If "
                    "the operator gives a prp_* proposal id, call this with "
                    "proposal_id and do not substitute a similarly named "
                    "promoted strategy_id. "
                    "The default preset requests roughly six months of "
                    "history for general CEX strategies; meme/on-chain/DEX "
                    "pool strategies use a one-week requested window unless "
                    "an explicit config overrides it; the recommended short "
                    "window is a 45-day window when real data coverage allows. "
                    "Treat a returned recommended_coverage_ok flag as the "
                    "short-window backtest coverage signal for those markets. "
                    "In both cases, use the "
                    "maximum real-data window the source can return. Shorter "
                    "windows are valid for new/short-lived markets when the "
                    "package/result explains the data coverage. "
                    "Writes backtest artifacts under the package's "
                    "backtests/<timestamp>/ directory and returns the verdict "
                    "plus key metrics, `strategy_root`, and artifact paths. "
                    "Do not call the standard backtest unavailable when real "
                    "candles were loaded, even if the market only has a few "
                    "days of history. For non-template or custom-data strategies, a "
                    "freeform SDK backtest is accepted when it returns a "
                    "capital curve and trade details; do not force a "
                    "different template in that lane. If the result has "
                    "`reason='no_historical_data'`, stop and report the data "
                    "gap; do not retry with mock/synthetic/placeholder data. "
                    "For custom/event-driven packages, use returned replay "
                    "evidence and `paper_review_allowed` / `review_gate` for "
                    "paper review; do not override those gate fields with a "
                    "manual FAIL/no_trades rejection. Shadow/live progression "
                    "still requires explicit operator approval. "
                    "If the returned payload has ok=true, describe it as a "
                    "completed backtest; when kind=freeform_backtest, describe "
                    "it as a strategy-local SDK research backtest, otherwise "
                    "as a completed standard OHLCV replay over the loaded window, "
                    "never as unavailable, impossible, or not applicable; "
                    "zero trades means no simulated OHLCV fills. "
                    "Write the operator-facing summary in plain language the "
                    "user can understand: say whether the result is good or "
                    "bad and call out when zero/low trade counts make the "
                    "numbers unreliable. Reuse the exact numbers from "
                    "metrics_display / operator_summary_text / operator_summary, "
                    "but do not copy their internal field labels, key=value "
                    "dumps, or any 'copy these values' style notes verbatim. "
                    "Raw *_pct metric values are already percentage points; "
                    "display 0.15 as 0.15%, not 15%. Never multiply them "
                    "by 100. The model-facing `metrics` "
                    "object contains display strings; read `raw_metrics_file` "
                    "only for machine verification."
                ),
                input_schema=STRATEGY_BACKTEST_SCHEMA,
                handler=_wrap_strategy_backtest(deps),
                risk=RiskLevel.WRITE,
                permission_scope=PermissionScope.WORKSPACE,
                read_only=False,
                is_concurrency_safe=False,
                mutates_paths=True,
                tags=("strategy", "backtest", "write"),
                result_kind="json",
                auto_approve=True,
            ),
            make_native_descriptor(
                name="strategy_promote",
                description=(
                    "Approve + apply an agent-generated strategy "
                    "package proposal. Refuses to promote when the "
                    "validator reports any blockers. By default it also "
                    "refuses promotion until the proposal has a "
                    "strategy_backtest artifact. Custom event-driven "
                    "packages may use backtest_policy=flexible_meme with a "
                    "real replay or explicit operator-approved "
                    "standard-backtest waiver. Do not call this tool during "
                    "an ordinary create/review strategy request. Call it only "
                    "when the operator explicitly asks to promote, approve, "
                    "move to paper/shadow/live, or continue an approval gate; "
                    "otherwise report the required approval as the next step."
                ),
                input_schema=STRATEGY_PROMOTE_SCHEMA,
                handler=_wrap_strategy_promote(deps),
                risk=RiskLevel.DANGEROUS,
                permission_scope=PermissionScope.WORKSPACE,
                read_only=False,
                is_concurrency_safe=False,
                mutates_paths=True,
                tags=("strategy", "evolve", "promote"),
                result_kind="json",
            ),
            make_native_descriptor(
                name="strategy_run_tick",
                description=(
                    "Run one strategy tick. The runner imports the "
                    "package's main.py, threads in a StrategyContext, "
                    "and writes a run record to "
                    "strategies/<id>/runs/<run_id>.json. Live mode "
                    "still requires runtime.live_trading_enabled."
                ),
                input_schema=STRATEGY_RUN_TICK_SCHEMA,
                handler=_wrap_strategy_run_tick(deps),
                risk=RiskLevel.EXEC,
                permission_scope=PermissionScope.WORKSPACE,
                read_only=False,
                is_concurrency_safe=False,
                mutates_paths=True,
                tags=("strategy", "exec"),
                result_kind="json",
                auto_approve_when=lambda payload: is_strategy_run_tick_auto_approved(
                    payload, deps
                ),
            ),
            make_native_descriptor(
                name="strategy_kill_switch",
                description=(
                    "Inspect / set / clear the per-strategy kill "
                    "switch. Asserting requires a non-empty reason; "
                    "the next tick will return HOLD until cleared."
                ),
                input_schema=STRATEGY_KILL_SWITCH_SCHEMA,
                handler=_wrap_strategy_kill_switch(deps),
                risk=RiskLevel.WRITE,
                permission_scope=PermissionScope.WORKSPACE,
                read_only=False,
                is_concurrency_safe=False,
                mutates_paths=True,
                tags=("strategy", "kill_switch"),
                result_kind="json",
            ),
            make_native_descriptor(
                name="strategy_run_history",
                description=(
                    "List recent strategy runs with status, mode, "
                    "duration, and an audit log per tick. Read-only."
                ),
                input_schema=STRATEGY_RUN_HISTORY_SCHEMA,
                handler=_wrap_strategy_run_history(deps),
                risk=RiskLevel.READ,
                permission_scope=PermissionScope.WORKSPACE,
                tags=("strategy", "history", "read"),
                result_kind="json",
                auto_approve=True,
            ),
            # ----- self-evolution tuning loop -----
            # tuning is the per-strategy self-evolution
            # surface. ``tuning_generate`` adds a tuning block to an
            # existing package; ``tuning_run`` executes one cycle and
            # writes a strategy_tuning_proposal; ``tuning_status``/
            # ``tuning_snapshot`` are read-only views the dashboard's
            # Self-Evolution panel binds to.
            make_native_descriptor(
                name="strategy_tuning_generate",
                description=(
                    "Add a tuning block (schedule + objectives + "
                    "subagents/strategy_tuner.agent.md) to an existing "
                    "strategy package as a strategy_tuning_proposal. "
                    "Promotion still requires operator approval."
                ),
                input_schema=STRATEGY_TUNING_GENERATE_SCHEMA,
                handler=_wrap_strategy_tuning_generate(deps),
                risk=RiskLevel.WRITE,
                permission_scope=PermissionScope.WORKSPACE,
                read_only=False,
                is_concurrency_safe=False,
                mutates_paths=True,
                tags=("strategy", "tuning", "evolve", "write"),
                result_kind="json",
            ),
            make_native_descriptor(
                name="strategy_tuning_run",
                description=(
                    "Run one self-evolution cycle: build performance "
                    "snapshot, dispatch the strategy_tuner subagent, "
                    "filter proposed_changes through the manifest's "
                    "guardrails, and (unless dry_run) emit a "
                    "strategy_tuning_proposal."
                ),
                input_schema=STRATEGY_TUNING_RUN_SCHEMA,
                handler=_wrap_strategy_tuning_run(deps),
                risk=RiskLevel.EXEC,
                permission_scope=PermissionScope.WORKSPACE,
                read_only=False,
                is_concurrency_safe=False,
                mutates_paths=True,
                tags=("strategy", "tuning", "exec"),
                result_kind="json",
            ),
            make_native_descriptor(
                name="strategy_tuning_status",
                description=(
                    "Aggregate tuning view: manifest tuning block + "
                    "performance snapshot + pending tuning proposals."
                ),
                input_schema=STRATEGY_TUNING_STATUS_SCHEMA,
                handler=_wrap_strategy_tuning_status(deps),
                risk=RiskLevel.READ,
                permission_scope=PermissionScope.WORKSPACE,
                tags=("strategy", "tuning", "read"),
                result_kind="json",
                auto_approve=True,
            ),
            make_native_descriptor(
                name="strategy_tuning_snapshot",
                description=(
                    "Read-only performance snapshot the tuning loop "
                    "consumes (run/trade/risk/cost metrics)."
                ),
                input_schema=STRATEGY_TUNING_SNAPSHOT_SCHEMA,
                handler=_wrap_strategy_tuning_snapshot(deps),
                risk=RiskLevel.READ,
                permission_scope=PermissionScope.WORKSPACE,
                tags=("strategy", "tuning", "read"),
                result_kind="json",
                auto_approve=True,
            ),
        ])
    registry.register_all(descriptors, replace=replace)
    register_skill_tool(
        registry,
        skill_index=deps.skill_index,
        replace=replace,
    )
    deps.skill_index.reload()
    return deps


__all__ = [
    "NativeToolDeps",
    "build_native_tool_deps",
    "register_native_tools",
]
