"""Operator continuation uses the same executor/approval boundary as a parent turn."""
from __future__ import annotations

import uuid
from typing import Any

from .threads import AgentThreadStore


def session_permission_mode(config: Any, session_id: str, actor_id: str):
    """Read a signed-in operator's latest accepted mode, not compacted prose.

    The parent conversation persists each send's permission mode in the
    command journal. Child Agent continuation previously reconstructed a new
    DEFAULT/AUTO kernel from nerya.yml and silently lost an explicit YOLO
    choice after the parent turn finished or its transcript was compacted.
    Never inherit a different actor's choice; no matching receipt means the
    configured default. Domain restrictions and saved child ceilings stay in
    force regardless of this native-tool convenience mode.
    """
    from ..tools.permissions import PermissionMode

    try:
        default = PermissionMode(str(config.get("runtime.permission_mode", "default")))
    except ValueError:
        default = PermissionMode.DEFAULT
    if not actor_id or not session_id:
        return default
    from ..db.sqlite import connect

    try:
        con = connect(config.paths.db)
        try:
            row = con.execute(
                "SELECT json_extract(request_json, '$.permission_mode') "
                "FROM agent_commands WHERE session_id=? AND actor_id=? "
                "AND kind IN ('send','resume') ORDER BY created_at DESC LIMIT 1",
                (session_id, actor_id),
            ).fetchone()
        finally:
            con.close()
    except Exception:
        return default
    requested = row[0] if row else None
    try:
        return PermissionMode(requested) if requested else default
    except ValueError:
        return default


def continue_agent(client: Any, *, session_id: str, agent_id: str,
                   message: str, request_id: str, actor_id: str = "") -> dict[str, Any]:
    from ..agent.kernel import AgentKernel
    from ..agent.hooks import _bind_config, _unbind_config
    from ..harness.cancellation import CancelToken, register_token, unregister_token
    from ..tools import NativeToolExecutor, PermissionEngine, PermissionContext
    from .permissions import child_permission_context
    from ..tools.tool_approvals import ToolApprovalCoordinator, ToolApprovalScope
    from .dispatcher import SubAgentDispatcher

    if not message.strip() or len(message) > 16000 or not request_id or len(request_id) > 120:
        raise ValueError("continuation requires a message and a request id")
    store = AgentThreadStore(client.config.paths)
    saved = store.load(agent_id, session_id)
    call_id = "resume:" + request_id
    if saved.get("last_resume_call_id") == call_id:
        return {"ok": True, "already_applied": True, "agent_id": agent_id, "state": saved["state"]}
    if saved["state"] == "running":
        raise ValueError("agent is already running; send a message instead")
    mode = session_permission_mode(client.config, session_id, actor_id)
    kernel = AgentKernel(config=client.config, skills=client.skills, permission_mode=mode)
    strategy_id = kernel._bind_session_strategy(
        session_id=session_id, requested_strategy_id=saved.get("strategy_id"))
    deps = kernel._ensure_registry()
    turn_id = "child_" + uuid.uuid4().hex
    deps.active_session_id = session_id
    deps.active_conversation_id = session_id
    deps.active_strategy_id = strategy_id
    deps.active_actor_id = actor_id or "operator"
    deps.active_trigger_source = "dashboard"
    deps.permission_mode = kernel.permission_mode.value
    from ..agent.assistants import current_assistant_policy
    from ..sdk.task_execution import current_task_policy
    current_policy = lambda: current_assistant_policy(
        client.config, session_id, current_task_policy(client.config))
    executor = NativeToolExecutor(
        registry=kernel.tool_registry,
        permission_engine=PermissionEngine(),
        permission_context=child_permission_context(
            client.config, parent=PermissionContext(mode=mode, current_tool_policy=current_policy),
            saved=saved.get("permission_ceiling"), strategy_id=strategy_id),
        approval_resolver=ToolApprovalCoordinator(
            client.config, scope=ToolApprovalScope.from_values(
                session_id=session_id, strategy_id=strategy_id, actor_id=actor_id or "operator"),
            turn_id=turn_id,
        ),
    )
    if kernel.ext_host is not None:
        bus = kernel.ext_host.bus
        if bus.has_listeners("tools/pre-execute"):
            executor.add_pre_hook(kernel.ext_host.tool_pre_hook())
        if bus.has_listeners("tools/post-execute"):
            executor.add_post_hook(kernel.ext_host.tool_post_hook())
    deps.executor = executor
    token = CancelToken()
    register_token(turn_id, token)
    _bind_config(turn_id, client.config)
    try:
        result = SubAgentDispatcher(config=client.config, skills=client.skills,
            tool_registry=kernel.tool_registry, executor=executor).dispatch(
                "subagent:" + saved["name"], payload={},
                session_id=session_id, strategy_id=strategy_id, turn_id=turn_id,
                parent_call_id=call_id, agent_id=agent_id,
                continuation_text=message.strip(), cancel_token=token,
            )
        return {"ok": bool(result.get("ok")), "agent_id": agent_id,
                "error": result.get("error"), "state": store.load(agent_id, session_id)["state"]}
    finally:
        unregister_token(turn_id)
        _unbind_config(turn_id)
