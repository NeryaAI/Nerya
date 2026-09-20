"use client";
import { useEffect, useState } from 'react';
import Link from 'next/link';
import { useLocale } from 'next-intl';
import { clientApi } from '../lib/clientApi';
import { GlobeIcon } from './icons';

export function BrowserPreferences(){
  const zh=useLocale().startsWith('zh');
  const [automatic,setAutomatic]=useState(true),[busy,setBusy]=useState(true),[error,setError]=useState('');
  useEffect(()=>{let active=true;clientApi.browserDesktop({operation:'preferences'}).then(r=>{if(active){if(!r.ok)throw new Error(r.error);setAutomatic(r.preferences?.automatic??true);}}).catch(e=>{if(active)setError(String(e.message||e));}).finally(()=>{if(active)setBusy(false);});return()=>{active=false;};},[]);
  async function change(value:boolean){const previous=automatic;setAutomatic(value);setBusy(true);setError('');try{const r=await clientApi.browserDesktop({operation:'preferences',automatic:value});if(!r.ok)throw new Error(r.error);setAutomatic(r.preferences?.automatic??value);}catch(e){setAutomatic(previous);setError(e instanceof Error?e.message:'browser_preferences_failed');}finally{setBusy(false);}}
  return <section className="mx-auto w-full max-w-2xl rounded-xl border border-[color:var(--line)] bg-[color:var(--card)] p-6" data-testid="browser-preferences">
    <div className="mb-6 flex items-center gap-3"><GlobeIcon size={23}/><div><h2 className="text-base font-semibold">{zh?'Nerya 工作浏览器':'Nerya work browser'}</h2><p className="mt-1 text-xs text-[color:var(--text-muted)]">Chromium · {zh?'独立持久化配置':'Isolated persistent profile'}</p></div></div>
    <label className="flex items-center justify-between gap-6"><span className="text-sm font-medium">{zh?'Agent 自动操作':'Automatic Agent control'}</span><input type="checkbox" role="switch" checked={automatic} disabled={busy} onChange={e=>void change(e.target.checked)}/></label>
    <p className="mt-3 text-sm leading-relaxed text-[color:var(--text-muted)]">{zh?'默认允许 Agent 自动打开浏览器、访问普通网页、读取 DOM、截图和下载。不需要选择引擎、逐站点授权或设置有效期。':'The Agent can start the browser, visit ordinary websites, read DOM, take screenshots and download. No engine selection, per-site grants or expiry setup.'}</p>
    <p className="mt-4 text-xs leading-relaxed text-[color:var(--text-muted)]">{zh?'浏览器、标签页、扩展和历史记录都在对话右侧面板。Agent 操作时输入锁定，点击“接管”可手动操作。密码、钱包签名、支付和身份验证仍需人工确认。':'Browser, tabs, extensions and history are in the conversation side panel. Input is locked during Agent control; take over to interact. Passwords, wallet signing, payments and authentication still require human confirmation.'}</p>
    {error&&<p className="mt-4 text-sm text-danger" role="alert">{error}</p>}
    <Link className="btn btn-primary mt-6 inline-flex" href="/chat">{zh?'前往对话':'Go to conversation'} ↗</Link>
  </section>;
}
