"use client";

import { useEffect, useState } from "react";
import { useTranslations } from "next-intl";
import { callApi } from "../../lib/clientApi";
import { asObject } from "../../lib/workflowPresentation";
import { publicReplay, rememberInvocation } from "../../lib/workflowReplay";
import { WorkflowRunCanvas } from "./WorkflowRunCanvas";
import { ReplayEvidence } from "./ReplayEvidence";
import { ReviewExplanation } from "./ReviewExplanation";
import styles from "./WorkflowReplay.module.css";
import { usePolledResource } from "../../lib/usePolledResource";
import { ResourceNotice } from "./ResourceNotice";
import { RunWorkspace } from "./RunWorkspace";

type Facts = Record<string, unknown>;
function useRecordedResource(path: string) {
  return usePolledResource<Facts>(path || null, async signal => {
    const data = await callApi<Facts>(path, { signal });
    if (data.ok === false) throw new Error(String(data.error || "Record unavailable"));
    return data;
  }, { intervalMs: 10000 });
}

export function WorkflowReviewActivity({ strategyId }: { strategyId: string }) {
  const t = useTranslations("workflowExperience");
  const [selected, setSelected] = useState("");
  const [offset, setOffset] = useState(0);
  const w = useTranslations("workflowUpgrade");
  const pageSize = 50;
  useEffect(() => {
    const params = new URLSearchParams(window.location.search);
    if (params.get("workflow_log") === "evolution") setSelected(params.get("workflow_run") || "");
  }, []);
  const base = `/strategies/runtime/tuning`;
  const query = `strategy_id=${encodeURIComponent(strategyId)}`;
  const history = useRecordedResource(`${base}/history?${query}&limit=${pageSize}&offset=${offset}`);
  const detail = useRecordedResource(selected ? `${base}/record?${query}&run_id=${encodeURIComponent(selected)}` : "");
  const historyMatches = history.data?.strategy_id === strategyId;
  const rows = historyMatches && Array.isArray(history.data?.runs) ? history.data.runs.map(asObject) : [];
  useEffect(() => {
    if (!selected && history.data?.strategy_id === strategyId && Array.isArray(history.data.runs)) {
      const first = asObject(history.data.runs[0]);
      if (typeof first.run_id === "string") setSelected(first.run_id);
    }
  }, [history.data, selected, strategyId]);
  function choose(id: string) { setSelected(id); rememberInvocation(id, "evolution"); }
  const matches = detail.data?.strategy_id === strategyId && detail.data?.run_id === selected;
  const record = asObject(matches ? detail.data?.record : undefined);
  const audit = asObject(matches ? detail.data?.audit : undefined);
  const replay = publicReplay(audit.conversation);
  const status = (value: unknown) => t(({ ok: "returned", success: "returned", error: "failed", failed: "failed", returned: "returned", running: "running", skipped: "skipped", hold: "held" } as Record<string, string>)[String(value)] || "unknown");
  const prompts = Array.isArray(audit.prompt_records) ? audit.prompt_records.map((value) => asObject(value).prompt).filter((value) => typeof value === "string") : [];
  const prompt = prompts.length ? prompts.join("\n\n") : audit.role_prompt;
  const output = audit.subagent_output ?? record.subagent_output ?? record.error;
  const error = history.error || detail.error || (history.data && !historyMatches || detail.data && !matches ? t("recordError") : "");
  return <section className="space-y-5 p-5 sm:p-7" data-testid="workflow-review-activity">
    <header className={styles.replayHeader}><h3>{t("reviewLogs")}</h3><button type="button" disabled={history.loading || detail.loading} onClick={() => { history.refresh(); detail.refresh(); }}>{t("refresh")}</button></header>
    <ResourceNotice error={error || undefined} updatedAt={detail.updatedAt || history.updatedAt} onRetry={() => { history.refresh(); detail.refresh(); }} />
    {history.data?.partial === true && <p className={styles.note}>{t("partialHistory")}</p>}
    <div className={styles.records} aria-label={t("selectInvocation")}>
      {rows.map((row) => <button type="button" className={styles.record} key={String(row.run_id)} aria-pressed={selected === row.run_id} onClick={() => choose(String(row.run_id))}>
        <strong>{t("review")} · {status(row.status)}</strong>
        <small>{Number.isNaN(Date.parse(String(row.started_at || row.ts))) ? String(row.run_id) : new Date(String(row.started_at || row.ts)).toLocaleString()}</small>
        <small>{String(row.reason || row.run_id)}</small>
      </button>)}
    </div>
    {!rows.length && !history.error && <p role="status">{history.loading && !history.data ? t("loading") : t("noReviewLogs")}</p>}
    {(offset > 0 || history.data?.has_more === true) && <nav className="flex items-center gap-4" aria-label={t("reviewLogs")}>
      <button type="button" disabled={!offset || history.loading} onClick={() => setOffset(Math.max(0, offset - pageSize))}>{t("previousPage")}</button>
      <span>{Math.floor(offset / pageSize) + 1}</span>
      <button type="button" disabled={!history.data?.has_more || history.loading} onClick={() => setOffset(offset + pageSize)}>{t("nextPage")}</button>
    </nav>}
    {selected && !matches && !detail.error && <p role="status">{t("loading")}</p>}
    {matches && <RunWorkspace key={`${strategyId}:${selected}`} title={t("review")} subtitle={String(record.started_at || record.ts || selected)} status={status(record.status)}
      result={<ReviewExplanation record={{...record,strategy_id:strategyId}} output={output} status={String(record.status || "")} />}
      activity={<WorkflowRunCanvas invocation={{
        id: selected, kind: "agent", title: t("review"), status: status(record.status),
        input: audit.payload ?? record.request, output: output ?? record,
        calls: replay.calls.map(call => ({...call,status:status(call.status)})),
      }} prompt={prompt} reply={output} messages={replay.messages} partial={detail.data?.partial===true || !Array.isArray(audit.conversation)}/>}
      provenance={<><p className="mb-3">{w("readOnlyHistory")}</p><details><summary>{t("recordedSteps")}</summary><ReplayEvidence value={{result:record,steps:audit.steps,model_calls:audit.model_calls,missing:detail.data?.missing}}/></details></>}
    />}

  </section>;
}
