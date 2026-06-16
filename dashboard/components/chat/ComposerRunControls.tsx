"use client";

import { useMemo, useRef, useState, type ReactNode } from "react";
import { useTranslations } from "next-intl";
import type {
  ChatModelOption,
  ChatRunSettings,
  ModelContextWindow,
  PermissionMode,
  ReasoningEffort,
} from "../../lib/chat";
import {
  CheckIcon,
  ChevronDownIcon,
  SearchIcon,
  ShieldCheckIcon,
  SparkIcon,
} from "../icons";
import { PortalDropdown, useDropdown } from "../PortalDropdown";

type ComposerControlSize = "hero" | "docked";

const REASONING_LEVELS: ReasoningEffort[] = [
  "off",
  "minimal",
  "low",
  "medium",
  "high",
  "xhigh",
];

const CONTEXT_WINDOWS: Array<{ value: ModelContextWindow; label: string }> = [
  { value: 131072, label: "128k" },
  { value: 262144, label: "256k" },
  { value: 1048576, label: "1m" },
];

function reasoningKey(level: ReasoningEffort): string {
  return `thinkLevel${level.charAt(0).toUpperCase()}${level.slice(1)}`;
}

function tierBadge(tier?: string): string {
  const value = String(tier || "").trim();
  const normalized = value.toLowerCase();
  if (!normalized) return "";
  if (normalized === "high") return "5.5";
  if (normalized === "medium") return "5.4";
  if (normalized === "light" || normalized === "low") return "5.3";
  return value;
}

function compactModelName(model?: string): string {
  const raw = String(model || "").trim();
  if (!raw) return "";
  return raw
    .replace(/^claude-/i, "")
    .replace(/^gpt-/i, "GPT-")
    .replace(/^gemini-/i, "Gemini ")
    .replace(/-/g, " ")
    .replace(/\b\d{4}\d{2}\d{2}\b/g, "")
    .replace(/\s+/g, " ")
    .trim();
}

function selectedModelKey(
  settings: ChatRunSettings,
  modelOptions: ChatModelOption[],
): string {
  return (
    modelOptions.find(
      (option) =>
        option.provider === settings.model_provider &&
        option.model === settings.model_id &&
        (option.tier || "") === (settings.model_tier || ""),
    )?.key ||
    modelOptions.find(
      (option) =>
        option.provider === settings.model_provider &&
        option.model === settings.model_id,
    )?.key ||
    modelOptions.find(
      (option) =>
        option.tier === settings.model_tier &&
        !settings.model_provider &&
        !settings.model_id,
    )?.key ||
    (settings.model_provider || settings.model_id || settings.model_tier
      ? "__custom"
      : "__default")
  );
}

function triggerSizeClasses(size: ComposerControlSize): string {
  return size === "hero"
    ? "h-7 px-2 text-[12px]"
    : "h-7 px-2 text-[12px]";
}

function contextLabel(value: ModelContextWindow | undefined): string {
  return CONTEXT_WINDOWS.find((item) => item.value === value)?.label || "256k";
}

export function ComposerPermissionMenu({
  settings,
  onSettingsChange,
  disabled,
  size = "docked",
}: {
  settings: ChatRunSettings;
  onSettingsChange: (settings: ChatRunSettings) => void;
  disabled?: boolean;
  size?: ComposerControlSize;
}) {
  const t = useTranslations("chat");
  const triggerRef = useRef<HTMLButtonElement | null>(null);
  const dropdown = useDropdown();
  const fullAccess = settings.permission_mode === "yolo";
  const label = fullAccess ? t("fullAccess") : t("approveActions");

  function setMode(permission_mode: PermissionMode) {
    onSettingsChange({ ...settings, permission_mode });
    dropdown.close();
  }

  return (
    <>
      <button
        ref={triggerRef}
        type="button"
        aria-haspopup="menu"
        aria-expanded={dropdown.open}
        disabled={disabled}
        onClick={() => {
          if (!disabled) dropdown.toggle();
        }}
        className={[
          "inline-flex min-w-0 shrink items-center gap-1.5 rounded-full font-semibold transition-colors",
          fullAccess ? "text-[#ff8a4c]" : "text-ink-300",
          dropdown.open ? "bg-white/[0.07] text-white" : "hover:bg-white/5 hover:text-white",
          disabled ? "cursor-not-allowed opacity-45" : "cursor-pointer",
          triggerSizeClasses(size),
        ].join(" ")}
        title={label}
      >
        <ShieldCheckIcon size={14} />
        <span className="min-w-0 truncate">{label}</span>
        <ChevronDownIcon
          size={12}
          className={dropdown.open ? "rotate-180 transition-transform" : "transition-transform"}
        />
      </button>
      <PortalDropdown
        open={dropdown.open}
        onClose={dropdown.close}
        anchorRef={triggerRef}
        align="left"
        width={280}
        offset={8}
        className="overflow-hidden rounded-[16px] border border-[color:var(--line-hi)] bg-[color:var(--card)] p-1.5 shadow-[0_18px_38px_rgba(0,0,0,0.34)] backdrop-blur-xl"
      >
        <div className="px-2 pb-1.5 pt-1 text-[11px] font-medium uppercase tracking-[0.08em] text-ink-500">
          {t("modeMenuTitle")}
        </div>
        {[
          { value: "default" as const, label: t("approveActions") },
          { value: "yolo" as const, label: t("fullAccess") },
        ].map((item) => {
          const active = settings.permission_mode === item.value;
          return (
            <button
              key={item.value}
              type="button"
              onClick={() => setMode(item.value)}
              className={[
                "flex w-full items-center gap-3 rounded-xl px-3 py-2.5 text-left text-[13px] transition-colors",
                active ? "bg-brand-500/12 text-white" : "text-ink-200 hover:bg-white/[0.045] hover:text-white",
              ].join(" ")}
            >
              <ShieldCheckIcon
                size={15}
                className={item.value === "yolo" ? "text-[#ff8a4c]" : "text-ink-400"}
              />
              <span className="min-w-0 flex-1 truncate">{item.label}</span>
              {active ? <CheckIcon size={15} className="text-brand-300" /> : null}
            </button>
          );
        })}
      </PortalDropdown>
    </>
  );
}

export function ComposerModelMenu({
  settings,
  onSettingsChange,
  modelOptions,
  disabled,
  size = "docked",
}: {
  settings: ChatRunSettings;
  onSettingsChange: (settings: ChatRunSettings) => void;
  modelOptions: ChatModelOption[];
  disabled?: boolean;
  size?: ComposerControlSize;
}) {
  const t = useTranslations("chat");
  const triggerRef = useRef<HTMLButtonElement | null>(null);
  const dropdown = useDropdown();
  const [query, setQuery] = useState("");
  const [editKey, setEditKey] = useState<string | null>(null);
  const activeKey = selectedModelKey(settings, modelOptions);
  const activeOption = modelOptions.find((option) => option.key === activeKey) ?? null;
  const modelLabel =
    activeKey === "__default"
      ? t("runtimeDefault")
      : activeOption
      ? tierBadge(activeOption.tier) || compactModelName(activeOption.model)
      : tierBadge(settings.model_tier) ||
        compactModelName(settings.model_id) ||
        t("customOverride");
  const reasonLabel = t(reasoningKey(settings.reasoning_effort));
  const contextText = contextLabel(settings.model_context_window);
  const editingKey = editKey || activeKey;

  const filteredOptions = useMemo(() => {
    const needle = query.trim().toLowerCase();
    const rows = needle
      ? modelOptions.filter((option) =>
          [option.label, option.provider, option.model, option.tier]
            .filter(Boolean)
            .join(" ")
            .toLowerCase()
            .includes(needle),
        )
      : modelOptions;
    return rows.slice(0, 80);
  }, [modelOptions, query]);

  function overrideFor(key: string) {
    return settings.model_overrides?.[key] ?? {};
  }

  function settingsForModelKey(key: string) {
    const override = overrideFor(key);
    const option = modelOptions.find((item) => item.key === key);
    return {
      reasoning_effort: override.reasoning_effort ?? option?.reasoning_effort ?? "off",
      model_context_window: override.model_context_window ?? 262144,
    };
  }

  function applyModel(key: string) {
    const modelSettings = settingsForModelKey(key);
    if (key === "__default") {
      onSettingsChange({
        ...settings,
        ...modelSettings,
        model_tier: "",
        model_provider: "",
        model_id: "",
      });
      dropdown.close();
      return;
    }
    if (key === "__add_custom") {
      if (typeof window !== "undefined") {
        window.location.href = "/settings#models";
      }
      return;
    }
    if (key === "__custom") {
      dropdown.close();
      return;
    }
    const option = modelOptions.find((item) => item.key === key);
    if (!option) return;
    onSettingsChange({
      ...settings,
      ...modelSettings,
      model_tier: option.tier || "",
      model_provider: option.provider,
      model_id: option.model,
    });
    dropdown.close();
  }

  function updateModelSetting(
    key: string,
    patch: {
      reasoning_effort?: ReasoningEffort;
      model_context_window?: ModelContextWindow;
    },
  ) {
    const nextOverride = {
      ...overrideFor(key),
      ...patch,
    };
    const nextOverrides = {
      ...(settings.model_overrides ?? {}),
      [key]: nextOverride,
    };
    onSettingsChange({
      ...settings,
      ...(key === activeKey ? patch : {}),
      model_overrides: nextOverrides,
    });
  }

  return (
    <>
      <button
        ref={triggerRef}
        type="button"
        aria-haspopup="menu"
        aria-expanded={dropdown.open}
        disabled={disabled}
        onClick={() => {
          if (!disabled) dropdown.toggle();
        }}
        className={[
          "inline-flex min-w-0 shrink items-center gap-1.5 rounded-full font-semibold text-ink-100 transition-colors",
          dropdown.open ? "bg-white/[0.07]" : "hover:bg-white/5",
          disabled ? "cursor-not-allowed opacity-45" : "cursor-pointer",
          size === "hero" ? "max-w-[220px]" : "max-w-[200px]",
          triggerSizeClasses(size),
        ].join(" ")}
        title={`${modelLabel} ${contextText} ${reasonLabel}`}
      >
        <span className="min-w-0 truncate">{modelLabel}</span>
        <span className="shrink-0 text-ink-500">{contextText}</span>
        <span className="shrink-0 text-ink-500">{reasonLabel}</span>
        <ChevronDownIcon
          size={12}
          className={[
            "shrink-0 text-ink-400 transition-transform",
            dropdown.open ? "rotate-180" : "",
          ].join(" ")}
        />
      </button>
      <PortalDropdown
        open={dropdown.open}
        onClose={dropdown.close}
        anchorRef={triggerRef}
        align="right"
        width={editKey ? 720 : 420}
        offset={8}
        className="overflow-hidden rounded-[18px] border border-[color:var(--line-hi)] bg-[color:var(--card)] shadow-[0_18px_42px_rgba(0,0,0,0.36)] backdrop-blur-xl"
      >
        <div className="flex">
          <div className={editKey ? "w-[420px] shrink-0 p-1.5" : "w-full p-1.5"}>
            <div className="p-2">
              <label className="flex h-10 items-center gap-2 rounded-xl border border-[color:var(--line)] bg-black/10 px-3 text-ink-400 focus-within:border-[color:var(--line-hi)]">
                <SearchIcon size={15} />
                <input
                  value={query}
                  onChange={(event) => setQuery(event.target.value)}
                  placeholder={t("modelSearchPlaceholder")}
                  className="min-w-0 flex-1 bg-transparent text-[14px] text-ink-100 placeholder:text-ink-500 focus:outline-none"
                />
              </label>
            </div>

            <div className="max-h-[260px] overflow-y-auto py-1">
              <ModelRow
                active={activeKey === "__default"}
                title={t("runtimeDefault")}
                detail={`${contextLabel(settingsForModelKey("__default").model_context_window)} · ${t(reasoningKey(settingsForModelKey("__default").reasoning_effort))}`}
                editLabel={t("editModelSettings")}
                onClick={() => applyModel("__default")}
                onEdit={() => setEditKey("__default")}
              />
              {activeKey === "__custom" ? (
                <ModelRow
                  active
                  title={t("customOverride")}
                  detail={[
                    settings.model_provider,
                    settings.model_id,
                    contextText,
                    reasonLabel,
                  ]
                    .filter(Boolean)
                    .join(" · ")}
                  editLabel={t("editModelSettings")}
                  onClick={() => applyModel("__custom")}
                  onEdit={() => setEditKey("__custom")}
                />
              ) : null}
              {filteredOptions.map((option) => {
                const modelSettings = settingsForModelKey(option.key);
                return (
                  <ModelRow
                    key={option.key}
                    active={option.key === activeKey}
                    title={tierBadge(option.tier) || compactModelName(option.model)}
                    detail={[
                      option.tier ? compactModelName(option.model) : option.provider,
                      contextLabel(modelSettings.model_context_window),
                      t(reasoningKey(modelSettings.reasoning_effort)),
                    ]
                      .filter(Boolean)
                      .join(" · ")}
                    editLabel={t("editModelSettings")}
                    onClick={() => applyModel(option.key)}
                    onEdit={() => setEditKey(option.key)}
                  />
                );
              })}
            </div>

            <div className="border-t border-[color:var(--line)] px-1 pt-1">
              <button
                type="button"
                onClick={() => applyModel("__add_custom")}
                className="flex w-full items-center gap-2 rounded-xl px-3 py-2.5 text-left text-[14px] text-ink-300 transition-colors hover:bg-white/[0.045] hover:text-white"
              >
                <SparkIcon size={15} />
                <span>{t("addCustomProvider")}</span>
              </button>
            </div>
          </div>

          {editKey ? (
            <div className="max-h-[430px] w-[300px] shrink-0 overflow-y-auto border-l border-[color:var(--line)] bg-black/[0.08] p-2">
              <div className="px-3 py-2 text-[17px] text-ink-300">
                {t("optionsTitle")}
              </div>

              <OptionSection title={t("think")}>
                {REASONING_LEVELS.map((level) => (
                  <OptionRow
                    key={level}
                    active={settingsForModelKey(editingKey).reasoning_effort === level}
                    label={t(reasoningKey(level))}
                    onClick={() => updateModelSetting(editingKey, { reasoning_effort: level })}
                  />
                ))}
              </OptionSection>

              <OptionSection title={t("contextLength")}>
                {CONTEXT_WINDOWS.map((item) => (
                  <OptionRow
                    key={item.value}
                    active={settingsForModelKey(editingKey).model_context_window === item.value}
                    label={item.label}
                    onClick={() =>
                      updateModelSetting(editingKey, {
                        model_context_window: item.value,
                      })
                    }
                  />
                ))}
              </OptionSection>
            </div>
          ) : null}
        </div>
      </PortalDropdown>
    </>
  );
}

function OptionSection({
  title,
  children,
}: {
  title: string;
  children: ReactNode;
}) {
  return (
    <div className="border-t border-[color:var(--line)] py-1.5">
      <div className="px-3 py-0.5 text-[13px] text-ink-500">{title}</div>
      <div>{children}</div>
    </div>
  );
}

function OptionRow({
  active,
  label,
  onClick,
}: {
  active: boolean;
  label: string;
  onClick: () => void;
}) {
  return (
    <button
      type="button"
      onClick={onClick}
      className={[
        "flex w-full items-center gap-3 rounded-xl px-3 py-1 text-left text-[14px] transition-colors",
        active ? "text-white" : "text-ink-200 hover:bg-white/[0.045] hover:text-white",
      ].join(" ")}
    >
      <span className="min-w-0 flex-1 truncate">{label}</span>
      {active ? <CheckIcon size={15} className="shrink-0 text-ink-300" /> : null}
    </button>
  );
}

function ModelRow({
  active,
  title,
  detail,
  editLabel,
  onClick,
  onEdit,
}: {
  active: boolean;
  title: string;
  detail?: string;
  editLabel?: string;
  onClick: () => void;
  onEdit?: () => void;
}) {
  return (
    <button
      type="button"
      onClick={onClick}
      className={[
        "group flex w-full items-center gap-3 rounded-xl px-3 py-2.5 text-left transition-colors",
        active ? "bg-white/[0.07] text-white" : "text-ink-200 hover:bg-white/[0.045] hover:text-white",
      ].join(" ")}
    >
      <span className="min-w-0 flex-1">
        <span className="block truncate text-[14px] leading-5">{title}</span>
        {detail ? (
          <span className="block truncate text-[12px] leading-4 text-ink-500">{detail}</span>
        ) : null}
      </span>
      {onEdit ? (
        <span
          role="button"
          tabIndex={0}
          onClick={(event) => {
            event.stopPropagation();
            onEdit();
          }}
          onKeyDown={(event) => {
            if (event.key !== "Enter" && event.key !== " ") return;
            event.preventDefault();
            event.stopPropagation();
            onEdit();
          }}
          className={[
            "inline-flex shrink-0 items-center rounded-lg px-2 py-1 text-[12px] text-ink-300 transition-colors hover:bg-white/10 hover:text-white",
            active ? "" : "opacity-70 group-hover:opacity-100",
          ].join(" ")}
        >
          {editLabel}
        </span>
      ) : null}
      {active ? <CheckIcon size={15} className="shrink-0 text-ink-300" /> : null}
    </button>
  );
}
