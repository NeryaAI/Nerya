"use client";

import { copy as i18nCopy } from "../../lib/i18n";

import { useLocale } from "next-intl";
import { backtestCoverage } from "../../lib/backtestCoverage";

export function BacktestCoverage({ meta, compact = false }: { meta: Record<string, unknown>; compact?: boolean }) {
  const locale = useLocale(), zh = locale.startsWith("zh");
  const coverage = backtestCoverage(meta);
  const number = (value: number | null) => value === null ? "—" : value.toLocaleString(locale, { maximumFractionDigits: 2 });
  if (coverage.requestedDays === null && coverage.recordedDays === null) return null;
  return <section className={`rounded-lg border border-[color:var(--line)] px-3 py-2 text-xs ${coverage.state === "partial" ? "text-warn" : "text-[color:var(--text-muted)]"}`}
    data-testid="backtest-coverage" data-coverage-status={coverage.state} data-recorded-start={coverage.start ?? undefined} data-recorded-end={coverage.end ?? undefined}>
    <div className="flex flex-wrap items-center gap-x-5 gap-y-1 tabular-nums">
      <span>{i18nCopy(zh, "copy.components_backtest_BacktestCoverage.001")} <strong>{number(coverage.requestedDays)}</strong> {i18nCopy(zh, "copy.components_backtest_BacktestCoverage.002")}</span>
      <span>{i18nCopy(zh, "copy.components_backtest_BacktestCoverage.003")} <strong>{number(coverage.recordedDays)}</strong> {i18nCopy(zh, "copy.components_backtest_BacktestCoverage.004")}</span>
      <span>{coverage.state === "partial" ? (i18nCopy(zh, "copy.components_backtest_BacktestCoverage.005")) : coverage.state === "complete" ? (i18nCopy(zh, "copy.components_backtest_BacktestCoverage.006")) : (i18nCopy(zh, "copy.components_backtest_BacktestCoverage.007"))}</span>
    </div>
    {coverage.state === "partial" && <p className="mt-1 leading-5">{i18nCopy(zh, "copy.components_backtest_BacktestCoverage.008")}</p>}
    {!compact && coverage.start !== null && coverage.end !== null && <p className="mt-1 leading-5">{new Date(coverage.start).toISOString().slice(0, 10)} — {new Date(coverage.end).toISOString().slice(0, 10)} UTC · {i18nCopy(zh, "copy.components_backtest_BacktestCoverage.009")}</p>}
  </section>;
}
