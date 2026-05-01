"use client";

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
  CheckIcon,
  ChevronDownIcon,
  ChevronRightIcon,
  CopyIcon,
  WrenchIcon,
} from "../icons";

function CopyButton({ text }: { text: string }) {
  const [copied, setCopied] = useState(false);
  if (!text) return null;
  return (
    <button
      onClick={async () => {
        try {
          await navigator.clipboard.writeText(text);
          setCopied(true);
          window.setTimeout(() => setCopied(false), 1200);
        } catch {
          // Clipboard access can be blocked outside secure contexts.
        }
      }}
      className="inline-flex h-7 w-7 items-center justify-center rounded-md border border-white/10 text-ink-400 hover:text-white hover:border-white/20 transition-colors"
      title={copied ? "Copied" : "Copy"}
      aria-label={copied ? "Copied" : "Copy"}
    >
      {copied ? <CheckIcon size={14} /> : <CopyIcon size={14} />}
    </button>
  );
}

function JsonBlock({ value }: { value: unknown }) {
  let text = "";
  try {
    text = JSON.stringify(value, null, 2);
  } catch {
    text = String(value);
  }
  return (
    <div className="relative">
      <div className="absolute right-2 top-2 z-10">
        <CopyButton text={text} />
      </div>
      <pre className="text-[11px] font-mono text-ink-200 bg-ink-900/70 border border-ink-700/70 rounded-md p-2 pr-16 overflow-auto max-h-64">
        {text}
      </pre>
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
}: {
  title: ReactNode;
  tone?: "neutral" | "ok" | "warn" | "err" | "brand";
  badge?: ReactNode;
  defaultOpen?: boolean;
  children: ReactNode;
  right?: ReactNode;
}) {
  const [open, setOpen] = useState(defaultOpen);
  const border = {
    neutral: "border-ink-700/70",
    ok: "border-brand-500/40",
    warn: "border-[#f5a524]/40",
    err: "border-[#ef5564]/40",
    brand: "border-brand-500/40",
  }[tone];
  const bg = {
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
        className="w-full flex items-center justify-between gap-2 px-3 py-2 text-left hover:bg-ink-800/40 transition-colors"
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
        <div className="px-3 pb-3 pt-1 border-t border-ink-700/50">{children}</div>
      ) : null}
    </div>
  );
}

function Tag({
  tone = "neutral",
  children,
}: {
  tone?: "neutral" | "ok" | "warn" | "err" | "brand";
  children: ReactNode;
}) {
  const cls = {
    neutral: "bg-ink-900/60 border-ink-700 text-ink-300",
    ok: "bg-brand-500/15 border-brand-500/40 text-brand-300",
    warn: "bg-[#f5a524]/10 border-[#f5a524]/40 text-[#f5a524]",
    err: "bg-[#ef5564]/10 border-[#ef5564]/40 text-[#ef5564]",
    brand: "bg-brand-500/15 border-brand-500/40 text-brand-300",
  }[tone];
  return (
    <span
      className={`inline-flex items-center gap-1 rounded border px-1.5 py-0.5 text-[10px] font-mono ${cls}`}
    >
      {children}
    </span>
  );
}

function ActionBlock({ action }: { action: ActionRecord }) {
  const ok = !action.error;
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

function ToolBlock({ t }: { t: ToolTraceEntry }) {
  const ok = t.ok;
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
    >
      <div className="space-y-2">
        {t.error ? (
          <div className="text-xs text-[#ef5564]">{t.error}</div>
        ) : null}
        <div className="text-[10px] uppercase tracking-wider text-ink-400">
          payload
        </div>
        <JsonBlock value={t.payload} />
        <div className="text-[10px] uppercase tracking-wider text-ink-400">
          result
        </div>
        <JsonBlock value={t.result ?? t.error} />
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
            <div className="text-[10px] uppercase tracking-wider text-ink-400 mb-1">
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
// Phase 14 — provider-native block rendering
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

function NativeTextBlock({ block }: { block: NativeBlock }) {
  const text = typeof block.text === "string" ? block.text.trim() : "";
  if (!text) return null;
  return (
    <div className="rounded-md border border-ink-700/70 bg-ink-800/40 px-3 py-2">
      <div className="flex items-center justify-between gap-2 text-[10px] uppercase tracking-wider text-ink-400 mb-1">
        <span>assistant text</span>
        <CopyButton text={text} />
      </div>
      <Markdown>{text}</Markdown>
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

function recordOf(value: unknown): Record<string, unknown> {
  return value && typeof value === "object" && !Array.isArray(value)
    ? (value as Record<string, unknown>)
    : {};
}

function arrayOfRecords(value: unknown): Record<string, unknown>[] {
  return Array.isArray(value)
    ? value.filter(
        (row): row is Record<string, unknown> =>
          !!row && typeof row === "object" && !Array.isArray(row),
      )
    : [];
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

function payloadRoles(payload: unknown): string[] {
  const p = recordOf(payload);
  return stringArray(p.roles);
}

function NativeAgentToolIntro({ block }: { block: NativeBlock }) {
  const action = String(block.action || "");
  const payload = recordOf(block.payload);
  const isTeam = action === "team_run";
  const isSubagent = action === "subagent_run" || action === "subagent_run_async";
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

function NativeToolUseBlock({
  block,
  defaultOpen = false,
  pending = false,
}: {
  block: NativeBlock;
  defaultOpen?: boolean;
  pending?: boolean;
}) {
  const name =
    (block.action as string | undefined) ||
    (block.skill_id as string | undefined) ||
    "tool";
  const skill = block.skill_id as string | undefined;
  const action = String(block.action || "");
  const agentLabel =
    action === "team_run"
      ? "Agent Team"
      : action === "subagent_run" || action === "subagent_run_async"
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
        <div className="text-[10px] uppercase tracking-wider text-ink-400">
          input
        </div>
        <JsonBlock value={block.payload ?? {}} />
      </div>
    </Collapsible>
  );
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
  const steps = arrayOfRecords(block.steps);
  const memberNames = Array.from(
    new Set([...roles, ...Object.keys(members)]),
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
        {task ? <div className="text-xs text-ink-200">{task}</div> : null}
        {memberNames.length ? (
          <div className="grid grid-cols-1 sm:grid-cols-2 gap-1.5">
            {memberNames.map((name) => {
              const member = recordOf(members[name]);
              const state = String(member.status || "planned");
              return (
                <div
                  key={name}
                  className="rounded-md border border-ink-700/60 bg-ink-900/45 px-2.5 py-2"
                >
                  <div className="flex items-center justify-between gap-2">
                    <span className="font-mono text-xs text-ink-100 truncate">
                      {name}
                    </span>
                    <Tag tone={traceTone(state)}>{state}</Tag>
                  </div>
                  {member.error ? (
                    <div className="mt-1 text-[11px] text-[#ef5564] break-words">
                      {String(member.error)}
                    </div>
                  ) : null}
                </div>
              );
            })}
          </div>
        ) : null}
        {steps.length ? (
          <div className="space-y-1.5">
            <div className="text-[10px] uppercase tracking-wider text-ink-400">
              team timeline
            </div>
            {steps.map((step, i) => (
              <div
                key={i}
                className="flex items-start gap-2 rounded-md border border-ink-700/50 bg-ink-900/35 px-2.5 py-2"
              >
                <div className="mt-1 h-1.5 w-1.5 rounded-full bg-brand-300 shrink-0" />
                <div className="min-w-0 flex-1">
                  <div className="flex items-center gap-1.5 flex-wrap">
                    <span className="text-xs text-ink-100">
                      {String(step.kind || "step")}
                    </span>
                    {step.subagent ? <Tag>{String(step.subagent)}</Tag> : null}
                    {step.status ? (
                      <Tag tone={traceTone(String(step.status))}>
                        {String(step.status)}
                      </Tag>
                    ) : null}
                  </div>
                  {step.error ? (
                    <div className="mt-1 text-[11px] text-[#ef5564] break-words">
                      {String(step.error)}
                    </div>
                  ) : null}
                </div>
              </div>
            ))}
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
  const status = String(block.status || (live ? "running" : "completed"));
  const steps = arrayOfRecords(block.steps);
  const payloadKeys = stringArray(block.payload_keys);
  const title = (
    <span className="flex items-center gap-2 min-w-0">
      <span className="text-ink-400">Sub Agent</span>
      <span className="font-mono text-ink-100 truncate">{name}</span>
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
      right={
        <span className="flex items-center gap-1">
          {compactNumber(block.tokens)}
          {compactNumber(block.usd, "usd")}
          {compactNumber(block.wall_ms, "ms")}
        </span>
      }
    >
      <div className="space-y-3">
        {payloadKeys.length ? (
          <div className="flex items-center gap-1 flex-wrap">
            {payloadKeys.map((key) => (
              <Tag key={key}>{key}</Tag>
            ))}
          </div>
        ) : null}
        {steps.length ? (
          <div className="space-y-1.5">
            {steps.map((step, i) => {
              const stepKind = String(
                step.step_kind || step.lifecycle || "step",
              );
              const label =
                stepKind === "act" && step.skill
                  ? `${String(step.skill)}.${String(step.action || "")}`
                  : stepKind;
              const parsedKeys = stringArray(step.parsed_keys);
              return (
                <div
                  key={i}
                  className="rounded-md border border-ink-700/50 bg-ink-900/35 px-2.5 py-2"
                >
                  <div className="flex items-center gap-1.5 flex-wrap">
                    <span className="text-xs text-ink-100">{label}</span>
                    {typeof step.iteration === "number" ? (
                      <Tag>{`iter ${step.iteration}`}</Tag>
                    ) : null}
                    {step.status ? (
                      <Tag tone={traceTone(String(step.status))}>
                        {String(step.status)}
                      </Tag>
                    ) : null}
                    {step.provider ? <Tag>{String(step.provider)}</Tag> : null}
                    {step.model ? <Tag>{String(step.model)}</Tag> : null}
                    {compactNumber(step.wall_ms, "ms")}
                  </div>
                  {parsedKeys.length ? (
                    <div className="mt-1.5 flex items-center gap-1 flex-wrap">
                      {parsedKeys.map((key) => (
                        <Tag key={key}>{key}</Tag>
                      ))}
                    </div>
                  ) : null}
                  {step.error ? (
                    <div className="mt-1.5 text-[11px] text-[#ef5564] break-words">
                      {String(step.error)}
                    </div>
                  ) : null}
                </div>
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
  const ok = block.ok !== false && !block.error;
  const action = (block.action as string | undefined) || "tool";
  const skill = (block.skill_id as string | undefined) || "native";
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
        <div className="text-[10px] uppercase tracking-wider text-ink-400">
          output
        </div>
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
  return (
    <div className="space-y-1.5">
      {label || live ? (
        <div className="flex items-center gap-2 text-[10px] uppercase tracking-wider text-ink-400">
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
          return <NativeToolResultBlock key={i} block={block} defaultOpen={auto} />;
        if (kind === "team_trace")
          return (
            <NativeTeamTraceBlock
              key={i}
              block={block}
              defaultOpen={auto}
              live={live}
            />
          );
        if (kind === "subagent_trace")
          return (
            <NativeSubagentTraceBlock
              key={i}
              block={block}
              defaultOpen={auto}
              live={live}
            />
          );
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
  resolvingApprovalIds,
}: {
  turn: TurnPayload;
  pendingApprovals?: Map<string, ApprovalCard>;
  onApprovalAction?: (callbackData: string) => void;
  approvalEvents?: LiveEvent[];
  activityEvents?: LiveEvent[];
  resolvingApprovalIds?: Set<string>;
}) {
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
  const blocks = mergeApprovalEventsIntoBlocks(
    [...activityBlocks, ...(turn.blocks || [])],
    approvalEvents,
  );
  const harness = turn.harness;

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
          {plan.kind ? <Tag>plan: {plan.kind}</Tag> : null}
          {plan.tier ? <Tag tone="brand">tier: {plan.tier}</Tag> : null}
          {harness === "native" ? (
            <Tag tone="brand">native loop</Tag>
          ) : harness === "legacy" ? (
            <Tag>legacy harness</Tag>
          ) : null}
          {typeof (budget as Record<string, unknown>).calls === "number" ? (
            <Tag>
              {`${(budget as Record<string, unknown>).calls} call${
                (budget as Record<string, unknown>).calls === 1 ? "" : "s"
              }`}
            </Tag>
          ) : null}
          {typeof (budget as Record<string, unknown>).wall_ms === "number" ? (
            <Tag>{`${(budget as Record<string, unknown>).wall_ms}ms`}</Tag>
          ) : null}
        </div>
      ) : null}

      {/* Phase 14 — when the workspace-native loop produced block
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

      {/* Apr-27 2026 — Claude Code TUI parity render order. Upstream
        * streams content blocks chronologically: thinking → tool_use →
        * text. We mirror that here: high-level decision trail first
        * (events), then the granular tool calls in the order they
        * fired, then any subagent outputs, then a compact summary of
        * the top-level actions the kernel ultimately committed. The
        * final reply prose lands BELOW this whole block (rendered by
        * ``AssistantBubble``) so the visible flow is exactly:
        *
        *     decisions → tool calls → subagents → actions → reply.
        */}
      {events.length ? (
        <div className="space-y-1.5">
          <div className="text-[10px] uppercase tracking-wider text-ink-400">
            agent decision trail
          </div>
          {events.map((event, i) => (
            <EventBlock key={i} event={event} index={i} />
          ))}
        </div>
      ) : null}

      {tools.length ? (
        <div className="space-y-1.5">
          <div className="text-[10px] uppercase tracking-wider text-ink-400">
            tool calls
          </div>
          {tools.map((t, i) => (
            <ToolBlock key={i} t={t} />
          ))}
        </div>
      ) : null}

      {subNames.length ? (
        <div className="space-y-1.5">
          <div className="text-[10px] uppercase tracking-wider text-ink-400">
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

      {actions.length ? (
        <div className="space-y-1.5">
          <div className="text-[10px] uppercase tracking-wider text-ink-400">
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
