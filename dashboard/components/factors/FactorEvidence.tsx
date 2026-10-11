"use client";

import { copy as i18nCopy } from "../../lib/i18n";

import { useLocale } from "next-intl";
import type { FactorRun, FactorStats } from "../../lib/factorLibrary";
import { ErrorBanner } from "../Page";
import styles from "./Factors.module.css";

export function FactorEvidence({ run }: { run: FactorRun }) {
  const locale = useLocale(), zh = locale.startsWith("zh"), analysis = run.analysis;
  const fmt = (n: unknown, decimals = 3) => typeof n === "number" && Number.isFinite(n) ? n.toLocaleString(locale, { maximumFractionDigits: decimals }) : "—";
  const metrics: [keyof FactorStats, string][] = [["samples", i18nCopy(zh, "copy.components_factors_FactorEvidence.001")], ["ic", "IC"], ["rank_ic", "Rank IC"],
    ["quantile_spread_bps", i18nCopy(zh, "copy.components_factors_FactorEvidence.002")], ["favored_bucket_mean_bps", i18nCopy(zh, "copy.components_factors_FactorEvidence.003")],
    ["fee_slippage_adjusted_mean_bps", i18nCopy(zh, "copy.components_factors_FactorEvidence.004")], ["double_cost_mean_bps", i18nCopy(zh, "copy.components_factors_FactorEvidence.005")]];
  return <div className={styles.evidence} data-testid="factor-evidence">
    <div className={styles.runHeading}><div><strong>{run.profile.market} <span className={styles.muted}>{run.profile.timeframe}</span></strong><p className={styles.muted}>{run.request.start} — {run.request.end} UTC</p></div><span className={styles.badge}>{run.status === "completed" ? (i18nCopy(zh, "copy.components_factors_FactorEvidence.006")) : run.status}</span></div>
    {run.error && <ErrorBanner error={run.error}/>}
    {analysis && <>
      <p className={styles.notice}>{i18nCopy(zh, "copy.components_factors_FactorEvidence.007")}</p>
      <div className={styles.tableScroll}><table className={styles.table}><thead><tr><th>{i18nCopy(zh, "copy.components_factors_FactorEvidence.008")}</th><th>{i18nCopy(zh, "copy.components_factors_FactorEvidence.009")}</th><th>{i18nCopy(zh, "copy.components_factors_FactorEvidence.010")}</th></tr></thead><tbody>{metrics.map(([key,label]) => <tr key={key}><th>{label}</th><td>{fmt(analysis.in_sample[key])}</td><td>{fmt(analysis.out_of_sample[key])}</td></tr>)}</tbody></table></div>
      <section><h3 className={styles.subheading}>{i18nCopy(zh, "copy.components_factors_FactorEvidence.011")}</h3>
        <div className={styles.quantiles}>{analysis.out_of_sample.quantiles.map(q => {
          const max = Math.max(1, ...analysis.out_of_sample.quantiles.map(v => Math.abs(v.mean_forward_bps || 0)));
          const value = q.mean_forward_bps;
          return <div key={q.bucket} className={styles.quantile}><span>Q{q.bucket}</span><div className={styles.barTrack}><span className={styles.barZero}/>{value != null && <span className={styles.bar} data-negative={value < 0} style={{ left: `${value < 0 ? 50 - Math.abs(value) / max * 50 : 50}%`, width: `${Math.abs(value) / max * 50}%` }}/>}</div><strong>{fmt(value, 2)} bps</strong><small>n={q.count}</small></div>;
        })}</div>
      </section>
      <section><h3 className={styles.subheading}>{i18nCopy(zh, "copy.components_factors_FactorEvidence.012")}</h3><div className={styles.blocks}>{analysis.test_blocks.map((block, i) => <div key={i}><span>{new Date(block.start * 1000).toISOString().slice(0,10)}</span><strong>{fmt(block.rank_ic)}</strong><small>Rank IC · n={block.samples}</small></div>)}</div></section>
      {!!analysis.comparisons.length && <section><h3 className={styles.subheading}>{i18nCopy(zh, "copy.components_factors_FactorEvidence.013")}</h3>{analysis.comparisons.map(c => <p key={`${c.factor_id}:${c.version}`} className={styles.compareRow}><span>{c.factor_id} · v{c.version}</span><strong>{fmt(c.rank_correlation)}</strong></p>)}</section>}
      <p className={styles.muted}>{i18nCopy(zh, "copy.components_factors_FactorEvidence.014")}: {new Date(analysis.split.test_start * 1000).toISOString()} · {i18nCopy(zh, "copy.components_factors_FactorEvidence.015")}: {analysis.split.purge_bars} bars · {i18nCopy(zh, "copy.components_factors_FactorEvidence.016")}: {run.request.horizon} bars</p>
      <div className={styles.notice}><strong>{i18nCopy(zh, "copy.components_factors_FactorEvidence.017")}</strong><p>{i18nCopy(zh, "copy.components_factors_FactorEvidence.018")}</p>
        {run.profile.instrument_type === "perpetual" && <p className="text-warn">{i18nCopy(zh, "copy.components_factors_FactorEvidence.019")}</p>}
      </div>
      <details className={styles.details}><summary>{i18nCopy(zh, "copy.components_factors_FactorEvidence.020")}</summary><pre>{JSON.stringify({ warnings: analysis.warnings, run_id: run.run_id, engine: run.engine, data: run.data, manifest: run.manifest_path, profile: run.profile }, null, 2)}</pre></details>
    </>}
    {!analysis && <p className={styles.muted}>{i18nCopy(zh, "copy.components_factors_FactorEvidence.021")}</p>}
  </div>;
}
