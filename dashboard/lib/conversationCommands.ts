"use client";

import { ApiError, callApi } from "./clientApi";
import type { AssistantMessage, ChatAttachment, ChatRunSettings, ChatThread, LiveEvent, TaskDelivery, TurnPayload } from "./chat";
import { DEFAULT_CHAT_RUN_SETTINGS, uuid } from "./chat";
import { mergeStreamEvents } from "./streamEvents";
import { getWorkspaceIdentity, subscribeWorkspaceIdentity, workspaceGeneration, workspaceStorageKey } from "./workspaceIdentity";

export type CommandState = "queued" | "running" | "stopping" | "delivering" | "delivered" | "injected" |
  "succeeded" | "failed" | "blocked" | "awaiting_approval" | "awaiting_input" | "interrupted" | "unconfirmed" | "not_consumed" | "removed";
export type CommandKind = "send" | "resume" | "guide";
export type ConversationCommand = {
  command_id: string; session_id: string; kind: CommandKind; state: CommandState;
  revision: number; turn_id: string; position: number; created_at: number; updated_at: number;
  input: string; attachments: ChatAttachment[]; context: Record<string, unknown>;
  editable_settings?: Partial<ChatRunSettings> & { reasoning_summary?: string | null };
  queue_editable?: boolean;
  show_user?: boolean; has_result?: boolean; result?: TurnPayload | null;
  error?: { code?: string; status_code?: number; request_id?: string; retrying?: boolean } | null;
  delivery?: TaskDelivery | null;
  /** Observer-owned durable replay, retained when transcript hydration finishes later. */
  hydrated_events?: LiveEvent[];
};
export type QueueEditDraft = { text: string; attachments: ChatAttachment[]; settings: ChatRunSettings; revision: number };
export function queueEditDraft(command: ConversationCommand): QueueEditDraft {
  const settings = command.editable_settings || command.context.run_settings || command.context.requested_model || {};
  return { text: command.input, attachments: command.attachments, revision: command.revision,
    settings: { ...DEFAULT_CHAT_RUN_SETTINGS, permission_mode: "default", work_mode: command.context.work_mode === "plan" || command.context.work_mode === "goal" ? command.context.work_mode : "execute",
      ...Object.fromEntries(Object.entries(settings).filter(([, value]) => value != null)) } as ChatRunSettings };
}
export function queueEditRequest(command: ConversationCommand, draft: QueueEditDraft): Record<string, unknown> {
  const initial = queueEditDraft(command).settings;
  const request: Record<string, unknown> = { payload: { text: draft.text, attachments: draft.attachments.map(file => ({
    id: file.id, name: file.name, mime_type: file.mime_type, size: file.size, kind: file.kind,
    artifact_uri: file.artifact_uri, reference: file.reference,
  })) } };
  for (const key of ["work_mode", "permission_mode", "model_provider", "model_id", "model_tier", "model_context_window",
    "reasoning_effort", "max_iterations", "max_total_tool_calls", "max_wall_seconds"] as const) {
    if (draft.settings[key] !== initial[key]) request[key] = draft.settings[key];
  }
  if (draft.settings.reasoning_effort !== initial.reasoning_effort) {
    request.reasoning_effort = draft.settings.reasoning_effort === "inherit" ? null : draft.settings.reasoning_effort;
    request.reasoning_summary = ["inherit", "off"].includes(draft.settings.reasoning_effort) ? null : "auto";
  }
  return request;
}
export type CommandSnapshot = {
  ok: boolean; commands: ConversationCommand[];
  queue: { paused: boolean | number; pause_reason: string; revision: number };
  checkpoint?: { turn_id: string; resumable: boolean; resume_count?: number } | null;
  branch_of?: { session_id: string; message_id: string } | null;
};
export type PendingCommand = { workspace_id: string; command_id: string; session_id: string; command_type: CommandKind;
  request: Record<string, unknown>; created_at: number };
export const COMMAND_PENDING_EVENT = "nerya:command-pending";
const KEY = "nerya.chat.pending-commands.v1";
const LEGACY_PREFIX = "nerya.chat.pending-command.v2:";
const PREFIX = "nerya.chat.pending-command.v3:";
const memory = new Map<string, PendingCommand>();
const volatile = new Set<string>();
// In-memory only: after reload, a durable pending row must remain recoverable.
const submitting = new Set<string>();
const submissionKey = (item: PendingCommand, generation = workspaceGeneration()) => JSON.stringify([item.workspace_id, generation, item.command_id]);
export const commandSubmitting = (item: PendingCommand) => submitting.has(submissionKey(item));
export const runningCommand = (command: ConversationCommand) => command.kind !== "guide" && ["running", "stopping"].includes(command.state);
export const commandSettled = (command: ConversationCommand) => !["queued", "running", "stopping", "delivering", "delivered"].includes(command.state);

export function pendingCommands(sid?: string): PendingCommand[] {
  const workspace = getWorkspaceIdentity();
  if (!workspace) return [];
  if (typeof window !== "undefined") {
    try {
      const legacy: unknown = JSON.parse(localStorage.getItem(KEY) || "[]");
      if (Array.isArray(legacy)) {
        const remaining = legacy.filter(item => {
          if (!validPending(item, workspace)) return true;
          localStorage.setItem(workspaceStorageKey(PREFIX, workspace, item.command_id), JSON.stringify(item));
          return false;
        });
        if (remaining.length !== legacy.length) localStorage.setItem(KEY, JSON.stringify(remaining));
      }
      const found = new Map<string,PendingCommand>();
      const keys = Array.from({ length: localStorage.length }, (_, index) => localStorage.key(index));
      for (const key of keys) {
        if (!key || (!key.startsWith(PREFIX) && !key.startsWith(LEGACY_PREFIX))) continue;
        try {
          const item = JSON.parse(localStorage.getItem(key) || "null");
          if (!validPending(item, workspace)) continue;
          const target = workspaceStorageKey(PREFIX, workspace, item.command_id);
          if (key.startsWith(LEGACY_PREFIX)) {
            localStorage.setItem(target, JSON.stringify(item)); localStorage.removeItem(key);
          } else if (key !== target) continue;
          found.set(item.command_id, item);
        } catch { /* A malformed row must not hide valid pending receipts. */ }
      }
      // Per-command keys prevent two tabs from overwriting each other's pending
      // requests. A removal in another tab clears the corresponding clue here.
      for (const id of memory.keys()) if (!found.has(id) && !volatile.has(id)) memory.delete(id);
      for (const [id,item] of found) memory.set(id,item);
    } catch { /* A blocked preference store does not turn an ACK into a failure. */ }
  }
  return [...memory.values()].filter(item => item.workspace_id === workspace && (!sid || item.session_id === sid));
}
subscribeWorkspaceIdentity(() => {
  memory.clear(); volatile.clear();
  if (typeof window !== "undefined") window.dispatchEvent(new Event(COMMAND_PENDING_EVENT));
});
function validPending(item: PendingCommand | null, workspace: string): item is PendingCommand {
  return Boolean(item && item.workspace_id === workspace && typeof item.command_id === "string" && typeof item.session_id === "string"
    && ["send", "resume", "guide"].includes(item.command_type) && item.request && typeof item.request === "object");
}
function persistPending(id: string, value: PendingCommand | null, workspace: string) {
  if (typeof window === "undefined") return;
  try {
    const key = workspaceStorageKey(PREFIX, workspace, id);
    if (value) localStorage.setItem(key,JSON.stringify(value)); else localStorage.removeItem(key);
    if (workspace === getWorkspaceIdentity()) volatile.delete(id);
  } catch { if (value && workspace === getWorkspaceIdentity()) volatile.add(id); }
  window.dispatchEvent(new Event(COMMAND_PENDING_EVENT));
}
export function settlePending(id: string, workspace = getWorkspaceIdentity()) {
  if (!workspace) return;
  if (workspace === getWorkspaceIdentity()) { memory.delete(id); volatile.delete(id); }
  persistPending(id,null,workspace);
}

export class CommandClientError extends Error {
  constructor(public readonly code: string, public readonly uncertain = false) { super(code); this.name = "CommandClientError"; }
}
export function commandErrorCode(error: unknown): string {
  if (error instanceof CommandClientError) return error.code;
  if (error instanceof ApiError && error.payload && typeof error.payload === "object") {
    return String((error.payload as { error?: string }).error || "command_failed");
  }
  return "connection_lost";
}
async function request<T extends { ok?: boolean }>(path: string, body?: unknown, signal?: AbortSignal): Promise<T> {
  const workspace = getWorkspaceIdentity(), generation = workspaceGeneration();
  if (!workspace) throw new CommandClientError("workspace_unknown");
  const controller = new AbortController();
  const abort = () => controller.abort();
  if (signal?.aborted) abort();
  signal?.addEventListener("abort", abort, { once: true });
  const timer = setTimeout(abort, 12000);
  try {
    const result = await callApi<T & { error?: string }>(path, { method: body === undefined ? "GET" : "POST", body, signal: controller.signal });
    if (workspace !== getWorkspaceIdentity() || generation !== workspaceGeneration()) throw new CommandClientError("workspace_changed", true);
    if (result.ok === false) throw new CommandClientError(result.error || "command_failed");
    return result;
  } finally { clearTimeout(timer); signal?.removeEventListener("abort", abort); }
}
function query(sid: string, id?: string) {
  const q = new URLSearchParams({ session_id: sid });
  if (id) q.set("command_id", id);
  return q.toString();
}
export const commandSnapshot = (sid: string, signal?: AbortSignal) => request<CommandSnapshot>(`/agent/commands?${query(sid)}`, undefined, signal);
export const commandGet = (sid: string, id: string, signal?: AbortSignal) => request<{ ok: boolean; command: ConversationCommand }>(`/agent/commands?${query(sid,id)}`, undefined, signal);
export const commandEvents = (sid: string, id: string, after: number, signal?: AbortSignal) => request<{
  ok: boolean; events: LiveEvent[]; cursor: number; has_more: boolean;
}>(`/agent/commands/events?${query(sid,id)}&after_seq=${after}&limit=500`, undefined, signal);
export const commandControl = (sid: string, action: string, revision: number, commandId?: string, extra: Record<string, unknown> = {}) =>
  request<CommandSnapshot & { command?: ConversationCommand }>("/agent/commands/control", {
    session_id: sid, action, expected_revision: revision, command_id: commandId, ...extra,
  });

/** One deliberate send, one stable key. Network failures never trigger a new turn. */
export async function submitCommand(sid: string, kind: CommandKind, body: Record<string, unknown>, existing?: PendingCommand) {
  const workspace = getWorkspaceIdentity(), generation = workspaceGeneration();
  if (!workspace) throw new CommandClientError("workspace_unknown");
  if (existing && (existing.workspace_id !== workspace || existing.session_id !== sid || existing.command_type !== kind)) throw new CommandClientError("workspace_mismatch");
  pendingCommands();
  if (!existing && memory.size >= 32) throw new CommandClientError("pending_limit");
  const pending = existing || { workspace_id: workspace, command_id: uuid(), session_id: sid, command_type: kind, request: body, created_at: Date.now() };
  const key = submissionKey(pending, generation);
  submitting.add(key);
  memory.set(pending.command_id, pending);
  persistPending(pending.command_id,pending,workspace);
  const started = typeof performance !== "undefined" ? performance.now() : Date.now();
  try {
    const result = await request<{ ok: boolean; command: ConversationCommand; duplicate: boolean }>("/agent/commands", pending);
    if (!result.command?.command_id || result.command.command_id !== pending.command_id || result.command.session_id !== pending.session_id) throw new CommandClientError("receipt_invalid",true);
    settlePending(pending.command_id,workspace);
    recordCommandTiming({ command_id: pending.command_id, phase: "ack", ms: (typeof performance !== "undefined" ? performance.now() : Date.now())-started });
    if (workspace !== getWorkspaceIdentity() || generation !== workspaceGeneration()) throw new CommandClientError("workspace_changed", true);
    return result.command;
  } catch (error) {
    if (error instanceof ApiError && error.status >= 400 && error.status < 500 && error.status !== 408) {
      settlePending(pending.command_id,workspace);
      throw new CommandClientError(commandErrorCode(error));
    }
    if (error instanceof CommandClientError && !error.uncertain) {
      settlePending(pending.command_id,workspace); throw error;
    }
    throw new CommandClientError("delivery_unconfirmed", true);
  } finally {
    submitting.delete(key);
    if (typeof window !== "undefined") window.dispatchEvent(new Event(COMMAND_PENDING_EVENT));
  }
}

// Local, bounded, content-free diagnostics. No prompt or provider key leaves the device.
const timing: { command_id: string; phase: string; ms: number }[] = [];
export function recordCommandTiming(item: { command_id: string; phase: string; ms: number }) {
  timing.push({ ...item, ms: Math.max(0, Math.round(item.ms)) });
  if (timing.length > 100) timing.splice(0, timing.length-100);
}
export const commandTimings = () => [...timing];

/** Projection only: source command identity prevents ACK/reload/stream duplicates. */
export function projectCommand(thread: ChatThread, command: ConversationCommand, fresh: LiveEvent[] = []): ChatThread {
  if (command.session_id !== thread.id || command.kind === "guide" || ["queued","removed"].includes(command.state)) return thread;
  const messages = [...thread.messages];
  const tid = command.turn_id;
  if (command.kind === "send" && command.show_user !== false && !messages.some(m => m.role === "user" && (m.command_id === command.command_id || m.backend_message_id === `${tid}:user`))) {
    messages.push({ id: command.command_id+":user", role: "user", ts: command.created_at*1000,
      text: command.input, attachments: command.attachments, command_id: command.command_id,
      task_message: command.context.task_message as import("./chat").UserMessage["task_message"],
      backend_message_id: `${tid}:user`, delivery_state: "received" });
  }
  const index = messages.findIndex(m => m.role === "assistant" && (m.command_id === command.command_id || m.backend_message_id === `${tid}:assistant`));
  const old = index >= 0 ? messages[index] as AssistantMessage : undefined;
  // An older GET/ACK must not undo a terminal projection already displayed.
  if (old?.command_id === command.command_id && (old.command_revision || 0) > command.revision) return thread;
  const oldCreated = old?.command_created_at ?? Number(old?.turn?.context_snapshot?.captured_at || 0);
  // A resumed turn keeps turn_id. Its older command snapshot must never put
  // the turn back into the previous failure/stop state after reconnecting.
  if (old?.command_id && old.command_id !== command.command_id && oldCreated > command.created_at) return thread;
  const live = runningCommand(command);
  const events = mergeStreamEvents(old?.live_events || [], command.hydrated_events || [], fresh);
  // Terminal replay can arrive before the result GET/transcript snapshot. Only
  // explicit producer terminal fields may fill that gap, never narrative text.
  const terminalEvents = commandSettled(command) ? mergeStreamEvents(
    !old?.command_id || old.command_id === command.command_id ? old?.live_events || [] : [],
    command.hydrated_events || [], fresh,
  ).filter(event => event.session_id === command.session_id && event.turn_id === tid &&
    (!event.command_id || event.command_id === command.command_id)) : [];
  const outcome = terminalEvents.findLast(event => event.kind === "command.outcome" && event.command_id === command.command_id);
  const result = command.result || (outcome?.result && typeof outcome.result === "object" ? outcome.result as TurnPayload : undefined);
  const turn = {...(result || old?.turn || { turn_id: tid })};
  if (!result && !turn.reply_text && !turn.final_text && !turn.decision?.text) {
    const complete = terminalEvents.findLast(event => event.kind === "turn.complete" && typeof event.final_text === "string");
    if (complete) { turn.final_text = complete.final_text as string; turn.stopped_reason = complete.stop_reason as string | undefined; }
  }
  // Command snapshots own delivery. Older APIs may omit it, so retain matching
  // evidence across result replacement, but never carry it into a resumed command.
  const delivery = command.delivery !== undefined ? command.delivery
    : result?.delivery !== undefined ? result.delivery
    : old?.turn?.delivery;
  if(command.state === "succeeded" || live) delete turn.error;
  const error = (["failed","unconfirmed"].includes(command.state) || command.state === "blocked" && Boolean(command.error))
    ? JSON.stringify(command.error || { code: command.state === "unconfirmed" ? "execution_unconfirmed" : "turn_failed", retrying: false }) : undefined;
  const next: AssistantMessage = { ...old, id: old?.id || command.command_id+":assistant", role: "assistant",
    ts: old?.ts || command.created_at*1000, backend_message_id: `${tid}:assistant`,
    command_id: command.command_id, command_created_at:command.created_at, command_revision: command.revision, execution_status: command.state,
    loading: live, error,
    started_ms: old?.started_ms || (live ? Date.now() : undefined),
    elapsed_ms: result?.execution_elapsed_ms ?? old?.elapsed_ms,
    live_events: events, live_cursor: fresh.at(-1)?.seq ?? old?.live_cursor,
    turn: { ...turn, command_id: command.command_id, execution_status: command.state, context_snapshot: command.context,
      delivery: delivery?.command_id === command.command_id ? delivery : null } };
  if (index >= 0) messages[index] = next; else messages.push(next);
  // A snapshot may include an older command outside the loaded transcript page.
  // Insert its messages chronologically so it cannot become the header's latest turn.
  messages.sort((a, b) => a.ts - b.ts);
  // Hydration can timestamp a prompt after its command's assistant. Emit the
  // matching prompt first without changing timestamps used by unread anchors.
  // Reapply this to every linked turn so later snapshots cannot invert it again.
  const users = messages.filter(message => message.role === "user");
  const usersByCommand = new Map(users.filter(message => message.command_id).map(message => [message.command_id, message]));
  const usersByBackend = new Map(users.filter(message => message.backend_message_id).map(message => [message.backend_message_id, message]));
  const ordered: ChatThread["messages"] = [];
  const emitted = new Set<ChatThread["messages"][number]>();
  for (const message of messages) {
    if (message.role === "assistant") {
      const user = usersByCommand.get(message.command_id) || (message.backend_message_id?.endsWith(":assistant")
        ? usersByBackend.get(message.backend_message_id.slice(0, -10) + ":user") : undefined);
      if (user && !emitted.has(user)) { ordered.push(user); emitted.add(user); }
    }
    if (!emitted.has(message)) { ordered.push(message); emitted.add(message); }
  }
  return { ...thread, messages: ordered, imported: false, message_count: ordered.length,
    updated_ts: Math.max(thread.updated_ts || 0, command.updated_at*1000) };
}
