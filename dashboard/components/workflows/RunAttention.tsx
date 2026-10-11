"use client";

import { useRef, useState } from "react";
import { useTranslations } from "next-intl";
import { callApi, clientApi, type ApprovalCard } from "../../lib/clientApi";
import type { TaskRun } from "../../lib/taskRuns";
import type { SessionView } from "../../lib/workbench";
import type { LiveEvent } from "../../lib/chat";
import { usePolledResource } from "../../lib/usePolledResource";
import { InteractionPanel } from "../chat/InteractionPanel";
import { ApprovalRequestCard } from "../chat/ApprovalRequestCard";
import { ResourceNotice } from "./ResourceNotice";

type Attention = { view: SessionView; approvals: ApprovalCard[] };

/** Scope by exact run / turn / recorded approval identity, never the latest
 * message in a reused session. All actions use existing signed callbacks and
 * interaction revisions; this surface grants no additional authority.
 */
export function RunAttention({ run, events, onResolved }: { run: TaskRun; events: LiveEvent[]; onResolved: () => void }) {
  const t = useTranslations("workflowUpgrade");
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const lock = useRef(false);
  const waiting = ["awaiting_input", "awaiting_approval"].includes(run.execution_status);
  const resource = usePolledResource<Attention>(waiting ? `run-attention:${run.run_id}:${run.session_id}:${run.turn_id || ""}` : null, async signal => {
    const view = await callApi<SessionView>(`/agent/sessions/view?session_id=${encodeURIComponent(run.session_id)}`, { signal });
    if (!view.ok || view.session_id !== run.session_id) throw new Error("Session identity mismatch");
    const pending = run.execution_status === "awaiting_approval" ? await callApi<{ ok: boolean; approvals: ApprovalCard[] }>("/approvals/pending", { signal }) : { ok: true, approvals: [] };
    if (!pending.ok || !Array.isArray(pending.approvals)) throw new Error("Pending approvals unavailable");
    return { view, approvals: pending.approvals };
  }, { intervalMs: 3000 });
  if (!waiting) return null;
  const ids = new Set(events.filter(event => event.kind === "approval.request").map(event => String(event.approval_id || "")));
  const interactions = (resource.data?.view.pending_interactions || []).filter(item => item.session_id === run.session_id && !!run.turn_id && item.turn_id === run.turn_id && ["pending", "deferred"].includes(item.state));
  const approvals = (resource.data?.approvals || []).filter(card => {
    const record = card.record || {};
    const id = card.prompt?.approval_id || String(record.approval_id || record.id || "");
    return !!id && (record.run_id === run.run_id || !!run.turn_id && record.session_id === run.session_id && record.turn_id === run.turn_id || ids.has(id));
  });
  function resolved() { resource.refresh(); onResolved(); }
  async function act(callback: string) {
    if (lock.current || resource.error || !approvals.some(card => card.prompt?.buttons?.some(button => button.callback_data === callback))) return;
    lock.current = true; setBusy(true); setError("");
    try { const result = await clientApi.approvalCallback({ callback_data: callback }); if (!result.ok) throw new Error(result.error || "Action not accepted"); }
    catch (reason) { setError(String(reason)); }
    finally { lock.current = false; setBusy(false); resolved(); }
  }
  return <section className="mb-5 space-y-3" data-testid="run-attention">
    <ResourceNotice error={resource.error} updatedAt={resource.updatedAt} onRetry={resource.refresh} />
    {error && <p role="alert" className="text-sm text-danger">{error}</p>}
    <fieldset disabled={busy || !!resource.error}>
      {!!interactions.length && <InteractionPanel items={interactions} onResolved={resolved} />}
      {approvals.map(card => <ApprovalRequestCard key={card.prompt?.approval_id || String(card.record?.id)} card={card} event={{ kind: "approval_request", approval_id: card.prompt?.approval_id || card.record?.id, record: card.record, prompt: card.prompt }} busy={busy || !!resource.error} onAction={callback => void act(callback)} />)}
    </fieldset>
    {resource.loading && !resource.data && <p role="status" className="text-xs text-[color:var(--text-muted)]">{t("loading")}</p>}
  </section>;
}
