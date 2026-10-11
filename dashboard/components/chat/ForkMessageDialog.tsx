"use client";

import { copy as i18nCopy } from "../../lib/i18n";

import { useRef, useState } from "react";
import { useLocale } from "next-intl";
import * as Dialog from "@radix-ui/react-dialog";
import type { UserMessage } from "../../lib/chat";
import { uuid } from "../../lib/chat";
import { callApi } from "../../lib/clientApi";
import { readEditDraft, writeEditDraft } from "../../lib/editDrafts";
import { writeChatDraft } from "../../lib/chatDraft";
import { useUnsavedChanges } from "./useUnsavedChanges";

export function ForkMessageDialog({ sessionId, message, onClose, onCreated }: {
  sessionId: string; message: UserMessage; onClose: () => void; onCreated: (id: string) => void;
}) {
  const zh = useLocale().startsWith("zh");
  const scope = `fork:${sessionId}:${message.backend_message_id}`;
  const [saved] = useState(() => readEditDraft<{ text:string; requestId?:string; original?:string }>(scope));
  const [text,setText] = useState(saved?.text ?? message.text);
  const key = useRef(saved?.original === message.text && saved.requestId ? saved.requestId : uuid()), active = useRef(false);
  const [busy,setBusy] = useState(false), [error,setError] = useState("");
  useUnsavedChanges(text !== message.text);
  async function create() {
    if (active.current || !text.trim()) return;
    active.current = true; setBusy(true); setError("");
    writeEditDraft(scope,{ text,requestId:key.current,original:message.text });
    try {
      const result = await callApi<{ ok: boolean; session_id: string; error?: string }>("/agent/commands/fork", {
        method:"POST", signal:AbortSignal.timeout(15000), body:{ session_id:sessionId,message_id:message.backend_message_id,
          expected_content:message.text,client_request_id:key.current,language:zh?"zh":"en" },
      });
      if (!result.ok || !/^branch_[a-f0-9]{32}$/.test(result.session_id)) throw new Error(result.error || "fork_failed");
      writeChatDraft(result.session_id,{ text,attachments:message.attachments || [] });
      writeEditDraft(scope,null); onCreated(result.session_id);
    } catch { setError(i18nCopy(zh, "copy.components_chat_ForkMessageDialog.001")); }
    finally { active.current = false; setBusy(false); }
  }
  return <Dialog.Root open onOpenChange={open => { if (!open && !busy) onClose(); }}><Dialog.Portal>
    <Dialog.Overlay className="ui-modal-overlay" /><Dialog.Content className="ui-dialog" onEscapeKeyDown={event => { if (busy) event.preventDefault(); }}>
      <Dialog.Title className="text-base font-semibold">{i18nCopy(zh, "copy.components_chat_ForkMessageDialog.002")}</Dialog.Title>
      <Dialog.Description className="mt-2 text-sm leading-6 text-[color:var(--text-muted)]">{i18nCopy(zh, "copy.components_chat_ForkMessageDialog.003")}</Dialog.Description>
      <form onSubmit={event => { event.preventDefault(); void create(); }}>
        <textarea autoFocus aria-label={i18nCopy(zh, "copy.components_chat_ForkMessageDialog.004")} className="input-dark mt-4 min-h-36 w-full" value={text} disabled={busy}
          onChange={event => { setText(event.target.value); writeEditDraft(scope,{ text:event.target.value,requestId:key.current,original:message.text }); }} />
        {error && <p role="alert" className="mt-2 text-xs text-danger">{error}</p>}
        <div className="mt-4 flex justify-end gap-2"><button type="button" className="btn btn-ghost" disabled={busy} onClick={onClose}>{i18nCopy(zh, "copy.components_chat_ForkMessageDialog.005")}</button>
          <button type="submit" data-testid="create-history-branch" className="btn btn-primary" disabled={busy || !text.trim()} aria-busy={busy}>{i18nCopy(zh, "copy.components_chat_ForkMessageDialog.006")}</button></div>
      </form>
    </Dialog.Content></Dialog.Portal></Dialog.Root>;
}
