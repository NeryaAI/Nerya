"use client";

import { useTranslations } from "next-intl";
import type { ApprovalCard } from "../../lib/clientApi";
import {
  alert as alertDialog,
  confirm as confirmDialog,
} from "../../lib/dialogs";
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

function tradeApprovalData(
  event: Record<string, unknown>,
  card?: ApprovalCard,
): {
  record: Record<string, unknown>;
  intent: Record<string, unknown>;
  risk: Record<string, unknown>;
  isTrade: boolean;
} {
  const record = asRecord(card?.record ?? event.record);
  const payload = asRecord(record.payload);
  const metadata = asRecord(card?.prompt?.metadata ?? asRecord(event.prompt).metadata);
  const intent = asRecord(metadata.intent ?? payload.intent ?? record.intent);
  const risk = asRecord(metadata.risk ?? payload.risk ?? record.risk);
  return {
    record,
    intent,
    risk,
    isTrade:
      String(record.kind || metadata.kind || "") === "trade_intent" ||
      String(event.action || "") === "trade_intent_submit",
  };
}

function walletSwapApprovalData(
  event: Record<string, unknown>,
  card?: ApprovalCard,
): {
  record: Record<string, unknown>;
  request: Record<string, unknown>;
  quote: Record<string, unknown>;
  risk: Record<string, unknown>;
  isWalletSwap: boolean;
} {
  const record = asRecord(card?.record ?? event.record);
  const payload = asRecord(record.payload);
  const metadata = asRecord(card?.prompt?.metadata ?? asRecord(event.prompt).metadata);
  const request = asRecord(
    metadata.wallet_swap ?? payload.wallet_swap ?? record.wallet_swap,
  );
  const quote = asRecord(metadata.quote ?? payload.quote ?? record.quote);
  const risk = asRecord(metadata.risk ?? payload.risk ?? record.risk);
  return {
    record,
    request,
    quote,
    risk,
    isWalletSwap:
      String(record.kind || metadata.kind || "") === "wallet_swap" ||
      String(event.action || "") === "wallet_swap",
  };
}

function displayNumber(value: unknown, digits = 4): string {
  const number = Number(value);
  if (!Number.isFinite(number)) return value == null ? "—" : String(value);
  return number.toLocaleString(undefined, { maximumFractionDigits: digits });
}

function TradeApprovalDetails({
  record,
  intent,
  risk,
  labels,
}: {
  record: Record<string, unknown>;
  intent: Record<string, unknown>;
  risk: Record<string, unknown>;
  labels: Record<string, string>;
}) {
  const estimatedNotional =
    record.notional_usd ?? risk.estimated_notional_usd ?? intent.notional_usd;
  const reasons = asArray(risk.reasons ?? record.risk_reasons).map(String);
  const meta = asRecord(intent.meta);
  const frozenPlan = asRecord(record.frozen_plan);
  const protection = asRecord(frozenPlan.protection);
  const reduceOnly =
    meta.reduce_only === true || String(meta.reduce_only || "").toLowerCase() === "true";
  const size = `${displayNumber(intent.size)} ${String(intent.size_unit || "")}`.trim();
  const rows = [
    [labels.mode, String(record.execution_mode || "unknown").toUpperCase()],
    [labels.account, String(intent.account_id || record.account_id || "—")],
    [labels.strategy, String(intent.strategy_id || record.strategy_id || "—")],
    [labels.market, String(intent.market || record.market || "—")],
    [labels.side, String(intent.side || record.side || "—").toUpperCase()],
    [labels.orderType, String(intent.order_type || record.order_type || "market").toUpperCase()],
    [labels.size, size || "—"],
    [labels.notional, estimatedNotional == null ? "—" : `$${displayNumber(estimatedNotional, 2)}`],
  ];
  if (intent.limit_price != null) rows.push([labels.limitPrice, displayNumber(intent.limit_price)]);
  if (intent.stop_price != null) rows.push([labels.stopPrice, displayNumber(intent.stop_price)]);
  rows.push([labels.timeInForce, String(intent.time_in_force || "gtc").toUpperCase()]);
  rows.push([labels.reduceOnly, reduceOnly ? labels.yes : labels.no]);
  rows.push([labels.source, String(intent.source || record.source || "—")]);
  if (intent.trigger_event_id || record.trigger_event_id) {
    rows.push([labels.triggerEvent, String(intent.trigger_event_id || record.trigger_event_id)]);
  }
  if (Object.keys(protection).length) {
    rows.push([labels.protection, JSON.stringify(protection)]);
  }
  if (intent.confidence != null) rows.push([labels.confidence, displayNumber(intent.confidence, 2)]);
  return (
    <div className="min-w-0 space-y-3 text-left">
      <div className="grid grid-cols-2 gap-x-5 gap-y-2 rounded-lg border border-brand-500/20 bg-black/20 p-3 text-xs">
        {rows.map(([label, value]) => (
          <div key={label} className="min-w-0">
            <div className="text-[10px] uppercase tracking-[0.08em] text-ink-500">{label}</div>
            <div className="mt-0.5 break-words font-mono text-ink-100">{value}</div>
          </div>
        ))}
      </div>
      {intent.reasoning ? (
        <div className="rounded-md border border-white/10 bg-white/[0.03] p-2 text-xs text-ink-200">
          <div className="mb-1 text-[10px] uppercase tracking-[0.08em] text-ink-500">{labels.reasoning}</div>
          <div className="whitespace-pre-wrap break-words">{String(intent.reasoning)}</div>
        </div>
      ) : null}
      {reasons.length ? (
        <div className="text-[11px] leading-relaxed text-warn">
          {labels.risk}: {reasons.join(" · ")}
        </div>
      ) : null}
    </div>
  );
}

function WalletSwapApprovalDetails({
  record,
  request,
  quote,
  risk,
  labels,
}: {
  record: Record<string, unknown>;
  request: Record<string, unknown>;
  quote: Record<string, unknown>;
  risk: Record<string, unknown>;
  labels: Record<string, string>;
}) {
  const tokenIn = String(request.token_in || quote.token_in || "—");
  const tokenOut = String(request.token_out || quote.token_out || "—");
  const amountIn = request.amount_in ?? quote.amount_in;
  const expectedOut = quote.expected_out;
  const minOut = quote.min_out;
  const reasons = asArray(risk.reasons ?? record.risk_reasons).map(String);
  const rows = [
    [labels.mode, String(record.execution_mode || "live").toUpperCase()],
    [labels.provider, String(request.provider || quote.provider || record.provider || "—")],
    [labels.chain, String(request.chain || quote.chain || record.chain || "—")],
    [labels.tokenIn, tokenIn],
    [labels.tokenOut, tokenOut],
    [
      labels.amountIn,
      amountIn == null ? "—" : `${displayNumber(amountIn)} ${tokenIn}`,
    ],
    [
      labels.expectedOut,
      expectedOut == null ? "—" : `${displayNumber(expectedOut)} ${tokenOut}`,
    ],
    [
      labels.minOut,
      minOut == null ? "—" : `${displayNumber(minOut)} ${tokenOut}`,
    ],
    [
      labels.slippage,
      request.slippage_bps == null
        ? "—"
        : `${displayNumber(request.slippage_bps, 0)} bps`,
    ],
    [
      labels.priceImpact,
      quote.price_impact_bps == null
        ? "—"
        : `${displayNumber(quote.price_impact_bps, 0)} bps`,
    ],
    [
      labels.gasCost,
      quote.gas_cost_usd == null
        ? "—"
        : `$${displayNumber(quote.gas_cost_usd, 4)}`,
    ],
  ];
  if (request.receiver) rows.push([labels.receiver, String(request.receiver)]);
  const actor = record.approval_actor_id || record.actor_id;
  if (actor) rows.push([labels.actor, String(actor)]);
  if (record.source) rows.push([labels.source, String(record.source)]);
  return (
    <div className="min-w-0 space-y-3 text-left">
      <div className="grid grid-cols-2 gap-x-5 gap-y-2 rounded-lg border border-danger/25 bg-black/20 p-3 text-xs">
        {rows.map(([label, value]) => (
          <div key={label} className="min-w-0">
            <div className="text-[10px] uppercase tracking-[0.08em] text-ink-500">{label}</div>
            <div className="mt-0.5 break-words font-mono text-ink-100">{value}</div>
          </div>
        ))}
      </div>
      {reasons.length ? (
        <div className="text-[11px] leading-relaxed text-warn">
          {labels.risk}: {reasons.join(" · ")}
        </div>
      ) : null}
    </div>
  );
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
  const t = useTranslations("approvals");
  const tCommon = useTranslations("common");
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
    String(event.reason || t("needsPermission"));
  const items = permissionItems(event, card);
  const trade = tradeApprovalData(event, card);
  const walletSwap = walletSwapApprovalData(event, card);
  const tradeLabels = {
    mode: t("mode"),
    account: t("account"),
    strategy: t("strategy"),
    market: t("market"),
    side: t("side"),
    orderType: t("orderType"),
    size: t("size"),
    notional: t("notional"),
    limitPrice: t("limitPrice"),
    stopPrice: t("stopPrice"),
    timeInForce: t("timeInForce"),
    reduceOnly: t("reduceOnly"),
    yes: t("yes"),
    no: t("no"),
    source: t("source"),
    triggerEvent: t("triggerEvent"),
    protection: t("protection"),
    confidence: t("confidence"),
    reasoning: t("reasoning"),
    risk: t("risk"),
  };
  const walletLabels = {
    mode: t("mode"),
    provider: t("provider"),
    chain: t("chain"),
    tokenIn: t("tokenIn"),
    tokenOut: t("tokenOut"),
    amountIn: t("amountIn"),
    expectedOut: t("expectedOut"),
    minOut: t("minOut"),
    slippage: t("slippage"),
    priceImpact: t("priceImpact"),
    gasCost: t("gasCost"),
    receiver: t("receiver"),
    actor: t("actor"),
    source: t("source"),
    risk: t("risk"),
  };
  const financialDetails = walletSwap.isWalletSwap ? (
    <WalletSwapApprovalDetails
      record={walletSwap.record}
      request={walletSwap.request}
      quote={walletSwap.quote}
      risk={walletSwap.risk}
      labels={walletLabels}
    />
  ) : trade.isTrade ? (
    <TradeApprovalDetails
      record={trade.record}
      intent={trade.intent}
      risk={trade.risk}
      labels={tradeLabels}
    />
  ) : null;
  const isFinancialApproval = trade.isTrade || walletSwap.isWalletSwap;
  const financialDetailsTitle = walletSwap.isWalletSwap
    ? t("walletSwapDetailsTitle")
    : t("tradeDetailsTitle");
  const financialApprovalTitle = walletSwap.isWalletSwap
    ? t("walletSwapApprovalTitle")
    : t("tradeApprovalTitle");
  const financialConfirmLabel = walletSwap.isWalletSwap
    ? t("confirmWalletSwap")
    : t("confirmTrade");
  const financialTone =
    walletSwap.isWalletSwap ||
    ["canary", "live"].includes(
      String(trade.record.execution_mode || "").toLowerCase(),
    )
      ? "danger"
      : "warning";
  const isBatch =
    items.length > 1 ||
    Boolean(prompt?.metadata?.tool_batch) ||
    String(asRecord(card?.record ?? event.record).kind || "") === "tool_permission_batch";
  const buttons = resolved ? [] : prompt?.buttons ?? [];
  const statusTone =
    state === "approved"
      ? "border-brand-500/40 bg-brand-500/[0.08] text-brand-200"
      : state === "rejected"
      ? "border-danger/40 bg-danger/[0.08] text-rose-300"
      : "border-warn/40 bg-warn/[0.07] text-warn";
  async function runApprovalAction(callbackData: string) {
    const lower = callbackData.toLowerCase();
    if (lower.startsWith("details:") && isFinancialApproval && financialDetails) {
      await alertDialog({
        title: financialDetailsTitle,
        message: financialDetails,
        okLabel: tCommon("close"),
        tone: "brand",
      });
      return;
    }
    if (lower.startsWith("approve:") && isFinancialApproval && financialDetails) {
      const confirmed = await confirmDialog({
        title: financialApprovalTitle,
        message: financialDetails,
        okLabel: financialConfirmLabel,
        cancelLabel: tCommon("cancel"),
        tone: financialTone,
      });
      if (!confirmed) return;
    }
    onAction?.(callbackData);
  }
  return (
    <div className={`rounded-lg border px-3 py-2.5 space-y-2 ${statusTone}`}>
      <div className="flex items-center justify-between gap-2">
        <div className="text-[12px] font-medium">
          {state === "approved"
            ? t("approved")
            : state === "rejected"
            ? t("rejected")
            : t("request")}
        </div>
        <span className="font-mono text-[10px] text-ink-400">
          {approvalId.slice(0, 18)}
        </span>
      </div>
      {financialDetails ? (
        financialDetails
      ) : items.length ? (
        <div className="space-y-2">
          <div className="text-xs leading-relaxed text-ink-100">
            {isBatch
              ? t("batchApproval", { count: items.length })
              : t("needsPermission")}
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
                  className="rounded-md border border-brand-500/20 bg-black/15 px-2 py-1.5"
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
                ? "border-danger/50 bg-danger/10 text-rose-300 hover:bg-danger/20"
                : style === "primary"
                ? "border-accent-400/50 bg-accent-400/10 text-accent-400 hover:bg-accent-400/20"
                : "border-brand-500/20 bg-white/[0.04] text-ink-200 hover:bg-white/[0.08]";
            const lower = button.callback_data.toLowerCase();
            const Icon =
              style === "danger" || lower.startsWith("reject:")
                ? ShieldXIcon
                : style === "primary" || lower.startsWith("approve:")
                ? ShieldCheckIcon
                : CheckIcon;
            async function handleAction() {
              await runApprovalAction(button.callback_data);
            }
            return (
              <button
                key={button.callback_data}
                onClick={() => void handleAction()}
                disabled={busy}
                className={`inline-flex items-center gap-1.5 rounded-md border px-2.5 py-1 text-xs transition-colors disabled:opacity-50 disabled:cursor-wait ${cls}`}
              >
                <Icon size={14} />
                <span>{busy ? tCommon("working") : button.label}</span>
              </button>
            );
          })}
          {buttons.length === 0 ? (
            <>
              <button
                onClick={() => void runApprovalAction(`approve:${approvalId}`)}
                disabled={busy}
                className="inline-flex items-center gap-1.5 rounded-md border border-accent-400/50 bg-accent-400/10 px-2.5 py-1 text-xs text-accent-400 hover:bg-accent-400/20 transition-colors disabled:opacity-50 disabled:cursor-wait"
              >
                <ShieldCheckIcon size={14} />
                <span>{busy ? tCommon("working") : tCommon("approve")}</span>
              </button>
              <button
                onClick={() => void runApprovalAction(`reject:${approvalId}`)}
                disabled={busy}
                className="inline-flex items-center gap-1.5 rounded-md border border-danger/50 bg-danger/10 px-2.5 py-1 text-xs text-rose-300 hover:bg-danger/20 transition-colors disabled:opacity-50 disabled:cursor-wait"
              >
                <ShieldXIcon size={14} />
                <span>{busy ? tCommon("working") : tCommon("reject")}</span>
              </button>
            </>
          ) : null}
        </div>
      ) : null}
    </div>
  );
}
