"use client";

import { useEffect, useMemo, useState } from "react";
import { useTranslations } from "next-intl";

import {
  clientApi,
  type BrowserSessionRecord,
  type BrowserSessionScreenshot,
  type BrowserSessionScreenshotResponse,
} from "../../lib/clientApi";
import {
  liveEventsToBlocks,
  type ChatAttachment,
  type ChatMessage,
  type ChatThread,
  type NativeBlock,
  type NativeBlockEnvelope,
} from "../../lib/chat";
import {
  ChevronLeftIcon,
  ChevronRightIcon,
  ChartIcon,
  FileIcon,
  GlobeIcon,
  ImageIcon,
  MessagesIcon,
} from "../icons";
import { AgentChartPanel, hasAgentVisuals } from "./AgentChartPanel";
import { Markdown } from "./Markdown";
import { CopyButton } from "./tool-cards/atoms";

type CanvasKind = "browser" | "file" | "html" | "web" | "image" | "text" | "charts";

type CanvasItem = {
  id: string;
  kind: CanvasKind;
  title: string;
  subtitle?: string;
  body?: string;
  html?: string;
  imageSrc?: string;
  url?: string;
  sessionId?: string;
  mimeType?: string;
  language?: string;
  thread?: ChatThread | null;
  seenAt: number;
};

type ToolLike = {
  action?: unknown;
  payload?: unknown;
  result?: unknown;
  ok?: unknown;
  error?: unknown;
  error_kind?: unknown;
  elapsed_ms?: unknown;
};

type BrowserSessionRef = {
  sessionId?: string;
  url?: string;
  seenAt: number;
};

function recordOf(value: unknown): Record<string, unknown> {
  return value && typeof value === "object" && !Array.isArray(value)
    ? (value as Record<string, unknown>)
    : {};
}

function stringValue(value: unknown): string {
  return typeof value === "string" ? value : "";
}

function parseJsonRecord(value: string): Record<string, unknown> {
  const trimmed = value.trim();
  if (!trimmed || !trimmed.startsWith("{")) return {};
  try {
    return recordOf(JSON.parse(trimmed));
  } catch {
    return {};
  }
}

function resultRecordFrom(value: unknown): Record<string, unknown> {
  const direct = recordOf(value);
  if (Object.keys(direct).length > 0) return direct;
  if (typeof value !== "string") return {};
  const parsed = parseJsonRecord(value);
  if (Object.keys(parsed).length > 0) return parsed;
  const stdout = value.match(/---- stdout ----\s*([\s\S]*?)(?:\r?\n---- stderr ----|$)/);
  return stdout?.[1] ? parseJsonRecord(stdout[1]) : {};
}

function decodeDataText(dataUrl: string): string {
  const idx = dataUrl.indexOf(",");
  if (idx < 0) return "";
  const meta = dataUrl.slice(0, idx).toLowerCase();
  const body = dataUrl.slice(idx + 1);
  try {
    if (meta.includes(";base64")) return atob(body);
    return decodeURIComponent(body);
  } catch {
    return "";
  }
}

function looksLikeHtml(value: string): boolean {
  const trimmed = value.trim();
  if (!trimmed) return false;
  if (/^<!doctype\s+html/i.test(trimmed)) return true;
  if (/^<html[\s>]/i.test(trimmed)) return true;
  if (/^<body[\s>]/i.test(trimmed)) return true;
  if (/^<(script|style|iframe|svg|canvas)[\s>]/i.test(trimmed)) return true;
  const tag = trimmed.match(/^<([a-z][\w:-]*)(?:\s[^>]*)?>[\s\S]*<\/\1>\s*$/i);
  return Boolean(tag);
}

function withBaseUrl(html: string, url?: string): string {
  if (!url || /<base\s/i.test(html)) return html;
  const base = `<base href="${url.replace(/"/g, "&quot;")}">`;
  if (/<head[\s>]/i.test(html)) {
    return html.replace(/<head([^>]*)>/i, `<head$1>${base}`);
  }
  return `${base}${html}`;
}

function filenameFromPath(path: string): string {
  const parts = path.replace(/\\/g, "/").split("/").filter(Boolean);
  return parts[parts.length - 1] || path || "file";
}

function languageForPath(path: string): string {
  const ext = filenameFromPath(path).split(".").pop()?.toLowerCase() || "";
  const map: Record<string, string> = {
    js: "javascript",
    jsx: "jsx",
    ts: "typescript",
    tsx: "tsx",
    py: "python",
    json: "json",
    yml: "yaml",
    yaml: "yaml",
    md: "markdown",
    html: "html",
    css: "css",
    sh: "shell",
    ps1: "powershell",
  };
  return map[ext] || ext || "text";
}

function unwrapBlock(env: NativeBlockEnvelope): NativeBlock {
  const block = env.block;
  if (block && typeof block === "object" && Object.keys(block).length > 0) {
    return block as NativeBlock;
  }
  return env as unknown as NativeBlock;
}

function bodyFromResult(result: Record<string, unknown>): string {
  const content = result.content;
  if (Array.isArray(content)) {
    const textParts = content
      .map((part) => stringValue(recordOf(part).text))
      .filter(Boolean);
    if (textParts.length) return textParts.join("\n");
  }
  return (
    stringValue(result.text) ||
    stringValue(result.body) ||
    stringValue(result.content) ||
    stringValue(result.markdown) ||
    stringValue(result.diff) ||
    ""
  );
}

function browserScriptPayload(payload: Record<string, unknown>): Record<string, unknown> {
  const scriptName = String(
    payload.name || payload.script || payload.file || payload.path || "",
  )
    .replace(/\\/g, "/")
    .toLowerCase();
  const skillId = String(payload.skill_id || payload.skill || "").toLowerCase();
  const isBrowserScript =
    scriptName.endsWith("browser_session.py") ||
    (skillId === "browser" && scriptName.includes("browser_session"));
  if (!isBrowserScript) return {};

  const args = Array.isArray(payload.args) ? payload.args : [payload.args];
  for (const arg of args) {
    if (typeof arg !== "string") continue;
    const parsed = parseJsonRecord(arg);
    if (Object.keys(parsed).length > 0) return parsed;
  }
  return {};
}

function pathFromTool(payload: Record<string, unknown>, result: Record<string, unknown>): string {
  return String(
    payload.path ||
      payload.file ||
      payload.filename ||
      result.path ||
      result.file ||
      result.filename ||
      "",
  );
}

function itemFromAttachment(
  attachment: ChatAttachment,
  message: ChatMessage,
  index: number,
): CanvasItem | null {
  const mime = attachment.mime_type || "";
  const name = attachment.name || "attachment";
  const base = {
    id: `attachment:${message.id}:${attachment.id || index}`,
    title: name,
    subtitle: mime || undefined,
    mimeType: mime,
    seenAt: message.ts,
  };
  if (attachment.data_url?.startsWith("data:image/")) {
    return { ...base, kind: "image", imageSrc: attachment.data_url };
  }
  const text = attachment.text || decodeDataText(attachment.data_url || "");
  if (!text) return null;
  if (mime.includes("html") || /\.html?$/i.test(name) || looksLikeHtml(text)) {
    return { ...base, kind: "html", html: text, language: "html" };
  }
  return {
    ...base,
    kind: "file",
    body: text,
    language: languageForPath(name),
  };
}

function itemFromToolLike(
  value: ToolLike,
  idPrefix: string,
  seenAt: number,
): CanvasItem | null {
  const action = String(value.action || "").toLowerCase();
  const payload = recordOf(value.payload);
  const result = resultRecordFrom(value.result);
  const error = stringValue(value.error || result.error || result.detail);
  const path = pathFromTool(payload, result);
  const url = String(payload.url || result.url || result.link || result.href || "");
  const title = String(result.title || payload.title || path || url || action || "artifact");
  const html = stringValue(result.html || result.rendered_html || payload.html);
  const body = bodyFromResult(result) || error;
  const imageSrc = stringValue(result.data_uri || result.image_data_uri || payload.data_uri);
  const failed = value.ok === false || Boolean(error);

  if (failed && !html && !imageSrc) {
    return null;
  }

  if (imageSrc.startsWith("data:image/")) {
    return {
      id: `${idPrefix}:image`,
      kind: "image",
      title,
      subtitle: url || path || undefined,
      imageSrc,
      url,
      seenAt,
    };
  }

  if (html) {
    return {
      id: `${idPrefix}:html`,
      kind: action.includes("web") ? "web" : "html",
      title,
      subtitle: url || path || undefined,
      html,
      url,
      language: "html",
      seenAt,
    };
  }

  if (action.includes("web_fetch") || action.includes("web_search_fetch")) {
    if (!body) return null;
    return {
      id: `${idPrefix}:web`,
      kind: "web",
      title: title || url,
      subtitle: url || undefined,
      body,
      url,
      seenAt,
    };
  }

  if (
    action === "write_file" ||
    action === "edit_file" ||
    action === "create_file"
  ) {
    if (!body && !path) return null;
    if (body && (looksLikeHtml(body) || /\.html?$/i.test(path))) {
      return {
        id: `${idPrefix}:html-file`,
        kind: "html",
        title: filenameFromPath(path),
        subtitle: path,
        html: body,
        language: "html",
        seenAt,
      };
    }
    return {
      id: `${idPrefix}:file`,
      kind: "file",
      title: filenameFromPath(path),
      subtitle: path || action,
      body: body || String(result.message || result.status || ""),
      language: languageForPath(path),
      seenAt,
    };
  }

  if (body && looksLikeHtml(body)) {
    return {
      id: `${idPrefix}:html-body`,
      kind: "html",
      title,
      subtitle: url || path || undefined,
      html: body,
      url,
      language: "html",
      seenAt,
    };
  }

  return null;
}

function looksLikeBrowserTool(value: ToolLike): boolean {
  const action = String(value.action || "").toLowerCase();
  const payload = recordOf(value.payload);
  const result = resultRecordFrom(value.result);
  const scriptPayload = browserScriptPayload(payload);
  const op = String(
    scriptPayload.operation ||
      payload.operation ||
      payload.action ||
      result.action ||
      "",
  ).toLowerCase();
  if (Object.keys(scriptPayload).length > 0) return true;
  if (action.includes("browser") || action.includes("cdp")) return true;
  if (op.includes("browser") || op.includes("cdp")) return true;
  if (
    op === "open" ||
    op === "navigate" ||
    op === "snapshot" ||
    op === "screenshot" ||
    op === "click" ||
    op === "type" ||
    op === "press" ||
    op === "scroll" ||
    op === "goto"
  ) {
    return true;
  }
  return Boolean(
    result.current_url ||
      result.engine ||
      result.history ||
      result.screenshots ||
      payload.interactive === true,
  );
}

function browserSessionRefFromTool(
  value: ToolLike,
  seenAt: number,
): BrowserSessionRef | null {
  if (!looksLikeBrowserTool(value)) return null;
  const payload = recordOf(value.payload);
  const result = resultRecordFrom(value.result);
  const scriptPayload = browserScriptPayload(payload);
  const snapshot = recordOf(result.snapshot);
  const sessionId = String(
    result.session_id ||
      payload.session_id ||
      scriptPayload.session_id ||
      result.browser_session_id ||
      payload.browser_session_id ||
      "",
  ).trim();
  const url = String(
    result.current_url ||
      result.url ||
      snapshot.url ||
      payload.url ||
      scriptPayload.url ||
      "",
  ).trim();
  if (!sessionId && !url) return null;
  return { sessionId: sessionId || undefined, url: url || undefined, seenAt };
}

function collectThreadItems(thread: ChatThread | null): CanvasItem[] {
  if (!thread) return [];
  const items: CanvasItem[] = [];
  const seen = new Set<string>();

  function add(item: CanvasItem | null) {
    if (!item || seen.has(item.id)) return;
    seen.add(item.id);
    items.push(item);
  }

  for (const message of thread.messages) {
    if (message.role === "user") continue;

    const envelopes: NativeBlockEnvelope[] = [
      ...(message.turn?.blocks || []),
      ...liveEventsToBlocks(message.live_events || []),
    ];
    envelopes.forEach((env, index) => {
      const block = unwrapBlock(env);
      add(
        itemFromToolLike(
          block as ToolLike,
          `block:${message.id}:${String(block.call_id || index)}`,
          message.ts,
        ),
      );
    });

    (message.turn?.tool_trace || []).forEach((tool, index) =>
      add(itemFromToolLike(tool, `tool:${message.id}:${index}`, message.ts)),
    );
    (message.turn?.actions || []).forEach((action, index) =>
      add(itemFromToolLike(action as ToolLike, `action:${message.id}:${index}`, message.ts)),
    );
    (message.turn?.attachments || []).forEach((attachment, index) =>
      add(itemFromAttachment(attachment, message, index)),
    );
  }

  return items.sort((a, b) => b.seenAt - a.seenAt);
}

function collectBrowserSessionRefs(thread: ChatThread | null): BrowserSessionRef[] {
  if (!thread) return [];
  const refs = new Map<string, BrowserSessionRef>();

  function add(ref: BrowserSessionRef | null) {
    if (!ref) return;
    const key = ref.sessionId ? `session:${ref.sessionId}` : `url:${ref.url}`;
    const current = refs.get(key);
    if (!current || ref.seenAt > current.seenAt) {
      refs.set(key, ref);
    }
  }

  for (const message of thread.messages) {
    if (message.role !== "assistant") continue;
    const seenAt = message.ts;
    const envelopes: NativeBlockEnvelope[] = [
      ...(message.turn?.blocks || []),
      ...liveEventsToBlocks(message.live_events || []),
    ];
    envelopes.forEach((env) =>
      add(browserSessionRefFromTool(unwrapBlock(env) as ToolLike, seenAt)),
    );
    (message.turn?.tool_trace || []).forEach((tool) =>
      add(browserSessionRefFromTool(tool, seenAt)),
    );
    (message.turn?.actions || []).forEach((action) =>
      add(browserSessionRefFromTool(action as ToolLike, seenAt)),
    );
  }

  return Array.from(refs.values()).sort((a, b) => b.seenAt - a.seenAt);
}

export function hasWorkspaceCanvas(thread: ChatThread | null | undefined): boolean {
  if (!thread) return false;
  return (
    collectThreadItems(thread).length > 0 ||
    collectBrowserSessionRefs(thread).length > 0 ||
    hasAgentVisuals(thread)
  );
}

function itemFromBrowserRecord(
  record: BrowserSessionRecord | null,
  seenAtFloor = 0,
): CanvasItem | null {
  if (!record?.session_id) return null;
  const last = record.last || {};
  const screenshot = latestScreenshot(record);
  const url = record.current_url || screenshot?.url || last.url || "";
  const updated = Math.max(
    Date.parse(record.updated_at || "") || 0,
    Date.parse(screenshot?.ts || "") || 0,
    seenAtFloor,
    Date.now(),
  );
  const html = stringValue(last.html);
  const text =
    stringValue(last.markdown) ||
    stringValue(last.text) ||
    stringValue(last.error) ||
    stringValue(last.detail) ||
    "";
  return {
    id: `browser:${record.session_id}`,
    kind: "browser",
    title: url ? filenameFromPath(url) : "browser",
    subtitle: url || undefined,
    html,
    body: text,
    imageSrc: screenshot?.data_uri,
    url,
    sessionId: record.session_id,
    seenAt: updated,
  };
}

function latestScreenshot(record: BrowserSessionRecord | null): BrowserSessionScreenshot | undefined {
  if (!record) return undefined;
  if (record.last_screenshot?.data_uri) return record.last_screenshot;
  const shots = record.screenshots || [];
  for (let i = shots.length - 1; i >= 0; i -= 1) {
    if (shots[i]?.data_uri) return shots[i];
  }
  return record.last_screenshot || shots[shots.length - 1];
}

function comparableUrl(value?: string): string {
  const raw = String(value || "").trim();
  if (!raw) return "";
  try {
    const url = new URL(raw);
    url.hash = "";
    return url.toString().replace(/\/$/, "");
  } catch {
    return raw.replace(/\/$/, "");
  }
}

function samePageUrl(a?: string, b?: string): boolean {
  const left = comparableUrl(a);
  const right = comparableUrl(b);
  if (!left || !right) return false;
  if (left === right) return true;
  try {
    const leftUrl = new URL(left);
    const rightUrl = new URL(right);
    return leftUrl.origin === rightUrl.origin && leftUrl.pathname === rightUrl.pathname;
  } catch {
    return false;
  }
}

async function resolveBrowserRecord(ref: BrowserSessionRef): Promise<BrowserSessionRecord> {
  if (ref.sessionId) return clientApi.browserSessionGet(ref.sessionId);
  const list = await clientApi.browserSessionList();
  const sessions = Array.isArray(list.sessions) ? list.sessions : [];
  const matched = sessions.find((session) => samePageUrl(session.current_url, ref.url));
  if (!matched?.session_id) {
    throw new Error("browser session not found for current thread");
  }
  return clientApi.browserSessionGet(matched.session_id);
}

function isInteractiveBrowserRecord(record: BrowserSessionRecord): boolean {
  const engine = String(record.engine || "").toLowerCase();
  return Boolean(record.cdp) || engine === "camofox" || engine === "cloakbrowser";
}

function mergeScreenshot(
  record: BrowserSessionRecord,
  shot: BrowserSessionScreenshotResponse,
): BrowserSessionRecord {
  if (!shot.data_uri) return record;
  const now = new Date().toISOString();
  const nextShot: BrowserSessionScreenshot = {
    ts: now,
    url: shot.url || record.current_url || "",
    ok: shot.ok !== false,
    path: shot.path,
    bytes: shot.bytes,
    elapsed_ms: shot.elapsed_ms,
    fetch_method: shot.fetch_method,
    error: shot.error || shot.detail || shot.data_uri_error,
    stderr_tail: shot.stderr_tail,
    data_uri: shot.data_uri,
  };
  const shots = [...(record.screenshots || []), nextShot].slice(-12);
  return {
    ...record,
    current_url: nextShot.url || record.current_url,
    updated_at: now,
    screenshots: shots,
    last_screenshot: nextShot,
  };
}

function shouldRefreshFrame(record: BrowserSessionRecord, lastAttemptAt: number): boolean {
  const now = Date.now();
  if (now - lastAttemptAt < 6000) return false;
  const shot = latestScreenshot(record);
  if (!shot?.data_uri) return true;
  const shotTs = Date.parse(shot.ts || "") || 0;
  const recordTs = Date.parse(record.updated_at || "") || 0;
  return Boolean(recordTs && shotTs && recordTs - shotTs > 2000);
}

async function captureBrowserFrame(
  record: BrowserSessionRecord,
): Promise<BrowserSessionScreenshotResponse> {
  const body = { session_id: record.session_id, full_page: false, timeout_s: 12 };
  return isInteractiveBrowserRecord(record)
    ? clientApi.browserSessionCdpScreenshot(body)
    : clientApi.browserSessionScreenshot(body);
}

function iconForKind(kind: CanvasKind) {
  if (kind === "charts") return ChartIcon;
  if (kind === "browser" || kind === "web") return GlobeIcon;
  if (kind === "image") return ImageIcon;
  if (kind === "text") return MessagesIcon;
  return FileIcon;
}

function kindLabelKey(kind: CanvasKind) {
  const key: Record<CanvasKind, "canvasKindBrowser" | "canvasKindFile" | "canvasKindHtml" | "canvasKindWeb" | "canvasKindImage" | "canvasKindText" | "canvasKindCharts"> = {
    browser: "canvasKindBrowser",
    file: "canvasKindFile",
    html: "canvasKindHtml",
    web: "canvasKindWeb",
    image: "canvasKindImage",
    text: "canvasKindText",
    charts: "canvasKindCharts",
  };
  return key[kind];
}

function CodeView({ item }: { item: CanvasItem }) {
  const body = item.body || "";
  return (
    <div className="flex h-full min-h-0 flex-col overflow-hidden bg-ink-950">
      <div className="flex items-center justify-between gap-2 border-b border-brand-500/10 px-3 py-2">
        <span className="truncate font-mono text-[11px] text-ink-400">
          {item.language || item.mimeType || "text"}
        </span>
        <CopyButton text={body} />
      </div>
      <pre className="min-h-0 flex-1 overflow-auto p-4 text-[12px] leading-relaxed text-ink-100 whitespace-pre-wrap break-words">
        {body}
      </pre>
    </div>
  );
}

function HtmlFrame({ item, title }: { item: CanvasItem; title: string }) {
  const srcDoc = item.html ? withBaseUrl(item.html, item.url) : "";
  if (!srcDoc) {
    return item.body ? <TextView item={item} /> : <EmptyCanvas title={title} />;
  }
  return (
    <iframe
      title={title}
      srcDoc={srcDoc}
      className="h-full w-full border-0 bg-white"
      sandbox="allow-forms allow-popups allow-scripts"
    />
  );
}

function ImageView({ item }: { item: CanvasItem }) {
  if (!item.imageSrc) return <EmptyCanvas title={item.title} />;
  return (
    <div className="flex h-full min-h-0 items-center justify-center overflow-auto bg-[#050711] p-3">
      <img
        src={item.imageSrc}
        alt={item.title}
        className="max-h-full max-w-full object-contain"
      />
    </div>
  );
}

function TextView({ item }: { item: CanvasItem }) {
  const body = item.body || "";
  if (!body) return <EmptyCanvas title={item.title} />;
  return (
    <div className="h-full min-h-0 overflow-auto bg-ink-950/70 p-4">
      <Markdown className="text-[13px] leading-relaxed">{body}</Markdown>
    </div>
  );
}

function BrowserSessionFrame({ item }: { item: CanvasItem }) {
  if (!item.sessionId) return null;
  const params = new URLSearchParams({ session_id: item.sessionId });
  if (item.url) params.set("url", item.url);
  return (
    <iframe
      title={item.title}
      src={`/browser-session/embed?${params.toString()}`}
      className="h-full w-full border-0 bg-[#050711]"
      sandbox="allow-same-origin allow-scripts allow-forms allow-popups"
    />
  );
}

function BrowserView({ item }: { item: CanvasItem }) {
  if (item.sessionId) return <BrowserSessionFrame item={item} />;
  if (item.imageSrc) return <ImageView item={item} />;
  if (item.html) return <HtmlFrame item={item} title={item.title} />;
  return <TextView item={item} />;
}

function CanvasBody({ item }: { item: CanvasItem }) {
  if (item.kind === "charts") {
    return <AgentChartPanel thread={item.thread || null} embedded />;
  }
  if (item.kind === "browser") return <BrowserView item={item} />;
  if (item.kind === "image") return <ImageView item={item} />;
  if (item.kind === "html") return <HtmlFrame item={item} title={item.title} />;
  if (item.kind === "web") {
    return item.html ? <HtmlFrame item={item} title={item.title} /> : <TextView item={item} />;
  }
  return <CodeView item={item} />;
}

function EmptyCanvas({ title }: { title?: string }) {
  const t = useTranslations("chat");
  return (
    <div className="flex h-full min-h-0 items-center justify-center bg-ink-950/70 px-8 text-center">
      <div>
        <div className="mx-auto mb-3 flex h-10 w-10 items-center justify-center rounded-md border border-brand-500/20 bg-brand-500/[0.08] text-brand-200">
          <MessagesIcon size={18} />
        </div>
        <div className="text-sm font-medium text-ink-100">
          {title || t("canvasEmptyTitle")}
        </div>
        <div className="mt-1 max-w-xs text-xs leading-relaxed text-ink-500">
          {t("canvasEmptyBody")}
        </div>
      </div>
    </div>
  );
}

export function WorkspaceCanvas({
  thread,
  open,
  onToggle,
}: {
  thread: ChatThread | null;
  open: boolean;
  onToggle: () => void;
}) {
  const t = useTranslations("chat");
  const [browserRecord, setBrowserRecord] = useState<BrowserSessionRecord | null>(null);
  const [error, setError] = useState("");
  const [activeItemId, setActiveItemId] = useState("");
  const browserRefs = useMemo(() => collectBrowserSessionRefs(thread), [thread]);

  useEffect(() => {
    if (!browserRefs.length) {
      setBrowserRecord(null);
      setError("");
      return;
    }
    let cancelled = false;
    let refreshing = false;
    let lastCaptureAttemptAt = 0;

    async function refresh() {
      if (refreshing) return;
      refreshing = true;
      try {
        const nextRecord = await resolveBrowserRecord(browserRefs[0]);
        if (cancelled) return;
        setBrowserRecord(nextRecord);
        setError("");
        let nextError = "";
        if (open && shouldRefreshFrame(nextRecord, lastCaptureAttemptAt)) {
          lastCaptureAttemptAt = Date.now();
          try {
            const shot = await captureBrowserFrame(nextRecord);
            if (shot.ok && shot.data_uri) {
              const merged = mergeScreenshot(nextRecord, shot);
              if (!cancelled) setBrowserRecord(merged);
            } else if (!latestScreenshot(nextRecord)?.data_uri) {
              nextError = shot.error || shot.detail || "browser screenshot unavailable";
            }
          } catch (captureError) {
            if (!latestScreenshot(nextRecord)?.data_uri) {
              nextError =
                captureError instanceof Error ? captureError.message : String(captureError);
            }
          }
        }
        if (cancelled) return;
        if (nextError) setError(nextError);
      } catch (e) {
        if (cancelled) return;
        setError(e instanceof Error ? e.message : String(e));
      } finally {
        refreshing = false;
      }
    }

    void refresh();
    const id = window.setInterval(refresh, 2500);
    return () => {
      cancelled = true;
      window.clearInterval(id);
    };
  }, [browserRefs, open]);

  const browserSeenAt = browserRefs[0]?.seenAt || 0;
  const browserItem = useMemo(
    () => itemFromBrowserRecord(browserRecord, browserSeenAt),
    [browserRecord, browserSeenAt],
  );
  const threadItems = useMemo(() => collectThreadItems(thread), [thread]);
  const chartItem = useMemo<CanvasItem | null>(() => {
    if (!thread || !hasAgentVisuals(thread)) return null;
    const seenAt = Math.max(...thread.messages.map((message) => message.ts || 0), 0);
    return {
      id: `charts:${thread.id}`,
      kind: "charts",
      title: thread.title || t("canvasKindCharts"),
      subtitle: t("canvasChartsSubtitle"),
      thread,
      seenAt,
    };
  }, [thread, t]);
  const items = useMemo(() => {
    const next = [
      ...(chartItem ? [chartItem] : []),
      ...(browserItem ? [browserItem] : []),
      ...threadItems,
    ];
    return next.sort((a, b) => b.seenAt - a.seenAt);
  }, [browserItem, chartItem, threadItems]);

  useEffect(() => {
    if (!items.length) {
      setActiveItemId("");
      return;
    }
    const activeStillExists = items.some((item) => item.id === activeItemId);
    const activeKind = items.find((item) => item.id === activeItemId)?.kind;
    if (
      browserItem &&
      activeItemId !== browserItem.id &&
      (!activeStillExists || activeKind === "web")
    ) {
      setActiveItemId(browserItem.id);
      return;
    }
    if (!activeStillExists) {
      setActiveItemId(items[0].id);
    }
  }, [activeItemId, browserItem, items]);

  const activeItem = items.find((item) => item.id === activeItemId) || items[0] || null;
  const ActiveIcon = activeItem ? iconForKind(activeItem.kind) : MessagesIcon;

  return (
    <aside
      className={`hidden min-h-0 shrink-0 border-l border-brand-500/15 bg-ink-950/45 xl:flex ${
        open ? "w-[520px]" : "w-11"
      } flex-col transition-[width] duration-200`}
      aria-label={t("canvasTitle")}
    >
      {open ? (
        <>
          <div className="border-b border-brand-500/15 px-3 py-2.5">
            <div className="flex items-start justify-between gap-3">
              <div className="min-w-0">
                <div className="flex items-center gap-2 text-[12px] font-medium text-brand-300">
                  <ActiveIcon size={14} />
                  <span>{t("canvasTitle")}</span>
                </div>
                <div className="mt-1 truncate text-[11px] text-ink-400">
                  {activeItem?.subtitle ||
                    browserRecord?.current_url ||
                    thread?.title ||
                    t("canvasEmptyTitle")}
                </div>
              </div>
              <button
                type="button"
                onClick={onToggle}
                className="inline-flex h-7 w-7 shrink-0 items-center justify-center rounded-md border border-brand-500/20 text-ink-300 hover:border-brand-500/45 hover:text-white"
                aria-label={t("canvasHide")}
                title={t("canvasHide")}
              >
                <ChevronRightIcon size={14} />
              </button>
            </div>

            {items.length ? (
              <div className="mt-2 flex gap-1 overflow-x-auto pb-0.5">
                {items.slice(0, 8).map((item) => {
                  const Icon = iconForKind(item.kind);
                  const selected = item.id === activeItem?.id;
                  return (
                    <button
                      key={item.id}
                      type="button"
                      onClick={() => setActiveItemId(item.id)}
                      className={`inline-flex max-w-[11rem] shrink-0 items-center gap-1.5 rounded-md border px-2 py-1 text-[10px] transition-colors ${
                        selected
                          ? "border-brand-500/45 bg-brand-500/[0.14] text-brand-100"
                          : "border-brand-500/15 bg-ink-900/50 text-ink-400 hover:border-brand-500/35 hover:text-ink-100"
                      }`}
                      title={item.title}
                    >
                      <Icon size={12} />
                      <span className="font-mono">
                        {t(kindLabelKey(item.kind))}
                      </span>
                      <span className="truncate">{item.title}</span>
                    </button>
                  );
                })}
              </div>
            ) : null}

            {error ? (
              <div className="mt-2 rounded border border-[#ef4560]/25 bg-[#ef4560]/10 px-2 py-1 text-[11px] text-[#ff9aa8]">
                {error}
              </div>
            ) : null}
          </div>

          <div className="min-h-0 flex-1 overflow-hidden">
            {activeItem ? <CanvasBody item={activeItem} /> : <EmptyCanvas />}
          </div>
        </>
      ) : (
        <button
          type="button"
          onClick={onToggle}
          className="flex h-full w-full items-center justify-center text-ink-400 hover:bg-brand-500/[0.06] hover:text-brand-100"
          aria-label={t("canvasShow")}
          title={t("canvasShow")}
        >
          <div className="flex flex-col items-center gap-2">
            <ChevronLeftIcon size={14} />
            <span className="[writing-mode:vertical-rl] text-[11px] font-medium">
              {t("canvasCollapsed")}
            </span>
          </div>
        </button>
      )}
    </aside>
  );
}
