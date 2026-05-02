"use client";

/**
 * Pure SVG candlestick / line chart.
 *
 * Deliberately dependency-free: the dashboard ships without `recharts`,
 * `lightweight-charts` or any other heavy charting bundle. Everything here
 * is readable enough to extend (volume bars, moving averages, etc.).
 */

import { useMemo } from "react";
import { useTranslations } from "next-intl";
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

const TONE_UP = "#10d993";   // neon mint
const TONE_DOWN = "#ef4560"; // danger
const BRAND = "#b48bff";     // brand violet
const GRID = "rgba(139,92,246,0.12)";
const TEXT = "rgba(202,201,225,0.55)";

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
  const padding = { top: 8, right: 56, bottom: 20, left: 6 };
  const volumeH = showVolume ? Math.max(28, Math.round(height * 0.18)) : 0;
  const chartH = height - padding.top - padding.bottom - volumeH;

  const normalized = useMemo(() => {
    if (!candles || candles.length === 0) {
      return { candles: [], min: 0, max: 1, vmax: 1 };
    }
    let min = Infinity;
    let max = -Infinity;
    let vmax = 0;
    for (const c of candles) {
      if (c.low < min) min = c.low;
      if (c.high > max) max = c.high;
      if ((c.volume ?? 0) > vmax) vmax = c.volume ?? 0;
    }
    if (min === Infinity) { min = 0; max = 1; }
    if (min === max) max = min + 1;
    return { candles, min, max, vmax: vmax || 1 };
  }, [candles]);

  const innerW = width - padding.left - padding.right;
  const n = normalized.candles.length || 1;
  const step = innerW / Math.max(1, n);
  const bodyW = Math.max(2, step * 0.66);

  const yPrice = (v: number) =>
    padding.top + (1 - (v - normalized.min) / (normalized.max - normalized.min)) * chartH;

  // gridlines: 4 horizontal
  const gridlines: number[] = [];
  for (let i = 0; i <= 4; i++) {
    gridlines.push(padding.top + (chartH * i) / 4);
  }

  // price labels (5)
  const priceLabels: { y: number; text: string }[] = [];
  for (let i = 0; i <= 4; i++) {
    const v = normalized.max - ((normalized.max - normalized.min) * i) / 4;
    priceLabels.push({ y: padding.top + (chartH * i) / 4, text: formatPrice(v) });
  }

  // current price
  const last = normalized.candles[normalized.candles.length - 1];
  const lastUp = last ? last.close >= last.open : true;

  // line / area geometry
  const linePoints = normalized.candles.map((c, i) => {
    const x = padding.left + step * i + step / 2;
    const y = yPrice(c.close);
    return `${x.toFixed(1)},${y.toFixed(1)}`;
  }).join(" ");

  const areaPoints = (() => {
    if (!normalized.candles.length) return "";
    const pts = linePoints.split(" ");
    const first = pts[0].split(",");
    const lastPt = pts[pts.length - 1].split(",");
    return `${first[0]},${padding.top + chartH} ${linePoints} ${lastPt[0]},${padding.top + chartH}`;
  })();

  return (
    <div className="relative" style={{ width: "100%", height }}>
      <svg
        viewBox={`0 0 ${width} ${height}`}
        width="100%"
        height="100%"
        preserveAspectRatio="none"
        style={{ display: "block" }}
      >
        <defs>
          <linearGradient id="nerya-ccl-area" x1="0" x2="0" y1="0" y2="1">
            <stop offset="0%" stopColor={BRAND} stopOpacity="0.35" />
            <stop offset="100%" stopColor={BRAND} stopOpacity="0.0" />
          </linearGradient>
        </defs>

        {/* grid */}
        {gridlines.map((y, i) => (
          <line
            key={`gl-${i}`}
            x1={padding.left}
            x2={padding.left + innerW}
            y1={y}
            y2={y}
            stroke={GRID}
            strokeDasharray="3 4"
          />
        ))}

        {/* price labels on the right edge */}
        {priceLabels.map((pl, i) => (
          <text
            key={`pl-${i}`}
            x={padding.left + innerW + 6}
            y={pl.y + 3}
            fontSize="10"
            fontFamily="ui-monospace, SFMono-Regular, Menlo, monospace"
            fill={TEXT}
          >
            {pl.text}
          </text>
        ))}

        {/* area / line */}
        {mode === "area" && normalized.candles.length > 1 ? (
          <polygon points={areaPoints} fill="url(#nerya-ccl-area)" stroke="none" />
        ) : null}
        {(mode === "line" || mode === "area") && normalized.candles.length > 1 ? (
          <polyline
            points={linePoints}
            fill="none"
            stroke={BRAND}
            strokeWidth={1.6}
            strokeLinejoin="round"
            strokeLinecap="round"
          />
        ) : null}

        {/* candlesticks */}
        {mode === "candlestick" && normalized.candles.map((c, i) => {
          const x = padding.left + step * i + step / 2;
          const up = c.close >= c.open;
          const color = up ? TONE_UP : TONE_DOWN;
          const yOpen = yPrice(c.open);
          const yClose = yPrice(c.close);
          const yHigh = yPrice(c.high);
          const yLow = yPrice(c.low);
          const bodyY = Math.min(yOpen, yClose);
          const bodyH = Math.max(1, Math.abs(yClose - yOpen));
          return (
            <g key={`k-${i}`}>
              <line x1={x} x2={x} y1={yHigh} y2={yLow} stroke={color} strokeWidth={1} />
              <rect
                x={x - bodyW / 2}
                y={bodyY}
                width={bodyW}
                height={bodyH}
                fill={color}
                opacity={up ? 0.9 : 0.95}
              />
            </g>
          );
        })}

        {/* volume bars */}
        {showVolume && normalized.candles.map((c, i) => {
          const x = padding.left + step * i + step / 2;
          const h = ((c.volume ?? 0) / normalized.vmax) * (volumeH - 6);
          const y = height - padding.bottom - h;
          const up = c.close >= c.open;
          return (
            <rect
              key={`v-${i}`}
              x={x - bodyW / 2}
              y={y}
              width={bodyW}
              height={Math.max(1, h)}
              fill={up ? TONE_UP : TONE_DOWN}
              opacity={0.35}
            />
          );
        })}

        {/* x axis baseline */}
        <line
          x1={padding.left}
          x2={padding.left + innerW}
          y1={height - padding.bottom}
          y2={height - padding.bottom}
          stroke={GRID}
        />
      </svg>

      {/* overlay: last-price tag */}
      {last ? (
        <div
          className="absolute top-1 right-1 text-right pointer-events-none"
          aria-hidden
        >
          <div className="text-[10px] text-ink-500 uppercase tracking-wider">
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

      {(loading || error || normalized.candles.length === 0) ? (
        <div className="absolute inset-0 flex items-center justify-center">
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
