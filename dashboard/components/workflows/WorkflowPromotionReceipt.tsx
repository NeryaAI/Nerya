"use client";

import { copy as i18nCopy } from "../../lib/i18n";

import { useEffect, useRef, useState } from "react";
import { useLocale } from "next-intl";
import Link from "next/link";
import { clientApi } from "../../lib/clientApi";

function record(value: unknown): Record<string, unknown> {
  return value && typeof value === "object" && !Array.isArray(value) ? value as Record<string, unknown> : {};
}

export function promotionReceiptFacts(value: unknown) {
  const receipt = record(value), sync = record(receipt.schedule_sync);
  const warnings = Array.isArray(record(receipt.promotion).warnings)
    ? (record(receipt.promotion).warnings as unknown[]).filter((item): item is string => typeof item === "string") : [];
  return {
    validation: record(receipt.validation).ok,
    application: record(receipt.application).status || (receipt.ok === true || record(receipt.promotion).ok === true ? "applied" : "unrecorded"),
    sync,
    syncFailed: sync.status === "failed" || (!sync.status && warnings.includes("schedule_sync_failed")),
    warnings: sync.status === "synced" ? warnings.filter((item) => item !== "schedule_sync_failed") : warnings,
    service: record(receipt.service),
  };
}

export function WorkflowPromotionReceipt({ strategyId, proposalId, receipt: initial }: {
  strategyId: string; proposalId: string; receipt?: unknown;
}) {
  const zh = useLocale().startsWith("zh");
  const [receipt, setReceipt] = useState<unknown>(initial);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [newerProposal, setNewerProposal] = useState(false);
  const request = useRef(0);
  useEffect(() => { setReceipt(initial); }, [initial]);
  useEffect(() => {
    let disposed = false;
    const version = ++request.current;
    void clientApi.strategyRuntimeScheduleStatus(strategyId).then((response) => {
      if (disposed || version !== request.current) return;
      if (response.ok === false) throw new Error(String(record(response).error || "schedule_status_failed"));
      const saved = record(record(response).promotion_receipt);
      setNewerProposal(!!saved.proposal_id && saved.proposal_id !== proposalId);
      if (saved.proposal_id === proposalId) setReceipt({ ...saved, service: record(response).service });
    }).catch(() => { if (!disposed && version === request.current) setError(i18nCopy(zh, "copy.components_workflows_WorkflowPromotionReceipt.001")); });
    return () => { disposed = true; };
  }, [strategyId, proposalId, initial, zh]);
  const facts = promotionReceiptFacts(receipt);
  const unknown = i18nCopy(zh, "copy.components_workflows_WorkflowPromotionReceipt.002");
  async function refresh() {
    ++request.current;
    setBusy(true); setError("");
    try {
      const response = await clientApi.strategyRuntimeScheduleStatus(strategyId);
      if (response.ok === false) throw new Error("schedule_status_failed");
      const saved = record(record(response).promotion_receipt);
      setNewerProposal(!!saved.proposal_id && saved.proposal_id !== proposalId);
      if (saved.proposal_id === proposalId) setReceipt({ ...saved, service: record(response).service });
    } catch { setError(i18nCopy(zh, "copy.components_workflows_WorkflowPromotionReceipt.003")); }
    finally { setBusy(false); }
  }
  async function retrySync() {
    if (!facts.syncFailed || facts.application !== "applied" || newerProposal) return;
    ++request.current;
    setBusy(true); setError("");
    try {
      const response = await clientApi.strategyRuntimeSchedule(strategyId);
      if (response.ok === false) throw new Error("schedule_sync_failed");
      setReceipt((previous: unknown) => ({ ...record(previous), schedule_sync: { ...response, status: "synced" } }));
      if (Array.isArray(record(response).warnings)) setError(i18nCopy(zh, "copy.components_workflows_WorkflowPromotionReceipt.004"));
    } catch { setError(i18nCopy(zh, "copy.components_workflows_WorkflowPromotionReceipt.005")); }
    finally { setBusy(false); }
  }
  const label = (status: unknown) => {
    const labels: Record<string, string> = {
      applied: "copy.components_workflows_WorkflowPromotionReceipt.006", failed: "copy.components_workflows_WorkflowPromotionReceipt.007", not_applied: "copy.components_workflows_WorkflowPromotionReceipt.008",
      synced: "copy.components_workflows_WorkflowPromotionReceipt.009", not_attempted: "copy.components_workflows_WorkflowPromotionReceipt.010",
      not_applicable: "copy.components_workflows_WorkflowPromotionReceipt.011", stopped: "copy.components_workflows_WorkflowPromotionReceipt.012",
      running: "copy.components_workflows_WorkflowPromotionReceipt.013", starting: "copy.components_workflows_WorkflowPromotionReceipt.014", stopping: "copy.components_workflows_WorkflowPromotionReceipt.015",
      restarting: "copy.components_workflows_WorkflowPromotionReceipt.016", interrupted: "copy.components_workflows_WorkflowPromotionReceipt.017",
      unresponsive: "copy.components_workflows_WorkflowPromotionReceipt.018", finished: "copy.components_workflows_WorkflowPromotionReceipt.019",
      unconfirmed: "copy.components_workflows_WorkflowPromotionReceipt.020", not_checked: "copy.components_workflows_WorkflowPromotionReceipt.021",
    };
    return i18nCopy(zh, labels[String(status)] ?? "") || unknown;
  };
  const timer = (key: "trading_id" | "tuning_id") => facts.syncFailed ? (i18nCopy(zh, "copy.components_workflows_WorkflowPromotionReceipt.022"))
    : facts.sync.status !== "synced" ? unknown
    : facts.sync[key] ? (i18nCopy(zh, "copy.components_workflows_WorkflowPromotionReceipt.023")) : (i18nCopy(zh, "copy.components_workflows_WorkflowPromotionReceipt.024"));
  return <section className="mt-3 rounded-md border border-ink-500/20 p-3 text-xs" data-testid="workflow-promotion-receipt">
    <dl className="grid grid-cols-[auto_minmax(0,1fr)] gap-x-4 gap-y-1">
      <dt>{i18nCopy(zh, "copy.components_workflows_WorkflowPromotionReceipt.025")}</dt><dd>{facts.validation === true ? (i18nCopy(zh, "copy.components_workflows_WorkflowPromotionReceipt.026")) : facts.validation === false ? (i18nCopy(zh, "copy.components_workflows_WorkflowPromotionReceipt.027")) : unknown}</dd>
      <dt>{i18nCopy(zh, "copy.components_workflows_WorkflowPromotionReceipt.028")}</dt><dd>{label(facts.application)}</dd>
      <dt>{i18nCopy(zh, "copy.components_workflows_WorkflowPromotionReceipt.029")}</dt><dd>{timer("trading_id")}</dd>
      <dt>{i18nCopy(zh, "copy.components_workflows_WorkflowPromotionReceipt.030")}</dt><dd>{timer("tuning_id")}</dd>
      <dt>{i18nCopy(zh, "copy.components_workflows_WorkflowPromotionReceipt.031")}</dt><dd>{label(facts.service.state)}</dd>
    </dl>
    {newerProposal && <p className="mt-2 text-warn" role="status">{i18nCopy(zh, "copy.components_workflows_WorkflowPromotionReceipt.032")}</p>}
    {facts.syncFailed && !newerProposal && <p className="mt-2 text-warn" role="status">{facts.application === "applied"
      ? (i18nCopy(zh, "copy.components_workflows_WorkflowPromotionReceipt.033"))
      : (i18nCopy(zh, "copy.components_workflows_WorkflowPromotionReceipt.034"))}</p>}
    {facts.warnings.filter((warning) => warning !== "schedule_sync_failed").map((warning) => <p key={warning} className="mt-2 text-warn">{warning}</p>)}
    <p className="mt-2 text-ink-400">{i18nCopy(zh, "copy.components_workflows_WorkflowPromotionReceipt.035")}</p>
    <div className="mt-2 flex flex-wrap gap-3">
      {facts.syncFailed && facts.application === "applied" && !newerProposal && <button type="button" className="btn btn-ghost text-xs" disabled={busy} onClick={() => void retrySync()}>{i18nCopy(zh, "copy.components_workflows_WorkflowPromotionReceipt.036")}</button>}
      <button type="button" className="btn btn-ghost text-xs" disabled={busy} onClick={() => void refresh()}>{i18nCopy(zh, "copy.components_workflows_WorkflowPromotionReceipt.037")}</button>
      <Link className="btn btn-ghost text-xs" href={`/strategies/${encodeURIComponent(strategyId)}`}>{i18nCopy(zh, "copy.components_workflows_WorkflowPromotionReceipt.038")}</Link>
    </div>
    {error && <p role="alert" className="mt-2 text-danger">{error}</p>}
  </section>;
}
