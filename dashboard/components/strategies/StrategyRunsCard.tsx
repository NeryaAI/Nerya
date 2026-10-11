"use client";
import { copy as i18nCopy } from "../../lib/i18n";

import Link from "next/link";
import {useLocale} from "next-intl";

import { useState } from "react";
import { useTranslations } from "next-intl";

import { Card, Empty, Json, Pill } from "../Page";
import type { StrategyRunRecord } from "../../lib/strategyTypes";

interface Props {
  runs: StrategyRunRecord[];
  total: number;
}

const TONES: Record<
  StrategyRunRecord["status"] | string,
  "ok" | "warn" | "danger" | "neutral" | "brand"
> = {
  submitted: "ok",
  ok: "ok",
  hold: "warn",
  error: "danger",
};

/**
 * Recent strategy runs table with an inline drawer for the run audit
 * trail. Reads from the workspace envelope (no extra fetch).
 */
export function StrategyRunsCard({ runs, total }: Props) {
  const t = useTranslations("strategyRuns");
  const zh=useLocale().startsWith("zh");
  const [openId, setOpenId] = useState<string | null>(null);

  if (!runs.length) {
    return (
      <Card title={t("title")} description={t("description")}>
        <Empty label={t("noRuns")} />
      </Card>
    );
  }

  return (
    <Card
      title={t("titleWithCount", { shown: runs.length, total })}
      description={t("rowDescription")}
    >
      <ul className="embedded-list-scroll-lg divide-y divide-brand-500/10 text-xs">
        {runs.map((run) => (
          <li key={run.run_id} className="py-2">
            <button
              onClick={() => setOpenId((cur) => (cur === run.run_id ? null : run.run_id))}
              className="w-full text-left"
            >
              <div className="flex items-center gap-2 flex-wrap">
                <Pill tone={TONES[run.status] ?? "neutral"}>{run.status}</Pill>
                <span className="font-mono text-ink-300 truncate max-w-[180px]">
                  {run.run_id}
                </span>
                <span className="text-ink-500">
                  {new Date(run.finished_at || run.started_at).toLocaleString()}
                </span>
                <span className="text-ink-500">{run.duration_ms} ms</span>
                <Pill tone="brand">{run.mode}</Pill>
              </div>
              {run.reason && (
                <div className="mt-0.5 text-[11px] text-ink-400 truncate">
                  {run.reason}
                </div>
              )}
            </button>
            {openId === run.run_id && (
              <div className="mt-2 space-y-2">
                <div className="flex flex-wrap items-center gap-3 text-xs"><span>{i18nCopy(zh, "copy.components_strategies_StrategyRunsCard.001")}<code>{run.package_hash}</code></span>
                  {run.session_id&&<Link className="min-h-11 py-3 underline" href={"/chat/"+encodeURIComponent(run.session_id)}>{i18nCopy(zh, "copy.components_strategies_StrategyRunsCard.002")}</Link>}
                  <Link className="min-h-11 py-3 underline" href={"/orders?strategy="+encodeURIComponent(run.strategy_id)}>{i18nCopy(zh, "copy.components_strategies_StrategyRunsCard.003")}</Link>
                </div>
                {run.status==="submitted"&&<p className="text-warn">{i18nCopy(zh, "copy.components_strategies_StrategyRunsCard.004")}</p>}
                {run.outputs && Object.keys(run.outputs).length > 0 && (
                  <Json value={run.outputs} />
                )}
                {run.audit && run.audit.length > 0 && (
                  <details>
                    <summary className="text-[11px] text-ink-400 cursor-pointer">
                      {t("auditEntries", { n: run.audit.length })}
                    </summary>
                    <Json value={run.audit} />
                  </details>
                )}
                {run.error && (
                  <div className="rounded border border-danger/40 bg-danger/10 px-2 py-1 text-[11px] text-danger">
                    {run.error.kind ? `${run.error.kind}: ` : ""}
                    {run.error.message ?? t("error")}
                  </div>
                )}
              </div>
            )}
          </li>
        ))}
      </ul>
    </Card>
  );
}
