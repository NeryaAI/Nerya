"use client";

import { ChoiceSelect } from "../ChoiceSelect";

import { useEffect, useMemo, useRef, useState } from "react";
import { useLocale } from "next-intl";
import { copy as i18nCopy, type ResourceTranslator } from "../../lib/i18n";
import { ChevronRightIcon, SendIcon } from "../icons";
import { RoleAvatar } from "../RoleAvatar";
import { WorkspaceTabs } from "./WorkspaceTabs";
import { AgentConversation } from "./AgentConversation";
import { FinanceDraftContext, appendReviewDraft } from "../finance/FinanceReview";
import type { ChatResult } from "../../lib/chatResults";
import { agentRequest, isWorking, type AgentDetail, type AgentWork, type AgentWorkSource } from "./useAgentWork";

const controls = "rounded-lg px-3 py-2 text-xs transition-colors focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-brand-400 disabled:cursor-not-allowed disabled:opacity-40";
const quiet = `${controls} text-[color:var(--text-muted)] hover:bg-ink-800/40`;
function useCopy(): ResourceTranslator {
  const zh = useLocale().startsWith("zh");
  return (key: string, values?: Record<string, unknown>) => i18nCopy(zh, key, values);
}
function statusText(state: string, text: ResourceTranslator): string {
  const names: Record<string, string> = { running: "copy.agentStatus.001", planned: "copy.agentStatus.002", queued: "copy.agentStatus.003", pending: "copy.agentStatus.004", completed: "copy.agentStatus.005", failed: "copy.agentStatus.006", timeout: "copy.agentStatus.007", blocked: "copy.agentStatus.008", cancelled: "copy.agentStatus.009", interrupted: "copy.agentStatus.010", skipped: "copy.agentStatus.011" };
  return names[state] ? text(names[state]) : text("copy.components_chat_AgentWorkspace.001");
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
      <span className="native-avatar-stack flex shrink-0 -space-x-1">{source.rows.slice(0, 3).map((agent) => <RoleAvatar key={agent.id} role={agent.name} size={24} />)}</span>
      <span className="min-w-0 flex-1 truncate text-[13px] font-medium text-ink-200">{first.title || text("copy.components_chat_AgentWorkspace.002")}</span>
      <span className="shrink-0 text-xs tabular-nums text-[color:var(--text-muted)]">{source.error ? text("copy.components_chat_AgentWorkspace.003") : working ? text("copy.components_chat_AgentWorkspace.004", { value0: working }) : text("copy.components_chat_AgentWorkspace.005", { value0: source.rows.length })}</span>
      <ChevronRightIcon size={14} className={`shrink-0 text-[color:var(--text-muted)] ${open ? "rotate-90" : ""}`} />
    </button>
  </div>;
}

export type AgentFocusRequest = { id: string; count: number; attempt?: number };
export function AgentWorkPanel({ source, active = true, onOpenResult, focusRequest }: {
  source: AgentWorkSource; active?: boolean; onOpenResult?: (result: ChatResult) => void; focusRequest?: AgentFocusRequest;
}) {
  const text = useCopy();
  const zh = useLocale().startsWith("zh");
  const [memberFilter, setMemberFilter] = useState("all");
  const [chosenGroup, setChosenGroup] = useState("");
  const [chosenAgent, setChosenAgent] = useState("");
  const [visited, setVisited] = useState<Set<string>>(() => new Set());
  const groups = useMemo(() => [...new Map(source.rows.map((a) => [a.group_id, a.title])).entries()], [source.rows]);
  const group = groups.some(([id]) => id === chosenGroup) ? chosenGroup : groups[0]?.[0];
  const groupMembers = source.rows.filter(a => a.group_id === group);
  const section = (state:string):"attention"|"active"|"ended" => ["failed","blocked","timeout","interrupted"].includes(state) ? "attention" : isWorking(state) ? "active" : "ended";
  const priority = {attention:0,active:1,ended:2};
  const members = groupMembers.filter(a => memberFilter === "all" || section(a.state) === memberFilter)
    .sort((a,b) => priority[section(a.state)]-priority[section(b.state)] || a.name.localeCompare(b.name));
  const selected = members.find((a) => a.id === chosenAgent) || members.find((a) => isWorking(a.state)) || members[0];
  useEffect(() => {
    if (group && group !== chosenGroup) setChosenGroup(group);
    if (selected && selected.id !== chosenAgent) setChosenAgent(selected.id);
    if (selected) setVisited((old) => old.has(selected.id) ? old : new Set([...old, selected.id]));
  }, [group, chosenGroup, selected?.id, chosenAgent]);
  const requestedAgent = source.rows.find((a) => a.id === focusRequest?.id);
  useEffect(() => {
    if (!requestedAgent) return;
    setMemberFilter("all");
    setChosenGroup(requestedAgent.group_id); setChosenAgent(requestedAgent.id);
  }, [focusRequest?.count, requestedAgent?.id, requestedAgent?.group_id]);
  return <div className="flex h-full min-h-0 flex-col" data-testid="agent-work-panel">
    {groups.length > 1 ? <div className="shrink-0 px-4 py-3"><ChoiceSelect aria-label={text("copy.components_chat_AgentWorkspace.006")} value={group}
      onValueChange={(value) => { setChosenGroup(value); setChosenAgent(""); }} className="w-full text-sm">
      {groups.map(([id, title]) => <option key={id} value={id}>{title || text("copy.components_chat_AgentWorkspace.007")}</option>)}
    </ChoiceSelect></div> : null}
    {source.error ? <div role="status" className="px-4 py-3 text-xs text-warn">{text("copy.components_chat_AgentWorkspace.008")}<button type="button" onClick={source.refresh} className={quiet}>{text("copy.components_chat_AgentWorkspace.009")}</button></div> : null}
    {groupMembers.length > 0 && <div className="flex flex-wrap gap-1 border-b border-[color:var(--line)] px-3 py-2" role="group" aria-label={i18nCopy(zh, "copy.components_chat_AgentWorkspace.046")}>
      {([["all",i18nCopy(zh, "copy.components_chat_AgentWorkspace.047")],["attention",i18nCopy(zh, "copy.components_chat_AgentWorkspace.048")],["active",i18nCopy(zh, "copy.components_chat_AgentWorkspace.049")],["ended",i18nCopy(zh, "copy.components_chat_AgentWorkspace.050")]] as const).map(([value,label]) => <button key={value} type="button" aria-pressed={memberFilter===value} onClick={()=>setMemberFilter(value)} className={"min-h-11 rounded px-3 text-xs " + (memberFilter===value?"bg-[color:var(--panel-bg)] text-[color:var(--text-base)]":"text-[color:var(--text-muted)]")}>
        {label} · {value==="all"?groupMembers.length:groupMembers.filter(a=>section(a.state)===value).length}
      </button>)}
    </div>}
    {members.length ? <WorkspaceTabs id="agent-members" label={text("copy.components_chat_AgentWorkspace.010")} value={selected?.id || ""} onChange={setChosenAgent}
      tabs={members.map((agent) => ({ id: agent.id, label: agent.name, portrait: <RoleAvatar role={agent.name} size={26} alt="" />, meta: <Status state={agent.state} /> }))} /> : null}
    {source.rows.map((agent) => <section key={agent.id} role="tabpanel" id={`agent-members-panel-${agent.id}`}
      aria-labelledby={agent.group_id === group ? `agent-members-tab-${agent.id}` : undefined} aria-label={agent.group_id === group ? undefined : agent.name}
      hidden={selected?.id !== agent.id} className={selected?.id === agent.id ? "flex min-h-0 flex-1 flex-col" : "hidden"}>
      {visited.has(agent.id) || selected?.id === agent.id ? <AgentInspector row={agent} source={source} active={active && selected?.id === agent.id} onOpenResult={onOpenResult}
        focusRequest={focusRequest?.id === agent.id ? focusRequest : undefined} /> : null}
    </section>)}
    {!selected ? <div className="m-auto max-w-md px-6 py-12 text-center"><h2 className="text-base font-medium">{text("copy.components_chat_AgentWorkspace.011")}</h2><p className="mt-3 text-sm leading-relaxed text-[color:var(--text-muted)]">{text("copy.components_chat_AgentWorkspace.012")}</p></div> : null}
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
      setNotice(action === "message" ? text("copy.components_chat_AgentWorkspace.013") : text("copy.components_chat_AgentWorkspace.014"));
    } catch (error) { setActionError(error instanceof Error ? error.message : String(error)); }
    finally { actionLock.current = false; setBusy(""); source.refresh(); setRefresh((n) => n + 1); }
  }
  const contextPanel = <details data-testid="agent-context" className="mx-auto max-w-[860px] text-xs text-[color:var(--text-muted)]">
        <summary className="flex min-h-9 cursor-pointer items-center justify-between gap-3 rounded focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-brand-400">
          <span>{row.legacy ? text("copy.components_chat_AgentWorkspace.015") : context?.scope === "explicit_payload_only" ? text("copy.components_chat_AgentWorkspace.016") : text("copy.components_chat_AgentWorkspace.017")}</span>
          <span>{text("copy.components_chat_AgentWorkspace.018")}</span>
        </summary>
        <p className="py-2 leading-relaxed">{row.legacy ? text("copy.components_chat_AgentWorkspace.019") : text("copy.components_chat_AgentWorkspace.020")}</p>
        {context ? <dl className="grid grid-cols-2 gap-x-6 gap-y-3 py-3">
          {[[text("copy.components_chat_AgentWorkspace.021"), context.inherited_messages], [text("copy.components_chat_AgentWorkspace.022"), context.saved_messages], [text("copy.components_chat_AgentWorkspace.023"), current.attempt], [text("copy.components_chat_AgentWorkspace.024"), (Array.isArray(context.allowed_skills) ? context.allowed_skills.join(", ") : '') || text("copy.components_chat_AgentWorkspace.025")], [text("copy.components_chat_AgentWorkspace.026"), context.scope === "explicit_payload_only" ? text("copy.components_chat_AgentWorkspace.027") : text("copy.components_chat_AgentWorkspace.028")], [text("copy.components_chat_AgentWorkspace.029"), context.model || text("copy.components_chat_AgentWorkspace.030")]].map(([label, value]) => <div key={label}><dt>{label}</dt><dd className="mt-1 break-words text-ink-100">{value}</dd></div>)}
        </dl> : null}
        <p className="break-all pb-3">Agent ID: {id}</p>
      </details>;
  return <div className="relative flex min-h-0 flex-1 flex-col">
    {loadError ? <div role="status" className="px-4 py-2 text-xs text-warn">{text("copy.components_chat_AgentWorkspace.031")} <button type="button" onClick={() => setRefresh((n) => n + 1)} className="underline">{text("copy.components_chat_AgentWorkspace.032")}</button></div> : null}
    <div ref={scrollRef} className="min-h-0 flex-1 overflow-y-auto" data-testid="agent-detail-content" onScroll={() => {
      const el = scrollRef.current; if (!el) return;
      const near = el.scrollHeight - el.scrollTop - el.clientHeight < 100;
      stickToBottom.current = near; setAway(!near);
    }}>
      <div className="mx-auto w-full max-w-[860px] px-4 pb-6 sm:px-5">
        {contextPanel}
        {hasMore ? <button type="button" onClick={earlier} disabled={loadingOlder} className={`${quiet} mt-3 w-full`}>{text("copy.components_chat_AgentWorkspace.033")}</button> : null}
        <FinanceDraftContext.Provider value={{ disabled: Boolean(busy) || Boolean(row.legacy) || context?.scope === "explicit_payload_only", append: (review) => {
          const next = appendReviewDraft(draft, review);
          setDraft(next); setNotice(text("copy.components_chat_AgentWorkspace.035"));
          requestAnimationFrame(() => inputRef.current?.focus());
        } }}><AgentConversation row={current} detail={detail} rows={source.rows} onOpenResult={onOpenResult} /></FinanceDraftContext.Provider>
      </div>
    </div>
    {away ? <button type="button" className={`${quiet} mx-auto my-1 shrink-0 border border-[color:var(--line)] bg-[color:var(--card)]`} onClick={() => { stickToBottom.current = true; setAway(false); scrollRef.current?.scrollTo({ top: scrollRef.current.scrollHeight }); }}>{text("copy.components_chat_AgentWorkspace.036")}</button> : null}
    {!row.legacy && context?.scope !== "explicit_payload_only" ? <form className="shrink-0 px-3 pb-3 pt-2 sm:px-5" onSubmit={(e) => { e.preventDefault(); void submit(running ? "message" : "resume"); }}>
      <div className="mx-auto max-w-[860px] rounded-2xl border border-[color:var(--line-hi)] bg-[color:var(--card-hi)] p-3">
        <label htmlFor={`agent-message-${id}`} className="sr-only">{text("copy.components_chat_AgentWorkspace.037", { value0: row.name })}</label>
        <textarea ref={inputRef} id={`agent-message-${id}`} value={draft} onChange={(e) => setDraft(e.target.value)} rows={1} disabled={Boolean(busy)}
          onKeyDown={(e) => { if (e.key === "Enter" && !e.shiftKey && !e.nativeEvent.isComposing && e.keyCode !== 229) { e.preventDefault(); void submit(running ? "message" : "resume"); } }}
          placeholder={running ? text("copy.components_chat_AgentWorkspace.038") : text("copy.components_chat_AgentWorkspace.039", { value0: row.name })}
          className="block min-h-8 w-full resize-none overflow-y-auto bg-transparent px-1 py-1 text-base leading-6 text-ink-100 placeholder:text-[color:var(--text-muted)] focus:outline-none" />
        <div className="mt-2 flex flex-wrap items-center justify-between gap-2">
          <span className="text-[11px] text-[color:var(--text-muted)]">{running ? text("copy.components_chat_AgentWorkspace.040") : text("copy.components_chat_AgentWorkspace.041")}</span>
          <div className="ml-auto flex gap-1">
            {!running ? <button type="button" onClick={() => void submit("message")} disabled={!draft.trim() || Boolean(busy)} className={quiet}>{text("copy.components_chat_AgentWorkspace.042")}</button> : null}
            <button type="submit" disabled={!draft.trim() || Boolean(busy)} className={`${controls} inline-flex items-center gap-2 bg-brand-600 text-white hover:bg-brand-500`}>
              <SendIcon size={14} />{busy ? text("copy.components_chat_AgentWorkspace.043") : running ? text("copy.components_chat_AgentWorkspace.044") : text("copy.components_chat_AgentWorkspace.045")}
            </button>
          </div>
        </div>
      </div>
      {notice ? <p role="status" className="mx-auto mt-2 max-w-[860px] text-xs text-[color:var(--text-muted)]">{notice}</p> : null}
      {actionError ? <p role="alert" className="mx-auto mt-2 max-w-[860px] break-words text-xs text-danger">{actionError}</p> : null}
    </form> : null}
  </div>;
}
