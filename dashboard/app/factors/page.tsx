"use client";

import { copy as i18nCopy } from "../../lib/i18n";

import { Suspense, useCallback, useEffect, useId, useMemo, useState } from "react";
import { useLocale } from "next-intl";
import { useSearchParams } from "next/navigation";
import Link from "next/link";
import { PageBody, PageHeader, ErrorBanner } from "../../components/Page";
import { WorkspaceTabs } from "../../components/chat/WorkspaceTabs";
import { PlusIcon, SkillsIcon } from "../../components/icons";
import { FactorEditor, categories, categoryLabel } from "../../components/factors/FactorEditor";
import { FactorValidation } from "../../components/factors/FactorValidation";
import { FactorEvidence } from "../../components/factors/FactorEvidence";
import { FactorResearchButton } from "../../components/factors/FactorResearchButton";
import { FactorSkillAccess } from "../../components/factors/FactorSkillAccess";
import { factorRequest, type Factor, type FactorDetail, type FactorSource } from "../../lib/factorLibrary";
import styles from "../../components/factors/Factors.module.css";

function FactorLibrary() {
  const zh = useLocale().startsWith("zh"), search = useSearchParams(), tabsId = `factors-${useId().replace(/:/g,"")}`;
  const requestedId = search?.get("id") || "", requestedVersion = Number(search?.get("version")) || undefined;
  const source = useMemo<FactorSource | undefined>(() => search?.get("strategy_id") && search?.get("ts") ? {strategy_id:search.get("strategy_id")!,ts:search.get("ts")!,proposal_id:search.get("proposal_id")} : undefined, [search]);
  const [factors,setFactors] = useState<Factor[]>([]), [loading,setLoading] = useState(true), [error,setError] = useState("");
  const [selected,setSelected] = useState(requestedId), [version,setVersion] = useState<number | undefined>(requestedVersion);
  const [detail,setDetail] = useState<FactorDetail | null>(null), [detailError,setDetailError] = useState(""), [detailLoading,setDetailLoading] = useState(false);
  const [query,setQuery] = useState(""), [category,setCategory] = useState(""), [status,setStatus] = useState("");
  const [tab,setTab] = useState("definition"), [editor,setEditor] = useState<"create"|"edit"|null>(null), [runIndex,setRunIndex] = useState(0);
  const [revision,setRevision] = useState(0), [exporting,setExporting] = useState(false);
  useEffect(()=>{setSelected(requestedId);setVersion(requestedVersion);},[requestedId,requestedVersion]);
  const refresh = useCallback(()=>setRevision(value=>value+1),[]);
  useEffect(()=>{
    const controller = new AbortController(); setLoading(true);
    factorRequest<{factors:Factor[]}>("list",{},controller.signal).then(result=>{setFactors(result.factors);setError("");}).catch(e=>{if(!controller.signal.aborted)setError(String(e.message||e));}).finally(()=>{if(!controller.signal.aborted)setLoading(false);});
    return ()=>controller.abort();
  },[revision]);
  useEffect(()=>{
    if(!selected){setDetail(null);setDetailError("");return;}
    const controller = new AbortController();setDetail(null);setDetailLoading(true);setDetailError("");setRunIndex(0);
    factorRequest<FactorDetail>("get",{factor_id:selected,version},controller.signal).then(result=>{if(!controller.signal.aborted)setDetail(result);}).catch(e=>{if(!controller.signal.aborted)setDetailError(String(e.message||e));}).finally(()=>{if(!controller.signal.aborted)setDetailLoading(false);});
    return ()=>controller.abort();
  },[selected,version,revision]);
  const filtered = useMemo(()=>factors.filter(f=>(!category||f.category===category)&&(!status||f.status===status)&&(!query||`${f.name} ${f.factor_id} ${f.expression} ${f.tags.join(" ")}`.toLowerCase().includes(query.toLowerCase()))),[factors,category,status,query]);
  const current = detail?.factor, run = detail?.runs[runIndex];
  const stateKeys:Record<string,string> = {candidate:"copy.app_factors_page.label_candidate",retired:"copy.app_factors_page.label_retired",rejected:"copy.app_factors_page.label_rejected"};
  const stateLabel = (state:string) => stateKeys[state] ? i18nCopy(zh,stateKeys[state]) : state;
  return <PageBody>
    <PageHeader eyebrow={i18nCopy(zh, "copy.app_factors_page.001")} title={i18nCopy(zh, "copy.app_factors_page.002")}
      description={i18nCopy(zh, "copy.app_factors_page.003")}
      actions={<><FactorResearchButton source={source}/><button className="btn btn-primary" onClick={()=>setEditor("create")}><PlusIcon size={15}/>{i18nCopy(zh, "copy.app_factors_page.004")}</button></>}/>
    {source && <div className={styles.source}><div><strong className="text-sm">{i18nCopy(zh, "copy.app_factors_page.005")}</strong><p className={styles.muted}>{source.strategy_id} · {source.ts}</p><p className={styles.muted}>{i18nCopy(zh, "copy.app_factors_page.006")}</p></div><FactorResearchButton source={source}/></div>}
    {error && <div className="mb-4"><ErrorBanner error={error}/><button className="btn btn-ghost" onClick={refresh}>{i18nCopy(zh, "copy.app_factors_page.007")}</button></div>}
    <FactorSkillAccess/>
    <div className={styles.layout}>
      <aside className={styles.catalog} aria-label={i18nCopy(zh, "copy.app_factors_page.008")}>
        <div className={styles.toolbar}><span className={styles.eyebrow}>{i18nCopy(zh, "copy.app_factors_page.009")} / {factors.length.toString().padStart(2,"0")}</span><button className="ml-auto text-xs text-[color:var(--text-muted)]" onClick={refresh} disabled={loading}>{i18nCopy(zh, "copy.app_factors_page.010")}</button></div>
        <input className={styles.search} type="search" aria-label={i18nCopy(zh, "copy.app_factors_page.011")} placeholder={i18nCopy(zh, "copy.app_factors_page.012")} value={query} onChange={e=>setQuery(e.target.value)}/>
        <div className={styles.fields}><select className={styles.select} aria-label={i18nCopy(zh, "copy.app_factors_page.013")} value={category} onChange={e=>setCategory(e.target.value)}><option value="">{i18nCopy(zh, "copy.app_factors_page.014")}</option>{categories.map(c=><option key={c} value={c}>{categoryLabel(c,zh)}</option>)}</select><select className={styles.select} aria-label={i18nCopy(zh, "copy.app_factors_page.015")} value={status} onChange={e=>setStatus(e.target.value)}><option value="">{i18nCopy(zh, "copy.app_factors_page.016")}</option>{["candidate","retired","rejected"].map(s=><option key={s} value={s}>{stateLabel(s)}</option>)}</select></div>
        <div className={styles.list} aria-busy={loading}>{loading && !factors.length ? <p className={styles.muted} role="status">{i18nCopy(zh, "copy.app_factors_page.017")}</p> : filtered.map(f=><button key={f.factor_id} type="button" className={styles.item} data-active={selected===f.factor_id} aria-pressed={selected===f.factor_id} onClick={()=>{setSelected(f.factor_id);setVersion(undefined);setTab("definition");}}><span className={styles.itemTitle}>{f.name}<span className={styles.badge}>v{f.version}</span></span><code className={styles.itemFormula}>{f.expression}</code><span className={styles.itemMeta}><span>{categoryLabel(f.category,zh)} · {stateLabel(f.status)}</span><span>{f.run_count || 0} {i18nCopy(zh, "copy.app_factors_page.018")}</span></span></button>)}</div>
        {!loading && !filtered.length && <p className={styles.muted}>{factors.length ? (i18nCopy(zh, "copy.app_factors_page.019")) : (i18nCopy(zh, "copy.app_factors_page.020"))}</p>}
      </aside>
      <section className={styles.detail} data-testid="factor-detail" aria-label={i18nCopy(zh, "copy.app_factors_page.021")}>
        {detailLoading ? <div className={styles.empty} role="status">{i18nCopy(zh, "copy.app_factors_page.022")}</div> : detailError ? <div className={styles.content}><ErrorBanner error={detailError}/><button className="btn btn-ghost" onClick={refresh}>{i18nCopy(zh, "copy.app_factors_page.023")}</button></div> : !current ? <div className={styles.empty}><SkillsIcon size={36}/><h2>{i18nCopy(zh, "copy.app_factors_page.024")}</h2><p className={styles.muted}>{factors.length ? (i18nCopy(zh, "copy.app_factors_page.025")) : (i18nCopy(zh, "copy.app_factors_page.026"))}</p><FactorResearchButton source={source}/></div> : <>
          <header className={styles.detailHeader}><div className={styles.eyebrow}>{current.factor_id}</div><div className="flex flex-wrap items-center gap-3"><h2 className={styles.heading}>{current.name}</h2><span className={styles.badge}>{stateLabel(current.status)}</span><label className="ml-auto text-xs"><span className="sr-only">{i18nCopy(zh, "copy.app_factors_page.027")}</span><select className={styles.select} value={current.version} onChange={e=>setVersion(Number(e.target.value))}>{detail!.versions.map(v=><option key={v.version} value={v.version}>v{v.version}{v.version===detail!.versions[0].version ? (i18nCopy(zh, "copy.app_factors_page.028")) : ""}</option>)}</select></label></div>
            <pre className={styles.formula}>{current.expression}</pre><p className={styles.muted}>{current.hypothesis || current.description || (i18nCopy(zh, "copy.app_factors_page.029"))}</p>
            <div className={styles.actions}><FactorResearchButton factor={current}/><button className="btn btn-ghost" disabled={current.version!==detail!.versions[0].version} onClick={()=>setEditor("edit")}>{i18nCopy(zh, "copy.app_factors_page.030")}</button><button className="btn btn-ghost" disabled={exporting} onClick={async()=>{setExporting(true);try{const result=await factorRequest<{files:Record<string,string>}>("export",{factor_id:current.factor_id,version:current.version});const url=URL.createObjectURL(new Blob([result.files["factors.json"]],{type:"application/json"}));const link=document.createElement("a");link.href=url;link.download=`${current.factor_id}.v${current.version}.factors.json`;link.click();setTimeout(()=>URL.revokeObjectURL(url),1000);}catch(e){setDetailError(String(e));}finally{setExporting(false);}}}>{i18nCopy(zh, "copy.app_factors_page.031")}</button></div>
          </header>
          <WorkspaceTabs id={tabsId} label={i18nCopy(zh, "copy.app_factors_page.032")} value={tab} onChange={setTab} tabs={[{id:"definition",label:i18nCopy(zh, "copy.app_factors_page.033")},{id:"validation",label:i18nCopy(zh, "copy.app_factors_page.034")},{id:"evidence",label:i18nCopy(zh, "copy.app_factors_page.035"),meta:detail?.runs.length || "0"}]}/>
          <div className={styles.content} role="tabpanel" id={`${tabsId}-panel-${tab}`} aria-labelledby={`${tabsId}-tab-${tab}`}>
            {tab==="definition" && <><dl className={styles.facts}>{[[i18nCopy(zh, "copy.app_factors_page.036"),current.inputs.join(", ")],[i18nCopy(zh, "copy.app_factors_page.037"),`${current.lookback} bars`],[i18nCopy(zh, "copy.app_factors_page.038"),i18nCopy(zh, "copy.app_factors_page.039")],[i18nCopy(zh, "copy.app_factors_page.040"),current.markets.join(", ")||(i18nCopy(zh, "copy.app_factors_page.041"))],[i18nCopy(zh, "copy.app_factors_page.042"),current.timeframes.join(", ")||(i18nCopy(zh, "copy.app_factors_page.043"))],[i18nCopy(zh, "copy.app_factors_page.044"),current.direction==="higher_is_bullish"?(i18nCopy(zh, "copy.app_factors_page.045")):(i18nCopy(zh, "copy.app_factors_page.046"))]].map(([key,value])=><div key={key}><dt>{key}</dt><dd>{value}</dd></div>)}</dl><section className="mt-6"><h3 className={styles.subheading}>{i18nCopy(zh, "copy.app_factors_page.047")}</h3><pre className={styles.formula}>{JSON.stringify(current.parameters,null,2)}</pre></section>{current.source_backtest && <p className={styles.notice}>{i18nCopy(zh, "copy.app_factors_page.048")}: {current.source_backtest.strategy_id} · {current.source_backtest.ts}<br/>{i18nCopy(zh, "copy.app_factors_page.049")}</p>}<div className="mt-5"><p className={styles.muted}>{i18nCopy(zh, "copy.app_factors_page.050")}: {current.change_reason}</p><p className={styles.muted}>{i18nCopy(zh, "copy.app_factors_page.051")}: {current.definition_hash}</p></div><p className={`${styles.notice} mt-5`}>{i18nCopy(zh, "copy.app_factors_page.052")}</p></>}
            {tab==="validation" && <FactorValidation key={`${current.factor_id}:${current.version}`} factor={current} factors={factors} onComplete={()=>{refresh();setTab("evidence");}}/>}
            {tab==="evidence" && (detail?.runs.length ? <><div className={styles.runs}>{detail.runs.map((r,i)=><button type="button" aria-pressed={runIndex===i} key={r.run_id} onClick={()=>setRunIndex(i)}>{r.created_at.slice(0,19).replace("T"," ")} UTC · {r.status}</button>)}</div>{run && <FactorEvidence run={run}/>}</> : <div className={styles.empty}><h2>{i18nCopy(zh, "copy.app_factors_page.053")}</h2><p className={styles.muted}>{i18nCopy(zh, "copy.app_factors_page.054")}</p><button className="btn btn-secondary" onClick={()=>setTab("validation")}>{i18nCopy(zh, "copy.app_factors_page.055")}</button></div>)}
          </div>
        </>}
      </section>
    </div>
    <p className={`${styles.muted} mt-6`}>{i18nCopy(zh, "copy.app_factors_page.056")} <Link className="underline" href="/agents?tab=skills">{i18nCopy(zh, "copy.app_factors_page.057")}</Link></p>
    {editor && <FactorEditor factor={editor==="edit" ? current : undefined} source={editor==="create" ? source : undefined} onClose={()=>setEditor(null)} onSaved={f=>{setEditor(null);setSelected(f.factor_id);setVersion(f.version);setTab("definition");refresh();}}/>}
  </PageBody>;
}

export default function FactorsPage(){return <Suspense fallback={<PageBody><p role="status">Loading…</p></PageBody>}><FactorLibrary/></Suspense>;}
