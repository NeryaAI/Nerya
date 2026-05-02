"use client";

import { useEffect, useState } from "react";
import { createChart, type IChartApi } from "lightweight-charts";
import {
  clientApi,
  type BacktestChartData,
  type BacktestPanel,
} from "../../lib/clientApi";
import { Card, Empty, ErrorBanner } from "../Page";
import { SummaryCards } from "./SummaryCards";
import { BacktestTables } from "./BacktestTables";

export function BacktestChart({
  strategyId,
  ts,
}: {
  strategyId: string;
  ts: string;
}) {
  const [chart, setChart] = useState<BacktestChartData | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    clientApi.strategyBacktestChart(strategyId, ts)
      .then((res) => {
        if (!cancelled) setChart(res.chart);
      })
      .catch((e) => {
        if (!cancelled) setError(e instanceof Error ? e.message : String(e));
      });
    return () => {
      cancelled = true;
    };
  }, [strategyId, ts]);

  if (error) return <ErrorBanner error={error} />;
  if (!chart) return <Card title="Backtest"><Empty label="Loading backtest chart..." /></Card>;

  return (
    <div className="space-y-4">
      <SummaryCards cards={chart.summary_cards ?? []} />
      <div className="space-y-4">
        {(chart.panels ?? []).map((panel) => (
          <ChartPanel key={panel.id} panel={panel} />
        ))}
      </div>
      <BacktestTables tables={chart.tables ?? []} />
    </div>
  );
}

function ChartPanel({ panel }: { panel: BacktestPanel }) {
  const [node, setNode] = useState<HTMLDivElement | null>(null);

  useEffect(() => {
    if (!node) return;
    const api = createChart(node, {
      height: panel.id === "price" ? 340 : 220,
      layout: { background: { color: "transparent" }, textColor: "#cbd5e1" },
      grid: { vertLines: { color: "rgba(148,163,184,.12)" }, horzLines: { color: "rgba(148,163,184,.12)" } },
      rightPriceScale: { borderColor: "rgba(148,163,184,.2)" },
      timeScale: { borderColor: "rgba(148,163,184,.2)", timeVisible: true },
    });
    renderSeries(api, panel);
    const ro = new ResizeObserver(() => {
      api.applyOptions({ width: Math.max(320, node.clientWidth) });
    });
    ro.observe(node);
    api.applyOptions({ width: Math.max(320, node.clientWidth) });
    return () => {
      ro.disconnect();
      api.remove();
    };
  }, [node, panel]);

  if (panel.type === "overlay_spans") {
    return (
      <Card title={panel.title}>
        <div className="embedded-list-scroll max-h-64 space-y-2">
          {(panel.annotations ?? []).map((row, idx) => (
            <div key={idx} className="rounded border border-white/5 bg-ink-900/50 p-3 text-xs text-ink-200 font-mono">
              {JSON.stringify(row)}
            </div>
          ))}
        </div>
      </Card>
    );
  }

  return (
    <Card title={panel.title} padded={false}>
      <div ref={setNode} className="w-full px-2 py-3" />
    </Card>
  );
}

function renderSeries(api: IChartApi, panel: BacktestPanel) {
  for (const series of panel.series ?? []) {
    if (series.kind === "candles") {
      const s = api.addCandlestickSeries({
        upColor: "#22c55e",
        downColor: "#ef4444",
        borderVisible: false,
        wickUpColor: "#22c55e",
        wickDownColor: "#ef4444",
      });
      s.setData(series.data as never);
    } else if (series.kind === "area") {
      const s = api.addAreaSeries({
        lineColor: "#ef4444",
        topColor: "rgba(239,68,68,.22)",
        bottomColor: "rgba(239,68,68,.02)",
      });
      s.setData(series.data as never);
    } else if (series.kind === "line") {
      const s = api.addLineSeries({ color: panel.id === "rsi" ? "#38bdf8" : "#22c55e", lineWidth: 2 });
      s.setData(series.data as never);
    } else if (series.kind === "markers") {
      // Markers attach to the first candlestick series; skipped when
      // no price series exists because lightweight-charts requires a series host.
    }
  }
  api.timeScale().fitContent();
}

