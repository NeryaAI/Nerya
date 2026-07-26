"""The canonical list of directories a workspace must have.

`WorkspaceManager.init()` walks this list and creates missing dirs. New
features add to the list here rather than sprinkling mkdirs across
modules.
"""

from __future__ import annotations

from pathlib import Path

from ..core.paths import WorkspacePaths


def required_dirs(p: WorkspacePaths) -> list[Path]:
    return [
        p.state, p.snapshots, p.virtual_ledgers, p.workspace_sync_state,
        p.journals,
        p.inbox, p.inbox_triggers, p.inbox_sdk_orders,
        p.inbox_llm_requests, p.inbox_tasks, p.inbox_messages,
        p.dead_letter,
        p.outbox, p.outbox_messages, p.outbox_reports, p.outbox_sdk_results,
        p.memory, p.strategy_learnings,
        p.agents, p.subagents,
        p.skills, p.skills_installed, p.skills_pending, p.skills_rejected,
        p.accounts,
        p.strategies,
        p.triggers_dir,
        p.scripts_dir, p.scripts_pending, p.scripts_approved,
        p.scripts_rejected, p.scripts_examples,
        p.messages_dir, p.messages_templates,
        p.approvals,
        p.vault,
        p.evolution, p.proposals,
        p.providers_dir, p.providers_pending,
        p.artifacts, p.artifacts / "reports",
        p.artifacts / "generated", p.artifacts / "charts",
        p.artifacts / "backtests",
    ]


REQUIRED_JOURNALS = [
    "agent", "skills", "triggers", "trading", "messages",
    "scripts", "llm", "security", "evolution", "errors",
]
