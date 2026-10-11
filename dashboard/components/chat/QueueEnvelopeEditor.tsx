"use client";

import { copy as i18nCopy } from "../../lib/i18n";

import { useEffect, useRef, useState } from "react";
import { NextIntlClientProvider, useLocale, useMessages } from "next-intl";
import { clientApi } from "../../lib/clientApi";
import { buildChatModelOptions, type ChatModelOption } from "../../lib/chat";
import { commandErrorCode, queueEditDraft, queueEditRequest, type ConversationCommand, type QueueEditDraft } from "../../lib/conversationCommands";
import { commandErrorText } from "../../lib/commandCopy";
import { readEditDraft, writeEditDraft } from "../../lib/editDrafts";
import { getWorkspaceIdentity, workspaceGeneration } from "../../lib/workspaceIdentity";
import { ChatInput } from "./ChatInput";
import { ComposerModelMenu } from "./ComposerRunControls";
import { useUnsavedChanges } from "./useUnsavedChanges";

export function QueueEnvelopeEditor({ command, current, onSave, onClose }: {
  command: ConversationCommand; current?: ConversationCommand; onClose: () => void;
  onSave: (command: ConversationCommand, request: Record<string, unknown>) => Promise<unknown>;
}) {
  const locale = useLocale(), zh = locale.startsWith("zh"), messages = useMessages();
  const scope = "queue:" + command.command_id;
  const [base, setBase] = useState(command);
  const [draft, setDraft] = useState<QueueEditDraft>(() => {
    const saved = readEditDraft<Partial<QueueEditDraft>>(scope);
    return saved && typeof saved.text === "string" ? { ...queueEditDraft(command), ...saved } : queueEditDraft(command);
  });
  const [busy, setBusy] = useState(false), [error, setError] = useState("");
  const [models, setModels] = useState<ChatModelOption[]>([]);
  const mounted = useRef(true);
  const conflict = !current || current.state !== "queued" || current.queue_editable === false || current.revision !== draft.revision;
  useUnsavedChanges(JSON.stringify(draft) !== JSON.stringify(queueEditDraft(base)));
  useEffect(() => {
    mounted.current = true;
    let active = true;
    void Promise.allSettled([clientApi.llmTiers(), clientApi.llmModels(), clientApi.llmConfig()]).then(([tiers, models, config]) => {
      if (active) setModels(buildChatModelOptions({ tiers: tiers.status === "fulfilled" ? tiers.value : null,
        models: models.status === "fulfilled" ? models.value : null, config: config.status === "fulfilled" ? config.value : null }));
    });
    return () => { active = false; mounted.current = false; };
  }, []);
  function update(next: QueueEditDraft) {
    const safe = { ...next, attachments: next.attachments.map(file => {
      const { data_url, text, ...saved } = file; return saved;
    }) };
    setDraft(safe); writeEditDraft(scope, safe);
  }
  async function save() {
    if (busy || conflict || !getWorkspaceIdentity()) return;
    const workspace = getWorkspaceIdentity(), generation = workspaceGeneration();
    setBusy(true); setError("");
    writeEditDraft(scope, draft);
    try {
      await onSave({ ...base, revision: draft.revision }, queueEditRequest(base, draft));
      if (!mounted.current || workspace !== getWorkspaceIdentity() || generation !== workspaceGeneration()) return;
      writeEditDraft(scope, null); onClose();
    } catch (reason) {
      if (mounted.current && workspace === getWorkspaceIdentity() && generation === workspaceGeneration()) setError(commandErrorText(commandErrorCode(reason), zh));
    } finally { if (mounted.current) setBusy(false); }
  }
  const saveLabel = i18nCopy(zh, "copy.components_chat_QueueEnvelopeEditor.001");
  return <div className="space-y-2 py-3" data-testid="queue-envelope-editor">
    <p className="text-sm font-medium">{i18nCopy(zh, "copy.components_chat_QueueEnvelopeEditor.002")}</p>
    {conflict && <div role="alert" className="text-sm text-[color:var(--warn)]">
      <p>{i18nCopy(zh, "copy.components_chat_QueueEnvelopeEditor.003")}</p>
      {current?.state === "queued" && current.queue_editable !== false && <button type="button" disabled={busy} className="btn btn-ghost" onClick={() => {
        setBase(current); update({ ...draft, revision: current.revision }); setError("");
      }}>{i18nCopy(zh, "copy.components_chat_QueueEnvelopeEditor.004")}</button>}
    </div>}
    {command.kind === "resume" ? <>
      <label className="block text-sm">{i18nCopy(zh, "copy.components_chat_QueueEnvelopeEditor.005")}
        <textarea className="w-full" value={draft.text} disabled={busy} onChange={event => update({ ...draft, text: event.target.value })} />
      </label>
      <ComposerModelMenu settings={draft.settings} onSettingsChange={settings => update({ ...draft, settings })} modelOptions={models} disabled={busy} />
      <button type="button" disabled={busy || conflict || !draft.text.trim()} onClick={() => void save()}>{saveLabel}</button>
    </> : <NextIntlClientProvider locale={locale} messages={{ ...messages, chat: { ...(messages.chat as Record<string, string>), send: saveLabel } }}>
      <ChatInput value={draft.text} onChange={text => update({ ...draft, text })} onSend={() => void save()}
        sending={false} submitting={busy} draftLocked={busy} locked={conflict}
        lockMessage={i18nCopy(zh, "copy.components_chat_QueueEnvelopeEditor.006")}
        placeholder={i18nCopy(zh, "copy.components_chat_QueueEnvelopeEditor.007")}
        settings={draft.settings} onSettingsChange={settings => update({ ...draft, settings })} modelOptions={models}
        attachments={draft.attachments} onAttachmentsChange={attachments => update({ ...draft, attachments })} sessionId={command.session_id} />
    </NextIntlClientProvider>}
    <button type="button" className="btn btn-ghost" disabled={busy} onClick={onClose}>{i18nCopy(zh, "copy.components_chat_QueueEnvelopeEditor.008")}</button>
    {error && <p role="alert" className="text-sm text-[color:var(--danger)]">{error}</p>}
  </div>;
}
