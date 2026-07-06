"use client";

/**
 * Compact backtest summary shown on each strategy card: a mini equity sparkline
 * (strategy vs. buy&hold), the latest backtest return, verdict, and max
 * drawdown. Fetches its own data so the strategies list stays a light payload.
 * Numbers are backtest-only (labelled as such) — never live/paper P&L.
 */
import { useEffect, useState } from "react";
import { useTranslations } from "next-intl";
import { clientApi } from "../../lib/clientApi";

type Mini = {
  returnPct?: number;
  ddPct?: number;
  verdict?: string;
  winRatePct?: number;
  tradeCount?: number;
  spark: number[];
  bench: number[];
};

function downsample(arr: number[], n: number): number[] {
  if (arr.length <= n) return arr;
  const out: number[] = [];
  const step = (arr.length - 1) / (n - 1);
  for (let i = 0; i < n; i++) out.push(arr[Math.round(i * step)]);
  return out;
}

function polyline(vals: number[], lo: number, hi: number, w: number, h: number, pad = 2): string {
  if (!vals.length) return "";
  const span = hi - lo || 1;
  const n = vals.length;
  return vals
    .map((v, i) => {
      const x = n === 1 ? pad : (i / (n - 1)) * (w - pad * 2) + pad;
      const y = h - pad - ((v - lo) / span) * (h - pad * 2);
      return `${x.toFixed(1)},${y.toFixed(1)}`;
    })
    .join(" ");
}

function numberFromSummary(cards: Array<Record<string, unknown>> | undefined, label: string): number | undefined {
  const card = (cards || []).find((item) => String(item.label || "") === label);
  const value = Number(card?.value);
  return Number.isFinite(value) ? value : undefined;
}

function textFromSummary(cards: Array<Record<string, unknown>> | undefined, label: string): string | undefined {
  const card = (cards || []).find((item) => String(item.label || "") === label);
  return typeof card?.value === "string" ? card.value : undefined;
}

function seriesValues(series: Array<{ data: Array<Record<string, unknown>> }> | undefined, key: string): number[] {
  return (series?.[0]?.data ?? [])
    .map((d) => Number(d[key]))
    .filter(Number.isFinite);
}

function tradeStats(rows: unknown[][] | undefined, retIdx: number): { tradeCount?: number; winRatePct?: number } {
  const trades = rows || [];
  if (!trades.length || retIdx < 0) return {};
  const wins = trades.filter((row) => Number(row[retIdx]) > 0).length;
  return { tradeCount: trades.length, winRatePct: (wins / trades.length) * 100 };
}

export function StrategyCardSpark({ strategyId }: { strategyId: string }) {
  const t = useTranslations("strategies");
  const [mini, setMini] = useState<Mini | null>(null);

  useEffect(() => {
    let cancelled = false;
    (async () => {
      try {
        const bl = await clientApi.strategyBacktests(strategyId);
        const runs = (bl?.backtests ?? [])
          .slice()
          .sort((a, b) => String(b.ts).localeCompare(String(a.ts)));
        const latest = runs[0];
        if (!latest) return;
        const env = await clientApi.strategyBacktestChart(strategyId, latest.ts);
        const chart = env?.chart;
        const eq = chart?.panels?.find((p) => p.id === "equity");
        const price = chart?.panels?.find((p) => p.id === "price");
        const trades = chart?.tables?.find((table) => table.id === "trades");
        const retIdx = trades?.columns?.findIndex((c) => c === "ret_pct") ?? -1;
        const stats = tradeStats(trades?.rows, retIdx);
        const eqSpark = seriesValues(eq?.series, "value");
        const priceSpark = seriesValues(price?.series, "close");
        const spark = downsample(eqSpark.length ? eqSpark : priceSpark, 48);
        const bench = downsample(
          eq?.series?.[1]?.data
            ?.map((d) => Number((d as { value?: unknown }).value))
            .filter(Number.isFinite) ?? [],
          48,
        );
        if (!cancelled) {
          setMini({
            returnPct:
              latest.total_return_pct ??
              numberFromSummary(chart?.summary_cards, "total_return_pct"),
            ddPct:
              latest.max_dd_pct ??
              numberFromSummary(chart?.summary_cards, "max_drawdown_pct"),
            verdict:
              latest.verdict ??
              textFromSummary(chart?.summary_cards, "verdict"),
            winRatePct: stats.winRatePct,
            tradeCount: stats.tradeCount,
            spark,
            bench,
          });
        }
      } catch {
        /* no backtest for this strategy — leave the reserved placeholder */
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [strategyId]);

  if (!mini || !mini.spark.length) {
    return <div className="relative h-[96px] rounded-lg" style={{ background: "rgba(255,255,255,0.02)" }} />;
  }

  const W = 260;
  const H = 58;
  const all = mini.bench.length ? mini.spark.concat(mini.bench) : mini.spark;
  const lo = Math.min(...all);
  const hi = Math.max(...all);
  const up = (mini.returnPct ?? 0) >= 0;
  const stroke = up ? "#34d399" : "#fb7185";
  const fill = up ? "rgba(52,211,153,0.14)" : "rgba(251,113,133,0.14)";
  const sp = polyline(mini.spark, lo, hi, W, H);
  const bp = mini.bench.length ? polyline(mini.bench, lo, hi, W, H) : "";
  const area = sp ? `2,${H - 2} ${sp} ${W - 2},${H - 2}` : "";
  const v = (mini.verdict || "").toUpperCase();
  const vtone =
    v === "PASS"
      ? "text-emerald-400 border-emerald-400/40 bg-emerald-400/10"
      : v === "FAIL"
        ? "text-rose-400 border-rose-400/40 bg-rose-400/10"
        : "text-amber-400 border-amber-400/40 bg-amber-400/10";

  return (
    <div className="relative">
      <svg viewBox={`0 0 ${W} ${H}`} preserveAspectRatio="none" className="h-[58px] w-full">
        {area ? <polygon points={area} fill={fill} /> : null}
        {bp ? (
          <polyline points={bp} fill="none" stroke="rgba(148,163,184,0.5)" strokeWidth="1" strokeDasharray="3 3" />
        ) : null}
        <polyline points={sp} fill="none" stroke={stroke} strokeWidth="1.7" strokeLinejoin="round" strokeLinecap="round" />
      </svg>
      <div className="mt-1 flex items-center justify-between gap-2">
        <div className="flex items-center gap-1.5">
          <span className="text-[11px] text-[color:var(--text-muted)]">{t("backtestReturn")}</span>
          <span className={`text-[15px] font-semibold tabular-nums ${up ? "text-emerald-400" : "text-rose-400"}`}>
            {mini.returnPct != null ? `${up ? "+" : ""}${mini.returnPct.toFixed(1)}%` : "–"}
          </span>
          {v ? <span className={`rounded-full border px-1.5 py-0.5 text-[10px] font-medium ${vtone}`}>{v}</span> : null}
        </div>
        {mini.ddPct != null ? (
          <span className="text-[11px] tabular-nums text-[color:var(--text-muted)]">
            {t("maxDrawdown")} {Math.abs(mini.ddPct).toFixed(1)}%
          </span>
        ) : null}
      </div>
      <div className="mt-2 grid grid-cols-3 gap-2 border-t border-[color:var(--line)] pt-2 text-[11px] text-[color:var(--text-muted)]">
        <span className="tabular-nums">
          {t("winRate")}{" "}
          <b className="font-medium text-[color:var(--text-base)]">
            {mini.winRatePct != null ? `${mini.winRatePct.toFixed(1)}%` : "–"}
          </b>
        </span>
        <span className="tabular-nums">
          {t("trades")}{" "}
          <b className="font-medium text-[color:var(--text-base)]">
            {mini.tradeCount ?? "–"}
          </b>
        </span>
        <span className="text-right text-[10px] font-medium text-violet-300">
          {t("backtestOnly")}
        </span>
      </div>
    </div>
  );
}
