"use client";

import { useTranslations } from "next-intl";
import type { AssistantMessage } from "../../lib/chat";
import { finalReplyText } from "../../lib/chatResults";
import { Markdown } from "./Markdown";
import { AssistantTaskReceipt } from "./AssistantTaskReceipt";

/** Assistant chat publishes completed replies; execution stays in task chats. */
export function AssistantReplyBubble({ message, onRetry, onContinue }: {
  message: AssistantMessage; onRetry?: () => void; onContinue?: () => void;
}) {
  const t = useTranslations("assistants");
  const reply = finalReplyText(message);
  const stopped = ["cancelled", "interrupted", "stopped"].includes(message.execution_status || "");
  if (!reply && !message.loading && !message.error && !stopped && !message.assistant_task) return null;
  return <article tabIndex={-1} data-turn-role="assistant" data-turn-id={message.id}
    data-testid="assistant-reply-bubble" data-presentation="assistant-chat" data-command-id={message.command_id}
    data-turn-loading={message.loading ? "true" : "false"} className="my-4 min-w-0 outline-none">
    <div className="w-fit max-w-full rounded-2xl rounded-tl-sm bg-[color:var(--panel-bg)] px-4 py-3 text-sm leading-7">
      {reply ? <div data-turn-section="reply" data-find-text><Markdown>{reply}</Markdown></div>
        : message.loading ? <div role="status" aria-label={t("replying")} className="flex min-h-5 items-center gap-1.5">
          <span className="typing-dot"/><span className="typing-dot"/><span className="typing-dot"/>
        </div> : !message.assistant_task && <p role={message.error ? "alert" : "status"}>{t(message.error ? "replyFailed" : "replyStopped")}</p>}
      <AssistantTaskReceipt task={message.assistant_task} />
      {message.error && (onContinue || onRetry) ? <button type="button" className="mt-2 min-h-9 text-xs underline"
        onClick={onContinue || onRetry}>{t(onContinue ? "continueReply" : "retryReply")}</button> : null}
    </div>
  </article>;
}
