"use client";
import { useEffect, useRef, useState } from 'react';
import { useLocale } from 'next-intl';
import { clientApi } from '../../lib/clientApi';
import type { BrowserNetworkRequest } from '../../lib/browserDesktopTypes';
import styles from './BrowserWorkspacePanel.module.css';

export function BrowserNetworkPanel({profile, active=true}:{profile:string;active?:boolean}) {
  const activeRef=useRef(active);
  activeRef.current=active;
  const zh=useLocale().startsWith('zh'), t=(cn:string,en:string)=>zh?cn:en;
  const [rows,setRows]=useState<BrowserNetworkRequest[]>([]);
  const [query,setQuery]=useState(''),[kind,setKind]=useState('');
  const [error,setError]=useState(''),[listening,setListening]=useState(false);
  const [dropped,setDropped]=useState(0);
  const [selected,setSelected]=useState(''),[detail,setDetail]=useState<BrowserNetworkRequest|null>(null);
  const [detailTab,setDetailTab]=useState('headers'),[detailError,setDetailError]=useState('');
  const [offset,setOffset]=useState(0),[loading,setLoading]=useState(false);
  const generation=useRef(0);
  useEffect(()=>{
    let alive=true,cursor=0,instance:string|undefined,timer:ReturnType<typeof setTimeout>,request:AbortController;
    setRows([]);setSelected('');setDetail(null);generation.current++;
    const poll=async()=>{
      if(document.hidden || !activeRef.current){timer=setTimeout(poll,750);return;}
      request=new AbortController();const deadline=setTimeout(()=>request.abort(),6000);
      let delay=750;
      try {
        const data=await clientApi.browserSurface({operation:'network',profile_id:profile,after:cursor,generation:instance,limit:100,filter:query,resource_type:kind},request.signal);
        if(!alive || !activeRef.current)return;
        if(!data.ok)throw new Error(data.error||'network_unavailable');
        const reset=data.reset===true || (data.cursor??0)<cursor || (!!instance&&instance!==data.generation);
        if(reset){generation.current++;setSelected('');setDetail(null);}
        instance=data.generation;
        cursor=data.cursor??0;
        setRows(old=>[...new Map([...(reset?[]:old),...(data.requests||[])].map(r=>[r.id,r])).values()].sort((a,b)=>a.seq-b.seq).slice(-500));
        setListening(data.listening===true);setDropped(data.dropped||0);setError('');
        if(data.attach_errors)setError(t('部分标签监听不可用','Some tabs could not be monitored'));
        if(data.has_more)delay=40;
      }catch(e){if(alive){setError(e instanceof Error?e.message:'network_unavailable');setListening(false);}delay=1500;}
      finally{clearTimeout(deadline);if(alive)timer=setTimeout(poll,delay);}
    };
    const start=setTimeout(()=>void poll(),180);
    return()=>{alive=false;generation.current++;clearTimeout(start);clearTimeout(timer);request?.abort();};
  },[profile,query,kind]);
  useEffect(()=>{
    setDetail(null);setDetailError('');
    if(!selected)return;
    let alive=true;const version=generation.current;
    setLoading(true);
    clientApi.browserDesktop({operation:'network_detail',profile_id:profile,network_id:selected,include_body:detailTab==='response',offset,max_chars:6000})
      .then(data=>{if(!alive||generation.current!==version)return;if(!data.ok||!data.request)throw new Error(data.error||'network_request_missing_or_expired');setDetail(data.request);})
      .catch(e=>{if(alive)setDetailError(e.message||'network_detail_failed');})
      .finally(()=>{if(alive)setLoading(false);});
    return()=>{alive=false;};
  },[profile,selected,detailTab,offset]);
  const choose=(id:string)=>{setSelected(id);setOffset(0);setDetailTab('headers');};
  return <section className={styles.network} data-testid="browser-network">
    <div className={styles.networkStatus}><span role="status">{listening?t('● 实时监听','● Listening live'):t('监听未连接','Listener disconnected')}</span><span>{rows.length}{dropped?` · ${t('旧记录已淘汰','older entries evicted')}`:''}</span></div>
    <div className={styles.networkFilters}>
      <input aria-label={t('筛选请求','Filter requests')} value={query} onChange={e=>setQuery(e.target.value)} placeholder={t('筛选网址','Filter by URL')}/>
      <select aria-label={t('请求类型','Request type')} value={kind} onChange={e=>setKind(e.target.value)}><option value="">{t('全部','All')}</option><option value="fetch">Fetch</option><option value="xhr">XHR</option><option value="document">Document</option><option value="script">Script</option></select>
    </div>
    {error&&<p role="alert" className="text-xs text-danger">{error}</p>}
    <div className={styles.networkList} role="list" aria-label={t('网络请求','Network requests')}>
      {rows.slice().reverse().map(row=><button key={row.id} type="button" role="listitem" className={styles.networkRow} data-selected={selected===row.id} onClick={()=>choose(row.id)}>
        <span className={row.state==='failed'||(row.status||0)>=400?'text-danger':''}>{row.status??'…'}</span>
        <span>{row.method}</span><span className="min-w-0 truncate" title={row.url}>{row.url.replace(/^https?:\/\//,'')}</span><span>{Math.round(row.duration_ms)}ms</span>
      </button>)}
      {!rows.length&&<p className={styles.muted}>{t('请求会自动出现在这里，不需要开始录制。','Requests appear automatically. No recording step is required.')}</p>}
    </div>
    {selected&&<div className={styles.networkDetail}>
      <nav aria-label={t('请求详情','Request details')} className={styles.networkDetailTabs}>{[['headers',t('标头','Headers')],['payload',t('请求内容','Payload')],['response',t('响应','Response')]].map(([id,label])=><button key={id} aria-pressed={detailTab===id} onClick={()=>{setDetailTab(id);setOffset(0);}}>{label}</button>)}</nav>
      {loading&&<p className={styles.muted}>{t('读取已捕获内容…','Reading captured data…')}</p>}
      {detailError&&<p role="alert" className="text-xs text-danger">{detailError}</p>}
      {detail&&<><p className={`${styles.muted} break-all`}>{detail.method} {detail.url}</p>
        <pre>{detailTab==='headers'?JSON.stringify({request:detail.request_headers,response:detail.response_headers},null,2):detailTab==='payload'?(detail.request_body||t('无已捕获的文本内容','No captured text payload')):(detail.body??detail.body_state??t('无响应内容','No response body'))}</pre>
        {detailTab==='response'&&detail.next_offset!=null&&<button className="btn btn-ghost text-xs" onClick={()=>setOffset(detail.next_offset!)}>{t('下一段','Next section')}</button>}
      </>}
    </div>}
    <p className={styles.muted}>{t('仅观察，不重发请求。认证标头与 Cookie 不展示；正文按需读取。','Observation only, no request replay. Auth headers and cookies are omitted; bodies load on demand.')}</p>
  </section>;
}
