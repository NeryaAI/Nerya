"use client";

import { useEffect, useMemo, useState, type ReactNode } from "react";
import { useTranslations } from "next-intl";
import { Card, ErrorBanner, PageBody, PageHeader, Pill } from "../../components/Page";
import { SwitchControl } from "../../components/SwitchControl";
import { CheckIcon, RefreshIcon, SearchIcon, SettingsIcon, SparkIcon } from "../../components/icons";
import { DEFAULT_SETTINGS, useUiSettings } from "../../lib/settings";
import {
  clientApi,
  type LlmProviderProfile,
  type LlmTierConfig,
  type MemoryVectorStatus,
} from "../../lib/clientApi";

const STANDARD_TIERS = ["light", "medium", "high"] as const;
const INTENT_TIER = "intent";
const ASSIGNMENT_TIERS = [...STANDARD_TIERS, INTENT_TIER] as const;
const KNOWN_LLM_PROVIDERS = [
  "openai",
  "anthropic",
  "openrouter",
  "gemini",
  "deepseek",
  "moonshot",
  "xai",
  "mistral",
  "together",
  "groq",
  "cerebras",
  "stepfun",
  "ollama",
  "compat",
  "bedrock",
] as const;

const DEFAULT_PROVIDER_BASE_URLS: Record<string, string> = {
  openai: "https://api.openai.com/v1",
  anthropic: "https://api.anthropic.com/v1",
  openrouter: "https://openrouter.ai/api/v1",
  gemini: "https://generativelanguage.googleapis.com/v1beta",
  deepseek: "https://api.deepseek.com/v1",
  moonshot: "https://api.moonshot.ai/v1",
  xai: "https://api.x.ai/v1",
  mistral: "https://api.mistral.ai/v1",
  together: "https://api.together.xyz/v1",
  groq: "https://api.groq.com/openai/v1",
  cerebras: "https://api.cerebras.ai/v1",
  stepfun: "https://api.stepfun.com/v1",
  ollama: "http://127.0.0.1:11434",
};

type ProviderOption = {
  provider: string;
  ready: boolean;
  base_url?: string | null;
};

function emptyTier(tier: string): LlmTierConfig {
  return { tier, provider: "", model: "", base_url: "", provider_key_ref: "" };
}

function ensureAssignmentTiers(rows: LlmTierConfig[]): LlmTierConfig[] {
  const byTier = new Map(rows.map((row) => [row.tier, row]));
  for (const tier of ASSIGNMENT_TIERS) {
    if (!byTier.has(tier)) byTier.set(tier, emptyTier(tier));
  }
  const primary = ASSIGNMENT_TIERS.map((tier) => byTier.get(tier)).filter(Boolean) as LlmTierConfig[];
  const extra = rows
    .filter((row) => !ASSIGNMENT_TIERS.includes(row.tier as typeof ASSIGNMENT_TIERS[number]))
    .sort((a, b) => a.tier.localeCompare(b.tier));
  return [...primary, ...extra];
}

function Row({
  label,
  desc,
  children,
}: {
  label: string;
  desc?: string;
  children: ReactNode;
}) {
  return (
    <div className="flex items-center justify-between gap-4 border-b border-brand-500/10 py-3 last:border-b-0">
      <div className="min-w-0 flex-1">
        <div className="text-[13px] text-ink-100">{label}</div>
        {desc ? <div className="mt-0.5 text-[11px] text-ink-400">{desc}</div> : null}
      </div>
      <div className="shrink-0">{children}</div>
    </div>
  );
}

function Field({
  label,
  hint,
  children,
}: {
  label: string;
  hint?: ReactNode;
  children: ReactNode;
}) {
  return (
    <label className="block text-[12px] text-ink-300">
      <span className="flex items-center justify-between gap-2">
        <span>{label}</span>
        {hint ? <span className="text-[10px] text-ink-500">{hint}</span> : null}
      </span>
      <div className="mt-1">{children}</div>
    </label>
  );
}

function Metric({
  label,
  value,
  detail,
  icon,
}: {
  label: string;
  value: ReactNode;
  detail?: ReactNode;
  icon: ReactNode;
}) {
  return (
    <div className="rounded-xl border border-brand-500/10 bg-ink-950/30 p-3">
      <div className="flex items-center justify-between gap-3">
        <span className="text-[10px] uppercase tracking-[0.16em] text-ink-500">
          {label}
        </span>
        <span className="text-brand-200">{icon}</span>
      </div>
      <div className="mt-2 text-xl font-semibold text-white">{value}</div>
      {detail ? <div className="mt-0.5 text-[11px] text-ink-500">{detail}</div> : null}
    </div>
  );
}

function Select({
  value,
  onChange,
  options,
}: {
  value: string;
  onChange: (v: string) => void;
  options: { value: string; label: string }[];
}) {
  return (
    <select
      value={value}
      onChange={(e) => onChange(e.target.value)}
      className="min-w-[180px] rounded-md border border-brand-500/20 bg-ink-900 px-3 py-1.5 text-xs text-ink-100 focus:border-brand-500/60 focus:outline-none"
    >
      {options.map((o) => (
        <option key={o.value} value={o.value}>
          {o.label}
        </option>
      ))}
    </select>
  );
}

function modelId(row: Record<string, unknown>): string {
  return String(row.id || row.model || row.name || row.model_id || "").trim();
}

function tierLabel(tier: string, t?: (k: string) => string): string {
  if (t) {
    if (tier === "light") return t("tierLight");
    if (tier === "medium") return t("tierMedium");
    if (tier === "high") return t("tierHigh");
    if (tier === INTENT_TIER) return t("tierIntent");
  } else {
    if (tier === "light") return "Low / light";
    if (tier === "medium") return "Medium";
    if (tier === "high") return "High";
    if (tier === INTENT_TIER) return "Intent recognition";
  }
  return tier;
}

function fingerprintConfig(
  defaultTier: string,
  intentTier: string,
  tiers: LlmTierConfig[],
  profiles: LlmProviderProfile[],
): string {
  return JSON.stringify({
    default_tier: defaultTier,
    intent_tier: intentTier,
    provider_profiles: profiles.map((row) => ({
      provider: row.provider,
      base_url: row.base_url || "",
      provider_key_ref: row.provider_key_ref || "",
    })),
    tiers: tiers.map((row) => ({
      tier: row.tier,
      provider: row.provider,
      model: row.model,
    })),
  });
}

export default function SettingsPage() {
  const [uiSettings, patchUi] = useUiSettings();
  const t = useTranslations("settings");
  const tProvider = useTranslations("settings.providerCard");
  const tModel = useTranslations("settings.modelCard");
  const tMemory = useTranslations("settings.memoryCard");
  const tDisplay = useTranslations("settings.displayCard");
  const tChart = useTranslations("settings.chartCard");
  const tCommon = useTranslations("common");
  const [venues, setVenues] = useState<{ name: string; label: string }[]>([]);
  const [providers, setProviders] = useState<ProviderOption[]>([]);
  const [providerProfiles, setProviderProfiles] = useState<LlmProviderProfile[]>([]);
  const [modelCatalog, setModelCatalog] = useState<Record<string, string[]>>({});
  const [defaultTier, setDefaultTier] = useState("medium");
  const [intentTier, setIntentTier] = useState("light");
  const [tierRows, setTierRows] = useState<LlmTierConfig[]>([]);
  const [loadedFingerprint, setLoadedFingerprint] = useState("");
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [refreshing, setRefreshing] = useState(false);
  const [providerDraft, setProviderDraft] = useState("openai");
  const [providerBaseUrlDraft, setProviderBaseUrlDraft] = useState("https://api.openai.com/v1");
  const [providerKeyDraft, setProviderKeyDraft] = useState("");
  const [discovering, setDiscovering] = useState(false);
  const [importing, setImporting] = useState(false);
  const [discoveredProvider, setDiscoveredProvider] = useState("");
  const [discoveredBaseUrl, setDiscoveredBaseUrl] = useState("");
  const [discoveredModels, setDiscoveredModels] = useState<Array<Record<string, unknown>>>([]);
  const [selectedModelIds, setSelectedModelIds] = useState<Set<string>>(new Set());
  const [memoryStatus, setMemoryStatus] = useState<MemoryVectorStatus | null>(null);
  const [memoryBusy, setMemoryBusy] = useState("");
  const [memoryQuery, setMemoryQuery] = useState("");
  const [memoryResults, setMemoryResults] = useState<Array<Record<string, unknown>>>([]);
  const [embProvider, setEmbProvider] = useState("openai");
  const [embModel, setEmbModel] = useState("text-embedding-3-small");
  const [embBaseUrl, setEmbBaseUrl] = useState("");
  const [embKeyRef, setEmbKeyRef] = useState("");
  const [milvusUri, setMilvusUri] = useState("~/.memsearch/milvus.db");
  const [milvusToken, setMilvusToken] = useState("");
  const [milvusCollection, setMilvusCollection] = useState("memsearch_chunks");
  const [error, setError] = useState<string | null>(null);
  const [info, setInfo] = useState<string | null>(null);

  async function loadMemoryStatus() {
    try {
      const next = await clientApi.memoryVectorStatus();
      setMemoryStatus(next);
      syncMemoryDrafts(next);
    } catch {
      setMemoryStatus(null);
    }
  }

  function syncMemoryDrafts(s: MemoryVectorStatus | null | undefined) {
    if (!s) return;
    if (s.embedding) {
      setEmbProvider(s.embedding.provider || "openai");
      setEmbModel(s.embedding.model || "text-embedding-3-small");
      setEmbBaseUrl(s.embedding.base_url || "");
      setEmbKeyRef(s.embedding.api_key_ref || "");
    }
    if (s.milvus) {
      setMilvusUri(s.milvus.uri || "~/.memsearch/milvus.db");
      setMilvusCollection(s.milvus.collection || "memsearch_chunks");
    }
  }

  async function loadModelConfig() {
    setLoading(true);
    try {
      const [cfg, providerRes, modelRes, venueRes, memoryRes] = await Promise.all([
        clientApi.llmConfig(),
        clientApi.llmProviders(),
        clientApi.llmModels(),
        clientApi.marketVenues().catch(() => ({ venues: [] })),
        clientApi.memoryVectorStatus().catch(() => null),
      ]);
      if (!cfg.ok) throw new Error(cfg.error || "cannot load llm config");
      const loadedDefaultTier = cfg.default_tier || "medium";
      const loadedIntentTier = cfg.intent_tier || "light";
      const loadedTiers = ensureAssignmentTiers(cfg.tiers || []);
      const profiles = cfg.provider_profiles || [];
      setDefaultTier(loadedDefaultTier);
      setIntentTier(loadedIntentTier);
      setTierRows(loadedTiers);
      setProviderProfiles(profiles);
      setLoadedFingerprint(fingerprintConfig(loadedDefaultTier, loadedIntentTier, loadedTiers, profiles));
      setProviders(
        (providerRes.providers || []).map((p) => ({
          provider: p.provider,
          ready: p.ready,
          base_url: p.base_url,
        })),
      );
      const nextCatalog: Record<string, string[]> = {};
      for (const [provider, rows] of Object.entries(modelRes.providers || {})) {
        nextCatalog[provider] = rows.map(modelId).filter(Boolean).slice(0, 400);
      }
      setModelCatalog(nextCatalog);
      setVenues((venueRes.venues || []).map((v) => ({ name: v.name, label: v.label })));
      setMemoryStatus(memoryRes);
      syncMemoryDrafts(memoryRes);
      setError(null);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setLoading(false);
    }
  }

  useEffect(() => {
    void loadModelConfig();
  }, []);

  const providerOptions = useMemo(() => {
    const names = new Set<string>(KNOWN_LLM_PROVIDERS);
    providers.forEach((p) => names.add(p.provider));
    providerProfiles.forEach((p) => names.add(p.provider));
    Object.keys(modelCatalog).forEach((p) => names.add(p));
    if (providerDraft) names.add(providerDraft.trim().toLowerCase());
    tierRows.forEach((r) => {
      if (r.provider) names.add(r.provider);
    });
    return Array.from(names).filter(Boolean).sort();
  }, [modelCatalog, providerDraft, providerProfiles, providers, tierRows]);

  const defaultTierOptions = useMemo(
    () => STANDARD_TIERS.map((tier) => ({ value: tier, label: tierLabel(tier, tModel) })),
    [tModel],
  );

  const providerProfileMap = useMemo(() => {
    const map = new Map<string, LlmProviderProfile>();
    for (const profile of providerProfiles) map.set(profile.provider, profile);
    return map;
  }, [providerProfiles]);

  const configuredTierCount = useMemo(
    () => tierRows.filter((row) => row.provider && row.model).length,
    [tierRows],
  );

  const readyProviderCount = useMemo(
    () => providers.filter((provider) => provider.ready).length,
    [providers],
  );

  const catalogModelCount = useMemo(
    () => Object.values(modelCatalog).reduce((total, rows) => total + rows.length, 0),
    [modelCatalog],
  );

  const currentFingerprint = useMemo(
    () => fingerprintConfig(defaultTier, intentTier, tierRows, providerProfiles),
    [defaultTier, intentTier, providerProfiles, tierRows],
  );

  const dirty = Boolean(loadedFingerprint && currentFingerprint !== loadedFingerprint);

  function setProviderDraftFromSelect(provider: string) {
    const profile = providerProfileMap.get(provider);
    const providerInfo = providers.find((p) => p.provider === provider);
    setProviderDraft(provider);
    setProviderBaseUrlDraft(
      profile?.base_url ||
      providerInfo?.base_url ||
      DEFAULT_PROVIDER_BASE_URLS[provider] ||
      "",
    );
    setProviderKeyDraft(profile?.provider_key_ref || "");
  }

  function upsertProviderProfile(profile: LlmProviderProfile) {
    setProviderProfiles((prev) => {
      const next = prev.filter((row) => row.provider !== profile.provider);
      return [...next, profile].sort((a, b) => a.provider.localeCompare(b.provider));
    });
  }

  function patchTier(index: number, patch: Partial<LlmTierConfig>) {
    setTierRows((rows) =>
      rows.map((row, i) => {
        if (i !== index) return row;
        const next = { ...row, ...patch };
        if (patch.provider) next.model = "";
        return next;
      }),
    );
  }

  function profilesForSave(): LlmProviderProfile[] {
    const profiles = [...providerProfiles];
    const provider = providerDraft.trim().toLowerCase();
    if (provider) {
      const idx = profiles.findIndex((row) => row.provider === provider);
      const next = {
        provider,
        base_url: providerBaseUrlDraft.trim() || DEFAULT_PROVIDER_BASE_URLS[provider] || "",
        ...(providerKeyDraft.trim()
          ? providerKeyDraft.trim().startsWith("vault://")
            ? { provider_key_ref: providerKeyDraft.trim() }
            : { provider_key: providerKeyDraft.trim() }
          : {}),
      };
      if (idx >= 0) profiles[idx] = { ...profiles[idx], ...next };
      else profiles.push(next);
    }
    return profiles.filter((row) => row.provider);
  }

  async function saveModelConfig() {
    setSaving(true);
    try {
      const intentRow = tierRows.find((row) => row.tier === INTENT_TIER);
      const nextIntentTier = intentRow?.provider && intentRow.model ? INTENT_TIER : intentTier || "light";
      const res = await clientApi.llmConfigSet({
        default_tier: defaultTier,
        intent_tier: nextIntentTier,
        providers: profilesForSave(),
        tiers: tierRows
          .filter((row) => row.tier.trim() && row.provider.trim() && row.model.trim())
          .map((row) => ({
            tier: row.tier.trim(),
            provider: row.provider.trim().toLowerCase(),
            model: row.model.trim(),
          })),
      });
      if (!res.ok) throw new Error(res.error || "save failed");
      const savedDefaultTier = res.default_tier || defaultTier;
      const savedIntentTier = res.intent_tier || nextIntentTier;
      const savedProfiles = res.provider_profiles || profilesForSave();
      const nextTiers = ensureAssignmentTiers(res.tiers || tierRows);
      setDefaultTier(savedDefaultTier);
      setIntentTier(savedIntentTier);
      setTierRows(nextTiers);
      setProviderProfiles(savedProfiles);
      setLoadedFingerprint(fingerprintConfig(savedDefaultTier, savedIntentTier, nextTiers, savedProfiles));
      setInfo("Model providers and assignments saved to workspace nerya.yml.");
      setError(null);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setSaving(false);
    }
  }

  async function discoverProviderModels() {
    const provider = providerDraft.trim().toLowerCase();
    if (!provider) {
      setError("Provider is required.");
      return;
    }
    setDiscovering(true);
    setError(null);
    setInfo(null);
    try {
      const key = providerKeyDraft.trim();
      const res = await clientApi.llmModelsDiscover({
        provider,
        base_url: providerBaseUrlDraft.trim() || undefined,
        ...(key
          ? key.startsWith("vault://")
            ? { provider_key_ref: key }
            : { provider_key: key }
          : {}),
      });
      if (!res.ok) {
        throw new Error(res.detail || res.error || "model discovery failed");
      }
      const rows = (res.models || []).filter((row) => modelId(row));
      const resolvedProvider = res.provider || provider;
      const resolvedBaseUrl = res.base_url || providerBaseUrlDraft.trim();
      setDiscoveredProvider(resolvedProvider);
      setDiscoveredBaseUrl(resolvedBaseUrl);
      setDiscoveredModels(rows);
      setSelectedModelIds(new Set(rows.map(modelId)));
      setProviderKeyDraft(res.provider_key_ref || "");
      if (resolvedBaseUrl) setProviderBaseUrlDraft(resolvedBaseUrl);
      upsertProviderProfile({
        provider: resolvedProvider,
        base_url: resolvedBaseUrl,
        provider_key_ref: res.provider_key_ref || key,
        has_key_ref: Boolean(res.provider_key_ref || key),
      });
      setProviders((prev) => {
        const next = prev.filter((row) => row.provider !== resolvedProvider);
        next.push({
          provider: resolvedProvider,
          ready: Boolean(res.provider_key_ref) || resolvedProvider === "ollama",
          base_url: resolvedBaseUrl || null,
        });
        return next.sort((a, b) => a.provider.localeCompare(b.provider));
      });
      setInfo(`Discovered ${rows.length} model(s) from ${resolvedProvider}. Select what to import.`);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setDiscovering(false);
    }
  }

  async function importSelectedModels() {
    const provider = discoveredProvider || providerDraft.trim().toLowerCase();
    const selected = discoveredModels.filter((row) => selectedModelIds.has(modelId(row)));
    if (!provider || selected.length === 0) {
      setError("Select at least one discovered model to import.");
      return;
    }
    setImporting(true);
    setError(null);
    setInfo(null);
    try {
      const res = await clientApi.llmModelsImport({
        provider,
        base_url: discoveredBaseUrl || providerBaseUrlDraft.trim() || undefined,
        models: selected,
      });
      if (!res.ok) throw new Error(res.error || "model import failed");
      const nextCatalog: Record<string, string[]> = {};
      for (const [name, rows] of Object.entries(res.providers || {})) {
        nextCatalog[name] = rows.map(modelId).filter(Boolean).slice(0, 400);
      }
      setModelCatalog(nextCatalog);
      setInfo(`Imported ${selected.length} model(s) for ${provider}. Use assignments below to bind them to tiers.`);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setImporting(false);
    }
  }

  function toggleDiscoveredModel(id: string, checked: boolean) {
    setSelectedModelIds((prev) => {
      const next = new Set(prev);
      if (checked) next.add(id);
      else next.delete(id);
      return next;
    });
  }

  async function refreshModels() {
    setRefreshing(true);
    try {
      const res = await clientApi.llmModelsRefresh();
      const nextCatalog: Record<string, string[]> = {};
      for (const [provider, rows] of Object.entries(res.providers || {})) {
        nextCatalog[provider] = rows.map(modelId).filter(Boolean).slice(0, 400);
      }
      setModelCatalog(nextCatalog);
      setInfo("Model catalog refreshed.");
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setRefreshing(false);
    }
  }

  async function runMemoryAction(action: string, fn: () => Promise<unknown>) {
    setMemoryBusy(action);
    setError(null);
    setInfo(null);
    try {
      const result = await fn();
      if (result && typeof result === "object" && "ok" in result && !(result as { ok?: boolean }).ok) {
        const body = result as { error?: string; detail?: string };
        throw new Error(body.detail || body.error || "memory vector action failed");
      }
      await loadMemoryStatus();
      return result;
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
      return null;
    } finally {
      setMemoryBusy("");
    }
  }

  async function searchMemory() {
    const query = memoryQuery.trim();
    if (!query) return;
    const result = await runMemoryAction("search", () =>
      clientApi.memoryVectorSearch({ query, top_k: 5 }),
    );
    if (result && typeof result === "object" && "results" in result) {
      setMemoryResults((result as { results?: Array<Record<string, unknown>> }).results || []);
    }
  }

  return (
    <PageBody>
      <PageHeader
        title={t("title")}
        description={t("description")}
        actions={
          <button
            type="button"
            className="btn btn-ghost"
            onClick={() => void loadModelConfig()}
            disabled={loading}
          >
            <RefreshIcon size={14} />
            {loading ? tCommon("loading") : tCommon("refresh")}
          </button>
        }
      />

      {error ? <ErrorBanner error={error} /> : null}
      {info ? (
        <div className="rounded-lg border border-emerald-400/30 bg-emerald-500/10 px-3 py-2 text-[12px] text-emerald-200">
          {info}
        </div>
      ) : null}

      <div className="grid grid-cols-1 xl:grid-cols-[1fr_360px] gap-5">
        <div className="space-y-5">
          <Card
            featured
            title={tProvider("title")}
            description={tProvider("description")}
            actions={
              discoveredModels.length ? (
                <Pill tone="brand">{tProvider("selectedCount", { selected: selectedModelIds.size, total: discoveredModels.length })}</Pill>
              ) : null
            }
          >
            <div className="grid grid-cols-1 gap-3 lg:grid-cols-[180px_1fr]">
              <Field label={tProvider("providerLabel")} hint={tProvider("providerHint")}>
                <select
                  className="input-dark font-mono"
                  value={providerDraft}
                  onChange={(e) => setProviderDraftFromSelect(e.target.value)}
                >
                  {providerOptions.map((p) => (
                    <option key={p} value={p}>
                      {p}
                    </option>
                  ))}
                </select>
              </Field>
              <Field label={tProvider("baseUrlLabel")} hint={tProvider("baseUrlHint")}>
                <input
                  className="input-dark font-mono"
                  value={providerBaseUrlDraft}
                  onChange={(e) => setProviderBaseUrlDraft(e.target.value)}
                  placeholder="https://api.openai.com/v1"
                />
              </Field>
              <Field label={tProvider("apiKeyLabel")} hint={tProvider("apiKeyHint")}>
                <input
                  className="input-dark font-mono"
                  value={providerKeyDraft}
                  onChange={(e) => setProviderKeyDraft(e.target.value)}
                  type={providerKeyDraft.startsWith("vault://") ? "text" : "password"}
                  placeholder={tProvider("apiKeyPlaceholder")}
                />
              </Field>
              <div className="flex items-end gap-2">
                <button
                  type="button"
                  className="btn btn-primary"
                  onClick={discoverProviderModels}
                  disabled={discovering || !providerDraft.trim()}
                >
                  <SearchIcon size={14} />
                  {discovering ? tProvider("fetching") : tProvider("fetchModels")}
                </button>
                <button
                  type="button"
                  className="btn btn-ghost"
                  onClick={() => setSelectedModelIds(new Set(discoveredModels.map(modelId)))}
                  disabled={!discoveredModels.length}
                >
                  {tProvider("selectAll")}
                </button>
                <button
                  type="button"
                  className="btn btn-ghost"
                  onClick={() => setSelectedModelIds(new Set())}
                  disabled={!discoveredModels.length}
                >
                  {tProvider("clear")}
                </button>
                <button
                  type="button"
                  className="btn btn-primary ml-auto"
                  onClick={importSelectedModels}
                  disabled={importing || selectedModelIds.size === 0}
                >
                  <CheckIcon size={14} />
                  {importing ? tProvider("importing") : tProvider("importSelected")}
                </button>
              </div>
            </div>

            {discoveredModels.length ? (
              <div className="embedded-list-scroll mt-4 rounded-lg border border-brand-500/10 bg-ink-950/35">
                {discoveredModels.map((row) => {
                  const id = modelId(row);
                  return (
                    <label
                      key={id}
                      className="flex items-center justify-between gap-3 border-b border-brand-500/10 px-3 py-2 text-xs last:border-b-0"
                    >
                      <span className="min-w-0">
                        <span className="block truncate font-mono text-ink-100">{id}</span>
                        {row.owned_by ? (
                          <span className="text-[10px] text-ink-500">{String(row.owned_by)}</span>
                        ) : null}
                      </span>
                      <input
                        type="checkbox"
                        checked={selectedModelIds.has(id)}
                        onChange={(e) => toggleDiscoveredModel(id, e.target.checked)}
                      />
                    </label>
                  );
                })}
              </div>
            ) : null}
          </Card>

          <Card
            featured
            title={
              <span className="inline-flex items-center gap-2">
                <SparkIcon size={16} className="text-fluid-300" />
                {tModel("title")}
              </span>
            }
            description={tModel("description")}
            actions={
              <div className="flex items-center gap-2">
                {dirty ? <Pill tone="warn">{tModel("unsaved")}</Pill> : null}
                <Pill tone="brand">{tModel("catalogModels", { count: catalogModelCount })}</Pill>
              </div>
            }
          >
            <div className="mb-4 grid grid-cols-2 gap-3 lg:grid-cols-4">
              <Metric
                label={tModel("defaultLabel")}
                value={<span className="font-mono">{defaultTier}</span>}
                detail={tModel("agentTurns")}
                icon={<SparkIcon size={16} />}
              />
              <Metric
                label={tModel("intentLabel")}
                value={<span className="font-mono">{intentTier}</span>}
                detail={tModel("classification")}
                icon={<SearchIcon size={16} />}
              />
              <Metric
                label={tModel("configuredLabel")}
                value={`${configuredTierCount}/${tierRows.length}`}
                detail={tModel("providerModel")}
                icon={<CheckIcon size={16} />}
              />
              <Metric
                label={tModel("providersLabel")}
                value={`${readyProviderCount}/${providers.length || providerOptions.length}`}
                detail={tModel("credentialReady")}
                icon={<SettingsIcon size={16} />}
              />
            </div>

            <div className="mb-4 flex flex-wrap items-end gap-3 rounded-xl border border-brand-500/10 bg-ink-900/40 p-3">
              <label className="text-[12px] text-ink-300">
                {tModel("defaultTier")}
                <Select
                  value={defaultTier}
                  onChange={setDefaultTier}
                  options={defaultTierOptions}
                />
              </label>
              <button
                type="button"
                className="btn btn-ghost"
                onClick={refreshModels}
                disabled={refreshing}
              >
                <RefreshIcon size={14} />
                {refreshing ? tCommon("refreshing") : tModel("refreshCatalog")}
              </button>
              <button
                type="button"
                className="btn btn-primary ml-auto"
                onClick={saveModelConfig}
                disabled={saving || tierRows.length === 0}
              >
                <CheckIcon size={14} />
                {saving ? tCommon("saving") : tModel("saveAssignments")}
              </button>
            </div>

            <div className="space-y-3">
              {tierRows.map((row, index) => {
                const models = modelCatalog[row.provider] || [];
                const provider = providers.find((p) => p.provider === row.provider);
                const profile = providerProfileMap.get(row.provider);
                const modelOptions = row.model && !models.includes(row.model)
                  ? [row.model, ...models]
                  : models;
                const laneKey = row.tier === INTENT_TIER
                  ? "laneIntent"
                  : row.tier === "light" ? "laneLight"
                  : row.tier === "medium" ? "laneMedium"
                  : row.tier === "high" ? "laneHigh"
                  : null;
                return (
                  <div
                    key={row.tier}
                    className="rounded-xl border border-brand-500/10 bg-ink-900/35 p-3.5"
                  >
                    <div className="mb-3 flex flex-wrap items-start justify-between gap-2">
                      <div>
                        <div className="font-mono text-[13px] text-ink-100">{tierLabel(row.tier, tModel)}</div>
                        <div className="mt-0.5 text-[11px] text-ink-500">
                          {laneKey ? tModel(laneKey) : `${row.tier} model lane`}
                        </div>
                      </div>
                      <div className="flex flex-wrap justify-end gap-1.5">
                        <Pill tone={provider?.ready || profile?.has_key_ref || profile?.provider_key_ref ? "ok" : "warn"}>
                          {provider?.ready || profile?.has_key_ref || profile?.provider_key_ref ? tModel("ready") : tModel("keyRefMissing")}
                        </Pill>
                        {row.tier === defaultTier ? <Pill tone="brand">{tModel("default")}</Pill> : null}
                        {row.tier === intentTier ? <Pill tone="brand">{tModel("intent")}</Pill> : null}
                      </div>
                    </div>
                    <div className="grid grid-cols-1 md:grid-cols-2 gap-3">
                      <Field label={tProvider("providerLabel")} hint={profile?.base_url || provider?.base_url || tModel("selectProvider")}>
                        <select
                          className="input-dark"
                          value={row.provider}
                          onChange={(e) => patchTier(index, { provider: e.target.value })}
                        >
                          <option value="">{tModel("selectProvider")}</option>
                          {providerOptions.map((p) => (
                            <option key={p} value={p}>
                              {p}
                            </option>
                          ))}
                        </select>
                      </Field>
                      <Field label={tProvider("modelLabel")} hint={models.length ? tModel("importedCount", { count: models.length }) : tModel("importModelsFirst")}>
                        <select
                          className="input-dark font-mono"
                          value={row.model}
                          onChange={(e) => {
                            patchTier(index, { model: e.target.value });
                            if (row.tier === INTENT_TIER && e.target.value) setIntentTier(INTENT_TIER);
                          }}
                          disabled={!row.provider}
                        >
                          <option value="">{tModel("selectModel")}</option>
                          {modelOptions.map((m) => (
                            <option key={m} value={m}>
                              {m}
                            </option>
                          ))}
                        </select>
                      </Field>
                    </div>
                  </div>
                );
              })}
            </div>
          </Card>
        </div>

        <div className="space-y-5">
          <Card title={tMemory("title")} description={tMemory("description")}>
            <Row
              label={tMemory("enableLabel")}
              desc={memoryStatus?.dependency_available ? tMemory("dependencyAvailable") : tMemory("dependencyMissing")}
            >
              <SwitchControl
                checked={Boolean(memoryStatus?.enabled)}
                disabled={memoryBusy === "toggle"}
                label={tMemory("enableLabel")}
                onCheckedChange={(v) => {
                  void runMemoryAction("toggle", () => clientApi.memoryVectorConfig({ enabled: v }));
                }}
              />
            </Row>
            <Row label={tMemory("backendLabel")} desc={memoryStatus?.paths?.join(", ") || "memory, strategies"}>
              <span className="font-mono text-xs text-ink-200">
                {memoryStatus?.backend || "memsearch"}
              </span>
            </Row>
            <div className="mt-3 space-y-3 rounded-lg border border-brand-500/10 bg-ink-950/30 p-3">
              <div className="flex items-center justify-between">
                <div className="text-[12px] font-medium text-ink-100">Embedding provider</div>
                <span className="text-[10px] uppercase tracking-wider text-ink-500">
                  {memoryStatus?.embedding?.has_key ? "key resolved" : "no key resolved"}
                </span>
              </div>
              <div className="grid grid-cols-2 gap-3">
                <Field label="Provider">
                  <Select
                    value={embProvider}
                    onChange={setEmbProvider}
                    options={[
                      { value: "openai", label: "OpenAI / OpenAI-compatible" },
                      { value: "google", label: "Google (Gemini)" },
                      { value: "voyage", label: "Voyage" },
                      { value: "ollama", label: "Ollama" },
                      { value: "local", label: "Local (sentence-transformers)" },
                    ]}
                  />
                </Field>
                <Field label="Model" hint="see provider docs">
                  <input
                    className="input-dark text-xs"
                    value={embModel}
                    onChange={(e) => setEmbModel(e.target.value)}
                    placeholder="text-embedding-3-small"
                  />
                </Field>
                <Field
                  label="Base URL"
                  hint={embProvider === "openai" ? "optional, e.g. https://ai.gitee.com/v1" : "provider-specific"}
                >
                  <input
                    className="input-dark text-xs"
                    value={embBaseUrl}
                    onChange={(e) => setEmbBaseUrl(e.target.value)}
                    placeholder="https://api.openai.com/v1"
                  />
                </Field>
                <Field label="API key ref" hint="reuse existing LLM provider key">
                  <div className="flex gap-2">
                    <Select
                      value={embKeyRef}
                      onChange={setEmbKeyRef}
                      options={[
                        { value: "", label: "— none —" },
                        ...providerProfiles
                          .filter((p) => (p.provider_key_ref || "").startsWith("vault://"))
                          .map((p) => ({
                            value: String(p.provider_key_ref || ""),
                            label: `${p.provider} (${String(p.provider_key_ref || "").replace("vault://", "")})`,
                          })),
                      ]}
                    />
                  </div>
                </Field>
              </div>
              <div className="text-[11px] text-ink-500">
                Milvus store (leave defaults to use the local file-backed vector store).
              </div>
              <div className="grid grid-cols-2 gap-3">
                <Field label="Milvus URI">
                  <input
                    className="input-dark text-xs"
                    value={milvusUri}
                    onChange={(e) => setMilvusUri(e.target.value)}
                    placeholder="~/.memsearch/milvus.db"
                  />
                </Field>
                <Field label="Collection">
                  <input
                    className="input-dark text-xs"
                    value={milvusCollection}
                    onChange={(e) => setMilvusCollection(e.target.value)}
                    placeholder="memsearch_chunks"
                  />
                </Field>
                <Field
                  label="Milvus token"
                  hint={memoryStatus?.milvus?.has_token ? "token stored" : "optional"}
                >
                  <input
                    className="input-dark text-xs"
                    type="password"
                    value={milvusToken}
                    onChange={(e) => setMilvusToken(e.target.value)}
                    placeholder={memoryStatus?.milvus?.has_token ? "•••••••• (unchanged)" : "optional"}
                  />
                </Field>
              </div>
              <div className="flex justify-end">
                <button
                  type="button"
                  className="btn btn-primary"
                  disabled={memoryBusy === "save"}
                  onClick={() =>
                    void runMemoryAction("save", async () => {
                      const res = await clientApi.memoryVectorConfig({
                        embedding: {
                          provider: embProvider,
                          model: embModel.trim(),
                          base_url: embBaseUrl.trim(),
                          api_key_ref: embKeyRef.trim(),
                        },
                        milvus: {
                          uri: milvusUri.trim(),
                          collection: milvusCollection.trim(),
                          // Only send token when user typed something: empty
                          // means "keep existing" so we don't overwrite a
                          // previously-saved secret with blanks.
                          ...(milvusToken ? { token: milvusToken } : {}),
                        },
                      });
                      if (res.ok) {
                        setInfo("Memory vector embedding settings saved.");
                        setMilvusToken("");
                      }
                      return res;
                    })
                  }
                >
                  Save embedding settings
                </button>
              </div>
            </div>
            <div className="mt-3 flex flex-wrap gap-2">
              <button
                type="button"
                className="btn btn-ghost"
                disabled={!memoryStatus?.enabled || memoryBusy === "install"}
                onClick={() =>
                  void runMemoryAction("install", async () => {
                    const res = await clientApi.memoryVectorInstall();
                    if (res.ok) setInfo("memsearch dependency installed.");
                    return res;
                  })
                }
              >
                {tMemory("installDeps")}
              </button>
              <button
                type="button"
                className="btn btn-ghost"
                disabled={!memoryStatus?.enabled || !memoryStatus?.dependency_available || memoryBusy === "reindex"}
                onClick={() =>
                  void runMemoryAction("reindex", async () => {
                    const res = await clientApi.memoryVectorReindex({ force: false });
                    if (res.ok) setInfo("Memory vector index rebuilt.");
                    return res;
                  })
                }
              >
                {tMemory("rebuildIndex")}
              </button>
              {memoryStatus?.watcher_running ? (
                <button
                  type="button"
                  className="btn btn-ghost"
                  disabled={memoryBusy === "stop"}
                  onClick={() => void runMemoryAction("stop", () => clientApi.memoryVectorStop())}
                >
                  {tMemory("stopWatcher")}
                </button>
              ) : (
                <button
                  type="button"
                  className="btn btn-ghost"
                  disabled={!memoryStatus?.enabled || !memoryStatus?.dependency_available || memoryBusy === "start"}
                  onClick={() => void runMemoryAction("start", () => clientApi.memoryVectorStart())}
                >
                  {tMemory("startWatcher")}
                </button>
              )}
            </div>
            <div className="mt-4 flex gap-2">
              <input
                className="input-dark text-xs"
                value={memoryQuery}
                onChange={(e) => setMemoryQuery(e.target.value)}
                placeholder={tMemory("searchPlaceholder")}
                disabled={!memoryStatus?.enabled || !memoryStatus?.dependency_available}
              />
              <button
                type="button"
                className="btn btn-primary"
                disabled={!memoryQuery.trim() || memoryBusy === "search"}
                onClick={() => void searchMemory()}
              >
                <SearchIcon size={14} />
                {tCommon("search")}
              </button>
            </div>
            {memoryResults.length ? (
              <div className="embedded-list-scroll mt-3 max-h-64 rounded-lg border border-brand-500/10 bg-ink-950/35">
                {memoryResults.map((row, idx) => (
                  <div key={idx} className="border-b border-brand-500/10 px-3 py-2 text-xs last:border-b-0">
                    <div className="font-mono text-ink-300">
                      {String(row.source || row.path || row.file || "memory")}
                    </div>
                    <div className="mt-1 text-ink-100">
                      {String(row.content || row.text || row.chunk || "").slice(0, 260)}
                    </div>
                  </div>
                ))}
              </div>
            ) : null}
          </Card>

          <Card title={tDisplay("title")} description={tDisplay("description")}>
            <Row label={tDisplay("timezone")} desc={tDisplay("timezoneDesc")}>
              <Select
                value={uiSettings.timezone}
                onChange={(v) => patchUi({ timezone: v as typeof uiSettings.timezone })}
                options={[
                  { value: "auto", label: "Auto" },
                  { value: "utc+0", label: "UTC+0" },
                  { value: "utc+8", label: "UTC+8 Shanghai" },
                  { value: "utc+9", label: "UTC+9 Tokyo" },
                  { value: "utc-5", label: "UTC-5 New York" },
                  { value: "utc-8", label: "UTC-8 Los Angeles" },
                ]}
              />
            </Row>
            <Row label={tDisplay("refreshCadence")} desc={tDisplay("refreshCadenceDesc")}>
              <Select
                value={String(uiSettings.refreshSeconds || 0)}
                onChange={(v) => patchUi({ refreshSeconds: Number(v) })}
                options={[
                  { value: "0", label: tDisplay("refreshOff") },
                  { value: "5", label: "5 sec" },
                  { value: "10", label: "10 sec" },
                  { value: "30", label: "30 sec" },
                  { value: "60", label: "1 min" },
                ]}
              />
            </Row>
            <Row label={tDisplay("compactMode")} desc={tDisplay("compactModeDesc")}>
              <SwitchControl
                checked={uiSettings.compact}
                label={tDisplay("compactMode")}
                onCheckedChange={(v) => patchUi({ compact: v })}
              />
            </Row>
          </Card>

          <Card title={tChart("title")} description={tChart("description")}>
            <Row label={tChart("venue")} desc={tChart("venueDesc")}>
              {venues.length ? (
                <Select
                  value={uiSettings.kline.venue}
                  onChange={(v) => patchUi({ kline: { ...uiSettings.kline, venue: v as typeof uiSettings.kline.venue } })}
                  options={venues.map((v) => ({ value: v.name, label: v.label }))}
                />
              ) : (
                <span className="text-[11px] text-ink-400">{tModel("noVenues")}</span>
              )}
            </Row>
            <Row label={tChart("symbol")} desc={tChart("symbolDesc")}>
              <input
                value={uiSettings.kline.symbol}
                onChange={(e) => patchUi({ kline: { ...uiSettings.kline, symbol: e.target.value.toUpperCase() } })}
                className="min-w-[180px] rounded-md border border-brand-500/20 bg-ink-900 px-3 py-1.5 font-mono text-xs text-ink-100 focus:border-brand-500/60 focus:outline-none"
                placeholder="BTCUSDT"
              />
            </Row>
            <Row label={tChart("timeframe")} desc={tChart("timeframeDesc")}>
              <Select
                value={uiSettings.kline.interval}
                onChange={(v) => patchUi({ kline: { ...uiSettings.kline, interval: v as typeof uiSettings.kline.interval } })}
                options={[
                  { value: "1m", label: "1m" },
                  { value: "5m", label: "5m" },
                  { value: "15m", label: "15m" },
                  { value: "1h", label: "1H" },
                  { value: "4h", label: "4H" },
                  { value: "1d", label: "1D" },
                ]}
              />
            </Row>
            <Row label={tChart("candles")} desc={tChart("candlesDesc")}>
              <Select
                value={String(uiSettings.kline.count)}
                onChange={(v) => patchUi({ kline: { ...uiSettings.kline, count: Number(v) } })}
                options={[
                  { value: "48", label: "48" },
                  { value: "96", label: "96" },
                  { value: "192", label: "192" },
                  { value: "288", label: "288" },
                ]}
              />
            </Row>
            <Row label={tChart("showVolume")} desc={tChart("showVolumeDesc")}>
              <SwitchControl
                checked={uiSettings.showVolume}
                label={tChart("showVolume")}
                onCheckedChange={(v) => patchUi({ showVolume: v })}
              />
            </Row>
            <Row label={tChart("resetSettings")} desc={tChart("resetSettingsDesc")}>
              <button className="btn btn-ghost" onClick={() => patchUi(DEFAULT_SETTINGS)}>
                <SettingsIcon size={14} />
                {tCommon("reset")}
              </button>
            </Row>
          </Card>
        </div>
      </div>
    </PageBody>
  );
}
