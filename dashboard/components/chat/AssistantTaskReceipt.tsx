"use client";

import Link from "next/link";
import { useTranslations } from "next-intl";
import type { AssistantMessage, TaskDelivery } from "../../lib/chat";

export type AssistantDeliveryEvidence = Pick<NonNullable<AssistantMessage["assistant_task"]>,
  "state" | "task_complete" | "delivery_completion" | "unresolved_deliveries"> & {delivery?: TaskDelivery | null};

/** Completion comes from host records, never from execution success or prose. */
export function assistantDeliveryStatus(task: AssistantDeliveryEvidence) {
  const completion = task.delivery?.completion || task.delivery_completion;
  const unresolved = task.unresolved_deliveries || [];
  if (task.state === "succeeded" && completion?.allowed === true) {
    if (completion.status === "complete" && task.task_complete !== false && !unresolved.length) return "complete";
    if (completion.status === "partial"
        && !unresolved.some(row => row.command_id !== task.delivery?.command_id)) return "partial";
  }
  if (unresolved.length || completion && !["unavailable", "unconfirmed"].includes(completion.status)) return "incomplete";
  if (completion || task.task_complete !== undefined || ["succeeded", "failed", "blocked", "interrupted", "removed", "cancelled", "unconfirmed"].includes(task.state)) return "unconfirmed";
  return null;
}

export function AssistantDeliveryStatus({task}: {task: AssistantDeliveryEvidence}) {
  const t = useTranslations("assistants");
  const status = assistantDeliveryStatus(task);
  return status ? <span role="status" data-testid="assistant-delivery-status" data-delivery-state={status}
    className={status === "complete" ? "text-ok" : "text-warn"}>{t("deliveryStates." + status)}</span> : null;
}

export function AssistantTaskReceipt({ task }: { task?: AssistantMessage["assistant_task"] }) {
  const t = useTranslations("assistants");
  if (!task) return null;
  return <div data-testid="assistant-task-receipt" className="mt-3 flex flex-wrap items-center gap-2 text-xs text-[color:var(--text-muted)]">
    {task.title && <span>{task.title}</span>}
    {t.has("taskStates." + task.state) && <span role="status">{t("taskStates." + task.state)}</span>}
    <AssistantDeliveryStatus task={task}/>
    <Link className="inline-flex min-h-9 items-center underline" href={"/chat/" + encodeURIComponent(task.task_session_id)}>{t("openTaskConversation")}</Link>
  </div>;
}
