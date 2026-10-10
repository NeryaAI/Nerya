"use client";

import type { ReactNode } from "react";

import { useLocale, useTranslations } from "next-intl";
import { useEffect, useId, useRef, useState, type Ref } from "react";
import type { ChatAttachment, ChatModelOption, ChatRunSettings } from "../../lib/chat";
import { callApi } from "../../lib/clientApi";
import { uuid } from "../../lib/chat";
import { FileIcon, SendIcon, StopIcon, XIcon, PlusIcon } from "../icons";
import type { ComposerOption } from "../../lib/composerInput";
import { ComposerResourceError, prepareComposerReference } from "../../lib/composerResources";
import { ComposerAddMenu, ComposerSourceIcon, ComposerSuggestions, useComposerSuggestions } from "./ComposerSuggestions";
import { ComposerModelMenu, ComposerPermissionMenu } from "./ComposerRunControls";
import { ReferenceSnapshot } from "./ReferenceSnapshot";
import { getWorkspaceIdentity, useWorkspaceIdentity, workspaceGeneration } from "../../lib/workspaceIdentity";

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
  onGuide?: () => void;
  submitting?: boolean;
  stopping?: boolean;
  sending: boolean;
  queued?:boolean;
  submitMode?: "send" | "queue";
  locked?: boolean;
  draftLocked?: boolean;
  lockMessage?: string;
  placeholder?: string;
  settings: ChatRunSettings;
  onSettingsChange: (settings: ChatRunSettings) => void;
  modelOptions?: ChatModelOption[];
  attachments?: ChatAttachment[];
  onAttachmentsChange?: (attachments: ChatAttachment[]) => void;
  variant?: "docked" | "hero";
  inputRef?: Ref<HTMLTextAreaElement>;
  taskHeader?: ReactNode;
  contextControl?: ReactNode;
  external?: boolean;
  assistant?: boolean;
  sessionId?: string;
}

/** One composer for home, empty chat and active chat. Only its frame changes. */
export function ChatInput({ value, onChange, onSend, onCancel, onGuide, sending, queued=false, submitMode, submitting = sending, stopping = false, locked = false, draftLocked = false,
  lockMessage, placeholder, settings, onSettingsChange, modelOptions = [], attachments = [],
  onAttachmentsChange, variant = "docked", inputRef, taskHeader, contextControl, external = false, assistant = false, sessionId }: ChatInputProps) {
  const t = useTranslations("chat");
  const tUi = useTranslations("ui");
  const tc = useTranslations("composer");
  const zh = useLocale().startsWith("zh");
  const workspaceId = useWorkspaceIdentity();
  const identityGeneration = workspaceGeneration();
  const editingLocked = draftLocked || (external && locked);
  const busy = submitting || (external && sending);
  const sendLabel = assistant ? t("send") : (submitMode ? submitMode === "queue" : sending||queued) && !external ? (zh ? "排队发送" : "Queue message") : t("send");
  const fieldId = useId();
  const textareaRef = useRef<HTMLTextAreaElement | null>(null);
  const pickerRef = useRef<HTMLInputElement>(null);
  const uploadRef = useRef<AbortController | null>(null);
  const latest = useRef({ attachments, onAttachmentsChange, locked: editingLocked, external, sessionId });
  latest.current = { attachments, onAttachmentsChange, locked: editingLocked, external, sessionId };
  const retryRef = useRef<(() => void) | null>(null);
  const dragDepth = useRef(0);
  const [dragging, setDragging] = useState(false);
  const [referenceName, setReferenceName] = useState("");
  const [uploading, setUploading] = useState(false);
  const [uploadError, setUploadError] = useState("");
  const [fileStates,setFileStates]=useState<Record<string,{name:string;state:string}>>({});
  const failedFiles=useRef(new Map<string,ChatAttachment>());
  const hero = variant === "hero";
  const basePlaceholder = placeholder ?? t("inputPlaceholder");
  const composerPlaceholder = basePlaceholder;
  const canSend = !busy && !locked && !editingLocked && (external || (Boolean(workspaceId) && !uploading && !uploadError)) && Boolean(value.trim() || (!external && attachments.length)) && !attachments.some(file=>file.reason==="reselect_required");

  const sendStatus = locked ? (lockMessage || (zh ? "发送暂不可用；可以继续撰写草稿。" : "Sending is unavailable. You can keep drafting."))
    : busy ? (zh ? "正在等待发送确认；可以继续编辑，Enter 换行。" : "Waiting for confirmation. Keep editing; Enter adds a line.")
    : !external && !workspaceId ? (zh ? "正在确认工作区；可以先撰写草稿。" : "Identifying the workspace. You can draft now.") : "";

  useEffect(() => {
    if (!textareaRef.current) return;
    textareaRef.current.style.height = "auto";
    textareaRef.current.style.height = `${Math.min(textareaRef.current.scrollHeight, hero ? 192 : 240)}px`;
  }, [value, hero]);
  useEffect(() => {
    setUploading(false); setUploadError(""); setReferenceName(""); setDragging(false);setFileStates({});failedFiles.current.clear();
    retryRef.current = null; dragDepth.current = 0;
    return () => { uploadRef.current?.abort(); uploadRef.current = null; };
  }, [sessionId, external, workspaceId, identityGeneration]);
  const suggestions = useComposerSuggestions({ value, onChange, inputRef: textareaRef,
    disabled: external || editingLocked || !workspaceId || uploading || !onAttachmentsChange, onReference: addReference, sessionId });

  function submit() {
    if (canSend && !uploadRef.current) onSend();
  }
  function withinLimits(files: readonly { size: number }[]): boolean {
    const existing = latest.current.attachments;
    return existing.length + files.length <= MAX_FILES && files.every(file => file.size <= MAX_FILE_BYTES)
      && [...existing, ...files].reduce((total, file) => total + file.size, 0) <= MAX_TOTAL_BYTES;
  }
  function pickFiles(files: readonly File[]) {
    if (!files.length || uploadRef.current || !getWorkspaceIdentity() || latest.current.locked || external) return;
    if (!withinLimits(files)) { retryRef.current = null; setUploadError(tUi("attachmentLimits")); return; }
    void uploadPrepared(() => Promise.all(files.map(fileToAttachment)));
  }
  function addReference(item: ComposerOption): boolean {
    if (uploadRef.current || !getWorkspaceIdentity() || latest.current.locked || latest.current.external) return false;
    if (latest.current.attachments.some(file => file.reference?.kind === item.kind && file.reference.id === item.id)) return true;
    if (!withinLimits([{ size: 0 }])) { retryRef.current = null; setUploadError(tUi("attachmentLimits")); return false; }
    void uploadPrepared(async signal => [await prepareComposerReference(item, signal)], item.label);
    return true;
  }
  async function uploadPrepared(prepare: (signal: AbortSignal) => Promise<ChatAttachment[]>, label = "") {
    if (uploadRef.current || !getWorkspaceIdentity() || latest.current.locked || latest.current.external) return;
    if (!latest.current.onAttachmentsChange) { setUploadError(t("attachmentNotReady")); return; }
    const scope = latest.current.sessionId;
    const generation = workspaceGeneration();
    const controller = new AbortController();
    uploadRef.current = controller;
    setUploading(true);
    setReferenceName(label);
    setUploadError("");
    retryRef.current = () => { void uploadPrepared(prepare, label); };
    try {
      const picked = await prepare(controller.signal);
      if (controller.signal.aborted || latest.current.sessionId !== scope || generation !== workspaceGeneration()) return;
      if (!withinLimits(picked)) throw new Error("attachment_limits");
      for (const file of picked) {
        if(controller.signal.aborted)break;
        setFileStates(old=>({...old,[file.id]:{name:file.name,state:"uploading"}}));
        try {
          const response=await callApi<UploadEnvelope>("/agent/attachments/upload",{method:"POST",signal:controller.signal,body:{upload_id:"upload_"+file.id,attachments:[file]}});
          if(controller.signal.aborted||latest.current.sessionId!==scope||generation!==workspaceGeneration())return;
          const uploaded=response.attachments?.[0];
          if(response.ok===false||!uploaded?.artifact_uri||uploaded.uploaded===false)throw new Error("upload_rejected");
          const merged={...file,...uploaded,data_url:file.kind==="image"?file.data_url:undefined,text:undefined,reference:file.reference};
          const next=[...latest.current.attachments.filter(item=>item.id!==file.id),merged];
          latest.current.attachments=next;latest.current.onAttachmentsChange?.(next);
          failedFiles.current.delete(file.id);setFileStates(old=>({...old,[file.id]:{name:file.name,state:"ready"}}));
        } catch(error) {
          if(controller.signal.aborted||generation!==workspaceGeneration())throw error;
          failedFiles.current.set(file.id,file);setFileStates(old=>({...old,[file.id]:{name:file.name,state:"failed"}}));
        }
      }
      if(failedFiles.current.size){setUploadError(t("uploadFailed"));retryRef.current=()=>{void uploadPrepared(async()=>[...failedFiles.current.values()]);};}
      else retryRef.current=null;
    } catch (error) {
      if (!controller.signal.aborted && generation === workspaceGeneration()) setUploadError(error instanceof ComposerResourceError && error.code === "binary"
        ? tc("binary") : error instanceof Error && error.message === "attachment_limits"
        ? tUi("attachmentLimits") : label ? tc("referenceFailed") : t("uploadFailed"));
    } finally {
      if (uploadRef.current === controller) uploadRef.current = null;
      if (!controller.signal.aborted && generation === workspaceGeneration()) setUploading(false);
    }
  }
  function cancelUpload() {
    uploadRef.current?.abort(); uploadRef.current = null;
    setUploading(false); setUploadError(""); setReferenceName(""); retryRef.current = null;failedFiles.current.clear();setFileStates({});
  }
  function openPicker(imagesOnly: boolean) {
    if (!pickerRef.current) return;
    pickerRef.current.accept = imagesOnly ? "image/*" : "image/*,.pdf,.txt,.md,.csv,.json,.html,.xml,.py,.ts,.tsx,.js,.yaml,.yml,.log";
    pickerRef.current.click();
  }
  function removeAttachment(id: string) {
    failedFiles.current.delete(id);
    setFileStates(old => { const next = { ...old }; delete next[id]; return next; });
    if (!failedFiles.current.size) { setUploadError(""); retryRef.current = null; }
    const next = latest.current.attachments.filter((file) => file.id !== id);
    latest.current.attachments = next;
    latest.current.onAttachmentsChange?.(next);
  }

  const composer = (
    <div data-chat-composer={variant} data-composer-mode={external ? 'external' : assistant ? 'assistant' : 'agent'}
      onDragEnter={event => { if (!external && event.dataTransfer.types.includes("Files")) { event.preventDefault(); dragDepth.current += 1; if (!editingLocked && workspaceId && !uploading) setDragging(true); } }}
      onDragOver={event => { if (!external && event.dataTransfer.types.includes("Files")) { event.preventDefault(); event.dataTransfer.dropEffect = editingLocked || !workspaceId || uploading ? "none" : "copy"; } }}
      onDragLeave={event => { if (!external && event.dataTransfer.types.includes("Files")) { dragDepth.current = Math.max(0, dragDepth.current - 1); if (!dragDepth.current) setDragging(false); } }}
      onDrop={event => { if (!external && event.dataTransfer.types.includes("Files")) { event.preventDefault(); dragDepth.current = 0; setDragging(false); suggestions.dismiss(); pickFiles(Array.from(event.dataTransfer.files)); } }}
      className={`relative min-w-0 ${taskHeader ? "rounded-b-2xl" : "rounded-2xl"} border border-[color:var(--line-hi)] bg-[color:var(--card-hi)] p-3 transition-colors focus-within:border-brand-500/60 sm:p-4`}>
      {dragging ? <div role="status" className="pointer-events-none absolute inset-0 z-10 flex items-center justify-center rounded-2xl border-2 border-dashed border-brand-500 bg-[color:var(--card-hi)] text-sm">{tc("drop")}</div> : null}
      {!external&&Object.entries(fileStates).filter(([,f])=>f.state!=="ready").map(([id,file])=><div key={id} className="mb-2 flex items-center gap-2 text-xs" role="status" data-testid="attachment-progress"><span className="min-w-0 flex-1 truncate">{file.name}</span><span>{file.state==="failed"?(zh?"上传失败":"Upload failed"):(zh?"准备发送…":"Preparing…")}</span>{file.state==="failed"&&<button type="button" className="min-h-11 underline" disabled={uploading} onClick={()=>{const value=failedFiles.current.get(id);if(value)void uploadPrepared(async()=>[value]);}}>{zh?"重试此文件":"Retry this file"}</button>}{file.state==="failed"&&<button type="button" className="ui-icon-button" disabled={uploading || editingLocked} aria-label={`${t("removeAttachment")}: ${file.name}`} onClick={()=>removeAttachment(id)}><XIcon size={14}/></button>}</div>)}
      {!external && attachments.length ? (
        <div className="mb-3 flex max-h-28 flex-wrap gap-2 overflow-y-auto">
          {attachments.map((file) => (
            <div key={file.id} data-context-id={file.reference?.id} data-context-kind={file.reference?.kind}
              title={file.reference ? `${tc("snapshot")} · ${file.reference.id}${file.reference.truncated ? ` · ${tc("excerpt")}` : ""}` : file.name}
              className="flex max-w-full items-center gap-2 rounded-lg border border-[color:var(--line)] bg-[color:var(--card)] py-1 pl-2 pr-1 text-xs">
              {file.reference ? <ComposerSourceIcon kind={file.reference.kind} /> : file.data_url?.startsWith("data:image/") ? (
                // eslint-disable-next-line @next/next/no-img-element
                <img src={file.data_url} alt="" className="h-6 w-6 rounded object-cover" />
              ) : <FileIcon size={15} />}
              {file.reference ? <ReferenceSnapshot attachment={file} className="min-w-0 max-w-[180px] truncate text-left hover:underline">{file.reference.label}</ReferenceSnapshot> : <span className="min-w-0 max-w-[180px] truncate">{file.name}</span>}
              {file.reason==="reselect_required"&&<span className="text-warn">{zh?"请重新选择文件":"Select this file again"}</span>}
              <span className="shrink-0 text-[color:var(--text-muted)]">{file.reference ? (file.reference.truncated ? "…" : "@") : formatBytes(file.size)}</span>
              <button type="button" onClick={() => removeAttachment(file.id)} disabled={editingLocked}
                className="ui-icon-button shrink-0 disabled:opacity-40" aria-label={file.reference ? tc("remove", { name: file.reference.label }) : `${t("removeAttachment")}: ${file.name}`}>
                <XIcon size={14} />
              </button>
            </div>
          ))}
        </div>
      ) : null}
      <textarea
        ref={(node) => { textareaRef.current = node; if (typeof inputRef === "function") inputRef(node); else if (inputRef) (inputRef as { current: HTMLTextAreaElement | null }).current = node; }}
        value={value} {...suggestions.inputProps}
        role={external ? "textbox" : "combobox"} aria-haspopup={external ? undefined : "listbox"}
        onKeyDown={(event) => {
          if (event.nativeEvent.isComposing || event.keyCode === 229 || suggestions.onKeyDown(event)) return;
          if (event.key === "Enter" && !event.shiftKey && canSend && !uploadRef.current) { event.preventDefault(); submit(); }
        }}
        maxLength={external ? 8000 : undefined}
        onPaste={(event) => {
          if (external) return;
          const files = Array.from(event.clipboardData.files);
          if (files.length) { event.preventDefault(); void pickFiles(files); }
        }}
        disabled={editingLocked} rows={hero ? 2 : 1}
        aria-label={basePlaceholder}
        aria-describedby={[sendStatus ? `${fieldId}-send-status` : "", uploading || uploadError ? `${fieldId}-status` : ""].filter(Boolean).join(" ") || undefined}
        placeholder={composerPlaceholder}
        className={`block ${hero ? "min-h-[88px]" : "min-h-8"} w-full resize-none overflow-y-auto bg-transparent text-base leading-6 text-[color:var(--text-base)] placeholder:text-[color:var(--text-muted)] focus:outline-none disabled:cursor-not-allowed disabled:opacity-70 sm:text-[15px]`}
      />
      {external ? <div className="mt-2 flex justify-end" data-testid="external-send-toolbar">
        <button type="button" onClick={submit} disabled={!canSend} aria-label={t('send')} aria-busy={sending}
          data-testid="external-send" className="inline-flex min-h-9 items-center gap-2 rounded-lg bg-brand-500 px-4 text-sm text-white hover:bg-brand-400 disabled:cursor-not-allowed disabled:opacity-40">
          <SendIcon size={16} /><span>{t('send')}</span>
        </button>
      </div> : <div data-composer-toolbar className="mt-2 flex flex-wrap items-center gap-1.5">
        <input ref={pickerRef} id={fieldId} type="file" multiple className="hidden" tabIndex={-1}
          aria-label={t("addAttachment")} disabled={editingLocked || !workspaceId || uploading || !onAttachmentsChange}
          accept="image/*,.pdf,.txt,.md,.csv,.json,.html,.xml"
          onChange={(event) => { const files = Array.from(event.currentTarget.files ?? []); event.currentTarget.value = ""; void pickFiles(files); }} />
        {assistant ? <button type="button" className="ui-icon-button" aria-label={t("addAttachment")}
          disabled={editingLocked || !workspaceId || uploading || !onAttachmentsChange} onClick={() => openPicker(false)}><PlusIcon size={18}/></button> : <ComposerAddMenu disabled={editingLocked} uploadDisabled={!workspaceId || uploading || !onAttachmentsChange}
          settings={settings} onSettingsChange={onSettingsChange}
          onFiles={openPicker} onTrigger={suggestions.openTrigger} onOpen={suggestions.dismiss} inputRef={textareaRef} />}
        {!assistant && <div data-composer-options>
          <ComposerPermissionMenu settings={settings} onSettingsChange={onSettingsChange} disabled={editingLocked} size={variant} compact/>
        </div>}
        <div data-composer-models title={sending ? (zh ? "模型与权限更改只用于下一条消息，不改变正在执行的任务。" : "Model and permission changes apply to the next message, not the running task.") : undefined} className="ml-auto flex min-w-0 items-center gap-1.5">
          {!external && !assistant && <ComposerModelMenu settings={settings} onSettingsChange={onSettingsChange} modelOptions={modelOptions} disabled={editingLocked} size={variant} />}
          {!assistant && contextControl}
        </div>
        <div data-composer-actions>
          {sending && value.trim() && onGuide ? <button type="button" onClick={onGuide} data-testid="guide-current-turn" disabled={!canSend || attachments.length > 0}
            title={zh ? "将文字补充到当前轮次的下一次模型调用；附件请排队发送。" : "Add text to this turn's next model call. Queue attachments separately."}
            className="min-h-9 shrink-0 rounded-lg px-2 text-xs hover:bg-brand-500/10 disabled:opacity-40">{zh ? "指导本轮" : "Guide this turn"}</button> : null}
          {sending && onCancel ? <button type="button" onClick={onCancel} disabled={stopping} aria-busy={stopping} data-testid="stop-current-command"
            className="ui-icon-button border border-[color:var(--line-hi)] disabled:opacity-40" title={zh?"请求停止当前轮次；已完成的外部操作不会撤销。":"Request this turn to stop. Completed external actions remain."} aria-label={stopping ? (zh ? "正在停止" : "Stopping") : t("cancelTurn")}><StopIcon size={17} /></button> : null}
          {(!sending||Boolean(value.trim()||attachments.length))&&<button type="button" onClick={submit} disabled={!canSend} aria-label={sendLabel} title={locked ? lockMessage : sendLabel} aria-busy={busy}
            data-testid="native-command-send" className={`inline-flex h-9 shrink-0 items-center justify-center gap-1.5 rounded-full bg-brand-500 text-white transition-colors hover:bg-brand-400 disabled:cursor-not-allowed disabled:opacity-40 ${sending && !assistant ? "px-3" : "w-9"}`}><SendIcon size={17} />{sending && !assistant ? <span className="text-xs">{sendLabel}</span> : null}</button>}
        </div>
      </div>}
      {sendStatus && <p id={`${fieldId}-send-status`} role="status" className="mt-2 text-xs text-[color:var(--text-muted)]">{sendStatus}</p>}
      {!external && (uploading || uploadError) ? <div id={`${fieldId}-status`} role={uploadError ? "alert" : "status"} className="mt-3 flex items-center justify-between gap-2 text-xs leading-relaxed text-[color:var(--text-muted)]">
        <span className="min-w-0 flex-1">{uploadError || (referenceName ? tc("adding", { name: referenceName }) : t("uploadingAttachment"))}</span>
        {uploadError && retryRef.current ? <button type="button" className="btn btn-ghost shrink-0" onClick={() => retryRef.current?.()}>{tc("retry")}</button> : null}
        {uploadError ? <button type="button" className="ui-icon-button shrink-0" aria-label={tUi("close")} onClick={cancelUpload}><XIcon size={14} /></button> : <button type="button" className="btn btn-ghost shrink-0" onClick={cancelUpload}>{tUi("cancel")}</button>}
      </div> : null}
    </div>
  );
  const editor = external ? composer : <ComposerSuggestions controller={suggestions}>{composer}</ComposerSuggestions>;
  return hero ? editor : (
    <div className="shrink-0 bg-transparent" data-testid="composer-dock">
      <div className="mx-auto max-w-[800px] px-4 pb-3 pt-1 sm:px-6">{taskHeader}{editor}</div>
    </div>
  );
}
