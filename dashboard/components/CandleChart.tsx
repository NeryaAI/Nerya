"use client";

import { useEffect, useMemo, useRef } from "react";
import { useTranslations } from "next-intl";
import { createVelaChart } from "../lib/velaChart";
import type { Candle } from "../lib/api";
import { useChartTheme } from "../lib/chartTheme";

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

function toChartTime(ts: number): number {
  return ts > 1e12 ? Math.floor(ts / 1000) : ts;
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
  const chartTheme = useChartTheme();
  const containerRef = useRef<HTMLDivElement | null>(null);

  // The shared time axis requires ascending, unique timestamps.
  // Upstream APIs sometimes return descending order or duplicate ts on
  // tick boundaries; normalise before rendering and choosing the last quote.
  const cleanedCandles = useMemo(() => {
    if (!candles.length) return [] as Candle[];
    const byTs = new Map<number, Candle>();
    for (const c of candles) {
      const key = toChartTime(c.ts);
      byTs.set(key, c);
    }
    return Array.from(byTs.values()).sort(
      (a, b) =>
        toChartTime(a.ts) - toChartTime(b.ts),
    );
  }, [candles]);

  const last = useMemo(
    () => (cleanedCandles.length > 0 ? cleanedCandles[cleanedCandles.length - 1] : null),
    [cleanedCandles],
  );

  useEffect(() => {
    const container = containerRef.current;
    if (!container) return;
    const data = cleanedCandles.map(c => ({ time: toChartTime(c.ts), open: c.open, high: c.high, low: c.low, close: c.close }));
    const chart = createVelaChart(container, { height, theme: chartTheme, series: [
      mode === "candlestick" ? { type: "candlestick", name: "OHLC", data } : { type: mode, name: "Close", color: BRAND, data: data.map(c => ({ time: c.time, value: c.close })) },
      ...(showVolume ? [{ type: "histogram" as const, name: "Volume", price_format: "volume" as const,
        data: cleanedCandles.map(c => ({ time: toChartTime(c.ts), value: c.volume ?? 0, color: c.close >= c.open ? TONE_UP : TONE_DOWN })) }] : []),
    ] });
    return () => chart.destroy();
  }, [chartTheme, cleanedCandles, height, mode, showVolume, width]);

  const lastUp = last ? last.close >= last.open : true;

  return (
    <div className="relative" style={{ width: "100%", height }}>
      <div ref={containerRef} className="h-full w-full" />

      {last ? (
        /* Top-left so it never collides with the price-scale labels on
           the right edge of the chart. */
        <div className="absolute top-1 left-2 pointer-events-none" aria-hidden>
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
  if (!Number.isFinite(v)) return "-";
  if (v >= 1000) return v.toLocaleString(undefined, { maximumFractionDigits: 2 });
  if (v >= 1) return v.toFixed(2);
  if (v >= 0.01) return v.toFixed(4);
  return v.toPrecision(4);
}
