"use client";

import { copy as i18nCopy } from "../../lib/i18n";

import { useLocale } from "next-intl";
import styles from "./BacktestBiasChecks.module.css";

const object = (value: unknown): Record<string, unknown> => value && typeof value === "object" && !Array.isArray(value) ? value as Record<string, unknown> : {};
const text = (value: unknown) => typeof value === "string" ? value : "";
const labels: Record<string, string> = {
  dynamic_lookahead: "copy.components_backtest_BacktestBiasChecks.001",
  warmup_stability: "copy.components_backtest_BacktestBiasChecks.002",
  out_of_sample: "copy.components_backtest_BacktestBiasChecks.003",
  walk_forward: "copy.components_backtest_BacktestBiasChecks.004",
  cost_stress: "copy.components_backtest_BacktestBiasChecks.005",
  parameter_sensitivity: "copy.components_backtest_BacktestBiasChecks.006",
  ablation: "copy.components_backtest_BacktestBiasChecks.007",
};
const limitLabels: Record<string, string> = {
  funding: "copy.components_backtest_BacktestBiasChecks.008", liquidation: "copy.components_backtest_BacktestBiasChecks.009",
  partial_fills: "copy.components_backtest_BacktestBiasChecks.010", maker_taker_fee_split: "copy.components_backtest_BacktestBiasChecks.011",
  exchange_precision_and_minimums: "copy.components_backtest_BacktestBiasChecks.012",
  extra_latency_beyond_next_bar: "copy.components_backtest_BacktestBiasChecks.013",
};

/** Render saved receipts only. Runtime constraints are not per-run dynamic audits. */
export function BacktestBiasChecks({ meta, compact = false }: { meta: Record<string, unknown>; compact?: boolean }) {
  const zh = useLocale().startsWith("zh");
  const checks = object(meta.bias_checks), research = object(meta.research_checks);
  const warnings = (Array.isArray(checks.static_warnings) ? checks.static_warnings : []).map(object)
    .filter(row => text(row.message) || text(row.code));
  const pending = (Array.isArray(research.checks) ? research.checks : []).map(object);
  const recorded = Object.entries(labels).map(([id, label]) => ({ id, label: i18nCopy(zh, label ?? ""),
    status: pending.find(row => row.id === id)?.status }));
  const notRun = recorded.filter(row => row.status === "not_run").length;
  const hasResearch = recorded.some(row => typeof row.status === "string");
  const failed = checks.static_temporal_scan === "failed";
  const passed = checks.static_temporal_scan === "passed";
  const staticState = failed ? "failed" : warnings.length ? "review" : passed ? "clear" : "not_recorded";
  const staticLabel = failed ? (i18nCopy(zh, "copy.components_backtest_BacktestBiasChecks.014")) : warnings.length
    ? (i18nCopy(zh, "copy.components_backtest_BacktestBiasChecks.015", { value0: warnings.length }))
    : passed ? (i18nCopy(zh, "copy.components_backtest_BacktestBiasChecks.016")) : (i18nCopy(zh, "copy.components_backtest_BacktestBiasChecks.017"));
  const unknown = i18nCopy(zh, "copy.components_backtest_BacktestBiasChecks.018");
  const statusLabel = (value: unknown) => value === "not_run" ? (i18nCopy(zh, "copy.components_backtest_BacktestBiasChecks.019"))
    : value === "blocked" ? (i18nCopy(zh, "copy.components_backtest_BacktestBiasChecks.020")) : value === "inconclusive" ? (i18nCopy(zh, "copy.components_backtest_BacktestBiasChecks.021"))
    : value === "failed" ? (i18nCopy(zh, "copy.components_backtest_BacktestBiasChecks.022")) : typeof value === "string"
      ? (i18nCopy(zh, "copy.components_backtest_BacktestBiasChecks.023")) : unknown;
  const assumptions = object(object(meta.provenance).assumptions), limits = object(assumptions.execution_model_limits);
  const omitted = Object.keys(limitLabels).filter(key => limits[key] === "not_modeled");
  const runtime = [
    [i18nCopy(zh, "copy.components_backtest_BacktestBiasChecks.024"), checks.historical_prefix_only === true && checks.closed_bar_context === true ? (i18nCopy(zh, "copy.components_backtest_BacktestBiasChecks.025")) : unknown],
    [i18nCopy(zh, "copy.components_backtest_BacktestBiasChecks.026"), checks.multi_timeframe_close_aligned === true ? (i18nCopy(zh, "copy.components_backtest_BacktestBiasChecks.027")) : unknown],
    [i18nCopy(zh, "copy.components_backtest_BacktestBiasChecks.028"), checks.strategy_order_execution === "next_bar_open" ? (i18nCopy(zh, "copy.components_backtest_BacktestBiasChecks.029")) : unknown],
    [i18nCopy(zh, "copy.components_backtest_BacktestBiasChecks.030"), checks.end_of_data_signal === "rejected_no_next_bar" ? (i18nCopy(zh, "copy.components_backtest_BacktestBiasChecks.031")) : unknown],
  ];
  return <section className={styles.evidence} data-testid="backtest-bias-checks" aria-label={i18nCopy(zh, "copy.components_backtest_BacktestBiasChecks.032")}>
    <div className={styles.summary}>
      <span>{i18nCopy(zh, "copy.components_backtest_BacktestBiasChecks.033")} <strong data-state={staticState} data-testid="backtest-static-status">{staticLabel}</strong></span>
      <span>{i18nCopy(zh, "copy.components_backtest_BacktestBiasChecks.034")} <strong data-testid="backtest-research-status">{notRun ? (i18nCopy(zh, "copy.components_backtest_BacktestBiasChecks.035", { value0: notRun })) : hasResearch ? (i18nCopy(zh, "copy.components_backtest_BacktestBiasChecks.036")) : unknown}</strong></span>
    </div>
    <details className={styles.details} open={!compact}>
      <summary>{i18nCopy(zh, "copy.components_backtest_BacktestBiasChecks.037")}</summary>
      <div className={styles.body}>
        <p>{i18nCopy(zh, "copy.components_backtest_BacktestBiasChecks.038")}</p>
        {!!warnings.length && <div className={styles.warnings} role="note"><h4>{i18nCopy(zh, "copy.components_backtest_BacktestBiasChecks.039")}</h4>{warnings.map((row, i) => <p key={i}>{text(row.message) || text(row.code)}{text(row.file) && <code> · {text(row.file)}{typeof row.line === "number" ? `:${row.line}` : ""}</code>}</p>)}</div>}
        <section><h4>{i18nCopy(zh, "copy.components_backtest_BacktestBiasChecks.040")}</h4>
          <dl className={styles.rows}>{runtime.map(([label, value]) => <div key={label}><dt>{label}</dt><dd>{value}</dd></div>)}</dl></section>
        <section><h4>{i18nCopy(zh, "copy.components_backtest_BacktestBiasChecks.041")}</h4>
          {!hasResearch ? <p>{i18nCopy(zh, "copy.components_backtest_BacktestBiasChecks.042")}</p>
            : <dl className={styles.rows}>{recorded.map(row => <div key={row.id} data-check={row.id}><dt>{row.label}</dt><dd>{statusLabel(row.status)}</dd></div>)}</dl>}
        </section>
        <section><h4>{i18nCopy(zh, "copy.components_backtest_BacktestBiasChecks.043")}</h4>
          <p>{limits.order_types === "market_only" ? (i18nCopy(zh, "copy.components_backtest_BacktestBiasChecks.044")) : (i18nCopy(zh, "copy.components_backtest_BacktestBiasChecks.045"))}
            {typeof assumptions.warmup_bars === "number" && Number.isFinite(assumptions.warmup_bars) ? (i18nCopy(zh, "copy.components_backtest_BacktestBiasChecks.046", { value0: assumptions.warmup_bars })) : ""}</p>
          {omitted.length ? <><p className={styles.limits}>{i18nCopy(zh, "copy.components_backtest_BacktestBiasChecks.047")}{omitted.map(key => i18nCopy(zh, limitLabels[key] ?? "")).join(i18nCopy(zh, "copy.components_backtest_BacktestBiasChecks.048"))}</p>
            <p>{i18nCopy(zh, "copy.components_backtest_BacktestBiasChecks.049")}</p></>
            : <p>{i18nCopy(zh, "copy.components_backtest_BacktestBiasChecks.050")}</p>}
        </section>
      </div>
    </details>
  </section>;
}
