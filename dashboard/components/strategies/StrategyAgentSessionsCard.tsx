"use client";

import Link from "next/link";
import { useCallback, useEffect, useMemo, useState } from "react";

import {
  clientApi,
  type AgentSession,
} from "../../lib/clientApi";
import type {
  AgentTaskArtifactsEnvelope,
  AgentTaskRow,
  AgentTaskTimelineEnvelope,
  AgentTasksEnvelope,
} from "../../lib/operatorTypes";
import type {
  StrategyHistoryEnvelope,
  StrategyRunRecord,
  StrategyWorkspaceEnvelope,
} from "../../lib/strategyTypes";
import type {
  AssistantMessage,
  ChatMessage,
  TurnPayload,
  UserMessage,
} from "../../lib/chat";
import { formatTime } from "../../lib/format";
import { Card, Empty, ErrorBanner, Json, Pill } from "../Page";
import { AssistantBubble, UserBubble } from "../chat/ChatMessage";

type TranscriptMessage = {
  message_id?: string;
  role: "user" | "assistant";
  content: string;
  turn_id?: string;
  ts?: string;
  meta?: Record<string, unknown>;
  turn?: Record<string, unknown> | null;
};

type SessionTranscriptEnvelope = {
  ok: boolean;
  session_id: string;
  strategy_id?: string | null;
  title?: string;
  created_at?: string;
  updated_at?: string;
  messages: TranscriptMessage[];
  count: number;
  error?: string;
};

type SessionCandidate = {
  id: string;
  title: string;
  source: string;
  status: string;
  createdAt?: string;
  updatedAt?: string;
  messageCount: number;
  turnCount: number;
  runCount: number;
  triggerCount: number;
  reviewCount: number;
  task?: AgentTaskRow;
};

type LedgerGroup = {
  name: string;
  count: number;
  rows: Array<Record<string, unknown>>;
};

const STATUS_TONE: Record<string, "ok" | "warn" | "danger" | "brand" | "neutral"> = {
  done: "ok",
  in_progress: "warn",
  failed: "danger",
  empty: "brand",
  ok: "ok",
  hold: "warn",
  error: "danger",
  submitted: "brand",
  filled: "ok",
};

function asRecord(value: unknown): Record<string, unknown> {
  return value && typeof value === "object" && !Array.isArray(value)
    ? (value as Record<string, unknown>)
    : {};
}

function parseTs(value: unknown): number {
  if (typeof value === "number" && Number.isFinite(value)) {
    return value > 10_000_000_000 ? value : value * 1000;
  }
  if (typeof value === "string" && value.trim()) {
    const n = Number(value);
    if (Number.isFinite(n)) return n > 10_000_000_000 ? n : n * 1000;
    const t = Date.parse(value);
    if (Number.isFinite(t)) return t;
  }
  return Date.now();
}

function collectSessionIds(value: unknown, out = new Set<string>()): Set<string> {
  if (!value || typeof value !== "object") return out;
  if (Array.isArray(value)) {
    for (const item of value) collectSessionIds(item, out);
    return out;
  }
  for (const [key, raw] of Object.entries(value as Record<string, unknown>)) {
    if (key === "session_id" && typeof raw === "string" && raw.trim()) {
      out.add(raw.trim());
      continue;
    }
    if (raw && typeof raw === "object") collectSessionIds(raw, out);
  }
  return out;
}

function rowMatchesSession(row: Record<string, unknown>, sessionId: string): boolean {
  if (!sessionId) return false;
  if (collectSessionIds(row).has(sessionId)) return true;
  try {
    return JSON.stringify(row).includes(sessionId);
  } catch {
    return false;
  }
}

function historyRows(history: StrategyHistoryEnvelope | null | undefined) {
  const rows: Array<{ ledger: string; row: Record<string, unknown> }> = [];
  for (const [ledger, body] of Object.entries(history?.ledgers ?? {})) {
    for (const raw of body.tail ?? []) {
      rows.push({ ledger, row: asRecord(raw) });
    }
  }
  return rows;
}

function ledgerGroupsForSession(
  history: StrategyHistoryEnvelope | null | undefined,
  sessionId: string,
): LedgerGroup[] {
  return Object.entries(history?.ledgers ?? {})
    .map(([name, body]) => {
      const rows = (body.tail ?? [])
        .map((row) => asRecord(row))
        .filter((row) => rowMatchesSession(row, sessionId));
      return { name, count: rows.length, rows };
    })
    .filter((group) => group.count > 0)
    .sort((a, b) => {
      const rank = (name: string) => {
        if (/trigger/i.test(name)) return 0;
        if (/agent|message|decision|skill|subagent/i.test(name)) return 1;
        if (/review|reflection|evolution|tuning|learning/i.test(name)) return 2;
        if (/order|fill|risk|pnl/i.test(name)) return 3;
        return 4;
      };
      return rank(a.name) - rank(b.name) || a.name.localeCompare(b.name);
    });
}

function previewRecord(value: unknown): string {
  const record = asRecord(value);
  const candidates = [
    record.kind,
    record.event,
    record.action,
    record.status,
    record.reason,
    record.message,
    record.summary,
    record.error,
  ];
  for (const item of candidates) {
    if (typeof item === "string" && item.trim()) return item.trim();
  }
  try {
    return JSON.stringify(value).slice(0, 180);
  } catch {
    return String(value).slice(0, 180);
  }
}

function candidateTitle(
  id: string,
  session?: AgentSession,
  task?: AgentTaskRow,
  run?: StrategyRunRecord,
): string {
  const metaTitle =
    typeof session?.meta?.title === "string" ? session.meta.title.trim() : "";
  if (metaTitle) return metaTitle;
  if (task?.title) return task.title;
  if (run) {
    const trigger = run.trigger_event_id ? ` · ${run.trigger_event_id}` : "";
    return `${run.status.toUpperCase()} run${trigger}`;
  }
  return `Session ${id.slice(0, 12)}`;
}

function mergeCandidate(
  map: Map<string, SessionCandidate>,
  next: Partial<SessionCandidate> & { id: string },
) {
  const prev = map.get(next.id);
  map.set(next.id, {
    id: next.id,
    title: next.title || prev?.title || `Session ${next.id.slice(0, 12)}`,
    source: next.source || prev?.source || "runtime",
    status: next.status || prev?.status || "done",
    createdAt: next.createdAt || prev?.createdAt,
    updatedAt: next.updatedAt || prev?.updatedAt,
    messageCount: Math.max(next.messageCount ?? 0, prev?.messageCount ?? 0),
    turnCount: Math.max(next.turnCount ?? 0, prev?.turnCount ?? 0),
    runCount: (prev?.runCount ?? 0) + (next.runCount ?? 0),
    triggerCount: (prev?.triggerCount ?? 0) + (next.triggerCount ?? 0),
    reviewCount: (prev?.reviewCount ?? 0) + (next.reviewCount ?? 0),
    task: next.task || prev?.task,
  });
}

function buildCandidates(
  strategyId: string,
  workspace: StrategyWorkspaceEnvelope | null,
  sessions: AgentSession[],
  tasks: AgentTaskRow[],
): SessionCandidate[] {
  const map = new Map<string, SessionCandidate>();
  const runs = workspace?.runs?.runs ?? [];
  const history = workspace?.history ?? null;
  const historyBySession = new Map<string, { triggers: number; reviews: number }>();

  for (const { ledger, row } of historyRows(history)) {
    const sessionIds = collectSessionIds(row);
    for (const id of sessionIds) {
      const prev = historyBySession.get(id) ?? { triggers: 0, reviews: 0 };
      if (/trigger/i.test(ledger)) prev.triggers += 1;
      if (/review|reflection|learning|evolution|tuning/i.test(ledger)) {
        prev.reviews += 1;
      }
      historyBySession.set(id, prev);
    }
  }

  for (const session of sessions) {
    const id = String(session.session_id || "").trim();
    if (!id) continue;
    const counts = historyBySession.get(id);
    mergeCandidate(map, {
      id,
      title: candidateTitle(id, session),
      source: session.source || String(session.meta?.kind || "agent_session"),
      status: "done",
      createdAt: session.created_at,
      updatedAt: session.updated_at,
      messageCount: Number(session.message_count || 0),
      turnCount: Array.isArray(session.turn_ids) ? session.turn_ids.length : 0,
      triggerCount: counts?.triggers ?? 0,
      reviewCount: counts?.reviews ?? 0,
    });
  }

  for (const task of tasks) {
    const id = String(task.id || "").trim();
    if (!id) continue;
    const counts = historyBySession.get(id);
    mergeCandidate(map, {
      id,
      title: candidateTitle(id, undefined, task),
      source: task.meta?.source ? String(task.meta.source) : "agent_task",
      status: task.status,
      createdAt: task.created_at,
      updatedAt: task.updated_at,
      messageCount: Math.max(0, task.turn_count * 2),
      turnCount: task.turn_count,
      triggerCount: counts?.triggers ?? 0,
      reviewCount: counts?.reviews ?? 0,
      task,
    });
  }

  for (const run of runs) {
    const id = String(run.session_id || "").trim();
    if (!id) continue;
    const counts = historyBySession.get(id);
    mergeCandidate(map, {
      id,
      title: candidateTitle(id, undefined, undefined, run),
      source: run.trigger_event_id ? "strategy_trigger" : "strategy_run",
      status: run.status,
      createdAt: run.started_at,
      updatedAt: run.finished_at || run.started_at,
      messageCount: 0,
      turnCount: 1,
      runCount: 1,
      triggerCount: counts?.triggers ?? (run.trigger_event_id ? 1 : 0),
      reviewCount: counts?.reviews ?? 0,
    });
  }

  for (const [id, counts] of historyBySession.entries()) {
    if (!id) continue;
    mergeCandidate(map, {
      id,
      title: `Session ${id.slice(0, 12)}`,
      source: id.startsWith("sched:") ? "scheduled_trigger" : "history_ledger",
      status: "done",
      triggerCount: counts.triggers,
      reviewCount: counts.reviews,
    });
  }

  return [...map.values()]
    .filter((row) => row.id)
    .sort((a, b) => parseTs(b.updatedAt || b.createdAt) - parseTs(a.updatedAt || a.createdAt));
}

function transcriptToMessages(transcript: SessionTranscriptEnvelope | null): ChatMessage[] {
  if (!transcript?.messages?.length) return [];
  return transcript.messages.map((row, index): ChatMessage => {
    const id = row.message_id || `${transcript.session_id}:${index}`;
    const ts = parseTs(row.ts);
    if (row.role === "user") {
      return {
        id,
        role: "user",
        ts,
        text: row.content || "",
        backend_message_id: row.message_id,
      } satisfies UserMessage;
    }
    const turn = {
      ...(asRecord(row.turn) as TurnPayload),
    };
    if (!turn.reply_text && !turn.decision && row.content) {
      turn.reply_text = row.content;
    }
    if (row.turn_id && !turn.turn_id) {
      turn.turn_id = row.turn_id;
    }
    return {
      id,
      role: "assistant",
      ts,
      backend_message_id: row.message_id,
      turn,
    } satisfies AssistantMessage;
  });
}

function runFallbackMessages(
  workspace: StrategyWorkspaceEnvelope | null,
  sessionId: string,
): ChatMessage[] {
  if (!sessionId) return [];
  const runs = (workspace?.runs?.runs ?? []).filter(
    (run) => String(run.session_id || "") === sessionId,
  );
  return runs.flatMap((run, index): ChatMessage[] => {
    const started = parseTs(run.started_at);
    const finished = parseTs(run.finished_at || run.started_at);
    const inputText = [
      `Trigger ${run.trigger_event_id || "manual"}`,
      "",
      "```json",
      JSON.stringify(run.inputs ?? {}, null, 2),
      "```",
    ].join("\n");
    const outputText = [
      `Status: ${String(run.status).toUpperCase()}`,
      run.reason ? `Reason: ${run.reason}` : "",
      run.duration_ms ? `Duration: ${run.duration_ms}ms` : "",
      "",
      "Outputs",
      "```json",
      JSON.stringify(run.outputs ?? {}, null, 2),
      "```",
    ].filter((line) => line !== "").join("\n");
    const user: UserMessage = {
      id: `${run.run_id}:input:${index}`,
      role: "user",
      ts: started,
      text: inputText,
    };
    const assistant: AssistantMessage = {
      id: `${run.run_id}:output:${index}`,
      role: "assistant",
      ts: finished,
      turn: {
        reply_text: outputText,
        turn_id: run.run_id,
        trigger_event_id: run.trigger_event_id,
        actions: (run.audit ?? []).map((entry) => ({
          action: String(entry.kind || "audit"),
          result: entry,
        })),
      },
      elapsed_ms: run.duration_ms,
    };
    return [user, assistant];
  });
}

function sourceLabel(candidate: SessionCandidate): string {
  if (candidate.id.startsWith("sched:")) return "scheduled";
  if (/trigger/i.test(candidate.source)) return "trigger";
  if (/tuning|review|reflection|learning|evolution/i.test(candidate.source)) {
    return "reflection";
  }
  if (candidate.task) return "agent task";
  return candidate.source.replace(/_/g, " ");
}

function SessionListItem({
  item,
  active,
  onClick,
}: {
  item: SessionCandidate;
  active: boolean;
  onClick: () => void;
}) {
  const tone = STATUS_TONE[item.status] ?? "neutral";
  return (
    <button
      type="button"
      onClick={onClick}
      className={[
        "w-full border-b border-brand-500/5 px-3 py-3 text-left last:border-b-0 hover:bg-brand-500/5",
        active ? "bg-brand-500/10" : "",
      ].join(" ")}
    >
      <div className="flex items-center gap-2">
        <Pill tone={tone}>{item.status.replace("_", " ")}</Pill>
        <span className="min-w-0 flex-1 truncate text-[12px] font-medium text-ink-100">
          {item.title}
        </span>
      </div>
      <div className="mt-1 flex items-center gap-2 text-[10.5px] text-ink-500">
        <span className="font-mono">{item.id}</span>
        <span>·</span>
        <span>{sourceLabel(item)}</span>
        <span className="ml-auto">{formatTime(item.updatedAt || item.createdAt || "")}</span>
      </div>
      <div className="mt-2 flex flex-wrap gap-1.5 text-[10px]">
        <span className="rounded border border-brand-500/10 px-1.5 py-0.5 text-ink-400">
          {item.turnCount} turns
        </span>
        <span className="rounded border border-brand-500/10 px-1.5 py-0.5 text-ink-400">
          {item.triggerCount} triggers
        </span>
        <span className="rounded border border-brand-500/10 px-1.5 py-0.5 text-ink-400">
          {item.reviewCount} reflections
        </span>
      </div>
    </button>
  );
}

function TranscriptPane({
  transcript,
  loading,
  fallbackMessages,
}: {
  transcript: SessionTranscriptEnvelope | null;
  loading: boolean;
  fallbackMessages: ChatMessage[];
}) {
  const messages = useMemo(() => {
    const fromTranscript = transcriptToMessages(transcript);
    return fromTranscript.length ? fromTranscript : fallbackMessages;
  }, [transcript, fallbackMessages]);

  if (loading && !transcript) {
    return <Empty label="Loading transcript..." />;
  }
  if (!messages.length) {
    return (
      <Empty
        title="No chat transcript"
        subtitle="This session may only have ledger events or runtime trace rows."
      />
    );
  }

  return (
    <div className="embedded-scroll max-h-[640px] space-y-4 rounded-lg border border-brand-500/10 bg-ink-950/25 px-3 py-4">
      {messages.map((msg) =>
        msg.role === "user" ? (
          <UserBubble key={msg.id} msg={msg} />
        ) : (
          <AssistantBubble key={msg.id} msg={msg} />
        ),
      )}
    </div>
  );
}

function TimelinePane({
  timeline,
}: {
  timeline: AgentTaskTimelineEnvelope | null;
}) {
  const events = timeline?.data?.events ?? [];
  if (!events.length) {
    return <Empty label="No trace events for this session." />;
  }
  return (
    <ol className="embedded-list-scroll-lg space-y-1">
      {events.slice(-200).map((event, index) => (
        <li
          key={`${event.surface}:${index}`}
          className="flex gap-2 text-[11px] font-mono text-ink-200"
        >
          <span className="w-32 shrink-0 text-ink-500">
            {formatTime(event.ts || "")}
          </span>
          <span className="w-32 shrink-0 text-brand-300">
            {event.surface}
          </span>
          <span className="min-w-0 flex-1 truncate text-ink-300">
            {previewRecord(event.record)}
          </span>
        </li>
      ))}
    </ol>
  );
}

function LedgerPane({ groups }: { groups: LedgerGroup[] }) {
  if (!groups.length) {
    return <Empty label="No strategy ledgers matched this session." />;
  }
  return (
    <div className="embedded-list-scroll-lg space-y-3">
      {groups.map((group) => (
        <div
          key={group.name}
          className="rounded-lg border border-brand-500/10 bg-ink-950/25 px-3 py-2"
        >
          <div className="mb-2 flex items-center justify-between gap-2">
            <div className="text-[12px] font-medium text-ink-300">
              {group.name}
            </div>
            <Pill tone={/trigger/i.test(group.name) ? "brand" : /review|reflection|learning|tuning/i.test(group.name) ? "warn" : "neutral"}>
              {group.count}
            </Pill>
          </div>
          <div className="space-y-2">
            {group.rows.slice(-12).map((row, index) => (
              <details
                key={`${group.name}:${index}`}
                className="rounded-md border border-brand-500/10 bg-ink-900/35 px-2 py-1.5"
              >
                <summary className="cursor-pointer text-[11px] text-ink-200">
                  {previewRecord(row)}
                </summary>
                <div className="mt-2">
                  <Json value={row} />
                </div>
              </details>
            ))}
          </div>
        </div>
      ))}
    </div>
  );
}

function ArtifactsPane({
  artifacts,
}: {
  artifacts: AgentTaskArtifactsEnvelope | null;
}) {
  const data = artifacts?.data?.artifacts;
  if (!data) return <Empty label="No artifact summary available." />;
  const rows = [
    ...data.files.map((item) => ({ kind: "file", text: `${item.action} · ${item.path}` })),
    ...data.messages.map((item) => ({ kind: "message", text: `${item.channel ?? "message"} · ${item.text}` })),
    ...data.orders.map((item) => ({ kind: "order", text: `${item.action} · ${item.symbol ?? "?"} ${item.side ?? ""} ${item.quantity ?? ""}` })),
    ...data.created.map((item) => ({ kind: "created", text: `${item.action} · ${previewRecord(item.result)}` })),
    ...data.memory.map((item) => ({ kind: "memory", text: `${item.key ?? "memory"} · ${item.summary}` })),
  ];
  if (!rows.length) return <Empty label="No files, messages, memory writes, or orders were mined from the trace." />;
  return (
    <ul className="embedded-list-scroll-sm space-y-1">
      {rows.map((row, index) => (
        <li
          key={`${row.kind}:${index}`}
          className="flex gap-2 rounded-md border border-brand-500/10 bg-ink-950/25 px-2 py-1.5 text-[11px]"
        >
          <span className="w-16 shrink-0 text-brand-300">
            {row.kind}
          </span>
          <span className="min-w-0 flex-1 truncate text-ink-200">
            {row.text}
          </span>
        </li>
      ))}
    </ul>
  );
}

export function StrategyAgentSessionsCard({
  strategyId,
  workspace,
}: {
  strategyId: string;
  workspace: StrategyWorkspaceEnvelope | null;
}) {
  const [sessions, setSessions] = useState<AgentSession[]>([]);
  const [tasksEnv, setTasksEnv] = useState<AgentTasksEnvelope | null>(null);
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [transcript, setTranscript] = useState<SessionTranscriptEnvelope | null>(null);
  const [timeline, setTimeline] = useState<AgentTaskTimelineEnvelope | null>(null);
  const [artifacts, setArtifacts] = useState<AgentTaskArtifactsEnvelope | null>(null);
  const [loading, setLoading] = useState(true);
  const [loadingSession, setLoadingSession] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [activeView, setActiveView] = useState<"chat" | "trace" | "ledgers" | "artifacts">("chat");

  const load = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const [sessionRes, taskRes] = await Promise.all([
        clientApi.sessionList(strategyId, 100, { include: "all" }),
        clientApi.agentTasks({ strategy_id: strategyId, limit: 100 }),
      ]);
      setSessions(sessionRes.sessions ?? []);
      setTasksEnv(taskRes);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setLoading(false);
    }
  }, [strategyId]);

  useEffect(() => {
    void load();
  }, [load]);

  const candidates = useMemo(
    () =>
      buildCandidates(
        strategyId,
        workspace,
        sessions,
        tasksEnv?.data?.tasks ?? [],
      ),
    [strategyId, workspace, sessions, tasksEnv],
  );

  useEffect(() => {
    if (selectedId && candidates.some((item) => item.id === selectedId)) return;
    setSelectedId(candidates[0]?.id ?? null);
  }, [candidates, selectedId]);

  const selected = useMemo(
    () => candidates.find((item) => item.id === selectedId) ?? null,
    [candidates, selectedId],
  );

  useEffect(() => {
    if (!selectedId) {
      setTranscript(null);
      setTimeline(null);
      setArtifacts(null);
      return;
    }
    const sid = selectedId;
    let cancelled = false;
    async function loadSelected() {
      setLoadingSession(true);
      setError(null);
      try {
        const [nextTranscript, nextTimeline, nextArtifacts] = await Promise.all([
          clientApi.sessionTranscript(sid, {
            full: true,
            per_msg_cap: 24_000,
          }) as Promise<SessionTranscriptEnvelope>,
          clientApi.agentTaskTimeline(sid).catch(() => null),
          clientApi.agentTaskArtifacts(sid).catch(() => null),
        ]);
        if (cancelled) return;
        setTranscript(nextTranscript);
        setTimeline(nextTimeline);
        setArtifacts(nextArtifacts);
      } catch (e) {
        if (cancelled) return;
        setError(e instanceof Error ? e.message : String(e));
        setTranscript(null);
        setTimeline(null);
        setArtifacts(null);
      } finally {
        if (!cancelled) setLoadingSession(false);
      }
    }
    void loadSelected();
    return () => {
      cancelled = true;
    };
  }, [selectedId]);

  const selectedLedgers = useMemo(
    () => ledgerGroupsForSession(workspace?.history ?? null, selectedId ?? ""),
    [workspace, selectedId],
  );
  const fallbackMessages = useMemo(
    () => runFallbackMessages(workspace, selectedId ?? ""),
    [workspace, selectedId],
  );

  return (
    <Card
      title="Agent sessions"
      description="Read-only strategy Agent Workspace: trigger runs, scheduled sessions, reflections, turn input/output, tools, and trace."
      actions={
        <div className="flex items-center gap-2">
          {selectedId ? (
            <Link
              href={`/chat/${encodeURIComponent(selectedId)}`}
              className="btn-ghost text-xs"
            >
              Open in chat
            </Link>
          ) : null}
          <button
            type="button"
            onClick={() => void load()}
            disabled={loading}
            className="btn-ghost text-xs"
          >
            {loading ? "Refreshing..." : "Refresh"}
          </button>
        </div>
      }
    >
      <div className="space-y-4">
        {error ? <ErrorBanner error={error} /> : null}

        <div className="grid grid-cols-1 gap-4 xl:grid-cols-[340px_minmax(0,1fr)]">
          <div className="rounded-lg border border-brand-500/10 bg-ink-950/20">
            <div className="border-b border-brand-500/10 px-3 py-2">
              <div className="text-[12px] font-medium text-ink-300">
                Sessions
              </div>
              <div className="mt-1 text-[11px] text-ink-500">
                {candidates.length} strategy-scoped sessions
              </div>
            </div>
            {loading && candidates.length === 0 ? (
              <Empty label="Loading sessions..." />
            ) : candidates.length === 0 ? (
              <Empty
                title="No strategy sessions"
                subtitle="Runs, trigger ledgers, and agent tasks will appear here after the strategy executes."
              />
            ) : (
              <div className="embedded-list-scroll-lg">
                {candidates.map((item) => (
                  <SessionListItem
                    key={item.id}
                    item={item}
                    active={item.id === selectedId}
                    onClick={() => setSelectedId(item.id)}
                  />
                ))}
              </div>
            )}
          </div>

          <div className="min-w-0 space-y-4">
            {selected ? (
              <>
                <div className="rounded-lg border border-brand-500/10 bg-ink-950/20 px-3 py-3">
                  <div className="flex flex-wrap items-start justify-between gap-3">
                    <div className="min-w-0">
                      <div className="text-[13px] font-semibold text-ink-100">
                        {selected.title}
                      </div>
                      <div className="mt-1 flex flex-wrap items-center gap-2 text-[11px] text-ink-500">
                        <span className="font-mono">{selected.id}</span>
                        <span>source: {sourceLabel(selected)}</span>
                        <span>updated: {formatTime(selected.updatedAt || selected.createdAt || "")}</span>
                      </div>
                    </div>
                    <div className="flex flex-wrap gap-1.5">
                      <Pill tone={STATUS_TONE[selected.status] ?? "neutral"}>
                        {selected.status.replace("_", " ")}
                      </Pill>
                      <Pill tone="brand">{selected.triggerCount} triggers</Pill>
                      <Pill tone="warn">{selected.reviewCount} reflections</Pill>
                    </div>
                  </div>
                  <div className="mt-3 flex flex-wrap gap-1.5">
                    {(["chat", "trace", "ledgers", "artifacts"] as const).map((view) => (
                      <button
                        key={view}
                        type="button"
                        onClick={() => setActiveView(view)}
                        className={[
                          "rounded-md border px-2.5 py-1 text-[11px] transition-colors",
                          activeView === view
                            ? "border-brand-500/50 bg-brand-500/20 text-white"
                            : "border-brand-500/15 text-ink-300 hover:bg-brand-500/10",
                        ].join(" ")}
                      >
                        {view === "chat"
                          ? "Chat transcript"
                          : view === "trace"
                            ? "Agent trace"
                            : view === "ledgers"
                              ? "Triggers / reflections"
                              : "Artifacts"}
                      </button>
                    ))}
                  </div>
                </div>

                {activeView === "chat" ? (
                  <TranscriptPane
                    transcript={transcript}
                    loading={loadingSession}
                    fallbackMessages={fallbackMessages}
                  />
                ) : null}
                {activeView === "trace" ? <TimelinePane timeline={timeline} /> : null}
                {activeView === "ledgers" ? <LedgerPane groups={selectedLedgers} /> : null}
                {activeView === "artifacts" ? <ArtifactsPane artifacts={artifacts} /> : null}
              </>
            ) : (
              <Empty label="Select a session to inspect the Agent path." />
            )}
          </div>
        </div>
      </div>
    </Card>
  );
}
