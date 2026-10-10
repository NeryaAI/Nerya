"use client";

import { useRef, useState } from "react";
import Link from "next/link";
import * as Popover from "@radix-ui/react-popover";
import { useLocale, useTranslations } from "next-intl";
import type { AssistantProfile } from "../../lib/assistants";
import { conversationTimestamp } from "../../lib/externalCalls";
import { readableResult } from "../../lib/agentConversation";
import { AssistantAvatar } from "../NeryaLogo";
import { CheckIcon, ChevronLeftIcon, ClockIcon, FileIcon, SettingsIcon, StopIcon, XIcon } from "../icons";
import { useAssistantResults } from "../shell/AssistantResults";
import { assistantTaskActive, type AssistantTask, type useAssistantTasks } from "./useAssistantTasks";
import { Markdown } from "./Markdown";

type Tasks = ReturnType<typeof useAssistantTasks>;
function output(task: AssistantTask): unknown {
  return task.output && typeof task.output === "object" && "output" in task.output
    ? (task.output as {output: unknown}).output : task.output;
}
function relativeTime(time: number, locale: string): string {
  const seconds = Math.max(0, Math.floor((Date.now() - time) / 1000));
  const [value, unit]: [number, Intl.RelativeTimeFormatUnit] = seconds < 60 ? [0, "second"] : seconds < 3600 ? [Math.floor(seconds / 60), "minute"] : seconds < 86400 ? [Math.floor(seconds / 3600), "hour"] : [Math.floor(seconds / 86400), "day"];
  return new Intl.RelativeTimeFormat(locale, {numeric: "auto"}).format(-value, unit);
}
function ActivityList({tasks, onNavigate, onInspect}: {tasks: Tasks; onNavigate: () => void; onInspect: (task: AssistantTask) => void}) {
  const t = useTranslations("assistants"), [expanded, setExpanded] = useState(false);
  return <section aria-label={t("recentActivity")} data-testid="assistant-activities">
    <h3 className="mb-2 text-xs font-medium text-[color:var(--text-muted)]">{t("recentActivity")}</h3>
    {tasks.loading && !tasks.rows.length ? <p role="status" className="py-3 text-xs text-[color:var(--text-muted)]">{t("loading")}</p> : null}
    {tasks.error ? <div role="status" className="py-2 text-xs text-warn">{tasks.error}<button type="button" className="ml-2 min-h-9 underline" onClick={tasks.retry}>{t("retry")}</button></div> : null}
    {!tasks.loading && !tasks.error && !tasks.rows.length ? <p className="py-3 text-sm text-[color:var(--text-muted)]">{t("noRecentActivity")}</p> : null}
    {(expanded ? tasks.rows : tasks.rows.slice(0, 3)).map(task => {
      const body = <><span aria-hidden className="mt-0.5 shrink-0 text-[color:var(--text-muted)]">{task.state === "succeeded" ? <CheckIcon size={16}/> : <ClockIcon size={16}/>}</span>
        <span className="min-w-0 flex-1"><span className="block truncate text-sm">{task.title}</span><span className="mt-0.5 block text-xs text-[color:var(--text-muted)]">{t.has("taskStates." + task.state) ? t("taskStates." + task.state) : task.state}</span></span></>;
      return <div key={task.task_id} className="flex min-w-0 items-center gap-1" data-testid="assistant-activity" data-state={task.state}>
        {task.task_session_id ? <Link href={"/chat/" + encodeURIComponent(task.task_session_id)} title={task.title} onClick={onNavigate}
          className="flex min-h-11 min-w-0 flex-1 items-start gap-2.5 rounded-lg px-2 py-2 hover:bg-[color:var(--panel-bg)]">{body}</Link>
          : <button type="button" title={task.title} onClick={() => onInspect(task)} className="flex min-h-11 min-w-0 flex-1 items-start gap-2.5 rounded-lg px-2 py-2 text-left hover:bg-[color:var(--panel-bg)]">{body}</button>}
        {assistantTaskActive(task) ? <button type="button" className="ui-icon-button shrink-0" disabled={tasks.busy === task.task_id || task.state === "stopping"}
          aria-busy={tasks.busy === task.task_id} aria-label={t("stopTask") + ": " + task.title} title={t("stopTask")} onClick={() => void tasks.stop(task)}><StopIcon size={15}/></button> : null}
      </div>;
    })}
    {tasks.rows.length > 3 ? <button type="button" className="min-h-9 px-2 text-xs text-[color:var(--text-muted)] underline" onClick={() => setExpanded(value => !value)}>{t(expanded ? "fewerTasks" : "allTasks")}</button> : null}
    {tasks.actionError ? <p role="alert" className="mt-2 text-xs text-danger">{tasks.actionError}</p> : null}
  </section>;
}

function Overview({assistant, tasks, lastActiveAt, onEdit, onOpenReports, onOpenResult, onNavigate}: {
  assistant: AssistantProfile; tasks: Tasks; lastActiveAt?: number;
  onEdit: () => void; onOpenReports: () => void; onOpenResult: (commandId: string) => void; onNavigate: () => void;
}) {
  const t = useTranslations("assistants"), locale = useLocale();
  const reports = useAssistantResults(assistant.id);
  const [inspected, setInspected] = useState<AssistantTask | null>(null);
  const [expandedOutputs, setExpandedOutputs] = useState(false);
  const results = reports.rows.filter(row => row.state === "succeeded");
  const legacyOutputs = tasks.rows.filter(task => task.state === "succeeded" && Boolean(task.output) && !reports.rows.some(row => row.task_id === task.task_id));
  const last = Math.max(lastActiveAt || 0, ...tasks.rows.map(task => conversationTimestamp(task.updated_at) || 0));
  const relative = last ? relativeTime(last, locale) : "";
  if (inspected) return <div data-testid="assistant-activity-detail">
    <div className="mb-4 flex items-center justify-between gap-2"><button type="button" className="inline-flex min-h-11 items-center gap-1 text-xs" onClick={() => setInspected(null)}><ChevronLeftIcon size={14}/>{t("recentActivity")}</button><Popover.Close className="ui-icon-button" aria-label={t("closeAssistantPanel")}><XIcon size={16}/></Popover.Close></div>
    <h2 className="mb-3 break-words text-sm font-semibold">{inspected.title}</h2>
    {inspected.kind === "scheduled" ? <p className="mb-3 text-xs text-[color:var(--text-muted)]">{inspected.every_seconds ? t("taskInterval", {seconds: inspected.every_seconds}) : inspected.cron}</p> : null}
    {inspected.error ? <p role="alert" className="mb-3 break-words text-sm text-danger">{inspected.error}</p> : null}
    {inspected.state === "succeeded" && Boolean(inspected.output) ? <Markdown>{readableResult(output(inspected), locale.startsWith("zh"))}</Markdown>
      : <p role="status" className="text-sm text-[color:var(--text-muted)]">{inspected.progress?.at(-1)?.note || (t.has("taskStates." + inspected.state) ? t("taskStates." + inspected.state) : inspected.state)}</p>}
  </div>;
  return <>
    <div className="mb-4 flex items-start gap-3">
      <AssistantAvatar profile={assistant} size={56}/>
      <div className="min-w-0 flex-1 pt-1"><h2 className="truncate text-base font-semibold">{assistant.name}</h2><p className="mt-1 text-xs text-[color:var(--text-muted)]">{last ? t("lastActive", {time: relative}) : t("noAssistantActivity")}</p></div>
      <Popover.Close className="ui-icon-button -mr-2 -mt-2 shrink-0" aria-label={t("closeAssistantPanel")}><XIcon size={16}/></Popover.Close>
    </div>
    <div className="mb-5 flex gap-2">
      <button type="button" onClick={onEdit} className="flex min-h-11 flex-1 items-center justify-center gap-2 rounded-lg bg-[color:var(--panel-bg)] px-2 text-xs hover:bg-[color:var(--card-hi)]"><SettingsIcon size={15}/>{t("edit")}</button>
      <button type="button" onClick={onOpenReports} className="flex min-h-11 flex-1 items-center justify-center gap-2 rounded-lg bg-[color:var(--panel-bg)] px-2 text-xs hover:bg-[color:var(--card-hi)]"><FileIcon size={15}/>{t("openResults")}</button>
    </div>
    <ActivityList tasks={tasks} onNavigate={onNavigate} onInspect={setInspected}/>
    <section aria-label={t("outputs")} data-testid="assistant-outputs" className="mt-4 border-t border-[color:var(--line)] pt-4">
      <h3 className="mb-2 text-xs font-medium text-[color:var(--text-muted)]">{t("outputs")}</h3>
      {reports.loading && !reports.rows.length ? <p role="status" className="py-3 text-xs text-[color:var(--text-muted)]">{t("loading")}</p> : null}
      {reports.error ? <div role="status" className="py-2 text-xs text-warn">{t("resultLoadFailed")}<button type="button" className="ml-2 min-h-9 underline" onClick={reports.retry}>{t("retry")}</button></div> : null}
      {!reports.loading && !reports.error && !results.length && !legacyOutputs.length ? <p className="py-3 text-sm text-[color:var(--text-muted)]">{t("noOutputs")}</p> : null}
      {(expandedOutputs ? results : results.slice(0, 3)).map(row => <button type="button" key={row.command_id} onClick={() => onOpenResult(row.command_id)} data-testid="assistant-output" title={row.title}
        className="flex min-h-11 w-full min-w-0 items-start gap-2.5 rounded-lg px-2 py-2 text-left hover:bg-[color:var(--panel-bg)]"><FileIcon size={16} className="mt-0.5 shrink-0 text-[color:var(--text-muted)]"/><span className="min-w-0 flex-1"><span className="block truncate text-sm">{row.title}</span><span className="mt-0.5 block truncate text-xs text-[color:var(--text-muted)]">{t("taskStates.succeeded")}{conversationTimestamp(row.created_at) ? " · " + relativeTime(conversationTimestamp(row.created_at)!, locale) : ""}</span></span></button>)}
      {(expandedOutputs ? legacyOutputs : legacyOutputs.slice(0, Math.max(0, 3 - results.length))).map(task => <button type="button" key={task.task_id} onClick={() => setInspected(task)} className="flex min-h-11 w-full items-center gap-2.5 rounded-lg px-2 py-2 text-left text-sm hover:bg-[color:var(--panel-bg)]"><FileIcon size={16}/><span className="min-w-0 truncate">{task.title}</span></button>)}
      {results.length + legacyOutputs.length > 3 ? <button type="button" className="min-h-9 px-2 text-xs text-[color:var(--text-muted)] underline" onClick={() => setExpandedOutputs(value => !value)}>{t(expandedOutputs ? "fewerOutputs" : "allOutputs")}</button> : null}
    </section>
  </>;
}

export function AssistantOverviewPopover({assistant, tasks, lastActiveAt, onEdit, onOpenReports, onOpenResult}: {
  assistant: AssistantProfile; tasks: Tasks; lastActiveAt?: number;
  onEdit: () => void; onOpenReports: () => void; onOpenResult: (commandId: string) => void;
}) {
  const t = useTranslations("assistants"), [open, setOpen] = useState(false);
  const handoff = useRef(false);
  const act = (action: () => void) => { handoff.current = true; setOpen(false); action(); };
  return <Popover.Root open={open} onOpenChange={setOpen}>
    <Popover.Trigger asChild><button type="button" className="ui-icon-button shrink-0" data-testid="open-assistant-panel" aria-label={t("assistantPanel")} title={t("assistantPanel")}><SettingsIcon size={19}/></button></Popover.Trigger>
    <Popover.Portal><Popover.Content align="end" sideOffset={10} collisionPadding={12} aria-label={t("assistantPanel")} data-testid="assistant-overview-panel"
      className="z-[1250] overflow-y-auto overscroll-contain rounded-2xl border border-[color:var(--line-hi)] bg-[color:var(--overlay-surface)] p-4 text-[color:var(--text-base)] outline-none"
      style={{width: "min(380px, calc(100vw - 24px))", maxHeight: "min(70dvh, var(--radix-popover-content-available-height))"}}
      onCloseAutoFocus={event => { if (handoff.current) { event.preventDefault(); handoff.current = false; } }}>
      <Overview assistant={assistant} tasks={tasks} lastActiveAt={lastActiveAt} onEdit={() => act(onEdit)}
        onOpenReports={() => act(onOpenReports)} onOpenResult={id => act(() => onOpenResult(id))} onNavigate={() => { handoff.current = true; setOpen(false); }}/>
    </Popover.Content></Popover.Portal>
  </Popover.Root>;
}
