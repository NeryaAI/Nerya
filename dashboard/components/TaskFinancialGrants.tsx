"use client";
import {useEffect,useState} from 'react';
import {useLocale,useTranslations} from 'next-intl';
import {financialApi,type FinancialGrant,type FinancialPolicy,type TaskDescriptor} from '../lib/financial';
import type {RunOrigin} from '../lib/taskRuns';
import {useWorkspaceIdentity} from '../lib/workspaceIdentity';
import {confirm} from '../lib/dialogs';

const actionKinds=['trade','swap','exchange_transfer','withdraw','wallet_transfer','contract_approval','bridge_swap'];
export function TaskFinancialGrants({taskKind,taskId}:{taskKind:RunOrigin;taskId:string}){
  const locale=useLocale();const t=useTranslations("financial");const workspace=useWorkspaceIdentity();
  const [task,setTask]=useState<TaskDescriptor>();const [grants,setGrants]=useState<FinancialGrant[]>([]);const [open,setOpen]=useState(false);const [error,setError]=useState('');const [busy,setBusy]=useState(false);
  const [actions,setActions]=useState<string[]>(['trade']);const [resources,setResources]=useState<Record<string,string>>({});
  const [limits,setLimits]=useState({single_usd:'',rolling_24h_usd:'',total_usd:'',fee_usd:'',slippage_bps:'50',asset_amounts:'',hours:'24',trade_actions:'open_position,close_position,reduce_position',max_leverage:'1'});
  async function load(){const [a,b]=await Promise.all([financialApi.task(taskKind,taskId),financialApi.grants(taskKind,taskId)]);setTask(a.task);setGrants(b.grants);}
  useEffect(()=>{let disposed=false;void Promise.all([financialApi.task(taskKind,taskId),financialApi.grants(taskKind,taskId)]).then(([a,b])=>{if(!disposed){setTask(a.task);setGrants(b.grants);setError('');}}).catch(e=>{if(!disposed)setError(String(e));});return()=>{disposed=true;};},[taskKind,taskId,workspace]);
  async function create(event:React.FormEvent){event.preventDefault();if(!task||busy)return;setBusy(true);try{
    const quantities=Object.fromEntries(limits.asset_amounts.split(',').map(x=>x.trim()).filter(Boolean).map(x=>{const [asset,value,...rest]=x.split('=');if(!asset||!value||rest.length)throw new Error(t("quantityFormat"));return [asset.trim(),value.trim()];}));
    const policy:FinancialPolicy={actions,resources:Object.fromEntries(Object.entries(resources).map(([k,v])=>[k,v.split(',').map(x=>x.trim()).filter(Boolean)])),
      limits:{single_usd:limits.single_usd,rolling_24h_usd:limits.rolling_24h_usd,total_usd:limits.total_usd,fee_usd:limits.fee_usd,slippage_bps:Number(limits.slippage_bps),asset_amounts:quantities,
        trade_actions:limits.trade_actions.split(',').map(s=>s.trim()).filter(Boolean),max_leverage:limits.max_leverage}};
    await financialApi.create(task,policy,Number(limits.hours));setOpen(false);await load();setError('');
  }catch(e){setError(String(e));}finally{setBusy(false);}}
  async function decide(grant:FinancialGrant,action:'approve'|'revoke'){
    if(action==='approve'&&!await confirm({title:t("approveTitle"),message:t("approveMessage"),okLabel:t("approve")}))return;
    setBusy(true);try{await financialApi.decision(grant,action);await load();setError('');}catch(e){setError(String(e));}finally{setBusy(false);}
  }
  return <details className="mt-4 rounded-lg border border-[color:var(--line)] p-3" data-testid="task-financial-grants"><summary className="cursor-pointer text-sm font-medium">{t("authorization")}</summary>
    <p className="mt-3 text-xs text-[color:var(--text-muted)]">{t("bindingHint")}</p>
    {error&&<p role="alert" className="mt-2 text-xs text-danger">{error}</p>}
    {grants.map(grant=><div key={grant.grant_id} data-grant-id={grant.grant_id} className="mt-3 border-t border-[color:var(--line)] pt-3 text-xs">
      <div className="flex flex-wrap items-center justify-between gap-2"><strong>{grant.state==='active'&&grant.expires_at<Date.now()/1000?t("grantStates.expired"):t.has("grantStates."+grant.state)?t("grantStates."+grant.state):grant.state}</strong><span>{new Date(grant.expires_at*1000).toLocaleString(locale)}</span></div>
      <p className="mt-2">{grant.policy.actions.map(a=>t.has("actions."+a)?t("actions."+a):a).join(' · ')}</p>
      <dl className="mt-2 grid gap-2 sm:grid-cols-2">{Object.entries(grant.policy.resources).filter(([,v])=>v.length).map(([k,v])=><div key={k} className="min-w-0"><dt className="text-[color:var(--text-muted)]">{t.has("resources."+k)?t("resources."+k):k}</dt><dd className="break-all">{v.join(', ')}</dd></div>)}</dl>
      <p className="mt-2">{t("limitsSummary",{single:grant.policy.limits.single_usd,rolling:grant.policy.limits.rolling_24h_usd,total:grant.policy.limits.total_usd})}</p>
      <p className="mt-1">{t("feesSummary",{fee:grant.policy.limits.fee_usd,slippage:grant.policy.limits.slippage_bps})}</p>
      {grant.policy.actions.includes('trade')&&<p className="mt-1">{t("tradeSummary",{actions:grant.policy.limits.trade_actions?.join(", ")||"—",leverage:grant.policy.limits.max_leverage||"—"})}</p>}
      <p className="mt-1 break-all">{t("assetQuantities",{quantities:Object.entries(grant.policy.limits.asset_amounts).map(([k,v])=>`${k}=${v}`).join(", ")||"—"})}</p>
      <p className="mt-1">{t("usageSummary",{used:grant.usage?.total_usd||"0",rolling:grant.usage?.rolling_24h_usd||"0",reserved:grant.usage?.reserved_usd||"0"})}</p>
      {grant.policy.actions.includes('contract_approval')&&<p className="mt-1">{t("allowanceExposure",{amount:grant.usage?.allowance_risk_usd||"0"})}</p>}
      <p className="mt-1 break-all text-[color:var(--text-muted)]">{t("approvedBy",{actor:grant.approved_by||"—"})} · {grant.security_revision===task?.security_revision?(t("currentRevision")):(t("revisionChanged"))}</p>
      <div className="mt-2 flex gap-2">{grant.state==='draft'&&<button disabled={busy||grant.security_revision!==task?.security_revision} className="btn-ghost text-xs" onClick={()=>void decide(grant,'approve')}>{t("approveGrant")}</button>}{['draft','active'].includes(grant.state)&&<button disabled={busy} className="btn-ghost text-xs text-danger" onClick={()=>void decide(grant,'revoke')}>{t("revoke")}</button>}</div>
    </div>)}
    <button className="btn-ghost mt-3 text-xs" disabled={!task||busy} onClick={()=>setOpen(!open)}>{open?(t("cancel")):(t("createDraft"))}</button>
    {open&&<form onSubmit={create} className="mt-3 space-y-3"><div className="flex flex-wrap gap-3">{actionKinds.map(kind=><label className="flex items-center gap-1 text-xs" key={kind}><input type="checkbox" checked={actions.includes(kind)} onChange={e=>setActions(e.target.checked?[...actions,kind]:actions.filter(a=>a!==kind))}/>{t("actions."+kind)}</label>)}</div>
      <div className="grid gap-3 sm:grid-cols-2">{(['accounts','wallets','assets','markets','chains','recipients','spenders','routers','account_types','memos'] as const).map(key=><label className="text-xs" key={key}>{t("resources."+key)}<input className="input-dark mt-1 w-full" value={resources[key]||''} placeholder={t("exactIds")} onChange={e=>setResources({...resources,[key]:e.target.value})}/></label>)}</div>
      {actions.includes('trade')&&<div className="grid gap-3 sm:grid-cols-2"><label className="text-xs">{t("tradeActions")}<input required className="input-dark mt-1 w-full" value={limits.trade_actions} onChange={e=>setLimits({...limits,trade_actions:e.target.value})}/></label><label className="text-xs">{t("maximumLeverage")}<input required className="input-dark mt-1 w-full" value={limits.max_leverage} onChange={e=>setLimits({...limits,max_leverage:e.target.value})}/></label></div>}
      <div className="grid gap-3 sm:grid-cols-2">{(['single_usd','rolling_24h_usd','total_usd','fee_usd','slippage_bps','asset_amounts','hours'] as const).map(key=><label className="text-xs" key={key}>{t("limits."+key)}<input required={key!=='asset_amounts'} className="input-dark mt-1 w-full" value={limits[key as keyof typeof limits]} onChange={e=>setLimits({...limits,[key]:e.target.value})}/></label>)}</div>
      <button className="btn-primary text-xs" disabled={busy||!actions.length} type="submit">{t("saveDraft")}</button>
    </form>}
  </details>;
}
