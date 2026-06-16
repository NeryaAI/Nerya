// Chat thread model + localStorage persistence.
//
// Threads are stored entirely client-side — the backend `/agent/run_turn`
// endpoint is stateless (every call builds its own context from the workspace
// journals), so we just keep the rendered transcript locally for now.

export type Role = "user" | "assistant";

export type ToolTraceEntry = {
  skill_id?: string;
  action?: string;
  caller?: string;
  ok?: boolean;
  error?: string | null;
  error_kind?: string | null;
  elapsed_ms?: number | null;
  attempts?: number;
  result?: unknown;
  payload?: Record<string, unknown>;
  budget_snapshot?: Record<string, unknown>;
};

export type ActionRecord = {
  action: string;
  result?: unknown;
  error?: string;
  error_kind?: string;
};

export type GatewayEvent = {
  phase?: string;
  status?: string;
  text?: string;
  wall_ms?: number | null;
  detail?: Record<string, unknown>;
};

/** provider-native block envelope from
 * :class:`WorkspaceNativeAgentLoop`. Each envelope wraps one of:
 *   - ``text``           — model's natural-language reply chunk
 *   - ``thinking``       — extended-reasoning trace (only when the
 *                          provider exposes it)
 *   - ``tool_use``       — model decided to call a tool
 *   - ``tool_result``    — orchestrator response for a previous
 *                          ``tool_use``
 *
 * The shape is intentionally permissive — different providers carry
 * slightly different fields and we do not want the dashboard to break
 * when the kernel adds richer metadata.
 */
export type NativeBlockKind =
  | "text"
  | "thinking"
  | "tool_use"
  | "tool_result"
  | "chart"
  | "attachment"
  | string;

export type NativeBlock = {
  kind?: NativeBlockKind;
  index?: number;
  iteration?: number;
  ts?: string;
  // text / thinking
  text?: string;
  // tool_use
  call_id?: string;
  action?: string;
  skill_id?: string;
  payload?: Record<string, unknown>;
  // tool_result
  ok?: boolean;
  error?: string | null;
  error_kind?: string | null;
  elapsed_ms?: number | null;
  result?: unknown;
  // catch-all
  [key: string]: unknown;
};

export type ChatAttachment = {
  id: string;
  name: string;
  mime_type: string;
  size: number;
  kind?: "image" | "document" | "file" | string;
  data_url?: string;
  url?: string;
  text?: string;
  artifact_uri?: string;
  model_sent?: boolean;
  reason?: string;
};

export type NativeBlockEnvelope = {
  block?: NativeBlock;
  // Some providers/serialisers flatten the payload into the envelope
  // root; treat those as the block itself.
  kind?: NativeBlockKind;
  ts?: string;
  index?: number;
  iteration?: number;
  [key: string]: unknown;
};

export type TurnPayload = {
  trigger_event_id?: string | null;
  plan?: { kind?: string; tier?: string };
  decision?: Record<string, unknown> & {
    reasoning?: string;
    action?: string;
    actions?: ActionRecord[];
  };
  actions?: ActionRecord[];
  subagents?: Record<string, unknown>;
  tool_trace?: ToolTraceEntry[];
  budget?: Record<string, unknown>;
  reply_text?: string;
  final_text?: string;
  events?: GatewayEvent[];
  activity_events?: LiveEvent[];
  turn_id?: string;
  stopped_reason?: string | null;
  transition_reason?: string | null;
  /** block envelopes emitted by the workspace-native agent
   * loop. Empty under the legacy JSON-decision harness; populated when
   * the backend feature flag ``agent.harness.native_loop`` is on.
   * The dashboard prefers ``blocks`` over ``actions``/``tool_trace``
   * when this array is non-empty. */
  blocks?: NativeBlockEnvelope[];
  attachments?: ChatAttachment[];
  artifact_index?: Record<string, unknown>;
  final_report?: Record<string, unknown>;
  /** Identifies which agent harness produced this turn. Useful for
   * debug overlays (legacy vs native). */
  harness?: "legacy" | "native" | string;
};

export type UserMessage = {
  id: string;
  role: "user";
  ts: number;
  text: string;
  attachments?: ChatAttachment[];
  backend_message_id?: string;
};

/** One frame of the live agent run, sourced from
 * ``GET /agent/stream/events?after_seq=N`` (see
 * ``nerya/agent/streaming.py``).
 *
 * The bus emits dozens of event ``kind``s — we surface the ones that
 * matter for human observability in chat (start/progress/complete tool
 * calls, journalled turn steps, approval requests). Everything else is
 * ignored at render-time but still pushed to ``live_events`` so the
 * advanced "raw events" panel can show the full trace.
 */
export type LiveEvent = {
  kind: string;
  seq: number;
  event_id?: string;
  ts?: number;
  // Frequently populated payload fields. Treat all as optional so the
  // schema can grow without breaking renders.
  skill_id?: string;
  action?: string;
  caller?: string;
  ok?: boolean;
  error?: string | null;
  error_kind?: string | null;
  elapsed_ms?: number | null;
  message?: string;
  text?: string;
  step?: string;
  status?: string;
  detail?: Record<string, unknown>;
  payload?: Record<string, unknown>;
  reasoning_effort?: string;
  reasoning_tokens?: number;
  provider?: string;
  model?: string;
  approval_id?: string;
  session_id?: string;
  strategy_id?: string;
  // Catch-all so renderers can dig deeper without TS blocking us.
  [key: string]: unknown;
};

export type AssistantMessage = {
  id: string;
  role: "assistant";
  ts: number;
  backend_message_id?: string;
  loading?: boolean;
  error?: string;
  turn?: TurnPayload;
  started_ms?: number;
  elapsed_ms?: number;
  /** In-flight + post-turn streaming events from
   * ``/agent/stream/events``. Captured while ``loading`` is ``true`` so
   * the UI can render a live activity timeline; retained after the
   * turn returns so users can audit what happened step-by-step.
   */
  live_events?: LiveEvent[];
  /** Highest seq we've already ingested for ``live_events``. Used as
   * the ``after_seq`` cursor for the next poll, so we never duplicate
   * frames and never miss frames that arrived between polls.
   */
  live_cursor?: number;
};

export type ChatMessage = UserMessage | AssistantMessage;

export type ReasoningEffort =
  | "off"
  | "minimal"
  | "low"
  | "medium"
  | "high"
  | "xhigh";

export type PermissionMode = "default" | "yolo";
export type ModelContextWindow = 131072 | 262144 | 1048576;

export type ChatModelOverride = {
  reasoning_effort?: ReasoningEffort;
  model_context_window?: ModelContextWindow;
};

export type ChatRunSettings = {
  reasoning_effort: ReasoningEffort;
  permission_mode: PermissionMode;
  model_context_window: ModelContextWindow;
  model_tier: string;
  model_provider: string;
  model_id: string;
  model_overrides?: Record<string, ChatModelOverride>;
  max_iterations: number;
  max_total_tool_calls: number;
  max_wall_seconds: number;
  evidence_contract?: Record<string, unknown>;
};

export type ChatModelOption = {
  key: string;
  label: string;
  tier?: string;
  provider: string;
  model: string;
  source: "tier" | "catalog";
  reasoning_effort?: ReasoningEffort;
};

type LlmRouteLike = {
  provider?: string | null;
  model?: string | string[] | null;
  models?: string[] | null;
};

type LlmTierLike = LlmRouteLike & {
  tier?: string | null;
  reasoning_effort?: string | null;
  routes?: LlmRouteLike[] | null;
};

type LlmTiersLike = {
  tiers?: LlmTierLike[] | null;
};

type LlmConfigLike = {
  tiers?: LlmTierLike[] | null;
};

type LlmModelsLike = {
  providers?: Record<string, Array<Record<string, unknown>>> | null;
};

function splitModelValues(value: unknown): string[] {
  if (!value) return [];
  const raw = Array.isArray(value) ? value : String(value).split(/[\n,]/);
  const seen = new Set<string>();
  const out: string[] = [];
  for (const item of raw) {
    const text = String(item || "").trim();
    if (!text || seen.has(text)) continue;
    seen.add(text);
    out.push(text);
  }
  return out;
}

function modelIdFromCatalogRow(row: Record<string, unknown>): string {
  return String(row.id || row.model || row.name || row.model_id || "").trim();
}

function normaliseReasoningEffort(value: unknown): ReasoningEffort | undefined {
  const raw = String(value || "").trim().toLowerCase();
  if (!raw) return undefined;
  if (raw === "none") return "off";
  if (raw === "extra_high" || raw === "extra-high" || raw === "max") return "xhigh";
  if (["off", "minimal", "low", "medium", "high", "xhigh"].includes(raw)) {
    return raw as ReasoningEffort;
  }
  return undefined;
}

export function buildChatModelOptions({
  tiers,
  config,
  models,
}: {
  tiers?: LlmTiersLike | null;
  config?: LlmConfigLike | null;
  models?: LlmModelsLike | null;
}): ChatModelOption[] {
  const options: ChatModelOption[] = [];
  const seenExact = new Set<string>();
  const seenProviderModel = new Set<string>();

  function addTierOption(
    tierRaw: unknown,
    providerRaw: unknown,
    modelRaw: unknown,
    reasoningRaw?: unknown,
  ) {
    const tier = String(tierRaw || "").trim();
    const provider = String(providerRaw || "").trim().toLowerCase();
    const reasoning = normaliseReasoningEffort(reasoningRaw);
    if (!tier || !provider) return;
    for (const model of splitModelValues(modelRaw)) {
      const exact = `${provider}:${model}:${tier}`;
      if (seenExact.has(exact)) continue;
      seenExact.add(exact);
      seenProviderModel.add(`${provider}:${model}`);
      options.push({
        key: `tier:${tier}:${provider}:${model}`,
        label: `${tier}: ${provider}/${model}`,
        tier,
        provider,
        model,
        source: "tier",
        reasoning_effort: reasoning,
      });
    }
  }

  for (const row of tiers?.tiers ?? []) {
    addTierOption(
      row.tier,
      row.provider,
      row.models?.length ? row.models : row.model,
      row.reasoning_effort,
    );
  }

  for (const row of config?.tiers ?? []) {
    const routes = Array.isArray(row.routes) && row.routes.length ? row.routes : [row];
    for (const route of routes) {
      addTierOption(
        row.tier,
        route.provider,
        route.models?.length ? route.models : route.model,
        row.reasoning_effort,
      );
    }
  }

  for (const [providerRaw, rows] of Object.entries(models?.providers ?? {})) {
    const provider = providerRaw.trim().toLowerCase();
    if (!provider) continue;
    for (const row of rows.slice(0, 160)) {
      const model = modelIdFromCatalogRow(row);
      if (!model) continue;
      const providerModel = `${provider}:${model}`;
      if (seenProviderModel.has(providerModel)) continue;
      seenProviderModel.add(providerModel);
      options.push({
        key: `catalog:${provider}:${model}`,
        label: `${provider}/${model}`,
        provider,
        model,
        source: "catalog",
      });
    }
  }

  return options.slice(0, 320);
}

export type ChatThread = {
  id: string;
  title: string;
  created_ts: number;
  updated_ts: number;
  messages: ChatMessage[];
  message_count?: number;
  /** When true, this thread mirrors a backend session that was started
   * outside the dashboard (curl, gateway, scripted run). The transcript
   * is reconstructed from the agent journal on hydrate. New messages
   * sent into this thread will reuse ``id`` as the ``session_id`` so
   * the conversation continues against the same on-disk session.
   */
  imported?: boolean;
  /** Last time we re-pulled this thread's transcript from the backend.
   * Used to decide whether a refresh on focus is worth doing. */
  imported_at?: number;
  transcript_loaded?: boolean;
  transcript_cached_at?: number;
  backend_updated_ts?: number;
};

const STORAGE_KEY = "nerya.chat.threads.v1";
const ACTIVE_KEY = "nerya.chat.active";
const SETTINGS_KEY = "nerya.chat.runSettings.v2";
const TRANSCRIPT_CACHE_PREFIX = "nerya.chat.transcript.v2:";
const TRANSCRIPT_CACHE_INDEX_KEY = "nerya.chat.transcriptIndex.v2";
const MAX_TRANSCRIPT_CACHE_ENTRIES = 20;
const DEFAULT_PERMISSION_MODE: PermissionMode =
  process.env.NEXT_PUBLIC_NERYA_PERMISSION_MODE === "yolo" ? "yolo" : "default";

export const DEFAULT_CHAT_RUN_SETTINGS: ChatRunSettings = {
  reasoning_effort: "off",
  permission_mode: DEFAULT_PERMISSION_MODE,
  model_context_window: 262144,
  model_tier: "",
  model_provider: "",
  model_id: "",
  max_iterations: 120,
  max_total_tool_calls: 400,
  max_wall_seconds: 1800,
};

export function uuid(): string {
  if (typeof crypto !== "undefined" && "randomUUID" in crypto) {
    return crypto.randomUUID();
  }
  return Math.random().toString(36).slice(2) + Date.now().toString(36);
}

function isBrowser() {
  return typeof window !== "undefined" && typeof localStorage !== "undefined";
}

type TranscriptCacheIndexEntry = {
  id: string;
  cached_at: number;
  updated_ts: number;
  bytes: number;
};

function stripLargeMessagePayloads(message: ChatMessage): ChatMessage {
  if (message.role !== "user" || !message.attachments?.length) return message;
  return {
    ...message,
    attachments: message.attachments.map((attachment) => ({
      ...attachment,
      data_url: undefined,
      text: undefined,
    })),
  };
}

function compactThreadForHistory(thread: ChatThread): ChatThread {
  if (thread.imported && thread.transcript_cached_at) {
    return {
      ...thread,
      messages: [],
      transcript_loaded: false,
    };
  }
  return {
    ...thread,
    messages: thread.messages.map(stripLargeMessagePayloads),
  };
}

function transcriptCacheKey(id: string): string {
  return `${TRANSCRIPT_CACHE_PREFIX}${id}`;
}

function loadTranscriptCacheIndex(): TranscriptCacheIndexEntry[] {
  if (!isBrowser()) return [];
  try {
    const parsed = JSON.parse(
      localStorage.getItem(TRANSCRIPT_CACHE_INDEX_KEY) || "[]",
    );
    if (!Array.isArray(parsed)) return [];
    return parsed.filter((row): row is TranscriptCacheIndexEntry => {
      return (
        row &&
        typeof row === "object" &&
        typeof row.id === "string" &&
        typeof row.cached_at === "number"
      );
    });
  } catch {
    return [];
  }
}

function saveTranscriptCacheIndex(index: TranscriptCacheIndexEntry[]) {
  if (!isBrowser()) return;
  try {
    localStorage.setItem(
      TRANSCRIPT_CACHE_INDEX_KEY,
      JSON.stringify(index.slice(0, MAX_TRANSCRIPT_CACHE_ENTRIES)),
    );
  } catch {
    // The transcript cache is opportunistic; history metadata still works.
  }
}

function pruneTranscriptCache(keepId?: string) {
  if (!isBrowser()) return;
  const index = loadTranscriptCacheIndex()
    .filter((row) => row.id !== keepId)
    .sort((a, b) => b.cached_at - a.cached_at);
  const keep = index.slice(0, Math.max(0, MAX_TRANSCRIPT_CACHE_ENTRIES - 1));
  const drop = index.slice(keep.length);
  for (const row of drop) {
    try {
      localStorage.removeItem(transcriptCacheKey(row.id));
    } catch {
      // best effort
    }
  }
  saveTranscriptCacheIndex(keep);
}

export function cacheThreadTranscript(thread: ChatThread): ChatThread {
  if (!isBrowser() || thread.messages.length === 0) {
    return { ...thread, transcript_loaded: thread.messages.length > 0 };
  }
  const cachedAt = Date.now();
  const next: ChatThread = {
    ...thread,
    transcript_loaded: true,
    transcript_cached_at: cachedAt,
    backend_updated_ts: thread.updated_ts,
    message_count: Math.max(thread.message_count ?? 0, thread.messages.length),
  };
  const raw = JSON.stringify({
    thread: {
      ...next,
      messages: next.messages.map(stripLargeMessagePayloads),
    },
    cached_at: cachedAt,
  });
  pruneTranscriptCache(thread.id);
  try {
    localStorage.setItem(transcriptCacheKey(thread.id), raw);
  } catch {
    // Quota can be hit by very large tool traces. Drop older cached
    // transcripts once more before giving up on this one.
    for (const row of loadTranscriptCacheIndex().sort((a, b) => a.cached_at - b.cached_at)) {
      if (row.id === thread.id) continue;
      try {
        localStorage.removeItem(transcriptCacheKey(row.id));
        localStorage.setItem(transcriptCacheKey(thread.id), raw);
        break;
      } catch {
        // keep pruning
      }
    }
  }
  try {
    if (localStorage.getItem(transcriptCacheKey(thread.id)) !== raw) {
      return { ...thread, transcript_loaded: true };
    }
    const index = loadTranscriptCacheIndex().filter((row) => row.id !== thread.id);
    index.unshift({
      id: thread.id,
      cached_at: cachedAt,
      updated_ts: thread.updated_ts,
      bytes: raw.length,
    });
    saveTranscriptCacheIndex(index);
    return next;
  } catch {
    return { ...thread, transcript_loaded: true };
  }
}

export function loadCachedThreadTranscript(
  id: string,
  minUpdatedTs = 0,
): ChatThread | null {
  if (!isBrowser() || !id) return null;
  try {
    const raw = localStorage.getItem(transcriptCacheKey(id));
    if (!raw) return null;
    const parsed = JSON.parse(raw);
    const thread = parsed?.thread as ChatThread | undefined;
    if (!thread || thread.id !== id || !Array.isArray(thread.messages)) {
      return null;
    }
    if (thread.messages.length === 0) {
      localStorage.removeItem(transcriptCacheKey(id));
      saveTranscriptCacheIndex(
        loadTranscriptCacheIndex().filter((row) => row.id !== id),
      );
      return null;
    }
    const updated = Number(thread.backend_updated_ts || thread.updated_ts || 0);
    if (minUpdatedTs > 0 && updated + 1000 < minUpdatedTs) return null;
    const cachedAt = Date.now();
    const index = loadTranscriptCacheIndex().filter((row) => row.id !== id);
    index.unshift({
      id,
      cached_at: cachedAt,
      updated_ts: updated,
      bytes: raw.length,
    });
    saveTranscriptCacheIndex(index);
    return {
      ...thread,
      transcript_loaded: true,
      transcript_cached_at: cachedAt,
      imported: true,
    };
  } catch {
    return null;
  }
}

export function loadThreads(): ChatThread[] {
  if (!isBrowser()) return [];
  try {
    const raw = localStorage.getItem(STORAGE_KEY);
    if (!raw) return [];
    const parsed = JSON.parse(raw);
    if (!Array.isArray(parsed)) return [];
    return parsed as ChatThread[];
  } catch {
    return [];
  }
}

export function saveThreads(threads: ChatThread[]) {
  if (!isBrowser()) return;
  try {
    localStorage.setItem(
      STORAGE_KEY,
      JSON.stringify(threads.map(compactThreadForHistory)),
    );
  } catch {
    // ignore quota errors — the UI will keep working with in-memory state.
  }
  emitThreadsChanged();
}

// ── Cross-component thread sync ─────────────────────────────────────
// The Codex shell renders the conversation list in the global sidebar
// while ChatView owns the live transcript + persistence. Both read the
// same localStorage key, so we broadcast a same-tab event whenever the
// store changes (the native ``storage`` event only fires in *other*
// tabs). Listeners re-read lazily — ``loadThreads`` is a cheap parse.
export const THREADS_CHANGED_EVENT = "nerya:threads-changed";

export function emitThreadsChanged() {
  if (!isBrowser()) return;
  try {
    window.dispatchEvent(new CustomEvent(THREADS_CHANGED_EVENT));
  } catch {
    /* ignore */
  }
}

export function subscribeThreadsChanged(cb: () => void): () => void {
  if (!isBrowser()) return () => {};
  const handler = (event?: Event) => {
    if (event && event.type === "storage") {
      const key = (event as StorageEvent).key;
      if (key && key !== STORAGE_KEY && key !== DELETED_SESSIONS_KEY) return;
    }
    cb();
  };
  window.addEventListener(THREADS_CHANGED_EVENT, handler as EventListener);
  window.addEventListener("storage", handler as EventListener);
  return () => {
    window.removeEventListener(THREADS_CHANGED_EVENT, handler as EventListener);
    window.removeEventListener("storage", handler as EventListener);
  };
}

// ── Deleted-session tombstones ─────────────────────────────────────
// Shared with ChatView (same storage key) so a delete from the global
// sidebar also stops ChatView's backend-session hydrate from re-importing
// the conversation the operator just removed.
export const DELETED_SESSIONS_KEY = "nerya.chat.deletedSessions.v1";

export function loadDeletedSessionIds(): Set<string> {
  if (!isBrowser()) return new Set();
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

export function rememberDeletedSession(id: string): Set<string> {
  const next = loadDeletedSessionIds();
  next.add(id);
  if (isBrowser()) {
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

/**
 * Remove a thread from the local store: write a tombstone, drop it from
 * the persisted list, and broadcast the change. The backend session
 * delete is fired by the caller so this module stays free of the API
 * client import (and works even when the backend is offline).
 */
export function deleteThreadLocally(id: string): ChatThread[] {
  rememberDeletedSession(id);
  const next = loadThreads().filter((t) => t.id !== id);
  saveThreads(next);
  return next;
}

export function loadActiveId(): string | null {
  if (!isBrowser()) return null;
  return localStorage.getItem(ACTIVE_KEY);
}

export function saveActiveId(id: string | null) {
  if (!isBrowser()) return;
  if (id === null) localStorage.removeItem(ACTIVE_KEY);
  else localStorage.setItem(ACTIVE_KEY, id);
}

function saneRunLimit(
  value: unknown,
  fallback: number,
  min: number,
  max: number,
): number {
  const n =
    typeof value === "number"
      ? value
      : typeof value === "string"
        ? Number(value)
        : NaN;
  if (!Number.isFinite(n)) return fallback;
  return Math.max(min, Math.min(max, Math.round(n)));
}

function saneContextWindow(value: unknown): ModelContextWindow {
  const raw =
    typeof value === "number"
      ? value
      : typeof value === "string"
        ? Number(value.replace(/_/g, ""))
        : NaN;
  if (raw === 131072 || raw === 128000) return 131072;
  if (raw === 262144 || raw === 256000) return 262144;
  if (raw === 1048576 || raw === 1000000) return 1048576;
  return DEFAULT_CHAT_RUN_SETTINGS.model_context_window;
}

function saneModelOverrides(value: unknown): Record<string, ChatModelOverride> | undefined {
  if (!value || typeof value !== "object" || Array.isArray(value)) return undefined;
  const out: Record<string, ChatModelOverride> = {};
  for (const [key, raw] of Object.entries(value as Record<string, unknown>)) {
    if (!key || !raw || typeof raw !== "object" || Array.isArray(raw)) continue;
    const row = raw as Record<string, unknown>;
    const effort = row.reasoning_effort;
    const override: ChatModelOverride = {};
    if (
      typeof effort === "string" &&
      ["off", "minimal", "low", "medium", "high", "xhigh"].includes(effort)
    ) {
      override.reasoning_effort = effort as ReasoningEffort;
    }
    if ("model_context_window" in row) {
      override.model_context_window = saneContextWindow(row.model_context_window);
    }
    if (override.reasoning_effort || override.model_context_window) {
      out[key.slice(0, 220)] = override;
    }
  }
  return Object.keys(out).length ? out : undefined;
}

export function loadRunSettings(): ChatRunSettings {
  if (!isBrowser()) return DEFAULT_CHAT_RUN_SETTINGS;
  try {
    const raw = localStorage.getItem(SETTINGS_KEY);
    if (!raw) return DEFAULT_CHAT_RUN_SETTINGS;
    const parsed = JSON.parse(raw) as Partial<ChatRunSettings>;
    const effort = parsed.reasoning_effort;
    const mode = parsed.permission_mode;
    return {
      reasoning_effort:
        effort && ["off", "minimal", "low", "medium", "high", "xhigh"].includes(effort)
          ? effort
          : DEFAULT_CHAT_RUN_SETTINGS.reasoning_effort,
      permission_mode: mode === "yolo" ? "yolo" : "default",
      model_context_window: saneContextWindow(parsed.model_context_window),
      model_tier:
        typeof parsed.model_tier === "string" ? parsed.model_tier.trim() : "",
      model_provider:
        typeof parsed.model_provider === "string"
          ? parsed.model_provider.trim().toLowerCase()
          : "",
      model_id:
        typeof parsed.model_id === "string" ? parsed.model_id.trim() : "",
      model_overrides: saneModelOverrides(parsed.model_overrides),
      max_iterations: saneRunLimit(
        parsed.max_iterations,
        DEFAULT_CHAT_RUN_SETTINGS.max_iterations,
        1,
        240,
      ),
      max_total_tool_calls: saneRunLimit(
        parsed.max_total_tool_calls,
        DEFAULT_CHAT_RUN_SETTINGS.max_total_tool_calls,
        1,
        1000,
      ),
      max_wall_seconds: saneRunLimit(
        parsed.max_wall_seconds,
        DEFAULT_CHAT_RUN_SETTINGS.max_wall_seconds,
        10,
        7200,
      ),
      evidence_contract:
        parsed.evidence_contract &&
        typeof parsed.evidence_contract === "object" &&
        !Array.isArray(parsed.evidence_contract)
          ? (parsed.evidence_contract as Record<string, unknown>)
          : undefined,
    };
  } catch {
    return DEFAULT_CHAT_RUN_SETTINGS;
  }
}

export function saveRunSettings(settings: ChatRunSettings) {
  if (!isBrowser()) return;
  try {
    localStorage.setItem(SETTINGS_KEY, JSON.stringify(settings));
  } catch {
    // ignore quota errors; the in-memory setting still applies.
  }
}

export function deriveTitle(text: string): string {
  const clean = text.trim().replace(/\s+/g, " ");
  if (clean.length <= 60) return clean;
  return clean.slice(0, 57) + "...";
}

export function newThread(seedText?: string): ChatThread {
  const now = Date.now();
  return {
    id: uuid(),
    title: seedText ? deriveTitle(seedText) : "New chat",
    created_ts: now,
    updated_ts: now,
    messages: [],
  };
}

export function upsertThread(
  threads: ChatThread[],
  next: ChatThread
): ChatThread[] {
  const i = threads.findIndex((t) => t.id === next.id);
  if (i < 0) return [next, ...threads];
  const copy = threads.slice();
  copy[i] = next;
  return copy;
}

/**
 * Convert the bus-event stream from ``/agent/stream/events`` into the
 * block-envelope shape used by the dashboard's
 * ``NativeBlocksTrack`` renderer. The kernel maps native blocks ↔ bus
 * events 1:1 on the way out (see ``nerya/agent/kernel.py`` ``_event_sink``):
 *
 *   text  block  → ``message.delta``      (accumulated)
 *   thinking      → ``turn.step`` step.kind=thinking
 *   tool_use      → ``tool.start``
 *   tool_result   → ``tool.complete``
 *   chart         → ``chart.block``        (kernel chart_hook splice)
 *
 * Reversing the mapping lets us render live progress through the same
 * block UI used for the final ``turn.blocks`` payload — one transcript
 * lens for both streaming and committed turns.
 */
export function liveEventsToBlocks(events: LiveEvent[]): NativeBlockEnvelope[] {
  const out: NativeBlockEnvelope[] = [];
  let textAccum = "";
  let textIdx: number | null = null;

  function mergeStreamingText(current: string, piece: string): string {
    if (!current) return piece;
    // Some backends publish true deltas (" world"), while others publish the
    // current accumulated block text ("Hello world"). Support both shapes so
    // the UI never duplicates text during reconnects or adapter changes.
    if (piece.startsWith(current)) return piece;
    if (current.endsWith(piece)) return current;
    return current + piece;
  }

  function flushText() {
    if (textIdx !== null) {
      // The accumulated text block is already pushed; nothing else to
      // do — text is live-updated in place via ``out[textIdx]``.
      textIdx = null;
      textAccum = "";
    }
  }

  const approvalIndex = new Map<string, number>();
  const subagentIndex = new Map<string, number>();
  const teamIndex = new Map<string, number>();

  function asRecord(value: unknown): Record<string, unknown> {
    return value && typeof value === "object" && !Array.isArray(value)
      ? (value as Record<string, unknown>)
      : {};
  }

  function roleNames(value: unknown): string[] {
    if (!Array.isArray(value)) return [];
    const names: string[] = [];
    for (const row of value) {
      if (typeof row === "string" && row.trim()) {
        names.push(row.trim());
      } else if (row && typeof row === "object") {
        const name = String((row as Record<string, unknown>).name || "").trim();
        if (name) names.push(name);
      }
    }
    return names;
  }

  function teamKey(ev: LiveEvent): string {
    const key = String(
      ev.team_run_id ||
        ev.run_id ||
        ev.call_id ||
        ev.event_id ||
        "team",
    );
    return key || "team";
  }

  function ensureTeamTrace(ev: LiveEvent): NativeBlock {
    const key = teamKey(ev);
    const existing = teamIndex.get(key);
    if (existing !== undefined) {
      const block = (out[existing].block ?? out[existing]) as NativeBlock;
      block.status = ev.status || block.status;
      block.phase = ev.phase || block.phase;
      block.goal = ev.goal || block.goal;
      block.template_id = ev.template || ev.template_id || block.template_id;
      return block;
    }
    const payload = asRecord(ev.payload);
    const roles = roleNames(ev.roles ?? payload.roles);
    const block: NativeBlock = {
      kind: "team_trace",
      call_id: String(ev.call_id || ev.tool_call_id || ""),
      team_key: key,
      run_id: ev.team_run_id || ev.run_id,
      template_id: ev.team_template || ev.template || ev.template_id,
      phase: ev.phase,
      goal: ev.goal,
      task: String(
        ev.task ||
          ev.team_task ||
          ev.team_task_subject ||
          payload.task ||
          ev.goal ||
          ev.team_run_id ||
          "",
      ),
      status: "running",
      roles,
      max_parallel: ev.max_parallel ?? payload.max_parallel,
      collaboration_model: ev.collaboration_model,
      steps: [],
      members: {},
      index: out.length,
    };
    teamIndex.set(key, out.length);
    out.push({ block, kind: "team_trace" });
    return block;
  }

  function appendTeamStep(ev: LiveEvent, stepKind: string) {
    const block = ensureTeamTrace(ev);
    const steps = Array.isArray(block.steps) ? [...block.steps] : [];
    const eventKind = String(ev.team_event_kind || stepKind);
    const subagent = String(
      ev.subagent || ev.subagent_name || ev.role || ev.owner || ev.from_agent || "",
    );
    steps.push({
      kind: eventKind,
      lifecycle: stepKind,
      subagent,
      owner: ev.owner,
      task_id: ev.task_id || ev.team_task_id,
      subject: ev.subject || ev.team_task_subject,
      phase: ev.phase,
      status: ev.status || (ev.ok === false ? "error" : "ok"),
      ok: ev.ok,
      error: ev.error,
      summary: ev.summary,
      content: ev.content,
      from_agent: ev.from_agent,
      to: ev.to,
      artifact: ev.artifact || ev.artifact_id,
      artifact_refs: ev.artifact_refs,
      entry_kind: ev.entry_kind,
      outcomes: ev.outcomes,
      tokens: ev.tokens,
      usd: ev.usd,
      wall_ms: ev.wall_ms,
      payload: ev.payload,
      input_payload: ev.input_payload,
      assignment_prompt: ev.assignment_prompt,
      output: ev.output,
      metrics: ev.metrics,
      results: ev.results,
      failures: ev.failures,
      aggregated: ev.aggregated,
      ts: ev.ts,
      raw: ev.team_event || ev,
    });
    block.steps = steps;
    if (ev.status) block.status = ev.status;
    if (ev.phase) block.phase = ev.phase;
    if (eventKind === "run.completed") block.status = "completed";
    if (eventKind === "run.failed") block.status = "failed";
    if (subagent) {
      const members = asRecord(block.members);
      const memberStatus = ev.status
        ? String(ev.status)
        : eventKind === "task.updated" && ev.status
        ? String(ev.status)
        : stepKind === "member.start"
        ? "running"
        : stepKind === "member.skip"
        ? "skipped"
        : ev.ok === false
        ? "failed"
        : "completed";
      members[subagent] = {
        ...(asRecord(members[subagent])),
        name: subagent,
        owner: ev.owner,
        task_id: ev.task_id || ev.team_task_id,
        subject: ev.subject || ev.team_task_subject,
        team_task_id: ev.team_task_id || ev.task_id,
        team_task_owner: ev.team_task_owner || ev.owner,
        team_task_subject: ev.team_task_subject || ev.subject,
        status: memberStatus,
        ok: ev.ok,
        caveat: ev.caveat ?? asRecord(members[subagent]).caveat,
        error: ev.error,
        summary: ev.summary,
        artifact: ev.artifact || ev.artifact_id,
        tokens: ev.tokens,
        usd: ev.usd,
        wall_ms: ev.wall_ms,
        payload: ev.payload,
        input_payload: ev.input_payload,
        assignment_prompt: ev.assignment_prompt,
        output: ev.output,
        metrics: ev.metrics,
      };
      block.members = members;
    }
  }

  function mergeSubagentIntoTeam(ev: LiveEvent, lifecycle: string) {
    if (!ev.team_run_id) return;
    const block = ensureTeamTrace(ev);
    const name = String(
      ev.subagent ||
        ev.name ||
        ev.team_task_owner ||
        ev.team_task_id ||
        "subagent",
    );
    const members = asRecord(block.members);
    const previous = asRecord(members[name]);
    const memberSteps = Array.isArray(previous.steps)
      ? [...(previous.steps as unknown[])]
      : [];
    const step = {
      lifecycle,
      step_kind: ev.step_kind,
      iteration: ev.iteration,
      status: ev.status || (ev.error ? "error" : "ok"),
      skill: ev.skill,
      action: ev.action,
      error: ev.error,
      wall_ms: ev.wall_ms,
      tokens: ev.tokens,
      usd: ev.usd,
      provider: ev.provider,
      model: ev.model,
      parsed_keys: ev.parsed_keys,
      reasoning: ev.reasoning,
      reasoning_tokens: ev.reasoning_tokens,
      reasoning_effort: ev.reasoning_effort,
      prompt: ev.prompt,
      prompt_chars: ev.prompt_chars,
      payload: ev.payload,
      output: ev.output,
      metrics: ev.metrics,
      ts: ev.ts,
    };
    memberSteps.push(step);
    const nextStatus =
      lifecycle === "start"
        ? "running"
        : lifecycle === "end"
        ? ev.error
          ? "failed"
          : "completed"
        : ev.error || ev.status === "error"
        ? "error"
        : String(previous.status || "running");
    members[name] = {
      ...previous,
      name,
      status: nextStatus,
      tier: previous.tier || ev.tier,
      team_task_id: ev.team_task_id,
      team_task_owner: ev.team_task_owner,
      team_task_subject: ev.team_task_subject,
      payload_keys: ev.payload_keys || previous.payload_keys,
      payload: ev.payload || previous.payload,
      input_payload: ev.input_payload || previous.input_payload,
      assignment_prompt: ev.assignment_prompt || previous.assignment_prompt,
      role_prompt: ev.role_prompt || previous.role_prompt,
      prompt_path: ev.prompt_path || previous.prompt_path,
      allowed_skills: ev.allowed_skills || previous.allowed_skills,
      native_tools: ev.native_tools || previous.native_tools,
      last_prompt: ev.prompt || previous.last_prompt,
      prompt_chars: ev.prompt_chars || previous.prompt_chars,
      output: ev.output || previous.output,
      metrics: ev.metrics || previous.metrics,
      tokens: ev.tokens ?? previous.tokens,
      usd: ev.usd ?? previous.usd,
      wall_ms: ev.wall_ms ?? previous.wall_ms,
      steps: memberSteps,
    };
    block.members = members;

    const steps = Array.isArray(block.steps) ? [...block.steps] : [];
    steps.push({
      kind: `subagent.${String(ev.step_kind || lifecycle)}`,
      lifecycle,
      subagent: name,
      task_id: ev.team_task_id,
      subject: ev.team_task_subject,
      status: step.status,
      skill: ev.skill,
      action: ev.action,
      prompt_chars: ev.prompt_chars,
      summary: lifecycle === "end" ? "member completed" : undefined,
      error: ev.error,
      tokens: ev.tokens,
      usd: ev.usd,
      wall_ms: ev.wall_ms,
      ts: ev.ts,
      raw: ev,
    });
    block.steps = steps;
    if (String(block.status || "") !== "completed") {
      block.status = nextStatus === "failed" || nextStatus === "error" ? "error" : "running";
    }
  }

  function ensureSubagentTrace(ev: LiveEvent): NativeBlock {
    const name = String(ev.subagent || ev.name || "subagent");
    const key = [
      String(ev.team_run_id || ""),
      String(ev.team_task_id || ""),
      name,
    ]
      .filter(Boolean)
      .join(":") || name;
    const existing = subagentIndex.get(key);
    if (existing !== undefined) {
      return (out[existing].block ?? out[existing]) as NativeBlock;
    }
    const block: NativeBlock = {
      kind: "subagent_trace",
      subagent: name,
      team_run_id: ev.team_run_id,
      team_template: ev.team_template,
      team_call_id: ev.team_call_id,
      team_task_id: ev.team_task_id,
      team_task_owner: ev.team_task_owner,
      team_task_subject: ev.team_task_subject,
      tier: ev.tier,
      status: "running",
      payload_keys: ev.payload_keys,
      payload: ev.payload,
      role_prompt: ev.role_prompt,
      prompt_path: ev.prompt_path,
      allowed_skills: ev.allowed_skills,
      native_tools: ev.native_tools,
      steps: [],
      index: out.length,
    };
    subagentIndex.set(key, out.length);
    out.push({ block, kind: "subagent_trace" });
    return block;
  }

  function appendSubagentStep(ev: LiveEvent, lifecycle: string) {
    const block = ensureSubagentTrace(ev);
    const steps = Array.isArray(block.steps) ? [...block.steps] : [];
    steps.push({
      lifecycle,
      step_kind: ev.step_kind,
      iteration: ev.iteration,
      status: ev.status || (ev.error ? "error" : "ok"),
      skill: ev.skill,
      action: ev.action,
      error: ev.error,
      wall_ms: ev.wall_ms,
      tokens: ev.tokens,
      usd: ev.usd,
      provider: ev.provider,
      model: ev.model,
      parsed_keys: ev.parsed_keys,
      reasoning: ev.reasoning,
      reasoning_tokens: ev.reasoning_tokens,
      reasoning_effort: ev.reasoning_effort,
      prompt: ev.prompt,
      prompt_chars: ev.prompt_chars,
      payload: ev.payload,
      output: ev.output,
      metrics: ev.metrics,
      ts: ev.ts,
    });
    block.steps = steps;
    block.tier = block.tier || ev.tier;
    if (lifecycle === "end") {
      block.status = ev.error ? "failed" : "completed";
      block.iterations = ev.iterations;
      block.skill_calls = ev.skill_calls;
      block.rejected = ev.rejected;
      block.tokens = ev.tokens;
      block.usd = ev.usd;
      block.wall_ms = ev.wall_ms;
      block.output = ev.output;
      block.metrics = ev.metrics;
    } else if (ev.error || ev.status === "error") {
      block.status = "error";
    } else {
      block.status = "running";
    }
    if (ev.prompt) block.last_prompt = ev.prompt;
    if (ev.payload) block.payload = ev.payload;
    if (ev.role_prompt) block.role_prompt = ev.role_prompt;
    mergeSubagentIntoTeam(ev, lifecycle);
  }

  function rebuildApprovalIndex() {
    approvalIndex.clear();
    out.forEach((env, idx) => {
      if ((env.block?.kind ?? env.kind) !== "approval_request") return;
      const id = String(env.block?.approval_id ?? env.block?.id ?? "");
      if (id) approvalIndex.set(id, idx);
    });
  }
  function insertIndexAfterCall(callId: string): number | null {
    if (!callId) return null;
    for (let idx = out.length - 1; idx >= 0; idx -= 1) {
      const block = out[idx]?.block;
      if (
        (block?.kind ?? out[idx]?.kind) === "tool_result" &&
        String(block?.call_id ?? "") === callId
      ) {
        return idx + 1;
      }
    }
    return null;
  }

  for (const ev of events) {
    if (ev.kind === "message.delta") {
      const piece =
        typeof ev.text === "string"
          ? ev.text
          : typeof ev.message === "string"
          ? ev.message
          : "";
      if (!piece) continue;
      if (textIdx === null) {
        textAccum = piece;
        const env: NativeBlockEnvelope = {
          block: { kind: "text", text: textAccum, index: out.length },
          kind: "text",
        };
        textIdx = out.length;
        out.push(env);
      } else {
        textAccum = mergeStreamingText(textAccum, piece);
        const env = out[textIdx];
        if (env.block) {
          env.block = { ...env.block, text: textAccum };
        }
      }
      continue;
    }

    if (ev.kind === "turn.step") {
      const step = (ev.step ?? {}) as Record<string, unknown>;
      const kind =
        typeof step === "object" && typeof step.step_kind === "string"
          ? step.step_kind
          : typeof step === "object" && typeof step.kind === "string"
          ? (step.kind as string)
          : typeof ev.step === "string"
          ? ev.step
          : "step";
      if (kind === "thinking" || kind === "think") {
        flushText();
        const detail =
          typeof step === "object" &&
          step.detail &&
          typeof step.detail === "object"
            ? (step.detail as Record<string, unknown>)
            : null;
        const text =
          (detail && typeof detail.text === "string" && detail.text) ||
          (detail && typeof detail.reasoning === "string" && detail.reasoning) ||
          "";
        out.push({
          block: { kind: "thinking", text: String(text), index: out.length },
          kind: "thinking",
        });
      }
      continue;
    }

    if (ev.kind === "tool.start") {
      flushText();
      const callId = String(ev.call_id ?? ev.tool_call_id ?? "");
      const env: NativeBlockEnvelope = {
        block: {
          kind: "tool_use",
          call_id: callId,
          skill_id: ev.skill_id ?? "native",
          action: ev.action ?? "",
          payload: (ev.payload as Record<string, unknown>) ?? {},
          index: out.length,
        },
        kind: "tool_use",
      };
      out.push(env);
      if (ev.action === "team_run") {
        ensureTeamTrace(ev);
      }
      continue;
    }

    if (ev.kind === "tool.complete") {
      flushText();
      const callId = String(ev.call_id ?? ev.tool_call_id ?? "");
      out.push({
        block: {
          kind: "tool_result",
          call_id: callId,
          skill_id: ev.skill_id ?? "native",
          action: ev.action ?? "",
          payload: (ev.payload as Record<string, unknown>) ?? {},
          ok: ev.ok ?? true,
          error: ev.error ?? null,
          error_kind: ev.error_kind ?? null,
          elapsed_ms: ev.elapsed_ms ?? null,
          result: (ev as Record<string, unknown>).result,
          index: out.length,
        },
        kind: "tool_result",
      });
      if (ev.action === "team_run") {
        const block = ensureTeamTrace(ev);
        block.status = ev.ok === false || ev.error ? "failed" : "completed";
        block.result = (ev as Record<string, unknown>).result;
      }
      continue;
    }

    if (ev.kind === "chart.block") {
      flushText();
      const evRec = ev as Record<string, unknown>;
      const chartBlockRaw = evRec.chart_block;
      if (!chartBlockRaw || typeof chartBlockRaw !== "object") continue;
      const chartBlock = chartBlockRaw as Record<string, unknown>;
      const chartId = String(chartBlock.chart_id ?? evRec.chart_id ?? "");
      if (!chartId) continue;
      // Idempotent: if we already saw this chart_id, skip — the
      // streaming bus may emit the same envelope after a reconnect.
      const dup = out.find(
        (entry) =>
          entry.kind === "chart" &&
          entry.block &&
          (entry.block as Record<string, unknown>).chart_id === chartId,
      );
      if (dup) continue;
      const callId = String(evRec.call_id ?? evRec.tool_call_id ?? "");
      const block: Record<string, unknown> = {
        ...chartBlock,
        kind: "chart",
        index: out.length,
      };
      if (callId && !block.call_id) block.call_id = callId;
      // Try to insert immediately after the matching tool_result so
      // the chart sits where the user expects (next to the call). If
      // we never saw the tool_result yet (event ordering), append.
      let insertAt = -1;
      if (callId) {
        for (let i = out.length - 1; i >= 0; i -= 1) {
          const entry = out[i];
          if (
            entry.kind === "tool_result" &&
            entry.block &&
            (entry.block as Record<string, unknown>).call_id === callId
          ) {
            insertAt = i + 1;
            break;
          }
        }
      }
      const env: NativeBlockEnvelope = { block, kind: "chart" };
      if (insertAt >= 0) {
        out.splice(insertAt, 0, env);
      } else {
        out.push(env);
      }
      continue;
    }

    if (ev.kind === "approval.request") {
      flushText();
      const approvalId = String(ev.approval_id ?? ev.id ?? "");
      if (!approvalId) continue;
      const callId = String(ev.call_id ?? ev.tool_call_id ?? "");
      const env: NativeBlockEnvelope = {
        block: {
          kind: "approval_request",
          approval_id: approvalId,
          call_id: callId,
          prompt: ev.prompt,
          record: ev.record,
          reason: ev.reason,
          index: out.length,
        },
        kind: "approval_request",
      };
      const existingIdx = approvalIndex.get(approvalId);
      if (existingIdx !== undefined && out[existingIdx]?.block) {
        env.block = {
          ...out[existingIdx].block,
          ...env.block,
          index: out[existingIdx].block?.index ?? existingIdx,
        };
        const targetIdx = insertIndexAfterCall(callId);
        if (targetIdx !== null && targetIdx !== existingIdx + 1) {
          out.splice(existingIdx, 1);
          const adjustedTarget = targetIdx > existingIdx ? targetIdx - 1 : targetIdx;
          out.splice(Math.min(adjustedTarget, out.length), 0, env);
        } else {
          out[existingIdx] = env;
        }
        rebuildApprovalIndex();
      } else {
        const insertAt = insertIndexAfterCall(callId) ?? out.length;
        out.splice(insertAt, 0, env);
        rebuildApprovalIndex();
      }
      continue;
    }

    if (ev.kind === "approval.resolved") {
      flushText();
      const approvalId = String(ev.approval_id ?? ev.id ?? "");
      if (!approvalId) continue;
      const idx = approvalIndex.get(approvalId);
      if (idx !== undefined && out[idx]?.block) {
        out[idx].block = {
          ...out[idx].block,
          state: ev.state,
          resolved_state: ev.state,
        };
      } else {
        out.push({
          block: {
            kind: "approval_request",
            approval_id: approvalId,
            state: ev.state,
            resolved_state: ev.state,
            index: out.length,
          },
          kind: "approval_request",
        });
      }
      continue;
    }

    if (ev.kind === "team.start") {
      flushText();
      appendTeamStep(ev, "start");
      continue;
    }

    if (ev.kind === "team.event") {
      flushText();
      appendTeamStep(ev, String(ev.team_event_kind || "event"));
      continue;
    }

    if (ev.kind === "team.member.start") {
      flushText();
      appendTeamStep(ev, "member.start");
      continue;
    }

    if (ev.kind === "team.member.end") {
      flushText();
      appendTeamStep(ev, "member.end");
      continue;
    }

    if (ev.kind === "team.member.skip") {
      flushText();
      appendTeamStep(ev, "member.skip");
      continue;
    }

    if (ev.kind === "team.end") {
      flushText();
      const block = ensureTeamTrace(ev);
      block.status = ev.ok === false || ev.error ? "failed" : "completed";
      block.roles_succeeded = ev.roles_succeeded;
      block.roles_failed = ev.roles_failed;
      block.tokens_total = ev.tokens_total;
      block.usd_total = ev.usd_total;
      appendTeamStep(ev, "end");
      continue;
    }

    if (ev.kind === "subagent.start") {
      flushText();
      appendSubagentStep(ev, "start");
      continue;
    }

    if (ev.kind === "subagent.step") {
      flushText();
      appendSubagentStep(ev, "step");
      continue;
    }

    if (ev.kind === "subagent.end") {
      flushText();
      appendSubagentStep(ev, "end");
      continue;
    }
  }
  return out;
}

export function topLevelDecisionText(turn: TurnPayload | undefined): string {
  if (!turn) return "";
  if (typeof turn.reply_text === "string" && turn.reply_text.trim()) {
    return turn.reply_text.trim();
  }
  if (typeof turn.final_text === "string" && turn.final_text.trim()) {
    return turn.final_text.trim();
  }
  const d = turn.decision;
  if (!d) return "";
  const reasoning = typeof d.reasoning === "string" ? d.reasoning : "";
  if (reasoning) return reasoning;
  // Fall back to the first action's text / first message / raw decision.
  const actions = (turn.actions || []) as ActionRecord[];
  for (const a of actions) {
    const r = (a as { result?: { text?: string } }).result;
    if (r && typeof r.text === "string") return r.text;
  }
  return "";
}
