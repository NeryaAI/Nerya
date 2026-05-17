"use client";

import { useEffect, useMemo, useRef, useState } from "react";
import {
  createChart,
  type IChartApi,
  type ISeriesApi,
  type Time,
} from "lightweight-charts";
import { useTranslations } from "next-intl";
import { Card, Empty, ErrorBanner } from "../Page";
import { clientApi, type AccountEquityPoint } from "../../lib/clientApi";

type RangeId = "1h" | "24h" | "7d" | "30d" | "all";

type RangeConfig = {
  id: RangeId;
  windowSeconds: number | null;
  bucketSeconds: number | null;
  defaultLimit: number;
};

const RANGE_CONFIGS: RangeConfig[] = [
  { id: "1h", windowSeconds: 60 * 60, bucketSeconds: 30, defaultLimit: 240 },
  {
    id: "24h",
    windowSeconds: 24 * 60 * 60,
    bucketSeconds: 5 * 60,
    defaultLimit: 360,
  },
  {
    id: "7d",
    windowSeconds: 7 * 24 * 60 * 60,
    bucketSeconds: 30 * 60,
    defaultLimit: 400,
  },
  {
    id: "30d",
    windowSeconds: 30 * 24 * 60 * 60,
    bucketSeconds: 2 * 60 * 60,
    defaultLimit: 480,
  },
  { id: "all", windowSeconds: null, bucketSeconds: null, defaultLimit: 1000 },
];

function fmtUsd(value: number, currency: string = "USDT"): string {
  if (!Number.isFinite(value)) return "—";
  const abs = Math.abs(value);
  const digits = abs >= 1000 ? 2 : abs >= 1 ? 4 : 6;
  return `${value.toLocaleString(undefined, {
    minimumFractionDigits: digits,
    maximumFractionDigits: digits,
  })} ${currency}`;
}

function fmtPctChange(start: number, end: number): {
  text: string;
  positive: boolean;
} | null {
  if (!Number.isFinite(start) || !Number.isFinite(end) || start === 0) {
    return null;
  }
  const pct = ((end - start) / Math.abs(start)) * 100;
  if (!Number.isFinite(pct)) return null;
  return {
    text: `${pct >= 0 ? "+" : ""}${pct.toFixed(2)}%`,
    positive: pct >= 0,
  };
}

/**
 * Account NAV / equity curve card.
 *
 * Pulls per-account snapshot history from ``/accounts/equity_curve``
 * (backed by the same ``account_snapshots`` SQLite table that the
 * background refresh loop writes into). Renders an area + line chart
 * via lightweight-charts and lets the operator switch range buckets
 * (1H / 24H / 7D / 30D / ALL).
 *
 * The card is intentionally self-contained: it fetches its own data
 * on mount and on range change, and refreshes every 30s so the live
 * accounts' fund curve stays close to real-time without the parent
 * page having to coordinate the refresh.
 */
export function AccountEquityCurveCard({
  accountId,
  currency = "USDT",
}: {
  accountId: string;
  currency?: string;
}) {
  const t = useTranslations("accountEquity");
  const [range, setRange] = useState<RangeId>("24h");
  const [points, setPoints] = useState<AccountEquityPoint[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [node, setNode] = useState<HTMLDivElement | null>(null);
  const chartRef = useRef<IChartApi | null>(null);
  const areaRef = useRef<ISeriesApi<"Area"> | null>(null);

  const config = useMemo<RangeConfig>(
    () => RANGE_CONFIGS.find((r) => r.id === range) ?? RANGE_CONFIGS[1],
    [range],
  );

  async function load() {
    setLoading(true);
    setError(null);
    try {
      const nowSec = Math.floor(Date.now() / 1000);
      const body: {
        account_id: string;
        since_ts?: number;
        limit?: number;
        bucket_seconds?: number;
      } = { account_id: accountId, limit: config.defaultLimit };
      if (config.windowSeconds != null) {
        body.since_ts = nowSec - config.windowSeconds;
      }
      if (config.bucketSeconds != null) {
        body.bucket_seconds = config.bucketSeconds;
      }
      const res = await clientApi.accountsEquityCurve(body);
      if (!res.ok) {
        throw new Error(res.error || res.detail || "equity_curve_failed");
      }
      setPoints(res.points ?? []);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setLoading(false);
    }
  }

  useEffect(() => {
    void load();
    const ticker = setInterval(() => {
      void load();
    }, 30_000);
    return () => clearInterval(ticker);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [accountId, range]);

  // Mount/dispose the chart whenever the host node or range changes.
  useEffect(() => {
    if (!node) return;
    const api = createChart(node, {
      height: 260,
      layout: {
        background: { color: "transparent" },
        textColor: "#cbd5e1",
        attributionLogo: false,
      },
      grid: {
        vertLines: { color: "rgba(148,163,184,.10)" },
        horzLines: { color: "rgba(148,163,184,.10)" },
      },
      rightPriceScale: { borderColor: "rgba(148,163,184,.2)" },
      timeScale: {
        borderColor: "rgba(148,163,184,.2)",
        timeVisible: true,
        secondsVisible: range === "1h",
      },
      crosshair: { mode: 1 },
    });
    chartRef.current = api;
    const series = api.addAreaSeries({
      lineColor: "#22c55e",
      topColor: "rgba(34,197,94,.28)",
      bottomColor: "rgba(34,197,94,.02)",
      lineWidth: 2,
      priceFormat: { type: "price", precision: 2, minMove: 0.01 },
    });
    areaRef.current = series;
    const ro = new ResizeObserver(() => {
      api.applyOptions({ width: Math.max(320, node.clientWidth) });
    });
    ro.observe(node);
    api.applyOptions({ width: Math.max(320, node.clientWidth) });
    return () => {
      ro.disconnect();
      api.remove();
      chartRef.current = null;
      areaRef.current = null;
    };
  }, [node, range]);

  // Push fresh data into the area series whenever `points` changes.
  useEffect(() => {
    const series = areaRef.current;
    const api = chartRef.current;
    if (!series || !api) return;
    if (points.length === 0) {
      series.setData([]);
      return;
    }
    // Lightweight-charts requires strictly-increasing timestamps; the
    // backend already returns ASC order but we de-dup on the boundary
    // here to be safe (bucket boundaries on the SQL side can land on
    // the same epoch second under fast refresh cadences).
    const sorted = [...points].sort((a, b) => a.ts - b.ts);
    const data: { time: Time; value: number }[] = [];
    let prevTs = -Infinity;
    for (const row of sorted) {
      const sec = Math.floor(row.ts);
      if (sec <= prevTs) continue;
      prevTs = sec;
      data.push({ time: sec as Time, value: row.nav_usd });
    }
    series.setData(data);
    api.timeScale().fitContent();
  }, [points]);

  const stats = useMemo(() => {
    if (points.length === 0) return null;
    const navs = points.map((p) => p.nav_usd).filter(Number.isFinite);
    if (navs.length === 0) return null;
    const start = navs[0];
    const end = navs[navs.length - 1];
    const min = Math.min(...navs);
    const max = Math.max(...navs);
    return { start, end, min, max, change: fmtPctChange(start, end) };
  }, [points]);

  return (
    <Card
      title={t("title")}
      description={t("description")}
      actions={
        <div className="flex items-center gap-1.5">
          {RANGE_CONFIGS.map((cfg) => (
            <button
              key={cfg.id}
              onClick={() => setRange(cfg.id)}
              className={`text-[11px] px-2 py-0.5 rounded border ${
                range === cfg.id
                  ? "border-brand-400/50 bg-brand-500/15 text-brand-100"
                  : "border-brand-500/10 text-ink-400 hover:text-ink-200"
              }`}
              aria-pressed={range === cfg.id}
            >
              {t(`range.${cfg.id}`)}
            </button>
          ))}
          <button
            onClick={() => void load()}
            disabled={loading}
            className="btn-ghost text-[11px] py-0.5 ml-1.5"
          >
            {loading ? t("loading") : t("refresh")}
          </button>
        </div>
      }
      padded={false}
    >
      <div className="px-3 pt-2 pb-3">
        {error ? <ErrorBanner error={error} /> : null}
        {!error && points.length === 0 && !loading ? (
          <Empty label={t("empty")} />
        ) : null}
        {points.length > 0 ? (
          <div className="mb-2 grid grid-cols-2 md:grid-cols-4 gap-2 text-[11px]">
            <Stat label={t("statCurrent")} value={fmtUsd(stats?.end ?? NaN, currency)} tone="brand" />
            <Stat
              label={t("statChange")}
              value={stats?.change?.text || "—"}
              tone={
                stats?.change == null
                  ? "neutral"
                  : stats.change.positive
                    ? "ok"
                    : "danger"
              }
            />
            <Stat label={t("statMin")} value={fmtUsd(stats?.min ?? NaN, currency)} />
            <Stat label={t("statMax")} value={fmtUsd(stats?.max ?? NaN, currency)} />
          </div>
        ) : null}
        <div ref={setNode} className="w-full" />
        <div className="mt-1 text-[10px] text-ink-500 font-mono">
          {t("footnote", { count: points.length })}
        </div>
      </div>
    </Card>
  );
}

function Stat({
  label,
  value,
  tone = "neutral",
}: {
  label: string;
  value: string;
  tone?: "neutral" | "brand" | "ok" | "danger";
}) {
  const toneClass =
    tone === "brand"
      ? "text-brand-200"
      : tone === "ok"
        ? "text-accent-200"
        : tone === "danger"
          ? "text-[#ff8a9a]"
          : "text-ink-100";
  return (
    <div className="rounded border border-brand-500/10 px-2 py-1.5">
      <div className="text-ink-500">{label}</div>
      <div className={`font-mono ${toneClass}`}>{value}</div>
    </div>
  );
}
