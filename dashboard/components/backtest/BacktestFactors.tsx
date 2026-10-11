"use client";

import { copy as i18nCopy } from "../../lib/i18n";

import Link from "next/link";
import { useEffect, useState } from "react";
import { useLocale } from "next-intl";
import { factorRequest, factorSourceUrl, type Factor, type BacktestFactorsData } from "../../lib/factorLibrary";
import { FactorResearchButton } from "../factors/FactorResearchButton";
import { ErrorBanner } from "../Page";
import styles from "../factors/Factors.module.css";

export function BacktestFactors({strategyId,ts,proposalId}:{strategyId:string;ts:string;proposalId?:string|null}){
  const zh=useLocale().startsWith("zh");
  const [data,setData]=useState<BacktestFactorsData|null>(null),[error,setError]=useState(""),[retry,setRetry]=useState(0);
  useEffect(()=>{
    const controller=new AbortController();setData(null);setError("");
    factorRequest<BacktestFactorsData>("backtest",{strategy_id:strategyId,ts,proposal_id:proposalId||null},controller.signal).then(setData).catch(e=>{if(!controller.signal.aborted)setError(String(e.message||e));});
    return ()=>controller.abort();
  },[strategyId,ts,proposalId,retry]);
  const source={strategy_id:strategyId,ts,proposal_id:proposalId};
  const row=(factor:Factor)=><Link key={`${factor.factor_id}:${factor.version}`} className={styles.item} href={`/factors?id=${encodeURIComponent(factor.factor_id)}&version=${factor.version}`}><span className={styles.itemTitle}>{factor.name}<span className={styles.badge}>v{factor.version}</span></span><code className={styles.itemFormula}>{factor.expression}</code><span className={styles.muted}>{factor.factor_id} · {factor.definition_hash?.slice(0,12)}</span></Link>;
  return <section className="space-y-5" data-testid="backtest-factors">
    <div><h3 className="text-sm font-semibold">{i18nCopy(zh, "copy.components_backtest_BacktestFactors.001")}</h3><p className={styles.muted}>{i18nCopy(zh, "copy.components_backtest_BacktestFactors.002")}</p></div>
    {error ? <><ErrorBanner error={error}/><button className="btn btn-ghost" onClick={()=>setRetry(v=>v+1)}>{i18nCopy(zh, "copy.components_backtest_BacktestFactors.003")}</button></> : !data ? <p role="status" className={styles.muted}>{i18nCopy(zh, "copy.components_backtest_BacktestFactors.004")}</p> : <>
      <section><h4 className={styles.subheading}>{i18nCopy(zh, "copy.components_backtest_BacktestFactors.005")}</h4><div className="grid gap-2">{data.used_factors.length?data.used_factors.map(row):<p className={styles.notice}>{i18nCopy(zh, "copy.components_backtest_BacktestFactors.006")}</p>}</div></section>
      <section><h4 className={styles.subheading}>{i18nCopy(zh, "copy.components_backtest_BacktestFactors.007")}</h4><div className="grid gap-2">{data.extracted_factors.length?data.extracted_factors.map(row):<p className={styles.muted}>{i18nCopy(zh, "copy.components_backtest_BacktestFactors.008")}</p>}</div></section>
    </>}
    <div className={styles.actions}><FactorResearchButton source={source}/><Link className="btn btn-ghost" href={factorSourceUrl(source)}>{i18nCopy(zh, "copy.components_backtest_BacktestFactors.009")}</Link></div>
    <p className={styles.muted}>{i18nCopy(zh, "copy.components_backtest_BacktestFactors.010")}</p>
  </section>;
}
