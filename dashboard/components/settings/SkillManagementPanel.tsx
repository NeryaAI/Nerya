"use client";

import { useEffect, useState } from "react";
import { useTranslations } from "next-intl";
import * as Dialog from "@radix-ui/react-dialog";
import { clientApi } from "../../lib/clientApi";
import { Markdown } from "../chat/Markdown";
import { ErrorBanner } from "../Page";
import { SearchIcon, SkillsIcon, ChevronRightIcon, XIcon, PlusIcon, RefreshIcon } from "../icons";
import styles from "./SkillLibrary.module.css";
import { SkillFileTree } from "./SkillFileTree";
import { Field } from "./SettingsFields";
import { confirm } from "../../lib/dialogs";
import { mcpRequest, type SkillCatalog, type SkillFile, type SkillRow } from "../../lib/mcpSettings";

type Scope = "all" | "builtin" | "workspace" | "agent";

export default function SkillManagementPanel() {
  const t = useTranslations("settings.skillManagement");
  const common = useTranslations("common");
  const [filtersOpen, setFiltersOpen] = useState(false);
  const [editing, setEditing] = useState(false);
  const [importing, setImporting] = useState(false);
  const [importSource, setImportSource] = useState("");
  const [importSubdir, setImportSubdir] = useState("");
  const [scope, setScope] = useState<Scope>("all");
  const [view, setView] = useState<"core" | "professional" | "all">("core");
  const [methods, setMethods] = useState<Record<string, SkillRow[]>>({});
  const [expanded, setExpanded] = useState<string[]>([]);
  const [loadingMethods, setLoadingMethods] = useState("");
  const [agent, setAgent] = useState("");
  const [roles, setRoles] = useState<{ name: string; catalog_parent?: string }[]>([]);
  const [query, setQuery] = useState("");
  const [search, setSearch] = useState("");
  const [offset, setOffset] = useState(0);
  const [epoch, setEpoch] = useState(0);
  const [catalog, setCatalog] = useState<SkillCatalog | null>(null);
  const [rootSkill, setRootSkill] = useState<SkillRow | null>(null);
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
    if (!dirty) return;
    const guard = (event: BeforeUnloadEvent) => { event.preventDefault(); event.returnValue = ""; };
    window.addEventListener("beforeunload", guard);
    return () => window.removeEventListener("beforeunload", guard);
  }, [dirty]);
  useEffect(() => {
    const abort = new AbortController();
    void mcpRequest<{ roles: { name: string; catalog_parent?: string }[] }>("/mcp-settings/roles", undefined, abort.signal).then(r => { setRoles(r.roles); setAgent(r.roles.find(role => !role.catalog_parent)?.name || r.roles[0]?.name || ""); })
      .catch(e => { if (!abort.signal.aborted) setError(e.message); });
    return () => abort.abort();
  }, []);
  useEffect(() => {
    if (scope === "agent" && !agent) return;
    const abort = new AbortController();
    setLoading(true); setError(""); setCatalog(null); setSelected(null); setFile(null); setMethods({}); setExpanded([]);
    const params = new URLSearchParams({ scope, view: search ? "all" : view, hierarchical: search ? "false" : "true", agent_id: agent, query: search, offset: String(offset), limit: "30", include_unassigned: "true" });
    void mcpRequest<SkillCatalog>("/skills/catalog?" + params, undefined, abort.signal).then(setCatalog)
      .catch(e => { if (!abort.signal.aborted) setError(e.message); }).finally(() => { if (!abort.signal.aborted) setLoading(false); });
    return () => abort.abort();
  }, [scope, view, agent, search, offset, epoch]);
  useEffect(() => {
    if (dirty || busy) return;
    const timer = window.setTimeout(() => { setSearch(query); setOffset(0); }, 250);
    return () => window.clearTimeout(timer);
  }, [query, dirty, busy]);
  async function toggleMethods(row: SkillRow, loadOnly = false) {
    if (!loadOnly && expanded.includes(row.id)) { setExpanded(ids => ids.filter(id => id !== row.id)); return; }
    if (methods[row.id]) { if (!loadOnly) setExpanded(ids => [...ids, row.id]); return; }
    setLoadingMethods(row.id); setBusy(true); setError("");
    try {
      const params = new URLSearchParams({ scope, agent_id: agent, parent: row.id, limit: "200", include_unassigned: "true" });
      const rows: SkillRow[] = [];
      let next: number | null = 0;
      do {
        params.set("offset", String(next));
        const result: SkillCatalog = await mcpRequest<SkillCatalog>("/skills/catalog?" + params);
        rows.push(...result.skills); next = result.next_offset;
      } while (next !== null);
      setMethods(previous => ({ ...previous, [row.id]: rows })); if (!loadOnly) setExpanded(ids => [...ids, row.id]);
    } catch (e) { setError(e instanceof Error ? e.message : String(e)); }
    finally { setBusy(false); setLoadingMethods(""); }
  }
  async function navigate(action: () => void) {
    if (dirty && !(await confirm({ title: t("discard"), message: t("discardFileHint"), tone: "warning" }))) return false;
    setCreating(false); action();
    return true;
  }
  async function openSkill(row: SkillRow) {
    const loaded = await read(row);
    if (!loaded) return;
    const root = row.catalog_parent ? catalog?.skills.find(item => item.id === row.catalog_parent) : row;
    setRootSkill(root || row);
    if (root?.method_count) await toggleMethods(root, true);
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
      setSelected(row); setFile({ ...result, text }); setContent(text); setCreating(false); setEditing(false);
      return true;
    } catch (e) { setError(e instanceof Error ? e.message : String(e)); }
    finally { setBusy(false); }
  }
  async function applyChange(action: "create" | "update" | "delete" | "enable" | "disable", row = selected) {
    if (!catalog || (!creating && !row)) return;
    if (action === "delete" && !(await confirm({ title: t("remove"), message: t("deleteHint", { name: `${row!.id}/${file?.file || "SKILL.md"}` }), tone: "danger" }))) return;
    setBusy(true); setError(""); setNotice("");
    try {
      const binding = action === "enable" || action === "disable";
      const revision = binding ? (scope === "agent" ? catalog.binding_revision : catalog.enabled_revision) : creating ? "missing" : file?.revision;
      await mcpRequest<{ applied: boolean }>("/skills/manage", {
        action, skill_id: creating ? newId : row!.id, scope: creating ? "workspace" : scope,
        agent_id: scope === "agent" ? agent : "", file: binding ? "SKILL.md" : file?.file || "SKILL.md",
        revision, content: binding || action === "delete" ? "" : creating ? content.replace(/^name:.*$/m, "name: " + newId.trim()) : content,
      });
      setNotice(t("saved"));
      if (!binding) { setCreating(false); setSelected(null); setFile(null); setContent(""); }
      setEpoch(value => value + 1);
    } catch (e) { setError(e instanceof Error ? e.message : String(e)); }
    finally { setBusy(false); }
  }
  async function importSkill() {
    if (!importSource.trim() || busy) return;
    setBusy(true); setError("");
    try {
      const result = await clientApi.skillsInstall({ source: importSource.trim(), subdir: importSubdir.trim() || undefined });
      if (result.error || result.ok === false || result.status === "rejected") throw new Error(String(result.error || result.status));
      setNotice(t("imported")); setImporting(false); setImportSource(""); setImportSubdir(""); setEpoch(value => value + 1);
    } catch (e) { setError(e instanceof Error ? e.message : String(e)); }
    finally { setBusy(false); }
  }
  async function removeSkill(row: SkillRow) {
    if (!await confirm({ title: t("remove"), message: t("deleteHint", { name: row.id }), tone: "danger" })) return;
    setBusy(true); setError("");
    try {
      const params = new URLSearchParams({ skill_id: row.id, scope, file: "SKILL.md" });
      const current = await mcpRequest<SkillFile>("/skills/read?" + params);
      await mcpRequest("/skills/manage", { action: "delete", skill_id: row.id, scope, file: "SKILL.md", revision: current.revision });
      setNotice(t("saved")); setEpoch(value => value + 1);
    } catch (e) { setError(e instanceof Error ? e.message : String(e)); }
    finally { setBusy(false); }
  }
  function skillRow(row: SkillRow): JSX.Element {
    const enabled = scope === "agent" ? Boolean(row.assigned) : row.enabled;
    const title = (row.title || row.id).replaceAll("_", " ").replace(/(^|\s)\S/g, value => value.toUpperCase());
    return <div key={row.id + row.source} data-skill-id={row.id} className={styles.row}>
      <div className={styles.rowMain}>
        <button type="button" className={styles.open} aria-label={`${t("read")}: ${row.id}`} disabled={busy || dirty || loading} onClick={() => void openSkill(row)}>
          <span className={styles.glyph} aria-hidden><SkillsIcon size={18}/></span>
          <span className={styles.rowText}><span className={styles.rowTitle}>{title}</span>
            <span className={styles.rowDescription}>{row.description}</span><span className={styles.rowId}>{row.id}</span>
          </span><ChevronRightIcon size={16} className={styles.chevron}/>
        </button>
        <div className={styles.controls}>
          <span className={styles.enabledLabel}>{enabled ? t(scope === "agent" ? "assigned" : "enabled") : t(scope === "agent" ? "unassigned" : "disabled")}</span>
          <button type="button" className={styles.switch} aria-label={`${scope === "agent" ? enabled ? t("unassign") : t("assign") : enabled ? t("disable") : t("enable")}: ${row.id}`}
            role="switch" aria-checked={enabled} disabled={busy || dirty || loading} onClick={() => void applyChange(enabled ? "disable" : "enable", row)}><span aria-hidden/></button>
        </div>
      </div>
      {(Boolean(row.method_count) || row.catalog_parent || ["workspace", "workspace_installed"].includes(row.source)) && <div className={styles.rowMeta}>
        {Boolean(row.method_count) && <button type="button" aria-expanded={expanded.includes(row.id)} aria-controls={`methods-${row.id}`} disabled={busy || dirty} onClick={() => void toggleMethods(row)}>{loadingMethods === row.id ? t("loading") : t("methods", { count: row.method_count || 0 })}<ChevronRightIcon size={12}/></button>}
        {row.catalog_parent && <span>{t("partOf", { name: row.catalog_parent })}</span>}
        {scope !== "agent" && ["workspace", "workspace_installed"].includes(row.source) && <button type="button" className="text-danger" disabled={busy || dirty} onClick={() => void removeSkill(row)}>{common("delete")}</button>}
      </div>}
      {expanded.includes(row.id) && <div id={`methods-${row.id}`} className={styles.methods} aria-label={t("methods", { count: methods[row.id]?.length || 0 })}>{methods[row.id]?.map(skillRow)}</div>}
    </div>;
  }
  return <section className={styles.library} aria-label={t("title")}>
    <div className="space-y-5"><ErrorBanner error={error} onRetry={() => void navigate(() => setEpoch(v => v + 1))} />
      {notice && <p role="status" className="text-sm">{notice}</p>}
      <div className={styles.intro}><h2>{t("libraryTitle")}</h2><p>{t("libraryHint")}</p></div>
      <fieldset disabled={busy || dirty} className={styles.searchBar}>
        <label className={styles.search}><SearchIcon size={17}/><span className="sr-only">{t("search")}</span>
          <input placeholder={t("searchHint")} maxLength={512} value={query} onChange={e => setQuery(e.target.value)} onKeyDown={e => { if (e.key === "Enter") { e.preventDefault(); setSearch(query); setOffset(0); } }} />
          {query && <button type="button" aria-label={t("clearSearch")} onClick={() => { setQuery(""); setSearch(""); setOffset(0); }}><XIcon size={15}/></button>}
        </label>
        <button type="button" className="btn btn-ghost" onClick={() => { setError(""); setImporting(true); }}>{t("import")}</button>
      </fieldset>
      <div className={styles.discoveryBar}>
        <div className={styles.views} role="group" aria-label={t("view")}>{(["core", "professional", "all"] as const).map(value => <button type="button" key={value} disabled={busy || dirty} aria-pressed={!search && view === value} onClick={() => { setView(value); setQuery(""); setSearch(""); setOffset(0); }}>{t(`${value}View`)}</button>)}</div>
        <details className={styles.filters} open={filtersOpen} onToggle={event => setFiltersOpen(event.currentTarget.open)}><summary>{t("scope")}</summary><fieldset disabled={busy || dirty}>
        <Field label={t("scope")}><select className="input-dark w-full" value={scope} onChange={e => { setOffset(0); setScope(e.target.value as Scope); }}><option value="all">{t("all")}</option><option value="builtin">{t("builtin")}</option><option value="workspace">{t("workspace")}</option><option value="agent">{t("agent")}</option></select></Field>
        {scope === "agent" && <Field label={t("role")}><select className="input-dark w-full" value={agent} onChange={e => { setOffset(0); setAgent(e.target.value); }}><optgroup label={t("primaryRoles")}>{roles.filter(r => !r.catalog_parent).map(r => <option key={r.name}>{r.name}</option>)}</optgroup><optgroup label={t("profileRoles")}>{roles.filter(r => r.catalog_parent).map(r => <option key={r.name}>{r.name}</option>)}</optgroup></select></Field>}
        </fieldset></details>
      </div>
      <div className={styles.actions}><button type="button" className="btn btn-ghost" disabled={busy || !catalog || scope === "agent"} onClick={() => void navigate(() => { setNotice(""); setError(""); setCreating(true); setSelected(null); setFile(null); setNewId("new_skill"); setContent("---\nname: new_skill\ndescription: Describe when this Skill should be used.\n---\n\n# Workflow\n\n"); })}><PlusIcon size={14}/>{t("create")}</button>
        {dirty && <button type="button" className="btn btn-ghost" onClick={() => void navigate(() => { setContent(file?.text || ""); })}>{t("discard")}</button>}
        <span className={styles.count} role="status" aria-live="polite">{loading ? t("loading") : catalog ? t(search ? "entryCount" : "workflowCount", { count: catalog.total }) : ""}</span>
        <button type="button" className="ui-icon-button" aria-label={t("refresh")} title={t("refresh")} disabled={busy || dirty || loading} onClick={() => setEpoch(v => v + 1)}><RefreshIcon size={15}/></button>
      </div>
      {scope === "agent" && <p className="text-sm text-[color:var(--text-muted)]">{t("roleHint")}</p>}
      {loading && <div className={styles.skeleton} aria-hidden>{[0, 1, 2].map(i => <div key={i}/>)}</div>}
      {catalog?.skills.length === 0 && <div className={styles.empty}><SearchIcon size={22}/><h3>{t("empty")}</h3><p>{t("emptyHint")}</p><button type="button" className="btn btn-ghost" onClick={() => { setQuery(""); setSearch(""); setView("all"); setScope("all"); setOffset(0); }}>{t("resetFilters")}</button></div>}
      <div className="min-w-0">
        <div data-testid="skill-catalog" className="min-w-0" aria-busy={loading}>{Array.from(new Set(catalog?.skills.map(row => row.source))).map(source => <section key={source} className="mb-6"><h3 className={styles.source}>{source === "builtin" ? t("builtin") : source === "workspace" || source === "workspace_installed" ? t("workspace") : source}</h3>{catalog?.skills.filter(row => row.source === source).map(skillRow)}</section>)}</div>
        <Dialog.Root open={Boolean(file) || creating} onOpenChange={open => { if (!open && !busy) void navigate(() => { setSelected(null); setFile(null); setContent(""); }); }}>
        <Dialog.Portal>
        <Dialog.Overlay className="ui-modal-overlay" />
        <Dialog.Content aria-describedby={undefined} className="ui-dialog" style={{ width: "min(1120px, calc(100vw - 24px))" }}>
        <div className="mb-4 flex items-center justify-between gap-3"><Dialog.Title className="min-w-0 break-all text-lg font-semibold">{creating ? t("create") : rootSkill?.id || selected?.id}</Dialog.Title><Dialog.Close className="btn btn-ghost" disabled={busy}>{common("close")}</Dialog.Close></div>
        <div className={creating ? "" : "grid min-w-0 gap-5 md:grid-cols-[240px_minmax(0,1fr)]"}>
        {!creating && file && <aside className="max-h-[65vh] min-w-0 space-y-4 overflow-auto border-b border-[color:var(--line)] pb-4 md:border-b-0 md:border-r md:pr-4">
          {rootSkill && <button type="button" className="btn btn-ghost w-full justify-start" disabled={busy} onClick={() => void read(rootSkill)}>{t("mainSkill")}: {rootSkill.id}</button>}
          {!!rootSkill?.method_count && <section><h3 className="mb-2 text-sm font-semibold">{t("subSkills")}</h3>{loadingMethods ? <p role="status">{t("loading")}</p> : methods[rootSkill.id]?.map(row => <button key={row.id} type="button" disabled={busy} aria-pressed={selected?.id === row.id} onClick={() => void read(row)} className={"mb-1 w-full break-all rounded px-2 py-2 text-left text-xs " + (selected?.id === row.id ? "bg-brand-500/10 text-brand-500" : "hover:bg-brand-500/10")}>{row.id}</button>)}</section>}
          <section><h3 className="mb-2 break-all text-sm font-semibold">{selected?.id} /</h3><SkillFileTree files={file.files} selected={file.file} disabled={busy} onSelect={name => { if (selected) void read(selected, name); }} /></section>
        </aside>}
        <div className="min-w-0 space-y-3">
          {file && <div className={styles.fileMeta}><span>{t("fileSize", { count: content.length })}</span><span>{t("fileCount", { count: file.files.length })}</span><code title={file.revision}>{file.revision.slice(0, 12)}</code></div>}
          <ErrorBanner error={error} />
          {notice && <p role="status" className="text-sm">{notice}</p>}
          {!file && !creating && <p className="text-sm text-[color:var(--text-muted)]">{t("select")}</p>}
          {creating && <Field label={t("id")}><input className="input-dark w-full" value={newId} onChange={e => setNewId(e.target.value.trim())} /></Field>}
          {file && <div className="flex flex-wrap items-center justify-between gap-2 border-b border-[color:var(--line)] pb-3"><span className="min-w-0 break-all font-mono text-xs">{selected?.id} / {file.file}</span><div className="flex gap-2"><button type="button" className="btn btn-ghost" aria-pressed={!editing} disabled={busy} onClick={() => setEditing(false)}>{t("preview")}</button><button type="button" className="btn btn-ghost" aria-pressed={editing} disabled={busy || scope === "agent" || content.includes("***REDACTED***")} onClick={() => setEditing(true)}>{t("edit")}</button></div></div>}
          {file && !editing && !creating && <div className="max-h-[60vh] min-w-0 overflow-auto">{/\.md$/i.test(file.file) ? <Markdown>{content.replace(/^(?:\s*<!--[\s\S]*?-->\s*)*---\r?\n[\s\S]*?\r?\n---(?:\r?\n|$)/, "").trim()}</Markdown> : <pre className="whitespace-pre-wrap break-words rounded bg-[color:var(--bg)] p-4 font-mono text-xs leading-6">{content}</pre>}</div>}
          {(creating || (file && editing)) && <><Field label={t("content")}><textarea className="input-dark min-h-[22rem] w-full font-mono text-xs leading-6" rows={18} value={content} onChange={e => setContent(e.target.value)} spellCheck={false} readOnly={busy || scope === "agent" || Boolean(file?.text.includes("***REDACTED***"))} /></Field>
            {file?.source === "builtin" && <p className="text-sm text-[color:var(--text-muted)]">{t("overrides")}</p>}
            {file?.text.includes("***REDACTED***") && <p className="text-sm text-danger">{t("redacted")}</p>}
            {scope !== "agent" && <div className="flex flex-wrap gap-2"><button type="button" className="btn btn-primary" disabled={busy || !dirty || (creating && !/^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$/.test(newId)) || Boolean(file?.text.includes("***REDACTED***"))} onClick={() => void applyChange(creating ? "create" : "update")}>{t("save")}</button>
              {file && ["workspace", "workspace_installed"].includes(file.source) && <button type="button" className="btn btn-ghost" disabled={busy || dirty} onClick={() => void applyChange("delete")}>{t("remove")}</button>}</div>}
          </>}
        </div>
        </div>
        </Dialog.Content></Dialog.Portal></Dialog.Root>
      </div>
      {catalog && (offset > 0 || catalog.next_offset !== null) && <div className="flex gap-2"><button type="button" className="btn btn-ghost" disabled={!offset || busy || dirty} onClick={() => setOffset(Math.max(0, offset - 30))}>{t("back")}</button><button type="button" className="btn btn-ghost" disabled={catalog.next_offset === null || busy || dirty} onClick={() => setOffset(catalog.next_offset || 0)}>{t("next")}</button></div>}
      <Dialog.Root open={importing} onOpenChange={open => { if (!busy) setImporting(open); }}><Dialog.Portal><Dialog.Overlay className="ui-modal-overlay" /><Dialog.Content className="ui-dialog p-6" aria-describedby="skill-import-help"><Dialog.Title className="text-lg font-semibold">{t("import")}</Dialog.Title><Dialog.Description id="skill-import-help" className="my-3 text-sm text-[color:var(--text-muted)]">{t("importHint")}</Dialog.Description><form className="space-y-4" onSubmit={event => { event.preventDefault(); void importSkill(); }}><ErrorBanner error={error} /><Field label={t("importSource")}><input className="input-dark w-full" value={importSource} onChange={event => setImportSource(event.target.value)} disabled={busy} required /></Field><Field label={t("importSubdir")}><input className="input-dark w-full" value={importSubdir} onChange={event => setImportSubdir(event.target.value)} disabled={busy} /></Field><div className="flex justify-end gap-2"><Dialog.Close className="btn btn-ghost" disabled={busy}>{common("cancel")}</Dialog.Close><button className="btn btn-primary" disabled={busy || !importSource.trim()}>{busy ? t("busy") : t("import")}</button></div></form></Dialog.Content></Dialog.Portal></Dialog.Root>
      {busy && <p role="status">{t("busy")}</p>}
    </div>
  </section>;
}
