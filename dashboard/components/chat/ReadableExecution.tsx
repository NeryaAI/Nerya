"use client";
import { copy as i18nCopy } from "../../lib/i18n";

import { useState } from "react";
import { useLocale } from "next-intl";
import { contentValue, readableResult, record, toolPresentation, type ToolStep } from "../../lib/agentConversation";
import { ChevronRightIcon, Icon as NeryaGlyph, CheckIcon, type IconName } from "../icons";
import { StreamedMarkdown } from "./TurnBlocks";
import { portfolioSnapshot } from "../../lib/portfolioSnapshot";
import { PortfolioSnapshot } from "../finance/PortfolioSnapshot";
import { plainToolOutput } from "../../lib/toolSemantics";
import { ToolResultContent } from "./ToolResultContent";
import { CodeOutput } from "./CodeOutput";
import { shellResult } from "../../lib/toolOutputPresentation";
import styles from "./ExecutionTimeline.module.css";

/** Raw transport is an explicit troubleshooting view, never the default conversation. */
export function AgentDebug({ value }: { value: unknown }) {
  const zh = useLocale().startsWith("zh");
  const [open, setOpen] = useState(false);
  return <details onToggle={(e) => setOpen(e.currentTarget.open)} data-testid="agent-debug" className="mt-3 text-xs text-[color:var(--text-muted)]">
    <summary className="w-fit cursor-pointer rounded py-2 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-brand-400">{i18nCopy(zh, "copy.components_chat_ReadableExecution.001")}</summary>
    {open ? <pre data-testid="agent-debug-json" className="max-h-72 overflow-auto whitespace-pre-wrap break-all rounded-md bg-[color:var(--panel-bg)] p-3 text-[11px]">{JSON.stringify(value, null, 2)}</pre> : null}
  </details>;
}

const toolIcons:Record<string,IconName>={skill:"skills",shell:"terminal",search:"search",read:"file",edit:"diff",plan:"check",message:"messages",backtest:"history",strategy:"strategies",market:"candles",research:"document",portfolio:"portfolio",agent:"agents",browser:"globe",mcp:"puzzle",tool:"wrench"};

export function ToolActivity({ step, state, open, onOpenChange }: { step: ToolStep; state: string; open?:boolean; onOpenChange?:(value:boolean)=>void }) {
  const zh = useLocale().startsWith("zh");
  const p = toolPresentation(step, state, zh);
  const targetPath=String(p.payload.path||p.payload.file_path||"");
  const subject=["read","edit"].includes(p.family)&&targetPath===p.subject&&targetPath.includes("/")
    ? `${targetPath.split("/").pop()} · ${targetPath.split("/").slice(-3,-1).join("/")}` : p.subject;
  const phase = p.failed ? "failed" : p.waiting ? "waiting" : p.preparing ? "preparing" : p.pending ? "running" : step.result ? "settled" : "unconfirmed";
  const [choice,setChoice] = useState<boolean|null>(null);
  const expanded = open ?? choice ?? (p.failed || (p.pending && p.family === "shell"));
  const displayValue = step.data.display_result ?? step.data.result;
  const value = contentValue(displayValue);
  const snapshot = !p.failed ? portfolioSnapshot(step.data) : null;
  const shell = shellResult(value);
  const out = { ...record(value), ...shell };
  const command = String(p.payload.command || p.payload.cmd || (p.name === "script_run" ? p.payload.script || p.payload.script_path || "" : ""));
  const diff = typeof out.diff === "string" ? out.diff : "";
  const output = plainToolOutput(shell ? shell.stdout ?? "" : out.stdout ?? step.data.stdout ?? (command && typeof value === "string" ? value : ""));
  const errorOutput = plainToolOutput(shell ? shell.stderr ?? "" : out.stderr ?? step.data.stderr);
  const progress=record(step.data.progress),current=Number(progress.current??progress.completed),total=Number(progress.total);
  const percent=Number.isFinite(current)&&total>0?Math.min(100,Math.max(0,current/total*100)):undefined;
  const progressMessage=String(step.data.progress_message||"");
  const duration=typeof step.data.elapsed_ms === "number" ? step.data.elapsed_ms : undefined;
  return <details open={expanded} onToggle={(e) => {const next=e.currentTarget.open;if(next!==expanded){setChoice(next);onOpenChange?.(next);}}}
    data-testid="agent-operation" data-operation-kind={step.event.kind} data-tool-family={p.family} data-operation-id={step.key} data-state={phase} className="group/step min-w-0 py-0.5">
    <summary className="flex min-h-8 max-[600px]:min-h-11 cursor-pointer list-none flex-col justify-center rounded text-[12px] text-[color:var(--text-base)] outline-none focus-visible:ring-2 focus-visible:ring-brand-400">
      <span className="flex min-h-8 max-[600px]:min-h-11 min-w-0 items-center gap-2">
      <ChevronRightIcon size={12} className="shrink-0 text-[color:var(--text-muted)] motion-safe:transition-transform group-open/step:rotate-90" />
      <NeryaGlyph name={toolIcons[p.family]} size={14} className="shrink-0 text-[color:var(--text-muted)]" />
      <span className="shrink-0 text-[color:var(--text-base)]">{p.title}</span>
      <span className="min-w-0 flex-1 truncate text-[color:var(--text-muted)]" title={p.subject}>{subject}</span>
      {duration!==undefined&&duration>=1000&&<span className={styles.duration}>{`${(duration/1000).toFixed(1)} s`}</span>}
      <span className={`shrink-0 text-xs ${p.failed ? "text-danger" : p.waiting ? "text-warn" : "text-[color:var(--text-muted)]"}`} role={p.pending?"status":undefined}>{p.pending ? <span aria-hidden className={styles.activeDot} /> : null}{p.failed || p.pending || p.waiting || !step.result ? p.state : <CheckIcon size={12} aria-label={p.state}/>}</span>
      </span>
      {!expanded&&p.summary&&<span className={styles.outcome} title={p.summary}>{p.summary}</span>}
      {p.pending&&(progressMessage||percent!==undefined)&&<span className={styles.progress} role="status"><span>{progressMessage}</span>{percent!==undefined&&<progress max={100} value={percent} aria-label={i18nCopy(zh, "copy.components_chat_ReadableExecution.005")}/>}</span>}
    </summary>
    {expanded&&<div className={styles.toolBody}>
      {snapshot ? <PortfolioSnapshot accounts={snapshot} /> : command || output.text || errorOutput.text ? <div className={styles.terminal}>
        <div className={styles.terminalHeader}><span>{i18nCopy(zh, "copy.components_chat_ReadableExecution.006")}</span>{typeof out.exit_code==="number"&&<span>{i18nCopy(zh, "copy.components_chat_ReadableExecution.007")} {out.exit_code}</span>}</div>
        <pre tabIndex={0}>{command?`$ ${command}\n`:""}{output.text}{errorOutput.text&&<span className="text-danger">{`\n${errorOutput.text}`}</span>}</pre>
      </div> : diff ? <CodeOutput diff text={diff} path={String(p.payload.path||out.path||"")}/>
        : value!=null ? <ToolResultContent value={displayValue} family={p.family} path={String(p.payload.path||p.payload.file_path||"")}/>
        : p.family==="plan"?<ToolResultContent value={p.payload} family="plan"/>
        : <p className="py-2 text-[color:var(--text-muted)]">{p.pending ? (i18nCopy(zh, "copy.components_chat_ReadableExecution.002")) : (i18nCopy(zh, "copy.components_chat_ReadableExecution.003"))}</p>}
      {command&&!shell&&typeof value==="object"&&!output.text&&!errorOutput.text&&<ToolResultContent value={displayValue} family={p.family}/>}
      {p.failed && step.data.error ? <p role="status" className="mt-2 whitespace-pre-wrap text-danger">{readableResult(step.data.error, zh)}</p> : null}
      {step.data.truncated || out.truncated || step.data.output_truncated || output.truncated || errorOutput.truncated || diff.length>64000 ? <p className="mt-2 text-warn">{i18nCopy(zh, "copy.components_chat_ReadableExecution.004")}</p> : null}
      <AgentDebug value={{ request: step.event.data, response: step.result?.data }} />
    </div>}
  </details>;
}

export function ReadableExecution({ steps, state }: { steps: ToolStep[]; state: string }) {
  const zh = useLocale().startsWith("zh");
  return <div data-testid="lead-readable-trace">{steps.map((step) => step.event.kind === "text"
    ? <div key={step.key} className="py-3 text-sm leading-relaxed"><StreamedMarkdown text={readableResult(step.data.text, zh)} /></div>
    : <ToolActivity key={step.key} step={step} state={state} />)}</div>;
}
