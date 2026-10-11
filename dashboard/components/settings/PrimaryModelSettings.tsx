"use client";
import { copy as i18nCopy } from "../../lib/i18n";

import { useLocale, useTranslations } from "next-intl";
import { ChoiceSelect } from "../ChoiceSelect";
import { Field } from "./SettingsFields";
import type { ModelRouteState } from "./modelSettingsState";
import { ModelRoutePriority } from "./ModelRoutePriority";

type ModelChoice = { provider: string; model: string };
const DEFAULT_MODEL_CONTEXT_WINDOW = 1_048_576;
const MIN_MODEL_CONTEXT_WINDOW = 4_096;
const MAX_MODEL_CONTEXT_WINDOW = 16_777_216;

export function PrimaryModelSettings({ routes, catalog, disabled, onChange, onContextChange, onInheritConnection, onMove, onAdd, primaryOnly = false }: {
  primaryOnly?: boolean;
  routes: ModelRouteState[];
  catalog: Record<string, string[]>;
  disabled: boolean;
  onChange: (index: number, choice: ModelChoice | null) => void;
  onContextChange: (index: number, contextWindow: number) => void;
  onInheritConnection?: (index: number) => void;
  onMove: (from: number, to: number) => void;
  onAdd?: () => void;
}) {
  const zh = useLocale().startsWith("zh");
  const tModel = useTranslations("settings.modelCard");
  const choices = new Map<string, ModelChoice>();
  for (const [provider, models] of Object.entries(catalog)) {
    for (const model of models) choices.set(JSON.stringify([provider, model]), { provider, model });
  }
  for (const route of routes) {
    if (route.provider && route.model) choices.set(JSON.stringify([route.provider, route.model]), route);
  }
  return <div className="space-y-3" data-testid="primary-model-settings">
    <div className="grid gap-3">
      {(primaryOnly ? [0] : Array.from({ length: Math.max(2, routes.length) }, (_, index) => index)).map((index) => {
        const route = routes[index];
        const label = index === 0 ? (i18nCopy(zh, "copy.components_settings_PrimaryModelSettings.001")) : tModel("backupModelLabel", { index });
        return <ModelRoutePriority key={index} group="primary-models" index={index} count={routes.length} disabled={disabled || primaryOnly || !route?.model} onMove={onMove} className="space-y-2">
          <Field label={label}>
            <ChoiceSelect aria-label={label} className="w-full" searchable disabled={disabled || (index > 0 && !routes[0]?.model)}
              value={route?.provider && route.model ? JSON.stringify([route.provider, route.model]) : ""}
              placeholder={i18nCopy(zh, "copy.components_settings_PrimaryModelSettings.003")}
              onValueChange={(value) => onChange(index, value ? choices.get(value) || null : null)}>
              {index > 0 && <option value="">{i18nCopy(zh, "copy.components_settings_PrimaryModelSettings.004")}</option>}
              {[...choices].map(([value, choice]) => <option key={value} value={value}>{choice.model} · {choice.provider}</option>)}
            </ChoiceSelect>
          </Field>
          {route?.model ? <Field label={tModel("contextWindowLabel")} hint={tModel("contextWindowHint")}>
            <input
              className="input-dark w-full font-mono"
              type="number"
              min={MIN_MODEL_CONTEXT_WINDOW}
              max={MAX_MODEL_CONTEXT_WINDOW}
              step={1}
              value={route.context_window ?? DEFAULT_MODEL_CONTEXT_WINDOW}
              disabled={disabled}
              onChange={(event) => {
                const value = Number(event.currentTarget.value);
                if (Number.isFinite(value) && value >= MIN_MODEL_CONTEXT_WINDOW) {
                  onContextChange(index, Math.min(MAX_MODEL_CONTEXT_WINDOW, Math.round(value)));
                }
              }}
              aria-label={`${label} · ${tModel("contextWindowLabel")}`}
            />
          </Field> : null}
          {route?.model && <details className="text-xs text-[color:var(--text-muted)]">
            <summary className="cursor-pointer">{i18nCopy(zh, "copy.components_settings_PrimaryModelSettings.007")}</summary>
            <dl className="mt-2 space-y-1 break-all">
              <div><dt className="inline">{i18nCopy(zh, "copy.components_settings_PrimaryModelSettings.008")}</dt><dd className="inline">{route.base_url || (i18nCopy(zh, "copy.components_settings_PrimaryModelSettings.009"))}</dd></div>
              <div><dt className="inline">{i18nCopy(zh, "copy.components_settings_PrimaryModelSettings.010")}</dt><dd className="inline">{route.effective?.base_url || "—"} · {route.source?.base_url || "—"}</dd></div>
              <div><dt className="inline">{i18nCopy(zh, "copy.components_settings_PrimaryModelSettings.011")}</dt><dd className="inline">{route.provider_key_ref ? (i18nCopy(zh, "copy.components_settings_PrimaryModelSettings.012")) : (i18nCopy(zh, "copy.components_settings_PrimaryModelSettings.013"))} · {route.source?.provider_key_ref || "—"}</dd></div>
            </dl>
            {onInheritConnection && <button type="button" className="btn btn-ghost mt-1" disabled={disabled || (!route.base_url && !route.provider_key_ref && !route.provider_key_env && !route.kind)} onClick={() => onInheritConnection(index)}>{i18nCopy(zh, "copy.components_settings_PrimaryModelSettings.014")}</button>}
          </details>}
        </ModelRoutePriority>;
      })}
    </div>
    {!primaryOnly && onAdd && routes.length >= 2 && <button type="button" className="btn btn-ghost" disabled={disabled || !routes[0]?.model} onClick={onAdd}>{tModel("addRoute")}</button>}
    <p className="text-xs text-[color:var(--text-muted)]">{tModel("priorityHint")}</p>
  </div>;
}
