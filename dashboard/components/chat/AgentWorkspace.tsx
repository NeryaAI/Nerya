"use client";

import { ChoiceSelect } from "../ChoiceSelect";

import { useEffect, useMemo, useRef, useState } from "react";
import { useLocale } from "next-intl";
import { ChevronRightIcon, MessagesIcon, SendIcon } from "../icons";
import { WorkspaceTabs } from "./WorkspaceTabs";
import { AgentConversation } from "./AgentConversation";
import { FinanceDraftContext, appendReviewDraft } from "../finance/FinanceReview";
import type { ChatResult } from "../../lib/chatResults";
import { agentRequest, isWorking, type AgentDetail, type AgentWork, type AgentWorkSource } from "./useAgentWork";

const controls = "rounded-lg px-3 py-2 text-xs transition-colors focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-brand-400 disabled:cursor-not-allowed disabled:opacity-40";
const quiet = `${controls} text-[color:var(--text-muted)] hover:bg-ink-800/40`;
function useCopy() {
  const zh = useLocale().startsWith("zh");
  return (cn: string, en: string) => zh ? cn : en;
}
function statusText(state: string, text: (cn: string, en: string) => string): string {
  const names: Record<string, [string, string]> = {
    running: ["进行中", "Running"], planned: ["准备中", "Starting"], queued: ["排队中", "Queued"], pending: ["等待中", "Pending"],
    completed: ["已完成", "Completed"], failed: ["失败", "Failed"], timeout: ["超时", "Timed out"],
    blocked: ["受阻", "Blocked"], cancelled: ["已停止", "Stopped"], interrupted: ["已中断", "Interrupted"], skipped: ["已跳过", "Skipped"],
  };
  return names[state] ? text(...names[state]) : text("状态未知", "Unknown status");
}
function Status({ state }: { state: string }) {
  const text = useCopy();
  const tone = isWorking(state) ? "bg-brand-400" : state === "completed" ? "bg-ok" : ["failed", "timeout"].includes(state) ? "bg-danger" : "bg-warn";
  return <span className="inline-flex shrink-0 items-center gap-1.5 text-xs text-[color:var(--text-muted)]">
    <span aria-hidden="true" className={`h-1.5 w-1.5 rounded-full ${tone} ${isWorking(state) ? "motion-safe:animate-pulse" : ""}`} />
    {statusText(state, text)}
  </span>;
}

export function AgentTaskBar({ source, open, onOpen }: { source: AgentWorkSource; open: boolean; onOpen: () => void }) {
  const text = useCopy();
  const first = source.rows[0];
  if (!first) return null;
  const working = source.rows.filter((a) => isWorking(a.state)).length;
  return <div className="w-full shrink-0" data-testid="agent-task-bar">
    <button id="agent-task-trigger" type="button" onClick={onOpen} aria-expanded={open} aria-controls="chat-workspace-panel-agents" title={first.title}
      className="group flex min-h-11 w-full items-center gap-3 rounded-t-2xl border border-b-0 border-[color:var(--line-hi)] bg-[color:var(--card-hi)] px-3.5 py-2 text-left transition-colors hover:bg-ink-800/40 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-inset focus-visible:ring-brand-400">
      <MessagesIcon size={16} className="shrink-0 text-[color:var(--text-muted)]" />
      <span className="min-w-0 flex-1 truncate text-[13px] font-medium text-ink-200">{first.title || text("多 agent 任务", "Agent task")}</span>
      <span className="shrink-0 text-xs tabular-nums text-[color:var(--text-muted)]">{source.error ? text("连接中断", "Disconnected") : working ? text(`${working} 个进行中`, `${working} running`) : text(`${source.rows.length} 个 agent`, `${source.rows.length} agents`)}</span>
      <ChevronRightIcon size={14} className={`shrink-0 text-[color:var(--text-muted)] ${open ? "rotate-90" : ""}`} />
    </button>
  </div>;
}

export type AgentFocusRequest = { id: string; count: number; attempt?: number };
export function AgentWorkPanel({ source, active = true, onOpenResult, focusRequest }: {
  source: AgentWorkSource; active?: boolean; onOpenResult?: (result: ChatResult) => void; focusRequest?: AgentFocusRequest;
}) {
  const text = useCopy();
  const [chosenGroup, setChosenGroup] = useState("");
  const [chosenAgent, setChosenAgent] = useState("");
  const [visited, setVisited] = useState<Set<string>>(() => new Set());
  const groups = useMemo(() => [...new Map(source.rows.map((a) => [a.group_id, a.title])).entries()], [source.rows]);
  const group = groups.some(([id]) => id === chosenGroup) ? chosenGroup : groups[0]?.[0];
  const members = source.rows.filter((a) => a.group_id === group).sort((a, b) => a.name.localeCompare(b.name));
  const selected = members.find((a) => a.id === chosenAgent) || members.find((a) => isWorking(a.state)) || members[0];
  useEffect(() => {
    if (group && group !== chosenGroup) setChosenGroup(group);
    if (selected && selected.id !== chosenAgent) setChosenAgent(selected.id);
    if (selected) setVisited((old) => old.has(selected.id) ? old : new Set([...old, selected.id]));
  }, [group, chosenGroup, selected?.id, chosenAgent]);
  const requestedAgent = source.rows.find((a) => a.id === focusRequest?.id);
  useEffect(() => {
    if (!requestedAgent) return;
    setChosenGroup(requestedAgent.group_id); setChosenAgent(requestedAgent.id);
  }, [focusRequest?.count, requestedAgent?.id, requestedAgent?.group_id]);
  return <div className="flex h-full min-h-0 flex-col" data-testid="agent-work-panel">
    {groups.length > 1 ? <div className="shrink-0 px-4 py-3"><ChoiceSelect aria-label={text("选择协作任务", "Select agent task")} value={group}
      onValueChange={(value) => { setChosenGroup(value); setChosenAgent(""); }} className="w-full text-sm">
      {groups.map(([id, title]) => <option key={id} value={id}>{title || text("协作任务", "Agent task")}</option>)}
    </ChoiceSelect></div> : null}
    {source.error ? <div role="status" className="px-4 py-3 text-xs text-warn">{text("连接中断，显示最后一次保存的状态。", "Connection interrupted. Showing the last saved state.")}<button type="button" onClick={source.refresh} className={quiet}>{text("重试", "Retry")}</button></div> : null}
    {members.length ? <WorkspaceTabs id="agent-members" label={text("任务成员", "Task members")} value={selected?.id || ""} onChange={setChosenAgent}
      tabs={members.map((agent) => ({ id: agent.id, label: agent.name, meta: <Status state={agent.state} /> }))} /> : null}
    {source.rows.map((agent) => <section key={agent.id} role="tabpanel" id={`agent-members-panel-${agent.id}`}
      aria-labelledby={agent.group_id === group ? `agent-members-tab-${agent.id}` : undefined} aria-label={agent.group_id === group ? undefined : agent.name}
      hidden={selected?.id !== agent.id} className={selected?.id === agent.id ? "flex min-h-0 flex-1 flex-col" : "hidden"}>
      {visited.has(agent.id) || selected?.id === agent.id ? <AgentInspector row={agent} source={source} active={active && selected?.id === agent.id} onOpenResult={onOpenResult}
        focusRequest={focusRequest?.id === agent.id ? focusRequest : undefined} /> : null}
    </section>)}
    {!selected ? <div className="m-auto max-w-md px-6 py-12 text-center"><h2 className="text-base font-medium">{text("还没有协作成员", "No collaborators yet")}</h2><p className="mt-3 text-sm leading-relaxed text-[color:var(--text-muted)]">{text("任务分配后，在这里切换成员，查看执行过程、结果并继续对话。", "Once work is delegated, switch between agents here to read their progress, review results and continue the conversation.")}</p></div> : null}
  </div>;
}

function AgentInspector({ row, source, active, onOpenResult, focusRequest }: {
  row: AgentWork; source: AgentWorkSource; active: boolean; onOpenResult?: (result: ChatResult) => void; focusRequest?: AgentFocusRequest;
}) {
  const text = useCopy();
  const [detail, setDetail] = useState<AgentDetail | null>(null);
  const [loadError, setLoadError] = useState("");
  const [actionError, setActionError] = useState("");
  const [notice, setNotice] = useState("");
  const [draft, setDraft] = useState("");
  const [busy, setBusy] = useState("");
  const [refresh, setRefresh] = useState(0);
  const [hasMore, setHasMore] = useState(false);
  const [loadingOlder, setLoadingOlder] = useState(false);
  const [away, setAway] = useState(false);
  const olderLoaded = useRef(false);
  const actionLock = useRef(false);
  const requestRef = useRef({ key: "", id: "" });
  const scrollRef = useRef<HTMLDivElement>(null);
  const stickToBottom = useRef(true);
  const inputRef = useRef<HTMLTextAreaElement>(null);
  const id = row.id, sessionId = source.sessionId;
  const current = detail?.agent && detail.agent.updated_at >= row.updated_at ? detail.agent : row;
  const context = current.context;
  const running = isWorking(current.state);
  useEffect(() => {
    if (row.legacy || !active) return;
    const controller = new AbortController();
    let cancelled = false;
    let timer: ReturnType<typeof setTimeout>;
    async function poll() {
      try {
        const next = await agentRequest<AgentDetail>("/teams/agents/get", { session_id: sessionId, agent_id: id }, controller.signal);
        if (cancelled) return;
        if (next.agent.id !== id || next.agent.session_id !== sessionId || !Array.isArray(next.events)) throw new Error("Invalid agent conversation response");
        setDetail((old) => ({ ...next, events: [...new Map([...(old?.events || []), ...next.events].map((e) => [e.seq, e])).values()].sort((a, b) => a.seq - b.seq) }));
        if (!olderLoaded.current) setHasMore(next.has_more);
        setLoadError("");
      } catch (error) {
        if (!cancelled) setLoadError(error instanceof Error ? error.message : String(error));
      }
      if (!cancelled) timer = setTimeout(poll, document.hidden ? 15000 : 1200);
    }
    void poll();
    return () => { cancelled = true; controller.abort(); clearTimeout(timer); };
  }, [id, sessionId, row.legacy, refresh, active]);
  useEffect(() => {
    const el = inputRef.current;
    if (el) { el.style.height = "auto"; el.style.height = `${Math.min(el.scrollHeight, 200)}px`; }
  }, [draft, active]);
  useEffect(() => {
    if (active && stickToBottom.current && !loadingOlder) scrollRef.current?.scrollTo({ top: scrollRef.current.scrollHeight });
  }, [active, detail?.events.length, detail?.messages.length, current.state, current.attempt, loadingOlder]);
  useEffect(() => {
    if (!active || !focusRequest?.attempt) return;
    stickToBottom.current = false;
    const frame = requestAnimationFrame(() => {
      const el = scrollRef.current?.querySelector<HTMLElement>(`[data-agent-run="${focusRequest.attempt}"]`);
      el?.scrollIntoView({ block: "start" });
      el?.querySelector<HTMLElement>("[data-turn-role='assistant']")?.focus({ preventScroll: true });
    });
    return () => cancelAnimationFrame(frame);
  }, [active, focusRequest?.count, focusRequest?.attempt]);
  async function earlier() {
    if (loadingOlder || !detail?.events.length) return;
    stickToBottom.current = false;
    const height = scrollRef.current?.scrollHeight || 0, top = scrollRef.current?.scrollTop || 0;
    setLoadingOlder(true);
    try {
      const next = await agentRequest<AgentDetail>("/teams/agents/get", { session_id: sessionId, agent_id: id, before: detail.events[0].seq });
      olderLoaded.current = true; setHasMore(next.has_more);
      setDetail((old) => old ? { ...old, events: [...new Map([...next.events, ...old.events].map((e) => [e.seq, e])).values()].sort((a, b) => a.seq - b.seq) } : next);
      requestAnimationFrame(() => { if (scrollRef.current) scrollRef.current.scrollTop = top + scrollRef.current.scrollHeight - height; });
    } catch (error) { setLoadError(error instanceof Error ? error.message : String(error)); }
    finally { setLoadingOlder(false); }
  }
  async function submit(action: "message" | "resume") {
    const message = draft.trim();
    if (!message || actionLock.current || row.legacy || context?.scope === "explicit_payload_only") return;
    actionLock.current = true; setBusy(action); setActionError(""); setNotice("");
    const key = `${id}:${action}:${message}`;
    if (requestRef.current.key !== key) requestRef.current = { key, id: crypto.randomUUID() };
    try {
      await agentRequest(`/teams/agents/${action}`, { session_id: sessionId, agent_id: id, message, request_id: requestRef.current.id });
      setDraft(""); requestRef.current = { key: "", id: "" };
      stickToBottom.current = true; setAway(false);
      setNotice(action === "message" ? text("消息已排队，接收状态会显示在对话中。", "Message queued. Delivery status appears in the conversation.") : text("已沿用这个 agent 继续执行。", "Continuing with this agent and its saved context."));
    } catch (error) { setActionError(error instanceof Error ? error.message : String(error)); }
    finally { actionLock.current = false; setBusy(""); source.refresh(); setRefresh((n) => n + 1); }
  }
  const contextPanel = <details data-testid="agent-context" className="mx-auto max-w-[860px] text-xs text-[color:var(--text-muted)]">
        <summary className="flex min-h-9 cursor-pointer items-center justify-between gap-3 rounded focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-brand-400">
          <span>{row.legacy ? text("历史记录", "Historical record") : context?.scope === "explicit_payload_only" ? text("隔离上下文", "Isolated context") : text("沿用任务上下文", "Task context retained")}</span>
          <span>{text("上下文与权限", "Context & permissions")}</span>
        </summary>
        <p className="py-2 leading-relaxed">{row.legacy ? text("这条历史记录没有可续跑的会话快照。", "This historical record has no resumable conversation snapshot.") : text("继续对话保留同一身份、角色、工具权限和此前保存的工具对话。", "Continuing retains this agent's identity, role, permissions and saved tool conversation.")}</p>
        {context ? <dl className="grid grid-cols-2 gap-x-6 gap-y-3 py-3">
          {[[text("父会话消息", "Inherited parent messages"), context.inherited_messages], [text("已保存的对话消息", "Saved conversation messages"), context.saved_messages], [text("执行次数", "Runs"), current.attempt], [text("技能权限", "Allowed skills"), (Array.isArray(context.allowed_skills) ? context.allowed_skills.join(", ") : '') || text("未配置技能白名单", "No skill allowlist configured")], [text("上下文范围", "Context scope"), context.scope === "explicit_payload_only" ? text("隔离任务", "Isolated task") : text("继承父任务", "Inherited task context")], [text("模型", "Model"), context.model || text("沿用角色配置", "Role configuration")]].map(([label, value]) => <div key={label}><dt>{label}</dt><dd className="mt-1 break-words text-ink-100">{value}</dd></div>)}
        </dl> : null}
        <p className="break-all pb-3">Agent ID: {id}</p>
      </details>;
  return <div className="relative flex min-h-0 flex-1 flex-col">
    {loadError ? <div role="status" className="px-4 py-2 text-xs text-warn">{text("刷新失败，保留已有记录。", "Refresh failed; saved records are still shown.")} <button type="button" onClick={() => setRefresh((n) => n + 1)} className="underline">{text("重试", "Retry")}</button></div> : null}
    <div ref={scrollRef} className="min-h-0 flex-1 overflow-y-auto" data-testid="agent-detail-content" onScroll={() => {
      const el = scrollRef.current; if (!el) return;
      const near = el.scrollHeight - el.scrollTop - el.clientHeight < 100;
      stickToBottom.current = near; setAway(!near);
    }}>
      <div className="mx-auto w-full max-w-[860px] px-4 pb-6 sm:px-5">
        {contextPanel}
        {hasMore ? <button type="button" onClick={earlier} disabled={loadingOlder} className={`${quiet} mt-3 w-full`}>{text("加载更早的对话", "Load earlier conversation")}</button> : null}
        <FinanceDraftContext.Provider value={{ disabled: Boolean(busy) || Boolean(row.legacy) || context?.scope === "explicit_payload_only", append: (review) => {
          const next = appendReviewDraft(draft, review);
          if (next.length > 16000) { setNotice(text("草稿过长，请先缩短内容再加入复盘。", "Draft is too long; shorten it before adding this review.")); return; }
          setDraft(next); setNotice(text("已填入此成员的草稿，确认后再发送。", "Added to this agent’s draft. Review before sending."));
          requestAnimationFrame(() => inputRef.current?.focus());
        } }}><AgentConversation row={current} detail={detail} rows={source.rows} onOpenResult={onOpenResult} /></FinanceDraftContext.Provider>
      </div>
    </div>
    {away ? <button type="button" className={`${quiet} mx-auto my-1 shrink-0 border border-[color:var(--line)] bg-[color:var(--card)]`} onClick={() => { stickToBottom.current = true; setAway(false); scrollRef.current?.scrollTo({ top: scrollRef.current.scrollHeight }); }}>{text("回到最新消息 ↓", "Jump to latest ↓")}</button> : null}
    {!row.legacy && context?.scope !== "explicit_payload_only" ? <form className="shrink-0 px-3 pb-3 pt-2 sm:px-5" onSubmit={(e) => { e.preventDefault(); void submit(running ? "message" : "resume"); }}>
      <div className="mx-auto max-w-[860px] rounded-2xl border border-[color:var(--line-hi)] bg-[color:var(--card-hi)] p-3">
        <label htmlFor={`agent-message-${id}`} className="sr-only">{text(`发给 ${row.name}`, `Message ${row.name}`)}</label>
        <textarea ref={inputRef} id={`agent-message-${id}`} value={draft} onChange={(e) => setDraft(e.target.value)} maxLength={16000} rows={1} disabled={Boolean(busy)}
          onKeyDown={(e) => { if (e.key === "Enter" && !e.shiftKey && !e.nativeEvent.isComposing && e.keyCode !== 229) { e.preventDefault(); void submit(running ? "message" : "resume"); } }}
          placeholder={running ? text("补充信息，或调整当前任务…", "Add context or guide the current task…") : text(`继续与 ${row.name} 对话…`, `Continue with ${row.name}…`)}
          className="block min-h-8 w-full resize-none overflow-y-auto bg-transparent px-1 py-1 text-base leading-6 text-ink-100 placeholder:text-[color:var(--text-muted)] focus:outline-none" />
        <div className="mt-2 flex flex-wrap items-center justify-between gap-2">
          <span className="text-[11px] text-[color:var(--text-muted)]">{running ? text("下一轮接收补充消息", "Delivered at the next iteration") : text("保留上下文继续执行", "Continue with saved context")}</span>
          <div className="ml-auto flex gap-1">
            {!running ? <button type="button" onClick={() => void submit("message")} disabled={!draft.trim() || Boolean(busy)} className={quiet}>{text("仅加入消息队列", "Queue only")}</button> : null}
            <button type="submit" disabled={!draft.trim() || Boolean(busy)} className={`${controls} inline-flex items-center gap-2 bg-brand-600 text-white hover:bg-brand-500`}>
              <SendIcon size={14} />{busy ? text("提交中…", "Submitting…") : running ? text("发送消息", "Send message") : text("继续此 agent", "Continue agent")}
            </button>
          </div>
        </div>
      </div>
      {notice ? <p role="status" className="mx-auto mt-2 max-w-[860px] text-xs text-[color:var(--text-muted)]">{notice}</p> : null}
      {actionError ? <p role="alert" className="mx-auto mt-2 max-w-[860px] break-words text-xs text-danger">{actionError}</p> : null}
    </form> : null}
  </div>;
}
