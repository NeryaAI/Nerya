"use client";

import { type ReactNode, useState } from "react";
import { useTranslations } from "next-intl";
import type {
  AssistantMessage,
  ChatAttachment,
  NativeBlockEnvelope,
  UserMessage,
} from "../../lib/chat";
import { liveEventsToBlocks, topLevelDecisionText } from "../../lib/chat";
import type { ApprovalCard } from "../../lib/clientApi";
import { NativeBlocksTrack, TurnBlocks } from "./TurnBlocks";
import { Markdown } from "./Markdown";
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

function formatTime(ts: number): string {
  try {
    return formatTimeWithTz(ts).slice(0, 5);
  } catch {
    return "";
  }
}


function PendingTrail() {
  // Lightweight placeholder shown only before the first event lands.
  // Once the kernel emits its first ``message.delta`` / ``tool.start`` /
  // ``turn.step`` we switch to the real ``NativeBlocksTrack`` stream.
  const steps = [
    "route selection",
    "model decision",
    "tool execution",
    "observation / replan",
  ];
  return (
    <div className="mt-2 grid grid-cols-2 gap-1.5">
      {steps.map((step, i) => (
        <div
          key={step}
          className="rounded-md border border-brand-500/10 bg-brand-500/[0.04] px-2 py-1.5 text-[11px] text-ink-300 flex items-center gap-2"
        >
          <span
            className="typing-dot"
            style={{ animationDelay: `${i * 0.18}s` }}
          />
          <span>{step}</span>
        </div>
      ))}
    </div>
  );
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
    if (status === "429") hint = "Provider rate-limited. The kernel will back off — try again in a few seconds.";
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

function ErrorCard({ error }: { error: string }) {
  const { kind, message, hint, showRawByDefault } = classifyError(error);
  const [showRaw, setShowRaw] = useState(showRawByDefault);
  // The classifier collapses the original ``LLMError: openai messages
  // api error (502): ...`` envelope to just the inner status. When we
  // need to debug — provider, model id, status code, full traceback —
  // the full ``error`` string is the source of truth. Toggle it.
  const hasRawDetail = error.trim() !== `${kind}: ${message}`;
  return (
    <div
      className="rounded-lg border border-rose-500/30 bg-rose-500/[0.06] px-3 py-2.5 space-y-1.5"
      data-turn-section="error"
      role="alert"
    >
      <div className="flex items-center gap-2 text-[12px] text-rose-300 font-medium">
        <span className="inline-block w-1.5 h-1.5 rounded-full bg-rose-400 shadow-[0_0_8px_rgba(244,63,94,0.7)]" />
        Turn failed · {kind}
      </div>
      <div className="text-sm text-rose-50 whitespace-pre-wrap break-words">
        {message}
      </div>
      {hint ? (
        <div className="text-xs text-rose-200/70">{hint}</div>
      ) : null}
      {hasRawDetail ? (
        <div className="pt-1">
          <button
            onClick={() => setShowRaw((v) => !v)}
            className="text-[12px] text-rose-300/80 hover:text-rose-200 cursor-pointer transition-colors"
            data-turn-section="error-toggle"
          >
            {showRaw ? "▾ hide raw error" : "▸ show raw error / trace"}
          </button>
          {showRaw ? (
            <pre
              className="mt-1.5 text-[11px] leading-relaxed text-rose-100/85 bg-rose-950/40 border border-rose-500/20 rounded-md px-2.5 py-2 overflow-x-auto whitespace-pre-wrap break-all max-h-[280px] overflow-y-auto font-mono"
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
      ? "hover:text-[#ef5564] hover:border-[#ef5564]/40"
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
}: {
  text: string;
  onEdit?: () => void;
  onDelete?: () => void;
}) {
  const t = useTranslations("chat");
  return (
    <div className="mt-1 flex items-center gap-1.5 text-[10px]">
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
        className="min-h-[96px] w-full resize-y rounded-lg border border-brand-500/25 bg-ink-950/45 px-3 py-2 text-sm leading-relaxed text-white placeholder:text-ink-500 focus:outline-none focus:border-brand-500/60"
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
      <div className="max-w-[85%]">
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
        {!editing ? (
          <div className="flex justify-end">
            <MessageActions
              text={msg.text}
              onEdit={msg.text ? onEdit : undefined}
              onDelete={onDelete}
            />
          </div>
        ) : null}
        <div className="text-[10px] text-ink-400 mt-1 text-right">
          {formatTime(msg.ts)}
        </div>
      </div>
    </div>
  );
}

export function AssistantBubble({
  msg,
  pendingApprovals,
  onApprovalAction,
  resolvingApprovalIds,
  onEdit,
  onDelete,
  editing = false,
  editValue = "",
  onEditChange,
  onSaveEdit,
  onCancelEdit,
}: {
  msg: AssistantMessage;
  pendingApprovals?: Map<string, ApprovalCard>;
  onApprovalAction?: (callbackData: string) => void;
  resolvingApprovalIds?: Set<string>;
  onEdit?: () => void;
  onDelete?: () => void;
  editing?: boolean;
  editValue?: string;
  onEditChange?: (value: string) => void;
  onSaveEdit?: () => void;
  onCancelEdit?: () => void;
}) {
  // Render each assistant turn in execution order: thinking, then tool
  // use/results, and finally the reply text. While the turn is still
  // running, keep the reply text at the bottom so live activity and audit
  // details stay in the order the agent produced them.
  const reasoning = topLevelDecisionText(msg.turn);
  const showStreaming = msg.loading && !msg.error;
  const liveEvents = mergeActivityEvents(
    msg.turn?.activity_events ?? [],
    msg.live_events ?? [],
  );
  const hasLive = liveEvents.length > 0;
  const hasTurnBody = !!msg.turn && (
    (msg.turn.actions?.length ?? 0) > 0
    || (msg.turn.tool_trace?.length ?? 0) > 0
    || (msg.turn.events?.length ?? 0) > 0
    || Object.keys(msg.turn.subagents ?? {}).length > 0
    || !!msg.turn.plan?.kind
  );
  // Source of truth for the chronological tool/thinking trace:
  //   - while ``loading`` we materialise the live event stream into
  //     block envelopes so the chat can render
  //     ``NativeBlocksTrack`` immediately, expanding the most recent
  //     pending tool_use card.
  //   - after the turn returns ``TurnBlocks`` owns the render (it
  //     already re-emits ``NativeBlocksTrack`` from ``msg.turn.blocks``).
  //     If the backend skipped native blocks, fall back to the
  //     reconstructed live stream so the audit history isn't lost.
  const finalBlocks = msg.turn?.blocks ?? [];
  const liveBlocks = hasLive ? liveEventsToBlocks(liveEvents) : [];
  const fallbackBlocks = reasoning ? withoutTextBlocks(liveBlocks) : liveBlocks;
  const approvalEvents = liveEvents.filter(
    (ev) => ev.kind === "approval.request" || ev.kind === "approval.resolved",
  );
  const activityEvents = liveEvents.filter(
    (ev) => ev.kind.startsWith("subagent.") || ev.kind.startsWith("team."),
  );
  const replayEvents = liveEvents.filter((ev) => ev.kind === "tool.complete");
  return (
    <div
      className="flex justify-start"
      data-turn-role="assistant"
      data-turn-id={msg.id}
      data-turn-loading={msg.loading ? "true" : "false"}
    >
      <div className="max-w-[92%] min-w-[200px] w-full">
        <div className="flex items-center gap-2 mb-1.5">
          <div className="relative h-8 w-8 shrink-0">
            {showStreaming ? (
              <span className="absolute -inset-0.5 rounded-full ring-ai opacity-80 animate-spin" style={{ animationDuration: "8s" }} />
            ) : null}
            <div className="relative h-8 w-8 rounded-full overflow-hidden ring-1 ring-brand-500/40 shadow-glow bg-black/20 flex items-center justify-center">
              <NeryaAvatar size={32} />
            </div>
            <span className="absolute -bottom-0.5 -right-0.5 w-2.5 h-2.5 rounded-full bg-accent-500 ring-2 ring-[var(--bg-deep)]" />
          </div>
          <div className="text-[12px] text-ink-200 font-semibold tracking-tight">Nerya</div>
          {showStreaming ? (
            <div className="flex items-center gap-1.5 text-[10px] text-fluid-400">
              <StreamingDots />
              <span className="text-ink-400">thinking…</span>
            </div>
          ) : null}
          {msg.elapsed_ms && !msg.loading ? (
            <div className="text-[10px] text-ink-500 font-mono">
              {msg.elapsed_ms}ms
            </div>
          ) : null}
        </div>
        <div className="bubble-ai space-y-3">
          {/* When a turn fails before the backend commits ``msg.turn``,
            * the already-streamed live events may be the only surviving
            * record of what happened. Render those partial blocks before
            * the error card so the user still sees the audit trail. */}
          {msg.error && liveBlocks.length ? (
            <div data-turn-section="blocks-on-error">
              <NativeBlocksTrack
                envelopes={liveBlocks}
                label="partial transcript before error"
                pendingApprovals={pendingApprovals}
                onApprovalAction={onApprovalAction}
                resolvingApprovalIds={resolvingApprovalIds}
              />
            </div>
          ) : null}

          {msg.error ? <ErrorCard error={msg.error} /> : null}

          {msg.loading && !hasLive && !hasTurnBody ? (
            <div className="text-ink-400 text-xs italic" data-turn-section="pending">
              Waiting for the kernel to emit the first block…
              <PendingTrail />
            </div>
          ) : null}

          {/* Native block stream — chronological text / thinking /
            * tool_use / tool_result blocks, mirroring exactly what the
            * model produced. While ``loading`` is true we feed the
            * live ``message.delta`` / ``turn.step`` / ``tool.*`` events
            * through ``liveEventsToBlocks`` so the same renderer
            * powers both streaming and committed turns. After commit
            * we let ``TurnBlocks`` own the rendering so the audit
            * trail (actions / subagents / plan) shows alongside it. */}
          {msg.loading && liveBlocks.length ? (
            <div data-turn-section="blocks">
              <NativeBlocksTrack
                envelopes={liveBlocks}
                live
                label="agent transcript"
                pendingApprovals={pendingApprovals}
                onApprovalAction={onApprovalAction}
                resolvingApprovalIds={resolvingApprovalIds}
              />
            </div>
          ) : null}

          {!msg.loading && msg.turn ? (
            <div data-turn-section="trace">
              <TurnBlocks
                turn={msg.turn}
                pendingApprovals={pendingApprovals}
                onApprovalAction={onApprovalAction}
                approvalEvents={approvalEvents}
                activityEvents={activityEvents}
                replayEvents={replayEvents}
                resolvingApprovalIds={resolvingApprovalIds}
              />
            </div>
          ) : null}

          {/* Fallback: turn settled but the backend didn't return
            * native blocks — surface the converted live stream so the
            * chronological tool trace doesn't disappear. */}
          {!msg.loading && msg.turn && !finalBlocks.length && fallbackBlocks.length ? (
            <div data-turn-section="blocks-fallback">
              <NativeBlocksTrack
                envelopes={fallbackBlocks}
                label="reconstructed transcript"
                pendingApprovals={pendingApprovals}
                onApprovalAction={onApprovalAction}
                resolvingApprovalIds={resolvingApprovalIds}
              />
            </div>
          ) : null}

          {/* Final reply text last — chat transcript order: the
            * assistant's prose summary always lands AFTER tool_use
            * blocks, never above them. The text is rendered through
            * the shared Markdown component (Apr-27 user feedback —
            * the runtime already does GFM, our previous plain-text
            * ``whitespace-pre-wrap`` made tables/lists/code blocks
            * unreadable). */}
          {!msg.error && reasoning ? (
            <div
              data-turn-section="reply"
              className="pt-3 border-t border-ink-700/50 leading-relaxed text-ink-100"
            >
              {editing ? (
                <InlineEditor
                  value={editValue}
                  onChange={onEditChange ?? (() => {})}
                  onSave={onSaveEdit ?? (() => {})}
                  onCancel={onCancelEdit ?? (() => {})}
                />
              ) : (
                <>
                  <Markdown>{reasoning}</Markdown>
                  <MessageActions
                    text={reasoning}
                    onEdit={onEdit}
                    onDelete={onDelete}
                  />
                </>
              )}
            </div>
          ) : null}

          {!msg.error && !reasoning && !msg.loading ? (
            <div
              data-turn-section="reply-empty"
              className="text-ink-400 italic text-xs"
            >
              (no reply returned)
            </div>
          ) : null}
        </div>
        <div className="text-[10px] text-ink-400 mt-1">{formatTime(msg.ts)}</div>
      </div>
    </div>
  );
}
