"use client";
import { Icon as NeryaGlyph } from "../icons";

import { useMemo, useState } from "react";
import { useTranslations } from "next-intl";
import * as Popover from "@radix-ui/react-popover";
import * as Menu from "@radix-ui/react-dropdown-menu";
import { applyModelTierPreset, applyReasoningPreset, type ChatModelOption, type ChatRunSettings, type ModelContextWindow, type PermissionMode, type QuickModelTier, type ReasoningEffort } from "../../lib/chat";
import { CheckIcon, ChevronDownIcon, SearchIcon, SettingsIcon, ShieldCheckIcon, SparkIcon, XIcon } from "../icons";
import { Select } from "../Select";

type ControlProps = {
  settings: ChatRunSettings;
  onSettingsChange: (settings: ChatRunSettings) => void;
  disabled?: boolean;
  compact?:boolean;
  size?: "hero" | "docked";
};
const REASONING_LEVELS: ReasoningEffort[] = ["inherit", "off", "minimal", "low", "medium", "high", "xhigh"];
const CONTEXT_WINDOWS: { value: ModelContextWindow; label: string }[] = [
  { value: 131072, label: "128k" }, { value: 262144, label: "256k" }, { value: 1048576, label: "1M" },
];
const reasoningKey = (level: ReasoningEffort) => `thinkLevel${level.charAt(0).toUpperCase()}${level.slice(1)}`;
const contextLabel = (value?: ModelContextWindow) => {
  const preset = CONTEXT_WINDOWS.find((item) => item.value === value)?.label;
  if (preset) return preset;
  if (!value || !Number.isFinite(value)) return "Auto";
  if (value >= 1_000_000) return `${Number((value / 1_000_000).toFixed(2))}M`;
  return `${Math.round(value / 1_000)}k`;
};
const contextOptions = (value: ModelContextWindow | undefined, automaticLabel: string) => {
  const options = [{ value: "0", label: automaticLabel }, ...CONTEXT_WINDOWS.map((item) => ({ value: String(item.value), label: item.label }))];
  if (value && !CONTEXT_WINDOWS.some((item) => item.value === value)) {
    options.unshift({ value: String(value), label: contextLabel(value) });
  }
  return options;
};
const controlClass = "inline-flex min-h-8 min-w-0 items-center gap-1.5 rounded-lg px-2 text-xs text-[color:var(--text-base)] transition-colors hover:bg-brand-500/10 disabled:cursor-not-allowed disabled:opacity-45";

function selectedModelKey(settings: ChatRunSettings, options: ChatModelOption[]): string {
  // A tier preset delegates routing to the configured tier. It must not
  // accidentally appear as the first concrete model assigned to that tier.
  if (!settings.model_provider && !settings.model_id) return settings.model_tier ? "__tier" : "__default";
  return options.find((item) => item.provider === settings.model_provider && item.model === settings.model_id && (item.tier || "") === (settings.model_tier || ""))?.key
    || options.find((item) => item.provider === settings.model_provider && item.model === settings.model_id)?.key
    || "__custom";
}

export function ComposerPermissionMenu({ settings, onSettingsChange, disabled,compact }: ControlProps) {
  const t = useTranslations("chat");
  const permissionLabel = (mode: PermissionMode) => t(mode === "yolo" ? "fullAccess" : mode === "auto" ? "autonomousMode" : "approveActions");
  const label = permissionLabel(settings.permission_mode);
  return (
    <Menu.Root>
      <Menu.Trigger asChild>
        <button type="button" disabled={disabled} className={controlClass} aria-label={label} title={label} data-composer-permission>
          <ShieldCheckIcon size={15} className={settings.permission_mode === "yolo" ? "text-[color:var(--warn)]" : ""} />
          {!compact&&<span className="truncate">{label}</span>}<ChevronDownIcon size={12} />
        </button>
      </Menu.Trigger>
      <Menu.Portal>
        <Menu.Content className="ui-select-menu w-64" align="start" sideOffset={8} collisionPadding={8} aria-label={t("modeMenuTitle")}>
          <Menu.Label className="px-3 py-2 text-xs text-[color:var(--text-muted)]">{t("modeMenuTitle")}</Menu.Label>
          <Menu.RadioGroup value={settings.permission_mode} onValueChange={(permission_mode) => onSettingsChange({ ...settings, permission_mode: permission_mode as PermissionMode })}>
            {(["default", "auto", "yolo"] as const).map((mode) => <Menu.RadioItem key={mode} value={mode} className="ui-select-option">
              <ShieldCheckIcon size={15} /><span className="flex-1">{permissionLabel(mode)}</span>
              <Menu.ItemIndicator><CheckIcon size={14} /></Menu.ItemIndicator>
            </Menu.RadioItem>)}
          </Menu.RadioGroup>
          <p className="px-3 py-2 text-xs text-[color:var(--text-muted)]">{t("autonomousModeHelp")}</p>
        </Menu.Content>
      </Menu.Portal>
    </Menu.Root>
  );
}

export function ComposerModelMenu({ settings, onSettingsChange, modelOptions, disabled }: ControlProps & { modelOptions: ChatModelOption[] }) {
  const t = useTranslations("chat");
  const tUi = useTranslations("ui");
  const tModel = useTranslations("settings.modelCard");
  const [open, setOpen] = useState(false);
  const [advancedOpen, setAdvancedOpen] = useState(false);
  const [query, setQuery] = useState("");
  const [editKey, setEditKey] = useState<string | null>(null);
  const activeKey = selectedModelKey(settings, modelOptions);
  const active = modelOptions.find((item) => item.key === activeKey);
  const tierName = (tier?: string) => tier === "light" ? tModel("tierLight") : tier === "medium" ? tModel("tierMedium") : tier === "high" ? tModel("tierHigh") : tier || "";
  const quickTiers: { tier: QuickModelTier; label: string }[] = [
    { tier: "", label: t("autoMode") },
    { tier: "light", label: t("quickModelLow") },
    { tier: "medium", label: t("quickModelMedium") },
    { tier: "high", label: t("quickModelHigh") },
  ];
  const quickThoughts: ReasoningEffort[] = ["inherit", "low", "medium", "high"];
  const activeTier = settings.model_provider || settings.model_id ? null : settings.model_tier;
  const modelLabel = activeKey === "__default" ? t("runtimeDefault")
    : activeKey === "__tier" ? (quickTiers.find((item) => item.tier === settings.model_tier)?.label || tierName(settings.model_tier))
    : active?.model || settings.model_id || t("customOverride");
  const thinkingLabel = settings.reasoning_effort === "inherit" ? t("autoMode") : t(reasoningKey(settings.reasoning_effort));
  const filtered = useMemo(() => {
    const needle = query.trim().toLowerCase();
    return modelOptions.filter((item) => !needle || [item.label, item.model, item.provider, item.tier].join(" ").toLowerCase().includes(needle)).slice(0, 80);
  }, [modelOptions, query]);

  function optionsFor(key: string) {
    const override = settings.model_overrides?.[key] ?? {};
    const option = modelOptions.find((item) => item.key === key);
    return {
      reasoning_effort: key === activeKey ? settings.reasoning_effort : override.reasoning_effort ?? option?.reasoning_effort ?? "inherit",
      model_context_window: key === activeKey ? settings.model_context_window : override.model_context_window ?? option?.model_context_window ?? 0,
    };
  }
  function applyModel(key: string) {
    if (key === "__default") onSettingsChange(applyModelTierPreset(settings, ""));
    else if (key !== "__custom") {
      const option = modelOptions.find((item) => item.key === key);
      if (option) {
        const override = settings.model_overrides?.[key];
        onSettingsChange({
          ...settings,
          model_tier: option.tier || "",
          model_provider: option.provider,
          model_id: option.model,
          // Quick thinking remains in effect until the operator explicitly
          // saves a different effort for this particular model.
          reasoning_effort: override?.reasoning_effort ?? settings.reasoning_effort,
          model_context_window: override?.model_context_window ?? option.model_context_window ?? 0,
        });
      }
    }
    setOpen(false);
  }
  function patchOptions(key: string, patch: { reasoning_effort?: ReasoningEffort; model_context_window?: ModelContextWindow }) {
    onSettingsChange({
      ...settings, ...(key === activeKey ? patch : {}),
      model_overrides: { ...settings.model_overrides, [key]: { ...settings.model_overrides?.[key], ...patch } },
    });
  }
  const rows = [
    { key: "__default", title: t("runtimeDefault"), detail: "" },
    ...(activeKey === "__custom" ? [{ key: "__custom", title: modelLabel, detail: settings.model_provider || t("customOverride") }] : []),
    ...filtered.map((item) => ({ key: item.key, title: item.model || item.label, detail: [item.provider, tierName(item.tier)].filter(Boolean).join(" · ") })),
  ];

  return (
    <Popover.Root open={open} onOpenChange={(next) => { setOpen(next); if (!next) { setQuery(""); setEditKey(null); setAdvancedOpen(false); } }}>
      <Popover.Trigger asChild>
        <button type="button" disabled={disabled} className={`${controlClass} max-w-[260px]`} data-composer-model-trigger aria-label={tUi("modelOptions")}
          title={`${modelLabel} · ${t("think")} ${thinkingLabel} · ${contextLabel(settings.model_context_window)}`}>
          <SparkIcon size={14} className="shrink-0" /><span className="truncate">{modelLabel}</span>
          <span className="shrink-0 text-[color:var(--text-muted)]">· {t("think")} {thinkingLabel}</span>
          <ChevronDownIcon size={12} className="shrink-0" />
        </button>
      </Popover.Trigger>
      <Popover.Portal>
        <Popover.Content aria-label={tUi("modelOptions")} align="end" sideOffset={8} collisionPadding={8}
          className="z-[1250] w-[420px] max-w-[calc(100vw-16px)] overflow-y-auto rounded-xl border border-[color:var(--line-hi)] bg-[color:var(--overlay-surface)] p-3 text-[color:var(--text-base)] shadow-lg"
          style={{ maxHeight: "min(620px, var(--radix-popover-content-available-height))" }}>
          <div className="flex items-center justify-between px-1 pb-3">
            <span className="text-sm font-semibold">{tUi("modelOptions")}</span>
            <Popover.Close className="ui-icon-button shrink-0" aria-label={tUi("close")}><XIcon size={16} /></Popover.Close>
          </div>
          <div className="space-y-4 px-1 pb-3">
            <div role="group" aria-label={t("model")}>
              <div className="mb-2 text-xs font-medium text-[color:var(--text-muted)]">{t("model")}</div>
              <div className="grid grid-cols-4 gap-1 rounded-lg bg-[color:var(--panel-bg)] p-1">
                {quickTiers.map(({ tier, label }) => <button key={tier || "auto"} type="button"
                  data-model-tier={tier || "auto"} aria-pressed={activeTier === tier}
                  onClick={() => onSettingsChange(applyModelTierPreset(settings, tier))}
                  className={`min-h-9 rounded-md px-2 text-sm transition-colors ${activeTier === tier ? "bg-brand-500/15 font-semibold text-[color:var(--text-base)] ring-1 ring-brand-500/30" : "text-[color:var(--text-muted)] hover:bg-brand-500/10"}`}>
                  {label}
                </button>)}
              </div>
            </div>
            <div role="group" aria-label={t("think")}>
              <div className="mb-2 text-xs font-medium text-[color:var(--text-muted)]">{t("think")}</div>
              <div className="grid grid-cols-4 gap-1 rounded-lg bg-[color:var(--panel-bg)] p-1">
                {quickThoughts.map((effort) => <button key={effort} type="button"
                  data-thinking-preset={effort} aria-pressed={settings.reasoning_effort === effort}
                  onClick={() => onSettingsChange(applyReasoningPreset(settings, effort, activeKey))}
                  className={`min-h-9 rounded-md px-2 text-sm transition-colors ${settings.reasoning_effort === effort ? "bg-brand-500/15 font-semibold text-[color:var(--text-base)] ring-1 ring-brand-500/30" : "text-[color:var(--text-muted)] hover:bg-brand-500/10"}`}>
                  {effort === "inherit" ? t("autoMode") : t(reasoningKey(effort))}
                </button>)}
              </div>
            </div>
            <p className="text-xs leading-5 text-[color:var(--text-muted)]">{t("quickModelHint")}</p>
          </div>
          <div className="border-t border-[color:var(--line)] pt-2">
            <button type="button" aria-expanded={advancedOpen} aria-controls="composer-advanced-model-settings"
              onClick={() => setAdvancedOpen(value => !value)}
              className="flex min-h-9 w-full items-center justify-between rounded-lg px-2 text-left text-sm hover:bg-brand-500/5">
              <span className="inline-flex items-center gap-2"><SettingsIcon size={15} />{t("advancedSettings")}</span>
              <ChevronDownIcon size={14} className={advancedOpen ? "rotate-180 transition-transform" : "transition-transform"} />
            </button>
          </div>
          {advancedOpen && <div id="composer-advanced-model-settings" className="space-y-3 px-1 pt-2">
            <div className="grid grid-cols-2 gap-3">
              <label className="space-y-1 text-xs"><span>{t("think")}</span>
                <Select value={settings.reasoning_effort}
                  onChange={(value) => onSettingsChange(applyReasoningPreset(settings, value, activeKey))}
                  ariaLabel={t("think")} options={REASONING_LEVELS.map((value) => ({ value, label: t(reasoningKey(value)) }))} />
              </label>
              <label className="space-y-1 text-xs"><span>{t("contextLength")}</span>
                <Select value={String(settings.model_context_window)}
                  onChange={(value) => onSettingsChange({ ...settings, model_context_window: Number(value) })}
                  ariaLabel={t("contextLength")} options={contextOptions(settings.model_context_window, t("contextAutomatic"))} />
              </label>
            </div>
            <div className="border-t border-[color:var(--line)] pt-3">
              <div className="mb-2 text-xs font-medium text-[color:var(--text-muted)]">{t("specificModel")}</div>
              <label className="flex min-w-0 items-center gap-2 rounded-lg border border-[color:var(--line)] px-2 py-2">
                <SearchIcon size={15} className="shrink-0 text-[color:var(--text-muted)]" />
                <input value={query} onChange={(event) => setQuery(event.target.value)} placeholder={t("modelSearchPlaceholder")} aria-label={t("modelSearchPlaceholder")}
                  className="min-w-0 flex-1 bg-transparent text-sm outline-none" />
              </label>
              <div className="mt-2 max-h-52 overflow-y-auto">
                {rows.map((row) => <div key={row.key} className={`flex items-center gap-1 rounded-lg ${row.key === activeKey ? "bg-brand-500/10" : "hover:bg-brand-500/5"}`}>
                  <button type="button" onClick={() => applyModel(row.key)} aria-pressed={row.key === activeKey} className="flex min-w-0 flex-1 items-center gap-2 px-3 py-2 text-left">
                    <span className="min-w-0 flex-1"><span className="block break-words text-sm">{row.title}</span>{row.detail ? <span className="block text-xs text-[color:var(--text-muted)]">{row.detail}</span> : null}</span>
                    {row.key === activeKey ? <CheckIcon size={14} className="shrink-0" /> : null}
                  </button>
                  {row.key !== "__default" && row.key !== "__custom" && <button type="button" className="ui-icon-button mr-1 shrink-0" aria-label={`${t("editModelSettings")}: ${row.title}`} aria-expanded={row.key === editKey}
                    onClick={() => setEditKey(row.key === editKey ? null : row.key)}><SettingsIcon size={15} /></button>}
                </div>)}
                {query.trim() && !filtered.length ? <p role="status" className="px-3 py-3 text-sm text-[color:var(--text-muted)]">{tUi("modelNoMatches")}</p> : null}
              </div>
              {editKey ? <section aria-label={t("optionsTitle")} className="mt-2 space-y-3 rounded-lg border border-[color:var(--line)] p-3">
                <h3 className="text-sm font-semibold">{rows.find((row) => row.key === editKey)?.title || tUi("modelOptions")}</h3>
                <label className="block space-y-1 text-xs"><span>{t("think")}</span>
                  <Select value={optionsFor(editKey).reasoning_effort} onChange={(value) => patchOptions(editKey, { reasoning_effort: value })}
                    ariaLabel={t("think")} options={REASONING_LEVELS.map((value) => ({ value, label: t(reasoningKey(value)) }))} />
                </label>
                <label className="block space-y-1 text-xs"><span>{t("contextLength")}</span>
                  <Select value={String(optionsFor(editKey).model_context_window)} onChange={(value) => patchOptions(editKey, { model_context_window: Number(value) })}
                    ariaLabel={t("contextLength")} options={contextOptions(optionsFor(editKey).model_context_window, t("contextAutomatic"))} />
                </label>
              </section> : null}
            </div>
          </div>}
          <div className="mt-2 border-t border-[color:var(--line)] px-1 pt-1">
            <a href="/settings#models" target="_blank" rel="noreferrer" className="flex min-h-10 items-center gap-2 rounded-lg px-3 text-sm text-[color:var(--text-muted)] hover:bg-brand-500/5">
              <SparkIcon size={15} />{t("addCustomProvider")}<NeryaGlyph name="arrowUpRight" size={16} className="ml-auto" />
            </a>
          </div>
        </Popover.Content>
      </Popover.Portal>
    </Popover.Root>
  );
}
