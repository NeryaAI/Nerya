"use client";

import { useLocale, useTranslations } from "next-intl";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import { callApi } from "../../lib/clientApi";
import type { EvolutionProposalDetail } from "../../lib/evolutionTypes";
import { usePolledResource } from "../../lib/usePolledResource";
import { StrategyProposalApprovalCard, strategyProposalFromToolResult } from "../strategies/StrategyProposalApprovalCard";
import { ResourceNotice } from "./ResourceNotice";
import { fieldLabel } from "../../lib/agentConversation";

/** Reuses the existing revision/approval/promotion component. Opening a record
 * never approves, applies, validates, starts or reconstructs a candidate.
 */
export function WorkflowProposalDetail({ proposalId, strategyId }: { proposalId: string; strategyId: string }) {
  const t = useTranslations("workflowUpgrade");
  const locale = useLocale();
  const number = (value: unknown) => typeof value === "number" && Number.isFinite(value) ? new Intl.NumberFormat(locale, { maximumFractionDigits: 6 }).format(value) : "—";
  const resource = usePolledResource<EvolutionProposalDetail>(`workflow-proposal:${strategyId}:${proposalId}`, async signal => {
    const response = await callApi<EvolutionProposalDetail>(`/evolution/proposals/${encodeURIComponent(proposalId)}`, { method: "POST", body: { proposal_id: proposalId }, signal });
    if (response.error || response.id !== proposalId) throw new Error(response.error || "Proposal identity mismatch");
    const normalized = strategyProposalFromToolResult(response);
    if (!normalized || normalized.strategy_id !== strategyId) throw new Error("Proposal does not belong to this strategy");
    return response;
  }, { intervalMs: 30_000 });
  const data = resource.data;
  const markdown = (text: string) => <div className="prose prose-sm mt-3 max-w-none dark:prose-invert"><ReactMarkdown remarkPlugins={[remarkGfm]}>{text}</ReactMarkdown></div>;
  return <section className="mt-4 space-y-4" data-testid="workflow-proposal-detail">
    <ResourceNotice error={resource.error} updatedAt={resource.updatedAt} onRetry={resource.refresh} />
    {!data && resource.loading && <p role="status" className="text-sm">{t("loading")}</p>}
    {data && <>
      <StrategyProposalApprovalCard proposal={data} onApproved={() => resource.refresh()} onDeleted={() => resource.refresh()} />
      {data.rationale_md && markdown(data.rationale_md)}
      {data.test_plan_md && <details className="text-sm"><summary>{t("validationPlan")}</summary>{markdown(data.test_plan_md)}</details>}
      {data.diff_patch && <details className="text-sm"><summary>{t("proposalDetails")}</summary><pre className="mt-3 max-h-96 overflow-auto whitespace-pre-wrap break-words rounded-md border border-[color:var(--line)] p-3 text-xs">{data.diff_patch}</pre></details>}
      {data.backtest_comparison && <section className="text-sm" data-testid="review-backtest-comparison"><h4 className="font-medium">{t("recordedComparison")}</h4>
        {data.backtest_comparison.summary && <p className="mt-2 leading-7 text-[color:var(--text-muted)]">{data.backtest_comparison.summary}</p>}
        {data.backtest_comparison.status !== "complete" && <p className="mt-2 text-xs text-warn">{t("incompleteComparison")}</p>}
        {!!data.backtest_comparison.metrics_delta?.length && <div className="mt-3 overflow-auto"><table className="w-full text-left text-xs"><thead><tr><th className="py-2 pr-3">{t("metric")}</th><th className="pr-3">{t("before")}</th><th className="pr-3">{t("after")}</th><th>{t("difference")}</th></tr></thead><tbody>{data.backtest_comparison.metrics_delta.map(metric => <tr key={metric.key} className="border-t border-[color:var(--line)]"><td className="py-3 pr-3">{fieldLabel(metric.key, locale.startsWith("zh"))}</td><td className="pr-3 tabular-nums">{number(metric.before)}</td><td className="pr-3 tabular-nums">{number(metric.after)}</td><td className="tabular-nums">{number(metric.delta)}</td></tr>)}</tbody></table></div>}
        <p className="mt-2 text-xs leading-6 text-[color:var(--text-muted)]">{t("recordedUnits")}</p>
        <details className="mt-2 text-xs"><summary>{t("diagnostics")}</summary><pre className="mt-2 max-h-72 overflow-auto whitespace-pre-wrap">{JSON.stringify(data.backtest_comparison, null, 2)}</pre></details>
      </section>}
      {data.post_apply_monitor && <section className="text-sm" data-testid="review-post-apply"><h4 className="font-medium">{t("postApplyObservation")}</h4>
        {data.post_apply_monitor.summary && <p className="mt-2 leading-7">{data.post_apply_monitor.summary}</p>}
        {(data.post_apply_monitor.observations?.length ? data.post_apply_monitor.observations : data.post_apply_monitor.latest ? [data.post_apply_monitor.latest] : []).map((observation, index) => <article key={observation.id || index} className="mt-3 border-l-2 border-[color:var(--line)] pl-3"><p className="text-xs text-[color:var(--text-muted)]">{observation.observed_at || t("notSpecified")} · {observation.status || t("notSpecified")}</p>{observation.summary && <p className="mt-1 leading-7">{observation.summary}</p>}</article>)}
        <details className="mt-2 text-xs"><summary>{t("diagnostics")}</summary><pre className="mt-2 max-h-72 overflow-auto whitespace-pre-wrap">{JSON.stringify(data.post_apply_monitor, null, 2)}</pre></details>
      </section>}
    </>}
  </section>;
}
