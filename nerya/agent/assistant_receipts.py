"""Host-owned publication receipts for the current assistant request.

Tool output is a hint, never evidence of admission or completion. Verify against
the durable request and command rows immediately before rendering/publication.
"""
from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from contextlib import closing
from urllib.parse import quote

from ..core.redaction import redact_text
from ..db.sqlite import connect_readonly
from ..memory.scope import memory_actor
from .assistant_coordination import command_verification, delivery_projection
from .assistants import AssistantError

PROTOCOL = "nerya.assistant_receipt.v1"
_BINDING_FIELDS = ("command_id", "session_id", "turn_id", "actor_id", "input_digest")
_OPERATIONS = {"create": "task_create", "read": "task_read", "list": "task_read",
               "followup": "task_follow_up", "follow_up": "task_follow_up",
               "task_followup": "task_follow_up"}


class AssistantReceiptError(AssistantError):
    def __init__(self, code: str, status: int = 409):
        super().__init__(code, status)


def _object(value):
    parsed = json.loads(value or "{}")
    if not isinstance(parsed, dict):
        raise AssistantReceiptError("assistant_receipt_invalid_record")
    return parsed


def _operation(value):
    if not isinstance(value, str):
        raise AssistantReceiptError("assistant_receipt_operation_mismatch")
    return _OPERATIONS.get(value, value)


def assistant_request_binding(config, con, *, operation, contract=None) -> dict:
    """Validate the server envelope; used by producers before recording binding."""
    from .assistant_intent import assistant_request_digest

    runtime_contract = config.get("runtime.assistant_request")
    contract = runtime_contract if contract is None else contract
    if (not isinstance(contract, dict) or contract.get("protocol") != "nerya.assistant_request.v1"
            or contract.get("status") != "ready" or contract != runtime_contract
            or _operation(contract.get("disposition")) != _operation(operation)
            or _operation(operation) not in {"task_create", "task_read", "task_follow_up"}):
        raise AssistantReceiptError("assistant_request_contract_mismatch")
    binding = contract.get("binding")
    if not isinstance(binding, dict) or any(not isinstance(binding.get(k), str) or not binding[k] for k in _BINDING_FIELDS):
        raise AssistantReceiptError("assistant_request_binding_mismatch")
    for key, runtime_key in (("command_id", "command_id"), ("session_id", "command_session_id"),
                             ("turn_id", "command_turn_id")):
        if binding[key] != config.get("runtime." + runtime_key):
            raise AssistantReceiptError("assistant_request_binding_mismatch")
    command = con.execute("SELECT * FROM agent_commands WHERE command_id=?", (binding["command_id"],)).fetchone()
    if command is None:
        raise AssistantReceiptError("assistant_request_command_missing")
    if any(command[k] != binding[k] for k in ("command_id", "session_id", "turn_id", "actor_id")):
        raise AssistantReceiptError("assistant_request_binding_mismatch")
    if command["state"] != "running" or command["kind"] == "guide":
        raise AssistantReceiptError("assistant_request_inactive")
    if con.execute("SELECT 1 FROM agent_deleted_sessions WHERE session_id=?", (binding["session_id"],)).fetchone():
        raise AssistantReceiptError("assistant_request_session_deleted")
    request = _object(command["request_json"])
    if (config.get("runtime.financial_actor_id") not in (None, binding["actor_id"])
            or request.get("_auth_actor_id") not in (None, "", binding["actor_id"])):
        raise AssistantReceiptError("assistant_request_binding_mismatch")
    if _object(command["context_json"]).get("assistant_request") != contract:
        raise AssistantReceiptError("assistant_request_contract_mismatch")
    if assistant_request_digest(request) != binding["input_digest"]:
        raise AssistantReceiptError("assistant_request_input_mismatch")
    return {key: binding[key] for key in _BINDING_FIELDS}


def _owned_work(con, binding, task_id):
    # Match read_task's legacy owner aliases; admission still requires the raw
    # command actor to match the task's persisted actor.
    row = con.execute("""SELECT w.* FROM assistant_work w
        JOIN assistant_sessions s ON s.session_id=w.parent_session_id AND s.assistant_id=w.assistant_id
        JOIN assistant_sessions c ON c.session_id=w.task_session_id AND c.assistant_id=w.assistant_id
        WHERE w.task_id=? AND w.parent_session_id=?
          AND NOT EXISTS(SELECT 1 FROM agent_deleted_sessions d
                         WHERE d.session_id IN (w.parent_session_id,w.task_session_id))
    """, (task_id, binding["session_id"])).fetchone()
    if row is None or memory_actor(row["actor_id"]) != memory_actor(binding["actor_id"]):
        raise AssistantReceiptError("assistant_receipt_owner_mismatch", 403)
    return row


def _admission(con, work, command_id):
    row = con.execute("SELECT * FROM agent_commands WHERE command_id=?", (command_id,)).fetchone()
    if row is None:
        raise AssistantReceiptError("assistant_receipt_admission_missing")
    if (row["session_id"] != work["task_session_id"] or row["actor_id"] != work["actor_id"]
            or row["kind"] == "guide"):
        raise AssistantReceiptError("assistant_receipt_admission_mismatch", 403)
    return row


def _projection(con, work, command, *, include_result=False):
    delivery = _object(command["delivery_json"])
    if delivery and (delivery.get("protocol") != "nerya.task_delivery.v1"
                     or delivery.get("command_id") != command["command_id"]):
        raise AssistantReceiptError("assistant_receipt_delivery_mismatch")
    publication = delivery_projection(con, work, command)
    from .assistant_coordination import task_command_projection
    snapshots = task_command_projection(con, work, command) if include_result else {}
    answer = _object(command["result_json"])
    text = answer.get("reply_text") or answer.get("final_text") or ""
    text = redact_text(text) if isinstance(text, str) else ""
    return {
        "task_id": work["task_id"], "title": redact_text(work["title"]),
        "parent_session_id": work["parent_session_id"], "task_session_id": work["task_session_id"],
        "assistant_id": work["assistant_id"], "command_id": command["command_id"],
        "command_revision": command["revision"], "state": command["state"],
        "admission_confirmed": True, **publication, **snapshots,
        "verification": command_verification(command),
        "limitations": [redact_text(x)[:256] for x in (delivery.get("limitations") or [])[:16] if isinstance(x, str)],
        "url": "/chat/" + quote(work["task_session_id"], safe=""),
        "reported_result": {"authority": "untrusted_model_output", "command_id": command["command_id"],
                            "text": text[:4000], "truncated": len(text) > 4000}
                           if include_result and text else None,
    }


def _current(con, work):
    row = con.execute("""SELECT command_id FROM agent_commands WHERE session_id=? AND kind!='guide'
        ORDER BY CASE WHEN state IN ('running','stopping','unconfirmed') THEN 0 ELSE 1 END,
                 created_at DESC,position DESC LIMIT 1""", (work["task_session_id"],)).fetchone()
    if row is None:
        raise AssistantReceiptError("assistant_receipt_admission_missing")
    return _admission(con, work, row["command_id"])


def _verify(config, contract, receipt, operation):
    operation = _operation(operation)
    with closing(connect_readonly(config.paths.db)) as con:
        con.execute("BEGIN")
        binding = assistant_request_binding(config, con, operation=operation, contract=contract)
        verified = {"protocol": "nerya.assistant_task.v2" if operation == "task_create" else PROTOCOL,
                    "publication_protocol": "nerya.assistant_request.v1", "operation": operation, "binding": binding,
                    "source": "assistant_work+agent_commands", "verified": True, "ok": True}
        if operation == "task_create":
            key = hashlib.sha256(json.dumps([binding["session_id"], binding["turn_id"],
                "assistant_request_" + binding["command_id"]]).encode()).hexdigest()[:24]
            work = _owned_work(con, binding, "awork_" + key)
            if (work["command_id"] != "acmd_" + key or work["task_session_id"] != "taskchat_" + key
                    or work["actor_id"] != binding["actor_id"]):
                raise AssistantReceiptError("assistant_receipt_admission_mismatch")
            saved = _object(work["request_json"])
            if saved.get("assistant_request_binding") != binding:
                raise AssistantReceiptError("assistant_receipt_binding_mismatch")
            command = _admission(con, work, work["command_id"])
            request = _object(command["request_json"])
            if (request.get("source") != "assistant_task" or saved.get("source") != "assistant_task"
                    or (request.get("payload") or {}).get("assistant_request_binding") != binding
                    or request.get("payload") != saved.get("payload")):
                raise AssistantReceiptError("assistant_receipt_binding_mismatch")
        elif operation == "task_follow_up":
            work = _owned_work(con, binding, contract.get("target_task_id"))
            request_id = "assistant_followup_" + binding["command_id"]
            digest = hashlib.sha256(json.dumps([binding["session_id"], work["task_id"], request_id]).encode()).hexdigest()[:32]
            command = _admission(con, work, "atmsg_" + digest)
            audit = con.execute("SELECT * FROM assistant_task_messages WHERE message_id=?", (request_id + ":" + digest,)).fetchone()
            request = _object(command["request_json"])
            payload = request.get("payload") or {}
            if (audit is None or audit["sender_session_id"] != binding["session_id"]
                    or audit["parent_session_id"] != work["parent_session_id"]
                    or audit["target_task_id"] != work["task_id"] or audit["target_session_id"] != work["task_session_id"]
                    or audit["command_id"] != command["command_id"] or audit["kind"] != "follow_up"
                    or audit["message"] != payload.get("text")
                    or request.get("source") != "assistant_task_message"
                    or request.get("assistant_request_binding") != binding
                    or payload.get("assistant_request_binding") != binding
                    or payload.get("task_message") != {"sender_session_id": binding["session_id"],
                        "target_task_id": work["task_id"], "request_id": request_id}):
                raise AssistantReceiptError("assistant_receipt_binding_mismatch")
        else:
            target = contract.get("target_task_id")
            if not target:
                rows = con.execute("SELECT task_id,actor_id FROM assistant_work WHERE parent_session_id=? ORDER BY created_at DESC,task_id",
                                   (binding["session_id"],)).fetchall()
                tasks = []
                for row in rows:
                    if memory_actor(row["actor_id"]) != memory_actor(binding["actor_id"]):
                        continue
                    # Deleted tasks are absent from the public list.
                    deleted = con.execute("SELECT 1 FROM assistant_work w JOIN agent_deleted_sessions d ON d.session_id=w.task_session_id WHERE w.task_id=?", (row["task_id"],)).fetchone()
                    if deleted:
                        continue
                    work = _owned_work(con, binding, row["task_id"])
                    tasks.append(_projection(con, work, _current(con, work)))
                return {**verified, "tasks": tasks, "total_tasks": len(tasks)}
            work = _owned_work(con, binding, target)
            command = _current(con, work)
        if not isinstance(receipt, dict):
            raise AssistantReceiptError("assistant_receipt_missing")
        expected = {"task_id": work["task_id"], "task_session_id": work["task_session_id"]}
        if operation != "task_read":
            expected["command_id"] = command["command_id"]
        if any(receipt.get(key) != value for key, value in expected.items()):
            raise AssistantReceiptError("assistant_receipt_identity_mismatch")
        return {**verified, **_projection(con, work, command, include_result=operation == "task_read")}


def verify_assistant_receipt(config, contract, receipt, operation: str) -> dict:
    """Re-read current durable authority; supplied ok/state/prose never grant it."""
    try:
        return _verify(config, contract, receipt, operation)
    except AssistantReceiptError:
        raise
    except AssistantError as exc:
        raise AssistantReceiptError(exc.code, exc.status) from exc
    except (sqlite3.Error, OSError, ImportError) as exc:
        raise AssistantReceiptError("assistant_receipt_unavailable", 503) from exc
    except (ValueError, TypeError, KeyError, AttributeError) as exc:
        raise AssistantReceiptError("assistant_receipt_invalid_record") from exc


def _literal(value):
    # Titles/quotes may contain Markdown/HTML; keep them inert public text.
    text = redact_text(str(value)).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    return re.sub(r"([\\`*_{}\[\]()#!|~])", r"\\\1", text)


def render_assistant_receipt(contract, verified) -> str:
    """Host copy only. Call only with a freshly verified receipt."""
    if (not isinstance(verified, dict) or verified.get("protocol") not in {PROTOCOL, "nerya.assistant_task.v2"}
            or verified.get("verified") is not True or verified.get("binding") != contract.get("binding")
            or verified.get("operation") != _operation(contract.get("disposition"))):
        raise AssistantReceiptError("assistant_receipt_not_verified")
    zh = str(contract.get("language", "en")).lower().startswith("zh")
    operation = verified["operation"]
    states = {"queued": ("排队中", "Queued"), "running": ("执行中", "Running"),
              "succeeded": ("本轮已结束", "Run ended"), "failed": ("执行失败", "Failed"),
              "blocked": ("受阻", "Blocked")}
    if "tasks" in verified:
        header = "当前任务：" if zh else "Current tasks:"
        if not verified["tasks"]:
            return "当前没有可见任务。" if zh else "No visible tasks."
        rows = [f"- [{_literal(row['title'])}]({row['url']}) — "
                + states.get(row["state"], (_literal(row["state"]),) * 2)[0 if zh else 1]
                for row in verified["tasks"]]
        return header + "\n\n" + "\n".join(rows) + "\n\n" + ("命令状态不代表任务交付完成。" if zh else "Command state does not establish task delivery completion.")
    headline = ({"task_create": "已创建任务并确认命令已受理。", "task_follow_up": "补充请求已受理。",
                 "task_read": "已读取任务当前状态。"} if zh else
                {"task_create": "Task created; command admission confirmed.", "task_follow_up": "Follow-up request admitted.",
                 "task_read": "Current task state retrieved."})[operation]
    state = verified["state"]
    explanation = ("任务交付已完成（交付记录确认）。" if zh else "Task delivery complete (confirmed by the delivery record).") if verified["task_complete"] else ("任务交付完成尚未确认。" if zh else "Task delivery completion is not confirmed.")
    if state == "succeeded":
        explanation = ("本轮已结束；" if zh else "The run ended; ") + explanation
    if operation == "task_read":
        state_label = states.get(state, (_literal(state),) * 2)[0 if zh else 1]
        checks = {"passed": ("已通过", "Passed"), "failed": ("未通过", "Failed"),
                  "missing": ("缺少验证证据", "Verification evidence missing")}
        evidence = verified["verification"]["hard_status"]
        check_label = checks.get(evidence, (_literal(evidence),) * 2)[0 if zh else 1]
        return (f"[{_literal(verified['title'])}]({verified['url']}) — {state_label}  \n{explanation}"
                + (" 检查概况：" if zh else " Recorded checks: ") + check_label + "  \n"
                + f"[{'查看任务与执行详情' if zh else 'View task and execution details'}]({verified['url']})")
    label = "状态" if zh else "State"
    text = f"{headline}\n\n[{_literal(verified['title'])}]({verified['url']})\n\n{label}: `{state}` · command: `{verified['command_id']}`\n\n{explanation}"
    evidence = verified["verification"]["hard_status"]
    text += ("\n\n已记录检查：" if zh else "\n\nRecorded checks: ") + f"`{evidence}`"
    if verified["limitations"]:
        text += ("\n\n限制：" if zh else "\n\nLimitations: ") + "; ".join(_literal(x) for x in verified["limitations"])
    return text
