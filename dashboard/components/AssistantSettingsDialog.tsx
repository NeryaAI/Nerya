"use client";

import { useState } from "react";
import { useTranslations } from "next-intl";
import { ModalFrame } from "./ModalFrame";
import { AssistantAvatar } from "./NeryaLogo";
import { AssistantAvatarPicker } from "./AssistantAvatarPicker";
import { assistantsApi, ASSISTANTS_CHANGED, type AssistantProfile } from "../lib/assistants";

export function AssistantSettingsDialog({current, onClose, onSaved}: {current?: AssistantProfile; onClose: () => void; onSaved: (profile: AssistantProfile) => void}) {
  const t = useTranslations("assistants");
  const [draft, setDraft] = useState({name: current?.name || "", avatar: current?.avatar || "", instructions: current?.instructions || ""});
  const [busy, setBusy] = useState(false), [processingAvatar, setProcessingAvatar] = useState(false), [error, setError] = useState("");
  const readableError = error ? (t.has("errors." + error) ? t("errors." + error) : error) : "";
  return <ModalFrame title={t(current ? "edit" : "create")} width="30rem" busy={busy} onClose={onClose} testId="assistant-editor">
    <form className="min-h-0 overflow-y-auto overscroll-contain space-y-5 p-5 sm:p-6" onSubmit={event => {
      event.preventDefault(); if (busy || processingAvatar) return;
      setBusy(true); setError("");
      // Send only personalization fields; hidden models and policies are preserved.
      void assistantsApi.save(draft, current).then(result => {
        window.dispatchEvent(new Event(ASSISTANTS_CHANGED)); onSaved(result.assistant);
      }).catch(error => setError(error instanceof Error ? error.message : String(error))).finally(() => setBusy(false));
    }}>
      <div className="flex items-center gap-3"><AssistantAvatar profile={{id: current?.id || "", name: draft.name, avatar: draft.avatar}} size={48}/><h2 className="text-lg font-semibold">{t(current ? "edit" : "create")}</h2></div>
      {readableError ? <p role="alert" className="text-sm text-danger">{readableError}</p> : null}
      <AssistantAvatarPicker value={draft.avatar} onChange={avatar => setDraft(previous => ({...previous, avatar}))} disabled={busy} main={current?.id === "main"} onProcessing={setProcessingAvatar}/>
      <label className="block text-sm font-medium">{t("name")}<input required maxLength={60} disabled={busy} className="input-dark mt-2 w-full" value={draft.name} onChange={event => setDraft(previous => ({...previous, name: event.target.value}))}/></label>
      <label className="block text-sm font-medium">{t("personalInstructions")}<textarea maxLength={8000} rows={3} disabled={busy} placeholder={t("personalInstructionsHint")} className="input-dark mt-2 w-full resize-y" value={draft.instructions} onChange={event => setDraft(previous => ({...previous, instructions: event.target.value}))}/></label>
      <div className="flex justify-end gap-2 border-t border-[color:var(--line)] pt-4"><button type="button" className="btn btn-ghost" disabled={busy} onClick={onClose}>{t("cancel")}</button><button type="submit" className="btn btn-primary" disabled={busy || processingAvatar}>{t(busy ? "saving" : "save")}</button></div>
    </form>
  </ModalFrame>;
}
