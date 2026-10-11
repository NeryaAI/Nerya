"use client";

import { copy as i18nCopy } from "../../lib/i18n";

import { useLocale } from "next-intl";

/** Only persisted execution counters are facts; legacy missing values stay unknown. */
export function BacktestExecutionEvidence({ replay, legacyBenchmark = false }: {
  replay: Record<string, unknown>; legacyBenchmark?: boolean;
}) {
  const locale = useLocale(), zh = locale.startsWith("zh");
  if (replay.order_attempts === undefined && !legacyBenchmark) return null;
  const rows = [["order_attempts", i18nCopy(zh, "copy.components_backtest_BacktestExecutionEvidence.001")],
    ["orders_submitted", i18nCopy(zh, "copy.components_backtest_BacktestExecutionEvidence.002")], ["orders_filled", i18nCopy(zh, "copy.components_backtest_BacktestExecutionEvidence.003")],
    ["orders_rejected", i18nCopy(zh, "copy.components_backtest_BacktestExecutionEvidence.004")], ["sdk_errors", i18nCopy(zh, "copy.components_backtest_BacktestExecutionEvidence.005")]];
  const reasons = replay.rejection_reasons && typeof replay.rejection_reasons === "object" ? Object.entries(replay.rejection_reasons) : [];
  const labels: Record<string, string> = { max_open_trades: "copy.components_backtest_BacktestExecutionEvidence.label_max_open_trades", confidence_below_minimum: "copy.components_backtest_BacktestExecutionEvidence.label_confidence_below_minimum",
    direct_orders_disabled: "copy.components_backtest_BacktestExecutionEvidence.label_direct_orders_disabled", insufficient_cash: "copy.components_backtest_BacktestExecutionEvidence.label_insufficient_cash", no_open_position: "copy.components_backtest_BacktestExecutionEvidence.label_no_open_position",
    position_side_mismatch: "copy.components_backtest_BacktestExecutionEvidence.label_position_side_mismatch", short_not_allowed: "copy.components_backtest_BacktestExecutionEvidence.label_short_not_allowed", missing_market_bar: "copy.components_backtest_BacktestExecutionEvidence.label_missing_market_bar" };
  return <section className="space-y-2 border-t border-[color:var(--line)] pt-3 text-xs" aria-label={i18nCopy(zh, "copy.components_backtest_BacktestExecutionEvidence.006")} data-testid="backtest-execution-evidence">
    {legacyBenchmark && <p role="status" className="text-warn leading-6">{i18nCopy(zh, "copy.components_backtest_BacktestExecutionEvidence.007")}</p>}
    <dl className="flex flex-wrap gap-x-5 gap-y-3">{rows.map(([key, label]) => {
      const value = replay[key];
      return <div key={key}><dt className="text-[color:var(--text-muted)]">{label}</dt><dd className="mt-1 font-medium tabular-nums" data-execution-metric={key}>{typeof value === "number" && Number.isFinite(value) ? value.toLocaleString(locale) : "—"}</dd></div>;
    })}</dl>
    <p className="text-[color:var(--text-muted)] leading-6">{i18nCopy(zh, "copy.components_backtest_BacktestExecutionEvidence.008")}{typeof replay.forced_closes === "number" ? ` ${i18nCopy(zh, "copy.components_backtest_BacktestExecutionEvidence.009")}: ${replay.forced_closes}` : ""}</p>
    {typeof replay.protective_closes === "number" && replay.protective_closes > 0 && <p className="text-[color:var(--text-muted)] leading-6" data-execution-metric="protective_closes">{i18nCopy(zh, "copy.components_backtest_BacktestExecutionEvidence.010")}: {replay.protective_closes.toLocaleString(locale)} · {i18nCopy(zh, "copy.components_backtest_BacktestExecutionEvidence.011")}</p>}
    {replay.order_attempts === 0 && <p role="status" className="text-warn">{i18nCopy(zh, "copy.components_backtest_BacktestExecutionEvidence.012")}</p>}
    {replay.order_accounting_ok === false && <p role="alert" className="text-danger">{i18nCopy(zh, "copy.components_backtest_BacktestExecutionEvidence.013")}</p>}
    {!!reasons.length && <details><summary className="cursor-pointer py-2">{i18nCopy(zh, "copy.components_backtest_BacktestExecutionEvidence.014")}</summary><dl className="space-y-2">{reasons.map(([key, count]) => <div key={key} className="flex justify-between gap-4"><dt title={key}>{labels[key] ? i18nCopy(zh, labels[key]) : key.replaceAll("_", " ")}</dt><dd className="tabular-nums">{String(count)}</dd></div>)}</dl></details>}
  </section>;
}
