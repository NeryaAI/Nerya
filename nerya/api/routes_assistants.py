"""Operator-owned assistant profiles and conversations."""
from __future__ import annotations

from functools import wraps

from ..agent.assistants import AssistantError, AssistantStore, DEFAULT_ID, PERMISSIONS
from ..core import yaml_io


def routes():
    def safe(handler):
        @wraps(handler)
        def wrapped(client, payload):
            try:
                if not isinstance(payload, dict):
                    raise AssistantError("invalid_assistant_request")
                store = AssistantStore(client.config.paths)
                store.config = client.config
                return handler(store, payload)
            except AssistantError as exc:
                return {"ok": False, "error": exc.code, "_status": exc.status}
            except (ValueError, TypeError):
                return {"ok": False, "error": "invalid_assistant_request", "_status": 400}
        return wrapped

    @safe
    def listing(store, _p):
        doc = yaml_io.load(store.paths.messages_channels, default={}) or {}
        channels = [{"channel": name, "kind": cfg.get("kind", name), "assistant_id": cfg.get("assistant_id") or DEFAULT_ID,
                     "enabled": cfg.get("enabled") is not False} for name, cfg in (doc.get("channels") or {}).items()
                    if cfg.get("kind", name) not in ("dashboard", "local")]
        return {"ok": True, "assistants": store.list(), "channels": channels, "workspace": str(store.paths.root)}

    @safe
    def save(store, payload):
        allowed = {"id", "expected_revision", "profile"}
        if set(payload) - allowed or not isinstance(payload.get("profile"), dict):
            raise AssistantError("invalid_assistant_request")
        return {"ok": True, "assistant": store.save(payload["profile"], aid=payload.get("id"), expected_revision=payload.get("expected_revision"))}

    @safe
    def duplicate(store, payload):
        profile = store.get(payload.get("id"))
        fields = {key: profile[key] for key in ("name", "role", "instructions", "avatar", "enabled", "model_provider", "model_id", "model_tier", "tool_policy", *PERMISSIONS)}
        fields.update(name=str(payload.get("name") or profile["name"] + " (copy)")[:60], enabled=True)
        return {"ok": True, "assistant": store.save(fields)}

    @safe
    def archive(store, payload):
        aid = payload.get("id")
        if aid == DEFAULT_ID:
            raise AssistantError("default_assistant_cannot_archive", 409)
        doc = yaml_io.load(store.paths.messages_channels, default={}) or {}
        if any((cfg.get("assistant_id") or DEFAULT_ID) == aid for cfg in (doc.get("channels") or {}).values()):
            raise AssistantError("assistant_has_gateway_bindings", 409)
        # Retain historical conversations. Restoring is an ordinary profile edit.
        return {"ok": True, "assistant": store.save({"enabled": False}, aid=aid, expected_revision=payload.get("expected_revision"))}

    @safe
    def conversation(store, payload):
        return {"ok": True, **store.conversation(payload.get("assistant_id"), payload.get("title", ""))}

    @safe
    def conversations(store, query):
        return {"ok": True, "sessions": store.conversations(query.get("assistant_id"), query.get("kind"))}

    @safe
    def home(store, payload):
        from ..agent.assistant_work import AssistantWork
        from ..agent.command_runtime import runtime
        result = store.home(payload.get("assistant_id", DEFAULT_ID))
        AssistantWork(store.paths).recover(runtime(store.config))
        return {"ok": True, **result}

    @safe
    def results(store, query):
        from ..agent.assistant_work import AssistantWork
        aid = query.get("assistant_id", DEFAULT_ID)
        store.get(aid)
        return {"ok": True, "results": AssistantWork(store.paths).results(aid)}

    @safe
    def session(store, query):
        from ..agent.assistant_work import AssistantWork
        sid = query.get("session_id")
        return {"ok": True, "assistant": store.for_session(sid, enabled=False), "conversation": AssistantWork(store.paths).session(sid)}

    def tools(client, _query):
        from ..agent.kernel import AgentKernel
        registry, _deps = AgentKernel(config=client.config, skills=client.skills).prepare_tools()
        return {"ok": True, "tools": [{"name": d.name, "description": d.description, "namespace": d.namespace,
                                         "read_only": d.read_only} for d in registry.list_tools()]}

    @safe
    def tasks(store, query):
        from ..agent.history_mutations import _session_id
        from ..subagents.tasks import TaskStore
        from ..triggers.schedule import load_schedules
        sid = _session_id(query.get("session_id"))
        store.for_session(sid, enabled=False)
        from ..agent.assistant_work import AssistantWork
        rows = AssistantWork(store.paths).tasks(sid=sid)
        for record in TaskStore(store.paths).list(parent_session_id=sid, limit=50):
            rows.append({**record.asdict(), "kind": "background", "title": record.payload.get("title") or record.name,
                         "assistant_id": record.payload.get("assistant_id")})
        for entry in load_schedules(store.paths):
            if entry.payload.get("assistant_conversation_id") == sid:
                rows.append({"task_id": entry.id, "kind": "scheduled", "title": entry.payload.get("title") or entry.id,
                             "state": "scheduled" if entry.enabled else "paused", "assistant_id": entry.execution.get("assistant_id"),
                             "every_seconds": entry.every_seconds, "cron": entry.cron, "output": {}})
        return {"ok": True, "tasks": rows}

    @safe
    def stop_task(store, payload):
        from ..agent.history_mutations import _session_id
        from ..subagents.tasks import TaskStore
        sid, tid = _session_id(payload.get("session_id")), _session_id(payload.get("task_id"))
        if tid.startswith("awork_"):
            from ..agent.assistant_work import AssistantWork
            return {"ok": True, "cancel_requested": AssistantWork(store.paths).stop(store.config, tid, sid)}
        task_store = TaskStore(store.paths)
        record = task_store.load(tid)
        if record is None:
            raise AssistantError("assistant_task_not_found", 404)
        if record.parent_session_id != sid:
            raise AssistantError("assistant_task_owner_mismatch", 403)
        return {"ok": True, "cancel_requested": task_store.request_stop(tid)}

    return [("GET", "/assistants", listing), ("POST", "/assistants/home", home), ("GET", "/assistants/results", results), ("GET", "/assistants/tasks", tasks), ("POST", "/assistants/tasks/stop", stop_task), ("GET", "/assistants/tools", tools), ("GET", "/assistants/session", session), ("POST", "/assistants/save", save),
            ("POST", "/assistants/duplicate", duplicate), ("POST", "/assistants/archive", archive),
            ("GET", "/assistants/conversations", conversations), ("POST", "/assistants/conversations", conversation)]
