"use client";

import { copy as i18nCopy } from "../../lib/i18n";

import { useEffect, useState, type ReactNode } from "react";
import { useLocale } from "next-intl";
import * as Dialog from "@radix-ui/react-dialog";
import { callApi } from "../../lib/clientApi";
import type { ChatAttachment } from "../../lib/chat";
import { XIcon } from "../icons";

type Reference = Pick<ChatAttachment, "name" | "artifact_uri" | "reference" | "text">;
export function ReferenceSnapshot({ attachment, children, className = "" }: {
  attachment: Reference; children?: ReactNode; className?: string;
}) {
  const zh = useLocale().startsWith("zh");
  const [open, setOpen] = useState(false), [retry, setRetry] = useState(0);
  const [data, setData] = useState<{ content: string; truncated?: boolean; artifact_sha256?: string | null } | null>(null);
  const [loading, setLoading] = useState(false), [error, setError] = useState(false);
  useEffect(() => {
    if (!open) return;
    const controller = new AbortController();
    let disposed = false;
    const timer = setTimeout(() => controller.abort(), 12000);
    setData(null); setError(false); setLoading(true);
    if (!attachment.artifact_uri) {
      setData(attachment.text ? { content: attachment.text } : null);
      setError(!attachment.text); setLoading(false); clearTimeout(timer);
      return () => controller.abort();
    }
    void callApi<{ ok: boolean; content: string; truncated?: boolean; artifact_sha256?: string | null }>(
      `/agent/commands/reference?uri=${encodeURIComponent(attachment.artifact_uri)}`, { signal: controller.signal },
    ).then(result => { if (controller.signal.aborted) return; if (!result.ok) throw new Error("preview_unavailable"); setData(result); })
      .catch(() => { if (!disposed) setError(true); })
      .finally(() => { clearTimeout(timer); if (!disposed) setLoading(false); });
    return () => { disposed = true; clearTimeout(timer); controller.abort(); };
  }, [open, attachment.artifact_uri, attachment.text, retry]);
  let content = data?.content || "";
  let captured = attachment.reference?.captured_at || "";
  try { const parsed = JSON.parse(content); if (typeof parsed.content === "string") { content = parsed.content; captured = parsed.reference?.captured_at || captured; } } catch { /* Ordinary text reference. */ }
  const title = attachment.reference?.label || attachment.name;
  return <Dialog.Root open={open} onOpenChange={setOpen}>
    <Dialog.Trigger asChild><button type="button" className={className} title={i18nCopy(zh, "copy.components_chat_ReferenceSnapshot.001")} data-testid="reference-snapshot-trigger">{children || title}</button></Dialog.Trigger>
    <Dialog.Portal><Dialog.Overlay className="ui-modal-overlay" /><Dialog.Content className="ui-dialog" style={{ width:"min(760px,calc(100vw - 24px))", maxHeight:"calc(100dvh - 32px)", display:"flex", flexDirection:"column" }}>
      <div className="flex shrink-0 items-start justify-between gap-3"><Dialog.Title className="min-w-0 break-words text-base font-semibold">{title}</Dialog.Title>
        <Dialog.Close asChild><button type="button" className="ui-icon-button" aria-label={i18nCopy(zh, "copy.components_chat_ReferenceSnapshot.002")}><XIcon size={16} /></button></Dialog.Close></div>
      <Dialog.Description className="mt-2 shrink-0 text-xs leading-5 text-[color:var(--text-muted)]">
        {i18nCopy(zh, "copy.components_chat_ReferenceSnapshot.003")}
      </Dialog.Description>
      <dl className="my-3 shrink-0 text-xs text-[color:var(--text-muted)]">
        {attachment.reference?.id && <div className="flex gap-2"><dt>{i18nCopy(zh, "copy.components_chat_ReferenceSnapshot.004")}</dt><dd className="min-w-0 break-all font-mono">{attachment.reference.id}</dd></div>}
        {captured && <div className="flex gap-2"><dt>{i18nCopy(zh, "copy.components_chat_ReferenceSnapshot.005")}</dt><dd>{Number.isFinite(Date.parse(captured)) ? new Date(captured).toLocaleString(zh ? "zh-CN" : "en-US") : captured}</dd></div>}
        {data?.artifact_sha256 && <div className="flex gap-2"><dt>{i18nCopy(zh, "copy.components_chat_ReferenceSnapshot.006")}</dt><dd className="min-w-0 break-all font-mono">{data.artifact_sha256}</dd></div>}
      </dl>
      {loading ? <p role="status">{i18nCopy(zh, "copy.components_chat_ReferenceSnapshot.007")}</p> : error ? <div role="alert" className="text-sm"><p>{i18nCopy(zh, "copy.components_chat_ReferenceSnapshot.008")}</p><button type="button" className="btn btn-ghost mt-3" onClick={() => setRetry(value => value+1)}>{i18nCopy(zh, "copy.components_chat_ReferenceSnapshot.009")}</button></div> :
        <pre className="min-h-0 overflow-auto whitespace-pre-wrap break-words rounded-lg bg-[color:var(--bg)] p-3 text-xs leading-6" data-testid="reference-snapshot-content">{content}</pre>}
      {(data?.truncated || attachment.reference?.truncated) && <p className="mt-2 text-xs text-[color:var(--text-muted)]">{i18nCopy(zh, "copy.components_chat_ReferenceSnapshot.010")}</p>}
    </Dialog.Content></Dialog.Portal>
  </Dialog.Root>;
}
