"use client";

import Link from "next/link";
import { useEffect, useState } from "react";
import { useTranslations } from "next-intl";
import {
  clientApi,
  type BacktestRunSummary,
} from "../../../../lib/clientApi";
import { Card, Empty, ErrorBanner, PageBody, PageHeader, Pill } from "../../../../components/Page";
import { SectionTabs } from "../../../../components/SectionTabs";

export default function StrategyBacktestsPage({
  params,
}: {
  params: { id: string };
}) {
  const strategyId = decodeURIComponent(params.id);
  const t = useTranslations("strategyBacktests");
  const [runs, setRuns] = useState<BacktestRunSummary[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  async function refresh() {
    setLoading(true);
    setError(null);
    try {
      const res = await clientApi.strategyBacktests(strategyId);
      setRuns(res.backtests ?? []);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setLoading(false);
    }
  }

  useEffect(() => {
    void refresh();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [strategyId]);

  return (
    <div>
      <PageHeader
        title={t("title")}
        eyebrow={strategyId}
        description={t("description")}
        actions={
          <div className="flex items-center gap-2">
            <Link href={`/strategies/${encodeURIComponent(strategyId)}`} className="btn-ghost text-xs">
              {t("strategy")}
            </Link>
            <button onClick={() => void refresh()} disabled={loading} className="btn-ghost text-xs">
              {loading ? t("refreshing") : t("refresh")}
            </button>
          </div>
        }
      />
      <SectionTabs section="strategy" />
      <PageBody>
        {error && <ErrorBanner error={error} />}
        <Card title={t("runsTitle")}>
          {runs.length === 0 ? (
            <Empty label={loading ? t("loading") : t("empty")} />
          ) : (
            <div className="embedded-table-scroll max-h-[620px] overflow-auto">
              <table className="w-full text-xs">
                <thead className="sticky top-0 bg-ink-950">
                  <tr>
                    <th className="text-left py-2 pr-3">{t("colRun")}</th>
                    <th className="text-left py-2 pr-3">{t("colVerdict")}</th>
                    <th className="text-right py-2 pr-3">{t("colReturn")}</th>
                    <th className="text-right py-2 pr-3">{t("colMaxDd")}</th>
                    <th className="text-right py-2 pr-3">{t("colSharpe")}</th>
                    <th className="text-left py-2 pr-3">{t("colWindow")}</th>
                  </tr>
                </thead>
                <tbody>
                  {runs.map((run) => (
                    <tr key={run.ts} className="border-t border-brand-500/10">
                      <td className="py-2 pr-3 font-mono">
                        <Link className="text-brand-300 hover:text-brand-200" href={`/strategies/${encodeURIComponent(strategyId)}/backtests/${encodeURIComponent(run.ts)}`}>
                          {run.ts}
                        </Link>
                      </td>
                      <td className="py-2 pr-3"><Verdict value={run.verdict} t={t} /></td>
                      <td className="py-2 pr-3 text-right font-mono">{fmt(run.total_return_pct)}%</td>
                      <td className="py-2 pr-3 text-right font-mono">{fmt(run.max_dd_pct)}%</td>
                      <td className="py-2 pr-3 text-right font-mono">{fmt(run.sharpe_ratio)}</td>
                      <td className="py-2 pr-3 text-ink-300">
                        {run.start_utc} {"->"} {run.end_utc}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </Card>
      </PageBody>
    </div>
  );
}

function Verdict({ value, t }: { value?: string; t: ReturnType<typeof useTranslations<"strategyBacktests">> }) {
  const tone = value === "PASS" ? "ok" : value === "WARN" ? "warn" : value === "FAIL" ? "danger" : "neutral";
  return <Pill tone={tone}>{value || t("unknown")}</Pill>;
}

function fmt(value: unknown): string {
  return typeof value === "number" && Number.isFinite(value) ? value.toFixed(2) : "null";
}
