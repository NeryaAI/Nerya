"use client";

import { useTranslations } from "next-intl";

import { ReactNode, useEffect, useMemo, useRef, useState } from "react";
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
  SubagentsIcon,
  WrenchIcon,
} from "../icons";
import { JsonView } from "../JsonView";
import {
  StrategyProposalApprovalCard,
  type StrategyProposalView,
  isHoistableStrategyProposal,
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
  isFriendlyToolCard,
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
  autoOpen,
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
  autoOpen?: boolean;
  children: ReactNode;
  right?: ReactNode;
  chrome?: { border?: string; bg?: string };
  headerClassName?: string;
  bodyClassName?: string;
}) {
  const [open, setOpen] = useState(autoOpen ?? defaultOpen);
  useEffect(() => {
    if (typeof autoOpen === "boolean") {
      setOpen(autoOpen);
    }
  }, [autoOpen]);
  const border = chrome?.border ?? {
    neutral: "border-ink-700/70",
    ok: "border-brand-500/40",
    warn: "border-warn/40",
    err: "border-danger/40",
    brand: "border-brand-500/40",
  }[tone];
  const bg = chrome?.bg ?? {
    neutral: "bg-ink-800/50",
    ok: "bg-brand-500/5",
    warn: "bg-warn/5",
    err: "bg-danger/5",
    brand: "bg-brand-500/5",
  }[tone];
  return (
    <div className={`rounded-lg border ${border} ${bg} overflow-hidden`}>
      <button
        onClick={() => setOpen((v) => !v)}
        aria-expanded={open}
        className={`w-full min-h-9 flex items-center justify-between gap-2 px-2.5 py-1.5 text-left hover:bg-ink-800/35 transition-colors ${headerClassName}`}
      >
        <div className="flex min-w-0 flex-1 items-center gap-2">
          <div className="min-w-0 flex-1 text-[12.5px] font-semibold leading-tight text-ink-100">
            {title}
          </div>
          {badge}
        </div>
        <div className="flex shrink-0 items-center gap-1.5">
          {right}
          <span className="text-ink-500">
            {open ? <ChevronDownIcon size={13} /> : <ChevronRightIcon size={13} />}
          </span>
        </div>
      </button>
      {open ? (
        <div className={`px-2.5 pb-2.5 pt-2 border-t border-ink-700/50 ${bodyClassName}`}>{children}</div>
      ) : null}
    </div>
  );
}

// ---------------------------------------------------------------------
// Strategy proposal "popup" hoist
// ---------------------------------------------------------------------
//
// A freshly generated strategy package proposal is the one thing in a
// turn the operator usually wants to act on right away (approve / add,
// or delete). It would otherwise be buried inside a collapsed tool
// result. We pull every *active* proposal out of the turn and render it
// as a prominent card at the very top of the trace, then suppress the
// buried duplicates by id so the same proposal never shows twice.

function pushActiveProposal(
  out: StrategyProposalView[],
  seen: Set<string>,
  candidate: StrategyProposalView | null,
): void {
  if (!candidate || !isHoistableStrategyProposal(candidate)) return;
  const id = String(candidate.id || "");
  if (!id || seen.has(id)) return;
  seen.add(id);
  out.push(candidate);
}

function activeProposalsFromEnvelopes(
  envelopes: NativeBlockEnvelope[],
): StrategyProposalView[] {
  const out: StrategyProposalView[] = [];
  const seen = new Set<string>();
  for (const env of envelopes) {
    if (blockKind(env) !== "tool_result") continue;
    const block = unwrapBlock(env);
    pushActiveProposal(
      out,
      seen,
      strategyProposalFromToolResult(block.result ?? block, String(block.action || "")),
    );
  }
  return out;
}

function activeProposalsFromLegacy(
  tools: ToolTraceEntry[],
  actions: ActionRecord[],
): StrategyProposalView[] {
  const out: StrategyProposalView[] = [];
  const seen = new Set<string>();
  for (const t of tools) {
    pushActiveProposal(
      out,
      seen,
      strategyProposalFromToolResult(t.result, String(t.action || "")),
    );
  }
  for (const a of actions) {
    if (a.error) continue;
    pushActiveProposal(
      out,
      seen,
      strategyProposalFromToolResult(a.result, a.action),
    );
  }
  return out;
}

// The ``strategy_backtest`` tool result is a string that prefixes a compact
// metrics summary before the real JSON payload, e.g.
// ``backtest: metrics=[...]\n[compacted_kept]\n{ ...json... }``. Parse the
// trailing object so we can read ``metrics.verdict`` for the proposal card.
function parseTrailingJsonObject(value: unknown): Record<string, unknown> | null {
  if (value && typeof value === "object" && !Array.isArray(value)) {
    return value as Record<string, unknown>;
  }
  if (typeof value !== "string") return null;
  const start = value.indexOf("{");
  if (start < 0) return null;
  try {
    const parsed = JSON.parse(value.slice(start)) as unknown;
    return parsed && typeof parsed === "object" && !Array.isArray(parsed)
      ? (parsed as Record<string, unknown>)
      : null;
  } catch {
    return null;
  }
}

// Map proposal_id (and ``sid:<strategy_id>``) -> latest backtest verdict
// (PASS / WARN / FAIL) seen in the turn, so the approve/add card can surface
// the verdict and warn before approving a strategy that failed its backtest.
function backtestVerdictsFromTurn(turn: TurnPayload): Map<string, string> {
  const out = new Map<string, string>();
  const consider = (action: unknown, result: unknown): void => {
    if (String(action || "") !== "strategy_backtest") return;
    const rec = parseTrailingJsonObject(result);
    if (!rec) return;
    const metrics = recordOf(rec.metrics);
    const verdict = String(metrics.verdict || rec.verdict || "")
      .toUpperCase()
      .trim();
    if (!verdict) return;
    const proposalId = String(rec.proposal_id || "");
    const strategyId = String(rec.strategy_id || "");
    if (proposalId) out.set(proposalId, verdict);
    if (strategyId) out.set(`sid:${strategyId}`, verdict);
  };
  for (const env of turn.blocks ?? []) {
    if (blockKind(env) !== "tool_result") continue;
    const block = unwrapBlock(env);
    consider(block.action, block.result);
  }
  for (const entry of turn.tool_trace ?? []) consider(entry.action, entry.result);
  for (const action of turn.actions ?? []) consider(action.action, action.result);
  return out;
}

// Collect the active (submitted, non-draft) strategy proposals for a whole
// committed turn — native blocks first, then the legacy tool_trace / actions
// fallback — and attach the latest backtest verdict to each. ``AssistantBubble``
// uses this to render the approve/add card at the *bottom* of the bubble (right
// under the plain-language verdict) instead of hoisting it above the trace, so
// the operator reads what happened and the recommendation before acting on it.
export function activeProposalsFromTurn(
  turn: TurnPayload | null | undefined,
): StrategyProposalView[] {
  if (!turn) return [];
  const nativeEnvelopes = turn.blocks ?? [];
  const fromNative = nativeEnvelopes.length
    ? activeProposalsFromEnvelopes(nativeEnvelopes)
    : [];
  const proposals = fromNative.length
    ? fromNative
    : activeProposalsFromLegacy(turn.tool_trace ?? [], turn.actions ?? []);
  if (!proposals.length) return proposals;
  const verdicts = backtestVerdictsFromTurn(turn);
  if (!verdicts.size) return proposals;
  return proposals.map((proposal) => {
    const verdict =
      verdicts.get(String(proposal.id || "")) ??
      (proposal.strategy_id
        ? verdicts.get(`sid:${String(proposal.strategy_id)}`)
        : undefined);
    return verdict ? { ...proposal, backtest_verdict: verdict } : proposal;
  });
}

export function StrategyProposalsHoist({
  proposals,
}: {
  proposals: StrategyProposalView[];
}) {
  const t = useTranslations("strategyProposal");
  if (!proposals.length) return null;
  return (
    <div className="space-y-2" data-strategy-proposal-hoist="true">
      <div className="flex items-center gap-2 text-[11px] text-warn font-medium">
        <span>{t("pendingHeading", { count: proposals.length })}</span>
      </div>
      {proposals.map((proposal) => (
        <StrategyProposalApprovalCard
          key={String(proposal.id)}
          proposal={proposal}
          approveNote="approved from chat"
        />
      ))}
    </div>
  );
}

function ActionBlock({
  action,
  suppressProposalIds,
}: {
  action: ActionRecord;
  suppressProposalIds?: Set<string>;
}) {
  const ok = !action.error;
  const rawProposal = strategyProposalFromToolResult(
    action.result,
    action.action,
  );
  const strategyProposal =
    rawProposal &&
    isHoistableStrategyProposal(rawProposal) &&
    !suppressProposalIds?.has(String(rawProposal.id))
      ? rawProposal
      : null;
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
          <div className="text-xs text-danger">{action.error}</div>
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

function ToolBlock({
  t,
  suppressProposalIds,
}: {
  t: ToolTraceEntry;
  suppressProposalIds?: Set<string>;
}) {
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
  const rawProposal = strategyProposalFromToolResult(
    t.result,
    String(t.action || ""),
  );
  const strategyProposal =
    rawProposal &&
    isHoistableStrategyProposal(rawProposal) &&
    !suppressProposalIds?.has(String(rawProposal.id))
      ? rawProposal
      : null;
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
          <div className="text-xs text-danger">{t.error}</div>
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

function mergeToolUsePayloadsIntoResults(
  envelopes: NativeBlockEnvelope[],
): NativeBlockEnvelope[] {
  const payloadByCallId = new Map<string, Record<string, unknown>>();
  for (const env of envelopes) {
    const block = unwrapBlock(env);
    if (blockKind(env) !== "tool_use") continue;
    const callId = toolCallId(block);
    if (!callId) continue;
    const payload = block.payload;
    if (payload && typeof payload === "object") {
      payloadByCallId.set(callId, recordOf(payload));
    }
  }
  if (!payloadByCallId.size) return envelopes;

  let changed = false;
  const merged = envelopes.map((env) => {
    const block = unwrapBlock(env);
    if (blockKind(env) !== "tool_result") return env;
    const callId = toolCallId(block);
    if (!callId || !payloadByCallId.has(callId)) return env;
    if (Object.keys(recordOf(block.payload)).length > 0) return env;
    changed = true;
    return {
      ...env,
      kind: "tool_result",
      block: {
        ...block,
        kind: "tool_result",
        payload: payloadByCallId.get(callId),
      },
    };
  });
  return changed ? merged : envelopes;
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
    const normalizedText = normalizedBlockText(block.text);
    if (!normalizedText) return false;
    if (normalizedText === normalizedReply) return false;
    return !(
      normalizedText.length >= 48 &&
      normalizedReply.includes(normalizedText)
    );
  });
}

const STREAM_BASE_DELAY_MS = 22;

function commonPrefixLength(a: string, b: string): number {
  const limit = Math.min(a.length, b.length);
  let i = 0;
  while (i < limit && a[i] === b[i]) i += 1;
  return i;
}

function streamChunkSize(remaining: number): number {
  if (remaining > 1200) return 36;
  if (remaining > 600) return 24;
  if (remaining > 240) return 14;
  if (remaining > 96) return 8;
  if (remaining > 32) return 5;
  return 2;
}

function streamDelayMs(remaining: number, baseDelayMs: number): number {
  if (remaining > 600) return Math.max(10, baseDelayMs - 10);
  if (remaining > 240) return Math.max(14, baseDelayMs - 6);
  return baseDelayMs;
}

export function useTypewriterText(
  text: string,
  active: boolean,
  delayMs = STREAM_BASE_DELAY_MS,
): { text: string; streaming: boolean } {
  const chars = useMemo(() => Array.from(text), [text]);
  const [count, setCount] = useState(active ? 0 : chars.length);
  const prevTextRef = useRef(text);

  useEffect(() => {
    if (!active) {
      prevTextRef.current = text;
      setCount(chars.length);
      return;
    }
    const previous = prevTextRef.current;
    prevTextRef.current = text;
    setCount((current) => {
      if (text.startsWith(previous)) return Math.min(current, chars.length);
      if (previous.startsWith(text)) return Math.min(current, chars.length);
      return Math.min(current, commonPrefixLength(previous, text));
    });
  }, [active, chars.length, text]);

  useEffect(() => {
    if (!active || count >= chars.length) return;
    const remaining = chars.length - count;
    const timer = window.setTimeout(() => {
      setCount((current) =>
        Math.min(current + streamChunkSize(chars.length - current), chars.length),
      );
    }, streamDelayMs(remaining, delayMs));
    return () => window.clearTimeout(timer);
  }, [active, chars.length, count, delayMs]);

  if (!active) return { text, streaming: false };
  return {
    text: chars.slice(0, count).join(""),
    streaming: count < chars.length,
  };
}

export function TypewriterCursor({ show }: { show: boolean }) {
  if (!show) return null;
  return (
    <span
      className="ml-0.5 inline-block h-3 w-[1px] translate-y-[2px] bg-fluid-300 animate-pulse"
      aria-hidden
    />
  );
}

export function StreamedPlainText({
  text,
  active = false,
  className = "",
}: {
  text: string;
  active?: boolean;
  className?: string;
}) {
  const streamed = useTypewriterText(text, active);
  return (
    <span className={className}>
      {streamed.text}
      <TypewriterCursor show={streamed.streaming} />
    </span>
  );
}

export function StreamedMarkdown({
  text,
  active = false,
}: {
  text: string;
  active?: boolean;
}) {
  const streamed = useTypewriterText(text, active);
  return (
    <>
      <Markdown>{streamed.text || " "}</Markdown>
      <TypewriterCursor show={streamed.streaming} />
    </>
  );
}

export function StreamedPreText({
  text,
  active = false,
}: {
  text: string;
  active?: boolean;
}) {
  const streamed = useTypewriterText(text, active, 16);
  return (
    <>
      {streamed.text}
      <TypewriterCursor show={streamed.streaming} />
    </>
  );
}

function NativeTextBlock({
  block,
  stream = false,
}: {
  block: NativeBlock;
  stream?: boolean;
}) {
  const text = typeof block.text === "string" ? block.text.trim() : "";
  if (!text) return null;
  return (
    <div className="rounded-md border border-ink-700/70 bg-ink-800/40 px-3 py-2">
      <div className="flex items-center justify-between gap-2 text-[11px] text-ink-400 font-medium mb-1">
        <span>assistant text</span>
        <CopyButton text={text} />
      </div>
      <StreamedMarkdown text={text} active={stream} />
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
  autoOpen,
  stream = false,
}: {
  block: NativeBlock;
  defaultOpen?: boolean;
  autoOpen?: boolean;
  stream?: boolean;
}) {
  const text = typeof block.text === "string" ? block.text : "";
  if (!text.trim()) return null;
  const running = autoOpen === true || defaultOpen;
  return (
    <Collapsible
      title={
        <span className="flex min-w-0 items-center gap-2">
          <span className="inline-flex h-5 w-5 shrink-0 items-center justify-center rounded-md border border-brand-500/25 bg-brand-500/10 text-brand-200">
            <span className="h-1.5 w-1.5 rounded-full bg-brand-300" />
          </span>
          <span className="shrink-0 text-ink-100">{running ? "Thinking" : "Thought"}</span>
          <span className="min-w-0 truncate font-mono text-[11px] font-normal text-ink-400">
            {text.slice(0, 56)}
            {text.length > 56 ? "…" : ""}
          </span>
        </span>
      }
      tone="brand"
      badge={<Tag tone="brand">think</Tag>}
      defaultOpen={defaultOpen}
      autoOpen={autoOpen}
    >
      <pre className="whitespace-pre-wrap text-[11px] font-mono text-ink-200 bg-ink-900/70 border border-ink-700/70 rounded-md p-2 overflow-auto max-h-60">
        <StreamedPreText text={text} active={stream} />
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
  borderColor: string;
  ring: string;
  dot: string;
  chip: string;
  text: string;
};

const AGENT_ACCENTS: AgentAccent[] = [
  {
    border: "border-cyan-400/35",
    bg: "bg-cyan-400/[0.055]",
    softBg: "bg-cyan-400/[0.08]",
    borderColor: "rgba(34, 211, 238, 0.48)",
    ring: "ring-cyan-400/25",
    dot: "bg-cyan-300",
    chip: "border-cyan-400/40 bg-cyan-400/10 text-cyan-200",
    text: "text-cyan-200",
  },
  {
    border: "border-emerald-400/35",
    bg: "bg-emerald-400/[0.055]",
    softBg: "bg-emerald-400/[0.08]",
    borderColor: "rgba(52, 211, 153, 0.48)",
    ring: "ring-emerald-400/25",
    dot: "bg-emerald-300",
    chip: "border-emerald-400/40 bg-emerald-400/10 text-emerald-200",
    text: "text-emerald-200",
  },
  {
    border: "border-amber-400/35",
    bg: "bg-amber-400/[0.055]",
    softBg: "bg-amber-400/[0.08]",
    borderColor: "rgba(251, 191, 36, 0.48)",
    ring: "ring-amber-400/25",
    dot: "bg-amber-300",
    chip: "border-amber-400/40 bg-amber-400/10 text-amber-200",
    text: "text-amber-200",
  },
  {
    border: "border-fuchsia-400/35",
    bg: "bg-fuchsia-400/[0.055]",
    softBg: "bg-fuchsia-400/[0.08]",
    borderColor: "rgba(232, 121, 249, 0.48)",
    ring: "ring-fuchsia-400/25",
    dot: "bg-fuchsia-300",
    chip: "border-fuchsia-400/40 bg-fuchsia-400/10 text-fuchsia-200",
    text: "text-fuchsia-200",
  },
  {
    border: "border-sky-400/35",
    bg: "bg-sky-400/[0.055]",
    softBg: "bg-sky-400/[0.08]",
    borderColor: "rgba(56, 189, 248, 0.48)",
    ring: "ring-sky-400/25",
    dot: "bg-sky-300",
    chip: "border-sky-400/40 bg-sky-400/10 text-sky-200",
    text: "text-sky-200",
  },
  {
    border: "border-rose-400/35",
    bg: "bg-rose-400/[0.055]",
    softBg: "bg-rose-400/[0.08]",
    borderColor: "rgba(251, 113, 133, 0.48)",
    ring: "ring-rose-400/25",
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
  stream = false,
}: {
  label: string;
  value: unknown;
  accent?: AgentAccent;
  stream?: boolean;
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
          <StreamedMarkdown text={text} active={stream} />
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
  stream = false,
}: {
  label: string;
  text: unknown;
  maxH?: string;
  stream?: boolean;
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
        <StreamedPreText text={body} active={stream} />
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
  const t = useTranslations("turnBlocks");
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
                <span>{t("current")}</span>
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
              <StreamedPlainText
                text={String(step.summary)}
                active={live && current}
              />
            </div>
          ) : null}
          {step.content ? (
            <div className="text-[11px] text-ink-200 break-words">
              <span className="text-ink-500">
                {String(step.from_agent || agent || "agent")}
                {step.to ? ` -> ${String(step.to)}` : ""}:{" "}
              </span>
              <StreamedPlainText
                text={String(step.content)}
                active={live && current}
              />
            </div>
          ) : null}
          {step.error ? (
            <div className="text-[11px] text-danger break-words">
              <StreamedPlainText
                text={String(step.error)}
                active={live && current}
              />
            </div>
          ) : null}
          {hasPromptOrReasoning ? (
            <div className="grid grid-cols-1 gap-2">
              <TextPanel
                label={t("prompt")}
                text={promptText}
                maxH="max-h-72"
                stream={live && current}
              />
              <TextPanel
                label={t("reasoningSummary")}
                text={reasoningText}
                maxH="max-h-56"
                stream={live && current}
              />
            </div>
          ) : null}
          {hasStructured ? (
            <div className="grid grid-cols-1 gap-2">
              <OutputPanel
                label={t("inputPayload")}
                value={payload}
                accent={accent}
                stream={live && current}
              />
              <OutputPanel
                label={t("output")}
                value={output}
                accent={accent}
                stream={live && current}
              />
              <OutputPanel
                label={t("metrics")}
                value={step.metrics}
                accent={accent}
                stream={live && current}
              />
            </div>
          ) : null}
          {hasRaw ? (
            <RawDetails title={t("rawEventData")}>
              <JsonPanel label={t("payload")} value={payload} />
              <JsonPanel label={t("rawOutput")} value={output} />
              <JsonPanel label={t("metrics")} value={step.metrics} />
              <JsonPanel label={t("results")} value={step.results} />
              <JsonPanel label={t("failures")} value={step.failures} />
              <JsonPanel label={t("rawEvent")} value={step.raw} />
            </RawDetails>
          ) : null}
        </div>
      </div>
    </div>
  );
}

function NativeAgentToolIntro({ block }: { block: NativeBlock }) {
  const t = useTranslations("turnBlocks");
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
        <Tag tone="brand">{isTeam ? t("agentTeam") : t("subAgent")}</Tag>
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

function plural(count: number, singular: string, pluralForm = `${singular}s`): string {
  return `${count} ${count === 1 ? singular : pluralForm}`;
}

function traceSummaryFor(envelopes: NativeBlockEnvelope[], live: boolean): string[] {
  let thinking = 0;
  let files = 0;
  let searches = 0;
  let commands = 0;
  let genericTools = 0;
  const seenCalls = new Set<string>();

  envelopes.forEach((env, index) => {
    const block = unwrapBlock(env);
    const kind = (block.kind || env.kind || "").toString();
    if (kind === "thinking") {
      thinking += 1;
      return;
    }
    if (kind !== "tool_use" && kind !== "tool_result") return;

    const action = String(block.action || "").toLowerCase();
    const key = toolCallId(block) || `${kind}:${action}:${index}`;
    if (seenCalls.has(key)) return;
    seenCalls.add(key);

    genericTools += 1;
    if (isShellTool(block) || action.includes("shell") || action.includes("command")) {
      commands += 1;
      return;
    }
    if (
      action === "grep" ||
      action === "glob" ||
      action.includes("search")
    ) {
      searches += 1;
      return;
    }
    if (isWebTool(block)) {
      searches += 1;
      return;
    }
    if (isFileOp(block)) {
      files += 1;
      return;
    }
  });

  const lines: string[] = [];
  if (thinking > 0 || live) {
    lines.push(live ? "Thinking..." : thinking > 1 ? `Thought through ${plural(thinking, "step")}` : "Thought briefly");
  }

  const explored: string[] = [];
  if (files > 0) explored.push(plural(files, "file"));
  if (searches > 0) explored.push(plural(searches, "search", "searches"));
  const actions: string[] = [];
  if (explored.length) actions.push(`Explored ${explored.join(", ")}`);
  if (commands > 0) actions.push(`ran ${plural(commands, "command")}`);
  const accounted = files + searches + commands;
  const otherTools = Math.max(0, genericTools - accounted);
  if (otherTools > 0) actions.push(`ran ${plural(otherTools, "tool")}`);
  if (actions.length) {
    lines.push(actions.join(", "));
  }
  return lines;
}

function TraceSummary({
  envelopes,
  live,
}: {
  envelopes: NativeBlockEnvelope[];
  live: boolean;
}) {
  const lines = traceSummaryFor(envelopes, live);
  if (!lines.length) return null;
  return (
    <div className="space-y-0.5 text-[12px] font-semibold leading-relaxed text-ink-400">
      {lines.map((line) => (
        <div key={line}>{line}</div>
      ))}
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
  const t = useTranslations("turnBlocks");
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
      ? t("agentTeam")
      : isSubagentToolAction(action)
      ? t("subAgent")
      : t("tool");
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
          <span>{t("running")}</span>
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
          {(block.call_id as string | undefined)?.slice(0, 8) || t("call")}
        </Tag>
      }
    >
      <div className="space-y-2">
        <NativeAgentToolIntro block={block} />
        <div className="text-[11px] text-ink-400 font-medium">
          {t("input")}
        </div>
        <JsonBlock value={block.payload ?? {}} />
      </div>
    </Collapsible>
  );
}

function AgentToolResultCard({ block }: { block: NativeBlock }) {
  const t = useTranslations("turnBlocks");
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
        t("agentTeam"),
    );
    return (
      <Collapsible
        title={
          <span className="flex items-center gap-2 min-w-0">
            <span className="text-ink-400">{t("agentTeam")}</span>
            <span className="font-mono text-ink-100 truncate">{title}</span>
          </span>
        }
        tone={ok ? "ok" : "err"}
        badge={<Tag tone={ok ? "ok" : "err"}>{ok ? t("completed") : t("error")}</Tag>}
        defaultOpen={false}
      >
        <div className="space-y-3">
          {block.error ? (
            <div className="text-xs text-danger">{String(block.error)}</div>
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
            label={t("teamAggregate")}
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
                      <div className="text-[11px] text-danger break-words">
                        {String(row.error)}
                      </div>
                    ) : null}
                    <OutputPanel
                      label={t("output")}
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
                    className="rounded-md border border-danger/35 bg-danger/[0.06] px-3 py-2"
                  >
                    <div className="flex items-center justify-between gap-2">
                      <Tag tone="err">{name}</Tag>
                      <Tag tone="err">{t("failed")}</Tag>
                    </div>
                    <div className="mt-1.5 text-[11px] text-danger break-words">
                      {String(row.error || row.message || t("unknownError"))}
                    </div>
                  </div>
                );
              })}
            </div>
          ) : null}
          <RawDetails>
            <JsonPanel label={t("payload")} value={payload} />
            <JsonPanel label={t("rawTeamResult")} value={block.result} />
          </RawDetails>
        </div>
      </Collapsible>
    );
  }

  if (isSubagentToolAction(action)) {
    const name = String(
      result.subagent || result.name || payload.name || payload.subagent || t("subagentLower"),
    );
    const accent = agentAccent(name);
    return (
      <Collapsible
        title={
          <span className="flex items-center gap-2 min-w-0">
            <span className="text-ink-400">{t("subAgent")}</span>
            <span className="font-mono text-ink-100 truncate">{name}</span>
          </span>
        }
        tone={ok ? "ok" : "err"}
        badge={<Tag tone={ok ? "ok" : "err"}>{ok ? t("completed") : t("error")}</Tag>}
        defaultOpen={false}
        chrome={{ border: accent.border, bg: accent.bg }}
        bodyClassName={accent.border}
      >
        <div className="space-y-3">
          {block.error ? (
            <div className="text-xs text-danger">{String(block.error)}</div>
          ) : null}
          <div className="flex items-center gap-1 flex-wrap">
            <AgentChip accent={accent}>{name}</AgentChip>
            {compactNumber(result.tokens)}
            {compactNumber(result.usd, "usd")}
            {compactNumber(result.wall_ms, "ms")}
          </div>
          <OutputPanel
            label={t("output")}
            value={result.output || result.summary || result.result || block.result}
            accent={accent}
          />
          <RawDetails>
            <JsonPanel label={t("payload")} value={payload} />
            <JsonPanel label={t("rawSubagentResult")} value={block.result} />
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
  const t = useTranslations("turnBlocks");
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
                  className="rounded-md border border-danger/35 bg-danger/[0.06] px-3 py-2"
                >
                  <div className="flex items-center justify-between gap-2">
                    <Tag tone="err">{name}</Tag>
                    <Tag tone="err">failed</Tag>
                  </div>
                  <div className="mt-1.5 text-[11px] text-danger break-words">
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
                      <div className="text-[11px] text-danger break-words">
                        {String(member.error)}
                      </div>
                    ) : null}
                    <OutputPanel
                      label={t("output")}
                      value={member.output || member.summary || member.result}
                      accent={accent}
                    />
                    {hasMemberRaw ? (
                      <RawDetails title={t("promptsAndRawIo")}>
                        <TextPanel
                          label={t("assignmentPrompt")}
                          text={member.assignment_prompt}
                        />
                        <TextPanel
                          label={t("rolePrompt")}
                          text={member.role_prompt}
                          maxH="max-h-56"
                        />
                        <TextPanel
                          label={t("runtimePromptToSubagent")}
                          text={member.last_prompt || promptStep?.prompt}
                          maxH="max-h-96"
                        />
                        <JsonPanel
                          label={t("inputPayload")}
                          value={member.payload || member.input_payload}
                        />
                        <JsonPanel label={t("rawOutput")} value={member.output} />
                        <JsonPanel label={t("metrics")} value={member.metrics} />
                      </RawDetails>
                    ) : null}
                    {memberSteps.length ? (
                      <Collapsible
                        title={t("memberStepStream")}
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
              {t("teamTimeline")}
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
  const t = useTranslations("turnBlocks");
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
          <span>{t("working")}</span>
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
        <OutputPanel label={t("output")} value={block.output} accent={accent} />
        {hasSubagentRaw ? (
          <RawDetails title={t("promptsAndRawIo")}>
            <TextPanel label={t("rolePrompt")} text={block.role_prompt} maxH="max-h-56" />
            <TextPanel
              label={t("runtimePromptToSubagent")}
              text={block.last_prompt}
              maxH="max-h-96"
            />
            <JsonPanel label={t("inputPayload")} value={block.payload} />
            <JsonPanel label={t("rawOutput")} value={block.output} />
            <JsonPanel label={t("metrics")} value={block.metrics} />
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
  suppressProposalIds,
  stream = false,
}: {
  block: NativeBlock;
  defaultOpen?: boolean;
  suppressProposalIds?: Set<string>;
  stream?: boolean;
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
  const rawProposal = strategyProposalFromToolResult(block.result ?? block, action);
  const strategyProposal =
    rawProposal &&
    isHoistableStrategyProposal(rawProposal) &&
    !suppressProposalIds?.has(String(rawProposal.id))
      ? rawProposal
      : null;
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
          <div className="text-xs text-danger">{String(block.error)}</div>
        ) : null}
        {strategyProposal ? (
          <StrategyProposalApprovalCard
            proposal={strategyProposal}
            compact
            approveNote="approved from chat tool result"
          />
        ) : null}
        <OutputPanel
          label="output"
          value={block.error ? String(block.error) : (block.result ?? block)}
          stream={stream}
        />
        <RawDetails>
          <JsonPanel label="payload" value={block.payload} />
          <JsonPanel label="raw result" value={block.result ?? block} />
        </RawDetails>
      </div>
    </Collapsible>
  );
}

// ---------------------------------------------------------------------
// Multi-agent (team / subagent) presentation
// ---------------------------------------------------------------------
//
// A ``team_run`` / ``subagent_run`` tool call collapses a whole crew of
// sub-agents into ONE tool_result. The backend then *compacts* that
// result before persisting it, so on reload the block carries a string
// like ``"team_run summary: …\n[compacted_kept]\n{<json>}"`` rather than a
// structured object. The helpers below recover the embedded JSON and
// normalise it into per-member records, so each sub-agent can be
// rendered as its own avatar + bubble (a distinct "speaker") instead of
// being buried inside a single collapsed card.

/** Extract a JSON object embedded anywhere inside ``value``.
 *
 * Accepts either an already-structured object or a compacted string
 * (``team_run summary: …\n[compacted_kept]\n{…}``). Brace-matching is
 * string-literal aware so Chinese prose / nested braces don't trip it. */
function parseAgentResult(value: unknown): Record<string, unknown> | null {
  if (value && typeof value === "object" && !Array.isArray(value)) {
    return value as Record<string, unknown>;
  }
  if (typeof value !== "string") return null;
  const raw = value.trim();
  const direct = parseJsonLike(raw);
  if (direct && typeof direct === "object" && !Array.isArray(direct)) {
    return direct as Record<string, unknown>;
  }
  const start = raw.indexOf("{");
  if (start < 0) return null;
  let depth = 0;
  let inStr = false;
  let escaped = false;
  for (let i = start; i < raw.length; i += 1) {
    const ch = raw[i];
    if (inStr) {
      if (escaped) escaped = false;
      else if (ch === "\\") escaped = true;
      else if (ch === '"') inStr = false;
      continue;
    }
    if (ch === '"') inStr = true;
    else if (ch === "{") depth += 1;
    else if (ch === "}") {
      depth -= 1;
      if (depth === 0) {
        try {
          const parsed = JSON.parse(raw.slice(start, i + 1)) as unknown;
          return parsed && typeof parsed === "object" && !Array.isArray(parsed)
            ? (parsed as Record<string, unknown>)
            : null;
        } catch {
          return null;
        }
      }
    }
  }
  return null;
}

/** One normalised step in a sub-agent's think → act → observe loop, used to
 * render its progress live (mirrors how the main agent streams tool calls). */
export type MemberStep = {
  key: string;
  kind: string; // act | think | think_retry | observe | close | …
  label: string; // "web_search", "market_data.get_candles", "thinking", …
  status: "ok" | "error" | "retry" | "running" | "info";
  iteration?: number;
  detail?: string; // reasoning snippet / error message
  wallMs?: number;
  ts?: number | string;
};

export type TeamMemberInfo = {
  name: string;
  role?: string;
  status: "completed" | "timeout" | "failed" | "running" | "planned" | "skipped";
  ok: boolean;
  output?: unknown;
  error?: string;
  tokens?: number;
  usd?: number;
  wallMs?: number;
  raw?: unknown;
  // ``caveat`` marks a member that succeeded but with a flagged evidence gap
  // (e.g. a private company whose hard figures cannot be fully sourced).
  caveat?: string;
  // Step-by-step trace of the member's run, rendered as a live timeline.
  steps?: MemberStep[];
  // Live-only extras (populated while a turn is still streaming):
  //   currentActivity — short label of what the agent is doing right now
  //   stepCount       — how many steps it has emitted so far
  currentActivity?: string;
  stepCount?: number;
};

/** Normalise the raw streamed ``steps`` of a member record into compact,
 * render-ready bubbles. Prompt/start/end frames stay visible, but long
 * prompts are summarised so the main chat does not dump raw instructions. */
function normalizeMemberSteps(raw: unknown): MemberStep[] {
  const rows = arrayOfRecords(raw);
  if (!rows.length) return [];
  const out: MemberStep[] = [];
  const clipDetail = (value: string): string | undefined => {
    const trimmed = value.trim();
    if (!trimmed) return undefined;
    return trimmed.length > 520 ? `${trimmed.slice(0, 520)}...` : trimmed;
  };
  rows.forEach((r, i) => {
    const kind = String(r.step_kind || r.lifecycle || r.kind || "")
      .replace(/^subagent\./, "")
      .trim();
    const statusRaw = String(r.status || "").toLowerCase();
    const hasError = statusRaw === "error" || Boolean(r.error);
    const reasoning = r.reasoning ? String(r.reasoning).trim() : "";
    const prompt = typeof r.prompt === "string" ? r.prompt.trim() : "";
    const promptChars =
      typeof r.prompt_chars === "number"
        ? r.prompt_chars
        : prompt
        ? Array.from(prompt).length
        : undefined;
    const skill = r.skill ? String(r.skill) : "";
    const actionRaw = r.action ? String(r.action) : "";
    const action = actionRaw && actionRaw !== "(native)" ? actionRaw : "";
    const isAct = Boolean(skill) || kind === "act";
    const isThink = kind === "think" || kind === "think_retry";
    const isPrompt = kind === "prompt" || (kind === "start" && Boolean(prompt));
    const isEnd = kind === "end" || kind === "close";
    // Drop think frames that carry no reasoning and no error — they're just
    // "model produced JSON" markers and would flood the bubble stream.
    if (isThink && !reasoning && !hasError) return;
    let label: string;
    if (skill) label = action ? `${skill}.${action}` : skill;
    else if (isPrompt) label = "prompt sent";
    else if (isThink) label = "thinking";
    else if (kind === "start") label = "started";
    else if (kind === "observe") label = "observe";
    else if (isEnd) label = "finalize";
    else label = kind || "step";
    const status: MemberStep["status"] = hasError
      ? "error"
      : statusRaw === "retry" || kind === "think_retry"
      ? "retry"
      : statusRaw === "running"
      ? "running"
      : isAct || isThink
      ? "ok"
      : "info";
    const summary = String(r.summary || r.content || "").trim();
    const subject = String(r.subject || r.team_task_subject || "").trim();
    const outputText = readableText(r.output ?? r.result ?? r.outcomes);
    const detailRaw = hasError
      ? String(r.error || "")
      : reasoning ||
        summary ||
        (isPrompt
          ? [
              subject,
              promptChars ? `runtime prompt prepared (${promptChars} chars)` : "",
            ]
              .filter(Boolean)
              .join("\n")
          : "") ||
        (isEnd && outputText ? "output received" : "");
    const iteration =
      typeof r.iteration === "number" && r.iteration >= 0 ? r.iteration : undefined;
    out.push({
      key: `${i}-${kind}`,
      kind: isAct ? "act" : kind || "step",
      label,
      status,
      iteration,
      detail: clipDetail(detailRaw),
      wallMs: numOrUndef(r.wall_ms),
      ts: stepTsOrUndef(r.ts),
    });
  });
  return out;
}

export type AgentSegmentInfo = {
  callId: string;
  kind: "team" | "subagent";
  runId?: string;
  template?: string;
  task?: string;
  status: string;
  ok: boolean;
  rolesSucceeded: string[];
  rolesFailed: string[];
  tokensTotal?: number;
  usdTotal?: number;
  aggregate?: unknown;
  members: TeamMemberInfo[];
};

function numOrUndef(value: unknown): number | undefined {
  return typeof value === "number" && Number.isFinite(value) ? value : undefined;
}

function stepTimeValue(value: unknown): number | null {
  if (typeof value === "number" && Number.isFinite(value)) return value;
  if (typeof value === "string" && value.trim()) {
    const parsed = Date.parse(value);
    return Number.isFinite(parsed) ? parsed : null;
  }
  return null;
}

function stepTsOrUndef(value: unknown): number | string | undefined {
  if (typeof value === "number" && Number.isFinite(value)) return value;
  if (typeof value === "string" && value.trim()) return value;
  return undefined;
}

function memberFromRow(
  row: Record<string, unknown>,
  index: number,
  forceFailed = false,
): TeamMemberInfo {
  const name = String(
    row.subagent || row.name || row.role || `agent-${index + 1}`,
  );
  const errorKind = String(row.error_kind || "").toLowerCase();
  const error = row.error ? String(row.error) : "";
  const ok = !forceFailed && row.ok !== false && !error;
  const status: TeamMemberInfo["status"] = ok
    ? "completed"
    : errorKind === "timeout" || /timeout|timed out/i.test(error)
    ? "timeout"
    : "failed";
  // Live runs stream a rich ``steps`` list; a committed team result instead
  // carries the compacted ``metrics.skill_calls``. Fall back to the latter so
  // the step timeline still renders after the turn settles.
  let timeline = normalizeMemberSteps(row.steps);
  if (!timeline.length) {
    const skillCalls = arrayOfRecords(recordOf(row.metrics).skill_calls).filter(
      (c) => c.skill || c.action,
    );
    timeline = normalizeMemberSteps(
      skillCalls.map((c) => ({ step_kind: "act", ...c })),
    );
  }
  return {
    name,
    role: row.role_profile ? String(row.role_profile) : undefined,
    status,
    ok,
    output: row.output ?? row.summary ?? row.result,
    error: error || undefined,
    caveat: !forceFailed && row.caveat ? String(row.caveat) : undefined,
    tokens: numOrUndef(row.tokens),
    usd: numOrUndef(row.usd),
    wallMs: numOrUndef(row.wall_ms),
    raw: row,
    steps: timeline.length ? timeline : undefined,
  };
}

/** Inspect a committed turn and pull every team/subagent tool result out
 * into ordered segments, plus the set of tool-call ids whose generic
 * cards should be suppressed from the trace (they're hoisted into
 * dedicated speaker bubbles instead). */
export function collectAgentSegments(turn: TurnPayload | undefined): {
  segments: AgentSegmentInfo[];
  suppressedCallIds: Set<string>;
} {
  const segments: AgentSegmentInfo[] = [];
  const suppressedCallIds = new Set<string>();
  const blocks = turn?.blocks ?? [];
  if (!blocks.length) return { segments, suppressedCallIds };

  const payloadByCall = new Map<string, Record<string, unknown>>();
  for (const env of blocks) {
    const block = unwrapBlock(env);
    if (String(block.kind || env.kind || "") !== "tool_use") continue;
    if (!isAgentTool(block)) continue;
    const callId = toolCallId(block);
    if (callId) payloadByCall.set(callId, recordOf(block.payload));
  }

  for (const env of blocks) {
    const block = unwrapBlock(env);
    if (String(block.kind || env.kind || "") !== "tool_result") continue;
    const action = String(block.action || "");
    if (!isAgentTool(block)) continue;
    const callId = toolCallId(block);
    const parsed = parseAgentResult(block.result) ?? {};
    const payload = callId ? payloadByCall.get(callId) ?? {} : {};

    if (action === "team_run") {
      const okRows = arrayOfRecords(parsed.role_outputs ?? parsed.results);
      const failRows = arrayOfRecords(parsed.failures);
      const members: TeamMemberInfo[] = [
        ...okRows.map((row, i) => memberFromRow(row, i)),
        ...failRows.map((row, i) => memberFromRow(row, i, true)),
      ];
      if (!members.length) continue;
      const status = String(
        parsed.status || (block.ok === false ? "error" : "completed"),
      );
      segments.push({
        callId,
        kind: "team",
        runId: parsed.team_run_id ? String(parsed.team_run_id) : undefined,
        template: String(
          parsed.team_template || payload.team_template || "",
        ) || undefined,
        task: String(parsed.task || payload.task || "") || undefined,
        status,
        ok: block.ok !== false && parsed.ok !== false,
        rolesSucceeded: stringArray(parsed.roles_succeeded),
        rolesFailed: stringArray(parsed.roles_failed),
        tokensTotal: numOrUndef(parsed.tokens_total),
        usdTotal: numOrUndef(parsed.usd_total),
        aggregate: parsed.aggregated || parsed.final_context,
        members,
      });
      if (callId) suppressedCallIds.add(callId);
      continue;
    }

    // single subagent_run / subagent_run_async
    const member = memberFromRow(
      {
        subagent: parsed.subagent || payload.name || payload.subagent,
        ok: block.ok,
        output: parsed.output ?? parsed.summary ?? block.result,
        error: block.error ?? parsed.error,
        error_kind: block.error_kind,
        tokens: parsed.tokens,
        usd: parsed.usd,
        wall_ms: parsed.wall_ms ?? block.elapsed_ms,
        role_profile: parsed.role_profile,
      },
      0,
    );
    segments.push({
      callId,
      kind: "subagent",
      status: member.status,
      ok: member.ok,
      rolesSucceeded: member.ok ? [member.name] : [],
      rolesFailed: member.ok ? [] : [member.name],
      members: [member],
    });
    if (callId) suppressedCallIds.add(callId);
  }
  return { segments, suppressedCallIds };
}

/** Normalise a raw live status string (running / planned / completed /
 * skipped / failed / timeout …) into the closed ``TeamMemberInfo`` union,
 * falling back to error/output presence when the runtime hasn't set an
 * explicit status yet. */
function normalizeLiveStatus(
  raw: string,
  hasError: boolean,
  hasOutput: boolean,
): TeamMemberInfo["status"] {
  const s = raw.trim().toLowerCase();
  if (["completed", "done", "ok", "success", "succeeded"].includes(s))
    return "completed";
  if (s === "timeout" || s === "timed_out") return "timeout";
  if (["failed", "error", "errored"].includes(s)) return "failed";
  if (s === "skipped") return "skipped";
  if (["running", "started", "start", "in_progress", "active", "working"].includes(s))
    return "running";
  if (["planned", "pending", "queued", "scheduled", "waiting", ""].includes(s)) {
    if (hasError) return "failed";
    if (hasOutput) return "completed";
    return "planned";
  }
  if (hasError) return "failed";
  if (hasOutput) return "completed";
  return "running";
}

/** Build a short "what is this agent doing right now" label from the most
 * recent step in a live member record (e.g. ``web_search`` or
 * ``llm · iter 3``). Returns the running step count too. */
function liveActivity(record: Record<string, unknown>): {
  label?: string;
  steps: number;
} {
  const steps = arrayOfRecords(record.steps);
  if (!steps.length) return { steps: 0 };
  const last = steps[steps.length - 1];
  const skill = last.skill ? String(last.skill) : "";
  const action = last.action ? `.${String(last.action)}` : "";
  const stepKind = String(
    last.step_kind || last.lifecycle || last.kind || "",
  ).replace(/^subagent\./, "");
  let label = skill ? `${skill}${action}` : stepKind;
  if (typeof last.iteration === "number" && label) {
    label = `${label} · iter ${last.iteration}`;
  }
  return { label: label || undefined, steps: steps.length };
}

/** Convert one live member record (from a streaming ``team_trace.members``
 * dict, or a standalone ``subagent_trace`` block) into a ``TeamMemberInfo``
 * carrying its live state, partial output and current activity. */
function liveMemberFromRecord(
  name: string,
  record: Record<string, unknown>,
): TeamMemberInfo {
  const error = record.error ? String(record.error) : "";
  const output = record.output ?? record.summary ?? record.result;
  const hasOut = hasReadableValue(output);
  const status = normalizeLiveStatus(
    String(record.status || ""),
    Boolean(error),
    hasOut,
  );
  const { label, steps } = liveActivity(record);
  const timeline = normalizeMemberSteps(record.steps);
  const role = record.role_profile
    ? String(record.role_profile)
    : record.tier
    ? String(record.tier)
    : undefined;
  return {
    name,
    role,
    status,
    ok: status === "completed",
    output,
    error: error || undefined,
    caveat: record.caveat ? String(record.caveat) : undefined,
    tokens: numOrUndef(record.tokens),
    usd: numOrUndef(record.usd),
    wallMs: numOrUndef(record.wall_ms),
    raw: hasReadableValue(record) ? record : undefined,
    steps: timeline.length ? timeline : undefined,
    currentActivity: status === "running" ? label : undefined,
    stepCount: steps || undefined,
  };
}

function liveTeamSegment(block: NativeBlock): AgentSegmentInfo {
  const members = recordOf(block.members);
  const roles = stringArray(block.roles);
  const resultRows = arrayOfRecords(block.results);
  const names = Array.from(
    new Set([
      ...roles,
      ...Object.keys(members),
      ...resultRows.map((row, i) =>
        String(row.subagent || row.name || `agent-${i + 1}`),
      ),
    ]),
  );
  const memberInfos = names.map((name) => {
    const fromResults = resultRows.find(
      (row, j) => String(row.subagent || row.name || `agent-${j + 1}`) === name,
    );
    const merged = {
      ...recordOf(fromResults),
      ...recordOf(members[name]),
    };
    return liveMemberFromRecord(name, merged);
  });
  // ``liveEventsToBlocks`` lets a member-level event status bleed onto the
  // team block (e.g. a skip/fail), so don't trust ``block.status`` blindly:
  // if any member is still running/queued the team is still running; only
  // honour an explicit terminal team status once every member has settled.
  const rawStatus = String(block.status || "running").toLowerCase();
  const anyActive = memberInfos.some(
    (m) => m.status === "running" || m.status === "planned",
  );
  const terminalTeam = [
    "completed",
    "failed",
    "error",
    "completed_with_failures",
    "done",
  ];
  const status = anyActive
    ? "running"
    : terminalTeam.includes(rawStatus)
    ? rawStatus
    : memberInfos.length
    ? "completed"
    : rawStatus;
  const rolesSucceeded = stringArray(block.roles_succeeded);
  const rolesFailed = stringArray(block.roles_failed);
  return {
    callId: String(block.call_id || ""),
    kind: "team",
    runId: block.run_id
      ? String(block.run_id)
      : block.team_key
      ? String(block.team_key)
      : undefined,
    template: block.template_id ? String(block.template_id) : undefined,
    task: block.task ? String(block.task) : undefined,
    status,
    ok: status !== "failed" && status !== "error",
    rolesSucceeded: rolesSucceeded.length
      ? rolesSucceeded
      : memberInfos.filter((m) => m.status === "completed").map((m) => m.name),
    rolesFailed: rolesFailed.length
      ? rolesFailed
      : memberInfos
          .filter((m) => m.status === "failed" || m.status === "timeout")
          .map((m) => m.name),
    tokensTotal: numOrUndef(block.tokens_total),
    usdTotal: numOrUndef(block.usd_total),
    aggregate: block.aggregated,
    members: memberInfos,
  };
}

function liveSubagentSegment(block: NativeBlock): AgentSegmentInfo {
  const name = String(block.subagent || block.name || "subagent");
  const member = liveMemberFromRecord(name, {
    status: block.status,
    output: block.output,
    error: block.error ?? undefined,
    tokens: block.tokens,
    usd: block.usd,
    wall_ms: block.wall_ms,
    steps: block.steps,
    tier: block.tier,
    summary: block.summary,
  });
  const status = String(block.status || "running");
  return {
    callId: String(block.team_call_id || block.call_id || name),
    kind: "subagent",
    status,
    ok: member.status === "completed",
    rolesSucceeded: member.status === "completed" ? [name] : [],
    rolesFailed:
      member.status === "failed" || member.status === "timeout" ? [name] : [],
    members: [member],
  };
}

/** While a turn is still streaming, pull the in-flight team / subagent
 * activity out of the live ``team_trace`` / ``subagent_trace`` blocks so the
 * chat can render each agent as its own avatar bubble (running / queued /
 * done) instead of one opaque "Agent Team" card. Mirrors
 * ``collectAgentSegments`` but reads the cumulative live snapshot. */
export function collectLiveAgentSegments(blocks: NativeBlockEnvelope[]): {
  segments: AgentSegmentInfo[];
} {
  const segments: AgentSegmentInfo[] = [];
  if (!blocks.length) return { segments };
  const teamRunIds = new Set<string>();
  for (const env of blocks) {
    const block = unwrapBlock(env);
    if (String(block.kind || env.kind || "") !== "team_trace") continue;
    const id = String(block.run_id || block.team_key || "");
    if (id) teamRunIds.add(id);
  }
  for (const env of blocks) {
    const block = unwrapBlock(env);
    const kind = String(block.kind || env.kind || "");
    if (kind === "team_trace") {
      const seg = liveTeamSegment(block);
      if (seg.members.length) segments.push(seg);
    } else if (kind === "subagent_trace") {
      const teamRunId = String(block.team_run_id || "");
      if (teamRunId && teamRunIds.has(teamRunId)) continue;
      segments.push(liveSubagentSegment(block));
    }
  }
  return { segments };
}

function agentInitials(name: string): string {
  const parts = name
    .replace(/[_\-./]+/g, " ")
    .trim()
    .split(/\s+/)
    .filter(Boolean);
  if (!parts.length) return "AG";
  if (parts.length === 1) return parts[0].slice(0, 2).toUpperCase();
  return (parts[0][0] + parts[1][0]).toUpperCase();
}

function AgentAvatar({
  name,
  accent,
}: {
  name: string;
  accent: AgentAccent;
}) {
  return (
    <span
      className={`shrink-0 pt-1 font-mono text-[11px] font-semibold leading-none tracking-tight ${accent.text}`}
      aria-hidden
    >
      {agentInitials(name)}
    </span>
  );
}

function memberStatusKey(status: TeamMemberInfo["status"]): string {
  switch (status) {
    case "completed":
      return "completed";
    case "timeout":
      return "timeout";
    case "running":
      return "running";
    case "planned":
      return "planned";
    case "skipped":
      return "skipped";
    default:
      return "failed";
  }
}

function memberTone(
  status: TeamMemberInfo["status"],
): "ok" | "warn" | "err" | "brand" | "neutral" {
  switch (status) {
    case "completed":
      return "ok";
    case "running":
      return "brand";
    case "timeout":
      return "warn";
    case "planned":
      return "neutral";
    case "skipped":
      return "warn";
    default:
      return "err";
  }
}

function memberStepDotClass(step: MemberStep, liveLast: boolean): string {
  if (step.status === "error") return "bg-danger";
  if (step.status === "retry") return "bg-amber-300";
  if (liveLast) return "bg-fluid-400 animate-pulse";
  return "bg-emerald-300/80";
}

function memberStepLabel(
  step: MemberStep,
  labels: { started: string; promptSent: string; finalize: string; step: string },
): string {
  if (step.kind === "start") return labels.started;
  if (step.kind === "prompt" || step.label === "prompt sent") {
    return labels.promptSent;
  }
  if (step.kind === "end" || step.kind === "close") return labels.finalize;
  return step.label || labels.step;
}

function AgentStatusBubble({
  accent,
  tone = "neutral",
  muted = false,
  current = false,
  children,
}: {
  accent: AgentAccent;
  tone?: "neutral" | "ok" | "warn" | "err" | "brand";
  muted?: boolean;
  current?: boolean;
  children: ReactNode;
}) {
  const borderClass =
    tone === "err"
      ? "border-danger/35"
      : tone === "warn"
      ? "border-warn/35"
      : accent.border;
  const borderStyle =
    tone === "err" || tone === "warn"
      ? undefined
      : { borderColor: accent.borderColor };
  const stateClass =
    tone === "err"
      ? "text-danger"
      : muted
      ? "opacity-75"
      : "";
  return (
    <div
      className={`bubble-ai ${borderClass} ${stateClass} ${
        muted ? "border-dashed" : ""
      } ${current ? `ring-1 ${accent.ring}` : ""}`}
      style={borderStyle}
    >
      {children}
    </div>
  );
}

function MemberStepBubble({
  step,
  accent,
  live = false,
  current = false,
}: {
  step: MemberStep;
  accent: AgentAccent;
  live?: boolean;
  current?: boolean;
}) {
  const t = useTranslations("turnBlocks");
  const label = memberStepLabel(step, {
    started: t("started"),
    promptSent: t("promptSent"),
    finalize: t("finalize"),
    step: t("step"),
  });
  const tone =
    step.status === "info" ? "neutral" : traceTone(step.status || "ok");
  return (
    <AgentStatusBubble accent={accent} tone={tone} current={current}>
      <div className="flex items-start gap-2">
        <span
          className={`mt-[7px] h-1.5 w-1.5 shrink-0 rounded-full ${memberStepDotClass(
            step,
            current,
          )}`}
          aria-hidden
        />
        <div className="min-w-0 flex-1 space-y-1.5">
          <div className="flex items-center gap-1.5 flex-wrap">
            {step.kind === "act" ? (
              <WrenchIcon size={12} className="shrink-0 text-ink-500" />
            ) : null}
            <span
              className={`text-[12px] font-mono truncate ${
                step.status === "error" ? "text-danger" : "text-ink-100"
              }`}
            >
              {label}
            </span>
            {current ? (
              <span className="inline-flex items-center gap-1 text-[10px] text-fluid-400">
                {live ? <span className="typing-dot" /> : null}
                <span>{t("current")}</span>
              </span>
            ) : null}
            {typeof step.iteration === "number" ? (
              <Tag>{`iter ${step.iteration}`}</Tag>
            ) : null}
            <Tag tone={tone}>{step.status === "info" ? t("step") : step.status}</Tag>
            {step.wallMs ? compactNumber(step.wallMs, "ms") : null}
          </div>
          {step.detail ? (
            <div
              className={`text-[12px] leading-relaxed break-words ${
                step.status === "error" ? "text-danger" : "text-ink-300"
              }`}
            >
              <StreamedMarkdown text={step.detail} active={live && current} />
            </div>
          ) : null}
        </div>
      </div>
    </AgentStatusBubble>
  );
}

function MemberOutputBubble({
  member,
  accent,
  stream = false,
}: {
  member: TeamMemberInfo;
  accent: AgentAccent;
  stream?: boolean;
}) {
  const t = useTranslations("turnBlocks");
  if (!hasReadableValue(member.output)) return null;
  const text = readableText(member.output);
  const entries = text ? [] : structuredEntries(member.output);
  return (
    <AgentStatusBubble accent={accent} tone={member.ok ? "ok" : "neutral"}>
      <div className="space-y-2">
        <div className="flex items-center justify-between gap-2">
          <div className="flex items-center gap-1.5 text-[11px] font-medium text-ink-400">
            <span>{t("finalOutput")}</span>
          </div>
          {text ? <CopyButton text={text} /> : null}
        </div>
        {text ? (
          <div className="text-[14px] leading-relaxed text-ink-100">
            <StreamedMarkdown text={text} active={stream} />
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
          <JsonBlock value={member.output} />
        )}
      </div>
    </AgentStatusBubble>
  );
}

function AgentMemberBubble({
  member,
  live = false,
}: {
  member: TeamMemberInfo;
  live?: boolean;
}) {
  const t = useTranslations("turnBlocks");
  const accent = agentAccent(member.name);
  const isRunning = member.status === "running";
  const isPlanned = member.status === "planned";
  const isSkipped = member.status === "skipped";
  const muted = isPlanned || isSkipped;
  const tone = memberTone(member.status);
  const steps = member.steps ?? [];
  const MAX_STEPS = 80;
  const overflow = steps.length > MAX_STEPS ? steps.length - MAX_STEPS : 0;
  const shownSteps = overflow ? steps.slice(steps.length - MAX_STEPS) : steps;
  // A "no output" hint only makes sense for a member that actually finished
  // without returning anything — never for queued / running / skipped ones.
  const showNoOutput =
    !member.error &&
    !isRunning &&
    !muted &&
    !hasReadableValue(member.output);
  return (
    <div
      className={`flex gap-2.5 ${muted ? "opacity-70" : ""}`}
      data-agent-member={member.name}
      data-agent-status={member.status}
    >
      <div className="relative w-8 shrink-0 text-right">
        <AgentAvatar name={member.name} accent={accent} />
        {isRunning ? (
          <span
            className={`absolute right-0 top-4 h-1.5 w-1.5 rounded-full ${accent.dot} animate-pulse`}
            aria-hidden
          />
        ) : null}
      </div>
      <div className="flex-1 min-w-0 space-y-1.5">
        <div className="flex items-center gap-2 flex-wrap">
          <span className={`text-[12px] font-semibold tracking-tight ${accent.text}`}>
            {member.name}
          </span>
          {member.role && member.role !== member.name ? (
            <span className="text-[10px] text-ink-500 font-mono truncate">
              {member.role}
            </span>
          ) : null}
          <Tag tone={tone}>{t(memberStatusKey(member.status))}</Tag>
          {member.caveat && !member.error ? (
            <Tag tone="warn">{t("withCaveats")}</Tag>
          ) : null}
          {compactNumber(member.tokens)}
          {compactNumber(member.usd, "usd")}
          {compactNumber(member.wallMs, "ms")}
        </div>
        {isPlanned ? (
          <AgentStatusBubble accent={accent} tone="neutral" muted>
            <div className="flex items-center gap-2 text-[12px] text-ink-400 italic">
              <span className="h-1.5 w-1.5 rounded-full bg-ink-500" />
              {t("queued")}
            </div>
          </AgentStatusBubble>
        ) : null}
        {isSkipped ? (
          <AgentStatusBubble accent={accent} tone="warn" muted>
            <div className="text-[12px] text-ink-400 italic break-words">
              {member.error || t("skipped")}
            </div>
          </AgentStatusBubble>
        ) : null}
        {member.error && !isSkipped ? (
          <AgentStatusBubble accent={accent} tone="err">
            <div className="text-[12px] text-danger break-words">
              <StreamedPlainText text={member.error} active={live} />
            </div>
          </AgentStatusBubble>
        ) : null}
        {isRunning && !shownSteps.length ? (
          <AgentStatusBubble accent={accent} tone="brand" current>
            <div className="flex items-center gap-2 text-[12px] text-fluid-400">
              <span className="typing-dot" />
              <span className="text-ink-200">
                {member.currentActivity || t("working")}
              </span>
            </div>
          </AgentStatusBubble>
        ) : null}
        {!muted && overflow ? (
          <div className="px-1 text-[10px] text-ink-600 italic">
            {t("moreSteps", { count: overflow })}
          </div>
        ) : null}
        {!muted
          ? shownSteps.map((step, i) => {
              const current = live && isRunning && i === shownSteps.length - 1;
              return (
                <MemberStepBubble
                  key={step.key}
                  step={step}
                  accent={accent}
                  live={live}
                  current={current}
                />
              );
            })
          : null}
        {!muted ? (
          <MemberOutputBubble
            member={member}
            accent={accent}
            stream={live && hasReadableValue(member.output)}
          />
        ) : null}
        {showNoOutput ? (
          <AgentStatusBubble accent={accent} tone="neutral">
            <div className="text-[12px] text-ink-400 italic">{t("noOutput")}</div>
          </AgentStatusBubble>
        ) : null}
        {member.raw && !muted && hasReadableValue(member.raw) ? (
          <RawDetails>
            <JsonPanel label={t("rawSubagentResult")} value={member.raw} />
          </RawDetails>
        ) : null}
      </div>
    </div>
  );
}

function TeamHeaderRow({
  segment,
  live = false,
}: {
  segment: AgentSegmentInfo;
  live?: boolean;
}) {
  const t = useTranslations("turnBlocks");
  const total = segment.members.length;
  const done = segment.members.filter((m) => m.status === "completed").length;
  const anyRunning = segment.members.some((m) => m.status === "running");
  const isRunning =
    anyRunning ||
    segment.status.toLowerCase() === "running" ||
    segment.status.toLowerCase() === "started";
  return (
    <div className="rounded-xl border border-brand-500/25 bg-brand-500/[0.05] px-3 py-2 space-y-1.5">
      <div className="flex items-center gap-2 flex-wrap">
        <SubagentsIcon size={14} className="text-brand-300" />
        <span className="text-[12px] font-semibold text-ink-100">
          {t("agentTeam")}
        </span>
        {segment.template ? <Tag tone="brand">{segment.template}</Tag> : null}
        <Tag tone={traceTone(segment.status)}>{segment.status}</Tag>
        {total ? (
          <Tag tone={done === total && !isRunning ? "ok" : "neutral"}>
            {t("teamProgress", { done, total })}
          </Tag>
        ) : null}
        {segment.rolesFailed.length ? (
          <Tag tone="err">
            {t("failed")}: {segment.rolesFailed.length}
          </Tag>
        ) : null}
        {isRunning ? (
          <span className="inline-flex items-center gap-1 text-[10px] text-fluid-400">
            <span className="typing-dot" />
            <span>{t("running")}</span>
          </span>
        ) : null}
        {compactNumber(segment.tokensTotal)}
        {compactNumber(segment.usdTotal, "usd")}
      </div>
      {segment.task ? (
        <div className="text-[12px] text-ink-300 leading-relaxed">
          {segment.task}
        </div>
      ) : null}
      {/* The aggregate is just the per-member summaries concatenated, which
       * each member bubble already shows below — so in the group-chat layout
       * we keep it out of the banner and tuck the raw value behind a details
       * toggle for anyone who wants the combined JSON. */}
      {!live && hasReadableValue(segment.aggregate) ? (
        <RawDetails title={t("teamAggregate")}>
          <OutputPanel label={t("teamAggregate")} value={segment.aggregate} />
        </RawDetails>
      ) : null}
    </div>
  );
}

/** Renders one team/subagent segment as a self-contained top-level row:
 * a team header banner followed by one distinct avatar+bubble per
 * member, so each sub-agent reads like its own speaker in the thread. */
export function AgentSegmentBlock({
  segment,
  live = false,
}: {
  segment: AgentSegmentInfo;
  live?: boolean;
}) {
  // Group-chat layout: the team banner is a slim system row, and every
  // sub-agent is its own left-aligned chat bubble (same column width as the
  // Nerya speaker) so the thread reads like distinct participants talking,
  // not one packed team panel.
  const transcript = agentTranscriptItems(segment, { includeOutputs: true });
  return (
    <div
      className="space-y-3"
      data-agent-segment={segment.runId || segment.callId}
    >
      {segment.kind === "team" ? (
        <div className="flex justify-start">
          <div className="max-w-[92%] min-w-[200px] w-full">
            <TeamHeaderRow segment={segment} />
          </div>
        </div>
      ) : null}
      <div className="space-y-2.5" data-agent-transcript-messages={transcript.length}>
        {transcript.map((item) => (
          <AgentTranscriptRow
            key={`${segment.callId || segment.runId || "segment"}-${item.key}`}
            item={item}
            live={live}
          />
        ))}
      </div>
    </div>
  );
}

type AgentTranscriptItem = {
  key: string;
  member: TeamMemberInfo;
  kind: "step" | "output";
  step?: MemberStep;
  memberIndex: number;
  stepIndex: number;
  order: number;
};

function agentTranscriptItems(
  segment: AgentSegmentInfo,
  { includeOutputs = false }: { includeOutputs?: boolean } = {},
): AgentTranscriptItem[] {
  const items: AgentTranscriptItem[] = [];
  let order = 0;
  segment.members.forEach((member, memberIndex) => {
    const steps = member.steps ?? [];
    if (steps.length) {
      steps.forEach((step, stepIndex) => {
        items.push({
          key: `${member.name}-${step.key}-${stepIndex}`,
          member,
          kind: "step",
          step,
          memberIndex,
          stepIndex,
          order: order++,
        });
      });
    } else if (member.status === "running" || member.status === "planned") {
      items.push({
        key: `${member.name}-waiting`,
        member,
        kind: "step",
        step: {
          key: "waiting",
          kind: member.status === "planned" ? "start" : "step",
          label: member.currentActivity || "working",
          status: member.status === "running" ? "running" : "info",
        },
        memberIndex,
        stepIndex: 0,
        order: order++,
      });
    }
    if (includeOutputs && hasReadableValue(member.output)) {
      items.push({
        key: `${member.name}-output`,
        member,
        kind: "output",
        memberIndex,
        stepIndex: steps.length,
        order: order++,
      });
    }
  });
  return items.sort((a, b) => {
    const aTs = stepTimeValue(a.step?.ts);
    const bTs = stepTimeValue(b.step?.ts);
    if (aTs !== null && bTs !== null && aTs !== bTs) {
      return aTs - bTs;
    }
    if (aTs !== null && bTs === null) return -1;
    if (aTs === null && bTs !== null) return 1;
    return a.order - b.order;
  });
}

function AgentTranscriptRow({
  item,
  live,
}: {
  item: AgentTranscriptItem;
  live: boolean;
}) {
  const accent = agentAccent(item.member.name);
  const steps = item.member.steps ?? [];
  const current =
    live &&
    item.kind === "step" &&
    item.member.status === "running" &&
    item.stepIndex === steps.length - 1;
  return (
    <div
      className="flex justify-start"
      data-agent-live-message={item.member.name}
      data-agent-live-step={item.step?.kind || item.kind}
    >
      <div className="max-w-[92%] min-w-[200px] w-full">
        <div className="flex gap-2.5">
          <div className="relative w-8 shrink-0 text-right">
            <AgentAvatar name={item.member.name} accent={accent} />
            {current ? (
              <span
                className={`absolute right-0 top-4 h-1.5 w-1.5 rounded-full ${accent.dot} animate-pulse`}
                aria-hidden
              />
            ) : null}
          </div>
          <div className="min-w-0 flex-1 space-y-1">
            <div className="flex items-center gap-2">
              <span className={`text-[12px] font-semibold tracking-tight ${accent.text}`}>
                {item.member.name}
              </span>
              {item.member.role && item.member.role !== item.member.name ? (
                <span className="truncate text-[10px] font-mono text-ink-500">
                  {item.member.role}
                </span>
              ) : null}
            </div>
            {item.kind === "output" ? (
              <MemberOutputBubble
                member={item.member}
                accent={accent}
                stream={live && hasReadableValue(item.member.output)}
              />
            ) : item.step ? (
              <MemberStepBubble
                step={item.step}
                accent={accent}
                live={live}
                current={current}
              />
            ) : null}
          </div>
        </div>
      </div>
    </div>
  );
}

export function LiveAgentTranscriptBlock({
  segments,
}: {
  segments: AgentSegmentInfo[];
}) {
  const visibleSegments = segments.filter((segment) => segment.members.length > 0);
  if (!visibleSegments.length) return null;

  return (
    <div className="space-y-3" data-agent-live-transcript="true">
      {visibleSegments.map((segment, segmentIndex) => {
        const key =
          segment.runId ||
          segment.callId ||
          `${segment.kind}-${segmentIndex}`;
        const transcript = agentTranscriptItems(segment);
        return (
          <div
            key={key}
            className="space-y-2.5"
            data-agent-live-segment={segment.runId || segment.callId || segment.kind}
          >
            {segment.kind === "team" ? (
              <div className="flex justify-start">
                <div className="max-w-[92%] min-w-[200px] w-full">
                  <TeamHeaderRow segment={segment} live />
                </div>
              </div>
            ) : null}
            <div className="space-y-2.5" data-agent-live-messages={transcript.length}>
              {transcript.map((item) => (
                <AgentTranscriptRow
                  key={`${key}-${item.key}`}
                  item={item}
                  live
                />
              ))}
            </div>
          </div>
        );
      })}
    </div>
  );
}

/** Collapses a run of routine "figuring things out" steps (interleaved
 * thinking + low-signal tool lookups like role_list / subagent_list /
 * role_get) into a single closed-by-default card, so the high-signal
 * cards (skills, file ops, charts, errors, agent teams) aren't buried.
 * Expanding the card replays each original step via ``renderOne``. */
function ProcessGroupCard({
  items,
  toolCalls,
  renderOne,
}: {
  items: { env: NativeBlockEnvelope; i: number }[];
  toolCalls: number;
  renderOne: (env: NativeBlockEnvelope, i: number) => ReactNode;
}) {
  const t = useTranslations("turnBlocks");
  const names: string[] = [];
  for (const { env } of items) {
    const block = unwrapBlock(env);
    if ((block.kind || env.kind || "") !== "tool_use") continue;
    const name = String(block.action || block.skill_id || "tool");
    if (!names.includes(name)) names.push(name);
  }
  const preview = names.slice(0, 4).join(", ") + (names.length > 4 ? "…" : "");
  return (
    <Collapsible
      title={
        <span className="flex items-center gap-2 min-w-0">
          <WrenchIcon size={13} className="text-ink-400" />
          <span className="text-ink-400">{t("process")}</span>
          <span className="font-mono text-ink-300 truncate">{preview}</span>
        </span>
      }
      tone="neutral"
      defaultOpen={false}
      badge={<Tag>{t("stepsCount", { count: toolCalls })}</Tag>}
    >
      <div className="space-y-1.5">{items.map(({ env, i }) => renderOne(env, i))}</div>
    </Collapsible>
  );
}

export function NativeBlocksTrack({
  envelopes,
  live = false,
  streamText = false,
  label = "",
  pendingApprovals,
  onApprovalAction,
  resolvingApprovalIds,
  suppressTopProposalHoist = false,
  suppressAgentResultCallIds,
  hoistTeamTraces = false,
}: {
  envelopes: NativeBlockEnvelope[];
  live?: boolean;
  streamText?: boolean;
  label?: string;
  pendingApprovals?: Map<string, ApprovalCard>;
  onApprovalAction?: (callbackData: string) => void;
  resolvingApprovalIds?: Set<string>;
  // When true, don't render the proposal card at the top of the track —
  // the duplicates inside collapsed tool results are still suppressed, but the
  // prominent approve/add card is rendered at the bottom of the bubble instead
  // (right under the agent's plain-language verdict). See ``AssistantBubble``.
  suppressTopProposalHoist?: boolean;
  // Tool-call ids for team_run / subagent_run calls that are hoisted into
  // dedicated speaker bubbles (see ``AssistantBubble``). Their generic
  // tool_use / tool_result cards are dropped here so the work isn't shown
  // twice.
  suppressAgentResultCallIds?: Set<string>;
  // Live counterpart of ``suppressAgentResultCallIds``: while streaming the
  // team activity lives in ``team_trace`` / ``subagent_trace`` blocks (no
  // committed tool_result yet). When the live segments are hoisted into
  // dedicated speaker bubbles, drop every team/subagent trace AND any agent
  // tool_use/tool_result so the Nerya bubble keeps only its own work.
  hoistTeamTraces?: boolean;
}) {
  if (!envelopes.length) return null;
  const displayEnvelopes = mergeToolUsePayloadsIntoResults(envelopes);
  // Hoist any active strategy proposals to a prominent card at the top
  // of the track and suppress their buried duplicates inside the
  // collapsed tool_result cards below.
  const hoistedProposals = activeProposalsFromEnvelopes(displayEnvelopes);
  const suppressProposalIds = new Set(
    hoistedProposals.map((p) => String(p.id)),
  );
  // Find the index of the last block that hasn't yet committed (a
  // tool_use that has no matching tool_result yet, or a partial text
  // block). That block stays expanded while we're streaming so the
  // operator sees what the model is doing right now.
  let pendingIdx = -1;
  if (live) {
    const openCalls = new Map<string, number>();
    displayEnvelopes.forEach((env, i) => {
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
  displayEnvelopes.forEach((env) => {
    const block = unwrapBlock(env);
    const kind = (block.kind || env.kind || "").toString();
    if (kind !== "team_trace") return;
    const id = String(block.run_id || block.team_key || "");
    if (id) teamRunIds.add(id);
  });

  // Call ids whose tool_result failed — keep those (and their tool_use)
  // visible instead of folding them into the routine "process" group.
  const errorCallIds = new Set<string>();
  displayEnvelopes.forEach((env) => {
    const block = unwrapBlock(env);
    if ((block.kind || env.kind || "") !== "tool_result") return;
    if (block.ok === false || block.error) {
      const callId = toolCallId(block);
      if (callId) errorCallIds.add(callId);
    }
  });

  // Per-block renderer (shared by the top-level track and the collapsed
  // ``ProcessGroupCard`` body).
  const renderOne = (env: NativeBlockEnvelope, i: number): ReactNode => {
    const block = unwrapBlock(env);
    const kind = (block.kind || env.kind || "").toString();
    const auto = i === pendingIdx && live;
    if (kind === "text")
      return <NativeTextBlock key={i} block={block} stream={live || streamText} />;
    if (kind === "thinking")
      return (
        <NativeThinkingBlock
          key={i}
          block={block}
          defaultOpen={auto}
          autoOpen={live ? auto : undefined}
          stream={live || streamText}
        />
      );
    if (kind === "tool_use") {
      // pending = no matching tool_result later in the envelope list
      let hasResult = false;
      for (let j = i + 1; j < displayEnvelopes.length; j += 1) {
        const next =
          displayEnvelopes[j].block ??
          (displayEnvelopes[j] as unknown as NativeBlock);
        if (
          (next.kind || displayEnvelopes[j].kind) === "tool_result" &&
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
          defaultOpen={auto}
          suppressProposalIds={suppressProposalIds}
          stream={live || streamText}
        />
      );
    if (kind === "attachment")
      return <NativeAttachmentBlock key={i} block={block} />;
    if (kind === "chart") return <NativeChartBlock key={i} block={block} />;
    if (kind === "team_trace")
      return (
        <NativeTeamTraceBlock key={i} block={block} defaultOpen={auto} live={live} />
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
  };

  // A "routine" envelope is intermediate reasoning or a low-signal native
  // lookup (role_list / subagent_list / role_get / resource_list …) that
  // succeeded. Friendly cards (skill / file / shell / web / todo), charts,
  // attachments, errors and agent teams are always high-signal and never
  // folded. Grouping is disabled while streaming so the live trace is
  // never hidden behind a collapsed card.
  const isRoutine = (env: NativeBlockEnvelope): boolean => {
    const block = unwrapBlock(env);
    const kind = (block.kind || env.kind || "").toString();
    if (kind === "thinking") return true;
    if (kind === "tool_use") {
      if (isAgentTool(block) || isFriendlyToolCard(block)) return false;
      const callId = toolCallId(block);
      if (callId && errorCallIds.has(callId)) return false;
      return true;
    }
    if (kind === "tool_result") {
      if (isAgentTool(block) || isFriendlyToolCard(block)) return false;
      if (block.ok === false || block.error) return false;
      return true;
    }
    return false;
  };

  const rendered: ReactNode[] = [];
  let run: { env: NativeBlockEnvelope; i: number }[] = [];
  const flushRun = () => {
    if (!run.length) return;
    const toolCalls = run.filter(
      ({ env }) => (unwrapBlock(env).kind || env.kind || "") === "tool_use",
    ).length;
    if (toolCalls >= 2) {
      rendered.push(
        <ProcessGroupCard
          key={`grp-${run[0].i}`}
          items={run}
          toolCalls={toolCalls}
          renderOne={renderOne}
        />,
      );
    } else {
      for (const { env, i } of run) {
        const node = renderOne(env, i);
        if (node) rendered.push(node);
      }
    }
    run = [];
  };
  displayEnvelopes.forEach((env, i) => {
    const block = unwrapBlock(env);
    const kind = (block.kind || env.kind || "").toString();
    // team_run / subagent_run hoisted into dedicated speaker bubbles.
    if (suppressAgentResultCallIds?.size && isAgentTool(block)) {
      const callId = toolCallId(block);
      if (callId && suppressAgentResultCallIds.has(callId)) return;
    }
    // Live hoist mode: the team is mid-flight so there are no committed
    // tool ids to match — drop every agent tool_use/tool_result outright
    // since the streaming activity is shown in the dedicated bubbles.
    if (hoistTeamTraces && isAgentTool(block)) return;
    // In hoist mode the inline team / subagent activity traces are
    // superseded by the dedicated speaker bubbles rendered outside this
    // track, so drop the duplicates.
    if (
      (suppressAgentResultCallIds?.size || hoistTeamTraces) &&
      (kind === "team_trace" || kind === "subagent_trace")
    ) {
      return;
    }
    // Backend batch bookkeeping — never had a renderer, only noise.
    if (kind === "tool_batch_summary") return;
    if (!live && !streamText && isRoutine(env)) {
      run.push({ env, i });
      return;
    }
    flushRun();
    const node = renderOne(env, i);
    if (node) rendered.push(node);
  });
  flushRun();

  return (
    <div className="space-y-1.5">
      {suppressTopProposalHoist ? null : (
        <StrategyProposalsHoist proposals={hoistedProposals} />
      )}
      <TraceSummary envelopes={displayEnvelopes} live={live} />
      {label || live ? (
        <div className="flex items-center gap-2 text-[10.5px] text-ink-500 font-medium">
          {label ? <span>{label}</span> : null}
          {live ? (
            <span className="inline-flex items-center gap-1 text-fluid-400">
              <span className="typing-dot" />
              <span>live</span>
            </span>
          ) : null}
        </div>
      ) : null}
      {rendered}
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
  suppressTopProposalHoist = false,
  suppressAgentResultCallIds,
  streamText = false,
}: {
  turn: TurnPayload;
  pendingApprovals?: Map<string, ApprovalCard>;
  onApprovalAction?: (callbackData: string) => void;
  approvalEvents?: LiveEvent[];
  activityEvents?: LiveEvent[];
  replayEvents?: LiveEvent[];
  resolvingApprovalIds?: Set<string>;
  // Forwarded to ``NativeBlocksTrack`` and applied to the legacy hoist below so
  // the approve/add card can be rendered at the bottom of the bubble instead.
  suppressTopProposalHoist?: boolean;
  // Team/subagent tool-call ids hoisted into dedicated speaker bubbles.
  suppressAgentResultCallIds?: Set<string>;
  // Animate text-like committed blocks for a just-settled turn without
  // changing the structural live trace behavior.
  streamText?: boolean;
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
  // When native blocks exist, NativeBlocksTrack hoists proposals itself.
  // Only the legacy actions/tools path needs its own hoist here.
  const legacyProposals = blocks.length
    ? []
    : activeProposalsFromLegacy(tools, actions);
  const legacySuppressProposalIds = new Set(
    legacyProposals.map((p) => String(p.id)),
  );

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
    <div className="mt-2 space-y-1.5">
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

      {/* Legacy-only proposal hoist: when there are no native blocks the
        * proposal lives inside a collapsed legacy tool/action card, so we
        * surface it prominently here too — unless the bubble is rendering the
        * approve/add card at the bottom (suppressTopProposalHoist). */}
      {suppressTopProposalHoist ? null : (
        <StrategyProposalsHoist proposals={legacyProposals} />
      )}

      {/* when the workspace-native loop produced block
        * envelopes, render them up top in their original chronological
        * order. The legacy ``actions`` / ``tool_trace`` views below act
        * as a familiar summary, but the native track is the operator's
        * primary lens because it matches what the model actually saw.
        */}
      {blocks.length ? (
        <NativeBlocksTrack
          envelopes={blocks}
          streamText={streamText}
          pendingApprovals={pendingApprovals}
          onApprovalAction={onApprovalAction}
          resolvingApprovalIds={resolvingApprovalIds}
          suppressTopProposalHoist={suppressTopProposalHoist}
          suppressAgentResultCallIds={suppressAgentResultCallIds}
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
            <ToolBlock key={i} t={t} suppressProposalIds={legacySuppressProposalIds} />
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
            <ActionBlock key={i} action={a} suppressProposalIds={legacySuppressProposalIds} />
          ))}
        </div>
      ) : null}
    </div>
  );
}
