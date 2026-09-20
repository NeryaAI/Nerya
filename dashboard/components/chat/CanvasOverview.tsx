"use client";

import { ChoiceSelect } from "../ChoiceSelect";

import { useMemo, useState } from "react";
import { useLocale } from "next-intl";
import type { ChatThread } from "../../lib/chat";
import type { ChatResult } from "../../lib/chatResults";
import { workbenchTimeline, type WorkbenchFile } from "../../lib/canvasWorkbench";
import { isWorking, type AgentWork } from "./useAgentWork";
import { ChevronRightIcon, FileIcon, MessagesIcon } from "../icons";
import { StreamedMarkdown } from "./TurnBlocks";

const quiet = "min-h-9 rounded px-2 text-xs text-[color:var(--text-muted)] hover:bg-ink-800/30 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-brand-400";
const box = "overflow-hidden rounded-lg border border-[color:var(--line)]";
export function CanvasOverview({ thread, results, files, agents = [], onResult, onResource, onAgent, onBrowse, onReveal }: {
  thread: ChatThread | null; results: ChatResult[]; files: WorkbenchFile[]; agents?: AgentWork[];
  onResult: (id: string) => void; onResource: (id: string) => void; onAgent?: (id: string) => void;
  onBrowse: (tab: "files" | "preview") => void; onReveal?: (id: string) => void;
}) {
  const locale = useLocale(), zh = locale.startsWith("zh");
  const [timelineOpen, setTimelineOpen] = useState(true);
  const [allSteps, setAllSteps] = useState(false);
  const [selectedResult, setSelectedResult] = useState("");
  const steps = useMemo(() => workbenchTimeline(thread, zh), [thread, zh]);
  const latestMessage = [...(thread?.messages || [])].reverse().find((m) => m.role === "assistant");
  const busy = Boolean(latestMessage?.role === "assistant" && latestMessage.loading) || agents.some((a) => isWorking(a.state));
  const attention = Boolean(latestMessage?.role === "assistant" && latestMessage.error) || agents.some((a) => ["failed", "blocked", "timeout", "interrupted"].includes(a.state));
  const status = attention ? (zh ? "有操作需要处理" : "Needs attention") : busy ? (zh ? "任务进行中" : "Work in progress") : results.length ? (zh ? "结果可供查看" : "Results ready to review") : (zh ? "等待任务输出" : "Waiting for task output");
  const result = results.find((r) => r.id === selectedResult) || results[results.length - 1];
  const original = thread?.messages.find((m) => m.role === "user");
  const grouped = steps.reduce<Record<string, number>>((acc, step) => ({ ...acc, [step.title]: (acc[step.title] || 0) + 1 }), {});
  const summary = Object.entries(grouped).map(([name, count]) => `${name} ${count}`).join(" · ");
  return <div className="h-full overflow-y-auto px-4 py-5" data-testid="canvas-overview">
    <div className="mx-auto max-w-[780px] space-y-5">
      <div><h2 className="break-words text-lg font-semibold leading-snug">{thread?.title || (zh ? "任务工作区" : "Task workspace")}</h2>
        <div className="mt-3 flex flex-wrap items-center gap-x-3 gap-y-1 text-xs"><span className={`inline-flex items-center gap-2 ${attention ? "text-warn" : "text-[color:var(--text-muted)]"}`}><span aria-hidden className={`h-1.5 w-1.5 rounded-full ${attention ? "bg-warn" : busy ? "bg-brand-400 motion-safe:animate-pulse" : "bg-ok"}`} />{status}</span>
          {latestMessage?.ts ? <time className="text-[color:var(--text-muted)]" dateTime={new Date(latestMessage.ts).toISOString()}>{new Intl.DateTimeFormat(locale, { hour: "2-digit", minute: "2-digit" }).format(latestMessage.ts)}</time> : null}
        </div>
      </div>
      <section className={box} aria-label={zh ? "执行时间线" : "Execution timeline"}>
        <button type="button" onClick={() => setTimelineOpen((v) => !v)} aria-expanded={timelineOpen} aria-controls="workbench-timeline-items" className="flex min-h-12 w-full items-center justify-between gap-3 bg-ink-800/15 px-3 py-2 text-left focus-visible:ring-2 focus-visible:ring-inset focus-visible:ring-brand-400"><span><span className="block text-sm font-medium">{zh ? "执行时间线" : "Execution timeline"}<span className="ml-2 text-xs text-[color:var(--text-muted)]">{steps.length}</span></span>{!timelineOpen && summary ? <span className="mt-1 block text-xs text-[color:var(--text-muted)]">{summary}</span> : null}</span><ChevronRightIcon size={14} className={timelineOpen ? "rotate-90" : ""} /></button>
        <div id="workbench-timeline-items" hidden={!timelineOpen} className="border-t border-[color:var(--line)] px-3 py-2">
          {!steps.length ? <p className="py-3 text-sm text-[color:var(--text-muted)]">{zh ? "尚未记录工具操作。" : "No tool operations have been recorded."}</p> : <ol>{(allSteps ? steps : steps.slice(-8)).map((step) => <li key={step.id} className="relative flex gap-3 py-2" data-testid="workbench-timeline-step">
            <div aria-hidden className="relative flex w-4 shrink-0 justify-center"><span className="absolute bottom-[-8px] top-4 w-px bg-[color:var(--line)]" /><span className={`z-10 mt-2 h-2 w-2 rounded-full ${step.failed ? "bg-danger" : step.pending ? "bg-brand-400 motion-safe:animate-pulse" : "bg-ink-400"}`} /></div>
            <details className="min-w-0 flex-1" open={step.failed || undefined}><summary className="flex min-h-9 cursor-pointer list-none items-start gap-2 text-xs focus-visible:ring-2 focus-visible:ring-brand-400"><span className="min-w-0 flex-1"><span className="block text-sm">{step.title}</span><span className="mt-1 block break-all text-[color:var(--text-muted)]">{step.subject}</span></span><span className={`shrink-0 pt-0.5 ${step.failed ? "text-danger" : "text-[color:var(--text-muted)]"}`}>{step.state}</span></summary>
              {step.detail ? <div className="max-h-56 overflow-auto py-2 text-xs"><StreamedMarkdown text={step.detail} /></div> : null}
              <div className="flex items-center justify-between">{step.elapsed !== undefined ? <span className="text-xs text-[color:var(--text-muted)]">{step.elapsed} ms</span> : <span />}{onReveal ? <button type="button" className={quiet} onClick={() => onReveal(step.turnId)}>{zh ? "定位到对话" : "Show operation in conversation"}</button> : null}</div>
            </details>
          </li>)}</ol>}
          {steps.length > 8 ? <button type="button" className={`${quiet} w-full`} onClick={() => setAllSteps((v) => !v)}>{allSteps ? (zh ? "只看最近 8 项" : "Show latest 8") : (zh ? `查看全部 ${steps.length} 项` : `Show all ${steps.length} operations`)}</button> : null}
        </div>
      </section>
      {files.length ? <section aria-label={zh ? "文件与预览" : "Files and previews"}><div className="flex items-center justify-between"><h3 className="text-sm font-medium">{zh ? "文件与改动" : "Files and changes"}<span className="ml-2 text-xs text-[color:var(--text-muted)]">{files.length}</span></h3><button type="button" className={quiet} onClick={() => onBrowse("files")}>{zh ? "审阅文件" : "Review files"}</button></div>
        {files.slice(0, 4).map((file) => <button key={file.key} type="button" onClick={() => onResource(file.latest.id)} className="flex min-h-10 w-full items-center gap-2 border-b border-[color:var(--line)] text-left text-xs focus-visible:ring-2 focus-visible:ring-brand-400"><FileIcon size={14} /><span className="min-w-0 flex-1 truncate">{file.path}</span><span className="shrink-0 text-[color:var(--text-muted)]">{file.changed ? (zh ? "已修改" : "Modified") : (zh ? "查看" : "Preview")}</span><ChevronRightIcon size={12} /></button>)}
      </section> : null}
      {agents.length ? <section aria-label={zh ? "协作成员" : "Collaborators"}><h3 className="mb-2 text-sm font-medium">{zh ? "协作成员" : "Collaborators"}</h3>{agents.map((agent) => <button key={agent.id} type="button" disabled={!onAgent} onClick={() => onAgent?.(agent.id)} className="flex min-h-10 w-full items-center gap-2 border-b border-[color:var(--line)] text-left text-xs focus-visible:ring-2 focus-visible:ring-brand-400"><MessagesIcon size={14} /><span className="min-w-0 flex-1 truncate">{agent.name}</span><span className="text-[color:var(--text-muted)]">{isWorking(agent.state) ? (zh ? "进行中" : "Running") : agent.state === "completed" ? (zh ? "已完成" : "Completed") : (zh ? "需要查看" : "Review status")}</span><ChevronRightIcon size={12} /></button>)}</section> : null}
      {original?.role === "user" ? <details className={box}><summary className="min-h-11 cursor-pointer bg-ink-800/15 px-3 py-3 text-sm font-medium">{zh ? "原始任务" : "Original request"}</summary><div className="max-h-52 overflow-auto border-t border-[color:var(--line)] px-3 py-3 text-sm"><StreamedMarkdown text={original.text} /></div></details> : null}
      <section className={box} aria-label={zh ? "任务结果" : "Task results"}>
        <div className="flex min-h-12 items-center justify-between gap-2 bg-ink-800/15 px-3"><h3 className="text-sm font-medium">{zh ? "最终结果" : "Final result"}</h3>{result ? <button type="button" className={quiet} onClick={() => onResult(result.id)}>{zh ? "打开完整预览" : "Open full preview"}</button> : null}</div>
        {result ? <><div className="flex items-center gap-2 border-t border-[color:var(--line)] px-3 py-2"><ChoiceSelect aria-label={zh ? "摘要来源" : "Summary source"} value={result.id} onValueChange={setSelectedResult} className="min-h-9 min-w-0 w-full text-xs">{[...results].reverse().map((r) => <option key={r.id} value={r.id}>{r.agentId ? (zh ? "成员 · " : "Agent · ") : (zh ? "主对话 · " : "Main conversation · ")}{r.title}</option>)}</ChoiceSelect></div><div className="max-h-72 overflow-auto border-t border-[color:var(--line)] p-4 text-sm leading-relaxed" data-testid="workbench-summary"><StreamedMarkdown text={result.text} /></div></>
          : <p className="border-t border-[color:var(--line)] p-4 text-sm text-[color:var(--text-muted)]">{zh ? "任务完成后在此展示最终结果。执行中的内容不会标为完成。" : "Final results will appear here when the task finishes. In-progress work is not marked complete."}</p>}
      </section>
    </div>
  </div>;
}
