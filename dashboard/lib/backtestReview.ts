import { copy as i18nCopy } from "./i18n";
/** A review targets one immutable report, never the latest strategy version. */
export type BacktestReviewTarget = {
  strategyId: string;
  ts: string;
  proposalId?: string | null;
  sourceRevision?: string;
};

export function backtestReviewDraft(target: BacktestReviewTarget, zh: boolean): string {
  const identity = JSON.stringify({
    strategy_id: target.strategyId, backtest_ts: target.ts,
    ...(target.proposalId ? { proposal_id: target.proposalId } : {}),
    ...(target.sourceRevision ? { source_revision: target.sourceRevision } : {}),
  });
  return i18nCopy(zh, "copy.lib_backtestReview.001", { value0: identity });
}
