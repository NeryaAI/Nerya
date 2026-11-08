"use client";

import { copy as i18nCopy } from "../../lib/i18n";

import dynamic from "next/dynamic";
import Link from "next/link";
import { useContext, useId, useMemo, useState } from "react";
import { useLocale } from "next-intl";
import type { AssistantMessage } from "../../lib/chat";
import { collectBacktestResults, type BacktestResultRef } from "../../lib/backtestResults";
import { financeNumber } from "../../lib/financeDisplay";
import { Icon } from "../icons";
import { JsonDetails } from "../JsonView";
import { StrategyDetailContext } from "./StrategyDetailContext";
import { strategyDetailId, type StrategyDetailTarget } from "../../lib/strategyDetail";
import styles from "./BacktestReplyCards.module.css";
import { BacktestExecutionEvidence } from "../backtest/BacktestExecutionEvidence";
import { BacktestCoverage } from "../backtest/BacktestCoverage";
import { BacktestBiasChecks } from "../backtest/BacktestBiasChecks";
import { BacktestReviewAction } from "../backtest/BacktestReviewAction";
import { factorSourceUrl } from "../../lib/factorLibrary";
import { backtestEvidenceKind, customResearchDiagnostics, lpCounterfactualLabelPrefix, rateSimulationLabelPrefix } from "../../lib/backtestEvidence";
import { toolFailureSummary } from "../../lib/toolFailureSummary";
import { backtestCoverage } from "../../lib/backtestCoverage";

const BacktestChart = dynamic(() => import("../backtest/BacktestChart").then(module => module.BacktestChart), { ssr: false });
const evaluationNotes: Record<string, string> = {
  no_trades: "copy.components_chat_BacktestReplyCards.001",
  "risk_breach:max_drawdown": "copy.components_chat_BacktestReplyCards.002",
  negative_net_return: "copy.components_chat_BacktestReplyCards.003",
  benchmark_capture_below_threshold: "copy.components_chat_BacktestReplyCards.004",
  strategy_returned_errors: "copy.components_chat_BacktestReplyCards.005",
  sdk_order_errors: "copy.components_chat_BacktestReplyCards.006",
  market_data_gaps: "copy.components_chat_BacktestReplyCards.007",
};
const rateCoverageKeys: Record<string, string> = {
  observation_coverage_gap: "rateReplayCoverageGap",
  observation_intervals_covered: "rateReplayCoverageIntervals",
  modeled_intervals_covered: "rateReplayCoverageModeled",
};
const legacyRateCoverage: Record<string, string> = {
  "Historical observation gaps remain; partial simulation only.": "observation_coverage_gap",
  "Observation intervals covered.": "observation_intervals_covered",
};

const stableReason = /^[a-z][a-z0-9_:-]{2,127}$/i;
function failureSummaryInput(result: BacktestResultRef): Record<string, unknown> {
  const codes: string[] = [];
  const add = (value: unknown) => {
    if (typeof value === "string" && stableReason.test(value.trim())) codes.push(value.trim());
  };
  const visit = (value: unknown, depth = 0) => {
    if (depth > 4 || !value || typeof value !== "object") return;
    if (Array.isArray(value)) { value.slice(0, 20).forEach(item => visit(item, depth + 1)); return; }
    const row = value as Record<string, unknown>;
    add(row.reason); add(row.code); add(row.error_code);
    visit(row.recovery, depth + 1); visit(row.diagnostics, depth + 1); visit(row.blockers, depth + 1);
  };
  add(result.failureReason);
  visit(result.failureDiagnostics);
  const prefix = result.message.trim().match(/^([a-z][a-z0-9_:-]{2,127})\b/i)?.[1];
  add(prefix);
  return { ok: false, reason: [...new Set(codes)], diagnostics: { blockers: [...new Set(codes)].map(code => ({ code })) } };
}

/** A persisted tool receipt is a deliverable even before final prose arrives. */
export function BacktestReplyCards({ message }: { message: AssistantMessage }) {
  const results = useMemo(() => collectBacktestResults({ messages: [message] }), [message]);
  if (!results.length) return null;
  return <div className={styles.results} data-testid="backtest-reply-cards" data-backtest-turn={message.id}>
    {results.map(result => <BacktestResultCard key={result.id} result={result} />)}
  </div>;
}

export function BacktestResultCard({ result }: { result: BacktestResultRef }) {
  const locale = useLocale(), zh = locale.startsWith("zh"), id = useId();
  const [open, setOpen] = useState(false);
  const details = useContext(StrategyDetailContext);
  const reportTarget: StrategyDetailTarget = { kind: "backtest", strategyId: result.strategyId, proposalId: result.proposalId, ts: result.ts, title: result.title };
  const reportId = strategyDetailId(reportTarget);
  const expanded = details ? details.active === reportId : open;
  const openReport = () => details ? details.open(reportTarget) : setOpen(value => !value);
  const openStrategy = () => details?.open({ kind: "strategy", strategyId: result.strategyId, proposalId: result.proposalId, title: result.title });
  const complete = result.status === "completed";
  const evidenceKind = backtestEvidenceKind({ ...result.metrics, decision_replay: result.decisionReplay, evaluation_mode: result.evaluationMode, execution_mode: result.executionMode, performance_evidence: result.performanceEvidence, replay: result.replay, provenance: result.provenance });
  const observation = evidenceKind === "observation", simulated = evidenceKind === "script_simulated";
  const retrospective = evidenceKind === "retrospective_replay";
  const poolPrice = evidenceKind === "pool_price_simulation";
  const priceSimulation = result.provenance.price_simulation as Record<string, unknown> | undefined;
  const quoteUnits = String(result.metrics.accounting_currency || priceSimulation?.quote_units || "—");
  const lpCounterfactual = evidenceKind === "lp_counterfactual";
  const lpPrefix = lpCounterfactualLabelPrefix({ replay: result.replay });
  const custom = evidenceKind === "custom_research", rateSimulation = evidenceKind === "rate_simulation";
  const ratePrefix = rateSimulationLabelPrefix({ replay: result.replay });
  const nonPerformance = observation || simulated || retrospective || poolPrice || custom || rateSimulation || lpCounterfactual;
  const customDiagnostics = custom ? customResearchDiagnostics(result.metrics) : {};
  const rateCoverageKey = rateCoverageKeys[typeof result.metrics.coverage_reason === "string" ? result.metrics.coverage_reason : legacyRateCoverage[result.message]];
  const coverageMeta = { ...result.metrics, ...result.coverage, start: result.start, end: result.end, provenance: result.provenance };
  const coverage = backtestCoverage(coverageMeta);
  const rateCoverage = rateSimulation ? coverage : null;
  const coverageNumber = (value: number | null) => value === null ? "—" : financeNumber(value, locale, 2);
  const agentNotRun = !poolPrice && !custom && !rateSimulation && result.executionMode === "agent" && result.replay.agent_execution === "not_run";
  const dataLabel = ({ historical: i18nCopy(zh, "copy.components_chat_BacktestReplyCards.008"), sample: i18nCopy(zh, "copy.components_chat_BacktestReplyCards.009"),
    historical_observations: i18nCopy(zh, "copy.components_backtest_BacktestChart.customResearchData"),
    unverified: i18nCopy(zh, "copy.components_chat_BacktestReplyCards.010") } as Record<string,string>)[result.dataKind] || (i18nCopy(zh, "copy.components_chat_BacktestReplyCards.011"));
  const date = (raw: string) => Number.isFinite(Date.parse(raw)) ? new Date(raw).toLocaleString(locale, { year:"numeric",month:"short",day:"numeric",hour:"2-digit",minute:"2-digit",timeZone:"UTC" }) : "";
  const values = rateSimulation ? { ...result.metrics, ...result.replay } : observation ? result.replay : result.metrics;
  const metrics = rateSimulation ? [["initial_capital_usd", i18nCopy(zh, "copy.components_backtest_BacktestChart.rateReplayInitial")], ["final_equity_usd", i18nCopy(zh, "copy.components_backtest_BacktestChart.rateReplayFinal")],
    ["decisions", i18nCopy(zh, "copy.components_backtest_BacktestChart.rateReplayDecisions")], ["simulated_trades", i18nCopy(zh, "copy.components_backtest_BacktestChart.rateReplayTrades")]]
    : observation ? [["decisions", i18nCopy(zh, "copy.components_chat_BacktestReplyCards.012")], ["dispatches", i18nCopy(zh, "copy.components_chat_BacktestReplyCards.013")],
    ["skipped", i18nCopy(zh, "copy.components_chat_BacktestReplyCards.014")], ["errors", i18nCopy(zh, "copy.components_chat_BacktestReplyCards.015")]]
    : [["total_return_pct", i18nCopy(zh, "copy.components_chat_BacktestReplyCards.016")], ["max_drawdown_pct", i18nCopy(zh, "copy.components_chat_BacktestReplyCards.017")],
      ["sharpe_ratio", i18nCopy(zh, "copy.components_chat_BacktestReplyCards.018")], ["total_trades", i18nCopy(zh, "copy.components_chat_BacktestReplyCards.019")]];
  const format = (key: string, raw: unknown): string => typeof raw === "string" && (rateSimulation || ["sharpe_ratio","total_trades"].includes(key)) && raw.trim() && Number.isFinite(Number(raw)) ? format(key,Number(raw)) : typeof raw === "number" && Number.isFinite(raw)
    ? `${financeNumber(raw, locale, observation || ["total_trades", "decisions", "simulated_trades"].includes(key) ? 0 : 2)}${key.endsWith("_pct") ? "%" : ""}`
    : rateSimulation ? "—"
    : typeof raw === "string" && raw.trim() && !/^(nan|none|null|undefined|[-+]?inf(?:inity)?|[-+]?∞)$/i.test(raw.trim()) ? raw : "—";
  const verdict = rateSimulation ? i18nCopy(zh, "copy.components_backtest_BacktestChart.rateReplayRecorded") : custom ? i18nCopy(zh, "copy.components_chat_BacktestReplyCards.022") : ({ PASS: nonPerformance ? (i18nCopy(zh, "copy.components_chat_BacktestReplyCards.020")) : (i18nCopy(zh, "copy.components_chat_BacktestReplyCards.021")), WARN: i18nCopy(zh, "copy.components_chat_BacktestReplyCards.022"), FAIL: i18nCopy(zh, "copy.components_chat_BacktestReplyCards.023") } as Record<string,string>)[result.verdict];
  const points = [...new Map(result.equityPreview.map(p => [p.time,p])).values()].sort((a,b) => a.time-b.time);
  const min = Math.min(...points.map(p=>p.value)), max = Math.max(...points.map(p=>p.value));
  const from = points[0]?.time || 0, span = (points.at(-1)?.time || 0) - from;
  const line = points.length > 1 ? points.map((p,i) => `${i ? "L" : "M"}${(4 + (p.time-from)/(span||1)*552).toFixed(2)},${(max === min ? 32 : 58-(p.value-min)/(max-min)*50).toFixed(2)}`).join(" ") : "";
  const target = result.strategyId ? `/strategies?strategy_id=${encodeURIComponent(result.strategyId)}${result.proposalId ? `&proposal_id=${encodeURIComponent(result.proposalId)}` : ""}` : "";
  return <section className={styles.card} data-testid="backtest-result-card" data-backtest-id={result.id} data-status={result.status}
    data-evaluation={evidenceKind} aria-label={i18nCopy(zh, "copy.components_chat_BacktestReplyCards.024")}>
    <div className={styles.body}>
      <header className={styles.header}>
        <div className={styles.identity}><div className={styles.kind}><Icon name="chart" size={15}/>{poolPrice ? i18nCopy(zh, "copy.components_backtest_BacktestChart.poolPriceTitle") : lpCounterfactual ? i18nCopy(zh, `copy.components_backtest_BacktestChart.${lpPrefix}Title`) : retrospective ? i18nCopy(zh, "copy.components_backtest_BacktestChart.retrospectiveReplayTitle") : rateSimulation ? i18nCopy(zh, `copy.components_backtest_BacktestChart.${ratePrefix}Title`) : custom ? i18nCopy(zh, "copy.components_backtest_BacktestChart.customResearchTitle") : simulated ? i18nCopy(zh, "copy.components_backtest_BacktestChart.scriptReplayTitle") : observation ? (i18nCopy(zh, "copy.components_chat_BacktestReplyCards.025")) : (i18nCopy(zh, "copy.components_chat_BacktestReplyCards.026"))}</div>
          <h3 className={styles.title}>{result.title}</h3></div>
        <div className={styles.headerActions}><span className={`${styles.state} ${!complete || result.verdict === "WARN" ? "text-warn" : result.verdict === "FAIL" ? "text-danger" : "text-[color:var(--text-muted)]"}`}>
          {complete ? verdict || (i18nCopy(zh, "copy.components_chat_BacktestReplyCards.027")) : result.status === "blocked" ? (i18nCopy(zh, "copy.components_chat_BacktestReplyCards.028")) : (i18nCopy(zh, "copy.components_chat_BacktestReplyCards.029"))}
        </span>{complete && <button type="button" className={styles.expand} onClick={openReport} aria-label={i18nCopy(zh, "copy.components_chat_BacktestReplyCards.030")} title={i18nCopy(zh, "copy.components_chat_BacktestReplyCards.031")} aria-expanded={expanded} aria-controls={details ? `task-dock-panel-${reportId}` : id} data-testid="expand-backtest-details"><Icon name="arrowUpRight" size={18}/></button>}</div>
      </header>
      {complete && <p className={styles.period}>{result.start && result.end ? `${date(result.start)} – ${date(result.end)} UTC` : (i18nCopy(zh, "copy.components_chat_BacktestReplyCards.032"))}</p>}
      {complete && (rateCoverage ? <section className={styles.note} data-testid="backtest-coverage" data-coverage-status={rateCoverage.state} data-recorded-start={rateCoverage.start ?? undefined} data-recorded-end={rateCoverage.end ?? undefined}>
        {i18nCopy(zh, "copy.components_backtest_BacktestChart.rateReplayRequestedDays")} {coverageNumber(rateCoverage.requestedDays)} · {i18nCopy(zh, "copy.components_backtest_BacktestChart.rateReplayRecordedDays")} {coverageNumber(rateCoverage.recordedDays)}
        <p>{i18nCopy(zh, `copy.components_backtest_BacktestChart.${rateCoverage.state === "partial" ? "rateReplayCoverageGap" : rateCoverageKey || "rateReplayCoverageUnknown"}`)}</p>
      </section> : <BacktestCoverage compact meta={coverageMeta}/>)}
      {complete && !poolPrice && !simulated && !retrospective && !custom && !lpCounterfactual && <dl className={styles.metrics}>{metrics.map(([key,label]) => <div key={key}><dt>{label}</dt><dd data-metric={key}>{format(key,values[key])}</dd></div>)}</dl>}
      {complete && poolPrice && <p className={styles.note} data-testid="pool-price-units">{i18nCopy(zh, "copy.components_backtest_BacktestChart.poolPriceUnits")}: {quoteUnits}. {i18nCopy(zh, "copy.components_backtest_BacktestChart.poolPriceAccounting")}</p>}
      {complete && rateSimulation && <p className={styles.note} role="status">{i18nCopy(zh, `copy.components_backtest_BacktestChart.${ratePrefix}Warning`)}</p>}
      {complete && lpCounterfactual && <p className={styles.note} role="status">{i18nCopy(zh, `copy.components_backtest_BacktestChart.${lpPrefix}Warning`)}</p>}
      {complete && !observation && !custom && !rateSimulation && !lpCounterfactual && <BacktestExecutionEvidence replay={result.replay}/>}
      {complete && custom && (Object.keys(customDiagnostics).length > 0 || line) && <details className={styles.details}>
        <summary>{i18nCopy(zh, "copy.components_backtest_BacktestChart.customResearchOutputs")}</summary>
        {Object.keys(customDiagnostics).length > 0 && <pre>{JSON.stringify(customDiagnostics, null, 2)}</pre>}
        {line && <figure className={styles.trend}>
          <svg viewBox="0 0 560 64" preserveAspectRatio="none" role="img" aria-label={i18nCopy(zh, "copy.components_backtest_BacktestChart.customResearchCurve")}><path d={line} fill="none" stroke="currentColor" strokeWidth="1.6" vectorEffect="non-scaling-stroke"/></svg>
          <figcaption>{i18nCopy(zh, "copy.components_backtest_BacktestChart.customResearchCurve")}</figcaption>
        </figure>}
      </details>}
      {complete && (!nonPerformance || rateSimulation) && line && <figure className={styles.trend}>
        <svg viewBox="0 0 560 64" preserveAspectRatio="none" role="img" aria-label={i18nCopy(zh, rateSimulation ? "copy.components_backtest_BacktestChart.rateReplayCurve" : "copy.components_chat_BacktestReplyCards.033")}><path d={line} fill="none" stroke="currentColor" strokeWidth="1.6" vectorEffect="non-scaling-stroke"/></svg>
        <figcaption><span>{i18nCopy(zh, rateSimulation ? "copy.components_backtest_BacktestChart.rateReplayCurve" : "copy.components_chat_BacktestReplyCards.034")}</span><span>{financeNumber(points.at(-1)!.value,locale,2)} USD</span></figcaption>
      </figure>}
      {complete && !poolPrice && !rateSimulation && !lpCounterfactual && <BacktestBiasChecks compact meta={{ bias_checks: result.biasChecks, research_checks: result.researchChecks, provenance: result.provenance }}/>}
      {complete ? <>
        {!poolPrice && !custom && !rateSimulation && !lpCounterfactual && result.flags.filter(flag => evaluationNotes[flag]).map(flag => <p key={flag} className={styles.note} data-testid="backtest-evaluation-note">{i18nCopy(zh, evaluationNotes[flag] ?? "")}</p>)}
        <div className={styles.evidence}><span>{dataLabel}</span><span>{poolPrice ? i18nCopy(zh, "copy.components_backtest_BacktestChart.poolPriceTitle") : lpCounterfactual ? i18nCopy(zh, `copy.components_backtest_BacktestChart.${lpPrefix}Title`) : rateSimulation ? i18nCopy(zh, `copy.components_backtest_BacktestChart.${ratePrefix}Accounting`) : custom || result.engine === "freeform" ? (i18nCopy(zh, "copy.components_chat_BacktestReplyCards.035")) : (i18nCopy(zh, "copy.components_chat_BacktestReplyCards.036"))}</span>
          {agentNotRun && <strong className="text-warn">{i18nCopy(zh, "copy.components_chat_BacktestReplyCards.037")}</strong>}</div>
        {rateSimulation ? !rateCoverageKey && (result.message || result.metrics.coverage_reason) && <p className={styles.note}>{i18nCopy(zh, "copy.components_backtest_BacktestChart.rateReplayProducerNotes")}</p> : !lpCounterfactual && result.message && <p className={styles.note} data-testid="backtest-coverage-summary">{i18nCopy(zh, `copy.components_backtest_BacktestCoverage.${custom ? "producerNotes" : coverage.state === "partial" ? "005" : coverage.state === "complete" ? "006" : "007"}`)}</p>}
        {!rateSimulation && !lpCounterfactual && <p className={styles.note}>{poolPrice ? i18nCopy(zh, "copy.components_backtest_BacktestChart.poolPriceWarning") : retrospective ? i18nCopy(zh, "copy.components_backtest_BacktestChart.retrospectiveReplayWarning") : custom ? i18nCopy(zh, "copy.components_backtest_BacktestChart.customResearchWarning") : simulated ? i18nCopy(zh, "copy.components_backtest_BacktestChart.scriptReplayWarning") : observation ? (i18nCopy(zh, "copy.components_chat_BacktestReplyCards.038"))
          : result.dataKind !== "historical" ? (i18nCopy(zh, "copy.components_chat_BacktestReplyCards.039"))
          : (i18nCopy(zh, "copy.components_chat_BacktestReplyCards.040"))}</p>}
        {(lpCounterfactual || poolPrice || rateSimulation) && <JsonDetails className={styles.details} summary={i18nCopy(zh, rateSimulation ? "copy.components_backtest_BacktestChart.rateReplayEvidence" : poolPrice ? "copy.components_backtest_BacktestChart.poolPriceEvidence" : "copy.components_backtest_BacktestChart.lpCounterfactualEvidence")} value={{
          message: result.message, metrics: result.metrics, provenance: result.provenance, replay: result.replay, coverage: result.coverage,
        }}/>}
      </> : <p className={styles.diagnostic} role="status">{toolFailureSummary(failureSummaryInput(result), zh)}</p>}
      <div className={styles.footer}>
        {complete && <BacktestReviewAction className={styles.secondary} target={{strategyId:result.strategyId,ts:result.ts,proposalId:result.proposalId,sourceRevision:typeof result.provenance.source_revision === "string" ? result.provenance.source_revision : undefined}}/>}
        {complete && !nonPerformance && <Link href={factorSourceUrl({strategy_id:result.strategyId,ts:result.ts,proposal_id:result.proposalId})} className={styles.secondary} data-testid="backtest-factor-library">{i18nCopy(zh, "copy.components_chat_BacktestReplyCards.042")}<Icon name="arrowUpRight" size={14}/></Link>}
        {complete && <button type="button" className={styles.action} aria-expanded={expanded} aria-controls={details ? `task-dock-panel-${reportId}` : id} onClick={openReport} data-testid="open-backtest-report">
<Icon name="chart" size={14}/>{!details && open ? (i18nCopy(zh, "copy.components_chat_BacktestReplyCards.043")) : poolPrice ? i18nCopy(zh, "copy.components_backtest_BacktestChart.poolPriceAction") : lpCounterfactual ? i18nCopy(zh, `copy.components_backtest_BacktestChart.${lpPrefix}Action`) : retrospective ? i18nCopy(zh, "copy.components_backtest_BacktestChart.retrospectiveReplayAction") : rateSimulation ? i18nCopy(zh, "copy.components_backtest_BacktestChart.rateReplayAction") : custom ? i18nCopy(zh, "copy.components_backtest_BacktestChart.customResearchAction") : simulated ? i18nCopy(zh, "copy.components_backtest_BacktestChart.scriptReplayAction") : observation ? i18nCopy(zh, "copy.components_chat_BacktestReplyCards.044") : i18nCopy(zh, "copy.components_chat_BacktestReplyCards.045")}<Icon name="chevronRight" size={14}/></button>}
        {target && (details ? <button type="button" onClick={openStrategy} className={styles.secondary}>{i18nCopy(zh, "copy.components_chat_BacktestReplyCards.046")}<Icon name="chevronRight" size={14}/></button> : <Link href={target} className={styles.secondary}>{i18nCopy(zh, "copy.components_chat_BacktestReplyCards.047")}<Icon name="arrowUpRight" size={14}/></Link>)}
      </div>
      <details className={styles.details}><summary>{i18nCopy(zh, "copy.components_chat_BacktestReplyCards.048")}</summary><pre>{[
        result.proposalId, result.strategyId, result.ts, typeof result.provenance.source_revision === "string" && !result.provenance.source_revision.includes("REDACTED") ? result.provenance.source_revision : "",
        ...result.flags, ...(!complete ? [result.message,result.nextAction] : !rateSimulation ? [result.message] : []),
      ].filter(Boolean).join("\n")}</pre></details>
    </div>
    {complete && open && !details && <div id={id} className={styles.report}><BacktestChart strategyId={result.strategyId} ts={result.ts} proposalId={result.proposalId}/></div>}
  </section>;
}
