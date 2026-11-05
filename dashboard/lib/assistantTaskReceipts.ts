import type { AssistantMessage } from "./chat";
import { projectMessageBlocks } from "./streamTranscript";
import { contentValue, record } from "./agentConversation";
import { assistantDeliveryFields } from "./assistantTaskDelivery";

function admissionReceiptValues(message: AssistantMessage) {
  const values: Record<string, unknown>[] = [];
  for (const envelope of projectMessageBlocks(message)) {
    const block = record(envelope.block || envelope);
    if (block.kind !== "tool_result" || block.ok !== true) continue;
    const display = record(block.display_result);
    const parts = Array.isArray(display.content) ? display.content.map(record) : [];
    const value = record(contentValue(parts.find(part=>part.type==="json")?.data ?? block.result));
    if (block.action === "assistant_task_create" && value.protocol === "nerya.assistant_task.v2"
        && (value.operation === undefined || value.operation === "task_create")) values.push(value);
    if (block.action === "assistant_task_message" && verifiedFollowup(value, message)) values.push(value);
  }
  const turn = record(message.turn), publication = record(turn.publication);
  const receipt = record(turn.assistant_receipt);
  if (publication.source === "host" && publication.receipt_verified === true
      && (verifiedCreation(receipt, message) || verifiedFollowup(receipt, message))) {
    values.push(receipt);
  }
  return values.filter((value): value is Record<string, unknown> & {task_id: string; task_session_id: string} =>
    (value.protocol === "nerya.assistant_task.v2" || verifiedFollowup(value, message)) && value.ok === true
    && typeof value.task_id === "string" && /^awork_[a-zA-Z0-9_-]+$/.test(value.task_id)
    && typeof value.task_session_id === "string" && /^taskchat_[a-zA-Z0-9_-]+$/.test(value.task_session_id));
}

function verifiedCreation(value: Record<string, unknown>, message: AssistantMessage) {
  const binding = record(value.binding);
  return value.protocol === "nerya.assistant_task.v2" && value.verified === true && value.source === "assistant_work+agent_commands"
    && value.publication_protocol === "nerya.assistant_request.v1" && value.operation === "task_create"
    && value.admission_confirmed === true && typeof binding.command_id === "string"
    && (!message.command_id || binding.command_id === message.command_id);
}

function verifiedFollowup(value: Record<string, unknown>, message: AssistantMessage) {
  const binding = record(value.binding), turn = record(message.turn);
  const request = record(turn.assistant_request), requestBinding = record(request.binding);
  const fields = ["command_id", "session_id", "turn_id", "actor_id", "input_digest"];
  return value.protocol === "nerya.assistant_receipt.v1" && value.operation === "task_follow_up"
    && value.ok === true && value.verified === true && value.source === "assistant_work+agent_commands"
    && value.publication_protocol === "nerya.assistant_request.v1" && value.admission_confirmed === true
    && fields.every(key => typeof binding[key] === "string" && Boolean(binding[key]))
    && Boolean(message.command_id || turn.command_id)
    && (!message.command_id || binding.command_id === message.command_id)
    && (!turn.command_id || binding.command_id === turn.command_id)
    && binding.session_id === value.parent_session_id
    && (!turn.session_id || binding.session_id === turn.session_id)
    && (!turn.turn_id || binding.turn_id === turn.turn_id)
    && (!turn.assistant_request || (request.protocol === "nerya.assistant_request.v1"
      && request.disposition === "task_follow_up" && request.target_task_id === value.task_id
      && fields.every(key => binding[key] === requestBinding[key])))
    && typeof value.command_id === "string" && /^atmsg_[a-zA-Z0-9_-]+$/.test(value.command_id)
    && value.url === "/chat/" + value.task_session_id;
}

/** An admitted create/follow-up is navigable before the task completes. */
export function assistantTaskReceipts(message: AssistantMessage): NonNullable<AssistantMessage["assistant_task"]>[] {
  const tasks = new Map<string, NonNullable<AssistantMessage["assistant_task"]>>();
  for (const value of admissionReceiptValues(message)) {
    tasks.set(value.task_id, {task_id:value.task_id,task_session_id:value.task_session_id,
      title:typeof value.title === "string" ? value.title : "",state:typeof value.state === "string" ? value.state : "queued",
      // Verified admission publications carry the same delivery fields as results.
      ...(verifiedCreation(value, message) || verifiedFollowup(value, message)
        ? assistantDeliveryFields({...value, source: "assistant_task_result"}) : {})});
  }
  if (message.assistant_task) tasks.set(message.assistant_task.task_id,message.assistant_task);
  return [...tasks.values()];
}

/** Peel only the exact host admission prefix backed by its structured receipt.
 * null means ordinary/model prose; a string preserves any warnings or answer after the prefix. */
export function assistantCreationReply(message: AssistantMessage, reply: string): string | null {
  return assistantAdmissionReply(message, reply, "task_create");
}

export function assistantFollowupReply(message: AssistantMessage, reply: string): string | null {
  return assistantAdmissionReply(message, reply, "task_follow_up");
}

function assistantAdmissionReply(message: AssistantMessage, reply: string, operation: "task_create" | "task_follow_up"): string | null {
  if (message.assistant_task || (message.backend_message_id || message.id).startsWith("assistant_result:")) return null;
  for (const value of admissionReceiptValues(message)) {
    if (!(operation === "task_create" ? verifiedCreation(value, message) : verifiedFollowup(value, message))) continue;
    if (!reply) return "";
    if (typeof value.title !== "string" || typeof value.state !== "string"
        || typeof value.command_id !== "string" || typeof value.task_complete !== "boolean"
        || value.url !== "/chat/" + value.task_session_id) continue;
    const checks = record(value.verification).hard_status;
    if (typeof checks !== "string") continue;
    // Same literal escaping as the historical host renderer, never a prose regex.
    const title = value.title.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
      .replace(/([\\`*_{}\[\]()#!|~])/g, "\\$1");
    for (const zh of [false, true]) {
      let explanation = value.task_complete
        ? (zh ? "任务交付已完成（交付记录确认）。" : "Task delivery complete (confirmed by the delivery record).")
        : (zh ? "任务交付完成尚未确认。" : "Task delivery completion is not confirmed.");
      if (value.state === "succeeded") explanation = (zh ? "命令已成功结束；" : "The command turn finished successfully; ") + explanation;
      const headline = operation === "task_follow_up"
        ? (zh ? "补充请求已受理。" : "Follow-up request admitted.")
        : (zh ? "已创建任务并确认命令已受理。" : "Task created; command admission confirmed.");
      const prefix = headline
        + `\n\n[${title}](${value.url})\n\n${zh ? "状态" : "State"}: ` + "`" + value.state + "` · command: `" + value.command_id + "`"
        + `\n\n${explanation}` + (zh ? "\n\n已记录检查：" : "\n\nRecorded checks: ") + "`" + checks + "`";
      if (reply === prefix) return "";
      if (reply.startsWith(prefix + "\n\n")) return reply.slice(prefix.length + 2);
    }
  }
  return null;
}
