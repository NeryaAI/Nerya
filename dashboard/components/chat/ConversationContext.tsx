"use client";

import { copy as i18nCopy } from "../../lib/i18n";

import * as Dialog from "@radix-ui/react-dialog";
import {InfoIcon,XIcon} from "../icons";
import { useState } from "react";
import { useLocale } from "next-intl";
import type { AssistantMessage, ChatAttachment } from "../../lib/chat";
import { ReferenceSnapshot } from "./ReferenceSnapshot";

const object = (value: unknown): Record<string, unknown> => value && typeof value === "object" && !Array.isArray(value) ? value as Record<string, unknown> : {};
const number = (value: unknown) => typeof value === "number" && Number.isFinite(value) && value >= 0 ? value : null;
export function ConversationContext({ message }: { message: AssistantMessage }) {
  const zh = useLocale().startsWith("zh"), locale = zh ? "zh-CN" : "en-US";
  const [open, setOpen] = useState(false);
  const context = object(message.turn?.context_snapshot), budget = object(message.turn?.budget);
  const requested = object(context.requested_model), accepted = object(context.accepted_model), strategy = object(context.strategy_source);
  const response = object(context.interaction_response);
  const memory = object(budget.memory_usage);
  const memories = Array.isArray(memory.included) ? memory.included.map(object) : [];
  const omittedMemories = Array.isArray(memory.omitted) ? memory.omitted.length : 0;
  const attachmentReceipts = (message.turn?.attachments || []).filter(item => typeof item.model_sent === "boolean");
  const original = typeof context.input_text === "string" ? context.input_text : "";
  const attachments = Array.isArray(context.attachments) ? context.attachments.map(object) : [];
  const format = (value: unknown) => number(value) === null ? (i18nCopy(zh, "copy.components_chat_ConversationContext.001")) : new Intl.NumberFormat(locale).format(Number(value));
  if (!Object.keys(context).length && !Object.keys(budget).length && !attachmentReceipts.length) return null;
  return <Dialog.Root open={open} onOpenChange={setOpen}><Dialog.Trigger asChild><button type="button" aria-label={i18nCopy(zh, "copy.components_chat_ConversationContext.002")} title={i18nCopy(zh, "copy.components_chat_ConversationContext.003")} className="inline-flex h-8 w-8 items-center justify-center rounded text-[color:var(--text-muted)] hover:bg-[color:var(--panel-bg)]" data-testid="turn-context"><InfoIcon size={14}/></button></Dialog.Trigger><Dialog.Portal><Dialog.Overlay className="ui-modal-overlay"/><Dialog.Content className="ui-dialog max-h-[80vh] overflow-y-auto" aria-describedby={undefined}><div className="mb-3 flex items-center justify-between gap-3"><Dialog.Title className="text-sm font-semibold">{i18nCopy(zh, "copy.components_chat_ConversationContext.004")}</Dialog.Title><Dialog.Close className="ui-icon-button" aria-label={i18nCopy(zh, "copy.components_chat_ConversationContext.005")}><XIcon size={16}/></Dialog.Close></div>
    {open && <div className="space-y-3 border-t border-[color:var(--line)] py-3">
      {Object.keys(response).length > 0 && <div className="rounded border border-[color:var(--line)] p-3 text-sm"><p>{String(response.title || (i18nCopy(zh, "copy.components_chat_ConversationContext.006")))}</p><p className="mt-1 text-[color:var(--text-muted)]">{response.action === "revise" ? (i18nCopy(zh, "copy.components_chat_ConversationContext.007")) : response.action === "accept" || response.action === "answer" ? (i18nCopy(zh, "copy.components_chat_ConversationContext.008")) : String(response.action || "")}</p>{typeof response.text === "string" && response.text && <p className="mt-2 whitespace-pre-wrap">{response.text}</p>}</div>}
      <dl className="grid grid-cols-[minmax(100px,auto)_minmax(0,1fr)] gap-x-4 gap-y-2">
        <dt>{i18nCopy(zh, "copy.components_chat_ConversationContext.009")}</dt><dd className="break-words">{[accepted.provider, accepted.model].filter(value => typeof value === "string" && value).join(" · ") || (i18nCopy(zh, "copy.components_chat_ConversationContext.010"))}</dd>
        <dt>{i18nCopy(zh, "copy.components_chat_ConversationContext.011")}</dt><dd className="break-words">{[budget.reported_provider,budget.reported_model].filter(value => typeof value === "string" && value).join(" · ") || (i18nCopy(zh, "copy.components_chat_ConversationContext.012"))}</dd>
        <dt>{i18nCopy(zh, "copy.components_chat_ConversationContext.013")}</dt><dd>{format(requested.model_context_window)}</dd>
        <dt>{i18nCopy(zh, "copy.components_chat_ConversationContext.014")}</dt><dd>{format(budget.context_window)}</dd>
        <dt>{i18nCopy(zh, "copy.components_chat_ConversationContext.015")}</dt><dd>{format(budget.prompt_tokens_last)}</dd>
        <dt>{i18nCopy(zh, "copy.components_chat_ConversationContext.016")}</dt><dd>{format(budget.input_tokens_total)} / {format(budget.output_tokens_total)}</dd>
        <dt>{i18nCopy(zh, "copy.components_chat_ConversationContext.017")}</dt><dd>{format(budget.compaction_count)}</dd>
        {Boolean(context.strategy_id) && <><dt>{i18nCopy(zh, "copy.components_chat_ConversationContext.018")}</dt><dd className="break-all font-mono">{String(context.strategy_id)}</dd><dt>{i18nCopy(zh, "copy.components_chat_ConversationContext.019")}</dt><dd className="break-all">{String(context.proposal_id || (i18nCopy(zh, "copy.components_chat_ConversationContext.020")))}</dd></>}
        {Boolean(strategy.revision) && <><dt>{i18nCopy(zh, "copy.components_chat_ConversationContext.021")}</dt><dd className="break-all font-mono">{String(strategy.revision)}</dd></>}
      </dl>
      {attachmentReceipts.length > 0 && <section><h3 className="text-sm font-medium">{i18nCopy(zh, "copy.components_chat_ConversationContext.022")}</h3>{attachmentReceipts.map(item => <div key={item.id} className="mt-2 text-xs"><span>{item.name}</span><span className={item.model_sent ? "ml-2 text-[color:var(--text-muted)]" : "ml-2 text-warn"}>{item.model_sent ? (i18nCopy(zh, "copy.components_chat_ConversationContext.023")) : (i18nCopy(zh, "copy.components_chat_ConversationContext.024"))}</span>{!item.model_sent && item.reason && <p className="mt-1 break-words text-[color:var(--text-muted)]">{item.reason}</p>}</div>)}<p className="mt-2 text-xs text-[color:var(--text-muted)]">{i18nCopy(zh, "copy.components_chat_ConversationContext.025")}</p></section>}
      {Object.keys(memory).length > 0 && <details><summary className="cursor-pointer py-2">{i18nCopy(zh, "copy.components_chat_ConversationContext.026")} · {memories.length}</summary>
        {memory.reason === "checkpoint_context_reused" ? <p>{i18nCopy(zh, "copy.components_chat_ConversationContext.027")}</p> : <>
          <p>{i18nCopy(zh, "copy.components_chat_ConversationContext.028")}</p>
          {memories.map((item,index) => <div key={String(item.memory_id || index)} className="mt-2 border-l border-[color:var(--line)] pl-3"><p className="break-words">{String(item.stable_key || item.category || item.kind || "Memory")}</p><p className="break-all text-xs text-[color:var(--text-muted)]">{String(item.source_ref || (i18nCopy(zh, "copy.components_chat_ConversationContext.029")))} · {String(item.version || "").slice(0,12)}</p></div>)}
          {omittedMemories > 0 && <p className="mt-2 text-xs">{i18nCopy(zh, "copy.components_chat_ConversationContext.030")}{omittedMemories}</p>}
          {!memories.length && <p>{memory.reason === "use_disabled" ? (i18nCopy(zh, "copy.components_chat_ConversationContext.031")) : (i18nCopy(zh, "copy.components_chat_ConversationContext.032"))}</p>}
        </>}
      </details>}
      <p>{i18nCopy(zh, "copy.components_chat_ConversationContext.033")}</p>
      {attachments.length > 0 && <div className="flex flex-wrap gap-2">{attachments.map((item,index) => <ReferenceSnapshot key={String(item.id || index)}
        attachment={{ name:String(item.name || "Reference"), artifact_uri:typeof item.artifact_uri === "string" ? item.artifact_uri : undefined, reference:item.reference as ChatAttachment["reference"] }}
        className="rounded-md border border-[color:var(--line)] px-2 py-1.5 text-left hover:bg-[color:var(--card)]" />)}</div>}
      {original && <details><summary className="cursor-pointer py-1">{i18nCopy(zh, "copy.components_chat_ConversationContext.034")}</summary><p className="max-h-40 overflow-auto whitespace-pre-wrap break-words py-2">{original}</p></details>}
    </div>}
  </Dialog.Content></Dialog.Portal></Dialog.Root>;
}
