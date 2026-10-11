"use client";

import Link from "next/link";
import { useEffect, useRef, useState } from "react";
import { useLocale, useTranslations } from "next-intl";
import { pendingRun, runLabel, taskRunApi, type RunOrigin } from "../../lib/taskRuns";
import { loadRunEvidence, type RunEvidence } from "../../lib/runEvidence";
import { usePolledResource } from "../../lib/usePolledResource";
import { financialApi } from "../../lib/financial";
import { confirm } from "../../lib/dialogs";
import { AssistantBubble } from "./ChatMessage";
import { ResourceNotice } from "../workflows/ResourceNotice";
import { RunWorkspace } from "../workflows/RunWorkspace";
import { RunAttention } from "../workflows/RunAttention";

export function TaskRunDetail({ runId, onClose, onOpenRun, taskKind, taskId }: {
  runId: string; onClose?: () => void; onOpenRun?: (id: string) => void;
  taskKind?: RunOrigin; taskId?: string;
}) {
  const locale = useLocale();
  const t = useTranslations("taskRuns"), w = useTranslations("workflowUpgrade"), tf = useTranslations("financial");
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const lock = useRef(false);
  const [successor, setSuccessor] = useState<{ id: string; session: string }>();
  const resource = usePolledResource<RunEvidence>(`run:${runId}:${taskKind || ""}:${taskId || ""}`, async (signal, previous) => {
    const evidence = await loadRunEvidence(runId, signal, previous);
    if (taskKind && evidence.run.task_kind !== taskKind || taskId && evidence.run.task_id !== taskId) throw new Error("Run does not belong to this task");
    return evidence;
  }, { intervalMs: evidence => evidence?.hasMore ? 150 : !evidence || pendingRun(evidence.run) ? 2000 : 30_000 });
  const run = resource.data?.run;
  const events = resource.data?.events || [];
  useEffect(() => { setError(""); setSuccessor(undefined); }, [runId]);

  async function control(action: "stop" | "resume" | "rerun" | "reconcile") {
    if (!run || lock.current) return;
    lock.current = true;
    let runOnly = false;
    try {
      if (action === "rerun" && !await confirm({ title: w("rerunTitle"), message: w("rerunConfirm") })) return;
      if (action === "resume" && run.queue?.paused && run.queue.pause_reason === "operator") {
        runOnly = await confirm({ title: t("resumeOnlyTitle"), message: t("resumeOnlyMessage") });
        if (!runOnly) return;
      }
      setBusy(true); setError("");
      const extra: Record<string, unknown> = action === "resume" ? { run_only: runOnly } : {};
      if (action === "rerun") {
        const descriptor = await financialApi.task(run.task_kind, run.task_id);
        extra.expected_task_revision = descriptor.task.source_revision;
      }
      const receipt = await taskRunApi.control(run, action, extra);
      if (!receipt.ok) throw new Error("Run control was not accepted");
      if (action === "rerun" && receipt.run_id) setSuccessor({ id: receipt.run_id, session: receipt.session_id || run.session_id });
      resource.refresh();
    } catch (reason) { setError(String(reason)); }
    finally { lock.current = false; setBusy(false); }
  }

  async function reconcileFunds() {
    if (!run || lock.current) return;
    lock.current = true; setBusy(true); setError("");
    try {
      for (const effect of run.result.effects || []) if (typeof effect.action_id === "string") await financialApi.reconcileAction(effect.action_id);
      resource.refresh();
    } catch (reason) { setError(String(reason)); }
    finally { lock.current = false; setBusy(false); }
  }

  const active = !!run && ["queued", "running", "stopping"].includes(run.execution_status);
  const message = run ? { id: run.turn_id || run.run_id, role: "assistant" as const, ts: run.created_at * 1000,
    loading: active, execution_status: run.execution_status,
    turn: { ...run.result.turn, reply_text: run.result.final_text || run.result.turn?.reply_text || "" } } : null;

  return <section data-testid="task-run-detail" data-run-id={runId}>
    <ResourceNotice error={resource.error} updatedAt={resource.updatedAt} onRetry={resource.refresh} />
    {error && <p role="alert" className="my-3 text-sm text-danger">{error}</p>}
    {!run ? <p role="status" className="p-4 text-sm text-[color:var(--text-muted)]">{resource.loading ? w("loading") : w("readFailed")}</p> : <RunWorkspace
      title={run.snapshot.title || run.task_id || t("thisRun")}
      subtitle={<>{new Date(run.created_at * 1000).toLocaleString(locale)} · {run.snapshot.mode === "analysis" ? t("analysisTask") : run.snapshot.mode?.toUpperCase()} · <span title={run.source_revision}>{w("sourceRevision")} {run.source_revision.slice(0, 10)}</span></>}
      status={<span>{runLabel(run.execution_status, t)}</span>}
      actions={<>
        {onClose && <button type="button" className="btn-ghost text-xs" onClick={onClose}>{t("backToConversation")}</button>}
        <button type="button" className="btn-ghost text-xs" onClick={resource.refresh} disabled={resource.loading}>{w("refresh")}</button>
        {["queued", "running", "awaiting_input", "awaiting_approval"].includes(run.execution_status) && <button type="button" className="btn-ghost text-xs" title={w("stopHint")} disabled={busy} onClick={() => void control("stop")}>{t("stopRun")}</button>}
        {run.execution_status === "unconfirmed" && <button type="button" className="btn-ghost text-xs" disabled={busy} onClick={() => void control("reconcile")}>{t("reconcileReceipt")}</button>}
        {["submitted", "confirming", "unconfirmed", "needs_recovery", "partial"].includes(run.business_status) && <button type="button" className="btn-ghost text-xs" disabled={busy} onClick={() => void reconcileFunds()}>{t("reconcileFunds")}</button>}
        {run.execution_status === "awaiting_approval" && <Link className="btn-ghost text-xs" href="/inbox?type=approval">{t("reviewApproval")}</Link>}
        {run.execution_status === "awaiting_input" && <Link className="btn-ghost text-xs" href={`/chat/${encodeURIComponent(run.session_id)}?run=${encodeURIComponent(run.run_id)}`}>{t("provideInput")}</Link>}
        {["interrupted", "blocked"].includes(run.execution_status) && <button type="button" className="btn-ghost text-xs" disabled={busy} onClick={() => void control("resume")}>{t("resumeRun")}</button>}
        {["succeeded", "failed", "interrupted", "blocked"].includes(run.execution_status) && <button type="button" className="btn-ghost text-xs" disabled={busy} onClick={() => void control("rerun")}>{t("startNewRun")}</button>}
      </>}
      result={<>
        <RunAttention run={run} events={events} onResolved={resource.refresh} />
        {run.reason && <p className="mb-3 text-sm text-[color:var(--text-muted)]">{run.reason}</p>}
        {successor && <p role="status" className="mb-4 text-sm">{w("newRun")} · {onOpenRun ? <button type="button" className="underline" onClick={() => onOpenRun(successor.id)}>{w("openNewRun")}</button> : <Link className="underline" href={`/chat/${encodeURIComponent(successor.session)}?run=${encodeURIComponent(successor.id)}`}>{w("openNewRun")}</Link>}</p>}
        {message && <AssistantBubble msg={{ ...message, live_events: active ? events : [] }} />}
        {!!run.result.effects?.length && <section className="mt-4 space-y-3 text-sm" aria-label={t("actionReceipts")}><h4 className="font-medium">{t("actionReceipts")}</h4>{run.result.effects.map((effect, index) => {
          const request = (effect.request || {}) as Record<string, unknown>;
          const submission = (effect.submission || {}) as Record<string, unknown>;
          const receipt = (effect.receipt || {}) as Record<string, unknown>;
          return <div key={String(effect.action_id || index)} className="border-t border-[color:var(--line)] pt-3"><p className="flex flex-wrap justify-between gap-2"><strong>{tf.has("actions." + String(effect.kind)) ? tf("actions." + String(effect.kind)) : String(effect.kind || "")}</strong><span>{runLabel(String(effect.state || ""), t)}</span></p>
            {request.amount !== undefined && <p className="mt-1 break-all text-xs">{String(request.amount)} {String(request.asset || "")}</p>}
            {typeof request.recipient === "string" && <p className="mt-1 break-all text-xs">{t("recipient", { recipient: request.recipient })}</p>}
            {typeof submission.transaction_hash === "string" && <p className="mt-1 break-all text-xs text-[color:var(--text-muted)]">{t("submissionIdentity", { identity: submission.transaction_hash })}</p>}
            {receipt.source_confirmed === true && <p className="mt-1 text-xs">{t(receipt.destination_confirmed === true ? "bridgeConfirmed" : "bridgePending")}</p>}
            <details className="mt-2 text-xs"><summary>{t("receiptEvidence")}</summary><pre className="mt-2 max-h-72 overflow-auto whitespace-pre-wrap break-all">{JSON.stringify(effect, null, 2)}</pre></details>
          </div>;
        })}</section>}
        <details className="mt-4 text-xs"><summary>{w("stateDetails")}</summary><div className="mt-2 flex flex-wrap gap-3" aria-label={t("runStatus")}><span>{runLabel(run.execution_status, t)}</span><span>{runLabel(run.business_status, t)}</span><span>{t("deliveryStatus", { status: runLabel("delivery:" + run.delivery_status, t) })}</span></div></details>
      </>}
      activity={<>
        <p className="mb-3 text-xs text-[color:var(--text-muted)]">{w("readOnlyHistory")}</p>
        {resource.data?.hasMore && <p role="status" className="text-sm">{w("moreEvents")}</p>}
        {message && events.length > 0 ? <AssistantBubble msg={{ ...message, live_events: events, turn: { ...run.result.turn, reply_text: "" } }} /> : <p className="text-sm text-[color:var(--text-muted)]">{w("noRecordedSteps")}</p>}
      </>}
      provenance={<>
        <dl className="grid grid-cols-[auto_minmax(0,1fr)] gap-x-4 gap-y-2"><dt>{w("runIdentity")}</dt><dd className="break-all font-mono">{run.run_id}</dd><dt>{w("sourceRevision")}</dt><dd className="break-all font-mono">{run.source_revision}</dd><dt>{w("trigger")}</dt><dd>{run.trigger_kind === "manual" ? w("manualTrigger") : run.trigger_kind === "schedule" ? w("scheduleTrigger") : run.trigger_kind}</dd></dl>
        {run.retry_of_run_id && <p className="mt-3">{w("previousRun")} · {onOpenRun ? <button type="button" className="underline" onClick={() => onOpenRun(run.retry_of_run_id!)}>{run.retry_of_run_id}</button> : <code>{run.retry_of_run_id}</code>}</p>}
        <details className="mt-3"><summary>{t("permissionsConfiguration")}</summary><pre className="mt-2 max-h-72 overflow-auto whitespace-pre-wrap break-all">{JSON.stringify({ version: run.source_revision, model: run.snapshot.accepted_model, permissions: run.snapshot.effective_permissions, budget: run.snapshot.budget }, null, 2)}</pre></details>
      </>}
    />}
  </section>;
}
