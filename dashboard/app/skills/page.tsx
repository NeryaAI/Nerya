"use client";

import { useEffect, useMemo, useState, type ReactNode } from "react";
import {
  Card,
  Empty,
  ErrorBanner,
  PageBody,
  PageHeader,
  Pill,
} from "../../components/Page";
import { SectionTabs } from "../../components/SectionTabs";
import {
  CheckIcon,
  EditIcon,
  PlusIcon,
  SearchIcon,
  SkillsIcon,
  WrenchIcon,
  XIcon,
} from "../../components/icons";
import {
  clientApi,
  type SkillDetail,
  type SkillFileSummary,
  type SkillSummary,
} from "../../lib/clientApi";

type FilterMode = "all" | "workspace" | "builtin" | "installed" | "editable";

function asText(value: unknown): string {
  if (value === null || value === undefined) return "";
  if (typeof value === "string") return value;
  if (typeof value === "number" || typeof value === "boolean") return String(value);
  return JSON.stringify(value);
}

function sourceTone(source?: string): "neutral" | "ok" | "brand" | "warn" {
  if (source === "workspace" || source === "workspace_installed") return "ok";
  if (source === "builtin") return "brand";
  if (source === "external" || source === "user_home") return "warn";
  return "neutral";
}

function sourceLabel(source?: string): string {
  if (source === "workspace_installed") return "workspace installed";
  if (source === "workspace") return "workspace";
  if (source === "builtin") return "builtin";
  if (source === "user_home") return "home";
  return source || "runtime";
}

function bytes(value: number | undefined): string {
  const n = Number(value || 0);
  if (n < 1024) return `${n} B`;
  if (n < 1024 * 1024) return `${(n / 1024).toFixed(1)} KB`;
  return `${(n / 1024 / 1024).toFixed(1)} MB`;
}

function timestamp(seconds: number | undefined): string {
  if (!seconds) return "-";
  return new Date(seconds * 1000).toLocaleString();
}

function firstLines(text: string, max = 7): string {
  const lines = text.split(/\r?\n/).filter((line) => line.trim());
  return lines.slice(0, max).join("\n");
}

type ParsedInstallSource = {
  repo: string;
  ref?: string;
  subdir?: string;
};

function parseGithubInstallSource(value: string): ParsedInstallSource | null {
  const raw = value.trim();
  if (!raw) return null;
  try {
    const url = new URL(raw);
    if (!["github.com", "www.github.com"].includes(url.hostname.toLowerCase())) {
      return null;
    }
    const parts = url.pathname.split("/").filter(Boolean).map(decodeURIComponent);
    if (parts.length < 2) return null;
    const owner = parts[0];
    const repoName = parts[1].replace(/\.git$/, "");
    const repo = `https://github.com/${owner}/${repoName}.git`;
    if (parts.length < 4 || !["tree", "blob"].includes(parts[2])) {
      return { repo };
    }
    const tail = parts.slice(3);
    const markerIndex = tail.slice(1).findIndex((part) => part === "skills");
    const splitAt = markerIndex >= 0 ? markerIndex + 1 : 1;
    const ref = tail.slice(0, splitAt).join("/");
    let subdirParts = tail.slice(splitAt);
    if (parts[2] === "blob" && subdirParts[subdirParts.length - 1]?.toLowerCase() === "skill.md") {
      subdirParts = subdirParts.slice(0, -1);
    }
    return {
      repo,
      ref,
      subdir: subdirParts.length ? subdirParts.join("/") : undefined,
    };
  } catch {
    return null;
  }
}

function FilterButton({
  active,
  children,
  onClick,
}: {
  active: boolean;
  children: ReactNode;
  onClick: () => void;
}) {
  return (
    <button
      type="button"
      onClick={onClick}
      className={`rounded-md border px-2.5 py-1 text-[11px] transition-colors ${
        active
          ? "border-brand-400/50 bg-brand-500/15 text-white"
          : "border-white/5 bg-ink-950/30 text-ink-400 hover:border-brand-500/25 hover:text-white"
      }`}
    >
      {children}
    </button>
  );
}

function Metric({
  label,
  value,
  detail,
}: {
  label: string;
  value: string | number;
  detail?: string;
}) {
  return (
    <div className="rounded-lg border border-brand-500/10 bg-ink-950/30 p-3">
      <div className="text-[10px] uppercase tracking-[0.16em] text-ink-500">{label}</div>
      <div className="mt-1 text-xl font-semibold text-white">{value}</div>
      {detail ? <div className="mt-0.5 text-[11px] text-ink-500">{detail}</div> : null}
    </div>
  );
}

function FileGroup({ title, files }: { title: string; files: SkillFileSummary[] }) {
  if (!files.length) return null;
  return (
    <section>
      <div className="mb-2 text-[12px] font-medium text-ink-200">{title}</div>
      <div className="embedded-list-scroll-sm rounded-lg border border-brand-500/10">
        {files.map((file) => (
          <div
            key={`${file.kind}:${file.path}`}
            className="grid grid-cols-[1fr_auto] gap-3 border-b border-brand-500/10 px-3 py-2 last:border-b-0"
          >
            <div className="min-w-0">
              <div className="truncate font-mono text-[12px] text-ink-100">{file.path}</div>
              <div className="mt-0.5 text-[10px] text-ink-500">{timestamp(file.mtime)}</div>
            </div>
            <div className="text-right text-[11px] text-ink-400">{bytes(file.size)}</div>
          </div>
        ))}
      </div>
    </section>
  );
}

function groupFiles(files: SkillFileSummary[] | undefined, kind: string): SkillFileSummary[] {
  return (files || []).filter((file) => file.kind === kind);
}

export default function SkillsPage() {
  const [skills, setSkills] = useState<SkillSummary[]>([]);
  const [installed, setInstalled] = useState<Array<Record<string, unknown>>>([]);
  const [lockStatus, setLockStatus] = useState<Record<string, unknown> | null>(null);
  const [lockEntries, setLockEntries] = useState<Array<Record<string, unknown>>>([]);
  const [selected, setSelected] = useState<string | null>(null);
  const [detail, setDetail] = useState<SkillDetail | null>(null);
  const [draft, setDraft] = useState("");
  const [query, setQuery] = useState("");
  const [filterMode, setFilterMode] = useState<FilterMode>("all");
  const [source, setSource] = useState("");
  const [subdir, setSubdir] = useState("");
  const [gitRef, setGitRef] = useState("");
  const [advancedInstall, setAdvancedInstall] = useState(false);
  const [creating, setCreating] = useState(false);
  const [createName, setCreateName] = useState("");
  const [createDescription, setCreateDescription] = useState("");
  const [createBody, setCreateBody] = useState("");
  const [loading, setLoading] = useState(true);
  const [detailLoading, setDetailLoading] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [info, setInfo] = useState<string | null>(null);

  async function loadDetail(skillId: string) {
    setDetailLoading(true);
    try {
      const res = await clientApi.skillDetail(skillId);
      if (!res.ok || !res.skill) {
        throw new Error(res.error || "skill detail unavailable");
      }
      setDetail(res.skill);
      setDraft(res.skill.skill_md || "");
    } catch (e) {
      setDetail(null);
      setDraft("");
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setDetailLoading(false);
    }
  }

  async function refresh(focus?: string | null) {
    setLoading(true);
    try {
      const [skillsRes, installedRes, statusRes, inspectRes] = await Promise.all([
        clientApi.skills(),
        clientApi.skillsInstalled().catch(() => ({ installed: [] })),
        clientApi.skillsLockStatus().catch(() => null),
        clientApi.skillsLockInspect().catch(() => ({ entries: [] })),
      ]);
      const rows = (skillsRes.skills || []).slice().sort((a, b) => a.id.localeCompare(b.id));
      setSkills(rows);
      setInstalled(installedRes.installed || []);
      setLockStatus(statusRes || null);
      setLockEntries(inspectRes.entries || []);
      const next = focus && rows.some((skill) => skill.id === focus)
        ? focus
        : selected && rows.some((skill) => skill.id === selected)
        ? selected
        : rows[0]?.id || null;
      setSelected(next);
      if (next) {
        await loadDetail(next);
      } else {
        setDetail(null);
        setDraft("");
      }
      setError(null);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setLoading(false);
    }
  }

  useEffect(() => {
    void refresh();
  }, []);

  useEffect(() => {
    if (!selected) return;
    void loadDetail(selected);
  }, [selected]);

  const installedIds = useMemo(
    () => new Set(installed.map((row) => asText(row.id || row.skill_id || row.name))),
    [installed],
  );

  const pendingInstalled = useMemo(
    () =>
      installed.filter((row) => {
        const id = asText(row.id || row.skill_id || row.name);
        return id && !skills.some((skill) => skill.id === id);
      }),
    [installed, skills],
  );

  const workspaceCount = useMemo(
    () => skills.filter((skill) => (skill.source || "").startsWith("workspace")).length,
    [skills],
  );

  const builtinCount = useMemo(
    () => skills.filter((skill) => skill.source === "builtin" || !skill.source).length,
    [skills],
  );

  const filtered = useMemo(() => {
    const needle = query.trim().toLowerCase();
    return skills.filter((skill) => {
      const source = skill.source || "";
      const installed = installedIds.has(skill.id);
      const modeMatch =
        filterMode === "all" ||
        (filterMode === "workspace" && source.startsWith("workspace")) ||
        (filterMode === "builtin" && (source === "builtin" || !source)) ||
        (filterMode === "installed" && installed) ||
        (filterMode === "editable" && source.startsWith("workspace"));
      if (!modeMatch) return false;
      if (!needle) return true;
      const haystack = [
        skill.id,
        skill.title,
        skill.description,
        skill.source,
        skill.path,
        ...(skill.tags || []),
      ]
        .filter(Boolean)
        .join(" ")
        .toLowerCase();
      return haystack.includes(needle);
    });
  }, [filterMode, installedIds, query, skills]);

  const selectedSkill = detail || skills.find((skill) => skill.id === selected) || null;
  const dirty = Boolean(detail?.editable && draft !== (detail.skill_md || ""));
  const fileCount = detail?.files?.length || 0;
  const lock = lockStatus?.lock as Record<string, unknown> | undefined;
  const drift = lockStatus?.drift as Record<string, unknown> | undefined;
  const installPreview = useMemo(() => parseGithubInstallSource(source), [source]);

  async function saveSkill() {
    if (!detail || !detail.editable || !dirty) return;
    setBusy(true);
    try {
      const res = await clientApi.skillUpdate({
        skill_id: detail.id,
        skill_md: draft,
        reason: "dashboard skill editor",
      });
      if (!res.ok || !res.skill) {
        throw new Error(res.detail || res.error || "skill update failed");
      }
      setInfo(`Saved ${detail.id}`);
      setDetail(res.skill);
      setDraft(res.skill.skill_md || "");
      await refresh(detail.id);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  }

  async function createSkill() {
    if (!createName.trim()) {
      setError("skill name is required");
      return;
    }
    setBusy(true);
    try {
      const res = await clientApi.skillCreate({
        name: createName.trim(),
        description: createDescription.trim(),
        body: createBody.trim(),
      });
      if (!res.ok || !res.skill) {
        throw new Error(res.detail || res.error || "skill create failed");
      }
      setInfo(`Created ${res.skill.id}`);
      setCreating(false);
      setCreateName("");
      setCreateDescription("");
      setCreateBody("");
      await refresh(res.skill.id);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  }

  async function installSkill() {
    if (!source.trim()) {
      setError("source is required");
      return;
    }
    setBusy(true);
    try {
      const res = await clientApi.skillsInstall({
        source: source.trim(),
        kind: "auto",
        subdir: advancedInstall ? subdir.trim() || undefined : undefined,
        git_ref: advancedInstall ? gitRef.trim() || undefined : undefined,
      });
      const nextId = asText(res.skill_id || res.id || source);
      setInfo(`Install requested: ${nextId}`);
      setSource("");
      setSubdir("");
      setGitRef("");
      setAdvancedInstall(false);
      await refresh(nextId);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  }

  async function promoteSkill(skillId: string) {
    setBusy(true);
    try {
      await clientApi.skillsPromote(skillId);
      setInfo(`Promoted ${skillId}`);
      await refresh(skillId);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  }

  return (
    <PageBody>
      <PageHeader
        eyebrow="Skill playbooks"
        title="Skills"
        description="Browse SKILL.md playbooks, folder assets, workspace installs, and editable workspace skills."
        actions={
          <div className="flex items-center gap-2">
            <button
              type="button"
              className="btn btn-primary"
              onClick={() => setCreating(true)}
            >
              <PlusIcon size={14} />
              Create skill
            </button>
            <button
              type="button"
              className="btn btn-ghost"
              onClick={() => void refresh(selected)}
              disabled={loading}
            >
              <WrenchIcon size={14} />
              {loading ? "Refreshing..." : "Refresh"}
            </button>
          </div>
        }
      />
      <SectionTabs section="runtime" />

      {error ? <ErrorBanner error={error} /> : null}
      {info ? (
        <div className="rounded-lg border border-emerald-400/30 bg-emerald-500/10 px-3 py-2 text-[12px] text-emerald-200">
          {info}
        </div>
      ) : null}

      <div className="grid grid-cols-2 gap-3 lg:grid-cols-4">
        <Metric label="Loaded" value={skills.length} detail="runtime visible" />
        <Metric label="Workspace" value={workspaceCount} detail="editable roots" />
        <Metric label="Builtin" value={builtinCount} detail="repo playbooks" />
        <Metric label="Staged" value={pendingInstalled.length} detail="pending promote" />
      </div>

      <div className="grid grid-cols-1 gap-5 xl:grid-cols-[340px_minmax(0,1fr)_320px]">
        <div className="min-w-0">
        <Card title={`Playbooks (${skills.length})`} description="SKILL.md entries loaded by the runtime.">
          <div className="relative mb-3">
            <SearchIcon size={15} className="absolute left-2.5 top-2.5 text-ink-500" />
            <input
              className="input-dark pl-8"
              value={query}
              onChange={(e) => setQuery(e.target.value)}
              placeholder="Search skills"
            />
          </div>
          <div className="mb-3 flex flex-wrap gap-1.5">
            <FilterButton active={filterMode === "all"} onClick={() => setFilterMode("all")}>
              All
            </FilterButton>
            <FilterButton active={filterMode === "workspace"} onClick={() => setFilterMode("workspace")}>
              Workspace
            </FilterButton>
            <FilterButton active={filterMode === "builtin"} onClick={() => setFilterMode("builtin")}>
              Builtin
            </FilterButton>
            <FilterButton active={filterMode === "installed"} onClick={() => setFilterMode("installed")}>
              Installed
            </FilterButton>
            <FilterButton active={filterMode === "editable"} onClick={() => setFilterMode("editable")}>
              Editable
            </FilterButton>
          </div>
          {filtered.length ? (
            <div className="embedded-scroll max-h-[calc(100vh-310px)] min-h-[320px] space-y-1 pr-1">
              {filtered.map((skill) => {
                const active = selected === skill.id;
                return (
                  <button
                    key={skill.id}
                    type="button"
                    onClick={() => setSelected(skill.id)}
                    className={`w-full rounded-lg border px-3 py-2.5 text-left transition-colors ${
                      active
                        ? "border-brand-400/60 bg-brand-500/10"
                        : "border-white/5 bg-ink-950/30 hover:border-brand-500/25 hover:bg-white/5"
                    }`}
                  >
                    <div className="flex items-start gap-2.5">
                      <span className="mt-0.5 flex h-8 w-8 shrink-0 items-center justify-center rounded-lg border border-brand-500/15 bg-brand-500/10 text-brand-200">
                        <SkillsIcon size={15} />
                      </span>
                      <span className="min-w-0 flex-1">
                        <span className="block truncate font-mono text-[12px] text-ink-100">
                          {skill.id}
                        </span>
                        <span className="mt-1 block truncate text-[11px] leading-snug text-ink-400">
                          {skill.description || skill.title || "No description"}
                        </span>
                      </span>
                      <Pill tone={sourceTone(skill.source)}>{sourceLabel(skill.source)}</Pill>
                    </div>
                    <div className="mt-2 truncate font-mono text-[10px] text-ink-500">
                      {skill.path || "runtime registry"}
                    </div>
                  </button>
                );
              })}
            </div>
          ) : (
            <Empty title="No matching skills" subtitle="Try a different filter." />
          )}
        </Card>
        </div>

        <div className="min-w-0">
        <Card
          title={selectedSkill?.title || selectedSkill?.id || "Select a skill"}
          description={
            selectedSkill
              ? selectedSkill.description || selectedSkill.id
              : "Pick a skill to inspect its playbook and folder."
          }
          actions={
            selectedSkill ? (
              <div className="flex flex-wrap items-center justify-end gap-1.5">
                <Pill tone={sourceTone(selectedSkill.source)}>{sourceLabel(selectedSkill.source)}</Pill>
                {detail?.editable ? <Pill tone="ok">editable</Pill> : <Pill tone="neutral">read-only</Pill>}
              </div>
            ) : null
          }
        >
          {detailLoading ? (
            <Empty title="Loading skill detail" />
          ) : selectedSkill ? (
            <div className="min-w-0 space-y-4">
              <div className="grid grid-cols-2 gap-2 lg:grid-cols-4">
                <Metric label="Version" value={selectedSkill.version || "-"} />
                <Metric label="Files" value={fileCount} />
                <Metric label="Scripts" value={groupFiles(detail?.files, "script").length} />
                <Metric label="Actions" value={(selectedSkill.actions || []).length} detail="legacy bridge" />
              </div>

              <section className="rounded-lg border border-brand-500/10 bg-ink-950/30 p-3">
                <div className="grid gap-2 text-[12px] lg:grid-cols-2">
                  <div>
                    <div className="text-[10px] uppercase tracking-[0.16em] text-ink-500">Folder</div>
                    <div className="mt-1 break-all font-mono text-ink-200">
                      {detail?.relative_path || selectedSkill.path || "-"}
                    </div>
                  </div>
                  <div>
                    <div className="text-[10px] uppercase tracking-[0.16em] text-ink-500">Edit mode</div>
                    <div className="mt-1 text-ink-200">
                      {detail?.editable ? "workspace write enabled" : detail?.editable_reason || "read-only"}
                    </div>
                  </div>
                </div>
              </section>

              <section>
                <div className="mb-2 flex items-center justify-between gap-3">
                  <div className="text-[12px] font-medium text-ink-200">SKILL.md</div>
                  {detail?.editable ? (
                    <button
                      type="button"
                      className="btn btn-primary"
                      onClick={() => void saveSkill()}
                      disabled={busy || !dirty}
                    >
                      <EditIcon size={14} />
                      {busy ? "Saving..." : dirty ? "Save" : "Saved"}
                    </button>
                  ) : null}
                </div>
                {detail?.editable ? (
                  <textarea
                    className="input-dark min-h-[480px] max-w-full resize-y overflow-x-auto text-[12px] leading-relaxed"
                    value={draft}
                    onChange={(e) => setDraft(e.target.value)}
                    spellCheck={false}
                  />
                ) : (
                  <pre className="embedded-scroll max-h-[560px] max-w-full whitespace-pre-wrap break-words rounded-lg border border-brand-500/10 bg-ink-950/40 p-3 text-[12px] leading-relaxed text-ink-200">
                    {draft || firstLines(detail?.instructions || "", 40) || "No SKILL.md body available."}
                  </pre>
                )}
              </section>

              <section className="space-y-4">
                <FileGroup title="Playbook" files={groupFiles(detail?.files, "playbook")} />
                <FileGroup title="Scripts" files={groupFiles(detail?.files, "script")} />
                <FileGroup title="References" files={groupFiles(detail?.files, "reference")} />
                <FileGroup title="Templates" files={groupFiles(detail?.files, "template")} />
                <FileGroup title="Other files" files={groupFiles(detail?.files, "file")} />
                {!detail?.files?.length ? <Empty title="No folder files found" /> : null}
              </section>
            </div>
          ) : (
            <Empty title="No skill selected" />
          )}
        </Card>
        </div>

        <div className="min-w-0 space-y-5">
          <Card title="Add from repo" description="Paste a GitHub repo, GitHub folder link, local folder, or archive.">
            <label className="mb-3 block text-[12px] text-ink-300">
              Source URL or path
              <input
                className="input-dark mt-1"
                value={source}
                onChange={(e) => setSource(e.target.value)}
                placeholder="https://github.com/org/repo/tree/main/skills/name"
              />
            </label>
            {installPreview ? (
              <div className="mb-3 rounded-lg border border-brand-500/10 bg-ink-950/40 p-3 text-[11px] text-ink-300">
                <div className="text-[10px] uppercase tracking-[0.16em] text-ink-500">Detected GitHub source</div>
                <div className="mt-1 break-all font-mono text-ink-100">{installPreview.repo}</div>
                <div className="mt-2 grid grid-cols-1 gap-2">
                  <div className="min-w-0">
                    <div className="text-[10px] uppercase tracking-[0.16em] text-ink-500">Ref</div>
                    <div className="truncate font-mono text-ink-200">{installPreview.ref || "default"}</div>
                  </div>
                  <div className="min-w-0">
                    <div className="text-[10px] uppercase tracking-[0.16em] text-ink-500">Folder</div>
                    <div className="break-all font-mono text-ink-200">{installPreview.subdir || "repo root"}</div>
                  </div>
                </div>
              </div>
            ) : null}
            <details
              className="rounded-lg border border-brand-500/10 bg-ink-950/30"
              open={advancedInstall}
              onToggle={(e) => setAdvancedInstall(e.currentTarget.open)}
            >
              <summary className="cursor-pointer px-3 py-2 text-[12px] text-ink-300">
                Advanced overrides
              </summary>
              <div className="grid grid-cols-1 gap-2 border-t border-brand-500/10 p-3">
                <label className="text-[12px] text-ink-300">
                  Subdir
                  <input
                    className="input-dark mt-1"
                    value={subdir}
                    onChange={(e) => setSubdir(e.target.value)}
                    placeholder={installPreview?.subdir || "optional"}
                  />
                </label>
                <label className="text-[12px] text-ink-300">
                  Git ref
                  <input
                    className="input-dark mt-1"
                    value={gitRef}
                    onChange={(e) => setGitRef(e.target.value)}
                    placeholder={installPreview?.ref || "optional"}
                  />
                </label>
              </div>
            </details>
            <button
              type="button"
              className="btn btn-primary mt-3 w-full justify-center"
              onClick={installSkill}
              disabled={busy || !source.trim()}
            >
              <PlusIcon size={14} />
              {busy ? "Working..." : "Install"}
            </button>
          </Card>

          <Card title={`Staged installs (${pendingInstalled.length})`} description="Pending promotion into the active registry.">
            {pendingInstalled.length ? (
              <div className="embedded-list-scroll-sm space-y-2">
                {pendingInstalled.map((row, index) => {
                  const id = asText(row.id || row.skill_id || row.name);
                  return (
                    <div
                      key={`${id}_${index}`}
                      className="rounded-lg border border-brand-500/10 bg-ink-900/40 p-3"
                    >
                      <div className="font-mono text-[12px] text-ink-100">{id || "unknown"}</div>
                      <div className="mt-1 truncate text-[10px] text-ink-500">
                        {asText(row.path || row.source)}
                      </div>
                      {id ? (
                        <button
                          type="button"
                          className="btn btn-ghost mt-2"
                          onClick={() => promoteSkill(id)}
                          disabled={busy}
                        >
                          <CheckIcon size={14} />
                          Promote
                        </button>
                      ) : null}
                    </div>
                  );
                })}
              </div>
            ) : (
              <Empty title="No staged installs" subtitle="Installed skills are already active." />
            )}
          </Card>

          <Card title="Lockfile" description="Workspace installed-skill integrity snapshot.">
            <div className="grid grid-cols-2 gap-2 text-[12px]">
              <Metric label="Entries" value={asText(lock?.entries || lockEntries.length || 0)} />
              <Metric label="Problems" value={asText(drift?.problem_count || drift?.problems || 0)} />
            </div>
            {lockEntries.length ? (
              <details className="mt-3 rounded-lg border border-brand-500/10 bg-ink-950/30">
                <summary className="cursor-pointer px-3 py-2 text-[12px] text-ink-300">
                  Installed lock entries
                </summary>
                <div className="embedded-list-scroll-sm border-t border-brand-500/10">
                  {lockEntries.slice(0, 80).map((entry, index) => (
                    <div
                      key={`${asText(entry.skill_id)}_${index}`}
                      className="border-b border-brand-500/10 px-3 py-2 last:border-b-0"
                    >
                      <div className="font-mono text-[11px] text-ink-100">
                        {asText(entry.skill_id)}
                      </div>
                      <div className="truncate text-[10px] text-ink-500">
                        {asText(entry.version || entry.sha256)}
                      </div>
                    </div>
                  ))}
                </div>
              </details>
            ) : null}
          </Card>
        </div>
      </div>

      {creating ? (
        <div
          className="fixed inset-0 z-50 flex items-center justify-center bg-black/70 px-4 backdrop-blur-sm"
          onClick={(e) => {
            if (e.target === e.currentTarget) setCreating(false);
          }}
        >
          <div className="embedded-scroll max-h-[88vh] w-[760px] max-w-full rounded-xl border border-white/10 bg-bg-card shadow-glow">
            <div className="flex items-start justify-between gap-4 border-b border-brand-500/10 px-5 py-4">
              <div className="min-w-0">
                <h3 className="text-lg font-semibold text-ink-100">Create workspace skill</h3>
                <p className="mt-1 text-[12px] text-ink-400">
                  Creates <code className="text-fluid-300">workspace/skills/&lt;skill&gt;/SKILL.md</code>.
                </p>
              </div>
              <button
                type="button"
                className="icon-btn h-8 w-8 shrink-0"
                onClick={() => setCreating(false)}
                aria-label="Close"
              >
                <XIcon size={15} />
              </button>
            </div>
            <div className="space-y-4 px-5 py-4">
              <div className="grid grid-cols-1 gap-3 md:grid-cols-2">
                <label className="text-[12px] text-ink-300">
                  Name
                  <input
                    className="input-dark mt-1"
                    value={createName}
                    onChange={(e) => setCreateName(e.target.value)}
                    placeholder="clawcast wallet"
                    autoFocus
                  />
                </label>
                <label className="text-[12px] text-ink-300">
                  Description
                  <input
                    className="input-dark mt-1"
                    value={createDescription}
                    onChange={(e) => setCreateDescription(e.target.value)}
                    placeholder="When this skill should be used"
                  />
                </label>
              </div>
              <label className="block text-[12px] text-ink-300">
                Playbook body
                <textarea
                  className="input-dark mt-1 min-h-[280px] resize-y text-[12px] leading-relaxed"
                  value={createBody}
                  onChange={(e) => setCreateBody(e.target.value)}
                  placeholder="# Workflow&#10;&#10;Describe the steps, scripts, references, and expected output."
                  spellCheck={false}
                />
              </label>
              <div className="flex justify-end gap-2">
                <button
                  type="button"
                  className="btn btn-ghost"
                  onClick={() => setCreating(false)}
                  disabled={busy}
                >
                  <XIcon size={14} />
                  Cancel
                </button>
                <button
                  type="button"
                  className="btn btn-primary"
                  onClick={() => void createSkill()}
                  disabled={busy || !createName.trim()}
                >
                  <CheckIcon size={14} />
                  {busy ? "Creating..." : "Create"}
                </button>
              </div>
            </div>
          </div>
        </div>
      ) : null}
    </PageBody>
  );
}
