"use client";

import { useTranslations } from "next-intl";
import { useEffect, useMemo, useRef, useState } from "react";
import type { ChatAttachment, ChatModelOption, ChatRunSettings, ReasoningEffort } from "../../lib/chat";
import { callApi } from "../../lib/clientApi";
import { FileIcon, FilePlusIcon, SendIcon, SparkIcon, StopIcon, WrenchIcon, XIcon } from "../icons";
import { Select, type SelectOption } from "../Select";

const THINK_LEVELS: ReasoningEffort[] = [
  "off",
  "minimal",
  "low",
  "medium",
  "high",
  "xhigh",
];

type AttachmentUploadEnvelope = {
  ok?: boolean;
  upload_id?: string;
  attachments?: ChatAttachment[];
};

function fileToAttachment(file: File): Promise<ChatAttachment> {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = () => {
      resolve({
        id:
          typeof crypto !== "undefined" && "randomUUID" in crypto
            ? crypto.randomUUID()
            : `${Date.now()}-${Math.random().toString(36).slice(2)}`,
        name: file.name,
        mime_type: file.type || "application/octet-stream",
        size: file.size,
        kind: file.type.startsWith("image/")
          ? "image"
          : file.type === "application/pdf" || file.type.startsWith("text/")
          ? "document"
          : "file",
        data_url: String(reader.result || ""),
      });
    };
    reader.onerror = () => reject(reader.error ?? new Error("file read failed"));
    reader.readAsDataURL(file);
  });
}

function formatBytes(size: number): string {
  if (!Number.isFinite(size) || size <= 0) return "";
  if (size < 1024) return `${size} B`;
  if (size < 1024 * 1024) return `${Math.round(size / 1024)} KB`;
  return `${(size / (1024 * 1024)).toFixed(1)} MB`;
}

function uploadPayload(attachment: ChatAttachment): ChatAttachment {
  return {
    id: attachment.id,
    name: attachment.name,
    mime_type: attachment.mime_type,
    size: attachment.size,
    kind: attachment.kind,
    data_url: attachment.data_url,
    url: attachment.url,
    text: attachment.text,
  };
}

export function ChatInput({
  value,
  onChange,
  onSend,
  onCancel,
  sending,
  locked = false,
  lockMessage,
  placeholder,
  settings,
  onSettingsChange,
  modelOptions = [],
  attachments = [],
  onAttachmentsChange,
}: {
  value: string;
  onChange: (v: string) => void;
  onSend: () => void;
  onCancel?: () => void;
  sending: boolean;
  locked?: boolean;
  lockMessage?: string;
  placeholder?: string;
  settings: ChatRunSettings;
  onSettingsChange: (settings: ChatRunSettings) => void;
  modelOptions?: ChatModelOption[];
  attachments?: ChatAttachment[];
  onAttachmentsChange?: (attachments: ChatAttachment[]) => void;
}) {
  const ref = useRef<HTMLTextAreaElement | null>(null);
  const [uploading, setUploading] = useState(false);
  const [uploadError, setUploadError] = useState("");
  const [advancedOpen, setAdvancedOpen] = useState(false);
  const t = useTranslations("chat");
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

  const thinkOptions = useMemo<SelectOption<ReasoningEffort>[]>(
    () =>
      THINK_LEVELS.map((level) => ({
        value: level,
        label: t(`thinkLevel${level.charAt(0).toUpperCase()}${level.slice(1)}`),
      })),
    [t],
  );

  const modelSelectOptions = useMemo<SelectOption<string>[]>(() => {
    const list: SelectOption<string>[] = [
      { value: "__default", label: t("runtimeDefault") },
    ];
    if (selectedModelKey === "__custom") {
      list.push({ value: "__custom", label: t("customOverride") });
    }
    for (const option of modelOptions) {
      list.push({ value: option.key, label: option.label });
    }
    list.push({
      value: "__add_custom",
      label: `+ ${t("addCustomProvider")}`,
    });
    return list;
  }, [modelOptions, selectedModelKey, t]);

  useEffect(() => {
    if (!ref.current) return;
    ref.current.style.height = "auto";
    ref.current.style.height = Math.min(ref.current.scrollHeight, 240) + "px";
  }, [value]);

  function onKeyDown(e: React.KeyboardEvent<HTMLTextAreaElement>) {
    if (e.key === "Enter" && !e.shiftKey) {
      e.preventDefault();
      if (!sending && !locked && (value.trim() || attachments.length)) onSend();
    }
  }

  async function onPickFiles(files: FileList | null) {
    if (!files || files.length === 0) return;
    if (!onAttachmentsChange) {
      setUploadError(t("attachmentNotReady"));
      return;
    }
    setUploading(true);
    setUploadError("");
    try {
      const picked = await Promise.all(Array.from(files).map(fileToAttachment));
      const upload = await callApi<AttachmentUploadEnvelope>(
        "/agent/attachments/upload",
        {
          method: "POST",
          body: {
            upload_id:
              typeof crypto !== "undefined" && "randomUUID" in crypto
                ? crypto.randomUUID()
                : `${Date.now()}-${Math.random().toString(36).slice(2)}`,
            attachments: picked.map(uploadPayload),
          },
        },
      );
      const uploaded = upload.attachments ?? [];
      const merged = picked.map((item, index) => ({
        ...item,
        ...(uploaded[index] ?? {}),
        data_url: item.data_url,
      }));
      onAttachmentsChange([...attachments, ...merged]);
    } catch {
      setUploadError(t("uploadFailed"));
    } finally {
      setUploading(false);
    }
  }

  function removeAttachment(id: string) {
    if (!onAttachmentsChange) return;
    onAttachmentsChange(attachments.filter((item) => item.id !== id));
  }

  function runLimitValue(value: string, fallback: number, min: number, max: number): number {
    const parsed = Number(value);
    if (!Number.isFinite(parsed)) return fallback;
    return Math.max(min, Math.min(max, Math.round(parsed)));
  }

  return (
    <div className="border-t backdrop-blur-glass" style={{ background: "var(--panel-bg)", borderColor: "var(--line)" }}>
      <div className="max-w-4xl mx-auto px-4 py-3">
        <div className="rounded-lg border border-brand-500/15 bg-brand-500/[0.04] backdrop-blur-glass focus-within:border-brand-500/50 focus-within:shadow-[0_0_0_4px_rgba(139,92,246,0.08)] transition-all px-3 py-2">
          {attachments.length ? (
            <div className="mb-2 flex max-h-24 flex-wrap gap-1.5 overflow-y-auto pr-1">
              {attachments.map((attachment) => (
                <div
                  key={attachment.id}
                  className="inline-flex max-w-full items-center gap-1.5 rounded-md border border-brand-500/20 bg-ink-950/35 px-2 py-1 text-[11px] text-ink-200"
                >
                  {attachment.data_url?.startsWith("data:image/") ? (
                    // eslint-disable-next-line @next/next/no-img-element
                    <img
                      src={attachment.data_url}
                      alt=""
                      className="h-5 w-5 rounded object-cover"
                    />
                  ) : (
                    <FileIcon size={13} />
                  )}
                  <span className="max-w-[180px] truncate">{attachment.name}</span>
                  <span className="text-ink-500">{formatBytes(attachment.size)}</span>
                  <button
                    type="button"
                    onClick={() => removeAttachment(attachment.id)}
                    disabled={sending || locked}
                    className="inline-flex h-5 w-5 items-center justify-center rounded text-ink-500 hover:text-white disabled:opacity-40"
                    title={t("removeAttachment")}
                    aria-label={t("removeAttachment")}
                  >
                    <XIcon size={12} />
                  </button>
                </div>
              ))}
            </div>
          ) : null}
          <div className="flex items-end gap-2">
            <label
              className={`inline-flex h-8 w-8 shrink-0 items-center justify-center rounded-md border border-brand-500/25 bg-ink-950/25 text-ink-300 transition-colors ${
                sending || locked || uploading
                  ? "cursor-not-allowed opacity-40"
                  : "cursor-pointer hover:text-white hover:border-brand-500/55 hover:bg-brand-500/10"
              }`}
              title={t("addAttachment")}
              aria-label={t("addAttachment")}
            >
              <input
                type="file"
                multiple
                className="sr-only"
                disabled={sending || locked || uploading}
                accept="image/*,.pdf,.txt,.md,.csv,.json,.html,.xml"
                onChange={(event) => {
                  const files = event.currentTarget.files;
                  void onPickFiles(files);
                  event.currentTarget.value = "";
                }}
              />
              <FilePlusIcon size={15} />
            </label>
            <textarea
              ref={ref}
              value={value}
              onChange={(e) => onChange(e.target.value)}
              onKeyDown={onKeyDown}
              disabled={locked}
              rows={1}
              placeholder={locked ? lockMessage : placeholder ?? t("inputPlaceholder")}
              className="flex-1 bg-transparent resize-none text-sm text-ink-100 placeholder:text-ink-500 focus:outline-none py-1.5 min-h-[28px] max-h-[240px] disabled:cursor-not-allowed disabled:opacity-70"
            />
            {sending && onCancel ? (
            <button
              onClick={onCancel}
              className="inline-flex h-8 w-8 items-center justify-center rounded-md border border-brand-500/20 text-ink-300 hover:text-white hover:border-brand-500/45 transition-colors cursor-pointer"
              title={t("cancelTurn")}
              aria-label={t("cancelTurn")}
            >
              <StopIcon size={15} />
            </button>
          ) : null}
          <button
            onClick={onSend}
            disabled={sending || locked || (!value.trim() && !attachments.length)}
            className="inline-flex h-8 w-8 items-center justify-center bg-brand-500 hover:bg-brand-400 disabled:opacity-40 disabled:cursor-not-allowed text-white text-sm rounded-md transition-colors shadow-glow cursor-pointer"
            title={locked ? lockMessage : sending ? t("running") : t("send")}
            aria-label={locked ? lockMessage : sending ? t("running") : t("send")}
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
          {uploading || uploadError ? (
            <div
              className={`mt-2 text-[11px] ${
                uploadError ? "text-rose-300" : "text-ink-400"
              }`}
              role={uploadError ? "alert" : "status"}
            >
              {uploadError || t("uploadingAttachment")}
            </div>
          ) : null}
        </div>
        <div className="mt-2 px-1">
          <div className="flex flex-wrap items-center justify-between gap-x-2 gap-y-1.5 text-[10px] text-ink-500 min-w-0">
            <span className="truncate min-w-0">
              {locked ? lockMessage : t("enterToSend")}
            </span>
            <button
              type="button"
              onClick={() => setAdvancedOpen((open) => !open)}
              disabled={sending || locked}
              aria-expanded={advancedOpen}
              aria-label={advancedOpen ? t("advancedSettingsCollapse") : t("advancedSettingsExpand")}
              className="inline-flex items-center gap-1.5 rounded-md border border-brand-500/20 bg-brand-500/[0.05] px-2 py-1 text-ink-300 transition-colors hover:border-brand-500/40 hover:bg-brand-500/[0.1] hover:text-white disabled:cursor-not-allowed disabled:opacity-40"
              title={advancedOpen ? t("advancedSettingsCollapse") : t("advancedSettingsExpand")}
            >
              <WrenchIcon size={12} />
              <span>{t("advancedSettings")}</span>
              <span className="text-[9px] text-ink-500">{advancedOpen ? "▾" : "▸"}</span>
            </button>
          </div>
          {advancedOpen ? (
            <div className="mt-2 flex flex-wrap items-center gap-1.5 min-w-0 max-w-full text-[10px] text-ink-500">
              <div className="flex items-center gap-1.5 min-w-0">
                <span className="hidden sm:inline-flex items-center gap-1 text-ink-300 shrink-0">
                  <SparkIcon size={13} />
                  <span>{t("think")}</span>
                </span>
                <div className="w-[110px] shrink-0">
                  <Select<ReasoningEffort>
                    value={settings.reasoning_effort}
                    onChange={(value) =>
                      onSettingsChange({
                        ...settings,
                        reasoning_effort: value,
                      })
                    }
                    options={thinkOptions}
                    size="sm"
                    ariaLabel={t("think")}
                    disabled={sending || locked}
                    className="text-[11px]"
                  />
                </div>
              </div>
              <div className="flex items-center gap-1.5 min-w-0 flex-1 sm:flex-initial">
                <span className="hidden sm:inline-flex items-center gap-1 text-ink-300 shrink-0">
                  <WrenchIcon size={13} />
                  <span>{t("model")}</span>
                </span>
                <div className="min-w-0 max-w-[180px] sm:max-w-[220px] flex-1 sm:flex-initial">
                  <Select
                    value={selectedModelKey}
                    onChange={(key) => {
                      if (key === "__default") {
                        onSettingsChange({
                          ...settings,
                          model_tier: "",
                          model_provider: "",
                          model_id: "",
                        });
                        return;
                      }
                      if (key === "__add_custom") {
                        if (typeof window !== "undefined") {
                          window.open("/settings?tab=models", "_self");
                        }
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
                    options={modelSelectOptions}
                    size="sm"
                    ariaLabel={t("model")}
                    disabled={sending || locked}
                    panelWidth={260}
                    className="text-[11px]"
                  />
                </div>
              </div>
              <label className="inline-flex items-center gap-1.5 rounded-md border border-brand-500/20 bg-brand-500/[0.06] px-2 py-1 text-ink-300 shrink-0">
              <input
                type="checkbox"
                checked={settings.permission_mode === "yolo"}
                disabled={sending || locked}
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
              <label
                className="inline-flex items-center gap-1 rounded-md border border-brand-500/15 bg-ink-950/25 px-2 py-1 text-ink-300 shrink-0"
                title={t("maxIterationsHint")}
              >
                <span className="hidden 2xl:inline">{t("maxIterations")}</span>
                <span className="2xl:hidden">{t("maxIterationsShort")}</span>
                <input
                  type="number"
                  min={1}
                  max={240}
                  value={settings.max_iterations}
                  disabled={sending || locked}
                  onChange={(e) =>
                    onSettingsChange({
                      ...settings,
                      max_iterations: runLimitValue(
                        e.target.value,
                        settings.max_iterations,
                        1,
                        240,
                      ),
                    })
                  }
                  className="w-12 bg-transparent text-right font-mono text-[11px] text-ink-100 focus:outline-none disabled:opacity-50"
                  aria-label={t("maxIterations")}
                />
              </label>
              <label
                className="inline-flex items-center gap-1 rounded-md border border-brand-500/15 bg-ink-950/25 px-2 py-1 text-ink-300 shrink-0"
                title={t("maxToolCallsHint")}
              >
                <span className="hidden 2xl:inline">{t("maxToolCalls")}</span>
                <span className="2xl:hidden">{t("maxToolCallsShort")}</span>
                <input
                  type="number"
                  min={1}
                  max={1000}
                  value={settings.max_total_tool_calls}
                  disabled={sending || locked}
                  onChange={(e) =>
                    onSettingsChange({
                      ...settings,
                      max_total_tool_calls: runLimitValue(
                        e.target.value,
                        settings.max_total_tool_calls,
                        1,
                        1000,
                      ),
                    })
                  }
                  className="w-14 bg-transparent text-right font-mono text-[11px] text-ink-100 focus:outline-none disabled:opacity-50"
                  aria-label={t("maxToolCalls")}
                />
              </label>
              <label
                className="inline-flex items-center gap-1 rounded-md border border-brand-500/15 bg-ink-950/25 px-2 py-1 text-ink-300 shrink-0"
                title={t("maxWallSecondsHint")}
              >
                <span className="hidden 2xl:inline">{t("maxWallSeconds")}</span>
                <span className="2xl:hidden">{t("maxWallSecondsShort")}</span>
                <input
                  type="number"
                  min={10}
                  max={7200}
                  value={settings.max_wall_seconds}
                  disabled={sending || locked}
                  onChange={(e) =>
                    onSettingsChange({
                      ...settings,
                      max_wall_seconds: runLimitValue(
                        e.target.value,
                        settings.max_wall_seconds,
                        10,
                        7200,
                      ),
                    })
                  }
                  className="w-16 bg-transparent text-right font-mono text-[11px] text-ink-100 focus:outline-none disabled:opacity-50"
                  aria-label={t("maxWallSeconds")}
                />
              </label>
            </div>
          ) : null}
        </div>
      </div>
    </div>
  );
}
