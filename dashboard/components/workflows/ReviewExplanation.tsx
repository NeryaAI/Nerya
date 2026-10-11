"use client";

import Link from "next/link";
import { useState } from "react";
import { useLocale, useTranslations } from "next-intl";
import { fieldLabel } from "../../lib/agentConversation";
import { reviewExplanation } from "../../lib/reviewExplanation";
import styles from "./WorkflowReplay.module.css";
import { WorkflowProposalDetail } from "./WorkflowProposalDetail";
import { asObject } from "../../lib/workflowPresentation";

export function ReviewExplanation({ record, output, status }: { record: Record<string, unknown>; output?: unknown; status?: string }) {
  const t = useTranslations("workflowExperience");
  const w = useTranslations("workflowUpgrade");
  const [openProposal, setOpenProposal] = useState(false);
  const zh = useLocale().startsWith("zh");
  const review = reviewExplanation(record, output);
  const proposalId = typeof record.proposal_id === "string" ? record.proposal_id : "";
  const strategyId = typeof record.strategy_id === "string" ? record.strategy_id : "";
  const scope = asObject(asObject(record.snapshot).evidence_scope);
  return <section className={styles.reviewExplanation} aria-label={t("reviewConclusion")} data-testid="review-explanation">
    <header><h3>{t("reviewConclusion")}</h3><p className={styles.reviewLead}>{review.summary || t("missingReviewSummary")}</p></header>
    {review.partial && <p className={styles.note}>{t("partialHistory")}</p>}
    <p className={styles.note}>{t(status === "applied" ? "changeApplied" : proposalId ? "proposalRecorded" : "reviewOnly")}{proposalId && <> {strategyId ? <button type="button" className="underline underline-offset-4" aria-expanded={openProposal} onClick={() => setOpenProposal(!openProposal)}>{w("inspectProposal")}</button> : <Link className="underline underline-offset-4" href={`/self-evolution?tab=proposals&proposal_id=${encodeURIComponent(proposalId)}`}>{t("viewProposal")}</Link>}</>}</p>
    {openProposal && proposalId && strategyId && <WorkflowProposalDetail key={proposalId} proposalId={proposalId} strategyId={strategyId} />}
    <details className="text-sm" data-testid="review-evidence-coverage"><summary>{w("actualCoverage")}</summary>
      {Object.keys(scope).length ? <div className="mt-3 space-y-2 text-xs leading-6">
        <p>{w("requestedScope")} · {String(scope.window_started_at || "—")} → {String(scope.window_ended_at || "—")}</p>
        <p>{w("sourceRevision")} · <code className="break-all">{String(scope.package_hash || "—")}</code></p>
        <p>{w("selectedRecords", { count: Array.isArray(scope.selected_run_ids) ? scope.selected_run_ids.length : 0 })} · {w("selectedAgentRecords", { count: Array.isArray(scope.selected_agent_task_ids) ? scope.selected_agent_task_ids.length : 0 })}</p>
        {Number(asObject(scope.excluded_run_counts).lookback_limit) > 0 && <p className="text-warn">{w("truncatedEvidence", { count: Number(asObject(scope.excluded_run_counts).lookback_limit) })}</p>}
        <p className="text-[color:var(--text-muted)]">{w("coverageNotCompleteness")}</p>
        <details><summary>{w("diagnostics")}</summary><pre className="max-h-64 overflow-auto whitespace-pre-wrap">{JSON.stringify(scope, null, 2)}</pre></details>
      </div> : <p className="mt-2 text-xs text-[color:var(--text-muted)]">{w("missingCoverage")}</p>}
    </details>
    {review.error && <p role="alert" className="text-danger">{review.error}</p>}
    {review.rationale && <section><h4>{t("rationale")}</h4><p>{review.rationale}</p></section>}
    <section><h4>{t("scope")}</h4>{review.scope.length > 0 && <ul className={styles.proseList}>{review.scope.map((value, index) => <li key={index}>{value}</li>)}</ul>}
      {review.changes.length ? <ol className={styles.changeList}>{review.changes.map((change, index) => <li key={index}>
        <div className={styles.replayHeader}><h4>{change.summary || change.target || t("targetNotRecorded")}</h4><span className={styles.note}>{t(change.rejected ? "changeRejected" : change.advisory ? "changeAdvisory" : "changeProposed")}</span></div>
        {change.summary && change.target && <small className={styles.note}>{change.target}</small>}
        <dl className={styles.facts}>
          <div><dt>{t("rationale")}</dt><dd>{change.rationale || t("reasonNotRecorded")}</dd></div>
          {(change.before || change.after) && <><div><dt>{t("before")}</dt><dd>{change.before || t("notRecorded")}</dd></div><div><dt>{t("after")}</dt><dd>{change.after || t("notRecorded")}</dd></div></>}
          {change.scope.length > 0 && <div><dt>{t("scope")}</dt><dd>{change.scope.join(" · ")}</dd></div>}
          {change.rejected && <div><dt>{t("changeRejected")}</dt><dd>{change.rejection || t("notRecorded")}</dd></div>}
        </dl>
      </li>)}</ol> : <p className={styles.note}>{t(review.changesRecorded ? "noProposedChanges" : "scopeNotRecorded")}</p>}
    </section>
    {review.expected.length > 0 && <section><h4>{t("expectedEffect")}</h4><p className={styles.note}>{t("expectedOnly")}</p><dl className={styles.facts}>{review.expected.map(([key, value]) => <div key={key}><dt>{t.has("effectKinds." + key) ? t("effectKinds." + key) : fieldLabel(key, zh)}</dt><dd>{value}</dd></div>)}</dl></section>}
    {review.evidence.length > 0 && <section><h4>{t("reviewEvidence")}</h4><ul className={styles.proseList}>{review.evidence.map((row, index) => <li key={index}>{row.finding}{row.source && <small className={styles.note}> · {row.source}</small>}</li>)}</ul></section>}
    <section><h4>{t("validation")}</h4><p>{review.validationStatus ? `${t("recordedValidation")}: ${review.validationStatus}` : t("validationNotRecorded")}</p>{review.validation.length > 0 && <p className={styles.note}>{t("validationPlan")}: {review.validation.map((value) => t.has(`validationKinds.${value}`) ? t(`validationKinds.${value}`) : value).join(" · ")}</p>}</section>
    {review.risks.length > 0 && <section><h4>{t("risks")}</h4><ul className={styles.proseList}>{review.risks.map((value, index) => <li key={index}>{value}</li>)}</ul></section>}
  </section>;
}
