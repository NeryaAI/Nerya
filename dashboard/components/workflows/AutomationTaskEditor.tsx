"use client";

import { useTranslations } from "next-intl";
import { Advanced } from "../Page";
import { deliveryExtrasError, jsonFieldError, type ScheduleDraft, type ScheduleKind, type Translate } from "../../lib/automationDraft";
import { ScheduleFields } from "./ScheduleFields";
import styles from "./WorkflowForms.module.css";

export function AutomationTaskEditor({ mode, draft, busy, t, tCommon, onChange, onCancel, onSave }: {
  mode: "create" | "edit"; draft: ScheduleDraft; busy: string | null;
  t: Translate; tCommon: Translate; onChange: (draft: ScheduleDraft) => void; onCancel: () => void; onSave: () => void;
}) {
  const w = useTranslations("workflowUpgrade");
  function patch(next: Partial<ScheduleDraft>) {
    const deliveryEdited = Object.keys(next).some(key => key.startsWith("delivery") || key === "extraDeliveryJson");
    onChange({ ...draft, ...next,
      ...(deliveryEdited ? { deliveryEdited: true } : {}),
      ...(next.sourceRequest !== undefined && draft.usePromptOverride ? { overrideAcknowledged: false } : {}),
      ...(next.generatedPrompt !== undefined ? { overrideAcknowledged: true } : {}),
    });
  }
  const jsonError = jsonFieldError(draft.triggerPayloadJson, t) || (draft.sessionKind === "script" ? jsonFieldError(draft.scriptArgsJson, t) : null) || deliveryExtrasError(draft.extraDeliveryJson, t);
  const canSave = busy !== "save" && !jsonError && (!draft.usePromptOverride || draft.overrideAcknowledged && draft.generatedPrompt.trim()) && (draft.sessionKind !== "agent" || draft.sourceRequest.trim());
  return <section className={styles.editor} data-testid="automation-task-editor">
    <header className={styles.editorHeader}><div><h2>{mode === "create" ? w("createAutomation") : w("definition")}</h2><p>{w(draft.enabled ? "enabledHint" : "disabledHint")}</p></div><button type="button" className="btn-ghost text-sm" disabled={busy === "save"} onClick={onCancel}>{tCommon("cancel")}</button></header>
    <fieldset disabled={busy === "save"}>
      {mode === "create" && draft.sessionKind === "agent" && !draft.sourceRequest && <div className={styles.templates} aria-label={w("templates")}>{(["morning", "risk", "data"] as const).map(template => <button type="button" key={template} onClick={() => patch({ title: w(`${template}Title`), sourceRequest: w(`${template}Goal`) })}>{w(`${template}Title`)}</button>)}</div>}
      <section className={styles.section}>
        <label className={styles.field}><span>{w("title")}</span><input className="input-dark" maxLength={200} value={draft.title} placeholder={w("titlePlaceholder")} onChange={event => patch({ title: event.target.value })} /></label>
        {draft.sessionKind === "agent" && <label className={`${styles.field} mt-5`}><span>{w("goal")}</span><textarea className={`input-dark ${styles.goal}`} value={draft.sourceRequest} placeholder={w("goalPlaceholder")} onChange={event => patch({ sourceRequest: event.target.value })} /></label>}
        {draft.sessionKind === "script" && <label className={`${styles.field} mt-5`}><span>{t("scriptId")}</span><input className="input-dark" value={draft.scriptId} onChange={event => patch({ scriptId: event.target.value, target: `script:${event.target.value}` })} /></label>}
      </section>
      <ScheduleFields value={draft} onChange={patch} />
      <section className={styles.section}><h3>{w("delivery")}</h3><div className={styles.grid}>
        <label className={styles.field}><span>{t("fieldDeliveryKind")}</span><select className="input-dark" value={draft.deliveryKind} onChange={event => patch({ deliveryKind: event.target.value as ScheduleDraft["deliveryKind"], ...(event.target.value === "none" ? { extraDeliveryJson: "" } : {}) })}>{[["none", "deliveryNone"], ["gateway", "deliveryGateway"], ["messages", "deliveryMessages"], ["webhook", "deliveryWebhook"]].map(([value, label]) => <option key={value} value={value}>{t(label)}</option>)}</select></label>
        {draft.deliveryKind === "gateway" && <label className={styles.field}><span>{t("fieldDeliveryPlatform")}</span><input className="input-dark" value={draft.deliveryPlatform} onChange={event => patch({ deliveryPlatform: event.target.value })} /></label>}
        {["gateway", "messages"].includes(draft.deliveryKind) && <label className={styles.field}><span>{t("fieldDeliveryChannel")}</span><input className="input-dark" value={draft.deliveryChannel} onChange={event => patch({ deliveryChannel: event.target.value })} /></label>}
        {draft.deliveryKind === "webhook" && <label className={styles.field}><span>{t("fieldDeliveryUrl")}</span><input className="input-dark" type="url" value={draft.deliveryUrl} onChange={event => patch({ deliveryUrl: event.target.value })} /></label>}
      </div></section>
      <section className={styles.section}><h3>{w("scope")}</h3><p className="text-sm leading-7 text-[color:var(--text-muted)]">{w("scopeHint")}</p></section>
      <Advanced title={w("advanced")} defaultOpen={draft.sessionKind === "trigger"}>
        <div className="space-y-5 py-3">
          <div className={styles.grid}>
            <label className={styles.field}><span>{t("metricKind")}</span><select className="input-dark" disabled={mode === "edit"} value={draft.sessionKind} onChange={event => { const kind = event.target.value as ScheduleKind; patch({ sessionKind: kind, kind: `${kind}.task`, target: kind === "script" ? `script:${draft.scriptId}` : kind === "agent" ? "agent" : "main" }); }}>{["agent", "script", "trigger"].map(kind => <option key={kind} value={kind}>{t(`kind${kind[0].toUpperCase()}${kind.slice(1)}`)}</option>)}</select></label>
            <label className={styles.field}><span>{t("fieldId")}</span><input className="input-dark font-mono" disabled={mode === "edit"} value={draft.id} onChange={event => patch({ id: event.target.value })} /></label>
            <label className={styles.field}><span>{t("fieldKind")}</span><input className="input-dark" value={draft.kind} onChange={event => patch({ kind: event.target.value })} /></label>
            <label className={styles.field}><span>{t("fieldTarget")}</span><input className="input-dark" value={draft.target} onChange={event => patch({ target: event.target.value })} /></label>
          </div>
          {draft.sessionKind === "agent" && <>
            <label className="flex items-center gap-2 text-sm"><input type="checkbox" checked={draft.usePromptOverride} onChange={event => patch({ usePromptOverride: event.target.checked, generatedPrompt: draft.generatedPrompt || draft.sourceRequest, overrideAcknowledged: true })} />{w("promptOverride")}</label>
            {draft.usePromptOverride && <><p className="text-sm text-warn">{w("overrideNotice")}</p><label className={styles.field}><span>{w("effectivePrompt")}</span><textarea className={`input-dark ${styles.goal}`} value={draft.generatedPrompt} onChange={event => patch({ generatedPrompt: event.target.value })} /></label>{!draft.overrideAcknowledged && <div role="alert" className="text-sm text-warn"><p>{w("overrideStale")}</p><button type="button" className="mt-2 underline" onClick={() => patch({ overrideAcknowledged: true })}>{w("overrideAcknowledge")}</button></div>}</>}
            {!!draft.originalSourceRequest && draft.originalSourceRequest !== draft.sourceRequest && <details className="text-sm"><summary>{w("originalRequest")}</summary><p className="mt-2 whitespace-pre-wrap text-[color:var(--text-muted)]">{draft.originalSourceRequest}</p></details>}
            <div className={styles.grid}>
              <label className={styles.field}><span>{t("fieldSessionMode")}</span><select className="input-dark" value={draft.sessionMode} onChange={event => patch({ sessionMode: event.target.value as ScheduleDraft["sessionMode"] })}>{[["reuse", "sessionReuse"], ["ephemeral", "sessionEphemeral"], ["fanout", "sessionFanout"]].map(([value, label]) => <option value={value} key={value}>{t(label)}</option>)}</select></label>
              {draft.sessionMode === "reuse" && <label className={styles.field}><span>{t("fieldSessionId")}</span><input className="input-dark" value={draft.sessionId} onChange={event => patch({ sessionId: event.target.value })} /></label>}
              {draft.sessionMode === "fanout" && <label className={styles.field}><span>{t("fieldSessionIds")}</span><input className="input-dark" value={draft.sessionIds} onChange={event => patch({ sessionIds: event.target.value })} /></label>}
              <label className={styles.field}><span>{t("fieldAttachedSkills")}</span><input className="input-dark" value={draft.attachedSkills} onChange={event => patch({ attachedSkills: event.target.value })} /></label>
              <label className={styles.field}><span>{t("fieldModelProvider")}</span><input className="input-dark" value={String(draft.execution.model_provider || "")} onChange={event => patch({ execution: { ...draft.execution, model_provider: event.target.value || null } })} /></label>
              <label className={styles.field}><span>{t("fieldModelId")}</span><input className="input-dark" value={String(draft.execution.model_id || "")} onChange={event => patch({ execution: { ...draft.execution, model_id: event.target.value || null } })} /></label>
              <label className={styles.field}><span>{t("fieldPermissionMode")}</span><select className="input-dark" value={String(draft.execution.permission_mode || "default")} onChange={event => patch({ execution: { ...draft.execution, permission_mode: event.target.value } })}>{["default", "auto", "yolo"].map(value => <option key={value} value={value}>{value}</option>)}</select></label>
              <label className={styles.field}><span>{t("fieldRequiredFiles")}</span><textarea className="input-dark" value={Array.isArray(draft.execution.required_files) ? draft.execution.required_files.join("\n") : ""} onChange={event => patch({ execution: { ...draft.execution, required_files: event.target.value.split("\n").map(value => value.trim()).filter(Boolean) } })} /></label>
            </div>
          </>}
          <div className={styles.grid}>
            <label className={styles.field}><span>{draft.sessionKind === "trigger" ? t("fieldPayload") : t("fieldPayloadExtra")}</span><textarea className="input-dark min-h-24 font-mono text-xs" value={draft.triggerPayloadJson} onChange={event => patch({ triggerPayloadJson: event.target.value })} /></label>
            {draft.sessionKind === "script" && <label className={styles.field}><span>{t("scriptArgs")}</span><textarea className="input-dark min-h-24 font-mono text-xs" value={draft.scriptArgsJson} onChange={event => patch({ scriptArgsJson: event.target.value })} /></label>}
            <label className={styles.field}><span>{t("fieldDeliveryExtra")}</span><textarea className="input-dark min-h-24 font-mono text-xs" value={draft.extraDeliveryJson} placeholder="[]" onChange={event => patch({ extraDeliveryJson: event.target.value })} /></label>
            <label className={styles.field}><span>{t("fieldTtl")}</span><input className="input-dark" type="number" min={0} value={draft.ttlSeconds} onChange={event => patch({ ttlSeconds: event.target.value })} /></label>
          </div>
          {jsonError && <p role="alert" className="text-sm text-danger">{jsonError}</p>}
        </div>
      </Advanced>
      <label className="my-5 flex items-start gap-3 text-sm"><input type="checkbox" className="mt-1" checked={draft.enabled} onChange={event => patch({ enabled: event.target.checked })} /><span>{w("enableExplicit")}<small className="mt-1 block text-[color:var(--text-muted)]">{w(draft.enabled ? "enabledHint" : "disabledHint")}</small></span></label>
    </fieldset>
    <footer className={styles.footer}><p>{w("configurationOnly")}</p><button type="button" className="btn-primary" disabled={!canSave} onClick={onSave}>{busy === "save" ? tCommon("saving") : mode === "create" && !draft.enabled ? w("saveDisabled") : w("saveChanges")}</button></footer>
  </section>;
}
