"use client";

import Link from "next/link";
import { useEffect, useRef, useState } from "react";
import { useLocale, useTranslations } from "next-intl";
import { pendingRun, runLabel, taskRunApi, type TaskRun, type RunOrigin } from "../../lib/taskRuns";
import { usePolledResource } from "../../lib/usePolledResource";
import { TaskRunDetail } from "../chat/TaskRunDetail";
import { ResourceNotice } from "./ResourceNotice";

type History = { runs: TaskRun[]; cursor?: string | null };

export function TaskRunHistory({ sessionId, taskKind, taskId, chat = false, onSelect }: {
  sessionId?: string; taskKind?: RunOrigin; taskId?: string; chat?: boolean; onSelect?: (run: TaskRun) => void;
}) {
  const locale = useLocale(), t = useTranslations("taskRuns");
  const [selected, setSelected] = useState<string>();
  const [date, setDate] = useState(""), [end, setEnd] = useState(""), [state, setState] = useState("");
  const pages = useRef(1);
  const queryKey = JSON.stringify([sessionId, taskKind, taskId, date, end, state]);
  useEffect(() => { pages.current = 1; setSelected(undefined); }, [queryKey]);
  const resource = usePolledResource<History>(`run-history:${queryKey}`, async signal => {
    const params: Record<string, string> = { limit: "50" };
    if (sessionId) params.session_id = sessionId;
    if (taskKind) params.task_kind = taskKind;
    if (taskId) params.task_id = taskId;
    if (date) params.since = String(new Date(date + "T00:00:00").getTime() / 1000);
    if (end) { const exclusive = new Date(end + "T00:00:00"); exclusive.setDate(exclusive.getDate() + 1); params.until = String(exclusive.getTime() / 1000); }
    if (state) params.state = state;
    const runs = new Map<string, TaskRun>();
    let cursor: string | null | undefined;
    // Refresh the loaded window, not an unbounded union of stale records. This
    // also evicts a completed run from the running filter on older loaded pages.
    for (let page = 0; page < pages.current; page += 1) {
      const response = await taskRunApi.list({ ...params, ...(cursor ? { cursor } : {}) }, signal);
      if (!response.ok || !Array.isArray(response.runs)) throw new Error(response.error || "Run history unavailable");
      for (const run of response.runs) runs.set(run.run_id, run);
      if (response.next_cursor && response.next_cursor === cursor) throw new Error("Run history cursor did not advance");
      cursor = response.next_cursor;
      if (!cursor) break;
    }
    return { runs: [...runs.values()], cursor };
  }, { intervalMs: value => value?.runs.some(pendingRun) ? 5000 : 30_000 });
  const rows = resource.data?.runs || [];
  function choose(run: TaskRun) { setSelected(run.run_id); onSelect?.(run); }
  if (chat && !rows.length && !resource.error) return null;
  return <section className={chat ? "px-4 py-2" : "mt-4"} data-testid="task-run-history">
    <details open={!chat} className="rounded-lg border border-[color:var(--line)] p-3">
      <summary className="cursor-pointer text-sm font-medium">{t("history")}{rows.length ? ` · ${rows.length}` : ""}</summary>
      <div className="mt-3 flex flex-wrap items-center gap-2">
        <label className="text-xs">{t("from")}<input type="date" className="input-dark ml-1 !w-auto max-w-[160px] text-xs" aria-label={t("runDate")} value={date} onChange={event => setDate(event.target.value)} /></label>
        <label className="text-xs">{t("to")}<input type="date" className="input-dark ml-1 !w-auto max-w-[160px] text-xs" aria-label={t("endDate")} value={end} onChange={event => setEnd(event.target.value)} /></label>
        <select className="input-dark !w-auto max-w-[160px] text-xs" aria-label={t("executionStatus")} value={state} onChange={event => setState(event.target.value)}><option value="">{t("allStates")}</option>{["queued", "running", "awaiting_approval", "awaiting_input", "unconfirmed", "succeeded", "failed", "blocked", "interrupted", "skipped"].map(value => <option key={value} value={value}>{runLabel(value, t)}</option>)}</select>
        <button type="button" className="btn-ghost text-xs" disabled={resource.loading} onClick={resource.refresh}>{t("refresh")}</button>
      </div>
      <div className="mt-3"><ResourceNotice error={resource.error} updatedAt={resource.updatedAt} onRetry={resource.refresh} /></div>
      {!rows.length && !resource.loading && !resource.error && <p className="mt-3 text-sm text-[color:var(--text-muted)]">{t("noRuns")}</p>}
      <div className="mt-3 max-h-96 overflow-auto"><table className="w-full text-left text-xs"><thead><tr><th className="py-2">{t("triggered")}</th><th>{t("source")}</th><th>{t("executionResult")}</th><th>{t("duration")}</th></tr></thead><tbody>
        {rows.map(run => <tr key={run.run_id} className="border-t border-[color:var(--line)]" aria-selected={selected === run.run_id}>
          <td className="py-3 pr-3"><button type="button" className="text-left underline underline-offset-4" onClick={() => choose(run)}>{new Date((run.scheduled_at || run.created_at) * 1000).toLocaleString(locale)}</button></td>
          <td className="pr-3">{run.trigger_kind === "manual" ? t("manual") : t("scheduled")}</td>
          <td className="pr-3">{runLabel(run.execution_status, t)}<span className="block text-[color:var(--text-muted)]">{runLabel(run.business_status, t)}</span></td>
          <td>{run.elapsed_ms !== undefined ? t("durationSeconds", { seconds: Math.round(run.elapsed_ms / 1000) }) : "—"}</td>
        </tr>)}
      </tbody></table></div>
      {resource.data?.cursor && <button type="button" className="btn-ghost mt-2 text-xs" disabled={resource.loading} onClick={() => { pages.current += 1; resource.refresh(); }}>{t("earlierRuns")}</button>}
    </details>
    {!chat && selected && <div className="mt-4">
      {!onSelect && <TaskRunDetail key={selected} runId={selected} taskKind={taskKind} taskId={taskId} onOpenRun={setSelected} />}
      {rows.find(run => run.run_id === selected)?.session_id && <Link className="mt-2 inline-flex min-h-9 items-center text-sm underline" href={`/chat/${encodeURIComponent(rows.find(run => run.run_id === selected)!.session_id)}?run=${encodeURIComponent(selected)}`}>{t("openConversation")}</Link>}
    </div>}
  </section>;
}
