"use client";

import { useLocale } from "next-intl";
import { copy } from "../../lib/i18n";
import type { ChatResult } from "../../lib/chatResults";
import { deliveryEvidence } from "../../lib/deliveryEvidence";
import { CheckIcon, ClockIcon, FileIcon, ShieldCheckIcon, ShieldXIcon, ChevronDownIcon } from "../icons";
import styles from "./DeliveryEvidence.module.css";

const producerLabels: Record<string, string> = {
  "Source version unchanged": "sourceBinding",
  "Candidate and replay match admitted request": "requestScope",
  "Strategy package structure": "packageStructure",
  "Executed candidate tests": "candidateTests",
  "Strategy structure and SDK contract": "sdkContract",
  "Isolated candidate tests": "isolatedTests",
  "Historical replay completed": "historicalRun",
  "Persisted replay report": "reportOutput",
  "Requested history coverage": "historyCoverage",
  "Historical source snapshots": "historicalSources",
  "Replay matches this source version": "replaySource",
  "Replay execution and order accounting": "executionIntegrity",
  "Historical trading performance evidence": "historicalPerformance",
  "Declared source captures": "researchSources",
  "Producer retrieval times": "researchRetrieval",
  "Saved research report": "researchReport",
};
const producerMessages: Record<string, string> = {
  "Declared captures cited by URL or exact call reference; not exhaustive question coverage.": "researchSourcesHint",
  "Producer retrieval times within command; not current market freshness.": "researchRetrievalHint",
  "Saved bytes match same-command write and bundle hash.": "researchReportHint",
};

export function DeliveryEvidence({ result, onOpenFile, onReveal, expanded = false }: {
  result: ChatResult; onOpenFile?: (path: string) => void; onReveal?: () => void; expanded?: boolean;
}) {
  const zh = useLocale().startsWith("zh");
  const t = (key: string, values?: Record<string, unknown>) => copy(zh, `copy.deliveryEvidence.${key}`, values);
  const evidence = deliveryEvidence(result);
  const Icon = evidence.status === "passed" ? ShieldCheckIcon : evidence.status === "failed" ? ShieldXIcon : ClockIcon;
  return <section aria-label={t("title")} data-testid="delivery-evidence" data-state={evidence.status} className={styles.evidence}>
    <div className={styles.summary}>
      <span className={styles.signal} aria-hidden><Icon size={20}/></span>
      <div className={styles.heading}>
        <p className={styles.eyebrow}>{t("title")}</p>
        <h2>{t(evidence.partialDelivery ? "partialDelivery" : evidence.executionErrors ? "executionErrors" : evidence.delivery && evidence.status === "pending" && !evidence.checks.length ? "incomplete" : evidence.status)}</h2>
      </div>
      <span className={styles.binding}>{t(evidence.versionBound ? "versionBound" : "recorded")}</span>
    </div>
    <p className={styles.explanation}>{t(evidence.partialDelivery
      ? evidence.delivery?.completion?.allowed === false ? "partialDeliveryPendingHint" : "partialDeliveryHint"
      : evidence.executionErrors ? "executionErrorsHint" : `${evidence.status}Hint`)}</p>
    <div className={styles.counts}>
      <span><FileIcon size={14}/>{t("files", { count: evidence.files.length })}</span>
      <span><CheckIcon size={14}/>{t("passedCount", { count: evidence.passed })}</span>
      <span>{t("unresolvedCount", { count: evidence.unresolved })}</span>
      {evidence.completionReasons.length > 0 && <span>{t("unmetCount", { count: evidence.completionReasons.length })}</span>}
    </div>
    <details className={styles.details} open={expanded}>
      <summary>{t("inspect")}<ChevronDownIcon size={14}/></summary>
      <div className={styles.content}>
        {evidence.completionReasons.length > 0 && <div>
          <h3>{t("completionReasons")}</h3>
          <ul className={styles.checks}>{evidence.completionReasons.map(reason => {
            const label = t(`reason_${reason.code}`);
            const subjectLabel = reason.subject ? t(`reason_${reason.subject}`) : "";
            const displayLabel = subjectLabel && !subjectLabel.startsWith("copy.") ? subjectLabel : label;
            return <li key={reason.raw}><div className={styles.checkBody}>
              {label.startsWith("copy.") ? reason.raw : <>{displayLabel}{displayLabel === label && reason.subject && <>: <code>{reason.subject}</code></>}</>}
            </div></li>;
          })}</ul>
        </div>}
        {evidence.checks.length > 0 && <div>
          <h3>{t("checks")}</h3>
          <ul className={styles.checks}>{evidence.checks.map(check => <li key={check.id} data-state={check.state}>
            <span className={styles.checkState}>{t(`check_${check.state}`)}</span>
            <div className={styles.checkBody}><span>{Object.hasOwn(producerLabels, check.label) ? t(`label_${producerLabels[check.label]}`) : check.label}</span>
              {check.message && <p>{Object.hasOwn(producerMessages, check.message) ? t(producerMessages[check.message]) : check.message}</p>}
              {check.revision && <p className={styles.revision} title={check.revision}>{t("revision")}: <code>{check.revision.slice(0, 12)}</code></p>}
              {check.evidenceRef && <p className={styles.revision}>{onOpenFile && !/^research(?:-report)?:/.test(check.evidenceRef) ? <button type="button" onClick={() => onOpenFile(check.evidenceRef)}>{check.evidenceRef}</button> : check.evidenceRef}</p>}
            </div>
          </li>)}</ul>
        </div>}
        {evidence.files.length > 0 && <div>
          <h3>{t("artifacts")}</h3>
          <ul className={styles.files}>{evidence.files.map(path => <li key={path}><FileIcon size={14}/>
            {onOpenFile && !/^research(?:-report)?:/.test(path) ? <button type="button" onClick={() => onOpenFile(path)}>{path}</button> : <code>{path}</code>}
          </li>)}</ul>
        </div>}
        {(evidence.risks.length > 0 || evidence.unverified.length > 0) && <div className={styles.limitations}>
          <h3>{t("limitations")}</h3>
          {evidence.risks.map(risk => {
            const label = t(`diagnostic_${risk}`);
            return label.startsWith("copy.") ? <p key={risk}>{risk}</p> : <div key={risk}>
              <p>{label}</p>
              <details><summary>{t("technicalDetails")}</summary><code>{risk}</code></details>
            </div>;
          })}
          {evidence.unverified.length > 0 && <p>{evidence.unverified.map(value => {
            const label = t(`scope_${value}`);
            return label.startsWith("copy.") ? value.replaceAll("_", " ") : label;
          }).join(t("scopeSeparator"))}</p>}
        </div>}
        <p className={styles.boundary}>{t("boundary")}</p>
        {result.turnId && onReveal && <button type="button" className={styles.trace} onClick={onReveal}>{t("trace")}</button>}
      </div>
    </details>
  </section>;
}
