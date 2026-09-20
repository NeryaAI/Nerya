"use client";

import { useEffect, useMemo, useRef, useState } from 'react';
import { useLocale } from 'next-intl';
import * as Menu from '@radix-ui/react-dropdown-menu';
import { clientApi } from '../../lib/clientApi';
import type { DesktopBrowserResponse, DesktopExtension } from '../../lib/browserDesktopTypes';
import { mergeBrowserEvents, type BrowserCall, type BrowserTrace, type BrowserTraceEvent } from '../../lib/browserTrace';
import { confirm } from '../../lib/dialogs';
import { GlobeIcon } from '../icons';
import { BrowserNetworkPanel } from './BrowserNetworkPanel';
import styles from './BrowserWorkspacePanel.module.css';

const EMPTY: BrowserTrace = { ok:true, status:'pending', events:[], cursor:0 };
const labels: Record<string, [string,string]> = { navigate:['前往页面','Navigate'], open:['启动浏览器','Open browser'], batch:['连续操作','Sequence'],
  click:['点击','Click'], click_xy:['点击','Click'], fill:['填写','Fill'], select:['选择','Select'], select_text:['选中文字','Select text'],
  check:['勾选','Check'], press:['按键','Key press'], move:['移动光标','Move pointer'], dom:['读取 DOM','Read DOM'], snapshot:['读取 DOM','Read DOM'],
  read:['读取片段','Read section'], scroll:['滚动','Scroll'], wait_for:['等待页面','Wait'], screenshot:['观察画面','Screenshot'], hover:['悬停','Hover'], drag:['拖拽','Drag'],
  new_tab:['新建标签','New tab'], select_tab:['切换标签','Switch tab'], close_tab:['关闭标签','Close tab'], extension_open:['打开扩展','Open extension'] };
const shortUrl = (url: string) => { try { const u=new URL(url); return u.hostname + (u.pathname === '/' ? '' : u.pathname); } catch { return url || 'New tab'; } };

export function BrowserWorkspacePanel({ conversationId, calls = [], active = true, expanded = false, onToggleSize, onClose }: {
  conversationId: string; calls?: BrowserCall[]; active?: boolean; expanded?: boolean;
  onToggleSize?: () => void; onClose?: () => void;
}) {
  const zh = useLocale().startsWith('zh');
  const t = (cn:string,en:string) => zh ? cn : en;
  const actionName = (op:string) => labels[op]?.[zh ? 0 : 1] || op;
  const [chosen,setChosen] = useState('');
  const selected = calls.find(c=>c.id===chosen) || calls.filter(c=>!['network','api_requests','network_detail'].includes(c.operation)).at(-1) || calls.at(-1);
  const callId = selected?.id || '';
  const profile = selected?.profileId || 'work';
  const [surface,setSurface] = useState<DesktopBrowserResponse>({ok:true});
  const [trace,setTrace] = useState<BrowserTrace>(EMPTY);
  const [historyFrame,setHistoryFrame] = useState<number | null>(null);
  const [section,setSection] = useState('page');
  const [timelineOpen,setTimelineOpen] = useState(false);
  const [address,setAddress] = useState('');
  const [history,setHistory] = useState<NonNullable<DesktopBrowserResponse['history']>>([]);
  const [query,setQuery] = useState('');
  const [extensionPath,setExtensionPath] = useState('');
  const [review,setReview] = useState<DesktopExtension | null>(null);
  const [error,setError] = useState('');
  const [connectionError,setConnectionError] = useState(false);
  const [traceError,setTraceError] = useState(false);
  const [busy,setBusy] = useState(false);
  const [refresh,setRefresh] = useState(0);
  const epoch = useRef(0), inFlight=useRef(false);
  const keyboard=useRef<HTMLTextAreaElement>(null);
  const inputQueue=useRef(Promise.resolve());
  const mounted=useRef(true);
  useEffect(()=>{mounted.current=true;return()=>{mounted.current=false;epoch.current++;};},[]);
  useEffect(()=>{setTrace(EMPTY);setHistoryFrame(null);setReview(null);setError('');epoch.current++;},[conversationId,callId]);

  useEffect(()=>{setSurface({ok:true});setHistory([]);setAddress('');setConnectionError(false);epoch.current++;},[conversationId,profile]);

  // Independent reads keep the view responsive while the Chromium queue is busy.
  useEffect(()=>{
    if(!active) return;
    let alive=true; let timer:ReturnType<typeof setTimeout>; let request:AbortController;
    const poll=async()=>{
      if(!alive)return;
      if(document.hidden || inFlight.current){timer=setTimeout(poll,600);return;}
      const version=epoch.current;
      request=new AbortController(); const deadline=setTimeout(()=>request.abort(),12000);
      try {
        const data=await clientApi.browserSurface({operation:'surface',profile_id:profile},request.signal);
        if(!alive || version!==epoch.current)return;
        if(!data.ok)throw new Error(data.error);
        setSurface(data);setConnectionError(false);
      }catch{if(alive && version===epoch.current){setConnectionError(true);setSurface(s=>({...s,image:undefined}));}}
      finally{clearTimeout(deadline);if(alive)timer=setTimeout(poll,650);}
    };
    void poll();
    const hidden=()=>{if(document.hidden){request?.abort();setSurface(s=>({...s,image:undefined}));}};
    document.addEventListener('visibilitychange',hidden);
    return()=>{alive=false;clearTimeout(timer);request?.abort();document.removeEventListener('visibilitychange',hidden);};
  },[active,profile,conversationId,refresh]);

  useEffect(()=>{
    if(!active || !callId || !conversationId)return;
    let alive=true,cursor=0,pending=0; let timer:ReturnType<typeof setTimeout>; let request:AbortController;
    const version=epoch.current;
    const poll=async()=>{
      if(!alive)return;
      if(document.hidden){timer=setTimeout(poll,800);return;}
      request=new AbortController(); const deadline=setTimeout(()=>request.abort(),8000);
      let delay=220;
      try{
        const data=await clientApi.browserTrace({operation:'trace',conversation_id:conversationId,call_id:callId,after:cursor,...(historyFrame?{frame_id:historyFrame}:{})},request.signal);
        if(!alive || epoch.current!==version)return;
        if(!data.ok)throw new Error(data.error);
        cursor=data.cursor;setTrace(old=>({...data,events:mergeBrowserEvents(old.events,data.events)}));setTraceError(false);
        if(data.status==='pending' && ++pending>120)return;
        if(!['queued','running','pending'].includes(data.status))delay=1200;
        if(data.frame_state==='expired')return;
      }catch{if(alive && epoch.current===version){setTraceError(true);setTrace(s=>({...s,frame:null}));}delay=1500;}
      finally{clearTimeout(deadline);}
      if(alive)timer=setTimeout(poll,delay);
    };
    void poll();
    const hidden=()=>{if(document.hidden){request?.abort();setTrace(s=>({...s,frame:null}));}};
    document.addEventListener('visibilitychange',hidden);
    return()=>{alive=false;clearTimeout(timer);request?.abort();document.removeEventListener('visibilitychange',hidden);};
  },[active,conversationId,callId,historyFrame,refresh]);

  const human=surface.human_control === true;
  const owned=!!surface.agent_access?.occupied;
  const locked=!human || !surface.control_id || !!surface.sensitive || busy || connectionError || !!historyFrame;
  const currentUrl=surface.tabs?.find(tab=>tab.selected)?.url || trace.frame?.url || '';
  useEffect(()=>setAddress(currentUrl),[currentUrl]);
  const frame=historyFrame ? trace.frame?.image : human ? surface.image : surface.agent_access?.executing ? trace.frame?.image : (surface.image || trace.frame?.image);
  const visual=historyFrame ? trace.frame?.visual : trace.visual;
  const displayedUrl=historyFrame || !surface.image || surface.agent_access?.executing ? trace.frame?.url : currentUrl;
  const showVisual=!human && !!frame && !!visual && (!visual.url || visual.url===displayedUrl);
  const steps=useMemo(()=>{
    const map=new Map<number,BrowserTraceEvent>();
    for(const e of trace.events)if(e.kind==='step' && typeof e.index==='number')map.set(e.index,e);
    return [...map.values()].sort((a,b)=>(a.index||0)-(b.index||0));
  },[trace.events]);

  async function run(body:Record<string,unknown>):Promise<DesktopBrowserResponse | null>{
    if(inFlight.current)return null;
    inFlight.current=true;setBusy(true);setError('');const version=++epoch.current;
    try{
      const data=await clientApi.browserDesktop({profile_id:profile,...body});
      if(!mounted.current || epoch.current!==version)return null;
      if(!data.ok)throw new Error(data.error || 'browser_operation_failed');
      if(data.history)setHistory(data.history);
      if(data.review)setReview(data.review);
      if(data.preferences)setSurface(s=>({...s,preferences:data.preferences}));
      return data;
    }catch(e){if(mounted.current)setError(e instanceof Error?e.message:'browser_operation_failed');return null;}
    finally{inFlight.current=false;if(mounted.current){setBusy(false);setRefresh(n=>n+1);}}
  }
  async function takeOver(){
    setHistoryFrame(null);setTrace(s=>({...s,frame:null}));
    const sid=surface.agent_access?.session_id;
    await run({operation:'command',command:sid?'trace_control':'handoff',payload:sid?{session_id:sid,control:'handoff'}:{}});
  }
  async function releaseControl(){
    const sid=surface.agent_access?.session_id;
    await run({operation:'command',command:sid?'trace_control':'resume',payload:sid?{session_id:sid,control:'resume'}:{}});
  }
  async function openBrowser(){if(await run({operation:'open'}))await takeOver();}
  function manual(command:string,payload:Record<string,unknown>={}){
    if(locked || !surface.running)return;
    const version=epoch.current,session=surface.agent_access?.session_id || '',controlId=surface.control_id;
    inputQueue.current=inputQueue.current.then(async()=>{
      if(version!==epoch.current || !mounted.current)return;
      try{
        const data=await clientApi.browserDesktop({operation:'human_command',profile_id:profile,session_id:session,control_id:controlId,command,payload});
        if(!data.ok)throw new Error(data.error);
        if(version===epoch.current)setSurface(data);
      }catch(e){epoch.current++;if(mounted.current){setError(e instanceof Error?e.message:'manual_input_failed');setRefresh(n=>n+1);}}
    });
  }
  async function showSection(value:string){
    setSection(value);
    if(value==='history')await run({operation:'history'});
  }
  async function changeAutomatic(value:boolean){
    const previous=surface.preferences?.automatic??true;
    setSurface(s=>({...s,preferences:{automatic:value}}));
    if(!await run({operation:'preferences',automatic:value}))setSurface(s=>({...s,preferences:{automatic:previous}}));
  }
  async function applyExtensions(extensions:DesktopExtension[]){
    if(surface.running && !human)return;
    if(!await confirm({message:t('应用扩展更改需要重启工作浏览器，并结束当前 Agent 浏览器会话。继续？','Applying extensions restarts the work browser and ends its Agent session. Continue?')}))return;
    if(await run({operation:'extension_apply',extensions})){setReview(null);setExtensionPath('');}
  }
  const status=connectionError ? t('连接中断','Disconnected') : surface.sensitive ? t('敏感操作 · 原生接管','Sensitive UI · native control')
    : human ? t('你在控制','You are in control') : owned ? t('Agent 控制中','Agent in control') : t('自动模式就绪','Ready for Agent');

  return <section className={styles.panel} data-testid="browser-workspace" aria-label={t('浏览器侧边面板','Browser side panel')}>
    <header className={styles.header}>
      <div className={styles.title}><GlobeIcon size={16}/><span>{t('浏览器','Browser')}</span><span className={styles.status} role="status">{status}</span></div>
      <div className={styles.tools}>
        {surface.running ? <button className="btn btn-ghost text-xs" disabled={busy} onClick={()=>void(human?releaseControl():takeOver())}>{human?t('交还 Agent','Give to Agent'):t('接管','Take over')}</button>
          : <button className="btn btn-ghost text-xs" disabled={busy} onClick={()=>void openBrowser()}>{t('打开','Open browser')}</button>}
        <Menu.Root>
          <Menu.Trigger asChild><button type="button" className={styles.icon} aria-label={t('浏览器更多功能','More browser tools')} title={t('更多','More')}>⋯</button></Menu.Trigger>
          <Menu.Portal><Menu.Content className="ui-select-menu min-w-48" align="end" sideOffset={6} collisionPadding={8}>
            {[['tabs',t('标签页','Tabs')],['extensions',t('扩展','Extensions')],['history',t('历史记录','History')],['network',t('网络请求','Network')],['settings',t('设置','Settings')]].map(([id,label])=>
              <Menu.Item className="ui-select-option" key={id} onSelect={()=>void showSection(id)}>{label}{id==='tabs'&&<span className="ml-auto text-xs text-[color:var(--text-muted)]">{surface.tabs?.length||0}</span>}</Menu.Item>)}
            <Menu.Item className="ui-select-option" onSelect={()=>{setSection('page');setTimelineOpen(true);}}>{t('操作记录','Operation history')}</Menu.Item>
            {(onToggleSize||onClose)&&<Menu.Separator className="my-1 h-px bg-[color:var(--line)]"/>}
            {onToggleSize&&<Menu.Item className="ui-select-option" onSelect={onToggleSize}>{expanded?t('还原侧栏','Restore side panel'):t('放大浏览器','Expand browser')}</Menu.Item>}
            {onClose&&<Menu.Item className="ui-select-option" onSelect={onClose}>{t('关闭浏览器面板','Close browser panel')}</Menu.Item>}
          </Menu.Content></Menu.Portal>
        </Menu.Root>
      </div>
    </header>
    {section!=='page'&&<div className={styles.sectionHeading}><button type="button" className={styles.icon} onClick={()=>setSection('page')} aria-label={t('返回网页','Back to page')}>←</button><h3>{({tabs:t('标签页','Tabs'),extensions:t('扩展','Extensions'),history:t('历史记录','History'),network:t('网络请求','Network'),settings:t('设置','Settings')} as Record<string,string>)[section]}</h3></div>}
    {section==='tabs'&&<div className={styles.tabs} aria-label={t('标签页','Browser tabs')}>
      {(surface.tabs||trace.frame?.tabs||[]).map(tab=><div key={tab.id} className={styles.tab} data-selected={tab.selected}>
        <button className="min-w-0 flex-1 truncate px-3 py-2 text-left" title={tab.url} disabled={locked} aria-pressed={tab.selected} onClick={()=>{manual('select_tab',{tab_id:tab.id});setSection('page');}}>{shortUrl(tab.url)}</button>
        <button className="px-2 py-1" disabled={locked} aria-label={`${t('关闭标签','Close tab')} ${tab.id}`} onClick={()=>manual('close_tab',{tab_id:tab.id})}>×</button>
      </div>)}
      <button className={styles.icon} disabled={locked || !surface.running} aria-label={t('新建标签页','New tab')} onClick={()=>manual('new_tab')}>+</button>
    </div>}
    {section==='page'&&<form className={styles.address} onSubmit={e=>{e.preventDefault();manual('navigate',{url:address.includes('://')||address==='about:blank'?address:`https://${address}`});}}>
      {human && (['back','forward','reload'] as const).map((command,i)=><button key={command} type="button" className={styles.icon} disabled={locked || !surface.running} aria-label={[t('后退','Back'),t('前进','Forward'),t('刷新','Reload')][i]} onClick={()=>manual(command)}>{['←','→','↻'][i]}</button>)}
      <input aria-label={t('浏览器地址','Browser address')} value={address} onChange={e=>setAddress(e.target.value)} disabled={locked} placeholder={t('输入网址','Enter address')}/>
    </form>}
    {error && <div role="alert" className="mx-3 mt-2 rounded border border-danger/30 px-3 py-2 text-xs text-danger">{error}</div>}
    {['waiting','handoff_required'].includes(surface.challenge?.state||'')&&<p role="status" className={styles.challengeNotice}>{surface.challenge?.state==='waiting'?t('正在等待站点验证，页面会自动恢复。','Waiting for site verification to finish.'):t('站点仍要求验证，请使用顶部接管按钮。','The site still requires verification. Use Take over above.')}</p>}
    <div className={styles.body}>
      {section==='network'&&<BrowserNetworkPanel key={profile} profile={profile} active={active}/>}
      {section==='page' && <>
        <div className={styles.viewport} data-testid="browser-viewport" onClick={e=>{
          if(locked || !frame)return;const r=e.currentTarget.getBoundingClientRect();
          manual('click',{x:Math.min(1279,(e.clientX-r.left)/r.width*1280),y:Math.min(799,(e.clientY-r.top)/r.height*800)});keyboard.current?.focus({preventScroll:true});
        }} onWheel={e=>{if(!locked)manual('scroll',{dy:Math.max(-3000,Math.min(3000,e.deltaY))});}}>
          {frame && !connectionError && !(traceError&&!human&&!surface.image) && !surface.sensitive ? <img src={frame} alt={t('浏览器实时画面','Live browser viewport')} draggable={false}/> : <div className={styles.empty}>
            <GlobeIcon size={28}/><p>{surface.sensitive?t('请在原生窗口完成密码、钱包或身份验证。','Complete password, wallet or authentication in the native window.'):connectionError||traceError?t('画面连接中断，正在重新连接。不会重复执行动作。','View disconnected. Reconnecting without repeating actions.'):trace.frame_state==='expired'?t('过程画面已过期，操作记录仍保留。','Frames expired; the operation log remains.'):t('浏览器将随 Agent 任务自动启动。','The browser starts automatically with an Agent task.')}</p>
          </div>}
          {showVisual && visual && <svg className={styles.visual} viewBox="0 0 1280 800" data-dom={visual.action==='dom'} data-testid="browser-action-overlay" aria-label={actionName(visual.action||'')}>
            {(visual.boxes||[]).map((box,i)=><rect key={i} {...box} rx={4}/>)}
            {visual.cursor && <g className={styles.cursor} transform={`translate(${visual.cursor.x} ${visual.cursor.y})`}><path d="M0 0 L0 26 L7 19 L13 31 L18 28 L12 17 L22 17 Z"/></g>}
          </svg>}
          {!human && surface.running && <div className={styles.shield} data-testid="browser-input-shield" onClick={e=>{e.preventDefault();e.stopPropagation();}} onPointerDown={e=>e.stopPropagation()} onWheel={e=>e.stopPropagation()}>
            <div className={styles.shieldNote}>{t('输入已锁定，接管后可操作','Input locked. Take over to interact.')}</div>
          </div>}
        </div>
        <textarea ref={keyboard} className="sr-only" aria-label={t('浏览器键盘输入','Browser keyboard input')} disabled={locked} onChange={e=>{if(!(e.nativeEvent as InputEvent).isComposing){manual('type',{text:e.target.value});e.target.value='';}}} onCompositionEnd={e=>{manual('type',{text:e.currentTarget.value});e.currentTarget.value='';}}
          onKeyDown={e=>{if(['Enter','Tab','Escape','Backspace','ArrowUp','ArrowDown','ArrowLeft','ArrowRight'].includes(e.key)&&!e.nativeEvent.isComposing){e.preventDefault();manual('press',{key:e.shiftKey&&e.key==='Tab'?'Shift+Tab':e.key});}}}/>
        <details className={styles.steps} open={timelineOpen} onToggle={e=>setTimelineOpen(e.currentTarget.open)} data-testid="browser-operation-details">
          <summary className={styles.activitySummary}><span>{showVisual?actionName(visual?.action||''):t('操作过程','Operation timeline')}</span><span>{steps.filter(e=>e.phase==='completed').length}/{steps.length} <span aria-hidden>⌄</span></span></summary>
          <div className="pt-3">
          {calls.length>0 && <div className="mb-3 flex items-center gap-2"><select className={styles.callSelect} aria-label={t('操作记录','Browser operation')} value={callId} onChange={e=>{setChosen(e.target.value);setHistoryFrame(null);}}>{calls.map((c,i)=><option key={c.id} value={c.id}>{i+1}. {actionName(c.operation)}</option>)}</select><button className="text-xs text-[color:var(--text-muted)]" onClick={()=>{setChosen('');setHistoryFrame(null);setTrace(s=>({...s,frame:null}));}}>{t('跟随最新','Follow latest')}</button></div>}
          {historyFrame && <button className="mb-2 text-xs text-brand-300" onClick={()=>{setHistoryFrame(null);setTrace(s=>({...s,frame:null}));}}>{t('返回实时画面','Back to live')}</button>}
          <ol aria-label={t('全部操作步骤','All operation steps')}>{steps.map(e=><li className={styles.step} key={e.index} data-browser-step={e.index}><span className={e.phase==='failed'?'text-danger':e.phase==='completed'?'text-ok':'text-[color:var(--text-muted)]'}>{e.phase==='completed'?'✓':e.phase==='failed'?'!':e.phase==='skipped'?'–':'◌'}</span><span className="min-w-0 flex-1 break-words">{actionName(e.action||'')} {e.target}<span className="ml-2 text-[color:var(--text-muted)]">{e.phase==='started'?t('执行中','Running'):e.phase==='skipped'?t('未执行','Skipped'):e.phase==='failed'?t('失败','Failed'):t('完成','Done')}</span></span>{!!e.frame_id&&<button onClick={()=>{setHistoryFrame(e.frame_id!);setTrace(s=>({...s,frame:null}));}}>{t('回看','View frame')}</button>}</li>)}</ol>
          </div>
        </details>
      </>}
      {section==='tabs'&&<p className={`${styles.content} ${styles.muted}`}>{human?t('选择标签回到网页。','Select a tab to return to the page.'):t('接管后可切换、新建或关闭标签。','Take over to switch, add or close tabs.')}</p>}
      {section==='history' && <div className={styles.content}><div className="flex gap-2"><input className="input min-w-0 flex-1" aria-label={t('搜索历史','Search history')} value={query} onChange={e=>setQuery(e.target.value)} placeholder={t('搜索访问记录','Search visited pages')}/><button className="btn btn-ghost" disabled={busy} onClick={async()=>{if(await confirm({message:t('清空工作浏览器的访问记录？','Clear work browser history?')}))await run({operation:'history_clear'});}}>{t('清空','Clear')}</button></div><div className={styles.rows}>{history.slice().reverse().filter(r=>r.url.toLowerCase().includes(query.toLowerCase())).map(r=><button key={r.id} className={`${styles.row} text-left`} disabled={locked} onClick={()=>{manual('navigate',{url:r.url});setSection('page');}}><span className="block break-all">{shortUrl(r.url)}</span><time className={styles.muted}>{new Date(r.at*1000).toLocaleString()}</time></button>)}</div>{!history.length&&<p className={styles.muted}>{t('还没有访问记录。','No browsing history yet.')}</p>}<p className={styles.muted}>{t('接管后可打开记录。历史仅保存页面地址，不保留查询参数。','Take over to reopen a page. History omits URL query parameters.')}</p></div>}
      {section==='extensions' && <div className={styles.content}>
        {(surface.config?.extensions||[]).map((extension,i)=><div key={extension.path} className={styles.row}><div className="flex items-center justify-between gap-3"><span className="min-w-0 truncate font-medium">{extension.name} <small>{extension.version}</small></span><button className="btn btn-ghost" disabled={locked||!extension.extension_id||extension.enabled===false} onClick={()=>{manual('extension_open',{extension_id:extension.extension_id});setSection('page');}}>{t('打开','Open')}</button></div><div className="mt-2 flex flex-wrap items-center gap-4"><span className={styles.muted}>{extension.enabled!==false?t('已启用','Enabled'):t('已停用','Disabled')}</span><button className="text-[color:var(--text-muted)]" disabled={busy||!!surface.running&&!human} onClick={()=>void applyExtensions((surface.config?.extensions||[]).map((v,j)=>i===j?{...v,enabled:extension.enabled===false}:v))}>{extension.enabled!==false?t('停用','Disable'):t('启用','Enable')}</button><button className="text-[color:var(--text-muted)]" disabled={busy||!!surface.running&&!human} onClick={()=>void applyExtensions((surface.config?.extensions||[]).filter((_,j)=>i!==j))}>{t('移除','Remove')}</button><span className={styles.muted}>{extension.control_ui?t('Agent 可操作','Agent UI enabled'):t('人工控制','Human-only UI')}</span></div></div>)}
        <form className="mt-4 space-y-3" onSubmit={e=>{e.preventDefault();void run({operation:'review_extension',path:extensionPath});}}><label className="block">{t('添加本地扩展','Add local extension')}<input className="input mt-2 w-full" aria-label={t('扩展目录','Extension directory')} value={extensionPath} onChange={e=>{setExtensionPath(e.target.value);setReview(null);}} placeholder="/path/to/unpacked-extension"/></label><button className="btn btn-ghost" disabled={busy||!extensionPath}>{t('检查扩展','Review extension')}</button></form>
        {review&&<div className="mt-3 rounded border border-[color:var(--line)] p-3"><p className="font-medium">{review.name} · {review.version}</p><p className={`${styles.muted} break-all`}>{review.permissions.join(', ')||t('无额外权限','No extra permissions')}</p><label className="mt-3 block"><input type="checkbox" checked={review.control_ui} disabled={!review.extension_id} onChange={e=>setReview({...review,control_ui:e.target.checked})}/> {t('允许 Agent 操作普通扩展 UI','Allow Agent to control ordinary extension UI')}</label><button className="btn btn-primary mt-3" disabled={busy||!!surface.running&&!human} onClick={()=>void applyExtensions([...(surface.config?.extensions||[]).filter(e=>e.path!==review.path),review])}>{t('添加并应用','Add and apply')}</button></div>}
        <p className={`${styles.muted} mt-4`}>{t('支持本地解压 MV3 扩展。应用更改会重启浏览器；钱包、密码管理器保留人工确认。','Supports local unpacked MV3 extensions. Applying changes restarts the browser; wallets and password managers stay human-controlled.')}</p>
      </div>}
      {section==='settings'&&<div className={styles.content}><p className="mb-4 font-medium">Chromium · {t('内置工作浏览器','Built-in work browser')}</p><label className="flex items-center justify-between gap-4"><span>{t('Agent 自动操作','Automatic Agent control')}</span><input type="checkbox" role="switch" checked={surface.preferences?.automatic??true} disabled={busy} onChange={e=>void changeAutomatic(e.target.checked)}/></label><p className={`${styles.muted} mt-3`}>{t('允许 Agent 自动启动、浏览网页、读取 DOM、截图与下载，不再逐站点配置。密码、签名和支付仍需人工确认。','Lets the Agent start, browse, read DOM, take screenshots and download without per-site setup. Passwords, signing and payments still require human confirmation.')}</p></div>}
    </div>
  </section>;
}
