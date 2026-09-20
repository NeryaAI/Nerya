"use client";

import { useEffect, useRef, useState } from "react";
import { useLocale } from "next-intl";
import { Card, Pill } from "./Page";
import { BrowserAgentAccess } from "./BrowserAgentAccess";
import { clientApi } from "../lib/clientApi";
import { confirm, toast } from "../lib/dialogs";
import type { DesktopBrowserResponse, DesktopExtension } from "../lib/browserDesktopTypes";

export function ManagedBrowserPanel() {
  const zh = useLocale().startsWith("zh");
  const text = (cn: string, en: string) => zh ? cn : en;
  const [profile, setProfile] = useState("work");
  const [draftProfile, setDraftProfile] = useState("work");
  const [url, setUrl] = useState("https://example.com");
  const [state, setState] = useState<DesktopBrowserResponse>({ ok: true, running: false, tabs: [] });
  const [busy, setBusy] = useState(false);
  const [loading, setLoading] = useState(true);
  const inflight = useRef(false);
  const [error, setError] = useState("");
  const [image, setImage] = useState("");
  const [input, setInput] = useState("");
  const [packagePath, setPackagePath] = useState("");
  const [review, setReview] = useState<DesktopExtension | null>(null);
  const [allowUi, setAllowUi] = useState(false);
  const [livePreview, setLivePreview] = useState(true);
  const epoch = useRef(0);
  const pollInFlight = useRef(false);
  const locked = busy || loading;
  const interactive = !!state.running && !state.paused && !state.agent_access?.occupied && !locked;

  useEffect(() => {
    if (!state.running) return;
    let active = true;
    let timer: ReturnType<typeof setTimeout>;
    const poll = async () => {
      if (!active) return;
      if (!document.hidden && !inflight.current && !pollInFlight.current) {
        pollInFlight.current = true;
        const version = epoch.current;
        try {
          const result = await clientApi.browserDesktop({ profile_id: profile, operation: livePreview ? "preview" : "status" });
          if (active && version === epoch.current && !result.ok) setImage("");
          if (active && version === epoch.current && result.ok) {
            const { image: nextImage, ...metadata } = result;
            setState((old) => ({ ...old, ...metadata }));
            if (result.paused || !result.running) setImage("");
            else if (livePreview) setImage(nextImage || "");
          }
        } catch { if (active && version === epoch.current) setImage(""); }
        finally { pollInFlight.current = false; }
      }
      if (active) timer = setTimeout(poll, livePreview ? 900 : 2000);
    };
    timer = setTimeout(poll, 900);
    const hidden = () => { if (document.hidden) { epoch.current++; setImage(""); } };
    document.addEventListener("visibilitychange", hidden);
    return () => { active = false; clearTimeout(timer); document.removeEventListener("visibilitychange", hidden); };
  }, [profile, state.running, livePreview]);

  useEffect(() => {
    let current = true;
    setLoading(true);
    epoch.current++;
    setState({ ok: true, running: false, paused: false, tabs: [] });
    setImage("");
    setReview(null);
    setError("");
    clientApi.browserDesktopStatus(profile).then((result) => {
      if (!current) return;
      if (!result.ok) throw new Error(result.error || "browser_status_failed");
      setState(result);
    }).catch((err: unknown) => {
      if (current) setError(err instanceof Error ? err.message : "browser_status_failed");
    }).finally(() => { if (current) setLoading(false); });
    return () => { current = false; };
  }, [profile]);

  async function run(body: Record<string, unknown>, capture = false) {
    if (inflight.current || loading) return;
    inflight.current = true;
    setBusy(true);
    epoch.current++;
    setError("");
    if (body.command === "handoff" || body.operation === "close" || body.operation === "agent_revoke") setImage("");
    try {
      let result = await clientApi.browserDesktop({ ...body, profile_id: profile });
      if (!result.ok) throw new Error([result.error, result.hint].filter(Boolean).join(" · "));
      const { image: _firstImage, ...metadata } = result;
      setState((old) => ({ ...old, ...metadata }));
      if (capture && body.command !== "screenshot" && result.running && !result.paused && result.tabs?.length) {
        result = await clientApi.browserDesktop({ profile_id: profile, operation: "command", command: "screenshot" });
        if (!result.ok) throw new Error(result.error || "browser_capture_failed");
        const { image: _nextImage, ...nextMetadata } = result;
        setState((old) => ({ ...old, ...nextMetadata }));
      }
      setImage(result.paused ? "" : result.image || "");
      return result;
    } catch (err) {
      const message = err instanceof Error ? err.message : "browser_request_failed";
      setError(message);
      setImage("");
      toast({ tone: "error", message });
      // Never retry a timed-out click, navigation, or extension operation.
    } finally {
      inflight.current = false;
      setBusy(false);
    }
  }

  const command = (name: string, payload: Record<string, unknown> = {}, capture = true) =>
    run({ operation: "command", command: name, payload }, capture);

  async function inspect() {
    setReview(null);
    setAllowUi(false);
    const result = await run({ operation: "review_extension", path: packagePath });
    if (result?.review) setReview(result.review);
  }

  async function saveExtensions(extensions: DesktopExtension[]) {
    const accepted = await confirm({
      title: text("确认扩展权限", "Review extension access"),
      message: text("扩展代码可以访问其获准的网站。仅添加可信来源；钱包和密码管理器不要开放 UI 控制。更新文件后必须重新审查。", "Extensions can access their permitted sites. Use trusted packages; keep wallets and password managers human-only. Changed files require a new review."),
      okLabel: text("保存配置", "Save configuration"),
      tone: "warning",
    });
    if (!accepted) return;
    const result = await run({ operation: "configure", extensions });
    if (result?.ok) { setReview(null); setPackagePath(""); }
  }

  return (
    <div className="space-y-4" data-testid="managed-browser-panel">
      <Card title={text("工作浏览器", "Work browser")}
        description={text("独立 Chromium · 持久化配置 · 不共用个人浏览器目录", "Dedicated Chromium · Persistent profile · Separate from your personal browser")}
        actions={<Pill tone={state.paused ? "warn" : state.running ? "ok" : "neutral"}>{state.paused ? text("人工接管中", "Human control") : state.running ? text("已打开", "Open") : text("已关闭", "Closed")}</Pill>}>
        <div className="space-y-3">
          <div className="flex flex-wrap items-end gap-2">
            <label className="space-y-1 text-xs text-ink-400">
              <span className="block">{text("配置名称", "Profile")}</span>
              <input className="input w-40" aria-label={text("配置名称", "Profile")}
                value={draftProfile} disabled={locked || !!state.running} maxLength={48}
                onChange={(e) => setDraftProfile(e.target.value)} />
            </label>
            <button type="button" className="btn btn-ghost" disabled={locked || !!state.running || draftProfile === profile}
              onClick={() => {
                if (!/^[a-z0-9][a-z0-9_-]{0,47}$/.test(draftProfile)) { setError(text("使用小写字母、数字、连字符或下划线。", "Use lowercase letters, numbers, hyphens or underscores.")); return; }
                setProfile(draftProfile);
              }}>{text("切换配置", "Switch profile")}</button>
            {!state.running ? <button type="button" className="btn btn-primary" disabled={locked}
              onClick={() => void run({ operation: "open", url }, true)}>{text("打开浏览器", "Open browser")}</button> : <>
              <button type="button" className="btn btn-ghost" disabled={locked} onClick={() => void command("handoff", {}, false)}>{text("在原生窗口接管", "Take over in native window")}</button>
              <button type="button" className="btn btn-ghost" disabled={locked} onClick={() => void run({ operation: "close" })}>{text("关闭并保存", "Close and save")}</button>
            </>}
            <button type="button" className="btn btn-ghost" disabled={locked} onClick={() => void run({ operation: "status" })}>{text("检查状态", "Check status")}</button>
            <label className="flex items-center gap-2 text-xs text-ink-400"><input type="checkbox" checked={livePreview} onChange={(e) => { epoch.current++; setLivePreview(e.target.checked); setImage(""); }} />{text("实时预览", "Live preview")}</label>
          </div>
          <form className="flex gap-2" onSubmit={(e) => { e.preventDefault(); if (interactive) void command("navigate", { url }); }}>
            <input className="input min-w-0 flex-1" aria-label={text("网址", "URL")} value={url} onChange={(e) => setUrl(e.target.value)} placeholder="https://" spellCheck={false} />
            <button type="submit" className="btn btn-primary" disabled={!interactive}>{text("前往", "Go")}</button>
          </form>
          {state.running && <div className="flex flex-wrap gap-1" aria-label={text("浏览器操作", "Browser controls")}>
            {([["back", "后退", "Back"], ["forward", "前进", "Forward"], ["reload", "刷新页面", "Reload"], ["new_tab", "新标签", "New tab"], ["close_tab", "关闭标签", "Close tab"]] as const).map(([name, cn, en]) =>
              <button key={name} type="button" className="btn btn-ghost" disabled={!interactive} onClick={() => void command(name)}>{text(cn, en)}</button>)}
            <button type="button" className="btn btn-ghost" disabled={!interactive} onClick={() => void command("screenshot")}>{text("更新预览", "Update preview")}</button>
          </div>}
          {!!state.tabs?.length && <div className="flex gap-2 overflow-x-auto border-b border-brand-500/10 pb-2" aria-label={text("标签页", "Tabs")}>
            {state.tabs.map((tab) => <button key={tab.id} type="button" className={`btn shrink-0 ${tab.selected ? "btn-primary" : "btn-ghost"}`} disabled={locked || (!!state.agent_access?.occupied && !state.paused)}
              aria-pressed={tab.selected} onClick={() => { if (!state.agent_access?.occupied || state.paused) void command("select_tab", { tab_id: tab.id }); }}>
              <span className="max-w-64 truncate">{tab.protected ? "🔒 " : ""}{tab.url || "about:blank"}</span>
            </button>)}
          </div>}
          {state.paused ? <div className="rounded-lg border border-warn/30 p-5 text-sm" role="status">
            <p>{text("预览和输入已暂停。请在原生窗口完成钱包解锁、签名或身份验证，并关闭受保护的扩展页面后恢复。", "Preview and input are paused. Finish unlocking, signing or verification in the native window, then close protected extension pages before resuming.")}</p>
            <button type="button" className="btn btn-ghost mt-3" disabled={locked} onClick={async () => {
              if (await confirm({ message: text("已完成敏感操作，恢复页面预览和控制？", "Sensitive work is finished. Resume preview and control?") })) void command("resume");
            }}>{text("恢复浏览器控制", "Resume browser control")}</button>
          </div> : image ? <button type="button" className="block w-full overflow-hidden rounded-lg border border-brand-500/10 text-left" disabled={!interactive}
            aria-label={text("点击浏览器预览以操作页面；键盘激活发送 Enter", "Click the browser preview to interact; keyboard activation sends Enter")}
            onClick={(e) => {
              if (e.detail === 0) { void command("press", { key: "Enter" }); return; }
              const rect = e.currentTarget.getBoundingClientRect();
              void command("click", { x: (e.clientX - rect.left) / rect.width * 1280, y: (e.clientY - rect.top) / rect.height * 800 });
            }}>
            {/* In-memory authenticated viewport: no Next image optimizer or disk cache. */}
            {/* eslint-disable-next-line @next/next/no-img-element */}
            <img src={image} alt={text("工作浏览器交互预览", "Work browser interactive preview")} className="block h-auto w-full" />
          </button> : <div className="rounded-lg border border-dashed border-brand-500/20 px-5 py-10 text-center text-sm text-ink-400">
            {text("打开浏览器或更新预览。预览不会自动录制；需要输入敏感信息时请使用原生接管。", "Open the browser or update the preview. It is not automatically recorded. Use native takeover for sensitive input.")}
          </div>}
          {state.running && !state.paused && <div className="flex flex-wrap gap-2">
            <input className="input min-w-0 flex-1" aria-label={text("输入到当前焦点", "Text for focused element")} value={input} onChange={(e) => setInput(e.target.value)} autoComplete="off" placeholder={text("普通文本；敏感信息使用原生窗口", "Ordinary text; use native window for sensitive input")} />
            <button type="button" className="btn btn-ghost" disabled={!interactive || !input} onClick={() => { const value = input; setInput(""); void command("type", { text: value }); }}>{text("输入", "Type")}</button>
            {(["Enter", "Tab", "Escape"] as const).map((key) => <button key={key} type="button" className="btn btn-ghost" disabled={!interactive} onClick={() => void command("press", { key })}>{key}</button>)}
            <button type="button" className="btn btn-ghost" disabled={!interactive} onClick={() => void command("scroll", { dy: -500 })}>{text("向上滚动", "Scroll up")}</button>
            <button type="button" className="btn btn-ghost" disabled={!interactive} onClick={() => void command("scroll", { dy: 500 })}>{text("向下滚动", "Scroll down")}</button>
          </div>}
          {error && <p className="break-words text-sm text-danger" role="alert">{error}</p>}
          <p className="text-xs text-ink-400" role="status">{locked ? text("正在处理…", "Working…") : text("Agent 使用同一浏览器：一次站点授权后连续操作；人工接管随时暂停。", "The Agent uses this browser: delegate sites once for continuous work; native takeover pauses automation.")}</p>
        </div>
      </Card>
      <BrowserAgentAccess state={state} busy={locked} onAction={run} />
      <Card title={text("扩展与身份", "Extensions and identity")} description={text("先审查权限，再加载扩展；身份验证留在受信任的原生界面。", "Review permissions before loading extensions; keep verification in trusted native UI.")}>
        <div className="space-y-4 text-sm">
          <p className="text-ink-400">{text("已支持本地解压的 Manifest V3 扩展，不等同于 Chrome 商店安装。修改扩展配置前请关闭浏览器。", "Supports local unpacked Manifest V3 packages, not Chrome Web Store installation. Close the browser before changing extensions.")}</p>
          {(state.config?.extensions || []).map((ext) => <div key={ext.digest} className="flex flex-wrap items-center justify-between gap-2 border-b border-brand-500/10 pb-3">
            <div><p className="font-medium">{ext.name} <span className="text-ink-400">{ext.version}</span></p><p className="text-xs text-ink-400">{ext.control_ui ? text("已授权普通扩展 UI 控制", "Ordinary extension UI control enabled") : text("受保护 · 原生窗口操作", "Protected · Native control")}</p></div>
            <div className="flex gap-2">
              <button type="button" className="btn btn-ghost" disabled={!interactive || !ext.extension_id} onClick={() => void command("extension_open", { extension_id: ext.extension_id })}>{text("打开扩展页面", "Open extension page")}</button>
              <button type="button" className="btn btn-ghost" disabled={locked || !!state.running} onClick={() => void saveExtensions((state.config?.extensions || []).filter((item) => item.digest !== ext.digest))}>{text("停用", "Disable")}</button>
            </div>
          </div>)}
          <details className="rounded-lg border border-brand-500/10 p-3">
            <summary className="cursor-pointer font-medium">{text("加载本地扩展", "Load a local extension")}</summary>
            <div className="mt-3 space-y-3">
              <label className="block text-xs text-ink-400">{text("运行 Nerya 后端的电脑上的扩展目录（绝对路径）", "Absolute package path on the computer running the Nerya backend")}
                <input className="input mt-1 w-full" value={packagePath} disabled={locked || !!state.running} onChange={(e) => { setPackagePath(e.target.value); setReview(null); }} placeholder="/absolute/path/to/unpacked-extension" />
              </label>
              <button type="button" className="btn btn-ghost" disabled={locked || !!state.running || !packagePath} onClick={() => void inspect()}>{text("审查包和权限", "Review package and permissions")}</button>
              {review && <div className="space-y-2 rounded-md border border-brand-500/10 p-3">
                <p className="font-medium">{review.name} · {review.version}</p>
                <p className="break-all text-xs text-ink-400">SHA-256: {review.digest}</p>
                <p className="break-words text-xs">{review.permissions.join(" · ") || text("未声明额外权限", "No additional permissions declared")}</p>
                <label className="flex items-start gap-2 text-xs"><input type="checkbox" checked={allowUi} disabled={!review.extension_id} onChange={(e) => setAllowUi(e.target.checked)} />{text("仅对普通扩展开放 UI 控制。钱包、密码管理器保持关闭。无稳定扩展 ID 时仅支持原生操作。", "Enable UI control only for ordinary extensions. Leave OFF for wallets/password managers. Packages without a stable ID use native control only.")}</label>
                <button type="button" className="btn btn-primary" disabled={locked || !!state.running} onClick={() => void saveExtensions([...(state.config?.extensions || []).filter((e) => e.digest !== review.digest), { ...review, control_ui: allowUi }])}>{text("确认并保存", "Confirm and save")}</button>
              </div>}
            </div>
          </details>
          <div className="space-y-2 border-t border-brand-500/10 pt-3 text-xs text-ink-400">
            <p>{text("登录态：在此独立浏览器中登录后由 Chromium 持久化；Chrome / Edge / Firefox / Safari 的凭据导入尚未实现。", "Sign-in persists in this dedicated Chromium profile. Credential import from Chrome / Edge / Firefox / Safari is not implemented.")}</p>
            <p>{text("钱包：可加载可信钱包扩展并在原生窗口配置；现有本地钱包的 provider 注入尚未实现，不导入私钥或助记词。", "Trusted wallet extensions may be loaded and configured natively. Existing local-wallet provider injection is not implemented; no key or seed import.")}</p>
            <p>{text("Passkey：保留浏览器和系统认证器流程，未实现 Nerya 自有存储；设备级创建、重启后认证尚待验收。", "Passkeys use browser/system authenticator flows, not a Nerya vault. Creation and authentication after restart still need device-level validation.")}</p>
          </div>
        </div>
      </Card>
    </div>
  );
}
