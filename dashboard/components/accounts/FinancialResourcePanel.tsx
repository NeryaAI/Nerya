"use client";
import {useEffect,useState} from 'react';
import {useTranslations} from 'next-intl';
import {financialApi,type FinancialCapability} from '../../lib/financial';
import {useWorkspaceIdentity} from '../../lib/workspaceIdentity';

export function FinancialResourcePanel({resourceId}:{resourceId:string}){
  const t=useTranslations("financial");const workspace=useWorkspaceIdentity();
  const [rows,setRows]=useState<FinancialCapability[]>([]);const [revision,setRevision]=useState('');const [permissions,setPermissions]=useState<Record<string,boolean>>({});
  const [limits,setLimits]=useState<Record<string,string>>({single_usd:'',rolling_24h_usd:''});const [error,setError]=useState('');const [busy,setBusy]=useState(false);const [editing,setEditing]=useState(false);const [enabled,setEnabled]=useState(false);
  async function load(){const [a,b]=await Promise.all([financialApi.capabilities(),financialApi.resource(resourceId)]);setRows(a.capabilities.filter(r=>(r.resource_type==='wallet'?'wallet:':'')+r.resource_id===resourceId));setEnabled(a.enabled);setRevision(b.revision);setPermissions(b.permissions);setLimits({single_usd:'',rolling_24h_usd:'',...b.limits});}
  useEffect(()=>{void load().catch(e=>setError(String(e)));setEditing(false);},[resourceId,workspace]);
  async function save(event:React.FormEvent){event.preventDefault();setBusy(true);try{await financialApi.updateResource(resourceId,revision,permissions,limits);await load();setEditing(false);setError('');}catch(e){setError(String(e));}finally{setBusy(false);}}
  const actions=resourceId.startsWith('wallet:')?['wallet_transfer','contract_approval','swap','bridge_swap']:['transfer','withdraw'];
  return <section className="rounded-lg border border-[color:var(--line)] p-4" data-testid="financial-resource-panel">
    <div className="flex flex-wrap items-center justify-between gap-2"><h3 className="text-sm font-medium">{t("resourceTitle")}</h3><button className="btn-ghost text-xs" disabled={!revision||busy} onClick={()=>setEditing(!editing)}>{editing?(t("cancel")):(t("configurePermissions"))}</button></div>
    <p className="mt-2 text-xs text-[color:var(--text-muted)]">{enabled?(t("moduleEnabled")):(t("moduleDisabled"))}</p>
    {error&&<p role="alert" className="mt-2 text-xs text-danger">{error}</p>}
    <ul className="mt-3 space-y-2 text-xs">{rows.map(row=><li key={row.kind} className="flex flex-wrap justify-between gap-2"><span>{t.has("actions."+row.kind)?t("actions."+row.kind):row.kind}</span><span className={row.ready?'text-ok':'text-[color:var(--text-muted)]'}>{row.ready?(t("verificationPending")):row.reason||(!row.permission_enabled?(t("permissionDisabled")):(t("notReady")))}</span></li>)}</ul>
    {!rows.length&&<p className="mt-3 text-xs text-[color:var(--text-muted)]">{t("noProvider")}</p>}
    {editing&&<form className="mt-4 space-y-3" onSubmit={save}><div className="flex flex-wrap gap-3">{actions.map(kind=><label className="flex items-center gap-2 text-xs" key={kind}><input type="checkbox" checked={permissions[kind]===true} onChange={e=>setPermissions({...permissions,[kind]:e.target.checked})}/>{t.has("actions."+kind)?t("actions."+kind):kind}</label>)}</div>
      <div className="grid gap-3 sm:grid-cols-2">{['single_usd','rolling_24h_usd'].map(key=><label className="text-xs" key={key}>{t("accountLimits."+key)}<input className="input-dark mt-1 w-full" required value={limits[key]} onChange={e=>setLimits({...limits,[key]:e.target.value})}/></label>)}</div>
      <p className="text-xs text-[color:var(--text-muted)]">{t("auditHint")}</p>
      <button className="btn-primary text-xs" type="submit" disabled={busy}>{t("savePermissions")}</button>
    </form>}
  </section>;
}
