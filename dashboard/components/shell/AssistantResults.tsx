"use client";

import { copy as i18nCopy } from "../../lib/i18n";
import Link from "next/link";
import { useEffect, useRef, useState } from "react";
import { useLocale, useTranslations } from "next-intl";
import { callApi } from "../../lib/clientApi";
import { commandGet, type ConversationCommand } from "../../lib/conversationCommands";
import { finalReplyText, type ChatResult } from "../../lib/chatResults";
import { ChatResultsPanel } from "../chat/ChatResultsPanel";
import { AssistantDeliveryStatus, type AssistantDeliveryEvidence } from "../chat/AssistantTaskReceipt";
import { ChevronLeftIcon } from "../icons";

export type AssistantResult = AssistantDeliveryEvidence & {command_id: string; task_id: string; title: string; summary: string; task_session_id: string; created_at: number};

/** List summaries may be flattened and end before the host's closing marker.
 * Recognize only exact host fields in a bounded terminal preview, including a
 * partial model introduction. This matcher must never process full reply bodies. */
export function assistantResultSummary(row: Pick<AssistantResult, "summary" | "state">, zh = true): string {
  if (!["failed", "error", "aborted", "blocked", "cancelled", "canceled", "interrupted", "stopped", "timed_out"].includes(row.state)) return row.summary;
  const compact = row.summary.slice(0, 1201).trimStart().replace(/\s+/g, " ");
  const host = /(?:^|\s)(?:本轮任务未完成：[^。]+。 执行情况：\d+ 步，\d+ 次工具调用(?:，其中 \d+ 次报错)?。(?: |$)|本轮 Agent 在运行过程中异常退出，未能生成最终回复。 错误类型: \S+ 错误信息: )/.exec(compact);
  if (!host) return row.summary;
  const introduction = compact.slice(0, host.index);
  // Quoted examples, fenced/inline code, report headings and structured data
  // retain their authored preview, even when they contain the exact template.
  if (/[\x60~>"“”‘’「」『』:：#{}\[\]]/.test(introduction)) return row.summary;
  return i18nCopy(zh, "copy.AssistantResultsCopy.summaryIncomplete");
}

/** The selected result uses the same public final projection as chat. */
export function assistantResultPreview(selected: AssistantResult, command: ConversationCommand, zh = true): ChatResult {
  const turn = command.result || undefined;
  const ts = selected.created_at * 1000;
  const text = finalReplyText({id: selected.command_id, role: "assistant", ts, turn, execution_status: command.state})
    || assistantResultSummary({...selected, state: command.state}, zh);
  return {id: selected.command_id, title: selected.title, text, ts,
    taskSessionId: selected.task_session_id, delivery: command.delivery || selected.delivery || null,
    turnId: command.turn_id, evidence: turn ? {artifacts:turn.artifact_index,verifier:turn.verifier_outcome,execution:turn.execution_state} : undefined};
}

export function useAssistantResults(assistantId?: string) {
  const [snapshot, setSnapshot] = useState<{assistant?: string; rows: AssistantResult[]; error: boolean; loading: boolean}>({rows: [], error: false, loading: false});
  const [attempt, setAttempt] = useState(0);
  useEffect(() => {
    let stopped = false; let timer: ReturnType<typeof setTimeout>;
    const controller = new AbortController();
    setSnapshot(previous => ({assistant: assistantId, rows: previous.assistant === assistantId ? previous.rows : [], error: false, loading: Boolean(assistantId)}));
    if (!assistantId) return;
    async function refresh() {
      try {
        const result = await callApi<{ok: boolean; results: AssistantResult[]}>("/assistants/results?assistant_id=" + encodeURIComponent(assistantId!), {signal: controller.signal});
        if (!result.ok) throw new Error("assistant_results_unavailable");
        if (!stopped) setSnapshot({assistant: assistantId, rows: result.results, error: false, loading: false});
      } catch { if (!stopped) setSnapshot(previous => ({...previous, error: true, loading: false})); }
      finally { if (!stopped) timer = setTimeout(refresh, document.hidden ? 15000 : 4000); }
    }
    void refresh(); return () => { stopped = true; controller.abort(); clearTimeout(timer); };
  }, [assistantId, attempt]);
  return {rows: snapshot.assistant === assistantId ? snapshot.rows : [], error: snapshot.assistant === assistantId && snapshot.error,
    loading: Boolean(assistantId) && (snapshot.assistant !== assistantId || snapshot.loading), retry: () => setAttempt(value => value + 1)};
}

export function AssistantResults({assistantId, onOpenFile, requestedResult}: {assistantId: string; onOpenFile?: (path: string)=>void; requestedResult?: {commandId: string} | null}) {
  const t = useTranslations("assistants");
  const zh = useLocale().startsWith("zh");
  const {rows, error, loading} = useAssistantResults(assistantId);
  const [selected, setSelected] = useState<AssistantResult | null>(null);
  const [preview, setPreview] = useState<ChatResult | null>(null);
  const [previewError, setPreviewError] = useState(false);
  const consumed = useRef<typeof requestedResult>(null);
  useEffect(() => { setSelected(null); consumed.current = null; }, [assistantId]);
  useEffect(() => {
    if (!requestedResult || consumed.current === requestedResult) return;
    const row = rows.find(row => row.command_id === requestedResult.commandId);
    if (row) { consumed.current = requestedResult; setSelected(row); }
  }, [rows, requestedResult]);
  useEffect(() => {
    setPreview(null); setPreviewError(false);
    if (!selected) return;
    const controller = new AbortController();
    commandGet(selected.task_session_id, selected.command_id, controller.signal).then(response => {
      if (controller.signal.aborted) return;
      if (!response.ok || !response.command) throw new Error("assistant_result_unavailable");
      setPreview(assistantResultPreview(selected, response.command, zh));
    }).catch(()=>{if(!controller.signal.aborted)setPreviewError(true);});
    return ()=>controller.abort();
  }, [selected, zh]);
  return <section className="flex h-full min-h-0 flex-col" aria-label={t("reportsAndResults")} data-testid="assistant-results">
    {selected ? <>
      <div className="flex shrink-0 items-center justify-between gap-3 border-b border-[color:var(--line)] px-3 py-2">
        <button type="button" className="inline-flex min-h-9 items-center gap-1 rounded px-2 text-xs text-[color:var(--text-muted)] hover:bg-[color:var(--panel-bg)]" onClick={()=>setSelected(null)}><ChevronLeftIcon size={14}/>{t("allResults")}</button>
        <Link className="truncate py-2 text-xs text-[color:var(--text-muted)] underline" href={"/chat/"+encodeURIComponent(selected.task_session_id)}>{t("openTaskConversation")}</Link>
      </div>
      <div className="min-h-0 flex-1">
        {preview ? <ChatResultsPanel results={[preview]} selectedId={preview.id} hidePicker onOpenFile={onOpenFile}/>
          : <p role={previewError ? "alert" : "status"} className="p-4 text-sm text-[color:var(--text-muted)]">{t(previewError ? "resultLoadFailed" : "loading")}</p>}
      </div>
    </> : <div className="min-h-0 flex-1 overflow-y-auto p-3">
      <h2 className="px-2 py-2 text-sm font-medium">{t("reportsAndResults")}</h2>
      {error ? <p role="status" className="px-2 py-3 text-xs text-warn">{t("resultLoadFailed")}</p>
        : loading ? <p role="status" className="px-2 py-3 text-xs text-[color:var(--text-muted)]">{t("loading")}</p>
        : !rows.length && <p className="px-2 py-3 text-xs text-[color:var(--text-muted)]">{t("noResults")}</p>}
      {rows.map(row => <button key={row.command_id} type="button" onClick={()=>setSelected(row)}
        className="block w-full rounded-lg px-3 py-3 text-left hover:bg-[color:var(--panel-bg)]" data-testid="assistant-result" data-state={row.state}>
        <span className="block truncate text-sm">{row.title}</span>
        <span className="mt-1 flex flex-wrap gap-x-2 text-xs text-[color:var(--text-muted)]">
          <span>{t.has("taskStates."+row.state) ? t("taskStates."+row.state) : row.state}</span>
          <AssistantDeliveryStatus task={row}/>
        </span>
        <span className="mt-1 block truncate text-xs text-[color:var(--text-muted)]">{assistantResultSummary(row, zh)}</span>
      </button>)}
    </div>}
  </section>;
}
