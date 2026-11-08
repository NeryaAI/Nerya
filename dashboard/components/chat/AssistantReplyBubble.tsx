"use client";

import { useTranslations } from "next-intl";
import type { AssistantMessage } from "../../lib/chat";
import { finalReplyText, publicReplyText, legacyReplyDiagnostic } from "../../lib/chatResults";
import { ConversationContext } from "./ConversationContext";
import { projectMessageBlocks } from "../../lib/streamTranscript";
import { contentValue, record } from "../../lib/agentConversation";
import { Markdown } from "./Markdown";
import { AssistantTaskReceipt, assistantDeliveryStatus } from "./AssistantTaskReceipt";
import { assistantCreationReply, assistantFollowupReply, assistantTaskReceipts } from "../../lib/assistantTaskReceipts";
import type { AssistantTask } from "./useAssistantTasks";
import { assistantDeliveryFields } from "../../lib/assistantTaskDelivery";

/** Assistant chat publishes completed replies; execution stays in task chats. */
export function AssistantReplyBubble({ message, onRetry, onContinue, tasks = [] }: {
  message: AssistantMessage; onRetry?: () => void; onContinue?: () => void; tasks?: AssistantTask[];
}) {
  const t = useTranslations("assistants");
  const originalReply = finalReplyText(message) || publicReplyText(message, projectMessageBlocks(message));
  const creationReply = assistantCreationReply(message, originalReply);
  const followupReply = assistantFollowupReply(message, originalReply);
  const admissionReply = creationReply ?? followupReply;
  const reply = admissionReply ?? originalReply;
  const stopped = ["cancelled", "interrupted", "stopped"].includes(message.execution_status || "");
  const resultId = message.backend_message_id || message.id;
  const historical = Boolean(message.assistant_task) || resultId.startsWith("assistant_result:");
  // Recover child command bindings only from publications the existing host
  // admission verifier accepts, never from reply/model delivery fields.
  const publications = historical ? [] : [{...message, live_events: [], turn: {...message.turn, blocks: []}},
    ...projectMessageBlocks(message).map(envelope => ({
    ...message, live_events: [], turn: {...message.turn, assistant_receipt: undefined, blocks: [envelope]},
  }))].flatMap(scoped => {
    if (assistantCreationReply(scoped, "") === null && assistantFollowupReply(scoped, "") === null) return [];
    const block = record(scoped.turn?.blocks?.[0]?.block || scoped.turn?.blocks?.[0]);
    const parts = record(block.display_result).content;
    const json = Array.isArray(parts) ? parts.map(record).find(part => part.type === "json")?.data : undefined;
    return [record(record(scoped.turn).assistant_receipt ?? contentValue(json ?? block.result))];
  });
  const receipts = assistantTaskReceipts(message).map(receipt => {
    const resultCommand = resultId.startsWith("assistant_result:") ? resultId.slice("assistant_result:".length) : undefined;
    const commandId = historical ? resultCommand || message.command_id : publications.find(value =>
      value.task_id === receipt.task_id && value.task_session_id === receipt.task_session_id)?.command_id;
    const live = tasks.find(row => row.task_id === receipt.task_id && row.task_session_id === receipt.task_session_id);
    if (!live || resultCommand && message.command_id && resultCommand !== message.command_id) return receipt;
    if (typeof commandId === "string" && commandId && live.command_id === commandId) {
      return {...receipt, state: live.state, ...(live.delivery_completion ? {
        task_complete: live.task_complete ?? receipt.task_complete, delivery_completion: live.delivery_completion,
        unresolved_deliveries: live.unresolved_deliveries ?? receipt.unresolved_deliveries,
      } : {})};
    }
    // Legacy polling rows can update admission activity, but supply no new
    // delivery authority and cannot replace an archived command's evidence.
    return !historical && !live.command_id ? {...receipt, state: live.state} : receipt;
  }).map(receipt => ({...receipt, ...assistantDeliveryFields({...receipt, source: "assistant_task_result"})}));
  if (!reply && !message.loading && !message.error && !stopped && !receipts.length) return null;
  return <article tabIndex={-1} data-turn-role="assistant" data-turn-id={message.id}
    data-testid="assistant-reply-bubble" data-presentation="assistant-chat" data-command-id={message.command_id}
    data-turn-loading={message.loading ? "true" : "false"} className="my-4 min-w-0 outline-none">
    <div className="w-fit max-w-full rounded-2xl rounded-tl-sm bg-[color:var(--panel-bg)] px-4 py-3 text-sm leading-7">
      {receipts.filter(receipt => receipt.unresolved_deliveries?.length
        || receipt.task_complete === false && assistantDeliveryStatus(receipt) !== "partial").map(receipt =>
        <div key={`delivery:${receipt.task_id}`} role="status" data-testid="assistant-delivery-warning" className="mb-3 text-xs text-warn">
          <p>{t(admissionReply !== null ? "creationDeliveryUnconfirmed" : "deliveryUnconfirmed")}</p>
          {Boolean(receipt.unresolved_deliveries?.length) && <details className="mt-1">
            <summary>{t("earlierDeliveryUnresolved", { count: receipt.unresolved_deliveries!.length })}</summary>
            <ul className="mt-1 list-inside list-disc">{receipt.unresolved_deliveries!.flatMap(row => row.reasons.map(reason =>
              <li key={`${row.command_id}:${reason}`}><code className="break-all">{reason}</code></li>))}</ul>
          </details>}
        </div>)}
      {creationReply !== null && <p data-testid="assistant-task-created">{t("taskCreated")}</p>}
      {followupReply !== null && <p data-testid="assistant-task-followup-admitted">{t("taskFollowupAdmitted")}</p>}
      {reply ? <div data-turn-section="reply" data-find-text aria-busy={Boolean(message.loading)}><Markdown streaming={Boolean(message.loading)}>{reply}</Markdown></div>
        : admissionReply !== null ? null
        : message.loading ? <div role="status" aria-label={t("replying")} className="flex min-h-5 items-center gap-1.5">
          <span className="typing-dot"/><span className="typing-dot"/><span className="typing-dot"/>
        </div> : !receipts.length && <p role={message.error ? "alert" : "status"}>{t(message.error ? "replyFailed" : "replyStopped")}</p>}
      {receipts.map(receipt => <AssistantTaskReceipt key={receipt.task_id} task={receipt} />)}
      {legacyReplyDiagnostic(message) && <ConversationContext message={message} />}
      {message.error && (onContinue || onRetry) ? <button type="button" className="mt-2 min-h-9 text-xs underline"
        onClick={onContinue || onRetry}>{t(onContinue ? "continueReply" : "retryReply")}</button> : null}
    </div>
  </article>;
}
