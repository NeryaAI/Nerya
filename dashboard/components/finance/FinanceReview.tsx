"use client";

import { createContext, useContext } from "react";
import { useRouter } from "next/navigation";
import { useLocale } from "next-intl";
import type { PortfolioPosition } from "../../lib/api";
import { setComposeDraftPayload, takeComposeDraftPayload } from "../../lib/composeDraft";
import { financeMoney, financeNumber, positionSide } from "../../lib/financeDisplay";
import { toast } from "../../lib/dialogs";
import { MessagesIcon } from "../icons";

export const FinanceDraftContext = createContext<{ append: (text: string) => void; disabled: boolean } | null>(null);
export function appendReviewDraft(before: string, next: string) {
  return before.trimEnd().endsWith(next) ? before : before.trim() ? `${before.trimEnd()}\n\n${next}` : next;
}

/** A deliberate handoff into a draft, never a model call or trading command. */
export function FinanceReview({ position, mode }: { position: PortfolioPosition; mode?: string }) {
  const locale = useLocale(), zh = locale.startsWith("zh");
  const destination = useContext(FinanceDraftContext), router = useRouter();
  const label = zh ? "复盘此仓位" : "Review with agent";
  function review() {
    const p = position;
    const side = positionSide(p);
    const sideLabel = zh ? side === "long" ? "多头" : side === "short" ? "空头" : "方向未提供" : side;
    const modeLabel = zh ? mode === "paper" ? "模拟" : mode === "live" ? "实盘" : "模式未提供" : mode || "Not provided";
    const text = [zh ? "请复盘以下页面记录中的仓位，先核验数据时间与当前状态，再解释风险和需要人工确认的事项。只分析，不下单、不平仓、不调整止盈止损。" : "Review this recorded position. Verify its timestamp and current state before explaining risks and items requiring human review. Analysis only: do not place or close orders or change TP/SL.",
      `${zh ? "账户" : "Account"}: ${p.account_id || "—"}; ${zh ? "模式" : "Mode"}: ${modeLabel}`,
      `${zh ? "市场 / 方向" : "Market / side"}: ${p.market || "—"} / ${sideLabel}`,
      `${zh ? "报告数量" : "Reported size"}: ${financeNumber(p.size_base ?? p.size, locale)}`,
      `${zh ? "开仓 / 标记价" : "Entry / mark"}: ${financeNumber(p.avg_entry_price ?? p.avg_price, locale)} / ${financeNumber(p.mark_price, locale)}`,
      `${zh ? "未实现盈亏 · USD" : "Unrealized P&L · USD"}: ${financeMoney(p.unrealized_pnl_usd, locale, true)}`,
      zh ? "以上为页面快照，不是实时下单依据；缺失信息请明确说明，不推算杠杆或强平价。" : "These are page snapshots, not live execution quotes. Identify missing data; do not infer leverage or liquidation levels.",
    ].join("\n");
    if (destination) { if (!destination.disabled) destination.append(text); return; }
    const previous = takeComposeDraftPayload();
    setComposeDraftPayload({ text: appendReviewDraft(previous?.text || "", text), attachments: previous?.attachments || [], autoSend: false });
    toast({ tone: "ok", message: zh ? "已准备复盘草稿，确认后再发送。" : "Review draft prepared. Read it before sending." });
    router.push("/chat");
  }
  return <button type="button" disabled={destination?.disabled} onClick={review} data-testid="review-position"
    title={zh ? "仅填入可编辑草稿，不会自动发送或交易" : "Fill an editable draft without sending or trading"}
    className="inline-flex min-h-8 items-center gap-2 rounded-md border border-[color:var(--line)] px-3 text-xs text-[color:var(--text-base)] hover:bg-[color:var(--panel-bg)] disabled:opacity-40"><MessagesIcon size={14} />{label}</button>;
}
