"use client";
import { ContextCompactionStatus } from "./ContextCompactionStatus";
import { Icon as NeryaGlyph } from "../icons";
import { copy as i18nCopy } from "../../lib/i18n";

import { type ReactNode, useState, useId, useRef, useMemo } from "react";
import { useLocale, useTranslations } from "next-intl";
import type {
  AssistantMessage,
  ChatAttachment,
  UserMessage,
} from "../../lib/chat";
import { liveEventsToBlocks } from "../../lib/chat";
import { mergeStreamEvents } from "../../lib/streamEvents";
import { normalizeTranscriptBlocks } from "../../lib/transcriptProjection";
import { finalReplyText, publicReplyText } from "../../lib/chatResults";
import type { ApprovalCard } from "../../lib/clientApi";
import {
  formatDuration,
  NativeBlocksTrack,
  StrategyProposalsHoist,
  StreamedMarkdown,
  activeProposalsFromTurn,
} from "./TurnBlocks";
import { formatTime as formatTimeWithTz } from "../../lib/format";
import {
  CheckIcon,
  CopyIcon,
  EditIcon,
  FileIcon,
  TrashIcon,
  XIcon,
} from "../icons";
import { RoleAvatar } from "../RoleAvatar";
import { toolPresentation } from "../../lib/agentConversation";
import { ExecutionTimeline } from "./ExecutionTimeline";
import { executionSteps, executionMembers, executionArtifacts, executionDefaultOpen, partitionTranscript, enrichCommittedBlocks } from "../../lib/executionTimeline";
import { browserCalls } from '../../lib/browserTrace';
import { ResearchReplyCards } from './ResearchReplyCards';
import { BacktestReplyCards } from './BacktestReplyCards';
import { latestModelRetry } from '../../lib/modelRetry';
import { ErrorCard } from './RecoveryErrorCard';
import { ConversationContext } from './ConversationContext';
import { ReferenceSnapshot } from './ReferenceSnapshot';
import { commandStateText } from '../../lib/commandCopy';
import { ModelRetryStatus } from './ModelRetryStatus';
import { interactionReceipt } from '../../lib/toolOutputPresentation';
import { InteractionReceipt } from './InteractionReceipt';
import { AssistantTaskReceipt } from './AssistantTaskReceipt';

function formatTime(ts: number): string {
  try {
    return formatTimeWithTz(ts).slice(0, 5);
  } catch {
    return "";
  }
}


function mergeActivityEvents(
  persisted: AssistantMessage["live_events"] = [],
  live: AssistantMessage["live_events"] = [],
): NonNullable<AssistantMessage["live_events"]> {
  return mergeStreamEvents(persisted, live);
}

function StreamingDots() {
  return (
    <span className="inline-flex items-center gap-1">
      <span className="typing-dot" />
      <span className="typing-dot" />
      <span className="typing-dot" />
    </span>
  );
}

// Shared "Nerya is speaking" row: avatar + name + optional streaming /
// elapsed chrome, wrapping a single ``bubble-ai`` body. Used by the
// multi-bubble team layouts (live + committed) so every Nerya turn reads
// like a distinct speaker in the thread.
function NeryaSpeaker({
  streaming = false,
  elapsedMs,
  children,
  footer,
}: {
  streaming?: boolean;
  elapsedMs?: number | null;
  children: ReactNode;
  footer?: ReactNode;
}) {
  return (
    <div className="flex justify-start">
      <div className="group max-w-[92%] min-w-[200px] w-full">
        <div className="flex items-center gap-2 mb-1.5">
          <div className="relative h-8 w-8 shrink-0">
            {streaming ? (
              <span
                className="absolute -inset-0.5 rounded-full ring-ai opacity-80 animate-spin"
                style={{ animationDuration: "8s" }}
              />
            ) : null}
            <div className="relative h-8 w-8 rounded-full overflow-hidden ring-1 ring-brand-500/40 shadow-glow bg-black/20 flex items-center justify-center">
              <RoleAvatar role="lead" size={32} alt="" />
            </div>
            <span className="absolute -bottom-0.5 -right-0.5 w-2.5 h-2.5 rounded-full bg-accent-500 ring-2 ring-[var(--bg-deep)]" />
          </div>
          <div className="text-[12px] text-ink-200 font-semibold tracking-tight">
            Nerya
          </div>
          {streaming ? (
            <div className="flex items-center gap-1.5 text-[10px] text-fluid-400">
              <StreamingDots />
              <span className="text-ink-400">thinking…</span>
            </div>
          ) : null}
          {elapsedMs ? (
            <div className="text-[10px] text-ink-500 font-mono">
              {formatDuration(elapsedMs)}
            </div>
          ) : null}
        </div>
        <div className="bubble-ai space-y-2">{children}</div>
        {footer}
      </div>
    </div>
  );
}

function AnswerPanel({ children }: { children: ReactNode }) {
  // ``bubble-ai`` already draws the single bubble chrome (border + bg);
  // wrapping the reply in another bordered panel produced a
  // bubble-in-bubble, so this is now just a text container.
  return <div className="leading-relaxed text-ink-100">{children}</div>;
}

function CopyButton({ text }: { text: string }) {
  const [copied, setCopied] = useState(false);
  const t = useTranslations("chat");
  if (!text) return null;
  return (
    <button
      onClick={async () => {
        try {
          await navigator.clipboard.writeText(text);
          setCopied(true);
          window.setTimeout(() => setCopied(false), 1200);
        } catch {
          // Clipboard can be blocked in non-secure contexts.
        }
      }}
      className="inline-flex h-7 w-7 items-center justify-center rounded-md border border-brand-500/20 text-ink-400 hover:text-white hover:border-brand-500/40 transition-colors"
      title={t("copyMessage")}
      aria-label={copied ? t("copied") : t("copyMessage")}
    >
      {copied ? <CheckIcon size={14} /> : <CopyIcon size={14} />}
    </button>
  );
}

function IconButton({
  label,
  children,
  tone = "neutral",
  onClick,
  type = "button",
  disabled = false,
}: {
  label: string;
  children: ReactNode;
  tone?: "neutral" | "danger" | "primary";
  onClick?: () => void;
  type?: "button" | "submit";
  disabled?: boolean;
}) {
  const toneClass =
    tone === "danger"
      ? "hover:text-danger hover:border-danger/40"
      : tone === "primary"
      ? "text-accent-300 border-accent-400/40 bg-accent-400/10 hover:bg-accent-400/20"
      : "hover:text-white hover:border-brand-500/40";
  return (
    <button
      type={type}
      onClick={onClick}
      disabled={disabled}
      className={`inline-flex h-7 w-7 items-center justify-center rounded-md border border-brand-500/20 text-ink-400 transition-colors disabled:opacity-40 disabled:cursor-not-allowed ${toneClass}`}
      title={label}
      aria-label={label}
    >
      {children}
    </button>
  );
}

function MessageActions({
  text,
  onEdit,
  onDelete,
  persistent = false,
  disabled = false,
}: {
  text: string;
  onEdit?: () => void;
  onDelete?: () => void;
  persistent?: boolean;
  disabled?: boolean;
}) {
  const zh = useLocale().startsWith("zh");
  // Hidden until the parent message (``group``) is hovered or the row
  // itself receives keyboard focus — keeps long threads quiet while the
  // copy/edit/delete actions stay reachable and accessible. Touch devices
  // have no hover, so the row falls back to always-visible via the
  // ``hover:none`` media query.
  return (
    <div className={`mt-1 flex items-center gap-1.5 text-[10px] transition-opacity duration-150 ${persistent ? "opacity-100" : "opacity-0 group-hover:opacity-100 focus-within:opacity-100 [@media(hover:none)]:opacity-100"}`}>
      <CopyButton text={text} />
      {onEdit ? (
        <IconButton label={i18nCopy(zh, "copy.components_chat_ChatMessage.013")} onClick={onEdit} disabled={disabled}>
          <EditIcon size={14} />
        </IconButton>
      ) : null}
      {onDelete ? (
        <IconButton label={i18nCopy(zh, "copy.components_chat_ChatMessage.014")} onClick={onDelete} tone="danger" disabled={disabled}>
          <TrashIcon size={14} />
        </IconButton>
      ) : null}
    </div>
  );
}

function InlineEditor({ value, originalValue, onChange, onSave, onCancel, saving, error }: {
  value: string; originalValue: string; onChange: (value: string) => void;
  onSave: () => void; onCancel: () => void; saving: boolean; error?: string;
}) {
  const t = useTranslations("chatHistory");
  const id = useId();
  const composing = useRef(false);
  const canSave = !saving && value.trim().length > 0 && value !== originalValue;
  return <form className="min-w-0 space-y-3" data-testid="message-editor" aria-busy={saving}
    onSubmit={event => { event.preventDefault(); if (canSave && !composing.current) onSave(); }}>
    <textarea autoFocus value={value} disabled={saving} aria-label={t("editLabel")}
      aria-describedby={`${id}-help${error ? ` ${id}-error` : ""}`} aria-invalid={Boolean(error)}
      onChange={event => onChange(event.target.value)}
      onCompositionStart={() => { composing.current = true; }} onCompositionEnd={() => { composing.current = false; }}
      onKeyDown={event => {
        if (composing.current || event.nativeEvent.isComposing || event.keyCode === 229) return;
        if (event.key === "Escape" && !saving) { event.preventDefault(); onCancel(); }
        if ((event.metaKey || event.ctrlKey) && event.key === "Enter") { event.preventDefault(); if (canSave) onSave(); }
      }}
      className="block min-h-28 max-h-80 w-full min-w-0 resize-y rounded-lg border border-[color:var(--line-hi)] bg-[color:var(--bg)] px-3 py-2 text-sm leading-6 text-[color:var(--text-base)] focus:outline-none focus:ring-2 focus:ring-brand-500/40 disabled:opacity-60" />
    <p id={`${id}-help`} className="text-xs leading-5 text-[color:var(--text-muted)]">{t("editHelp")}</p>
    {error ? <p id={`${id}-error`} role="alert" className="text-sm text-danger">{error || t("tooLong")}</p> : null}
    <div className="flex flex-wrap items-center justify-end gap-2">
      <button type="button" className="btn btn-ghost" disabled={saving} onClick={onCancel}>{t("cancel")}</button>
      <button type="submit" className="btn btn-primary" disabled={!canSave} aria-busy={saving}>{t(saving ? "saving" : "save")}</button>
    </div>
  </form>;
}

function formatBytes(size: number | undefined): string {
  if (!Number.isFinite(size) || !size) return "";
  if (size < 1024) return `${size} B`;
  if (size < 1024 * 1024) return `${Math.round(size / 1024)} KB`;
  return `${(size / (1024 * 1024)).toFixed(1)} MB`;
}

function MessageAttachmentList({
  attachments,
}: {
  attachments?: ChatAttachment[];
}) {
  if (!attachments?.length) return null;
  return (
    <div className="flex max-h-64 flex-wrap justify-end gap-1.5 overflow-y-auto pr-1">
      {attachments.map((attachment, index) => {
        const src = attachment.data_url || attachment.url || "";
        const isImage =
          attachment.kind === "image" ||
          attachment.mime_type?.startsWith("image/") ||
          src.startsWith("data:image/");
        return (
          <div
            key={attachment.id || `${attachment.name}-${index}`}
            className="max-w-full overflow-hidden rounded-md border border-[color:var(--line)] bg-[color:var(--card)]"
          >
            {isImage && src ? (
              // eslint-disable-next-line @next/next/no-img-element
              <img
                src={src}
                alt={attachment.name || "attachment"}
                className="block max-h-48 max-w-[260px] object-contain"
              />
            ) : null}
            <div className="flex max-w-[260px] items-center gap-1.5 px-2 py-1.5 text-[11px] text-ink-100">
              {!isImage || !src ? <FileIcon size={13} /> : null}
              {attachment.reference ? <ReferenceSnapshot attachment={attachment} className="min-w-0 truncate text-left underline-offset-2 hover:underline">{attachment.reference.label}</ReferenceSnapshot> : <span className="truncate">{attachment.name || "attachment"}</span>}
              <span className="shrink-0 text-ink-300">
                {formatBytes(attachment.size)}
              </span>
            </div>
          </div>
        );
      })}
    </div>
  );
}

export function UserBubble({
  msg,
  onEdit,
  onDelete,
  onFork,
  editing = false,
  editValue = "",
  editOriginal, saving = false, editError, actionsDisabled = false,
  onEditChange,
  onSaveEdit,
  onCancelEdit,
}: {
  msg: UserMessage;
  onEdit?: () => void;
  onDelete?: () => void;
  onFork?: () => void;
  editing?: boolean;
  editValue?: string;
  editOriginal?: string; saving?: boolean; editError?: string; actionsDisabled?: boolean;
  onEditChange?: (value: string) => void;
  onSaveEdit?: () => void;
  onCancelEdit?: () => void;
}) {
  const th = useTranslations("chatHistory");
  const zh = useLocale().startsWith("zh");
  const receipt = useMemo(() => interactionReceipt(msg.text), [msg.text]);
  return (
    <div className="flex justify-end" data-turn-role="user" data-turn-id={msg.backend_message_id || msg.id}>
      <div className={editing ? "group w-full min-w-0 max-w-[680px]" : "group min-w-0 max-w-[85%]"}>
        <div className={editing ? "space-y-3 rounded-xl border border-[color:var(--line)] bg-[color:var(--card)] p-3 sm:p-4" : "bubble-user space-y-2"}>
          {editing ? (
            <InlineEditor
              value={editValue} originalValue={editOriginal ?? msg.text} saving={saving} error={editError}
              onChange={onEditChange ?? (() => {})}
              onSave={onSaveEdit ?? (() => {})}
              onCancel={onCancelEdit ?? (() => {})}
            />
          ) : receipt ? <InteractionReceipt receipt={receipt}/> : msg.text ? (
            <div data-find-text className="whitespace-pre-wrap">{msg.text}</div>
          ) : (
            null
          )}
          {editing && <p className="mt-2 text-left text-xs leading-5 text-[color:var(--text-muted)]">{i18nCopy(zh, "copy.components_chat_ChatMessage.015")}</p>}
          <MessageAttachmentList attachments={msg.attachments} />
        </div>
        <div data-testid="user-message-meta" className="mt-1 flex min-h-7 items-center justify-end gap-2 text-[10px] text-[color:var(--text-muted)]">
          {!editing ? <MessageActions text={msg.text} onEdit={msg.text ? onEdit : undefined} onDelete={onDelete} disabled={actionsDisabled} /> : null}
          {msg.external_request ? <ExternalMessageReceipt state={msg.external_request.state} /> : null}
          {!editing && onFork ? <button type="button" onClick={onFork} disabled={!msg.backend_message_id || saving || actionsDisabled} data-testid="fork-from-message" title={i18nCopy(zh, "copy.components_chat_ChatMessage.016")} className="rounded px-1 py-1 opacity-0 group-hover:opacity-100 group-focus-within:opacity-100 [@media(hover:none)]:opacity-100">{i18nCopy(zh, "copy.components_chat_ChatMessage.017")}</button> : null}
          {msg.edited_at ? <span title={i18nCopy(zh, "copy.components_chat_ChatMessage.018")}>{th("edited")}</span> : null}
          <time>{formatTime(msg.ts)}</time>
        </div>
      </div>
    </div>
  );
}

function ExternalMessageReceipt({ state }: { state: NonNullable<UserMessage['external_request']>['state'] }) {
  const zh = useLocale().startsWith('zh');
  const labels = { queued: i18nCopy(zh, "copy.components_chat_ChatMessage.019"),
    delivered: i18nCopy(zh, "copy.components_chat_ChatMessage.020"),
    acknowledged: i18nCopy(zh, "copy.components_chat_ChatMessage.021") };
  return <span role="status" data-testid="external-message-receipt" data-state={state}>{labels[state]}</span>;
}

export function AssistantBubble({
  msg, pendingApprovals, onApprovalAction, resolvingApprovalIds, onRetry,onContinue,
  onOpenResult, traceContent, traceLabel, conversationId = '', onOpenBrowser,
}: {
  msg: AssistantMessage;
  pendingApprovals?: Map<string, ApprovalCard>;
  onApprovalAction?: (callbackData: string) => void;
  resolvingApprovalIds?: Set<string>;
  onRetry?: () => void; onContinue?:()=>void; onOpenResult?: () => void;
  traceContent?: ReactNode; traceLabel?: string; conversationId?: string; onOpenBrowser?: () => void;
}) {
  const zh = useLocale().startsWith("zh");

  const executionState = msg.execution_status || (msg.loading ? "running" : msg.error ? "failed" : /cancel|interrupt/.test(String(msg.turn?.stopped_reason || "")) ? "interrupted" : "succeeded");
  const finalReply = useMemo(() => finalReplyText(msg), [msg]);
  const events = useMemo(() => mergeActivityEvents(msg.turn?.activity_events ?? [], msg.live_events ?? []), [msg.turn?.activity_events, msg.live_events]);
  const streamedBlocks = useMemo(() => liveEventsToBlocks(events), [events]);
  const modelRetry = msg.loading && !msg.error
    ? latestModelRetry([...(msg.turn?.blocks || []), ...streamedBlocks]) : null;
  const sourceBlocks = useMemo(() => normalizeTranscriptBlocks(!msg.loading && msg.turn?.blocks?.length
    ? enrichCommittedBlocks(msg.turn.blocks, streamedBlocks)
    : streamedBlocks.length ? streamedBlocks : msg.turn?.blocks || []), [msg.loading,msg.turn?.blocks,streamedBlocks]);
  const {reply,work} = useMemo(() => partitionTranscript(sourceBlocks,finalReply,publicReplyText(msg,sourceBlocks)),[sourceBlocks,finalReply,msg]);
  const reasoningBlocks = sourceBlocks.filter(env => (env.block?.kind || env.kind) === "thinking");
  const browserOperations = useMemo(() => browserCalls([...(msg.turn?.blocks || []), ...streamedBlocks], events), [msg.turn?.blocks,streamedBlocks,events]);
  const activityEvents = events.filter((e) => e.kind.startsWith("subagent.") || e.kind.startsWith("team."));
  // A successful save is an artifact even if a later tool fails or the final
  // reply is still streaming. Read the same enriched tool evidence as the
  // trace, not only the optional committed-turn snapshot.
  const proposals = useMemo(() => activeProposalsFromTurn({
    ...msg.turn, blocks: sourceBlocks,
  } as NonNullable<AssistantMessage['turn']>), [sourceBlocks, msg.turn]);
  const toolSteps = useMemo(() => executionSteps(sourceBlocks, msg.turn),[sourceBlocks,msg.turn]);
  const members = executionMembers(activityEvents);
  const failedSteps = toolSteps.filter(step => toolPresentation(step, executionState, zh).failed).length;
  const artifacts = executionArtifacts(sourceBlocks);
  const attentionBlocks = artifacts.filter(env => (env.block?.kind || env.kind) === "approval_request");
  const visualBlocks = artifacts.filter(env => (env.block?.kind || env.kind) !== "approval_request");
  const hasTrace = Boolean(traceContent || toolSteps.length || members.length || reasoningBlocks.length || work.some(env=>(env.block||env).kind==="text"));
  const autoOpen = failedSteps > 0 || executionDefaultOpen(Boolean(msg.loading), executionState, finalReply);
  const phase = msg.id;
  const [disclosure,setDisclosure] = useState<{phase:string;open:boolean}|null>(null);
  const processOpen = disclosure?.phase === phase ? disclosure.open : autoOpen;
  const trace = <>
    {traceContent ?? <ExecutionTimeline steps={toolSteps} members={members} blocks={work} state={executionState}/>}
    {traceContent && reasoningBlocks.length > 0 && <ExecutionTimeline steps={[]} blocks={reasoningBlocks} state={executionState}/>}
  </>;
  const processLabel = commandStateText(executionState, zh);

  return <article tabIndex={-1} className="group min-w-0 w-full py-2 outline-none" data-turn-role="assistant"
    data-turn-id={msg.id} data-turn-loading={msg.loading ? "true" : "false"} data-presentation="flat">

    {hasTrace ? <div data-turn-section="trace" className="mb-5 text-[13px]">
      <details open={processOpen} onToggle={event => { const open=event.currentTarget.open; if(open!==processOpen)setDisclosure({phase,open}); }} className="group/process" data-testid="execution-process">
        <summary className="flex min-h-9 w-fit cursor-pointer list-none items-center gap-2 rounded text-xs text-[color:var(--text-muted)] outline-none focus-visible:ring-2 focus-visible:ring-ink-400">
          <NeryaGlyph name="chevronRight" size={14} className="transition-transform group-open/process:rotate-90" />
          {msg.loading && !msg.error ? <StreamingDots /> : null}
          <span>{traceLabel || processLabel}{!traceLabel && toolSteps.length ? (i18nCopy(zh, "copy.components_chat_ChatMessage.005", { value0: toolSteps.length })) : ""}</span>
          {failedSteps ? <span className="text-warn">{i18nCopy(zh, "copy.components_chat_ChatMessage.006", { value0: failedSteps })}</span> : null}
          {msg.elapsed_ms ? <span className="tabular-nums">· {formatDuration(msg.elapsed_ms)}</span> : null}
        </summary>
        {processOpen ? <div className="pt-1" onClickCapture={event=>{if((event.target as HTMLElement).closest("summary"))setDisclosure({phase,open:true});}}>{trace}</div> : null}
      </details>
    </div> : msg.loading && !msg.error ? <div role="status" className="flex items-center gap-2 py-2 text-xs text-[color:var(--text-muted)]" data-turn-section="pending">
      <StreamingDots />{i18nCopy(zh, "copy.components_chat_ChatMessage.007")}
    </div> : null}
    {msg.execution_status && !msg.assistant_task && !msg.loading && !msg.error && msg.execution_status !== "succeeded" ? <p role="status" className="mb-3 text-xs text-[color:var(--text-muted)]">{commandStateText(msg.execution_status, zh)}</p> : null}
    {attentionBlocks.length > 0 && <NativeBlocksTrack envelopes={attentionBlocks} live={Boolean(msg.loading)} pendingApprovals={pendingApprovals} onApprovalAction={onApprovalAction} resolvingApprovalIds={resolvingApprovalIds} suppressTopProposalHoist presentation="expanded"/>}
    {visualBlocks.length > 0 && <NativeBlocksTrack envelopes={visualBlocks} suppressTopProposalHoist presentation="expanded"/>}
    {modelRetry ? <ModelRetryStatus retry={modelRetry} /> : null}
    {msg.turn?.attachments?.some(item => item.model_sent === false) && <p role="status" className="mb-3 text-xs text-warn">{i18nCopy(zh, "copy.components_chat_ChatMessage.022")}</p>}
    <ContextCompactionStatus events={events}/>
    {msg.error ? <ErrorCard error={msg.error} onRetry={onRetry} onContinue={onContinue} /> : null}
    {msg.error && onContinue ? <p className="mb-3 text-xs text-[color:var(--text-muted)]">{i18nCopy(zh, "copy.components_chat_ChatMessage.023")}</p> : null}

    <BacktestReplyCards message={msg} />
    {proposals.length ? <div data-turn-section="proposal-actions" className="mb-5"><StrategyProposalsHoist proposals={proposals} /></div> : null}
    {reply ? <section data-turn-section="reply" data-message-identity={msg.id} aria-busy={Boolean(msg.loading)} aria-label={i18nCopy(zh, "copy.components_chat_ChatMessage.008")}>
      <div data-find-text>
      <StreamedMarkdown text={reply} active={Boolean(msg.loading && !msg.error && sourceBlocks.at(-1)?.block?.kind === "text" && sourceBlocks.at(-1)?.block?.completed !== true)} />
      <ResearchReplyCards message={msg} />
      </div>
      {finalReply ? <>
      {<div data-testid="result-actions" className="mt-5 flex flex-wrap items-center gap-3 border-t border-[color:var(--line)] pt-3 text-xs text-[color:var(--text-muted)]">
        <MessageActions text={reply} persistent /><ConversationContext message={msg}/>
        {conversationId&&browserOperations.length>0&&onOpenBrowser&&<button type="button" className="inline-flex h-8 items-center gap-1 rounded px-2 text-xs" onClick={onOpenBrowser} data-testid="show-browser-sidebar"><NeryaGlyph name="globe" size={14}/>{i18nCopy(zh, "copy.components_chat_ChatMessage.024")}</button>}
        {onOpenResult ? <button type="button" onClick={onOpenResult} data-testid="open-result-tab"
          className="inline-flex min-h-9 items-center gap-2 rounded-lg border border-[color:var(--line)] bg-[color:var(--card)] px-3 text-xs text-[color:var(--text-base)] hover:border-[color:var(--line-hi)] focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-brand-400">
          <FileIcon size={13} />{i18nCopy(zh, "copy.components_chat_ChatMessage.012")}
        </button> : null}
        <time className="ml-auto text-[11px]">{formatTime(msg.ts)}</time>
      </div>}
      </> : <p role="status" className="mt-2 text-xs text-[color:var(--text-muted)]">{msg.loading ? (i18nCopy(zh, "copy.components_chat_ChatMessage.025")) : (i18nCopy(zh, "copy.components_chat_ChatMessage.026"))}</p>}
    </section> : !msg.loading && !msg.error && !hasTrace && !msg.execution_status && !msg.assistant_task ? <p className="py-2 text-xs text-[color:var(--text-muted)]" data-turn-section="empty-result">
      {i18nCopy(zh, "copy.components_chat_ChatMessage.011")}
    </p> : null}

    <AssistantTaskReceipt task={msg.assistant_task} />
  </article>;
}
