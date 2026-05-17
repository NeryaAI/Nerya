"use client";

import { useEffect, useMemo, useRef } from "react";
import { useTranslations } from "next-intl";
import {
  ColorType,
  CrosshairMode,
  createChart,
  type UTCTimestamp,
} from "lightweight-charts";
import type { Candle } from "../lib/api";

type Mode = "candlestick" | "line" | "area";

type Props = {
  candles: Candle[];
  width?: number;
  height?: number;
  mode?: Mode;
  showVolume?: boolean;
  loading?: boolean;
  error?: string;
};

const TONE_UP = "#10d993";
const TONE_DOWN = "#ef4560";
const BRAND = "#b48bff";
const GRID = "rgba(139,92,246,0.12)";
const TEXT = "rgba(202,201,225,0.72)";

function toChartTime(ts: number): UTCTimestamp {
  return (ts > 1e12 ? Math.floor(ts / 1000) : ts) as UTCTimestamp;
}

export function CandleChart({
  candles,
  width = 800,
  height = 260,
  mode = "candlestick",
  showVolume = true,
  loading = false,
  error,
}: Props) {
  const t = useTranslations("candleChart");
  const containerRef = useRef<HTMLDivElement | null>(null);

  // lightweight-charts requires strictly ascending, unique time keys.
  // Upstream APIs sometimes return descending order or duplicate ts on
  // tick boundaries; normalise here so the series.setData assertion
  // never trips.
  const cleanedCandles = useMemo(() => {
    if (!candles.length) return [] as Candle[];
    const byTs = new Map<number, Candle>();
    for (const c of candles) {
      const key = toChartTime(c.ts) as unknown as number;
      byTs.set(key, c);
    }
    return Array.from(byTs.values()).sort(
      (a, b) =>
        (toChartTime(a.ts) as unknown as number) -
        (toChartTime(b.ts) as unknown as number),
    );
  }, [candles]);

  const last = useMemo(
    () => (cleanedCandles.length > 0 ? cleanedCandles[cleanedCandles.length - 1] : null),
    [cleanedCandles],
  );

  useEffect(() => {
    const container = containerRef.current;
    if (!container) return;
    const resolveWidth = () => (container.clientWidth > 0 ? container.clientWidth : width);

    const chart = createChart(container, {
      width: resolveWidth(),
      height,
      layout: {
        background: { type: ColorType.Solid, color: "transparent" },
        textColor: TEXT,
        fontSize: 11,
        attributionLogo: false,
      },
      grid: {
        vertLines: { color: GRID },
        horzLines: { color: GRID },
      },
      crosshair: { mode: CrosshairMode.Magnet },
      rightPriceScale: {
        borderColor: GRID,
        scaleMargins: {
          top: 0.08,
          bottom: showVolume ? 0.24 : 0.08,
        },
      },
      timeScale: {
        borderColor: GRID,
        timeVisible: true,
        secondsVisible: false,
      },
      handleScale: { axisPressedMouseMove: false },
    });

    if (mode === "candlestick") {
      const series = chart.addCandlestickSeries({
        upColor: TONE_UP,
        downColor: TONE_DOWN,
        borderVisible: false,
        wickUpColor: TONE_UP,
        wickDownColor: TONE_DOWN,
      });
      series.setData(
        cleanedCandles.map((c) => ({
          time: toChartTime(c.ts),
          open: c.open,
          high: c.high,
          low: c.low,
          close: c.close,
        })),
      );
    } else if (mode === "line") {
      const series = chart.addLineSeries({
        color: BRAND,
        lineWidth: 2,
      });
      series.setData(
        cleanedCandles.map((c) => ({
          time: toChartTime(c.ts),
          value: c.close,
        })),
      );
    } else {
      const series = chart.addAreaSeries({
        lineColor: BRAND,
        topColor: "rgba(180,139,255,0.30)",
        bottomColor: "rgba(180,139,255,0.02)",
        lineWidth: 2,
      });
      series.setData(
        cleanedCandles.map((c) => ({
          time: toChartTime(c.ts),
          value: c.close,
        })),
      );
    }

    if (showVolume) {
      const volume = chart.addHistogramSeries({
        priceFormat: { type: "volume" },
        priceScaleId: "volume",
        lastValueVisible: false,
        priceLineVisible: false,
      });
      volume.setData(
        cleanedCandles.map((c) => ({
          time: toChartTime(c.ts),
          value: c.volume ?? 0,
          color: c.close >= c.open ? "rgba(16,217,147,0.35)" : "rgba(239,69,96,0.35)",
        })),
      );
      chart.priceScale("volume").applyOptions({
        scaleMargins: { top: 0.82, bottom: 0 },
      });
    }

    chart.timeScale().fitContent();

    const ro = new ResizeObserver(() => {
      chart.applyOptions({ width: resolveWidth(), height });
    });
    ro.observe(container);

    return () => {
      ro.disconnect();
      chart.remove();
    };
  }, [cleanedCandles, height, mode, showVolume, width]);

  const lastUp = last ? last.close >= last.open : true;

  return (
    <div className="relative" style={{ width: "100%", height }}>
      <div ref={containerRef} className="h-full w-full" />

      {last ? (
        <div className="absolute top-1 right-1 text-right pointer-events-none" aria-hidden>
          <div className="text-[11px] text-ink-500 font-medium">
            {t("last")}
          </div>
          <div
            className="text-sm font-mono"
            style={{ color: lastUp ? TONE_UP : TONE_DOWN }}
          >
            {formatPrice(last.close)}
          </div>
        </div>
      ) : null}

      {(loading || error || candles.length === 0) ? (
        <div className="absolute inset-0 flex items-center justify-center pointer-events-none">
          <div className="text-[11px] text-ink-500">
            {error
              ? t("failedToLoad", { error })
              : loading
                ? t("loading")
                : t("noData")}
          </div>
        </div>
      ) : null}
    </div>
  );
}

function formatPrice(v: number): string {
  if (!Number.isFinite(v)) return "—";
  if (v >= 1000) return v.toLocaleString(undefined, { maximumFractionDigits: 2 });
  if (v >= 1) return v.toFixed(2);
  if (v >= 0.01) return v.toFixed(4);
  return v.toPrecision(4);
}
