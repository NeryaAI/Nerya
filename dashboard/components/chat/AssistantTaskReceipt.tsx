"use client";

import Link from "next/link";
import { useTranslations } from "next-intl";
import type { AssistantMessage } from "../../lib/chat";

export function AssistantTaskReceipt({ task }: { task?: AssistantMessage["assistant_task"] }) {
  const t = useTranslations("assistants");
  if (!task) return null;
  return <div data-testid="assistant-task-receipt" className="mt-3 flex flex-wrap items-center gap-2 text-xs text-[color:var(--text-muted)]">
    {task.title && <span>{task.title}</span>}
    {t.has("taskStates." + task.state) && <span role="status">{t("taskStates." + task.state)}</span>}
    <Link className="inline-flex min-h-9 items-center underline" href={"/chat/" + encodeURIComponent(task.task_session_id)}>{t("openTaskConversation")}</Link>
  </div>;
}
