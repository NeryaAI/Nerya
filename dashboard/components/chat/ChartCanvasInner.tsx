"use client";
import { copy as i18nCopy } from "../../lib/i18n";

import { useEffect, useRef } from "react";
import { useLocale } from "next-intl";
import { cleanSeries } from "../../lib/financialChart";
import { chartColor } from "../../lib/chartColor";
import { chartTime, financeNumber } from "../../lib/financeDisplay";
import { createVelaChart } from "../../lib/velaChart";
import type { ChartBlockShape } from "../../lib/chartBlock";
import { useChartTheme } from "../../lib/chartTheme";

// Canvas consumes resolved colors from the same Nerya tokens as the page.
const DEFAULT_COLORS = {
  background: "transparent",
  text: "#9aa3b2",
  grid: "rgba(255,255,255,0.04)",
  border: "rgba(255,255,255,0.08)",
  up: "#10b981",
  down: "#ef4444",
  line: "#6b8cff",
  area: { top: "rgba(107, 140, 255, 0.32)", bottom: "rgba(107, 140, 255, 0.04)" },
  histogramPositive: "#10b981",
  histogramNegative: "#ef4444",
  marker: "#fbbf24",
};

function chartPalette() {
  const css = getComputedStyle(document.documentElement);
  const token = (name: string, fallback: string) => chartColor(css.getPropertyValue(name), fallback);
  return { ...DEFAULT_COLORS, up: token("--ok", DEFAULT_COLORS.up), down: token("--err", DEFAULT_COLORS.down),
    line: token("--violet-2", DEFAULT_COLORS.line), marker: token("--warn", DEFAULT_COLORS.marker),
    histogramPositive: token("--fluid", DEFAULT_COLORS.histogramPositive),
    area: { top: token("--fluid-soft", DEFAULT_COLORS.area.top), bottom: "transparent" } };
}

export type ChartCanvasInnerProps = {
  block: ChartBlockShape;
  height?: number;
};

export default function ChartCanvasInner({ block, height = 240 }: ChartCanvasInnerProps) {
  const containerRef = useRef<HTMLDivElement | null>(null);
  const readoutRef = useRef<HTMLDivElement | null>(null);
  const locale = useLocale();
  const chartTheme = useChartTheme();

  useEffect(() => {
    const container = containerRef.current;
    if (!container) return;

    const palette = chartPalette();
    const series = block.series.map(row => ({ ...row, color: row.color || (row.type === "histogram" ? palette.histogramPositive : row.type === "candlestick" || row.type === "bar" ? palette.up : palette.line) }));
    const chart = createVelaChart(container, { height, theme: chartTheme, series, overlays: block.overlays, range: block.default_range, embedded: true });
    const hint = i18nCopy(locale.startsWith("zh"), "copy.components_chat_ChartCanvasInner.001");
    if (readoutRef.current) readoutRef.current.textContent = hint;
    const unsubscribe = chart.onCrosshairMove(event => {
      const readout = readoutRef.current;
      if (!readout) return;
      if (event.time === null) { readout.textContent = hint; return; }
      const time = new Date(event.time).toLocaleString(locale);
      const values = series.flatMap((row, index) => {
        const point = (cleanSeries(row).data || []).find(point => (chartTime(point.time) ?? 0) * 1000 === event.time);
        if (!point) return [];
        const value = "close" in point ? `O ${financeNumber(point.open, locale)} H ${financeNumber(point.high, locale)} L ${financeNumber(point.low, locale)} C ${financeNumber(point.close, locale)}` : financeNumber(event.values.get(`nerya-series-${index}`) ?? point.value, locale);
        return [`${row.name}: ${value}`];
      });
      readout.textContent = [time, ...values].join(" · ");
    });
    return () => { unsubscribe(); chart.destroy(); };
    // We re-run the effect when the block identity changes; series
    // mutation across renders is rare in v1 and a full re-create keeps
    // memory & overlay state predictable.
  }, [block, chartTheme, height, locale]);

  return <div className="min-w-0"><div ref={readoutRef} data-testid="chart-crosshair-values" className="h-10 overflow-y-auto break-words pb-2 text-[11px] leading-4 tabular-nums text-[color:var(--text-muted)]" /><div ref={containerRef} role="img" aria-label={block.title} data-testid="chart-canvas" className="w-full min-w-0" style={{ height }} /></div>;
}
