"use client";

import { useId, useState, type ReactNode } from "react";
import { useTranslations } from "next-intl";
import styles from "./RunWorkspace.module.css";

/** One inspection surface for modern and legacy runs. Supplied content is
 * recorded evidence; this component never reconstructs execution from a graph.
 */
export function RunWorkspace({ title, subtitle, status, actions, result, activity, provenance, onClose }: {
  title: string; subtitle?: ReactNode; status?: ReactNode; actions?: ReactNode;
  result: ReactNode; activity?: ReactNode; provenance?: ReactNode; onClose?: () => void;
}) {
  const t = useTranslations("workflowUpgrade");
  const [tab, setTab] = useState<"result" | "activity">("result");
  const id = useId();
  const tabs = ["result", ...(activity ? ["activity"] : [])] as const;
  return <section className={styles.workspace} data-testid="run-workspace">
    <header className={styles.header}>
      <div className={styles.heading}><h3>{title}</h3>{subtitle && <div className={styles.subtitle}>{subtitle}</div>}</div>
      {status && <div className={styles.status}>{status}</div>}
      {onClose && <button type="button" className="btn-ghost text-xs" onClick={onClose}>{t("close")}</button>}
    </header>
    {actions && <div className={styles.actions}>{actions}</div>}
    {activity && <div className={styles.tabs} role="tablist" aria-label={title} onKeyDown={event => {
      if (!["ArrowLeft", "ArrowRight", "Home", "End"].includes(event.key)) return;
      event.preventDefault();
      const next = event.key === "Home" ? "result" : event.key === "End" ? "activity" : tab === "result" ? "activity" : "result";
      setTab(next); document.getElementById(`${id}-${next}`)?.focus();
    }}>{tabs.map(value => <button type="button" id={`${id}-${value}`} aria-controls={`${id}-panel`} key={value} role="tab" tabIndex={tab === value ? 0 : -1} aria-selected={tab === value} onClick={() => setTab(value as typeof tab)}>{t(value)}</button>)}</div>}
    <div className={styles.content} id={`${id}-panel`} role={activity ? "tabpanel" : undefined} aria-labelledby={activity ? `${id}-${tab}` : undefined}>
      {tab === "activity" && activity ? activity : result}
    </div>
    {provenance && <details className={styles.provenance}><summary>{t("provenance")}</summary>{provenance}</details>}
  </section>;
}
