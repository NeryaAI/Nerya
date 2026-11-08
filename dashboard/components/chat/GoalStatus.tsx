"use client";

import { useId, useRef, useState } from "react";
import { useLocale, useTranslations } from "next-intl";
import { goalAction, goalControlErrorKey, goalReasonKeys, type ConversationGoal, type GoalAction } from "../../lib/conversationGoal";
import styles from "./GoalStatus.module.css";

export function GoalStatus({ goal, connection, onControl }: {
  goal: ConversationGoal;
  connection: string;
  onControl: (action: GoalAction, goal: ConversationGoal) => Promise<unknown>;
}) {
  const t = useTranslations("conversationGoal"), locale = useLocale(), helpId = useId();
  const [busy, setBusy] = useState(false), [error, setError] = useState<string | null>(null);
  const inFlight = useRef(false);
  const action = goalAction(goal);
  const online = connection === "online";
  const format = (value: number) => value.toLocaleString(locale, { maximumFractionDigits: 0 });
  const reasonKey = goal.stop_reason && Object.hasOwn(goalReasonKeys, goal.stop_reason) ? goalReasonKeys[goal.stop_reason] : null;
  const reason = reasonKey ? t("reasons." + reasonKey) : goal.stop_reason?.replace(/_/g, " ");

  async function control() {
    if (!action || !online || inFlight.current) return;
    inFlight.current = true; setBusy(true); setError(null);
    try { await onControl(action, goal); }
    catch (error) { setError(goalControlErrorKey(error)); }
    finally { inFlight.current = false; setBusy(false); }
  }

  return <section className={styles.root} aria-label={t("label")} aria-busy={busy} data-testid="conversation-goal" data-state={goal.status}>
    <div className={styles.heading}>
      <span className={styles.label}>{t("label")}</span>
      <span className={styles.status} role="status" aria-live="polite">{t(goal.status === "active" && goal.stop_reason === "awaiting_child_task" ? "waitingForChild" : "states." + goal.status)}</span>
      {action && <button type="button" className={styles.control} onClick={control} disabled={busy || !online} aria-describedby={helpId}>
        {busy ? t("updating") : t(action)}
      </button>}
    </div>
    <details className={styles.objective}>
      <summary title={goal.objective}>{goal.objective}</summary>
      <p>{goal.objective}</p>
    </details>
    <div className={styles.metrics}>
      <span>{t("elapsed", { seconds: format(goal.time_used_seconds) })}</span>
      {goal.token_budget !== null ? <>
        <span>{t("budget", { used: format(goal.tokens_used), budget: format(goal.token_budget) })}</span>
        {goal.remaining_tokens !== null && <span>{t("remaining", { count: format(goal.remaining_tokens) })}</span>}
      </> : goal.tokens_used > 0 ? <span>{t("used", { count: format(goal.tokens_used) })}</span> : null}
    </div>
    {reason && <p className={styles.note}>{reason}</p>}
    {goal.completion_requested && goal.status !== "complete" && <p className={styles.note}>{t("completionRequested")}</p>}
    {action && <p className={styles.note} id={helpId}>{t(action === "pause" ? "pauseHelp" : "resumeHelp")}</p>}
    {!online && <p className={styles.note} role="status">{t("offline")}</p>}
    {error && <p className={styles.error} role="alert">{t("errors." + error)}</p>}
  </section>;
}
