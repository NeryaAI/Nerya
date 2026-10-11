"use client";
import { copy as i18nCopy } from "../../lib/i18n";

import { useEffect, useId, useState } from "react";
import { useTranslations, useLocale } from "next-intl";
import { WorkspaceTabs } from "../chat/WorkspaceTabs";
import { createVelaChart, type VelaSeries, type VelaMarker } from "../../lib/velaChart";
import {
  clientApi,
  type BacktestChartData,
  type BacktestPanel,
} from "../../lib/clientApi";
import { Card, Empty, ErrorBanner, Section } from "../Page";
import { JsonView } from "../JsonView";
import { SummaryCards } from "./SummaryCards";
import { BacktestTables } from "./BacktestTables";
import { BacktestExecutionEvidence } from "./BacktestExecutionEvidence";
import { BacktestCoverage } from "./BacktestCoverage";
import { BacktestBiasChecks } from "./BacktestBiasChecks";
import { BacktestReviewAction } from "./BacktestReviewAction";
import { BacktestFactors } from "./BacktestFactors";
import { BacktestMarketExplorer } from "./BacktestMarketExplorer";
import { replayTime } from "../../lib/backtestMarket";
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
  const t = useTranslations("strategyBacktests"), zh = useLocale().startsWith("zh");
  const id = `backtest-${useId().replace(/:/g, "")}`;
  const [view, setView] = useState("overview");
  useEffect(() => setView("overview"), [strategyId, ts, proposalId]);
  const [chart, setChart] = useState<BacktestChartData | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [retry, setRetry] = useState(0);

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
        if (res.strategy_id !== strategyId || res.ts !== ts || (res.proposal_id || null) !== (proposalId || null)) {
          setError(i18nCopy(zh, "copy.components_backtest_BacktestChart.009"));
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
  }, [strategyId, ts, proposalId, retry]);

  if (error) return <div className="space-y-3" role="status"><ErrorBanner error={error} /><button type="button" className="btn btn-ghost" onClick={() => setRetry(value => value + 1)}>{i18nCopy(zh, "copy.components_backtest_BacktestChart.010")}</button></div>;
  if (!chart) return <Card title={t("chartTitle")}><Empty label={t("chartLoading")} /></Card>;
  if (chart.meta.performance_evidence === false || chart.meta.evaluation_mode === "observation") {
    const replay = (chart.meta.replay || {}) as Record<string, unknown>;
    const reasons = (replay.reason_counts || {}) as Record<string, unknown>;
    const decisions = (chart.tables || []).filter(table => table.id === "decisions");
    return <div className="min-w-0 space-y-5" data-testid="backtest-report" data-report-kind="observation">
      <BacktestCoverage meta={chart.meta}/>
      <BacktestBiasChecks compact meta={chart.meta}/>
      <WorkspaceTabs id={id} label={i18nCopy(zh, "copy.components_backtest_BacktestChart.011")} value={view} onChange={setView} tabs={[{ id: "overview", label: i18nCopy(zh, "copy.components_backtest_BacktestChart.012") }, { id: "trades", label: i18nCopy(zh, "copy.components_backtest_BacktestChart.013") }]}/>
      <section role="tabpanel" id={`${id}-panel-overview`} aria-labelledby={`${id}-tab-overview`} hidden={view !== "overview"} className={view === "overview" ? "space-y-5" : "hidden"}>
      <div><h4 className="text-sm font-semibold">{i18nCopy(zh, "copy.components_backtest_BacktestChart.014")}</h4>
        <p className="mt-2 text-xs leading-6 text-[color:var(--text-muted)]">{i18nCopy(zh, "copy.components_backtest_BacktestChart.015")}</p></div>
      <dl className="grid grid-cols-2 gap-4 sm:grid-cols-4">{[["decisions",i18nCopy(zh, "copy.components_backtest_BacktestChart.016")],["dispatches",i18nCopy(zh, "copy.components_backtest_BacktestChart.017")],["skipped",i18nCopy(zh, "copy.components_backtest_BacktestChart.018")],["errors",i18nCopy(zh, "copy.components_backtest_BacktestChart.019")]].map(([key,label]) => <div key={key}><dt className="text-xs text-[color:var(--text-muted)]">{label}</dt><dd className="mt-1 text-lg font-semibold tabular-nums">{typeof replay[key] === "number" ? String(replay[key]) : "—"}</dd></div>)}</dl>
      {Object.keys(reasons).length > 0 && <section><h5 className="mb-2 text-xs font-medium">{i18nCopy(zh, "copy.components_backtest_BacktestChart.020")}</h5><dl className="space-y-2 text-xs">{Object.entries(reasons).map(([reason,count])=><div key={reason} className="flex justify-between gap-4 border-b border-[color:var(--line)] pb-2"><dt className="break-words">{reason}</dt><dd className="tabular-nums">{String(count)}</dd></div>)}</dl></section>}
      <p className="text-xs text-[color:var(--text-muted)]">{i18nCopy(zh, "copy.components_backtest_BacktestChart.021")}</p>
      <BacktestTables tables={decisions} compact />
      </section>
      <section role="tabpanel" id={`${id}-panel-trades`} aria-labelledby={`${id}-tab-trades`} hidden={view !== "trades"} className={view === "trades" ? "space-y-4" : "hidden"}>
        <p className="text-xs text-warn">{i18nCopy(zh, "copy.components_backtest_BacktestChart.022")}</p>
        {view === "trades" && <BacktestMarketExplorer panels={(chart.panels || []).filter(isPricePanel)} tables={[]} meta={chart.meta}/>}
      </section>
    </div>;
  }

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
    <div className="min-w-0 space-y-4" data-testid="backtest-report">
      <BacktestCoverage meta={chart.meta}/>
      <BacktestBiasChecks compact meta={chart.meta}/>
      <BacktestExecutionEvidence replay={(chart.meta.replay || {}) as Record<string, unknown>} legacyBenchmark={chart.meta.engine_version === "backtest_skill_v2_closed_bar" && Array.isArray(chart.meta.markets) && chart.meta.markets.length > 1}/>
      <div className="flex flex-wrap items-center justify-between gap-2 text-xs text-[color:var(--text-muted)]"><span className="font-medium text-warn">{i18nCopy(zh, "copy.components_backtest_BacktestChart.001")}</span><span>{i18nCopy(zh, "copy.components_backtest_BacktestChart.002")}</span></div>
      <WorkspaceTabs id={id} label={i18nCopy(zh, "copy.components_backtest_BacktestChart.003")} value={view} onChange={setView} tabs={[{ id: "overview", label: i18nCopy(zh, "copy.components_backtest_BacktestChart.023") }, { id: "trades", label: i18nCopy(zh, "copy.components_backtest_BacktestChart.024") }, { id: "factors", label: i18nCopy(zh, "copy.components_backtest_BacktestChart.025") }, { id: "diagnostics", label: i18nCopy(zh, "copy.components_backtest_BacktestChart.026") }]} />
      <section role="tabpanel" id={`${id}-panel-factors`} aria-labelledby={`${id}-tab-factors`} hidden={view !== "factors"} className={view === "factors" ? "space-y-5" : "hidden"}>
        {view === "factors" && <BacktestFactors strategyId={strategyId} ts={ts} proposalId={proposalId}/>}
      </section>
      <section role="tabpanel" id={`${id}-panel-overview`} aria-labelledby={`${id}-tab-overview`} hidden={view !== "overview"} className={view === "overview" ? "space-y-5" : "hidden"}>
      <SummaryCards cards={chart.summary_cards ?? []} />
      {primaryPanel && view === "overview" ? (
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
      </section>
      <section role="tabpanel" id={`${id}-panel-trades`} aria-labelledby={`${id}-tab-trades`} hidden={view !== "trades"} className={view === "trades" ? "space-y-5" : "hidden"}>
      {view === "trades" && <BacktestMarketExplorer panels={pricePanels} tables={tradeTables} meta={chart.meta}/>}
      </section>
      <section role="tabpanel" id={`${id}-panel-diagnostics`} aria-labelledby={`${id}-tab-diagnostics`} hidden={view !== "diagnostics"} className={view === "diagnostics" ? "space-y-5" : "hidden"}>
      {view === "diagnostics" && <><BacktestBiasChecks meta={chart.meta}/><BacktestReviewAction target={{strategyId,ts,proposalId}}/></>}
      {!diagnosticPanels.length && !diagnosticTables.length ? <Empty label={i18nCopy(zh, "copy.components_backtest_BacktestChart.008")} /> : null}
      {diagnosticPanels.length > 0 || diagnosticTables.length > 0 ? (
        <Section
          title={t("diagnosticsTitle")}
          description={t("diagnosticsDescription")}
          divider={false}
        >
          <div className="space-y-3">
            {view === "diagnostics" && diagnosticPanels.map((panel) => (
              <ChartPanel key={panel.id} panel={panel} height={220} compact />
            ))}
            {diagnosticTables.length > 0 ? (
              <BacktestTables tables={diagnosticTables} compact maxHeightClass="max-h-[360px]" />
            ) : null}
          </div>
        </Section>
      ) : null}
      </section>
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
    const series: VelaSeries[] = (panel.series || []).flatMap((row, index) => {
      if (row.kind === "markers") return [];
      return [{ type: row.kind === "candles" ? "candlestick" : row.kind === "area" ? "area" : "line", name: row.name || String(index),
        color: colorForSeries(panel, row, index), top_color: areaTopColor(panel, row), bottom_color: areaBottomColor(panel, row),
        data: normalizeSeriesData(row.data || []) as VelaSeries["data"] }];
    });
    const markers = (panel.series || []).filter(row => row.kind === "markers").flatMap(row => (row.data || []).flatMap(point => {
      const time = replayTime(point.time); return time === null ? [] : [{ ...point, time } as VelaMarker];
    }));
    const chart = createVelaChart(node, { height: resolvedHeight, theme: chartTheme, series, markers, embedded: true });
    return () => chart.destroy();
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
        className={`relative isolate w-full overflow-hidden ${compact ? "px-2 py-2" : "px-2 py-3"}`}
        style={{ minHeight: resolvedHeight }}
        data-testid="backtest-chart"
      />
    </Card>
  );
}

// Chart colors resolve to the console's design tokens instead of
// ad-hoc hex greens/reds: ok/accent mint for positive series, danger
// for negative/risk series, fluid cyan for neutral/benchmark lines,
// brand violet for indicator lines.
const CHART_OK = "#10d993"; // --ok / accent-500
const CHART_DANGER = "#ef4560"; // --err / danger
const CHART_FLUID = "#22d3ee"; // --fluid
const CHART_BRAND = "#a78bfa"; // brand-400
const CHART_WARN = "#f5a524"; // --warn
const CHART_MAGENTA = "#f472b6"; // magenta-400

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
  // A multi-market benchmark must be recorded by the engine, not guessed
  // from whichever instrument happens to be the first price panel.
  if (pricePanels.length !== 1) return panel;
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
  if (["order_events", "rejected_signals"].includes(table.id)) return false;
  return /(trade|fill|order|execution)/i.test(table.id) && table.columns.includes("side") && table.columns.includes("price");
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
  const name = String(series.name || "").toLowerCase();
  if (/benchmark|bench|b&h|buy.*hold/.test(name)) return CHART_FLUID;
  if (/equity|nav|capital/.test(name)) return CHART_OK;
  const text = `${panel.id} ${panel.title} ${series.name ?? ""} ${series.kind}`.toLowerCase();
  if (/benchmark|bench|b&h|buy.*hold/.test(text)) return CHART_FLUID;
  if (/drawdown|missed/.test(text)) return CHART_DANGER;
  if (/rsi/.test(text)) return CHART_BRAND;
  if (/equity|nav|capital/.test(text)) return CHART_OK;
  const palette = [CHART_OK, CHART_FLUID, CHART_BRAND, CHART_WARN, CHART_MAGENTA];
  return palette[index % palette.length];
}

function areaTopColor(panel: BacktestPanel, series: BacktestSeries): string {
  const text = `${panel.id} ${panel.title} ${series.name ?? ""}`.toLowerCase();
  if (/drawdown|missed/.test(text)) return "rgba(239,69,96,.22)";
  return "rgba(16,217,147,.18)";
}

function areaBottomColor(panel: BacktestPanel, series: BacktestSeries): string {
  const text = `${panel.id} ${panel.title} ${series.name ?? ""}`.toLowerCase();
  if (/drawdown|missed/.test(text)) return "rgba(239,69,96,.02)";
  return "rgba(16,217,147,.02)";
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
