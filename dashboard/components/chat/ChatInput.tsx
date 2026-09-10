"use client";

import { useTranslations } from "next-intl";
import { useEffect, useId, useRef, useState, type Ref } from "react";
import type { ChatAttachment, ChatModelOption, ChatRunSettings } from "../../lib/chat";
import { callApi } from "../../lib/clientApi";
import { uuid } from "../../lib/chat";
import { FileIcon, FilePlusIcon, SendIcon, StopIcon, XIcon } from "../icons";
import { ComposerModelMenu, ComposerPermissionMenu } from "./ComposerRunControls";

// Match the upload/turn limits in nerya/agent/attachments.py. Validate before
// FileReader allocates base64 copies; the server remains authoritative.
const MAX_FILES = 8;
const MAX_FILE_BYTES = 8 * 1024 * 1024;
const MAX_TOTAL_BYTES = 20 * 1024 * 1024;
type UploadedAttachment = ChatAttachment & { uploaded?: boolean; reason?: string };
type UploadEnvelope = { ok?: boolean; attachments?: UploadedAttachment[] };

function fileToAttachment(file: File): Promise<ChatAttachment> {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = () => resolve({
      id: uuid(), name: file.name, mime_type: file.type || "application/octet-stream",
      size: file.size,
      kind: file.type.startsWith("image/") ? "image" : file.type === "application/pdf" || file.type.startsWith("text/") ? "document" : "file",
      data_url: String(reader.result || ""),
    });
    reader.onerror = () => reject(reader.error ?? new Error("file read failed"));
    reader.onabort = () => reject(new Error("file read cancelled"));
    reader.readAsDataURL(file);
  });
}
function formatBytes(size: number): string {
  if (!Number.isFinite(size) || size <= 0) return "";
  if (size < 1024) return `${size} B`;
  if (size < 1024 * 1024) return `${Math.round(size / 1024)} KB`;
  return `${(size / (1024 * 1024)).toFixed(1)} MB`;
}

export interface ChatInputProps {
  value: string;
  onChange: (value: string) => void;
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
  variant?: "docked" | "hero";
  inputRef?: Ref<HTMLTextAreaElement>;
}

/** One composer for home, empty chat and active chat. Only its frame changes. */
export function ChatInput({ value, onChange, onSend, onCancel, sending, locked = false,
  lockMessage, placeholder, settings, onSettingsChange, modelOptions = [], attachments = [],
  onAttachmentsChange, variant = "docked", inputRef }: ChatInputProps) {
  const t = useTranslations("chat");
  const tUi = useTranslations("ui");
  const fieldId = useId();
  const textareaRef = useRef<HTMLTextAreaElement | null>(null);
  const pickerRef = useRef<HTMLInputElement>(null);
  const uploadRef = useRef<AbortController | null>(null);
  const latest = useRef({ attachments, onAttachmentsChange, sending, locked });
  latest.current = { attachments, onAttachmentsChange, sending, locked };
  const [uploading, setUploading] = useState(false);
  const [uploadError, setUploadError] = useState("");
  const hero = variant === "hero";
  const canSend = !sending && !locked && !uploading && !uploadError && Boolean(value.trim() || attachments.length);

  useEffect(() => {
    if (!textareaRef.current) return;
    textareaRef.current.style.height = "auto";
    textareaRef.current.style.height = `${Math.min(textareaRef.current.scrollHeight, hero ? 192 : 240)}px`;
  }, [value, hero]);
  useEffect(() => () => { uploadRef.current?.abort(); }, []);

  function submit() {
    if (canSend && !uploadRef.current) onSend();
  }
  async function pickFiles(files: readonly File[]) {
    if (!files.length || uploadRef.current || latest.current.sending || latest.current.locked) return;
    if (!latest.current.onAttachmentsChange) { setUploadError(t("attachmentNotReady")); return; }
    const existing = latest.current.attachments;
    if (existing.length + files.length > MAX_FILES || files.some((file) => file.size > MAX_FILE_BYTES)
      || [...existing, ...files].reduce((total, file) => total + file.size, 0) > MAX_TOTAL_BYTES) {
      setUploadError(tUi("attachmentLimits"));
      return;
    }
    const controller = new AbortController();
    uploadRef.current = controller;
    setUploading(true);
    setUploadError("");
    try {
      const picked = await Promise.all(files.map(fileToAttachment));
      if (controller.signal.aborted) return;
      const response = await callApi<UploadEnvelope>("/agent/attachments/upload", {
        method: "POST", signal: controller.signal,
        body: { upload_id: uuid(), attachments: picked },
      });
      if (controller.signal.aborted) return;
      const uploaded = response.attachments;
      // HTTP 200 is not sufficient: the server can reject individual files.
      if (response.ok === false || !Array.isArray(uploaded) || uploaded.length !== picked.length
        || uploaded.some((file) => file.uploaded === false || !file.artifact_uri)) {
        throw new Error("attachment upload rejected");
      }
      const merged = picked.map((file, index) => ({
        ...file, ...(uploaded.find((item) => item.id === file.id) ?? uploaded[index]),
        data_url: file.kind === "image" ? file.data_url : undefined,
      }));
      // Use the current list, not the list captured when upload started.
      const next = [...latest.current.attachments, ...merged];
      latest.current.attachments = next;
      latest.current.onAttachmentsChange?.(next);
    } catch {
      if (!controller.signal.aborted) setUploadError(t("uploadFailed"));
    } finally {
      if (uploadRef.current === controller) uploadRef.current = null;
      if (!controller.signal.aborted) setUploading(false);
    }
  }
  function removeAttachment(id: string) {
    const next = latest.current.attachments.filter((file) => file.id !== id);
    latest.current.attachments = next;
    latest.current.onAttachmentsChange?.(next);
  }

  const composer = (
    <div data-chat-composer={variant} className="min-w-0 rounded-2xl border border-[color:var(--line-hi)] bg-[color:var(--card-hi)] p-3 transition-colors focus-within:border-brand-500/60 sm:p-4">
      {attachments.length ? (
        <div className="mb-3 flex max-h-28 flex-wrap gap-2 overflow-y-auto">
          {attachments.map((file) => (
            <div key={file.id} className="flex max-w-full items-center gap-2 rounded-lg border border-[color:var(--line)] bg-[color:var(--card)] py-1 pl-2 pr-1 text-xs">
              {file.data_url?.startsWith("data:image/") ? (
                // eslint-disable-next-line @next/next/no-img-element
                <img src={file.data_url} alt="" className="h-6 w-6 rounded object-cover" />
              ) : <FileIcon size={15} />}
              <span className="min-w-0 max-w-[180px] truncate" title={file.name}>{file.name}</span>
              <span className="shrink-0 text-[color:var(--text-muted)]">{formatBytes(file.size)}</span>
              <button type="button" onClick={() => removeAttachment(file.id)} disabled={sending || locked}
                className="ui-icon-button shrink-0 disabled:opacity-40" aria-label={`${t("removeAttachment")}: ${file.name}`}>
                <XIcon size={14} />
              </button>
            </div>
          ))}
        </div>
      ) : null}
      <textarea
        ref={(node) => { textareaRef.current = node; if (typeof inputRef === "function") inputRef(node); else if (inputRef) (inputRef as { current: HTMLTextAreaElement | null }).current = node; }}
        value={value} onChange={(event) => onChange(event.target.value)}
        onKeyDown={(event) => {
          if (event.nativeEvent.isComposing || event.keyCode === 229) return;
          if (event.key === "Enter" && !event.shiftKey) { event.preventDefault(); submit(); }
        }}
        onPaste={(event) => {
          const files = Array.from(event.clipboardData.files);
          if (files.length) { event.preventDefault(); void pickFiles(files); }
        }}
        disabled={locked} rows={hero ? 2 : 1}
        aria-label={placeholder ?? t("inputPlaceholder")}
        aria-describedby={uploading || uploadError ? `${fieldId}-status` : undefined}
        placeholder={locked ? lockMessage : placeholder ?? t("inputPlaceholder")}
        className="block min-h-8 w-full resize-none overflow-y-auto bg-transparent text-base leading-6 text-[color:var(--text-base)] placeholder:text-[color:var(--text-muted)] focus:outline-none disabled:cursor-not-allowed disabled:opacity-70 sm:text-[15px]"
      />
      <div className="mt-2 flex flex-wrap items-center gap-1.5">
        <input ref={pickerRef} id={fieldId} type="file" multiple className="hidden" tabIndex={-1}
          aria-label={t("addAttachment")} disabled={sending || locked || uploading || !onAttachmentsChange}
          accept="image/*,.pdf,.txt,.md,.csv,.json,.html,.xml"
          onChange={(event) => { const files = Array.from(event.currentTarget.files ?? []); event.currentTarget.value = ""; void pickFiles(files); }} />
        <button type="button" onClick={() => pickerRef.current?.click()} aria-label={t("addAttachment")}
          disabled={sending || locked || uploading || !onAttachmentsChange} className="ui-icon-button disabled:cursor-not-allowed disabled:opacity-40"><FilePlusIcon size={18} /></button>
        <ComposerPermissionMenu settings={settings} onSettingsChange={onSettingsChange} disabled={sending || locked} size={variant} />
        <div className="ml-auto flex min-w-0 items-center gap-1.5 max-[520px]:ml-0 max-[520px]:w-full max-[520px]:justify-end">
          <ComposerModelMenu settings={settings} onSettingsChange={onSettingsChange} modelOptions={modelOptions} disabled={sending || locked} size={variant} />
          {sending && onCancel ? <button type="button" onClick={onCancel} className="ui-icon-button border border-[color:var(--line-hi)]" aria-label={t("cancelTurn")}><StopIcon size={17} /></button> : null}
          {!sending ? <button type="button" onClick={submit} disabled={!canSend} aria-label={t("send")} title={locked ? lockMessage : t("send")}
            className="inline-flex h-9 w-9 shrink-0 items-center justify-center rounded-full bg-brand-500 text-white transition-colors hover:bg-brand-400 disabled:cursor-not-allowed disabled:opacity-40"><SendIcon size={17} /></button> : null}
        </div>
      </div>
      {uploading || uploadError ? <div id={`${fieldId}-status`} role={uploadError ? "alert" : "status"} className="mt-3 flex items-center justify-between gap-2 text-xs leading-relaxed text-[color:var(--text-muted)]">
        <span>{uploadError || t("uploadingAttachment")}</span>
        {uploadError ? <button type="button" className="ui-icon-button shrink-0" aria-label={tUi("close")} onClick={() => setUploadError("")}><XIcon size={14} /></button> : <button type="button" className="btn btn-ghost shrink-0" onClick={() => { uploadRef.current?.abort(); uploadRef.current = null; setUploading(false); }}>{tUi("cancel")}</button>}
      </div> : null}
    </div>
  );
  return hero ? composer : (
    <div className="shrink-0 border-t border-[color:var(--line)] bg-[color:var(--bg)]">
      <div className="mx-auto max-w-[860px] px-3 py-3 sm:px-4">{composer}</div>
    </div>
  );
}
