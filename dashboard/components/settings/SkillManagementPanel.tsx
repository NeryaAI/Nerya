"use client";

import { useEffect, useState } from "react";
import { useLocale } from "next-intl";
import { ErrorBanner, Pill } from "../Page";
import { Field, SettingsGroup } from "./SettingsFields";
import { confirm } from "../../lib/dialogs";
import { mcpRequest, type SkillCatalog, type SkillFile, type SkillRow } from "../../lib/mcpSettings";

const copy = {
  en: { title: "Skill workspace", intro: "Browse every playbook, including nested and disabled Skills. Reading a Skill does not execute it.",
    scope: "Scope", all: "All Skills", builtin: "Built-in Skills", workspace: "Workspace Skills", agent: "Agent assignments",
    role: "Agent role", roleHint: "Agent Skills use shared definitions and per-role assignments. Edit shared content from All / Workspace scope.",
    search: "Search Skills", searchHint: "Name or description", refresh: "Refresh", loading: "Loading Skills…", empty: "No Skills match this scope.",
    read: "Read", next: "Next page", back: "Previous page", file: "Skill file", content: "Skill content", create: "New Workspace Skill", id: "Skill ID",
    enabled: "Enabled", disabled: "Disabled", assigned: "Assigned", unassigned: "Not assigned", overrides: "Changes to a built-in Skill create a Workspace override; packaged files stay unchanged.",
    save: "Propose content change", remove: "Propose deletion", enable: "Propose enable", disable: "Propose disable", assign: "Propose assignment", unassign: "Propose removal",
    proposal: "Proposal created. Review it in Action Inbox before applying.", pending: "Changes are proposals, not immediate edits. Existing approval and validation gates still apply.",
    discard: "Discard unsaved Skill content?", confirm: "Create this Skill change proposal?", confirmHint: "This stages a reviewable change. No live Skill is modified yet.",
    redacted: "Sensitive text was redacted. This view cannot be saved back as a complete replacement.", busy: "Working…", select: "Select a Skill to read its playbook and files.",
  },
  zh: { title: "Skill 工作台", intro: "浏览所有说明文档，包括子级和已禁用 Skill。读取 Skill 不会执行它。",
    scope: "范围", all: "全部 Skill", builtin: "内置 Skill", workspace: "Workspace Skill", agent: "Agent 的 Skill 分配",
    role: "Agent 角色", roleHint: "Agent 使用共享 Skill 定义与各自的分配列表。修改共享内容请使用「全部 / Workspace」范围。",
    search: "搜索 Skill", searchHint: "名称或说明", refresh: "刷新", loading: "正在读取 Skill…", empty: "此范围没有匹配的 Skill。",
    read: "读取", next: "下一页", back: "上一页", file: "Skill 文件", content: "Skill 内容", create: "新建 Workspace Skill", id: "Skill ID",
    enabled: "已启用", disabled: "已禁用", assigned: "已分配", unassigned: "未分配", overrides: "编辑内置 Skill 会创建 Workspace 覆盖版本，不修改程序自带文件。",
    save: "提交内容变更提案", remove: "提交删除提案", enable: "提议启用", disable: "提议禁用", assign: "提议分配", unassign: "提议移除分配",
    proposal: "提案已创建，请到 Action Inbox 审核后生效。", pending: "所有变更均为提案，不会立即修改运行中的 Skill，原有审批与验证仍然生效。",
    discard: "放弃未保存的 Skill 内容？", confirm: "创建这项 Skill 变更提案？", confirmHint: "仅生成供审核的变更，目前不会修改正在使用的 Skill。",
    redacted: "敏感内容已脱敏，此视图不能作为完整文件覆盖保存。", busy: "处理中…", select: "选择一个 Skill，读取其说明文档和文件。",
  },
};
type Scope = "all" | "builtin" | "workspace" | "agent";

export default function SkillManagementPanel() {
  const t = copy[useLocale().startsWith("zh") ? "zh" : "en"];
  const [scope, setScope] = useState<Scope>("all");
  const [agent, setAgent] = useState("");
  const [roles, setRoles] = useState<{ name: string }[]>([]);
  const [query, setQuery] = useState("");
  const [search, setSearch] = useState("");
  const [offset, setOffset] = useState(0);
  const [epoch, setEpoch] = useState(0);
  const [catalog, setCatalog] = useState<SkillCatalog | null>(null);
  const [selected, setSelected] = useState<SkillRow | null>(null);
  const [file, setFile] = useState<SkillFile | null>(null);
  const [content, setContent] = useState("");
  const [newId, setNewId] = useState("");
  const [creating, setCreating] = useState(false);
  const [loading, setLoading] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const dirty = creating || Boolean(file && content !== file.text);
  useEffect(() => {
    const abort = new AbortController();
    void mcpRequest<{ roles: { name: string }[] }>("/mcp-settings/roles", undefined, abort.signal).then(r => { setRoles(r.roles); setAgent(r.roles[0]?.name || ""); })
      .catch(e => { if (!abort.signal.aborted) setError(e.message); });
    return () => abort.abort();
  }, []);
  useEffect(() => {
    if (scope === "agent" && !agent) return;
    const abort = new AbortController();
    setLoading(true); setError(""); setCatalog(null); setSelected(null); setFile(null);
    const params = new URLSearchParams({ scope, agent_id: agent, query: search, offset: String(offset), limit: "30", include_unassigned: "true" });
    void mcpRequest<SkillCatalog>("/skills/catalog?" + params, undefined, abort.signal).then(setCatalog)
      .catch(e => { if (!abort.signal.aborted) setError(e.message); }).finally(() => { if (!abort.signal.aborted) setLoading(false); });
    return () => abort.abort();
  }, [scope, agent, search, offset, epoch]);
  async function navigate(action: () => void) {
    if (dirty && !(await confirm({ title: t.discard, message: t.pending, tone: "warning" }))) return false;
    setCreating(false); action();
    return true;
  }
  async function read(row: SkillRow, name = "SKILL.md") {
    if (!(await navigate(() => undefined))) return;
    setBusy(true); setError(""); setNotice("");
    try {
      const params = new URLSearchParams({ skill_id: row.id, scope: scope === "agent" && !row.assigned ? "all" : scope,
        agent_id: agent, file: name, limit: "32000" });
      let result = await mcpRequest<SkillFile>("/skills/read?" + params);
      let text = result.text;
      while (result.next_offset !== null) {
        params.set("offset", String(result.next_offset));
        const page = await mcpRequest<SkillFile>("/skills/read?" + params);
        if (page.revision !== result.revision) throw new Error("Skill changed during reading; refresh before editing.");
        text += page.text; result = page;
      }
      setSelected(row); setFile({ ...result, text }); setContent(text); setCreating(false);
    } catch (e) { setError(e instanceof Error ? e.message : String(e)); }
    finally { setBusy(false); }
  }
  async function propose(action: "create" | "update" | "delete" | "enable" | "disable", row = selected) {
    if (!catalog || (!creating && !row)) return;
    if (!(await confirm({ title: t.confirm, message: t.confirmHint, tone: "warning" }))) return;
    setBusy(true); setError(""); setNotice("");
    try {
      const binding = action === "enable" || action === "disable";
      const revision = binding ? (scope === "agent" ? catalog.binding_revision : catalog.enabled_revision) : creating ? "missing" : file?.revision;
      const response = await mcpRequest<{ proposal: { id: string } }>("/skills/manage", {
        action, skill_id: creating ? newId : row!.id, scope: creating ? "workspace" : scope,
        agent_id: scope === "agent" ? agent : "", file: binding ? "SKILL.md" : file?.file || "SKILL.md",
        revision, content: binding || action === "delete" ? "" : content,
      });
      setNotice(`${t.proposal} (${response.proposal.id})`);
      if (!binding) { setCreating(false); setSelected(null); setFile(null); setContent(""); }
    } catch (e) { setError(e instanceof Error ? e.message : String(e)); }
    finally { setBusy(false); }
  }
  return <SettingsGroup title={t.title} description={t.intro}>
    <div className="space-y-4 p-4"><ErrorBanner error={error} onRetry={() => void navigate(() => setEpoch(v => v + 1))} />
      {notice && <p role="status" className="text-sm">{notice}</p>}
      <fieldset disabled={busy || dirty} className="grid gap-3 border-0 p-0 md:grid-cols-3">
        <Field label={t.scope}><select className="input-dark w-full" value={scope} onChange={e => { setOffset(0); setScope(e.target.value as Scope); }}><option value="all">{t.all}</option><option value="builtin">{t.builtin}</option><option value="workspace">{t.workspace}</option><option value="agent">{t.agent}</option></select></Field>
        {scope === "agent" && <Field label={t.role}><select className="input-dark w-full" value={agent} onChange={e => { setOffset(0); setAgent(e.target.value); }}>{roles.map(r => <option key={r.name}>{r.name}</option>)}</select></Field>}
        <Field label={t.search}><input className="input-dark w-full" placeholder={t.searchHint} value={query} onChange={e => setQuery(e.target.value)} onKeyDown={e => { if (e.key === "Enter") { e.preventDefault(); setSearch(query); setOffset(0); setEpoch(v => v + 1); } }} /></Field>
        <div className="flex items-end gap-2"><button type="button" className="btn btn-ghost" onClick={() => { setSearch(query); setOffset(0); setEpoch(v => v + 1); }}>{t.search}</button><button type="button" className="btn btn-ghost" onClick={() => setEpoch(v => v + 1)}>{t.refresh}</button></div>
      </fieldset>
      <div className="flex flex-wrap items-center gap-3"><button type="button" className="btn btn-ghost" disabled={busy || !catalog || scope === "agent"} onClick={() => void navigate(() => { setCreating(true); setSelected(null); setFile(null); setNewId("new_skill"); setContent("---\nname: new_skill\ndescription: Describe when this Skill should be used.\n---\n\n# Workflow\n\n"); })}>{t.create}</button>
        {dirty && <button type="button" className="btn btn-ghost" onClick={() => void navigate(() => { setContent(file?.text || ""); })}>{t.discard}</button>}
        {catalog && <span className="text-xs text-[color:var(--text-muted)]">{catalog.total} Skills</span>}
      </div>
      {scope === "agent" && <p className="text-sm text-[color:var(--text-muted)]">{t.roleHint}</p>}
      {loading && <p role="status">{t.loading}</p>}
      {catalog?.skills.length === 0 && <p className="text-sm">{t.empty}</p>}
      <div className="grid min-w-0 gap-4 xl:grid-cols-[minmax(16rem,1fr)_minmax(0,2fr)]">
        <div className="max-h-[36rem] space-y-2 overflow-auto">
          {catalog?.skills.map(row => <div key={row.id + row.source} className="rounded-lg border border-[color:var(--line)] p-3">
            <div className="flex flex-wrap items-center justify-between gap-2"><button type="button" className="text-left text-sm font-medium underline-offset-4 hover:underline" disabled={busy || dirty} onClick={() => void read(row)}>{row.id}</button><Pill tone={row.enabled ? "ok" : "neutral"}>{row.enabled ? t.enabled : t.disabled}</Pill></div>
            <p className="mt-1 line-clamp-3 text-xs text-[color:var(--text-muted)]">{row.description}</p>
            <div className="mt-2 flex flex-wrap items-center gap-2"><span className="text-xs text-[color:var(--text-muted)]">{row.source}{scope === "agent" ? ` · ${row.assigned ? t.assigned : t.unassigned}` : ""}</span>
              <button type="button" className="btn btn-ghost text-xs" disabled={busy || dirty} onClick={() => void propose((scope === "agent" ? row.assigned : row.enabled) ? "disable" : "enable", row)}>{scope === "agent" ? row.assigned ? t.unassign : t.assign : row.enabled ? t.disable : t.enable}</button></div>
          </div>)}
        </div>
        <div className="min-w-0 space-y-3">
          {!file && !creating && <p className="text-sm text-[color:var(--text-muted)]">{t.select}</p>}
          {creating && <Field label={t.id}><input className="input-dark w-full" value={newId} onChange={e => setNewId(e.target.value)} /></Field>}
          {file && <Field label={t.file}><select className="input-dark w-full" value={file.file} disabled={busy || dirty} onChange={e => selected && void read(selected, e.target.value)}>{file.files.map(name => <option key={name}>{name}</option>)}</select></Field>}
          {(file || creating) && <><Field label={t.content}><textarea className="input-dark min-h-[22rem] w-full font-mono text-xs leading-6" rows={18} value={content} onChange={e => setContent(e.target.value)} spellCheck={false} readOnly={busy || scope === "agent" || Boolean(file?.text.includes("***REDACTED***"))} /></Field>
            {file?.source === "builtin" && <p className="text-sm text-[color:var(--text-muted)]">{t.overrides}</p>}
            {file?.text.includes("***REDACTED***") && <p className="text-sm text-danger">{t.redacted}</p>}
            {scope !== "agent" && <div className="flex flex-wrap gap-2"><button type="button" className="btn btn-primary" disabled={busy || !dirty || Boolean(file?.text.includes("***REDACTED***"))} onClick={() => void propose(creating ? "create" : "update")}>{t.save}</button>
              {file && ["workspace", "workspace_installed"].includes(file.source) && <button type="button" className="btn btn-ghost" disabled={busy || dirty} onClick={() => void propose("delete")}>{t.remove}</button>}</div>}
          </>}
        </div>
      </div>
      {catalog && <div className="flex gap-2"><button type="button" className="btn btn-ghost" disabled={!offset || busy || dirty} onClick={() => setOffset(Math.max(0, offset - 30))}>{t.back}</button><button type="button" className="btn btn-ghost" disabled={catalog.next_offset === null || busy || dirty} onClick={() => setOffset(catalog.next_offset || 0)}>{t.next}</button></div>}
      <p className="text-xs text-[color:var(--text-muted)]">{t.pending}</p>{busy && <p role="status">{t.busy}</p>}
    </div>
  </SettingsGroup>;
}
