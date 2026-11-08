"""Native file ops — read_file / list_dir / edit_file / write_file / apply_patch.

Lifted out of :mod:`nerya.skills.builtin.operator_skill.scripts.handlers`
into a *first-class* tool layer so the workspace-native agent loop can
drive them directly, without the SKILL.md-action -> ToolRunner detour.

Invariants kept from the operator-skill version:

* path safety via :func:`resolve_workspace_path`;
* :class:`FileStateCache` integration (fresh-read before edit);
* unified diff in the result so the dashboard can render edits;
* large files are truncated with explicit ``truncated=True`` markers.

Drop / change vs operator skill:

* No more dispatch through :class:`ToolRunner`. Each handler returns a
  :class:`ToolResult` directly, including provider-shaped error
  results for stale-read / not-found / multi-match conditions.
* Diffs come back as a :class:`ToolResultPart` of type ``"diff"``
  rather than a free-form text blob, so the dashboard can render them
  side-by-side without reparsing.
* ``edit_file`` requires *fresh read*. The legacy operator handler
  enforced the same; we surface it on the descriptor as
  ``requires_fresh_read=True`` and have the executor enforce it
  before calling the handler.
"""

from __future__ import annotations

import difflib
import fnmatch
import hashlib
import json
import os
import sqlite3
import stat
from contextlib import closing
from pathlib import Path
from typing import Any, Optional

from ...agent.file_state import (
    FileStateCache,
    StaleFileReadError,
    compute_file_hash,
)
from ..tool_errors import schema_validation_result
from ..types import (
    ContextModifier,
    RiskLevel,
    ToolCall,
    ToolError,
    ToolErrorKind,
    ToolResult,
    ToolResultPart,
)
from .conversation_files import conversation_files_dir, place_new_conversation_file
from .paths import (
    WorkspaceEscapeError,
    resolve_workspace_path,
    to_workspace_relative,
)


_DEFAULT_FILE_BYTES = 64 * 1024
_HARD_FILE_BYTES = 1 * 1024 * 1024
_DEFAULT_LIST_ENTRIES = 200
_HARD_LIST_ENTRIES = 1000
_BUILTIN_SKILL_ROOT = Path(__file__).resolve().parents[2] / "skills" / "builtin"


_SENSITIVE_MUTATION_GLOBS = (
    ".env",
    ".env.*",
    "nerya.yml",
    "nerya.yaml",
    "news_feeds.yml",
    "news_feeds.yaml",
    "messages/channels.yml",
    "messages/channels.yaml",
    "accounts/*",
    "exchanges.yml",
    "exchanges.yaml",
    "triggers/schedules.yml",
    "vault/*",
    "vault/**/*",
    "secrets/*",
    "secrets/**/*",
    "strategies/*/strategy.yml",
    "strategies/*/strategy.yaml",
    "strategies/*/limits.yml",
    "strategies/*/limits.yaml",
    "skills/enabled.yml",
    "skills/enabled.yaml",
    "ui/workspace.yml",
    "workspace/ui.yml",
    "providers/*",
    "providers/**/*",
)


_PROPOSAL_ONLY_CONFIG_FILES = {
    "nerya.yml",
    "nerya.yaml",
    "agents.yml",
    "workspace.yml",
    "news_feeds.yml",
    "news_feeds.yaml",
    "messages/channels.yml",
    "messages/channels.yaml",
    "policies/planner.yml",
    "policies/tier_policy.yml",
    "ui/workspace.yml",
    "workspace/ui.yml",
}


def _normalise_mutation_path(value: Any) -> str:
    raw = str(value or "").strip().replace("\\", "/")
    while raw.startswith("./"):
        raw = raw[2:]
    return raw.lower()


def is_sensitive_mutation_path(path: Any) -> bool:
    """Return true for files whose mutation changes runtime safety posture."""

    p = _normalise_mutation_path(path)
    if not p:
        return False
    return any(fnmatch.fnmatchcase(p, pattern) for pattern in _SENSITIVE_MUTATION_GLOBS)


def classify_file_mutation_risk(arguments: dict[str, Any]) -> RiskLevel:
    """Escalate safety-critical config writes while keeping code edits fluid."""

    if bool((arguments or {}).get("allow_outside_conversation")):
        return RiskLevel.DANGEROUS
    if is_sensitive_mutation_path((arguments or {}).get("path")):
        return RiskLevel.DANGEROUS
    return RiskLevel.WRITE


def _proposal_required_tools(path: str) -> list[str]:
    p = _normalise_mutation_path(path)
    if not p or p.startswith("evolution/proposals/"):
        return []
    if p in {"skills/enabled.yml", "skills/enabled.yaml"}:
        return ["skill_manage"]
    if p in _PROPOSAL_ONLY_CONFIG_FILES:
        return ["evolve_core_config_patch"]
    if fnmatch.fnmatchcase(p, "strategies/*") or fnmatch.fnmatchcase(p, "strategies/**/*"):
        return ["strategy_draft_proposal", "strategy_tuning_generate"]
    return []


def _proposal_required_result(
    call: ToolCall,
    *,
    path: str,
    tools: list[str],
) -> ToolResult:
    primary = tools[0] if tools else "proposal tool"
    if primary == "skill_manage":
        return ToolResult.from_error(
            tool_use_id=call.id,
            name=call.name,
            error=ToolError(
                kind=ToolErrorKind.PERMISSION_DENIED,
                message=(
                    f"direct mutation of {path!r} is managed by skill_manage. "
                    "Do not edit the Skill enablement file manually; call "
                    "skill_manage with enable/disable/delete instead. No proposal "
                    "or Action Inbox step is required."
                ),
                retryable=False,
                recovery_hint={
                    "next_required_action": {
                        "tool": "skill_manage",
                        "path": path,
                        "reason": "managed_skill_mutation",
                    }
                },
            ),
        )
    return ToolResult.from_error(
        tool_use_id=call.id,
        name=call.name,
        error=ToolError(
            kind=ToolErrorKind.PERMISSION_DENIED,
            message=(
                f"direct mutation of {path!r} is proposal-only. "
                "Do not mutate the live workspace file with edit_file/write_file; "
                f"call {primary} and stage the change under evolution/proposals/ "
                "for operator review."
            ),
            retryable=False,
            recovery_hint={
                "next_required_action": {
                    "tool": primary,
                    "path": path,
                    "reason": "proposal_only_mutation",
                }
            },
        ),
    )


def _read_text(path: Path) -> tuple[str, bool, int]:
    """Read text content, falling back to errors='replace' on bad UTF-8."""

    raw = path.read_bytes()
    try:
        text = raw.decode("utf-8")
        was_replaced = False
    except UnicodeDecodeError:
        text = raw.decode("utf-8", errors="replace")
        was_replaced = True
    return text, was_replaced, len(raw)


def _proposal_task_commands(config, con):
    """Resolve host-owned task identity without semantic scope admission."""
    from ...agent.assistant_intent import assistant_request_digest
    from ...agent.continuation_binding import trusted_continuation_source
    from ...task_delivery import delivery_command_chain

    chain = delivery_command_chain(config.paths, con, config.get('runtime.command_id'))
    current = chain[0]
    if (current['state'] != 'running' or current['kind'] == 'guide'
            or json.loads(current['context_json']).get('stop_requested')):
        raise ValueError('inactive command')
    for field, key in (('actor_id', 'financial_actor_id'), ('session_id', 'command_session_id'),
                       ('turn_id', 'command_turn_id')):
        if config.get('runtime.' + key) not in (None, '', current[field]):
            raise ValueError('runtime identity mismatch')
    session, actor = current['session_id'], current['actor_id']
    if con.execute('SELECT 1 FROM agent_deleted_sessions WHERE session_id=?', (session,)).fetchone():
        raise ValueError('deleted task session')
    work = None
    if con.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='assistant_work'").fetchone():
        work = con.execute('SELECT * FROM assistant_work WHERE task_session_id=?', (session,)).fetchone()
    rows = con.execute(
        "SELECT * FROM agent_commands WHERE session_id=? AND kind!='guide' "
        'AND (created_at<? OR (created_at=? AND position<=?)) ORDER BY created_at,position',
        (session, current['created_at'], current['created_at'], current['position'])).fetchall()
    if work:
        if (work['actor_id'] != actor or not rows or rows[0]['command_id'] != work['command_id']
                or json.loads(work['request_json']).get('payload') != json.loads(rows[0]['request_json']).get('payload')
                or con.execute('SELECT 1 FROM agent_deleted_sessions WHERE session_id=?',
                               (work['parent_session_id'],)).fetchone()):
            raise ValueError('task ownership mismatch')
    verified = set()
    for row in rows:
        request = json.loads(row['request_json'])
        payload = request.get('payload') or {}
        if row['actor_id'] != actor or request.get('_auth_actor_id') != actor:
            raise ValueError('command actor mismatch')
        if row['command_id'] == current['command_id']:
            scopes = request.get('_auth_scopes') or [request.get('_auth_scope')]
            if (not isinstance(scopes, list)
                    or not set(s for s in scopes if isinstance(s, str)) & {'api:all', 'execute:strategy'}):
                raise ValueError('command authorization missing')
        source = trusted_continuation_source(config, row, connection=con)
        if source is not None:
            if source['command_id'] not in verified:
                raise ValueError('unverified continuation source')
        elif work and request.get('source') != 'user_chat':
            binding = request.get('assistant_request_binding')
            nested = payload.get('assistant_request_binding')
            if row['command_id'] == work['command_id']:
                recorded = json.loads(work['request_json']).get('assistant_request_binding')
                if not isinstance(recorded, dict) or nested != recorded or binding not in (None, recorded):
                    raise ValueError('unbound original task')
                binding = recorded
            if not isinstance(binding, dict) or nested != binding:
                raise ValueError('unbound parent command')
            parent = con.execute('SELECT * FROM agent_commands WHERE command_id=?',
                                 (binding.get('command_id'),)).fetchone()
            if (not parent or any(parent[key] != binding.get(key)
                                  for key in ('command_id', 'session_id', 'turn_id', 'actor_id'))
                    or parent['actor_id'] != actor or parent['session_id'] != work['parent_session_id']):
                raise ValueError('parent identity mismatch')
            original = json.loads(parent['request_json'])
            if (original.get('_auth_actor_id') != actor
                    or assistant_request_digest(original) != binding.get('input_digest')
                    or (original.get('payload') or {}).get('text') != payload.get('text')):
                raise ValueError('parent input mismatch')
            if row['command_id'] != work['command_id']:
                message = con.execute('SELECT * FROM assistant_task_messages WHERE command_id=?',
                                      (row['command_id'],)).fetchone()
                if (not message or message['kind'] != 'follow_up'
                        or message['target_task_id'] != work['task_id']
                        or message['target_session_id'] != session
                        or message['parent_session_id'] != work['parent_session_id']
                        or message['sender_session_id'] != parent['session_id']
                        or message['message'] != payload.get('text')):
                    raise ValueError('task followup mismatch')
        elif (row['kind'] != 'send' or request.get('kind', 'user.chat') != 'user.chat'
                or request.get('session_id') != session
                or request.get('source') in {'assistant_task', 'assistant_task_message', 'goal_continue', 'approval_continue'}
                or 'assistant_request_binding' in request or 'assistant_request_binding' in payload
                or 'task_message' in payload):
            raise ValueError('unbound task command')
        verified.add(row['command_id'])
    return current, rows


def _proposal_task_guard(call, *, path, root, config, task_session_id, task_actor_id):
    """Authorize staged strategy writes from exact host draft ownership receipts.

    Proposal files and delivery artifact declarations are model-writable;
    neither is an ownership record. Offline operators have no task context.
    """
    parts = Path(path).parts
    if (len(parts) < 6 or parts[:2] != ('evolution', 'proposals')
            or parts[3:5] != ('after', 'strategies')):
        return None
    command_id = config.get('runtime.command_id') if config is not None else None
    if not command_id:
        return None
    pid, sid = parts[2], parts[5]
    diagnostic = 'ownership_unverified'
    try:
        from ..verification import read_verification_receipt

        if not command_id or config.paths.root.resolve() != root.resolve():
            raise ValueError('missing command context')
        with closing(sqlite3.connect(config.paths.db.as_uri() + '?mode=ro',
                                     uri=True, timeout=4.0)) as con:
            con.row_factory = sqlite3.Row
            con.execute('PRAGMA query_only=ON')
            con.execute('PRAGMA busy_timeout=4000')
            con.execute('BEGIN')
            current, commands = _proposal_task_commands(config, con)
            session = current['session_id']
            if task_session_id and task_session_id != session:
                raise ValueError('session mismatch')
            # Kernel deps use memory aliases; durable auth above stays exact.
            from ...memory.scope import memory_actor
            if task_actor_id and memory_actor(task_actor_id) != memory_actor(current['actor_id']):
                raise ValueError('actor mismatch')
            row = con.execute('SELECT state,revision FROM agent_commands WHERE command_id=?',
                              (command_id,)).fetchone()
            if row is None or row['state'] != 'running' or row['revision'] != current['revision']:
                diagnostic = 'command_context_changed'
                raise ValueError('command changed during reconciliation')
            diagnostic = 'producer_receipt_missing'
            for producer in commands:
                cid = producer['command_id']
                if cid != command_id and producer['state'] not in {
                        'succeeded', 'failed', 'interrupted', 'blocked', 'awaiting_input', 'awaiting_approval'}:
                    continue
                events = con.execute(
                    "SELECT payload_json FROM agent_command_events WHERE command_id=? "
                    "AND CASE WHEN json_valid(payload_json) THEN "
                    "json_extract(payload_json,'$.kind')='tool.complete' "
                    "AND json_extract(payload_json,'$.action')='strategy_draft_proposal' "
                    "AND coalesce(json_extract(payload_json,'$.skill_id'),'native')='native' "
                    "ELSE 0 END ORDER BY id LIMIT 1001", (cid,)).fetchall()
                if len(events) > 1000:
                    raise ValueError('unbounded producer history')
                for saved in events:
                    event = json.loads(saved['payload_json'])
                    if (not isinstance(event, dict) or event.get('kind') != 'tool.complete'
                            or event.get('session_id') != session
                            or event.get('turn_id') != producer['turn_id']
                            or event.get('action') != 'strategy_draft_proposal'
                            or event.get('skill_id', 'native') != 'native'
                            or event.get('ok') is not True or event.get('error')):
                        continue
                    receipt = read_verification_receipt(event.get('verification'))
                    if (receipt and receipt['artifact_id'] == f'strategy:{sid}:proposal:{pid}'
                            and receipt['revision'] != 'unbound'
                            and any(check['check_id'] == 'source_binding' and check['status'] == 'passed'
                                    and check['evidence_ref'] == '/'.join(parts[:6])
                                    for check in receipt['checks'])):
                        return None
    except sqlite3.Error:
        diagnostic = 'receipt_snapshot_unavailable'
    except (OSError, ValueError, TypeError, KeyError):
        pass
    return ToolResult.from_error(tool_use_id=call.id, name=call.name, error=ToolError(
        kind=ToolErrorKind.PERMISSION_DENIED,
        message=('proposal_task_binding_unverified: ' + diagnostic + '. Reconcile the existing candidate '
                 'with its owning task before writing; do not create a replacement draft. '
                 'Delivery updates and switching file tools cannot grant ownership.'),
        retryable=False,
        detail={'reason': 'proposal_task_binding_unverified', 'diagnostic_code': diagnostic},
        recovery_hint={'reason': 'proposal_task_binding_unverified',
                       'diagnostic_code': diagnostic,
                       'action': 'reconcile_existing_candidate', 'proposal_id': pid,
                       'strategy_id': sid, 'side_effects': 'none'}))


def _short_path(path: Path, root: Path) -> str:
    return to_workspace_relative(path, root)


def _builtin_skill_read_hint(raw_path: str) -> dict[str, Any]:
    """Suggest Skill for a real bundled asset; never grant/read the requested file."""
    from ...skills.registry import load_entry

    try:
        path = Path(raw_path)
        builtin = _BUILTIN_SKILL_ROOT
        if not path.is_absolute() or ".." in path.parts or builtin.is_symlink():
            return {}
        relative = path.relative_to(builtin)
        current = builtin
        for part in relative.parts:
            current /= part
            if current.is_symlink():
                return {}
        resolved = path.resolve(strict=True)
        if not resolved.is_relative_to(builtin.resolve(strict=True)) or not resolved.is_file():
            return {}
        directory = path.parent
        while directory != builtin:
            playbook = directory / "SKILL.md"
            if playbook.is_symlink():
                return {}
            if playbook.is_file():
                if playbook.stat().st_size > _HARD_FILE_BYTES:
                    return {}
                entry = load_entry(playbook, source="builtin")
                if entry is None:
                    return {}
                return {"next_required_action": {
                    "tool": "Skill",
                    "arguments": {"skill": entry.manifest.id,
                                  "file": path.relative_to(directory).as_posix()},
                    "reason": "builtin_skill_asset_requires_skill",
                }}
            directory = directory.parent
    except (OSError, RuntimeError, ValueError):
        pass
    return {}


# ---------------------------------------------------------------------------
# read_file
# ---------------------------------------------------------------------------


def read_file_handler(
    call: ToolCall,
    *,
    root: Path,
    file_state: Optional[FileStateCache],
) -> ToolResult:
    """Read a workspace file. Updates the FileStateCache for fresh-read."""

    args = call.arguments or {}
    raw_path = args.get("path")
    offset = int(args.get("offset", 0) or 0)
    limit = args.get("limit")
    max_bytes = int(args.get("max_bytes") or _DEFAULT_FILE_BYTES)
    max_bytes = max(1024, min(max_bytes, _HARD_FILE_BYTES))

    if not raw_path:
        return schema_validation_result(call, "read_file requires a 'path' argument")

    try:
        p = resolve_workspace_path(str(raw_path), root=root, must_exist=True)
    except WorkspaceEscapeError as exc:
        return ToolResult.from_error(
            tool_use_id=call.id,
            name=call.name,
            error=ToolError(
                kind=ToolErrorKind.PERMISSION_DENIED,
                message=str(exc),
                detail={"path": str(raw_path)},
                recovery_hint=_builtin_skill_read_hint(str(raw_path)),
            ),
        )
    except FileNotFoundError as exc:
        return ToolResult.from_error(
            tool_use_id=call.id,
            name=call.name,
            error=ToolError(
                kind=ToolErrorKind.NOT_FOUND,
                message=str(exc),
                detail={"path": str(raw_path)},
                recovery_hint={"action": "list_dir", "path": str(Path(raw_path).parent or '.')},
            ),
        )

    if p.is_dir():
        return schema_validation_result(
            call,
            f"{_short_path(p, root)} is a directory; use list_dir",
            recovery_hint={"action": "list_dir", "path": _short_path(p, root)},
        )

    try:
        text, replaced, raw_size = _read_text(p)
    except OSError as exc:
        return ToolResult.from_error(
            tool_use_id=call.id,
            name=call.name,
            error=ToolError(
                kind=ToolErrorKind.EXECUTION_ERROR,
                message=f"failed to read {_short_path(p, root)}: {exc}",
            ),
        )

    lines = text.splitlines()
    total_lines = len(lines)
    if offset < 0:
        offset = max(0, total_lines + offset)
    if limit is None:
        end = total_lines
    else:
        try:
            limit_int = int(limit)
        except Exception:
            limit_int = total_lines
        end = min(total_lines, offset + max(0, limit_int))
    sliced = lines[offset:end]
    truncated_lines = end < total_lines or offset > 0

    rendered = "\n".join(sliced)
    truncated_bytes = False
    if len(rendered.encode("utf-8")) > max_bytes:
        rendered = rendered.encode("utf-8")[:max_bytes].decode("utf-8", errors="ignore")
        truncated_bytes = True

    if file_state is not None:
        file_state.record_read(
            p,
            content=text,
            bytes_seen=raw_size,
            line_count=total_lines,
            truncated=truncated_lines or truncated_bytes,
        )

    payload: dict[str, Any] = {
        "path": _short_path(p, root),
        "absolute_path": str(p),
        "content": rendered,
        "offset": offset,
        "limit": (end - offset),
        "total_lines": total_lines,
        "bytes": raw_size,
        "truncated_lines": truncated_lines,
        "truncated_bytes": truncated_bytes,
        "encoding_replaced": replaced,
        "content_hash": compute_file_hash(text),
    }

    res = ToolResult(
        tool_use_id=call.id,
        name=call.name,
        content=[
            ToolResultPart.text_part(
                f"# {payload['path']} ({total_lines} lines, {raw_size} bytes)\n\n"
                + rendered
            ),
            ToolResultPart.json_part(
                {k: v for k, v in payload.items() if k != "content"}
            ),
        ],
        metadata={"path": payload["path"], "bytes": raw_size},
        context_modifiers=[
            ContextModifier(kind="file_read", path=str(p), payload={"hash": payload["content_hash"]})
        ],
    )
    return res


# ---------------------------------------------------------------------------
# list_dir
# ---------------------------------------------------------------------------


def list_dir_handler(
    call: ToolCall, *, root: Path, session_id: str | None = None,
) -> ToolResult:
    args = call.arguments or {}
    raw_path = args.get("path") or "."
    show_hidden = bool(args.get("show_hidden", False))
    limit = int(args.get("limit") or _DEFAULT_LIST_ENTRIES)
    limit = max(1, min(limit, _HARD_LIST_ENTRIES))
    pattern = args.get("pattern")

    try:
        p = resolve_workspace_path(str(raw_path), root=root, default=".")
    except WorkspaceEscapeError as exc:
        return ToolResult.from_error(
            tool_use_id=call.id,
            name=call.name,
            error=ToolError(kind=ToolErrorKind.PERMISSION_DENIED, message=str(exc)),
        )

    # Conversation output directories are lazy: a panel proposal need not write
    # any free-form files. Listing the current, not-yet-created root is empty.
    empty_conversation = (
        not p.exists() and not p.is_symlink() and session_id is not None
        and p == conversation_files_dir(root, session_id)
    )
    if not p.exists() and not empty_conversation:
        return ToolResult.from_error(
            tool_use_id=call.id,
            name=call.name,
            error=ToolError(
                kind=ToolErrorKind.NOT_FOUND,
                message=f"directory not found: {_short_path(p, root)}",
            ),
        )
    if not empty_conversation and not p.is_dir():
        return schema_validation_result(
            call, f"{_short_path(p, root)} is not a directory; use read_file",
        )

    items: list[dict[str, Any]] = []
    truncated = False
    try:
        entries = [] if empty_conversation else sorted(
            p.iterdir(), key=lambda x: (x.is_file(), x.name.lower()),
        )
    except OSError as exc:
        return ToolResult.from_error(
            tool_use_id=call.id,
            name=call.name,
            error=ToolError(kind=ToolErrorKind.EXECUTION_ERROR, message=str(exc)),
        )
    for entry in entries:
        if not show_hidden and entry.name.startswith("."):
            continue
        if pattern and not fnmatch.fnmatch(entry.name, str(pattern)):
            continue
        if len(items) >= limit:
            truncated = True
            break
        try:
            stat = entry.stat()
        except OSError:
            continue
        items.append(
            {
                "name": entry.name,
                "path": _short_path(entry, root),
                "type": "dir" if entry.is_dir() else "file",
                "bytes": stat.st_size if entry.is_file() else 0,
                "mtime": stat.st_mtime,
            }
        )

    summary_lines = [f"# {_short_path(p, root)} ({len(items)} entries)"]
    for it in items[:60]:
        suffix = "/" if it["type"] == "dir" else ""
        summary_lines.append(f"- {it['name']}{suffix}")
    if truncated:
        summary_lines.append(f"... truncated at {limit}")

    return ToolResult(
        tool_use_id=call.id,
        name=call.name,
        content=[
            ToolResultPart.text_part("\n".join(summary_lines)),
            ToolResultPart.json_part(
                {
                    "path": _short_path(p, root),
                    "items": items,
                    "truncated": truncated,
                    "count": len(items),
                }
            ),
        ],
        metadata={"path": _short_path(p, root), "count": len(items)},
    )


# ---------------------------------------------------------------------------
# edit_file (single-block string replace)
# ---------------------------------------------------------------------------


def _make_diff(before: str, after: str, path: str) -> str:
    return "".join(
        difflib.unified_diff(
            before.splitlines(keepends=True),
            after.splitlines(keepends=True),
            fromfile=f"a/{path}",
            tofile=f"b/{path}",
            n=3,
        )
    )


def edit_file_handler(
    call: ToolCall,
    *,
    root: Path,
    file_state: Optional[FileStateCache],
    config: Any = None,
    task_session_id: Optional[str] = None,
    task_actor_id: Optional[str] = None,
) -> ToolResult:
    args = call.arguments or {}
    raw_path = args.get("path")
    old_string = args.get("old_string")
    new_string = args.get("new_string")
    replace_all = bool(args.get("replace_all", False))

    if not raw_path or old_string is None or new_string is None:
        return schema_validation_result(
            call, "edit_file requires path, old_string, new_string",
        )

    try:
        p = resolve_workspace_path(str(raw_path), root=root, must_exist=True)
    except WorkspaceEscapeError as exc:
        return ToolResult.from_error(
            tool_use_id=call.id,
            name=call.name,
            error=ToolError(kind=ToolErrorKind.PERMISSION_DENIED, message=str(exc)),
        )
    except FileNotFoundError as exc:
        return ToolResult.from_error(
            tool_use_id=call.id,
            name=call.name,
            error=ToolError(
                kind=ToolErrorKind.NOT_FOUND,
                message=str(exc),
                recovery_hint={"action": "write_file", "path": str(raw_path)},
            ),
        )

    if p.is_dir():
        return schema_validation_result(call, "cannot edit a directory")

    rel_path = _short_path(p, root)
    denied = _proposal_task_guard(call, path=rel_path, root=root, config=config,
                                  task_session_id=task_session_id, task_actor_id=task_actor_id)
    if denied is not None:
        return denied
    proposal_tools = _proposal_required_tools(rel_path)
    if proposal_tools:
        return _proposal_required_result(
            call,
            path=rel_path,
            tools=proposal_tools,
        )

    try:
        before, _, _ = _read_text(p)
    except OSError as exc:
        return ToolResult.from_error(
            tool_use_id=call.id,
            name=call.name,
            error=ToolError(kind=ToolErrorKind.EXECUTION_ERROR, message=str(exc)),
        )

    if file_state is not None:
        try:
            file_state.assert_fresh_for_edit(p, on_disk=before, require_read=True)
        except StaleFileReadError as exc:
            return ToolResult.from_error(
                tool_use_id=call.id,
                name=call.name,
                error=ToolError(
                    kind=ToolErrorKind.STALE_FILE,
                    message=str(exc),
                    detail=exc.as_dict(),
                    retryable=True,
                    recovery_hint={
                        "action": "read_file",
                        "path": _short_path(p, root),
                        "next_required_action": {
                            "tool": "read_file",
                            "arguments": {"path": _short_path(p, root)},
                        },
                    },
                ),
            )

    if old_string == new_string:
        return schema_validation_result(call, "old_string equals new_string; no-op edit")

    # Tool transcripts render lines without CRLF. Match their LF snippets while
    # retaining an existing file's newline convention (never rewrite mixed EOLs).
    if "\r\n" in before and "\n" not in before.replace("\r\n", ""):
        old_string = old_string.replace("\r\n", "\n").replace("\n", "\r\n")
        new_string = new_string.replace("\r\n", "\n").replace("\n", "\r\n")
    elif "\r\n" not in before:
        old_string = old_string.replace("\r\n", "\n")
        new_string = new_string.replace("\r\n", "\n")

    occurrences = before.count(old_string)
    if occurrences == 0:
        return ToolResult.from_error(
            tool_use_id=call.id,
            name=call.name,
            error=ToolError(
                kind=ToolErrorKind.NOT_FOUND,
                message="old_string not found in file",
                detail={"path": _short_path(p, root)},
                recovery_hint={
                    "advice": (
                        "Re-read the file and provide an old_string that matches "
                        "the current content exactly (whitespace included)."
                    )
                },
            ),
        )
    if occurrences > 1 and not replace_all:
        return ToolResult.from_error(
            tool_use_id=call.id,
            name=call.name,
            error=ToolError(
                kind=ToolErrorKind.DIFF_CONFLICT,
                message=(
                    f"old_string appears {occurrences} times; provide more context "
                    "or set replace_all=true"
                ),
                detail={"occurrences": occurrences},
            ),
        )

    if replace_all:
        after = before.replace(old_string, new_string)
    else:
        after = before.replace(old_string, new_string, 1)

    try:
        p.write_bytes(after.encode("utf-8"))
    except OSError as exc:
        return ToolResult.from_error(
            tool_use_id=call.id,
            name=call.name,
            error=ToolError(
                kind=ToolErrorKind.EXECUTION_ERROR,
                message=f"write failed: {exc}",
            ),
        )

    if file_state is not None:
        file_state.record_write(p, new_content=after, line_count=after.count("\n") + 1)

    diff = _make_diff(before, after, _short_path(p, root))
    return ToolResult(
        tool_use_id=call.id,
        name=call.name,
        content=[
            ToolResultPart.diff_part(diff=diff, path=_short_path(p, root)),
            ToolResultPart.json_part(
                {
                    "path": _short_path(p, root),
                    "occurrences_replaced": occurrences if replace_all else 1,
                    "bytes_after": len(after.encode("utf-8")),
                    "lines_after": after.count("\n") + 1,
                    "content_hash": compute_file_hash(after),
                }
            ),
        ],
        metadata={"path": _short_path(p, root), "kind": "edit"},
        context_modifiers=[
            ContextModifier(kind="file_mutate", path=str(p), payload={"diff_lines": diff.count("\n")})
        ],
    )


# ---------------------------------------------------------------------------
# write_file (full replacement)
# ---------------------------------------------------------------------------


def _skill_template_source(call, source, skill_index):
    """Read only a current catalog's bounded, bundled Python template."""
    from .skill import skill_access_error, skill_asset_access_error

    if (not isinstance(source, dict)
            or set(source) != {"skill", "file", "expected_revision"}
            or any(not isinstance(value, str) or not value for value in source.values())):
        return schema_validation_result(call, "source_asset requires skill, file and expected_revision strings")
    sid, relative, expected = source["skill"], source["file"], source["expected_revision"]
    if len(expected) != 64 or any(c not in "0123456789abcdef" for c in expected):
        return schema_validation_result(call, "expected_revision must copy the complete 64-character lowercase SHA-256 literal from the template Skill read response's TOP-LEVEL revision, not skill.revision or catalog_revision")
    denied = skill_access_error(call, sid)
    if denied is not None:
        return denied
    record = skill_index.get(sid) if skill_index is not None else None
    if record is None:
        return ToolResult.from_error(tool_use_id=call.id, name=call.name, error=ToolError(
            kind=ToolErrorKind.NOT_FOUND, message="source Skill is not in the active catalog"))
    try:
        asset = Path(relative)
        playbook = Path(record.path)
        builtin = _BUILTIN_SKILL_ROOT
        target = playbook.parent / asset
        if (record.source != "builtin" or record.skill_id != sid
                or asset.is_absolute() or "\\" in relative or ".." in asset.parts
                or len(asset.parts) < 2 or asset.parts[0] != "templates"
                or not asset.name.endswith(".py.j2") or builtin.is_symlink()):
            raise ValueError()
        for path in (playbook, target):
            current = builtin
            for part in path.relative_to(builtin).parts:
                current /= part
                if current.is_symlink():
                    raise ValueError()
            if not path.resolve(strict=True).is_relative_to(builtin.resolve(strict=True)):
                raise ValueError()
        denied = skill_asset_access_error(call, record, target, skill_index)
        if denied is not None:
            return denied
        if not stat.S_ISREG(target.stat().st_mode):
            raise ValueError()
        with open(target, "rb", opener=lambda path, flags: os.open(
                path, flags | getattr(os, "O_NOFOLLOW", 0))) as stream:
            if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
                raise ValueError()
            raw = stream.read(_HARD_FILE_BYTES + 1)
        if len(raw) > _HARD_FILE_BYTES or b"\x00" in raw:
            raise ValueError()
        content = raw.decode("utf-8")
    except (OSError, ValueError, RuntimeError):
        return schema_validation_result(call, "source_asset must be a non-symlink builtin templates/*.py.j2 UTF-8 file of at most 1 MiB")
    revision = hashlib.sha256(raw).hexdigest()
    if revision != expected:
        return ToolResult.from_error(tool_use_id=call.id, name=call.name, error=ToolError(
            kind=ToolErrorKind.STALE_FILE, message="source_asset revision mismatch; read the template with Skill again and copy its TOP-LEVEL revision as the complete 64-character lowercase SHA-256 literal, not skill.revision or catalog_revision",
            retryable=True, recovery_hint={"next_required_action": {"tool": "Skill",
                "arguments": {"skill": sid, "file": relative}}}))
    return content, {"skill": sid, "file": asset.as_posix(), "revision": revision}


def write_file_handler(
    call: ToolCall,
    *,
    root: Path,
    file_state: Optional[FileStateCache],
    session_id: Optional[str] = None,
    config: Any = None,
    task_session_id: Optional[str] = None,
    task_actor_id: Optional[str] = None,
    skill_index: Any = None,
) -> ToolResult:
    args = call.arguments or {}
    raw_path = args.get("path")
    # Accept either ``content`` (legacy) or ``contents`` (current
    # bootstrap-declared schema). Different parts of the codebase used
    # different names; the handler is permissive so the model never has
    # to guess which spelling to use.
    if "content" in args:
        content = args.get("content")
    else:
        content = args.get("contents")
    require_existing_read = bool(args.get("require_existing_read", True))
    source_requested = "source_asset" in args
    if source_requested and ("contents" in args or "content" in args):
        return schema_validation_result(call, "source_asset and contents/content are mutually exclusive")

    if not raw_path or (not source_requested and content is None):
        return schema_validation_result(
            call, "write_file requires path and either contents (string body) or source_asset",
        )

    if not source_requested and not isinstance(content, str):
        return schema_validation_result(
            call, "content must be a string (write binary via separate handler)",
        )

    try:
        p = resolve_workspace_path(str(raw_path), root=root)
    except WorkspaceEscapeError as exc:
        return ToolResult.from_error(
            tool_use_id=call.id,
            name=call.name,
            error=ToolError(kind=ToolErrorKind.PERMISSION_DENIED, message=str(exc)),
        )

    rel_path = _short_path(p, root)
    denied = _proposal_task_guard(call, path=rel_path, root=root, config=config,
                                  task_session_id=task_session_id, task_actor_id=task_actor_id)
    if denied is not None:
        return denied
    proposal_tools = _proposal_required_tools(rel_path)
    if proposal_tools:
        return _proposal_required_result(
            call,
            path=rel_path,
            tools=proposal_tools,
        )

    provenance = None
    if source_requested:
        source = _skill_template_source(call, args["source_asset"], skill_index)
        if isinstance(source, ToolResult):
            return source
        content, provenance = source

    requested_path = p
    existed = requested_path.exists()
    placement_kind = "existing" if existed else ""
    outside_reason = ""
    if not existed:
        try:
            placement = place_new_conversation_file(
                requested_path,
                root=root,
                session_id=session_id,
                allow_outside_conversation=bool(
                    args.get("allow_outside_conversation", False)
                ),
                outside_conversation_reason=str(
                    args.get("outside_conversation_reason") or ""
                ),
            )
        except ValueError as exc:
            return schema_validation_result(call, str(exc))
        p = placement.path
        placement_kind = placement.kind
        outside_reason = placement.outside_reason
        existed = p.exists()
    before = ""
    if existed:
        try:
            before, _, _ = _read_text(p)
        except OSError as exc:
            return ToolResult.from_error(
                tool_use_id=call.id,
                name=call.name,
                error=ToolError(kind=ToolErrorKind.EXECUTION_ERROR, message=str(exc)),
            )
        if file_state is not None and require_existing_read:
            try:
                file_state.assert_fresh_for_edit(p, on_disk=before, require_read=True)
            except StaleFileReadError as exc:
                return ToolResult.from_error(
                    tool_use_id=call.id,
                    name=call.name,
                    error=ToolError(
                        kind=ToolErrorKind.STALE_FILE,
                        message=str(exc),
                        detail=exc.as_dict(),
                        retryable=True,
                        recovery_hint={
                            "action": "read_file",
                            "path": _short_path(p, root),
                            "next_required_action": {
                                "tool": "read_file",
                                "arguments": {"path": _short_path(p, root)},
                            },
                        },
                    ),
                )
    else:
        try:
            p.parent.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            return ToolResult.from_error(
                tool_use_id=call.id,
                name=call.name,
                error=ToolError(
                    kind=ToolErrorKind.EXECUTION_ERROR,
                    message=f"failed to create parent dir: {exc}",
                ),
            )

    try:
        # Text-mode newline translation on Windows changes the bytes behind
        # the recorded hash and turns existing CRLF into CRCRLF.
        p.write_bytes(content.encode("utf-8"))
    except OSError as exc:
        return ToolResult.from_error(
            tool_use_id=call.id,
            name=call.name,
            error=ToolError(
                kind=ToolErrorKind.EXECUTION_ERROR,
                message=f"write failed: {exc}",
            ),
        )

    if file_state is not None:
        file_state.record_write(
            p, new_content=content, line_count=content.count("\n") + 1
        )

    result_path = _short_path(p, root)
    requested_rel = _short_path(requested_path, root)
    diff = _make_diff(before, content, result_path) if existed else (
        f"--- /dev/null\n+++ b/{result_path}\n"
        + "".join(f"+{line}\n" for line in content.splitlines())
    )
    payload = {
        "path": result_path,
        "kind": "create" if not existed else "overwrite",
        "placement": placement_kind,
        "bytes": len(content.encode("utf-8")),
        "lines": content.count("\n") + 1,
        "content_hash": compute_file_hash(content),
    }
    if result_path != requested_rel:
        payload["requested_path"] = requested_rel
    if provenance is not None:
        payload["source_asset"] = provenance
    if outside_reason:
        payload["outside_conversation_reason"] = outside_reason
    return ToolResult(
        tool_use_id=call.id,
        name=call.name,
        content=[
            ToolResultPart.diff_part(diff=diff, path=result_path),
            ToolResultPart.json_part(payload),
        ],
        metadata={
            "path": result_path,
            "kind": "write",
            "placement": placement_kind,
            **(
                {"requested_path": requested_rel}
                if result_path != requested_rel
                else {}
            ),
            **(
                {"outside_conversation_reason": outside_reason}
                if outside_reason
                else {}
            ),
        },
        context_modifiers=[
            ContextModifier(kind="file_mutate", path=str(p), payload={"created": not existed})
        ],
    )


__all__ = [
    "edit_file_handler",
    "list_dir_handler",
    "read_file_handler",
    "write_file_handler",
]
