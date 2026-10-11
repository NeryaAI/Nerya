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
import { StrategyDetailContext } from "./StrategyDetailContext";
import { strategyDetailId, type StrategyDetailTarget } from "../../lib/strategyDetail";
import styles from "./BacktestReplyCards.module.css";
import { BacktestExecutionEvidence } from "../backtest/BacktestExecutionEvidence";
import { BacktestCoverage } from "../backtest/BacktestCoverage";
import { BacktestBiasChecks } from "../backtest/BacktestBiasChecks";
import { BacktestReviewAction } from "../backtest/BacktestReviewAction";
import { factorSourceUrl } from "../../lib/factorLibrary";

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
  const observation = result.evaluationMode === "observation" || result.performanceEvidence === false;
  const agentNotRun = result.executionMode === "agent" && result.replay.agent_execution === "not_run";
  const dataLabel = ({ historical: i18nCopy(zh, "copy.components_chat_BacktestReplyCards.008"), sample: i18nCopy(zh, "copy.components_chat_BacktestReplyCards.009"),
    unverified: i18nCopy(zh, "copy.components_chat_BacktestReplyCards.010") } as Record<string,string>)[result.dataKind] || (i18nCopy(zh, "copy.components_chat_BacktestReplyCards.011"));
  const date = (raw: string) => Number.isFinite(Date.parse(raw)) ? new Date(raw).toLocaleString(locale, { year:"numeric",month:"short",day:"numeric",hour:"2-digit",minute:"2-digit",timeZone:"UTC" }) : "";
  const values = observation ? result.replay : result.metrics;
  const metrics = observation ? [["decisions", i18nCopy(zh, "copy.components_chat_BacktestReplyCards.012")], ["dispatches", i18nCopy(zh, "copy.components_chat_BacktestReplyCards.013")],
    ["skipped", i18nCopy(zh, "copy.components_chat_BacktestReplyCards.014")], ["errors", i18nCopy(zh, "copy.components_chat_BacktestReplyCards.015")]]
    : [["total_return_pct", i18nCopy(zh, "copy.components_chat_BacktestReplyCards.016")], ["max_drawdown_pct", i18nCopy(zh, "copy.components_chat_BacktestReplyCards.017")],
      ["sharpe_ratio", i18nCopy(zh, "copy.components_chat_BacktestReplyCards.018")], ["total_trades", i18nCopy(zh, "copy.components_chat_BacktestReplyCards.019")]];
  const format = (key: string, raw: unknown): string => typeof raw === "string" && ["sharpe_ratio","total_trades"].includes(key) && raw.trim() && Number.isFinite(Number(raw)) ? format(key,Number(raw)) : typeof raw === "number" && Number.isFinite(raw)
    ? `${financeNumber(raw, locale, observation || key === "total_trades" ? 0 : 2)}${key.endsWith("_pct") ? "%" : ""}`
    : typeof raw === "string" && raw.trim() && !/^(nan|none|null|undefined|[-+]?inf(?:inity)?|[-+]?∞)$/i.test(raw.trim()) ? raw : "—";
  const verdict = ({ PASS: observation ? (i18nCopy(zh, "copy.components_chat_BacktestReplyCards.020")) : (i18nCopy(zh, "copy.components_chat_BacktestReplyCards.021")), WARN: i18nCopy(zh, "copy.components_chat_BacktestReplyCards.022"), FAIL: i18nCopy(zh, "copy.components_chat_BacktestReplyCards.023") } as Record<string,string>)[result.verdict];
  const points = [...new Map(result.equityPreview.map(p => [p.time,p])).values()].sort((a,b) => a.time-b.time);
  const min = Math.min(...points.map(p=>p.value)), max = Math.max(...points.map(p=>p.value));
  const from = points[0]?.time || 0, span = (points.at(-1)?.time || 0) - from;
  const line = points.length > 1 ? points.map((p,i) => `${i ? "L" : "M"}${(4 + (p.time-from)/(span||1)*552).toFixed(2)},${(max === min ? 32 : 58-(p.value-min)/(max-min)*50).toFixed(2)}`).join(" ") : "";
  const target = result.strategyId ? `/strategies?strategy_id=${encodeURIComponent(result.strategyId)}${result.proposalId ? `&proposal_id=${encodeURIComponent(result.proposalId)}` : ""}` : "";
  return <section className={styles.card} data-testid="backtest-result-card" data-backtest-id={result.id} data-status={result.status}
    data-evaluation={observation ? "observation" : "trading"} aria-label={i18nCopy(zh, "copy.components_chat_BacktestReplyCards.024")}>
    <div className={styles.body}>
      <header className={styles.header}>
        <div className={styles.identity}><div className={styles.kind}><Icon name="chart" size={15}/>{observation ? (i18nCopy(zh, "copy.components_chat_BacktestReplyCards.025")) : (i18nCopy(zh, "copy.components_chat_BacktestReplyCards.026"))}</div>
          <h3 className={styles.title}>{result.title}</h3></div>
        <div className={styles.headerActions}><span className={`${styles.state} ${!complete || result.verdict === "WARN" ? "text-warn" : result.verdict === "FAIL" ? "text-danger" : "text-[color:var(--text-muted)]"}`}>
          {complete ? verdict || (i18nCopy(zh, "copy.components_chat_BacktestReplyCards.027")) : result.status === "blocked" ? (i18nCopy(zh, "copy.components_chat_BacktestReplyCards.028")) : (i18nCopy(zh, "copy.components_chat_BacktestReplyCards.029"))}
        </span>{complete && <button type="button" className={styles.expand} onClick={openReport} aria-label={i18nCopy(zh, "copy.components_chat_BacktestReplyCards.030")} title={i18nCopy(zh, "copy.components_chat_BacktestReplyCards.031")} aria-expanded={expanded} aria-controls={details ? `task-dock-panel-${reportId}` : id} data-testid="expand-backtest-details"><Icon name="arrowUpRight" size={18}/></button>}</div>
      </header>
      {complete && <p className={styles.period}>{result.start && result.end ? `${date(result.start)} – ${date(result.end)} UTC` : (i18nCopy(zh, "copy.components_chat_BacktestReplyCards.032"))}</p>}
      {complete && <BacktestCoverage compact meta={{ ...result.metrics, ...result.coverage, start: result.start, end: result.end, provenance: result.provenance }}/>}
      {complete && <dl className={styles.metrics}>{metrics.map(([key,label]) => <div key={key}><dt>{label}</dt><dd data-metric={key}>{format(key,values[key])}</dd></div>)}</dl>}
      {complete && !observation && <BacktestExecutionEvidence replay={result.replay}/>}
      {complete && !observation && line && <figure className={styles.trend}>
        <svg viewBox="0 0 560 64" preserveAspectRatio="none" role="img" aria-label={i18nCopy(zh, "copy.components_chat_BacktestReplyCards.033")}><path d={line} fill="none" stroke="currentColor" strokeWidth="1.6" vectorEffect="non-scaling-stroke"/></svg>
        <figcaption><span>{i18nCopy(zh, "copy.components_chat_BacktestReplyCards.034")}</span><span>{financeNumber(points.at(-1)!.value,locale,2)} USD</span></figcaption>
      </figure>}
      {complete && <BacktestBiasChecks compact meta={{ bias_checks: result.biasChecks, research_checks: result.researchChecks, provenance: result.provenance }}/>}
      {complete ? <>
        {result.flags.filter(flag => evaluationNotes[flag]).map(flag => <p key={flag} className={styles.note} data-testid="backtest-evaluation-note">{i18nCopy(zh, evaluationNotes[flag] ?? "")}</p>)}
        <div className={styles.evidence}><span>{dataLabel}</span><span>{result.engine === "freeform" ? (i18nCopy(zh, "copy.components_chat_BacktestReplyCards.035")) : (i18nCopy(zh, "copy.components_chat_BacktestReplyCards.036"))}</span>
          {agentNotRun && <strong className="text-warn">{i18nCopy(zh, "copy.components_chat_BacktestReplyCards.037")}</strong>}</div>
        {result.message && <p className={styles.note}>{/^Loaded [\d.]+d of real candle coverage/.test(result.message) ? i18nCopy(zh,"copy.components_chat_BacktestReplyCards.legacyCoverage",{days:result.message.match(/^Loaded ([\d.]+)d/)?.[1]}) : result.message}</p>}
        <p className={styles.note}>{observation ? (i18nCopy(zh, "copy.components_chat_BacktestReplyCards.038"))
          : result.dataKind !== "historical" ? (i18nCopy(zh, "copy.components_chat_BacktestReplyCards.039"))
          : (i18nCopy(zh, "copy.components_chat_BacktestReplyCards.040"))}</p>
      </> : <p className={styles.diagnostic} role="status">{result.message.split("\n")[0].slice(0,360) || (i18nCopy(zh, "copy.components_chat_BacktestReplyCards.041"))}</p>}
      <div className={styles.footer}>
        {complete && <BacktestReviewAction className={styles.secondary} target={{strategyId:result.strategyId,ts:result.ts,proposalId:result.proposalId,sourceRevision:typeof result.provenance.source_revision === "string" ? result.provenance.source_revision : undefined}}/>}
        {complete && !observation && <Link href={factorSourceUrl({strategy_id:result.strategyId,ts:result.ts,proposal_id:result.proposalId})} className={styles.secondary} data-testid="backtest-factor-library">{i18nCopy(zh, "copy.components_chat_BacktestReplyCards.042")}<Icon name="arrowUpRight" size={14}/></Link>}
        {complete && <button type="button" className={styles.action} aria-expanded={expanded} aria-controls={details ? `task-dock-panel-${reportId}` : id} onClick={openReport} data-testid="open-backtest-report">
          <Icon name="chart" size={14}/>{!details && open ? (i18nCopy(zh, "copy.components_chat_BacktestReplyCards.043")) : observation ? (i18nCopy(zh, "copy.components_chat_BacktestReplyCards.044")) : (i18nCopy(zh, "copy.components_chat_BacktestReplyCards.045"))}<Icon name="chevronRight" size={14}/></button>}
        {target && (details ? <button type="button" onClick={openStrategy} className={styles.secondary}>{i18nCopy(zh, "copy.components_chat_BacktestReplyCards.046")}<Icon name="chevronRight" size={14}/></button> : <Link href={target} className={styles.secondary}>{i18nCopy(zh, "copy.components_chat_BacktestReplyCards.047")}<Icon name="arrowUpRight" size={14}/></Link>)}
      </div>
      <details className={styles.details}><summary>{i18nCopy(zh, "copy.components_chat_BacktestReplyCards.048")}</summary><pre>{[
        result.proposalId, result.strategyId, result.ts, typeof result.provenance.source_revision === "string" && !result.provenance.source_revision.includes("REDACTED") ? result.provenance.source_revision : "",
        ...result.flags, ...(!complete ? [result.message,result.nextAction] : []),
      ].filter(Boolean).join("\n")}</pre></details>
    </div>
    {complete && open && !details && <div id={id} className={styles.report}><BacktestChart strategyId={result.strategyId} ts={result.ts} proposalId={result.proposalId}/></div>}
  </section>;
}
