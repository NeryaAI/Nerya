"use client";

import type { ApprovalCard } from "../../lib/clientApi";
import {
  CheckIcon,
  ShieldCheckIcon,
  ShieldXIcon,
} from "../icons";

export function approvalIdFromEvent(ev: Record<string, unknown>): string {
  return String(ev.approval_id || ev.id || "");
}

function asRecord(value: unknown): Record<string, unknown> {
  return value && typeof value === "object"
    ? (value as Record<string, unknown>)
    : {};
}

function asArray(value: unknown): unknown[] {
  return Array.isArray(value) ? value : [];
}

function permissionItems(
  event: Record<string, unknown>,
  card?: ApprovalCard,
): Record<string, unknown>[] {
  const promptMetadata = asRecord(card?.prompt?.metadata ?? asRecord(event.prompt).metadata);
  const record = asRecord(card?.record ?? event.record);
  const payload = asRecord(record.payload);
  const candidates = [
    promptMetadata.items,
    record.items,
    payload.items,
  ];
  for (const candidate of candidates) {
    const items = asArray(candidate).filter(
      (item): item is Record<string, unknown> =>
        !!item && typeof item === "object",
    );
    if (items.length) return items;
  }
  return [];
}

function itemTool(item: Record<string, unknown>): Record<string, unknown> {
  const payload = asRecord(item.payload);
  return {
    ...asRecord(payload.tool),
    ...asRecord(item.tool),
  };
}

function itemArguments(item: Record<string, unknown>): Record<string, unknown> {
  const payload = asRecord(item.payload);
  return asRecord(payload.arguments ?? item.arguments);
}

function summarizeArguments(args: Record<string, unknown>): string {
  for (const key of ["cmd", "command", "path", "file", "url", "market", "symbol"]) {
    const value = args[key];
    if (value !== undefined && value !== null && String(value).trim()) {
      const text = String(value).trim();
      return `${key}=${text.length > 120 ? `${text.slice(0, 117)}...` : text}`;
    }
  }
  return Object.entries(args)
    .slice(0, 4)
    .map(([key, value]) => {
      const text = String(value);
      return `${key}=${text.length > 80 ? `${text.slice(0, 77)}...` : text}`;
    })
    .join(" | ");
}

function stateFromEvent(event: Record<string, unknown>): string {
  const record = event.record && typeof event.record === "object"
    ? (event.record as Record<string, unknown>)
    : {};
  return String(
    event.state ||
      event.resolved_state ||
      record.state ||
      "",
  ).toLowerCase();
}

export function ApprovalRequestCard({
  event,
  card,
  onAction,
  busy = false,
}: {
  event: Record<string, unknown>;
  card?: ApprovalCard;
  onAction?: (callbackData: string) => void;
  busy?: boolean;
}) {
  const approvalId = approvalIdFromEvent(event);
  if (!approvalId) return null;
  const prompt =
    card?.prompt ||
    ((event.prompt as ApprovalCard["prompt"] | undefined) ?? undefined);
  const rawState = stateFromEvent(event);
  const state = rawState || (card ? "pending" : "pending");
  const resolved = state === "approved" || state === "rejected";
  const text =
    prompt?.text ||
    String(event.reason || "This tool call needs operator permission.");
  const items = permissionItems(event, card);
  const isBatch =
    items.length > 1 ||
    Boolean(prompt?.metadata?.tool_batch) ||
    String(asRecord(card?.record ?? event.record).kind || "") === "tool_permission_batch";
  const buttons = resolved ? [] : prompt?.buttons ?? [];
  const statusTone =
    state === "approved"
      ? "border-brand-500/40 bg-brand-500/[0.08] text-brand-200"
      : state === "rejected"
      ? "border-[#ef5564]/40 bg-[#ef5564]/[0.08] text-[#ffb3bd]"
      : "border-[#f5a524]/40 bg-[#f5a524]/[0.07] text-[#f5a524]";
  return (
    <div className={`rounded-lg border px-3 py-2.5 space-y-2 ${statusTone}`}>
      <div className="flex items-center justify-between gap-2">
        <div className="text-[10px] uppercase tracking-[0.18em]">
          {state === "approved"
            ? "Permission approved"
            : state === "rejected"
            ? "Permission rejected"
            : "Permission request"}
        </div>
        <span className="font-mono text-[10px] text-ink-400">
          {approvalId.slice(0, 18)}
        </span>
      </div>
      {items.length ? (
        <div className="space-y-2">
          <div className="text-xs leading-relaxed text-ink-100">
            {isBatch
              ? `${items.length} tool calls require approval. One approve continues all of them.`
              : "This tool call needs operator permission."}
          </div>
          <div className="space-y-1.5">
            {items.map((item, idx) => {
              const tool = itemTool(item);
              const skillId = String(tool.skill_id || item.skill_id || "native");
              const toolName = String(tool.name || item.action || "tool");
              const callId = String(item.tool_use_id || tool.call_id || "");
              const reason = String(item.reason || "").trim();
              const argSummary = summarizeArguments(itemArguments(item));
              return (
                <div
                  key={`${callId || toolName}-${idx}`}
                  className="rounded-md border border-white/10 bg-black/15 px-2 py-1.5"
                >
                  <div className="flex items-center justify-between gap-2">
                    <span className="font-mono text-[11px] text-ink-100 truncate">
                      {idx + 1}. {skillId}.{toolName}
                    </span>
                    {callId ? (
                      <span className="font-mono text-[10px] text-ink-500 shrink-0">
                        {callId.slice(0, 10)}
                      </span>
                    ) : null}
                  </div>
                  {argSummary || reason ? (
                    <div className="mt-1 text-[11px] leading-relaxed text-ink-300 break-words">
                      {argSummary || reason}
                    </div>
                  ) : null}
                </div>
              );
            })}
          </div>
        </div>
      ) : (
        <pre className="whitespace-pre-wrap break-words text-xs leading-relaxed text-ink-100 font-sans">
          {text}
        </pre>
      )}
      {!resolved ? (
        <div className="flex flex-wrap items-center gap-2">
          {buttons.map((button) => {
            const style = button.style || "default";
            const cls =
              style === "danger"
                ? "border-[#ef5564]/50 bg-[#ef5564]/10 text-[#ffb3bd] hover:bg-[#ef5564]/20"
                : style === "primary"
                ? "border-accent-400/50 bg-accent-400/10 text-accent-400 hover:bg-accent-400/20"
                : "border-white/10 bg-white/[0.04] text-ink-200 hover:bg-white/[0.08]";
            const lower = button.callback_data.toLowerCase();
            const Icon =
              style === "danger" || lower.startsWith("reject:")
                ? ShieldXIcon
                : style === "primary" || lower.startsWith("approve:")
                ? ShieldCheckIcon
                : CheckIcon;
            return (
              <button
                key={button.callback_data}
                onClick={() => onAction?.(button.callback_data)}
                disabled={busy}
                className={`inline-flex items-center gap-1.5 rounded-md border px-2.5 py-1 text-xs transition-colors disabled:opacity-50 disabled:cursor-wait ${cls}`}
              >
                <Icon size={14} />
                <span>{busy ? "Working..." : button.label}</span>
              </button>
            );
          })}
          {buttons.length === 0 ? (
            <>
              <button
                onClick={() => onAction?.(`approve:${approvalId}`)}
                disabled={busy}
                className="inline-flex items-center gap-1.5 rounded-md border border-accent-400/50 bg-accent-400/10 px-2.5 py-1 text-xs text-accent-400 hover:bg-accent-400/20 transition-colors disabled:opacity-50 disabled:cursor-wait"
              >
                <ShieldCheckIcon size={14} />
                <span>{busy ? "Working..." : "Approve"}</span>
              </button>
              <button
                onClick={() => onAction?.(`reject:${approvalId}`)}
                disabled={busy}
                className="inline-flex items-center gap-1.5 rounded-md border border-[#ef5564]/50 bg-[#ef5564]/10 px-2.5 py-1 text-xs text-[#ffb3bd] hover:bg-[#ef5564]/20 transition-colors disabled:opacity-50 disabled:cursor-wait"
              >
                <ShieldXIcon size={14} />
                <span>{busy ? "Working..." : "Reject"}</span>
              </button>
            </>
          ) : null}
        </div>
      ) : null}
    </div>
  );
}
