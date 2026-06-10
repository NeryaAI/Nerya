"use client";

import { useEffect, useState } from "react";
import { createChart, type IChartApi } from "lightweight-charts";
import {
  clientApi,
  type BacktestChartData,
  type BacktestPanel,
} from "../../lib/clientApi";
import { Card, Empty, ErrorBanner } from "../Page";
import { JsonView } from "../JsonView";
import { SummaryCards } from "./SummaryCards";
import { BacktestTables } from "./BacktestTables";
import { useChartTheme } from "../../lib/chartTheme";

export function BacktestChart({
  strategyId,
  ts,
  proposalId,
}: {
  strategyId: string;
  ts: string;
  proposalId?: string | null;
}) {
  const [chart, setChart] = useState<BacktestChartData | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    setChart(null);
    setError(null);
    clientApi.strategyBacktestChart(strategyId, ts, proposalId)
      .then((res) => {
        if (cancelled) return;
        if (res.ok === false || !res.chart) {
          setError(res.error || "Backtest chart is unavailable for this run.");
          return;
        }
        setChart(res.chart);
      })
      .catch((e) => {
        if (!cancelled) setError(e instanceof Error ? e.message : String(e));
      });
    return () => {
      cancelled = true;
    };
  }, [strategyId, ts, proposalId]);

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
  const chartTheme = useChartTheme();

  useEffect(() => {
    if (!node) return;
    const api = createChart(node, {
      height: panel.id === "price" ? 340 : 220,
      layout: {
        background: { color: "transparent" },
        textColor: chartTheme.text,
        attributionLogo: false,
      },
      grid: { vertLines: { color: chartTheme.grid }, horzLines: { color: chartTheme.grid } },
      rightPriceScale: { borderColor: chartTheme.grid },
      timeScale: { borderColor: chartTheme.grid, timeVisible: true },
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
  }, [chartTheme, node, panel]);

  if (panel.type === "overlay_spans") {
    return (
      <Card title={panel.title}>
        <div className="embedded-list-scroll max-h-64 space-y-2">
          {(panel.annotations ?? []).map((row, idx) => (
            <JsonView key={idx} value={row} showRawToggle={false} />
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
    const data = normalizeSeriesData(series.data ?? []);
    if (!data.length) continue;
    if (series.kind === "candles") {
      const s = api.addCandlestickSeries({
        upColor: "#22c55e",
        downColor: "#ef4444",
        borderVisible: false,
        wickUpColor: "#22c55e",
        wickDownColor: "#ef4444",
      });
      s.setData(data as never);
    } else if (series.kind === "area") {
      const s = api.addAreaSeries({
        lineColor: "#ef4444",
        topColor: "rgba(239,68,68,.22)",
        bottomColor: "rgba(239,68,68,.02)",
      });
      s.setData(data as never);
    } else if (series.kind === "line") {
      const s = api.addLineSeries({ color: panel.id === "rsi" ? "#38bdf8" : "#22c55e", lineWidth: 2 });
      s.setData(data as never);
    } else if (series.kind === "markers") {
      // Markers attach to the first candlestick series; skipped when
      // no price series exists because lightweight-charts requires a series host.
    }
  }
  api.timeScale().fitContent();
}

function normalizeSeriesData(
  rows: Array<Record<string, unknown>>,
): Array<Record<string, unknown>> {
  const byTime = new Map<string, Record<string, unknown>>();
  for (const row of rows) {
    const key = timeKey(row.time);
    if (!key) continue;
    byTime.set(key, row);
  }
  return Array.from(byTime.entries())
    .sort((a, b) => compareTimeKeys(a[0], b[0]))
    .map(([, row]) => row);
}

function timeKey(value: unknown): string {
  if (typeof value === "number" && Number.isFinite(value)) return String(value);
  if (typeof value === "string" && value.trim()) return value.trim();
  if (value && typeof value === "object") {
    try {
      return JSON.stringify(value);
    } catch {
      return "";
    }
  }
  return "";
}

function compareTimeKeys(a: string, b: string): number {
  const na = Number(a);
  const nb = Number(b);
  if (Number.isFinite(na) && Number.isFinite(nb)) return na - nb;
  return a.localeCompare(b);
}
