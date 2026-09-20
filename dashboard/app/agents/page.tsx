"use client";

import { useTranslations } from "next-intl";
import { useEffect, useId, useMemo, useRef, useState } from "react";
import * as Dialog from "@radix-ui/react-dialog";
import {
  Advanced,
  Card,
  Empty,
  ErrorBanner,
  PageBody,
  PageHeader,
  Pill,
} from "../../components/Page";
import { SectionTabs } from "../../components/SectionTabs";
import {
  AgentsIcon,
  CheckIcon,
  PlusIcon,
  SearchIcon,
  SkillsIcon,
  TrashIcon,
  WrenchIcon,
  XIcon,
} from "../../components/icons";
import { clientApi, type SkillSummary } from "../../lib/clientApi";
import { confirm as confirmDialog, toast } from "../../lib/dialogs";
import { Select } from "../../components/Select";
import { Markdown } from "../../components/chat/Markdown";

// Backend actually sends source: "workspace" | "default" | "default_profile"
// (describe_role marks alias roles), plus provider/model/execution_policy/
// prompt_excerpt that this page renders read-only in the Advanced panel.
type AgentExecutionPolicy = Record<string, unknown>;

type AgentSummary = {
  name: string;
  tier: string;
  allowed_skills: string[];
  source: string;
  description?: string;
  prompt_path?: string;
  prompt_excerpt?: string;
  provider?: string;
  model?: string;
  execution_policy?: AgentExecutionPolicy;
};

type AgentDetail = {
  name: string;
  tier: string;
  allowed_skills: string[];
  prompt: string;
  prompt_path?: string;
  source: string;
  persistent: boolean;
  provider?: string;
  model?: string;
  execution_policy?: AgentExecutionPolicy;
};

type Tier = "light" | "medium" | "high";

const DEFAULT_PROMPT_TEMPLATE = `# <role-name>

You are the <role-name> subagent. Describe the role's mission in one paragraph.

## Output schema

\`\`\`json
{
  "recommendation": "buy|sell|hold|reduce|avoid",
  "confidence": 0.0,
  "thesis": "..."
}
\`\`\`

## Constraints

- Read-only. Never call trading.* tools.
- Always cite the data source for any claim.
- If unsure, return recommendation="hold" with confidence < 0.4.
`;

type Translator = (key: string) => string;

function translateSource(t: Translator, source: string): string {
  const map: Record<string, string> = {
    default: "sourceDefault",
    default_profile: "sourceDefaultProfile",
    workspace: "sourceWorkspace",
  };
  const key = map[source];
  return key ? t(key) : source;
}

function translateTier(t: Translator, tier: string): string {
  const map: Record<string, string> = {
    high: "tierHigh",
    medium: "tierMedium",
    light: "tierLight",
  };
  const key = map[tier];
  return key ? t(key) : tier;
}

function tierOptions(t: Translator): Array<{ value: Tier; label: string }> {
  return [
    { value: "light", label: t("tierLight") },
    { value: "medium", label: t("tierMedium") },
    { value: "high", label: t("tierHigh") },
  ];
}

// Show an excerpt of the saved instructions, never an invented capability.
function promptSummary(prompt: string): string {
  const body = prompt.replace(/^---\s*\n[\s\S]*?\n---\s*\n/, "");
  const paragraph = body.split(/\n\s*\n/).map((block) => block.split("\n")
    .filter((line) => !/^(#{1,6}\s|```)/.test(line.trim())).join(" ").trim()).find(Boolean) || "";
  return paragraph.length > 360 ? `${paragraph.slice(0, 360).trimEnd()}…` : paragraph;
}

// Flattened execution_policy rows for the read-only Advanced panel.
// Keeps scalar/list values; drops empty entries and nested objects.
function policyRows(
  policy: AgentExecutionPolicy | undefined,
): Array<[string, string]> {
  if (!policy) return [];
  return Object.entries(policy)
    .filter(([key, value]) => {
      if (key === "locked_tier") return false;
      if (value == null || value === "") return false;
      if (Array.isArray(value)) return value.length > 0;
      if (typeof value === "object") return false;
      return true;
    })
    .map(
      ([key, value]) =>
        [
          key,
          Array.isArray(value) ? value.map(String).join(", ") : String(value),
        ] as [string, string],
    );
}

function AdvancedRow({ label, value }: { label: string; value: string }) {
  if (!value) return null;
  return (
    <div className="flex gap-2">
      <span className="w-32 shrink-0 text-ink-500">{label}</span>
      <span className="min-w-0 break-words font-mono text-ink-200">{value}</span>
    </div>
  );
}

export default function AgentsPage() {
  const t = useTranslations("agentsPage");
  const tCommon = useTranslations("common");
  const [items, setItems] = useState<AgentSummary[]>([]);
  const [selected, setSelected] = useState<string | null>(null);
  const [detail, setDetail] = useState<AgentDetail | null>(null);
  const [draft, setDraft] = useState<{
    name: string;
    tier: "light" | "medium" | "high";
    allowed_skills: string[];
    prompt: string;
  }>({
    name: "",
    tier: "medium",
    allowed_skills: [],
    prompt: DEFAULT_PROMPT_TEMPLATE,
  });
  const [skills, setSkills] = useState<SkillSummary[]>([]);
  const [agentQuery, setAgentQuery] = useState("");
  const [sourceFilter, setSourceFilter] = useState<"all" | "workspace" | "default">("all");
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState(false);
  const [fetchingDetail, setFetchingDetail] = useState(false);
  const [creating, setCreating] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [skillsError, setSkillsError] = useState<string | null>(null);
  const [editorDirty, setEditorDirty] = useState(false);
  const [editing, setEditing] = useState(false);
  const [detailError, setDetailError] = useState<string | null>(null);
  const [detailRevision, setDetailRevision] = useState(0);
  const [createError, setCreateError] = useState<string | null>(null);
  const nameInputRef = useRef<HTMLInputElement | null>(null);
  const detailPanelRef = useRef<HTMLDivElement | null>(null);
  const explicitSelection = useRef(false);

  function roleLabel(name: string) {
    const key = `roleNames.${name}`;
    return t.has(key) ? t(key) : name.replace(/_/g, " ");
  }

  function applySelected(name: string | null) {
    if (name !== selected) {
      setDetail(null);
      setDetailError(null);
      setEditing(false);
      setEditorDirty(false);
    }
    setSelected(name);
    // Keep the selection deep-linkable: /agents?agent=<name>.
    if (typeof window === "undefined") return;
    const url = new URL(window.location.href);
    if (name) url.searchParams.set("agent", name);
    else url.searchParams.delete("agent");
    window.history.replaceState(null, "", url);
  }

  async function selectAgent(name: string) {
    if (name === selected || busy) return;
    if (editorDirty) {
      const ok = await confirmDialog({
        message: t("discardDraftConfirm"),
        tone: "warning",
      });
      if (!ok) return;
    }
    explicitSelection.current = true;
    applySelected(name);
  }

  async function closeEditor() {
    if (editorDirty && !await confirmDialog({ message: t("discardEditsConfirm"), tone: "warning" })) return;
    setEditing(false);
    setEditorDirty(false);
  }

  function loadSkills() {
    setSkillsError(null);
    clientApi
      .skills()
      .then((res) => {
        const rows = (res.skills || []).slice();
        rows.sort((a, b) => a.id.localeCompare(b.id));
        setSkills(rows);
      })
      .catch(() => setSkillsError(t("skillsLoadFailed")));
  }

  async function refreshList(focus?: string | null) {
    setLoading(true);
    try {
      const res = await clientApi.agentsList();
      if (!res.ok) throw new Error(t("loadFailedTitle"));
      const list: AgentSummary[] = (res.roles || []).slice();
      list.sort((a, b) => {
        if (a.source !== b.source) return a.source === "workspace" ? -1 : 1;
        return a.name.localeCompare(b.name);
      });
      setItems(list);
      const next = focus && list.some((r) => r.name === focus)
        ? focus
        : list[0]?.name || null;
      applySelected(next);
      setError(null);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setLoading(false);
    }
  }

  useEffect(() => {
    const fromUrl =
      typeof window !== "undefined"
        ? new URLSearchParams(window.location.search).get("agent")
        : null;
    void refreshList(fromUrl);
    loadSkills();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  useEffect(() => {
    let cancelled = false;
    if (!selected) {
      setDetail(null);
      return () => {
        cancelled = true;
      };
    }
    setFetchingDetail(true);
    setDetail(null);
    setDetailError(null);
    clientApi
      .agentsGet(selected)
      .then((res) => {
        if (cancelled) return;
        if (!res.ok || !res.role) {
          setDetail(null);
          setDetailError(res.error || t("roleNotFound"));
        } else {
          setDetail(res.role);
        }
      })
      .catch((e) => {
        if (cancelled) return;
        setDetail(null);
        setDetailError(e instanceof Error ? e.message : String(e));
      })
      .finally(() => {
        if (!cancelled) setFetchingDetail(false);
      });
    return () => {
      cancelled = true;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [selected, detailRevision]);

  // Explicit selection on a stacked mobile layout brings the role into view.
  useEffect(() => {
    if (!explicitSelection.current) return;
    explicitSelection.current = false;
    if (window.matchMedia("(max-width: 1023px)").matches) detailPanelRef.current?.scrollIntoView({ block: "start" });
  }, [selected]);

  useEffect(() => {
    if (!editorDirty) return;
    const guard = (event: BeforeUnloadEvent) => { event.preventDefault(); event.returnValue = ""; };
    window.addEventListener("beforeunload", guard);
    return () => window.removeEventListener("beforeunload", guard);
  }, [editorDirty]);

  const counts = useMemo(() => {
    const ws = items.filter((i) => i.source === "workspace").length;
    return { workspace: ws, defaults: items.length - ws, total: items.length };
  }, [items]);

  const filteredItems = useMemo(() => {
    const needle = agentQuery.trim().toLowerCase();
    return items.filter((agent) => {
      if (sourceFilter === "workspace" && agent.source !== "workspace") return false;
      if (sourceFilter === "default" && agent.source === "workspace") return false;
      return !needle || [
        agent.name,
        roleLabel(agent.name),
        agent.tier,
        translateTier(t, agent.tier),
        agent.source,
        translateSource(t, agent.source),
        agent.prompt_excerpt,
        ...(agent.allowed_skills || []),
      ]
        .filter(Boolean)
        .join(" ")
        .toLowerCase()
        .includes(needle);
    });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [agentQuery, sourceFilter, items, t]);

  const selectedSummary = useMemo(
    () => items.find((i) => i.name === selected) ?? null,
    [items, selected],
  );

  const skillOptions = useMemo(
    () =>
      skills.map((skill) => ({
        id: skill.id,
        label: skill.title || skill.id,
        style: skill.style || skill.status || "",
      })),
    [skills],
  );

  async function persistDetailEdit(next: AgentDetail) {
    setBusy(true);
    try {
      const res = await clientApi.agentsSave({
        name: next.name,
        prompt: next.prompt,
        tier: (next.tier as "light" | "medium" | "high") || undefined,
        allowed_skills: next.allowed_skills,
      });
      if (!res.ok || !res.role) throw new Error(res.error || t("saveFailed"));
      setDetail(res.role);
      setEditing(false);
      setEditorDirty(false);
      toast({ message: t("savedInfo", { name: next.name }), tone: "ok" });
      await refreshList(next.name);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  }

  async function deleteAgent(name: string) {
    const ok = await confirmDialog({
      message: t("deleteConfirm", { name }),
      tone: "danger",
    });
    if (!ok) return;
    setBusy(true);
    try {
      const res = await clientApi.agentsDelete(name);
      if (!res.ok) throw new Error(res.error || t("deleteFailed"));
      toast({ message: t("deletedInfo", { name }), tone: "ok" });
      await refreshList();
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  }

  async function createAgent() {
    if (busy) return;
    const name = draft.name.trim();
    if (!/^[A-Za-z0-9_]+$/.test(name)) {
      setCreateError(t("nameValidation"));
      nameInputRef.current?.focus();
      return;
    }
    if (items.some((agent) => agent.name === name)) {
      setCreateError(t("nameExists"));
      nameInputRef.current?.focus();
      return;
    }
    setCreateError(null);
    setBusy(true);
    try {
      const res = await clientApi.agentsSave({
        name,
        prompt: draft.prompt.replaceAll("<role-name>", name),
        tier: draft.tier,
        allowed_skills: draft.allowed_skills
          .map((s) => s.trim())
          .filter(Boolean),
      });
      if (!res.ok || !res.role) throw new Error(res.error || t("saveFailed"));
      toast({ message: t("createdInfo", { name: draft.name }), tone: "ok" });
      setCreating(false);
      setDraft({
        name: "",
        tier: "medium",
        allowed_skills: [],
        prompt: DEFAULT_PROMPT_TEMPLATE,
      });
      await refreshList(res.role.name);
    } catch (e) {
      setCreateError(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  }

  return (
    <Dialog.Root open={creating} onOpenChange={(open) => { if (!busy) { setCreating(open); setCreateError(null); } }}>
    <PageBody>
      <PageHeader
        title={t("title")}
        description={t("description")}
        actions={
          <>
            <button
              type="button"
              className="btn btn-ghost cursor-pointer"
              onClick={() => refreshList(selected)}
              disabled={loading || busy || editorDirty}
            >
              <WrenchIcon size={14} />
              {loading ? tCommon("refreshing") : tCommon("refresh")}
            </button>
            <Dialog.Trigger asChild><button
              type="button"
              className="btn btn-primary cursor-pointer"
              disabled={busy || editorDirty}
              title={editorDirty ? t("saveDraftFirst") : undefined}
            >
              <PlusIcon size={14} />
              {t("newAgent")}
            </button></Dialog.Trigger>
          </>
        }
      />
      <SectionTabs section="runtime" />

      {error ? <ErrorBanner error={error} /> : null}
      {skillsError ? (
        <div className="flex items-center justify-between gap-3 rounded-lg border border-warn/30 bg-warn/10 px-3 py-2 text-[12px] text-warn">
          <span>{skillsError}</span>
          <button
            type="button"
            className="shrink-0 rounded-md border border-warn/40 px-2 py-0.5 text-[11px] hover:bg-warn/10 cursor-pointer"
            onClick={loadSkills}
          >
            {t("retry")}
          </button>
        </div>
      ) : null}

      <div className="agent-library grid grid-cols-1 items-start gap-5 lg:grid-cols-[300px_minmax(0,1fr)] xl:grid-cols-[320px_minmax(0,1fr)]">
        <Card
          title={loading && !items.length ? t("loadingAgents") : t("personasCount", { count: counts.total })}
          description={t("libraryHint")}
        >
          <div className="relative mb-3">
            <SearchIcon size={15} className="absolute left-2.5 top-2.5 text-ink-500" />
            <input
              className="input-dark pl-8"
              value={agentQuery}
              onChange={(e) => setAgentQuery(e.target.value)}
              placeholder={t("searchPlaceholder")}
              aria-label={t("searchPlaceholder")}
            />
          </div>
          <div className="mb-4 flex gap-1 rounded-lg bg-[color:var(--bg)] p-1" role="group" aria-label={t("filterSource")}>
            {(["all", "workspace", "default"] as const).map((source) => <button key={source} type="button" aria-pressed={sourceFilter === source} onClick={() => setSourceFilter(source)}
              className={`min-h-8 min-w-0 flex-1 rounded-md px-1.5 text-xs transition-colors ${sourceFilter === source ? "bg-[color:var(--card)] font-semibold text-[color:var(--text-base)] shadow-sm" : "text-[color:var(--text-muted)] hover:text-[color:var(--text-base)]"}`}>
              {t(source === "all" ? "filterAll" : source === "workspace" ? "filterCustom" : "filterBuiltIn")}
            </button>)}
          </div>
          {loading && items.length === 0 ? (
            <div className="space-y-2" aria-hidden>
              {[0, 1, 2, 3, 4].map((i) => (
                <div
                  key={i}
                  className="flex items-center gap-2.5 rounded-lg border border-[color:var(--line)] px-3 py-2.5"
                >
                  <div className="skeleton h-8 w-8 shrink-0" />
                  <div className="min-w-0 flex-1">
                    <div className="skeleton h-3 w-1/2" />
                    <div className="skeleton mt-2 h-2.5 w-1/3" />
                  </div>
                </div>
              ))}
            </div>
          ) : error && items.length === 0 ? (
            <div className="rounded-lg border border-[color:var(--line)] bg-ink-950/30 px-3 py-6 text-center">
              <p className="text-[13px] text-ink-200">{t("loadFailedTitle")}</p>
              <p className="mt-1 text-[11px] text-ink-500">{t("loadFailedHint")}</p>
              <button
                type="button"
                className="btn btn-primary mt-3 cursor-pointer"
                onClick={() => refreshList(selected)}
              >
                {t("retry")}
              </button>
            </div>
          ) : items.length === 0 ? (
            <Empty title={t("noAgentsYet")} subtitle={t("noAgentsYetHint")} />
          ) : filteredItems.length === 0 ? (
            <div className="text-center"><Empty title={t("noMatchingAgents")} subtitle={t("noMatchingAgentsHint")} /><button type="button" className="btn btn-ghost mb-3" onClick={() => { setAgentQuery(""); setSourceFilter("all"); }}>{t("clearFilters")}</button></div>
          ) : (
            <ul className="agent-library-list embedded-scroll max-h-64 space-y-1 pr-1 lg:max-h-[calc(100dvh-360px)] lg:min-h-[240px]" aria-label={t("title")}>
              {filteredItems.map((agent) => (
                <li key={`${agent.source}_${agent.name}`}>
                  <button
                    type="button"
                    aria-pressed={selected === agent.name}
                    disabled={busy}
                    className={`group w-full text-left rounded-lg border px-3 py-3 text-[13px] cursor-pointer transition-colors disabled:opacity-50 ${
                      selected === agent.name
                        ? "border-brand-400/60 bg-brand-500/10"
                        : "border-transparent hover:bg-[color:var(--card-hi)]"
                    }`}
                    onClick={() => selectAgent(agent.name)}
                  >
                    <div className="flex items-start gap-2.5">
                      <span
                        className="mt-0.5 flex h-9 w-9 shrink-0 items-center justify-center rounded-lg border border-[color:var(--line)] bg-[color:var(--bg)] text-[color:var(--text-muted)]"
                        aria-hidden
                      >
                        <AgentsIcon size={17} />
                      </span>
                      <span className="min-w-0 flex-1">
                        <span className="flex items-center justify-between gap-2">
                          <span className="truncate font-medium text-ink-100" title={agent.name}>
                            {roleLabel(agent.name)}
                          </span>
                        </span>
                        <span
                          className="mt-1 block truncate text-[11px] text-ink-500"
                          title={(agent.allowed_skills || []).join(", ") || undefined}
                        >
                          {t("skillsCount", { count: agent.allowed_skills.length })}
                          {" · "}{t(agent.source === "workspace" ? "filterCustom" : "filterBuiltIn")}
                        </span>
                      </span>
                    </div>
                  </button>
                </li>
              ))}
            </ul>
          )}
        </Card>

        <div ref={detailPanelRef} className="min-w-0 scroll-mt-4" role="region" aria-label={t("agentDetails")} data-testid="agent-details">
        <Card
          title={selected ? roleLabel(selected) : t("pickAgent")}
          description={
            selected
              ? selected
              : t("pickAgentHint")
          }
          actions={
            detail && !fetchingDetail ? <button type="button" className="btn btn-ghost" disabled={busy} onClick={() => { if (editing) void closeEditor(); else setEditing(true); }}>
              {editing ? t("backToOverview") : t("editConfiguration")}
            </button> : null
          }
        >
          {fetchingDetail || (selected && !detail && !detailError) ? (
            <div className="space-y-4 py-4" role="status" aria-label={t("loadingDetails")}><div className="skeleton h-4 w-2/5" /><div className="skeleton h-16 w-full" /><div className="skeleton h-10 w-3/4" /></div>
          ) : detailError ? (
            <div className="space-y-4 py-3"><ErrorBanner error={detailError} /><button type="button" className="btn btn-primary" onClick={() => setDetailRevision((value) => value + 1)}>{t("retry")}</button></div>
          ) : detail ? (
            <>
              {editing ? <fieldset disabled={busy} className="min-w-0">
                <AgentEditor
                  key={detail.name}
                  detail={detail}
                  busy={busy}
                  skillOptions={skillOptions}
                  onSave={persistDetailEdit}
                  onDirtyChange={setEditorDirty}
                  onCancel={closeEditor}
                />
              </fieldset> : <div className="space-y-6 py-2" data-testid="agent-overview">
                <div className="flex flex-wrap items-center gap-2">
                  <Pill tone={detail.source === "workspace" ? "brand" : "neutral"}>{t(detail.source === "workspace" ? "filterCustom" : "filterBuiltIn")}</Pill>
                  <span className="text-xs text-[color:var(--text-muted)]">{t("llmTier")}: {translateTier(t, detail.tier)}</span>
                </div>
                <section>
                  <h2 className="mb-2 text-sm font-semibold">{t("responsibility")}</h2>
                  <p className="max-w-[68ch] whitespace-pre-wrap break-words text-sm leading-7 text-[color:var(--text-muted)]">{selectedSummary?.description || promptSummary(detail.prompt) || t("noDescription")}</p>
                </section>
                <section>
                  <h2 className="mb-3 text-sm font-semibold">{t("preloadedSkills")}</h2>
                  <div className="flex flex-wrap gap-2">{detail.allowed_skills.length ? detail.allowed_skills.map((skill) => <Pill key={skill}>{skillOptions.find((item) => item.id === skill)?.label || skill}</Pill>) : <span className="text-sm text-[color:var(--text-muted)]">{t("noSkillsSelected")}</span>}</div>
                </section>
                <p className="border-t border-[color:var(--line)] pt-4 text-xs leading-6 text-[color:var(--text-muted)]">{t(detail.source === "workspace" ? "customHint" : "builtInHint")}</p>
                <Advanced title={t("viewInstructions")}><div className="max-h-96 overflow-auto"><Markdown>{detail.prompt}</Markdown></div></Advanced>
              </div>}
              <Advanced title={t("advancedTitle")}>
                <div className="space-y-1.5 text-[12px]">
                  <AdvancedRow label={t("storagePath")} value={detail.prompt_path || ""} />
                  <AdvancedRow
                    label={t("advProvider")}
                    value={detail.provider || selectedSummary?.provider || ""}
                  />
                  <AdvancedRow
                    label={t("advModel")}
                    value={detail.model || selectedSummary?.model || ""}
                  />
                  <AdvancedRow
                    label={t("advLockedTier")}
                    value={
                      detail.execution_policy?.locked_tier
                        ? translateTier(t, String(detail.execution_policy.locked_tier))
                        : ""
                    }
                  />
                  {policyRows(detail.execution_policy).map(([key, value]) => (
                    <AdvancedRow key={key} label={key} value={value} />
                  ))}
                  {selectedSummary?.prompt_excerpt ? (
                    <div className="pt-1">
                      <div className="text-[11px] text-ink-500">
                        {t("advPromptExcerpt")}
                      </div>
                      <p className="mt-1 max-h-24 overflow-y-auto whitespace-pre-wrap break-words rounded-lg border border-[color:var(--line)] bg-ink-950/40 p-2 font-mono text-[11px] leading-relaxed text-ink-400">
                        {selectedSummary.prompt_excerpt}
                      </p>
                    </div>
                  ) : null}
                </div>
              </Advanced>
              {detail.source === "workspace" && !editing ? <div className="mt-4 flex justify-end border-t border-[color:var(--line)] pt-3"><button type="button" className="btn btn-ghost text-danger" disabled={busy} onClick={() => deleteAgent(detail.name)}><TrashIcon size={14} />{tCommon("delete")}</button></div> : null}
            </>
          ) : (
            <Empty title={t("pickAgent")} subtitle={t("pickAgentHint")} />
          )}
        </Card>
        </div>
      </div>

      <Dialog.Portal>
        <Dialog.Overlay className="ui-modal-overlay" />
        <Dialog.Content className="ui-dialog agent-create-dialog" onOpenAutoFocus={(event) => { event.preventDefault(); nameInputRef.current?.focus(); }}>
          <div className="flex items-start justify-between gap-4 border-b border-[color:var(--line)] px-5 py-4 sm:px-6">
            <div>
              <Dialog.Title className="text-lg font-semibold">{t("createPersona")}</Dialog.Title>
              <Dialog.Description className="mt-1 text-sm leading-6 text-[color:var(--text-muted)]">{t("createHint")}</Dialog.Description>
            </div>
            <Dialog.Close disabled={busy} className="ui-icon-button shrink-0" aria-label={tCommon("close")}><XIcon size={17} /></Dialog.Close>
          </div>
          <form className="space-y-5 px-5 pt-5 sm:px-6" onSubmit={(event) => { event.preventDefault(); void createAgent(); }}>
            <fieldset disabled={busy} className="min-w-0 space-y-5">
              {createError ? <div id="agent-create-error" role="alert" className="rounded-lg border border-danger/30 bg-danger/10 p-3 text-sm text-danger">{createError}</div> : null}
              <div className="grid grid-cols-1 gap-4 sm:grid-cols-2">
                <label className="block text-sm">
                  {t("nameLabel")}
                  <input ref={nameInputRef} type="text" className="input-dark mt-2" placeholder="risk_critic" value={draft.name}
                    aria-describedby={createError ? "agent-create-error agent-name-help" : "agent-name-help"}
                    onChange={(event) => { setDraft({ ...draft, name: event.target.value }); setCreateError(null); }} />
                  <span id="agent-name-help" className="mt-1.5 block text-xs leading-5 text-[color:var(--text-muted)]">{t("nameHint")}</span>
                </label>
                <div className="text-sm">
                  <div className="mb-2">{t("llmTier")}</div>
                  <Select<Tier> value={draft.tier} onChange={(tier) => setDraft({ ...draft, tier })} options={tierOptions(t)} size="sm" ariaLabel={t("llmTier")} />
                </div>
              </div>
              <div className="text-sm">
                <div className="mb-2">{t("preloadedSkills")}</div>
                <SkillSelector selected={draft.allowed_skills} options={skillOptions} onChange={(allowed_skills) => setDraft({ ...draft, allowed_skills })} />
              </div>
              <label className="block text-sm">
                {t("promptBody")}
                <textarea className="input-dark mt-2 min-h-[200px] font-mono text-[13px] leading-6" rows={8} value={draft.prompt} onChange={(event) => setDraft({ ...draft, prompt: event.target.value })} />
              </label>
            </fieldset>
            <div className="sticky bottom-0 flex justify-end gap-2 border-t border-[color:var(--line)] bg-[color:var(--overlay-surface)] py-4">
              <Dialog.Close asChild><button type="button" className="btn btn-ghost" disabled={busy}>{tCommon("cancel")}</button></Dialog.Close>
              <button type="submit" className="btn btn-primary" disabled={busy || !draft.name.trim()}><PlusIcon size={14} />{busy ? tCommon("saving") : t("create")}</button>
            </div>
          </form>
        </Dialog.Content>
      </Dialog.Portal>
    </PageBody>
    </Dialog.Root>
  );
}

function AgentEditor({
  detail,
  busy,
  skillOptions,
  onSave,
  onDirtyChange,
  onCancel,
}: {
  detail: AgentDetail;
  busy: boolean;
  skillOptions: Array<{ id: string; label: string; style: string }>;
  onSave: (next: AgentDetail) => void | Promise<void>;
  onDirtyChange?: (dirty: boolean) => void;
  onCancel: () => void | Promise<void>;
}) {
  const t = useTranslations("agentsPage");
  const tCommon = useTranslations("common");
  const [tier, setTier] = useState(detail.tier || "medium");
  const [allowed, setAllowed] = useState<string[]>(detail.allowed_skills || []);
  const [prompt, setPrompt] = useState(detail.prompt || "");

  useEffect(() => {
    setTier(detail.tier || "medium");
    setAllowed(detail.allowed_skills || []);
    setPrompt(detail.prompt || "");
  }, [detail.name, detail.tier, detail.allowed_skills, detail.prompt]);

  const dirty =
    tier !== detail.tier ||
    allowed.join(",") !== (detail.allowed_skills || []).join(",") ||
    prompt !== detail.prompt;

  // Expose dirty to the parent so switching agents can guard the draft.
  // The cleanup resets the flag when the editor unmounts (agentsGet
  // failure clears detail, selection cleared) so a stale dirty=true can't
  // make the next list click prompt about a draft that no longer exists.
  useEffect(() => {
    onDirtyChange?.(dirty);
    return () => onDirtyChange?.(false);
  }, [dirty, onDirtyChange]);

  return (
    <div className="space-y-5" data-testid="agent-editor">
      <div className="grid grid-cols-1 xl:grid-cols-[220px_1fr] gap-3">
        <label className="text-[12px] text-ink-300 block">
          {t("llmTier")}
          <div className="mt-1">
            <Select
              value={tier}
              onChange={(value) => setTier(value)}
              options={tierOptions(t)}
              size="sm"
              ariaLabel={t("llmTier")}
            />
          </div>
        </label>
        <div className="text-[12px] text-ink-300">
          <div className="mb-1">{t("preloadedSkills")}</div>
          <SkillSelector
            selected={allowed}
            options={skillOptions}
            onChange={setAllowed}
          />
        </div>
      </div>

      <label className="text-[12px] text-ink-300 block">
        {t("promptBodyShort")}
        <textarea
          className="input-dark mt-2 w-full font-mono text-[13px] leading-6"
          rows={12}
          value={prompt}
          onChange={(e) => setPrompt(e.target.value)}
        />
      </label>

      <p className="text-xs leading-6 text-[color:var(--text-muted)]">{t("promptHelp")}</p>

      <div className="sticky bottom-0 flex flex-wrap items-center justify-end gap-2 border-t border-[color:var(--line)] bg-[color:var(--card)] py-3">
        <span className="mr-auto text-xs text-[color:var(--text-muted)]" role="status">{dirty ? t("unsavedChanges") : t("noChanges")}</span>
        <button type="button" className="btn btn-ghost" disabled={busy} onClick={() => void onCancel()}>{tCommon("cancel")}</button>
        <button
          type="button"
          className="btn btn-primary cursor-pointer"
          disabled={busy || !dirty}
          onClick={() =>
            onSave({
              ...detail,
              tier,
              allowed_skills: allowed,
              prompt,
              source: "workspace",
              persistent: true,
            })
          }
        >
          <CheckIcon size={14} />
          {busy ? tCommon("saving") : detail.source === "workspace" ? tCommon("save") : t("saveAsOverride")}
        </button>
      </div>
    </div>
  );
}

function SkillSelector({
  selected,
  options,
  onChange,
}: {
  selected: string[];
  options: Array<{ id: string; label: string; style: string }>;
  onChange: (next: string[]) => void;
}) {
  const t = useTranslations("agentsPage");
  const selectedSet = new Set(selected);
  const optionsId = useId();
  const [query, setQuery] = useState("");
  const [open, setOpen] = useState(false);
  const selectedOptions = options.filter((skill) => selectedSet.has(skill.id));
  const selectedMissing = selected.filter((id) => !options.some((skill) => skill.id === id));
  const visible = options
    .filter((skill) => {
      const needle = query.trim().toLowerCase();
      if (!needle) return true;
      return (
        skill.id.toLowerCase().includes(needle) ||
        skill.label.toLowerCase().includes(needle)
      );
    })
    .slice(0, 80);

  function toggle(id: string) {
    if (selectedSet.has(id)) {
      onChange(selected.filter((x) => x !== id));
    } else {
      onChange([...selected, id]);
    }
  }

  if (options.length === 0) {
    return (
      <div className="mt-1 rounded-lg border border-brand-500/10 bg-ink-900/40 p-3 text-[11px] text-ink-400">
        {t("noSkillsLoaded")}
      </div>
    );
  }

  return (
    <div className="mt-1 rounded-lg border border-[color:var(--line)] bg-[color:var(--bg)] p-2">
      <div className="flex min-h-8 items-center gap-2">
        <div className="min-w-0 flex-1">
          {selected.length ? (
            <div className="flex flex-wrap gap-1.5">
              {selectedOptions.slice(0, 6).map((skill) => (
                <button
                  key={skill.id}
                  type="button"
                  onClick={() => toggle(skill.id)}
                  aria-label={t("removeSkill", { name: skill.label })}
                  title={skill.id}
                  className="inline-flex min-h-8 max-w-[200px] items-center gap-1 rounded-md border border-[color:var(--line-hi)] bg-[color:var(--card)] px-2 py-1 text-xs text-[color:var(--text-base)]"
                >
                  <span className="truncate">{skill.label}</span>
                  <XIcon size={13} className="shrink-0 text-[color:var(--text-muted)]" />
                </button>
              ))}
              {selectedMissing.slice(0, 4).map((id) => (
                <button
                  key={id}
                  type="button"
                  onClick={() => toggle(id)}
                  aria-label={t("removeSkill", { name: id })}
                  className="inline-flex min-h-8 max-w-[200px] items-center gap-1 rounded-md border border-warn/30 bg-warn/10 px-2 py-1 text-xs text-warn"
                >
                  <span className="truncate">{id}</span>
                  <XIcon size={11} className="shrink-0" />
                </button>
              ))}
              {selected.length > selectedOptions.slice(0, 6).length + selectedMissing.slice(0, 4).length ? (
                <span className="rounded-md border border-brand-500/10 bg-white/[0.03] px-2 py-0.5 text-[10px] text-ink-400">
                  +{selected.length - selectedOptions.slice(0, 6).length - selectedMissing.slice(0, 4).length}
                </span>
              ) : null}
            </div>
          ) : (
            <span className="text-[11px] text-ink-500">{t("noSkillsSelected")}</span>
          )}
        </div>
        {selected.length ? (
          <button
            type="button"
            onClick={() => onChange([])}
            className="min-h-8 rounded-md px-2 py-1 text-xs text-[color:var(--text-muted)] hover:bg-[color:var(--card-hi)] hover:text-[color:var(--text-base)]"
          >
            {t("clear")}
          </button>
        ) : null}
        <button
          type="button"
          onClick={() => setOpen((value) => !value)}
          aria-expanded={open}
          aria-controls={optionsId}
          className="inline-flex min-h-8 items-center gap-1 rounded-md border border-[color:var(--line-hi)] bg-[color:var(--card)] px-2 py-1 text-xs text-[color:var(--text-base)] hover:bg-[color:var(--card-hi)]"
        >
          <PlusIcon size={12} />
          {t("add")}
        </button>
      </div>

      {open ? (
        <div id={optionsId} className="mt-2 border-t border-[color:var(--line)] pt-2">
          <div className="relative mb-2">
            <SearchIcon size={14} className="absolute left-2.5 top-2.5 text-ink-500" />
            <input
              className="input-dark pl-8"
              value={query}
              onChange={(e) => setQuery(e.target.value)}
              placeholder={t("searchSkillsPlaceholder")}
              aria-label={t("searchSkillsPlaceholder")}
            />
          </div>
          <div className="embedded-list-scroll-sm space-y-1">
            {visible.map((skill) => {
              const checked = selectedSet.has(skill.id);
              return (
                <button
                  key={skill.id}
                  type="button"
                  onClick={() => toggle(skill.id)}
                  role="checkbox"
                  aria-checked={checked}
                  aria-label={skill.label}
                  className={`flex w-full items-center gap-2 rounded-lg border px-2.5 py-2 text-left text-[11px] transition-colors ${
                    checked
                      ? "border-brand-400/50 bg-brand-500/10 text-[color:var(--text-base)]"
                      : "border-transparent text-[color:var(--text-muted)] hover:bg-[color:var(--card-hi)]"
                  }`}
                >
                  <span
                    className={`flex h-5 w-5 shrink-0 items-center justify-center rounded-md border ${
                      checked ? "border-brand-600 bg-brand-600 text-white" : "border-[color:var(--line-hi)]"
                    }`}
                  >
                    {checked ? <CheckIcon size={12} /> : null}
                  </span>
                  <span className="min-w-0 flex-1">
                    <span className="flex items-center gap-1.5">
                      <SkillsIcon size={14} className="shrink-0 text-[color:var(--text-muted)]" />
                      <span className="block truncate text-xs">{skill.label}</span>
                    </span>
                    {skill.label !== skill.id || skill.style ? (
                      <span className="block truncate text-[10px] text-ink-500">
                        {[skill.label !== skill.id ? skill.id : "", skill.style]
                          .filter(Boolean)
                          .join(" · ")}
                      </span>
                    ) : null}
                  </span>
                </button>
              );
            })}
            {!visible.length ? (
              <div className="rounded-lg border border-brand-500/10 bg-ink-950/30 px-3 py-6 text-center text-[11px] text-ink-500">
                {t("noSkillsMatch")}
              </div>
            ) : null}
          </div>
          <div className="mt-2 text-[10px] text-ink-500">
            {t("selectedShown", { selected: selected.length, shown: visible.length })}
          </div>
        </div>
      ) : null}
    </div>
  );
}
