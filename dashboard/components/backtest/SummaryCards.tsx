"use client";

import { useLocale } from "next-intl";
import { finiteNumber, financeNumber } from "../../lib/financeDisplay";

const labels: Record<string, [string, string]> = {
  totalreturnpct: ["总收益率", "Total return"], totalreturn: ["总收益", "Total return"], annualreturnpct: ["年化收益率", "Annualized return"],
  maxdrawdownpct: ["最大回撤", "Max drawdown"], maxdrawdown: ["最大回撤", "Max drawdown"], sharpe: ["夏普比率", "Sharpe ratio"], sharperatio: ["夏普比率", "Sharpe ratio"],
  sortino: ["索提诺比率", "Sortino ratio"], sortinoratio: ["索提诺比率", "Sortino ratio"], calmar: ["卡玛比率", "Calmar ratio"],
  winratepct: ["胜率", "Win rate"], profitfactor: ["盈利因子", "Profit factor"], trades: ["成交笔数", "Trades"], totaltrades: ["成交笔数", "Total trades"],
  fees: ["费用", "Fees"], feesusd: ["费用 · USD", "Fees · USD"], netpnl: ["净盈亏", "Net P&L"], netpnlusd: ["净盈亏 · USD", "Net P&L · USD"],
};
export function SummaryCards({ cards }: { cards: Array<Record<string, unknown>> }) {
  const locale = useLocale(), zh = locale.startsWith("zh");
  const groups = [
    { id: "profit", title: zh ? "收益与成本" : "Return and costs" },
    { id: "risk", title: zh ? "风险" : "Risk" },
    { id: "trades", title: zh ? "成交质量" : "Trade quality" },
  ];
  const rows = cards.map((card) => {
    const raw = String(card.label ?? "Metric"), key = raw.toLowerCase().replace(/[^a-z0-9]/g, "");
    const group = /drawdown|sharpe|sortino|calmar|volatility|exposure|回撤|风险/.test(raw.toLowerCase()) ? "risk" : /trade|win|loss|profitfactor|胜率|成交/.test(`${key}${raw}`) ? "trades" : "profit";
    const n = finiteNumber(card.value);
    const suffix = /pct|percent/i.test(raw) ? "%" : "";
    const value = n !== null ? `${financeNumber(n, locale, /trades?$/.test(key) ? 0 : Math.abs(n) < 1 && !suffix ? 6 : 2)}${suffix}` : typeof card.value === "string" && card.value.trim() && !/^(null|undefined|nan|inf(inity)?|[-+]?∞)$/i.test(card.value) ? card.value : "—";
    return { raw, label: labels[key]?.[zh ? 0 : 1] || raw.replace(/_/g, " "), group, value, tone: String(card.tone || "") };
  });
  return <div className="grid min-w-0 gap-5 lg:grid-cols-3" data-testid="backtest-metrics">{groups.filter((group) => rows.some((r) => r.group === group.id)).map((group) => <section key={group.id} aria-label={group.title} className="min-w-0 border-t border-[color:var(--line)] pt-3"><h3 className="mb-3 text-xs font-medium text-[color:var(--text-muted)]">{group.title}</h3><dl className="grid grid-cols-2 gap-x-5 gap-y-4">{rows.filter((r) => r.group === group.id).map((row, i) => <div key={`${row.raw}:${i}`}><dt className="break-words text-xs text-[color:var(--text-muted)]" title={row.raw}>{row.label}</dt><dd className={`mt-1 break-words text-lg font-semibold tabular-nums ${row.tone === "positive" ? "text-ok" : row.tone === "negative" ? "text-danger" : row.tone === "warning" ? "text-warn" : "text-[color:var(--text-base)]"}`}>{row.value}</dd></div>)}</dl></section>)}</div>;
}
