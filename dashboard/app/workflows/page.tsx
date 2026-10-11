"use client";

import { blankDraft, buildDraftFromSchedule, buildSchedulePayload, cadenceFor, deliverySummary, isPaused, scheduleKind, statusTone, STATUS_DOT_CLASS, titleFor, type ScheduleDraft, type ScheduleFilter, type ScheduleKind, type Translate } from "../../lib/automationDraft";
import { AutomationTaskEditor as ScheduleEditor } from "../../components/workflows/AutomationTaskEditor";
import { usePolledResource } from "../../lib/usePolledResource";
import { ResourceNotice } from "../../components/workflows/ResourceNotice";
import { validateSchedule } from "../../lib/scheduleEditor";
import { TaskRunDetail } from "../../components/chat/TaskRunDetail";
import { useUnsavedChanges } from "../../components/chat/useUnsavedChanges";


import { useCallback, useMemo, useRef, useState } from "react";
import { useLocale, useTranslations } from "next-intl";
import {
  ErrorBanner,
  Json,
  PageBody,
  PageHeader,
  Pill,
} from "../../components/Page";
import {
  PauseIcon,
  PlusIcon,
  RefreshIcon,
  SendIcon,
} from "../../components/icons";
import { confirm as confirmDialog, toast } from "../../lib/dialogs";
import {
  describeCron,
  describeIntervalSeconds,
  formatTsShort,
} from "../../lib/format";
import { ApiError, callApi, clientApi } from "../../lib/clientApi";
import type {
  TriggerRoute,
  TriggerSchedule,
} from "../../lib/clientApi";
import { ScheduledExecutionReceipt } from "../../components/workflows/ScheduledExecutionReceipt";
import { TaskRunHistory } from "../../components/chat/TaskRunHistory";
import {TaskFinancialGrants} from '../../components/TaskFinancialGrants';

function useCadenceLabel(): (schedule: TriggerSchedule) => string {
  const t = useTranslations("cadence");
  const locale = useLocale();
  return (schedule) => {
    const desc = schedule.cron
      ? describeCron(String(schedule.cron))
      : schedule.every_seconds != null
        ? describeIntervalSeconds(Number(schedule.every_seconds))
        : null;
    if (!desc) return cadenceFor(schedule, locale);
    return t(desc.key, (desc.params ?? {}) as Record<string, string | number>);
  };
}

export default function WorkflowsPage() {
  const t = useTranslations("workflows") as Translate;
  const w = useTranslations("workflowUpgrade");
  const tCommon = useTranslations("common") as Translate;
  const routesRead = usePolledResource<TriggerRoute[]>("automation:routes", async signal => {
    const response = await callApi<{ routes: TriggerRoute[] } | TriggerRoute[]>("/triggers/routes", { signal });
    const list = Array.isArray(response) ? response : response.routes;
    if (!Array.isArray(list)) throw new Error("Event routes unavailable");
    return list;
  });
  const schedulesRead = usePolledResource<TriggerSchedule[]>("automation:schedules", async signal => {
    const response = await callApi<{ schedules: TriggerSchedule[] }>("/triggers/schedules", { signal });
    if (!Array.isArray(response.schedules)) throw new Error("Schedule definitions unavailable");
    return response.schedules;
  });
  const statusRead = usePolledResource<TriggerSchedule[]>("automation:status", async signal => {
    const response = await callApi<{ ok: boolean; schedules: TriggerSchedule[] }>("/triggers/schedules/status", { signal });
    if (!response.ok || !Array.isArray(response.schedules)) throw new Error("Schedule status unavailable");
    return response.schedules;
  });
  const routes = routesRead.data || [], schedules = schedulesRead.data || [], statusRows = statusRead.data || [];
  const [error, setError] = useState<string | null>(null);
  const loading = schedulesRead.loading;
  const [busy, setBusy] = useState<string | null>(null);
  const [filter, setFilter] = useState<ScheduleFilter>("all");
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [draftOpen, setDraftOpen] = useState(false);
  const [draftMode, setDraftMode] = useState<"create" | "edit">("create");
  const [draft, setDraft] = useState<ScheduleDraft>(() => blankDraft());
  const [baseline, setBaseline] = useState("");
  const [query, setQuery] = useState("");
  const saving = useRef(false);
  const mutating = useRef(false);
  const [openedRun, setOpenedRun] = useState<{ taskId: string; runId: string }>();
  const dirty = draftOpen && baseline !== JSON.stringify(draft);
  const load = useCallback(async () => { routesRead.refresh(); schedulesRead.refresh(); statusRead.refresh(); }, [routesRead.refresh, schedulesRead.refresh, statusRead.refresh]);
  useUnsavedChanges(dirty);
  async function leaveDraft() { return !dirty || await confirmDialog({ title: w("discardTitle"), message: w("discardDraft"), tone: "warning" }); }

  const rows = useMemo(() => {
    const statusById = new Map(statusRows.map((row) => [row.id, row]));
    return schedules.filter(row => !row.archived).map((row) => ({ ...(statusById.get(row.id) || {}), ...row }));
  }, [schedules, statusRows]);

  const filteredRows = useMemo(() => {
    return rows.filter((row) => {
      if(row.archived)return false;
      if (query.trim() && !`${titleFor(row)} ${row.id}`.toLocaleLowerCase().includes(query.trim().toLocaleLowerCase())) return false;
      if (filter === "all") return true;
      if (filter === "active") return !isPaused(row);
      if (filter === "paused") return isPaused(row);
      return scheduleKind(row) === filter;
    });
  }, [rows, filter, query]);

  const selected = useMemo(
    () => filteredRows.find((row) => row.id === selectedId) ?? filteredRows[0] ?? null,
    [filteredRows, selectedId],
  );

  const counts = useMemo(() => {
    return rows.reduce(
      (acc, row) => {
        acc.total += 1;
        if (isPaused(row)) acc.paused += 1;
        else acc.active += 1;
        acc[scheduleKind(row)] += 1;
        return acc;
      },
      { total: 0, active: 0, paused: 0, agent: 0, script: 0, trigger: 0 },
    );
  }, [rows]);

  async function openCreate(kind: ScheduleKind = "agent", template?: "morning" | "risk" | "data") {
    if (!await leaveDraft()) return;
    const next = blankDraft();
    next.id = `task_${crypto.randomUUID().slice(0, 8)}`;
    if (template) { next.title = w(`${template}Title`); next.sourceRequest = w(`${template}Goal`); }
    next.sessionKind = kind;
    next.kind = `${kind}.task`;
    if (kind === "script") {
      next.target = "script:";
      next.cadence = "cron";
    } else if (kind === "trigger") {
      next.target = "main";
      next.sessionMode = "ephemeral";
    } else {
      next.target = "agent";
    }
    setDraft(next);
    setBaseline(JSON.stringify(next));
    setDraftMode("create");
    setDraftOpen(true);
  }

  async function openEdit(schedule: TriggerSchedule) {
    if (!await leaveDraft()) return;
    const next = buildDraftFromSchedule(schedule);
    setDraft(next); setBaseline(JSON.stringify(next));
    setDraftMode("edit");
    setDraftOpen(true);
  }

  async function saveDraft() {
    if (saving.current || mutating.current) return;
    saving.current = true;
    setBusy("save");
    setError(null);
    try {
      const calendar = await validateSchedule(draft);
      const payload = buildSchedulePayload({ ...draft, runAtInstant: calendar.schedule.run_at || undefined }, t);
      if (draftMode === "edit") {
        const result = await clientApi.scheduleUpdate(payload.id, payload);
        if (!result.ok) throw new Error("Schedule update was not accepted");
        toast({ message: t("savedSchedule", { id: payload.id }), tone: "ok" });
      } else {
        const result = await clientApi.scheduleAdd(payload);
        if (!result.ok) throw new Error("Schedule creation was not accepted");
        toast({ message: t("createdSchedule", { id: payload.id }), tone: "ok" });
      }
      setDraftOpen(false);
      setSelectedId(payload.id);
      setFilter("all"); setQuery("");
      await load();
    } catch (e) {
      const code = e instanceof ApiError && e.payload && typeof e.payload === "object" ? String((e.payload as Record<string, unknown>).error || "") : "";
      setError(code && w.has(code) ? w(code) : e instanceof Error ? e.message : String(e));
    } finally {
      saving.current = false;
      setBusy(null);
    }
  }

  async function toggleSchedule(schedule: TriggerSchedule) {
    if (mutating.current || saving.current) return;
    mutating.current = true;
    setBusy(`toggle:${schedule.id}`);
    try {
      const result = isPaused(schedule) ? await clientApi.scheduleResume(schedule.id) : await clientApi.schedulePause(schedule.id);
      if (!result.ok) throw new Error("Schedule state change was not accepted");
      await load();
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      mutating.current = false;
      setBusy(null);
    }
  }

  async function runSchedule(schedule: TriggerSchedule) {
    if (mutating.current || saving.current) return;
    mutating.current = true;
    try {
      const ok = await confirmDialog({
        title: w("rerunTitle"),
        message: isStrategySchedule(schedule) ? t("runStrategyConfirm", { id: schedule.id }) : w("rerunConfirm"),
        okLabel: t("runNow"),
        cancelLabel: tCommon("cancel"),
        tone: "warning",
      });
      if (!ok) return;
      setBusy(`run:${schedule.id}`);
      const result = await clientApi.scheduleRunNow(schedule.id);
      if (result.ok && result.run_id) {
        setOpenedRun({ taskId: schedule.id, runId: result.run_id });
        setSelectedId(schedule.id);
      }
      toast({
        message: result.ok
          ? t("runQueued", { id: schedule.id })
          : t("runFailed", { id: schedule.id }),
        tone: result.ok ? "ok" : "error",
      });
      await load();
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      mutating.current = false;
      setBusy(null);
    }
  }

  async function removeSchedule(schedule: TriggerSchedule) {
    if (mutating.current || saving.current) return;
    mutating.current = true;
    try {
      const ok = await confirmDialog({
      title: t("deleteTitle"),
      message: t("deleteConfirm", { id: schedule.id }),
      okLabel: tCommon("delete"),
      cancelLabel: tCommon("cancel"),
      tone: "danger",
    });
      if (!ok) return;
      setBusy(`delete:${schedule.id}`);
      const result = await clientApi.scheduleRemove(schedule.id);
      if (!result.ok) throw new Error("Schedule removal was not accepted");
      if (selectedId === schedule.id) setSelectedId(null);
      await load();
      toast({ message: t("deletedSchedule", { id: schedule.id }), tone: "ok" });
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      mutating.current = false;
      setBusy(null);
    }
  }

  const filters: { id: ScheduleFilter; label: string; count: number }[] = [
    { id: "all", label: t("filterAll"), count: counts.total },
    { id: "agent", label: t("kindAgent"), count: counts.agent },
    { id: "script", label: t("kindScript"), count: counts.script },
    { id: "trigger", label: t("kindTrigger"), count: counts.trigger },
    { id: "active", label: t("active"), count: counts.active },
    { id: "paused", label: t("paused"), count: counts.paused },
  ];

  async function reuseTask(schedule: TriggerSchedule) {
    if (!await leaveDraft()) return;
    const original = buildDraftFromSchedule(schedule), next = blankDraft();
    next.id = `task_${crypto.randomUUID().slice(0, 8)}`;
    next.title = w("reuseTitle", { title: titleFor(schedule) });
    next.sourceRequest = original.usePromptOverride ? original.generatedPrompt : original.sourceRequest;
    next.cadence = original.cadence;
    next.cron = original.cron; next.everySeconds = original.everySeconds; next.timezone = original.timezone;
    next.sessionKind = original.sessionKind; next.kind = original.kind;
    next.scriptId = original.scriptId; next.scriptArgsJson = original.scriptArgsJson;
    next.target = original.sessionKind === "script" ? `script:${original.scriptId}` : original.sessionKind === "agent" ? "agent" : "main";
    // A reusable definition is not a copy of account grants, delivery recipients,
    // bound sessions, or runtime execution policy. The new definition stays paused.
    setDraft(next); setBaseline(JSON.stringify(next)); setDraftMode("create"); setDraftOpen(true);
  }
  return <PageBody>
    <PageHeader title={w("automationTitle")} description={w("automationDescription")} actions={<div className="flex flex-wrap gap-2">
      <button type="button" className="btn-ghost" onClick={() => void load()}><RefreshIcon size={15}/>{w("refresh")}</button>
      <button type="button" className="btn-primary" onClick={() => void openCreate()}><PlusIcon size={15}/>{w("createAutomation")}</button>
    </div>}/>
    {error && <ErrorBanner error={error}/>}
    <ResourceNotice error={schedulesRead.error} updatedAt={schedulesRead.updatedAt} onRetry={schedulesRead.refresh}/>
    <ResourceNotice error={statusRead.error} updatedAt={statusRead.updatedAt} onRetry={statusRead.refresh}/>
    <div className="grid min-w-0 grid-cols-1 gap-6 lg:grid-cols-[minmax(240px,0.7fr)_minmax(0,2fr)]">
      <aside className="min-w-0 self-start rounded-lg border border-[color:var(--line)]" aria-label={t("schedulesTitle")}>
        <div className="space-y-3 border-b border-[color:var(--line)] p-3">
          <input className="input-dark w-full" type="search" value={query} aria-label={w("searchTasks")} placeholder={w("searchTasks")} onChange={event => setQuery(event.target.value)}/>
          <select className="input-dark w-full text-sm" value={filter} aria-label={t("filterAll")} onChange={event => setFilter(event.target.value as ScheduleFilter)}>{filters.map(item => <option value={item.id} key={item.id}>{item.label} · {item.count}</option>)}</select>
        </div>
        {loading && !schedulesRead.data ? <div role="status" className="p-4 text-sm">{w("loading")}</div> : !filteredRows.length ? (!schedulesRead.error && <div className="p-4 text-sm leading-7 text-[color:var(--text-muted)]">{rows.length ? t("noSchedules") : w("noAutomations")}</div>) : <ul className="max-h-[65vh] overflow-auto">
          {filteredRows.map(schedule => <ScheduleListItem key={schedule.id} schedule={schedule} active={selected?.id === schedule.id} busy={busy}
            onSelect={() => { void leaveDraft().then(ok => { if (ok) { setSelectedId(schedule.id); setDraftOpen(false); } }); }}
            onRun={() => void runSchedule(schedule)} onToggle={() => void toggleSchedule(schedule)}/>)}</ul>}
      </aside>
      <section className="min-w-0" aria-label={w("definition")}>
        {draftOpen ? <ScheduleEditor mode={draftMode} draft={draft} busy={busy} t={t} tCommon={tCommon} onChange={setDraft}
          onCancel={() => { void leaveDraft().then(ok => { if(ok)setDraftOpen(false); }); }} onSave={() => void saveDraft()}/>
          : selected ? <ScheduleDetail key={selected.id} schedule={selected} busy={busy} routes={routes} t={t} tCommon={tCommon}
            onEdit={() => void openEdit(selected)} onRun={() => void runSchedule(selected)} onToggle={() => void toggleSchedule(selected)} onDelete={() => void removeSchedule(selected)} onReuse={() => void reuseTask(selected)}/>
          : !schedulesRead.error && <section className="py-5"><h2 className="text-lg font-semibold">{w("templates")}</h2><p className="mt-2 text-sm leading-7 text-[color:var(--text-muted)]">{w("emptyAutomationHint")}</p>
            <div className="mt-5 flex flex-col divide-y divide-[color:var(--line)]">{(["morning","risk","data"] as const).map(template => <button type="button" key={template} className="py-4 text-left hover:text-[color:var(--violet-2)]" onClick={() => void openCreate("agent",template)}><strong className="block text-sm">{w(`${template}Title`)}</strong><span className="mt-1 block text-sm leading-7 text-[color:var(--text-muted)]">{w(`${template}Goal`)}</span></button>)}</div>
          </section>}
        {!draftOpen && selected && openedRun?.taskId === selected.id && <div className="mt-5"><TaskRunDetail key={openedRun.runId} runId={openedRun.runId} taskKind="scheduled_agent" taskId={selected.id} onOpenRun={runId => setOpenedRun({ taskId: selected.id, runId })} /></div>}
      </section>
    </div>
    <details className="mt-8 border-t border-[color:var(--line)] py-4 text-sm"><summary className="cursor-pointer text-[color:var(--text-muted)]">{w("routes")} · {routes.length}</summary>
      <ResourceNotice error={routesRead.error} updatedAt={routesRead.updatedAt} onRetry={routesRead.refresh}/>
      {!routes.length && !routesRead.error ? <p className="py-3 text-[color:var(--text-muted)]">{routesRead.loading ? w("loading") : t("noRoutes")}</p> : <ul className="mt-3 max-h-64 overflow-auto">{routes.map(route => <RouteListItem key={route.id} route={route} t={t}/>)}</ul>}
    </details>
  </PageBody>;
}

function isStrategySchedule(schedule: TriggerSchedule): boolean {
  const kind = String(schedule.kind || "");
  const target = String(schedule.target || "");
  return Boolean(
    schedule.strategy_id ||
      kind.startsWith("strategy.") ||
      target.includes("strategy"),
  );
}

function ScheduleListItem({
  schedule,
  active,
  busy,
  onSelect,
  onRun,
  onToggle,
}: {
  schedule: TriggerSchedule;
  active: boolean;
  busy: string | null;
  onSelect: () => void;
  onRun: () => void;
  onToggle: () => void;
}) {
  const paused = isPaused(schedule);
  const kind = scheduleKind(schedule);
  const cadenceLabel = useCadenceLabel();
  const tone = statusTone(schedule);
  const nextRun = formatTsShort(schedule.next_due_at ?? undefined);
  const t = useTranslations("workflows");
  return (
    <li
      className={[
        "border-b border-brand-500/5 px-3 py-2.5 last:border-b-0",
        active ? "bg-brand-500/10" : "hover:bg-brand-500/5",
      ].join(" ")}
    >
      <button type="button" onClick={onSelect} className="block w-full min-w-0 text-left">
        <div className="flex min-w-0 items-center gap-2">
          <span className={`h-2 w-2 shrink-0 rounded-full ${STATUS_DOT_CLASS[tone]}`} />
          <span className="min-w-0 flex-1 truncate text-sm text-[color:var(--text-base)]" title={schedule.id}>
            {titleFor(schedule)}
          </span>
          <Pill tone={tone}>{paused ? t("paused") : t("active")}</Pill>
        </div>
        <div className="mt-2 flex min-w-0 flex-wrap items-center gap-x-2 gap-y-1 text-xs text-[color:var(--text-muted)]">
          <span>{cadenceLabel(schedule)}</span>
          <span>·</span>
          <span>{kindLabel(kind, t)}</span>
          {!paused && schedule.next_due_at != null ? (
            <span className="w-full">{t("metricNextRun")} · {nextRun}</span>
          ) : null}
        </div>
      </button>
      <div className="mt-2 flex items-center gap-1">
        <button
          type="button"
          className="btn-ghost px-2 py-0.5 text-[11px]"
          disabled={!!busy}
          onClick={onRun}
          title={t("runNow")}
        >
          <SendIcon size={12} />
        </button>
        <button
          type="button"
          className="btn-ghost px-2 py-0.5 text-[11px]"
          disabled={!!busy}
          onClick={onToggle}
          title={paused ? t("resume") : t("pause")}
        >
          <PauseIcon size={12} />
        </button>
      </div>
    </li>
  );
}

function ScheduleDetail({ schedule,busy,t,tCommon,onEdit,onRun,onToggle,onDelete,onReuse }: {
  schedule:TriggerSchedule;busy:string|null;routes:TriggerRoute[];t:Translate;tCommon:Translate;
  onEdit:()=>void;onRun:()=>void;onToggle:()=>void;onDelete:()=>void;onReuse:()=>void;
}) {
  const w=useTranslations("workflowUpgrade");
  const kind=scheduleKind(schedule), paused=isPaused(schedule);
  const [tab,setTab]=useState<"settings"|"history">("settings");
  const cadenceLabel=useCadenceLabel();
  return <section className="min-w-0" data-testid="automation-task-detail" data-task-id={schedule.id}>
    <header className="flex flex-wrap items-start justify-between gap-4 border-b border-[color:var(--line)] pb-4">
      <div className="min-w-0 flex-1"><h2 className="break-words text-xl font-semibold">{titleFor(schedule)}</h2><p className="mt-2 text-sm text-[color:var(--text-muted)]">{cadenceLabel(schedule)} · {schedule.timezone||"UTC"} · {paused?t("paused"):t("active")}</p></div>
      <div className="flex flex-wrap items-center gap-2"><button type="button" className="btn-ghost text-sm" onClick={onEdit}>{tCommon("edit")}</button><button type="button" className="btn-ghost text-sm" disabled={!!busy} onClick={onToggle}>{paused?t("resume"):t("pause")}</button><button type="button" className="btn-primary text-sm" disabled={!!busy} onClick={onRun}>{t("runNow")}</button></div>
    </header>
    <div className="flex flex-wrap items-center gap-5 border-b border-[color:var(--line)]" role="tablist" aria-label={t("taskDetail")}>
      {(["settings","history"] as const).map(value=><button type="button" key={value} role="tab" aria-selected={tab===value} className={`border-b-2 py-3 text-sm ${tab===value?"border-[color:var(--violet-2)]":"border-transparent text-[color:var(--text-muted)]"}`} onClick={()=>setTab(value)}>{w(value==="settings"?"definition":"history")}</button>)}
    </div>
    {tab==="settings" ? <div className="space-y-5 py-5">
      <p className="text-xs text-[color:var(--text-muted)]">{w("configuredNotHealthy")}</p>
      <dl className="grid grid-cols-2 gap-4 text-sm"><div><dt className="text-[color:var(--text-muted)]">{t("metricLastRun")}</dt><dd className="mt-1">{formatTsShort(schedule.last_fired_ts??undefined)}</dd></div><div><dt className="text-[color:var(--text-muted)]">{t("metricNextRun")}</dt><dd className="mt-1">{formatTsShort(schedule.next_due_at??undefined)}</dd></div></dl>
      {kind==="agent" && <section><h3 className="text-sm font-medium">{w("effectivePrompt")}</h3><p className="mt-3 whitespace-pre-wrap break-words text-sm leading-7">{String(schedule.payload?.prompt||schedule.payload?.source_request||"")}</p></section>}
      {kind==="script"&&<section className="text-sm"><h3 className="font-medium">{t("scriptId")}</h3><p className="mt-2">{String(schedule.payload?.script_id||"")}</p><PayloadTable value={(schedule.payload?.args||{}) as Record<string,unknown>}/></section>}
      <p className="text-sm text-[color:var(--text-muted)]">{w("delivery")} · {deliverySummary(schedule)}</p>
      {kind!=="trigger"&&<TaskFinancialGrants taskKind="scheduled_agent" taskId={schedule.id}/>}
      <details className="text-sm"><summary className="cursor-pointer text-[color:var(--text-muted)]">{w("advanced")}</summary><div className="mt-3 space-y-3"><p className="text-xs">{schedule.id} · {schedule.kind}</p><Json value={schedule}/><button type="button" className="btn-ghost text-danger" disabled={!!busy} onClick={onDelete}>{tCommon("delete")}</button></div></details>
      {kind!=="trigger"&&<div className="border-t border-[color:var(--line)] pt-4"><button type="button" className="btn-ghost text-sm" onClick={onReuse}>{w("saveReusable")}</button><p className="mt-2 text-xs leading-6 text-[color:var(--text-muted)]">{w("reuseHint")}</p></div>}
    </div> : kind!=="trigger" ? <TaskRunHistory taskKind="scheduled_agent" taskId={schedule.id}/>
      : <ScheduledExecutionReceipt receipt={(schedule as TriggerSchedule & {latest_execution?:import("../../components/workflows/ScheduledExecutionReceipt").ScheduledReceipt}).latest_execution}/>}
  </section>;
}

function RouteListItem({ route, t }: { route: TriggerRoute; t: Translate }) {
  const w = useTranslations("workflowUpgrade");
  const matchKind = typeof route.match?.kind === "string" ? route.match.kind : "any";
  const skill = String(route.action?.skill_id || route.action?.target || route.target || w("unresolvedTarget"));
  const paused = route.paused || route.enabled === false;
  return (
    <li className="border-b border-brand-500/5 px-3 py-2.5 last:border-b-0">
      <div className="flex min-w-0 items-center gap-2">
        <span className={`h-2 w-2 shrink-0 rounded-full ${paused ? "bg-ink-500" : "bg-brand-300"}`} />
        <span className="min-w-0 flex-1 truncate text-[12.5px] text-ink-100">
          {route.title || route.id}
        </span>
        <Pill tone="brand">
          {paused ? t("paused") : t("active")}
        </Pill>
      </div>
      <div className="mt-1 text-[10.5px] font-mono text-ink-500">
        {matchKind} -&gt; {skill}
      </div>
    </li>
  );
}

function PayloadTable({ value }: { value: Record<string, unknown> }) {
  const entries = Object.entries(value);
  if (!entries.length) return null;
  return (
    <div className="mt-3 overflow-auto rounded-lg border border-[color:var(--line)]">
      <table className="w-full text-left text-[12px]">
        <tbody>
          {entries.map(([key, raw]) => (
            <tr key={key} className="border-b border-brand-500/5 last:border-b-0">
              <th className="w-36 bg-ink-950/30 px-3 py-2 font-mono font-normal text-ink-500">
                {key}
              </th>
              <td className="break-words px-3 py-2 font-mono text-[color:var(--text-base)]">
                {typeof raw === "string" || typeof raw === "number" || typeof raw === "boolean"
                  ? String(raw)
                  : JSON.stringify(raw)}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

function kindLabel(kind: ScheduleKind, t: Translate): string {
  if (kind === "agent") return t("kindAgent");
  if (kind === "script") return t("kindScript");
  return t("kindTrigger");
}
