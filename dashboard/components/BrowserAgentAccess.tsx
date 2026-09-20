"use client";

import { useState } from "react";
import { useLocale } from "next-intl";
import { Card, Pill } from "./Page";
import { confirm } from "../lib/dialogs";
import type { DesktopBrowserResponse } from "../lib/browserDesktopTypes";

export function BrowserAgentAccess({ state, busy, onAction }: {
  state: DesktopBrowserResponse;
  busy: boolean;
  onAction: (body: Record<string, unknown>) => Promise<DesktopBrowserResponse | undefined>;
}) {
  const zh = useLocale().startsWith("zh");
  const text = (cn: string, en: string) => zh ? cn : en;
  const [sites, setSites] = useState("");
  const [vision, setVision] = useState(false);
  const [downloads, setDownloads] = useState(false);
  const [error, setError] = useState("");
  const [readingFile, setReadingFile] = useState(false);
  const access = state.agent_access;
  const disabled = busy || !state.running || readingFile;

  async function grant() {
    setError("");
    const origins = sites.split(/[\s,]+/).filter(Boolean);
    if (!origins.length) { setError(text("请填写要授权的网站来源。", "Enter the site origins to authorize.")); return; }
    if (!await confirm({ title: text("授权 Agent 操作这些网站", "Delegate these sites to the Agent"),
      message: <div className="space-y-3"><p>{text("授权有效期一小时。Agent 可以读取并操作这些网站；仅授权当前任务需要的站点。资金、签名和身份验证请人工接管。", "Access lasts one hour. The Agent can read and interact with these sites. Authorize only task-relevant sites; take over for financial actions, signing and verification.")}</p><p className="break-all">{origins.join(" · ")}</p>{vision && <p>{text("截图会发送给模型，可能保留在对话记录中。", "Screenshots will be sent to the model and may remain in the conversation.")}</p>}</div>, tone: "warning" })) return;
    await onAction({ operation: "agent_grant", origins, ttl_s: 3600, screenshots: vision, downloads });
  }

  async function stageFile(file?: File) {
    if (!file || disabled) return;
    setError("");
    if (file.size > 10 * 1024 * 1024) { setError(text("单文件上限 10 MB", "File limit: 10 MB")); return; }
    if (!await confirm({ message: text(`允许 Agent 将 ${file.name} 上传到已授权网站？`, `Allow the Agent to upload ${file.name} to authorized sites?`) })) return;
    setReadingFile(true);
    try {
      const data = await new Promise<string>((resolve, reject) => {
        const reader = new FileReader();
        reader.onload = () => resolve(String(reader.result).split(",", 2)[1]);
        reader.onerror = () => reject(new Error("file_read_failed"));
        reader.readAsDataURL(file);
      });
      await onAction({ operation: "agent_upload", name: file.name, data });
      await onAction({ operation: "status" });
    } catch {
      setError(text("无法读取文件，请重新选择。", "Unable to read this file. Select it again."));
    } finally { setReadingFile(false); }
  }

  return <Card title={text("Agent 访问权限", "Agent access")}
    description={text("按网站授权，默认一小时；管理员配置接口不开放给 Agent。", "Site-scoped access for one hour; administrator controls stay separate.")}
    actions={<Pill tone={access?.enabled ? "ok" : "neutral"}>{access?.occupied ? text("Agent 正在使用", "Agent session active") : access?.enabled ? text("已授权", "Authorized") : text("未授权", "Not authorized")}</Pill>}>
    <div className="space-y-3">
      <label className="block text-xs text-ink-400">{text("允许访问的网站（每行一个完整来源）", "Allowed site origins (one per line)")}
        <textarea className="input mt-1 w-full" rows={3} value={sites} onChange={(e) => setSites(e.target.value)} placeholder="https://example.com" />
      </label>
      <div className="flex flex-wrap gap-4 text-xs">
        <label className="flex items-center gap-2"><input type="checkbox" checked={vision} onChange={(e) => setVision(e.target.checked)} />{text("允许将截图发送给模型", "Allow screenshots to the model")}</label>
        <label className="flex items-center gap-2"><input type="checkbox" checked={downloads} onChange={(e) => setDownloads(e.target.checked)} />{text("允许保存下载文件", "Allow saving downloads")}</label>
      </div>
      <div className="flex flex-wrap gap-2">
        <button type="button" className="btn btn-primary" disabled={disabled} onClick={() => void grant()}>{text("授权一小时", "Authorize for one hour")}</button>
        <button type="button" className="btn btn-ghost" disabled={disabled} onClick={() => void onAction({ operation: "agent_revoke" })}>{text("撤销 Agent 权限", "Revoke Agent access")}</button>
      </div>
      {access?.origins?.length ? <p className="break-words text-xs text-ink-400">{access.origins.join(" · ")}</p> : null}
      <label className="block text-xs text-ink-400">{text("选择 Agent 可以上传的文件（每个文件单独授权）", "Select a file the Agent may upload (explicit per-file approval)")}
        <input type="file" className="mt-2 block max-w-full" disabled={disabled || !access?.enabled} onChange={(e) => { const file = e.target.files?.[0]; e.target.value = ""; void stageFile(file); }} />
      </label>
      {access?.uploads?.map((file) => <p className="text-xs" key={file.id}>{file.name} <code>{file.id}</code></p>)}
      {access?.last_actions?.length ? <p role="status" className="text-xs text-ink-400">{text("最近操作：", "Recent actions: ")}{access.last_actions.map((e) => e.action || e.kind).join(" → ")}</p> : null}
      {error && <p role="alert" className="text-sm text-danger">{error}</p>}
    </div>
  </Card>;
}
