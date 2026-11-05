"""Assistant coordination publication: original input in, durable receipt out.

Coordination actions do not enter the generative answer loop. The existing
executor still owns permission checks, and AssistantWork owns admission.
Only a separately verified receipt is published as confirmation.
"""
from __future__ import annotations

from contextlib import closing
import copy
import json
import re

from ..db.repositories import AgentSessionRepository
from ..db.sqlite import connect, connect_readonly
from ..tools import NativeToolExecutor, PermissionContext, PermissionEngine
from ..tools.capability_policy import normalise_tool_policy
from ..tools.result_contracts import tool_json_data
from ..tools.tool_approvals import ToolApprovalCoordinator, ToolApprovalScope
from ..tools.types import ToolCall
from .assistants import current_assistant_policy
from .session import SessionStore
from .streaming import get_default_bus


def _current(config, contract):
    binding = contract["binding"]
    with closing(connect_readonly(config.paths.db)) as con:
        row = con.execute(
            "SELECT state,session_id,turn_id,actor_id,context_json FROM agent_commands WHERE command_id=?",
            (binding["command_id"],),
        ).fetchone()
    if (not row or row["session_id"] != binding["session_id"]
            or row["turn_id"] != binding["turn_id"]
            or row["actor_id"] != binding["actor_id"]
            or json.loads(row["context_json"]).get("assistant_request") != contract):
        raise ValueError("assistant_request_binding_changed")
    return row["state"]


def _publish(config, contract, *, text, blocks, trace, stop="end_turn", receipt=None, error=None):
    binding = contract["binding"]
    sid, tid = binding["session_id"], binding["turn_id"]
    # Never turn a stop received during admission/verification into success.
    if _current(config, contract) != "running":
        stop, text = "cancelled", ""
    final = {"seq": len(blocks) + 1, "turn_id": tid, "message_id": tid + ":assistant",
             "role": "assistant", "block": {"kind": "text", "text": text,
             "stream_id": tid + ":receipt", "stream_mode": "final"}}
    blocks = [*blocks, final] if text else blocks
    result = {
        "turn_id": tid, "session_id": sid, "strategy_id": None,
        "command_id": binding["command_id"], "harness": "assistant_coordinator",
        "final_text": text, "reply_text": text, "stopped_reason": stop,
        "transition_reason": stop, "blocks": blocks, "steps": blocks,
        "tool_trace": trace, "actions": trace, "iterations": 0,
        "assistant_request": contract, "assistant_receipt": receipt,
        "publication": {"source": "host", "receipt_verified": receipt is not None,
                        "error_code": error},
        "budget": {"iterations": 0, "tool_calls": len(trace)},
    }
    # The first persisted answer is already safe, not a post-hoc rewrite.
    with closing(connect(config.paths.db)) as con:
        repo = AgentSessionRepository(con)
        repo.record_message(message_id=tid + ":assistant", session_id=sid, turn_id=tid,
                            role="assistant", content=text, meta={"turn": result,
                            "source_command_id": binding["command_id"]})
        con.commit()
    SessionStore(config.paths.root).append_turn(sid, tid)
    bus = get_default_bus()
    if text:
        bus.publish("message.delta", session_id=sid, turn_id=tid, text=text,
                    completed=True, mode="replace", stream_id=tid + ":receipt", stream_mode="final")
    bus.publish("turn.complete", session_id=sid, turn_id=tid, final_text=text,
                stop_reason=stop, transition_reason=stop, harness="assistant_coordinator")
    return result


def run_assistant_request(config, payload, contract, *, kernel):
    """Return a published coordination result, or None for ordinary conversation."""
    if contract is None or contract["disposition"] == "conversation":
        return None
    from .assistant_receipts import (
        AssistantReceiptError, render_assistant_receipt, verify_assistant_receipt,
    )
    binding = contract["binding"]
    sid, tid = binding["session_id"], binding["turn_id"]
    zh = contract.get("language") == "zh"
    if _current(config, contract) != "running":
        return _publish(config, contract, text="", blocks=[], trace=[], stop="cancelled")
    if contract["disposition"] == "clarify" or contract.get("status") == "clarify":
        text = ("这次请求的操作意图或目标任务尚未确认，未创建或修改任何任务。请说明要直接问答、创建新任务，还是继续哪一个现有任务。" if zh else
                "The requested action or target task is not confirmed. No task was created or changed. Please clarify whether to chat, create a new task, or continue a specific existing task.")
        return _publish(config, contract, text=text, blocks=[], trace=[], stop="assistant_intent_clarification")
    config.data.setdefault("runtime", {})["assistant_request"] = copy.deepcopy(contract)
    original = str((payload.get("payload") or {}).get("text") or "")
    disposition = contract["disposition"]
    if disposition == "task_create":
        name, args = "assistant_task_create", {"title": contract["title"], "prompt": original, "kind": "general"}
    elif disposition == "task_follow_up":
        name, args = "assistant_task_message", {"task_id": contract["target_task_id"],
            "message": original, "request_id": "assistant_followup_" + binding["command_id"], "mode": "follow_up"}
    else:
        name = "assistant_task_read" if contract.get("target_task_id") else "assistant_task_list"
        args = {"task_id": contract["target_task_id"]} if contract.get("target_task_id") else {}
    registry, deps = kernel.prepare_tools()
    deps.config = config
    deps.active_session_id, deps.active_conversation_id = sid, sid
    deps.active_actor_id = binding["actor_id"]
    deps.active_strategy_id = None
    policy = normalise_tool_policy(config.get("agent.native.tool_policy"))
    permission = PermissionContext(mode=kernel.permission_mode, tool_policy=policy,
        plan_only=bool(config.get("agent.native.plan_only", False)))
    permission.current_tool_policy = lambda: current_assistant_policy(config, sid, policy)
    approval = ToolApprovalCoordinator(config, scope=ToolApprovalScope.from_values(
        session_id=sid, strategy_id=None, actor_id=binding["actor_id"]), turn_id=tid)
    executor = NativeToolExecutor(registry=registry, permission_engine=PermissionEngine(),
                                  permission_context=permission, approval_resolver=approval)
    kernel.bind_tool_executor(executor)
    call = ToolCall(id="assistant_request_" + binding["command_id"], turn_id=tid,
                    name=name, arguments=args, caller="assistant:coordinator",
                    metadata={"cancel_token": payload.get("_task_cancel_token")})
    bus = get_default_bus()
    common = {"session_id": sid, "turn_id": tid, "call_id": call.id,
              "tool_call_id": call.id, "skill_id": "native", "action": name}
    bus.publish("tool.start", **common, payload=args, caller=call.caller)
    result = executor.execute(call)
    receipt, error = None, None
    stop = "end_turn"
    try:
        if result.is_error:
            code = result.error.message if result.error else ""
            error = code if re.fullmatch(r"[a-z][a-z0-9_]{0,100}", code) else "assistant_task_action_failed"
            stop = "awaiting_approval" if result.error and result.error.kind.value == "permission_pending" else "assistant_task_action_failed"
            raise AssistantReceiptError("assistant_task_action_failed")
        receipt = verify_assistant_receipt(config, contract, tool_json_data(result), operation=disposition)
        text = render_assistant_receipt(contract, receipt)
    except AssistantReceiptError as exc:
        error = error or exc.code
        if stop == "end_turn":
            stop = exc.code
        text = ("未获得本次请求的可靠回执，不能确认任务已创建或指令已送达。请先核对原请求，不要重复提交。" if zh else
                "No reliable receipt was obtained for this request. Task creation or instruction delivery is not confirmed. Reconcile the original request before retrying.")
        known_rejections = {
            "assistant_task_paused": ("目标任务的队列已暂停，本条指令未发送。请先在任务中处理暂停状态。",
                                      "The target task queue is paused. This instruction was not sent; review the pause in the task conversation."),
            "execution_unconfirmed": ("目标任务的执行状态尚未确认，本条指令未发送。请先核对原执行的回执。",
                                      "The target execution remains unconfirmed. This instruction was not sent; reconcile the original execution first."),
            "assistant_task_decision_pending": ("目标任务正在等待回答或审批，本条指令未发送。请先处理原决策。",
                                                "The target task is waiting for an answer or approval. This instruction was not sent; resolve that decision first."),
        }
        if error in known_rejections:
            text = known_rejections[error][0 if zh else 1]
    # Publish sanitized, independently verified producer data, never arbitrary
    # result prose from a plugin or a model pretending to return a receipt.
    public_data = receipt or {"ok": False, "error_code": error}
    trace = [{"action": name, "skill_id": "native", "call_id": call.id,
              "payload": args, "ok": receipt is not None, "result": public_data, "error": error}]
    blocks = [{"seq": 1, "turn_id": tid, "message_id": tid + ":assistant", "role": "assistant",
               "block": {"kind": "tool_use", "call_id": call.id, "action": name, "skill_id": "native", "payload": args}},
              {"seq": 2, "turn_id": tid, "message_id": tid + ":assistant", "role": "tool",
               "block": {"kind": "tool_result", "call_id": call.id, "action": name, "skill_id": "native",
                         "ok": receipt is not None, "result": public_data, "error": error}}]
    if stop == "awaiting_approval" and result.error:
        pending = (result.error.recovery_hint or {}).get("approval_request")
        if isinstance(pending, dict):
            blocks.append({"seq": 3, "turn_id": tid, "message_id": tid + ":assistant",
                           "role": "assistant", "block": {"kind": "approval_request", **pending}})
    bus.publish("tool.complete", **common, payload=args, ok=receipt is not None,
                result=public_data, error=error, result_protocol="nerya.assistant_request.v1")
    return _publish(config, contract, text=text, blocks=blocks, trace=trace,
                    stop=stop, receipt=receipt, error=error)
