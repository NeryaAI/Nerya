"use client";

import { useEffect, useState } from "react";
import { useTranslations } from "next-intl";
import { clientApi, type LlmProviderProfile, type LlmTierConfig } from "../../lib/clientApi";
import { confirm } from "../../lib/dialogs";
import { Pill } from "../Page";
import { Select } from "../Select";
import { Field } from "./SettingsFields";
import { providerDraftOf, providerPatch, type ProviderDraft } from "./providerManagementState";

export function ProviderManagementPanel({ profiles, catalog, defaultEndpoints, disabled, onSaved, onRefreshed, onBusyChange, onDirtyChange }: {
  profiles: LlmProviderProfile[];
  catalog: Record<string, string[]>;
  defaultEndpoints: Record<string, string>;
  disabled: boolean;
  onSaved: (profile: LlmProviderProfile, revision: string, tiers: LlmTierConfig[]) => void;
  onRefreshed: (provider: string, models: Array<Record<string, unknown>>) => void;
  onBusyChange: (busy: boolean) => void;
  onDirtyChange: (dirty: boolean) => void;
}) {
  const t = useTranslations("settings.providerManagement");
  const common = useTranslations("common");
  const [editing, setEditing] = useState<LlmProviderProfile | null>(null);
  const [draft, setDraft] = useState<ProviderDraft>({ baseUrl: "", kind: "", key: "" });
  const [busy, setBusy] = useState<{ provider: string; action: "save" | "refresh" } | null>(null);
  const [errors, setErrors] = useState<Record<string, string>>({});
  const [notices, setNotices] = useState<Record<string, string>>({});
  const dirty = Boolean(editing && Object.keys(providerPatch(editing, draft)).length > 1);
  useEffect(() => { onDirtyChange(dirty); return () => onDirtyChange(false); }, [dirty, onDirtyChange]);
  useEffect(() => () => onBusyChange(false), [onBusyChange]);
  async function edit(profile: LlmProviderProfile) {
    if (busy || disabled) return;
    if (dirty && !(await confirm({ title: t("discardTitle"), message: t("discardDescription"), tone: "warning" }))) return;
    setEditing(profile);
    setDraft(providerDraftOf(profile));
    setErrors(previous => ({ ...previous, ...(editing ? { [editing.provider]: "" } : {}), [profile.provider]: "" }));
    setNotices(previous => ({ ...previous, [profile.provider]: "" }));
  }
  async function run(profile: LlmProviderProfile, action: "save" | "refresh") {
    if (busy || disabled || (action === "save" && !dirty)) return;
    if (action === "save" && !draft.baseUrl.trim() && !defaultEndpoints[profile.provider]) {
      setErrors(previous => ({ ...previous, [profile.provider]: t("endpointRequired") })); return;
    }
    if (action === "save" && draft.baseUrl.trim()) {
      try {
        const url = new URL(draft.baseUrl.trim());
        if (!/^https?:\/\//.test(draft.baseUrl.trim()) || !url.hostname) throw new Error();
      } catch { setErrors(previous => ({ ...previous, [profile.provider]: t("invalidEndpoint") })); return; }
    }
    setBusy({ provider: profile.provider, action }); onBusyChange(true);
    setErrors(previous => ({ ...previous, [profile.provider]: "" }));
    setNotices(previous => ({ ...previous, [profile.provider]: "" }));
    try {
      if (action === "refresh") {
        const result = await clientApi.llmProviderModelsRefresh({ provider: profile.provider });
        if (!result.ok) throw new Error(result.detail || t("refreshFailed"));
        onRefreshed(profile.provider, result.models || []);
        setNotices(previous => ({ ...previous, [profile.provider]: t("refreshed", { count: result.count ?? result.models?.length ?? 0 }) }));
      } else {
        const result = await clientApi.llmConfigSet({ providers: [providerPatch(profile, draft)] });
        if (!result.ok) throw new Error(t("saveFailed"));
        const saved = result.provider_profiles?.find(row => row.provider === profile.provider);
        if (!saved) throw new Error(t("saveFailed"));
        onSaved(saved, (result as typeof result & { revision?: string }).revision || "", result.tiers || []);
        setDraft(providerDraftOf(saved));
        setEditing(null);
        setNotices(previous => ({ ...previous, [profile.provider]: t("saved") }));
      }
    } catch {
      // Provider errors may contain request credentials. Only show local copy.
      setErrors(previous => ({ ...previous, [profile.provider]: t(action === "refresh" ? "refreshFailed" : "saveFailed") }));
    } finally { setBusy(null); onBusyChange(false); }
  }
  if (!profiles.length) return null;
  return <section className="mb-5 space-y-3" data-testid="provider-management" aria-label={t("title")}>
    <div><h3 className="text-sm font-medium">{t("title")}</h3><p className="mt-1 text-xs text-[color:var(--text-muted)]">{t("description")}</p></div>
    <ul className="divide-y divide-[color:var(--line)] rounded-lg border border-[color:var(--line)]">
      {profiles.map(profile => {
        const active = editing?.provider === profile.provider;
        const working = busy?.provider === profile.provider;
        return <li key={profile.provider} data-provider-id={profile.provider} className="p-3">
          <div className="flex flex-col gap-3 sm:flex-row sm:items-center">
            <div className="min-w-0 flex-1 space-y-1">
              <div className="flex flex-wrap items-center gap-2"><span className="break-all font-mono text-sm">{profile.provider}</span><Pill tone={profile.provider_key_ref || profile.has_key_ref ? "ok" : "neutral"}>{t(profile.provider_key_ref || profile.has_key_ref ? "keyConfigured" : "keyMissing")}</Pill></div>
              <p className="break-all text-xs text-[color:var(--text-muted)]">{profile.base_url || t("defaultEndpoint")}</p>
              <p className="text-xs text-[color:var(--text-muted)]">{t("modelCount", { count: catalog[profile.provider]?.length || 0 })}</p>
            </div>
            <div className="flex shrink-0 flex-wrap gap-2">
              <button type="button" className="btn btn-secondary" disabled={disabled || Boolean(busy) || (active && dirty)} onClick={() => void run(profile, "refresh")} aria-label={t("refreshProvider", { provider: profile.provider })}>{working && busy.action === "refresh" ? common("refreshing") : t("refresh")}</button>
              <button type="button" className="btn btn-ghost" disabled={disabled || Boolean(busy)} onClick={() => void edit(profile)} aria-label={t("editProvider", { provider: profile.provider })}>{t("edit")}</button>
            </div>
          </div>
          {active && <div className="mt-3 space-y-3 border-t border-[color:var(--line)] pt-3" data-testid="provider-editor">
            <div className="grid gap-3 sm:grid-cols-2">
              <Field label={t("endpoint")} hint={t("endpointHint")}><input className="input-dark w-full font-mono" value={draft.baseUrl} disabled={disabled || Boolean(busy)} onChange={event => setDraft(previous => ({ ...previous, baseUrl: event.target.value }))} aria-label={t("endpoint")} /></Field>
              <Field label={t("apiMode")}><Select value={draft.kind} options={[{ value: "", label: t("automatic") }, { value: "chat_completions", label: "OpenAI compatible" }, { value: "anthropic_messages", label: "Anthropic Messages" }]} onChange={kind => setDraft(previous => ({ ...previous, kind }))} disabled={disabled || Boolean(busy)} ariaLabel={t("apiMode")} /></Field>
              <div className="sm:col-span-2"><Field label={t("apiKey")} hint={t("keyHint")}><input type="password" autoComplete="new-password" className="input-dark w-full font-mono" value={draft.key} disabled={disabled || Boolean(busy)} onChange={event => setDraft(previous => ({ ...previous, key: event.target.value }))} placeholder={t("keyPlaceholder")} aria-label={t("apiKey")} /></Field></div>
            </div>
            <p className="text-xs text-[color:var(--text-muted)]">{t("inheritanceHint")}</p>
            <div className="flex gap-2"><button type="button" className="btn btn-primary" disabled={disabled || Boolean(busy) || !dirty} onClick={() => void run(editing!, "save")}>{working && busy.action === "save" ? common("saving") : t("save")}</button><button type="button" className="btn btn-ghost" disabled={Boolean(busy)} onClick={() => { setErrors(previous => ({ ...previous, [profile.provider]: "" })); setEditing(null); setDraft({ baseUrl: "", kind: "", key: "" }); }}>{common("cancel")}</button></div>
          </div>}
          {errors[profile.provider] && <p role="alert" className="mt-2 text-xs text-danger">{errors[profile.provider]}</p>}
          {notices[profile.provider] && <p role="status" className="mt-2 text-xs text-[color:var(--text-muted)]">{notices[profile.provider]}</p>}
        </li>;
      })}
    </ul>
  </section>;
}
