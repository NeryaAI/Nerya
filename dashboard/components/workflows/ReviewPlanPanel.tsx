"use client";

import { useState } from "react";
import { useTranslations } from "next-intl";
import type { WorkflowGraph } from "../../lib/workflowTypes";
import { asObject } from "../../lib/workflowPresentation";
import { ScheduleFields } from "./ScheduleFields";
import { useCadenceHint } from "../../lib/useStrategyLifecycle";
import styles from "./WorkflowForms.module.css";

export function ReviewPlanPanel({ graph, onChange, onAskAgent, disabled }: {
  graph: WorkflowGraph; onChange: (id: string, value: Record<string, unknown>) => void;
  onAskAgent: () => void; disabled: boolean;
}) {
  const t = useTranslations("workflowUpgrade");
  const cadenceHint = useCadenceHint();
  const [editing, setEditing] = useState(false);
  const proposal = asObject(graph.nodes.find(node => node.id === "proposal:tuning")?.config);
  const schedule = asObject(graph.nodes.find(node => node.id === "scheduler:tuning")?.config);
  const evidence = asObject(graph.nodes.find(node => node.id === "evidence:review")?.config);
  const guardrails = asObject(graph.nodes.find(node => node.id === "validation:tuning")?.config);
  const plan = asObject(proposal.review_plan), policy = asObject(proposal.proposal_policy);
  const enabled = proposal.enabled === true && schedule.enabled === true;
  function planField(key: string, value: string) { onChange("proposal:tuning", { ...proposal, review_plan: { ...plan, [key]: value } }); }
  const value = {
    cadence: schedule.type === "interval" ? "interval" as const : "cron" as const,
    cron: String(schedule.cron || "0 9 * * *"), everySeconds: String(schedule.every_seconds ?? 86400),
    timezone: String(schedule.timezone || "UTC"), runAt: "",
    startsAt: typeof schedule.starts_at === "string" ? schedule.starts_at : null,
    endsAt: typeof schedule.ends_at === "string" ? schedule.ends_at : null,
  };
  return <section className="mx-5 mt-4 border-b border-[color:var(--line)] pb-4" data-testid="review-plan-panel">
    <header className="flex flex-wrap items-center justify-between gap-3">
      <div><h3 className="text-sm font-semibold">{t("reviewPlan")}</h3><p className="mt-1 text-xs text-[color:var(--text-muted)]">{t(enabled ? "reviewEnabled" : "reviewDisabled")} · {cadenceHint(typeof schedule.cron === "string" ? schedule.cron : null, typeof schedule.every_seconds === "number" ? schedule.every_seconds : null) || String(schedule.cron || t("notSpecified"))} · {schedule.timezone ? String(schedule.timezone) : "UTC"}</p></div>
      <div className="flex flex-wrap gap-3"><button type="button" className="btn-ghost text-sm" aria-expanded={editing} onClick={() => setEditing(!editing)}>{t("editReviewPlan")}</button><button type="button" className="btn-ghost text-sm" disabled={disabled} onClick={onAskAgent}>{t("askReviewPlan")}</button></div>
    </header>
    <p className="mt-3 text-xs leading-6 text-[color:var(--text-muted)]">{t("planNotEnabled")}</p>
    {typeof plan.focus === "string" && plan.focus && <p className="mt-2 text-sm leading-7">{plan.focus}</p>}
    <div className="mt-3 flex flex-wrap gap-4 text-xs text-[color:var(--text-muted)]"><span>{t("reviewWindow")} · {evidence.max_age_hours === undefined ? t("notSpecified") : `${evidence.max_age_hours} h`}</span><span>{t("maxRuns")} · {String(evidence.runs ?? t("notSpecified"))}</span><span>{t("minSamples")} · {String(evidence.min_closed_trades ?? t("notSpecified"))}</span></div>
    {editing && <fieldset disabled={disabled} className="mt-4 space-y-4 border-t border-[color:var(--line)] pt-4">
      <div className={styles.grid}>
        {[ ["max_age_hours", "reviewWindow", 1], ["runs", "maxRuns", 1], ["min_closed_trades", "minSamples", 0] ].map(([key, label, minimum]) => <label key={key} className={styles.field}><span>{t(String(label))}{key === "max_age_hours" ? " (h)" : ""}</span><input className="input-dark" type="number" step={1} min={Number(minimum)} value={String(evidence[String(key)] ?? "")} onChange={event => onChange("evidence:review", { ...evidence, [key]: event.target.value === "" ? "" : Number(event.target.value) })} /></label>)}
      </div>
      <label className={styles.field}><span>{t("focus")}</span><textarea className="input-dark min-h-24" maxLength={12000} value={String(plan.focus || "")} onChange={event => planField("focus", event.target.value)} /></label>
      <label className={styles.field}><span>{t("validationPlan")}</span><textarea className="input-dark min-h-24" maxLength={12000} value={String(plan.validation_plan || "")} onChange={event => planField("validation_plan", event.target.value)} /></label>
      <label className={styles.field}><span>{t("nextReview")}</span><textarea className="input-dark min-h-24" maxLength={12000} value={String(plan.next_review || "")} onChange={event => planField("next_review", event.target.value)} /></label>
      <ScheduleFields value={value} allowOnce={false} showOverlap={false} disabled={disabled} onChange={patch => {
        const next = { ...value, ...patch };
        const updated: Record<string, unknown> = { ...schedule, type: next.cadence, timezone: next.timezone, enabled: schedule.enabled === true };
        if (next.cadence === "cron") { updated.cron = next.cron; delete updated.every_seconds; }
        else { updated.every_seconds = Number(next.everySeconds); delete updated.cron; }
        onChange("scheduler:tuning", updated);
      }} />
      <details className="text-sm"><summary>{t("protectedSettings")}</summary><div className="mt-3 space-y-3">
        <label className={styles.field}><span>{t("allowedChanges")}</span><textarea className="input-dark" value={Array.isArray(policy.allowed_targets) ? policy.allowed_targets.join("\n") : ""} onChange={event => onChange("proposal:tuning", { ...proposal, proposal_policy: { ...policy, allowed_targets: event.target.value.split("\n").map(item => item.trim()).filter(Boolean) } })} /></label>
        <p className="break-words text-xs text-[color:var(--text-muted)]">{Array.isArray(policy.forbidden_targets) ? policy.forbidden_targets.join(" · ") : t("notSpecified")}</p>
        <pre className="max-h-40 overflow-auto whitespace-pre-wrap text-xs text-[color:var(--text-muted)]">{JSON.stringify(guardrails, null, 2)}</pre>
      </div></details>
      <label className="flex items-start gap-3 text-sm"><input type="checkbox" className="mt-1" checked={enabled} onChange={event => {
        onChange("proposal:tuning", { ...proposal, enabled: event.target.checked });
        onChange("scheduler:tuning", { ...schedule, type: value.cadence, ...(value.cadence === "cron" ? { cron: value.cron } : { every_seconds: Number(value.everySeconds) }), enabled: event.target.checked });
      }} /><span>{t("enableExplicit")}<small className="mt-1 block text-[color:var(--text-muted)]">{t("keepsCurrentVersion")}</small></span></label>
    </fieldset>}
  </section>;
}
