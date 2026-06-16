"use client";

import Link from "next/link";
import { useTranslations } from "next-intl";
import { BacktestChart } from "../../../../../components/backtest/BacktestChart";
import { Card, PageBody, PageHeader } from "../../../../../components/Page";
import { SectionTabs } from "../../../../../components/SectionTabs";

const FILES = [
  "report.md",
  "metrics.json",
  "trades.csv",
  "ohlcv_indicators_portfolio.csv",
  "analysis_by_reason.csv",
  "rejected_signals.csv",
  "config.yml",
  "chart.json",
];

export default function StrategyBacktestDetailPage({
  params,
}: {
  params: { id: string; ts: string };
}) {
  const strategyId = decodeURIComponent(params.id);
  const ts = decodeURIComponent(params.ts);
  const t = useTranslations("strategyBacktests");
  return (
    <div>
      <PageHeader
        title={ts}
        eyebrow={t("detailEyebrow", { id: strategyId })}
        description={t("detailDescription")}
        actions={
          <div className="flex items-center gap-2">
            <Link href={`/strategies/${encodeURIComponent(strategyId)}/backtests`} className="btn-ghost text-xs">
              {t("runsTitle")}
            </Link>
            <Link href={`/strategies/${encodeURIComponent(strategyId)}`} className="btn-ghost text-xs">
              {t("strategy")}
            </Link>
          </div>
        }
      />
      <SectionTabs section="strategy" />
      <PageBody>
        <BacktestChart strategyId={strategyId} ts={ts} />
        <Card title={t("artifactsTitle")}>
          <div className="flex flex-wrap gap-2">
            {FILES.map((name) => (
              <a
                key={name}
                href={`/api/proxy/strategy/backtests/file`}
                onClick={(event) => {
                  event.preventDefault();
                  void downloadArtifact(strategyId, ts, name);
                }}
                className="btn-ghost text-xs"
              >
                {name}
              </a>
            ))}
          </div>
        </Card>
      </PageBody>
    </div>
  );
}

async function downloadArtifact(strategyId: string, ts: string, name: string) {
  const { clientApi } = await import("../../../../../lib/clientApi");
  const res = await clientApi.strategyBacktestFile(strategyId, ts, name);
  const blob = new Blob([res.content], { type: "text/plain;charset=utf-8" });
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url;
  a.download = name;
  a.click();
  URL.revokeObjectURL(url);
}
