"use client";

import { DesktopSettings } from "./settings/DesktopSettings";
import { copy as i18nCopy } from "../lib/i18n";

import { useLocale,useTranslations } from "next-intl";
import { useEffect,useMemo,useRef,useState,type ReactNode } from "react";
import { clearStoredAuthToken,redirectToLogin,setStoredAuthToken } from "../lib/auth";
import {
clientApi,
invalidateReadCache,
type AuthStatus,
type LlmProviderProfile,
type LlmRouteConfig,
type LlmTierConfig,
type NetworkDashboardStatus,
type NetworkProxyPreset,
type NetworkProxyStatus,
type NetworkTunnelsStatus,
type OAuthProviderStatus,
type RuntimeEnvVar,
type SecretRef,
type TunnelProviderConfig,
type TunnelProviderStatus
} from "../lib/clientApi";
import { confirm,toast } from "../lib/dialogs";
import { ChoiceSelect } from "./ChoiceSelect";
import { GatewayChannelsPanel } from "./GatewayChannelsPanel";
import { LearningMemoryPanel } from "./LearningMemoryPanel";
import { Advanced,Card,ErrorBanner,PageBody,PageHeader,Pill } from "./Page";
import { Select as PortalSelect } from "./Select";
import { SwitchControl } from "./SwitchControl";
import { TermTip } from "./TermTip";
import { WorkspaceSyncPanel } from "./WorkspaceSyncPanel";
import { CheckIcon,PlusIcon,RefreshIcon,SearchIcon,SettingsIcon,SparkIcon,TrashIcon } from "./icons";
import { DataServiceSettings } from "./settings/DataServiceSettings";
import { InterfaceSettings } from "./settings/InterfaceSettings";
import McpSettingsPanel from "./settings/McpSettingsPanel";
import { PrimaryModelSettings } from "./settings/PrimaryModelSettings";
import { ModelRoutePriority } from "./settings/ModelRoutePriority";
import { ProviderManagementPanel } from "./settings/ProviderManagementPanel";
import { editableRoute, explicitRoute, tierPolicy, connectionPatch, moveModelRoute, type ModelRouteState, type ModelProfileState } from "./settings/modelSettingsState";
import { SavedModelTest } from "./SetupReadinessCard";
import { SearchSettings } from "./settings/SearchSettings";
import { Field,Metric,Row,CompactSelect as Select } from "./settings/SettingsFields";

const STANDARD_TIERS = ["light", "medium", "high"] as const;
const INTENT_TIER = "intent";
const ASSIGNMENT_TIERS = [...STANDARD_TIERS, INTENT_TIER] as const;
const DEFAULT_MODEL_CONTEXT_WINDOW = 1_048_576;
const MIN_MODEL_CONTEXT_WINDOW = 4_096;
const MAX_MODEL_CONTEXT_WINDOW = 16_777_216;
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

type ProviderCatalogEntry = {
  id: string;
  name: string;
  api_mode: string;
  auth_type: string;
  base_url: string;
  env_keys: string[];
  base_url_env: string;
  aliases: string[];
  description: string;
  family: string;
  extra: Record<string, unknown>;
};

type OAuthProviderInfo = {
  id: string;
  display_name: string;
  cli_name: string;
  cli_paths: string[];
  env_keys: string[];
  description: string;
};

// Two presets the operator can pick when adding a custom provider that
// is not in the catalogue. Each maps to one of the two adapter-shaped
// API modes the router can dispatch through.
const CUSTOM_PROVIDER_KINDS = [
  { id: "openai_compat", api_mode: "chat_completions", placeholder: "https://api.example.com/v1" },
  { id: "anthropic_compat", api_mode: "anthropic_messages", placeholder: "https://api.example.com/v1" },
] as const;
type CustomProviderKind = typeof CUSTOM_PROVIDER_KINDS[number]["id"];

const SETTINGS_TABS = ["models", "access", "runtime", "mcp", "capabilityGates", "envvault", "search", "memory", "interface"] as const;
type SettingsTabKey = typeof SETTINGS_TABS[number];
const DEFAULT_NO_PROXY = "127.0.0.1,localhost,::1";

// Gateway channel forms now live exclusively in
// ``components/GatewayChannelsPanel.tsx`` (shared draft helpers in
// ``lib/gatewayDraft.ts``) — this file no longer carries a duplicate
// draft builder.

type TunnelDraft = {
  enabled: boolean;
  target: "dashboard" | "api" | "custom";
  target_url: string;
  mode: string;
  cloudflare_mode: "quick" | "token";
  token: string;
  token_ref: string;
  public_hostname: string;
  region: string;
};

function emptyTunnelDraft(config?: Partial<TunnelProviderConfig>, fallbackMode = "public"): TunnelDraft {
  const target = config?.target === "api" || config?.target === "custom" ? config.target : "dashboard";
  return {
    enabled: Boolean(config?.enabled),
    target,
    target_url: config?.target_url || "",
    mode: config?.mode || fallbackMode,
    cloudflare_mode: config?.cloudflare_mode === "token" ? "token" : "quick",
    token: "",
    token_ref: config?.token_ref || "",
    public_hostname: config?.public_hostname || "",
    region: config?.region || "",
  };
}

function tunnelTargetHint(target: string, status: NetworkTunnelsStatus | null): string {
  if (target === "api") return status?.auth.api_target || "http://127.0.0.1:18317";
  if (target === "custom") return "";
  return status?.auth.dashboard_target || "http://127.0.0.1:18380";
}

function splitRouteValues(value: string | string[] | undefined): string[] {
  if (!value) return [];
  const raw = Array.isArray(value) ? value : value.split(/[\n,]/);
  const seen = new Set<string>();
  const out: string[] = [];
  for (const item of raw) {
    const text = String(item || "").trim();
    if (!text || seen.has(text)) continue;
    seen.add(text);
    out.push(text);
  }
  return out;
}

function emptyRoute(): ModelRouteState {
  return {
    provider: "",
    model: "",
    models: [],
    reasoning_effort: "",
    context_window: DEFAULT_MODEL_CONTEXT_WINDOW,
    base_url: "",
    provider_key_ref: "",
    provider_key_refs: [],
    provider_key: "",
    provider_keys: [],
    kind: "",
    declared: {},
    effective: { provider: "", model: "", context_window: DEFAULT_MODEL_CONTEXT_WINDOW, reasoning_effort: "" },
  };
}

function routesOf(row: LlmTierConfig): ModelRouteState[] {
  if (Array.isArray(row.routes) && row.routes.length > 0) {
    return row.routes.map((route) => ({
      ...route,
      provider: route.provider || "",
      model: route.model || splitRouteValues(route.models).join(", "),
      models: route.models || splitRouteValues(route.model),
      reasoning_effort: route.reasoning_effort ?? row.reasoning_effort ?? "",
      context_window: route.context_window ?? row.context_window ?? DEFAULT_MODEL_CONTEXT_WINDOW,
      base_url: route.base_url || "",
      provider_key_ref: route.provider_key_ref || splitRouteValues(route.provider_key_refs).join(", "),
      provider_key_refs: route.provider_key_refs || splitRouteValues(route.provider_key_ref),
      provider_key: route.provider_key || splitRouteValues(route.provider_keys).join(", "),
      provider_keys: route.provider_keys || splitRouteValues(route.provider_key),
      has_key_ref: route.has_key_ref,
      kind: route.kind || "",
      provider_native_web_search: route.provider_native_web_search,
    }));
  }
  if (row.provider || row.model || row.base_url || row.provider_key_ref) {
    return [{
      provider: row.provider || "",
      model: row.model || splitRouteValues(row.models).join(", "),
      models: row.models || splitRouteValues(row.model),
      reasoning_effort: row.reasoning_effort || "",
      context_window: row.context_window ?? DEFAULT_MODEL_CONTEXT_WINDOW,
      base_url: row.base_url || "",
      provider_key_ref: row.provider_key_ref || "",
      provider_key_refs: splitRouteValues(row.provider_key_ref),
      provider_key: row.provider_key || "",
      provider_keys: splitRouteValues(row.provider_key),
      has_key_ref: row.has_key_ref,
      kind: "chat_completions",
      provider_native_web_search: row.provider_native_web_search,
    }];
  }
  return [emptyRoute()];
}

function tierWithRoutes(row: LlmTierConfig): LlmTierConfig {
  const routes = routesOf(row);
  const first = routes[0] || emptyRoute();
  return {
    ...row,
    provider: first.provider || row.provider || "",
    model: first.model || row.model || "",
    context_window: first.context_window ?? row.context_window ?? DEFAULT_MODEL_CONTEXT_WINDOW,
    base_url: first.base_url || "",
    provider_key_ref: first.provider_key_ref || "",
    routes,
  };
}

function emptyTier(tier: string): LlmTierConfig {
  return {
    ...{ declared: {} },
    tier,
    provider: "",
    model: "",
    context_window: DEFAULT_MODEL_CONTEXT_WINDOW,
    base_url: "",
    provider_key_ref: "",
    reasoning_effort: "",
    routes: [emptyRoute()],
  };
}

// Canonical reasoning-effort levels, in display order. Mirrors the
// Python catalogue at ``nerya.llm.provider_catalog.REASONING_EFFORT_LEVELS``;
// the backend echoes this list under ``LlmConfigResponse.reasoning_levels``
// so we hydrate from there at runtime, but keep this fallback so the
// dropdown is still rendered before the first /llm/config response lands.
const FALLBACK_REASONING_LEVELS = [
  "none",
  "minimal",
  "low",
  "medium",
  "high",
  "extra_high",
] as const;

// Locale-independent display label for an unknown level value coming
// from a future backend version. Title-cases the level id so the
// dropdown stays readable even without a translation file update.
function prettifyReasoningLevel(level: string): string {
  if (!level) return "";
  return level
    .split("_")
    .map((part) => (part ? part[0].toUpperCase() + part.slice(1) : ""))
    .join(" ");
}

function ensureAssignmentTiers(rows: LlmTierConfig[]): LlmTierConfig[] {
  rows = rows.map(row => ({ ...row, routes: row.routes?.map(editableRoute) }));
  const byTier = new Map(rows.map((row) => [row.tier, tierWithRoutes(row)]));
  for (const tier of ASSIGNMENT_TIERS) {
    if (!byTier.has(tier)) byTier.set(tier, emptyTier(tier));
  }
  const primary = ASSIGNMENT_TIERS.map((tier) => byTier.get(tier)).filter(Boolean) as LlmTierConfig[];
  const extra = rows
    .filter((row) => !ASSIGNMENT_TIERS.includes(row.tier as typeof ASSIGNMENT_TIERS[number]))
    .map(tierWithRoutes)
    .sort((a, b) => a.tier.localeCompare(b.tier));
  return [...primary, ...extra];
}

function isSettingsTabKey(value: string): value is SettingsTabKey {
  return (SETTINGS_TABS as readonly string[]).includes(value);
}

function settingsPanelId(tab: SettingsTabKey) {
  return `settings-panel-${tab}`;
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
      routes: routesOf(row).map((route) => ({
        provider: route.provider || "",
        model: route.model || "",
        reasoning_effort: route.reasoning_effort || "",
        context_window: route.context_window,
        provider_key: route.provider_key,
        provider_native_web_search: route.provider_native_web_search,
        base_url: route.base_url || "",
        provider_key_ref: route.provider_key_ref || "",
        provider_key_env: route.provider_key_env || "",
        kind: route.kind || "",
      })),
      reasoning_effort: row.reasoning_effort || "",
    })),
  });
}

// Settings routes and onboarding share these sections. Legacy memory props
// resolve to built-in memory; old capability links lead to developer diagnostics.
export type ForceSectionKey =
  | "memory"
  | "search"
  | "envvault"
  | "access"
  | "models"
  | "runtime"
  | "gateway"
  | "capabilityGates"
  | "mcp";

export interface SettingsPageProps {
  /** @deprecated Use `forceSection="memory"` instead. */
  forceMemoryOnly?: boolean;
  forceSection?: ForceSectionKey;
  /**
   * Optional content rendered immediately after the section-mode
   * PageHeader and before any other content. Used by host pages to
   * inject its Engines/Session tab strip without duplicating the
   * section-page chrome.
   */
  topBanner?: ReactNode;
  /**
   * When `forceSection === "models"` and `compactLlm === true`, hide
   * the tier-assignment matrix. Used by the `/setup?mode=quick` wizard
   * which only needs the provider + API-key + import workflow on a
   * single screen; the assignment matrix is intentionally deferred to
   * `nerya setup` (full wizard) so the casual user isn't confronted
   * with 4 tier rows on first contact.
   */
  compactLlm?: boolean;
  /** Focused onboarding: the host owns the single submit/continue button. */
  setupMode?: boolean;
  onSetupComplete?: () => void;
  onSetupBusyChange?: (busy: boolean) => void;
  /**
   * Hide the built-in PageHeader. Layout-only escape hatch for hosts
   * that render their own title chrome around the panel — the setup
   * wizard mounts these sections inside its own step card, so a second
   * "Settings / Memory …" header inside the card read as duplication.
   * Standalone routes must NOT set this (they rely on the header).
   */
  hideHeader?: boolean;
}

function DiagnosticsLink() {
  const zh = useLocale().startsWith("zh");
  return <a className="btn btn-ghost" href="/advanced">{i18nCopy(zh, "copy.components_SettingsWorkspace.001")}</a>;
}

export function SettingsWorkspace(props: SettingsPageProps) {
  if (props.forceSection === "capabilityGates") return <DiagnosticsLink />;
  if (props.forceSection === "memory" || props.forceMemoryOnly) return <LearningMemoryPanel />;
  return <SettingsWorkspaceContent {...props} />;
}

function SettingsWorkspaceContent({
  forceMemoryOnly = false,
  forceSection: forceSectionProp,
  topBanner,
  compactLlm = false,
  hideHeader = false,
  setupMode = false,
  onSetupComplete,
  onSetupBusyChange,
}: SettingsPageProps) {
  // Normalise the two equivalent prop shapes into a single value the
  // rest of the component reads. `forceSection` wins if both are set.
  const forceSection: ForceSectionKey | undefined =
    forceSectionProp ?? (forceMemoryOnly ? "memory" : undefined);
  const inSectionMode = forceSection !== undefined;
  const zh = useLocale().startsWith("zh");
  const text = (key: string, values?: Record<string, unknown>) => i18nCopy(zh, key, values);
  const t = useTranslations("settings");
  const tSetup = useTranslations("setupWizard");
  const [setupError, setSetupError] = useState("");
  const tUi = useTranslations("ui");
  const tProvider = useTranslations("settings.providerCard");
  const tModel = useTranslations("settings.modelCard");
  const tWebSearchPage = useTranslations("webSearchPage");
  const tEnvVaultPage = useTranslations("envVaultPage");
  const tEnvCard = useTranslations("envVaultPage.envCard");
  const tVaultCard = useTranslations("envVaultPage.vaultCard");
  const tAuth = useTranslations("settings.authCard");
  const tTabs = useTranslations("settings.tabs");
  const tCommon = useTranslations("common");

  const tTunnel = useTranslations("settings.tunnelCard");

  const tProxy = useTranslations("networkProxy");
  const [providers, setProviders] = useState<ProviderOption[]>([]);
  const [providerProfiles, setProviderProfiles] = useState<LlmProviderProfile[]>([]);
  const [providerManagementBusy, setProviderManagementBusy] = useState(false);
  const [providerManagementDirty, setProviderManagementDirty] = useState(false);
  const [modelCatalog, setModelCatalog] = useState<Record<string, string[]>>({});
  const [defaultTier, setDefaultTier] = useState("medium");
  const [intentTier, setIntentTier] = useState("light");
  const [tierRows, setTierRows] = useState<LlmTierConfig[]>([]);
  // Hydrated from /llm/config's ``reasoning_levels``; the fallback
  // keeps the dropdown rendered before the first response lands.
  const [reasoningLevels, setReasoningLevels] = useState<string[]>(
    () => [...FALLBACK_REASONING_LEVELS],
  );
  // Hydrated from ``/llm/catalog``. Drives provider auto-fill (base
  // URL, api_mode badge), and the Add-Provider form's catalogue picker.
  const [providerCatalog, setProviderCatalog] = useState<ProviderCatalogEntry[]>([]);
  // OAuth login state for subscription-backed providers.
  const [oauthProviders, setOauthProviders] = useState<OAuthProviderInfo[]>([]);
  const [oauthStatuses, setOauthStatuses] = useState<Record<string, OAuthProviderStatus>>({});
  const [oauthBusy, setOauthBusy] = useState<string>("");
  const [oauthMessage, setOauthMessage] = useState<string>("");
  const [oauthPasteToken, setOauthPasteToken] = useState<Record<string, string>>({});
  // Login directives + device-code flow state. Keyed by provider id so
  // multiple OAuth cards (rare, but possible) keep independent panels.
  type OauthLoginDirective = {
    flow: "cli" | "device_code" | "paste";
    command?: string;
    verification_uri?: string;
    instruction: string;
  };
  type DeviceCodeSession = {
    device_code: string;
    user_code: string;
    verification_uri: string;
    verification_uri_complete?: string;
    interval: number;
    expires_at: number;
    status: "pending" | "slow_down" | "ok" | "error" | "polling";
    message?: string;
  };
  const [oauthDirective, setOauthDirective] = useState<Record<string, OauthLoginDirective>>({});
  const [deviceCodeSessions, setDeviceCodeSessions] = useState<Record<string, DeviceCodeSession>>({});
  // Mirror of ``deviceCodeSessions`` for setTimeout callbacks. React
  // re-creates the polling function on each render so the closure-
  // captured state would otherwise be stale by the time the timer
  // fires; reading from a ref sidesteps that without re-binding the
  // timer on every render.
  const deviceCodeSessionsRef = useRef<Record<string, DeviceCodeSession>>({});
  useEffect(() => {
    deviceCodeSessionsRef.current = deviceCodeSessions;
  }, [deviceCodeSessions]);
  const deviceCodePollRefs = useRef<Record<string, ReturnType<typeof setTimeout> | null>>({});
  // Custom-provider preset picker.
  const [customProviderKind, setCustomProviderKind] = useState<CustomProviderKind | "">("");
  const [loadedFingerprint, setLoadedFingerprint] = useState("");
  const [savedRevision, setSavedRevision] = useState("");
  const [connectionBaseline, setConnectionBaseline] = useState({ base_url: "https://api.openai.com/v1", provider_key_ref: "", kind: "" });
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
  // Surfaced inline next to the discover form so the operator sees
  // exactly which provider/url failed without scrolling to the global
  // error banner. Cleared on every fetch attempt.
  const [discoveryError, setDiscoveryError] = useState<string | null>(null);
  // Manual model id entry — escape hatch when the provider's
  // ``/models`` endpoint is missing, gated, or the operator simply
  // wants to import a single known id without round-tripping discovery.
  const [manualModelDraft, setManualModelDraft] = useState("");
  const [authStatus, setAuthStatus] = useState<AuthStatus | null>(null);
  const [authBusy, setAuthBusy] = useState(false);
  const [currentAdminPassword, setCurrentAdminPassword] = useState("");
  const [newAdminPassword, setNewAdminPassword] = useState("");
  const [confirmAdminPassword, setConfirmAdminPassword] = useState("");
  // Action feedback (save/delete/test results) goes through the global
  // toast stack instead of a page-top banner: these panels are far
  // taller than one viewport, so a banner under the page header was
  // invisible after any action performed at the bottom of the page.
  function reportError(message: string) {
    toast({ message, tone: "error" });
  }
  function reportOk(message: string) {
    toast({ message, tone: "ok" });
  }
  const [activeSettingsTab, setActiveSettingsTab] = useState<SettingsTabKey>("models");
  const effectiveSettingsTab: SettingsTabKey | ForceSectionKey = forceSection ?? activeSettingsTab;
  const [settingsReady, setSettingsReady] = useState(false);
  const [sectionStates, setSectionStates] = useState<Record<string, "loading" | "ready" | "error">>({});
  const [sectionErrors, setSectionErrors] = useState<Record<string, string>>({});
  const loadedSections = useRef(new Set<string>());
  const sectionRequests = useRef(new Map<string, Promise<void>>());
  const sectionKey = effectiveSettingsTab;
  const sectionBusy = !settingsReady || sectionStates[sectionKey] === "loading";
  const modelLoadError = sectionErrors.models;
  useEffect(() => {
    onSetupBusyChange?.(sectionBusy || loading || saving || discovering || importing || Boolean(oauthBusy));
  }, [sectionBusy, loading, saving, discovering, importing, oauthBusy, onSetupBusyChange]);
  // ---- Vault-backed runtime env + generic secret refs -------------
  const [runtimeEnv, setRuntimeEnv] = useState<RuntimeEnvVar[]>([]);
  const [vaultRefs, setVaultRefs] = useState<SecretRef[]>([]);
  const [securityBusy, setSecurityBusy] = useState<string>("");
  const [proxyStatus, setProxyStatus] = useState<NetworkProxyStatus | null>(null);
  const [proxyPresets, setProxyPresets] = useState<NetworkProxyPreset[]>([]);
  const [proxyBusy, setProxyBusy] = useState<string>("");
  const [proxyEnabled, setProxyEnabled] = useState(false);
  const [proxyMode, setProxyMode] = useState<"direct" | "pool">("direct");
  const [proxyPreset, setProxyPreset] = useState("custom");
  const [proxyAllUrl, setProxyAllUrl] = useState("");
  const [proxyHttpUrl, setProxyHttpUrl] = useState("");
  const [proxyHttpsUrl, setProxyHttpsUrl] = useState("");
  const [proxyPoolUrl, setProxyPoolUrl] = useState("");
  const [proxyPoolFormat, setProxyPoolFormat] = useState("auto");
  const [proxyNoProxy, setProxyNoProxy] = useState(DEFAULT_NO_PROXY);
  const [proxyRefs, setProxyRefs] = useState<Record<string, string>>({});
  const [proxyTestUrl, setProxyTestUrl] = useState("https://httpbin.org/ip");
  const [proxyTestResult, setProxyTestResult] = useState<string | null>(null);
  const [dashboardStatus, setDashboardStatus] = useState<NetworkDashboardStatus | null>(null);
  const [dashboardPortDraft, setDashboardPortDraft] = useState("18380");
  const [dashboardBusy, setDashboardBusy] = useState(false);
  const [dashboardMessage, setDashboardMessage] = useState<string | null>(null);
  const [tunnelsStatus, setTunnelsStatus] = useState<NetworkTunnelsStatus | null>(null);
  const [selectedTunnelProvider, setSelectedTunnelProvider] = useState("tailscale");
  const [tunnelDrafts, setTunnelDrafts] = useState<Record<string, TunnelDraft>>({});
  const [tunnelBusy, setTunnelBusy] = useState<string>("");
  const [tunnelMessage, setTunnelMessage] = useState<string | null>(null);
  const [envNameDraft, setEnvNameDraft] = useState("");
  const [envValueDraft, setEnvValueDraft] = useState("");
  const [vaultNameDraft, setVaultNameDraft] = useState("");
  const [vaultValueDraft, setVaultValueDraft] = useState("");
  const [vaultKindDraft, setVaultKindDraft] = useState("opaque");
  const [vaultScopeDraft, setVaultScopeDraft] = useState("runtime");



  async function loadAuthStatus() {
    try {
      setAuthStatus(await clientApi.authStatus());
    } catch {
      setAuthStatus(null);
    }
  }

  async function loadSecurityRuntime() {
    try {
      const [envRes, secretsRes] = await Promise.all([
        clientApi.runtimeEnvList().catch(() => null),
        clientApi.secretsList().catch(() => null),
      ]);
      if (envRes?.ok) setRuntimeEnv(envRes.env || []);
      if (secretsRes) setVaultRefs(secretsRes.refs || []);
      return { envRes, secretsRes };
    } catch (e) {
      reportError(e instanceof Error ? e.message : String(e));
      return null;
    }
  }

  function syncProxyDrafts(next: NetworkProxyStatus | null | undefined) {
    if (!next?.config) return;
    const cfg = next.config;
    setProxyStatus(next);
    setProxyPresets(next.presets || []);
    setProxyEnabled(Boolean(cfg.enabled));
    setProxyMode(cfg.mode === "pool" ? "pool" : "direct");
    setProxyPreset(cfg.preset || "custom");
    setProxyAllUrl(cfg.all_url_ref ? "" : (cfg.all_url || ""));
    setProxyHttpUrl(cfg.http_url_ref ? "" : (cfg.http_url || ""));
    setProxyHttpsUrl(cfg.https_url_ref ? "" : (cfg.https_url || ""));
    setProxyPoolUrl(cfg.pool_url_ref ? "" : (cfg.pool_url || ""));
    setProxyPoolFormat(cfg.pool_format || "auto");
    setProxyNoProxy(cfg.no_proxy || DEFAULT_NO_PROXY);
    setProxyRefs({
      all_url_ref: cfg.all_url_ref || "",
      http_url_ref: cfg.http_url_ref || "",
      https_url_ref: cfg.https_url_ref || "",
      pool_url_ref: cfg.pool_url_ref || "",
    });
  }

  function syncDashboardDraft(next: NetworkDashboardStatus | null | undefined) {
    if (!next?.config) return;
    setDashboardStatus(next);
    setDashboardPortDraft(String(next.config.port || 18380));
  }

  async function loadNetworkDashboard() {
    try {
      const next = await clientApi.networkDashboard();
      syncDashboardDraft(next);
      return next;
    } catch (e) {
      reportError(e instanceof Error ? e.message : String(e));
      return null;
    }
  }

  async function saveDashboardEndpoint() {
    const port = Number(dashboardPortDraft.trim());
    if (!Number.isFinite(port) || port < 1 || port > 65535) {
      reportError(tTunnel("dashboardPortInvalid"));
      return;
    }
    setDashboardBusy(true);
    setDashboardMessage(null);
    try {
      const next = await clientApi.networkDashboardSet({
        host: dashboardStatus?.config.host || "127.0.0.1",
        port,
      });
      syncDashboardDraft(next);
      const tunnels = await clientApi.networkTunnels().catch(() => null);
      if (tunnels?.ok) syncTunnelDrafts(tunnels);
      setDashboardMessage(tTunnel("dashboardSaved"));
    } catch (e) {
      reportError(e instanceof Error ? e.message : String(e));
    } finally {
      setDashboardBusy(false);
    }
  }

  function syncTunnelDrafts(next: NetworkTunnelsStatus | null | undefined) {
    if (!next?.providers?.length) return;
    setTunnelsStatus(next);
    const drafts: Record<string, TunnelDraft> = {};
    for (const row of next.providers) {
      drafts[row.spec.id] = emptyTunnelDraft(row.config, row.spec.modes?.[0] || "public");
    }
    setTunnelDrafts(drafts);
    if (!next.providers.some((row) => row.spec.id === selectedTunnelProvider)) {
      setSelectedTunnelProvider(next.providers[0]?.spec.id || "tailscale");
    }
  }

  async function loadNetworkTunnels() {
    try {
      const next = await clientApi.networkTunnels();
      syncTunnelDrafts(next);
      return next;
    } catch (e) {
      reportError(e instanceof Error ? e.message : String(e));
      return null;
    }
  }

  function patchTunnelDraft(provider: string, patch: Partial<TunnelDraft>) {
    setTunnelDrafts((prev) => ({
      ...prev,
      [provider]: {
        ...(prev[provider] || emptyTunnelDraft(undefined, "public")),
        ...patch,
      },
    }));
  }

  async function saveTunnelConfig(provider: string): Promise<boolean> {
    const draft = tunnelDrafts[provider] || emptyTunnelDraft();
    setTunnelBusy(`save:${provider}`);
    setTunnelMessage(null);
    try {
      const res = await clientApi.networkTunnelConfig({
        provider,
        enabled: draft.enabled,
        target: draft.target,
        target_url: draft.target_url.trim(),
        mode: draft.mode,
        cloudflare_mode: draft.cloudflare_mode,
        token: draft.token.trim(),
        token_ref: draft.token.trim() ? "" : draft.token_ref,
        public_hostname: draft.public_hostname.trim(),
        region: draft.region.trim(),
      });
      if (!res.ok) throw new Error(res.detail || res.error || "tunnel config save failed");
      syncTunnelDrafts(res);
      setTunnelMessage(tTunnel("saved"));
      return true;
    } catch (e) {
      reportError(e instanceof Error ? e.message : String(e));
      return false;
    } finally {
      setTunnelBusy("");
    }
  }

  async function installTunnelProvider(provider: string) {
    setTunnelBusy(`install:${provider}`);
    setTunnelMessage(null);
    try {
      const res = await clientApi.networkTunnelInstall({ provider, approve: true });
      if (!res.ok) throw new Error(res.detail || res.error || "tunnel install failed");
      setTunnelMessage(res.already_installed ? tTunnel("alreadyInstalled") : tTunnel("installFinished"));
      await loadNetworkTunnels();
    } catch (e) {
      reportError(e instanceof Error ? e.message : String(e));
    } finally {
      setTunnelBusy("");
    }
  }

  async function startTunnelProvider(provider: string) {
    const saved = await saveTunnelConfig(provider);
    if (!saved) return;
    setTunnelBusy(`start:${provider}`);
    setTunnelMessage(null);
    try {
      const res = await clientApi.networkTunnelStart(provider);
      if (!res.ok) throw new Error(res.detail || res.error || "tunnel start failed");
      const state = res.state as { external_urls?: unknown } | undefined;
      const stateUrls = Array.isArray(state?.external_urls)
        ? state.external_urls.filter((url): url is string => typeof url === "string" && url.length > 0)
        : [];
      const responseUrls = Array.isArray(res.external_urls) ? res.external_urls.filter(Boolean) : [];
      const urls = responseUrls.length ? responseUrls : stateUrls;
      setTunnelMessage(urls.length ? tTunnel("startedWithUrl", { url: urls[0] }) : tTunnel("started"));
      await loadNetworkTunnels();
    } catch (e) {
      reportError(e instanceof Error ? e.message : String(e));
    } finally {
      setTunnelBusy("");
    }
  }

  async function stopTunnelProvider(provider: string) {
    setTunnelBusy(`stop:${provider}`);
    setTunnelMessage(null);
    try {
      const res = await clientApi.networkTunnelStop(provider);
      if (!res.ok) throw new Error(res.detail || res.error || "tunnel stop failed");
      setTunnelMessage(tTunnel("stopFinished"));
      await loadNetworkTunnels();
    } catch (e) {
      reportError(e instanceof Error ? e.message : String(e));
    } finally {
      setTunnelBusy("");
    }
  }

  async function loadNetworkProxy() {
    try {
      const next = await clientApi.networkProxy();
      syncProxyDrafts(next);
      return next;
    } catch (e) {
      reportError(e instanceof Error ? e.message : String(e));
      return null;
    }
  }

  function applyProxyPreset(id: string) {
    setProxyPreset(id);
    const preset = proxyPresets.find((row) => row.id === id);
    if (!preset) return;
    setProxyMode(preset.mode === "pool" ? "pool" : "direct");
    if (preset.all_url) {
      setProxyAllUrl(preset.all_url);
      setProxyRefs((prev) => ({ ...prev, all_url_ref: "" }));
    }
    if (preset.pool_url) {
      setProxyPoolUrl(preset.pool_url);
      setProxyRefs((prev) => ({ ...prev, pool_url_ref: "" }));
    }
    if (preset.pool_format) setProxyPoolFormat(preset.pool_format);
  }

  async function saveNetworkProxy() {
    setProxyBusy("save");
    try {
      const res = await clientApi.networkProxySet({
        enabled: proxyEnabled,
        mode: proxyMode,
        preset: proxyPreset,
        all_url: proxyAllUrl.trim(),
        all_url_ref: proxyAllUrl.trim() ? "" : proxyRefs.all_url_ref,
        http_url: proxyHttpUrl.trim(),
        http_url_ref: proxyHttpUrl.trim() ? "" : proxyRefs.http_url_ref,
        https_url: proxyHttpsUrl.trim(),
        https_url_ref: proxyHttpsUrl.trim() ? "" : proxyRefs.https_url_ref,
        pool_url: proxyPoolUrl.trim(),
        pool_url_ref: proxyPoolUrl.trim() ? "" : proxyRefs.pool_url_ref,
        pool_format: proxyPoolFormat,
        no_proxy: proxyNoProxy.trim() || DEFAULT_NO_PROXY,
      });
      if (!res.ok) throw new Error(res.detail || res.error || "proxy save failed");
      syncProxyDrafts(res);
      reportOk(tProxy("saved"));
    } catch (e) {
      reportError(e instanceof Error ? e.message : String(e));
    } finally {
      setProxyBusy("");
    }
  }

  async function testNetworkProxy() {
    setProxyBusy("test");
    setProxyTestResult(null);
    try {
      const res = await clientApi.networkProxyTest({ url: proxyTestUrl.trim() });
      setProxyTestResult(
        res.ok
          ? `ok · HTTP ${res.status || 200} · ${res.elapsed_ms ?? 0}ms`
          : `error · ${res.error || "proxy test failed"}`,
      );
    } catch (e) {
      setProxyTestResult(e instanceof Error ? e.message : String(e));
    } finally {
      setProxyBusy("");
    }
  }

  async function saveRuntimeEnv() {
    setSecurityBusy("env:save");
    try {
      const res = await clientApi.runtimeEnvPut({
        name: envNameDraft.trim(),
        value: envValueDraft,
      });
      if (!res.ok) throw new Error(res.detail || res.error || "env save failed");
      setEnvValueDraft("");
      reportOk(tEnvCard("saved", { name: res.env?.name || envNameDraft.trim() }));
      await loadSecurityRuntime();
    } catch (e) {
      reportError(e instanceof Error ? e.message : String(e));
    } finally {
      setSecurityBusy("");
    }
  }

  async function deleteRuntimeEnv(name: string) {
    // Destructive: every subprocess that relied on this env var stops
    // receiving it on the next launch — require an explicit confirm.
    const confirmed = await confirm({
      title: tEnvCard("deleteTitle"),
      message: tEnvCard("deleteMessage", { name }),
      tone: "danger",
      okLabel: tCommon("delete"),
      cancelLabel: tCommon("cancel"),
    });
    if (!confirmed) return;
    setSecurityBusy(`env:delete:${name}`);
    try {
      const res = await clientApi.runtimeEnvDelete(name);
      if (!res.ok) throw new Error(res.detail || res.error || "env delete failed");
      reportOk(tEnvCard("removed", { name: res.name || name }));
      await loadSecurityRuntime();
    } catch (e) {
      reportError(e instanceof Error ? e.message : String(e));
    } finally {
      setSecurityBusy("");
    }
  }

  async function saveVaultSecret() {
    setSecurityBusy("vault:save");
    try {
      const scopes = vaultScopeDraft
        .split(",")
        .map((s) => s.trim())
        .filter(Boolean);
      const res = await clientApi.secretsPut({
        name: vaultNameDraft.trim(),
        value: vaultValueDraft,
        kind: vaultKindDraft.trim() || "opaque",
        scope: scopes,
      });
      if (!res.ok) throw new Error(res.detail || res.error || "vault save failed");
      setVaultValueDraft("");
      reportOk(tVaultCard("saved", { name: res.ref?.ref || vaultNameDraft.trim() }));
      await loadSecurityRuntime();
    } catch (e) {
      reportError(e instanceof Error ? e.message : String(e));
    } finally {
      setSecurityBusy("");
    }
  }

  async function deleteVaultSecret(name: string) {
    // Destructive: anything still pointing at this vault:// ref fails
    // to resolve afterwards — require an explicit confirm.
    const confirmed = await confirm({
      title: tVaultCard("deleteTitle"),
      message: tVaultCard("deleteMessage", { name }),
      tone: "danger",
      okLabel: tCommon("delete"),
      cancelLabel: tCommon("cancel"),
    });
    if (!confirmed) return;
    setSecurityBusy(`vault:delete:${name}`);
    try {
      const res = await clientApi.secretsDelete(name);
      if (!res.ok) throw new Error(res.error || "vault delete failed");
      reportOk(tVaultCard("removed", { name: res.name || name }));
      await loadSecurityRuntime();
    } catch (e) {
      reportError(e instanceof Error ? e.message : String(e));
    } finally {
      setSecurityBusy("");
    }
  }

  async function loadModelConfig() {
    setLoading(true);
    try {
      const [cfg, providerRes, modelRes, catalogRes, oauthRes] = await Promise.all([
        clientApi.llmConfig(),
        clientApi.llmProviders(),
        clientApi.llmModels(),
        clientApi.llmCatalog().catch(() => null),
        clientApi.llmOauthProviders().catch(() => null),
      ]);
      if (!cfg.ok) throw new Error(cfg.error || "cannot load llm config");
      const loadedDefaultTier = cfg.default_tier || "medium";
      const loadedIntentTier = cfg.intent_tier || "light";
      const loadedTiers = ensureAssignmentTiers(cfg.tiers || []);
      const profiles = cfg.provider_profiles || [];
      setSavedRevision((cfg as typeof cfg & { revision?: string }).revision || "");
      let selectedProvider = providerDraft;
      if (setupMode) {
        const primary = routesOf(loadedTiers.find(row => row.tier === loadedDefaultTier) || emptyTier(loadedDefaultTier))[0];
        if (primary?.provider && primary.provider !== "mock") {
          selectedProvider = primary.provider;
        }
      }
      const selectedProfile = profiles.find(profile => profile.provider === selectedProvider) as ModelProfileState | undefined;
      const selectedUrl = selectedProfile?.base_url || DEFAULT_PROVIDER_BASE_URLS[selectedProvider] || "";
      setProviderDraft(selectedProvider);
      setProviderBaseUrlDraft(selectedUrl);
      setProviderKeyDraft("");
      setCustomProviderKind("");
      setManualModelDraft("");
      setConnectionBaseline({ base_url: selectedUrl, provider_key_ref: selectedProfile?.provider_key_ref || "", kind: selectedProfile?.kind || "" });
      setDefaultTier(loadedDefaultTier);
      setIntentTier(loadedIntentTier);
      setTierRows(loadedTiers);
      if (Array.isArray(cfg.reasoning_levels) && cfg.reasoning_levels.length > 0) {
        setReasoningLevels(cfg.reasoning_levels);
      }
      if (catalogRes && Array.isArray(catalogRes.providers)) {
        setProviderCatalog(catalogRes.providers);
        // Catalog-level reasoning_levels override config-level if present.
        if (Array.isArray(catalogRes.reasoning_levels) && catalogRes.reasoning_levels.length > 0) {
          setReasoningLevels(catalogRes.reasoning_levels);
        }
      }
      if (oauthRes && Array.isArray(oauthRes.providers)) {
        setOauthProviders(oauthRes.providers);
        setOauthStatuses(oauthRes.statuses || {});
      }
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
      setSectionErrors((previous) => ({ ...previous, models: "" }));
      return true;
    } catch (e) {
      setSectionErrors((previous) => ({ ...previous, models: e instanceof Error ? e.message : String(e) }));
      return false;
    } finally {
      setLoading(false);
    }
  }

  // Scope reads to the visible panel. Reusing a resolved/in-flight request also
  // prevents tab changes, locale changes and StrictMode from overwriting drafts.
  function checked<T>(result: T): T {
    if (result && typeof result === "object" && "ok" in result && result.ok === false) {
      const error = result as { error?: string; detail?: string };
      throw new Error(error.detail || error.error || tUi("loadFailed"));
    }
    return result;
  }

  function loadSettingsSection(refresh = false): Promise<void> {
    const key = sectionKey;
    const pending = sectionRequests.current.get(key);
    if (pending) return pending;
    if (!refresh && loadedSections.current.has(key)) return Promise.resolve();
    if (refresh) invalidateReadCache();
    setSectionStates((previous) => ({ ...previous, [key]: "loading" }));
    setSectionErrors((previous) => ({ ...previous, [key]: "" }));
    const request = Promise.resolve().then(async () => {
      switch (effectiveSettingsTab) {
        case "models":
          if (!(await loadModelConfig())) throw new Error(tUi("loadFailed"));
          break;
        case "access": {
          setAuthStatus(checked(await clientApi.authStatus()));
          break;
        }
        case "runtime": {
          const [proxy, dashboard, tunnels] = await Promise.all([clientApi.networkProxy(), clientApi.networkDashboard(), clientApi.networkTunnels()]);
          syncProxyDrafts(checked(proxy));
          syncDashboardDraft(checked(dashboard));
          syncTunnelDrafts(checked(tunnels));
          break;
        }
        case "envvault": {
          const [env, secrets] = await Promise.all([clientApi.runtimeEnvList(), clientApi.secretsList()]);
          setRuntimeEnv(checked(env).env || []);
          setVaultRefs(checked(secrets).refs || []);
          break;
        }

        // RuntimeFlagsPanel and GatewayChannelsPanel own their own reads.
        default:
          break;
      }
      loadedSections.current.add(key);
      setSectionStates((previous) => ({ ...previous, [key]: "ready" }));
    }).catch((error: unknown) => {
      setSectionErrors((previous) => ({ ...previous, [key]: error instanceof Error ? error.message : String(error) }));
      setSectionStates((previous) => ({ ...previous, [key]: "error" }));
    }).finally(() => { sectionRequests.current.delete(key); });
    sectionRequests.current.set(key, request);
    return request;
  }

  useEffect(() => {
    if (settingsReady) void loadSettingsSection();
    // sectionKey includes the activity filter; translations are not a data dependency.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [settingsReady, sectionKey]);

  async function refreshCurrentSection() {
    if (effectiveSettingsTab === "search") { window.dispatchEvent(new Event("nerya:search-refresh")); return; }
    if (effectiveSettingsTab === "mcp") { window.dispatchEvent(new Event("nerya:mcp-refresh")); return; }
    if (effectiveSettingsTab === "models" && dirty && !(await confirm({
      title: tUi("discardChanges"), message: tUi("discardChangesDescription"), tone: "warning",
    }))) return;
    await loadSettingsSection(true);
  }

  useEffect(() => {
    if (typeof window === "undefined") return;
    if (inSectionMode) { setSettingsReady(true); return; }
    // Each moved section now has a dedicated top-level route. The map
    // catches anyone landing on `/settings#<section>` (old bookmarks,
    // in-doc deep-links, etc.) and rewrites them to the new page so
    // they don't end up on a tab that no longer exists in the section
    // nav.
    const movedSections: Record<string, string> = {
      memory: "/self-evolution?tab=memory",
      capabilityGates: "/advanced",
      search: "/web-search",
      envvault: "/env-vault",
    };
    const syncHash = () => {
      const tab = window.location.hash.replace(/^#/, "");
      const target = movedSections[tab];
      if (target) {
        window.location.replace(target);
        return;
      }
      setActiveSettingsTab(isSettingsTabKey(tab) ? tab : "models");
      setSettingsReady(true);
    };
    syncHash();
    window.addEventListener("hashchange", syncHash);
    return () => window.removeEventListener("hashchange", syncHash);
  }, [inSectionMode]);

  // Cancel any in-flight device-code poll timers when the page
  // unmounts. The mutable ref captures the live timer ids so we don't
  // need to thread them through cleanup of every individual handler.
  useEffect(() => {
    const refs = deviceCodePollRefs.current;
    return () => {
      for (const handle of Object.values(refs)) {
        if (handle) clearTimeout(handle);
      }
    };
  }, []);

  const providerOptions = useMemo(() => {
    const names = new Set<string>(KNOWN_LLM_PROVIDERS);
    // Include backend-discovered provider ids so newly supported or
    // manually imported providers remain selectable in the UI.
    providerCatalog.forEach((entry) => {
      if (entry.id) names.add(entry.id);
    });
    providers.forEach((p) => names.add(p.provider));
    providerProfiles.forEach((p) => names.add(p.provider));
    Object.keys(modelCatalog).forEach((p) => names.add(p));
    if (providerDraft) names.add(providerDraft.trim().toLowerCase());
    tierRows.forEach((r) => {
      if (r.provider) names.add(r.provider);
      routesOf(r).forEach((route) => {
        if (route.provider) names.add(route.provider);
      });
    });
    return Array.from(names).filter(Boolean).sort();
  }, [modelCatalog, providerCatalog, providerDraft, providerProfiles, providers, tierRows]);

  const catalogById = useMemo(() => {
    const map = new Map<string, ProviderCatalogEntry>();
    for (const entry of providerCatalog) {
      map.set(entry.id, entry);
      for (const alias of entry.aliases || []) {
        if (!map.has(alias)) map.set(alias, entry);
      }
    }
    return map;
  }, [providerCatalog]);

  // Map a chat-provider id (e.g. ``anthropic``, ``gemini``, ``openai-codex``)
  // to the OAuth provider that owns its login flow. Multiple chat ids
  // can route to the same OAuth provider (anthropic/claude-code share
  // the same Pro/Max OAuth; gemini/google-gemini-cli share Google
  // OAuth) so we keep this as a separate, explicit table.
  const oauthIdForProvider = useMemo(() => {
    const map: Record<string, string> = {
      "openai-codex": "openai-codex",
      "codex": "openai-codex",
      "claude-code": "claude-code",
      "anthropic": "claude-code",
      "google-gemini-cli": "google-gemini-cli",
      "gemini": "google-gemini-cli",
      "copilot": "copilot",
      "github-copilot": "copilot",
    };
    // Allow the catalog to opt new chat providers in via an
    // ``oauth_provider`` extra field on the catalogue entry.
    for (const entry of providerCatalog) {
      const oauthId = (entry.extra as Record<string, unknown> | undefined)?.["oauth_provider"];
      if (typeof oauthId === "string" && oauthId.trim()) {
        map[entry.id] = oauthId.trim();
      }
    }
    return map;
  }, [providerCatalog]);

  // OAuth providers relevant to the currently-selected chat provider.
  // Empty list = the form keeps the API-key path; only when the
  // selection has a matching OAuth do we surface the login row.
  const scopedOauthProviders = useMemo(() => {
    const pick = oauthIdForProvider[providerDraft.trim().toLowerCase()];
    if (!pick) return [];
    return oauthProviders.filter((op) => op.id === pick);
  }, [oauthIdForProvider, oauthProviders, providerDraft]);

  const defaultTierOptions = useMemo(
    () => STANDARD_TIERS.map((tier) => ({ value: tier, label: tierLabel(tier, tModel) })),
    [tModel],
  );

  const providerProfileMap = useMemo(() => {
    const map = new Map<string, LlmProviderProfile>();
    for (const profile of providerProfiles) map.set(profile.provider, profile);
    return map;
  }, [providerProfiles]);

  function routeDefaultsForProvider(_rawProvider: string) {
    return { base_url: "", kind: "" };
  }

  const configuredTierCount = useMemo(
    () => tierRows.filter((row) =>
      routesOf(row).some((route) => route.provider && route.model),
    ).length,
    [tierRows],
  );

  const catalogModelCount = useMemo(
    () => Object.values(modelCatalog).reduce((total, rows) => total + rows.length, 0),
    [modelCatalog],
  );

  const currentFingerprint = useMemo(
    () => fingerprintConfig(defaultTier, intentTier, tierRows, providerProfiles),
    [defaultTier, intentTier, providerProfiles, tierRows],
  );

  const pendingConnection = connectionPatch({ ...connectionBaseline, provider: providerDraft }, providerDraft.trim().toLowerCase(), providerBaseUrlDraft, providerKeyDraft,
    CUSTOM_PROVIDER_KINDS.find(kind => kind.id === customProviderKind)?.api_mode);
  const connectionDirty = Object.keys(pendingConnection).length > 1;
  const dirty = providerManagementDirty || Boolean(loadedFingerprint && (currentFingerprint !== loadedFingerprint || connectionDirty || (setupMode && manualModelDraft.trim())));

  const managedProviders = useMemo(() => {
    const profiles = new Map(providerProfiles.map(profile => [profile.provider, profile]));
    for (const tier of tierRows) for (const route of routesOf(tier)) {
      if (!route.provider || route.provider === "mock" || profiles.has(route.provider)) continue;
      profiles.set(route.provider, { provider: route.provider, base_url: route.base_url,
        provider_key_ref: route.provider_key_ref, has_key_ref: route.has_key_ref,
        kind: route.kind });
    }
    return [...profiles.values()].sort((a, b) => a.provider.localeCompare(b.provider));
  }, [providerProfiles, tierRows]);

  function providerProfileSaved(profile: LlmProviderProfile, revision: string, savedTiers: LlmTierConfig[]) {
    setProviderProfiles(previous => [...previous.filter(row => row.provider !== profile.provider), profile]
      .sort((a, b) => a.provider.localeCompare(b.provider)));
    setLoadedFingerprint(previous => {
      if (!previous) return previous;
      const baseline = JSON.parse(previous);
      const savedProfile = JSON.parse(fingerprintConfig("", "", [], [profile])).provider_profiles[0];
      baseline.provider_profiles = [...baseline.provider_profiles.filter((row: LlmProviderProfile) => row.provider !== profile.provider), savedProfile]
        .sort((a: LlmProviderProfile, b: LlmProviderProfile) => a.provider.localeCompare(b.provider));
      return JSON.stringify(baseline);
    });
    setSavedRevision(revision);
    setTierRows(previous => previous.map(tier => {
      const saved = savedTiers.find(row => row.tier === tier.tier);
      if (!saved) return tier;
      const savedRoutes = routesOf(saved);
      return tierWithRoutes({ ...tier, routes: routesOf(tier).map(route => {
        if (route.provider !== profile.provider) return route;
        const match = savedRoutes.find(row => row.provider === route.provider && row.model === route.model);
        return match ? { ...route, effective: match.effective || match, source: match.source || route.source } : route;
      }) });
    }));
    if (providerDraft.trim().toLowerCase() === profile.provider) {
      setProviderBaseUrlDraft(profile.base_url || "");
      setProviderKeyDraft("");
      setCustomProviderKind("");
      setConnectionBaseline({ base_url: profile.base_url || "", provider_key_ref: profile.provider_key_ref || "", kind: profile.kind || "" });
    }
  }

  useEffect(() => {
    if (!dirty || setupMode) return;
    const unload = (event: BeforeUnloadEvent) => { event.preventDefault(); event.returnValue = ""; };
    const navigation = (window as Window & { navigation?: EventTarget }).navigation;
    let approvedClick = false;
    const leave = (event: MouseEvent) => {
      const target = event.target instanceof Element ? event.target.closest("a[href], [data-settings-section]") : null;
      if (!target || (target instanceof HTMLAnchorElement && (target.target === "_blank" || target.hasAttribute("download")))) return;
      if (!window.confirm(tUi("discardChangesDescription"))) { event.preventDefault(); event.stopImmediatePropagation(); }
      else { approvedClick = true; setTimeout(() => { approvedClick = false; }, 0); }
    };
    const navigate = (event: Event) => {
      // Navigation API covers browser back/forward and imperative SPA routes.
      if (event.cancelable && !approvedClick && !window.confirm(tUi("discardChangesDescription"))) event.preventDefault();
    };
    window.addEventListener("beforeunload", unload);
    document.addEventListener("click", leave, true);
    navigation?.addEventListener("navigate", navigate);
    return () => { window.removeEventListener("beforeunload", unload); document.removeEventListener("click", leave, true); navigation?.removeEventListener("navigate", navigate); };
  }, [dirty, setupMode, tUi]);

  const tunnelEnabledCount = useMemo(
    () => tunnelsStatus?.providers.filter((row) => row.config.enabled).length || 0,
    [tunnelsStatus],
  );

  const tunnelRunningCount = useMemo(
    () => tunnelsStatus?.providers.filter((row) => row.running).length || 0,
    [tunnelsStatus],
  );

  const selectedTunnel = useMemo<TunnelProviderStatus | null>(
    () => tunnelsStatus?.providers.find((row) => row.spec.id === selectedTunnelProvider) || null,
    [selectedTunnelProvider, tunnelsStatus],
  );

  const selectedTunnelDraft = selectedTunnel
    ? (tunnelDrafts[selectedTunnel.spec.id] || emptyTunnelDraft(selectedTunnel.config, selectedTunnel.spec.modes?.[0] || "public"))
    : null;
  const selectedTunnelExternalUrls = selectedTunnel?.state?.external_urls?.filter(Boolean) || [];

  async function setProviderDraftFromSelect(provider: string) {
    if (connectionDirty && !(await confirm({ title: tUi("discardChanges"), message: tUi("discardChangesDescription"), tone: "warning" }))) return;
    // Free-text combo: normalise common case differences so typing
    // "OpenAI" still matches the "openai" catalogue entry. We keep
    // the original casing in the draft so the user sees what they
    // typed; lookups go through the lowercased key.
    const trimmed = provider.trim();
    const key = trimmed.toLowerCase();
    const profile = providerProfileMap.get(key) || providerProfileMap.get(trimmed);
    const providerInfo = providers.find(
      (p) => p.provider === key || p.provider === trimmed,
    );
    const catalogEntry = catalogById.get(key) || catalogById.get(trimmed);
    setProviderDraft(trimmed);
    // Resolution order: existing profile (operator's own override)
    // → live readiness probe → backend catalogue → hard-coded fallback
    // (legacy). The backend catalogue is the long-term source of truth;
    // the legacy table is kept only so this works pre-/llm/catalog.
    setProviderBaseUrlDraft(
      profile?.base_url ||
      providerInfo?.base_url ||
      catalogEntry?.base_url ||
      DEFAULT_PROVIDER_BASE_URLS[key] ||
      "",
    );
    setProviderKeyDraft("");
    setConnectionBaseline({
      base_url: profile?.base_url || providerInfo?.base_url || catalogEntry?.base_url || DEFAULT_PROVIDER_BASE_URLS[key] || "",
      provider_key_ref: profile?.provider_key_ref || "", kind: (profile as ModelProfileState)?.kind || "",
    });
    // Switching to a catalogue provider clears the custom-kind picker —
    // the picker is only meaningful when the user is rolling their own
    // provider id outside the catalogue.
    setCustomProviderKind("");
  }

  function patchTierRoute(
    tierIndex: number,
    routeIndex: number,
    patch: Partial<ModelRouteState>,
  ) {
    setTierRows((rows) =>
      rows.map((row, i) => {
        if (i !== tierIndex) return row;
        const routes = routesOf(row).map((route, j) => {
          if (j !== routeIndex) return route;
          const next = { ...route, ...patch };
          if (patch.provider !== undefined && patch.provider !== route.provider) {
            next.model = "";
            next.models = [];
            next.provider_key = "";
            next.provider_keys = [];
            next.provider_key_ref = "";
            next.provider_key_env = "";
            next.provider_key_refs = [];
            next.has_key_ref = false;
          }
          return next;
        });
        const first = routes[0] || emptyRoute();
        return {
          ...row,
          provider: first.provider || "",
          model: first.model || "",
          context_window: first.context_window ?? DEFAULT_MODEL_CONTEXT_WINDOW,
          base_url: first.base_url || "",
          provider_key_ref: first.provider_key_ref || "",
          routes,
        };
      }),
    );
  }

  function setPrimaryContextWindow(index: number, contextWindow: number) {
    const tierIndex = tierRows.findIndex((row) => row.tier === defaultTier);
    if (tierIndex < 0) return;
    patchTierRoute(tierIndex, index, {
      context_window: Math.max(
        MIN_MODEL_CONTEXT_WINDOW,
        Math.min(MAX_MODEL_CONTEXT_WINDOW, Math.round(contextWindow)),
      ),
    });
  }

  function selectPrimaryModel(index: number, choice: { provider: string; model: string } | null) {
    setTierRows((rows) => {
      const active = rows.find((row) => row.tier === defaultTier);
      if (!active || (!choice && index === 0)) return rows;
      const routes = routesOf(active);
      if (choice) {
        const previous = routes[index];
        routes[index] = {
          ...(previous?.provider === choice.provider ? previous : { ...emptyRoute(), ...routeDefaultsForProvider(choice.provider) }),
          ...choice, models: [choice.model],
        };
      } else routes.splice(index, 1);
      const primary = routes[0];
      return rows.map((row) => {
        if (row.tier === defaultTier) return tierWithRoutes({ ...row, routes });
        const configured = routesOf(row).some((route) => route.provider && route.model && route.provider !== "mock");
        if (index === 0 && primary && !configured && STANDARD_TIERS.includes(row.tier as typeof STANDARD_TIERS[number])) {
          return tierWithRoutes({ ...row, routes: [{ ...primary }] });
        }
        return row;
      });
    });
  }

  function addTierRoute(tierIndex: number) {
    setTierRows((rows) =>
      rows.map((row, i) => {
        if (i !== tierIndex) return row;
        return { ...row, routes: [...routesOf(row), emptyRoute()] };
      }),
    );
  }

  function moveTierRoute(tierIndex: number, from: number, to: number) {
    if (saving || loading) return;
    setTierRows(rows => rows.map((row, index) => {
      if (index !== tierIndex) return row;
      const routes = routesOf(row);
      if (![routes[from], routes[to]].every(route => route?.provider && route?.model)) return row;
      return tierWithRoutes({ ...row, routes: moveModelRoute(routes, from, to) });
    }));
  }

  function removeTierRoute(tierIndex: number, routeIndex: number) {
    setTierRows((rows) =>
      rows.map((row, i) => {
        if (i !== tierIndex) return row;
        const routes = routesOf(row).filter((_, j) => j !== routeIndex);
        const nextRoutes = routes.length ? routes : [emptyRoute()];
        const first = nextRoutes[0] || emptyRoute();
        return {
          ...row,
          provider: first.provider || "",
          model: first.model || "",
          context_window: first.context_window ?? DEFAULT_MODEL_CONTEXT_WINDOW,
          base_url: first.base_url || "",
          provider_key_ref: first.provider_key_ref || "",
          routes: nextRoutes,
        };
      }),
    );
  }

  function profilesForSave(): LlmProviderProfile[] {
    return connectionDirty && pendingConnection.provider ? [pendingConnection] : [];
  }

  async function saveModelConfig() {
    if (loading || saving || discovering || providerManagementBusy || providerManagementDirty || sectionStates.models !== "ready") return;
    setSetupError("");
    let configuredRows = tierRows;
    const manualModel = setupMode ? manualModelDraft.trim() : "";
    const manualProvider = providerDraft.trim().toLowerCase();
    if (manualModel && manualProvider && manualProvider !== "mock") {
      const active = tierRows.find(row => row.tier === defaultTier) || emptyTier(defaultTier);
      const routes = [...routesOf(active)];
      const previous = routes[0];
      const primaryDraft = {
        ...(previous?.provider === manualProvider ? previous : { ...emptyRoute(), ...routeDefaultsForProvider(manualProvider) }),
        provider: manualProvider, model: manualModel, models: [manualModel],
      };
      routes[0] = primaryDraft;
      configuredRows = tierRows.map(row => {
        if (row.tier === defaultTier) return tierWithRoutes({ ...row, routes });
        const configured = routesOf(row).some(route => route.provider && route.provider !== "mock" && route.model);
        return !configured && STANDARD_TIERS.includes(row.tier as typeof STANDARD_TIERS[number])
          ? tierWithRoutes({ ...row, routes: [{ ...primaryDraft }] }) : row;
      });
    }
    const primary = routesOf(configuredRows.find(row => row.tier === defaultTier) || emptyTier(defaultTier))[0];
    if (setupMode && (!primary?.provider || primary.provider === "mock" || !primary.model?.trim())) {
      setSetupError(tSetup("modelRequired"));
      return;
    }
    setSaving(true);
    try {
      const nextIntentTier = intentTier || "light";
      const rowsForSave = configuredRows
        .map((row) => {
          const routes = routesOf(row)
            .filter((route) => route.provider.trim() && route.model.trim())
            .map((route) => {
              const models = splitRouteValues(route.models?.length ? route.models : route.model);
              const providerKeyRefs = splitRouteValues(
                route.provider_key_refs?.length ? route.provider_key_refs : route.provider_key_ref,
              );
              const providerKeys = splitRouteValues(
                route.provider_keys?.length ? route.provider_keys : route.provider_key,
              );
              return explicitRoute({
                ...route,
                provider: route.provider.trim().toLowerCase(),
                model: models.join(", "),
                models,
                base_url: (route.base_url || "").trim(),
                provider_key_ref: providerKeyRefs.join(", "),
                provider_key_refs: providerKeyRefs,
                provider_key: providerKeys.join(", "),
                provider_keys: providerKeys,
                kind: (route.kind || "").trim(),
                provider_native_web_search: route.provider_native_web_search,
                reasoning_effort: (route.reasoning_effort ?? row.reasoning_effort ?? "").trim().toLowerCase(),
                context_window: route.context_window ?? row.context_window ?? DEFAULT_MODEL_CONTEXT_WINDOW,
              });
            });
          if (!row.tier.trim() || routes.length === 0) return null;
          const first = routes[0];
          return {
            tier: row.tier.trim(),
            provider: first.provider,
            model: first.model,
            base_url: first.base_url,
            provider_key_ref: first.provider_key_ref,
            ...tierPolicy(row),
            routes,
          };
        })
        .filter(Boolean) as LlmTierConfig[];
      const res = await clientApi.llmConfigSet({
        ...{ explicit_overrides: true },
        default_tier: defaultTier,
        intent_tier: nextIntentTier,
        providers: profilesForSave(),
        tiers: rowsForSave,
      });
      if (!res.ok) throw new Error(res.error || "save failed");
      const savedDefaultTier = res.default_tier || defaultTier;
      const savedIntentTier = res.intent_tier || nextIntentTier;
      const savedProfiles = res.provider_profiles || providerProfiles;
      const nextTiers = ensureAssignmentTiers(res.tiers || configuredRows);
      setDefaultTier(savedDefaultTier);
      setIntentTier(savedIntentTier);
      setTierRows(nextTiers);
      setProviderProfiles(savedProfiles);
      setLoadedFingerprint(fingerprintConfig(savedDefaultTier, savedIntentTier, nextTiers, savedProfiles));
      setProviderKeyDraft("");
      setSavedRevision((res as typeof res & { revision?: string }).revision || "");
      const profile = savedProfiles.find(row => row.provider === manualProvider) as ModelProfileState | undefined;
      const savedUrl = profile?.base_url || DEFAULT_PROVIDER_BASE_URLS[manualProvider] || "";
      setProviderBaseUrlDraft(savedUrl);
      setConnectionBaseline({ base_url: savedUrl, provider_key_ref: profile?.provider_key_ref || "", kind: profile?.kind || "" });
      setCustomProviderKind("");
      if (setupMode) setManualModelDraft("");
      reportOk(tModel("savedToWorkspace"));
      onSetupComplete?.();
    } catch (e) {
      if (setupMode) setSetupError(e instanceof Error ? e.message : String(e));
      reportError(e instanceof Error ? e.message : String(e));
    } finally {
      setSaving(false);
    }
  }

  async function discoverProviderModels() {
    const provider = providerDraft.trim().toLowerCase();
    if (!provider) {
      reportError(tProvider("providerRequired"));
      return;
    }
    if (saving || discovering || providerManagementBusy || providerManagementDirty) return;
    setDiscovering(true);
    setDiscoveryError(null);
    try {
      const key = providerKeyDraft.trim();
      // For a provider id that isn't in the catalogue we forward the
      // operator's compat-shape choice (``openai_compat`` →
      // ``chat_completions``, ``anthropic_compat`` →
      // ``anthropic_messages``) so the backend dispatches through the
      // right list-models adapter. Without this hint the backend
      // defaults to ``chat_completions`` and the Anthropic-compat
      // discovery 404s — that's the silent "add custom provider
      // throws" issue operators reported.
      const apiMode = customProviderKind
        ? CUSTOM_PROVIDER_KINDS.find((k) => k.id === customProviderKind)?.api_mode
        : undefined;
      const res = await clientApi.llmModelsDiscover({
        provider,
        base_url: providerBaseUrlDraft.trim() || undefined,
        ...(key
          ? key.startsWith("vault://")
            ? { provider_key_ref: key }
            : { provider_key: key }
          : {}),
        ...(apiMode ? { api_mode: apiMode } : {}),
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
      setProviderKeyDraft("");
      setProviderBaseUrlDraft(resolvedBaseUrl);
      const saved = res as typeof res & { provider_profile?: ModelProfileState; revision?: string };
      const profile: ModelProfileState = saved.provider_profile || { provider: resolvedProvider, base_url: resolvedBaseUrl, provider_key_ref: res.provider_key_ref || "" };
      const nextProfiles = [...providerProfiles.filter(row => row.provider !== resolvedProvider), profile].sort((a, b) => a.provider.localeCompare(b.provider));
      setProviderProfiles(nextProfiles);
      setConnectionBaseline({ base_url: resolvedBaseUrl, provider_key_ref: res.provider_key_ref || "", kind: profile.kind || "" });
      setCustomProviderKind("");
      setSavedRevision(saved.revision || "");
      // Discovery saves only the connection; preserve unrelated route drafts.
      setLoadedFingerprint(previous => {
        if (!previous) return previous;
        const baseline = JSON.parse(previous);
        baseline.provider_profiles = JSON.parse(fingerprintConfig(defaultTier, intentTier, [], nextProfiles)).provider_profiles;
        return JSON.stringify(baseline);
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
      reportOk(zh ? "连接已保存；模型列表已获取。尚未测试模型回复。" : "Connection saved; model list fetched. Model response remains untested.");
    } catch (e) {
      const msg = e instanceof Error ? e.message : String(e);
      // Mirror the failure both to the global banner (for visibility)
      // and to the inline ``discoveryError`` so the operator can react
      // without leaving the discover form.
      reportError(msg);
      setDiscoveryError(msg);
    } finally {
      setDiscovering(false);
    }
  }

  // Manual entry escape hatch — pushes a synthetic model row into
  // ``discoveredModels`` so the existing ``importSelectedModels`` path
  // can persist it. Useful when the provider's ``/models`` is gated /
  // missing, or the operator simply wants one known id.
  function addManualModel() {
    const id = manualModelDraft.trim();
    if (!id) return;
    setDiscoveryError(null);
    const provider = providerDraft.trim().toLowerCase();
    if (setupMode && provider) {
      setModelCatalog(previous => ({ ...previous, [provider]: [...new Set([...(previous[provider] || []), id])] }));
      selectPrimaryModel(0, { provider, model: id });
      setManualModelDraft("");
      setSetupError("");
      return;
    }
    if (provider && !discoveredProvider) {
      setDiscoveredProvider(provider);
    }
    if (providerBaseUrlDraft && !discoveredBaseUrl) {
      setDiscoveredBaseUrl(providerBaseUrlDraft.trim());
    }
    setDiscoveredModels((prev) => {
      if (prev.some((row) => modelId(row) === id)) return prev;
      const next: Array<Record<string, unknown>> = [
        ...prev,
        {
          id,
          owned_by: provider || "manual",
          // The backend ``models_import`` accepts arbitrary metadata —
          // tag manual rows so we can spot them in the catalogue if
          // the operator imports without first running discovery.
          source: "manual",
        },
      ];
      return next;
    });
    setSelectedModelIds((prev) => {
      const next = new Set(prev);
      next.add(id);
      return next;
    });
    setManualModelDraft("");
    reportOk(tProvider("manualQueued", { id }));
  }

  async function importSelectedModels() {
    if (providerManagementBusy || providerManagementDirty) return;
    const provider = discoveredProvider || providerDraft.trim().toLowerCase();
    const selected = discoveredModels.filter((row) => selectedModelIds.has(modelId(row)));
    if (!provider || selected.length === 0) {
      reportError(tProvider("selectAtLeastOne"));
      return;
    }
    setImporting(true);
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
      if (setupMode) selectPrimaryModel(0, { provider, model: modelId(selected[0]) });
      reportOk(tProvider("imported", { count: selected.length, provider }));
    } catch (e) {
      reportError(e instanceof Error ? e.message : String(e));
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
    if (providerManagementBusy || providerManagementDirty || refreshing) return;
    setRefreshing(true);
    try {
      const res = await clientApi.llmModelsRefresh();
      const nextCatalog: Record<string, string[]> = {};
      for (const [provider, rows] of Object.entries(res.providers || {})) {
        nextCatalog[provider] = rows.map(modelId).filter(Boolean).slice(0, 400);
      }
      setModelCatalog(nextCatalog);
      reportOk(tModel("catalogRefreshed"));
    } catch (e) {
      reportError(e instanceof Error ? e.message : String(e));
    } finally {
      setRefreshing(false);
    }
  }

  async function generateLoginLink(providerId: string) {
    setOauthBusy(providerId);
    setOauthMessage("");
    try {
      const dirRes = await clientApi.llmOauthLoginDirective(providerId);
      if (!dirRes.ok || !dirRes.directive) {
        throw new Error(dirRes.error || "no login directive");
      }
      const directive: OauthLoginDirective = {
        flow: dirRes.directive.flow,
        command: dirRes.directive.command,
        verification_uri: dirRes.directive.verification_uri,
        instruction: dirRes.directive.instruction,
      };
      setOauthDirective((prev) => ({ ...prev, [providerId]: directive }));
      if (directive.flow === "device_code") {
        await startDeviceCode(providerId);
      } else if (directive.flow === "cli") {
        setOauthMessage(
          tProvider("oauthDirectiveReady", { command: directive.command || "" }),
        );
      } else {
        setOauthMessage(directive.instruction);
      }
    } catch (e) {
      setOauthMessage(e instanceof Error ? e.message : String(e));
    } finally {
      setOauthBusy("");
    }
  }

  async function startDeviceCode(providerId: string) {
    const res = await clientApi.llmOauthDeviceCodeStart(providerId);
    if (!res.ok || !res.device_code) {
      throw new Error(res.error || "device-code start failed");
    }
    const session: DeviceCodeSession = {
      device_code: res.device_code,
      user_code: res.user_code || "",
      verification_uri: res.verification_uri || "",
      verification_uri_complete: res.verification_uri_complete,
      interval: Math.max(1, Number(res.interval) || 5),
      expires_at: Number(res.expires_at) || Date.now() / 1000 + 900,
      status: "polling",
    };
    setDeviceCodeSessions((prev) => ({ ...prev, [providerId]: session }));
    if (typeof window !== "undefined" && session.verification_uri_complete) {
      window.open(session.verification_uri_complete, "_blank", "noopener,noreferrer");
    } else if (typeof window !== "undefined" && session.verification_uri) {
      window.open(session.verification_uri, "_blank", "noopener,noreferrer");
    }
    schedulePoll(providerId, session.interval);
  }

  function schedulePoll(providerId: string, intervalSeconds: number) {
    const handles = deviceCodePollRefs.current;
    if (handles[providerId]) {
      clearTimeout(handles[providerId]!);
      handles[providerId] = null;
    }
    handles[providerId] = setTimeout(
      () => void pollDeviceCode(providerId),
      Math.max(1, intervalSeconds) * 1000,
    );
  }

  function cancelDeviceCode(providerId: string) {
    const handles = deviceCodePollRefs.current;
    if (handles[providerId]) {
      clearTimeout(handles[providerId]!);
      handles[providerId] = null;
    }
    setDeviceCodeSessions((prev) => {
      const next = { ...prev };
      delete next[providerId];
      return next;
    });
  }

  async function pollDeviceCode(providerId: string) {
    const session = deviceCodeSessionsRef.current[providerId];
    if (!session) return;
    // Expiry guard — GitHub stops accepting the device code after the
    // window passes; bail out instead of hammering the endpoint.
    if (Date.now() / 1000 >= session.expires_at) {
      setDeviceCodeSessions((prev) => ({
        ...prev,
        [providerId]: { ...session, status: "error", message: tProvider("oauthDeviceCodeExpired") },
      }));
      return;
    }
    try {
      const res = await clientApi.llmOauthDeviceCodePoll({
        provider: providerId,
        device_code: session.device_code,
      });
      if (!res.ok) {
        setDeviceCodeSessions((prev) => ({
          ...prev,
          [providerId]: { ...session, status: "error", message: res.error || "poll failed" },
        }));
        return;
      }
      if (res.status === "ok") {
        const statusRes = await clientApi.llmOauthStatus(providerId);
        if (statusRes.status) {
          setOauthStatuses((prev) => ({ ...prev, [providerId]: statusRes.status! }));
        }
        setDeviceCodeSessions((prev) => ({
          ...prev,
          [providerId]: { ...session, status: "ok", message: tProvider("oauthDeviceCodeOk") },
        }));
        setOauthMessage(tProvider("oauthDeviceCodeOk"));
        return;
      }
      if (res.status === "slow_down") {
        const next = Math.max(session.interval, Number(res.interval) || session.interval + 5);
        setDeviceCodeSessions((prev) => ({
          ...prev,
          [providerId]: { ...session, interval: next, status: "polling" },
        }));
        schedulePoll(providerId, next);
        return;
      }
      if (res.status === "error") {
        setDeviceCodeSessions((prev) => ({
          ...prev,
          [providerId]: { ...session, status: "error", message: res.error || "device-code error" },
        }));
        return;
      }
      // pending → keep polling at the same cadence.
      schedulePoll(providerId, session.interval);
    } catch (e) {
      setDeviceCodeSessions((prev) => ({
        ...prev,
        [providerId]: {
          ...session,
          status: "error",
          message: e instanceof Error ? e.message : String(e),
        },
      }));
    }
  }

  async function saveAdminPassword() {
    if (newAdminPassword.length < 8) {
      reportError(tAuth("tooShort"));
      return;
    }
    if (newAdminPassword !== confirmAdminPassword) {
      reportError(tAuth("mismatch"));
      return;
    }
    setAuthBusy(true);
    try {
      const res = await clientApi.authSetPassword({
        ...(authStatus?.password_configured ? { current_password: currentAdminPassword } : {}),
        new_password: newAdminPassword,
      });
      if (!res.ok) throw new Error(res.detail || res.error || "password_update_failed");
      if (res.token) setStoredAuthToken(res.token, res.expires_at);
      setCurrentAdminPassword("");
      setNewAdminPassword("");
      setConfirmAdminPassword("");
      reportOk(tAuth("saved"));
      await loadAuthStatus();
    } catch (e) {
      reportError(e instanceof Error ? e.message : String(e));
    } finally {
      setAuthBusy(false);
    }
  }

  function logoutAdmin() {
    clearStoredAuthToken();
    reportOk(tAuth("loggedOut"));
    if (authStatus?.local_access !== true) redirectToLogin();
  }

  // Pick which i18n namespace owns the PageHeader title/description for
  // the current standalone route. Each section page brands itself
  // explicitly; the legacy /settings route keeps the existing "Settings"
  // copy. Each tXxxPage hook returns a namespace-scoped function whose
  // keys are statically typed by next-intl, so we erase the precise type
  // down to a generic (key: string) => string lookup here — the three
  // keys we read (`eyebrow`, `title`, `description`) are guaranteed to
  // exist in every namespace by the matching en.json/zh.json entries.
  type SectionPageTranslator = (key: string) => string;
  // Only the four legacy standalone pages bring a dedicated page-header
  // namespace. The new wizard-only force sections (`access`, `models`,
  // `runtime`) intentionally fall through to the parent caller's
  // chrome (the SetupWizard renders its own stepper + title), so they
  // have no entry here — `tSectionPage` resolves to `null` and the
  // component falls back to the generic `t("title")` copy.
  const sectionPageTranslations: Partial<Record<ForceSectionKey, SectionPageTranslator>> = {
    search: tWebSearchPage as unknown as SectionPageTranslator,
    envvault: tEnvVaultPage as unknown as SectionPageTranslator,
  };
  const tSectionPage: SectionPageTranslator | null = forceSection
    ? sectionPageTranslations[forceSection] ?? null
    : null;

  return (
    <PageBody>
      {hideHeader ? null : (
        <PageHeader
          title={tSectionPage ? tSectionPage("title") : t("title")}
          description={
            tSectionPage ? tSectionPage("description") : t("description")
          }
          eyebrow={tSectionPage ? tSectionPage("eyebrow") : undefined}
          actions={
            <button
              type="button"
              className="btn btn-ghost"
              onClick={() => void refreshCurrentSection()}
              disabled={sectionBusy || (effectiveSettingsTab === "models" && (loading || saving || providerManagementBusy || providerManagementDirty))}
            >
              <RefreshIcon size={14} />
              {sectionBusy ? tCommon("loading") : tCommon("refresh")}
            </button>
          }
        />
      )}

      {/* Optional banner injected by a host page (e.g. a host page
          passes its Engines/Session tab strip here so the strip lives
          immediately under the section-page header instead of below
          the panel content). */}
      {inSectionMode && topBanner ? topBanner : null}

      {/* Loading failures remain visible; transient action results use toasts.
          Never allow server-setting edits against an uninitialized form. */}
      <ErrorBanner error={sectionErrors[sectionKey]} onRetry={() => void refreshCurrentSection()} />
      {sectionBusy ? <div role="status" className="text-sm text-[color:var(--text-muted)]">{tUi("loading")}</div> : null}
      <div aria-busy={sectionBusy}>
        <fieldset
          className="settings-flat m-0 min-w-0 space-y-6 border-0 p-0"
          disabled={(effectiveSettingsTab === "models" && (saving || discovering)) || (!["interface", "search", "capabilityGates", "gateway", "mcp"].includes(effectiveSettingsTab) && sectionStates[sectionKey] !== "ready")}
        >

      {settingsReady && effectiveSettingsTab === "models" && !loading && !modelLoadError && (!inSectionMode || forceSection === "models") ? (
        <form
          id={setupMode ? "nerya-setup-llm" : settingsPanelId("models")}
          onSubmit={(event) => { event.preventDefault(); void saveModelConfig(); }}
          role="region"
          aria-label={tTabs("models")}
          className="space-y-5"
        >
          <Card
            title={setupMode ? tSetup("modelConnection") : tProvider("title")}
            description={setupMode ? tSetup("manualModelHint") : tProvider("description")}
          >
            {/* OAuth provider login row — shown only when the
                selected chat provider has an associated OAuth login
                flow.
                Operators using API-key providers see no extra UI. */}
            {scopedOauthProviders.length ? (
              <div className="mb-4 rounded-lg border border-[color:var(--line)] p-3.5">
                <div className="mb-2 flex items-center justify-between">
                  <div>
                    <div className="text-[13px] font-medium text-ink-100">
                      {tProvider("oauthSectionTitle")}
                    </div>
                    <div className="mt-0.5 text-[12px] text-ink-500">
                      {tProvider("oauthSectionScopedHint", {
                        provider: providerDraft.trim(),
                      })}
                    </div>
                  </div>
                  {oauthMessage ? (
                    <Pill tone="brand">{oauthMessage}</Pill>
                  ) : null}
                </div>
                <div className="space-y-3">
                  {scopedOauthProviders.map((op) => {
                    const status = oauthStatuses[op.id];
                    const isReady = !!(status && status.has_token);
                    return (
                      <div
                        key={op.id}
                        className="rounded-lg border border-brand-500/10 bg-ink-950/40 p-3"
                      >
                        <div className="flex flex-wrap items-center justify-between gap-2">
                          <div>
                            <div className="font-mono text-[12px] text-ink-100">{op.display_name}</div>
                            <div className="mt-0.5 text-[11px] text-ink-500">{op.description}</div>
                          </div>
                          <div className="flex items-center gap-1.5">
                            <Pill tone={isReady ? "ok" : "warn"}>
                              {isReady ? tProvider("oauthReady") : tProvider("oauthMissing")}
                            </Pill>
                            {status?.cli_present ? (
                              <Pill tone="brand">{tProvider("oauthCliFound")}</Pill>
                            ) : null}
                            {status?.env_present ? (
                              <Pill tone="brand">{tProvider("oauthEnvSet")}</Pill>
                            ) : null}
                          </div>
                        </div>
                        <div className="mt-3 flex flex-wrap gap-2">
                          <button
                            type="button"
                            className="btn btn-secondary"
                            disabled={oauthBusy === op.id}
                            onClick={() => void generateLoginLink(op.id)}
                          >
                            {oauthBusy === op.id
                              ? tCommon("loading")
                              : tProvider("oauthGenerateLoginLink")}
                          </button>
                          <button
                            type="button"
                            className="btn btn-primary"
                            disabled={oauthBusy === op.id || !status?.cli_present}
                            onClick={async () => {
                              setOauthBusy(op.id);
                              setOauthMessage("");
                              try {
                                const res = await clientApi.llmOauthImport(op.id);
                                if (!res.ok) throw new Error(res.error || "import failed");
                                if (res.status) {
                                  setOauthStatuses((prev) => ({ ...prev, [op.id]: res.status! }));
                                }
                                setOauthMessage(
                                  tProvider("oauthImportedFrom", { source: res.source || "cli" }),
                                );
                              } catch (e) {
                                setOauthMessage(e instanceof Error ? e.message : String(e));
                              } finally {
                                setOauthBusy("");
                              }
                            }}
                          >
                            <CheckIcon size={14} />
                            {oauthBusy === op.id ? tCommon("loading") : tProvider("oauthImportFromCli")}
                          </button>
                          <button
                            type="button"
                            className="btn btn-ghost"
                            disabled={oauthBusy === op.id || !isReady}
                            onClick={async () => {
                              setOauthBusy(op.id);
                              setOauthMessage("");
                              try {
                                const res = await clientApi.llmOauthRevoke(op.id);
                                if (!res.ok) throw new Error(res.error || "revoke failed");
                                if (res.status) {
                                  setOauthStatuses((prev) => ({ ...prev, [op.id]: res.status! }));
                                }
                                setOauthMessage(tProvider("oauthRevoked"));
                              } catch (e) {
                                setOauthMessage(e instanceof Error ? e.message : String(e));
                              } finally {
                                setOauthBusy("");
                              }
                            }}
                          >
                            {tProvider("oauthRevoke")}
                          </button>
                          <div className="ml-auto flex w-full items-center gap-2 lg:w-auto">
                            <input
                              className="input-dark font-mono flex-1 min-w-[220px]"
                              type="password"
                              autoComplete="off"
                              placeholder={tProvider("oauthPastePlaceholder")}
                              value={oauthPasteToken[op.id] || ""}
                              onChange={(e) =>
                                setOauthPasteToken((prev) => ({ ...prev, [op.id]: e.target.value }))
                              }
                            />
                            <button
                              type="button"
                              className="btn btn-ghost"
                              disabled={oauthBusy === op.id || !(oauthPasteToken[op.id] || "").trim()}
                              onClick={async () => {
                                setOauthBusy(op.id);
                                setOauthMessage("");
                                try {
                                  const res = await clientApi.llmOauthPaste({
                                    provider: op.id,
                                    token: (oauthPasteToken[op.id] || "").trim(),
                                  });
                                  if (!res.ok) throw new Error(res.error || "paste failed");
                                  if (res.status) {
                                    setOauthStatuses((prev) => ({ ...prev, [op.id]: res.status! }));
                                  }
                                  setOauthPasteToken((prev) => ({ ...prev, [op.id]: "" }));
                                  setOauthMessage(tProvider("oauthPasted"));
                                } catch (e) {
                                  setOauthMessage(e instanceof Error ? e.message : String(e));
                                } finally {
                                  setOauthBusy("");
                                }
                              }}
                            >
                              {tProvider("oauthPasteSave")}
                            </button>
                          </div>
                        </div>

                        {/* Login directive panel (after Generate login link) */}
                        {oauthDirective[op.id] ? (
                          <div className="mt-3 rounded-lg border border-brand-500/15 bg-ink-950/60 p-3 text-[12px] text-ink-200">
                            <div className="font-mono text-[11px] text-ink-500">
                              {tProvider("oauthDirectiveHeading", {
                                flow: oauthDirective[op.id].flow,
                              })}
                            </div>
                            <div className="mt-1 text-[12px] text-ink-100">
                              {oauthDirective[op.id].instruction}
                            </div>
                            {oauthDirective[op.id].flow === "cli" && oauthDirective[op.id].command ? (
                              <div className="mt-2 flex items-center gap-2">
                                <code className="font-mono text-[12px] rounded bg-ink-900/80 px-2 py-1 text-ink-100">
                                  {oauthDirective[op.id].command}
                                </code>
                                <button
                                  type="button"
                                  className="btn btn-ghost"
                                  onClick={async () => {
                                    try {
                                      await navigator.clipboard.writeText(
                                        oauthDirective[op.id].command || "",
                                      );
                                      setOauthMessage(tProvider("oauthCopied"));
                                    } catch {
                                      setOauthMessage(tProvider("oauthCopyFailed"));
                                    }
                                  }}
                                >
                                  {tProvider("oauthCopyCommand")}
                                </button>
                              </div>
                            ) : null}
                          </div>
                        ) : null}

                        {/* Device-code session panel (Copilot today) */}
                        {deviceCodeSessions[op.id] ? (
                          <div className="mt-3 rounded-lg border border-brand-500/20 bg-ink-950/60 p-3">
                            <div className="flex flex-wrap items-center justify-between gap-2">
                              <div>
                                <div className="font-mono text-[11px] text-ink-500">
                                  {tProvider("oauthDeviceCodeTitle")}
                                </div>
                                <div className="mt-1 flex items-center gap-2">
                                  <span className="text-[11px] text-ink-400">
                                    {tProvider("oauthDeviceCodeUserCode")}
                                  </span>
                                  <code className="font-mono text-[16px] rounded bg-ink-900/80 px-3 py-1 tracking-[0.2em] text-brand-300">
                                    {deviceCodeSessions[op.id].user_code}
                                  </code>
                                  <button
                                    type="button"
                                    className="btn btn-ghost"
                                    onClick={async () => {
                                      try {
                                        await navigator.clipboard.writeText(
                                          deviceCodeSessions[op.id].user_code,
                                        );
                                        setOauthMessage(tProvider("oauthCopied"));
                                      } catch {
                                        setOauthMessage(tProvider("oauthCopyFailed"));
                                      }
                                    }}
                                  >
                                    {tProvider("oauthCopyCode")}
                                  </button>
                                </div>
                              </div>
                              <div className="flex items-center gap-2">
                                <Pill
                                  tone={
                                    deviceCodeSessions[op.id].status === "ok"
                                      ? "ok"
                                      : deviceCodeSessions[op.id].status === "error"
                                      ? "warn"
                                      : "brand"
                                  }
                                >
                                  {tProvider(`oauthDeviceCodeStatus.${deviceCodeSessions[op.id].status}`)}
                                </Pill>
                                <button
                                  type="button"
                                  className="btn btn-ghost"
                                  onClick={() => cancelDeviceCode(op.id)}
                                >
                                  {tCommon("cancel")}
                                </button>
                              </div>
                            </div>
                            <div className="mt-2 text-[12px] text-ink-200">
                              {tProvider("oauthDeviceCodeInstruction")}{" "}
                              <a
                                className="text-brand-300 underline"
                                target="_blank"
                                rel="noopener noreferrer"
                                href={
                                  deviceCodeSessions[op.id].verification_uri_complete
                                  || deviceCodeSessions[op.id].verification_uri
                                }
                              >
                                {deviceCodeSessions[op.id].verification_uri || "github.com/login/device"}
                              </a>
                            </div>
                            {deviceCodeSessions[op.id].message ? (
                              <div className="mt-1 text-[11px] text-ink-400">
                                {deviceCodeSessions[op.id].message}
                              </div>
                            ) : null}
                          </div>
                        ) : null}
                      </div>
                    );
                  })}
                </div>
              </div>
            ) : null}

            {!setupMode && <ProviderManagementPanel profiles={managedProviders} catalog={modelCatalog}
              defaultEndpoints={{ ...DEFAULT_PROVIDER_BASE_URLS, ...Object.fromEntries(providerCatalog.filter(provider => provider.base_url).map(provider => [provider.id, provider.base_url])) }}
              disabled={saving || loading || discovering || refreshing || importing || connectionDirty}
              onSaved={providerProfileSaved} onBusyChange={setProviderManagementBusy} onDirtyChange={setProviderManagementDirty}
              onRefreshed={(provider, models) => setModelCatalog(previous => ({ ...previous, [provider]: models.map(modelId).filter(Boolean) }))} />}

            <div className="grid grid-cols-1 gap-3 lg:grid-cols-2">
              <Field label={tProvider("providerLabel")} hint={tProvider("providerHint")}>
                <ChoiceSelect
                  value={providerDraft}
                  onValueChange={setProviderDraftFromSelect}
                  searchable
                  createOption={(value) => tProvider("createProvider", { value })}
                  aria-label={tProvider("providerLabel")}
                  className="w-full font-mono"
                  disabled={providerManagementBusy || providerManagementDirty}
                >
                  {providerOptions.map((provider) => {
                    const entry = catalogById.get(provider);
                    return <option key={provider} value={provider}>{entry?.name || provider}</option>;
                  })}
                </ChoiceSelect>
              </Field>
              <Field
                label={
                  <>
                    {tProvider("apiKeyLabel")}
                    <TermTip term="vaultRef" />
                  </>
                }
                hint={tProvider("apiKeyHint")}
              >
                <input
                  className="input-dark font-mono"
                  value={providerKeyDraft}
                  aria-label={tProvider("apiKeyLabel")}
                  onChange={(e) => setProviderKeyDraft(e.target.value)}
                  type={providerKeyDraft.startsWith("vault://") ? "text" : "password"}
                  disabled={providerManagementBusy || providerManagementDirty}
                  placeholder={tProvider("apiKeyPlaceholder")}
                />
              </Field>
              <div className="lg:col-span-2 flex flex-wrap items-center gap-2 border-t border-[color:var(--line)] pt-3">
                <button
                  type="button"
                  className={setupMode ? "btn btn-secondary" : "btn btn-primary"}
                  onClick={discoverProviderModels}
                  disabled={discovering || providerManagementBusy || providerManagementDirty || !providerDraft.trim()}
                >
                  <SearchIcon size={14} />
                  {discovering ? tProvider("fetching") : (zh ? "获取模型并保存连接" : "Fetch models and save connection")}
                </button>
                <TermTip term="fetchModels" />
                <span className="text-xs text-[color:var(--text-muted)]">{zh ? "成功后保存 URL 和凭据；失败保留草稿，不保存连接。此操作不会验证模型回复。" : "Saves URL and credentials on success; failure keeps drafts without saving. This does not test a model response."}</span>
                {!setupMode && <>
                  <button type="button" className="btn btn-secondary" disabled={!dirty || saving || providerManagementBusy || providerManagementDirty} onClick={() => void saveModelConfig()}>{zh ? "保存设置" : "Save settings"}</button>
                  <button type="button" className="btn btn-ghost" disabled={!dirty || saving || providerManagementBusy || providerManagementDirty} onClick={() => void refreshCurrentSection()}>{zh ? "放弃草稿" : "Discard drafts"}</button>
                  {dirty && <Pill tone="warn">{tModel("unsaved")}</Pill>}
                </>}
              </div>
            </div>

            {discoveryError ? (
              <div className="mt-3 rounded-lg border border-rose-500/30 bg-rose-500/[0.08] px-3 py-2 text-[12px] text-rose-200 font-mono break-all">
                <span className="text-rose-400 text-[11px] font-medium mr-1.5">
                  {tProvider("discoveryFailed")}
                </span>
                {discoveryError}
              </div>
            ) : null}

            {/* Low-frequency knobs live behind progressive disclosure:
                base URL is auto-filled for catalogue providers, the
                preset picker only matters for custom ids, and manual
                model entry is an escape hatch when /models is gated. */}
            <Advanced
              title={setupMode ? tSetup("modelConnection") : tProvider("advancedTitle")}
              description={setupMode ? tSetup("manualModelHint") : tProvider("advancedDesc")}
              defaultOpen={setupMode}
              storageKey={setupMode ? undefined : "nerya.settings.provider.advanced"}
            >
              <div className="grid grid-cols-1 gap-3 lg:grid-cols-2">
                <Field label={tProvider("baseUrlLabel")} hint={tProvider("baseUrlHint")}>
                  <input
                    className="input-dark font-mono"
                    value={providerBaseUrlDraft}
                    aria-label={tProvider("baseUrlLabel")}
                    disabled={providerManagementBusy || providerManagementDirty}
                    onChange={(e) => setProviderBaseUrlDraft(e.target.value)}
                    placeholder={
                      customProviderKind
                        ? CUSTOM_PROVIDER_KINDS.find((k) => k.id === customProviderKind)?.placeholder
                        : "https://api.openai.com/v1"
                    }
                  />
                </Field>
                {!catalogById.has(providerDraft.trim().toLowerCase()) ? (
                  <Field label={tProvider("customKindLabel")} hint={tProvider("customKindHint")}>
                    <PortalSelect<CustomProviderKind | "">
                      value={customProviderKind}
                      disabled={providerManagementBusy || providerManagementDirty}
                      onChange={(next) => {
                        setCustomProviderKind(next);
                        if (next === "openai_compat" && !providerBaseUrlDraft) {
                          setProviderBaseUrlDraft("https://api.example.com/v1");
                        } else if (
                          next === "anthropic_compat" &&
                          !providerBaseUrlDraft
                        ) {
                          setProviderBaseUrlDraft("https://api.example.com/v1");
                        }
                      }}
                      options={[
                        { value: "", label: tProvider("customKindAuto") },
                        {
                          value: "openai_compat",
                          label: tProvider("customKindOpenAI"),
                        },
                        {
                          value: "anthropic_compat",
                          label: tProvider("customKindAnthropic"),
                        },
                      ]}
                      size="sm"
                      ariaLabel={tProvider("customKindLabel")}
                      className="font-mono"
                    />
                  </Field>
                ) : null}
              </div>
              <div className="mt-3 flex flex-wrap items-end gap-2">
                <div className="flex-1 min-w-[180px]">
                  <div className="flex items-center justify-between gap-2 mb-1">
                    <span className="text-[12px] text-ink-300">
                      {tProvider("manualLabel")}
                    </span>
                    <span className="text-[11px] text-ink-500">
                      {tProvider("manualHint")}
                    </span>
                  </div>
                  <input
                    className="input-dark font-mono"
                    value={manualModelDraft}
                    aria-label={tProvider("manualLabel")}
                    onChange={(e) => setManualModelDraft(e.target.value)}
                    onKeyDown={(e) => {
                      if (e.key === "Enter") {
                        e.preventDefault();
                        addManualModel();
                      }
                    }}
                    placeholder={tProvider("manualPlaceholder")}
                    autoComplete="off"
                    spellCheck={false}
                  />
                </div>
                <button
                  type="button"
                  className="btn btn-ghost"
                  onClick={addManualModel}
                  disabled={!manualModelDraft.trim()}
                >
                  <PlusIcon size={14} />
                  {tProvider("manualAdd")}
                </button>
              </div>
            </Advanced>

            {discoveredModels.length ? (
              <div className="mt-4 overflow-hidden rounded-lg border border-[color:var(--line)] bg-ink-950/25">
                <div className="flex flex-wrap items-center gap-2 border-b border-[color:var(--line)] px-3 py-2">
                  <span className="text-xs font-medium text-[color:var(--text-base)]">
                    {tProvider("selectedCount", { selected: selectedModelIds.size, total: discoveredModels.length })}
                  </span>
                  <button type="button" className="btn btn-ghost ml-auto" onClick={() => setSelectedModelIds(new Set(discoveredModels.map(modelId)))}>
                    {tProvider("selectAll")}
                  </button>
                  <button type="button" className="btn btn-ghost" onClick={() => setSelectedModelIds(new Set())}>
                    {tProvider("clear")}
                  </button>
                  <button
                    type="button"
                    className="btn btn-primary"
                    onClick={importSelectedModels}
                    disabled={importing || providerManagementBusy || providerManagementDirty || selectedModelIds.size === 0}
                  >
                    <CheckIcon size={14} />
                    {importing ? tProvider("importing") : tProvider("importSelectedCount", { count: selectedModelIds.size })}
                  </button>
                </div>
                <div className="embedded-list-scroll">
                {discoveredModels.map((row) => {
                  const id = modelId(row);
                  const owner = String(row.owned_by ?? "");
                  const source = String(row.source ?? "");
                  return (
                    <label
                      key={id}
                      className="flex items-center justify-between gap-3 border-b border-brand-500/10 px-3 py-2 text-xs last:border-b-0 hover:bg-brand-500/[0.04] transition-colors cursor-pointer"
                    >
                      <span className="min-w-0 flex-1">
                        <span className="flex items-center gap-1.5">
                          <span className="block truncate font-mono text-ink-100">
                            {id}
                          </span>
                          {source === "manual" ? (
                            <span className="rounded-full bg-brand-500/15 text-brand-300 text-[11px] font-mono px-1.5 py-0.5">
                              manual
                            </span>
                          ) : null}
                        </span>
                        {owner ? (
                          <span className="text-[11px] text-ink-500">{owner}</span>
                        ) : null}
                      </span>
                      <input
                        type="checkbox"
                        className="accent-brand-500 cursor-pointer"
                        checked={selectedModelIds.has(id)}
                        onChange={(e) => toggleDiscoveredModel(id, e.target.checked)}
                      />
                    </label>
                  );
                })}
                </div>
              </div>
            ) : null}
          </Card>

          {compactLlm ? null : (
          <Card
            title={
              <span className="inline-flex items-center gap-2">
                <SparkIcon size={16} className="text-fluid-300" />
                {tModel("title")}

              </span>
            }
            description={tModel("description")}
            actions={setupMode ? null :
              <div className="flex items-center gap-2">
                {dirty ? <Pill tone="warn">{tModel("unsaved")}</Pill> : null}
                <Pill tone={configuredTierCount === tierRows.length ? "ok" : "warn"}>
                  {tModel("configuredSummary", { configured: configuredTierCount, total: tierRows.length })}
                </Pill>
              </div>
            }
          >
            <PrimaryModelSettings primaryOnly={setupMode} routes={routesOf(tierRows.find((row) => row.tier === defaultTier) || emptyTier(defaultTier))}
              catalog={modelCatalog} disabled={saving || loading} onChange={selectPrimaryModel}
              onContextChange={setPrimaryContextWindow}
              onMove={(from, to) => moveTierRoute(tierRows.findIndex(row => row.tier === defaultTier), from, to)}
              onAdd={() => addTierRoute(tierRows.findIndex(row => row.tier === defaultTier))}
              onInheritConnection={(routeIndex) => patchTierRoute(tierRows.findIndex(row => row.tier === defaultTier), routeIndex, { base_url: "", provider_key_ref: "", provider_key_refs: [], provider_key_env: "", provider_key: "", provider_keys: [], kind: "" })} />
            <SavedModelTest revision={savedRevision} dirty={dirty} disabled={saving || loading || discovering} />
            {!setupMode ? <div className="flex flex-wrap items-end gap-3 rounded-lg bg-ink-950/20 p-3">

              <button
                type="button"
                className="btn btn-ghost"
                onClick={refreshModels}
                disabled={refreshing || providerManagementBusy || providerManagementDirty}
              >
                <RefreshIcon size={14} />
                {refreshing ? tCommon("refreshing") : tModel("refreshCatalog")}
              </button>
              <span className="hidden text-xs text-[color:var(--text-muted)] lg:inline">
                {tModel("catalogModels", { count: catalogModelCount })}
              </span>
              <button
                type="button"
                className="btn btn-primary ml-auto"
                onClick={saveModelConfig}
                disabled={saving || loading || providerManagementBusy || providerManagementDirty || !dirty || tierRows.length === 0}
              >
                <CheckIcon size={14} />
                {saving ? tCommon("saving") : tModel("saveAssignments")}
              </button>
            </div> : null}

            {!setupMode ? <Advanced
              title={tUi("advancedRouting")}
              description={tUi("advancedRoutingDescription")}
              defaultOpen={false}
              storageKey="nerya.settings.models.advancedRouting"
            >
            <div className="space-y-3">
              <label className="min-w-[180px] text-[12px] text-ink-300">
                {tModel("defaultTier")}
                <Select
                  value={defaultTier}
                  onChange={setDefaultTier}
                  options={defaultTierOptions}
                />
              </label>
              {tierRows.map((row, index) => {
                const routes = routesOf(row);
                const configuredRoutes = routes.filter((route) =>
                  route.provider.trim() && route.model.trim()
                ).length;
                const anyRouteConfigured = configuredRoutes > 0;
                const laneKey = row.tier === INTENT_TIER
                  ? "laneIntent"
                  : row.tier === "light" ? "laneLight"
                  : row.tier === "medium" ? "laneMedium"
                  : row.tier === "high" ? "laneHigh"
                  : null;
                return (
                  <div
                    key={row.tier}
                    className="rounded-lg border border-[color:var(--line)] p-3.5"
                  >
                    <div className="mb-3 flex flex-col gap-3 border-b border-[color:var(--line)] pb-3 lg:flex-row lg:items-end lg:justify-between">
                      <div>
                        <div className="text-[13px] font-medium text-ink-100">{tierLabel(row.tier, tModel)}</div>
                        <div className="mt-0.5 text-[11px] text-ink-500">
                          {laneKey ? tModel(laneKey) : `${row.tier} model lane`}
                        </div>
                      </div>
                      <div className="flex flex-wrap gap-1.5">
                          <Pill tone={anyRouteConfigured ? "neutral" : "warn"}>
                            {tModel("configuredRoutes", { count: configuredRoutes })}
                          </Pill>
                          {row.tier === defaultTier ? <Pill tone="brand">{tModel("default")}</Pill> : null}
                          {row.tier === intentTier ? <Pill tone="brand">{tModel("intent")}</Pill> : null}
                      </div>
                    </div>
                    <div className="space-y-2.5">
                      {routes.map((route, routeIndex) => {
                        const models = modelCatalog[route.provider] || [];
                        const routeModelValues = splitRouteValues(
                          route.models?.length ? route.models : route.model,
                        );
                        const modelInputValue = routeModelValues.length
                          ? routeModelValues.join(", ")
                          : route.model;
                        const modelOptions = modelInputValue && !models.includes(modelInputValue)
                          ? [modelInputValue, ...models]
                          : models;
                        const canRemove = routes.length > 1;
                        return (
                          <ModelRoutePriority
                            key={`${row.tier}-${routeIndex}`}
                            group={`tier-${row.tier}`} index={routeIndex} count={routes.length}
                            disabled={saving || loading || !route.provider || !route.model} onMove={(from, to) => moveTierRoute(index, from, to)}
                            className="bg-ink-950/20"
                          >
                            <div className="mb-2.5 flex items-center justify-between gap-2">
                              <div className="flex min-w-0 items-center gap-2">
                                <span className="text-[12px] font-medium text-ink-200">
                                  {tModel("routeLabel", { index: routeIndex + 1 })}
                                </span>
                              </div>
                              <button
                                type="button"
                                className="icon-btn h-7 w-7 rounded-md"
                                onClick={() => removeTierRoute(index, routeIndex)}
                                disabled={!canRemove}
                                aria-label={tModel("removeRoute")}
                                title={tModel("removeRoute")}
                              >
                                <TrashIcon size={13} />
                              </button>
                            </div>
                            <div className="grid grid-cols-1 gap-3 md:grid-cols-4">
                              <Field label={tProvider("providerLabel")}>
                                <ChoiceSelect
                                  value={route.provider}
                                  onValueChange={(value) => {
                                    const defaults = routeDefaultsForProvider(value);
                                    patchTierRoute(index, routeIndex, {
                                      provider: value,
                                      base_url: defaults.base_url,
                                      kind: defaults.kind,
                                    });
                                  }}
                                  searchable
                                  createOption={(value) => tProvider("createProvider", { value })}
                                  placeholder={tModel("selectProvider")}
                                  aria-label={`${tierLabel(row.tier, tModel)} · ${tModel("routeLabel", { index: routeIndex + 1 })} · ${tProvider("providerLabel")}`}
                                  className="w-full font-mono"
                                >
                                  {providerOptions.map((provider) => <option key={provider} value={provider}>{catalogById.get(provider)?.name || provider}</option>)}
                                </ChoiceSelect>
                              </Field>
                              <Field label={tProvider("modelLabel")}>
                                <ChoiceSelect
                                  value={modelInputValue}
                                  onValueChange={(value) => {
                                    patchTierRoute(index, routeIndex, {
                                      model: value,
                                      models: splitRouteValues(value),
                                    });
                                    if (row.tier === INTENT_TIER && value)
                                      setIntentTier(INTENT_TIER);
                                  }}
                                  disabled={!route.provider}
                                  searchable
                                  placeholder={tModel("selectModel")}
                                  aria-label={`${tierLabel(row.tier, tModel)} · ${tModel("routeLabel", { index: routeIndex + 1 })} · ${tProvider("modelLabel")}`}
                                  className="w-full font-mono"
                                >
                                  {modelOptions.map((model) => <option key={model} value={model}>{model}</option>)}
                                </ChoiceSelect>
                              </Field>
                              <Field label={tModel("contextWindowLabel")} hint={tModel("contextWindowHint")}>
                                <input
                                  className="input-dark font-mono"
                                  type="number"
                                  min={MIN_MODEL_CONTEXT_WINDOW}
                                  max={MAX_MODEL_CONTEXT_WINDOW}
                                  step={1000}
                                  value={route.context_window ?? DEFAULT_MODEL_CONTEXT_WINDOW}
                                  onChange={(event) => {
                                    const value = Number(event.currentTarget.value);
                                    if (Number.isFinite(value) && value >= MIN_MODEL_CONTEXT_WINDOW) {
                                      patchTierRoute(index, routeIndex, {
                                        context_window: Math.min(MAX_MODEL_CONTEXT_WINDOW, Math.round(value)),
                                      });
                                    }
                                  }}
                                  aria-label={`${tierLabel(row.tier, tModel)} · ${tModel("routeLabel", { index: routeIndex + 1 })} · ${tModel("contextWindowLabel")}`}
                                />
                              </Field>
                              <Field label={tModel("reasoningEffortLabel")}>
                                <PortalSelect
                                  value={route.reasoning_effort || ""}
                                  onChange={(value) => patchTierRoute(index, routeIndex, { reasoning_effort: value })}
                                  options={[
                                    { value: "", label: tModel("reasoningEffortDefault") },
                                    ...reasoningLevels.map((level) => {
                                      let label: string;
                                      try {
                                        label = tModel(`reasoningEffortLevel.${level}` as never);
                                      } catch {
                                        label = prettifyReasoningLevel(level);
                                      }
                                      return { value: level, label };
                                    }),
                                  ]}
                                  size="sm"
                                  className="!min-h-[34px]"
                                  ariaLabel={`${tierLabel(row.tier, tModel)} · ${tModel("routeLabel", { index: routeIndex + 1 })} · ${tModel("reasoningEffortLabel")}`}
                                />
                              </Field>
                            </div>
                            <div className="mt-2 flex flex-wrap items-center gap-2 text-xs text-[color:var(--text-muted)]">
                              <span>{zh ? "连接覆盖：" : "Connection overrides: "}{route.base_url || (zh ? "URL 继承" : "URL inherited")} · {route.provider_key_ref ? (zh ? "路由凭据" : "Route credentials") : (zh ? "凭据继承" : "Credentials inherited")}</span>
                              <button type="button" className="btn btn-ghost" disabled={!route.base_url && !route.provider_key_ref && !route.provider_key_env && !route.kind}
                                onClick={() => patchTierRoute(index, routeIndex, { base_url: "", provider_key_ref: "", provider_key_refs: [], provider_key_env: "", provider_key: "", provider_keys: [], kind: "" })}>
                                {zh ? "恢复连接继承" : "Inherit connection"}
                              </button>
                            </div>
                          </ModelRoutePriority>
                        );
                      })}
                      <div className="flex flex-wrap items-end gap-3">
                        <button
                          type="button"
                          className="btn btn-ghost"
                          onClick={() => addTierRoute(index)}
                        >
                          <PlusIcon size={14} />
                          {tModel("addRoute")}
                        </button>
                      </div>
                    </div>
                  </div>
                );
              })}
            </div>
            </Advanced> : null}
          </Card>
          )}
          {setupError ? <p role="alert" className="text-sm text-danger">{setupError}</p> : null}
        </form>
      ) : null}

      {effectiveSettingsTab === "access" && (!inSectionMode || forceSection === "access") ? (
        <div
          id={settingsPanelId("access")}
          role="region"
          aria-label={tTabs("access")}
          className="grid grid-cols-1 gap-5"
        >
          <div className="space-y-5">
          <Card
            title={tAuth("title")}
            description={tAuth("description")}
            actions={
              <Pill tone={authStatus?.password_configured ? "ok" : "warn"}>
                {authStatus?.password_configured ? tAuth("configured") : tAuth("notConfigured")}
              </Pill>
            }
          >
            <div className="space-y-3">
              {authStatus?.password_configured ? (
                <Field label={tAuth("currentPassword")} hint={tAuth("requiredForRotation")}>
                  <input
                    className="input-dark text-xs"
                    type="password"
                    autoComplete="current-password"
                    value={currentAdminPassword}
                    onChange={(e) => setCurrentAdminPassword(e.target.value)}
                    placeholder="••••••••"
                  />
                </Field>
              ) : null}
              <Field label={tAuth("newPassword")} hint={tAuth("minLength")}>
                <input
                  className="input-dark text-xs"
                  type="password"
                  autoComplete="new-password"
                  value={newAdminPassword}
                  onChange={(e) => setNewAdminPassword(e.target.value)}
                  placeholder="••••••••"
                />
              </Field>
              <Field label={tAuth("confirmPassword")}>
                <input
                  className="input-dark text-xs"
                  type="password"
                  autoComplete="new-password"
                  value={confirmAdminPassword}
                  onChange={(e) => setConfirmAdminPassword(e.target.value)}
                  placeholder="••••••••"
                />
              </Field>
              <div className="flex flex-wrap justify-end gap-2">
                <button
                  type="button"
                  className="btn btn-ghost"
                  onClick={logoutAdmin}
                >
                  {tAuth("clearLogin")}
                </button>
                <button
                  type="button"
                  className="btn btn-primary"
                  onClick={() => void saveAdminPassword()}
                  disabled={
                    authBusy ||
                    !newAdminPassword ||
                    !confirmAdminPassword ||
                    Boolean(authStatus?.password_configured && !currentAdminPassword)
                  }
                >
                  {authBusy ? tCommon("saving") : tAuth("savePassword")}
                </button>
              </div>
              <p className="text-[11px] leading-5 text-ink-500">
                {tAuth("note")}
              </p>
            </div>
          </Card>
          <DesktopSettings passwordConfigured={Boolean(authStatus?.password_configured)} />
          </div>
        </div>
      ) : null}

      {effectiveSettingsTab === "gateway" && forceSection === "gateway" ? (
        <div className="space-y-5">
          <GatewayChannelsPanel />
        </div>
      ) : null}

      {effectiveSettingsTab === "runtime" && (!inSectionMode || forceSection === "runtime") ? (
        <div
          id={settingsPanelId("runtime")}
          role="region"
          aria-label={tTabs("runtime")}
          className="grid grid-cols-1 gap-5 xl:grid-cols-[380px_1fr]"
        >
          <div className="xl:col-span-2">
            <WorkspaceSyncPanel />
            <a className="mt-3 inline-block text-xs text-[color:var(--text-muted)] underline" href="/advanced">{text("copy.components_SettingsWorkspace.002")}</a>
          </div>
          <div className="xl:col-span-2">
            <Card
              title={tProxy("title")}
              description={tProxy("description")}
              actions={
                <Pill tone={proxyEnabled ? (proxyStatus?.applied?.error ? "warn" : "ok") : "neutral"}>
                  {proxyEnabled ? tProxy("enabled") : tProxy("disabled")}
                </Pill>
              }
            >
              <div className="grid grid-cols-1 gap-4 xl:grid-cols-[minmax(0,1fr)_360px]">
                <div className="space-y-4">
                  <div className="grid grid-cols-1 gap-3 md:grid-cols-3">
                    <Row label={tProxy("enableProxy")} desc={tProxy("enableProxyDesc")}>
                      <SwitchControl
                        checked={proxyEnabled}
                        label={proxyEnabled ? tProxy("enabled") : tProxy("disabled")}
                        onCheckedChange={(v) => setProxyEnabled(v)}
                      />
                    </Row>
                    <Field label={tProxy("mode")}>
                      <Select
                        value={proxyMode}
                        onChange={(v) => setProxyMode(v === "pool" ? "pool" : "direct")}
                        options={[
                          { value: "direct", label: tProxy("modeDirect") },
                          { value: "pool", label: tProxy("modePool") },
                        ]}
                      />
                    </Field>
                    <Field label={tProxy("preset")} hint={tProxy("presetHint")}>
                      <Select
                        value={proxyPreset}
                        onChange={applyProxyPreset}
                        options={(proxyPresets.length ? proxyPresets : [{ id: "custom", label: tProxy("customProxy") }]).map((row) => ({
                          value: row.id,
                          label: row.label,
                        }))}
                      />
                    </Field>
                  </div>

                  {proxyMode === "direct" ? (
                    <div className="grid grid-cols-1 gap-3 md:grid-cols-3">
                      <Field
                        label={tProxy("allProxy")}
                        hint={proxyRefs.all_url_ref ? `stored ${proxyStatus?.config?.all_url_preview || proxyRefs.all_url_ref}` : tProxy("allProxyHint")}
                      >
                        <input
                          className="input-dark font-mono text-xs"
                          value={proxyAllUrl}
                          onChange={(e) => {
                            setProxyAllUrl(e.target.value);
                            if (e.target.value.trim()) setProxyRefs((p) => ({ ...p, all_url_ref: "" }));
                          }}
                          placeholder={proxyRefs.all_url_ref ? "stored in vault://; paste to replace" : "http://127.0.0.1:7890"}
                          autoCapitalize="off"
                          autoCorrect="off"
                          spellCheck={false}
                        />
                      </Field>
                      <Field
                        label={tProxy("httpProxy")}
                        hint={proxyRefs.http_url_ref ? `stored ${proxyStatus?.config?.http_url_preview || proxyRefs.http_url_ref}` : tProxy("optional")}
                      >
                        <input
                          className="input-dark font-mono text-xs"
                          value={proxyHttpUrl}
                          onChange={(e) => {
                            setProxyHttpUrl(e.target.value);
                            if (e.target.value.trim()) setProxyRefs((p) => ({ ...p, http_url_ref: "" }));
                          }}
                          placeholder={proxyRefs.http_url_ref ? "stored in vault://; paste to replace" : "http://host:port"}
                          autoCapitalize="off"
                          autoCorrect="off"
                          spellCheck={false}
                        />
                      </Field>
                      <Field
                        label={tProxy("httpsProxy")}
                        hint={proxyRefs.https_url_ref ? `stored ${proxyStatus?.config?.https_url_preview || proxyRefs.https_url_ref}` : tProxy("optional")}
                      >
                        <input
                          className="input-dark font-mono text-xs"
                          value={proxyHttpsUrl}
                          onChange={(e) => {
                            setProxyHttpsUrl(e.target.value);
                            if (e.target.value.trim()) setProxyRefs((p) => ({ ...p, https_url_ref: "" }));
                          }}
                          placeholder={proxyRefs.https_url_ref ? "stored in vault://; paste to replace" : "http://host:port"}
                          autoCapitalize="off"
                          autoCorrect="off"
                          spellCheck={false}
                        />
                      </Field>
                    </div>
                  ) : (
                    <div className="grid grid-cols-1 gap-3 md:grid-cols-[1fr_220px]">
                      <Field
                        label={tProxy("poolApiUrl")}
                        hint={proxyRefs.pool_url_ref ? `stored ${proxyStatus?.config?.pool_url_preview || proxyRefs.pool_url_ref}` : tProxy("poolApiUrlHint")}
                      >
                        <input
                          className="input-dark font-mono text-xs"
                          value={proxyPoolUrl}
                          onChange={(e) => {
                            setProxyPoolUrl(e.target.value);
                            if (e.target.value.trim()) setProxyRefs((p) => ({ ...p, pool_url_ref: "" }));
                          }}
                          placeholder={proxyRefs.pool_url_ref ? "stored in vault://; paste to replace" : "http://127.0.0.1:5010/get/?type=https"}
                          autoCapitalize="off"
                          autoCorrect="off"
                          spellCheck={false}
                        />
                      </Field>
                      <Field label={tProxy("responseFormat")}>
                        <Select
                          value={proxyPoolFormat}
                          onChange={setProxyPoolFormat}
                          options={[
                            { value: "auto", label: tProxy("formatAuto") },
                            { value: "jhao_json", label: "jhao JSON" },
                            { value: "smart_json", label: tProxy("formatSmartJson") },
                            { value: "json", label: tProxy("formatGenericJson") },
                            { value: "text", label: tProxy("formatPlainText") },
                          ]}
                        />
                      </Field>
                    </div>
                  )}

                  <Field label="NO_PROXY" hint={tProxy("noProxyHint")}>
                    <input
                      className="input-dark font-mono text-xs"
                      value={proxyNoProxy}
                      onChange={(e) => setProxyNoProxy(e.target.value)}
                      placeholder={DEFAULT_NO_PROXY}
                      autoCapitalize="off"
                      autoCorrect="off"
                      spellCheck={false}
                    />
                  </Field>

                  <div className="flex flex-wrap items-center gap-2">
                    <button
                      type="button"
                      className="btn btn-primary"
                      onClick={() => void saveNetworkProxy()}
                      disabled={Boolean(proxyBusy)}
                    >
                      <CheckIcon size={14} />
                      {proxyBusy === "save" ? tCommon("saving") : tProxy("saveProxy")}
                    </button>
                    <button
                      type="button"
                      className="btn btn-ghost"
                      onClick={() => void loadNetworkProxy()}
                      disabled={Boolean(proxyBusy)}
                    >
                      <RefreshIcon size={14} />
                      {tCommon("refresh")}
                    </button>
                    {proxyStatus?.applied?.error ? (
                      <span className="text-[11px] text-amber-300">{proxyStatus.applied.error}</span>
                    ) : null}
                  </div>
                </div>

                <div className="space-y-3 rounded-lg border border-brand-500/10 bg-ink-950/30 p-3">
                  <div>
                    <div className="text-[11px] text-ink-500 font-medium">{tProxy("effectiveEnv")}</div>
                    <div className="mt-2 embedded-list-scroll-sm rounded-lg border border-brand-500/10 bg-ink-950/40">
                      {Object.entries(proxyStatus?.applied?.env || {}).length ? (
                        Object.entries(proxyStatus?.applied?.env || {}).map(([key, value]) => (
                          <div key={key} className="border-b border-brand-500/10 px-3 py-2 last:border-b-0">
                            <div className="font-mono text-[11px] text-ink-200">{key}</div>
                            <div className="truncate font-mono text-[11px] text-ink-500">{String(value)}</div>
                          </div>
                        ))
                      ) : (
                        <div className="px-3 py-6 text-center text-[12px] text-ink-500">
                          {tProxy("noManagedEnv")}
                        </div>
                      )}
                    </div>
                  </div>

                  <Field label={tProxy("probeUrl")}>
                    <input
                      className="input-dark font-mono text-xs"
                      value={proxyTestUrl}
                      onChange={(e) => setProxyTestUrl(e.target.value)}
                      placeholder="https://httpbin.org/ip"
                      spellCheck={false}
                    />
                  </Field>
                  <div className="flex flex-wrap items-center gap-2">
                    <button
                      type="button"
                      className="btn btn-ghost"
                      onClick={() => void testNetworkProxy()}
                      disabled={Boolean(proxyBusy) || !proxyEnabled}
                    >
                      <SearchIcon size={14} />
                      {proxyBusy === "test" ? tProxy("testing") : tProxy("testProxy")}
                    </button>
                    {proxyTestResult ? (
                      <span className={`font-mono text-[11px] ${proxyTestResult.startsWith("ok") ? "text-emerald-300" : "text-rose-300"}`}>
                        {proxyTestResult}
                      </span>
                    ) : null}
                  </div>
                  <p className="text-[11px] leading-5 text-ink-500">
                    URLs with username/password are stored as vault:// refs. Pool mode resolves one proxy when the runtime applies the setting.
                  </p>
                </div>
              </div>
            </Card>
          </div>

          <div className="xl:col-span-2">
            <Advanced
              title={
                <span className="inline-flex items-center gap-2">
                  {tTunnel("title")}
                  <TermTip term="tunnel" />
                </span>
              }
              description={tTunnel("description")}
              storageKey="nerya.settings.runtime.advanced.tunnel"
              count={
                tunnelRunningCount
                  ? tTunnel("statusPill", { running: tunnelRunningCount, enabled: tunnelEnabledCount })
                  : tunnelEnabledCount || undefined
              }
            >
              <div className="mb-4 rounded-lg border border-brand-500/10 bg-ink-950/30 p-3">
                <div className="grid grid-cols-1 gap-3 lg:grid-cols-[1fr_180px_auto]">
                  <div>
                    <div className="text-[12px] font-medium text-ink-100">
                      {tTunnel("dashboardEndpointTitle")}
                    </div>
                    <div className="mt-1 font-mono text-[11px] text-ink-500">
                      {dashboardStatus?.config.url || tunnelTargetHint("dashboard", tunnelsStatus)}
                    </div>
                    <p className="mt-1 text-[11px] leading-5 text-ink-500">
                      {tTunnel("dashboardEndpointDesc")}
                    </p>
                  </div>
                  <Field label={tTunnel("dashboardPort")}>
                    <input
                      className="input-dark font-mono text-xs"
                      value={dashboardPortDraft}
                      onChange={(e) => setDashboardPortDraft(e.target.value.replace(/[^\d]/g, "").slice(0, 5))}
                      placeholder="18380"
                      inputMode="numeric"
                    />
                  </Field>
                  <div className="flex items-end gap-2">
                    <button
                      type="button"
                      className="btn btn-primary"
                      onClick={() => void saveDashboardEndpoint()}
                      disabled={dashboardBusy}
                    >
                      <CheckIcon size={14} />
                      {dashboardBusy ? tCommon("saving") : tTunnel("dashboardSave")}
                    </button>
                    <button
                      type="button"
                      className="btn btn-ghost"
                      onClick={() => void loadNetworkDashboard()}
                      disabled={dashboardBusy}
                      title={tCommon("refresh")}
                    >
                      <RefreshIcon size={14} />
                    </button>
                  </div>
                </div>
                {dashboardMessage ? (
                  <div className="mt-3 rounded-md border border-emerald-400/20 bg-emerald-400/10 px-3 py-2 text-[12px] text-emerald-200">
                    {dashboardMessage}
                  </div>
                ) : null}
                <div className="mt-2 text-[11px] leading-5 text-ink-500">
                  {tTunnel("dashboardRestartHint")}
                </div>
              </div>
              <div className="grid grid-cols-1 gap-4 xl:grid-cols-[320px_1fr]">
                <div className="embedded-list-scroll-sm rounded-lg border border-brand-500/10 bg-ink-950/35">
                  {tunnelsStatus?.providers?.length ? tunnelsStatus.providers.map((row) => (
                    <button
                      key={row.spec.id}
                      type="button"
                      className={`flex w-full items-start justify-between gap-3 border-b border-brand-500/10 px-3 py-3 text-left last:border-b-0 ${
                        selectedTunnelProvider === row.spec.id ? "bg-brand-500/10" : "hover:bg-white/5"
                      }`}
                      onClick={() => setSelectedTunnelProvider(row.spec.id)}
                    >
                      <div className="min-w-0">
                        <div className="text-[13px] font-medium text-ink-100">{row.spec.label}</div>
                        <div className="mt-1 line-clamp-2 text-[11px] leading-4 text-ink-500">{row.spec.free_tier}</div>
                      </div>
                      <div className="flex shrink-0 flex-col items-end gap-1">
                        <Pill tone={row.installed ? "ok" : "warn"}>
                          {row.installed ? tTunnel("installed") : tTunnel("notInstalled")}
                        </Pill>
                        {row.running ? <Pill tone="brand">{tTunnel("running")}</Pill> : null}
                      </div>
                    </button>
                  )) : (
                    <div className="px-3 py-10 text-center text-[12px] text-ink-500">
                      {tTunnel("loading")}
                    </div>
                  )}
                </div>

                {selectedTunnel && selectedTunnelDraft ? (
                  <div className="space-y-4">
                    <div className="grid grid-cols-1 gap-3 md:grid-cols-3">
                      <Row label={tTunnel("enableLabel")} desc={tTunnel("enableDesc")}>
                        <SwitchControl
                          checked={selectedTunnelDraft.enabled}
                          label={selectedTunnelDraft.enabled ? tTabs("enabled") : tTabs("disabled")}
                          onCheckedChange={(v) => patchTunnelDraft(selectedTunnel.spec.id, { enabled: v })}
                        />
                      </Row>
                      <Field label={tTunnel("targetLabel")} hint={tunnelTargetHint(selectedTunnelDraft.target, tunnelsStatus)}>
                        <Select
                          value={selectedTunnelDraft.target}
                          onChange={(v) => patchTunnelDraft(selectedTunnel.spec.id, { target: v === "api" || v === "custom" ? v : "dashboard" })}
                          options={[
                            { value: "dashboard", label: tTunnel("targetDashboard") },
                            { value: "api", label: tTunnel("targetApi") },
                            { value: "custom", label: tTunnel("targetCustom") },
                          ]}
                        />
                      </Field>
                      <Field label={selectedTunnel.spec.id === "cloudflare" ? tTunnel("cloudflareMode") : tTunnel("modeLabel")}>
                        {selectedTunnel.spec.id === "cloudflare" ? (
                          <Select
                            value={selectedTunnelDraft.cloudflare_mode}
                            onChange={(v) => patchTunnelDraft(selectedTunnel.spec.id, { cloudflare_mode: v === "token" ? "token" : "quick" })}
                            options={[
                              { value: "quick", label: tTunnel("cloudflareQuick") },
                              { value: "token", label: tTunnel("cloudflareToken") },
                            ]}
                          />
                        ) : (
                          <Select
                            value={selectedTunnelDraft.mode}
                            onChange={(v) => patchTunnelDraft(selectedTunnel.spec.id, { mode: v })}
                            options={(selectedTunnel.spec.modes || ["public"]).map((mode) => ({
                              value: mode,
                              label: mode,
                            }))}
                          />
                        )}
                      </Field>
                    </div>

                    {selectedTunnelDraft.target === "custom" ? (
                      <Field label={tTunnel("customUrl")} hint={tTunnel("customUrlHint")}>
                        <input
                          className="input-dark font-mono text-xs"
                          value={selectedTunnelDraft.target_url}
                          onChange={(e) => patchTunnelDraft(selectedTunnel.spec.id, { target_url: e.target.value })}
                          placeholder="http://127.0.0.1:8080"
                          autoCapitalize="off"
                          autoCorrect="off"
                          spellCheck={false}
                        />
                      </Field>
                    ) : null}

                    {selectedTunnel.spec.token_label ? (
                      <div className="grid grid-cols-1 gap-3 md:grid-cols-2">
                        <Field
                          label={selectedTunnel.spec.token_label}
                          hint={selectedTunnelDraft.token_ref ? tTunnel("tokenStored", { ref: selectedTunnelDraft.token_ref }) : tTunnel("tokenHint")}
                        >
                          <input
                            className="input-dark font-mono text-xs"
                            type="password"
                            value={selectedTunnelDraft.token}
                            onChange={(e) => patchTunnelDraft(selectedTunnel.spec.id, { token: e.target.value })}
                            placeholder={selectedTunnelDraft.token_ref ? tTunnel("tokenReplacePlaceholder") : tTunnel("tokenPlaceholder")}
                            autoCapitalize="off"
                            autoCorrect="off"
                            spellCheck={false}
                          />
                        </Field>
                        <Field label={tTunnel("hostnameLabel")} hint={tTunnel("hostnameHint")}>
                          <input
                            className="input-dark font-mono text-xs"
                            value={selectedTunnelDraft.public_hostname}
                            onChange={(e) => patchTunnelDraft(selectedTunnel.spec.id, { public_hostname: e.target.value })}
                            placeholder={selectedTunnel.spec.id === "ngrok" ? "https://your-domain.ngrok.app" : ""}
                            autoCapitalize="off"
                            autoCorrect="off"
                            spellCheck={false}
                          />
                        </Field>
                      </div>
                    ) : null}

                    <div className="rounded-lg border border-brand-500/10 bg-ink-950/30 p-3">
                      <div className="grid grid-cols-1 gap-3 md:grid-cols-3">
                        <Metric
                          label={tTunnel("dependency")}
                          value={selectedTunnel.installed ? tTunnel("installed") : tTunnel("notInstalled")}
                          detail={selectedTunnel.version || selectedTunnel.executable_path || selectedTunnel.spec.install_hint}
                          icon={<SettingsIcon size={16} />}
                        />
                        <Metric
                          label={tTunnel("auth")}
                          value={tunnelsStatus?.auth.admin_password_configured ? tAuth("configured") : tAuth("notConfigured")}
                          detail={selectedTunnelDraft.target === "api" ? tTunnel("apiAuthDetail") : tTunnel("dashboardAuthDetail")}
                          icon={<CheckIcon size={16} />}
                        />
                        <Metric
                          label={tTunnel("process")}
                          value={selectedTunnel.running ? tTunnel("running") : tTunnel("stopped")}
                          detail={selectedTunnel.state?.log_path || tTunnel("noProcess")}
                          icon={<SparkIcon size={16} />}
                        />
                      </div>
                    </div>

                    {selectedTunnelExternalUrls.length ? (
                      <div className="rounded-lg border border-emerald-400/20 bg-emerald-400/10 p-3">
                        <div className="text-[12px] font-medium text-emerald-400">
                          {tTunnel("externalUrls")}
                        </div>
                        <div className="mt-2 flex flex-col gap-2">
                          {selectedTunnelExternalUrls.map((url) => (
                            <a
                              key={url}
                              href={url}
                              target="_blank"
                              rel="noreferrer"
                              className="truncate font-mono text-[12px] text-emerald-100 underline decoration-emerald-300/40 underline-offset-4 hover:text-white"
                              title={url}
                            >
                              {url}
                            </a>
                          ))}
                        </div>
                      </div>
                    ) : null}

                    <div className="flex flex-wrap items-center gap-2">
                      <button
                        type="button"
                        className="btn btn-primary"
                        onClick={() => void saveTunnelConfig(selectedTunnel.spec.id)}
                        disabled={Boolean(tunnelBusy)}
                      >
                        <CheckIcon size={14} />
                        {tunnelBusy === `save:${selectedTunnel.spec.id}` ? tCommon("saving") : tTunnel("save")}
                      </button>
                      <button
                        type="button"
                        className="btn btn-ghost"
                        onClick={() => void installTunnelProvider(selectedTunnel.spec.id)}
                        disabled={Boolean(tunnelBusy) || selectedTunnel.installed}
                      >
                        <PlusIcon size={14} />
                        {tunnelBusy === `install:${selectedTunnel.spec.id}` ? tCommon("working") : tTunnel("install")}
                      </button>
                      <button
                        type="button"
                        className="btn btn-ghost"
                        onClick={() => void startTunnelProvider(selectedTunnel.spec.id)}
                        disabled={Boolean(tunnelBusy) || !selectedTunnelDraft.enabled || selectedTunnel.running}
                      >
                        <SparkIcon size={14} />
                        {tunnelBusy === `start:${selectedTunnel.spec.id}` ? tCommon("working") : tTunnel("start")}
                      </button>
                      <button
                        type="button"
                        className="btn btn-ghost"
                        onClick={() => void stopTunnelProvider(selectedTunnel.spec.id)}
                        disabled={Boolean(tunnelBusy) || !selectedTunnel.running}
                      >
                        {tunnelBusy === `stop:${selectedTunnel.spec.id}` ? tCommon("working") : tTunnel("stop")}
                      </button>
                      <button
                        type="button"
                        className="btn btn-ghost"
                        onClick={() => void loadNetworkTunnels()}
                        disabled={Boolean(tunnelBusy)}
                      >
                        <RefreshIcon size={14} />
                        {tCommon("refresh")}
                      </button>
                    </div>

                    {tunnelMessage ? (
                      <div className="rounded-md border border-emerald-400/20 bg-emerald-400/10 px-3 py-2 text-[12px] text-emerald-200">
                        {tunnelMessage}
                      </div>
                    ) : null}
                    <p className="text-[11px] leading-5 text-ink-500">
                      {tTunnel("securityNote")}
                    </p>
                  </div>
                ) : null}
              </div>
            </Advanced>
          </div>

          {/* Pointer to the standalone /advanced page so the power-user
              shortcut promised by app/advanced/page.tsx is actually
              reachable from the UI. */}
          <div className="xl:col-span-2 flex justify-end">
            <a className="btn btn-ghost" href="/advanced">
              {t("openAdvanced")}
            </a>
          </div>
        </div>
      ) : null}

      {effectiveSettingsTab === "mcp" ? <McpSettingsPanel /> : null}

      {effectiveSettingsTab === "envvault" && forceSection === "envvault" ? (
        <div
          id={settingsPanelId("envvault")}
          role="region"
          aria-label={tTabs("envvault")}
          className="grid grid-cols-1 gap-5 xl:grid-cols-2"
        >
          <Card
            title={tEnvCard("title")}
            description={tEnvCard("description")}
            actions={<Pill tone={runtimeEnv.length ? "ok" : "warn"}>{tEnvCard("configuredCount", { count: runtimeEnv.length })}</Pill>}
          >
            <div className="space-y-3">
              <div className="embedded-list-scroll-sm rounded-lg border border-brand-500/10 bg-ink-950/35">
                {runtimeEnv.length ? runtimeEnv.map((row) => (
                  <div
                    key={row.name}
                    className="flex items-center justify-between gap-3 border-b border-brand-500/10 px-3 py-2 text-xs last:border-b-0"
                  >
                    <span className="min-w-0">
                      <span className="block truncate font-mono text-ink-100">{row.name}</span>
                      <span className="block truncate text-[11px] text-ink-500">{row.ref} · {row.preview}</span>
                    </span>
                    <button
                      type="button"
                      className="btn btn-ghost px-2 py-1 text-[11px]"
                      onClick={() => void deleteRuntimeEnv(row.name)}
                      disabled={securityBusy === `env:delete:${row.name}`}
                    >
                      {securityBusy === `env:delete:${row.name}` ? tCommon("working") : tCommon("delete")}
                    </button>
                  </div>
                )) : (
                  <div className="px-3 py-6 text-center text-[12px] text-ink-500">
                    {tEnvCard("empty")}
                  </div>
                )}
              </div>

              <div className="grid grid-cols-1 gap-3">
                <Field label={tEnvCard("nameLabel")} hint={tEnvCard("nameHint")}>
                  <input
                    className="input-dark font-mono text-xs"
                    value={envNameDraft}
                    onChange={(e) => setEnvNameDraft(e.target.value)}
                    placeholder="OPENAI_API_KEY"
                    autoCapitalize="off"
                    autoCorrect="off"
                  />
                </Field>
                <Field label={tEnvCard("valueLabel")} hint={tEnvCard("valueHint")}>
                  <input
                    className="input-dark font-mono text-xs"
                    type="password"
                    value={envValueDraft}
                    onChange={(e) => setEnvValueDraft(e.target.value)}
                    placeholder="paste secret value"
                  />
                </Field>
              </div>

              <div className="flex flex-wrap justify-end gap-2">
                <button
                  type="button"
                  className="btn btn-ghost"
                  onClick={() => void loadSecurityRuntime()}
                  disabled={Boolean(securityBusy)}
                >
                  <RefreshIcon size={14} />
                  {tCommon("refresh")}
                </button>
                <button
                  type="button"
                  className="btn btn-primary"
                  onClick={() => void saveRuntimeEnv()}
                  disabled={Boolean(securityBusy) || !envNameDraft.trim()}
                >
                  <CheckIcon size={14} />
                  {securityBusy === "env:save" ? tCommon("saving") : tEnvCard("save")}
                </button>
              </div>

              <p className="text-[11px] leading-5 text-ink-500">
                {tEnvCard("footnote")}
              </p>
            </div>
          </Card>

          <Card
            title={tVaultCard("title")}
            description={tVaultCard("description")}
            actions={<Pill tone={vaultRefs.length ? "brand" : "warn"}>{tVaultCard("refsCount", { count: vaultRefs.length })}</Pill>}
          >
            {/* This card shares a 2-col section grid with the runtime-env
                card, so a nested side-by-side split squeezes the refs list
                into a ~120px gutter (letters wrap vertically). Stack
                list → form instead; the form itself goes 2-col on sm+. */}
            <div className="space-y-4">
              <div className="embedded-list-scroll rounded-lg border border-brand-500/10 bg-ink-950/35">
                {vaultRefs.length ? vaultRefs.map((row) => (
                  <div
                    key={row.ref}
                    className="grid grid-cols-[minmax(0,1fr)_auto] gap-3 border-b border-brand-500/10 px-3 py-2 text-xs last:border-b-0"
                  >
                    <div className="min-w-0">
                      <div className="flex flex-wrap items-center gap-2">
                        <span className="truncate font-mono text-ink-100">{row.ref}</span>
                        <Pill tone={row.kind === "env" ? "ok" : "brand"}>{row.kind}</Pill>
                      </div>
                      <div className="mt-1 flex flex-wrap gap-2 text-[11px] text-ink-500">
                        <span>{tVaultCard("previewLabel")} {row.preview}</span>
                        <span>{tVaultCard("shaLabel")} {row.fingerprint}</span>
                        {row.scope?.length ? <span>{row.scope.join(", ")}</span> : null}
                      </div>
                    </div>
                    <button
                      type="button"
                      className="btn btn-ghost h-8 px-2 text-[11px]"
                      onClick={() => void deleteVaultSecret(row.name)}
                      disabled={securityBusy === `vault:delete:${row.name}`}
                    >
                      {securityBusy === `vault:delete:${row.name}` ? tCommon("working") : tCommon("delete")}
                    </button>
                  </div>
                )) : (
                  <div className="px-3 py-10 text-center text-[12px] text-ink-500">
                    {tVaultCard("empty")}
                  </div>
                )}
              </div>

              <div className="space-y-3 rounded-lg border border-brand-500/10 bg-ink-950/30 p-3">
                <Field label={tVaultCard("nameLabel")} hint={tVaultCard("nameHint")}>
                  <input
                    className="input-dark font-mono text-xs"
                    value={vaultNameDraft}
                    onChange={(e) => setVaultNameDraft(e.target.value)}
                    placeholder="mcp_fred_api_key"
                    autoCapitalize="off"
                    autoCorrect="off"
                  />
                </Field>
                <Field label={tVaultCard("kindLabel")}>
                  <input
                    className="input-dark font-mono text-xs"
                    value={vaultKindDraft}
                    onChange={(e) => setVaultKindDraft(e.target.value)}
                    placeholder="bearer"
                  />
                </Field>
                <Field label={tVaultCard("scopesLabel")} hint={tVaultCard("scopesHint")}>
                  <input
                    className="input-dark font-mono text-xs"
                    value={vaultScopeDraft}
                    onChange={(e) => setVaultScopeDraft(e.target.value)}
                    placeholder="mcp.read, env"
                  />
                </Field>
                <Field label={tVaultCard("valueLabel")} hint={tVaultCard("valueHint")}>
                  <input
                    className="input-dark font-mono text-xs"
                    type="password"
                    value={vaultValueDraft}
                    onChange={(e) => setVaultValueDraft(e.target.value)}
                    placeholder="paste secret value"
                  />
                </Field>
                <button
                  type="button"
                  className="btn btn-primary w-full"
                  onClick={() => void saveVaultSecret()}
                  disabled={Boolean(securityBusy) || !vaultNameDraft.trim() || !vaultValueDraft}
                >
                  <PlusIcon size={14} />
                  {securityBusy === "vault:save" ? tCommon("saving") : tVaultCard("save")}
                </button>
              </div>
            </div>
          </Card>
        </div>
      ) : null}

      {effectiveSettingsTab === "search" ? <SearchSettings /> : null}

      {effectiveSettingsTab === "envvault" ? <DataServiceSettings /> : null}

      {effectiveSettingsTab === "interface" && !inSectionMode ? <InterfaceSettings /> : null}
        </fieldset>
      </div>
    </PageBody>
  );
}
