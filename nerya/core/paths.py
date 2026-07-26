"""Workspace path layout. Every filesystem write in Nerya goes through here."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class WorkspacePaths:
    root: Path

    @property
    def config(self) -> Path: return self.root / "nerya.yml"

    # state
    @property
    def state(self) -> Path: return self.root / "state"
    @property
    def runtime_state(self) -> Path: return self.state / "runtime.json"
    @property
    def snapshots(self) -> Path: return self.state / "snapshots"
    @property
    def virtual_ledgers(self) -> Path: return self.state / "virtual_ledgers"
    @property
    def workspace_sync_state(self) -> Path: return self.state / "workspace_sync"
    @property
    def workspace_sync_status(self) -> Path: return self.workspace_sync_state / "status.json"
    @property
    def workspace_sync_git_checkout(self) -> Path: return self.workspace_sync_state / "git_checkout"

    @property
    def workspace_sync_config(self) -> Path: return self.root / "workspace-sync.yml"

    # journals
    @property
    def journals(self) -> Path: return self.root / "journals"
    def journal(self, name: str) -> Path: return self.journals / f"{name}.jsonl"

    # inbox / outbox
    @property
    def inbox(self) -> Path: return self.root / "inbox"
    @property
    def outbox(self) -> Path: return self.root / "outbox"
    @property
    def inbox_triggers(self) -> Path: return self.inbox / "triggers"
    @property
    def inbox_sdk_orders(self) -> Path: return self.inbox / "sdk_orders"
    @property
    def inbox_llm_requests(self) -> Path: return self.inbox / "llm_requests"
    @property
    def inbox_tasks(self) -> Path: return self.inbox / "tasks"
    @property
    def inbox_messages(self) -> Path: return self.inbox / "messages"
    @property
    def outbox_messages(self) -> Path: return self.outbox / "messages"
    @property
    def outbox_reports(self) -> Path: return self.outbox / "reports"
    @property
    def outbox_sdk_results(self) -> Path: return self.outbox / "sdk_results"
    @property
    def dead_letter(self) -> Path: return self.inbox_triggers / "_dead_letter"

    # memory
    @property
    def memory(self) -> Path: return self.root / "memory"
    @property
    def strategy_learnings(self) -> Path: return self.memory / "strategy_learnings"
    @property
    def memory_index(self) -> Path:
        # Long-term structured fact index (JSONL). Lives next to the
        # whitelisted markdown notes so backup/export tooling already
        # picks it up; one record per appended fact, see
        # ``nerya.agent.memory_index.MemoryIndex``.
        return self.memory / "index.jsonl"

    # agents / subagents
    @property
    def agents(self) -> Path: return self.root / "agents"
    @property
    def subagents(self) -> Path: return self.root / "subagents"

    # skills
    @property
    def skills(self) -> Path: return self.root / "skills"
    @property
    def skills_enabled(self) -> Path: return self.skills / "enabled.yml"
    @property
    def skills_installed(self) -> Path: return self.skills / "installed"
    @property
    def skills_pending(self) -> Path: return self.skills / "pending"
    @property
    def skills_rejected(self) -> Path: return self.skills / "rejected"
    # runtime skills hub (trust + hash + lock).
    @property
    def skills_lock(self) -> Path: return self.skills / "skills.lock.yml"
    @property
    def skills_trust(self) -> Path: return self.skills / "trust.yml"

    # accounts
    @property
    def accounts(self) -> Path: return self.root / "accounts"
    @property
    def accounts_file(self) -> Path: return self.accounts / "accounts.yml"
    @property
    def exchanges_file(self) -> Path: return self.accounts / "exchanges.yml"
    @property
    def secrets_refs_file(self) -> Path: return self.accounts / "secrets.refs.yml"

    # strategies
    @property
    def strategies(self) -> Path: return self.root / "strategies"
    def strategy(self, sid: str) -> Path: return self.strategies / sid
    def strategy_history(self, sid: str) -> Path: return self.strategy(sid) / "history"
    def strategy_sessions(self, sid: str) -> Path: return self.strategy(sid) / "sessions"

    # triggers
    @property
    def triggers_dir(self) -> Path: return self.root / "triggers"
    @property
    def triggers_routes_file(self) -> Path: return self.triggers_dir / "routes.yml"
    @property
    def triggers_schedules_file(self) -> Path: return self.triggers_dir / "schedules.yml"

    # scripts
    @property
    def scripts_dir(self) -> Path: return self.root / "scripts"
    @property
    def scripts_pending(self) -> Path: return self.scripts_dir / "pending"
    @property
    def scripts_approved(self) -> Path: return self.scripts_dir / "approved"
    @property
    def scripts_rejected(self) -> Path: return self.scripts_dir / "rejected"
    @property
    def scripts_examples(self) -> Path: return self.scripts_dir / "examples"

    # messages
    @property
    def messages_dir(self) -> Path: return self.root / "messages"
    @property
    def messages_channels(self) -> Path: return self.messages_dir / "channels.yml"
    @property
    def messages_templates(self) -> Path: return self.messages_dir / "templates"

    # approvals
    @property
    def approvals(self) -> Path: return self.root / "approvals"
    @property
    def approvals_pending(self) -> Path: return self.approvals / "pending.jsonl"
    @property
    def approvals_approved(self) -> Path: return self.approvals / "approved.jsonl"
    @property
    def approvals_rejected(self) -> Path: return self.approvals / "rejected.jsonl"

    # vault
    @property
    def vault(self) -> Path: return self.root / "vault"
    @property
    def vault_enc(self) -> Path: return self.vault / "secrets.enc"
    @property
    def vault_keyring(self) -> Path: return self.vault / "keyring.ref"

    # security
    @property
    def security(self) -> Path: return self.root / "security"
    # provider auth records (tokens live in vault, only
    # pointers + metadata sit on disk here).
    @property
    def provider_auth(self) -> Path: return self.security / "provider_auth.json"

    # evolution
    @property
    def evolution(self) -> Path: return self.root / "evolution"
    @property
    def proposals(self) -> Path: return self.evolution / "proposals"
    @property
    def evolution_events(self) -> Path: return self.evolution / "events.jsonl"
    @property
    def evolution_signals(self) -> Path: return self.evolution / "signals.jsonl"
    @property
    def evolution_assets(self) -> Path: return self.evolution / "assets"
    @property
    def evolution_genes(self) -> Path: return self.evolution_assets / "genes.json"
    @property
    def evolution_capsules(self) -> Path: return self.evolution_assets / "capsules.jsonl"
    @property
    def evolution_candidates(self) -> Path: return self.evolution_assets / "candidates.jsonl"
    @property
    def evolution_rejected(self) -> Path: return self.evolution_assets / "rejected.jsonl"
    @property
    def evolution_validation_plans(self) -> Path: return self.evolution / "validation_plans"

    # user-authored exchange providers (workspace/providers/<id>/provider.py)
    @property
    def providers_dir(self) -> Path: return self.root / "providers"
    @property
    def providers_pending(self) -> Path: return self.providers_dir / "_pending"

    # MCP connectors (workspace/connectors/mcp_servers.yml + token cache).
    # Distinct from providers_dir which holds *trading* venue adapters.
    @property
    def connectors_dir(self) -> Path: return self.root / "connectors"
    @property
    def connectors_mcp_servers(self) -> Path: return self.connectors_dir / "mcp_servers.yml"
    @property
    def connectors_oauth_cache(self) -> Path: return self.connectors_dir / ".oauth_cache.json"

    # artifacts
    @property
    def artifacts(self) -> Path: return self.root / "artifacts"

    # dev logs
    @property
    def dev_logs(self) -> Path: return self.root / "dev_logs"
    def dev_log(self, kind: str) -> Path: return self.dev_logs / f"{kind}.jsonl"

    # db
    @property
    def db(self) -> Path: return self.root / "nerya.db"


def _resolve_home() -> Path:
    """Return the Nerya home dir holding one or more profile workspaces.

    Resolution order:

    * ``NERYA_HOME`` env var if set
    * ``~/.nerya`` otherwise

    See runtime multi-profile isolation. Each profile
    becomes a workspace directory under this home (``<home>/<profile>``)
    so ``nerya --profile dev`` and ``nerya --profile live`` get fully
    independent journals, db, and outboxes without colliding.
    """
    raw = os.environ.get("NERYA_HOME")
    if raw and raw.strip():
        return Path(raw).expanduser().resolve()
    return (Path.home() / ".nerya").resolve()


def resolve_workspace(
    path: str | os.PathLike | None = None,
    *,
    profile: str | None = None,
) -> WorkspacePaths:
    """Resolve the active workspace root.

    Precedence (highest first):

    1. Explicit ``path`` argument.
    2. Explicit ``profile`` argument (resolved under :func:`_resolve_home`).
    3. ``NERYA_PROFILE`` env var (resolved under :func:`_resolve_home`).
    4. ``NERYA_WORKSPACE`` env var (legacy single-profile shape).
    5. ``$NERYA_HOME`` if it already looks like a workspace
       (contains ``nerya.yml`` or ``state/``).
    6. ``$NERYA_HOME/default``.

    keep legacy callers happy by treating
    ``NERYA_WORKSPACE`` as the deepest fallback when no profile is given.
    """
    if path is not None:
        root = Path(path).expanduser().resolve()
        return WorkspacePaths(root=root)

    selected_profile = (profile or os.environ.get("NERYA_PROFILE") or "").strip()
    if selected_profile:
        home = _resolve_home()
        return WorkspacePaths(root=(home / selected_profile).resolve())

    legacy = os.environ.get("NERYA_WORKSPACE")
    if legacy and legacy.strip():
        return WorkspacePaths(root=Path(legacy).expanduser().resolve())

    home = _resolve_home()
    if (home / "nerya.yml").exists() or (home / "state").exists():
        return WorkspacePaths(root=home)
    return WorkspacePaths(root=(home / "default").resolve())


def list_profiles(home: Path | None = None) -> list[str]:
    """Return the names of profiles present under :func:`_resolve_home`.

    A "profile" is any immediate subdirectory of ``$NERYA_HOME`` that
    looks like a workspace (i.e. contains either ``nerya.yml`` or a
    ``state/`` directory). Used by the CLI ``profile list`` command.
    """
    base = (home or _resolve_home())
    if not base.exists() or not base.is_dir():
        return []
    out: list[str] = []
    for child in sorted(base.iterdir()):
        if not child.is_dir():
            continue
        if (child / "nerya.yml").exists() or (child / "state").exists():
            out.append(child.name)
    return out
