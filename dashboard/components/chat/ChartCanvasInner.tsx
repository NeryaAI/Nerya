"use client";

import { useEffect, useRef } from "react";
import { useLocale } from "next-intl";
import { cleanSeries } from "../../lib/financialChart";
import { chartTime, financeNumber } from "../../lib/financeDisplay";
import {
  ColorType,
  CrosshairMode,
  LineStyle,
  createChart,
  type IChartApi,
  type ISeriesApi,
  type SeriesMarker,
  type Time,
  type UTCTimestamp,
} from "lightweight-charts";
import type {
  ChartBlockShape,
  ChartOverlay,
  ChartSeries,
  ChartSeriesPoint,
  OHLCV,
  TimeValue,
} from "../../lib/chartBlock";
import { useChartTheme } from "../../lib/chartTheme";

// ---------------------------------------------------------------------------
// Helpers — translate ChartBlock schema into lightweight-charts inputs.
// ---------------------------------------------------------------------------

function toTime(value: number | string): Time {
  return (chartTime(value) ?? 0) as UTCTimestamp;
}

function isOHLCV(p: ChartSeriesPoint): p is OHLCV {
  return (
    p && typeof (p as OHLCV).open === "number" && typeof (p as OHLCV).close === "number"
  );
}

function lineStyleFromString(value?: string): LineStyle {
  switch (value) {
    case "dashed":
      return LineStyle.Dashed;
    case "dotted":
      return LineStyle.Dotted;
    default:
      return LineStyle.Solid;
  }
}

function priceFormatFromString(
  value?: string
): { type: "price" | "percent" | "volume" } | undefined {
  if (value === "price" || value === "percent" || value === "volume") {
    return { type: value };
  }
  return undefined;
}

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
  const token = (name: string, fallback: string) => css.getPropertyValue(name).trim() || fallback;
  return { ...DEFAULT_COLORS, up: token("--ok", DEFAULT_COLORS.up), down: token("--err", DEFAULT_COLORS.down),
    line: token("--violet-2", DEFAULT_COLORS.line), marker: token("--warn", DEFAULT_COLORS.marker),
    histogramPositive: token("--fluid", DEFAULT_COLORS.histogramPositive),
    area: { top: token("--fluid-soft", DEFAULT_COLORS.area.top), bottom: "transparent" } };
}

function addSeries(
  chart: IChartApi,
  series: ChartSeries
): ISeriesApi<"Candlestick" | "Line" | "Area" | "Histogram" | "Bar" | "Baseline"> | null {
  const THEME = chartPalette();
  const data = cleanSeries(series).data ?? [];
  if (data.length === 0) return null;

  switch (series.type) {
    case "candlestick": {
      const s = chart.addCandlestickSeries({
        upColor: series.color || THEME.up,
        downColor: THEME.down,
        borderVisible: false,
        wickUpColor: series.color || THEME.up,
        wickDownColor: THEME.down,
      });
      const points = data.filter(isOHLCV).map((p) => ({
        time: toTime(p.time),
        open: p.open,
        high: p.high,
        low: p.low,
        close: p.close,
      }));
      s.setData(points);
      return s;
    }
    case "line": {
      const s = chart.addLineSeries({
        color: series.color || THEME.line,
        lineWidth: (series.line_width ?? 2) as 1 | 2 | 3,
        lineStyle: lineStyleFromString(series.line_style),
        priceFormat: priceFormatFromString(series.price_format),
      });
      s.setData(
        data.map((p) => {
          const tv = p as TimeValue;
          return { time: toTime(tv.time), value: tv.value };
        })
      );
      return s;
    }
    case "area": {
      const s = chart.addAreaSeries({
        lineColor: series.color || THEME.line,
        topColor: series.top_color || THEME.area.top,
        bottomColor: series.bottom_color || THEME.area.bottom,
        lineWidth: (series.line_width ?? 2) as 1 | 2 | 3,
        priceFormat: priceFormatFromString(series.price_format),
      });
      s.setData(
        data.map((p) => {
          const tv = p as TimeValue;
          return { time: toTime(tv.time), value: tv.value };
        })
      );
      return s;
    }
    case "baseline": {
      const s = chart.addBaselineSeries({
        baseValue: { type: "price", price: series.base_value ?? 0 },
        topLineColor: series.top_color || THEME.up,
        bottomLineColor: series.bottom_color || THEME.down,
      });
      s.setData(
        data.map((p) => {
          const tv = p as TimeValue;
          return { time: toTime(tv.time), value: tv.value };
        })
      );
      return s;
    }
    case "histogram": {
      const s = chart.addHistogramSeries({
        ...(series.price_format === "volume" ? { priceScaleId: "volume", lastValueVisible: false, priceLineVisible: false } : {}),
        color: series.color || THEME.histogramPositive,
        priceFormat: priceFormatFromString(series.price_format) ?? {
          type: "volume",
        },
      });
      if (series.price_format === "volume") s.priceScale().applyOptions({ scaleMargins: { top: .82, bottom: 0 } });
      s.setData(
        data.map((p) => {
          const tv = p as TimeValue;
          return { time: toTime(tv.time), value: tv.value };
        })
      );
      return s;
    }
    case "bar": {
      const s = chart.addBarSeries({
        upColor: series.color || THEME.up,
        downColor: THEME.down,
      });
      const points = data.filter(isOHLCV).map((p) => ({
        time: toTime(p.time),
        open: p.open,
        high: p.high,
        low: p.low,
        close: p.close,
      }));
      s.setData(points);
      return s;
    }
    default:
      return null;
  }
}

function applyOverlays(
  primary: ISeriesApi<"Candlestick" | "Line" | "Area" | "Histogram" | "Bar" | "Baseline">,
  overlays: ChartOverlay[]
) {
  const THEME = chartPalette();
  const markers: SeriesMarker<Time>[] = [];
  for (const overlay of overlays) {
    if (overlay.type === "marker") {
      markers.push({
        time: toTime(overlay.time),
        position: overlay.position === "below" ? "belowBar" : overlay.position === "inBar" ? "inBar" : "aboveBar",
        color: overlay.color ?? THEME.marker,
        shape:
          overlay.shape === "arrow_up"
            ? "arrowUp"
            : overlay.shape === "arrow_down"
            ? "arrowDown"
            : overlay.shape === "square"
            ? "square"
            : "circle",
        text: overlay.text,
      } as SeriesMarker<Time>);
    } else if (overlay.type === "annotation") {
      markers.push({
        time: toTime(overlay.time),
        position: "aboveBar",
        color: THEME.marker,
        shape: "circle",
        text: overlay.text,
      } as SeriesMarker<Time>);
    } else if (overlay.type === "price_line" && Number.isFinite(overlay.price)) {
      primary.createPriceLine({
        price: overlay.price,
        color: overlay.color ?? THEME.marker,
        lineStyle:
          overlay.line_style === "dashed" ? LineStyle.Dashed : LineStyle.Solid,
        lineWidth: 1,
        axisLabelVisible: overlay.axis_label ?? true,
        title: overlay.title ?? "",
      });
    }
    // ``region`` overlays would need a custom series plugin; v1 leaves
    // them as no-op so the data still round-trips and a future PR can
    // add the renderer without a schema change.
  }
  if (markers.length > 0) {
    primary.setMarkers(markers.filter((marker) => Number(toTime(marker.time as number | string)) > 0).sort((a, b) => Number(toTime(a.time as number | string)) - Number(toTime(b.time as number | string))));
  }
}

// ---------------------------------------------------------------------------
// Component
// ---------------------------------------------------------------------------

export type ChartCanvasInnerProps = {
  block: ChartBlockShape;
  height?: number;
};

export default function ChartCanvasInner({ block, height = 240 }: ChartCanvasInnerProps) {
  const containerRef = useRef<HTMLDivElement | null>(null);
  const readoutRef = useRef<HTMLDivElement | null>(null);
  const locale = useLocale();
  const chartRef = useRef<IChartApi | null>(null);
  const chartTheme = useChartTheme();

  useEffect(() => {
    const container = containerRef.current;
    if (!container) return;

    const chart = createChart(container, {
      width: Math.max(1, container.clientWidth),
      height,
      autoSize: false,
      layout: {
        background: { type: ColorType.Solid, color: "transparent" },
        textColor: chartTheme.text,
        fontSize: 11,
        attributionLogo: false,
      },
      grid: {
        vertLines: { color: chartTheme.grid },
        horzLines: { color: chartTheme.grid },
      },
      crosshair: { mode: CrosshairMode.Magnet },
      rightPriceScale: { borderColor: chartTheme.grid, scaleMargins: { top: .08, bottom: block.series.some((s) => s.price_format === "volume") ? .22 : .08 } },
      timeScale: {
        borderColor: chartTheme.grid,
        timeVisible: block.time?.format !== "business_day",
        secondsVisible: false,
      },
      handleScale: { axisPressedMouseMove: false },
    });
    chartRef.current = chart;

    let primary: ReturnType<typeof addSeries> | null = null;
    const names = new Map<NonNullable<ReturnType<typeof addSeries>>, string>();
    for (const series of block.series) {
      const s = addSeries(chart, series);
      if (!s) continue;
      if (series.price_format !== "volume" && (!primary || series.type === "candlestick")) primary = s;
      if (series.price_format === "percent" && block.series.some((item) => !item.price_format || item.price_format === "price")) {
        s.applyOptions({ priceScaleId: "left" });
        chart.priceScale("left").applyOptions({ visible: true, borderColor: chartTheme.grid });
      }
      names.set(s, series.name);
      if (!series.price_format || series.price_format === "price") {
        const values = (cleanSeries(series).data || []).map((point) => "close" in point ? point.close : point.value);
        const nonzero = values.filter((value) => value !== 0).map(Math.abs);
        const smallest = nonzero.length ? Math.min(...nonzero.slice(0, 1500)) : 1;
        const minMove = smallest < 1 ? Math.pow(10, Math.max(-16, Math.floor(Math.log10(smallest)) - 5)) : .01;
        s.applyOptions({ priceFormat: { type: "custom", minMove, formatter: (value: number) => financeNumber(value, locale, Math.abs(value) >= 1 ? 2 : undefined) } });
      }
    }
    const hint = locale.startsWith("zh") ? "移动指针查看时间和数值；触屏长按查看。" : "Hover or long-press to inspect time and values.";
    if (readoutRef.current) readoutRef.current.textContent = hint;
    chart.subscribeCrosshairMove((event) => {
      const readout = readoutRef.current;
      if (!readout) return;
      if (!event.time || !event.point) { readout.textContent = hint; return; }
      const time = typeof event.time === "number" ? new Date(event.time * 1000).toLocaleString(locale) : String(event.time);
      const values = [...names].flatMap(([series, name]) => {
        const point = event.seriesData.get(series);
        if (!point) return [];
        const value = "close" in point ? `O ${financeNumber(point.open, locale)} H ${financeNumber(point.high, locale)} L ${financeNumber(point.low, locale)} C ${financeNumber(point.close, locale)}` : "value" in point ? financeNumber(point.value, locale) : "";
        return value ? [`${name}: ${value}`] : [];
      });
      readout.textContent = [time, ...values].join(" · ");
    });
    if (primary && block.overlays && block.overlays.length > 0) {
      applyOverlays(primary, block.overlays);
    }
    if (block.default_range) {
      try {
        chart.timeScale().setVisibleRange({
          from: toTime(block.default_range.from),
          to: toTime(block.default_range.to),
        });
      } catch {
        chart.timeScale().fitContent();
      }
    } else {
      chart.timeScale().fitContent();
    }

    const ro = new ResizeObserver(() => {
      const w = container.clientWidth;
      if (w > 0) chart.applyOptions({ width: w });
    });
    ro.observe(container);

    return () => {
      ro.disconnect();
      chart.remove();
      chartRef.current = null;
    };
    // We re-run the effect when the block identity changes; series
    // mutation across renders is rare in v1 and a full re-create keeps
    // memory & overlay state predictable.
  }, [block, chartTheme, height, locale]);

  return <div className="min-w-0"><div ref={readoutRef} data-testid="chart-crosshair-values" className="h-10 overflow-y-auto break-words pb-2 text-[11px] leading-4 tabular-nums text-[color:var(--text-muted)]" /><div ref={containerRef} role="img" aria-label={block.title} data-testid="chart-canvas" className="w-full min-w-0" style={{ height }} /></div>;
}
