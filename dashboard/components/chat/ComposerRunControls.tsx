"use client";

import { useMemo, useState } from "react";
import { useTranslations } from "next-intl";
import * as Popover from "@radix-ui/react-popover";
import * as Menu from "@radix-ui/react-dropdown-menu";
import type { ChatModelOption, ChatRunSettings, ModelContextWindow, PermissionMode, ReasoningEffort } from "../../lib/chat";
import { CheckIcon, ChevronDownIcon, SearchIcon, SettingsIcon, ShieldCheckIcon, SparkIcon, XIcon } from "../icons";
import { Select } from "../Select";

type ControlProps = {
  settings: ChatRunSettings;
  onSettingsChange: (settings: ChatRunSettings) => void;
  disabled?: boolean;
  size?: "hero" | "docked";
};
const REASONING_LEVELS: ReasoningEffort[] = ["off", "minimal", "low", "medium", "high", "xhigh"];
const CONTEXT_WINDOWS: { value: ModelContextWindow; label: string }[] = [
  { value: 131072, label: "128k" }, { value: 262144, label: "256k" }, { value: 1048576, label: "1M" },
];
const reasoningKey = (level: ReasoningEffort) => `thinkLevel${level.charAt(0).toUpperCase()}${level.slice(1)}`;
const contextLabel = (value?: ModelContextWindow) => CONTEXT_WINDOWS.find((item) => item.value === value)?.label || "256k";
const controlClass = "inline-flex min-h-8 min-w-0 items-center gap-1.5 rounded-lg px-2 text-xs text-[color:var(--text-base)] transition-colors hover:bg-brand-500/10 disabled:cursor-not-allowed disabled:opacity-45";

function selectedModelKey(settings: ChatRunSettings, options: ChatModelOption[]): string {
  return options.find((item) => item.provider === settings.model_provider && item.model === settings.model_id && (item.tier || "") === (settings.model_tier || ""))?.key
    || options.find((item) => item.provider === settings.model_provider && item.model === settings.model_id)?.key
    || options.find((item) => item.tier === settings.model_tier && !settings.model_provider && !settings.model_id)?.key
    || (settings.model_provider || settings.model_id || settings.model_tier ? "__custom" : "__default");
}

export function ComposerPermissionMenu({ settings, onSettingsChange, disabled }: ControlProps) {
  const t = useTranslations("chat");
  const permissionLabel = (mode: PermissionMode) => t(mode === "yolo" ? "fullAccess" : mode === "auto" ? "autonomousMode" : "approveActions");
  const label = permissionLabel(settings.permission_mode);
  return (
    <Menu.Root>
      <Menu.Trigger asChild>
        <button type="button" disabled={disabled} className={controlClass} aria-label={label}>
          <ShieldCheckIcon size={15} className={settings.permission_mode === "yolo" ? "text-[color:var(--warn)]" : ""} />
          <span className="truncate">{label}</span><ChevronDownIcon size={12} />
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
  const [query, setQuery] = useState("");
  const [editKey, setEditKey] = useState<string | null>(null);
  const activeKey = selectedModelKey(settings, modelOptions);
  const active = modelOptions.find((item) => item.key === activeKey);
  const tierName = (tier?: string) => tier === "light" ? tModel("tierLight") : tier === "medium" ? tModel("tierMedium") : tier === "high" ? tModel("tierHigh") : tier || "";
  const modelLabel = activeKey === "__default" ? t("runtimeDefault") : active?.model || settings.model_id || tierName(active?.tier || settings.model_tier) || t("customOverride");
  const filtered = useMemo(() => {
    const needle = query.trim().toLowerCase();
    return modelOptions.filter((item) => !needle || [item.label, item.model, item.provider, item.tier].join(" ").toLowerCase().includes(needle)).slice(0, 80);
  }, [modelOptions, query]);

  function optionsFor(key: string) {
    const override = settings.model_overrides?.[key] ?? {};
    const option = modelOptions.find((item) => item.key === key);
    return {
      reasoning_effort: override.reasoning_effort ?? (key === activeKey ? settings.reasoning_effort : option?.reasoning_effort) ?? "off",
      model_context_window: override.model_context_window ?? (key === activeKey ? settings.model_context_window : undefined) ?? 262144,
    };
  }
  function applyModel(key: string) {
    if (key === "__default") onSettingsChange({ ...settings, ...optionsFor(key), model_tier: "", model_provider: "", model_id: "" });
    else {
      const option = modelOptions.find((item) => item.key === key);
      if (option) onSettingsChange({ ...settings, ...optionsFor(key), model_tier: option.tier || "", model_provider: option.provider, model_id: option.model });
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
    <Popover.Root open={open} onOpenChange={(next) => { setOpen(next); if (!next) { setQuery(""); setEditKey(null); } }}>
      <Popover.Trigger asChild>
        <button type="button" disabled={disabled} className={`${controlClass} max-w-[240px]`} aria-label={tUi("modelOptions")}
          title={`${modelLabel} · ${contextLabel(settings.model_context_window)} · ${t(reasoningKey(settings.reasoning_effort))}`}>
          <SparkIcon size={14} className="shrink-0" /><span className="truncate">{modelLabel}</span><ChevronDownIcon size={12} className="shrink-0" />
        </button>
      </Popover.Trigger>
      <Popover.Portal>
        <Popover.Content aria-label={tUi("modelOptions")} align="end" sideOffset={8} collisionPadding={8}
          className="z-[1250] w-[400px] max-w-[calc(100vw-16px)] overflow-y-auto rounded-xl border border-[color:var(--line-hi)] bg-[color:var(--overlay-surface)] p-2 text-[color:var(--text-base)] shadow-lg"
          style={{ maxHeight: "min(620px, var(--radix-popover-content-available-height))" }}>
          <div className="flex items-center gap-2 px-1 pb-2">
            <label className="flex min-w-0 flex-1 items-center gap-2 rounded-lg border border-[color:var(--line)] px-2 py-2">
              <SearchIcon size={15} className="shrink-0 text-[color:var(--text-muted)]" />
              <input value={query} onChange={(event) => setQuery(event.target.value)} placeholder={t("modelSearchPlaceholder")} aria-label={t("modelSearchPlaceholder")}
                className="min-w-0 flex-1 bg-transparent text-sm outline-none" />
            </label>
            <Popover.Close className="ui-icon-button shrink-0" aria-label={tUi("close")}><XIcon size={16} /></Popover.Close>
          </div>
          <div className="max-h-64 overflow-y-auto">
            {rows.map((row) => <div key={row.key} className={`flex items-center gap-1 rounded-lg ${row.key === activeKey ? "bg-brand-500/10" : "hover:bg-brand-500/5"}`}>
              <button type="button" onClick={() => applyModel(row.key)} aria-pressed={row.key === activeKey} className="flex min-w-0 flex-1 items-center gap-2 px-3 py-3 text-left">
                <span className="min-w-0 flex-1"><span className="block break-words text-sm">{row.title}</span>{row.detail ? <span className="block text-xs text-[color:var(--text-muted)]">{row.detail}</span> : null}</span>
                {row.key === activeKey ? <CheckIcon size={14} className="shrink-0" /> : null}
              </button>
              <button type="button" className="ui-icon-button mr-1 shrink-0" aria-label={`${t("editModelSettings")}: ${row.title}`} aria-expanded={row.key === editKey}
                onClick={() => setEditKey(row.key === editKey ? null : row.key)}><SettingsIcon size={15} /></button>
            </div>)}
            {query.trim() && !filtered.length ? <p role="status" className="px-3 py-3 text-sm text-[color:var(--text-muted)]">{tUi("modelNoMatches")}</p> : null}
          </div>
          {editKey ? <section aria-label={t("optionsTitle")} className="mt-2 space-y-3 border-t border-[color:var(--line)] p-3">
            <h3 className="text-sm font-semibold">{rows.find((row) => row.key === editKey)?.title || tUi("modelOptions")}</h3>
            <label className="block space-y-1 text-xs"><span>{t("think")}</span>
              <Select value={optionsFor(editKey).reasoning_effort} onChange={(value) => patchOptions(editKey, { reasoning_effort: value })}
                ariaLabel={t("think")} options={REASONING_LEVELS.map((value) => ({ value, label: t(reasoningKey(value)) }))} />
            </label>
            <label className="block space-y-1 text-xs"><span>{t("contextLength")}</span>
              <Select value={String(optionsFor(editKey).model_context_window)} onChange={(value) => patchOptions(editKey, { model_context_window: Number(value) as ModelContextWindow })}
                ariaLabel={t("contextLength")} options={CONTEXT_WINDOWS.map((item) => ({ value: String(item.value), label: item.label }))} />
            </label>
          </section> : null}
          <div className="mt-2 border-t border-[color:var(--line)] px-1 pt-1">
            <a href="/settings#models" target="_blank" rel="noreferrer" className="flex min-h-10 items-center gap-2 rounded-lg px-3 text-sm text-[color:var(--text-muted)] hover:bg-brand-500/5">
              <SparkIcon size={15} />{t("addCustomProvider")}<span aria-hidden className="ml-auto">↗</span>
            </a>
          </div>
        </Popover.Content>
      </Popover.Portal>
    </Popover.Root>
  );
}
