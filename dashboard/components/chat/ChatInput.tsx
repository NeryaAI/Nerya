"use client";

import { useTranslations } from "next-intl";
import { useEffect, useRef } from "react";
import type { ChatModelOption, ChatRunSettings, ReasoningEffort } from "../../lib/chat";
import { SendIcon, SparkIcon, StopIcon, WrenchIcon } from "../icons";

const THINK_LEVELS: Array<{ value: ReasoningEffort; label: string }> = [
  { value: "off", label: "Think off" },
  { value: "minimal", label: "Minimal" },
  { value: "low", label: "Low" },
  { value: "medium", label: "Medium" },
  { value: "high", label: "High" },
  { value: "xhigh", label: "XHigh" },
];

export function ChatInput({
  value,
  onChange,
  onSend,
  onCancel,
  sending,
  placeholder,
  settings,
  onSettingsChange,
  modelOptions = [],
}: {
  value: string;
  onChange: (v: string) => void;
  onSend: () => void;
  onCancel?: () => void;
  sending: boolean;
  placeholder?: string;
  settings: ChatRunSettings;
  onSettingsChange: (settings: ChatRunSettings) => void;
  modelOptions?: ChatModelOption[];
}) {
  const ref = useRef<HTMLTextAreaElement | null>(null);
  const t = useTranslations("chat");
  const tCommon = useTranslations("common");
  const selectedModelKey =
    modelOptions.find(
      (option) =>
        option.provider === settings.model_provider &&
        option.model === settings.model_id &&
        (option.tier || "") === (settings.model_tier || ""),
    )?.key ||
    (settings.model_provider || settings.model_id || settings.model_tier
      ? "__custom"
      : "__default");

  useEffect(() => {
    if (!ref.current) return;
    ref.current.style.height = "auto";
    ref.current.style.height = Math.min(ref.current.scrollHeight, 240) + "px";
  }, [value]);

  function onKeyDown(e: React.KeyboardEvent<HTMLTextAreaElement>) {
    if (e.key === "Enter" && !e.shiftKey) {
      e.preventDefault();
      if (!sending && value.trim()) onSend();
    }
  }

  return (
    <div className="border-t backdrop-blur-glass" style={{ background: "var(--panel-bg)", borderColor: "var(--line)" }}>
      <div className="max-w-4xl mx-auto px-4 py-3">
        <div className="flex items-end gap-2 rounded-2xl border border-white/8 bg-white/[0.04] backdrop-blur-glass focus-within:border-brand-500/50 focus-within:shadow-[0_0_0_4px_rgba(139,92,246,0.08)] transition-all px-3 py-2">
          <textarea
            ref={ref}
            value={value}
            onChange={(e) => onChange(e.target.value)}
            onKeyDown={onKeyDown}
            rows={1}
            placeholder={placeholder ?? t("inputPlaceholder")}
            className="flex-1 bg-transparent resize-none text-sm text-ink-100 placeholder:text-ink-500 focus:outline-none py-1.5 min-h-[28px] max-h-[240px]"
          />
          {sending && onCancel ? (
            <button
              onClick={onCancel}
              className="inline-flex h-8 w-8 items-center justify-center rounded-md border border-white/10 text-ink-300 hover:text-white hover:border-white/20 transition-colors cursor-pointer"
              title={t("cancelTurn")}
              aria-label={t("cancelTurn")}
            >
              <StopIcon size={15} />
            </button>
          ) : null}
          <button
            onClick={onSend}
            disabled={sending || !value.trim()}
            className="inline-flex h-8 w-8 items-center justify-center bg-gradient-to-r from-brand-500 to-brand-700 hover:from-brand-400 hover:to-brand-600 disabled:opacity-40 disabled:cursor-not-allowed text-white text-sm rounded-md transition-all shadow-glow cursor-pointer"
            title={sending ? t("running") : t("send")}
            aria-label={sending ? t("running") : t("send")}
          >
            {sending ? (
              <span className="inline-flex items-center gap-1.5">
                <span className="typing-dot" />
                <span className="sr-only">{t("running")}</span>
              </span>
            ) : (
              <SendIcon size={15} />
            )}
          </button>
        </div>
        <div className="mt-2 flex flex-wrap items-center justify-between gap-2 text-[10px] text-ink-500 px-1">
          <span>{t("enterToSend")}</span>
          <div className="flex items-center gap-2">
            <label className="flex items-center gap-1.5">
              <span className="inline-flex items-center gap-1 text-ink-300">
                <SparkIcon size={13} />
                <span>{t("think")}</span>
              </span>
              <select
                value={settings.reasoning_effort}
                onChange={(e) =>
                  onSettingsChange({
                    ...settings,
                    reasoning_effort: e.target.value as ReasoningEffort,
                  })
                }
                disabled={sending}
                className="rounded-md border border-white/10 bg-ink-900/80 px-2 py-1 text-[10px] text-ink-200 focus:outline-none focus:border-brand-500/50 disabled:opacity-60"
              >
                {THINK_LEVELS.map((level) => (
                  <option key={level.value} value={level.value}>
                    {level.label}
                  </option>
                ))}
              </select>
            </label>
            <label className="flex items-center gap-1.5">
              <span className="inline-flex items-center gap-1 text-ink-300">
                <WrenchIcon size={13} />
                <span>{t("model")}</span>
              </span>
              <select
                value={selectedModelKey}
                onChange={(e) => {
                  const key = e.target.value;
                  if (key === "__default") {
                    onSettingsChange({
                      ...settings,
                      model_tier: "",
                      model_provider: "",
                      model_id: "",
                    });
                    return;
                  }
                  if (key === "__custom") return;
                  const option = modelOptions.find((item) => item.key === key);
                  if (!option) return;
                  onSettingsChange({
                    ...settings,
                    model_tier: option.tier || "",
                    model_provider: option.provider,
                    model_id: option.model,
                  });
                }}
                disabled={sending}
                className="max-w-[220px] rounded-md border border-white/10 bg-ink-900/80 px-2 py-1 text-[10px] text-ink-200 focus:outline-none focus:border-brand-500/50 disabled:opacity-60"
              >
                <option value="__default">{t("runtimeDefault")}</option>
                {selectedModelKey === "__custom" ? (
                  <option value="__custom">{t("customOverride")}</option>
                ) : null}
                {modelOptions.map((option) => (
                  <option key={option.key} value={option.key}>
                    {option.label}
                  </option>
                ))}
              </select>
            </label>
            <label className="inline-flex items-center gap-1.5 rounded-md border border-white/10 bg-white/[0.03] px-2 py-1 text-ink-300">
              <input
                type="checkbox"
                checked={settings.permission_mode === "yolo"}
                disabled={sending}
                onChange={(e) =>
                  onSettingsChange({
                    ...settings,
                    permission_mode: e.target.checked ? "yolo" : "default",
                  })
                }
                className="h-3 w-3 accent-brand-500"
              />
              <span>{t("yolo")}</span>
            </label>
          </div>
        </div>
      </div>
    </div>
  );
}
