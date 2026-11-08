"use client";

import { useEffect, useRef, useState } from "react";
import { useTranslations } from "next-intl";
import { callApi } from "../../lib/clientApi";
import { useWorkspaceIdentity, getWorkspaceIdentity, workspaceGeneration } from "../../lib/workspaceIdentity";
import type { AssistantDeliveryEvidence } from "./AssistantTaskReceipt";

export type AssistantTask = {
  task_id: string; title: string; kind: "background" | "scheduled"; state: string;
  task_session_id?: string; command_id?: string; error?: string; output?: unknown; updated_at?: number | string; created_at?: number | string;
  progress?: {note?: string}[]; every_seconds?: number; cron?: string;
  control?: TaskControl;
} & Pick<AssistantDeliveryEvidence, "task_complete" | "delivery_completion" | "unresolved_deliveries">;
export type TaskControl = {
  mandate?: { task_id?: string; objective?: string; execution_mode?: string; scope?: Record<string, unknown> } | null;
  assignment?: { assignment_id?: string; state?: string; attempt?: number } | null;
  report?: { state?: string; summary?: string; human_action?: string; next_action?: string; evidence_refs?: string[] } | null;
  pending_inputs?: Array<{ input_id?: string; state?: string; body?: string }>;
  open_handbacks?: Array<{ handback_id?: string; request?: string; reason?: string; state?: string }>;
};
export const assistantTaskActive = (task: AssistantTask) => ["queued", "running", "stopping"].includes(task.state);

/** Polling keeps completion feedback alive independently of the viewing panel. */
export function useAssistantTasks(sessionId?: string) {
  const workspace = useWorkspaceIdentity(), generation = workspaceGeneration();
  const validWorkspace = () => workspace === getWorkspaceIdentity() && generation === workspaceGeneration();
  const t = useTranslations("assistants");
  const [snapshot, setSnapshot] = useState<{session?: string; rows: AssistantTask[]; loading: boolean; error: string}>({rows: [], loading: false, error: ""});
  const [attempt, setAttempt] = useState(0);
  const [busy, setBusy] = useState("");
  const [actionError, setActionError] = useState("");
  const owner = useRef(sessionId); owner.current = sessionId;
  useEffect(() => {
    let stopped = false; let timer: ReturnType<typeof setTimeout>;
    const controller = new AbortController();
    setBusy(""); setActionError("");
    setSnapshot(previous => ({session: sessionId, rows: previous.session === sessionId ? previous.rows : [], loading: Boolean(sessionId), error: ""}));
    if (!sessionId) return;
    const delivered = new Set<string>();
    async function poll() {
      try {
        const result = await callApi<{ok: boolean; tasks: AssistantTask[]}>("/assistants/tasks?session_id=" + encodeURIComponent(sessionId!), {signal: controller.signal});
        if (!result.ok) throw new Error("assistant_tasks_unavailable");
        if (!stopped && validWorkspace()) {
          setSnapshot({session: sessionId, rows: result.tasks, loading: false, error: ""});
          for (const task of result.tasks) {
            if (task.command_id && ["succeeded", "failed", "cancelled", "blocked"].includes(task.state) && !delivered.has(task.command_id)) {
              // A notification is not acknowledgement that history hydrated.
              // Retry on the next poll if the reader's request failed.
              const commandId = task.command_id;
              window.dispatchEvent(new CustomEvent("nerya:assistant-result", {detail: {sessionId,
                acknowledge: () => { if (!stopped && validWorkspace()) delivered.add(commandId); }}}));
            }
          }
        }
      } catch { if (!stopped) setSnapshot(previous => ({...previous, loading: false, error: t("taskLoadFailed")})); }
      finally { if (!stopped) timer = setTimeout(poll, document.hidden ? 15000 : 2000); }
    }
    void poll(); return () => { stopped = true; controller.abort(); clearTimeout(timer); };
  }, [sessionId, attempt, t, workspace, generation]);
  async function stop(task: AssistantTask) {
    const sid = sessionId;
    if (!sid || busy || task.state === "stopping") return;
    setBusy(task.task_id); setActionError("");
    try {
      const result = await callApi<{ok: boolean; cancel_requested: boolean}>("/assistants/tasks/stop", {method: "POST", body: {session_id: sid, task_id: task.task_id}});
      if (!result.ok || !result.cancel_requested) throw new Error(t("stopUnconfirmed"));
      if (owner.current === sid) setSnapshot(previous => ({...previous, rows: previous.rows.map(row => row.task_id === task.task_id ? {...row, state: "stopping"} : row)}));
    } catch (error) { if (owner.current === sid) setActionError(error instanceof Error ? error.message : t("stopUnconfirmed")); }
    finally { if (owner.current === sid) setBusy(""); }
  }
  return {sessionId, rows: snapshot.session === sessionId ? snapshot.rows : [], loading: Boolean(sessionId) && (snapshot.session !== sessionId || snapshot.loading),
    error: snapshot.session === sessionId ? snapshot.error : "", actionError, busy, stop, retry: () => setAttempt(value => value + 1)};
}
