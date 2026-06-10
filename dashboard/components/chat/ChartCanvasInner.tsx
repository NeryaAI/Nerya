"use client";

import { useEffect, useRef } from "react";
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
  if (typeof value === "number") return value as UTCTimestamp;
  // ISO 8601 → unix seconds. lightweight-charts also accepts business
  // day strings (YYYY-MM-DD) as Time directly.
  if (typeof value === "string") {
    if (/^\d{4}-\d{2}-\d{2}$/.test(value)) {
      return value as Time; // business day
    }
    const ms = Date.parse(value);
    if (!Number.isNaN(ms)) return Math.floor(ms / 1000) as UTCTimestamp;
  }
  return 0 as UTCTimestamp;
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

// Theme — keeps brand alignment with the rest of the dashboard. Lives
// here (not in CSS vars) because lightweight-charts paints to canvas
// and can't read CSS at runtime.
const THEME = {
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

function addSeries(
  chart: IChartApi,
  series: ChartSeries
): ISeriesApi<"Candlestick" | "Line" | "Area" | "Histogram" | "Bar" | "Baseline"> | null {
  const data = series.data ?? [];
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
        color: series.color || THEME.histogramPositive,
        priceFormat: priceFormatFromString(series.price_format) ?? {
          type: "volume",
        },
      });
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
  const markers: SeriesMarker<Time>[] = [];
  for (const overlay of overlays) {
    if (overlay.type === "marker") {
      markers.push({
        time: toTime(overlay.time),
        position: overlay.position ?? "aboveBar",
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
    } else if (overlay.type === "price_line") {
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
    primary.setMarkers(markers);
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
  const chartRef = useRef<IChartApi | null>(null);
  const chartTheme = useChartTheme();

  useEffect(() => {
    const container = containerRef.current;
    if (!container) return;

    const chart = createChart(container, {
      width: container.clientWidth,
      height,
      autoSize: false,
      layout: {
        background: { type: ColorType.Solid, color: THEME.background },
        textColor: chartTheme.text,
        fontSize: 11,
        attributionLogo: false,
      },
      grid: {
        vertLines: { color: chartTheme.grid },
        horzLines: { color: chartTheme.grid },
      },
      crosshair: { mode: CrosshairMode.Magnet },
      rightPriceScale: { borderColor: chartTheme.grid },
      timeScale: {
        borderColor: chartTheme.grid,
        timeVisible: block.time?.format !== "business_day",
        secondsVisible: false,
      },
      handleScale: { axisPressedMouseMove: false },
    });
    chartRef.current = chart;

    let primary: ReturnType<typeof addSeries> | null = null;
    for (let i = 0; i < block.series.length; i += 1) {
      const s = addSeries(chart, block.series[i]);
      if (i === 0) primary = s;
    }
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
  }, [block, chartTheme, height]);

  return <div ref={containerRef} className="w-full" style={{ height }} />;
}
