"use client";

import { useMemo } from "react";
import { useLocale } from "next-intl";
import { AssistantBubble, UserBubble } from "./ChatMessage";
import { StreamedMarkdown } from "./TurnBlocks";
import { MessagesIcon } from "../icons";
import { agentRuns, childResult, contentValue, pairOperations, readableResult, working, type AgentRun } from "../../lib/agentConversation";
import { AgentDebug, ToolActivity } from "./ReadableExecution";
import type { AgentDetail, AgentMail, AgentWork } from "./useAgentWork";
import type { ChatResult } from "../../lib/chatResults";

function Mail({ mail, names }: { mail: AgentMail; names: Map<string, string> }) {
  const zh = useLocale().startsWith("zh");
  return <article data-testid="agent-mail" className="my-4 text-[13px]">
    <div className="mb-2 flex flex-wrap items-center gap-2 text-xs text-[color:var(--text-muted)]">
      <MessagesIcon size={13} /><span>{names.get(mail.sender) || (zh ? "协作者" : "Collaborator")} → {names.get(mail.recipient) || (zh ? "协作者" : "Collaborator")}</span>
      <span>{mail.status === "queued" ? (zh ? "已排队，等待接收" : "Queued, awaiting delivery") : mail.status === "delivered" ? (zh ? "已送达本轮" : "Delivered this run") : (zh ? "已保存到上下文" : "Saved to context")}</span>
    </div>
    <StreamedMarkdown text={mail.content} />
  </article>;
}

function Run({ row, run, messages, names, onOpenResult }: {
  row: AgentWork; run: AgentRun; messages: AgentMail[]; names: Map<string, string>; onOpenResult?: (result: ChatResult) => void;
}) {
  const zh = useLocale().startsWith("zh");
  const result = childResult(row, run.output, run.attempt, zh);
  const isActive = working(run.state);
  const final = result.text.trim().replace(/\s+/g, " ");
  const complete = run.state === "completed";
  const steps = pairOperations(run.events).filter((step) => {
    if (step.event.kind === "thinking") return false;
    if (step.event.kind !== "text") return true;
    const raw = step.data.text;
    const text = readableResult(raw, zh).trim().replace(/\s+/g, " ");
    return Boolean(text) && (!final || !final.includes(text)) && typeof contentValue(raw) === "string";
  });
  const toolCount = steps.filter((s) => s.event.kind.startsWith("tool_")).length;
  const timeline = [
    ...steps.map((step) => ({ key: `step:${step.key}`, ts: step.event.ts, node: step.event.kind.startsWith("tool_") ? <ToolActivity step={step} state={run.state} />
      : <div className="py-3 text-[14px] leading-relaxed"><StreamedMarkdown text={readableResult(step.data.text || step.data.error, zh)} /></div> })),
    ...messages.map((mail) => ({ key: `mail:${mail.id}`, ts: mail.sender === row.id ? mail.ts : Number((mail as AgentMail & { delivered_at?: number }).delivered_at || mail.ts), node: <Mail mail={mail} names={names} /> })),
  ].sort((a, b) => a.ts - b.ts);
  const trace = timeline.length ? <div data-testid="agent-readable-trace">{timeline.map((item) => <div key={item.key}>{item.node}</div>)}</div> : undefined;
  return <section data-agent-run={run.attempt}>
    <div className="mb-3 mt-5 text-xs text-[color:var(--text-muted)]">{zh ? `第 ${run.attempt} 轮` : `Run ${run.attempt}`}</div>
    {run.instruction || run.attempt === 1 ? <UserBubble msg={{ id: `${row.id}:instruction:${run.attempt}`, role: "user", ts: run.ts * 1000, text: run.instruction || row.title }} /> : null}
    <AssistantBubble msg={{ id: `${row.id}:run:${run.attempt}`, role: "assistant", ts: run.ts * 1000, loading: isActive,
      error: run.state === "failed" ? run.error || (zh ? "本轮未能完成，请查看操作错误后重试。" : "This run failed. Review the operation error before retrying.") : undefined,
      turn: { reply_text: complete ? result.text : "" } }}
      traceContent={trace} traceLabel={toolCount ? (isActive ? (zh ? `执行中 · ${toolCount} 项操作` : `Working · ${toolCount} operations`) : (zh ? `查看执行过程 · ${toolCount} 项操作` : `View execution steps · ${toolCount} operations`)) : undefined}
      onOpenResult={onOpenResult && complete && result.text ? () => onOpenResult(result) : undefined} />
    {!complete && !isActive && result.text ? <section className="my-3 text-sm"><p className="mb-2 text-xs text-warn">{zh ? "本轮未完成，以下为已保存的部分输出" : "This run did not complete. Saved partial output follows."}</p><StreamedMarkdown text={result.text} /></section> : null}
    {run.output != null && !isActive ? <AgentDebug value={run.output} /> : null}
  </section>;
}

export function AgentConversation({ row, detail, rows, onOpenResult }: {
  row: AgentWork; detail: AgentDetail | null; rows: AgentWork[]; onOpenResult?: (result: ChatResult) => void;
}) {
  const zh = useLocale().startsWith("zh");
  const runs = useMemo(() => agentRuns(row, detail), [row, detail]);
  const names = new Map(rows.map((a) => [a.id, a.name]));
  names.set("operator", zh ? "你" : "You"); names.set("lead", "Nerya");
  const messages = detail?.messages || [];
  const mailbox = new Map<number, AgentMail[]>();
  for (const mail of messages.filter((m) => m.status !== "queued")) {
    const deliveredAttempt = (mail as AgentMail & { attempt?: number }).attempt;
    const targetRun = mail.recipient === row.id && deliveredAttempt && runs.some((r) => r.attempt === deliveredAttempt) ? deliveredAttempt
      : [...runs].reverse().find((r) => r.ts <= mail.ts)?.attempt || runs[0]?.attempt;
    if (targetRun) mailbox.set(targetRun, [...(mailbox.get(targetRun) || []), mail]);
  }
  return <div data-testid="agent-conversation">
    {runs.map((run) => <Run key={run.attempt} row={row} run={run} names={names}
      messages={mailbox.get(run.attempt) || []} onOpenResult={onOpenResult} />)}
    {row.legacy && !row.legacySteps?.length ? <p className="mt-3 text-xs text-[color:var(--text-muted)]">{zh ? "历史任务未保存详细过程，仍可查看已保存的结果。" : "The detailed history was not saved. The saved result is still available."}</p> : null}
    {row.legacySteps?.map((step) => <details key={step.key} className="py-2 text-sm"><summary className="cursor-pointer">{step.label}</summary><StreamedMarkdown text={readableResult(step.detail, zh)} /></details>)}
    {messages.filter((mail) => mail.status === "queued").map((mail) => <Mail key={mail.id} mail={mail} names={names} />)}
  </div>;
}
