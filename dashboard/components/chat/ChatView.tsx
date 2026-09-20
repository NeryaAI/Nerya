"use client";

import { useEffect, useMemo, useRef, useState } from "react";
import { useRouter } from "next/navigation";
import { useLocale, useTranslations } from "next-intl";
import {
  callApi,
  clientApi,
  type AgentSession,
  type ApprovalCard,
} from "../../lib/clientApi";
import { confirm as confirmDialog } from "../../lib/dialogs";
import {
  ChatAttachment,
  ChatMessage,
  ChatModelOption,
  ChatRunSettings,
  ChatThread,
  DEFAULT_CHAT_RUN_SETTINGS,
  buildChatModelOptions,
  cacheThreadTranscript,
  LiveEvent,
  TurnPayload,
  deriveTitle,
  loadCachedThreadTranscript,
  loadRunSettings,
  loadThreads,
  newThread,
  saveRunSettings,
  saveThreads,
  subscribeThreadsChanged,
  upsertThread,
  uuid,
} from "../../lib/chat";
import { AssistantBubble, UserBubble } from "./ChatMessage";
import { ChatInput } from "./ChatInput";
import { AgentStart } from "./AgentStart";
import { AgentTaskBar, AgentWorkPanel } from "./AgentWorkspace";
import { ChatTaskHeader } from "./ChatTaskHeader";
import { collectChatResults, type ChatResult } from "../../lib/chatResults";
import { childResult } from "../../lib/agentConversation";
import styles from "./ChatWorkbench.module.css";
import { useAgentWork } from "./useAgentWork";
import { WorkspaceCanvas, hasWorkspaceCanvas } from "./WorkspaceCanvas";
import { TaskDockHeader } from './TaskDockHeader';
import { useTaskDock, type TaskDockTab } from './useTaskDock';
import { BrowserWorkspacePanel } from './BrowserWorkspacePanel';
import { browserCalls } from '../../lib/browserTrace';
import { liveEventsToBlocks } from '../../lib/chat';
import { useCanvasLayout } from "./useCanvasLayout";
import { XIcon } from "../icons";
import { takeComposeDraftPayload } from "../../lib/composeDraft";
import { FinanceDraftContext, appendReviewDraft } from "../finance/FinanceReview";
import { toast } from "../../lib/dialogs";

function parseTs(ts: string | undefined | null): number | null {
  if (!ts) return null;
  const v = typeof ts === "string" ? Date.parse(ts) : Number(ts);
  return Number.isFinite(v) && v > 0 ? v : null;
}

const DELETED_SESSIONS_KEY = "nerya.chat.deletedSessions.v1";
const SESSION_PAGE_SIZE = 20;
const CANVAS_PANEL_KEY = "nerya.chat.canvasPanel.open.v1";
const LIVE_EVENT_POLL_MS = 160;

type PendingFirstMessage = {
  threadId: string;
  text: string;
  attachments?: ChatAttachment[];
};

const pendingFirstMessages = new Map<string, PendingFirstMessage>();

function loadCanvasPanelOpen(): boolean {
  if (typeof window === "undefined" || typeof localStorage === "undefined") {
    return true;
  }
  return localStorage.getItem(CANVAS_PANEL_KEY) !== "0";
}

function saveCanvasPanelOpen(open: boolean): void {
  if (typeof window === "undefined" || typeof localStorage === "undefined") {
    return;
  }
  try {
    localStorage.setItem(CANVAS_PANEL_KEY, open ? "1" : "0");
  } catch {
    // Non-essential UI preference.
  }
}

function loadDeletedSessionIds(): Set<string> {
  if (typeof window === "undefined" || typeof localStorage === "undefined") {
    return new Set();
  }
  try {
    const parsed = JSON.parse(localStorage.getItem(DELETED_SESSIONS_KEY) || "[]");
    return new Set(
      Array.isArray(parsed)
        ? parsed.filter((id): id is string => typeof id === "string" && !!id)
        : [],
    );
  } catch {
    return new Set();
  }
}

function rememberDeletedSession(id: string): Set<string> {
  const next = loadDeletedSessionIds();
  next.add(id);
  if (typeof window !== "undefined" && typeof localStorage !== "undefined") {
    try {
      localStorage.setItem(
        DELETED_SESSIONS_KEY,
        JSON.stringify(Array.from(next).slice(-500)),
      );
    } catch {
      // Ignore quota/privacy-mode failures; the backend delete still runs.
    }
  }
  return next;
}

function threadHasUnpersistedMessages(thread: ChatThread | null | undefined): boolean {
  return Boolean(
    thread?.messages.some((m) => {
      if (m.role === "assistant") return Boolean(m.loading) || !m.backend_message_id;
      return !m.backend_message_id;
    }),
  );
}

function sortThreadsByUpdated(threads: ChatThread[]): ChatThread[] {
  return threads
    .slice()
    .sort((a, b) => (Number(b.updated_ts) || 0) - (Number(a.updated_ts) || 0));
}

function selectInitialThreads(
  threads: ChatThread[],
  opts: { keepId?: string; limit: number },
): ChatThread[] {
  const sorted = sortThreadsByUpdated(threads);
  const pinnedIds = new Set<string>();
  if (opts.keepId) pinnedIds.add(opts.keepId);
  for (const thread of sorted) {
    if (threadHasUnpersistedMessages(thread)) pinnedIds.add(thread.id);
  }
  const selected: ChatThread[] = [];
  const seen = new Set<string>();
  for (const thread of sorted) {
    if (selected.length >= opts.limit && !pinnedIds.has(thread.id)) continue;
    if (seen.has(thread.id)) continue;
    selected.push(thread);
    seen.add(thread.id);
  }
  return selected;
}

function isoFromMs(ts: number | undefined): string | undefined {
  return typeof ts === "number" && Number.isFinite(ts) && ts > 0
    ? new Date(ts).toISOString()
    : undefined;
}

function sessionTitle(session: AgentSession, sid: string): string {
  const title =
    typeof session.meta?.title === "string"
      ? session.meta.title
      : String(session.meta?.title || "");
  // Persisted titles remain complete; CSS owns visual truncation.
  return title.trim() || `Session ${sid.slice(0, 8)}`;
}

function threadFromSessionMetadata(session: AgentSession): ChatThread | null {
  const sid = String(session.session_id || "").trim();
  if (!sid) return null;
  const created = parseTs(session.created_at) ?? Date.now();
  const updated = parseTs(session.updated_at) ?? created;
  const cached = loadCachedThreadTranscript(sid, updated);
  if (cached) {
    return {
      ...cached,
      title: cached.title || sessionTitle(session, sid),
      created_ts: created || cached.created_ts,
      updated_ts: Math.max(updated, cached.updated_ts),
      strategy_id: cached.strategy_id ?? session.strategy_id ?? undefined,
      message_count: Math.max(
        cached.message_count ?? 0,
        Number(session.message_count || 0),
        cached.messages.length,
      ),
    };
  }
  return {
    id: sid,
    title: sessionTitle(session, sid),
    created_ts: created,
    updated_ts: updated,
    messages: [],
    message_count: Number(session.message_count || 0),
    imported: true,
    imported_at: Date.now(),
    transcript_loaded: false,
    backend_updated_ts: updated,
    strategy_id: session.strategy_id ?? undefined,
  };
}

function chatMessageTextForMerge(message: ChatMessage): string {
  return message.role === "user"
    ? message.text
    : message.turn?.reply_text || message.turn?.final_text || "";
}

function mergeAuthoritativeThread(
  current: ChatThread | null | undefined,
  authoritative: ChatThread,
): ChatThread {
  if (!current) return authoritative;
  const currentByBackendId = new Map(
    current.messages
      .filter((m) => typeof m.backend_message_id === "string" && !!m.backend_message_id)
      .map((m) => [m.backend_message_id as string, m]),
  );
  const authoritativeMessages = authoritative.messages.map((message) => {
    const backendId = message.backend_message_id;
    const prior = backendId ? currentByBackendId.get(backendId) : null;
    if (!prior || prior.role !== message.role) return message;
    if (message.role === "user" && prior.role === "user") {
      return {
        ...message,
        id: prior.id,
        attachments: prior.attachments?.length
          ? prior.attachments
          : message.attachments,
      };
    }
    if (message.role === "assistant" && prior.role === "assistant") {
      return {
        ...message,
        id: prior.id,
        live_events: prior.live_events?.length
          ? prior.live_events
          : message.live_events,
        live_cursor: prior.live_cursor ?? message.live_cursor,
        started_ms: prior.started_ms ?? message.started_ms,
        elapsed_ms: prior.elapsed_ms ?? message.elapsed_ms,
      };
    }
    return message;
  });
  const stableAuthoritative = {
    ...authoritative,
    messages: authoritativeMessages,
  };
  const backendMessageIds = new Set(
    stableAuthoritative.messages
      .map((m) => m.backend_message_id)
      .filter((id): id is string => typeof id === "string" && !!id),
  );
  const pending = current.messages.filter((message) => {
    if (message.backend_message_id) {
      return !backendMessageIds.has(message.backend_message_id) && message.ts > stableAuthoritative.updated_ts + 1000;
    }
    if (message.ts <= stableAuthoritative.updated_ts + 1000) return false;
    const text = chatMessageTextForMerge(message).trim();
    if (!text) return true;
    return !stableAuthoritative.messages.some(
      (candidate) =>
        candidate.role === message.role &&
        chatMessageTextForMerge(candidate).trim() === text,
    );
  });
  if (!pending.length) return stableAuthoritative;
  return {
    ...stableAuthoritative,
    updated_ts: Math.max(stableAuthoritative.updated_ts, ...pending.map((m) => m.ts)),
    messages: [...stableAuthoritative.messages, ...pending],
    imported: false,
  };
}

function asRecord(value: unknown): Record<string, unknown> {
  return value && typeof value === "object"
    ? (value as Record<string, unknown>)
    : {};
}

function attachmentForRequest(attachment: ChatAttachment): ChatAttachment {
  if (!attachment.artifact_uri) return attachment;
  const { data_url: _dataUrl, text: _text, ...rest } = attachment;
  return rest;
}

function approvalState(value: unknown): string {
  const record = asRecord(value);
  const nested = asRecord(record.record);
  return String(
    record.state ||
      record.resolved_state ||
      nested.state ||
      "",
  ).toLowerCase();
}

function isApprovalResolved(value: unknown): boolean {
  const state = approvalState(value);
  return state === "approved" || state === "rejected";
}

function approvalIdFromRecord(value: unknown): string {
  const record = asRecord(value);
  return String(record.approval_id || record.id || "");
}

function approvalBlockFromEnvelope(value: unknown): Record<string, unknown> {
  const env = asRecord(value);
  return asRecord(env.block || env);
}

function cardSessionId(card: ApprovalCard): string {
  const record = asRecord(card.record);
  const metadata = asRecord(card.prompt?.metadata);
  return String(record.session_id || metadata.session_id || "");
}

function pendingApprovalIdsForThread(
  thread: ChatThread | null,
  pendingApprovals: Map<string, ApprovalCard>,
): string[] {
  if (!thread) return [];
  const requested = new Set<string>();
  const resolved = new Set<string>();
  const currentStopRequests = new Set<string>();
  const pendingRecordIds = new Set<string>();
  const latestAssistant = [...thread.messages]
    .reverse()
    .find(
      (msg): msg is Extract<ChatMessage, { role: "assistant" }> =>
        msg.role === "assistant",
    );
  const latestStoppedOnApproval =
    latestAssistant &&
    String(latestAssistant.turn?.stopped_reason || "").toLowerCase() ===
      "approval_pending";

  for (const msg of thread.messages) {
    if (msg.role !== "assistant") continue;
    for (const ev of msg.live_events ?? []) {
      const id = approvalIdFromRecord(ev);
      if (!id) continue;
      if (ev.kind === "approval.request") requested.add(id);
      if (ev.kind === "approval.resolved" || isApprovalResolved(ev)) {
        resolved.add(id);
      }
    }

    const stoppedOnApproval =
      latestStoppedOnApproval && msg.id === latestAssistant.id;
    for (const env of msg.turn?.blocks ?? []) {
      const block = approvalBlockFromEnvelope(env);
      if (String(block.kind || "") !== "approval_request") continue;
      const id = approvalIdFromRecord(block);
      if (!id) continue;
      requested.add(id);
      if (stoppedOnApproval) currentStopRequests.add(id);
      if (isApprovalResolved(block)) resolved.add(id);
    }
  }

  for (const [id, card] of pendingApprovals) {
    if (cardSessionId(card) !== thread.id) continue;
    requested.add(id);
    pendingRecordIds.add(id);
  }

  return Array.from(requested).filter(
    (id) =>
      !resolved.has(id) &&
      (pendingRecordIds.has(id) || currentStopRequests.has(id)),
  );
}

export function ChatView({ sessionId }: { sessionId?: string } = {}) {
  const router = useRouter();
  const t = useTranslations("chat");
  const tCommon = useTranslations("common");
  const cancelledReply = t("cancelNotice");
  const [threads, setThreads] = useState<ChatThread[]>([]);
  const [input, setInput] = useState("");
  const [attachments, setAttachments] = useState<ChatAttachment[]>([]);
  const [sending, setSending] = useState(false);
  const [hydrated, setHydrated] = useState(false);
  const [missingSession, setMissingSession] = useState(false);
  // Distinguish "the fetch blew up" from "the session does not exist" so
  // the failure case can offer a Retry instead of a dead end.
  const [transcriptLoadFailed, setTranscriptLoadFailed] = useState(false);
  const [transcriptRetry, setTranscriptRetry] = useState(0);
  const [settings, setSettings] = useState<ChatRunSettings>(
    DEFAULT_CHAT_RUN_SETTINGS,
  );
  const [modelOptions, setModelOptions] = useState<ChatModelOption[]>([]);
  const [editingMessageId, setEditingMessageId] = useState<string | null>(null);
  const [editDraft, setEditDraft] = useState("");
  const [pendingApprovals, setPendingApprovals] = useState<Map<string, ApprovalCard>>(
    () => new Map(),
  );
  const [resolvingApprovalIds, setResolvingApprovalIds] = useState<Set<string>>(
    () => new Set(),
  );
  const abortRef = useRef<AbortController | null>(null);
  const inFlightSessionRef = useRef<string | null>(null);
  const turnInFlightRef = useRef(false);
  const draftConsumedRef = useRef(false);
  const scrollRef = useRef<HTMLDivElement | null>(null);
  const transcriptRef = useRef<HTMLDivElement | null>(null);
  const followLatest = useRef(true);
  const lastScrollTop = useRef(0);
  const [readingHistory, setReadingHistory] = useState(false);
  function jumpToLatest() {
    followLatest.current = true; setReadingHistory(false);
    if (scrollRef.current) scrollRef.current.scrollTo({ top: scrollRef.current.scrollHeight });
  }
  // Live-event poller handle. We track it so ``cancel`` and unmount
  // can stop the timer; without this guard a fast click-and-cancel
  // would leak a setInterval and keep hitting ``/agent/stream/events``
  // forever.
  const livePollRef = useRef<{
    timer: ReturnType<typeof setInterval> | null;
    stop: boolean;
  }>({ timer: null, stop: false });
  const deletedSessionIdsRef = useRef<Set<string>>(new Set());
  const [sessionHasMore, setSessionHasMore] = useState(false);
  const [sessionLoading, setSessionLoading] = useState(false);
  const [sessionNextOffset, setSessionNextOffset] = useState(0);
  const [loadingTranscriptIds, setLoadingTranscriptIds] = useState<Set<string>>(
    () => new Set(),
  );
  const [canvasPanelOpen, setCanvasPanelOpen] = useState(true);

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
    const loaded = selectInitialThreads(loadThreads(), {
      keepId: sessionId,
      limit: SESSION_PAGE_SIZE,
    });
    const savedSettings = loadRunSettings();
    deletedSessionIdsRef.current = loadDeletedSessionIds();
    setSettings(savedSettings);
    setCanvasPanelOpen(loadCanvasPanelOpen());
    setThreads(loaded);
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

  // Hand-off from the Codex command home: when the new-chat route mounts
  // with a stashed composer draft, drop it into the input and auto-run the
  // turn so the home box behaves like Codex's "what should we build?" entry.
  useEffect(() => {
    if (!hydrated || draftConsumedRef.current || sessionId) return;
    const draft = takeComposeDraftPayload();
    if (!draft || (!draft.text.trim() && !draft.attachments.length)) return;
    draftConsumedRef.current = true;
    setInput(draft.text);
    setAttachments(draft.attachments);
    if (draft.autoSend) void runAgentTurn(draft.text, { visibleUser: true, attachments: draft.attachments });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [hydrated, sessionId]);

  // The conversation list now lives in the global Codex sidebar. If the
  // active chat is deleted from there (tombstoned), leave the dead route.
  useEffect(() => {
    if (!hydrated) return;
    return subscribeThreadsChanged(() => {
      if (!sessionId || !loadDeletedSessionIds().has(sessionId)) return;
      setThreads((prev) =>
        prev.some((t) => t.id === sessionId)
          ? prev.filter((t) => t.id !== sessionId)
          : prev,
      );
      router.replace("/chat");
    });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [hydrated, sessionId]);

  async function hydrateModelOptions() {
    try {
      const [tiersResp, modelsResp, configResp] = await Promise.allSettled([
        clientApi.llmTiers(),
        clientApi.llmModels(),
        clientApi.llmConfig(),
      ]);
      setModelOptions(
        buildChatModelOptions({
          tiers: tiersResp.status === "fulfilled" ? tiersResp.value : null,
          models: modelsResp.status === "fulfilled" ? modelsResp.value : null,
          config: configResp.status === "fulfilled" ? configResp.value : null,
        }),
      );
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
    if (!id || !sessionId) return;
    const resolved: LiveEvent = {
      kind: "approval.resolved",
      seq: Date.now(),
      ts: Date.now() / 1000,
      approval_id: id,
      state,
      session_id: sessionId,
    };
    updateThread(sessionId, (t) => ({
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
    opts: { full?: boolean } = {},
  ): Promise<ChatThread | null> {
    const t = await clientApi.sessionTranscript(sid, opts);
    if (!t?.ok || !Array.isArray(t.messages) || t.messages.length === 0) {
      return null;
    }
    const created = parseTs(t.created_at) ?? parseTs(sessionMeta.created_at) ?? Date.now();
    const updated = parseTs(t.updated_at) ?? parseTs(sessionMeta.updated_at) ?? created;
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
        // May-01 2026 — assistant rows now carry the full turn payload
        // (blocks / tool_trace / actions / budget) persisted by the
        // kernel. Prefer it over the bare ``{reply_text, turn_id}``
        // fallback so rehydrated sessions keep the tool_use timeline
        // the user saw during the live turn. Older rows that predate
        // the write still fall back to the minimal shape.
        const persistedTurn =
          m.turn && typeof m.turn === "object"
            ? (m.turn as TurnPayload)
            : null;
        const turn: TurnPayload = persistedTurn
          ? {
              ...persistedTurn,
              reply_text:
                typeof persistedTurn.reply_text === "string" &&
                persistedTurn.reply_text
                  ? persistedTurn.reply_text
                  : m.content,
              turn_id:
                typeof persistedTurn.turn_id === "string" &&
                persistedTurn.turn_id
                  ? persistedTurn.turn_id
                  : m.turn_id,
            }
          : { reply_text: m.content, turn_id: m.turn_id };
        msgs.push({
          id: uuid(),
          role: "assistant",
          ts,
          turn,
          elapsed_ms: 0,
          backend_message_id: m.message_id,
        });
      }
    }
    const savedTitle = t.title?.trim() || sessionMeta.title?.trim();
    const thread: ChatThread = {
      id: sid,
      title: savedTitle || deriveTitle(firstUser || `Session ${sid.slice(0, 8)}`),
      created_ts: created,
      updated_ts: updated,
      message_count: msgs.length,
      messages: msgs,
      imported: true,
      imported_at: Date.now(),
      transcript_loaded: true,
      backend_updated_ts: updated,
      strategy_id: t.strategy_id ?? undefined,
    };
    return cacheThreadTranscript(thread);
  }

  async function hydrateBackendSessions(
    localThreads: ChatThread[],
    opts: { offset?: number; append?: boolean } = {},
  ) {
    const offset = Math.max(0, Math.floor(opts.offset ?? 0));
    setSessionLoading(true);
    try {
      const resp = await clientApi.sessionList(undefined, SESSION_PAGE_SIZE, { offset });
      const sessions = Array.isArray(resp?.sessions) ? resp.sessions : [];
      setSessionHasMore(Boolean(resp?.has_more));
      setSessionNextOffset(
        typeof resp?.next_offset === "number"
          ? resp.next_offset
          : offset + sessions.length,
      );
      if (sessions.length === 0) return;
      const localById = new Map(localThreads.map((t) => [t.id, t]));
      const refreshed: ChatThread[] = [];
      for (const s of sessions) {
        const sid = String(s.session_id || "");
        if (!sid) continue;
        if (deletedSessionIdsRef.current.has(sid)) continue;
        const existing = localById.get(sid);
        if (threadHasUnpersistedMessages(existing)) continue;
        // Skip locally-grown threads (i.e. created in this browser) —
        // they may be richer than the journal-reconstructed transcript
        // (e.g. carry live_events, blocks, errors). Only import
        // sessions that are entirely new, or refresh ones we
        // previously imported.
        if (existing && !existing.imported) continue;
        const metaThread = threadFromSessionMetadata(s);
        if (metaThread) refreshed.push(metaThread);
      }
      if (refreshed.length === 0) return;
      setThreads((prev) => {
        const byId = new Map(prev.map((t) => [t.id, t]));
        for (const t of refreshed) {
          const current = byId.get(t.id);
          if (
            current &&
            current.messages.length > 0 &&
            current.updated_ts >= t.updated_ts &&
            (current.transcript_loaded || !t.transcript_loaded)
          ) {
            byId.set(t.id, {
              ...current,
              title: current.title || t.title,
              message_count: Math.max(
                current.message_count ?? 0,
                t.message_count ?? 0,
                current.messages.length,
              ),
              backend_updated_ts: Math.max(
                current.backend_updated_ts ?? 0,
                t.backend_updated_ts ?? 0,
              ),
            });
          } else {
            byId.set(t.id, t);
          }
        }
        const merged = Array.from(byId.values());
        merged.sort((a, b) => b.updated_ts - a.updated_ts);
        return merged;
      });
    } catch {
      // Backend unreachable — local threads still render.
    } finally {
      setSessionLoading(false);
    }
  }

  function loadMoreBackendSessions() {
    if (sessionLoading || !sessionHasMore) return;
    setThreads((prev) => {
      void hydrateBackendSessions(prev, {
        offset: sessionNextOffset,
        append: true,
      });
      return prev;
    });
  }

  useEffect(() => {
    if (!hydrated) return;
    saveThreads(threads);
  }, [threads, hydrated]);

  useEffect(() => {
    if (!hydrated) return;
    saveRunSettings(settings);
  }, [settings, hydrated]);

  useEffect(() => {
    if (!hydrated) return;
    saveCanvasPanelOpen(canvasPanelOpen);
  }, [canvasPanelOpen, hydrated]);

  const active = useMemo(
    () => (sessionId ? threads.find((t) => t.id === sessionId) || null : null),
    [threads, sessionId],
  );
  const activeApprovalIds = useMemo(
    () => pendingApprovalIdsForThread(active, pendingApprovals),
    [active, pendingApprovals],
  );
  const activeApprovalKey = activeApprovalIds.join("|");
  const activeLiveEventCount = useMemo(
    () =>
      active?.messages.reduce(
        (count, msg) =>
          msg.role === "assistant" ? count + (msg.live_events?.length ?? 0) : count,
        0,
      ) ?? 0,
    [active],
  );
  const awaitingApproval = activeApprovalIds.length > 0;
  const activeTranscriptLoading = Boolean(
    sessionId && loadingTranscriptIds.has(sessionId),
  );
  const agentWork = useAgentWork(active);
  const zh = useLocale().startsWith("zh");
  const [resultFocus, setResultFocus] = useState({ session: "", id: "", count: 0 });
  const results = useMemo(() => collectChatResults(active), [active]);
  const { layoutRef, width: canvasWidth, compact, changeWidth, separatorProps } = useCanvasLayout(hydrated);
  const [manualBrowser, setManualBrowser] = useState<Record<string, boolean>>({});
  const browserOperations = useMemo(() => {
    const rows = (active?.messages || []).flatMap(message => {
      if (message.role !== 'assistant') return [];
      const events = [...(message.turn?.activity_events || []), ...(message.live_events || [])];
      return browserCalls([...(message.turn?.blocks || []), ...liveEventsToBlocks(events)], events);
    });
    return [...new Map(rows.map(row => [row.id, row])).values()];
  }, [active]);
  function revealAgent(id: string, attempt?: number) {
    setAgentFocus((old) => ({ session: active?.id || "", id, count: old.count + 1, attempt: attempt || 0 }));
    selectWorkspace("agents", true);
  }
  const [inspectedChildResult, setInspectedChildResult] = useState<{ session: string; result: ChatResult } | null>(null);
  const [agentFocus, setAgentFocus] = useState({ session: "", id: "", count: 0, attempt: 1 });
  const agentResults = useMemo(() => {
    const latest = agentWork.rows.filter((a) => a.state === "completed" && a.output != null).map((a) => childResult(a, a.output, a.attempt, zh));
    const prior = inspectedChildResult && inspectedChildResult.session === active?.id && agentWork.rows.some((a) => a.id === inspectedChildResult.result.agentId) ? [inspectedChildResult.result] : [];
    return [...new Map([...prior, ...latest].map((r) => [r.id, r])).values()].filter((r) => r.text);
  }, [agentWork.rows, active?.id, inspectedChildResult, zh]);
  function openChildResult(result: ChatResult) {
    setInspectedChildResult({ session: active?.id || "", result });
    openResult(result.id);
  }
  const hasBrowser = browserOperations.length > 0 || !!manualBrowser[active?.id || ''];
  const canvasResources = useMemo(() => hasWorkspaceCanvas(active), [active]);
  const hasCanvas = results.length > 0 || agentResults.length > 0 || canvasResources;
  const dockTabs = [
    ...(hasBrowser ? [{ id: 'browser' as TaskDockTab, label: zh ? '浏览器' : 'Browser' }] : []),
    ...(hasCanvas ? [{ id: 'canvas' as TaskDockTab, label: 'Canvas' }] : []),
    ...(agentWork.rows.length ? [{ id: 'agents' as TaskDockTab, label: zh ? '成员' : 'Agents', compactLabel: zh ? '成员' : 'Agents', meta: <span className="text-[11px] tabular-nums">{agentWork.rows.length}</span> }] : []),
  ];
  const taskDock = useTaskDock(active?.id || '', dockTabs.map(tab => tab.id));
  const canvasVisible = taskDock.open;
  const fullCanvas = taskDock.expanded;
  const hideSource = canvasVisible && (compact || fullCanvas);
  const canvasTrigger = useRef<HTMLElement | null>(null);
  const focusDock = (tab: string) => requestAnimationFrame(() => document.getElementById(`task-dock-tab-${tab}`)?.focus({ preventScroll: true }));
  function closeCanvas() {
    taskDock.close();
    requestAnimationFrame(() => {
      const trigger = canvasTrigger.current;
      if (trigger?.isConnected && trigger.getClientRects().length && !trigger.closest('#task-workspace')) trigger.focus({ preventScroll: true });
      else document.getElementById('task-workspace-toggle')?.focus({ preventScroll: true });
    });
  }
  function selectWorkspace(tab: string, focus = false) {
    if (tab === 'conversation') { closeCanvas(); return; }
    if (!dockTabs.some(item => item.id === tab)) return;
    if (!canvasVisible) canvasTrigger.current = document.activeElement instanceof HTMLElement ? document.activeElement : null;
    taskDock.select(tab as TaskDockTab);
    if (focus) focusDock(tab);
  }
  function openDock() { selectWorkspace('canvas', true); }
  function openBrowserDock() {
    canvasTrigger.current = document.activeElement instanceof HTMLElement ? document.activeElement : null;
    setManualBrowser(old => ({ ...old, [active?.id || '']: true }));
    taskDock.select('browser');
    focusDock('browser');
  }
  function toggleCanvasSize() { taskDock.toggleSize(); }
  function openResult(id: string) {
    setResultFocus((old) => ({ session: active?.id || "", id, count: old.count + 1 }));
    canvasTrigger.current = document.activeElement instanceof HTMLElement ? document.activeElement : null;
    taskDock.select('canvas');
    focusDock('canvas');
  }
  function revealResult(id: string) {
    const child = agentResults.find((r) => r.id === id);
    if (child?.agentId) {
      setAgentFocus((old) => ({ session: active?.id || "", id: child.agentId!, count: old.count + 1, attempt: child.attempt || 1 }));
      selectWorkspace("agents", true);
      return;
    }
    followLatest.current = false; setReadingHistory(true);
    selectWorkspace("conversation");
    requestAnimationFrame(() => {
      const article = Array.from(scrollRef.current?.querySelectorAll<HTMLElement>("[data-turn-id]") || []).find((el) => el.dataset.turnId === id);
      article?.scrollIntoView({ block: "start" });
      article?.focus({ preventScroll: true });
    });
  }
  function pendingFirstMessageThreadId(): string {
    try {
      const raw = sessionStorage.getItem("nerya.chat.pendingFirstMessage");
      if (!raw) return "";
      const parsed = JSON.parse(raw) as { threadId?: unknown };
      return typeof parsed.threadId === "string" ? parsed.threadId : "";
    } catch {
      return "";
    }
  }

  // When the URL points at a saved session, treat the backend transcript
  // as authoritative. LocalStorage is only a UI cache and may contain an
  // older partial import from a previous dashboard load.
  useEffect(() => {
    setTranscriptLoadFailed(false);
    if (!hydrated || !sessionId) {
      setMissingSession(false);
      return;
    }
    if (pendingFirstMessageThreadId() === sessionId) {
      setMissingSession(false);
      return;
    }
    if (deletedSessionIdsRef.current.has(sessionId)) {
      setMissingSession(true);
      return;
    }
    const minUpdated = active?.backend_updated_ts || active?.updated_ts || 0;
    if (active?.transcript_loaded && active.messages.length > 0) {
      setMissingSession(false);
      return;
    }
    if (active && active.messages.length > 0 && !active.imported) {
      setMissingSession(false);
      return;
    }
    const cached = loadCachedThreadTranscript(sessionId, minUpdated);
    if (cached) {
      setThreads((prev) => {
        const current = prev.find((t) => t.id === sessionId);
        return upsertThread(prev, mergeAuthoritativeThread(current, cached));
      });
      setMissingSession(false);
      return;
    }
    let cancelled = false;
    setLoadingTranscriptIds((prev) => new Set(prev).add(sessionId));
    (async () => {
      try {
        const built = await buildImportedThread(
          sessionId,
          {
            created_at: isoFromMs(active?.created_ts),
            updated_at: isoFromMs(active?.updated_ts),
            title: active?.title,
          },
          { full: true },
        );
        if (cancelled) return;
        if (built) {
          setThreads((prev) => {
            const current = prev.find((t) => t.id === sessionId);
            return upsertThread(prev, mergeAuthoritativeThread(current, built));
          });
          setMissingSession(false);
        } else {
          setMissingSession(true);
        }
      } catch {
        // Transport/library error — the session may well exist. Surface a
        // retryable failure instead of claiming it is missing.
        if (!cancelled) setTranscriptLoadFailed(true);
      } finally {
        if (!cancelled) {
          setLoadingTranscriptIds((prev) => {
            const next = new Set(prev);
            next.delete(sessionId);
            return next;
          });
        }
      }
    })();
    return () => {
      cancelled = true;
      setLoadingTranscriptIds((prev) => {
        const next = new Set(prev);
        next.delete(sessionId);
        return next;
      });
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [hydrated, sessionId, transcriptRetry]);

  // Pick up a first message that was staged on `/chat` before the route
  // switched to `/chat/[id]`. Running it here keeps the turn alive across
  // the unmount/remount caused by navigation.
  useEffect(() => {
    if (!hydrated || !sessionId) return;
    let pending: PendingFirstMessage | null =
      pendingFirstMessages.get(sessionId) ?? null;
    if (pending) {
      pendingFirstMessages.delete(sessionId);
    }
    try {
      const raw = sessionStorage.getItem("nerya.chat.pendingFirstMessage");
      if (!pending && raw) pending = JSON.parse(raw);
    } catch {
      if (!pending) pending = null;
    }
    if (!pending || pending.threadId !== sessionId) return;
    sessionStorage.removeItem("nerya.chat.pendingFirstMessage");
    void runAgentTurn(pending.text, {
      visibleUser: true,
      attachments: pending.attachments ?? [],
    });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [hydrated, sessionId]);

  useEffect(() => {
    followLatest.current = true; lastScrollTop.current = 0; setReadingHistory(false);
  }, [active?.id]);

  useEffect(() => {
    const viewport = scrollRef.current, content = transcriptRef.current;
    if (!viewport || !content) return;
    // Streaming and late-loading content follow only while the reader is at the end.
    const follow = () => {
      if (followLatest.current && viewport.clientHeight) {
        viewport.scrollTo({ top: viewport.scrollHeight });
        lastScrollTop.current = viewport.scrollTop;
      } else if (viewport.clientHeight) {
        setReadingHistory(viewport.scrollHeight - viewport.scrollTop - viewport.clientHeight > 80);
      }
    };
    follow();
    const observer = new ResizeObserver(follow);
    observer.observe(content); observer.observe(viewport);
    return () => observer.disconnect();
  }, [active?.id, active?.messages.length, hydrated]);

  useEffect(() => {
    const el = scrollRef.current;
    if (el && el.clientHeight && followLatest.current) {
      el.scrollTo({ top: el.scrollHeight }); lastScrollTop.current = el.scrollTop;
    }
  }, [active?.messages.length, activeLiveEventCount, activeApprovalKey]);

  /** Strategy binding requested via ``/chat?strategy=<id>`` (the "+"
   * button on a sidebar strategy). Read lazily from the location so we
   * don't need a Suspense boundary for ``useSearchParams``. */
  function strategyIdFromLocation(): string {
    if (typeof window === "undefined") return "";
    try {
      return new URLSearchParams(window.location.search).get("strategy") || "";
    } catch {
      return "";
    }
  }

  function createThread(seedText?: string, id?: string): ChatThread {
    const minted = id ? { ...newThread(seedText), id } : newThread(seedText);
    const strategyId = strategyIdFromLocation();
    const t = strategyId ? { ...minted, strategy_id: strategyId } : minted;
    setThreads((prev) => upsertThread(prev, t));
    return t;
  }

  function updateThread(id: string, fn: (t: ChatThread) => ChatThread) {
    setThreads((prev) =>
      prev.map((t) => (t.id === id ? { ...fn(t), updated_ts: Date.now() } : t))
    );
  }

  function deleteThread(id: string) {
    deletedSessionIdsRef.current = rememberDeletedSession(id);
    setThreads((prev) => {
      const next = prev.filter((t) => t.id !== id);
      if (sessionId === id) {
        setEditingMessageId(null);
        setEditDraft("");
        const fallback = next[0]?.id;
        if (fallback) router.replace(`/chat/${fallback}`);
        else router.replace("/chat");
      }
      return next;
    });
    void clientApi.sessionDelete(id).catch(() => {
      // Keep the local tombstone so a transient backend failure does not
      // immediately re-import the session the operator just deleted.
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
    const okDelete = await confirmDialog({
      title: t("deleteMessage"),
      message: t("confirmDeleteMessage"),
      tone: "danger",
    });
    if (!okDelete) return;
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
    const approvalKind = String(
      pendingApprovals.get(approvalId)?.record?.kind || "",
    ).toLowerCase();
    const isAutoResumedFinancialApproval = [
      "trade_intent",
      "wallet_swap",
    ].includes(approvalKind);
    setResolvingApprovalIds((prev) => new Set(prev).add(approvalId));
    try {
      const res = await clientApi.approvalCallback({
        callback_data: callbackData,
        actor_id: "dashboard",
      });
      const state = String(res?.state || "").toLowerCase();
      const resolvedApprovalKind = String(
        res?.approval_kind || approvalKind,
      ).toLowerCase();
      const resolvedAutoResumedFinancialApproval =
        isAutoResumedFinancialApproval ||
        ["trade_intent", "wallet_swap"].includes(resolvedApprovalKind);
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
          resolvedAutoResumedFinancialApproval
            ? `Financial approval ${approvalId} was approved. The frozen financial action is resumed automatically by the approval handler. Do not submit or retry the order, trade intent, or wallet transaction. Query the existing approval, order, or transaction status, report the resulting state, and continue the original task from that result.`
            : "The requested permission was approved. Continue from the pending tool call, retry the approved action, and proceed with the original task.",
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
      attachments?: ChatAttachment[];
      source?: string;
      kind?: string;
      channel?: string;
      payloadExtra?: Record<string, unknown>;
      /** Retry replay: the failed assistant bubble to replace. */
      replaceFailedId?: string;
    } = {},
  ) {
    const clean = text.trim();
    const outgoingAttachments = options.attachments ?? [];
    const requestAttachments = outgoingAttachments.map(attachmentForRequest);
    if (!clean && !outgoingAttachments.length) return;
    const approvalContinue =
      options.source === "approval_continue" ||
      options.kind === "approval.continue";
    if (awaitingApproval && !approvalContinue) return;
    if (turnInFlightRef.current) return;
    const visibleUser = options.visibleUser !== false;

    let thread = active;
    if (!thread) {
      thread = createThread(
        clean || outgoingAttachments[0]?.name || "Attached file",
        sessionId,
      );
      setMissingSession(false);
    }
    const threadId = thread.id;

    // When we entered at `/chat` with no sessionId, we've just minted a
    // thread id — move the browser to `/chat/[id]` via the sibling page.
    // We stash the pending message in sessionStorage so the remounted
    // ChatView continues the turn, because Next App Router unmounts this
    // component on route change.
    if (!sessionId && visibleUser) {
      pendingFirstMessages.set(threadId, {
        threadId,
        text: clean,
        attachments: outgoingAttachments,
      });
      let staged = false;
      try {
        sessionStorage.setItem(
          "nerya.chat.pendingFirstMessage",
          JSON.stringify({
            threadId,
            text: clean,
            attachments: requestAttachments,
          }),
        );
        staged = true;
      } catch {
        // sessionStorage may be disabled; fall back to in-place send.
      }
      if (staged) {
        turnInFlightRef.current = true;
        setSending(true);
        // Persist the freshly-created thread now so the next mount sees it.
        saveThreads(upsertThread(loadThreads(), thread));
        router.replace(`/chat/${threadId}`);
        return;
      }
    }
    turnInFlightRef.current = true;

    const userMsgId = uuid();
    const assistantId = uuid();
    const startedMs = Date.now();
    const userMessage = {
      id: userMsgId,
      role: "user" as const,
      ts: Date.now(),
      text: clean,
      attachments: outgoingAttachments,
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
      imported: false,
      title:
        t.messages.length === 0 && visibleUser
          ? deriveTitle(clean || outgoingAttachments[0]?.name || "Attached file")
          : t.title,
      messages: visibleUser
        ? [...t.messages, userMessage, assistantMessage]
        : [
            // Retry replay: drop the failed assistant bubble in the same
            // commit that appends the new loading one. The error card is
            // replaced (never duplicated) and only after the replay has
            // been admitted past the in-flight / approval guards above —
            // if a guard bails, the failed turn stays on screen.
            ...t.messages.filter((m) => m.id !== options.replaceFailedId),
            assistantMessage,
          ],
    }));

    if (visibleUser) {
      setInput("");
      setAttachments([]);
    }
    setSending(true);

    const ctrl = new AbortController();
    abortRef.current = ctrl;
    inFlightSessionRef.current = threadId;

    // Anchor the live-events cursor *before* the turn fires. We use
    // the bus's current ``latest_seq`` so we ignore everything that
    // happened in earlier turns (otherwise the freshest message
    // would inherit a stale "approval requested" event from
    // five turns ago and look perpetually pending).
    let cursor = 0;
    try {
      const head = await clientApi.streamEvents(undefined, {
        limit: 1,
        session_id: threadId,
      });
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

    let pollInFlight: Promise<void> | null = null;

    async function pollOnce(): Promise<void> {
      if (handle.stop) return;
      if (pollInFlight) {
        await pollInFlight;
        return;
      }
      pollInFlight = (async () => {
        try {
          const resp = await clientApi.streamEvents(cursor, {
            limit: 500,
            session_id: threadId,
          });
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
      })();
      try {
        await pollInFlight;
      } finally {
        pollInFlight = null;
      }
    }

    handle.timer = setInterval(() => {
      pollOnce();
    }, LIVE_EVENT_POLL_MS);
    // Fire one poll immediately so the UI doesn't sit blank while the
    // agent is already churning.
    pollOnce();

    function stopPoller(): void {
      handle.stop = true;
      if (handle.timer) {
        clearInterval(handle.timer);
        handle.timer = null;
      }
    }

    try {
      // Dashboard is a trusted server-side lane. Public AgentOn traffic must
      // use /agent/run_turn so the public research-only intent gate remains
      // enforced; dashboard chat uses the separately authenticated internal
      // route and still gets Nerya's own prompt/risk/approval safeguards.
      const res = await callApi<TurnPayload>("/agent/run_turn_internal", {
        method: "POST",
        signal: ctrl.signal,
        body: {
          source: options.source || "user_chat",
          kind: options.kind || "user.chat",
          target: "main",
          payload: {
            text: clean || "Please review the attached file(s).",
            channel: options.channel || "dashboard",
            attachments: requestAttachments,
            ...(options.payloadExtra ?? {}),
          },
          // Reuse the thread id as the session id so every turn in
          // this chat lands in the same on-disk SessionState. This is
          // what lets the kernel reload prior chat history (context
          // persistence) and what lets curl / gateway turns show up
          // here (any caller that supplies the same session id is
          // grouped under the same thread).
          session_id: threadId,
          // Strategy sub-sessions forward their binding so the backend
          // session locks to the strategy and the kernel injects the
          // full strategy file context into the system prompt.
          ...(thread.strategy_id
            ? { strategy_id: thread.strategy_id }
            : {}),
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
          model_context_window: settings.model_context_window,
          max_iterations: settings.max_iterations,
          max_total_tool_calls: settings.max_total_tool_calls,
          max_wall_seconds: settings.max_wall_seconds,
          evidence_contract: settings.evidence_contract,
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
        imported: false,
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
      try {
        const fullThread = await buildImportedThread(
          threadId,
          { title: backendTitle },
          { full: true },
        );
        if (fullThread) {
          setThreads((prev) =>
            upsertThread(
              prev,
              mergeAuthoritativeThread(
                prev.find((t) => t.id === threadId),
                fullThread,
              ),
            ),
          );
        }
      } catch {
        // The live turn is already visible; a later route refresh will
        // retry the authoritative transcript hydrate.
      }
    } catch (e) {
      // Drain whatever the bus already produced so the failure card
      // shows the events that *did* fire before the crash.
      try {
        await pollOnce();
      } catch {
        // ignore
      }
      stopPoller();
      updateThread(threadId, (t) => ({
        ...t,
        imported: false,
        messages: t.messages.map((m) =>
          m.id === assistantId && m.role === "assistant"
            ? ctrl.signal.aborted
              ? {
                  ...m,
                  loading: false,
                  error: undefined,
                  turn: {
                    ...(m.turn ?? {}),
                    reply_text: cancelledReply,
                    final_text: cancelledReply,
                    stopped_reason: "cancelled",
                    transition_reason: "operator_cancel",
                  },
                  elapsed_ms: Date.now() - startedMs,
                }
              : {
                  ...m,
                  loading: false,
                  error: e instanceof Error ? e.message : String(e),
                  elapsed_ms: Date.now() - startedMs,
                }
            : m
        ),
      }));
    } finally {
      stopPoller();
      setSending(false);
      turnInFlightRef.current = false;
      abortRef.current = null;
      inFlightSessionRef.current = null;
    }
  }

  // Review-B P1: a failed turn used to be a dead end. The user message the
  // turn answered is still in the thread, so rebuild the original request
  // from it and re-run the turn with ``visibleUser: false`` — no duplicate
  // user bubble is appended and the failed assistant bubble is replaced.
  // Invisible turns (approval_continue) never stored their synthetic
  // prompt, so their error cards don't offer Retry (ChatView only wires
  // ``onRetry`` when the previous message is a user message).
  async function retryFailedTurn(assistantMsgId: string) {
    const thread = active;
    if (!thread) return;
    const idx = thread.messages.findIndex((m) => m.id === assistantMsgId);
    if (idx < 0) return;
    const failed = thread.messages[idx];
    if (failed.role !== "assistant" || failed.loading || !failed.error) return;
    const prev = thread.messages[idx - 1];
    if (!prev || prev.role !== "user") return;
    const text = prev.text.trim();
    const attachments = prev.attachments ?? [];
    if (!text && !attachments.length) return;
    await runAgentTurn(text, {
      visibleUser: false,
      attachments,
      replaceFailedId: assistantMsgId,
    });
  }

  async function send(text: string) {
    await runAgentTurn(text, { visibleUser: true, attachments });
  }

  function cancel() {
    const activeSessionId = inFlightSessionRef.current || sessionId || "";
    if (activeSessionId) {
      void callApi("/agent/interrupt", {
        method: "POST",
        body: { session_id: activeSessionId, reason: "operator_cancel" },
      }).catch(() => undefined);
      updateThread(activeSessionId, (t) => {
        const messages = [...t.messages];
        const idx = messages.findLastIndex((m) => m.role === "assistant" && m.loading);
        if (idx >= 0) {
          const m = messages[idx] as Extract<ChatMessage, { role: "assistant" }>;
          messages[idx] = {
            ...m,
            loading: false,
            error: undefined,
            turn: {
              ...(m.turn ?? {}),
              reply_text: cancelledReply,
              final_text: cancelledReply,
              stopped_reason: "cancelled",
              transition_reason: "operator_cancel",
            },
            elapsed_ms:
              typeof m.started_ms === "number" ? Date.now() - m.started_ms : undefined,
          };
        }
        return { ...t, imported: false, messages };
      });
    }
    if (abortRef.current) abortRef.current.abort();
    // Also stop the live-event poller so we don't keep hitting the
    // bus after the user explicitly aborted.
    const handle = livePollRef.current;
    handle.stop = true;
    if (handle.timer) {
      clearInterval(handle.timer);
      handle.timer = null;
    }
    setSending(false);
    turnInFlightRef.current = false;
    abortRef.current = null;
    inFlightSessionRef.current = null;
  }

  if (!hydrated) {
    return (
      <div className="flex h-full min-h-0 flex-col">
        <ChatTaskHeader thread={null} agents={[]} results={[]} sending={false} approvalCount={0} loading showTabs={false}
          tab="conversation" canvasVisible={false} onSelect={selectWorkspace} onToggleCanvas={openDock} onOpenResult={openResult} />
        <div className="flex min-h-0 flex-1 items-center justify-center text-sm text-ink-500">{t("loadingChat")}</div>
      </div>
    );
  }

  const conversationEmpty = !active || active.messages.length === 0;
  const showMissing = Boolean(sessionId && missingSession && conversationEmpty);
  const showLoadFailure = Boolean(
    sessionId && transcriptLoadFailed && conversationEmpty,
  );
  // A URL that addresses a session is still being resolved until we either
  // load its transcript or confirm it's missing. Treat that window as
  // "loading" so we never flash the new-chat hero for a session the user
  // deliberately opened (cold cache / slow transcript fetch). The pending
  // first-message case is the one exception: that thread is genuinely a
  // brand-new local chat the composer is about to populate.
  const resolvingAddressedSession =
    Boolean(sessionId) &&
    conversationEmpty &&
    !showMissing &&
    !showLoadFailure &&
    pendingFirstMessageThreadId() !== sessionId;
  const showLoading =
    conversationEmpty &&
    !showMissing &&
    !showLoadFailure &&
    (activeTranscriptLoading || resolvingAddressedSession);
  // Codex-style new-chat surface: a centred composer in the middle of
  // the canvas instead of a docked input + suggestion page. Only the
  // home route (no sessionId) or a fresh pending-first-message thread
  // reaches it now.
  const showHero =
    conversationEmpty && !showMissing && !showLoadFailure && !showLoading;

  const composerProps = {
    value: input,
    onChange: setInput,
    onSend: () => { followLatest.current = true; setReadingHistory(false); send(input); },
    onCancel: cancel,
    sending,
    locked: awaitingApproval,
    lockMessage: t("approvalPaused"),
    settings,
    onSettingsChange: setSettings,
    modelOptions,
    attachments,
    onAttachmentsChange: setAttachments,
  };

  return (
    <FinanceDraftContext.Provider value={{ disabled: sending || awaitingApproval, append: (text) => {
      setInput((previous) => appendReviewDraft(previous, text));
      selectWorkspace("conversation");
      requestAnimationFrame(() => document.querySelector<HTMLTextAreaElement>('#chat-workspace-panel-conversation [data-chat-composer] textarea')?.focus());
      toast({ tone: "ok", message: zh ? "已填入当前对话草稿，确认后再发送。" : "Added to this conversation’s draft. Review before sending." });
    } }}>
    <div className={`${styles.workbench} flex h-full min-h-0 min-w-0 flex-col`} data-testid="chat-workbench">
      <ChatTaskHeader thread={active} agents={agentWork.rows} results={[...agentResults, ...results]} sending={sending}
        approvalCount={activeApprovalIds.length} loading={showLoading} showTabs={!showHero} tab={taskDock.selected || ''}
        workspaceAvailable={dockTabs.length > 0} canvasVisible={canvasVisible} onSelect={(tab) => selectWorkspace(tab, true)}
        onToggleBrowser={openBrowserDock}
        onToggleCanvas={() => { if (canvasVisible) closeCanvas(); else { taskDock.show(); focusDock(taskDock.selected || ''); } }} onOpenResult={openResult} />
      {!showHero && awaitingApproval && hideSource ? <div role="status" className="flex shrink-0 items-center justify-between gap-3 border-b border-warn/25 bg-warn/10 px-4 py-2 text-xs text-warn">
        <span>{t("approvalPausedCount", { count: activeApprovalIds.length })}</span>
        <button type="button" onClick={() => selectWorkspace("conversation", true)} className="min-h-8 rounded px-2 underline">{zh ? "查看待审批操作" : "Review pending approvals"}</button>
      </div> : null}
      <div ref={layoutRef} className="flex min-h-0 min-w-0 flex-1" data-testid="workspace-split" data-compact={compact ? "true" : "false"}>
      <div hidden={!showHero && hideSource} className={showHero || !hideSource ? "flex min-h-0 min-w-0 flex-1 flex-col" : "hidden"} data-testid="workspace-source">
      <section id="chat-workspace-panel-conversation" aria-label={zh ? '对话' : 'Conversation'} className="flex min-h-0 flex-1 flex-col min-w-0">
        {showHero ? (
          <AgentStart value={input} onChange={setInput} disabled={sending || awaitingApproval}
            composer={<ChatInput {...composerProps} variant="hero" />} />
        ) : (
          <>
            <div className={styles.transcriptViewport}>
            <div ref={scrollRef} className="min-h-0 flex-1 overflow-y-auto" data-testid="transcript-scroll"
              onWheel={(e) => { if (e.deltaY < 0) followLatest.current = false; }}
              onPointerDownCapture={(e) => { if (e.target instanceof Element && e.target.closest("summary")) followLatest.current = false; }}
              onKeyDownCapture={(e) => { if (["PageUp", "Home", "ArrowUp"].includes(e.key) || (["Enter", " "].includes(e.key) && e.target instanceof Element && e.target.closest("summary"))) followLatest.current = false; }}
              onScroll={(e) => {
                const el = e.currentTarget;
                if (!el.clientHeight) return;
                const near = el.scrollHeight - el.scrollTop - el.clientHeight < 80;
                if (near) followLatest.current = true;
                else if (el.scrollTop < lastScrollTop.current - 1) followLatest.current = false;
                lastScrollTop.current = el.scrollTop;
                setReadingHistory(!followLatest.current && !near);
              }}>
              {showLoadFailure ? (
                <div className="max-w-xl mx-auto px-6 py-16 text-center">
                  <h2 className="text-lg text-white font-semibold mb-2">
                    {t("transcriptLoadFailed")}
                  </h2>
                  <div className="flex items-center justify-center gap-3">
                    <button
                      onClick={() => {
                        setTranscriptLoadFailed(false);
                        setTranscriptRetry((c) => c + 1);
                      }}
                      className="btn-primary px-4 py-2 text-sm"
                    >
                      {tCommon("retry")}
                    </button>
                    <button
                      onClick={() => router.push("/chat")}
                      className="glass hover:bg-white/[0.05] hover:border-brand-500/30 px-4 py-2 text-sm text-white transition-colors"
                    >
                      {t("startNewChat")}
                    </button>
                  </div>
                </div>
              ) : showMissing ? (
                <div className="max-w-xl mx-auto px-6 py-16 text-center">
                  <h2 className="text-lg text-white font-semibold mb-2">
                    {t("sessionNotFound")}
                  </h2>
                  <p className="text-sm text-ink-400 mb-6">
                    {t.rich("sessionNotFoundBody", {
                      sessionId,
                      code: (chunks) => (
                        <span className="font-mono">{chunks}</span>
                      ),
                    })}
                  </p>
                  <button
                    onClick={() => router.push("/chat")}
                    className="glass hover:bg-white/[0.05] hover:border-brand-500/30 px-4 py-2 text-sm text-white transition-colors"
                  >
                    {t("startNewChat")}
                  </button>
                </div>
              ) : showLoading ? (
                <div className="h-full flex items-center justify-center text-ink-500 text-sm">
                  {t("loadingConversation")}
                </div>
              ) : (
                <div ref={transcriptRef} className={styles.conversation} data-testid="conversation-content">
                  {active!.messages.map((m, mi) =>
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
                        conversationId={active!.id}
                        onOpenBrowser={openBrowserDock}
                        msg={m}
                        onOpenResult={() => openResult(m.id)}
                        pendingApprovals={pendingApprovals}
                        onApprovalAction={resolveApproval}
                        resolvingApprovalIds={resolvingApprovalIds}
                        onRetry={
                          m.error && !m.loading && active!.messages[mi - 1]?.role === "user"
                            ? () => void retryFailedTurn(m.id)
                            : undefined
                        }
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
            {readingHistory ? <button type="button" className={styles.jump} data-testid="jump-to-latest" onClick={jumpToLatest}>{zh ? "回到最新消息" : "Jump to latest"}<span aria-hidden>↓</span></button> : null}
            </div>
            {awaitingApproval ? (
              <div
                className="border-t border-warn/25 bg-warn/[0.06] px-4 py-2 text-xs text-warn"
                role="status"
              >
                <div className="max-w-[860px] mx-auto flex items-center justify-between gap-3">
                  <span>{t("approvalPausedCount", { count: activeApprovalIds.length })}</span>
                  <span className="font-mono text-[10px] text-warn/80">
                    {activeApprovalIds[0]?.slice(0, 18)}
                  </span>
                </div>
              </div>
            ) : null}
            <ChatInput {...composerProps} variant="docked" taskHeader={agentWork.rows.length ? (
              <AgentTaskBar source={agentWork} open={canvasVisible && taskDock.selected === 'agents'} onOpen={() => selectWorkspace("agents", true)} />
            ) : undefined} />
          </>
        )}
      </section>
      </div>
      {!showHero && canvasVisible && !compact && !fullCanvas ? <div role="separator" tabIndex={0} aria-label={zh ? "调整工作区宽度" : "Resize workspace"} aria-orientation="vertical"
        aria-valuemin={38} aria-valuemax={62} aria-valuenow={Math.round(canvasWidth)} aria-controls="task-workspace"
        {...separatorProps} onDoubleClick={() => changeWidth(48)} onKeyDown={(event) => {
          if (!["ArrowLeft", "ArrowRight", "Home", "End"].includes(event.key)) return;
          event.preventDefault(); changeWidth(event.key === "Home" ? 38 : event.key === "End" ? 62 : canvasWidth + (event.key === "ArrowLeft" ? 2 : -2));
        }} className="w-1 shrink-0 touch-none cursor-col-resize bg-[color:var(--line)] hover:bg-brand-400/50 focus-visible:bg-brand-400 focus-visible:outline-none" /> : null}
      {!showHero && dockTabs.length > 0 ? <section id="task-workspace" role="complementary" aria-label={zh ? '任务工作区' : 'Task workspace'}
        hidden={!canvasVisible} className={canvasVisible ? "flex min-h-0 min-w-0 flex-col bg-[color:var(--card)]" : "hidden"}
        style={canvasVisible ? { width: fullCanvas || compact ? "100%" : `${canvasWidth}%`, flexShrink: 0 } : undefined}
        onKeyDown={(event) => {
          if (event.key === "Escape" && !event.defaultPrevented && !(event.target instanceof HTMLElement && event.target.closest("select, textarea, input, [role='dialog'], [role='menu']"))) {
            event.preventDefault(); closeCanvas();
          }
        }}>
        <TaskDockHeader tabs={dockTabs} selected={taskDock.selected || ''} onSelect={tab => selectWorkspace(tab, true)} expanded={fullCanvas} onToggleSize={toggleCanvasSize} onClose={closeCanvas}/>
        {hasBrowser && <section id="task-dock-panel-browser" role="tabpanel" aria-labelledby="task-dock-tab-browser" hidden={taskDock.selected !== 'browser'} className={taskDock.selected === 'browser' ? 'min-h-0 flex-1' : 'hidden'}>
          <BrowserWorkspacePanel key={active?.id} conversationId={active?.id || ''} calls={browserOperations} active={canvasVisible && taskDock.selected === 'browser'} />
        </section>}
        {hasCanvas && <section id="task-dock-panel-canvas" role="tabpanel" aria-labelledby="task-dock-tab-canvas" hidden={taskDock.selected !== 'canvas'} className={taskDock.selected === 'canvas' ? 'min-h-0 flex-1' : 'hidden'}>
          <WorkspaceCanvas key={active?.id} thread={active} embedded open={canvasVisible && taskDock.selected === 'canvas'} onToggle={closeCanvas} agentWork={agentWork} agentResults={agentResults}
            resultFocusRequest={resultFocus.session === active?.id ? resultFocus : undefined} onRevealAgent={revealAgent} onRevealResult={revealResult} />
        </section>}
        {agentWork.rows.length > 0 && <section id="task-dock-panel-agents" role="tabpanel" aria-labelledby="task-dock-tab-agents" hidden={taskDock.selected !== 'agents'} className={taskDock.selected === 'agents' ? 'min-h-0 flex-1' : 'hidden'}>
          <AgentWorkPanel key={active?.id} source={agentWork} active={canvasVisible && taskDock.selected === 'agents'} onOpenResult={openChildResult}
            focusRequest={agentFocus.session === active?.id ? agentFocus : undefined}/>
        </section>}
      </section> : null}
      </div>
    </div>
    </FinanceDraftContext.Provider>
  );
}
