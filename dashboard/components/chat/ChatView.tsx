"use client";

import { useEffect, useMemo, useRef, useState } from "react";
import { callApi, clientApi, type ApprovalCard } from "../../lib/clientApi";
import {
  ChatMessage,
  ChatModelOption,
  ChatRunSettings,
  ChatThread,
  DEFAULT_CHAT_RUN_SETTINGS,
  LiveEvent,
  TurnPayload,
  deriveTitle,
  loadActiveId,
  loadRunSettings,
  loadThreads,
  newThread,
  saveActiveId,
  saveRunSettings,
  saveThreads,
  upsertThread,
  uuid,
} from "../../lib/chat";

function parseTs(ts: string | undefined | null): number | null {
  if (!ts) return null;
  const v = typeof ts === "string" ? Date.parse(ts) : Number(ts);
  return Number.isFinite(v) && v > 0 ? v : null;
}
import { AssistantBubble, UserBubble } from "./ChatMessage";
import { ChatInput } from "./ChatInput";
import { ChatSidebar } from "./ChatSidebar";

function EmptyState({ onPrompt }: { onPrompt: (s: string) => void }) {
  const suggestions: { title: string; body: string; prompt: string }[] = [
    {
      title: "Write a monitoring script",
      body: "Monitor BTCUSDT 5m bars for a MACD cross and propose an approval-gated script.",
      prompt:
        "Write me a Python script (paper mode only, using the Nerya script sandbox) that monitors BTCUSDT 5-minute bars on Binance. On a MACD golden cross (12,26,9) open a small long, on a death cross close it. Include a 1% trailing stop. Save it as btc_macd_5m.py and propose it as a script for approval.",
    },
    {
      title: "Create a subagent",
      body: "Spawn a narrative_watcher subagent that tracks news + social sentiment.",
      prompt:
        "Create a new subagent called narrative_watcher that tracks crypto news and social sentiment, and writes a daily brief. Generate its .agent.md with a clear system prompt.",
    },
    {
      title: "Schedule a heartbeat",
      body: "Every 60s, have the main agent review the portfolio and flag risk.",
      prompt:
        "Schedule a recurring portfolio heartbeat every 60 seconds. The main agent should review open positions, unrealized P&L, and flag any risk breaches via the message skill.",
    },
    {
      title: "Run a postmortem",
      body: "Orchestrate strategy_reviewer + risk_critic on a losing strategy.",
      prompt:
        "The btc_momentum strategy took a sharp loss today. Orchestrate strategy_reviewer + risk_critic to run a postmortem — root cause, execution quality, and one concrete proposal. Write the proposal into evolution/.",
    },
  ];
  return (
    <div className="max-w-3xl mx-auto px-6 py-16">
      <div className="text-center mb-10">
        <div className="inline-flex items-center gap-2 text-[10px] uppercase tracking-[0.22em] text-brand-300 mb-3">
          <span className="w-1 h-1 rounded-full bg-fluid-400 shadow-[0_0_8px_rgba(34,211,238,0.7)]" />
          Nerya Chat
          <span className="w-1 h-1 rounded-full bg-fluid-400 shadow-[0_0_8px_rgba(34,211,238,0.7)]" />
        </div>
        <h1 className="text-[34px] leading-[1.1] font-semibold tracking-tight text-gradient-brand">
          Talk to your trading runtime
        </h1>
        <p className="mt-4 text-ink-300 text-sm max-w-xl mx-auto leading-relaxed">
          Every message drives one real agent turn — route selection, tool
          calls, and on-disk artifacts. Suggestions below are working examples.
        </p>
      </div>
      <div className="grid grid-cols-1 md:grid-cols-2 gap-3">
        {suggestions.map((s) => (
          <button
            key={s.title}
            onClick={() => onPrompt(s.prompt)}
            className="group text-left glass hover:bg-white/[0.05] hover:border-brand-500/30 px-4 py-3.5 transition-colors cursor-pointer"
          >
            <div className="flex items-start justify-between gap-2">
              <div className="text-sm text-white font-medium">{s.title}</div>
              <span className="text-brand-300/60 group-hover:text-brand-200 transition-colors text-xs leading-none">
                →
              </span>
            </div>
            <div className="text-xs text-ink-400 mt-1.5 leading-relaxed">
              {s.body}
            </div>
          </button>
        ))}
      </div>
    </div>
  );
}

export function ChatView() {
  const [threads, setThreads] = useState<ChatThread[]>([]);
  const [activeId, setActiveId] = useState<string | null>(null);
  const [input, setInput] = useState("");
  const [sending, setSending] = useState(false);
  const [hydrated, setHydrated] = useState(false);
  const [settings, setSettings] = useState<ChatRunSettings>(
    DEFAULT_CHAT_RUN_SETTINGS,
  );
  const [modelOptions, setModelOptions] = useState<ChatModelOption[]>([]);
  const [modelProviders, setModelProviders] = useState<string[]>([]);
  const [editingMessageId, setEditingMessageId] = useState<string | null>(null);
  const [editDraft, setEditDraft] = useState("");
  const [pendingApprovals, setPendingApprovals] = useState<Map<string, ApprovalCard>>(
    () => new Map(),
  );
  const [resolvingApprovalIds, setResolvingApprovalIds] = useState<Set<string>>(
    () => new Set(),
  );
  const abortRef = useRef<AbortController | null>(null);
  const scrollRef = useRef<HTMLDivElement | null>(null);
  // Live-event poller handle. We track it so ``cancel`` and unmount
  // can stop the timer; without this guard a fast click-and-cancel
  // would leak a setInterval and keep hitting ``/agent/stream/events``
  // forever.
  const livePollRef = useRef<{
    timer: ReturnType<typeof setInterval> | null;
    stop: boolean;
  }>({ timer: null, stop: false });

  useEffect(() => {
    return () => {
      // Tear down any live-event poller on unmount so we don't keep
      // hammering the bus after the user navigates away.
      const handle = livePollRef.current;
      handle.stop = true;
      if (handle.timer) {
        clearInterval(handle.timer);
        handle.timer = null;
      }
    };
  }, []);

  useEffect(() => {
    const loaded = loadThreads();
    const savedActive = loadActiveId();
    const savedSettings = loadRunSettings();
    setSettings(savedSettings);
    setThreads(loaded);
    setActiveId(
      savedActive && loaded.some((t) => t.id === savedActive)
        ? savedActive
        : loaded[0]?.id ?? null
    );
    setHydrated(true);
    void hydrateModelOptions();
    // Fold in conversations that were started outside the dashboard
    // (curl / gateway / scripted runs) by walking backend sessions and
    // pulling their reconstructed transcript. Local threads always win
    // — we only import sessions whose ``session_id`` is not already a
    // local thread ``id``.
    void hydrateBackendSessions(loaded);
    // Refresh imported sessions when the tab regains focus so curl
    // turns that landed while the dashboard was hidden show up.
    function onVisibility() {
      if (document.visibilityState === "visible") {
        // Re-read the latest local threads via a state callback so we
        // don't capture a stale closure.
        setThreads((prev) => {
          void hydrateBackendSessions(prev);
          return prev;
        });
      }
    }
    document.addEventListener("visibilitychange", onVisibility);
    return () => {
      document.removeEventListener("visibilitychange", onVisibility);
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  async function hydrateModelOptions() {
    try {
      const [tiersResp, modelsResp] = await Promise.allSettled([
        clientApi.llmTiers(),
        clientApi.llmModels(),
      ]);
      const options: ChatModelOption[] = [];
      const seen = new Set<string>();
      const providerSet = new Set<string>();

      if (tiersResp.status === "fulfilled") {
        for (const tier of tiersResp.value?.tiers ?? []) {
          const provider = String(tier.provider || "").trim().toLowerCase();
          const model = String(tier.model || "").trim();
          if (!provider || !model) continue;
          providerSet.add(provider);
          const key = `tier:${tier.tier}:${provider}:${model}`;
          seen.add(`${provider}:${model}:${tier.tier || ""}`);
          options.push({
            key,
            label: `${tier.tier}: ${provider}/${model}`,
            tier: tier.tier,
            provider,
            model,
            source: "tier",
          });
        }
      }

      if (modelsResp.status === "fulfilled") {
        const providers = modelsResp.value?.providers ?? {};
        for (const [providerRaw, rows] of Object.entries(providers)) {
          const provider = providerRaw.trim().toLowerCase();
          if (!provider) continue;
          providerSet.add(provider);
          for (const row of rows.slice(0, 120)) {
            const model = String(
              row.id || row.model || row.name || row.model_id || "",
            ).trim();
            if (!model) continue;
            const dedupe = `${provider}:${model}:`;
            if (seen.has(dedupe)) continue;
            seen.add(dedupe);
            options.push({
              key: `catalog:${provider}:${model}`,
              label: `${provider}/${model}`,
              provider,
              model,
              source: "catalog",
            });
          }
        }
      }

      setModelOptions(options.slice(0, 240));
      setModelProviders(Array.from(providerSet).sort());
    } catch {
      // The chat still works with the runtime default model.
    }
  }

  function approvalIdFromCallback(callbackData: string): string {
    const idx = callbackData.indexOf(":");
    return idx >= 0 ? callbackData.slice(idx + 1).trim() : "";
  }

  function approvalActionFromCallback(callbackData: string): string {
    const idx = callbackData.indexOf(":");
    return (idx >= 0 ? callbackData.slice(0, idx) : callbackData)
      .trim()
      .toLowerCase();
  }

  function approvalEventFromCard(id: string, card: ApprovalCard): LiveEvent {
    const record = card.record ?? {};
    const tool =
      record.tool && typeof record.tool === "object"
        ? (record.tool as Record<string, unknown>)
        : {};
    return {
      kind: "approval.request",
      seq: Number(record.seq || 0),
      ts:
        typeof record.created_at === "number"
          ? record.created_at
          : Date.now() / 1000,
      approval_id: id,
      session_id:
        typeof record.session_id === "string" ? record.session_id : undefined,
      strategy_id:
        typeof record.strategy_id === "string" ? record.strategy_id : undefined,
      turn_id: typeof record.turn_id === "string" ? record.turn_id : undefined,
      call_id: String(
        record.tool_use_id ||
          tool.call_id ||
          card.prompt?.metadata?.tool_use_id ||
          "",
      ),
      prompt: card.prompt,
      record,
      reason: card.prompt?.text || String(record.reason || ""),
    };
  }

  function appendApprovalResolutionEvent(id: string, state: string) {
    if (!id || !activeId) return;
    const resolved: LiveEvent = {
      kind: "approval.resolved",
      seq: Date.now(),
      ts: Date.now() / 1000,
      approval_id: id,
      state,
      session_id: activeId,
    };
    updateThread(activeId, (t) => ({
      ...t,
      messages: t.messages.map((m) => {
        if (m.role !== "assistant") return m;
        const hasApproval = (m.live_events ?? []).some(
          (ev) =>
            (ev.kind === "approval.request" || ev.kind === "approval.resolved") &&
            String(ev.approval_id || "") === id,
        );
        if (!hasApproval) return m;
        return {
          ...m,
          live_events: [...(m.live_events ?? []), resolved],
        };
      }),
    }));
  }

  function attachPendingApprovalEvents(next: Map<string, ApprovalCard>) {
    if (!next.size) return;
    setThreads((prev) =>
      prev.map((thread) => {
        let changed = false;
        const messages = thread.messages.map((m) => ({ ...m }));
        for (const [id, card] of next) {
          const record = card.record ?? {};
          const sessionId = String(record.session_id || "");
          if (sessionId && sessionId !== thread.id) continue;
          const turnId = String(record.turn_id || "");
          let targetIdx = -1;
          if (turnId) {
            targetIdx = messages.findIndex(
              (m) =>
                m.role === "assistant" &&
                (m.turn?.turn_id === turnId ||
                  m.backend_message_id === `${turnId}:assistant`),
            );
          }
          if (targetIdx < 0) {
            for (let i = messages.length - 1; i >= 0; i -= 1) {
              if (messages[i].role === "assistant") {
                targetIdx = i;
                break;
              }
            }
          }
          if (targetIdx < 0) continue;
          const target = messages[targetIdx];
          if (target.role !== "assistant") continue;
          const exists = (target.live_events ?? []).some(
            (ev) => ev.kind === "approval.request" && String(ev.approval_id || "") === id,
          );
          if (exists) continue;
          messages[targetIdx] = {
            ...target,
            live_events: [
              ...(target.live_events ?? []),
              approvalEventFromCard(id, card),
            ],
          };
          changed = true;
        }
        return changed ? { ...thread, messages } : thread;
      }),
    );
  }

  async function refreshApprovals() {
    try {
      const resp = await clientApi.approvalsPending();
      const next = new Map<string, ApprovalCard>();
      for (const card of resp?.approvals ?? []) {
        const id =
          card.prompt?.approval_id ||
          String(card.record?.approval_id || card.record?.id || "");
        if (id) next.set(id, card);
      }
      setPendingApprovals(next);
      attachPendingApprovalEvents(next);
    } catch {
      // Backend may be down while the dashboard is still mounted.
    }
  }

  useEffect(() => {
    void refreshApprovals();
    const timer = setInterval(() => {
      void refreshApprovals();
    }, 2500);
    return () => clearInterval(timer);
  }, []);

  async function buildImportedThread(
    sid: string,
    sessionMeta: { created_at?: string; updated_at?: string; title?: string },
  ): Promise<ChatThread | null> {
    const t = await clientApi.sessionTranscript(sid);
    if (!t?.ok || !Array.isArray(t.messages) || t.messages.length === 0) {
      return null;
    }
    const created = parseTs(sessionMeta.created_at) ?? Date.now();
    const updated = parseTs(sessionMeta.updated_at) ?? created;
    const msgs: ChatMessage[] = [];
    let firstUser = "";
    for (const m of t.messages) {
      const ts = parseTs(m.ts) ?? created;
      if (m.role === "user") {
        if (m.meta?.source === "approval_continue") continue;
        if (!firstUser) firstUser = m.content;
        msgs.push({
          id: uuid(),
          role: "user",
          ts,
          text: m.content,
          backend_message_id: m.message_id,
        });
      } else {
        msgs.push({
          id: uuid(),
          role: "assistant",
          ts,
          turn: { reply_text: m.content, turn_id: m.turn_id },
          elapsed_ms: 0,
          backend_message_id: m.message_id,
        });
      }
    }
    const titleSeed = sessionMeta.title || firstUser || `Session ${sid.slice(0, 8)}`;
    return {
      id: sid,
      title: deriveTitle(titleSeed),
      created_ts: created,
      updated_ts: updated,
      messages: msgs,
      imported: true,
      imported_at: Date.now(),
    };
  }

  async function hydrateBackendSessions(localThreads: ChatThread[]) {
    try {
      const resp = await clientApi.sessionList(undefined, 50);
      const sessions = Array.isArray(resp?.sessions) ? resp.sessions : [];
      if (sessions.length === 0) return;
      const localById = new Map(localThreads.map((t) => [t.id, t]));
      const refreshed: ChatThread[] = [];
      for (const s of sessions) {
        const sid = s.session_id;
        if (!sid) continue;
        const existing = localById.get(sid);
        // Skip locally-grown threads (i.e. created in this browser) —
        // they may be richer than the journal-reconstructed transcript
        // (e.g. carry live_events, blocks, errors). Only import
        // sessions that are entirely new, or refresh ones we
        // previously imported.
        if (existing && !existing.imported) continue;
        try {
          const built = await buildImportedThread(sid, {
            created_at: s.created_at,
            updated_at: s.updated_at,
            title:
              typeof s.meta?.title === "string" ? s.meta.title : undefined,
          });
          if (built) refreshed.push(built);
        } catch {
          // ignore single-session failures
        }
      }
      if (refreshed.length === 0) return;
      setThreads((prev) => {
        const byId = new Map(prev.map((t) => [t.id, t]));
        for (const t of refreshed) byId.set(t.id, t);
        const merged = Array.from(byId.values());
        merged.sort((a, b) => b.updated_ts - a.updated_ts);
        return merged;
      });
    } catch {
      // Backend unreachable — local threads still render.
    }
  }

  useEffect(() => {
    if (!hydrated) return;
    saveThreads(threads);
  }, [threads, hydrated]);

  useEffect(() => {
    if (!hydrated) return;
    saveActiveId(activeId);
  }, [activeId, hydrated]);

  useEffect(() => {
    if (!hydrated) return;
    saveRunSettings(settings);
  }, [settings, hydrated]);

  const active = useMemo(
    () => threads.find((t) => t.id === activeId) || null,
    [threads, activeId]
  );

  useEffect(() => {
    if (!scrollRef.current) return;
    scrollRef.current.scrollTo({
      top: scrollRef.current.scrollHeight,
      behavior: "smooth",
    });
  }, [active?.messages.length, active?.id]);

  function createThread(seedText?: string): ChatThread {
    const t = newThread(seedText);
    setThreads((prev) => upsertThread(prev, t));
    setActiveId(t.id);
    return t;
  }

  function updateThread(id: string, fn: (t: ChatThread) => ChatThread) {
    setThreads((prev) =>
      prev.map((t) => (t.id === id ? { ...fn(t), updated_ts: Date.now() } : t))
    );
  }

  function deleteThread(id: string) {
    setThreads((prev) => {
      const next = prev.filter((t) => t.id !== id);
      if (activeId === id) {
        setEditingMessageId(null);
        setEditDraft("");
        setActiveId(next[0]?.id ?? null);
      }
      return next;
    });
  }

  function messageText(message: ChatMessage): string {
    return message.role === "user"
      ? message.text
      : message.turn?.reply_text || message.turn?.final_text || "";
  }

  function startEditMessage(messageId: string) {
    const thread = active;
    if (!thread) return;
    const message = thread.messages.find((m) => m.id === messageId);
    if (!message) return;
    setEditingMessageId(messageId);
    setEditDraft(messageText(message));
  }

  function cancelEditMessage() {
    setEditingMessageId(null);
    setEditDraft("");
  }

  async function saveEditedMessage(messageId: string) {
    const thread = active;
    if (!thread) return;
    const message = thread.messages.find((m) => m.id === messageId);
    if (!message) return;
    const clean = editDraft.trim();
    if (!clean) return;

    updateThread(thread.id, (t) => ({
      ...t,
      messages: t.messages.map((m) => {
        if (m.id !== messageId) return m;
        if (m.role === "user") return { ...m, text: clean };
        return {
          ...m,
          turn: {
            ...(m.turn ?? {}),
            reply_text: clean,
            final_text: clean,
          },
        };
      }),
    }));
    setEditingMessageId(null);
    setEditDraft("");

    const backendId = message.backend_message_id;
    if (backendId) {
      try {
        await clientApi.sessionMessageEdit({
          session_id: thread.id,
          message_id: backendId,
          content: clean,
        });
      } catch {
        // Keep the local edit; the next backend refresh may reconcile it.
      }
    }
  }

  async function deleteMessage(messageId: string) {
    const thread = active;
    if (!thread) return;
    const message = thread.messages.find((m) => m.id === messageId);
    if (!message) return;
    if (!window.confirm("Delete this message?")) return;
    if (editingMessageId === messageId) {
      setEditingMessageId(null);
      setEditDraft("");
    }
    updateThread(thread.id, (t) => ({
      ...t,
      messages: t.messages.filter((m) => m.id !== messageId),
    }));
    const backendId = message.backend_message_id;
    if (backendId) {
      try {
        await clientApi.sessionMessageDelete({
          session_id: thread.id,
          message_id: backendId,
        });
      } catch {
        // Local delete is still useful for the current dashboard view.
      }
    }
  }

  async function resolveApproval(callbackData: string) {
    const approvalId = approvalIdFromCallback(callbackData);
    const action = approvalActionFromCallback(callbackData);
    if (!approvalId) return;
    setResolvingApprovalIds((prev) => new Set(prev).add(approvalId));
    try {
      const res = await clientApi.approvalCallback({
        callback_data: callbackData,
        actor_id: "dashboard",
      });
      const state = String(res?.state || "").toLowerCase();
      if (state === "approved" || state === "rejected") {
        const resolvedIds =
          Array.isArray(res?.approval_ids) && res.approval_ids.length
            ? res.approval_ids.filter((id): id is string => typeof id === "string" && !!id)
            : [approvalId];
        for (const id of resolvedIds) appendApprovalResolutionEvent(id, state);
        setPendingApprovals((prev) => {
          if (!resolvedIds.some((id) => prev.has(id))) return prev;
          const next = new Map(prev);
          for (const id of resolvedIds) next.delete(id);
          return next;
        });
      }
      await refreshApprovals();
      if (res?.ok && action === "approve" && state === "approved") {
        await runAgentTurn(
          "The requested permission was approved. Continue from the pending tool call, retry the approved action, and proceed with the original task.",
          {
            visibleUser: false,
            source: "approval_continue",
            kind: "approval.continue",
            channel: "approval_continue",
            payloadExtra: {
              approval_id: approvalId,
              approval_callback_data: callbackData,
              approval_action: action,
              approval_state: state,
            },
          },
        );
      }
    } finally {
      setResolvingApprovalIds((prev) => {
        const next = new Set(prev);
        next.delete(approvalId);
        return next;
      });
    }
  }

  async function runAgentTurn(
    text: string,
    options: {
      visibleUser?: boolean;
      source?: string;
      kind?: string;
      channel?: string;
      payloadExtra?: Record<string, unknown>;
    } = {},
  ) {
    const clean = text.trim();
    if (!clean) return;
    const visibleUser = options.visibleUser !== false;

    let thread = active;
    if (!thread) {
      thread = createThread(clean);
    }
    const threadId = thread.id;

    const userMsgId = uuid();
    const assistantId = uuid();
    const startedMs = Date.now();
    const userMessage = {
      id: userMsgId,
      role: "user" as const,
      ts: Date.now(),
      text: clean,
    };
    const assistantMessage = {
      id: assistantId,
      role: "assistant" as const,
      ts: Date.now(),
      loading: true,
      started_ms: startedMs,
    };

    updateThread(threadId, (t) => ({
      ...t,
      title:
        t.messages.length === 0 && visibleUser ? deriveTitle(clean) : t.title,
      messages: visibleUser
        ? [...t.messages, userMessage, assistantMessage]
        : [...t.messages, assistantMessage],
    }));

    if (visibleUser) setInput("");
    setSending(true);

    const ctrl = new AbortController();
    abortRef.current = ctrl;

    // Anchor the live-events cursor *before* the turn fires. We use
    // the bus's current ``latest_seq`` so we ignore everything that
    // happened in earlier turns (otherwise the freshest message
    // would inherit a stale "approval requested" event from
    // five turns ago and look perpetually pending).
    let cursor = 0;
    try {
      const head = await clientApi.streamEvents(undefined, { limit: 1 });
      cursor =
        typeof head?.latest_seq === "number"
          ? head.latest_seq
          : typeof head?.cursor === "number"
          ? head.cursor
          : 0;
    } catch {
      // Ignore — the poller below will simply fetch from the start.
    }

    // Reset the per-turn poller state so a previous turn's stop flag
    // doesn't terminate this one immediately.
    livePollRef.current.stop = false;
    if (livePollRef.current.timer) {
      clearInterval(livePollRef.current.timer);
      livePollRef.current.timer = null;
    }

    const handle = livePollRef.current;

    async function pollOnce(): Promise<void> {
      if (handle.stop) return;
      try {
        const resp = await clientApi.streamEvents(cursor, { limit: 500 });
        const fresh: LiveEvent[] = Array.isArray(resp?.events)
          ? (resp.events as LiveEvent[])
          : [];
        if (fresh.length > 0) {
          const maxSeq = fresh.reduce(
            (acc, ev) =>
              typeof ev.seq === "number" && ev.seq > acc ? ev.seq : acc,
            cursor,
          );
          cursor = maxSeq;
          updateThread(threadId, (t) => ({
            ...t,
            messages: t.messages.map((m) =>
              m.id === assistantId && m.role === "assistant"
                ? {
                    ...m,
                    live_events: [...(m.live_events ?? []), ...fresh],
                    live_cursor: maxSeq,
                  }
                : m,
            ),
          }));
        } else if (typeof resp?.latest_seq === "number") {
          // Even when no events match the cursor still advances — the
          // bus may have rolled the ring. Pin to whichever is larger.
          cursor = Math.max(cursor, resp.latest_seq);
        }
      } catch {
        // Network blips are non-fatal — the next tick will retry. We
        // intentionally swallow errors so a transient 502 from the
        // local server doesn't terminate the whole chat thread.
      }
    }

    handle.timer = setInterval(() => {
      pollOnce();
    }, 600);
    // Fire one poll immediately so the UI doesn't sit blank for the
    // first 600ms while the agent is already churning.
    pollOnce();

    function stopPoller(): void {
      handle.stop = true;
      if (handle.timer) {
        clearInterval(handle.timer);
        handle.timer = null;
      }
    }

    try {
      const res = await callApi<TurnPayload>("/agent/run_turn", {
        method: "POST",
        body: {
          source: options.source || "user_chat",
          kind: options.kind || "user.chat",
          target: "main",
          payload: {
            text: clean,
            channel: options.channel || "dashboard",
            ...(options.payloadExtra ?? {}),
          },
          // Reuse the thread id as the session id so every turn in
          // this chat lands in the same on-disk SessionState. This is
          // what lets the kernel reload prior chat history (context
          // persistence) and what lets curl / gateway turns show up
          // here (any caller that supplies the same session id is
          // grouped under the same thread).
          session_id: threadId,
          reasoning_effort:
            settings.reasoning_effort === "off"
              ? undefined
              : settings.reasoning_effort,
          reasoning_summary:
            settings.reasoning_effort === "off" ? undefined : "auto",
          permission_mode: settings.permission_mode,
          model_tier: settings.model_tier || undefined,
          model_provider: settings.model_provider || undefined,
          model_id: settings.model_id || undefined,
        },
      });
      // One last poll to drain the tail of events that fired
      // between the last interval tick and the run_turn response.
      await pollOnce();
      stopPoller();
      const turnId = typeof res.turn_id === "string" ? res.turn_id : "";
      let backendTitle = "";
      try {
        const session = await clientApi.sessionGet(threadId);
        if (!("error" in session) && typeof session.meta?.title === "string") {
          backendTitle = session.meta.title;
        }
      } catch {
        backendTitle = "";
      }
      updateThread(threadId, (t) => ({
        ...t,
          title: backendTitle || t.title,
          messages: t.messages.map((m) =>
            visibleUser && m.id === userMsgId && m.role === "user" && turnId
              ? { ...m, backend_message_id: `${turnId}:user` }
              : m.id === assistantId && m.role === "assistant"
            ? {
                ...m,
                loading: false,
                turn: res,
                elapsed_ms: Date.now() - startedMs,
                backend_message_id: turnId ? `${turnId}:assistant` : undefined,
              }
            : m
        ),
      }));
    } catch (e) {
      // Drain whatever the bus already produced so the failure card
      // shows the events that *did* fire before the crash.
      try {
        await pollOnce();
      } catch {
        // ignore
      }
      stopPoller();
      const msg =
        ctrl.signal.aborted
          ? "Cancelled. The backend may still be finishing this turn; check the journals."
          : e instanceof Error
          ? e.message
          : String(e);
      updateThread(threadId, (t) => ({
        ...t,
        messages: t.messages.map((m) =>
          m.id === assistantId && m.role === "assistant"
            ? {
                ...m,
                loading: false,
                error: msg,
                elapsed_ms: Date.now() - startedMs,
              }
            : m
        ),
      }));
    } finally {
      stopPoller();
      setSending(false);
      abortRef.current = null;
    }
  }

  async function send(text: string) {
    await runAgentTurn(text, { visibleUser: true });
  }

  function cancel() {
    if (abortRef.current) abortRef.current.abort();
    // Also stop the live-event poller so we don't keep hitting the
    // bus after the user explicitly aborted.
    const handle = livePollRef.current;
    handle.stop = true;
    if (handle.timer) {
      clearInterval(handle.timer);
      handle.timer = null;
    }
  }

  if (!hydrated) {
    return (
      <div className="h-full flex items-center justify-center text-ink-500 text-sm">
        Loading chat…
      </div>
    );
  }

  return (
    <div className="h-[calc(100vh-0px)] flex">
      <ChatSidebar
        threads={threads}
        activeId={activeId}
        onPick={setActiveId}
        onNew={() => createThread()}
        onDelete={deleteThread}
      />
      <div className="flex-1 flex flex-col min-w-0">
        <div ref={scrollRef} className="flex-1 overflow-y-auto">
          {!active || active.messages.length === 0 ? (
            <EmptyState onPrompt={(p) => setInput(p)} />
          ) : (
            <div className="max-w-4xl mx-auto px-4 py-6 space-y-5">
              {active.messages.map((m) =>
                m.role === "user" ? (
                  <UserBubble
                    key={m.id}
                    msg={m}
                    onEdit={() => startEditMessage(m.id)}
                    onDelete={() => deleteMessage(m.id)}
                    editing={editingMessageId === m.id}
                    editValue={editingMessageId === m.id ? editDraft : ""}
                    onEditChange={setEditDraft}
                    onSaveEdit={() => saveEditedMessage(m.id)}
                    onCancelEdit={cancelEditMessage}
                  />
                ) : (
                  <AssistantBubble
                    key={m.id}
                    msg={m}
                    pendingApprovals={pendingApprovals}
                    onApprovalAction={resolveApproval}
                    resolvingApprovalIds={resolvingApprovalIds}
                    onEdit={() => startEditMessage(m.id)}
                    onDelete={() => deleteMessage(m.id)}
                    editing={editingMessageId === m.id}
                    editValue={editingMessageId === m.id ? editDraft : ""}
                    onEditChange={setEditDraft}
                    onSaveEdit={() => saveEditedMessage(m.id)}
                    onCancelEdit={cancelEditMessage}
                  />
                )
              )}
            </div>
          )}
        </div>
        <ChatInput
          value={input}
          onChange={setInput}
          onSend={() => send(input)}
          onCancel={cancel}
          sending={sending}
          settings={settings}
          onSettingsChange={setSettings}
          modelOptions={modelOptions}
          modelProviders={modelProviders}
        />
      </div>
    </div>
  );
}
