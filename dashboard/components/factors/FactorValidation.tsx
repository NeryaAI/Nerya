"use client";

import { copy as i18nCopy } from "../../lib/i18n";

import { useEffect, useState } from "react";
import { useLocale } from "next-intl";
import { factorRequest, FactorRequestError, type Factor, type FactorDataset } from "../../lib/factorLibrary";
import { ErrorBanner } from "../Page";
import styles from "./Factors.module.css";

export function FactorValidation({ factor, factors, onComplete }: { factor: Factor; factors: Factor[]; onComplete: () => void }) {
  const zh = useLocale().startsWith("zh");
  const [datasets,setDatasets] = useState<FactorDataset[]>([]), [loading,setLoading] = useState(true);
  const [market,setMarket] = useState(factor.markets[0] || ""), [timeframe,setTimeframe] = useState(factor.timeframes[0] || "1h");
  const [start,setStart] = useState(""), [end,setEnd] = useState(""), [instrument,setInstrument] = useState("");
  const [fee,setFee] = useState("5"), [slippage,setSlippage] = useState("2"), [horizon,setHorizon] = useState("5"), [fraction,setFraction] = useState("0.3");
  const [compare,setCompare] = useState(""), [busy,setBusy] = useState(false), [error,setError] = useState(""), [loaded,setLoaded] = useState(0);
  useEffect(() => {
    const control = new AbortController(); setLoading(true);
    factorRequest<{datasets:FactorDataset[]}>("data",{},control.signal).then(result => { setDatasets(result.datasets); setError(""); }).catch(e => { if (!control.signal.aborted) setError(String(e.message || e)); }).finally(() => { if (!control.signal.aborted) setLoading(false); });
    return () => control.abort();
  },[loaded]);
  return <form className={styles.form} onSubmit={async event => {
    event.preventDefault(); if (busy) return; setBusy(true); setError("");
    try {
      await factorRequest("evaluate",{factor_id:factor.factor_id,version:factor.version,market,timeframe,start,end,instrument_type:instrument,fee_bps:Number(fee),slippage_bps:Number(slippage),horizon:Number(horizon),test_fraction:Number(fraction),compare:compare ? [JSON.parse(compare)] : []});
      onComplete();
    } catch(e) {
      if (e instanceof FactorRequestError && e.run) onComplete();
      else setError(e instanceof Error ? e.message : String(e));
    } finally { setBusy(false); }
  }}>
    <p className={styles.notice}>{i18nCopy(zh, "copy.components_factors_FactorValidation.001")}</p>
    <label className={styles.field}>{i18nCopy(zh, "copy.components_factors_FactorValidation.002")}<select defaultValue="" onChange={event => {
      const dataset = datasets[Number(event.target.value)]; if (!dataset) return;
      setMarket(dataset.market); setTimeframe(dataset.timeframe);
      const match = /^(\d+)([smhdw])$/.exec(dataset.timeframe);
      const step = match ? Number(match[1]) * ({s:1,m:60,h:3600,d:86400,w:604800}[match[2]] || 0) : 0;
      setStart(new Date(dataset.first_ts * 1000).toISOString()); setEnd(new Date((dataset.last_ts + step) * 1000).toISOString());
    }} disabled={loading || busy}><option value="" disabled>{loading ? (i18nCopy(zh, "copy.components_factors_FactorValidation.003")) : (i18nCopy(zh, "copy.components_factors_FactorValidation.004"))}</option>{datasets.map((d,i) => <option value={i} key={`${d.market}:${d.timeframe}`}>{d.market} · {d.timeframe} · {d.verified_rows} {i18nCopy(zh, "copy.components_factors_FactorValidation.005")}</option>)}</select></label>
    {!loading && !datasets.length && <p className={styles.muted}>{i18nCopy(zh, "copy.components_factors_FactorValidation.006")}</p>}
    <div className={styles.fields}>
      <label className={styles.field}>{i18nCopy(zh, "copy.components_factors_FactorValidation.007")}<input required value={market} onChange={e=>setMarket(e.target.value)} placeholder="BINANCE:ETH/USDT:USDT"/></label>
      <label className={styles.field}>{i18nCopy(zh, "copy.components_factors_FactorValidation.008")}<input required value={timeframe} onChange={e=>setTimeframe(e.target.value)}/></label>
      <label className={styles.field}>{i18nCopy(zh, "copy.components_factors_FactorValidation.009")}<input required value={start} onChange={e=>setStart(e.target.value)} placeholder="2025-01-01T00:00:00Z"/></label>
      <label className={styles.field}>{i18nCopy(zh, "copy.components_factors_FactorValidation.010")}<input required value={end} onChange={e=>setEnd(e.target.value)} placeholder="2026-01-01T00:00:00Z"/></label>
      <label className={styles.field}>{i18nCopy(zh, "copy.components_factors_FactorValidation.011")}<select required value={instrument} onChange={e=>setInstrument(e.target.value)}><option value="" disabled>{i18nCopy(zh, "copy.components_factors_FactorValidation.012")}</option><option value="spot">{i18nCopy(zh, "copy.components_factors_FactorValidation.013")}</option><option value="perpetual">{i18nCopy(zh, "copy.components_factors_FactorValidation.014")}</option></select></label>
      <label className={styles.field}>{i18nCopy(zh, "copy.components_factors_FactorValidation.015")}<input required type="number" min="1" max="1000" step="1" value={horizon} onChange={e=>setHorizon(e.target.value)}/></label>
      <label className={styles.field}>{i18nCopy(zh, "copy.components_factors_FactorValidation.016")}<input required type="number" min="0" max="1000" step="0.01" value={fee} onChange={e=>setFee(e.target.value)}/></label>
      <label className={styles.field}>{i18nCopy(zh, "copy.components_factors_FactorValidation.017")}<input required type="number" min="0" max="1000" step="0.01" value={slippage} onChange={e=>setSlippage(e.target.value)}/></label>
      <label className={styles.field}>{i18nCopy(zh, "copy.components_factors_FactorValidation.018")}<input required type="number" min="0.2" max="0.5" step="0.05" value={fraction} onChange={e=>setFraction(e.target.value)}/></label>
      <label className={styles.field}>{i18nCopy(zh, "copy.components_factors_FactorValidation.019")}<select value={compare} onChange={e=>setCompare(e.target.value)}><option value="">{i18nCopy(zh, "copy.components_factors_FactorValidation.020")}</option>{factors.filter(item=>item.factor_id!==factor.factor_id).map(item=><option key={item.factor_id} value={JSON.stringify({factor_id:item.factor_id,version:item.version})}>{item.name} · v{item.version}</option>)}</select></label>
    </div>
    <p className={styles.muted}>{i18nCopy(zh, "copy.components_factors_FactorValidation.021")}</p>
    {error && <ErrorBanner error={error}/>}
    <div className={styles.actions}><button type="submit" className="btn btn-primary" disabled={busy}>{busy ? (i18nCopy(zh, "copy.components_factors_FactorValidation.022")) : (i18nCopy(zh, "copy.components_factors_FactorValidation.023"))}</button><button type="button" className="btn btn-ghost" disabled={busy} onClick={()=>setLoaded(v=>v+1)}>{i18nCopy(zh, "copy.components_factors_FactorValidation.024")}</button></div>
  </form>;
}
