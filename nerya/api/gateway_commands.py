"""Platform-neutral gateway command registry.

Hermes-aligned: the same `/help`, `/menu`, `/status`, `/new`, `/trace`
commands must work across Telegram, dashboard, generic webhook, and any
future adapter without copy/pasting Python `if/elif` chains in every
gateway route. Skills can extend the registry with additional commands
(planned — see ``Plan 23 #3``); for now we ship the Hermes baseline.

The registry is intentionally small and side-effect free at import time:
- ``BUILTIN_COMMANDS`` lists the canonical specs.
- ``GatewayCommandRegistry`` looks up by name/alias and renders menus +
  help text.
- ``handle_command`` runs the matching handler (if any) and returns a
  ``CommandOutcome`` whose ``handled`` flag tells the caller whether to
  fall through to the LLM agent loop.

Handlers receive a thin ``CommandContext`` so they don't import gateway
route helpers directly. Adapter-specific transports (sending Telegram
messages, recording dashboard mirror entries, etc.) remain the route's
job — the registry just produces text + bookkeeping instructions.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Iterable, Mapping, Optional


@dataclass(frozen=True)
class CommandSpec:
    """Declarative metadata for a gateway command."""

    name: str
    description: str
    aliases: tuple[str, ...] = ()
    platforms: tuple[str, ...] = ()  # empty == all platforms
    scope: str = "operator"
    show_in_menu: bool = True

    @property
    def menu_name(self) -> str:
        return self.name.lstrip("/")


@dataclass
class CommandContext:
    """Side-effect surface a command handler can rely on."""

    client: Any
    platform: str
    chat_id: str
    session_id: str
    raw_text: str
    state: Mapping[str, Any] = field(default_factory=dict)
    save_state: Callable[[Mapping[str, Any]], None] | None = None
    delete_session: Callable[[str], None] | None = None
    dashboard_url: str = ""

    def update_state(self, **values: Any) -> None:
        if self.save_state is None:
            return
        merged = dict(self.state) if isinstance(self.state, Mapping) else {}
        merged.update(values)
        self.save_state(merged)


@dataclass
class CommandOutcome:
    handled: bool
    reply_text: str = ""
    command: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {"handled": self.handled, "reply_text": self.reply_text, "command": self.command}


CommandHandler = Callable[[CommandSpec, CommandContext], CommandOutcome]


_DEFAULT_DASHBOARD_URL = "http://127.0.0.1:3001/dashboard"


def resolve_dashboard_url(config: Any) -> str:
    """Return the operator-configured dashboard URL (env > config > default)."""

    import os

    env = os.environ.get("NERYA_DASHBOARD_URL", "").strip()
    if env:
        return env
    if config is not None and hasattr(config, "get"):
        for key in ("gateway.dashboard_url", "gateway.dashboard.public_url", "dashboard.public_url"):
            value = config.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
    return _DEFAULT_DASHBOARD_URL


# ---------------------------------------------------------------------------
# Built-in handlers
# ---------------------------------------------------------------------------

def _handle_help(spec: CommandSpec, ctx: CommandContext) -> CommandOutcome:
    return CommandOutcome(handled=True, reply_text=DEFAULT_REGISTRY.help_text(platform=ctx.platform), command=spec.name)


def _handle_status(spec: CommandSpec, ctx: CommandContext) -> CommandOutcome:
    last_turn = "none"
    if isinstance(ctx.state, Mapping):
        last_turn = str(ctx.state.get("last_turn_id") or "none")
    dash = ctx.dashboard_url or _DEFAULT_DASHBOARD_URL
    text = (
        "Nerya API is running.\n"
        f"Dashboard: {dash}\n"
        f"Last {ctx.platform} turn: {last_turn}"
    )
    return CommandOutcome(handled=True, reply_text=text, command=spec.name)


def _handle_new(spec: CommandSpec, ctx: CommandContext) -> CommandOutcome:
    if ctx.delete_session is not None and ctx.session_id:
        try:
            ctx.delete_session(ctx.session_id)
        except Exception:
            pass
    if isinstance(ctx.state, Mapping):
        active = dict(ctx.state.get("active_sessions") or {})
        active.pop(str(ctx.chat_id), None)
        ctx.update_state(active_sessions=active)
    text = (
        f"Started a fresh Nerya {ctx.platform} session. "
        "Send your next task when ready."
    )
    return CommandOutcome(handled=True, reply_text=text, command=spec.name)


def _handle_trace(spec: CommandSpec, ctx: CommandContext) -> CommandOutcome:
    fallback = f"No {ctx.platform} agent turn has completed yet."
    last_trace = ""
    if isinstance(ctx.state, Mapping):
        last_trace = str(ctx.state.get("last_trace") or "")
    text = last_trace or fallback
    return CommandOutcome(handled=True, reply_text=text, command=spec.name)


def _iso_from_db_ts(value: Any) -> str:
    try:
        ts = float(value)
    except Exception:
        return ""
    if ts <= 0:
        return ""
    return datetime.fromtimestamp(ts, timezone.utc).isoformat().replace("+00:00", "Z")


def _parse_meta_json(raw: Any) -> dict[str, Any]:
    try:
        meta = json.loads(str(raw or "{}"))
    except Exception:
        meta = {}
    return meta if isinstance(meta, dict) else {}


def _file_session_asdict(row: Any) -> dict[str, Any]:
    data = row.asdict() if hasattr(row, "asdict") else {}
    data.setdefault("source", "session_file")
    data.setdefault("turn_count", len(data.get("turn_ids") or []))
    return data


def _db_session_asdict(row: Mapping[str, Any]) -> dict[str, Any]:
    title = str(row.get("title") or "").strip()
    meta = _parse_meta_json(row.get("meta_json"))
    if title and not meta.get("title"):
        meta["title"] = title
        meta.setdefault("title_source", "db")
    return {
        "session_id": str(row.get("session_id") or ""),
        "strategy_id": row.get("strategy_id"),
        "created_at": _iso_from_db_ts(row.get("created_at")),
        "updated_at": _iso_from_db_ts(row.get("updated_at")),
        "turn_ids": [],
        "invoked_skills": [],
        "skill_state": {},
        "last_action": None,
        "meta": meta,
        "source": row.get("source") or "",
        "message_count": int(row.get("message_count") or 0),
        "turn_count": int(row.get("turn_count") or 0),
    }


def _merge_session_dict(file_state: dict[str, Any], db_row: Mapping[str, Any] | None) -> dict[str, Any]:
    if not db_row:
        return file_state
    db_state = _db_session_asdict(db_row)
    merged = dict(db_state)
    merged.update(file_state)
    file_meta = file_state.get("meta") if isinstance(file_state.get("meta"), dict) else {}
    db_meta = db_state.get("meta") if isinstance(db_state.get("meta"), dict) else {}
    meta = {**db_meta, **file_meta}
    if not meta.get("title") and db_meta.get("title"):
        meta["title"] = db_meta["title"]
    merged["meta"] = meta
    if not merged.get("created_at"):
        merged["created_at"] = db_state.get("created_at") or ""
    if _session_updated_ts(db_state) > _session_updated_ts(file_state):
        merged["updated_at"] = db_state.get("updated_at") or merged.get("updated_at")
    if not merged.get("source"):
        merged["source"] = db_state.get("source") or ""
    if not merged.get("message_count"):
        merged["message_count"] = db_state.get("message_count") or 0
    if not merged.get("turn_count"):
        merged["turn_count"] = db_state.get("turn_count") or 0
    return merged


def _session_updated_ts(session: Mapping[str, Any]) -> float:
    raw = session.get("updated_at")
    if isinstance(raw, (int, float)):
        return float(raw)
    text = str(raw or "").strip()
    if not text:
        return 0.0
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).timestamp()
    except Exception:
        return 0.0


def _db_message_counts(paths: Any, session_ids: list[str]) -> dict[str, dict[str, int]]:
    if not session_ids:
        return {}
    from ..db.sqlite import connect

    placeholders = ",".join("?" for _ in session_ids)
    con = connect(paths.db)
    try:
        rows = con.execute(
            f"""
            SELECT
                session_id,
                COUNT(*) AS message_count,
                COUNT(DISTINCT COALESCE(turn_id, message_id)) AS turn_count
            FROM agent_messages
            WHERE deleted=0 AND session_id IN ({placeholders})
            GROUP BY session_id
            """,
            tuple(session_ids),
        ).fetchall()
        return {
            str(row["session_id"]): {
                "message_count": int(row["message_count"] or 0),
                "turn_count": int(row["turn_count"] or 0),
            }
            for row in rows
        }
    finally:
        con.close()


def _list_db_sessions(paths: Any, *, limit: int) -> list[dict[str, Any]]:
    from ..db.repositories import AgentSessionRepository
    from ..db.sqlite import connect

    con = connect(paths.db)
    try:
        rows = [dict(row) for row in AgentSessionRepository(con).list_sessions(limit=limit)]
        ids = [str(row.get("session_id") or "") for row in rows if row.get("session_id")]
    finally:
        con.close()
    counts = _db_message_counts(paths, ids)
    for row in rows:
        row.update(counts.get(str(row.get("session_id") or ""), {}))
    return rows


def _get_db_session(paths: Any, session_id: str) -> dict[str, Any] | None:
    from ..db.repositories import AgentSessionRepository
    from ..db.sqlite import connect

    con = connect(paths.db)
    try:
        row = AgentSessionRepository(con).get_session(session_id)
    finally:
        con.close()
    if not row:
        return None
    counts = _db_message_counts(paths, [session_id]).get(session_id, {})
    row.update(counts)
    return row


def _shared_session_rows(paths: Any, *, limit: int) -> list[dict[str, Any]]:
    from ..agent.session import SessionStore

    errors: list[Exception] = []
    by_id: dict[str, dict[str, Any]] = {}
    try:
        store = SessionStore(paths.root)
        for row in store.list(limit=max(limit * 2, limit)):
            data = _file_session_asdict(row)
            sid = str(data.get("session_id") or "")
            if sid:
                by_id[sid] = data
    except Exception as exc:
        errors.append(exc)
    try:
        for row in _list_db_sessions(paths, limit=max(limit * 2, limit)):
            sid = str(row.get("session_id") or "")
            if not sid:
                continue
            if sid in by_id:
                by_id[sid] = _merge_session_dict(by_id[sid], row)
            else:
                by_id[sid] = _db_session_asdict(row)
    except Exception as exc:
        errors.append(exc)
    if not by_id and errors:
        raise errors[0]
    rows = list(by_id.values())
    rows.sort(key=_session_updated_ts, reverse=True)
    return rows[:limit]


def _load_shared_session(paths: Any, session_id: str) -> dict[str, Any] | None:
    from ..agent.session import SessionStore

    store = SessionStore(paths.root)
    file_row = store.load(session_id)
    db_row = _get_db_session(paths, session_id)
    if file_row is None and db_row is None:
        return None
    if file_row is None:
        return _db_session_asdict(db_row or {})
    return _merge_session_dict(_file_session_asdict(file_row), db_row)


def _session_title(row: Any) -> str:
    if isinstance(row, Mapping):
        meta = row.get("meta") if isinstance(row.get("meta"), dict) else {}
        title = str(meta.get("title") or row.get("title") or "").strip()
        if title:
            return title
        sid = str(row.get("session_id") or "")
        return sid[:12] or "session"
    meta = getattr(row, "meta", {}) or {}
    title = str(meta.get("title") or "").strip()
    if title:
        return title
    sid = str(getattr(row, "session_id", "") or "")
    return sid[:12] or "session"


def _session_activity(row: Mapping[str, Any]) -> str:
    turn_ids = row.get("turn_ids") if isinstance(row.get("turn_ids"), list) else []
    if turn_ids:
        return f"{len(turn_ids)} turn(s)"
    turn_count = int(row.get("turn_count") or 0)
    if turn_count:
        return f"{turn_count} turn(s)"
    message_count = int(row.get("message_count") or 0)
    if message_count:
        return f"{message_count} message(s)"
    source = str(row.get("source") or "db").strip()
    return source or "db"


def _last_session_message(paths: Any, session_id: str) -> str:
    try:
        from ..db.repositories import AgentSessionRepository
        from ..db.sqlite import connect

        con = connect(paths.db)
        rows = AgentSessionRepository(con).transcript(session_id, limit=100)
        con.close()
        messages = [
            {
                "role": r.get("role"),
                "content": r.get("content"),
            }
            for r in rows
            if r.get("role") in {"user", "assistant"} and r.get("content")
        ]
        if messages:
            last = messages[-1]
            role = str(last.get("role") or "message")
            content = str(last.get("content") or "").strip()
            if len(content) > 1200:
                content = content[:1197].rstrip() + "..."
            return f"Last {role} message:\n{content}"
    except Exception:
        pass

    try:
        from ..agent.session_search import session_transcript

        messages = session_transcript(paths, session_id=session_id, max_pairs=50)
    except Exception:
        messages = []
    if not messages:
        return "(no transcript messages yet)"
    last = messages[-1]
    role = str(last.get("role") or "message")
    content = str(last.get("content") or "").strip()
    if len(content) > 1200:
        content = content[:1197].rstrip() + "..."
    return f"Last {role} message:\n{content}"


def _handle_sessions(spec: CommandSpec, ctx: CommandContext) -> CommandOutcome:
    try:
        rows = _shared_session_rows(ctx.client.config.paths, limit=12)
    except Exception as exc:
        return CommandOutcome(
            handled=True,
            reply_text=f"Cannot list sessions: {type(exc).__name__}: {exc}",
            command=spec.name,
        )
    if not rows:
        return CommandOutcome(
            handled=True,
            reply_text="No saved sessions yet.",
            command=spec.name,
        )
    lines = ["Recent sessions:"]
    for row in rows:
        sid = str(row.get("session_id") or "")
        lines.append(
            f"- {sid} · {_session_title(row)} · {_session_activity(row)}"
        )
    lines.append("")
    lines.append("Switch with `/session <session_id>`.")
    return CommandOutcome(handled=True, reply_text="\n".join(lines), command=spec.name)


def _handle_session(spec: CommandSpec, ctx: CommandContext) -> CommandOutcome:
    parts = (ctx.raw_text or "").strip().split(maxsplit=1)
    target = parts[1].strip() if len(parts) > 1 else ""
    if not target:
        return CommandOutcome(
            handled=True,
            reply_text="Usage: /session <session_id>",
            command=spec.name,
        )
    try:
        state = _load_shared_session(ctx.client.config.paths, target)
    except Exception as exc:
        return CommandOutcome(
            handled=True,
            reply_text=f"Cannot load session: {type(exc).__name__}: {exc}",
            command=spec.name,
        )
    if state is None:
        return CommandOutcome(
            handled=True,
            reply_text=f"No session named `{target}`.",
            command=spec.name,
        )
    active = dict(ctx.state.get("active_sessions") or {}) if isinstance(ctx.state, Mapping) else {}
    active[str(ctx.chat_id)] = target
    ctx.update_state(active_sessions=active)
    reply = (
        f"Switched to `{target}` · {_session_title(state)}\n\n"
        + _last_session_message(ctx.client.config.paths, target)
    )
    return CommandOutcome(handled=True, reply_text=reply, command=spec.name)


def _handle_skills(spec: CommandSpec, ctx: CommandContext) -> CommandOutcome:
    """Plan 02 P0 §3 — ``/skills`` lists every loaded skill + agent action.

    Resolves the live registry through ``ctx.client.skills`` so the
    list reflects post-install state without requiring a restart.
    """

    client = ctx.client
    skills = getattr(client, "skills", None)
    if skills is None:
        return CommandOutcome(handled=True, reply_text="(skill registry unavailable)", command=spec.name)
    rows = skills.list() if hasattr(skills, "list") else []
    if not rows:
        return CommandOutcome(handled=True, reply_text="(no skills registered)", command=spec.name)
    lines = ["Skills available to this gateway:"]
    for row in rows:
        actions = ", ".join(a["name"] for a in (row.get("actions") or []))
        title = row.get("title") or row.get("id")
        lines.append(f"- {row.get('id')} — {title}\n    actions: {actions}")
    lines.append("")
    lines.append("Tip: send `/skill view <id>` for detailed action schemas.")
    return CommandOutcome(handled=True, reply_text="\n".join(lines), command=spec.name)


def _handle_accounts(spec: CommandSpec, ctx: CommandContext) -> CommandOutcome:
    """Plan 2026-04-29 §11 P10 — ``/accounts`` renders the configured accounts roster.

    The output is intentionally compact (one line per account) so it
    survives Telegram's 4096-character limit even on workspaces with
    many accounts. Rich detail (positions, executors, snapshots) lives
    on the dashboard's per-account driver page.
    """

    client = ctx.client
    try:
        from ..trading import accounts as accounts_mod
        from ..trading.account_snapshots import latest_snapshot

        profiles = accounts_mod.load_account_profiles(client.config.paths)
    except Exception as exc:
        return CommandOutcome(
            handled=True,
            reply_text=f"⚠ accounts unavailable: {type(exc).__name__}: {exc}",
            command=spec.name,
        )
    if not profiles:
        return CommandOutcome(
            handled=True,
            reply_text=(
                "No accounts configured yet.\n\n"
                "Add one from the dashboard (Accounts → +Add account) or "
                "ask the agent to walk you through it — secrets you type "
                "into the dashboard's intake form never reach the LLM."
            ),
            command=spec.name,
        )
    parts = (ctx.raw_text or "").strip().split()
    target_id = parts[1] if len(parts) >= 2 else ""
    if target_id:
        profile = profiles.get(target_id)
        if profile is None:
            return CommandOutcome(
                handled=True,
                reply_text=(
                    f"No account named `{target_id}`.\n"
                    "Send `/accounts` for the full list."
                ),
                command=spec.name,
            )
        snap = None
        try:
            snap = latest_snapshot(client.config.paths, profile.id)
        except Exception:
            snap = None
        balance = "—"
        if snap is not None:
            try:
                balance = f"{float(snap.total_usd):,.2f} {profile.base_currency}"
            except Exception:
                balance = "—"
        cred_summary = (
            ", ".join(sorted(profile.credentials.keys())) if profile.credentials
            else "(none)"
        )
        wallet_line = profile.wallet_id or "(none)"
        text = (
            f"Account `{profile.id}` ({profile.mode}/{profile.status})\n"
            f"venue: {profile.venue} · kind: {profile.kind}\n"
            f"wallet: {wallet_line}\n"
            f"base_currency: {profile.base_currency or 'USDT'} · "
            f"balance: {balance}\n"
            f"live_trading_enabled: {profile.live_trading_enabled}\n"
            f"permissions: read={profile.permissions.read_balances} "
            f"place={profile.permissions.place_order} "
            f"cancel={profile.permissions.cancel_order} "
            f"withdraw=False\n"
            f"credentials: {cred_summary}\n"
        )
        return CommandOutcome(handled=True, reply_text=text, command=spec.name)

    lines = [f"Accounts ({len(profiles)})"]
    for profile in profiles.values():
        snap = None
        try:
            snap = latest_snapshot(client.config.paths, profile.id)
        except Exception:
            snap = None
        balance = "—"
        if snap is not None:
            try:
                balance = f"{float(snap.total_usd):,.2f} {profile.base_currency or 'USDT'}"
            except Exception:
                balance = "—"
        lines.append(
            f"- `{profile.id}` · {profile.mode}/{profile.status}"
            f" · {profile.venue}/{profile.kind} · {balance}"
        )
    lines.append("")
    lines.append("Tip: send `/accounts <id>` for credentials/permissions detail.")
    return CommandOutcome(handled=True, reply_text="\n".join(lines), command=spec.name)


def _handle_wallets(spec: CommandSpec, ctx: CommandContext) -> CommandOutcome:
    """Plan 2026-04-29 §11 P10 — ``/wallets`` lists on-chain wallet providers.

    Mirrors ``/accounts`` for the on-chain side: shows every wallet
    provider declared in :mod:`nerya.wallet.registry` plus its install
    state (pip / Node skill / npm) so the operator (or the agent) can
    decide whether a venue needs a one-shot
    ``POST /wallet/install`` call before they can attach an account.
    Sensitive data — signer refs, mnemonics, passphrases — never
    appear here; the gateway only renders provider id, runtime, chain
    family and install state.
    """

    try:
        from ..wallet import registry as wallet_registry
        from ..install.dep_installer import list_node_skills

        providers = wallet_registry.list_providers()
        installed = {row.get("id"): row for row in list_node_skills(ctx.client.config.paths)}
    except Exception as exc:
        return CommandOutcome(
            handled=True,
            reply_text=f"⚠ wallet registry unavailable: {type(exc).__name__}: {exc}",
            command=spec.name,
        )

    if not providers:
        return CommandOutcome(
            handled=True,
            reply_text="No on-chain wallet providers registered.",
            command=spec.name,
        )

    parts = (ctx.raw_text or "").strip().split()
    target_id = parts[1] if len(parts) >= 2 else ""

    if target_id:
        provider = next((p for p in providers if p.get("id") == target_id), None)
        if provider is None:
            return CommandOutcome(
                handled=True,
                reply_text=(
                    f"No wallet provider named `{target_id}`.\n"
                    "Send `/wallets` for the full list."
                ),
                command=spec.name,
            )
        install_cmd = provider.get("install_command") or "(none)"
        alts = provider.get("install_alternatives") or []
        alt_rendered: list[str] = []
        for alt in alts:
            if isinstance(alt, dict):
                lbl = alt.get("label") or alt.get("kind") or "alt"
                cmd = alt.get("command") or ""
                alt_rendered.append(f"  - {lbl}: `{cmd}`")
            else:
                alt_rendered.append(f"  - `{alt}`")
        alt_lines = "\n".join(alt_rendered) if alt_rendered else "  (none)"
        skill = installed.get(target_id)
        skill_line = (
            f"installed: yes ({skill.get('install_path')})"
            if skill else "installed: no"
        )
        caps = provider.get("capabilities") or {}
        chains = caps.get("chains") if isinstance(caps, dict) else None
        chain_line = ", ".join(chains) if chains else "—"
        docs_url = (
            (provider.get("links") or {}).get("docs")
            or provider.get("docs_url")
            or "(none)"
        )
        text = (
            f"Wallet `{provider.get('id')}` ({provider.get('runtime') or 'pip'})\n"
            f"chains: {chain_line}\n"
            f"docs: {docs_url}\n"
            f"install_command: {install_cmd}\n"
            f"alternatives:\n{alt_lines}\n"
            f"{skill_line}"
        )
        return CommandOutcome(handled=True, reply_text=text, command=spec.name)

    lines = [f"Wallet providers ({len(providers)})"]
    for provider in providers:
        marker = "✓" if installed.get(provider.get("id")) else "·"
        caps = provider.get("capabilities") or {}
        chains = caps.get("chains") if isinstance(caps, dict) else None
        chain_summary = ", ".join((chains or [])[:3]) or "—"
        lines.append(
            f"- {marker} `{provider.get('id')}` "
            f"({provider.get('runtime') or 'pip'}) · {chain_summary}"
        )
    lines.append("")
    lines.append("Tip: send `/wallets <id>` for install commands and chain detail.")
    return CommandOutcome(handled=True, reply_text="\n".join(lines), command=spec.name)


def _handle_intake(spec: CommandSpec, ctx: CommandContext) -> CommandOutcome:
    """``/intake`` lists open account-credential intakes the operator owes.

    Operators sometimes start an account-add flow from the dashboard
    or chat and don't immediately fill in the form. This command gives
    them a quick way to see what's still pending plus a deep link to
    finish each one without having to leave the gateway. Plaintext
    secrets always travel through the dashboard intake form, never
    through the chat — the message we render here only contains the
    intake id, target venue, and a follow-up url.
    """

    client = ctx.client
    try:
        from ..trading import account_intake as intake_mod

        intakes = intake_mod.list_intakes(
            client.config.paths, state="open",
        )
    except Exception as exc:
        return CommandOutcome(
            handled=True,
            reply_text=f"⚠ intakes unavailable: {type(exc).__name__}: {exc}",
            command=spec.name,
        )
    if not intakes:
        return CommandOutcome(
            handled=True,
            reply_text="No pending account intakes.",
            command=spec.name,
        )
    base = (ctx.dashboard_url or _DEFAULT_DASHBOARD_URL).rstrip("/")
    lines = [f"Pending intakes ({len(intakes)})"]
    for intake in intakes:
        url = f"{base}/accounts?intake={intake.id}"
        lines.append(
            f"- `{intake.id}` · {intake.venue}/{intake.account_kind} "
            f"· {intake.account_id}\n    open: {url}"
        )
    return CommandOutcome(handled=True, reply_text="\n".join(lines), command=spec.name)


def _handle_skill_subcommand(spec: CommandSpec, ctx: CommandContext) -> CommandOutcome:
    """Plan 02 P0 §3 — ``/skill view <id>`` renders manifest + actions."""

    parts = (ctx.raw_text or "").strip().split()
    sub = parts[1] if len(parts) >= 2 else ""
    target = parts[2] if len(parts) >= 3 else ""
    client = ctx.client
    if sub == "view":
        if not target:
            return CommandOutcome(handled=True, reply_text="Usage: /skill view <skill_id>", command=spec.name)
        skills = getattr(client, "skills", None)
        info = None
        if skills is not None and hasattr(skills, "view"):
            try:
                info = skills.view(target)
            except Exception:
                info = None
        if info is None:
            return CommandOutcome(handled=True, reply_text=f"No skill named `{target}`.", command=spec.name)
        lines = [
            f"Skill `{info.get('id')}` v{info.get('version')}",
            info.get("title", ""),
            "",
            "Permissions: " + ", ".join(info.get("permissions") or []),
            "",
            "Actions:",
        ]
        for action in info.get("actions") or []:
            lines.append(
                f"- {action.get('name')} gate={action.get('approval_gate')}"
                f" risk={action.get('risk_gate')}"
            )
            description = (action.get("description") or "").strip()
            if description:
                first = description.splitlines()[0][:160]
                if first:
                    lines.append(f"    {first}")
        return CommandOutcome(handled=True, reply_text="\n".join(lines), command=spec.name)
    if sub in {"doctor", "check"}:
        skills = getattr(client, "skills", None)
        if skills is None or not hasattr(skills, "doctor"):
            return CommandOutcome(handled=True, reply_text="(doctor unavailable)", command=spec.name)
        report = skills.doctor()
        if not report.get("problems"):
            return CommandOutcome(handled=True, reply_text=f"All {len(report.get('ok') or [])} skills are healthy.", command=spec.name)
        body = ["Skill diagnostics found problems:"]
        for prob in report.get("problems") or []:
            body.append(f"- {prob.get('id')}: {', '.join(prob.get('issues') or [])}")
        return CommandOutcome(handled=True, reply_text="\n".join(body), command=spec.name)
    return CommandOutcome(
        handled=True,
        reply_text=(
            "Available subcommands: /skill view <id>, /skill doctor"
        ),
        command=spec.name,
    )


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------

@dataclass
class _RegistryEntry:
    spec: CommandSpec
    handler: CommandHandler


class GatewayCommandRegistry:
    """In-process registry. Tests can build their own; route layer reuses
    ``DEFAULT_REGISTRY`` so all adapters share one source of truth."""

    def __init__(self) -> None:
        self._entries: list[_RegistryEntry] = []

    # registration ---------------------------------------------------------

    def register(self, spec: CommandSpec, handler: CommandHandler) -> None:
        self._entries.append(_RegistryEntry(spec=spec, handler=handler))

    def extend(self, items: Iterable[tuple[CommandSpec, CommandHandler]]) -> None:
        for spec, handler in items:
            self.register(spec, handler)

    # lookup ---------------------------------------------------------------

    def specs(self, *, platform: Optional[str] = None) -> list[CommandSpec]:
        out: list[CommandSpec] = []
        for entry in self._entries:
            if platform and entry.spec.platforms and platform not in entry.spec.platforms:
                continue
            out.append(entry.spec)
        return out

    def menu(self, *, platform: Optional[str] = None) -> list[dict[str, str]]:
        return [
            {"command": spec.menu_name, "description": spec.description}
            for spec in self.specs(platform=platform)
            if spec.show_in_menu
        ]

    def lookup(self, name: str, *, platform: Optional[str] = None) -> Optional[_RegistryEntry]:
        key = (name or "").strip().lower()
        if not key:
            return None
        if not key.startswith("/"):
            key = "/" + key
        # Two-pass lookup: primary names first, aliases second. Otherwise a
        # command that lists ``/help`` as an alias of ``/start`` would
        # shadow the actual ``/help`` command registered later.
        for entry in self._entries:
            if platform and entry.spec.platforms and platform not in entry.spec.platforms:
                continue
            if key == entry.spec.name.lower():
                return entry
        for entry in self._entries:
            if platform and entry.spec.platforms and platform not in entry.spec.platforms:
                continue
            if key in {a.lower() for a in entry.spec.aliases}:
                return entry
        return None

    # rendering ------------------------------------------------------------

    def help_text(self, *, platform: Optional[str] = None) -> str:
        lines = ["Nerya gateway is online.", "", "Commands:"]
        for spec in self.specs(platform=platform):
            if not spec.show_in_menu:
                continue
            lines.append(f"{spec.name} — {spec.description}")
        lines.extend([
            "",
            "What you will see during a turn:",
            "1. 🧭 route/planner decision",
            "2. 🧠 model decision",
            "3. ⚙️ skill/tool execution",
            "4. 👁 observations and re-plan when needed",
            "5. ✅ final reply",
        ])
        return "\n".join(lines)

    # dispatch -------------------------------------------------------------

    def handle(self, text: str, ctx: CommandContext) -> CommandOutcome:
        clean = (text or "").strip()
        if not clean.startswith("/"):
            return CommandOutcome(handled=False)
        head = clean.split()[0]
        entry = self.lookup(head, platform=ctx.platform)
        if entry is None:
            return CommandOutcome(handled=False)
        return entry.handler(entry.spec, ctx)


# ---------------------------------------------------------------------------
# Default registry (Hermes baseline)
# ---------------------------------------------------------------------------

BUILTIN_COMMANDS: tuple[tuple[CommandSpec, CommandHandler], ...] = (
    (CommandSpec(name="/start", description="Start Nerya chat", aliases=("/help",)), _handle_help),
    (CommandSpec(name="/help", description="Show help and workflow"), _handle_help),
    (CommandSpec(name="/menu", description="Show the command menu", aliases=("/commands",)), _handle_help),
    (CommandSpec(name="/new", description="Start a fresh session"), _handle_new),
    (CommandSpec(name="/sessions", description="List recent shared sessions"), _handle_sessions),
    (CommandSpec(name="/session", description="Switch to a shared session — usage: /session <id>"), _handle_session),
    (CommandSpec(name="/status", description="Check local runtime status"), _handle_status),
    (CommandSpec(name="/trace", description="Explain the last agent turn"), _handle_trace),
    (CommandSpec(name="/accounts", description="List configured trading accounts — usage: /accounts [<id>]"), _handle_accounts),
    (CommandSpec(name="/wallets", description="List on-chain wallet providers — usage: /wallets [<id>]"), _handle_wallets),
    (CommandSpec(name="/intake", description="List pending account-credential intakes"), _handle_intake),
    (CommandSpec(name="/skills", description="List skills and agent actions"), _handle_skills),
    (CommandSpec(name="/skill", description="Skill detail / doctor — usage: /skill view <id>"), _handle_skill_subcommand),
)


def _build_default_registry() -> GatewayCommandRegistry:
    registry = GatewayCommandRegistry()
    registry.extend(BUILTIN_COMMANDS)
    return registry


DEFAULT_REGISTRY: GatewayCommandRegistry = _build_default_registry()


def menu_commands(*, platform: Optional[str] = None) -> list[dict[str, str]]:
    """Public entry point for adapters that need the Hermes-aligned menu."""

    return DEFAULT_REGISTRY.menu(platform=platform)


def help_text(*, platform: Optional[str] = None) -> str:
    return DEFAULT_REGISTRY.help_text(platform=platform)
