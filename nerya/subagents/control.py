"""Operator continuation uses the same executor/approval boundary as a parent turn."""
from __future__ import annotations

import uuid
from typing import Any

from .threads import AgentThreadStore


def continue_agent(client: Any, *, session_id: str, agent_id: str,
                   message: str, request_id: str) -> dict[str, Any]:
    from ..agent.kernel import AgentKernel
    from ..agent.hooks import _bind_config, _unbind_config
    from ..harness.cancellation import CancelToken, register_token, unregister_token
    from ..tools import NativeToolExecutor, PermissionContext, PermissionEngine
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
    kernel = AgentKernel(config=client.config, skills=client.skills)
    strategy_id = kernel._bind_session_strategy(
        session_id=session_id, requested_strategy_id=saved.get("strategy_id"))
    deps = kernel._ensure_registry()
    turn_id = "child_" + uuid.uuid4().hex
    deps.active_session_id = session_id
    deps.active_conversation_id = session_id
    deps.active_strategy_id = strategy_id
    deps.active_actor_id = "operator"
    deps.active_trigger_source = "dashboard"
    deps.permission_mode = kernel.permission_mode.value
    executor = NativeToolExecutor(
        registry=kernel.tool_registry,
        permission_engine=PermissionEngine(),
        permission_context=PermissionContext(mode=kernel.permission_mode),
        approval_resolver=ToolApprovalCoordinator(
            client.config, scope=ToolApprovalScope.from_values(
                session_id=session_id, strategy_id=strategy_id, actor_id="operator"),
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
