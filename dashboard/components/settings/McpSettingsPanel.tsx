"use client";

import { useCallback, useEffect, useState } from "react";
import { useLocale } from "next-intl";
import { Advanced, ErrorBanner, Pill } from "../Page";
import { SwitchControl } from "../SwitchControl";
import { Field, Row, SettingsGroup } from "./SettingsFields";
import { confirm } from "../../lib/dialogs";
import { mcpRequest, type McpStatus } from "../../lib/mcpSettings";
import SkillManagementPanel from "./SkillManagementPanel";

const copy = {
  en: {
    title: "External agents & MCP", intro: "Publish Nerya's tools through the same public address as your dashboard. OAuth sign-in uses your administrator password.",
    enabled: "Enable MCP", enabledHint: "Off by default. Disabling immediately blocks new MCP requests.",
    ready: "Enabled", off: "Disabled", loading: "Loading MCP settings…", retry: "Refresh",
    password: "Administrator password", configured: "Configured", missing: "Not configured",
    passwordHint: "Set an administrator password in Access settings before enabling MCP.",
    sdkHint: "The running backend needs the MCP extra. From the product directory, run: uv sync --extra mcp",
    url: "Public origin", urlHint: "Leave blank to follow the active Nerya public tunnel. A custom origin must use HTTPS and have no path.",
    endpoint: "MCP endpoint", copy: "Copy endpoint", copied: "Copied", copiedError: "Copy failed; select the endpoint text instead.",
    auth: "OAuth 2 · authorization code + PKCE", scope: "External write permissions",
    proposals: "Strategy & Skill proposals", proposalsHint: "Allow external agents to propose Skill and strategy changes. Approval is still required before applying them.",
    roles: "Agent & configuration management", rolesHint: "Allow the existing role-management tools and configuration proposals. Does not grant shell or live-trading access.",
    advanced: "Tool allow / deny lists", restrict: "Restrict to specific tools", allow: "Allowed public tool names", deny: "Denied public tool names",
    allowHint: "One name per line. With restriction enabled, an empty list exposes no tools.",
    save: "Save MCP settings", saving: "Saving…", saved: "Settings saved. Previous authorizations were revoked; external clients must sign in again.",
    unsaved: "Unsaved changes", discard: "Discard unsaved changes?", discardHint: "Refresh will replace the current form with saved settings.",
    revoke: "Revoke all external authorizations", revokeHint: "All connected clients must sign in again. The administrator password is unchanged.", revoked: "External authorizations revoked.",
    tunnel: "OpenAI Secure MCP Tunnel", tunnelHint: "Use OpenAI's official tunnel-client for an outbound connection. The runtime API key authenticates the tunnel, not the MCP user.",
    tunnelEnable: "Enable OpenAI Tunnel", tunnelEnableHint: "Restore this connection when Nerya starts. Save, then connect.",
    id: "Tunnel ID", key: "Runtime API key", keyHint: "Stored in the encrypted Vault. Leave blank to keep the saved key. Never shown again.",
    keySaved: "Saved in Vault", keyNone: "No saved key", forget: "Disconnect saved key", forgetHint: "The saved reference will be disconnected on Save; it is not exported or displayed.",
    install: "Install the official client", installHint: "The tunnel-client executable is missing. Install it on the backend host; setup does not run shell commands automatically.",
    browser: "OAuth login must remain browser-reachable on a public HTTPS origin. The OpenAI tunnel does not publish the login page.",
    connected: "Ready", connecting: "Process running · not ready", stopped: "Stopped", connect: "Connect", disconnect: "Disconnect", refreshTunnel: "Refresh status",
    saveFirst: "Save changes before connecting or revoking authorizations.", api: "Responses API tool configuration", busy: "Working…",
  },
  zh: {
    title: "外部 Agent 与 MCP", intro: "通过 dashboard 的同一公网地址开放 Nerya 工具。OAuth 登录使用现有管理员密码。",
    enabled: "开放 MCP", enabledHint: "默认关闭。关闭后立即拒绝新的 MCP 请求。",
    ready: "已开启", off: "已关闭", loading: "正在读取 MCP 设置…", retry: "刷新",
    password: "管理员密码", configured: "已设置", missing: "未设置",
    passwordHint: "请先在「访问」设置中配置管理员密码，再开启 MCP。",
    sdkHint: "运行中的后端需要安装 MCP 可选依赖。在产品目录执行：uv sync --extra mcp",
    url: "公网域名", urlHint: "留空自动使用当前 Nerya 公网穿透地址。自定义地址必须使用 HTTPS，且不包含路径。",
    endpoint: "MCP 接口地址", copy: "复制接口地址", copied: "已复制", copiedError: "复制失败，请直接选择接口地址文本。",
    auth: "OAuth 2 · 授权码 + PKCE", scope: "外部写入权限",
    proposals: "策略与 Skill 提案", proposalsHint: "允许外部 Agent 提交 Skill 和策略变更提案，仍需审核后才能生效。",
    roles: "Agent 与配置管理", rolesHint: "允许现有角色管理工具和配置提案，不授权 shell 或实盘交易。",
    advanced: "工具允许与拒绝列表", restrict: "仅开放指定工具", allow: "允许的公开工具名", deny: "拒绝的公开工具名",
    allowHint: "每行一个完整工具名。启用限制后，空列表表示不开放任何工具。",
    save: "保存 MCP 设置", saving: "正在保存…", saved: "设置已保存，旧授权已撤销。外部客户端需重新登录授权。",
    unsaved: "有未保存的修改", discard: "放弃未保存的修改？", discardHint: "刷新会用已保存的设置替换当前表单。",
    revoke: "撤销所有外部授权", revokeHint: "所有已授权客户端将需要重新登录，管理员密码不会改变。", revoked: "外部授权已撤销。",
    tunnel: "OpenAI Secure MCP Tunnel", tunnelHint: "通过 OpenAI 官方 tunnel-client 建立出站连接。运行时 API key 用于隧道认证，不代替 MCP 用户授权。",
    tunnelEnable: "启用 OpenAI Tunnel", tunnelEnableHint: "Nerya 启动时恢复此连接。请先保存，再连接。",
    id: "Tunnel ID", key: "运行时 API key", keyHint: "保存在加密 Vault 中。留空保留已保存的 key，不会回显。",
    keySaved: "已保存至 Vault", keyNone: "尚未保存 key", forget: "解除已保存 key 的关联", forgetHint: "保存时解除关联，不导出或显示凭证。",
    install: "安装官方客户端", installHint: "后端主机尚未安装 tunnel-client。设置页不会自动运行 shell 安装命令。",
    browser: "OAuth 登录页仍需能通过公网 HTTPS 访问。OpenAI Tunnel 不会自动发布登录页。",
    connected: "已就绪", connecting: "进程运行中 · 尚未就绪", stopped: "未连接", connect: "连接", disconnect: "断开", refreshTunnel: "刷新连接状态",
    saveFirst: "连接或撤销授权前，请先保存修改。", api: "Responses API 工具配置", busy: "处理中…",
  },
};

export default function McpSettingsPanel() {
  const text = copy[useLocale().startsWith("zh") ? "zh" : "en"];
  const [saved, setSaved] = useState<McpStatus | null>(null);
  const [draft, setDraft] = useState<McpStatus | null>(null);
  const [apiKey, setApiKey] = useState("");
  const [allowText, setAllowText] = useState("");
  const [denyText, setDenyText] = useState("");
  const [clearKey, setClearKey] = useState(false);
  const [busy, setBusy] = useState("");
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const dirty = Boolean(saved && (JSON.stringify(saved) !== JSON.stringify(draft) || apiKey || clearKey || allowText !== (saved.allow_tools || []).join("\n") || denyText !== saved.deny_tools.join("\n")));
  const load = useCallback(async (signal?: AbortSignal) => {
    const result = await mcpRequest<McpStatus>("/mcp-settings", undefined, signal);
    setSaved(result); setDraft(result); setApiKey(""); setClearKey(false);
    setAllowText((result.allow_tools || []).join("\n")); setDenyText(result.deny_tools.join("\n"));
  }, []);
  useEffect(() => {
    const abort = new AbortController();
    void load(abort.signal).catch((e) => { if (!abort.signal.aborted) setError(String(e.message || e)); });
    return () => abort.abort();
  }, [load]);
  useEffect(() => {
    const unload = (e: BeforeUnloadEvent) => { if (dirty) { e.preventDefault(); e.returnValue = ""; } };
    window.addEventListener("beforeunload", unload);
    return () => window.removeEventListener("beforeunload", unload);
  }, [dirty]);
  async function refresh() {
    if (dirty && !(await confirm({ title: text.discard, message: text.discardHint, tone: "warning" }))) return;
    await run("refresh", async () => { await load(); });
  }
  useEffect(() => {
    const handler = () => void refresh();
    window.addEventListener("nerya:mcp-refresh", handler);
    return () => window.removeEventListener("nerya:mcp-refresh", handler);
  });
  async function run(name: string, action: () => Promise<void>) {
    setBusy(name); setError(""); setNotice("");
    try { await action(); } catch (e) { setError(e instanceof Error ? e.message : String(e)); }
    finally { setBusy(""); }
  }
  if (!draft || !saved) return <section aria-label="MCP"><ErrorBanner error={error} onRetry={() => void refresh()} />{!error && <p role="status">{text.loading}</p>}</section>;
  const update = (value: Partial<McpStatus>) => setDraft({ ...draft, ...value });
  const tunnel = draft.openai_tunnel;
  const setTunnel = (value: Partial<McpStatus["openai_tunnel"]>) => update({ openai_tunnel: { ...tunnel, ...value } });
  const canEnable = saved.sdk_installed && saved.admin_password_configured;
  const names = (value: string) => value.split(/\n|,/).map(v => v.trim()).filter(Boolean);
  async function save() {
    if (!draft || !saved) return;
    await run("save", async () => {
      await mcpRequest("/mcp-settings", { revision: saved.revision, enabled: draft.enabled,
        public_url: draft.public_url, allow_mutating: draft.allow_mutating,
        native_allow_mutating: draft.native_allow_mutating, allow_tools: draft.allow_tools === null ? null : names(allowText), deny_tools: names(denyText),
        openai_tunnel: { enabled: draft.enabled && tunnel.enabled, tunnel_id: tunnel.tunnel_id,
          ...(apiKey ? { api_key: apiKey } : {}), clear_api_key: clearKey } });
      await load(); setNotice(text.saved);
    });
  }
  return <section aria-label="MCP" className="space-y-6">
    <div className="flex flex-wrap items-start justify-between gap-3"><div><h2 className="text-lg font-semibold">{text.title}</h2><p className="mt-1 max-w-3xl text-sm text-[color:var(--text-muted)]">{text.intro}</p></div><Pill tone={saved.enabled ? "ok" : "neutral"}>{saved.enabled ? text.ready : text.off}</Pill></div>
    <ErrorBanner error={error} onRetry={() => void refresh()} />
    {notice && <p role="status" className="text-sm">{notice}</p>}
    <fieldset disabled={Boolean(busy)} className="space-y-5 border-0 p-0">
      <SettingsGroup title={text.auth}>
        <Row label={text.enabled} desc={text.enabledHint}><SwitchControl label={text.enabled} checked={draft.enabled} disabled={!draft.enabled && !canEnable} onCheckedChange={(enabled) => update({ enabled, openai_tunnel: { ...tunnel, enabled: enabled && tunnel.enabled } })} /></Row>
        <Row label={text.password}><Pill tone={saved.admin_password_configured ? "ok" : "warn"}>{saved.admin_password_configured ? text.configured : text.missing}</Pill></Row>
        {!saved.admin_password_configured && <p className="px-4 py-3 text-sm"><a href="/settings#access" className="underline">{text.passwordHint}</a></p>}
        {!saved.sdk_installed && <p className="px-4 py-3 text-sm">{text.sdkHint}</p>}
        <div className="space-y-3 p-4"><Field label={text.url} hint={text.urlHint}><input className="input-dark w-full" value={draft.public_url} onChange={e => update({ public_url: e.target.value })} placeholder={saved.detected_public_urls[0] || "https://nerya.example.com"} spellCheck={false} /></Field>
          <div><span className="text-xs text-[color:var(--text-muted)]">{text.endpoint}</span><div className="mt-1 flex flex-wrap items-center gap-2"><code className="break-all text-sm">{saved.endpoint}</code><button type="button" className="btn btn-ghost" onClick={() => void run("copy", async () => { try { await navigator.clipboard.writeText(saved.endpoint); setNotice(text.copied); } catch { throw new Error(text.copiedError); } })}>{text.copy}</button></div></div>
        </div>
      </SettingsGroup>
      <SettingsGroup title={text.scope}>
        <Row label={text.proposals} desc={text.proposalsHint}><SwitchControl label={text.proposals} checked={draft.allow_mutating} onCheckedChange={allow_mutating => { update({ allow_mutating }); if (allow_mutating) setDenyText([...new Set([...names(denyText), "nerya_trigger_emit"])].join("\n")); }} /></Row>
        <Row label={text.roles} desc={text.rolesHint}><SwitchControl label={text.roles} checked={draft.native_allow_mutating} onCheckedChange={native_allow_mutating => update({ native_allow_mutating })} /></Row>
        <div className="p-4"><Advanced title={text.advanced}><div className="space-y-3">
          <Row label={text.restrict}><SwitchControl label={text.restrict} checked={draft.allow_tools !== null} onCheckedChange={on => update({ allow_tools: on ? [] : null })} /></Row>
          {draft.allow_tools !== null && <Field label={text.allow} hint={text.allowHint}><textarea className="input-dark w-full font-mono text-xs" rows={4} value={allowText} onChange={e => setAllowText(e.target.value)} /></Field>}
          <Field label={text.deny}><textarea className="input-dark w-full font-mono text-xs" rows={3} value={denyText} onChange={e => setDenyText(e.target.value)} /></Field>
        </div></Advanced></div>
      </SettingsGroup>
      <SettingsGroup title={text.tunnel} description={text.tunnelHint}>
        <Row label={text.tunnelEnable} desc={text.tunnelEnableHint}><SwitchControl label={text.tunnelEnable} checked={tunnel.enabled} disabled={!draft.enabled} onCheckedChange={enabled => setTunnel({ enabled })} /></Row>
        <div className="grid gap-4 p-4 md:grid-cols-2">
          <Field label={text.id}><input className="input-dark w-full font-mono" placeholder="tunnel_…" value={tunnel.tunnel_id} onChange={e => setTunnel({ tunnel_id: e.target.value })} spellCheck={false} /></Field>
          <Field label={text.key} hint={text.keyHint}><input className="input-dark w-full" type="password" autoComplete="new-password" value={apiKey} onChange={e => setApiKey(e.target.value)} placeholder={tunnel.api_key_configured ? text.keySaved : text.keyNone} /></Field>
          {tunnel.api_key_configured && <label className="flex items-center gap-2 text-sm"><input type="checkbox" checked={clearKey} onChange={e => setClearKey(e.target.checked)} />{text.forget}</label>}
          {clearKey && <p className="text-sm text-[color:var(--text-muted)]">{text.forgetHint}</p>}
          <p className="text-sm text-[color:var(--text-muted)] md:col-span-2">{text.browser}</p>
          {!tunnel.installed && <div className="space-y-2 text-sm md:col-span-2"><strong>{text.install}</strong><p>{text.installHint}</p><code className="break-all">{tunnel.install_command}</code></div>}
          <div className="flex flex-wrap items-center gap-2 md:col-span-2"><Pill tone={saved.openai_tunnel.ready ? "ok" : saved.openai_tunnel.running ? "warn" : "neutral"}>{saved.openai_tunnel.ready ? text.connected : saved.openai_tunnel.running ? text.connecting : text.stopped}</Pill>
            <button type="button" className="btn btn-ghost" disabled={dirty || !saved.enabled || !saved.openai_tunnel.enabled || !tunnel.installed || saved.openai_tunnel.running} onClick={() => void run("connect", async () => { await mcpRequest("/mcp-settings/tunnel/start", {}); await load(); })}>{text.connect}</button>
            <button type="button" className="btn btn-ghost" disabled={dirty || !saved.openai_tunnel.running} onClick={() => void run("disconnect", async () => { await mcpRequest("/mcp-settings/tunnel/stop", { revision: saved.revision }); await load(); })}>{text.disconnect}</button>
            <button type="button" className="btn btn-ghost" onClick={() => void refresh()}>{text.refreshTunnel}</button>
          </div>
          {saved.openai_tunnel.error && <p role="alert" className="text-sm text-danger md:col-span-2">{saved.openai_tunnel.error}</p>}
          {tunnel.tunnel_id && <div className="md:col-span-2"><Advanced title={text.api}><pre className="overflow-auto text-xs">{JSON.stringify({ ...tunnel.api_tool, tunnel_id: tunnel.tunnel_id }, null, 2)}</pre></Advanced></div>}
        </div>
      </SettingsGroup>
      <div className="flex flex-wrap items-center gap-3"><button type="button" className="btn btn-primary" disabled={!dirty} onClick={() => void save()}>{busy === "save" ? text.saving : text.save}</button>
        {dirty && <span className="text-sm text-[color:var(--text-muted)]">{text.unsaved}</span>}
        <button type="button" className="btn btn-ghost" disabled={dirty || !saved.enabled} onClick={() => void run("revoke", async () => { if (await confirm({ title: text.revoke, message: text.revokeHint, tone: "warning" })) { await mcpRequest("/mcp-settings/revoke", { revision: saved.revision }); await load(); setNotice(text.revoked); } })}>{text.revoke}</button>
        {busy && <span role="status" className="text-sm">{text.busy}</span>}
      </div>
    </fieldset>
    <SkillManagementPanel />
  </section>;
}
