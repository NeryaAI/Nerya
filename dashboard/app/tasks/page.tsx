"use client";

import Link from "next/link";
import { useTranslations } from "next-intl";
import { useCallback, useEffect, useMemo, useState } from "react";
import {
  Advanced,
  Card,
  Empty,
  ErrorBanner,
  Json,
  PageBody,
  PageHeader,
  Pill,
} from "../../components/Page";
import { SectionTabs } from "../../components/SectionTabs";
import { PlusIcon, SendIcon, WrenchIcon, XIcon } from "../../components/icons";
import { clientApi } from "../../lib/clientApi";
import { authHeaders } from "../../lib/auth";
import { formatTime } from "../../lib/format";
import type {
  AgentTaskArtifactsEnvelope,
  AgentTaskRow,
  AgentTaskStatus,
  AgentTaskTimelineEnvelope,
  AgentTasksEnvelope,
  EnvelopeSeverity,
} from "../../lib/operatorTypes";

const STATUS_TONE: Record<AgentTaskStatus, "ok" | "warn" | "danger" | "brand"> = {
  done: "ok",
  in_progress: "warn",
  failed: "danger",
  empty: "brand",
};

export default function AgentTasksPage() {
  const t = useTranslations("tasks");
  const tCommon = useTranslations("common");
  const STATUS_FILTERS: { id: AgentTaskStatus | "all"; label: string }[] = [
    { id: "all", label: t("filterAll") },
    { id: "in_progress", label: t("filterInProgress") },
    { id: "failed", label: t("filterFailed") },
    { id: "done", label: t("filterDone") },
  ];

  const [env, setEnv] = useState<AgentTasksEnvelope | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [filter, setFilter] = useState<AgentTaskStatus | "all">("all");
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [timeline, setTimeline] = useState<AgentTaskTimelineEnvelope | null>(null);
  const [artifacts, setArtifacts] = useState<AgentTaskArtifactsEnvelope | null>(null);
  const [busy, setBusy] = useState<string | null>(null);
  const [creating, setCreating] = useState(false);
  const [taskPrompt, setTaskPrompt] = useState("");
  const [info, setInfo] = useState<string | null>(null);

  const load = useCallback(async () => {
    try {
      const next = await clientApi.agentTasks({
        status: filter === "all" ? undefined : filter,
        limit: 100,
      });
      setEnv(next);
      setError(null);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setLoading(false);
    }
  }, [filter]);

  useEffect(() => {
    setLoading(true);
    load();
  }, [load]);

  useEffect(() => {
    const t = setInterval(load, 30_000);
    return () => clearInterval(t);
  }, [load]);

  const tasks = env?.data?.tasks ?? [];
  const selected: AgentTaskRow | null = useMemo(
    () => tasks.find((t) => t.id === selectedId) ?? tasks[0] ?? null,
    [tasks, selectedId],
  );

  // Pull timeline + artifacts whenever the selection changes.
  useEffect(() => {
    if (!selected) {
      setTimeline(null);
      setArtifacts(null);
      return;
    }
    let cancelled = false;
    async function load() {
      try {
        const [tl, ar] = await Promise.all([
          clientApi.agentTaskTimeline(selected!.id),
          clientApi.agentTaskArtifacts(selected!.id),
        ]);
        if (cancelled) return;
        setTimeline(tl);
        setArtifacts(ar);
      } catch (e) {
        if (cancelled) return;
        setError(e instanceof Error ? e.message : String(e));
      }
    }
    load();
    return () => {
      cancelled = true;
    };
  }, [selected]);

  async function cancelTask(task: AgentTaskRow) {
    setBusy(task.id);
    try {
      await clientApi.agentTaskCancel(task.id, "operator_cancel");
      await load();
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(null);
    }
  }

  async function resumeTask(task: AgentTaskRow) {
    setBusy(task.id);
    try {
      const result = await clientApi.agentTaskResume(task.id);
      if (result.primary_action?.method === "POST" && result.primary_action.href) {
        await fetch(`/api/proxy${result.primary_action.href}`, {
          method: "POST",
          headers: authHeaders({ "content-type": "application/json" }),
          body: JSON.stringify(result.primary_action.body || {}),
        });
      }
      await load();
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(null);
    }
  }

  async function createTask() {
    const text = taskPrompt.trim();
    if (!text) {
      setError(t("taskPromptRequired"));
      return;
    }
    const sessionId = `task_${Date.now().toString(36)}`;
    setBusy("new");
    try {
      const result = await clientApi.agentRunTurn({
        source: "dashboard_task",
        kind: "user.task",
        target: "main",
        payload: {
          text,
          channel: "dashboard",
          title: text.slice(0, 96),
        },
        session_id: sessionId,
      });
      setCreating(false);
      setTaskPrompt("");
      setSelectedId(sessionId);
      setInfo(t("createdInfo", { sessionId, turnSuffix: result.turn_id ? ` · turn ${String(result.turn_id)}` : "" }));
      await load();
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(null);
    }
  }

  const counts = env?.data?.counts ?? {
    in_progress: 0,
    failed: 0,
    done: 0,
    empty: 0,
  };

  return (
    <div>
      {error ? <ErrorBanner error={error} /> : null}
      <PageBody>
        <PageHeader
          eyebrow={t("eyebrow")}
          title={t("title")}
          description={
            env?.summary ||
            t("description")
          }
          actions={
            <div className="flex items-center gap-2">
              <Pill tone="warn">{t("inProgressCount", { count: counts.in_progress })}</Pill>
              {counts.failed > 0 ? (
                <Pill tone="danger">{t("failedCount", { count: counts.failed })}</Pill>
              ) : null}
              <button
                type="button"
                onClick={() => setCreating(true)}
                className="btn btn-primary"
              >
                <PlusIcon size={14} />
                {t("newTask")}
              </button>
              <Link
                href="/workflows"
                className="btn btn-ghost"
              >
                {t("schedules")}
              </Link>
              <button
                onClick={load}
                className="btn btn-ghost"
              >
                <WrenchIcon size={14} />
                {tCommon("refresh")}
              </button>
            </div>
          }
        />
        <SectionTabs section="runtime" />

        <div className="rounded-lg border border-brand-500/15 bg-ink-950/30 px-3 py-2 text-[12px] text-ink-300">
          {t("taskExplainer")}
        </div>

        {info ? (
          <div className="rounded-lg border border-emerald-400/30 bg-emerald-500/10 px-3 py-2 text-[12px] text-emerald-200">
            {info}
          </div>
        ) : null}

        <div className="flex flex-wrap items-center gap-2 border-b border-brand-500/10 pb-3">
          {STATUS_FILTERS.map((opt) => (
            <button
              key={opt.id}
              onClick={() => setFilter(opt.id)}
              className={`text-[12px] px-2.5 py-1 rounded-md border transition ${
                filter === opt.id
                  ? "bg-brand-500/15 text-brand-100 border-brand-500/40"
                  : "text-ink-400 border-transparent hover:text-ink-200 hover:border-brand-500/20"
              }`}
            >
              {opt.label}
            </button>
          ))}
        </div>

        <div className="grid grid-cols-1 xl:grid-cols-3 gap-4">
          <Card
            title={t("tasksCount", { count: tasks.length })}
            description={
              filter === "all" ? t("allSessions") : t("filterLabel", { filter })
            }
            padded={false}
          >
            {loading && tasks.length === 0 ? (
              <div className="p-4 text-[12px] text-ink-500">{tCommon("loading")}</div>
            ) : tasks.length === 0 ? (
              <Empty label={t("noTasks")} />
            ) : (
              <ul className="embedded-list-scroll-lg">
                {tasks.map((task) => (
                  <li
                    key={task.id}
                    onClick={() => setSelectedId(task.id)}
                    className={`px-3 py-2.5 border-b border-brand-500/5 last:border-b-0 cursor-pointer hover:bg-brand-500/5 ${
                      (selected?.id ?? tasks[0]?.id) === task.id
                        ? "bg-brand-500/10"
                        : ""
                    }`}
                  >
                    <div className="flex items-center gap-2">
                      <span
                        className={`w-2 h-2 rounded-full ${dotForSeverity(task.severity)}`}
                      />
                      <Pill tone={STATUS_TONE[task.status]}>
                        {task.status.replace("_", " ")}
                      </Pill>
                      <span className="text-[12px] text-ink-100 truncate flex-1">
                        {task.title}
                      </span>
                    </div>
                    <div className="text-[10.5px] text-ink-500 mt-1 flex items-center gap-2">
                      <span className="font-mono">{task.id}</span>
                      <span>·</span>
                      <span>{t("turnCount", { count: task.turn_count })}</span>
                      {task.strategy_id ? (
                        <>
                          <span>·</span>
                          <span className="font-mono">{task.strategy_id}</span>
                        </>
                      ) : null}
                      <span className="ml-auto">{formatTime(task.updated_at)}</span>
                    </div>
                  </li>
                ))}
              </ul>
            )}
          </Card>

          <div className="xl:col-span-2 space-y-4">
            {selected ? (
              <>
                <Card
                  title={selected.title}
                  description={t("taskId", { id: selected.id })}
                  actions={
                    <div className="flex items-center gap-2">
                      <Pill tone={STATUS_TONE[selected.status]}>
                        {selected.status.replace("_", " ")}
                      </Pill>
                      {selected.status === "in_progress" ? (
                        <button
                          disabled={busy === selected.id}
                          onClick={() => cancelTask(selected)}
                          className="text-[11px] px-2 py-0.5 rounded-md border border-rose-500/40 text-rose-200 hover:bg-rose-500/10 disabled:opacity-50"
                        >
                          {tCommon("cancel")}
                        </button>
                      ) : null}
                      {selected.status === "failed" ? (
                        <button
                          disabled={busy === selected.id}
                          onClick={() => resumeTask(selected)}
                          className="text-[11px] px-2 py-0.5 rounded-md border border-amber-400/40 text-amber-200 hover:bg-amber-400/10 disabled:opacity-50"
                        >
                          {t("resume")}
                        </button>
                      ) : null}
                      <Link
                        href={`/chat?session_id=${encodeURIComponent(
                          selected.id,
                        )}`}
                        className="text-[11px] px-2 py-0.5 rounded-md border border-brand-500/25 text-brand-200 hover:bg-brand-500/10"
                      >
                        {t("openInChat")}
                      </Link>
                    </div>
                  }
                >
                  <div className="grid grid-cols-2 md:grid-cols-4 gap-3 text-[11px]">
                    <Stat
                      label={t("statStrategy")}
                      value={selected.strategy_id || "—"}
                      mono
                    />
                    <Stat label={t("statTurns")} value={selected.turn_count} />
                    <Stat
                      label={t("statSkills")}
                      value={selected.skills_invoked.slice(0, 3).join(", ") || "—"}
                    />
                    <Stat
                      label={t("statLastAction")}
                      value={selected.last_action || "—"}
                      mono
                    />
                    <Stat label={t("statCreated")} value={formatTime(selected.created_at)} />
                    <Stat label={t("statUpdated")} value={formatTime(selected.updated_at)} />
                    <Stat
                      label={t("statActiveTurns")}
                      value={selected.active_turn_ids.length}
                    />
                    <Stat
                      label={t("statFailedTurns")}
                      value={selected.failed_turn_ids.length}
                    />
                  </div>
                </Card>

                <Card
                  title={t("artifacts")}
                  description={
                    artifacts
                      ? t("artifactsCounts", {
                          files: artifacts.data.counts.files,
                          messages: artifacts.data.counts.messages,
                          orders: artifacts.data.counts.orders,
                        })
                      : t("loadingLower")
                  }
                >
                  {artifacts ? (
                    <div className="space-y-3 text-[12px]">
                      {artifacts.data.artifacts.files.length > 0 ? (
                        <ArtifactGroup label={t("artifactFiles")} rows={artifacts.data.artifacts.files.map((f) => `${f.action} · ${f.path}`)} />
                      ) : null}
                      {artifacts.data.artifacts.messages.length > 0 ? (
                        <ArtifactGroup
                          label={t("artifactMessages")}
                          rows={artifacts.data.artifacts.messages.map(
                            (m) => `${m.channel ?? "?"} · ${m.text.slice(0, 80)}`,
                          )}
                        />
                      ) : null}
                      {artifacts.data.artifacts.orders.length > 0 ? (
                        <ArtifactGroup
                          label={t("artifactOrders")}
                          rows={artifacts.data.artifacts.orders.map(
                            (o) => `${o.action} · ${o.symbol ?? "?"} ${o.side ?? ""} ${o.quantity ?? ""}`,
                          )}
                        />
                      ) : null}
                      {artifacts.data.artifacts.created.length > 0 ? (
                        <ArtifactGroup
                          label={t("artifactCreated")}
                          rows={artifacts.data.artifacts.created.map(
                            (c) => `${c.action} · ${summarizeArtifactResult(c.result)}`,
                          )}
                        />
                      ) : null}
                      {artifacts.data.artifacts.memory.length > 0 ? (
                        <ArtifactGroup
                          label={t("artifactMemory")}
                          rows={artifacts.data.artifacts.memory.map(
                            (m) => `${m.key ?? "?"} · ${m.summary.slice(0, 80)}`,
                          )}
                        />
                      ) : null}
                      {Object.values(artifacts.data.counts).every((c) => c === 0) ? (
                        <Empty label={t("noArtifacts")} />
                      ) : null}
                    </div>
                  ) : (
                    <div className="text-[12px] text-ink-500">{t("loadingArtifacts")}</div>
                  )}
                </Card>

                <Card
                  title={t("timeline")}
                  description={
                    timeline
                      ? t("timelineCounts", {
                          events: timeline.data.events.length,
                          surfaces: timeline.data.surfaces.length,
                        })
                      : t("loadingLower")
                  }
                >
                  {timeline ? (
                    <ol className="embedded-list-scroll-lg space-y-1">
                      {timeline.data.events.slice(-200).map((ev, i) => (
                        <li
                          key={i}
                          className="text-[11px] font-mono text-ink-200 flex gap-2"
                        >
                          <span className="text-ink-500 w-32 shrink-0">
                            {formatTime(ev.ts || "")}
                          </span>
                          <span className="text-brand-300 w-32 shrink-0">
                            {ev.surface}
                          </span>
                          <span className="text-ink-300 truncate flex-1">
                            {previewRecord(ev.record)}
                          </span>
                        </li>
                      ))}
                    </ol>
                  ) : (
                    <div className="text-[12px] text-ink-500">
                      {t("loadingTimeline")}
                    </div>
                  )}
                </Card>

                <Advanced
                  title={t("rawTask")}
                  storageKey="nerya.tasks.advanced.raw"
                >
                  <Json value={selected} />
                </Advanced>
              </>
            ) : (
              <Card title={t("selectTask")}>
                <div className="text-[12px] text-ink-500">
                  {t("selectTaskHint")}
                </div>
              </Card>
            )}
          </div>
        </div>

        {creating ? (
          <div
            className="fixed inset-0 z-50 flex items-center justify-center bg-black/70 px-4 backdrop-blur-sm"
            onClick={(e) => {
              if (e.target === e.currentTarget) setCreating(false);
            }}
          >
            <div className="w-[680px] max-w-full rounded-xl border border-brand-500/20 bg-bg-card shadow-glow">
              <div className="flex items-start justify-between gap-4 border-b border-brand-500/10 px-5 py-4">
                <div className="min-w-0">
                  <h3 className="text-lg font-semibold text-ink-100">{t("createDialogTitle")}</h3>
                  <p className="mt-1 text-[12px] text-ink-400">
                    {t("createDialogDesc")}
                  </p>
                </div>
                <button
                  type="button"
                  className="icon-btn h-8 w-8 shrink-0"
                  onClick={() => setCreating(false)}
                  aria-label={tCommon("close")}
                >
                  <XIcon size={15} />
                </button>
              </div>
              <div className="space-y-4 px-5 py-4">
                <label className="block text-[12px] text-ink-300">
                  {t("taskPromptLabel")}
                  <textarea
                    className="input-dark mt-1 min-h-[220px] resize-y text-[12px] leading-relaxed"
                    value={taskPrompt}
                    onChange={(e) => setTaskPrompt(e.target.value)}
                    placeholder={t("taskPromptPlaceholder")}
                    autoFocus
                  />
                </label>
                <div className="flex justify-end gap-2">
                  <button
                    type="button"
                    className="btn btn-ghost"
                    onClick={() => setCreating(false)}
                    disabled={busy === "new"}
                  >
                    <XIcon size={14} />
                    {tCommon("cancel")}
                  </button>
                  <button
                    type="button"
                    className="btn btn-primary"
                    onClick={() => void createTask()}
                    disabled={busy === "new" || !taskPrompt.trim()}
                  >
                    <SendIcon size={14} />
                    {busy === "new" ? t("starting") : t("startTask")}
                  </button>
                </div>
              </div>
            </div>
          </div>
        ) : null}
      </PageBody>
    </div>
  );
}

function Stat({
  label,
  value,
  mono,
}: {
  label: string;
  value: string | number;
  mono?: boolean;
}) {
  return (
    <div className="rounded-lg border border-brand-500/10 bg-ink-900/40 px-3 py-2">
      <div className="text-[11px] text-ink-500 font-medium">
        {label}
      </div>
      <div className={`truncate ${mono ? "font-mono text-[11px]" : ""} text-ink-100`}>
        {value}
      </div>
    </div>
  );
}

function ArtifactGroup({ label, rows }: { label: string; rows: string[] }) {
  return (
    <div>
      <div className="text-[11px] text-ink-500 font-medium mb-1">
        {label}
      </div>
      <ul className="embedded-list-scroll-sm space-y-1">
        {rows.slice(0, 10).map((row, i) => (
          <li
            key={i}
            className="text-[11.5px] font-mono text-ink-200 truncate"
            title={row}
          >
            {row}
          </li>
        ))}
      </ul>
    </div>
  );
}

function previewRecord(record: Record<string, unknown>): string {
  const candidates = ["action", "tool", "kind", "skill_id", "stage", "summary"];
  const parts: string[] = [];
  for (const c of candidates) {
    const v = record[c];
    if (typeof v === "string" && v) {
      parts.push(`${c}=${v}`);
      if (parts.length >= 3) break;
    }
  }
  if (parts.length) return parts.join(" · ");
  return summarizeArtifactResult(record);
}

function summarizeArtifactResult(value: unknown): string {
  if (value === null || value === undefined) return "—";
  if (typeof value === "string") return value.slice(0, 80);
  if (typeof value === "number" || typeof value === "boolean") return String(value);
  if (Array.isArray(value)) return `[${value.length} item${value.length === 1 ? "" : "s"}]`;
  if (typeof value === "object") {
    const obj = value as Record<string, unknown>;
    const keys = Object.keys(obj);
    if (keys.length === 0) return "{} (empty)";
    const head = keys
      .slice(0, 3)
      .map((k) => {
        const v = obj[k];
        if (typeof v === "string") return `${k}=${v.slice(0, 24)}`;
        if (typeof v === "number" || typeof v === "boolean") return `${k}=${v}`;
        return k;
      })
      .join(" · ");
    return keys.length > 3 ? `${head} · +${keys.length - 3} more` : head;
  }
  return String(value);
}

function dotForSeverity(severity: EnvelopeSeverity) {
  if (severity === "danger") return "bg-rose-500";
  if (severity === "warn") return "bg-amber-400";
  return "bg-brand-400";
}
