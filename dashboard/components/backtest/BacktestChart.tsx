"use client";

import { useEffect, useState } from "react";
import { useTranslations } from "next-intl";
import { createChart, type IChartApi } from "lightweight-charts";
import {
  clientApi,
  type BacktestChartData,
  type BacktestPanel,
} from "../../lib/clientApi";
import { Card, Empty, ErrorBanner, Section } from "../Page";
import { JsonView } from "../JsonView";
import { SummaryCards } from "./SummaryCards";
import { BacktestTables } from "./BacktestTables";
import { useChartTheme } from "../../lib/chartTheme";

type BacktestSeries = BacktestPanel["series"][number];
type BacktestTable = BacktestChartData["tables"][number];

export function BacktestChart({
  strategyId,
  ts,
  proposalId,
}: {
  strategyId: string;
  ts: string;
  proposalId?: string | null;
}) {
  const t = useTranslations("strategyBacktests");
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
          setError(res.error || t("chartUnavailable"));
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
  if (!chart) return <Card title={t("chartTitle")}><Empty label={t("chartLoading")} /></Card>;

  const panels = chart.panels ?? [];
  const pricePanels = panels.filter(isPricePanel);
  const primaryPanel = buildEquityBenchmarkPanel(chart, panels, pricePanels);
  const primaryPanelId = primaryPanel?.id ?? null;
  const diagnosticPanels = panels.filter(
    (panel) => panel.id !== primaryPanelId && !isPricePanel(panel),
  );
  const tables = chart.tables ?? [];
  const tradeTables = tables.filter(isTradeTable);
  const diagnosticTables = tables.filter((table) => !isTradeTable(table));

  return (
    <div className="space-y-5">
      <SummaryCards cards={chart.summary_cards ?? []} />
      {primaryPanel ? (
        <Section
          title={t("equityBenchmarkTitle")}
          description={t("equityBenchmarkDescription")}
          divider={false}
        >
          <ChartPanel
            panel={primaryPanel}
            title={primaryPanel.title}
            height={390}
            featured
          />
        </Section>
      ) : null}
      {tradeTables.length > 0 ? (
        <Section
          title={t("tradeDetailsTitle")}
          description={t("tradeDetailsDescription")}
          divider={false}
        >
          <BacktestTables tables={tradeTables} compact maxHeightClass="max-h-[560px]" />
        </Section>
      ) : null}
      {pricePanels.length > 0 ? (
        <Section
          title={t("instrumentKlineTitle")}
          description={t("instrumentKlineDescription")}
          divider={false}
        >
          <div className="space-y-3">
            {pricePanels.map((panel) => (
              <ChartPanel key={panel.id} panel={panel} height={320} compact />
            ))}
          </div>
        </Section>
      ) : null}
      {diagnosticPanels.length > 0 || diagnosticTables.length > 0 ? (
        <Section
          title={t("diagnosticsTitle")}
          description={t("diagnosticsDescription")}
          divider={false}
        >
          <div className="space-y-3">
            {diagnosticPanels.map((panel) => (
              <ChartPanel key={panel.id} panel={panel} height={220} compact />
            ))}
            {diagnosticTables.length > 0 ? (
              <BacktestTables tables={diagnosticTables} compact maxHeightClass="max-h-[360px]" />
            ) : null}
          </div>
        </Section>
      ) : null}
    </div>
  );
}

function ChartPanel({
  panel,
  title,
  description,
  height,
  compact = false,
  featured = false,
}: {
  panel: BacktestPanel;
  title?: string;
  description?: string;
  height?: number;
  compact?: boolean;
  featured?: boolean;
}) {
  const [node, setNode] = useState<HTMLDivElement | null>(null);
  const chartTheme = useChartTheme();
  const resolvedHeight = height ?? (isPricePanel(panel) ? 320 : 220);

  useEffect(() => {
    if (!node) return;
    const api = createChart(node, {
      height: resolvedHeight,
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
  }, [chartTheme, node, panel, resolvedHeight]);

  if (panel.type === "overlay_spans") {
    return (
      <Card title={title ?? panel.title} description={description} featured={featured}>
        <div className="embedded-list-scroll max-h-64 space-y-2">
          {(panel.annotations ?? []).map((row, idx) => (
            <JsonView key={idx} value={row} showRawToggle={false} />
          ))}
        </div>
      </Card>
    );
  }

  return (
    <Card
      title={title ?? panel.title}
      description={description}
      actions={<SeriesLegend panel={panel} />}
      padded={false}
      featured={featured}
    >
      <div
        ref={setNode}
        className={`w-full ${compact ? "px-2 py-2" : "px-2 py-3"}`}
        style={{ minHeight: resolvedHeight }}
        data-testid="backtest-chart"
      />
    </Card>
  );
}

function renderSeries(api: IChartApi, panel: BacktestPanel) {
  let markerHost: { setMarkers(markers: never[]): void } | null = null;
  const markers: Array<Record<string, unknown>> = [];

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
      markerHost = s as unknown as { setMarkers(markers: never[]): void };
    } else if (series.kind === "area") {
      const s = api.addAreaSeries({
        lineColor: colorForSeries(panel, series, 0),
        topColor: areaTopColor(panel, series),
        bottomColor: areaBottomColor(panel, series),
      });
      s.setData(data as never);
      if (!markerHost) markerHost = s as unknown as { setMarkers(markers: never[]): void };
    } else if (series.kind === "line") {
      const s = api.addLineSeries({
        color: colorForSeries(panel, series, panel.series.indexOf(series)),
        lineWidth: 2,
        priceLineVisible: false,
      });
      s.setData(data as never);
      if (!markerHost) markerHost = s as unknown as { setMarkers(markers: never[]): void };
    } else if (series.kind === "markers") {
      markers.push(...data);
    }
  }
  if (markerHost && markers.length > 0) markerHost.setMarkers(markers as never[]);
  api.timeScale().fitContent();
}

function buildEquityBenchmarkPanel(
  chart: BacktestChartData,
  panels: BacktestPanel[],
  pricePanels: BacktestPanel[],
): BacktestPanel | null {
  const explicit = panels.find(isEquityBenchmarkPanel);
  const fallback = panels.find((panel) => !isPricePanel(panel) && hasRenderableSeries(panel));
  const panel = explicit ?? fallback;
  if (!panel) return null;
  if (panel.series.some(isBenchmarkSeries)) return panel;
  const benchmark = deriveBenchmarkSeries(chart, panel, pricePanels[0]);
  if (!benchmark) return panel;
  return {
    ...panel,
    title: panel.title || "Equity / Benchmark",
    series: [...panel.series, benchmark],
  };
}

function deriveBenchmarkSeries(
  chart: BacktestChartData,
  equityPanel: BacktestPanel,
  pricePanel?: BacktestPanel,
): BacktestSeries | null {
  const candleSeries = pricePanel?.series.find((series) => series.kind === "candles");
  const candles = normalizeSeriesData(candleSeries?.data ?? []);
  if (candles.length < 2) return null;
  const firstClose = firstNumericFromRows(candles, ["close", "value"]);
  if (!firstClose || firstClose <= 0) return null;

  const equitySeries = equityPanel.series.find(
    (series) => series.kind === "line" || series.kind === "area",
  );
  const equityRows = normalizeSeriesData(equitySeries?.data ?? []);
  const seed =
    firstNumericFromRows(equityRows, ["value", "equity", "nav", "balance"]) ??
    numericValue(chart.meta?.initial_capital_usd);
  if (!seed || seed <= 0) return null;

  const data = candles
    .map((row) => {
      const close = numericValue(row.close ?? row.value);
      if (!close || close <= 0) return null;
      return { time: row.time, value: (seed * close) / firstClose };
    })
    .filter((row): row is { time: unknown; value: number } => row != null);
  if (data.length < 2) return null;
  return { kind: "line", name: "benchmark", data: data as Array<Record<string, unknown>> };
}

function SeriesLegend({ panel }: { panel: BacktestPanel }) {
  const entries = (panel.series ?? [])
    .filter((series) => series.kind !== "markers" && (series.data ?? []).length > 0)
    .map((series, index) => ({
      name: formatSeriesName(series),
      color: colorForSeries(panel, series, index),
    }));
  if (entries.length <= 1) return null;
  return (
    <div className="flex max-w-full flex-wrap justify-end gap-1.5">
      {entries.map((entry) => (
        <span
          key={`${entry.name}-${entry.color}`}
          className="inline-flex items-center gap-1.5 rounded-md border border-[color:var(--line)] bg-ink-950/25 px-2 py-0.5 text-[11px] text-[color:var(--text-muted)]"
        >
          <span
            aria-hidden
            className="h-1.5 w-1.5 rounded-full"
            style={{ backgroundColor: entry.color }}
          />
          {entry.name}
        </span>
      ))}
    </div>
  );
}

function isEquityBenchmarkPanel(panel: BacktestPanel): boolean {
  if (isPricePanel(panel)) return false;
  return /(equity|benchmark|bench|b&h|buy.*hold|nav|capital)/i.test(panelText(panel));
}

function isPricePanel(panel: BacktestPanel): boolean {
  if ((panel.series ?? []).some((series) => series.kind === "candles")) return true;
  return /(price|ohlc|ohlcv|kline|k-line|candle)/i.test(panelText(panel));
}

function isTradeTable(table: BacktestTable): boolean {
  return /(trade|fill|order|execution)/i.test(`${table.id} ${table.columns.join(" ")}`);
}

function hasRenderableSeries(panel: BacktestPanel): boolean {
  return (panel.series ?? []).some(
    (series) => series.kind !== "markers" && (series.data ?? []).length > 0,
  );
}

function panelText(panel: BacktestPanel): string {
  return [
    panel.id,
    panel.type,
    panel.title,
    ...(panel.series ?? []).map((series) => `${series.kind} ${series.name ?? ""}`),
  ]
    .join(" ")
    .toLowerCase();
}

function isBenchmarkSeries(series: BacktestSeries): boolean {
  return /(benchmark|bench|b&h|buy.*hold)/i.test(series.name ?? "");
}

function formatSeriesName(series: BacktestSeries): string {
  if (series.name) return series.name.replace(/_/g, " ");
  if (series.kind === "candles") return "K line";
  return series.kind.replace(/_/g, " ");
}

function colorForSeries(panel: BacktestPanel, series: BacktestSeries, index: number): string {
  const text = `${panel.id} ${panel.title} ${series.name ?? ""} ${series.kind}`.toLowerCase();
  if (/benchmark|bench|b&h|buy.*hold/.test(text)) return "#38bdf8";
  if (/drawdown|missed/.test(text)) return "#ef4444";
  if (/rsi/.test(text)) return "#a78bfa";
  if (/equity|nav|capital/.test(text)) return "#22c55e";
  const palette = ["#22c55e", "#38bdf8", "#a78bfa", "#f59e0b", "#f472b6"];
  return palette[index % palette.length];
}

function areaTopColor(panel: BacktestPanel, series: BacktestSeries): string {
  const text = `${panel.id} ${panel.title} ${series.name ?? ""}`.toLowerCase();
  if (/drawdown|missed/.test(text)) return "rgba(239,68,68,.22)";
  return "rgba(34,197,94,.18)";
}

function areaBottomColor(panel: BacktestPanel, series: BacktestSeries): string {
  const text = `${panel.id} ${panel.title} ${series.name ?? ""}`.toLowerCase();
  if (/drawdown|missed/.test(text)) return "rgba(239,68,68,.02)";
  return "rgba(34,197,94,.02)";
}

function firstNumericFromRows(
  rows: Array<Record<string, unknown>>,
  keys: string[],
): number | null {
  for (const row of rows) {
    for (const key of keys) {
      const value = numericValue(row[key]);
      if (value != null) return value;
    }
  }
  return null;
}

function numericValue(value: unknown): number | null {
  if (typeof value === "number" && Number.isFinite(value)) return value;
  if (typeof value === "string" && value.trim()) {
    const parsed = Number(value);
    if (Number.isFinite(parsed)) return parsed;
  }
  return null;
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
