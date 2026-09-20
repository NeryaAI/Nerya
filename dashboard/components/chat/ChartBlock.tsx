"use client";

import { useLocale } from "next-intl";
import { type ChartBlockShape, isChartBlockShape } from "../../lib/chartBlock";
import { useChartData } from "../../lib/useChartData";
import { FinancialChart } from "../finance/FinancialChart";
import { ChartPlaceholder } from "./ChartPlaceholder";

export function ChartBlock({ block }: { block: unknown }) {
  return isChartBlockShape(block) ? <ChartBlockCard block={block} /> : null;
}
function ChartBlockCard({ block: rawBlock }: { block: ChartBlockShape }) {
  const zh = useLocale().startsWith("zh");
  const { block, loading, error, ready } = useChartData(rawBlock);
  return <div className="my-3 min-w-0 border-y border-[color:var(--line)] py-2" data-testid="inline-financial-chart">
    {ready ? <FinancialChart key={block.chart_id} block={block} height={block.ui?.height ?? 260} /> : <ChartPlaceholder title={block.title || (zh ? "图表" : "Chart")} subtitle={loading ? (zh ? "正在加载图表数据…" : "Loading chart data…") : error ? `${zh ? "加载失败" : "Could not load"}: ${error}` : (zh ? "暂无图表数据" : "No chart data")} tone={loading ? "loading" : error ? "error" : "empty"} height={260} />}
  </div>;
}
