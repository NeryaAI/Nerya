"use client";

import { useTranslations } from "next-intl";

import { ReactNode, useState } from "react";
import type {
  ActionRecord,
  GatewayEvent,
  LiveEvent,
  NativeBlock,
  NativeBlockEnvelope,
  ToolTraceEntry,
  TurnPayload,
} from "../../lib/chat";
import { liveEventsToBlocks } from "../../lib/chat";
import type { ApprovalCard } from "../../lib/clientApi";
import {
  ApprovalRequestCard,
  approvalIdFromEvent,
} from "./ApprovalRequestCard";
import { Markdown } from "./Markdown";
import {
  ChevronDownIcon,
  ChevronRightIcon,
  FileIcon,
  WrenchIcon,
} from "../icons";
import { JsonView } from "../JsonView";
import {
  StrategyProposalApprovalCard,
  strategyProposalFromToolResult,
} from "../strategies/StrategyProposalApprovalCard";
import { ChartBlock as NativeChartBlock } from "./ChartBlock";
import {
  CopyButton,
  FileOpCard,
  ShellCard,
  SkillLoadCard,
  Tag,
  TodoChecklistCard,
  WebCard,
  arrayOfRecords,
  isFileOp,
  isShellTool,
  isSkillTool,
  isTodoWrite,
  isWebTool,
  recordOf,
  todosFromBlock,
} from "./tool-cards";

function JsonBlock({ value }: { value: unknown }) {
  let text = "";
  try {
    text = JSON.stringify(value, null, 2);
  } catch {
    text = String(value);
  }
  return (
    <div className="relative">
      <div className="absolute left-2 top-2 z-10">
        <CopyButton text={text} />
      </div>
      <JsonView value={value} initialCollapsed showRawToggle className="!pl-10" />
    </div>
  );
}

function Collapsible({
  title,
  tone = "neutral",
  badge,
  defaultOpen = false,
  children,
  right,
  chrome,
  headerClassName = "",
  bodyClassName = "",
}: {
  title: ReactNode;
  tone?: "neutral" | "ok" | "warn" | "err" | "brand";
  badge?: ReactNode;
  defaultOpen?: boolean;
  children: ReactNode;
  right?: ReactNode;
  chrome?: { border?: string; bg?: string };
  headerClassName?: string;
  bodyClassName?: string;
}) {
  const [open, setOpen] = useState(defaultOpen);
  const border = chrome?.border ?? {
    neutral: "border-ink-700/70",
    ok: "border-brand-500/40",
    warn: "border-[#f5a524]/40",
    err: "border-[#ef5564]/40",
    brand: "border-brand-500/40",
  }[tone];
  const bg = chrome?.bg ?? {
    neutral: "bg-ink-800/50",
    ok: "bg-brand-500/5",
    warn: "bg-[#f5a524]/5",
    err: "bg-[#ef5564]/5",
    brand: "bg-brand-500/5",
  }[tone];
  return (
    <div className={`rounded-md border ${border} ${bg} overflow-hidden`}>
      <button
        onClick={() => setOpen((v) => !v)}
        className={`w-full flex items-center justify-between gap-2 px-3 py-2 text-left hover:bg-ink-800/40 transition-colors ${headerClassName}`}
      >
        <div className="flex items-center gap-2 min-w-0">
          <span className="text-ink-400">
            {open ? <ChevronDownIcon size={13} /> : <ChevronRightIcon size={13} />}
          </span>
          <span className="text-xs text-ink-100 truncate">{title}</span>
          {badge}
        </div>
        {right}
      </button>
      {open ? (
        <div className={`px-3 pb-3 pt-1 border-t border-ink-700/50 ${bodyClassName}`}>{children}</div>
      ) : null}
    </div>
  );
}

function ActionBlock({ action }: { action: ActionRecord }) {
  const ok = !action.error;
  const strategyProposal = strategyProposalFromToolResult(
    action.result,
    action.action,
  );
  const title = (
    <span className="flex items-center gap-2">
      <span className="text-ink-400">action</span>
      <span className="font-mono text-ink-100">{action.action}</span>
    </span>
  );
  const badge = ok ? (
    <Tag tone="ok">applied</Tag>
  ) : (
    <Tag tone="err">{action.error_kind || "error"}</Tag>
  );
  const art = artifactSummary(action);
  return (
    <Collapsible title={title} tone={ok ? "ok" : "err"} badge={badge} right={art}>
      {ok ? (
        <div className="space-y-2">
          {art ? (
            <div className="text-[11px] text-ink-300">Landed: {art}</div>
          ) : null}
          {strategyProposal ? (
            <StrategyProposalApprovalCard
              proposal={strategyProposal}
              compact
              approveNote="approved from chat action"
            />
          ) : null}
          <JsonBlock value={action.result} />
        </div>
      ) : (
        <div className="space-y-2">
          <div className="text-xs text-[#ef5564]">{action.error}</div>
          <JsonBlock value={action} />
        </div>
      )}
    </Collapsible>
  );
}

function artifactSummary(a: ActionRecord): ReactNode {
  if (a.error) return null;
  const r = (a.result ?? {}) as Record<string, unknown>;
  if (a.action === "propose_script") {
    const pid = r.id as string | undefined;
    return pid ? <Tag tone="brand">{`proposal ${pid.slice(0, 8)}…`}</Tag> : null;
  }
  if (a.action === "create_subagent") {
    return r.name ? <Tag tone="brand">{String(r.name)}</Tag> : null;
  }
  if (a.action === "add_schedule") {
    const id = r.id as string | undefined;
    const kind = ((r.entry as Record<string, unknown>) || {}).kind as
      | string
      | undefined;
    return id ? (
      <Tag tone="brand">
        {id} · {kind ?? "schedule"}
      </Tag>
    ) : null;
  }
  if (a.action === "submit_trade_intent") {
    const intent =
      ((r as { intent?: Record<string, unknown> }).intent as Record<string, unknown>) ||
      ({} as Record<string, unknown>);
    const side = intent.side as string | undefined;
    const market = intent.market as string | undefined;
    const size = (intent.size ?? intent.size_usd) as number | string | undefined;
    if (side && market) {
      return (
        <Tag tone="brand">
          {side} {market} {size ? `@ ${size}` : ""}
        </Tag>
      );
    }
  }
  if (a.action === "propose_prompt_patch") {
    const pid = r.id as string | undefined;
    return pid ? <Tag tone="brand">{`patch ${pid.slice(0, 8)}…`}</Tag> : null;
  }
  return null;
}

function toolTraceAsBlock(t: ToolTraceEntry): NativeBlock {
  return {
    kind: t.ok === false || t.error ? "tool_result" : "tool_use",
    skill_id: t.skill_id || "native",
    action: t.action || "",
    ok: t.ok,
    error: t.error,
    error_kind: t.error_kind,
    elapsed_ms: t.elapsed_ms,
    payload: t.payload || {},
    result: t.result,
  };
}

function ToolBlock({ t }: { t: ToolTraceEntry }) {
  const block = toolTraceAsBlock(t);
  if (isTodoWrite(block)) {
    return <TodoChecklistCard todos={todosFromBlock(block)} />;
  }
  if (isSkillTool(block)) {
    return <SkillLoadCard block={block} variant="result" />;
  }
  if (isFileOp(block)) {
    return <FileOpCard block={block} variant="result" />;
  }
  if (isShellTool(block)) {
    return <ShellCard block={block} variant="result" />;
  }
  if (isWebTool(block)) {
    return <WebCard block={block} variant="result" />;
  }
  if (isAgentTool(block)) {
    return <AgentToolResultCard block={block} />;
  }
  const ok = t.ok !== false && !t.error;
  const strategyProposal = strategyProposalFromToolResult(
    t.result,
    String(t.action || ""),
  );
  const title = (
    <span className="flex items-center gap-2 min-w-0">
      <span className="text-ink-400">tool</span>
      <span className="font-mono text-ink-100 truncate">
        {t.skill_id}.{t.action}
      </span>
    </span>
  );
  const badges = (
    <span className="flex items-center gap-1">
      {ok ? <Tag tone="ok">ok</Tag> : <Tag tone="err">{t.error_kind || "error"}</Tag>}
      {typeof t.elapsed_ms === "number" ? (
        <Tag>{t.elapsed_ms}ms</Tag>
      ) : null}
    </span>
  );
  return (
    <Collapsible
      title={title}
      tone={ok ? "neutral" : "err"}
      badge={badges}
      defaultOpen={false}
    >
      <div className="space-y-2">
        {t.error ? (
          <div className="text-xs text-[#ef5564]">{t.error}</div>
        ) : null}
        <OutputPanel label="result" value={t.result ?? t.error} />
        {strategyProposal ? (
          <StrategyProposalApprovalCard
            proposal={strategyProposal}
            compact
            approveNote="approved from chat tool trace"
          />
        ) : null}
        <RawDetails>
          <JsonPanel label="payload" value={t.payload} />
          <JsonPanel label="raw result" value={t.result ?? t.error} />
          <JsonPanel label="budget" value={t.budget_snapshot} />
        </RawDetails>
      </div>
    </Collapsible>
  );
}


function EventBlock({ event, index }: { event: GatewayEvent; index: number }) {
  const ok = event.status !== "error";
  const detail = (event.detail || {}) as Record<string, unknown>;
  const effort = typeof detail.reasoning_effort === "string"
    ? detail.reasoning_effort
    : "";
  const reasoningTokens = typeof detail.reasoning_tokens === "number"
    ? detail.reasoning_tokens
    : 0;
  const reasoningText = typeof detail.reasoning === "string"
    ? detail.reasoning
    : "";
  const provider = typeof detail.provider === "string" ? detail.provider : "";
  const model = typeof detail.model === "string" ? detail.model : "";
  const title = (
    <span className="flex items-center gap-2 min-w-0">
      <span className="text-ink-400">step {index + 1}</span>
      <span className="font-mono text-ink-100 truncate">
        {event.phase || "event"}
      </span>
    </span>
  );
  const right = (
    <span className="flex items-center gap-1">
      {effort ? <Tag tone="brand">{`effort: ${effort}`}</Tag> : null}
      {reasoningTokens > 0 ? (
        <Tag tone="brand">{`${reasoningTokens} thought tok`}</Tag>
      ) : null}
      {typeof event.wall_ms === "number" ? (
        <Tag>{event.wall_ms}ms</Tag>
      ) : null}
    </span>
  );
  return (
    <Collapsible
      title={title}
      tone={ok ? "neutral" : "err"}
      badge={<Tag tone={ok ? "ok" : "err"}>{event.status || "ok"}</Tag>}
      right={right}
    >
      <div className="space-y-2">
        {event.text ? (
          <div className="text-xs text-ink-200">{event.text}</div>
        ) : null}
        {provider || model ? (
          <div className="flex items-center gap-1 flex-wrap">
            {provider ? <Tag>{provider}</Tag> : null}
            {model ? <Tag>{model}</Tag> : null}
          </div>
        ) : null}
        {reasoningText ? (
          <div>
            <div className="text-[11px] text-ink-400 font-medium mb-1">
              reasoning summary
            </div>
            <pre className="whitespace-pre-wrap text-[11px] font-mono text-ink-200 bg-ink-900/70 border border-ink-700/70 rounded-md p-2 overflow-auto max-h-64">
              {reasoningText}
            </pre>
          </div>
        ) : null}
        {event.detail ? <JsonBlock value={event.detail} /> : null}
      </div>
    </Collapsible>
  );
}

// ---------------------------------------------------------------------
// provider-native block rendering
// ---------------------------------------------------------------------
//
// The new ``WorkspaceNativeAgentLoop`` emits a chronological stream of
// content blocks (``text`` / ``thinking`` / ``tool_use`` /
// ``tool_result``). When the API surfaces them under ``turn.blocks`` we
// render them in their original order — that's the truth source the
// model's transcript was built from. The legacy ``actions`` /
// ``tool_trace`` views are still rendered below as a familiar summary,
// but the native track is the operator's primary lens.

function unwrapBlock(env: NativeBlockEnvelope): NativeBlock {
  const inner = (env.block ?? {}) as NativeBlock;
  if (inner && Object.keys(inner).length > 0) return inner;
  // Some serialisers flatten the payload onto the envelope; treat that
  // as the block.
  return env as unknown as NativeBlock;
}

function blockKind(env: NativeBlockEnvelope): string {
  const block = unwrapBlock(env);
  return String(block.kind || env.kind || "");
}

function approvalCallId(event: Record<string, unknown>): string {
  const record =
    event.record && typeof event.record === "object"
      ? (event.record as Record<string, unknown>)
      : {};
  const tool =
    record.tool && typeof record.tool === "object"
      ? (record.tool as Record<string, unknown>)
      : {};
  return String(
    event.call_id ||
      event.tool_call_id ||
      event.tool_use_id ||
      tool.call_id ||
      "",
  );
}

function approvalEventToEnvelope(
  event: Record<string, unknown>,
): NativeBlockEnvelope | null {
  const approvalId = approvalIdFromEvent(event);
  if (!approvalId) return null;
  return {
    kind: "approval_request",
    block: {
      kind: "approval_request",
      approval_id: approvalId,
      call_id: approvalCallId(event),
      prompt: event.prompt,
      record: event.record,
      reason: event.reason,
      state: event.state,
      resolved_state: event.resolved_state,
    },
  };
}

function mergeApprovalEventsIntoBlocks(
  blocks: NativeBlockEnvelope[],
  approvalEvents: LiveEvent[] = [],
): NativeBlockEnvelope[] {
  if (!approvalEvents.length) return blocks;
  const byId = new Map<string, NativeBlockEnvelope>();
  for (const ev of approvalEvents) {
    const id = approvalIdFromEvent(ev as Record<string, unknown>);
    if (!id) continue;
    if (ev.kind === "approval.request") {
      const env = approvalEventToEnvelope(ev as Record<string, unknown>);
      if (env) byId.set(id, env);
      continue;
    }
    if (ev.kind === "approval.resolved") {
      const existing =
        byId.get(id) ?? approvalEventToEnvelope(ev as Record<string, unknown>);
      if (!existing) continue;
      const block = unwrapBlock(existing);
      existing.block = {
        ...block,
        kind: "approval_request",
        approval_id: id,
        state: String(ev.state || "resolved"),
        resolved_state: String(ev.state || "resolved"),
      };
      byId.set(id, existing);
    }
  }
  if (!byId.size) return blocks;

  const merged = [...blocks];
  const existingApprovalIdx = new Map<string, number>();
  const rebuildApprovalIndex = () => {
    existingApprovalIdx.clear();
    merged.forEach((env, i) => {
      if (blockKind(env) !== "approval_request") return;
      const id = approvalIdFromEvent(unwrapBlock(env) as Record<string, unknown>);
      if (id) existingApprovalIdx.set(id, i);
    });
  };
  const insertIndexAfterCall = (callId: string): number | null => {
    if (!callId) return null;
    for (let i = merged.length - 1; i >= 0; i -= 1) {
      const candidate = unwrapBlock(merged[i]);
      if (
        blockKind(merged[i]) === "tool_result" &&
        String(candidate.call_id || "") === callId
      ) {
        return i + 1;
      }
    }
    return null;
  };
  rebuildApprovalIndex();

  for (const env of byId.values()) {
    const block = unwrapBlock(env);
    const approvalId = approvalIdFromEvent(block as Record<string, unknown>);
    const existingIdx = approvalId ? existingApprovalIdx.get(approvalId) : undefined;
    if (existingIdx !== undefined) {
      const updated = {
        ...merged[existingIdx],
        block: {
          ...unwrapBlock(merged[existingIdx]),
          ...block,
          kind: "approval_request",
          approval_id: approvalId,
        },
      };
      const callId = String(block.call_id || "");
      const targetIdx = insertIndexAfterCall(callId);
      if (targetIdx !== null && targetIdx !== existingIdx + 1) {
        merged.splice(existingIdx, 1);
        const adjustedTarget = targetIdx > existingIdx ? targetIdx - 1 : targetIdx;
        merged.splice(Math.min(adjustedTarget, merged.length), 0, updated);
        rebuildApprovalIndex();
      } else {
        merged[existingIdx] = updated;
      }
      continue;
    }
    const callId = String(block.call_id || "");
    let insertAt = insertIndexAfterCall(callId) ?? merged.length;
    merged.splice(insertAt, 0, env);
    if (approvalId) rebuildApprovalIndex();
  }
  return merged;
}

function toolCallId(block: NativeBlock): string {
  return String(block.call_id || block.tool_use_id || block.id || "");
}

function hasToolResultOutput(block: NativeBlock): boolean {
  if (block.error || block.error_kind) return true;
  if (!Object.prototype.hasOwnProperty.call(block, "result")) return false;
  const result = block.result;
  if (result === undefined || result === null) return false;
  if (typeof result === "string") return result.trim().length > 0;
  if (Array.isArray(result)) return result.length > 0;
  if (typeof result === "object") return Object.keys(recordOf(result)).length > 0;
  return true;
}

function mergeLiveToolResultsIntoBlocks(
  committedBlocks: NativeBlockEnvelope[],
  liveBlocks: NativeBlockEnvelope[] = [],
): NativeBlockEnvelope[] {
  if (!liveBlocks.length || !committedBlocks.length) return committedBlocks;
  const merged = [...committedBlocks];

  const resultIndexByCallId = new Map<string, number>();
  const useIndexByCallId = new Map<string, number>();
  const rebuildIndexes = () => {
    resultIndexByCallId.clear();
    useIndexByCallId.clear();
    merged.forEach((env, index) => {
      const block = unwrapBlock(env);
      const callId = toolCallId(block);
      if (!callId) return;
      const kind = blockKind(env);
      if (kind === "tool_use") useIndexByCallId.set(callId, index);
      if (kind === "tool_result") resultIndexByCallId.set(callId, index);
    });
  };
  rebuildIndexes();

  for (const liveEnv of liveBlocks) {
    if (blockKind(liveEnv) !== "tool_result") continue;
    const liveBlock = unwrapBlock(liveEnv);
    const callId = toolCallId(liveBlock);
    if (!callId || !hasToolResultOutput(liveBlock)) continue;

    const existingIdx = resultIndexByCallId.get(callId);
    if (existingIdx !== undefined) {
      const current = unwrapBlock(merged[existingIdx]);
      if (hasToolResultOutput(current)) continue;
      merged[existingIdx] = {
        ...merged[existingIdx],
        kind: "tool_result",
        block: { ...current, ...liveBlock, kind: "tool_result" },
      };
      continue;
    }

    const useIdx = useIndexByCallId.get(callId);
    const insertAt = useIdx === undefined ? merged.length : useIdx + 1;
    merged.splice(insertAt, 0, {
      ...liveEnv,
      kind: "tool_result",
      block: { ...liveBlock, kind: "tool_result" },
    });
    rebuildIndexes();
  }
  return merged;
}

function normalizedBlockText(value: unknown): string {
  return typeof value === "string" ? value.trim().replace(/\s+/g, " ") : "";
}

function dropDuplicateReplyTextBlocks(
  blocks: NativeBlockEnvelope[],
  replyText: unknown,
): NativeBlockEnvelope[] {
  const normalizedReply = normalizedBlockText(replyText);
  if (!normalizedReply) return blocks;
  return blocks.filter((env) => {
    if (blockKind(env) !== "text") return true;
    const block = unwrapBlock(env);
    return normalizedBlockText(block.text) !== normalizedReply;
  });
}

function NativeTextBlock({ block }: { block: NativeBlock }) {
  const text = typeof block.text === "string" ? block.text.trim() : "";
  if (!text) return null;
  return (
    <div className="rounded-md border border-ink-700/70 bg-ink-800/40 px-3 py-2">
      <div className="flex items-center justify-between gap-2 text-[11px] text-ink-400 font-medium mb-1">
        <span>assistant text</span>
        <CopyButton text={text} />
      </div>
      <Markdown>{text}</Markdown>
    </div>
  );
}

function formatAttachmentBytes(value: unknown): string {
  const size = typeof value === "number" ? value : Number(value || 0);
  if (!Number.isFinite(size) || size <= 0) return "";
  if (size < 1024) return `${size} B`;
  if (size < 1024 * 1024) return `${Math.round(size / 1024)} KB`;
  return `${(size / (1024 * 1024)).toFixed(1)} MB`;
}

function NativeAttachmentBlock({ block }: { block: NativeBlock }) {
  const name = String(block.name || block.title || "attachment");
  const mime = String(block.mime_type || block.media_type || "");
  const src = String(block.data_url || block.url || "");
  const attachmentKind = String(block.attachment_kind || block.kind || "");
  const isImage =
    attachmentKind === "image" ||
    mime.startsWith("image/") ||
    src.startsWith("data:image/");
  const size = formatAttachmentBytes(block.size);
  const source = String(block.source || "");
  return (
    <div className="rounded-md border border-brand-500/25 bg-brand-500/[0.055] overflow-hidden">
      <div className="flex items-center justify-between gap-2 px-3 py-2 text-[11px] text-ink-400 font-medium">
        <span>{source === "tool" ? "tool attachment" : "assistant attachment"}</span>
        <Tag tone={isImage ? "brand" : "neutral"}>{mime || attachmentKind || "file"}</Tag>
      </div>
      {isImage && src ? (
        // eslint-disable-next-line @next/next/no-img-element
        <img
          src={src}
          alt={name}
          className="max-h-[360px] w-full object-contain bg-black/20"
        />
      ) : null}
      <div className="flex items-center gap-2 px-3 py-2 text-sm text-ink-100">
        {!isImage || !src ? <FileIcon size={15} /> : null}
        {src ? (
          <a
            href={src}
            target="_blank"
            rel="noreferrer"
            className="min-w-0 truncate text-ink-100 hover:text-white"
          >
            {name}
          </a>
        ) : (
          <span className="min-w-0 truncate">{name}</span>
        )}
        {size ? <span className="shrink-0 text-xs text-ink-400">{size}</span> : null}
      </div>
      {typeof block.text === "string" && block.text.trim() ? (
        <div className="border-t border-brand-500/15 px-3 py-2 text-xs text-ink-200">
          <Markdown>{block.text}</Markdown>
        </div>
      ) : null}
    </div>
  );
}

function NativeThinkingBlock({
  block,
  defaultOpen = false,
}: {
  block: NativeBlock;
  defaultOpen?: boolean;
}) {
  const text = typeof block.text === "string" ? block.text : "";
  if (!text.trim()) return null;
  return (
    <Collapsible
      title={
        <span className="flex items-center gap-2">
          <span className="text-ink-400">thinking</span>
          <span className="font-mono text-ink-100 truncate">
            {text.slice(0, 56)}
            {text.length > 56 ? "…" : ""}
          </span>
        </span>
      }
      tone="brand"
      badge={<Tag tone="brand">reasoning</Tag>}
      defaultOpen={defaultOpen}
    >
      <pre className="whitespace-pre-wrap text-[11px] font-mono text-ink-200 bg-ink-900/70 border border-ink-700/70 rounded-md p-2 overflow-auto max-h-72">
        {text}
      </pre>
    </Collapsible>
  );
}

function stringArray(value: unknown): string[] {
  if (!Array.isArray(value)) return [];
  return value
    .map((row) => {
      if (typeof row === "string") return row.trim();
      if (row && typeof row === "object") {
        return String((row as Record<string, unknown>).name || "").trim();
      }
      return "";
    })
    .filter(Boolean);
}

function traceTone(status: string): "neutral" | "ok" | "warn" | "err" | "brand" {
  const s = status.toLowerCase();
  if (s === "completed" || s === "done" || s === "ok") return "ok";
  if (s === "failed" || s === "error") return "err";
  if (s === "skipped" || s === "blocked" || s === "completed_with_failures")
    return "warn";
  return "brand";
}

function compactNumber(value: unknown, suffix = ""): ReactNode {
  if (typeof value !== "number" || !Number.isFinite(value)) return null;
  if (suffix === "usd") return <Tag>{`$${value.toFixed(value < 1 ? 4 : 2)}`}</Tag>;
  return <Tag>{`${Math.round(value)}${suffix}`}</Tag>;
}

type AgentAccent = {
  border: string;
  bg: string;
  softBg: string;
  dot: string;
  chip: string;
  text: string;
};

const AGENT_ACCENTS: AgentAccent[] = [
  {
    border: "border-cyan-400/35",
    bg: "bg-cyan-400/[0.055]",
    softBg: "bg-cyan-400/[0.08]",
    dot: "bg-cyan-300",
    chip: "border-cyan-400/40 bg-cyan-400/10 text-cyan-200",
    text: "text-cyan-200",
  },
  {
    border: "border-emerald-400/35",
    bg: "bg-emerald-400/[0.055]",
    softBg: "bg-emerald-400/[0.08]",
    dot: "bg-emerald-300",
    chip: "border-emerald-400/40 bg-emerald-400/10 text-emerald-200",
    text: "text-emerald-200",
  },
  {
    border: "border-amber-400/35",
    bg: "bg-amber-400/[0.055]",
    softBg: "bg-amber-400/[0.08]",
    dot: "bg-amber-300",
    chip: "border-amber-400/40 bg-amber-400/10 text-amber-200",
    text: "text-amber-200",
  },
  {
    border: "border-fuchsia-400/35",
    bg: "bg-fuchsia-400/[0.055]",
    softBg: "bg-fuchsia-400/[0.08]",
    dot: "bg-fuchsia-300",
    chip: "border-fuchsia-400/40 bg-fuchsia-400/10 text-fuchsia-200",
    text: "text-fuchsia-200",
  },
  {
    border: "border-sky-400/35",
    bg: "bg-sky-400/[0.055]",
    softBg: "bg-sky-400/[0.08]",
    dot: "bg-sky-300",
    chip: "border-sky-400/40 bg-sky-400/10 text-sky-200",
    text: "text-sky-200",
  },
  {
    border: "border-rose-400/35",
    bg: "bg-rose-400/[0.055]",
    softBg: "bg-rose-400/[0.08]",
    dot: "bg-rose-300",
    chip: "border-rose-400/40 bg-rose-400/10 text-rose-200",
    text: "text-rose-200",
  },
];

function hashText(value: string): number {
  let hash = 0;
  for (let i = 0; i < value.length; i += 1) {
    hash = (hash * 31 + value.charCodeAt(i)) >>> 0;
  }
  return hash;
}

function agentAccent(name: string, index = 0): AgentAccent {
  const key = name.trim();
  if (key) return AGENT_ACCENTS[hashText(key) % AGENT_ACCENTS.length];
  return AGENT_ACCENTS[index % AGENT_ACCENTS.length];
}

function AgentChip({
  accent,
  children,
}: {
  accent: AgentAccent;
  children: ReactNode;
}) {
  return (
    <span
      className={`inline-flex items-center gap-1 rounded border px-1.5 py-0.5 text-[10px] font-mono ${accent.chip}`}
    >
      <span className={`h-1.5 w-1.5 rounded-full ${accent.dot}`} />
      {children}
    </span>
  );
}

function hasReadableValue(value: unknown): boolean {
  if (value === undefined || value === null || value === "") return false;
  if (Array.isArray(value)) return value.length > 0;
  if (typeof value === "object") return Object.keys(recordOf(value)).length > 0;
  return true;
}

function textFromContent(value: unknown): string {
  if (!Array.isArray(value)) return "";
  return value
    .map((row) => {
      if (typeof row === "string") return row.trim();
      const record = recordOf(row);
      return String(
        record.text ||
          record.content ||
          record.message ||
          record.summary ||
          "",
      ).trim();
    })
    .filter(Boolean)
    .join("\n\n");
}

function readableText(value: unknown): string {
  if (typeof value === "string") {
    const trimmed = value.trim();
    const parsed = parseJsonLike(trimmed);
    if (parsed) {
      const parsedText = readableText(parsed);
      if (parsedText) return parsedText;
    }
    return trimmed;
  }
  if (typeof value === "number" || typeof value === "boolean") return String(value);
  const record = recordOf(value);
  const direct = String(
    record.summary ||
      record.operator_summary_text ||
      record.final_text ||
      record.reply_text ||
      record.message ||
      record.text ||
      record.output ||
      record.error ||
      "",
  ).trim();
  if (direct && direct !== "[object Object]") return direct;
  const contentText = textFromContent(record.content);
  if (contentText) return contentText;
  const nested = record.result && record.result !== value ? readableText(record.result) : "";
  return nested;
}

function compactDisplayValue(value: unknown): string {
  if (value === undefined || value === null || value === "") return "";
  if (typeof value === "string") return value.trim();
  if (typeof value === "number" || typeof value === "boolean") return String(value);
  if (Array.isArray(value)) {
    const parts = value
      .slice(0, 6)
      .map((item) => readableText(item) || compactDisplayValue(item))
      .filter(Boolean);
    const suffix = value.length > parts.length ? ` +${value.length - parts.length}` : "";
    return parts.join(", ") + suffix;
  }
  const text = readableText(value);
  if (text) return text;
  return "";
}

function structuredEntries(value: unknown): Array<[string, string]> {
  const record = recordOf(value);
  const entries: Array<[string, string]> = [];
  for (const [key, item] of Object.entries(record)) {
    if (
      item === undefined ||
      item === null ||
      key === "raw" ||
      key === "prompt" ||
      key === "assignment_prompt" ||
      key === "role_prompt"
    ) {
      continue;
    }
    if (typeof item === "object" && !Array.isArray(item)) {
      const text = readableText(item);
      if (text) entries.push([key, text]);
      continue;
    }
    const text = compactDisplayValue(item);
    if (text) entries.push([key, text]);
  }
  return entries.slice(0, 12);
}

function parseJsonLike(value: string): unknown | null {
  if (!value || !/^[{\[]/.test(value)) return null;
  try {
    const parsed = JSON.parse(value) as unknown;
    return parsed && typeof parsed === "object" ? parsed : null;
  } catch {
    return null;
  }
}

function OutputPanel({
  label,
  value,
  accent,
}: {
  label: string;
  value: unknown;
  accent?: AgentAccent;
}) {
  if (!hasReadableValue(value)) return null;
  const text = readableText(value);
  const entries = text ? [] : structuredEntries(value);
  const chrome = accent
    ? `${accent.border} ${accent.softBg}`
    : "border-ink-700/60 bg-ink-900/40";
  return (
    <div className={`rounded-md border ${chrome} px-3 py-2 space-y-1.5`}>
      <div className="flex items-center justify-between gap-2">
        <div className="text-[11px] text-ink-400 font-medium">
          {label}
        </div>
        {text ? <CopyButton text={text} /> : null}
      </div>
      {text ? (
        <div className="text-[12px] leading-relaxed text-ink-100">
          <Markdown>{text}</Markdown>
        </div>
      ) : entries.length ? (
        <div className="grid grid-cols-1 sm:grid-cols-2 gap-1.5">
          {entries.map(([key, item]) => (
            <div
              key={key}
              className="rounded border border-ink-700/60 bg-black/10 px-2 py-1.5"
            >
              <div className="text-[10px] text-ink-500 font-mono truncate">
                {key}
              </div>
              <div className="mt-0.5 text-[11px] leading-relaxed text-ink-100 break-words">
                {item}
              </div>
            </div>
          ))}
        </div>
      ) : (
        <JsonBlock value={value} />
      )}
    </div>
  );
}

function RawDetails({
  title = "raw details",
  children,
}: {
  title?: ReactNode;
  children: ReactNode;
}) {
  return (
    <Collapsible title={title} tone="neutral" defaultOpen={false}>
      <div className="space-y-2">{children}</div>
    </Collapsible>
  );
}

function payloadRoles(payload: unknown): string[] {
  const p = recordOf(payload);
  return stringArray(p.roles);
}

function TextPanel({
  label,
  text,
  maxH = "max-h-72",
}: {
  label: string;
  text: unknown;
  maxH?: string;
}) {
  const body = typeof text === "string" ? text : "";
  if (!body.trim()) return null;
  return (
    <div className="space-y-1">
      <div className="flex items-center justify-between gap-2">
        <div className="text-[11px] text-ink-400 font-medium">
          {label}
        </div>
        <CopyButton text={body} />
      </div>
      <pre
        className={`whitespace-pre-wrap text-[11px] font-mono text-ink-200 bg-ink-900/70 border border-ink-700/70 rounded-md p-2 overflow-auto ${maxH}`}
      >
        {body}
      </pre>
    </div>
  );
}

function JsonPanel({ label, value }: { label: string; value: unknown }) {
  if (value === undefined || value === null || value === "") return null;
  if (Array.isArray(value) && value.length === 0) return null;
  if (
    typeof value === "object" &&
    !Array.isArray(value) &&
    Object.keys(recordOf(value)).length === 0
  ) {
    return null;
  }
  return (
    <div className="space-y-1">
      <div className="text-[11px] text-ink-400 font-medium">
        {label}
      </div>
      <JsonBlock value={value} />
    </div>
  );
}

function stepAgentName(step: Record<string, unknown>, fallback = "team"): string {
  return String(
    step.subagent ||
      step.name ||
      step.team_task_owner ||
      step.from_agent ||
      step.owner ||
      fallback,
  );
}

function stepTitle(step: Record<string, unknown>): string {
  const kind = String(
    step.step_kind ||
      step.kind ||
      step.lifecycle ||
      step.phase ||
      "step",
  );
  if (kind === "act" && step.skill) {
    const action = step.action ? `.${String(step.action)}` : "";
    return `${String(step.skill)}${action}`;
  }
  if (kind.startsWith("subagent.") && step.skill) {
    const action = step.action ? `.${String(step.action)}` : "";
    return `${kind} ${String(step.skill)}${action}`;
  }
  return kind;
}

function AgentStepCard({
  step,
  accent,
  live = false,
  current = false,
}: {
  step: Record<string, unknown>;
  accent: AgentAccent;
  live?: boolean;
  current?: boolean;
}) {
  const agent = stepAgentName(step);
  const status = String(step.status || (current ? "running" : "ok"));
  const parsedKeys = stringArray(step.parsed_keys);
  const promptText = typeof step.prompt === "string" ? step.prompt : "";
  const reasoningText =
    typeof step.reasoning === "string" ? step.reasoning : "";
  const payload = step.payload || step.input_payload;
  const output = step.output || step.outcomes;
  const hasPromptOrReasoning =
    Boolean(promptText.trim()) || Boolean(reasoningText.trim());
  const hasStructured =
    hasReadableValue(payload) ||
    hasReadableValue(output) ||
    hasReadableValue(step.metrics);
  const hasRaw =
    hasStructured ||
    hasReadableValue(step.raw) ||
    hasReadableValue(step.results) ||
    hasReadableValue(step.failures);
  return (
    <div
      className={`rounded-md border ${accent.border} ${
        current ? accent.softBg : "bg-ink-900/35"
      } px-2.5 py-2 ${current ? "ring-1 ring-fluid-400/30" : ""}`}
    >
      <div className="flex items-start gap-2">
        <div className={`mt-1.5 h-2 w-2 rounded-full ${accent.dot} shrink-0`} />
        <div className="min-w-0 flex-1 space-y-2">
          <div className="flex items-center gap-1.5 flex-wrap">
            <span className="text-xs text-ink-100">{stepTitle(step)}</span>
            {agent && agent !== "team" ? (
              <AgentChip accent={accent}>{agent}</AgentChip>
            ) : null}
            {current ? (
              <span className="inline-flex items-center gap-1 text-[10px] text-fluid-400">
                {live ? <span className="typing-dot" /> : null}
                <span>current</span>
              </span>
            ) : null}
            {typeof step.iteration === "number" ? (
              <Tag>{`iter ${step.iteration}`}</Tag>
            ) : null}
            {status ? <Tag tone={traceTone(status)}>{status}</Tag> : null}
            {step.provider ? <Tag>{String(step.provider)}</Tag> : null}
            {step.model ? <Tag>{String(step.model)}</Tag> : null}
            {step.skill && stepTitle(step) !== String(step.skill) ? (
              <Tag>{String(step.skill)}</Tag>
            ) : null}
            {step.action ? <Tag>{String(step.action)}</Tag> : null}
            {step.task_id ? <Tag>{String(step.task_id)}</Tag> : null}
            {step.prompt_chars ? <Tag>{`${String(step.prompt_chars)} chars`}</Tag> : null}
            {compactNumber(step.tokens)}
            {compactNumber(step.usd, "usd")}
            {compactNumber(step.wall_ms, "ms")}
          </div>
          {parsedKeys.length ? (
            <div className="flex items-center gap-1 flex-wrap">
              {parsedKeys.map((key) => (
                <Tag key={key}>{key}</Tag>
              ))}
            </div>
          ) : null}
          {step.subject ? (
            <div className="text-[11px] text-ink-300 break-words">
              {String(step.subject)}
            </div>
          ) : null}
          {step.summary ? (
            <div className="text-[11px] text-ink-200 break-words">
              {String(step.summary)}
            </div>
          ) : null}
          {step.content ? (
            <div className="text-[11px] text-ink-200 break-words">
              <span className="text-ink-500">
                {String(step.from_agent || agent || "agent")}
                {step.to ? ` -> ${String(step.to)}` : ""}:{" "}
              </span>
              {String(step.content)}
            </div>
          ) : null}
          {step.error ? (
            <div className="text-[11px] text-[#ef5564] break-words">
              {String(step.error)}
            </div>
          ) : null}
          {hasPromptOrReasoning ? (
            <div className="grid grid-cols-1 gap-2">
              <TextPanel label="prompt" text={promptText} maxH="max-h-72" />
              <TextPanel
                label="reasoning summary"
                text={reasoningText}
                maxH="max-h-56"
              />
            </div>
          ) : null}
          {hasStructured ? (
            <div className="grid grid-cols-1 gap-2">
              <OutputPanel label="input payload" value={payload} accent={accent} />
              <OutputPanel label="output" value={output} accent={accent} />
              <OutputPanel label="metrics" value={step.metrics} accent={accent} />
            </div>
          ) : null}
          {hasRaw ? (
            <RawDetails title="raw event data">
              <JsonPanel label="payload" value={payload} />
              <JsonPanel label="raw output" value={output} />
              <JsonPanel label="metrics" value={step.metrics} />
              <JsonPanel label="results" value={step.results} />
              <JsonPanel label="failures" value={step.failures} />
              <JsonPanel label="raw event" value={step.raw} />
            </RawDetails>
          ) : null}
        </div>
      </div>
    </div>
  );
}

function NativeAgentToolIntro({ block }: { block: NativeBlock }) {
  const action = String(block.action || "");
  const payload = recordOf(block.payload);
  const isTeam = action === "team_run";
  const isSubagent = isSubagentToolAction(action);
  if (!isTeam && !isSubagent) return null;

  const roles = isTeam ? payloadRoles(payload) : [];
  const subagentName = String(payload.name || payload.subagent || "").trim();
  const task = String(payload.task || payload.prompt || payload.goal || "").trim();
  return (
    <div className="rounded-md border border-brand-500/20 bg-brand-500/[0.04] px-3 py-2 space-y-2">
      <div className="flex items-center gap-1.5 flex-wrap">
        <Tag tone="brand">{isTeam ? "Agent Team" : "Sub Agent"}</Tag>
        {subagentName ? <Tag>{subagentName}</Tag> : null}
        {roles.map((role) => (
          <Tag key={role}>{role}</Tag>
        ))}
      </div>
      {task ? (
        <div className="text-xs leading-relaxed text-ink-200">{task}</div>
      ) : null}
    </div>
  );
}

function isSubagentToolAction(action: string): boolean {
  return action === "subagent_run" || action === "subagent_run_async";
}

function isAgentTool(block: NativeBlock): boolean {
  const action = String(block.action || "");
  return action === "team_run" || isSubagentToolAction(action);
}

function NativeToolUseBlock({
  block,
  defaultOpen = false,
  pending = false,
}: {
  block: NativeBlock;
  defaultOpen?: boolean;
  pending?: boolean;
}) {
  // High-traffic tools get a dedicated readable card instead of the
  // generic Collapsible. Both renderers also handle the matching
  // tool_result inside ``NativeToolResultBlock``.
  if (isTodoWrite(block)) {
    return <TodoChecklistCard todos={todosFromBlock(block)} pending={pending} />;
  }
  if (isSkillTool(block)) {
    return <SkillLoadCard block={block} variant="use" pending={pending} />;
  }
  if (isFileOp(block)) {
    return <FileOpCard block={block} variant="use" pending={pending} />;
  }
  if (isShellTool(block)) {
    return <ShellCard block={block} variant="use" pending={pending} />;
  }
  if (isWebTool(block)) {
    return <WebCard block={block} variant="use" pending={pending} />;
  }

  const name =
    (block.action as string | undefined) ||
    (block.skill_id as string | undefined) ||
    "tool";
  const skill = block.skill_id as string | undefined;
  const action = String(block.action || "");
  const agentLabel =
    action === "team_run"
      ? "Agent Team"
      : isSubagentToolAction(action)
      ? "Sub Agent"
      : "tool";
  const title = (
    <span className="flex items-center gap-2 min-w-0">
      <WrenchIcon size={13} className="text-ink-400" />
      <span className="text-ink-400">{agentLabel}</span>
      <span className="font-mono text-ink-100 truncate">
        {skill && skill !== "native" ? `${skill}.${name}` : name}
      </span>
      {pending ? (
        <span className="inline-flex items-center gap-1 text-[10px] text-fluid-400">
          <span className="typing-dot" />
          <span>running</span>
        </span>
      ) : null}
    </span>
  );
  return (
    <Collapsible
      title={title}
      tone="brand"
      defaultOpen={defaultOpen}
      badge={
        <Tag tone={pending ? "warn" : "brand"}>
          {(block.call_id as string | undefined)?.slice(0, 8) || "call"}
        </Tag>
      }
    >
      <div className="space-y-2">
        <NativeAgentToolIntro block={block} />
        <div className="text-[11px] text-ink-400 font-medium">
          input
        </div>
        <JsonBlock value={block.payload ?? {}} />
      </div>
    </Collapsible>
  );
}

function AgentToolResultCard({ block }: { block: NativeBlock }) {
  const action = String(block.action || "");
  const result = recordOf(block.result);
  const payload = recordOf(block.payload);
  const ok = block.ok !== false && !block.error;
  if (action === "team_run") {
    const rows = arrayOfRecords(result.results);
    const failures = arrayOfRecords(result.failures);
    const rolesSucceeded = stringArray(result.roles_succeeded);
    const rolesFailed = stringArray(result.roles_failed);
    const title = String(
      result.team_template ||
        result.team_run_id ||
        payload.team_template ||
        payload.task ||
        "Agent Team",
    );
    return (
      <Collapsible
        title={
          <span className="flex items-center gap-2 min-w-0">
            <span className="text-ink-400">Agent Team</span>
            <span className="font-mono text-ink-100 truncate">{title}</span>
          </span>
        }
        tone={ok ? "ok" : "err"}
        badge={<Tag tone={ok ? "ok" : "err"}>{ok ? "completed" : "error"}</Tag>}
        defaultOpen={false}
      >
        <div className="space-y-3">
          {block.error ? (
            <div className="text-xs text-[#ef5564]">{String(block.error)}</div>
          ) : null}
          <div className="flex items-center gap-1 flex-wrap">
            {rolesSucceeded.map((role, i) => (
              <AgentChip key={`ok-${role}`} accent={agentAccent(role, i)}>
                {role}
              </AgentChip>
            ))}
            {rolesFailed.map((role) => (
              <Tag key={`fail-${role}`} tone="err">{role}</Tag>
            ))}
          </div>
          <OutputPanel
            label="team aggregate"
            value={result.aggregated || result.final_context || result.summary}
          />
          {rows.length ? (
            <div className="grid grid-cols-1 sm:grid-cols-2 gap-2">
              {rows.map((row, i) => {
                const name = String(row.subagent || row.name || `agent-${i + 1}`);
                const accent = agentAccent(name, i);
                return (
                  <div
                    key={`${name}-${i}`}
                    className={`rounded-md border ${accent.border} ${accent.bg} px-3 py-2 space-y-2`}
                  >
                    <div className="flex items-center justify-between gap-2">
                      <AgentChip accent={accent}>{name}</AgentChip>
                      {row.status ? (
                        <Tag tone={traceTone(String(row.status))}>
                          {String(row.status)}
                        </Tag>
                      ) : null}
                    </div>
                    {row.error ? (
                      <div className="text-[11px] text-[#ef5564] break-words">
                        {String(row.error)}
                      </div>
                    ) : null}
                    <OutputPanel
                      label="output"
                      value={row.output || row.summary || row.result}
                      accent={accent}
                    />
                  </div>
                );
              })}
            </div>
          ) : null}
          {failures.length ? (
            <div className="grid grid-cols-1 sm:grid-cols-2 gap-2">
              {failures.map((row, i) => {
                const name = String(row.subagent || row.name || `failed-${i + 1}`);
                return (
                  <div
                    key={`${name}-${i}`}
                    className="rounded-md border border-[#ef5564]/35 bg-[#ef5564]/[0.06] px-3 py-2"
                  >
                    <div className="flex items-center justify-between gap-2">
                      <Tag tone="err">{name}</Tag>
                      <Tag tone="err">failed</Tag>
                    </div>
                    <div className="mt-1.5 text-[11px] text-[#ef5564] break-words">
                      {String(row.error || row.message || "unknown error")}
                    </div>
                  </div>
                );
              })}
            </div>
          ) : null}
          <RawDetails>
            <JsonPanel label="payload" value={payload} />
            <JsonPanel label="raw team result" value={block.result} />
          </RawDetails>
        </div>
      </Collapsible>
    );
  }

  if (isSubagentToolAction(action)) {
    const name = String(
      result.subagent || result.name || payload.name || payload.subagent || "subagent",
    );
    const accent = agentAccent(name);
    return (
      <Collapsible
        title={
          <span className="flex items-center gap-2 min-w-0">
            <span className="text-ink-400">Sub Agent</span>
            <span className="font-mono text-ink-100 truncate">{name}</span>
          </span>
        }
        tone={ok ? "ok" : "err"}
        badge={<Tag tone={ok ? "ok" : "err"}>{ok ? "completed" : "error"}</Tag>}
        defaultOpen={false}
        chrome={{ border: accent.border, bg: accent.bg }}
        bodyClassName={accent.border}
      >
        <div className="space-y-3">
          {block.error ? (
            <div className="text-xs text-[#ef5564]">{String(block.error)}</div>
          ) : null}
          <div className="flex items-center gap-1 flex-wrap">
            <AgentChip accent={accent}>{name}</AgentChip>
            {compactNumber(result.tokens)}
            {compactNumber(result.usd, "usd")}
            {compactNumber(result.wall_ms, "ms")}
          </div>
          <OutputPanel
            label="output"
            value={result.output || result.summary || result.result || block.result}
            accent={accent}
          />
          <RawDetails>
            <JsonPanel label="payload" value={payload} />
            <JsonPanel label="raw subagent result" value={block.result} />
          </RawDetails>
        </div>
      </Collapsible>
    );
  }
  return null;
}

function NativeTeamTraceBlock({
  block,
  defaultOpen = false,
  live = false,
}: {
  block: NativeBlock;
  defaultOpen?: boolean;
  live?: boolean;
}) {
  const status = String(block.status || (live ? "running" : "completed"));
  const roles = stringArray(block.roles);
  const members = recordOf(block.members);
  const task = String(block.task || "");
  const runId = String(block.run_id || block.team_key || "");
  const templateId = String(block.template_id || "");
  const phase = String(block.phase || "");
  const goal = String(block.goal || "");
  const steps = arrayOfRecords(block.steps);
  const result = recordOf(block.result);
  const teamRows = arrayOfRecords(block.results || result.results);
  const teamFailures = arrayOfRecords(block.failures || result.failures);
  const teamAggregate =
    block.aggregated || result.aggregated || result.final_context || result.summary;
  const memberNames = Array.from(
    new Set([
      ...roles,
      ...Object.keys(members),
      ...teamRows.map((row, i) =>
        String(row.subagent || row.name || `agent-${i + 1}`),
      ),
    ]),
  );
  const title = (
    <span className="flex items-center gap-2 min-w-0">
      <span className="text-ink-400">Agent Team</span>
      <span className="font-mono text-ink-100 truncate">
        {task ? task.slice(0, 60) : "team run"}
        {task.length > 60 ? "..." : ""}
      </span>
      {live && status === "running" ? (
        <span className="inline-flex items-center gap-1 text-[10px] text-fluid-400">
          <span className="typing-dot" />
          <span>running</span>
        </span>
      ) : null}
    </span>
  );
  return (
    <Collapsible
      title={title}
      tone={traceTone(status)}
      defaultOpen={defaultOpen || live || status === "running"}
      badge={<Tag tone={traceTone(status)}>{status}</Tag>}
      right={
        <span className="flex items-center gap-1">
          {compactNumber(block.tokens_total)}
          {compactNumber(block.usd_total, "usd")}
        </span>
      }
    >
      <div className="space-y-3">
        <div className="flex items-center gap-1 flex-wrap">
          {runId ? <Tag>{runId}</Tag> : null}
          {templateId ? <Tag tone="brand">{templateId}</Tag> : null}
          {phase ? <Tag tone="brand">{`phase: ${phase}`}</Tag> : null}
        </div>
        {task ? <div className="text-xs text-ink-200">{task}</div> : null}
        {goal && goal !== task ? (
          <div className="text-[11px] text-ink-300 leading-relaxed">{goal}</div>
        ) : null}
        {block.collaboration_model ? (
          <div className="rounded-md border border-brand-500/20 bg-brand-500/[0.04] px-2.5 py-2 text-[11px] text-ink-300 leading-relaxed">
            {String(block.collaboration_model)}
          </div>
        ) : null}
        <OutputPanel label="team aggregate" value={teamAggregate} />
        {teamFailures.length ? (
          <div className="grid grid-cols-1 sm:grid-cols-2 gap-2">
            {teamFailures.map((failure, i) => {
              const name = String(
                failure.subagent || failure.name || `failed-${i + 1}`,
              );
              return (
                <div
                  key={`${name}-${i}`}
                  className="rounded-md border border-[#ef5564]/35 bg-[#ef5564]/[0.06] px-3 py-2"
                >
                  <div className="flex items-center justify-between gap-2">
                    <Tag tone="err">{name}</Tag>
                    <Tag tone="err">failed</Tag>
                  </div>
                  <div className="mt-1.5 text-[11px] text-[#ef5564] break-words">
                    {String(failure.error || failure.message || "unknown error")}
                  </div>
                </div>
              );
            })}
          </div>
        ) : null}
        {memberNames.length ? (
          <div className="grid grid-cols-1 sm:grid-cols-2 gap-1.5">
            {memberNames.map((name, i) => {
              const resultRow = teamRows.find(
                (row, j) =>
                  String(row.subagent || row.name || `agent-${j + 1}`) === name,
              );
              const member = {
                ...recordOf(resultRow),
                ...recordOf(members[name]),
              };
              const accent = agentAccent(name, i);
              const state = String(
                member.status ||
                  (member.error
                    ? "failed"
                    : member.output || member.summary || member.result
                    ? "completed"
                    : "planned"),
              );
              const memberSteps = arrayOfRecords(member.steps);
              const promptStep = [...memberSteps]
                .reverse()
                .find((s) => typeof s.prompt === "string" && String(s.prompt).trim());
              const hasMemberRaw =
                hasReadableValue(member.assignment_prompt) ||
                hasReadableValue(member.role_prompt) ||
                hasReadableValue(member.last_prompt || promptStep?.prompt) ||
                hasReadableValue(member.payload || member.input_payload) ||
                hasReadableValue(member.output) ||
                hasReadableValue(member.metrics);
              return (
                <Collapsible
                  key={name}
                  title={
                    <span className="flex items-center gap-2 min-w-0">
                      <span className={`h-2 w-2 rounded-full ${accent.dot}`} />
                      <span className="text-ink-400">member</span>
                      <span className={`font-mono truncate ${accent.text}`}>
                        {name}
                      </span>
                    </span>
                  }
                  tone={traceTone(state)}
                  defaultOpen={
                    defaultOpen ||
                    live ||
                    state === "running"
                  }
                  badge={<Tag tone={traceTone(state)}>{state}</Tag>}
                  chrome={{ border: accent.border, bg: accent.bg }}
                  bodyClassName={accent.border}
                  right={
                    <span className="flex items-center gap-1">
                      {compactNumber(member.tokens)}
                      {compactNumber(member.usd, "usd")}
                      {compactNumber(member.wall_ms, "ms")}
                    </span>
                  }
                >
                  <div className="space-y-2">
                    <div className="flex items-center gap-1 flex-wrap">
                      {member.team_task_id ? (
                        <Tag tone="brand">{String(member.team_task_id)}</Tag>
                      ) : null}
                      {member.tier ? <Tag>{String(member.tier)}</Tag> : null}
                      {Array.isArray(member.payload_keys)
                        ? member.payload_keys.map((key) => (
                            <Tag key={String(key)}>{String(key)}</Tag>
                          ))
                        : null}
                    </div>
                    {member.team_task_subject ? (
                      <div className="text-[11px] text-ink-300 leading-relaxed">
                        {String(member.team_task_subject)}
                      </div>
                    ) : null}
                    {member.error ? (
                      <div className="text-[11px] text-[#ef5564] break-words">
                        {String(member.error)}
                      </div>
                    ) : null}
                    <OutputPanel
                      label="output"
                      value={member.output || member.summary || member.result}
                      accent={accent}
                    />
                    {hasMemberRaw ? (
                      <RawDetails title="prompts and raw I/O">
                        <TextPanel
                          label="assignment prompt"
                          text={member.assignment_prompt}
                        />
                        <TextPanel
                          label="role prompt"
                          text={member.role_prompt}
                          maxH="max-h-56"
                        />
                        <TextPanel
                          label="runtime prompt sent to subagent"
                          text={member.last_prompt || promptStep?.prompt}
                          maxH="max-h-96"
                        />
                        <JsonPanel
                          label="input payload"
                          value={member.payload || member.input_payload}
                        />
                        <JsonPanel label="raw output" value={member.output} />
                        <JsonPanel label="metrics" value={member.metrics} />
                      </RawDetails>
                    ) : null}
                    {memberSteps.length ? (
                      <Collapsible
                        title="member step stream"
                        tone="neutral"
                        defaultOpen={
                          defaultOpen ||
                          live ||
                          state === "running"
                        }
                        badge={<Tag>{memberSteps.length}</Tag>}
                      >
                        <div className="space-y-1.5">
                          {memberSteps.map((step, j) => {
                            return (
                              <AgentStepCard
                                key={j}
                                step={step}
                                accent={accent}
                                live={live}
                                current={
                                  live &&
                                  state === "running" &&
                                  j === memberSteps.length - 1
                                }
                              />
                            );
                          })}
                        </div>
                      </Collapsible>
                    ) : null}
                  </div>
                </Collapsible>
              );
            })}
          </div>
        ) : null}
        {steps.length ? (
          <div className="space-y-1.5">
            <div className="text-[11px] text-ink-400 font-medium">
              team timeline
            </div>
            {steps.map((step, i) => {
              const stepAgent = stepAgentName(step);
              const accent = agentAccent(stepAgent, i);
              return (
                <AgentStepCard
                  key={i}
                  step={step}
                  accent={accent}
                  live={live}
                  current={live && status === "running" && i === steps.length - 1}
                />
              );
            })}
          </div>
        ) : null}
      </div>
    </Collapsible>
  );
}

function NativeSubagentTraceBlock({
  block,
  defaultOpen = false,
  live = false,
}: {
  block: NativeBlock;
  defaultOpen?: boolean;
  live?: boolean;
}) {
  const name = String(block.subagent || "subagent");
  const accent = agentAccent(name);
  const status = String(block.status || (live ? "running" : "completed"));
  const steps = arrayOfRecords(block.steps);
  const payloadKeys = stringArray(block.payload_keys);
  const teamRunId = String(block.team_run_id || "");
  const teamTaskId = String(block.team_task_id || "");
  const teamTaskSubject = String(block.team_task_subject || "");
  const hasSubagentRaw =
    hasReadableValue(block.role_prompt) ||
    hasReadableValue(block.last_prompt) ||
    hasReadableValue(block.payload) ||
    hasReadableValue(block.output) ||
    hasReadableValue(block.metrics);
  const title = (
    <span className="flex items-center gap-2 min-w-0">
      <span className={`h-2 w-2 rounded-full ${accent.dot}`} />
      <span className="text-ink-400">Sub Agent</span>
      <span className={`font-mono truncate ${accent.text}`}>{name}</span>
      {live && status === "running" ? (
        <span className="inline-flex items-center gap-1 text-[10px] text-fluid-400">
          <span className="typing-dot" />
          <span>working</span>
        </span>
      ) : null}
    </span>
  );
  return (
    <Collapsible
      title={title}
      tone={traceTone(status)}
      defaultOpen={defaultOpen || live || status === "running"}
      badge={<Tag tone={traceTone(status)}>{status}</Tag>}
      chrome={{ border: accent.border, bg: accent.bg }}
      bodyClassName={accent.border}
      right={
        <span className="flex items-center gap-1">
          {compactNumber(block.tokens)}
          {compactNumber(block.usd, "usd")}
          {compactNumber(block.wall_ms, "ms")}
        </span>
      }
    >
      <div className="space-y-3">
        {teamRunId || teamTaskId || teamTaskSubject ? (
          <div className="space-y-1.5">
            <div className="flex items-center gap-1 flex-wrap">
              {teamRunId ? <Tag>{teamRunId}</Tag> : null}
              {teamTaskId ? <Tag tone="brand">{teamTaskId}</Tag> : null}
            </div>
            {teamTaskSubject ? (
              <div className="text-[11px] text-ink-300 leading-relaxed">
                {teamTaskSubject}
              </div>
            ) : null}
          </div>
        ) : null}
        {payloadKeys.length ? (
          <div className="flex items-center gap-1 flex-wrap">
            {payloadKeys.map((key) => (
              <Tag key={key}>{key}</Tag>
            ))}
          </div>
        ) : null}
        <OutputPanel label="output" value={block.output} accent={accent} />
        {hasSubagentRaw ? (
          <RawDetails title="prompts and raw I/O">
            <TextPanel label="role prompt" text={block.role_prompt} maxH="max-h-56" />
            <TextPanel
              label="runtime prompt sent to subagent"
              text={block.last_prompt}
              maxH="max-h-96"
            />
            <JsonPanel label="input payload" value={block.payload} />
            <JsonPanel label="raw output" value={block.output} />
            <JsonPanel label="metrics" value={block.metrics} />
          </RawDetails>
        ) : null}
        {steps.length ? (
          <div className="space-y-1.5">
            {steps.map((step, i) => {
              return (
                <AgentStepCard
                  key={i}
                  step={step}
                  accent={accent}
                  live={live}
                  current={live && status === "running" && i === steps.length - 1}
                />
              );
            })}
          </div>
        ) : null}
      </div>
    </Collapsible>
  );
}

function NativeToolResultBlock({
  block,
  defaultOpen = false,
}: {
  block: NativeBlock;
  defaultOpen?: boolean;
}) {
  // High-traffic tool results get a dedicated readable card.
  if (isTodoWrite(block)) {
    return <TodoChecklistCard todos={todosFromBlock(block)} />;
  }
  if (isSkillTool(block)) {
    return <SkillLoadCard block={block} variant="result" />;
  }
  if (isFileOp(block)) {
    return <FileOpCard block={block} variant="result" />;
  }
  if (isShellTool(block)) {
    return <ShellCard block={block} variant="result" />;
  }
  if (isWebTool(block)) {
    return <WebCard block={block} variant="result" />;
  }
  if (isAgentTool(block)) {
    return <AgentToolResultCard block={block} />;
  }
  const ok = block.ok !== false && !block.error;
  const action = (block.action as string | undefined) || "tool";
  const skill = (block.skill_id as string | undefined) || "native";
  const strategyProposal = strategyProposalFromToolResult(block.result ?? block, action);
  const title = (
    <span className="flex items-center gap-2 min-w-0">
      <WrenchIcon size={13} className="text-ink-400" />
      <span className="font-mono text-ink-100 truncate">
        {skill !== "native" ? `${skill}.${action}` : action}
      </span>
    </span>
  );
  const badges = (
    <span className="flex items-center gap-1">
      {ok ? (
        <Tag tone="ok">ok</Tag>
      ) : (
        <Tag tone="err">{(block.error_kind as string | undefined) || "error"}</Tag>
      )}
      {typeof block.elapsed_ms === "number" ? (
        <Tag>{block.elapsed_ms}ms</Tag>
      ) : null}
    </span>
  );
  return (
    <Collapsible
      title={title}
      tone={ok ? "neutral" : "err"}
      defaultOpen={defaultOpen}
      badge={badges}
    >
      <div className="space-y-2">
        {block.error ? (
          <div className="text-xs text-[#ef5564]">{String(block.error)}</div>
        ) : null}
        <div className="text-[11px] text-ink-400 font-medium">
          output
        </div>
        {strategyProposal ? (
          <StrategyProposalApprovalCard
            proposal={strategyProposal}
            compact
            approveNote="approved from chat tool result"
          />
        ) : null}
        <JsonBlock value={block.result ?? block} />
      </div>
    </Collapsible>
  );
}

export function NativeBlocksTrack({
  envelopes,
  live = false,
  label = "",
  pendingApprovals,
  onApprovalAction,
  resolvingApprovalIds,
}: {
  envelopes: NativeBlockEnvelope[];
  live?: boolean;
  label?: string;
  pendingApprovals?: Map<string, ApprovalCard>;
  onApprovalAction?: (callbackData: string) => void;
  resolvingApprovalIds?: Set<string>;
}) {
  if (!envelopes.length) return null;
  // Find the index of the last block that hasn't yet committed (a
  // tool_use that has no matching tool_result yet, or a partial text
  // block). That block stays expanded while we're streaming so the
  // operator sees what the model is doing right now.
  let pendingIdx = -1;
  if (live) {
    const openCalls = new Map<string, number>();
    envelopes.forEach((env, i) => {
      const block = env.block ?? (env as unknown as NativeBlock);
      const k = (block.kind || env.kind || "").toString();
      if (k === "tool_use") {
        openCalls.set(String(block.call_id || ""), i);
      } else if (k === "tool_result") {
        openCalls.delete(String(block.call_id || ""));
      }
    });
    if (openCalls.size > 0) {
      pendingIdx = Math.max(...Array.from(openCalls.values()));
    } else {
      pendingIdx = envelopes.length - 1;
    }
  }
  const teamRunIds = new Set<string>();
  envelopes.forEach((env) => {
    const block = unwrapBlock(env);
    const kind = (block.kind || env.kind || "").toString();
    if (kind !== "team_trace") return;
    const id = String(block.run_id || block.team_key || "");
    if (id) teamRunIds.add(id);
  });
  return (
    <div className="space-y-1.5">
      {label || live ? (
        <div className="flex items-center gap-2 text-[11px] text-ink-400 font-medium">
          {label ? <span>{label}</span> : null}
          {live ? (
            <span className="inline-flex items-center gap-1 text-fluid-400">
              <span className="typing-dot" />
              <span>live</span>
            </span>
          ) : null}
        </div>
      ) : null}
      {envelopes.map((env, i) => {
        const block = unwrapBlock(env);
        const kind = (block.kind || env.kind || "").toString();
        const auto = i === pendingIdx && live;
        if (kind === "text") return <NativeTextBlock key={i} block={block} />;
        if (kind === "thinking")
          return <NativeThinkingBlock key={i} block={block} defaultOpen={auto} />;
        if (kind === "tool_use") {
          // pending = no matching tool_result later in the envelope list
          let hasResult = false;
          for (let j = i + 1; j < envelopes.length; j += 1) {
            const next = envelopes[j].block ?? (envelopes[j] as unknown as NativeBlock);
            if (
              (next.kind || envelopes[j].kind) === "tool_result" &&
              String(next.call_id || "") === String(block.call_id || "")
            ) {
              hasResult = true;
              break;
            }
          }
          // For high-traffic friendly renderers (todo_write / Skill /
          // file ops / shell / web) the result envelope already shows
          // the same enriched card — hide the tool_use once the result
          // has landed so we don't render the same checklist / playbook
          // / diff twice.
          if (
            hasResult &&
            (isTodoWrite(block) ||
              isSkillTool(block) ||
              isFileOp(block) ||
              isShellTool(block) ||
              isWebTool(block) ||
              isAgentTool(block))
          ) {
            return null;
          }
          return (
            <NativeToolUseBlock
              key={i}
              block={block}
              defaultOpen={auto}
              pending={!hasResult && live}
            />
          );
        }
        if (kind === "tool_result")
          return (
            <NativeToolResultBlock
              key={i}
              block={block}
              defaultOpen={auto || !live}
            />
          );
        if (kind === "attachment")
          return <NativeAttachmentBlock key={i} block={block} />;
        if (kind === "chart")
          return <NativeChartBlock key={i} block={block} />;
        if (kind === "team_trace")
          return (
            <NativeTeamTraceBlock
              key={i}
              block={block}
              defaultOpen={auto}
              live={live}
            />
          );
        if (kind === "subagent_trace") {
          const teamRunId = String(block.team_run_id || "");
          if (teamRunId && teamRunIds.has(teamRunId)) return null;
          return (
            <NativeSubagentTraceBlock
              key={i}
              block={block}
              defaultOpen={auto}
              live={live}
            />
          );
        }
        if (kind === "approval_request") {
          const id = approvalIdFromEvent(block as Record<string, unknown>);
          return (
            <ApprovalRequestCard
              key={i}
              event={block as Record<string, unknown>}
              card={id ? pendingApprovals?.get(id) : undefined}
              onAction={onApprovalAction}
              busy={id ? resolvingApprovalIds?.has(id) ?? false : false}
            />
          );
        }
        return (
          <Collapsible
            key={i}
            title={
              <span className="flex items-center gap-2">
                <span className="text-ink-400">block</span>
                <span className="font-mono text-ink-100">{kind || "unknown"}</span>
              </span>
            }
            tone="neutral"
            badge={null}
          >
            <JsonBlock value={env} />
          </Collapsible>
        );
      })}
    </div>
  );
}

function SubagentBlock({ name, out }: { name: string; out: unknown }) {
  const title = (
    <span className="flex items-center gap-2">
      <span className="text-ink-400">subagent</span>
      <span className="font-mono text-ink-100">{name}</span>
    </span>
  );
  return (
    <Collapsible title={title} tone="brand" badge={<Tag tone="brand">ran</Tag>}>
      <JsonBlock value={out} />
    </Collapsible>
  );
}

export function TurnBlocks({
  turn,
  pendingApprovals,
  onApprovalAction,
  approvalEvents = [],
  activityEvents = [],
  replayEvents = [],
  resolvingApprovalIds,
}: {
  turn: TurnPayload;
  pendingApprovals?: Map<string, ApprovalCard>;
  onApprovalAction?: (callbackData: string) => void;
  approvalEvents?: LiveEvent[];
  activityEvents?: LiveEvent[];
  replayEvents?: LiveEvent[];
  resolvingApprovalIds?: Set<string>;
}) {
  const t = useTranslations("chat");
  const actions = turn.actions || [];
  const tools = turn.tool_trace || [];
  const events = turn.events || [];
  const subagents = turn.subagents || {};
  const plan = turn.plan || {};
  const budget = turn.budget || {};
  const activityBlocks = liveEventsToBlocks(activityEvents).filter((env) => {
    const kind = blockKind(env);
    return kind === "team_trace" || kind === "subagent_trace";
  });
  const replayBlocks = replayEvents.length ? liveEventsToBlocks(replayEvents) : [];
  const nativeBlocks = dropDuplicateReplyTextBlocks(
    mergeLiveToolResultsIntoBlocks(
      [...activityBlocks, ...(turn.blocks || [])],
      replayBlocks,
    ),
    turn.reply_text || turn.final_text,
  );
  const blocks = mergeApprovalEventsIntoBlocks(
    nativeBlocks,
    approvalEvents,
  );
  const harness = turn.harness;
  const showLegacyTools = tools.length > 0 && !blocks.length;
  const showLegacyActions = actions.length > 0 && !blocks.length;

  const subNames = Object.keys(subagents).filter(
    (k) => (subagents as Record<string, unknown>)[k] != null
  );

  if (
    !actions.length &&
    !tools.length &&
    !events.length &&
    !subNames.length &&
    !plan.kind &&
    !blocks.length
  ) {
    return null;
  }

  return (
    <div className="mt-3 space-y-2">
      {plan.kind || harness ? (
        <div className="flex flex-wrap items-center gap-1.5 text-[10px]">
          {plan.kind ? <Tag>{t("planLabel", { value: plan.kind })}</Tag> : null}
          {plan.tier ? <Tag tone="brand">{t("tierLabel", { value: plan.tier })}</Tag> : null}
          {harness === "legacy" ? (
            <Tag>{t("legacyHarness")}</Tag>
          ) : null}
          {typeof (budget as Record<string, unknown>).calls === "number" ? (
            <Tag>
              {t("callsCount", { count: (budget as Record<string, unknown>).calls as number })}
            </Tag>
          ) : null}
          {typeof (budget as Record<string, unknown>).wall_ms === "number" ? (
            <Tag>{t("wallMs", { ms: (budget as Record<string, unknown>).wall_ms as number })}</Tag>
          ) : null}
        </div>
      ) : null}

      {/* when the workspace-native loop produced block
        * envelopes, render them up top in their original chronological
        * order. The legacy ``actions`` / ``tool_trace`` views below act
        * as a familiar summary, but the native track is the operator's
        * primary lens because it matches what the model actually saw.
        */}
      {blocks.length ? (
        <NativeBlocksTrack
          envelopes={blocks}
          pendingApprovals={pendingApprovals}
          onApprovalAction={onApprovalAction}
          resolvingApprovalIds={resolvingApprovalIds}
        />
      ) : null}

      {/* Chat transcript render order stays chronological:
        * thinking → tool_use → text. We mirror that here: high-level decision trail first
        * (events), then granular tool calls, then subagent outputs.
        * Legacy ``actions`` remain available only when native blocks
        * are absent to avoid duplicate cards in modern runs. Final
        * reply prose lands BELOW this whole block (rendered by
        * ``AssistantBubble``).
        */}
      {events.length ? (
        <div className="space-y-1.5">
          <div className="text-[11px] text-ink-400 font-medium">
            agent decision trail
          </div>
          {events.map((event, i) => (
            <EventBlock key={i} event={event} index={i} />
          ))}
        </div>
      ) : null}

      {showLegacyTools ? (
        <div className="space-y-1.5">
          <div className="text-[11px] text-ink-400 font-medium">
            tool calls
          </div>
          {tools.map((t, i) => (
            <ToolBlock key={i} t={t} />
          ))}
        </div>
      ) : null}

      {subNames.length ? (
        <div className="space-y-1.5">
          <div className="text-[11px] text-ink-400 font-medium">
            subagents
          </div>
          {subNames.map((name) => (
            <SubagentBlock
              key={name}
              name={name}
              out={(subagents as Record<string, unknown>)[name]}
            />
          ))}
        </div>
      ) : null}

      {showLegacyActions ? (
        <div className="space-y-1.5">
          <div className="text-[11px] text-ink-400 font-medium">
            actions applied
          </div>
          {actions.map((a, i) => (
            <ActionBlock key={i} action={a} />
          ))}
        </div>
      ) : null}
    </div>
  );
}
