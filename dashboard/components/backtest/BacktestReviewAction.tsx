"use client";

import { copy as i18nCopy } from "../../lib/i18n";

import Link from "next/link";
import { useLocale } from "next-intl";
import { backtestReviewDraft, type BacktestReviewTarget } from "../../lib/backtestReview";
import { setWorkspaceComposeDraft } from "../../lib/workspaceComposeDraft";
import { toast } from "../../lib/dialogs";

export function BacktestReviewAction({ target, className }: { target: BacktestReviewTarget; className?: string }) {
  const zh = useLocale().startsWith("zh");
  if (!target.strategyId || !target.ts) return null;
  return <Link className={className || "btn btn-ghost"}
    href={`/chat?draft=${encodeURIComponent(`review:${target.strategyId}:${target.ts}`)}`}
    data-testid="backtest-research-review"
    title={i18nCopy(zh, "copy.components_backtest_BacktestReviewAction.001")}
    onClick={event => {
      if (!setWorkspaceComposeDraft({ text: backtestReviewDraft(target, zh), attachments: [], autoSend: false })) {
        event.preventDefault();
        toast({ tone: "warn", message: i18nCopy(zh, "copy.components_backtest_BacktestReviewAction.002") });
      }
    }}>{i18nCopy(zh, "copy.components_backtest_BacktestReviewAction.003")}</Link>;
}
