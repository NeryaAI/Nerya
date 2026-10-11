"use client";

import { useLocale, useTranslations } from "next-intl";

export function ResourceNotice({ error, updatedAt, onRetry }: {
  error?: string; updatedAt?: number; onRetry: () => void;
}) {
  const t = useTranslations("workflowUpgrade");
  const locale = useLocale();
  if (!error) return null;
  return <div role="status" className="flex flex-wrap items-center gap-3 rounded-lg border border-[color:var(--line)] px-4 py-3 text-sm" data-testid="resource-read-error">
    <span className="text-warn">{t(updatedAt ? "staleData" : "readFailed", {
      time: updatedAt ? new Date(updatedAt).toLocaleTimeString(locale) : "",
    })}</span>
    <button type="button" className="underline underline-offset-4" onClick={onRetry}>{t("retry")}</button>
    <details className="w-full text-xs text-[color:var(--text-muted)]"><summary>{t("diagnostics")}</summary><p className="mt-2 break-words">{error}</p></details>
  </div>;
}
