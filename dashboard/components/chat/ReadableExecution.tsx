"use client";

import { useEffect, useState } from "react";
import { useLocale } from "next-intl";
import { contentValue, readableResult, record, toolPresentation, type ToolStep } from "../../lib/agentConversation";
import { ChevronRightIcon, FileIcon, SearchIcon, MessagesIcon } from "../icons";
import { StreamedMarkdown } from "./TurnBlocks";
import { portfolioSnapshot } from "../../lib/portfolioSnapshot";
import { PortfolioSnapshot } from "../finance/PortfolioSnapshot";

/** Raw transport is an explicit troubleshooting view, never the default conversation. */
export function AgentDebug({ value }: { value: unknown }) {
  const zh = useLocale().startsWith("zh");
  const [open, setOpen] = useState(false);
  return <details onToggle={(e) => setOpen(e.currentTarget.open)} data-testid="agent-debug" className="mt-3 text-xs text-[color:var(--text-muted)]">
    <summary className="w-fit cursor-pointer rounded py-2 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-brand-400">{zh ? "调试详情" : "Debug details"}</summary>
    {open ? <pre data-testid="agent-debug-json" className="max-h-72 overflow-auto whitespace-pre-wrap break-all rounded-lg bg-ink-900/60 p-3 text-[11px]">{JSON.stringify(value, null, 2)}</pre> : null}
  </details>;
}

export function ToolActivity({ step, state }: { step: ToolStep; state: string }) {
  const zh = useLocale().startsWith("zh");
  const p = toolPresentation(step, state, zh);
  const [expanded, setExpanded] = useState(p.failed);
  useEffect(() => { if (p.failed) setExpanded(true); }, [p.failed]);
  const value = contentValue(step.data.result);
  const snapshot = !p.failed ? portfolioSnapshot(step.data) : null;
  const body = readableResult(value, zh), out = record(value);
  const Icon = p.family === "search" ? SearchIcon : p.family === "message" ? MessagesIcon : FileIcon;
  const command = typeof p.payload.command === "string" ? p.payload.command : "";
  const diff = typeof out.diff === "string" ? out.diff : "";
  const output = typeof out.stdout === "string" ? out.stdout : typeof value === "string" ? value : "";
  const errorOutput = typeof out.stderr === "string" ? out.stderr : "";
  return <details open={expanded} onToggle={(e) => setExpanded(e.currentTarget.open)}
    data-testid="agent-operation" data-operation-kind={step.event.kind} className="group/step border-b border-[color:var(--line)] py-1 last:border-b-0">
    <summary className="flex min-h-10 cursor-pointer list-none items-center gap-2 rounded text-[13px] text-[color:var(--text-base)] outline-none focus-visible:ring-2 focus-visible:ring-brand-400">
      <ChevronRightIcon size={12} className="shrink-0 text-[color:var(--text-muted)] motion-safe:transition-transform group-open/step:rotate-90" />
      <Icon size={14} className="shrink-0 text-[color:var(--text-muted)]" />
      <span className="shrink-0">{p.title}</span>
      <span className="min-w-0 flex-1 truncate text-[color:var(--text-muted)]" title={p.subject}>{p.subject}</span>
      <span className={`shrink-0 text-xs ${p.failed ? "text-danger" : "text-[color:var(--text-muted)]"}`}>{p.pending ? <span aria-hidden className="mr-1.5 inline-block h-1.5 w-1.5 rounded-full bg-brand-400 motion-safe:animate-pulse" /> : null}{p.state}</span>
    </summary>
    <div className="pb-3 pl-6 pr-1 text-[13px] leading-relaxed">
      {snapshot ? <PortfolioSnapshot accounts={snapshot} /> : command ? <pre className="my-2 max-h-80 overflow-auto whitespace-pre-wrap break-words rounded-lg bg-ink-900/60 p-3 font-mono text-xs">$ {command}{output ? `\n${output}` : ""}{errorOutput ? `\n${errorOutput}` : ""}</pre>
        : diff ? <pre className="my-2 max-h-80 overflow-auto whitespace-pre-wrap break-words rounded-lg bg-ink-900/60 p-3 text-xs">{diff}</pre>
        : body ? <div className="max-h-96 overflow-y-auto"><StreamedMarkdown text={body} /></div>
        : <p className="py-2 text-[color:var(--text-muted)]">{p.pending ? (zh ? "正在等待操作返回。" : "Waiting for the operation to return.") : (zh ? "这条操作没有保存可读输出。" : "No readable output was saved for this operation.")}</p>}
      {p.failed && step.data.error ? <p role="status" className="mt-2 whitespace-pre-wrap text-danger">{readableResult(step.data.error, zh)}</p> : null}
      {step.data.truncated ? <p className="mt-2 text-warn">{zh ? "此记录过长，后端仅保存了部分内容。" : "This record was truncated by the runtime."}</p> : null}
      <AgentDebug value={{ request: step.event.data, response: step.result?.data }} />
    </div>
  </details>;
}

export function ReadableExecution({ steps, state }: { steps: ToolStep[]; state: string }) {
  const zh = useLocale().startsWith("zh");
  return <div data-testid="lead-readable-trace">{steps.map((step) => step.event.kind === "text"
    ? <div key={step.key} className="py-3 text-sm leading-relaxed"><StreamedMarkdown text={readableResult(step.data.text, zh)} /></div>
    : <ToolActivity key={step.key} step={step} state={state} />)}</div>;
}
