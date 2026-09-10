"use client";

import { useTranslations } from "next-intl";
import { useEffect, useMemo, useRef, useState } from "react";
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

// Backend actually sends source: "workspace" | "default" | "default_profile"
// (describe_role marks alias roles), plus provider/model/execution_policy/
// prompt_excerpt that this page renders read-only in the Advanced panel.
type AgentExecutionPolicy = Record<string, unknown>;

type AgentSummary = {
  name: string;
  tier: string;
  allowed_skills: string[];
  source: string;
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

// List avatar: first letter of the agent name, tinted by tier so the
// column reads as a spectrum instead of 40 identical violet icons.
function tierAvatarClass(tier: string): string {
  if (tier === "high") {
    return "border-magenta-400/30 bg-magenta-500/10 text-magenta-400";
  }
  if (tier === "light") {
    return "border-ink-500/20 bg-ink-900/60 text-ink-300";
  }
  return "border-brand-400/30 bg-brand-500/10 text-brand-200";
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
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState(false);
  const [fetchingDetail, setFetchingDetail] = useState(false);
  const [creating, setCreating] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [skillsError, setSkillsError] = useState<string | null>(null);
  const [editorDirty, setEditorDirty] = useState(false);
  const nameInputRef = useRef<HTMLInputElement | null>(null);

  function applySelected(name: string | null) {
    setSelected(name);
    // Keep the selection deep-linkable: /agents?agent=<name>.
    if (typeof window === "undefined") return;
    const url = new URL(window.location.href);
    if (name) url.searchParams.set("agent", name);
    else url.searchParams.delete("agent");
    window.history.replaceState(null, "", url);
  }

  async function selectAgent(name: string) {
    if (name === selected) return;
    if (editorDirty) {
      const ok = await confirmDialog({
        message: t("discardDraftConfirm"),
        tone: "warning",
      });
      if (!ok) return;
    }
    applySelected(name);
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
    clientApi
      .agentsGet(selected)
      .then((res) => {
        if (cancelled) return;
        if (!res.ok || !res.role) {
          setDetail(null);
          setError(res.error || t("roleNotFound"));
        } else {
          setDetail(res.role);
          setError(null);
        }
      })
      .catch((e) => {
        if (cancelled) return;
        setError(e instanceof Error ? e.message : String(e));
      })
      .finally(() => {
        if (!cancelled) setFetchingDetail(false);
      });
    return () => {
      cancelled = true;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [selected]);

  // Create-modal a11y: Escape closes, focus moves to the first field on
  // open (mirrors lib/dialogs.tsx DialogShell behaviour).
  useEffect(() => {
    if (!creating) return;
    const onKey = (event: KeyboardEvent) => {
      if (event.key === "Escape") setCreating(false);
    };
    document.addEventListener("keydown", onKey);
    nameInputRef.current?.focus();
    return () => document.removeEventListener("keydown", onKey);
  }, [creating]);

  const counts = useMemo(() => {
    const ws = items.filter((i) => i.source === "workspace").length;
    return { workspace: ws, defaults: items.length - ws, total: items.length };
  }, [items]);

  const filteredItems = useMemo(() => {
    const needle = agentQuery.trim().toLowerCase();
    if (!needle) return items;
    return items.filter((agent) =>
      [
        agent.name,
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
        .includes(needle),
    );
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [agentQuery, items]);

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
    if (!/^[A-Za-z0-9_]+$/.test(draft.name)) {
      setError(t("nameValidation"));
      return;
    }
    setBusy(true);
    try {
      const res = await clientApi.agentsSave({
        name: draft.name,
        prompt: draft.prompt,
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
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  }

  return (
    <PageBody>
      <PageHeader
        eyebrow={t("eyebrow")}
        title={t("title")}
        description={t("description")}
        actions={
          <>
            <button
              type="button"
              className="btn btn-ghost cursor-pointer"
              onClick={() => refreshList(selected)}
              disabled={loading}
            >
              <WrenchIcon size={14} />
              {loading ? tCommon("refreshing") : tCommon("refresh")}
            </button>
            <button
              type="button"
              className="btn btn-primary cursor-pointer"
              onClick={() => setCreating(true)}
              disabled={busy}
            >
              <PlusIcon size={14} />
              {t("newAgent")}
            </button>
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

      <div className="grid grid-cols-1 lg:grid-cols-[320px_1fr] gap-5">
        <Card
          title={t("personasCount", { count: counts.total })}
          description={t("personasCountsDesc", { workspace: counts.workspace, defaults: counts.defaults })}
        >
          <div className="relative mb-3">
            <SearchIcon size={15} className="absolute left-2.5 top-2.5 text-ink-500" />
            <input
              className="input-dark pl-8"
              value={agentQuery}
              onChange={(e) => setAgentQuery(e.target.value)}
              placeholder={t("searchPlaceholder")}
            />
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
            <Empty title={t("noMatchingAgents")} subtitle={t("noMatchingAgentsHint")} />
          ) : (
            <ul className="embedded-scroll max-h-[calc(100vh-260px)] min-h-[260px] space-y-1 pr-1">
              {filteredItems.map((agent) => (
                <li key={`${agent.source}_${agent.name}`}>
                  <button
                    type="button"
                    className={`group w-full text-left rounded-lg border px-3 py-2.5 text-[12px] cursor-pointer transition-colors duration-200 ${
                      selected === agent.name
                        ? "border-brand-400/60 bg-brand-500/10"
                        : "border-brand-500/10 bg-ink-950/30 hover:border-brand-500/25 hover:bg-brand-500/[0.04]"
                    }`}
                    onClick={() => selectAgent(agent.name)}
                  >
                    <div className="flex items-start gap-2.5">
                      <span
                        className={`mt-0.5 flex h-8 w-8 shrink-0 items-center justify-center rounded-lg border font-mono text-[13px] font-semibold ${tierAvatarClass(agent.tier)}`}
                        aria-hidden
                      >
                        {agent.name.charAt(0).toUpperCase()}
                      </span>
                      <span className="min-w-0 flex-1">
                        <span className="flex items-center justify-between gap-2">
                          <span className="truncate font-mono text-[12px] text-ink-100">
                            {agent.name}
                          </span>
                        </span>
                        <span
                          className="mt-1 block truncate text-[11px] text-ink-500"
                          title={(agent.allowed_skills || []).join(", ") || undefined}
                        >
                          {translateTier(t, agent.tier)}
                          {" · "}
                          {t("skillsCount", { count: agent.allowed_skills.length })}
                          {agent.source !== "workspace"
                            ? ` · ${translateSource(t, agent.source)}`
                            : ""}
                        </span>
                      </span>
                    </div>
                  </button>
                </li>
              ))}
            </ul>
          )}
        </Card>

        <Card
          title={detail ? <span className="font-mono">{detail.name}</span> : t("pickAgent")}
          description={
            detail
              ? t("detailDescription", {
                  source: translateSource(t, detail.source),
                  tier: translateTier(t, detail.tier),
                  count: detail.allowed_skills.length,
                })
              : t("pickAgentHint")
          }
          actions={
            detail && detail.source === "workspace" ? (
              <button
                type="button"
                className="btn btn-ghost cursor-pointer text-danger"
                onClick={() => deleteAgent(detail.name)}
                disabled={busy}
              >
                <TrashIcon size={14} />
                {tCommon("delete")}
              </button>
            ) : null
          }
        >
          {detail ? (
            <>
              {/* Detail fetch no longer touches `busy`: while it is in
                  flight the previous editor dims instead of faking
                  "Saving..." on every button. */}
              <div
                className={`transition-opacity ${
                  fetchingDetail ? "pointer-events-none opacity-60" : ""
                }`}
              >
                <AgentEditor
                  key={detail.name}
                  detail={detail}
                  busy={busy}
                  skillOptions={skillOptions}
                  onSave={persistDetailEdit}
                  onDirtyChange={setEditorDirty}
                />
              </div>
              <Advanced title={t("advancedTitle")}>
                <div className="space-y-1.5 text-[12px]">
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
            </>
          ) : (
            <Empty title={t("pickAgent")} subtitle={t("pickAgentHint")} />
          )}
        </Card>
      </div>

      {creating ? (
        <div
          className="fixed inset-0 z-50 flex items-center justify-center bg-black/70 backdrop-blur-sm"
          onClick={(e) => {
            if (e.target === e.currentTarget) setCreating(false);
          }}
        >
          <div className="embedded-scroll w-[760px] max-w-[92vw] max-h-[88vh] rounded-2xl border border-brand-500/20 bg-bg-card shadow-glow" role="dialog" aria-modal="true" aria-label={t("createPersona")}>
            <div className="flex items-start justify-between gap-4 border-b border-brand-500/10 px-6 py-4">
              <div className="flex items-start gap-3">
                <span className="mt-0.5 flex h-10 w-10 shrink-0 items-center justify-center rounded-xl border border-brand-500/20 bg-brand-500/10 text-brand-200">
                  <AgentsIcon size={18} />
                </span>
                <div>
                  <h3 className="text-lg font-semibold text-ink-100">{t("createPersona")}</h3>
                  <p className="mt-1 text-[12px] text-ink-400">
                    {t.rich("createPersonaPath", {
                      code: (chunks) => <code className="text-fluid-300">{chunks}</code>,
                    })}
                  </p>
                </div>
              </div>
              <button
                type="button"
                className="icon-btn h-8 w-8"
                onClick={() => setCreating(false)}
                aria-label={tCommon("close")}
              >
                <XIcon size={15} />
              </button>
            </div>

            <div className="space-y-4 px-6 py-5">
              <div className="grid grid-cols-1 md:grid-cols-2 gap-3">
                <label className="text-[12px] text-ink-300">
                  {t("nameLabel")}
                  <input
                    type="text"
                    ref={nameInputRef}
                    className="input-dark mt-1 w-full"
                    placeholder="risk_critic"
                    value={draft.name}
                    onChange={(e) =>
                      setDraft({ ...draft, name: e.target.value })
                    }
                  />
                </label>
                <label className="text-[12px] text-ink-300 block">
                  {t("llmTier")}
                  <div className="mt-1">
                    <Select<Tier>
                      value={draft.tier}
                      onChange={(value) => setDraft({ ...draft, tier: value })}
                      options={tierOptions(t)}
                      size="sm"
                      ariaLabel={t("llmTier")}
                    />
                  </div>
                </label>
              </div>

              <div className="block text-[12px] text-ink-300">
                <div className="mb-1">{t("preloadedSkills")}</div>
                <SkillSelector
                  selected={draft.allowed_skills}
                  options={skillOptions}
                  onChange={(allowed_skills) =>
                    setDraft({ ...draft, allowed_skills })
                  }
                />
              </div>

              <label className="block text-[12px] text-ink-300">
                {t("promptBody")}
                <textarea
                  className="input-dark mt-1 min-h-[320px] w-full font-mono text-[12px]"
                  rows={14}
                  value={draft.prompt}
                  onChange={(e) => setDraft({ ...draft, prompt: e.target.value })}
                />
              </label>

              <div className="flex justify-end gap-2">
                <button
                  type="button"
                  className="btn btn-ghost cursor-pointer"
                  onClick={() => setCreating(false)}
                  disabled={busy}
                >
                  <XIcon size={14} />
                  {tCommon("cancel")}
                </button>
                <button
                  type="button"
                  className="btn btn-primary cursor-pointer"
                  onClick={createAgent}
                  disabled={busy || !draft.name.trim()}
                >
                  <CheckIcon size={14} />
                  {busy ? tCommon("saving") : t("create")}
                </button>
              </div>
            </div>
          </div>
        </div>
      ) : null}
    </PageBody>
  );
}

function AgentEditor({
  detail,
  busy,
  skillOptions,
  onSave,
  onDirtyChange,
}: {
  detail: AgentDetail;
  busy: boolean;
  skillOptions: Array<{ id: string; label: string; style: string }>;
  onSave: (next: AgentDetail) => void | Promise<void>;
  onDirtyChange?: (dirty: boolean) => void;
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
    <div className="space-y-3">
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
          className="input-dark mt-1 w-full font-mono text-[12px]"
          rows={20}
          value={prompt}
          onChange={(e) => setPrompt(e.target.value)}
        />
      </label>

      {detail.prompt_path ? (
        <p className="text-[11px] text-ink-500 font-mono">
          {detail.prompt_path}
        </p>
      ) : null}

      <div className="flex justify-end gap-2">
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
    <div className="mt-1 rounded-lg border border-brand-500/10 bg-ink-900/40 p-2">
      <div className="flex min-h-8 items-center gap-2">
        <div className="min-w-0 flex-1">
          {selected.length ? (
            <div className="flex flex-wrap gap-1.5">
              {selectedOptions.slice(0, 6).map((skill) => (
                <button
                  key={skill.id}
                  type="button"
                  onClick={() => toggle(skill.id)}
                  className="inline-flex max-w-[160px] items-center gap-1 rounded-md border border-brand-400/30 bg-brand-500/10 px-2 py-0.5 font-mono text-[10px] text-brand-100"
                >
                  <span className="truncate">{skill.id}</span>
                  <XIcon size={11} className="shrink-0 text-brand-200" />
                </button>
              ))}
              {selectedMissing.slice(0, 4).map((id) => (
                <button
                  key={id}
                  type="button"
                  onClick={() => toggle(id)}
                  className="inline-flex max-w-[160px] items-center gap-1 rounded-md border border-warn/30 bg-warn/10 px-2 py-0.5 font-mono text-[10px] text-warn"
                >
                  <span className="truncate">{id}</span>
                  <XIcon size={11} className="shrink-0" />
                </button>
              ))}
              {selected.length > 6 ? (
                <span className="rounded-md border border-brand-500/10 bg-white/[0.03] px-2 py-0.5 text-[10px] text-ink-400">
                  +{selected.length - 6}
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
            className="rounded-md border border-brand-500/10 px-2 py-1 text-[10px] text-ink-400 hover:border-brand-500/30 hover:text-white"
          >
            {t("clear")}
          </button>
        ) : null}
        <button
          type="button"
          onClick={() => setOpen((value) => !value)}
          className="inline-flex items-center gap-1 rounded-md border border-brand-500/20 px-2 py-1 text-[11px] text-brand-200 hover:bg-brand-500/10"
        >
          <PlusIcon size={12} />
          {t("add")}
        </button>
      </div>

      {open ? (
        <div className="mt-2 border-t border-brand-500/10 pt-2">
          <div className="relative mb-2">
            <SearchIcon size={14} className="absolute left-2.5 top-2.5 text-ink-500" />
            <input
              className="input-dark pl-8"
              value={query}
              onChange={(e) => setQuery(e.target.value)}
              placeholder={t("searchSkillsPlaceholder")}
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
                  className={`flex w-full items-center gap-2 rounded-lg border px-2.5 py-2 text-left text-[11px] transition-colors ${
                    checked
                      ? "border-brand-400/50 bg-brand-500/15 text-white"
                      : "border-brand-500/10 bg-ink-950/40 text-ink-300 hover:bg-brand-500/[0.04]"
                  }`}
                >
                  <span
                    className={`flex h-5 w-5 shrink-0 items-center justify-center rounded-md border ${
                      checked ? "border-brand-300 bg-brand-500" : "border-ink-600"
                    }`}
                  >
                    {checked ? <CheckIcon size={12} /> : null}
                  </span>
                  <span className="min-w-0 flex-1">
                    <span className="flex items-center gap-1.5">
                      <SkillsIcon size={12} className="shrink-0 text-brand-200" />
                      <span className="block truncate font-mono text-[11px]">{skill.id}</span>
                    </span>
                    {skill.label !== skill.id || skill.style ? (
                      <span className="block truncate text-[10px] text-ink-500">
                        {[skill.label !== skill.id ? skill.label : "", skill.style]
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
