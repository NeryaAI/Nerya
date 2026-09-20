"use client";

import { type ReactNode, useState } from "react";
import { useLocale, useTranslations } from "next-intl";
import type {
  AssistantMessage,
  ChatAttachment,
  NativeBlockEnvelope,
  UserMessage,
} from "../../lib/chat";
import { liveEventsToBlocks } from "../../lib/chat";
import { finalReplyText, turnWithoutFinalReply, withoutFinalReply } from "../../lib/chatResults";
import type { ApprovalCard } from "../../lib/clientApi";
import {
  formatDuration,
  NativeBlocksTrack,
  StrategyProposalsHoist,
  StreamedMarkdown,
  TurnBlocks,
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
import { NeryaAvatar } from "../NeryaLogo";
import { readableTurnSteps } from "../../lib/readableExecution";
import { toolPresentation } from "../../lib/agentConversation";
import { ReadableExecution } from "./ReadableExecution";
import { browserCalls } from '../../lib/browserTrace';

function formatTime(ts: number): string {
  try {
    return formatTimeWithTz(ts).slice(0, 5);
  } catch {
    return "";
  }
}


function withoutTextBlocks(blocks: NativeBlockEnvelope[]): NativeBlockEnvelope[] {
  return blocks.filter((env) => {
    const block = env.block ?? (env as unknown as { kind?: string });
    return String(block.kind || env.kind || "") !== "text";
  });
}

function mergeActivityEvents(
  persisted: AssistantMessage["live_events"] = [],
  live: AssistantMessage["live_events"] = [],
): NonNullable<AssistantMessage["live_events"]> {
  const out: NonNullable<AssistantMessage["live_events"]> = [];
  const seen = new Set<string>();
  for (const ev of [...persisted, ...live]) {
    const key = String(
      ev.event_id ||
        `${ev.kind}:${ev.seq || ""}:${ev.team_run_id || ""}:${ev.team_task_id || ""}:${ev.step_kind || ""}:${ev.iteration || ""}`,
    );
    if (seen.has(key)) continue;
    seen.add(key);
    out.push(ev);
  }
  return out;
}

function classifyError(raw: string): {
  kind: string;
  message: string;
  hint: string;
  showRawByDefault: boolean;
} {
  const text = (raw || "").trim();
  // Shape 1 — Next.js proxy unreachable envelope, set by
  // ``dashboard/app/api/proxy/[...path]/route.ts`` when ``fetch`` to the
  // backend throws (process not up, port mismatch, DNS, ...). After the
  // ``callApi`` upgrade we get the full envelope as a single string:
  //   ``HTTP 502 | upstream_unreachable | detail: ECONNREFUSED 127.0.0.1:18317 | trace: TypeError: fetch failed at ...``
  // The ``s`` flag + ``.+`` greedily slurps the trailing trace into
  // ``detail``, which is what we want — operators need the *raw*
  // upstream cause (status, code, message, stack) to debug, not a
  // wishy-washy hint that hides it behind a toggle.
  const proxyEnv = text.match(/^HTTP (\d{3})\s*\|\s*([A-Za-z_]+)(?:\s*\|\s*detail:\s*(.+))?$/s);
  if (proxyEnv) {
    const [, status, label, detail] = proxyEnv;
    let hint = "";
    if (label === "upstream_unreachable") {
      hint = "Backend (nerya run) appears down or unreachable from this proxy. Raw fetch error and stack are above.";
    } else if (status === "401" || status === "403") {
      hint = "Backend rejected the auth header. Check NERYA_API_TOKEN / Authorization wiring.";
    } else if (status === "404") {
      hint = "Proxy resolved a path the backend doesn't expose. Make sure the backend version matches the dashboard.";
    } else if (status === "504" || status === "522") {
      hint = "Backend took too long to respond. The kernel is probably busy / blocked on an LLM call.";
    }
    return {
      kind: `${label}@${status}`,
      message: detail ? detail.trim() : text,
      hint,
      // Proxy / upstream failures are rare and operator-debug-heavy:
      // open the raw payload by default so the ECONNREFUSED / 502 /
      // stack trace is right there without a click.
      showRawByDefault: true,
    };
  }
  // Shape 2 — backend ``LLMError`` / ``RiskRejection`` style:
  //   "LLMError: openai api error (429): rate_limit_exceeded ..."
  //   "RiskRejection: risk_rejected:..."
  //   "ApprovalPending: approval_pending:..."
  const m = text.match(/^([A-Z][A-Za-z0-9_]*Error|[A-Z][A-Za-z0-9_]*):\s*(.*)$/s);
  let kind = m ? m[1] : "Error";
  let message = m ? m[2] : text;
  // Drill into nested provider envelope: ``openai api error (429): ...``
  const provider = message.match(/^(\w+) api error \((\d{3})\):\s*(.*)$/s);
  let hint = "";
  let showRawByDefault = false;
  if (provider) {
    const [, p, status, body] = provider;
    kind = `${p}@${status}`;
    message = body || message;
    if (status === "429") hint = "Provider rate-limited. The kernel will back off. Try again in a few seconds.";
    else if (status === "401" || status === "403") hint = "Auth rejected. Check the API key under Settings → LLM Tiers.";
    else if (status === "500" || status === "502" || status === "503" || status === "504") {
      hint = "Provider had a transient outage. Loop will retry; raw response shown above.";
      // Provider 5xx — show the raw upstream body by default so the
      // operator can see request_id / model / actual provider message.
      showRawByDefault = true;
    } else if (status === "400") hint = "Provider rejected the request shape. Check schema / model availability.";
  } else if (kind === "ApprovalPending") {
    hint = "An approval is required before this action can complete. See the Approvals tab.";
  } else if (kind === "RiskRejection") {
    hint = "Risk Gate blocked the trade. The reasons are inside the message.";
  } else if (kind === "SkillNotFoundError") {
    hint = "The agent tried to call a skill that isn't installed. Check skills/ + bootstrap.";
  } else if (kind === "PromptInjectionDetected") {
    hint = "Prompt injection guard fired. Inspect the upstream payload for hostile text.";
  } else if (/network error|connection|timeout|ECONN|ETIMEDOUT/i.test(message)) {
    kind = "network";
    hint = "Network error reaching the provider. Check connectivity and try again.";
    showRawByDefault = true;
  }
  return { kind, message: message || text, hint, showRawByDefault };
}

function ErrorCard({ error, onRetry }: { error: string; onRetry?: () => void }) {
  const tCommon = useTranslations("common");
  const { kind, message, hint, showRawByDefault } = classifyError(error);
  const [showRaw, setShowRaw] = useState(showRawByDefault);
  // The classifier collapses the original ``LLMError: openai messages
  // api error (502): ...`` envelope to just the inner status. When we
  // need to debug — provider, model id, status code, full traceback —
  // the full ``error`` string is the source of truth. Toggle it.
  const hasRawDetail = error.trim() !== `${kind}: ${message}`;
  return (
    <div
      className="rounded-lg border border-danger/30 bg-danger/[0.06] px-3 py-2.5 space-y-1.5"
      data-turn-section="error"
      role="alert"
    >
      <div className="flex items-center gap-2 text-[12px] text-danger font-medium">
        <span className="inline-block w-1.5 h-1.5 rounded-full bg-danger" />
        Turn failed · {kind}
      </div>
      <div className="text-sm text-ink-100 whitespace-pre-wrap break-words">
        {message}
      </div>
      {hint ? (
        <div className="text-xs text-danger/70">{hint}</div>
      ) : null}
      {onRetry ? (
        <div className="pt-0.5">
          <button
            type="button"
            onClick={onRetry}
            className="inline-flex items-center gap-1 rounded-md border border-danger/40 px-2 py-0.5 text-[12px] text-danger hover:bg-danger/10 transition-colors"
            data-turn-section="error-retry"
          >
            {tCommon("retry")}
          </button>
        </div>
      ) : null}
      {hasRawDetail ? (
        <div className="pt-1">
          <button
            onClick={() => setShowRaw((v) => !v)}
            className="text-[12px] text-danger/80 hover:text-danger cursor-pointer transition-colors"
            data-turn-section="error-toggle"
          >
            {showRaw ? "▾ hide raw error" : "▸ show raw error / trace"}
          </button>
          {showRaw ? (
            <pre
              className="mt-1.5 text-[11px] leading-relaxed text-danger/85 bg-danger/10 border border-danger/20 rounded-md px-2.5 py-2 overflow-x-auto whitespace-pre-wrap break-all max-h-[280px] overflow-y-auto font-mono"
              data-turn-section="error-raw"
            >
              {error}
            </pre>
          ) : null}
        </div>
      ) : null}
    </div>
  );
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
              <NeryaAvatar size={32} />
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
}: {
  text: string;
  onEdit?: () => void;
  onDelete?: () => void;
  persistent?: boolean;
}) {
  const t = useTranslations("chat");
  // Hidden until the parent message (``group``) is hovered or the row
  // itself receives keyboard focus — keeps long threads quiet while the
  // copy/edit/delete actions stay reachable and accessible. Touch devices
  // have no hover, so the row falls back to always-visible via the
  // ``hover:none`` media query.
  return (
    <div className={`mt-1 flex items-center gap-1.5 text-[10px] transition-opacity duration-150 ${persistent ? "opacity-100" : "opacity-0 group-hover:opacity-100 focus-within:opacity-100 [@media(hover:none)]:opacity-100"}`}>
      <CopyButton text={text} />
      {onEdit ? (
        <IconButton label={t("editMessage")} onClick={onEdit}>
          <EditIcon size={14} />
        </IconButton>
      ) : null}
      {onDelete ? (
        <IconButton label={t("deleteMessage")} onClick={onDelete} tone="danger">
          <TrashIcon size={14} />
        </IconButton>
      ) : null}
    </div>
  );
}

function InlineEditor({
  value,
  onChange,
  onSave,
  onCancel,
  align = "left",
}: {
  value: string;
  onChange: (value: string) => void;
  onSave: () => void;
  onCancel: () => void;
  align?: "left" | "right";
}) {
  const t = useTranslations("chat");
  const canSave = value.trim().length > 0;
  return (
    <form
      className="space-y-2"
      onSubmit={(event) => {
        event.preventDefault();
        if (canSave) onSave();
      }}
    >
      <textarea
        autoFocus
        value={value}
        onChange={(event) => onChange(event.target.value)}
        onKeyDown={(event) => {
          if (event.key === "Escape") {
            event.preventDefault();
            onCancel();
          }
          if ((event.metaKey || event.ctrlKey) && event.key === "Enter") {
            event.preventDefault();
            if (canSave) onSave();
          }
        }}
        className="min-h-[96px] w-full resize-y rounded-lg border border-brand-500/25 bg-ink-950/45 px-3 py-2 text-sm leading-relaxed text-white placeholder:text-ink-300 focus:outline-none focus:border-brand-500/60"
      />
      <div
        className={`flex items-center gap-1.5 ${
          align === "right" ? "justify-end" : "justify-start"
        }`}
      >
        <IconButton label={t("cancelEdit")} onClick={onCancel}>
          <XIcon size={14} />
        </IconButton>
        <IconButton
          label={t("saveEdit")}
          type="submit"
          tone="primary"
          disabled={!canSave}
        >
          <CheckIcon size={14} />
        </IconButton>
      </div>
    </form>
  );
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
            className="max-w-full overflow-hidden rounded-md border border-white/15 bg-black/15"
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
              <span className="truncate">{attachment.name || "attachment"}</span>
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
  editing = false,
  editValue = "",
  onEditChange,
  onSaveEdit,
  onCancelEdit,
}: {
  msg: UserMessage;
  onEdit?: () => void;
  onDelete?: () => void;
  editing?: boolean;
  editValue?: string;
  onEditChange?: (value: string) => void;
  onSaveEdit?: () => void;
  onCancelEdit?: () => void;
}) {
  return (
    <div className="flex justify-end" data-turn-role="user" data-turn-id={msg.id}>
      <div className="group max-w-[85%]">
        <div className="bubble-user space-y-2">
          {editing ? (
            <InlineEditor
              value={editValue}
              onChange={onEditChange ?? (() => {})}
              onSave={onSaveEdit ?? (() => {})}
              onCancel={onCancelEdit ?? (() => {})}
              align="right"
            />
          ) : msg.text ? (
            <div className="whitespace-pre-wrap">{msg.text}</div>
          ) : (
            null
          )}
          {!editing ? <MessageAttachmentList attachments={msg.attachments} /> : null}
        </div>
        <div data-testid="user-message-meta" className="mt-1 flex min-h-7 items-center justify-end gap-2 text-[10px] text-[color:var(--text-muted)]">
          {!editing ? <MessageActions text={msg.text} onEdit={msg.text ? onEdit : undefined} onDelete={onDelete} /> : null}
          <time>{formatTime(msg.ts)}</time>
        </div>
      </div>
    </div>
  );
}

export function AssistantBubble({
  msg, pendingApprovals, onApprovalAction, resolvingApprovalIds, onRetry,
  onEdit, onDelete, editing = false, editValue = "", onEditChange,
  onSaveEdit, onCancelEdit, onOpenResult, traceContent, traceLabel, conversationId = '', onOpenBrowser,
}: {
  msg: AssistantMessage;
  pendingApprovals?: Map<string, ApprovalCard>;
  onApprovalAction?: (callbackData: string) => void;
  resolvingApprovalIds?: Set<string>;
  onRetry?: () => void; onEdit?: () => void; onDelete?: () => void;
  editing?: boolean; editValue?: string; onEditChange?: (value: string) => void;
  onSaveEdit?: () => void; onCancelEdit?: () => void; onOpenResult?: () => void;
  traceContent?: ReactNode; traceLabel?: string; conversationId?: string; onOpenBrowser?: () => void;
}) {
  const zh = useLocale().startsWith("zh");
  const reply = finalReplyText(msg);
  const events = mergeActivityEvents(msg.turn?.activity_events ?? [], msg.live_events ?? []);
  const streamedBlocks = liveEventsToBlocks(events);
  const liveBlocks = withoutFinalReply(streamedBlocks.length ? streamedBlocks : msg.turn?.blocks || [], reply);
  const traceTurn = msg.turn ? turnWithoutFinalReply(msg.turn, reply) : undefined;
  const browserOperations = browserCalls([...(msg.turn?.blocks || []), ...streamedBlocks], events);
  const approvalEvents = events.filter((e) => e.kind === "approval.request" || e.kind === "approval.resolved");
  const activityEvents = events.filter((e) => e.kind.startsWith("subagent.") || e.kind.startsWith("team."));
  const replayEvents = events.filter((e) => e.kind === "tool.complete");
  const hasStoredTrace = Boolean(traceTurn && ((traceTurn.blocks?.length ?? 0)
    || (traceTurn.tool_trace?.length ?? 0) || (traceTurn.events?.length ?? 0)
    || (traceTurn.actions?.length ?? 0) || traceTurn.plan?.kind || activityEvents.length));
  const pending = Boolean(pendingApprovals?.size && (approvalEvents.length
    || liveBlocks.some((env) => (env.block?.kind || env.kind) === "approval_request")
    || traceTurn?.blocks?.some((env) => (env.block?.kind || env.kind) === "approval_request")));
  const proposals = !msg.loading && !msg.error && msg.turn ? activeProposalsFromTurn(msg.turn) : [];
  // Specialized blocks, approval controls and team activities retain their existing renderer.
  const readableSteps = traceContent === undefined && !pending && !approvalEvents.length && !activityEvents.length
    && !traceTurn?.plan && !traceTurn?.events?.length && !traceTurn?.actions?.length
    ? readableTurnSteps(withoutFinalReply([...(msg.turn?.blocks || []), ...streamedBlocks], reply), msg.ts) : null;
  const toolSteps = readableSteps?.filter((step) => step.event.kind !== "text") || [];
  const failedSteps = toolSteps.filter((step) => toolPresentation(step, msg.loading ? "running" : "completed", zh).failed).length;
  const hasTrace = traceContent !== undefined || (readableSteps !== null ? readableSteps.length > 0 : hasStoredTrace || liveBlocks.length > 0);
  const trace = traceContent ?? (readableSteps !== null ? <ReadableExecution steps={readableSteps} state={msg.loading ? "running" : "completed"} /> : !msg.loading && traceTurn && hasStoredTrace ? (
    <TurnBlocks turn={traceTurn} hoistTeamTraces pendingApprovals={pendingApprovals}
      onApprovalAction={onApprovalAction} resolvingApprovalIds={resolvingApprovalIds}
      approvalEvents={approvalEvents} activityEvents={activityEvents} replayEvents={replayEvents}
      suppressTopProposalHoist />
  ) : <NativeBlocksTrack envelopes={liveBlocks} live={Boolean(msg.loading && !msg.error)}
    hoistTeamTraces pendingApprovals={pendingApprovals} onApprovalAction={onApprovalAction}
    resolvingApprovalIds={resolvingApprovalIds} suppressTopProposalHoist />);
  const processLabel = msg.loading ? (zh ? "正在执行" : "Working")
    : msg.error ? (zh ? "执行记录（含错误）" : "Execution log with errors")
    : (zh ? "查看执行过程" : "View execution steps");

  return <article tabIndex={-1} className="group min-w-0 w-full py-2 outline-none" data-turn-role="assistant"
    data-turn-id={msg.id} data-turn-loading={msg.loading ? "true" : "false"} data-presentation="flat">
    {conversationId && browserOperations.length && onOpenBrowser ? <button type="button" className="mb-3 inline-flex min-h-9 items-center gap-2 rounded-lg border border-[color:var(--line)] px-3 text-xs text-[color:var(--text-muted)]" onClick={onOpenBrowser} data-testid="show-browser-sidebar">↗ {zh ? '在侧栏查看浏览器' : 'View browser in side panel'}</button> : null}
    {hasTrace ? <div data-turn-section="trace" className="mb-5 text-[13px]">
      {pending ? <div className="space-y-3">{trace}</div> : <details open={Boolean(msg.loading || msg.error)} className="group/process">
        <summary className="flex min-h-9 w-fit cursor-pointer list-none items-center gap-2 rounded text-xs text-[color:var(--text-muted)] outline-none focus-visible:ring-2 focus-visible:ring-ink-400">
          <span aria-hidden="true" className="inline-block transition-transform group-open/process:rotate-90">›</span>
          {msg.loading && !msg.error ? <StreamingDots /> : null}
          <span>{traceLabel || processLabel}{!traceLabel && toolSteps.length ? (zh ? ` · ${toolSteps.length} 项操作` : ` · ${toolSteps.length} operations`) : ""}</span>
          {failedSteps ? <span className="text-warn">{zh ? `${failedSteps} 项需查看` : `${failedSteps} need review`}</span> : null}
          {msg.elapsed_ms ? <span className="tabular-nums">· {formatDuration(msg.elapsed_ms)}</span> : null}
        </summary>
        <div className="pt-3">{trace}</div>
      </details>}
    </div> : msg.loading && !msg.error ? <div role="status" className="flex items-center gap-2 py-2 text-xs text-[color:var(--text-muted)]" data-turn-section="pending">
      <StreamingDots />{zh ? "正在处理你的任务…" : "Working on your task…"}
    </div> : null}
    {msg.error ? <ErrorCard error={msg.error} onRetry={onRetry} /> : null}
    {proposals.length ? <div data-turn-section="proposal-actions" className="mb-5"><StrategyProposalsHoist proposals={proposals} /></div> : null}
    {reply ? <section data-turn-section="reply" aria-label={zh ? "最终结果" : "Final result"}>
      {hasTrace && !/^\s{0,3}#{1,6}\s/.test(reply.trimStart()) ? <div className="mb-3 flex items-center gap-2 text-xs font-medium text-[color:var(--text-muted)]">
        <FileIcon size={13} />{zh ? "本轮结果" : "Result"}
      </div> : null}
      {editing ? <InlineEditor value={editValue} onChange={onEditChange ?? (() => {})}
        onSave={onSaveEdit ?? (() => {})} onCancel={onCancelEdit ?? (() => {})} /> : <StreamedMarkdown text={reply} />}
      {!editing ? <div data-testid="result-actions" className="mt-5 flex flex-wrap items-center gap-3 border-t border-[color:var(--line)] pt-3 text-xs text-[color:var(--text-muted)]">
        <MessageActions text={reply} onEdit={onEdit} onDelete={onDelete} persistent />
        {onOpenResult ? <button type="button" onClick={onOpenResult} data-testid="open-result-canvas"
          className="inline-flex min-h-9 items-center gap-2 rounded-lg border border-[color:var(--line)] bg-[color:var(--card)] px-3 text-xs text-[color:var(--text-base)] hover:border-[color:var(--line-hi)] focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-brand-400">
          <FileIcon size={13} />{zh ? "在 Canvas 中查看" : "Open in Canvas"}
        </button> : null}
        <time className="ml-auto text-[11px]">{formatTime(msg.ts)}</time>
      </div> : null}
    </section> : !msg.loading && !msg.error ? <p className="py-2 text-xs text-[color:var(--text-muted)]" data-turn-section="empty-result">
      {zh ? "本轮没有返回最终结果。" : "No final result was returned for this turn."}
    </p> : null}
  </article>;
}
