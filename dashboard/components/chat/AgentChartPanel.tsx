"use client";

import { useEffect, useMemo, useRef, useState, type ReactNode } from "react";
import { useTranslations } from "next-intl";
import {
  ColorType,
  CrosshairMode,
  LineStyle,
  createChart,
  type IChartApi,
  type Time,
  type UTCTimestamp,
} from "lightweight-charts";

import { clientApi } from "../../lib/clientApi";
import type { Candle } from "../../lib/api";
import type {
  ChartBlockShape,
  ChartSeries,
  ChartSeriesPoint,
  OHLCV,
  TimeValue,
} from "../../lib/chartBlock";
import { isChartBlockShape } from "../../lib/chartBlock";
import {
  liveEventsToBlocks,
  type ChatThread,
  type NativeBlock,
  type NativeBlockEnvelope,
} from "../../lib/chat";
import { useChartData } from "../../lib/useChartData";
import { BacktestChart } from "../backtest/BacktestChart";
import {
  ChartIcon,
  ChevronDownIcon,
  RefreshIcon,
} from "../icons";
import { ChartCanvas } from "./ChartCanvas";
import { ChartPlaceholder } from "./ChartPlaceholder";

type AgentVisual = {
  id: string;
  block: ChartBlockShape;
  seenAt: number;
  source: string;
};

type BacktestRef = {
  id: string;
  strategyId: string;
  proposalId?: string | null;
  ts: string;
  title: string;
  seenAt: number;
};

type VisualContext = {
  charts: AgentVisual[];
  backtests: BacktestRef[];
};

type IndicatorKey =
  | "volume"
  | "ma20"
  | "ema50"
  | "bb20"
  | "vwap"
  | "rsi"
  | "macd"
  | "agent";

type IndicatorState = Record<IndicatorKey, boolean>;

type CandlePoint = {
  time: number | string;
  open: number;
  high: number;
  low: number;
  close: number;
  volume?: number;
};

type SeriesLine = {
  name: string;
  data: TimeValue[];
  color: string;
  style?: "solid" | "dashed" | "dotted";
  width?: 1 | 2 | 3;
  histogram?: boolean;
};

const INTERVALS = ["1m", "5m", "15m", "1h", "4h", "1d"];
const BAR_COUNTS = [60, 120, 240, 500, 1000];
const DEFAULT_INDICATORS: IndicatorState = {
  volume: true,
  ma20: true,
  ema50: true,
  bb20: true,
  vwap: false,
  rsi: true,
  macd: true,
  agent: true,
};

const THEME = {
  background: "transparent",
  text: "#a7b0c3",
  grid: "rgba(255,255,255,0.045)",
  border: "rgba(180,139,255,0.18)",
  up: "#10d993",
  down: "#ef4560",
  ma: "#f5a524",
  ema: "#22d3ee",
  band: "#b48bff",
  vwap: "#f472b6",
  rsi: "#38bdf8",
  macd: "#22c55e",
  signal: "#f59e0b",
  histogramUp: "rgba(16,217,147,0.42)",
  histogramDown: "rgba(239,69,96,0.42)",
};

export function hasAgentVisuals(thread: ChatThread | null | undefined): boolean {
  const ctx = collectAgentVisualContext(thread);
  return ctx.charts.length > 0 || ctx.backtests.length > 0;
}

export function AgentChartPanel({
  thread,
  embedded = false,
}: {
  thread: ChatThread | null;
  embedded?: boolean;
}) {
  const t = useTranslations("chatChartPanel");
  const ctx = useMemo(() => collectAgentVisualContext(thread), [thread]);
  const [view, setView] = useState<"charts" | "backtests">("charts");
  const [activeChartId, setActiveChartId] = useState("");
  const [activeBacktestId, setActiveBacktestId] = useState("");

  useEffect(() => {
    if (ctx.backtests.length) {
      setView("backtests");
    } else if (ctx.charts.length) {
      setView("charts");
    }
  }, [ctx.charts.length, ctx.backtests.length]);

  useEffect(() => {
    if (!ctx.charts.length) {
      setActiveChartId("");
      return;
    }
    if (!ctx.charts.some((v) => v.id === activeChartId)) {
      setActiveChartId(ctx.charts[0].id);
    }
  }, [activeChartId, ctx.charts]);

  useEffect(() => {
    if (!ctx.backtests.length) {
      setActiveBacktestId("");
      return;
    }
    if (!ctx.backtests.some((v) => v.id === activeBacktestId)) {
      setActiveBacktestId(ctx.backtests[0].id);
    }
  }, [activeBacktestId, ctx.backtests]);

  const activeChart =
    ctx.charts.find((v) => v.id === activeChartId) ?? ctx.charts[0] ?? null;
  const activeBacktest =
    ctx.backtests.find((v) => v.id === activeBacktestId) ??
    ctx.backtests[0] ??
    null;

  const content = (
    <>
      <div className="border-b border-brand-500/15 px-3 py-2.5">
        <div className="flex items-start justify-between gap-3">
          <div className="min-w-0">
            <div className="flex items-center gap-2 text-[12px] font-medium text-brand-300">
              <ChartIcon size={14} />
              <span>{t("title")}</span>
            </div>
            <div className="mt-1 truncate text-[11px] text-ink-400">
              {thread?.title || t("noSession")}
            </div>
          </div>
          <div className="flex rounded-md border border-brand-500/20 bg-ink-900/50 p-0.5 text-[11px]">
            <button
              type="button"
              onClick={() => setView("charts")}
              disabled={!ctx.charts.length}
              className={`rounded px-2 py-1 transition-colors ${
                view === "charts"
                  ? "bg-brand-500 text-white"
                  : "text-ink-400 hover:text-white disabled:opacity-35"
              }`}
            >
              {t("charts")} · {ctx.charts.length}
            </button>
            <button
              type="button"
              onClick={() => setView("backtests")}
              disabled={!ctx.backtests.length}
              className={`rounded px-2 py-1 transition-colors ${
                view === "backtests"
                  ? "bg-brand-500 text-white"
                  : "text-ink-400 hover:text-white disabled:opacity-35"
              }`}
            >
              {t("backtests")} · {ctx.backtests.length}
            </button>
          </div>
        </div>
      </div>

      <div className="min-h-0 flex-1 overflow-y-auto p-3">
        {view === "charts" && activeChart ? (
          <div className="space-y-3">
            {ctx.charts.length > 1 ? (
              <VisualPicker
                label={t("selectChart")}
                value={activeChart.id}
                rows={ctx.charts.map((v) => ({
                  id: v.id,
                  label: v.block.title,
                  meta: v.source,
                }))}
                onPick={setActiveChartId}
              />
            ) : null}
            <AgentVisualChart visual={activeChart} />
          </div>
        ) : null}

        {view === "backtests" && activeBacktest ? (
          <div className="space-y-3">
            {ctx.backtests.length > 1 ? (
              <VisualPicker
                label={t("selectBacktest")}
                value={activeBacktest.id}
                rows={ctx.backtests.map((v) => ({
                  id: v.id,
                  label: v.title,
                  meta: [v.proposalId, v.strategyId, v.ts].filter(Boolean).join(" · "),
                }))}
                onPick={setActiveBacktestId}
              />
            ) : null}
            <div className="rounded-md border border-brand-500/15 bg-ink-900/35 px-3 py-2">
              <div className="text-[11px] text-ink-500 font-medium">
                {t("backtestRun")}
              </div>
              <div className="mt-1 flex flex-wrap items-center gap-1.5 text-[11px]">
                <span className="rounded border border-brand-500/20 bg-brand-500/[0.08] px-1.5 py-0.5 font-mono text-brand-100">
                  {activeBacktest.strategyId}
                </span>
                {activeBacktest.proposalId ? (
                  <span className="rounded border border-sky-400/20 bg-sky-400/[0.08] px-1.5 py-0.5 font-mono text-sky-100">
                    {activeBacktest.proposalId}
                  </span>
                ) : null}
                <span className="rounded border border-ink-700 bg-ink-950/60 px-1.5 py-0.5 font-mono text-ink-300">
                  {activeBacktest.ts}
                </span>
              </div>
            </div>
            <BacktestChart
              strategyId={activeBacktest.strategyId}
              ts={activeBacktest.ts}
              proposalId={activeBacktest.proposalId}
            />
          </div>
        ) : null}
      </div>
    </>
  );

  if (embedded) {
    return <div className="flex h-full min-h-0 flex-col">{content}</div>;
  }

  return (
    <aside
      className="hidden min-h-0 w-[540px] shrink-0 border-l border-brand-500/15 bg-ink-950/45 xl:flex flex-col"
      aria-label={t("title")}
    >
      {content}
    </aside>
  );
}

function AgentVisualChart({ visual }: { visual: AgentVisual }) {
  const t = useTranslations("chatChartPanel");
  const resolved = useChartData(visual.block);

  if (resolved.loading) {
    return (
      <ChartPlaceholder
        title={visual.block.title}
        subtitle={t("loadingBulk")}
        tone="loading"
        height={360}
      />
    );
  }

  if (resolved.error) {
    return (
      <ChartPlaceholder
        title={visual.block.title}
        subtitle={resolved.error}
        tone="error"
        height={360}
      />
    );
  }

  if (!resolved.ready) {
    return (
      <ChartPlaceholder
        title={visual.block.title}
        subtitle={t("noChartData")}
        tone="empty"
        height={360}
      />
    );
  }

  if (isCandlestickBlock(resolved.block)) {
    return <MarketChartWorkbench block={resolved.block} />;
  }

  return (
    <div className="space-y-3">
      <ChartCardHeader block={resolved.block} />
      <div className="rounded-md border border-brand-500/15 bg-ink-900/35 p-2">
        <ChartCanvas block={resolved.block} height={360} />
      </div>
      <Insights block={resolved.block} />
    </div>
  );
}

function MarketChartWorkbench({ block }: { block: ChartBlockShape }) {
  const t = useTranslations("chatChartPanel");
  const initialTarget = useMemo(() => inferMarketTarget(block), [block]);
  const [overrideBlock, setOverrideBlock] = useState<ChartBlockShape | null>(null);
  const [interval, setIntervalValue] = useState(initialTarget.interval || "1h");
  const [count, setCount] = useState(
    nearestCount(primaryCandles(block).length || 120),
  );
  const [indicators, setIndicators] =
    useState<IndicatorState>(DEFAULT_INDICATORS);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");

  useEffect(() => {
    setOverrideBlock(null);
    setIntervalValue(initialTarget.interval || "1h");
    setCount(nearestCount(primaryCandles(block).length || 120));
    setError("");
  }, [block.chart_id, initialTarget.interval]);

  const displayBlock = overrideBlock ?? block;
  const target = useMemo(
    () => inferMarketTarget(displayBlock) || initialTarget,
    [displayBlock, initialTarget],
  );
  const candles = primaryCandles(displayBlock);
  const agentSeries = useMemo(
    () => splitAgentSeries(displayBlock, candles),
    [displayBlock, candles],
  );
  const rsi = useMemo(() => computeRsi(candles, 14), [candles]);
  const macd = useMemo(() => computeMacd(candles), [candles]);

  async function loadCandles(next?: { interval?: string; count?: number }) {
    if (!target.market || !target.venue) {
      setError(t("noMarketTarget"));
      return;
    }
    const nextInterval = next?.interval ?? interval;
    const nextCount = next?.count ?? count;
    setBusy(true);
    setError("");
    try {
      const res = await clientApi.marketCandles({
        venue: target.venue,
        market: target.market,
        interval: nextInterval,
        count: nextCount,
      });
      if (res.error) {
        setError(res.error);
      }
      setOverrideBlock(
        blockFromCandles({
          base: displayBlock,
          market: res.market || target.market,
          venue: res.venue || target.venue,
          interval: res.interval || nextInterval,
          candles: res.candles || [],
        }),
      );
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  }

  function toggleIndicator(key: IndicatorKey) {
    setIndicators((cur) => ({ ...cur, [key]: !cur[key] }));
  }

  return (
    <div className="space-y-3">
      <ChartCardHeader block={displayBlock} />

      <div className="rounded-md border border-brand-500/15 bg-ink-900/35 px-3 py-2">
        <div className="flex flex-wrap items-center gap-2">
          <div className="flex items-center gap-1">
            {INTERVALS.map((opt) => (
              <button
                key={opt}
                type="button"
                onClick={() => {
                  setIntervalValue(opt);
                  void loadCandles({ interval: opt });
                }}
                disabled={busy || !target.market}
                className={`rounded px-2 py-1 text-[11px] font-mono transition-colors ${
                  interval === opt
                    ? "bg-brand-500 text-white"
                    : "border border-ink-700 bg-ink-950/40 text-ink-300 hover:border-brand-500/40 hover:text-white"
                } disabled:opacity-45`}
              >
                {opt}
              </button>
            ))}
          </div>
          <select
            value={count}
            onChange={(e) => {
              const nextCount = Number(e.target.value);
              setCount(nextCount);
              void loadCandles({ count: nextCount });
            }}
            disabled={busy || !target.market}
            className="rounded-md border border-ink-700 bg-ink-950/60 px-2 py-1 text-[11px] text-ink-100 focus:border-brand-500/50 focus:outline-none disabled:opacity-45"
            aria-label={t("bars")}
          >
            {BAR_COUNTS.map((n) => (
              <option key={n} value={n}>
                {n} {t("bars")}
              </option>
            ))}
          </select>
          <button
            type="button"
            onClick={() => loadCandles()}
            disabled={busy || !target.market}
            className="inline-flex items-center gap-1 rounded-md border border-brand-500/25 px-2 py-1 text-[11px] text-brand-100 hover:border-brand-500/50 hover:bg-brand-500/10 disabled:opacity-45"
          >
            <RefreshIcon size={12} className={busy ? "animate-spin" : ""} />
            <span>{t("refresh")}</span>
          </button>
        </div>

        <div className="mt-2 flex flex-wrap gap-1.5">
          {(["volume", "ma20", "ema50", "bb20", "vwap", "rsi", "macd", "agent"] as IndicatorKey[]).map((key) => (
            <button
              key={key}
              type="button"
              onClick={() => toggleIndicator(key)}
              className={`rounded border px-2 py-1 text-[10px] transition-colors ${
                indicators[key]
                  ? "border-brand-500/35 bg-brand-500/[0.10] text-brand-100"
                  : "border-ink-700 bg-ink-950/40 text-ink-500 hover:text-ink-200"
              }`}
            >
              {t(key)}
            </button>
          ))}
        </div>

        {error ? (
          <div className="mt-2 rounded border border-[#ef4560]/25 bg-[#ef4560]/10 px-2 py-1 text-[11px] text-[#ff9aa8]">
            {error}
          </div>
        ) : null}
      </div>

      <div className="rounded-md border border-brand-500/15 bg-ink-950/30 p-2">
        <AgentKlineCanvas
          candles={candles}
          priceSeries={indicators.agent ? agentSeries.priceLike : []}
          indicators={indicators}
          height={360}
          emptyTitle={t("kline")}
          emptySubtitle={t("noCandleData")}
        />
      </div>

      <Insights block={displayBlock} />

      {indicators.rsi && rsi.length > 0 ? (
        <MiniSeriesPanel
          title={t("rsi14")}
          height={132}
          series={[{ name: "RSI(14)", data: rsi, color: THEME.rsi }]}
          priceLines={[30, 70]}
        />
      ) : null}

      {indicators.macd && macd.macd.length > 0 ? (
        <MiniSeriesPanel
          title={t("macd")}
          height={132}
          series={[
            { name: "histogram", data: macd.histogram, color: THEME.histogramUp, histogram: true },
            { name: "MACD", data: macd.macd, color: THEME.macd },
            { name: "signal", data: macd.signal, color: THEME.signal },
          ]}
        />
      ) : null}

      {indicators.agent && agentSeries.detached.length > 0 ? (
        <div className="space-y-2">
          <div className="text-[11px] text-ink-500 font-medium">
            {t("agentIndicators")}
          </div>
          {agentSeries.detached.slice(0, 6).map((series) => (
            <MiniSeriesPanel
              key={series.name}
              title={series.name}
              height={118}
              series={[series]}
            />
          ))}
        </div>
      ) : null}
    </div>
  );
}

function AgentKlineCanvas({
  candles,
  priceSeries,
  indicators,
  height,
  emptyTitle,
  emptySubtitle,
}: {
  candles: CandlePoint[];
  priceSeries: SeriesLine[];
  indicators: IndicatorState;
  height: number;
  emptyTitle: string;
  emptySubtitle: string;
}) {
  const ref = useRef<HTMLDivElement | null>(null);
  const payload = useMemo(() => {
    const base = normalizeCandles(candles);
    return {
      candles: base,
      sma20: indicators.ma20 ? computeSma(base, 20) : [],
      ema50: indicators.ema50 ? computeEma(base, 50) : [],
      bands: indicators.bb20 ? computeBollinger(base, 20, 2) : { upper: [], lower: [] },
      vwap: indicators.vwap ? computeVwap(base) : [],
      priceSeries,
    };
  }, [candles, indicators.bb20, indicators.ema50, indicators.ma20, indicators.vwap, priceSeries]);

  useEffect(() => {
    const node = ref.current;
    if (!node) return;
    const chart = createBaseChart(node, height, {
      bottom: indicators.volume ? 0.24 : 0.08,
      timeVisible: true,
    });
    const candle = chart.addCandlestickSeries({
      upColor: THEME.up,
      downColor: THEME.down,
      borderVisible: false,
      wickUpColor: THEME.up,
      wickDownColor: THEME.down,
    });
    candle.setData(payload.candles.map((c) => ({
      time: toTime(c.time),
      open: c.open,
      high: c.high,
      low: c.low,
      close: c.close,
    })));

    if (indicators.volume) {
      const volume = chart.addHistogramSeries({
        priceFormat: { type: "volume" },
        priceScaleId: "volume",
        lastValueVisible: false,
        priceLineVisible: false,
      });
      volume.setData(payload.candles.map((c) => ({
        time: toTime(c.time),
        value: c.volume ?? 0,
        color: c.close >= c.open ? THEME.histogramUp : THEME.histogramDown,
      })));
      chart.priceScale("volume").applyOptions({
        scaleMargins: { top: 0.82, bottom: 0 },
      });
    }

    addLine(chart, "MA20", payload.sma20, THEME.ma, "solid", 1);
    addLine(chart, "EMA50", payload.ema50, THEME.ema, "solid", 1);
    addLine(chart, "BB upper", payload.bands.upper, THEME.band, "dashed", 1);
    addLine(chart, "BB lower", payload.bands.lower, THEME.band, "dashed", 1);
    addLine(chart, "VWAP", payload.vwap, THEME.vwap, "solid", 2);
    payload.priceSeries.forEach((series, idx) => {
      addLine(
        chart,
        series.name,
        series.data,
        series.color || palette(idx),
        series.style,
        series.width ?? 1,
      );
    });

    chart.timeScale().fitContent();
    const ro = observeChart(node, chart, height);
    return () => {
      ro.disconnect();
      chart.remove();
    };
  }, [height, indicators.volume, payload]);

  if (!payload.candles.length) {
    return (
      <ChartPlaceholder
        title={emptyTitle}
        subtitle={emptySubtitle}
        tone="empty"
        height={height}
      />
    );
  }

  return <div ref={ref} className="w-full" style={{ height }} />;
}

function MiniSeriesPanel({
  title,
  series,
  priceLines = [],
  height,
}: {
  title: string;
  series: SeriesLine[];
  priceLines?: number[];
  height: number;
}) {
  return (
    <div className="rounded-md border border-brand-500/15 bg-ink-900/30 p-2">
      <div className="mb-1 flex items-center justify-between gap-2">
        <div className="truncate text-[11px] font-medium text-ink-100">
          {title}
        </div>
        <div className="flex shrink-0 flex-wrap justify-end gap-1">
          {series.map((s) => (
            <span
              key={s.name}
              className="rounded border border-ink-700 bg-ink-950/50 px-1.5 py-0.5 text-[10px] text-ink-300"
            >
              {s.name}
            </span>
          ))}
        </div>
      </div>
      <MiniSeriesCanvas
        series={series}
        priceLines={priceLines}
        height={height}
      />
    </div>
  );
}

function MiniSeriesCanvas({
  series,
  priceLines,
  height,
}: {
  series: SeriesLine[];
  priceLines: number[];
  height: number;
}) {
  const ref = useRef<HTMLDivElement | null>(null);

  useEffect(() => {
    const node = ref.current;
    if (!node) return;
    const chart = createBaseChart(node, height, {
      bottom: 0.1,
      timeVisible: false,
    });
    let host: ReturnType<IChartApi["addLineSeries"]> | null = null;
    for (const row of series) {
      if (row.histogram) {
        const s = chart.addHistogramSeries({
          color: row.color,
          priceLineVisible: false,
          lastValueVisible: false,
        });
        s.setData(row.data.map((p) => ({
          time: toTime(p.time),
          value: p.value,
          color: p.value >= 0 ? THEME.histogramUp : THEME.histogramDown,
        })));
        continue;
      }
      const s = chart.addLineSeries({
        color: row.color,
        lineWidth: row.width ?? 1,
        lineStyle: lineStyle(row.style),
        priceLineVisible: false,
      });
      s.setData(row.data.map((p) => ({ time: toTime(p.time), value: p.value })));
      if (!host) host = s;
    }
    if (host) {
      for (const price of priceLines) {
        host.createPriceLine({
          price,
          color: "rgba(255,255,255,0.24)",
          lineStyle: LineStyle.Dashed,
          lineWidth: 1,
          axisLabelVisible: true,
          title: String(price),
        });
      }
    }
    chart.timeScale().fitContent();
    const ro = observeChart(node, chart, height);
    return () => {
      ro.disconnect();
      chart.remove();
    };
  }, [height, priceLines, series]);

  return <div ref={ref} className="w-full" style={{ height }} />;
}

function ChartCardHeader({ block }: { block: ChartBlockShape }) {
  const t = useTranslations("chatChartPanel");
  const [open, setOpen] = useState(false);
  const source = [block.source?.skill, block.source?.action].filter(Boolean).join(".");
  const seriesCount = block.series?.length ?? 0;
  return (
    <div className="rounded-md border border-brand-500/15 bg-ink-900/35 px-3 py-2">
      <div className="flex items-start justify-between gap-3">
        <div className="min-w-0">
          <div className="truncate text-sm font-semibold text-ink-50">
            {block.title}
          </div>
          <div className="mt-1 flex flex-wrap gap-1 text-[10px]">
            {block.subtitle ? <Tag>{block.subtitle}</Tag> : null}
            {source ? <Tag>{source}</Tag> : null}
            <Tag>{block.path}</Tag>
            <Tag>{t("seriesCount", { count: seriesCount })}</Tag>
          </div>
        </div>
        <button
          type="button"
          onClick={() => setOpen((v) => !v)}
          className="rounded border border-ink-700 bg-ink-950/50 p-1 text-ink-400 hover:text-white"
          aria-label={t("chartDetails")}
        >
          <ChevronDownIcon
            size={13}
            className={open ? "" : "-rotate-90 transition-transform"}
          />
        </button>
      </div>
      {open ? (
        <div className="mt-2 truncate font-mono text-[10px] text-ink-500">
          {block.chart_id}
        </div>
      ) : null}
    </div>
  );
}

function Insights({ block }: { block: ChartBlockShape }) {
  const insights = block.insights ?? [];
  const warnings = block.warnings ?? [];
  if (!insights.length && !warnings.length) return null;
  return (
    <div className="rounded-md border border-brand-500/15 bg-ink-900/25 px-3 py-2">
      {warnings.length ? (
        <ul className="space-y-1 text-[11px] text-[#ffd58a]">
          {warnings.map((line, i) => (
            <li key={`w-${i}`}>{line}</li>
          ))}
        </ul>
      ) : null}
      {insights.length ? (
        <ul className="mt-1 space-y-1 text-[11px] text-ink-300">
          {insights.map((line, i) => (
            <li key={`i-${i}`} className="flex gap-2">
              <span className="text-brand-300">·</span>
              <span>{line}</span>
            </li>
          ))}
        </ul>
      ) : null}
    </div>
  );
}

function VisualPicker({
  label,
  value,
  rows,
  onPick,
}: {
  label: string;
  value: string;
  rows: { id: string; label: string; meta: string }[];
  onPick: (id: string) => void;
}) {
  return (
    <div className="rounded-md border border-brand-500/15 bg-ink-900/35 px-3 py-2">
      <label className="text-[11px] text-ink-500 font-medium">
        {label}
      </label>
      <select
        value={value}
        onChange={(e) => onPick(e.target.value)}
        className="mt-1 w-full rounded-md border border-ink-700 bg-ink-950/70 px-2 py-1.5 text-xs text-ink-100 focus:border-brand-500/50 focus:outline-none"
      >
        {rows.map((row) => (
          <option key={row.id} value={row.id}>
            {row.label} · {row.meta}
          </option>
        ))}
      </select>
    </div>
  );
}

function Tag({ children }: { children: ReactNode }) {
  return (
    <span className="rounded border border-brand-500/15 bg-ink-950/45 px-1.5 py-0.5 text-ink-400">
      {children}
    </span>
  );
}

function collectAgentVisualContext(thread: ChatThread | null | undefined): VisualContext {
  if (!thread) return { charts: [], backtests: [] };
  const chartById = new Map<string, AgentVisual>();
  const backtestById = new Map<string, BacktestRef>();

  function addChart(block: ChartBlockShape, seenAt: number) {
    const id = block.chart_id || `${block.title}:${seenAt}`;
    if (chartById.has(id)) return;
    const source = [block.source?.skill, block.source?.action].filter(Boolean).join(".");
    chartById.set(id, { id, block, seenAt, source: source || block.chart_kind });
    const ref = backtestRefFromChart(block, seenAt);
    if (ref) backtestById.set(ref.id, ref);
  }

  function addBacktest(ref: BacktestRef | null) {
    if (!ref || backtestById.has(ref.id)) return;
    backtestById.set(ref.id, ref);
  }

  for (const message of thread.messages) {
    if (message.role !== "assistant") continue;
    const seenAt = message.ts || 0;
    const envelopes: NativeBlockEnvelope[] = [
      ...(message.turn?.blocks ?? []),
      ...liveEventsToBlocks(message.live_events ?? []),
    ];
    for (const env of envelopes) {
      const block = unwrapNativeBlock(env);
      if (isChartBlockShape(block)) addChart(block, seenAt);
      for (const chart of extractChartBlocks(block)) addChart(chart, seenAt);
      for (const ref of extractBacktestRefs(block, seenAt)) addBacktest(ref);
    }
    for (const tool of message.turn?.tool_trace ?? []) {
      for (const chart of extractChartBlocks(tool.result)) addChart(chart, seenAt);
      for (const ref of extractBacktestRefs(tool.result, seenAt)) addBacktest(ref);
    }
    for (const action of message.turn?.actions ?? []) {
      for (const chart of extractChartBlocks(action.result)) addChart(chart, seenAt);
      for (const ref of extractBacktestRefs(action.result, seenAt)) addBacktest(ref);
    }
  }

  return {
    charts: Array.from(chartById.values()).sort((a, b) => b.seenAt - a.seenAt),
    backtests: Array.from(backtestById.values()).sort((a, b) => b.seenAt - a.seenAt),
  };
}

function unwrapNativeBlock(env: NativeBlockEnvelope): NativeBlock {
  const inner = env.block ?? {};
  if (inner && typeof inner === "object" && Object.keys(inner).length > 0) {
    return inner as NativeBlock;
  }
  return env as unknown as NativeBlock;
}

function extractChartBlocks(value: unknown): ChartBlockShape[] {
  const out: ChartBlockShape[] = [];
  const seen = new Set<unknown>();
  function visit(cur: unknown, depth: number) {
    if (!cur || depth > 4 || seen.has(cur)) return;
    if (typeof cur !== "object") return;
    seen.add(cur);
    if (isChartBlockShape(cur)) {
      out.push(cur);
      return;
    }
    const rec = cur as Record<string, unknown>;
    const blocks = rec.chart_blocks;
    if (Array.isArray(blocks)) {
      for (const block of blocks) visit(block, depth + 1);
    }
    visit(rec.chart_block, depth + 1);
    for (const key of ["result", "payload", "output", "chart", "data"]) {
      visit(rec[key], depth + 1);
    }
    if (Array.isArray(cur)) {
      for (const item of cur.slice(0, 24)) visit(item, depth + 1);
    }
  }
  visit(value, 0);
  return out;
}

function extractBacktestRefs(value: unknown, seenAt: number): BacktestRef[] {
  const out: BacktestRef[] = [];
  const seen = new Set<unknown>();
  function add(strategyId: string, ts: string, title?: string, proposalId?: string | null) {
    if (!strategyId || !ts) return;
    const cleanProposalId = proposalId?.trim() || "";
    const id = cleanProposalId ? `${cleanProposalId}:${strategyId}:${ts}` : `${strategyId}:${ts}`;
    out.push({
      id,
      strategyId,
      proposalId: cleanProposalId || null,
      ts,
      title: title || `${cleanProposalId ? `${cleanProposalId} · ` : ""}${strategyId} · ${ts}`,
      seenAt,
    });
  }
  function visit(cur: unknown, depth: number) {
    if (!cur || depth > 4 || seen.has(cur)) return;
    if (typeof cur === "string") {
      const parsedJson = parseJsonObject(cur);
      if (parsedJson) {
        visit(parsedJson, depth + 1);
      }
      const parsed = parseBacktestPath(cur);
      if (parsed) add(parsed.strategyId, parsed.ts, undefined, parsed.proposalId);
      return;
    }
    if (typeof cur !== "object") return;
    seen.add(cur);
    const rec = cur as Record<string, unknown>;
    const sid = stringValue(rec.strategy_id ?? rec.strategyId);
    const proposalId = stringValue(rec.proposal_id ?? rec.proposalId);
    const ts = stringValue(rec.backtest_ts ?? rec.backtestTs);
    if (sid && ts) add(sid, ts, undefined, proposalId);
    const outDir = stringValue(rec.out_dir ?? rec.backtest_dir ?? rec.path);
    if (outDir) {
      const parsed = parseBacktestPath(outDir);
      if (parsed) add(parsed.strategyId, parsed.ts, undefined, parsed.proposalId || proposalId);
    }
    for (const key of ["result", "payload", "output", "data", "chart", "metrics", "raw_metrics_file", "metrics_path", "report_path"]) {
      visit(rec[key], depth + 1);
    }
    if (Array.isArray(cur)) {
      for (const item of cur.slice(0, 24)) visit(item, depth + 1);
    }
  }
  visit(value, 0);
  return dedupeBacktests(out);
}

function backtestRefFromChart(block: ChartBlockShape, seenAt: number): BacktestRef | null {
  if (block.source?.skill !== "backtest") return null;
  const parts = block.title.split("·").map((p) => p.trim()).filter(Boolean);
  if (parts.length < 3) return null;
  const strategyId = parts[0];
  const ts = parts[parts.length - 1];
  if (!strategyId || !looksLikeBacktestTs(ts)) return null;
  return {
    id: `${strategyId}:${ts}`,
    strategyId,
    proposalId: null,
    ts,
    title: `${strategyId} · ${ts}`,
    seenAt,
  };
}

function parseBacktestPath(raw: string): { strategyId: string; ts: string; proposalId?: string | null } | null {
  const parts = raw.replace(/\\/g, "/").split("/").filter(Boolean);
  const idx = parts.lastIndexOf("backtests");
  if (idx <= 0 || idx + 1 >= parts.length) return null;
  const strategyId = parts[idx - 1];
  const ts = parts[idx + 1];
  if (!strategyId || !looksLikeBacktestTs(ts)) return null;
  const proposalIdx = parts.lastIndexOf("proposals");
  const proposalId =
    proposalIdx >= 0 && proposalIdx + 1 < parts.length
      ? parts[proposalIdx + 1]
      : null;
  return { strategyId, ts, proposalId };
}

function parseJsonObject(raw: string): Record<string, unknown> | unknown[] | null {
  const trimmed = raw.trim();
  if (!trimmed || !/^[{\[]/.test(trimmed)) return null;
  try {
    const parsed = JSON.parse(trimmed) as unknown;
    return parsed && typeof parsed === "object"
      ? (parsed as Record<string, unknown> | unknown[])
      : null;
  } catch {
    return null;
  }
}

function looksLikeBacktestTs(value: string): boolean {
  return /^\d{8}_\d{6}$/.test(value.trim());
}

function dedupeBacktests(rows: BacktestRef[]): BacktestRef[] {
  const out = new Map<string, BacktestRef>();
  for (const row of rows) {
    if (!out.has(row.id)) out.set(row.id, row);
  }
  return Array.from(out.values());
}

function isCandlestickBlock(block: ChartBlockShape): boolean {
  return block.series.some((series) => series.type === "candlestick");
}

function primaryCandles(block: ChartBlockShape): CandlePoint[] {
  const series = block.series.find((s) => s.type === "candlestick");
  if (!series?.data) return [];
  return normalizeCandles(series.data.filter(isOhlcPoint));
}

function splitAgentSeries(
  block: ChartBlockShape,
  candles: CandlePoint[],
): { priceLike: SeriesLine[]; detached: SeriesLine[] } {
  const closeMedian = median(candles.map((c) => c.close));
  const priceLike: SeriesLine[] = [];
  const detached: SeriesLine[] = [];
  let idx = 0;
  for (const series of block.series) {
    if (series.type === "candlestick") continue;
    if (!series.data?.length) continue;
    if (isVolumeLike(series)) continue;
    const points = timeValueData(series);
    if (!points.length) continue;
    const row: SeriesLine = {
      name: series.name || `series_${idx + 1}`,
      data: points,
      color: series.color || palette(idx),
      style: series.line_style,
      width: series.line_width,
      histogram: series.type === "histogram" || series.type === "bar",
    };
    const valueMedian = median(points.map((p) => p.value));
    if (
      closeMedian > 0 &&
      valueMedian > closeMedian * 0.2 &&
      valueMedian < closeMedian * 5
    ) {
      priceLike.push(row);
    } else {
      detached.push(row);
    }
    idx += 1;
  }
  return { priceLike, detached };
}

function inferMarketTarget(block: ChartBlockShape): {
  venue: string;
  market: string;
  interval: string;
} {
  const titleParts = block.title.split("·").map((p) => p.trim());
  const rawMarket = titleParts[0] || "";
  const rawInterval = titleParts.find((p) => /^\d+[mhd]$/.test(p)) || "";
  const subtitle = block.subtitle || "";
  const venueFromSubtitle = /venue:\s*([A-Za-z0-9:_-]+)/i.exec(subtitle)?.[1] || "";
  const venueFromMarket = rawMarket.includes(":") ? rawMarket.split(":", 1)[0] : "";
  return {
    venue: (venueFromSubtitle || venueFromMarket || "").toLowerCase(),
    market: rawMarket,
    interval: rawInterval,
  };
}

function blockFromCandles({
  base,
  market,
  venue,
  interval,
  candles,
}: {
  base: ChartBlockShape;
  market: string;
  venue: string;
  interval: string;
  candles: Candle[];
}): ChartBlockShape {
  const data = candles.map((c) => ({
    time: normalizeTimeNumber(c.ts),
    open: c.open,
    high: c.high,
    low: c.low,
    close: c.close,
    volume: c.volume,
  }));
  return {
    ...base,
    chart_id: `${base.chart_id}.${interval}.${data.length}.${data.at(-1)?.time ?? "0"}`,
    title: `${market} · ${interval} · ${data.length} bars`,
    subtitle: `venue: ${venue}`,
    path: "inline",
    bulk_data_uri: undefined,
    series: [{ type: "candlestick", name: "ohlc", data }],
  };
}

function createBaseChart(
  node: HTMLDivElement,
  height: number,
  opts: { bottom: number; timeVisible: boolean },
): IChartApi {
  return createChart(node, {
    width: Math.max(320, node.clientWidth),
    height,
    layout: {
      background: { type: ColorType.Solid, color: THEME.background },
      textColor: THEME.text,
      fontSize: 11,
      attributionLogo: false,
    },
    grid: {
      vertLines: { color: THEME.grid },
      horzLines: { color: THEME.grid },
    },
    crosshair: { mode: CrosshairMode.Magnet },
    rightPriceScale: {
      borderColor: THEME.border,
      scaleMargins: { top: 0.08, bottom: opts.bottom },
    },
    timeScale: {
      borderColor: THEME.border,
      timeVisible: opts.timeVisible,
      secondsVisible: false,
    },
    handleScale: { axisPressedMouseMove: false },
  });
}

function observeChart(node: HTMLDivElement, chart: IChartApi, height: number): ResizeObserver {
  const ro = new ResizeObserver(() => {
    const width = Math.max(320, node.clientWidth);
    chart.applyOptions({ width, height });
  });
  ro.observe(node);
  return ro;
}

function addLine(
  chart: IChartApi,
  name: string,
  data: TimeValue[],
  color: string,
  style: "solid" | "dashed" | "dotted" = "solid",
  width: 1 | 2 | 3 = 1,
) {
  if (!data.length) return;
  const s = chart.addLineSeries({
    title: name,
    color,
    lineWidth: width,
    lineStyle: lineStyle(style),
    priceLineVisible: false,
    lastValueVisible: false,
  });
  s.setData(data.map((p) => ({ time: toTime(p.time), value: p.value })));
}

function lineStyle(style?: "solid" | "dashed" | "dotted"): LineStyle {
  if (style === "dashed") return LineStyle.Dashed;
  if (style === "dotted") return LineStyle.Dotted;
  return LineStyle.Solid;
}

function computeSma(candles: CandlePoint[], period: number): TimeValue[] {
  const out: TimeValue[] = [];
  let sum = 0;
  for (let i = 0; i < candles.length; i += 1) {
    sum += candles[i].close;
    if (i >= period) sum -= candles[i - period].close;
    if (i >= period - 1) {
      out.push({ time: candles[i].time, value: sum / period });
    }
  }
  return out;
}

function computeEma(candles: CandlePoint[], period: number): TimeValue[] {
  if (!candles.length) return [];
  const out: TimeValue[] = [];
  const k = 2 / (period + 1);
  let ema = candles[0].close;
  for (let i = 0; i < candles.length; i += 1) {
    ema = i === 0 ? candles[i].close : candles[i].close * k + ema * (1 - k);
    if (i >= Math.min(period - 1, candles.length - 1)) {
      out.push({ time: candles[i].time, value: ema });
    }
  }
  return out;
}

function computeBollinger(
  candles: CandlePoint[],
  period: number,
  mult: number,
): { upper: TimeValue[]; lower: TimeValue[] } {
  const upper: TimeValue[] = [];
  const lower: TimeValue[] = [];
  for (let i = period - 1; i < candles.length; i += 1) {
    const slice = candles.slice(i - period + 1, i + 1).map((c) => c.close);
    const avg = slice.reduce((a, b) => a + b, 0) / period;
    const variance = slice.reduce((a, b) => a + (b - avg) ** 2, 0) / period;
    const band = Math.sqrt(variance) * mult;
    upper.push({ time: candles[i].time, value: avg + band });
    lower.push({ time: candles[i].time, value: avg - band });
  }
  return { upper, lower };
}

function computeVwap(candles: CandlePoint[]): TimeValue[] {
  let pv = 0;
  let vol = 0;
  const out: TimeValue[] = [];
  for (const c of candles) {
    const volume = c.volume ?? 0;
    const typical = (c.high + c.low + c.close) / 3;
    pv += typical * volume;
    vol += volume;
    if (vol > 0) out.push({ time: c.time, value: pv / vol });
  }
  return out;
}

function computeRsi(candles: CandlePoint[], period: number): TimeValue[] {
  if (candles.length <= period) return [];
  const out: TimeValue[] = [];
  let gains = 0;
  let losses = 0;
  for (let i = 1; i <= period; i += 1) {
    const diff = candles[i].close - candles[i - 1].close;
    if (diff >= 0) gains += diff;
    else losses -= diff;
  }
  let avgGain = gains / period;
  let avgLoss = losses / period;
  for (let i = period + 1; i < candles.length; i += 1) {
    const diff = candles[i].close - candles[i - 1].close;
    avgGain = (avgGain * (period - 1) + Math.max(diff, 0)) / period;
    avgLoss = (avgLoss * (period - 1) + Math.max(-diff, 0)) / period;
    const rs = avgLoss === 0 ? 100 : avgGain / avgLoss;
    const rsi = avgLoss === 0 ? 100 : 100 - 100 / (1 + rs);
    out.push({ time: candles[i].time, value: rsi });
  }
  return out;
}

function computeMacd(candles: CandlePoint[]): {
  macd: TimeValue[];
  signal: TimeValue[];
  histogram: TimeValue[];
} {
  const ema12 = computeEma(candles, 12);
  const ema26 = computeEma(candles, 26);
  const byTime = new Map(ema12.map((p) => [String(p.time), p.value]));
  const macd = ema26
    .map((p) => ({
      time: p.time,
      value: (byTime.get(String(p.time)) ?? p.value) - p.value,
    }))
    .filter((p) => Number.isFinite(p.value));
  const signal = emaFromPoints(macd, 9);
  const signalByTime = new Map(signal.map((p) => [String(p.time), p.value]));
  const histogram = macd
    .filter((p) => signalByTime.has(String(p.time)))
    .map((p) => ({ time: p.time, value: p.value - (signalByTime.get(String(p.time)) ?? 0) }));
  return { macd, signal, histogram };
}

function emaFromPoints(points: TimeValue[], period: number): TimeValue[] {
  if (!points.length) return [];
  const k = 2 / (period + 1);
  let ema = points[0].value;
  const out: TimeValue[] = [];
  for (let i = 0; i < points.length; i += 1) {
    ema = i === 0 ? points[i].value : points[i].value * k + ema * (1 - k);
    if (i >= period - 1) out.push({ time: points[i].time, value: ema });
  }
  return out;
}

function timeValueData(series: ChartSeries): TimeValue[] {
  if (!series.data) return [];
  return series.data
    .map((point) => {
      const rec = point as Partial<TimeValue>;
      const value = Number(rec.value);
      if (!Number.isFinite(value) || rec.time === undefined) return null;
      return { time: rec.time, value };
    })
    .filter((p): p is TimeValue => Boolean(p));
}

function normalizeCandles(candles: CandlePoint[]): CandlePoint[] {
  return candles
    .map((c) => ({
      time: typeof c.time === "number" ? normalizeTimeNumber(c.time) : c.time,
      open: Number(c.open),
      high: Number(c.high),
      low: Number(c.low),
      close: Number(c.close),
      volume: Number(c.volume ?? 0),
    }))
    .filter((c) =>
      [c.open, c.high, c.low, c.close].every((v) => Number.isFinite(v)),
    )
    .sort((a, b) => numericTime(a.time) - numericTime(b.time));
}

function isOhlcPoint(value: ChartSeriesPoint): value is OHLCV {
  const rec = value as OHLCV;
  return (
    rec &&
    rec.time !== undefined &&
    typeof rec.open === "number" &&
    typeof rec.high === "number" &&
    typeof rec.low === "number" &&
    typeof rec.close === "number"
  );
}

function isVolumeLike(series: ChartSeries): boolean {
  const name = series.name.toLowerCase();
  return name === "volume" || name === "vol" || series.price_format === "volume";
}

function normalizeTimeNumber(value: number): number {
  return value > 1e12 ? Math.floor(value / 1000) : Math.floor(value);
}

function toTime(value: number | string): Time {
  if (typeof value === "number") return normalizeTimeNumber(value) as UTCTimestamp;
  if (/^\d{4}-\d{2}-\d{2}$/.test(value)) return value as Time;
  const ms = Date.parse(value);
  if (!Number.isNaN(ms)) return Math.floor(ms / 1000) as UTCTimestamp;
  return 0 as UTCTimestamp;
}

function numericTime(value: number | string): number {
  if (typeof value === "number") return normalizeTimeNumber(value);
  const parsed = Date.parse(value);
  return Number.isFinite(parsed) ? Math.floor(parsed / 1000) : 0;
}

function median(values: number[]): number {
  const xs = values.filter((v) => Number.isFinite(v)).sort((a, b) => a - b);
  if (!xs.length) return 0;
  const mid = Math.floor(xs.length / 2);
  return xs.length % 2 ? xs[mid] : (xs[mid - 1] + xs[mid]) / 2;
}

function nearestCount(n: number): number {
  return BAR_COUNTS.reduce((best, cur) =>
    Math.abs(cur - n) < Math.abs(best - n) ? cur : best,
  );
}

function stringValue(value: unknown): string {
  return typeof value === "string" ? value.trim() : "";
}

function palette(index: number): string {
  const colors = [
    "#b48bff",
    "#22d3ee",
    "#f5a524",
    "#f472b6",
    "#84cc16",
    "#fb7185",
  ];
  return colors[index % colors.length];
}
