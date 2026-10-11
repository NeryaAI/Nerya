"use client";

import { copy as i18nCopy } from "../../lib/i18n";

import { useState } from "react";
import { useLocale } from "next-intl";
import type { Reconciliation, useConversationCommands } from "./useConversationCommands";
import { callApi } from "../../lib/clientApi";
import type { ConversationCommand } from "../../lib/conversationCommands";
import { commandErrorCode } from "../../lib/conversationCommands";
import { commandErrorText, commandStateText } from "../../lib/commandCopy";
import styles from "./ConversationControls.module.css";
import { QueueEnvelopeEditor } from "./QueueEnvelopeEditor";
import { useWorkspaceIdentity, workspaceGeneration } from "../../lib/workspaceIdentity";

type Engine = ReturnType<typeof useConversationCommands>;
export function ConversationControls({ engine, onContinue, onReuse }: {
  engine: Engine; onContinue: (turnId: string) => void; onReuse: (text: string) => void;
}) {
  const zh = useLocale().startsWith("zh");
  const [busy, setBusy] = useState("");
  const [error, setError] = useState("");
  const [diagnostics, setDiagnostics] = useState<Record<string, Reconciliation>>({});
  const [evidence, setEvidence] = useState<Record<string, string>>({});
  const workspace = useWorkspaceIdentity(), generation = workspaceGeneration();
  const [editState, setEditing] = useState<{ command: ConversationCommand; workspace: string | null; generation: number } | null>(null);
  const editing = editState?.workspace === workspace && editState?.generation === generation ? editState : null;
  const queue = engine.commands.filter(command => command.state === "queued" && command.kind !== "guide");
  const [dismissedGuidance,setDismissedGuidance]=useState<string[]>([]);
  const guidance = engine.commands.filter(command => command.kind === "guide" && !dismissedGuidance.includes(command.command_id) &&
    ((["queued","delivering","delivered"].includes(command.state)&&command.turn_id===engine.active?.turn_id)||command.state==="not_consumed")).slice(-4);
  const uncertain = engine.commands.filter(command => command.state === "unconfirmed");
  const checkpoint = engine.data?.checkpoint;
  const latestExecution=engine.commands.filter(command=>command.kind!=="guide"&&!["queued","removed"].includes(command.state)).sort((a,b)=>b.created_at-a.created_at)[0];
  const queueBlocked=uncertain.length>0 || ["awaiting_approval","awaiting_input"].includes(latestExecution?.state||engine.data?.queue.pause_reason||"");
  const latestCheckpointCommand=engine.commands.filter(command=>command.kind!=="guide"&&command.turn_id===checkpoint?.turn_id).sort((a,b)=>b.created_at-a.created_at)[0];
  const needsResume=checkpoint&&!engine.active&&latestCheckpointCommand&&["blocked","interrupted"].includes(latestCheckpointCommand.state)&&!latestCheckpointCommand.error;
  async function action(key: string, execute: () => Promise<unknown>) {
    if (busy) return;
    setBusy(key); setError("");
    try { await execute(); } catch (reason) { setError(commandErrorText(commandErrorCode(reason), zh)); }
    finally { setBusy(""); }
  }
  const control = (name: string, command?: ConversationCommand, extra?: Record<string, unknown>) =>
    action(name+(command?.command_id || ""), async () => {
      const response = await engine.control(name, command, extra);
      if (command && response.reconciliation) setDiagnostics(previous => ({ ...previous, [command.command_id]: response.reconciliation! }));
    });
  if (!editing && !queue.length && !guidance.length && !engine.pending.length && !uncertain.length && !needsResume && engine.connection !== "offline") return null;
  return <section className={styles.root} data-testid="conversation-controls" aria-label={i18nCopy(zh, "copy.components_chat_ConversationControls.001")}>
    <div className={styles.inner}>
      {engine.connection === "offline" && <div className={styles.notice} role="status"><span>{commandErrorText("connection_lost", zh)}</span><button type="button" onClick={engine.refresh}>{i18nCopy(zh, "copy.components_chat_ConversationControls.002")}</button></div>}
      {engine.pending.map(item => <div className={styles.notice} key={item.command_id} data-testid="command-delivery-unconfirmed">
        <p>{commandErrorText("delivery_unconfirmed", zh)}</p>
        <div className={styles.actions}><button type="button" disabled={!!busy} onClick={() => void action("check"+item.command_id, () => engine.recover(item))}>{i18nCopy(zh, "copy.components_chat_ConversationControls.003")}</button>
          <button type="button" disabled={!!busy} onClick={() => void action("resend"+item.command_id, () => engine.recover(item, true))}>{i18nCopy(zh, "copy.components_chat_ConversationControls.004")}</button></div>
      </div>)}
      {uncertain.map(command => <div className={styles.notice} key={command.command_id} role="status">
        <span>{i18nCopy(zh, "copy.components_chat_ConversationControls.005")}</span><button type="button" disabled={!!busy} onClick={() => void control("reconcile", command)}>{i18nCopy(zh, "copy.components_chat_ConversationControls.006")}</button>
        {diagnostics[command.command_id]&&<div>
          <p>{diagnostics[command.command_id].status==="lookup_failed"?(i18nCopy(zh, "copy.components_chat_ConversationControls.007")):diagnostics[command.command_id].status==="insufficient_evidence"?(i18nCopy(zh, "copy.components_chat_ConversationControls.008")):(i18nCopy(zh, "copy.components_chat_ConversationControls.009"))}</p>
          <details><summary>{i18nCopy(zh, "copy.components_chat_ConversationControls.010")}</summary>
            <p>{command.command_id} · {command.turn_id}</p>
            <button type="button" disabled={!!busy} onClick={()=>void action("evidence"+command.command_id,async()=>{
              const result=await callApi(diagnostics[command.command_id].evidence_path);
              setEvidence(previous=>({...previous,[command.command_id]:JSON.stringify(result,null,2)}));
            })}>{i18nCopy(zh, "copy.components_chat_ConversationControls.011")}</button>
            {evidence[command.command_id]&&<pre className="max-h-64 overflow-auto whitespace-pre-wrap break-all text-xs">{evidence[command.command_id]}</pre>}
          </details>
        </div>}
      </div>)}
      {queue.length > 0 && <details data-testid="conversation-queue">
        <summary>{i18nCopy(zh, "copy.components_chat_ConversationControls.012", { value0: queue.length })}{engine.data?.queue.paused ? (i18nCopy(zh, "copy.components_chat_ConversationControls.013")) : ""} · {queue[0]?.input.slice(0,80)}</summary>
        <div className={styles.queueHeader}><span>{engine.data?.queue.paused ? (i18nCopy(zh, "copy.components_chat_ConversationControls.014")) : (i18nCopy(zh, "copy.components_chat_ConversationControls.015"))}</span>
          <button type="button" disabled={!!busy || queueBlocked} onClick={() => void control(engine.data?.queue.paused ? "resume" : "pause")}>{engine.data?.queue.paused ? (i18nCopy(zh, "copy.components_chat_ConversationControls.016")) : (i18nCopy(zh, "copy.components_chat_ConversationControls.017"))}</button></div>
        <ol className={styles.queue}>
          {queue.map((command, index) => <li key={command.command_id} data-command-id={command.command_id}>
            <span className={styles.preview}>{command.input || (i18nCopy(zh, "copy.components_chat_ConversationControls.018"))}</span>
              {command.attachments.length > 0 && <small>{i18nCopy(zh, "copy.components_chat_ConversationControls.019", { value0: command.attachments.length })}</small>}
              <div className={styles.actions}>
                <button type="button" disabled={!!busy || command.queue_editable === false} onClick={() => setEditing({ command, workspace, generation })} title={command.queue_editable === false ? (i18nCopy(zh, "copy.components_chat_ConversationControls.020")) : undefined}>{i18nCopy(zh, "copy.components_chat_ConversationControls.021")}</button>
                <button type="button" disabled={!!busy || index === 0} onClick={() => void control("move", command, { before_command_id: queue[index-1]?.command_id })} aria-label={i18nCopy(zh, "copy.components_chat_ConversationControls.022")}>↑</button>
                <button type="button" disabled={!!busy || index === queue.length-1} onClick={() => void control("move", command, { before_command_id: queue[index+2]?.command_id || null })} aria-label={i18nCopy(zh, "copy.components_chat_ConversationControls.023")}>↓</button>
                <button type="button" disabled={!!busy} onClick={() => void control("remove", command)}>{i18nCopy(zh, "copy.components_chat_ConversationControls.024")}</button>
              </div>
          </li>)}
        </ol>
      </details>}
      {editing && <QueueEnvelopeEditor key={workspace+":"+generation+":"+editing.command.command_id} command={editing.command}
        current={engine.commands.find(command => command.command_id === editing.command.command_id)}
        onSave={(command, request) => engine.control("edit", command, { request })} onClose={() => setEditing(null)} />}
      {guidance.map(command => <div key={command.command_id} className={styles.guidance} data-testid="guidance-receipt" data-state={command.state}>
        <span className={styles.preview}>{command.input}</span><span>{commandStateText(command.state, zh)}</span>
        {command.state === "not_consumed" && <button type="button" onClick={() => onReuse(command.input)}>{i18nCopy(zh, "copy.components_chat_ConversationControls.025")}</button>}
        {command.state === "not_consumed"&&<button type="button" aria-label={i18nCopy(zh, "copy.components_chat_ConversationControls.026")} onClick={()=>setDismissedGuidance(ids=>[...ids,command.command_id])}>×</button>}
      </div>)}
      {needsResume && <div className={styles.resume}><span>{i18nCopy(zh, "copy.components_chat_ConversationControls.027")}</span>
        <button type="button" disabled={!!busy || engine.connection !== "online"} title={i18nCopy(zh, "copy.components_chat_ConversationControls.028")} onClick={() => onContinue(checkpoint.turn_id)}>{i18nCopy(zh, "copy.components_chat_ConversationControls.029")}</button></div>}
      {error && <p role="alert" className={styles.error}>{error}</p>}
    </div>
  </section>;
}
