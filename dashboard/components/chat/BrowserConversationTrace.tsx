"use client";

import { useEffect, useMemo, useRef, useState } from 'react';
import { useLocale } from 'next-intl';
import { clientApi } from '../../lib/clientApi';
import { mergeBrowserEvents, type BrowserCall, type BrowserTrace, type BrowserTraceEvent } from '../../lib/browserTrace';
import { confirm } from '../../lib/dialogs';

const actionNames: Record<string, [string, string]> = {
  open: ['打开浏览器', 'Open browser'], navigate: ['前往页面', 'Navigate'], batch: ['连续操作', 'Action sequence'],
  click: ['点击', 'Click'], click_xy: ['坐标点击', 'Visual click'], fill: ['填写', 'Fill'], type: ['输入', 'Type'],
  select: ['选择', 'Select'], check: ['勾选', 'Check'], press: ['按键', 'Press key'], hover: ['悬停', 'Hover'],
  drag: ['拖拽', 'Drag'], scroll: ['滚动', 'Scroll'], wait_for: ['等待页面', 'Wait'], snapshot: ['读取页面', 'Read page'],
  screenshot: ['观察画面', 'Observe'], new_tab: ['新建标签页', 'New tab'], select_tab: ['切换标签页', 'Switch tab'],
  close_tab: ['关闭标签页', 'Close tab'], close: ['结束控制', 'Release control'], handoff: ['人工接管', 'Handoff'],
  upload: ['上传文件', 'Upload'], save_download: ['保存下载', 'Save download'], downloads: ['查看下载', 'Downloads'],
  back: ['后退', 'Back'], forward: ['前进', 'Forward'], reload: ['刷新', 'Reload'], extension_open: ['打开扩展', 'Open extension'],
};
const empty: BrowserTrace = { ok: true, status: 'pending', events: [], cursor: 0, frame: null };

export function BrowserConversationTrace({ conversationId, calls, live }: {
  conversationId: string; calls: BrowserCall[]; live: boolean;
}) {
  const zh = useLocale().startsWith('zh');
  const text = (cn: string, en: string) => zh ? cn : en;
  const name = (op: string) => actionNames[op]?.[zh ? 0 : 1] || op;
  const [chosen, setChosen] = useState<string | null>(null);
  const selected = calls.find(c => c.id === chosen) || calls[calls.length - 1];
  const [state, setState] = useState<BrowserTrace>(empty);
  const [historyFrame, setHistoryFrame] = useState<number | null>(null);
  const [error, setError] = useState('');
  const [busy, setBusy] = useState(false);
  const [visible, setVisible] = useState(true);
  const [retry, setRetry] = useState(0);
  const epoch = useRef(0);
  const container = useRef<HTMLElement>(null);
  const callId = selected?.id || '';

  useEffect(() => {
    const element = container.current;
    if (!element) return;
    const observer = new IntersectionObserver(([entry]) => setVisible(entry.isIntersecting), { rootMargin: '300px' });
    observer.observe(element);
    return () => observer.disconnect();
  }, []);

  useEffect(() => { setHistoryFrame(null); setState(empty); setError(''); }, [callId, conversationId]);

  useEffect(() => {
    if (!callId || !conversationId || !visible) return;
    let active = true, cursor = 0, pendingReads = 0;
    let timer: ReturnType<typeof setTimeout>;
    let request: AbortController | undefined;
    const version = ++epoch.current;
    const poll = async () => {
      if (!active) return;
      if (document.hidden) { timer = setTimeout(poll, 1000); return; }
      request = new AbortController();
      const deadline = setTimeout(() => request?.abort(), 8000);
      let delay = 220;
      try {
        const result = await clientApi.browserTrace({ operation: 'trace', conversation_id: conversationId, call_id: callId,
          after: cursor, ...(historyFrame ? { frame_id: historyFrame } : {}) }, request.signal);
        if (!active || epoch.current !== version) return;
        if (!result.ok) throw new Error(result.error || 'browser_trace_failed');
        cursor = result.cursor;
        setState(old => ({ ...result, events: mergeBrowserEvents(old.events, result.events) }));
        setError('');
        if (result.status === 'pending') pendingReads++;
        // Older servers/historical calls do not have a trace. Never poll forever.
        if (pendingReads > (live ? 150 : 4)) { setError(text('该调用没有可用的浏览器过程记录。', 'No browser trace is available for this call.')); return; }
        if (!['pending', 'queued', 'running'].includes(result.status)) delay = result.controllable ? 1500 : 5000;
        if (result.frame_state === 'expired' && !result.controllable) return;
      } catch (err) {
        if (!active || epoch.current !== version) return;
        setState(old => ({ ...old, frame: null }));
        setError(text('实时画面连接中断；正在重新连接，浏览器动作不会重试。', 'Live view disconnected. Reconnecting without repeating browser actions.'));
        delay = 1500;
      } finally { clearTimeout(deadline); }
      if (active) timer = setTimeout(poll, delay);
    };
    void poll();
    const hidden = () => { if (document.hidden) { request?.abort(); setState(old => ({ ...old, frame: null })); } };
    document.addEventListener('visibilitychange', hidden);
    return () => { active = false; clearTimeout(timer); request?.abort(); document.removeEventListener('visibilitychange', hidden); };
  }, [callId, conversationId, visible, historyFrame, retry, live, zh]);

  const steps = useMemo(() => {
    const rows = new Map<number, BrowserTraceEvent>();
    for (const event of state.events) if (event.kind === 'step' && typeof event.index === 'number') rows.set(event.index, event);
    return [...rows.values()].sort((a, b) => (a.index || 0) - (b.index || 0));
  }, [state.events]);
  const current = steps.find(s => s.phase === 'started');
  const finished = steps.filter(s => s.phase === 'completed').length;
  const running = !error && ['queued', 'running', 'pending'].includes(state.status) && !state.paused;
  const status = error ? text('连接中断', 'Disconnected') : state.paused ? text('人工接管中', 'Human control')
    : state.status === 'failed' ? text('操作失败', 'Failed') : running ? text('实时', 'Live')
    : state.status === 'interrupted' ? text('已中断', 'Interrupted') : text('已结束', 'Finished');

  async function control() {
    if (!state.controllable || busy) return;
    if (state.paused && !await confirm({ message: text('已完成敏感操作，允许 Agent 恢复浏览器控制？', 'Sensitive work is finished. Resume Agent browser control?') })) return;
    const controlVersion = ++epoch.current;
    setBusy(true);
    setState(old => ({ ...old, frame: null }));
    try {
      const result = await clientApi.browserTrace({ operation: 'trace_control', conversation_id: conversationId, call_id: callId,
        control: state.paused ? 'resume' : 'handoff' });
      if (controlVersion !== epoch.current) return;
      if (!result.ok) throw new Error(result.error || 'control_failed');
      setState(result); setHistoryFrame(null);
    } catch { if (controlVersion === epoch.current) setError(text('控制请求未确认；请检查状态，不要重复提交。', 'Control outcome is unconfirmed. Check status before repeating.')); }
    finally { setBusy(false); setRetry(v => v + 1); }
  }

  if (!selected) return null;
  return <section ref={container} data-testid="chat-browser-trace" aria-label={text('浏览器操作全过程', 'Browser operation timeline')}
    className="mb-5 max-h-screen overflow-auto rounded-xl border border-[color:var(--line)] bg-[color:var(--card)] text-[color:var(--text-base)]">
    <header className="flex flex-wrap items-center justify-between gap-2 border-b border-[color:var(--line)] px-4 py-3">
      <div className="flex items-center gap-2 text-sm font-semibold"><span aria-hidden>▣</span>{text('浏览器现场', 'Browser live view')}
        <span role="status" className={`rounded-full px-2 py-0.5 text-[10px] font-medium ${running ? 'bg-ok/10 text-ok' : 'bg-ink-500/10 text-[color:var(--text-muted)]'}`}>{status}</span>
      </div>
      <div className="flex items-center gap-2">
        <button type="button" className="btn btn-ghost text-xs" disabled={!state.controllable || busy} onClick={() => void control()}>
          {busy ? text('正在处理…', 'Working…') : state.paused ? text('恢复控制', 'Resume control') : text('人工接管', 'Take over')}
        </button>
        <button type="button" className="btn btn-ghost text-xs" onClick={() => void container.current?.requestFullscreen().catch(() => setError(text('此环境不支持全屏。', 'Fullscreen is unavailable.')))}>{text('放大', 'Expand')}</button>
      </div>
    </header>
    {calls.length > 1 && <nav aria-label={text('浏览器调用记录', 'Browser calls')} className="flex gap-1 overflow-x-auto border-b border-[color:var(--line)] px-3 py-2">
      {calls.map((call, i) => <button type="button" key={call.id} aria-pressed={call.id === callId} onClick={() => { setChosen(call.id); setHistoryFrame(null); }}
        className={`shrink-0 rounded-md px-3 py-1.5 text-xs ${call.id === callId ? 'bg-brand-500/10 text-brand-300' : 'text-[color:var(--text-muted)]'}`}>{i + 1}. {name(call.operation)}</button>)}
      <button type="button" onClick={() => { setChosen(null); setHistoryFrame(null); }} className="shrink-0 px-3 text-xs text-[color:var(--text-muted)]">{text('跟随最新', 'Follow latest')}</button>
    </nav>}
    <div className="flex min-h-8 items-center gap-2 border-b border-[color:var(--line)] px-4 py-2 text-[11px] text-[color:var(--text-muted)]">
      <span aria-hidden>↗</span><span className="min-w-0 truncate" title={state.frame?.url}>{state.frame?.url || text('等待当前页面', 'Waiting for current page')}</span>
      {historyFrame && <button className="ml-auto shrink-0 text-brand-300" onClick={() => setHistoryFrame(null)}>{text('回到实时', 'Back to live')}</button>}
    </div>
    {!!state.frame?.tabs?.length && <div className="flex gap-3 overflow-x-auto px-4 py-1 text-[10px] text-[color:var(--text-muted)]" aria-label={text('当前标签页', 'Current tabs')}>
      {state.frame.tabs.map(tab => <span key={tab.id} className={`max-w-48 shrink-0 truncate ${tab.selected ? 'font-semibold text-[color:var(--text-base)]' : ''}`}>{tab.selected ? '● ' : ''}{tab.url || 'about:blank'}</span>)}
    </div>}
    <div className="flex aspect-[8/5] max-h-[65vh] w-full items-center justify-center overflow-hidden bg-ink-950/5">
      {state.frame?.image && !state.paused && !error ? <>
        {/* Operator pixels stay local to this component, never the transcript/cache. */}
        {/* eslint-disable-next-line @next/next/no-img-element */}
        <img src={state.frame.image} alt={text('对话中的浏览器实时画面', 'Live browser viewport in conversation')} className="h-full w-full object-contain" />
      </> : <p className="max-w-md px-6 text-center text-sm text-[color:var(--text-muted)]">
        {state.paused ? text('人工接管或敏感界面期间暂停画面。操作步骤仍保留在下方。', 'Preview is paused during native takeover or sensitive UI. Steps remain below.')
          : error || (state.frame_state === 'expired' || (!running && !state.frame) ? text('画面未保留或已过期；操作记录仍可查看。', 'Frames are unavailable or expired; the operation log is still available.')
          : text('正在连接本次操作的浏览器画面…', 'Connecting to this browser operation…'))}
      </p>}
    </div>
    <div className="border-t border-[color:var(--line)] px-4 py-3">
      <div className="mb-2 flex items-center justify-between gap-2 text-xs"><span className="font-semibold">{current ? `${name(current.action || '')} ${current.target || ''}` : text('操作过程', 'Operation timeline')}</span><span className="shrink-0 tabular-nums text-[color:var(--text-muted)]">{finished}/{steps.length} {text('已完成', 'completed')}</span></div>
      <ol className="max-h-64 space-y-0.5 overflow-y-auto" aria-label={text('全部操作步骤', 'All operation steps')}>
        {steps.map(step => <li key={step.index} data-browser-step={step.index} className="flex items-start gap-2 rounded px-1 py-1.5 text-xs">
          <span className={`w-4 shrink-0 ${step.phase === 'failed' ? 'text-danger' : step.phase === 'completed' ? 'text-ok' : 'text-[color:var(--text-muted)]'}`} aria-hidden>{step.phase === 'completed' ? '✓' : step.phase === 'failed' ? '!' : step.phase === 'skipped' ? '–' : '◌'}</span>
          <span className="min-w-0 flex-1 break-words"><span className="font-medium">{(step.index || 0) + 1}. {name(step.action || '')}</span>{step.target ? ` · ${step.target}` : ''}
            <span className="ml-2 text-[10px] text-[color:var(--text-muted)]">{step.phase === 'started' ? text('执行中', 'Running') : step.phase === 'failed' ? text('失败', 'Failed') : step.phase === 'skipped' ? text('未执行', 'Not executed') : text('完成', 'Done')}</span>
            {step.error && <span className="block text-danger">{step.error}</span>}
          </span>
          {!!step.frame_id && <button type="button" className="shrink-0 text-[10px] text-brand-300" onClick={() => setHistoryFrame(step.frame_id!)}>{text('查看画面', 'View frame')}</button>}
        </li>)}
      </ol>
      {state.events.filter(e => ['request_failed', 'replayed'].includes(e.kind)).map(e => <p key={e.seq} className="mt-2 break-words text-xs text-[color:var(--text-muted)]">{e.kind === 'replayed' ? text('返回原操作回执，没有再次执行。', 'Original receipt returned; actions were not repeated.') : e.error}</p>)}
      <p className="mt-3 text-[10px] leading-relaxed text-[color:var(--text-muted)]">{text('画面仅供操作员查看；敏感界面暂停显示。步骤记录保留，临时画面可能过期。', 'Operator-only view. Sensitive UI is hidden. Step logs remain; temporary frames may expire.')}</p>
      {state.storage_error && <p role="alert" className="mt-1 text-xs text-warn">{text('步骤记录未能保存，刷新后可能不可用。', 'Step history could not be saved and may be unavailable after reload.')}</p>}
    </div>
  </section>;
}
