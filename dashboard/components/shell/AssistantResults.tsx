"use client";

import Link from "next/link";
import { useEffect, useRef, useState } from "react";
import { useTranslations } from "next-intl";
import { callApi } from "../../lib/clientApi";
import { commandGet } from "../../lib/conversationCommands";
import type { ChatResult } from "../../lib/chatResults";
import { ChatResultsPanel } from "../chat/ChatResultsPanel";
import { ChevronLeftIcon } from "../icons";

export type AssistantResult = {command_id: string; task_id: string; title: string; summary: string; state: string; task_session_id: string; created_at: number};

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
      const turn = response.command.result;
      const text = turn?.reply_text || turn?.final_text || selected.summary;
      setPreview({id: selected.command_id, title: selected.title, text, ts: selected.created_at,
        turnId: response.command.turn_id, evidence: turn ? {artifacts:turn.artifact_index,verifier:turn.verifier_outcome,execution:turn.execution_state} : undefined});
    }).catch(()=>{if(!controller.signal.aborted)setPreviewError(true);});
    return ()=>controller.abort();
  }, [selected]);
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
        <span className="block truncate text-sm">{row.title}</span><span className="mt-1 block truncate text-xs text-[color:var(--text-muted)]">{t.has("taskStates."+row.state) ? t("taskStates."+row.state) : row.state} · {row.summary}</span>
      </button>)}
    </div>}
  </section>;
}
