"""Persistent specialist identities shared by chat and every message gateway.

Profiles live in the workspace database. A conversation's binding is immutable;
edits are re-read at turn admission and before each tool execution.
"""
from __future__ import annotations

import copy
import hashlib
import json
import re
import time
import uuid
from contextlib import closing
from typing import Any

from ..core.config import Config
from ..db.sqlite import connect
from ..db.repositories import AgentSessionRepository
from ..tools.capability_policy import normalise_tool_policy
from .history_mutations import _session_id
from .session import SessionStore, new_session_id

DEFAULT_ID = "main"
PERMISSIONS = ("research_allowed", "memory_allowed", "browser_allowed", "files_allowed", "shell_allowed")
_FIELDS = {"name", "role", "instructions", "avatar", "enabled", "model_provider", "model_id", "model_tier", "tool_policy", *PERMISSIONS}
_DENIED = {
    "research_allowed": ["web_*", "research_*", "data_*"],
    "memory_allowed": ["memory_*", "journal_search", "operator_profile_*"],
    "browser_allowed": ["browser_*", "computer_*"],
    "files_allowed": ["read_file", "write_file", "edit_file", "glob", "grep", "workspace_*"],
    "shell_allowed": ["run_shell", "shell_*", "skill_script_run", "script_run", "process_*"],
}


class AssistantError(ValueError):
    def __init__(self, code: str, status: int = 400):
        self.code, self.status = code, status
        super().__init__(code)


def default_profile() -> dict:
    return {"id": DEFAULT_ID, "name": "Nerya", "role": "", "instructions": "", "avatar": "N",
            "enabled": True, "model_provider": "", "model_id": "", "model_tier": "",
            "tool_policy": normalise_tool_policy(None), **{k: True for k in PERMISSIONS},
            "revision": 0, "created_at": 0, "updated_at": 0}


def validate_profile(raw: dict, current: dict | None = None) -> dict:
    if not isinstance(raw, dict) or set(raw) - _FIELDS:
        raise AssistantError("invalid_assistant_fields")
    profile = copy.deepcopy(current or default_profile())
    if current is None:
        profile["shell_allowed"] = False
    for key, limit in {"name": 60, "role": 240, "instructions": 8000,
                       "model_provider": 120, "model_id": 240, "model_tier": 32}.items():
        if key in raw:
            if not isinstance(raw[key], str) or len(raw[key]) > limit:
                raise AssistantError("invalid_assistant_" + key)
            profile[key] = raw[key].strip()
    if "avatar" in raw:
        from .assistant_avatar import valid_avatar
        if not isinstance(raw["avatar"], str) or not valid_avatar(raw["avatar"].strip()):
            raise AssistantError("invalid_assistant_avatar")
        profile["avatar"] = raw["avatar"].strip()
    if not profile["name"]:
        raise AssistantError("assistant_name_required")
    for key in (*PERMISSIONS, "enabled"):
        if key in raw:
            if not isinstance(raw[key], bool):
                raise AssistantError("invalid_assistant_" + key)
            profile[key] = raw[key]
    if bool(profile["model_provider"]) != bool(profile["model_id"]):
        raise AssistantError("assistant_model_pair_required")
    if profile["model_tier"] not in ("", "light", "medium", "high"):
        raise AssistantError("invalid_assistant_model_tier")
    if "tool_policy" in raw:
        policy = normalise_tool_policy(raw["tool_policy"])
        selectors = policy["deny"] + [s for group in policy["allow_groups"] for s in group]
        if len(selectors) > 200 or any(len(s) > 240 for s in selectors):
            raise AssistantError("invalid_assistant_tool_policy")
        profile["tool_policy"] = policy
    return profile


class AssistantStore:
    def __init__(self, paths):
        self.paths = paths

    def _connect(self):
        con = connect(self.paths.db)
        con.execute("CREATE TABLE IF NOT EXISTS assistant_profiles (id TEXT PRIMARY KEY, profile_json TEXT NOT NULL)")
        con.execute("CREATE TABLE IF NOT EXISTS assistant_sessions (session_id TEXT PRIMARY KEY, assistant_id TEXT NOT NULL, created_at REAL NOT NULL)")
        return con

    def list(self) -> list[dict]:
        with closing(self._connect()) as con:
            profiles = [json.loads(r[0]) for r in con.execute("SELECT profile_json FROM assistant_profiles ORDER BY id")]
        if not any(p["id"] == DEFAULT_ID for p in profiles):
            profiles.insert(0, default_profile())
        # The only built-in assistant is main; additional profiles are user-created.
        return sorted(profiles, key=lambda profile: (profile["id"] != DEFAULT_ID, profile["created_at"]))

    def get(self, aid: Any, *, enabled: bool = False) -> dict:
        if not isinstance(aid, str) or not re.fullmatch(r"[A-Za-z0-9_.-]{1,80}", aid):
            raise AssistantError("invalid_assistant_id")
        with closing(self._connect()) as con:
            row = con.execute("SELECT profile_json FROM assistant_profiles WHERE id=?", (aid,)).fetchone()
        profile = json.loads(row[0]) if row else default_profile() if aid == DEFAULT_ID else None
        if not profile:
            raise AssistantError("assistant_not_found", 404)
        if enabled and not profile["enabled"]:
            raise AssistantError("assistant_disabled", 409)
        return profile

    def save(self, raw: dict, *, aid: str | None = None, expected_revision: int | None = None) -> dict:
        with closing(self._connect()) as con:
            con.execute("BEGIN IMMEDIATE")
            try:
                row = con.execute("SELECT profile_json FROM assistant_profiles WHERE id=?", (aid,)).fetchone() if aid else None
                current = json.loads(row[0]) if row else default_profile() if aid == DEFAULT_ID else None
                if aid and not current:
                    raise AssistantError("assistant_not_found", 404)
                if aid and (isinstance(expected_revision, bool) or expected_revision != current["revision"]):
                    raise AssistantError("assistant_revision_conflict", 409)
                profile = validate_profile(raw, current)
                now = time.time()
                profile.update(id=aid or "asst_" + uuid.uuid4().hex[:16], revision=(current or {}).get("revision", 0) + 1,
                               created_at=(current or {}).get("created_at") or now, updated_at=now)
                con.execute("INSERT INTO assistant_profiles VALUES (?,?) ON CONFLICT(id) DO UPDATE SET profile_json=excluded.profile_json",
                            (profile["id"], json.dumps(profile, ensure_ascii=False)))
                con.execute("COMMIT")
            except BaseException:
                con.execute("ROLLBACK")
                raise
            if current and any(current.get(key) != profile.get(key) for key in ("enabled", "tool_policy", *PERMISSIONS)):
                from ..harness.cancellation import signal_cancel
                sessions = con.execute("SELECT session_id FROM assistant_sessions WHERE assistant_id=? UNION SELECT session_id FROM agent_sessions WHERE json_valid(meta_json) AND json_extract(meta_json,'$.assistant_id')=?", (profile["id"], profile["id"])).fetchall()
                for session in sessions:
                    signal_cancel(session[0], reason="assistant_permissions_changed")
                from ..subagents.tasks import TaskStore
                task_store = TaskStore(self.paths)
                for task in task_store.list(limit=1000):
                    if task.payload.get("kind") == "assistant_task" and task.payload.get("assistant_id") == profile["id"] and not task.is_terminal():
                        task_store.request_stop(task.task_id)
            return profile

    def for_session(self, sid: str | None, *, enabled: bool = True) -> dict | None:
        if not sid:
            return None
        _session_id(sid)
        with closing(self._connect()) as con:
            row = con.execute("SELECT assistant_id FROM assistant_sessions WHERE session_id=?", (sid,)).fetchone()
            session = AgentSessionRepository(con).get_session(sid) if not row else None
        # Forked conversations carry the identity in the existing session metadata.
        state = SessionStore(self.paths.root).load(sid) if not row else None
        aid = row[0] if row else (json.loads(session.get("meta_json") or "{}").get("assistant_id") if session else None) or (state.meta.get("assistant_id") if state else None)
        return self.get(aid, enabled=enabled) if aid else None

    def bind(self, sid: str, aid: str, *, source: str = "dashboard", title: str = "", kind: str = "assistant") -> dict:
        _session_id(sid)
        profile = self.get(aid, enabled=True)
        existing = self.for_session(sid, enabled=False)
        if existing and existing["id"] != aid:
            raise AssistantError("assistant_session_mismatch", 409)
        state = SessionStore(self.paths.root).load(sid)
        if state and state.meta.get("assistant_id") not in (None, aid):
            raise AssistantError("assistant_session_mismatch", 409)
        with closing(self._connect()) as con:
            con.execute("BEGIN IMMEDIATE")
            try:
                from .history_mutations import is_session_deleted
                if is_session_deleted(con, sid):
                    raise AssistantError("session_deleted", 409)
                created = con.execute("INSERT OR IGNORE INTO assistant_sessions VALUES (?,?,?)", (sid, aid, time.time())).rowcount == 1
                bound = con.execute("SELECT assistant_id FROM assistant_sessions WHERE session_id=?", (sid,)).fetchone()[0]
                if bound != aid:
                    raise AssistantError("assistant_session_mismatch", 409)
                if created:
                    repo = AgentSessionRepository(con)
                    previous = repo.get_session(sid)
                    repo.upsert_session(session_id=sid, title=title, source=source if not previous or not previous.get("source") else "",
                        meta={"assistant_id": aid, "assistant_name": profile["name"], "conversation_kind": kind})
                con.execute("COMMIT")
            except BaseException:
                con.execute("ROLLBACK")
                raise
        if created:
            SessionStore(self.paths.root).update_meta(sid, {"assistant_id": aid, "assistant_name": profile["name"], "conversation_kind": kind})
        return profile

    def home(self, aid: str) -> dict:
        profile = self.get(aid, enabled=True)
        sid = "assistant_" + hashlib.sha256(aid.encode()).hexdigest()[:24]
        self.bind(sid, aid, source="assistant", title=profile["name"])
        return {"session_id": sid, "assistant_id": aid, "assistant_name": profile["name"]}

    def conversation(self, aid: str, title: str = "") -> dict:
        if not isinstance(title, str) or len(title) > 160:
            raise AssistantError("invalid_conversation_title")
        sid = new_session_id()
        profile = self.bind(sid, aid, title=title.strip())
        return {"session_id": sid, "assistant_id": aid, "assistant_name": profile["name"]}

    def conversations(self, aid: str, kind: str | None = None) -> list[dict]:
        self.get(aid)
        with closing(self._connect()) as con:
            rows = con.execute("SELECT s.* FROM agent_sessions s LEFT JOIN assistant_sessions a USING(session_id) WHERE (a.assistant_id=? OR (a.assistant_id IS NULL AND json_extract(s.meta_json,'$.assistant_id')=?) OR (?='main' AND a.assistant_id IS NULL AND json_extract(s.meta_json,'$.assistant_id') IS NULL AND (s.strategy_id IS NULL OR s.strategy_id='') AND s.source NOT GLOB 'strategy*' AND s.source NOT GLOB 'scheduled*' AND s.source NOT GLOB 'subagent*' AND s.source NOT GLOB 'team*' AND s.source NOT GLOB 'trigger*')) AND NOT EXISTS (SELECT 1 FROM agent_deleted_sessions d WHERE d.session_id=s.session_id) ORDER BY s.updated_at DESC LIMIT 100", (aid, aid, aid)).fetchall()
        items = [{**dict(r), "meta": json.loads(r["meta_json"] or "{}"), "meta_json": None} for r in rows]
        return [row for row in items if row["meta"].get("conversation_kind") != "assistant"] if kind == "task" else items


def assistant_policy(profile: dict) -> dict:
    policy = normalise_tool_policy(profile.get("tool_policy"))
    for key, patterns in _DENIED.items():
        if not profile[key]:
            policy["deny"].extend(patterns)
    # Restricted assistants cannot delegate around their own capability boundary.
    if not all(profile[k] for k in PERMISSIONS) or policy["allow_groups"] or policy["deny"]:
        policy["deny"].extend(["subagent_run*", "team_run", "subagent_message", "agent_message"])
    return policy


def merge_policy(*policies: dict) -> dict:
    result = {"allow_groups": [], "deny": []}
    for raw in policies:
        policy = normalise_tool_policy(raw)
        result["allow_groups"].extend(policy["allow_groups"])
        result["deny"].extend(policy["deny"])
    return result


def prepare_request(config: Config, request: dict) -> dict:
    result = dict(request)
    store = AssistantStore(config.paths)
    sid, aid = result.get("session_id"), result.get("assistant_id")
    if aid:
        if not sid:
            raise AssistantError("assistant_session_required")
        kind = result.get("conversation_kind", "assistant")
        if kind not in {"assistant", "task"}:
            raise AssistantError("invalid_conversation_kind")
        profile = store.bind(sid, aid, source=str(result.get("source") or "dashboard"), kind=kind)
    else:
        profile = store.for_session(sid)
    if profile:
        result["assistant_id"] = profile["id"]
        for key in ("model_provider", "model_id", "model_tier"):
            if profile[key] and not result.get(key):
                result[key] = profile[key]
    return result


def assistant_config(config: Config, profile: dict | None, *, session_id: str | None = None) -> Config:
    if not profile:
        return config
    data = copy.deepcopy(config.data)
    native = data.setdefault("agent", {}).setdefault("native", {})
    base_policy = normalise_tool_policy(native.get("assistant_base_tool_policy", native.get("tool_policy")))
    native["assistant_base_tool_policy"] = base_policy
    native["tool_policy"] = merge_policy(base_policy, assistant_policy(profile))
    native["assistant_coordination_only"] = False
    if session_id:
        with closing(AssistantStore(config.paths)._connect()) as con:
            session = AgentSessionRepository(con).get_session(session_id)
        meta = json.loads(session["meta_json"] or "{}") if session else {}
        source = str((session or {}).get("source") or "")
        execution = meta.get("conversation_kind") == "task" or source.startswith(("scheduled", "strategy", "subagent", "team", "trigger"))
        if not execution:
            native["assistant_coordination_only"] = True
            native["tool_policy"] = merge_policy(native["tool_policy"], {"allow_groups": [["assistant_task_create", "task_create", "task_list", "task_get", "task_output", "task_stop", "task_summary", "Skill", "skill_view", "memory_*", "ask_user", "request_user_input", "send_message"]]})
    native["assistant_profile"] = copy.deepcopy(profile)
    return Config(paths=config.paths, data=data)


def current_assistant_policy(config: Config, sid: str | None, task_policy: dict) -> dict:
    profile = AssistantStore(config.paths).for_session(sid)
    return merge_policy(task_policy, assistant_policy(profile)) if profile else task_policy


def gateway_scope(platform: str, channel: str, aid: str, *, legacy: bool = False) -> str:
    if aid == DEFAULT_ID and (legacy or channel == platform):
        return platform  # Preserve the existing default Telegram conversations.
    suffix = hashlib.sha256(json.dumps([channel, aid]).encode()).hexdigest()[:12]
    return platform + "_a" + suffix
