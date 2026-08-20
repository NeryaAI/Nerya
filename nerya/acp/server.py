"""Stdio JSON-RPC 2.0 server exposing Nerya's approval + turn surface.

the method table is now registry-driven (see
:mod:`nerya.acp.methods`) instead of a hand-coded dict. The wire
shape is unchanged: line-delimited JSON-RPC 2.0, so existing IDE
clients keep working. New layers are stacked on top of the legacy
methods:

* ``meta.methods`` — introspection of every registered method.
* ``session.create`` / ``session.list`` / ``session.interrupt`` /
  ``session.resume`` / ``session.branch`` — talk-track lifecycle
  helpers backed by :class:`SessionStore`.
* ``tool.list`` / ``tool.call`` / ``tool.approve`` — manifest-driven
  tool surface that funnels through :meth:`InternalClient.skill.call`
  so the same risk/approval/availability gates apply as in the
  planner.
* ``event.subscribe`` / ``event.unsubscribe`` / ``event.poll`` —
  pub-sub style event drain so MCP/IDE clients can stream
  ``turn.start`` / ``turn.step`` / ``approval.pending`` updates over
  the same JSON-RPC pipe.
"""

from __future__ import annotations

import json
import sys
from dataclasses import dataclass, field
from typing import Any

from ..core.time import now_iso
from ..sdk.internal_client import InternalClient
from .methods import (
    AcpError,
    EventBus,
    MethodRegistry,
    SessionStore,
)


PROTOCOL_VERSION = "nerya-acp/0.2"

# Keep the legacy alias around so any external client that imported
# ``_AcpError`` from this module before the refactor keeps compiling.
_AcpError = AcpError


def _jsonrpc_error(rid: Any, code: int, message: str, data: Any = None) -> dict:
    err = {"jsonrpc": "2.0", "id": rid, "error": {"code": code, "message": message}}
    if data is not None:
        err["error"]["data"] = data
    return err


def _jsonrpc_ok(rid: Any, result: Any) -> dict:
    return {"jsonrpc": "2.0", "id": rid, "result": result}


@dataclass
class AcpServer:
    """Pure-Python ACP adapter. Methods come from a :class:`MethodRegistry`."""

    client: InternalClient
    methods: MethodRegistry = field(default_factory=MethodRegistry)
    sessions: SessionStore = field(default_factory=SessionStore)
    events: EventBus = field(default_factory=EventBus)

    def __post_init__(self) -> None:
        # Register the canonical default surface at boot. Tests can
        # override (or replace) individual methods via
        # ``self.methods.register(... override=True)``.
        register_default_methods(self)

    @classmethod
    def boot(cls) -> "AcpServer":
        return cls(client=InternalClient.boot())

    # -------------------------------------------------------------- routing
    def dispatch(self, method: str, params: dict[str, Any] | None) -> Any:
        params = params or {}
        spec = self.methods.get(method)
        if spec is None:
            raise AcpError(-32601, f"method not found: {method}")
        return spec.handler(params)

    # -------------------------------------------------------------- helpers
    def publish_event(self, event: dict[str, Any]) -> int:
        """Convenience for producers (turn engine, approvals helper).

        Stamps the event with ``ts`` if missing and forwards to the bus.
        """

        ev = dict(event)
        ev.setdefault("ts", now_iso())
        return self.events.publish(ev)

    def _move_approval(
        self,
        approval_id: str,
        state: str,
        *,
        note: str,
        resolver_actor_id: str = "",
    ) -> dict[str, Any]:
        from ..approval_service import ApprovalService

        service = ApprovalService(self.client.config)
        moved = service.move(
            approval_id,
            state=state,
            note=note,
            resolver_actor_id=resolver_actor_id,
        )
        if moved is None:
            raise AcpError(
                -32004,
                f"approval {approval_id} not found, expired, or unauthorized",
            )
        event = {
            "kind": "approval.resolved",
            "approval_id": approval_id,
            "state": state,
            "note": note,
            "resolver_actor_id": resolver_actor_id,
            "record": moved,
        }
        self.publish_event(event)
        service.publish_resolution(
            approval_id,
            state=state,
            record=moved,
        )
        return {"ok": True, "approval_id": approval_id, "state": state, "record": moved}


# --------------------------------------------------------------------- #
# Default method registration
# --------------------------------------------------------------------- #


def register_default_methods(server: AcpServer) -> None:
    """Populate ``server.methods`` with the canonical Nerya ACP surface."""

    methods = server.methods

    # ----- agent / meta -------------------------------------------------
    methods.add(
        "initialize",
        lambda p: _m_initialize(server, p),
        category="meta",
        description=(
            "Negotiate protocol version + return live capability matrix. "
            "capabilities now include the registered method "
            "categories and session/tool/event surfaces."
        ),
        params_schema={"type": "object", "properties": {}},
        result_schema={
            "type": "object",
            "properties": {
                "protocol": {"type": "string"},
                "capabilities": {"type": "object"},
            },
        },
    )
    methods.add(
        "meta.methods",
        lambda p: _m_meta_methods(server, p),
        category="meta",
        description="Return every registered ACP method.",
        result_schema={"type": "object", "properties": {"methods": {"type": "array"}}},
    )
    methods.add(
        "shutdown",
        lambda p: {"ok": True},
        category="meta",
        description="Graceful shutdown signal.",
    )

    # ----- agent introspection -----------------------------------------
    methods.add(
        "agent.info",
        lambda p: _m_info(server, p),
        category="agent",
        description="Workspace info + skill/connector summary.",
    )
    methods.add(
        "agent.capabilities",
        lambda p: _m_capabilities(server, p),
        category="agent",
        description="Live capability matrix from the MCP tools layer.",
    )
    methods.add(
        "agent.skills",
        lambda p: {"skills": server.client.skills.list()},
        category="agent",
        description="List installed/registered skills.",
    )
    methods.add(
        "agent.recent_turns",
        lambda p: _m_recent_turns(server, p),
        category="agent",
        description="Tail of the agent turn journal.",
    )
    methods.add(
        "agent.submit_message",
        lambda p: _m_submit_message(server, p),
        category="agent",
        description="Submit a free-form user message — emits an "
                    "``acp.user_message`` trigger.",
    )
    methods.add(
        "agent.triggers.explain",
        lambda p: _m_trigger_explain(server, p),
        category="agent",
        description="Trace what would happen if a hypothetical trigger fired.",
    )
    methods.add(
        "agent.proposals_list",
        lambda p: _m_proposals_list(server, p),
        category="agent",
        description="List patch proposals in the workspace queue.",
    )

    # ----- approvals ----------------------------------------------------
    methods.add(
        "agent.pending_approvals",
        lambda p: _m_pending_approvals(server, p),
        category="approvals",
        description="Tail pending approvals.",
    )
    methods.add(
        "agent.approve",
        lambda p: _m_approve(server, p),
        category="approvals",
        description="Move an approval from pending → approved.",
        params_schema={
            "type": "object",
            "required": ["approval_id", "actor_id"],
            "properties": {
                "approval_id": {"type": "string"},
                "note": {"type": "string"},
                "actor_id": {"type": "string"},
            },
        },
    )
    methods.add(
        "agent.reject",
        lambda p: _m_reject(server, p),
        category="approvals",
        description="Move an approval from pending → rejected.",
        params_schema={
            "type": "object",
            "required": ["approval_id", "actor_id"],
            "properties": {
                "approval_id": {"type": "string"},
                "note": {"type": "string"},
                "actor_id": {"type": "string"},
            },
        },
    )

    # ----- session lifecycle -------------------------------
    methods.add(
        "session.create",
        lambda p: _m_session_create(server, p),
        category="session",
        description="Create a session record (talk-track envelope).",
    )
    methods.add(
        "session.list",
        lambda p: {"sessions": [s.asdict() for s in server.sessions.list()]},
        category="session",
        description="List all known sessions with their lifecycle state.",
    )
    methods.add(
        "session.get",
        lambda p: _m_session_get(server, p),
        category="session",
        description="Fetch a single session by id.",
    )
    methods.add(
        "session.interrupt",
        lambda p: _m_session_interrupt(server, p),
        category="session",
        description="Mark a session as interrupted (operator stop button).",
    )
    methods.add(
        "session.resume",
        lambda p: _m_session_resume(server, p),
        category="session",
        description="Resume a previously interrupted session.",
    )
    methods.add(
        "session.branch",
        lambda p: _m_session_branch(server, p),
        category="session",
        description="Branch a new session off an existing one — preserves "
                    "parent metadata so the IDE can render thread trees.",
    )

    # ----- tool surface ------------------------------------
    methods.add(
        "tool.list",
        lambda p: _m_tool_list(server, p),
        category="tool",
        description="List the manifest-driven tool surface (mirrors the "
                    "MCP dynamic registry).",
    )
    methods.add(
        "tool.call",
        lambda p: _m_tool_call(server, p),
        category="tool",
        description="Invoke a tool by ``skill_id`` + ``action`` — same "
                    "dispatch chokepoint as the planner.",
        params_schema={
            "type": "object",
            "required": ["skill_id", "action"],
            "properties": {
                "skill_id": {"type": "string"},
                "action": {"type": "string"},
                "payload": {"type": "object"},
                "session_id": {"type": "string"},
                "actor": {"type": "string"},
            },
        },
    )
    methods.add(
        "tool.approve",
        lambda p: _m_tool_approve(server, p),
        category="tool",
        description="Approve a pending tool-call approval (alias of "
                    "``agent.approve`` with stable categorisation).",
        params_schema={
            "type": "object",
            "required": ["approval_id", "actor_id"],
            "properties": {
                "approval_id": {"type": "string"},
                "note": {"type": "string"},
                "actor_id": {"type": "string"},
            },
        },
    )

    # ----- event bus ---------------------------------------
    methods.add(
        "event.subscribe",
        lambda p: _m_event_subscribe(server, p),
        category="event",
        description="Open an event subscription and return its id.",
    )
    methods.add(
        "event.unsubscribe",
        lambda p: _m_event_unsubscribe(server, p),
        category="event",
        description="Close a subscription.",
    )
    methods.add(
        "event.poll",
        lambda p: _m_event_poll(server, p),
        category="event",
        description="Drain queued events for a subscription. Designed for "
                    "long-poll style clients that don't speak SSE.",
    )


# --------------------------------------------------------------------- #
# Handlers
# --------------------------------------------------------------------- #


def _m_initialize(server: AcpServer, params: dict[str, Any]) -> dict[str, Any]:
    skills_enabled: list[dict[str, Any]] = []
    for entry in server.client.skills.registry.list():
        m = entry.manifest
        skills_enabled.append({
            "id": m.id,
            "version": getattr(m, "version", ""),
            "status": getattr(m, "status", "ready"),
            "actions": sorted(m.actions.keys()),
        })
    capabilities = {
        "approvals": True,
        "proposals": True,
        "messages": True,
        "recent_turns": True,
        "skills": skills_enabled,
        "triggers_explain": True,
        # declare the new surfaces explicitly so clients
        # know which methods they can rely on without calling
        # ``meta.methods`` first.
        "sessions": True,
        "tools": True,
        "events": True,
        "method_categories": server.methods.categories(),
    }
    return {
        "protocol": PROTOCOL_VERSION,
        "server": "nerya",
        "capabilities": capabilities,
    }


def _m_meta_methods(server: AcpServer, params: dict[str, Any]) -> dict[str, Any]:
    category = params.get("category")
    specs = server.methods.specs()
    if category:
        specs = [s for s in specs if s.category == category]
    return {
        "methods": [s.asdict() for s in specs],
        "categories": server.methods.categories(),
        "total": len(specs),
    }


def _m_capabilities(server: AcpServer, params: dict[str, Any]) -> dict[str, Any]:
    from ..mcp.tools import NeryaTools
    info = NeryaTools(client=server.client).info()
    return {"capabilities": info}


def _m_info(server: AcpServer, params: dict[str, Any]) -> dict[str, Any]:
    from ..mcp.tools import NeryaTools
    return NeryaTools(client=server.client).info()


def _m_pending_approvals(server: AcpServer, params: dict[str, Any]) -> dict[str, Any]:
    from ..api import routes_approvals as _ra

    items = _ra._read_pending(server.client)
    limit = int(params.get("limit") or 100)
    return {"pending": items[-limit:]}


def _m_approve(server: AcpServer, params: dict[str, Any]) -> dict[str, Any]:
    approval_id = str(params.get("approval_id") or "")
    if not approval_id:
        raise AcpError(-32602, "approval_id required")
    note = str(params.get("note") or "")
    actor_id = str(params.get("actor_id") or params.get("actor") or "").strip()
    if not actor_id:
        raise AcpError(-32602, "actor_id required")
    return server._move_approval(
        approval_id,
        "approved",
        note=note,
        resolver_actor_id=actor_id,
    )


def _m_reject(server: AcpServer, params: dict[str, Any]) -> dict[str, Any]:
    approval_id = str(params.get("approval_id") or "")
    if not approval_id:
        raise AcpError(-32602, "approval_id required")
    note = str(params.get("note") or "")
    actor_id = str(params.get("actor_id") or params.get("actor") or "").strip()
    if not actor_id:
        raise AcpError(-32602, "actor_id required")
    return server._move_approval(
        approval_id,
        "rejected",
        note=note,
        resolver_actor_id=actor_id,
    )


def _m_recent_turns(server: AcpServer, params: dict[str, Any]) -> dict[str, Any]:
    limit = int(params.get("limit") or 20)
    p = server.client.config.paths.journal("agent")
    if not p.exists():
        return {"turns": []}
    out: list[dict] = []
    for line in p.read_text(encoding="utf-8").splitlines()[-500:]:
        try:
            rec = json.loads(line)
        except Exception:
            continue
        if rec.get("kind", "").startswith("agent.turn"):
            out.append(rec)
    return {"turns": out[-limit:]}


def _m_submit_message(server: AcpServer, params: dict[str, Any]) -> dict[str, Any]:
    text = str(params.get("text") or "").strip()
    if not text:
        raise AcpError(-32602, "text required")
    payload = {
        "text": text,
        "source_client": params.get("source_client") or "acp",
        "session_id": params.get("session_id"),
    }
    result = server.client.triggers.emit(
        source="user_command",
        kind="acp.user_message",
        payload=payload,
        target="main",
    )
    return {"ok": True, "trigger_result": result}


def _m_proposals_list(server: AcpServer, params: dict[str, Any]) -> dict[str, Any]:
    from ..evolution import patch_proposal
    props = patch_proposal.list_proposals(server.client.config.paths)
    return {"proposals": [p.asdict() for p in props]}


def _m_trigger_explain(server: AcpServer, params: dict[str, Any]) -> dict[str, Any]:
    source = str(params.get("source") or "script")
    kind = str(params.get("kind") or "")
    if not kind:
        raise AcpError(-32602, "kind required")
    payload = params.get("payload") or {}
    target = str(params.get("target") or "main")
    strategy_id = params.get("strategy_id")
    return server.client.triggers.explain(
        source=source, kind=kind, payload=payload,
        target=target, strategy_id=strategy_id,
    )


# ----- session helpers --------------------------------------------------


def _m_session_create(server: AcpServer, params: dict[str, Any]) -> dict[str, Any]:
    title = str(params.get("title") or "")
    actor = str(params.get("actor") or "")
    parent_id = params.get("parent_id")
    if parent_id is not None:
        parent_id = str(parent_id) or None
    tags = list(params.get("tags") or [])
    metadata = params.get("metadata") or {}
    if not isinstance(metadata, dict):
        raise AcpError(-32602, "metadata must be an object")
    try:
        sess = server.sessions.create(
            title=title, actor=actor, parent_id=parent_id,
            tags=tags, metadata=metadata, now_iso=now_iso,
        )
    except KeyError as exc:
        raise AcpError(-32004, str(exc)) from exc
    server.publish_event({
        "kind": "session.created",
        "session_id": sess.id,
        "parent_id": sess.parent_id,
        "actor": sess.actor,
    })
    return {"session": sess.asdict()}


def _m_session_get(server: AcpServer, params: dict[str, Any]) -> dict[str, Any]:
    sid = str(params.get("session_id") or "")
    if not sid:
        raise AcpError(-32602, "session_id required")
    sess = server.sessions.get(sid)
    if sess is None:
        raise AcpError(-32004, f"session {sid!r} not found")
    return {"session": sess.asdict()}


def _m_session_interrupt(server: AcpServer, params: dict[str, Any]) -> dict[str, Any]:
    sid = str(params.get("session_id") or "")
    if not sid:
        raise AcpError(-32602, "session_id required")
    try:
        sess = server.sessions.update_status(
            sid, "interrupted", now_iso=now_iso, interrupted=True,
        )
    except KeyError as exc:
        raise AcpError(-32004, str(exc)) from exc
    server.publish_event({"kind": "session.interrupted", "session_id": sid})
    return {"session": sess.asdict()}


def _m_session_resume(server: AcpServer, params: dict[str, Any]) -> dict[str, Any]:
    sid = str(params.get("session_id") or "")
    if not sid:
        raise AcpError(-32602, "session_id required")
    try:
        sess = server.sessions.update_status(
            sid, "active", now_iso=now_iso, interrupted=False,
        )
    except KeyError as exc:
        raise AcpError(-32004, str(exc)) from exc
    server.publish_event({"kind": "session.resumed", "session_id": sid})
    return {"session": sess.asdict()}


def _m_session_branch(server: AcpServer, params: dict[str, Any]) -> dict[str, Any]:
    parent_id = str(params.get("session_id") or params.get("parent_id") or "")
    if not parent_id:
        raise AcpError(-32602, "session_id required")
    try:
        server.sessions.require(parent_id)
    except KeyError as exc:
        raise AcpError(-32004, str(exc)) from exc
    branch_params = {
        "title": params.get("title") or f"branch of {parent_id}",
        "actor": params.get("actor") or "",
        "parent_id": parent_id,
        "tags": params.get("tags") or [],
        "metadata": params.get("metadata") or {},
    }
    return _m_session_create(server, branch_params)


# ----- tool helpers -----------------------------------------------------


def _m_tool_list(server: AcpServer, params: dict[str, Any]) -> dict[str, Any]:
    from ..mcp.dynamic_tools import DynamicMCPRegistry, policy_from_config

    policy = policy_from_config(server.client.config)
    registry = DynamicMCPRegistry.build(server.client, policy=policy)
    return {
        "tools": [t.asdict() for t in registry.tools],
        "dropped": [d.asdict() for d in registry.dropped],
        "policy": registry.asdict()["policy"],
        "total": len(registry.tools),
    }


def _m_tool_call(server: AcpServer, params: dict[str, Any]) -> dict[str, Any]:
    skill_id = str(params.get("skill_id") or "").strip()
    action = str(params.get("action") or "").strip()
    if not skill_id or not action:
        raise AcpError(-32602, "skill_id and action required")
    payload = params.get("payload") or {}
    if not isinstance(payload, dict):
        raise AcpError(-32602, "payload must be an object")
    actor = str(params.get("actor") or "acp")
    session_id = params.get("session_id")
    server.publish_event({
        "kind": "tool.start",
        "skill_id": skill_id,
        "action": action,
        "actor": actor,
        "session_id": session_id,
    })
    try:
        result = server.client.skill.call(
            skill_id, action,
            payload=dict(payload),
            caller=f"acp:{actor}",
        )
    except Exception as exc:  # surface to JSON-RPC layer cleanly
        server.publish_event({
            "kind": "tool.error",
            "skill_id": skill_id,
            "action": action,
            "session_id": session_id,
            "error": {"type": type(exc).__name__, "message": str(exc)},
        })
        raise AcpError(
            -32010,
            f"tool dispatch failed: {type(exc).__name__}: {exc}",
            data={"skill_id": skill_id, "action": action},
        ) from exc
    server.publish_event({
        "kind": "tool.result",
        "skill_id": skill_id,
        "action": action,
        "session_id": session_id,
        "ok": isinstance(result, dict) and "error" not in result,
    })
    return {"result": result, "skill_id": skill_id, "action": action}


def _m_tool_approve(server: AcpServer, params: dict[str, Any]) -> dict[str, Any]:
    return _m_approve(server, params)


# ----- event helpers ----------------------------------------------------


def _m_event_subscribe(server: AcpServer, params: dict[str, Any]) -> dict[str, Any]:
    kinds = params.get("kinds") or []
    if not isinstance(kinds, (list, tuple)):
        raise AcpError(-32602, "kinds must be a list")
    session_id = params.get("session_id")
    sub = server.events.subscribe(
        kinds=tuple(str(k) for k in kinds),
        session_id=str(session_id) if session_id else None,
        now_iso=now_iso,
    )
    return {
        "subscription_id": sub.id,
        "kinds": list(sub.kinds),
        "session_id": sub.session_filter,
    }


def _m_event_unsubscribe(server: AcpServer, params: dict[str, Any]) -> dict[str, Any]:
    sid = str(params.get("subscription_id") or "")
    if not sid:
        raise AcpError(-32602, "subscription_id required")
    ok = server.events.unsubscribe(sid)
    return {"ok": ok}


def _m_event_poll(server: AcpServer, params: dict[str, Any]) -> dict[str, Any]:
    sid = str(params.get("subscription_id") or "")
    if not sid:
        raise AcpError(-32602, "subscription_id required")
    max_items = int(params.get("max_items") or 64)
    try:
        events = server.events.drain(sid, max_items=max_items)
    except KeyError as exc:
        raise AcpError(-32004, str(exc)) from exc
    return {"events": events, "count": len(events)}


# --------------------------------------------------------------------- #
# Wire layer
# --------------------------------------------------------------------- #


def handle_request(server: AcpServer, doc: dict[str, Any]) -> dict[str, Any] | None:
    """Process one parsed JSON-RPC message, return the response envelope.

    Returns ``None`` for notifications (no ``id``).
    """
    rid = doc.get("id")
    is_notification = "id" not in doc
    method = doc.get("method")
    if not isinstance(method, str):
        return _jsonrpc_error(rid, -32600, "invalid request: missing method")
    params = doc.get("params") or {}
    if not isinstance(params, dict):
        return _jsonrpc_error(rid, -32602, "params must be an object")
    try:
        result = server.dispatch(method, params)
    except AcpError as e:
        return None if is_notification else _jsonrpc_error(rid, e.code, e.message, e.data)
    except Exception as e:  # pragma: no cover - defensive
        return None if is_notification else _jsonrpc_error(
            rid, -32603, f"internal error: {type(e).__name__}: {e}"
        )
    return None if is_notification else _jsonrpc_ok(rid, result)


def serve_stdio(server: AcpServer | None = None, *, stdin=None, stdout=None) -> None:
    """Read newline-delimited JSON-RPC messages from stdin, reply to stdout.

    The protocol is line-delimited JSON-RPC 2.0 — one message per line — which
    is simpler than LSP-style ``Content-Length:`` framing but fully
    interoperable for our use case.
    """
    stdin = stdin or sys.stdin
    stdout = stdout or sys.stdout
    server = server or AcpServer.boot()
    for line in stdin:
        line = line.strip()
        if not line:
            continue
        try:
            doc = json.loads(line)
        except Exception:
            stdout.write(json.dumps(_jsonrpc_error(None, -32700, "parse error")) + "\n")
            stdout.flush()
            continue
        resp = handle_request(server, doc)
        if resp is not None:
            stdout.write(json.dumps(resp) + "\n")
            stdout.flush()
