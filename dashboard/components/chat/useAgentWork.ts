"use client";

import { useCallback, useEffect, useMemo, useState } from "react";
import { callApi } from "../../lib/clientApi";
import type { ChatThread } from "../../lib/chat";
import { liveEventsToBlocks } from "../../lib/chat";
import { collectAgentSegments, collectLiveAgentSegments, type MemberStep } from "./TurnBlocks";

export type AgentOperation = { seq: number; kind: string; ts: number; data: Record<string, unknown> };
export type AgentMail = {
  id: string; sender: string; recipient: string; content: string;
  status: "queued" | "delivered" | "consumed"; ts: number;
};
export type AgentWork = {
  id: string; session_id: string; group_id: string; parent_call_id: string;
  name: string; title: string; state: string; attempt: number; updated_at: number;
  output?: unknown; error?: string; activity?: Record<string, unknown>;
  pending_messages?: number; legacy?: boolean; legacySteps?: MemberStep[];
  context?: {
    parent_session_id: string; scope: string; inherited_messages: number;
    saved_messages: number; allowed_skills: string[]; model: string;
  };
};
export type AgentDetail = {
  ok: boolean; agent: AgentWork; events: AgentOperation[];
  messages: AgentMail[]; has_more: boolean;
};

export async function agentRequest<T>(path: string, body?: unknown, signal?: AbortSignal): Promise<T> {
  const result = await callApi<T & { ok?: boolean; error?: string }>(path, {
    method: body === undefined ? "GET" : "POST", body, signal,
  });
  if (!result || result.ok === false) throw new Error(result?.error || "Agent service unavailable");
  return result;
}

export function isWorking(state: string): boolean {
  return ["running", "planned", "queued", "pending"].includes(state);
}

/** Historical runs remain inspectable, but never acquire a fabricated resumable id. */
export function historicalAgentWork(thread: ChatThread | null): AgentWork[] {
  if (!thread) return [];
  const rows = new Map<string, AgentWork>();
  for (const message of thread.messages) {
    if (message.role !== "assistant") continue;
    const live = collectLiveAgentSegments(liveEventsToBlocks([
      ...(message.turn?.activity_events || []), ...(message.live_events || []),
    ])).segments;
    const committed = collectAgentSegments(message.turn).segments;
    for (const segment of [...live, ...committed]) {
      const group = segment.runId || segment.callId || message.id;
      for (const member of segment.members) {
        const key = `${group}:${member.name}`;
        const prev = rows.get(key);
        rows.set(key, {
          id: `history:${key}`, session_id: thread.id, group_id: group,
          parent_call_id: segment.callId, name: member.name,
          title: segment.task || prev?.title || thread.title,
          state: member.status, attempt: 1, updated_at: (message.ts || 0) / 1000,
          output: member.output ?? prev?.output, error: member.error,
          activity: { action: member.currentActivity || "" }, legacy: true,
          legacySteps: member.steps?.length ? member.steps : prev?.legacySteps,
        });
      }
    }
    if (!live.length && !committed.length) {
      for (const [name, output] of Object.entries(message.turn?.subagents || {})) {
        const key = `${message.id}:${name}`;
        rows.set(key, {
          id: `history:${key}`, session_id: thread.id, group_id: message.id,
          parent_call_id: "", name, title: thread.title, state: "completed",
          attempt: 1, updated_at: (message.ts || 0) / 1000, output, legacy: true,
        });
      }
    }
  }
  return [...rows.values()];
}

export function mergeAgentWork(durable: AgentWork[], history: AgentWork[]): AgentWork[] {
  const archived = history.filter((row) => !durable.some((item) => item.name === row.name && (
    item.group_id === row.group_id || (item.parent_call_id && item.parent_call_id === row.parent_call_id)
  )));
  return [...durable, ...archived].sort((a, b) =>
    Number(isWorking(b.state)) - Number(isWorking(a.state)) || b.updated_at - a.updated_at || a.id.localeCompare(b.id));
}

export function useAgentWork(thread: ChatThread | null) {
  const sessionId = thread?.id || "";
  const loading = Boolean(thread?.messages.some((m) => m.role === "assistant" && m.loading));
  const [snapshot, setSnapshot] = useState<{ session: string; rows: AgentWork[]; error: string }>({ session: "", rows: [], error: "" });
  const [refreshKey, setRefreshKey] = useState(0);
  const refresh = useCallback(() => setRefreshKey((n) => n + 1), []);
  useEffect(() => {
    if (!sessionId) return;
    let cancelled = false;
    let timer: ReturnType<typeof setTimeout>;
    const controller = new AbortController();
    async function poll() {
      let active = loading;
      try {
        const response = await agentRequest<{ agents: AgentWork[] }>(
          `/teams/agents?session_id=${encodeURIComponent(sessionId)}`, undefined, controller.signal);
        if (!Array.isArray(response.agents)) throw new Error("Agent service requires a runtime update");
        if (cancelled) return;
        const rows = response.agents.filter((a) => a.session_id === sessionId);
        active ||= rows.some((a) => isWorking(a.state));
        setSnapshot({ session: sessionId, rows, error: "" });
      } catch (error) {
        if (cancelled) return;
        setSnapshot((old) => ({ session: sessionId, rows: old.session === sessionId ? old.rows : [],
          error: error instanceof Error ? error.message : String(error) }));
      }
      if (!cancelled) timer = setTimeout(poll, document.hidden ? 15000 : active ? 1000 : 4000);
    }
    void poll();
    return () => { cancelled = true; controller.abort(); clearTimeout(timer); };
  }, [sessionId, loading, refreshKey]);
  const history = useMemo(() => historicalAgentWork(thread), [thread]);
  const rows = useMemo(() => mergeAgentWork(snapshot.session === sessionId ? snapshot.rows : [], history), [snapshot, sessionId, history]);
  return { rows, sessionId, error: snapshot.session === sessionId ? snapshot.error : "", refresh };
}

export type AgentWorkSource = ReturnType<typeof useAgentWork>;
