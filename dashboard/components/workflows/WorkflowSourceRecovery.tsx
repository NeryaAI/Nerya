"use client";

import { useEffect, useState } from "react";
import { useTranslations } from "next-intl";
import type { WorkflowView } from "../../lib/workflowTypes";
import { workflowApi } from "../../lib/workflowApi";
import { readEditDraft, writeEditDraft } from "../../lib/editDrafts";
import { confirm } from "../../lib/dialogs";

/** Invalid YAML never hides its source or grants permission to apply it. */
export function WorkflowSourceRecovery({ data, onSaved, onDirtyChange }: {
  data: WorkflowView; onSaved: (view: WorkflowView) => void; onDirtyChange: (dirty: boolean) => void;
}) {
  const t = useTranslations("workflowRecovery");
  const original = data.recovery!.content;
  const scope = "manifest-repair:" + data.strategy_id + ":" + (data.source.proposal_id || "published") + ":" + data.revision;
  const [source, setSource] = useState(() => readEditDraft<{ source: string }>(scope)?.source ?? original);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const dirty = source !== original;
  useEffect(() => { onDirtyChange(dirty); writeEditDraft(scope, dirty ? { source } : null); }, [dirty, source, scope, onDirtyChange]);
  useEffect(() => () => onDirtyChange(false), [onDirtyChange]);
  async function save() {
    if (busy || !dirty || !data.can_edit) return;
    setBusy(true); setError("");
    try {
      const result = await workflowApi.propose({ strategy_id: data.strategy_id,
        proposal_id: data.source.proposal_id, base_revision: data.revision, manifest_source: source });
      writeEditDraft(scope, null); onDirtyChange(false); onSaved(result.workflow);
    } catch (reason) { setError(reason instanceof Error ? reason.message : String(reason)); }
    finally { setBusy(false); }
  }
  return <section className="min-w-0 space-y-4 p-4" data-testid="workflow-source-recovery" aria-busy={busy}>
    <h2 className="text-base font-medium">{t("title")}</h2>
    <p className="text-sm text-[color:var(--text-muted)]">{t("description")}</p>
    <pre role="alert" className="max-h-48 overflow-auto whitespace-pre-wrap break-words text-xs text-warn">{data.recovery!.diagnostic}</pre>
    <label className="block text-sm">{t("source")}
      <textarea aria-label={t("source")} value={source} onChange={event => setSource(event.target.value)}
        readOnly={!data.can_edit || busy} spellCheck={false}
        className="mt-2 min-h-80 w-full resize-y rounded-lg border border-[color:var(--line)] bg-[color:var(--panel-bg)] p-3 font-mono text-xs" />
    </label>
    {!data.can_edit && <p role="status" className="text-sm text-warn">{t("readOnly")}</p>}
    {error && <p role="alert" className="whitespace-pre-wrap break-words text-sm text-danger">{error}</p>}
    <div className="flex flex-wrap gap-2">
      <button type="button" className="btn btn-primary" disabled={!dirty || busy || !data.can_edit} onClick={() => void save()}>{t(busy ? "saving" : "save")}</button>
      <button type="button" className="btn" disabled={!dirty || busy} onClick={async () => {
        if (await confirm({ title: t("discard"), message: t("discardMessage") })) { setSource(original); setError(""); }
      }}>{t("discard")}</button>
    </div>
  </section>;
}
