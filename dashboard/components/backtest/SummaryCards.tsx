"use client";
import { copy as i18nCopy } from "../../lib/i18n";

import { useLocale } from "next-intl";
import { finiteNumber, financeNumber } from "../../lib/financeDisplay";

const labels: Record<string, string> = { totalreturnpct: "copy.backtestMetrics.001", totalreturn: "copy.backtestMetrics.002", annualreturnpct: "copy.backtestMetrics.003", maxdrawdownpct: "copy.backtestMetrics.004", maxdrawdown: "copy.backtestMetrics.005", sharpe: "copy.backtestMetrics.006", sharperatio: "copy.backtestMetrics.007", sortino: "copy.backtestMetrics.008", sortinoratio: "copy.backtestMetrics.009", calmar: "copy.backtestMetrics.010", winratepct: "copy.backtestMetrics.011", profitfactor: "copy.backtestMetrics.012", trades: "copy.backtestMetrics.013", totaltrades: "copy.backtestMetrics.014", fees: "copy.backtestMetrics.015", feesusd: "copy.backtestMetrics.016", netpnl: "copy.backtestMetrics.017", netpnlusd: "copy.backtestMetrics.018" };
export function SummaryCards({ cards }: { cards: Array<Record<string, unknown>> }) {
  const locale = useLocale(), zh = locale.startsWith("zh");
  const groups = [
    { id: "profit", title: i18nCopy(zh, "copy.components_backtest_SummaryCards.001") },
    { id: "risk", title: i18nCopy(zh, "copy.components_backtest_SummaryCards.002") },
    { id: "trades", title: i18nCopy(zh, "copy.components_backtest_SummaryCards.003") },
  ];
  const rows = cards.map((card) => {
    const raw = String(card.label ?? "Metric"), key = raw.toLowerCase().replace(/[^a-z0-9]/g, "");
    const group = /drawdown|sharpe|sortino|calmar|volatility|exposure/.test(raw.toLowerCase()) ? "risk" : /trade|win|loss|profitfactor/.test(`${key}${raw}`) ? "trades" : "profit";
    const n = finiteNumber(card.value);
    const suffix = /pct|percent/i.test(raw) ? "%" : "";
    const value = n !== null ? `${financeNumber(n, locale, /trades?$/.test(key) ? 0 : Math.abs(n) < 1 ? suffix ? 4 : 6 : 2)}${suffix}` : typeof card.value === "string" && card.value.trim() && !/^(null|undefined|nan|inf(inity)?|[-+]?∞)$/i.test(card.value) ? card.value : "—";
    const extraLabels: Record<string, string> = { verdict: "copy.components_backtest_SummaryCards.label_verdict", totalmissedprofitpct: "copy.components_backtest_SummaryCards.label_totalmissedprofitpct", exposurepct: "copy.components_backtest_SummaryCards.label_exposurepct", benchmarkbuyholdreturnpct: "copy.components_backtest_SummaryCards.label_benchmarkbuyholdreturnpct", alphavsbenchmarkpct: "copy.components_backtest_SummaryCards.label_alphavsbenchmarkpct" };
    const display = key === "verdict" ? ({ PASS: i18nCopy(zh,"copy.components_backtest_SummaryCards.label_PASS"), WARN: i18nCopy(zh,"copy.components_backtest_SummaryCards.label_WARN"), FAIL: i18nCopy(zh,"copy.components_backtest_SummaryCards.label_FAIL") } as Record<string, string>)[value] || value : value;
    return { raw, label: labels[key] ? i18nCopy(zh, labels[key]) : extraLabels[key] ? i18nCopy(zh, extraLabels[key]) : raw.replace(/_/g, " "), group, value: display, tone: String(card.tone || "") };
  });
  return <div className="grid min-w-0 gap-5" style={{ gridTemplateColumns: "repeat(auto-fit,minmax(min(100%,220px),1fr))" }} data-testid="backtest-metrics">{groups.filter((group) => rows.some((r) => r.group === group.id)).map((group) => <section key={group.id} aria-label={group.title} className="min-w-0 border-t border-[color:var(--line)] pt-3"><h3 className="mb-3 text-xs font-medium text-[color:var(--text-muted)]">{group.title}</h3><dl className="grid grid-cols-2 gap-x-5 gap-y-4">{rows.filter((r) => r.group === group.id).map((row, i) => <div key={`${row.raw}:${i}`}><dt className="break-words text-xs text-[color:var(--text-muted)]" title={row.raw}>{row.label}</dt><dd className={`mt-1 break-words text-lg font-semibold tabular-nums ${row.tone === "positive" ? "text-ok" : row.tone === "negative" ? "text-danger" : row.tone === "warning" ? "text-warn" : "text-[color:var(--text-base)]"}`}>{row.value}</dd></div>)}</dl></section>)}</div>;
}
