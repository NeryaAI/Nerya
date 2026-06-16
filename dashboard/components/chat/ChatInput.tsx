"use client";

import { useTranslations } from "next-intl";
import { useEffect, useRef, useState } from "react";
import type { ChatAttachment, ChatModelOption, ChatRunSettings } from "../../lib/chat";
import { callApi } from "../../lib/clientApi";
import { FileIcon, FilePlusIcon, SendIcon, StopIcon, XIcon } from "../icons";
import { ComposerModelMenu, ComposerPermissionMenu } from "./ComposerRunControls";

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
  variant = "docked",
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
  // "docked" sits at the bottom of an active chat; "hero" is the
  // centred new-chat composer (no top border / panel chrome) used by
  // the Codex-style empty state.
  variant?: "docked" | "hero";
}) {
  const ref = useRef<HTMLTextAreaElement | null>(null);
  const [uploading, setUploading] = useState(false);
  const [uploadError, setUploadError] = useState("");
  const t = useTranslations("chat");
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

  const isHero = variant === "hero";

  if (isHero) {
    return (
      <div className="overflow-hidden rounded-[18px] bg-[color:var(--card)] shadow-[0_10px_22px_rgba(0,0,0,0.14)]">
        <div className="rounded-[18px] border border-[color:var(--line)] bg-[color:var(--card-hi)] px-3.5 pb-2 pt-2.5 transition-colors focus-within:border-brand-500/55 sm:px-4">
          {attachments.length ? (
            <div className="mb-2 flex max-h-24 flex-wrap gap-1.5 overflow-y-auto pr-1">
              {attachments.map((attachment) => (
                <div
                  key={attachment.id}
                  className="inline-flex max-w-full items-center gap-1.5 rounded-md border border-brand-500/20 bg-ink-950/35 px-2 py-1 text-[11px] text-ink-200"
                >
                  {attachment.data_url?.startsWith("data:image/") ? (
                    // eslint-disable-next-line @next/next/no-img-element
                    <img src={attachment.data_url} alt="" className="h-5 w-5 rounded object-cover" />
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

          <textarea
            ref={ref}
            value={value}
            onChange={(e) => onChange(e.target.value)}
            onKeyDown={onKeyDown}
            disabled={locked}
            rows={1}
            placeholder={locked ? lockMessage : placeholder ?? t("inputPlaceholder")}
            className="block max-h-32 min-h-[28px] w-full resize-none bg-transparent text-[15px] leading-5 text-ink-100 placeholder:text-ink-300 focus:outline-none disabled:cursor-not-allowed disabled:opacity-70"
          />

          <div className="mt-1 flex flex-wrap items-center gap-1.5">
            <label
              className={`inline-flex h-7 w-7 shrink-0 items-center justify-center rounded-full text-ink-300 transition-colors ${
                sending || locked || uploading
                  ? "cursor-not-allowed opacity-40"
                  : "cursor-pointer hover:bg-white/5 hover:text-white"
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

            <ComposerPermissionMenu
              settings={settings}
              onSettingsChange={onSettingsChange}
              disabled={sending || locked}
              size="hero"
            />

            <div className="ml-auto flex min-w-0 items-center gap-1.5 max-[520px]:ml-0 max-[520px]:w-full max-[520px]:justify-end">
              <ComposerModelMenu
                settings={settings}
                onSettingsChange={onSettingsChange}
                modelOptions={modelOptions}
                disabled={sending || locked}
                size="hero"
              />

              {sending && onCancel ? (
                <button
                  onClick={onCancel}
                  className="inline-flex h-7 w-7 items-center justify-center rounded-full border border-brand-500/20 text-ink-300 transition-colors hover:border-brand-500/45 hover:text-white"
                  title={t("cancelTurn")}
                  aria-label={t("cancelTurn")}
                >
                  <StopIcon size={14} />
                </button>
              ) : null}
              {!sending ? (
                <button
                  onClick={onSend}
                  disabled={locked || (!value.trim() && !attachments.length)}
                  className="inline-flex h-7 w-7 items-center justify-center rounded-full bg-brand-500 text-white transition-colors hover:bg-brand-400 disabled:cursor-not-allowed disabled:opacity-40"
                  title={locked ? lockMessage : t("send")}
                  aria-label={locked ? lockMessage : t("send")}
                >
                  <span className="translate-y-[-1px] text-[20px] leading-none">↑</span>
                </button>
              ) : null}
            </div>
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
      </div>
    );
  }

  return (
    <div className="border-t border-[color:var(--line)] bg-[color:var(--panel-bg)]/95 backdrop-blur-glass">
      <div className="mx-auto max-w-[860px] px-4 py-2.5">
        <div
          className="rounded-[18px] border border-[color:var(--line)] bg-[color:var(--card-hi)] px-3 py-2.5 shadow-[0_10px_22px_rgba(0,0,0,0.16)] transition-colors focus-within:border-brand-500/55"
        >
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
          <textarea
            ref={ref}
            value={value}
            onChange={(e) => onChange(e.target.value)}
            onKeyDown={onKeyDown}
            disabled={locked}
            rows={1}
            placeholder={locked ? lockMessage : placeholder ?? t("inputPlaceholder")}
            className="block max-h-[160px] min-h-[32px] w-full resize-none bg-transparent px-1 text-[14px] leading-5 text-ink-100 placeholder:text-ink-300 focus:outline-none disabled:cursor-not-allowed disabled:opacity-70"
          />
          <div className="mt-1.5 flex flex-wrap items-center gap-1.5">
            <label
              className={`inline-flex h-7 w-7 shrink-0 items-center justify-center rounded-full text-ink-300 transition-colors ${
                sending || locked || uploading
                  ? "cursor-not-allowed opacity-40"
                  : "cursor-pointer hover:bg-white/5 hover:text-white"
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
            <ComposerPermissionMenu
              settings={settings}
              onSettingsChange={onSettingsChange}
              disabled={sending || locked}
              size="docked"
            />
            <div className="min-w-0 flex-1 max-[520px]:hidden" />
            <ComposerModelMenu
              settings={settings}
              onSettingsChange={onSettingsChange}
              modelOptions={modelOptions}
              disabled={sending || locked}
              size="docked"
            />
            {sending && onCancel ? (
              <button
                onClick={onCancel}
                className="inline-flex h-8 w-8 items-center justify-center rounded-full border border-brand-500/20 text-ink-300 transition-colors hover:border-brand-500/45 hover:text-white"
                title={t("cancelTurn")}
                aria-label={t("cancelTurn")}
              >
                <StopIcon size={15} />
              </button>
            ) : null}
            {!sending ? (
              <button
                onClick={onSend}
                disabled={locked || (!value.trim() && !attachments.length)}
                className="inline-flex h-8 w-8 items-center justify-center rounded-full bg-brand-500 text-white shadow-glow transition-colors hover:bg-brand-400 disabled:cursor-not-allowed disabled:opacity-40"
                title={locked ? lockMessage : t("send")}
                aria-label={locked ? lockMessage : t("send")}
              >
                <SendIcon size={15} />
              </button>
            ) : null}
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
      </div>
    </div>
  );
}
