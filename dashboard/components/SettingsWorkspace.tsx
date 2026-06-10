"use client";

import { useEffect, useMemo, useRef, useState, type ReactNode } from "react";
import { useTranslations } from "next-intl";
import { Advanced, Card, ErrorBanner, PageBody, PageHeader, Pill } from "./Page";
import { GatewayChannelsPanel } from "./GatewayChannelsPanel";
import { MemoryEvidencePanel } from "./MemoryEvidencePanel";
import { MemoryProfilePanel } from "./MemoryProfilePanel";
import { RuntimeFlagsPanel } from "./RuntimeFlagsPanel";
import { SwitchControl } from "./SwitchControl";
import { Select as PortalSelect, type SelectOption as PortalSelectOption } from "./Select";
import { PortalDropdown, useDropdown } from "./PortalDropdown";
import { CheckIcon, ChevronDownIcon, PlusIcon, RefreshIcon, SearchIcon, SettingsIcon, SparkIcon, TrashIcon } from "./icons";
import { DEFAULT_SETTINGS, useUiSettings } from "../lib/settings";
import {
  clientApi,
  type GatewayChannelConfig,
  type GatewayPlatformSpec,
  type GatewayUpsertRequest,
  type LlmProviderProfile,
  type LlmRouteConfig,
  type LlmTierConfig,
  type MemoryActivityEvent,
  type MemoryExternalConfig,
  type MemoryNotebookSnapshot,
  type MemoryProviderView,
  type MemoryWriteRuleConfig,
  type OAuthProviderStatus,
  type RuntimeEnvVar,
  type SecretRef,
  type MemoryVectorStatus,
  type AuthStatus,
  type SearchEnginesStatus,
  type SearchEngineStatus,
  type BrowsersStatus,
  type FinancialDatasetsStatus,
  type NetworkProxyPreset,
  type NetworkProxyStatus,
  type NetworkDashboardStatus,
  type NetworkTunnelsStatus,
  type TunnelProviderConfig,
  type TunnelProviderStatus,
} from "../lib/clientApi";
import { clearStoredAuthToken, isLocalDashboardHost, setStoredAuthToken } from "../lib/auth";

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

const SETTINGS_TABS = ["models", "access", "runtime", "capabilityGates", "envvault", "search", "browsers", "memory", "interface"] as const;
type SettingsTabKey = typeof SETTINGS_TABS[number];
const DEFAULT_NO_PROXY = "127.0.0.1,localhost,::1";

// Sub-tabs inside the /settings memory panel. Keeps the panel
// scannable now that it hosts vector index, notebook, activity feed,
// write rules, AND the provider directory.
// memsearch ("vector") used to live in this sub-tab list, but its full
// configuration panel is now inlined into the Selected backend settings
// card — and only rendered when the operator actually chooses memsearch
// as the active backend. The sub-tabs below the card are for things
// that apply regardless of backend (notebook, activity, rules,
// providers, evidence, profile).
const MEMORY_SUBTABS = ["notebook", "activity", "rules", "providers", "evidence", "profile"] as const;
type MemorySubTabKey = typeof MEMORY_SUBTABS[number];
type MemoryBackendChoice = "builtin" | "memsearch" | "agentmemory";

type SettingsTabItem = {
  key: SettingsTabKey;
  label: string;
  description: string;
  meta: ReactNode;
};

type GatewayDraft = {
  channel: string;
  kind: string;
  enabled: boolean;
  mode: string;
  polling: boolean;
  trade_notifications: boolean;
  approvals: boolean;
  auto_reply: boolean;
  allow_unknown_users: boolean;
  group_sessions_per_user: boolean;
  thread_sessions_per_user: boolean;
  topicsCsv: string;
  allowedChatIdsCsv: string;
  allowedUserIdsCsv: string;
  deniedUserIdsCsv: string;
  // Generic per-platform credentials. ``secrets`` holds plaintext values
  // typed in by the operator (vaulted server-side on save when the field
  // ``kind`` is ``secret`` or ``url``; persisted as plaintext in
  // ``messages/channels.yml`` when the kind is ``id``/``opaque``).
  // ``secretRefs`` holds ``vault://...`` pointers returned by the backend
  // after a previous save (or typed by the operator). Keys come from the
  // platform spec (``bot_token``, ``chat_id``, ``app_id``, ``app_secret``,
  // ``signing_secret``, ``verification_token``, ``webhook_url``,
  // ``incoming_webhook_url``, ``status_webhook_url``, ``corp_id``,
  // ``agent_id``, ``phone_number_id``, ``smtp_url``, ``imap_url``, ...).
  secrets: Record<string, string>;
  secretRefs: Record<string, string>;
  username: string;
  avatar_url: string;
  parse_mode: string;
  disable_web_page_preview: boolean;
  timeout_s: string;
};

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

function emptyGatewayDraft(kind = "telegram"): GatewayDraft {
  return {
    channel: kind === "telegram" ? "telegram" : `${kind}_ops`,
    kind,
    enabled: true,
    mode: kind === "telegram" ? "polling" : "send_only",
    polling: kind === "telegram",
    trade_notifications: true,
    approvals: true,
    auto_reply: true,
    allow_unknown_users: true,
    group_sessions_per_user: true,
    thread_sessions_per_user: false,
    topicsCsv: "trades, approvals",
    allowedChatIdsCsv: "",
    allowedUserIdsCsv: "",
    deniedUserIdsCsv: "",
    secrets: {},
    secretRefs: {},
    username: "Nerya",
    avatar_url: "",
    parse_mode: "HTML",
    disable_web_page_preview: true,
    timeout_s: "10",
  };
}

function gatewayCfgValueAsString(cfg: Record<string, unknown>, key: string): string {
  const value = cfg[key];
  if (value === undefined || value === null) return "";
  return String(value);
}

function strConfig(config: Record<string, unknown>, key: string, fallback = ""): string {
  const value = config[key];
  return value === undefined || value === null ? fallback : String(value);
}

function boolConfig(config: Record<string, unknown>, key: string, fallback: boolean): boolean {
  const value = config[key];
  return typeof value === "boolean" ? value : fallback;
}

function listConfig(config: Record<string, unknown>, key: string): string {
  const value = config[key];
  if (Array.isArray(value)) return value.map(String).join(", ");
  return typeof value === "string" ? value : "";
}

function refOf(channel: GatewayChannelConfig, ...keys: string[]): string {
  for (const key of keys) {
    const ref = channel.secret_refs?.[key]?.ref;
    if (ref) return ref;
  }
  return "";
}

function gatewayDraftFromChannel(channel: GatewayChannelConfig,
                                  spec?: GatewayPlatformSpec): GatewayDraft {
  const cfg = channel.config || {};
  const topics = Array.isArray(cfg.topics) ? cfg.topics.map(String).join(", ") : "";
  const secrets: Record<string, string> = {};
  const secretRefs: Record<string, string> = {};
  // Populate secret/url/id field map from the channel snapshot.
  // ``secret_refs`` (vault pointers) come from the safe public envelope
  // for ``secret``/``url`` fields. ``id``/``opaque`` values
  // (chat_id, app_id, corp_id, agent_id, phone_number_id, …) come from
  // ``config`` because they are persisted in plaintext YAML.
  if (spec?.secret_fields) {
    for (const field of spec.secret_fields) {
      const ref = channel.secret_refs?.[field.ref_key]?.ref;
      if (ref) {
        secretRefs[field.key] = ref;
      }
      if (field.kind === "id" || field.kind === "opaque") {
        const direct = gatewayCfgValueAsString(cfg, field.key);
        if (direct) secrets[field.key] = direct;
      }
    }
  } else {
    // Spec hasn't loaded yet — preserve the legacy bot_token / webhook
    // refs so the form still hydrates correctly on the very first paint.
    const botTokenRef = refOf(channel, "bot_token_ref", "token_ref");
    if (botTokenRef) secretRefs["bot_token"] = botTokenRef;
    const webhookRef = refOf(channel, "webhook_url_ref", "url_ref", "incoming_webhook_url_ref");
    if (webhookRef) secretRefs["webhook_url"] = webhookRef;
    const statusRef = refOf(channel, "status_webhook_url_ref");
    if (statusRef) secretRefs["status_webhook_url"] = statusRef;
    // Telegram chat_id is the most common public identifier — surface it
    // even when the platform catalog has not yet loaded so the operator
    // can read their currently-bound chat without waiting for a refresh.
    const directChat = gatewayCfgValueAsString(cfg, "chat_id");
    if (directChat) secrets["chat_id"] = directChat;
  }
  return {
    ...emptyGatewayDraft(channel.kind),
    channel: channel.channel,
    kind: channel.kind,
    enabled: channel.enabled,
    mode: strConfig(cfg, "mode", channel.mode),
    polling: boolConfig(cfg, "polling", channel.kind === "telegram"),
    trade_notifications: boolConfig(cfg, "trade_notifications", true),
    approvals: boolConfig(cfg, "approvals", true),
    auto_reply: boolConfig(cfg, "auto_reply", true),
    allow_unknown_users: boolConfig(cfg, "allow_unknown_users", true),
    group_sessions_per_user: boolConfig(cfg, "group_sessions_per_user", true),
    thread_sessions_per_user: boolConfig(cfg, "thread_sessions_per_user", false),
    topicsCsv: topics || "trades, approvals",
    allowedChatIdsCsv: listConfig(cfg, "allowed_chat_ids"),
    allowedUserIdsCsv: listConfig(cfg, "allowed_user_ids"),
    deniedUserIdsCsv: listConfig(cfg, "denied_user_ids"),
    secrets,
    secretRefs,
    username: strConfig(cfg, "username", "Nerya"),
    avatar_url: strConfig(cfg, "avatar_url"),
    parse_mode: strConfig(cfg, "parse_mode", "HTML"),
    disable_web_page_preview: boolConfig(cfg, "disable_web_page_preview", true),
    timeout_s: strConfig(cfg, "timeout_s", "10"),
  };
}

function gatewayTopics(csv: string): string[] {
  return csv.split(",").map((part) => part.trim()).filter(Boolean);
}

function gatewayCsvList(csv: string): string[] {
  return csv.split(/[,\n]/).map((part) => part.trim()).filter(Boolean);
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

function emptyRoute(): LlmRouteConfig {
  return {
    provider: "",
    model: "",
    models: [],
    base_url: "",
    provider_key_ref: "",
    provider_key_refs: [],
    provider_key: "",
    provider_keys: [],
    kind: "chat_completions",
  };
}

function routesOf(row: LlmTierConfig): LlmRouteConfig[] {
  if (Array.isArray(row.routes) && row.routes.length > 0) {
    return row.routes.map((route) => ({
      provider: route.provider || "",
      model: route.model || splitRouteValues(route.models).join(", "),
      models: route.models || splitRouteValues(route.model),
      base_url: route.base_url || "",
      provider_key_ref: route.provider_key_ref || splitRouteValues(route.provider_key_refs).join(", "),
      provider_key_refs: route.provider_key_refs || splitRouteValues(route.provider_key_ref),
      provider_key: route.provider_key || splitRouteValues(route.provider_keys).join(", "),
      provider_keys: route.provider_keys || splitRouteValues(route.provider_key),
      has_key_ref: route.has_key_ref,
      kind: route.kind || "chat_completions",
      provider_native_web_search: route.provider_native_web_search,
    }));
  }
  if (row.provider || row.model || row.base_url || row.provider_key_ref) {
    return [{
      provider: row.provider || "",
      model: row.model || splitRouteValues(row.models).join(", "),
      models: row.models || splitRouteValues(row.model),
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
    base_url: first.base_url || row.base_url || "",
    provider_key_ref: first.provider_key_ref || row.provider_key_ref || "",
    routes,
  };
}

function emptyTier(tier: string): LlmTierConfig {
  return {
    tier,
    provider: "",
    model: "",
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
        {hint ? <span className="text-[11px] text-ink-500">{hint}</span> : null}
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
    <div className="rounded-lg border border-[color:var(--line)] p-3">
      <div className="flex items-center justify-between gap-3">
        <span className="text-[12px] text-ink-400 font-medium">
          {label}
        </span>
        <span className="text-brand-300">{icon}</span>
      </div>
      <div className="mt-2 text-xl font-medium text-white tabular-nums">{value}</div>
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
    <div className="min-w-[180px] inline-block">
      <PortalSelect
        value={value}
        onChange={(next) => onChange(next)}
        options={options as PortalSelectOption[]}
        size="sm"
      />
    </div>
  );
}

function ModelSelectInput({
  value,
  onChange,
  options,
  disabled,
  placeholder,
  ariaLabel,
  className,
  emptyHint,
}: {
  value: string;
  onChange: (v: string) => void;
  options: string[];
  disabled?: boolean;
  placeholder: string;
  ariaLabel: string;
  className?: string;
  emptyHint: string;
}) {
  const rootRef = useRef<HTMLDivElement | null>(null);
  const inputRef = useRef<HTMLInputElement | null>(null);
  const dropdown = useDropdown();
  const [panelWidth, setPanelWidth] = useState(220);

  useEffect(() => {
    if (!rootRef.current) return;
    const update = () => {
      const next = rootRef.current?.offsetWidth;
      if (next && next > 0) setPanelWidth(next);
    };
    update();
    if (typeof ResizeObserver !== "undefined") {
      const obs = new ResizeObserver(update);
      obs.observe(rootRef.current);
      return () => obs.disconnect();
    }
    window.addEventListener("resize", update);
    return () => window.removeEventListener("resize", update);
  }, []);

  const normalized = useMemo(
    () => Array.from(new Set(options.map((row) => row.trim()).filter(Boolean))),
    [options],
  );

  const filtered = useMemo(() => {
    const q = value.trim().toLowerCase();
    const rows = q ? normalized.filter((row) => row.toLowerCase().includes(q)) : normalized;
    return rows.slice(0, 180);
  }, [normalized, value]);

  return (
    <div ref={rootRef} className="w-full">
      <div
        className={[
          "flex h-8 w-full items-center gap-1 rounded-lg border border-brand-500/15 bg-ink-900/40 pr-1 text-ink-100 transition-colors backdrop-blur-soft",
          dropdown.open ? "border-brand-500/45 bg-ink-900/55" : "hover:border-brand-500/35",
          disabled ? "opacity-60" : "",
          className ?? "",
        ].join(" ")}
      >
        <input
          ref={inputRef}
          className="h-full min-w-0 flex-1 bg-transparent px-2.5 text-[12px] font-mono text-ink-100 outline-none placeholder:text-ink-300 disabled:cursor-not-allowed"
          value={value}
          onFocus={() => {
            if (!disabled) dropdown.setOpen(true);
          }}
          onChange={(e) => onChange(e.target.value)}
          placeholder={placeholder}
          aria-label={ariaLabel}
          autoComplete="off"
          spellCheck={false}
          disabled={disabled}
        />
        <button
          type="button"
          aria-label={ariaLabel}
          disabled={disabled}
          onClick={() => {
            if (disabled) return;
            dropdown.toggle();
            inputRef.current?.focus();
          }}
          className="inline-flex h-6 w-6 items-center justify-center rounded-md text-ink-400 transition-colors hover:bg-brand-500/12 hover:text-ink-200 disabled:cursor-not-allowed disabled:opacity-50"
        >
          <ChevronDownIcon size={14} className={dropdown.open ? "rotate-180 transition-transform" : "transition-transform"} />
        </button>
      </div>
      <PortalDropdown
        open={!disabled && dropdown.open}
        onClose={dropdown.close}
        anchorRef={rootRef}
        align="left"
        width={panelWidth}
        offset={6}
        className="max-h-72 overflow-y-auto rounded-lg border border-[color:var(--line)] bg-ink-950/95 py-1"
      >
        <ul role="listbox" className="text-[13px]">
          {filtered.length === 0 ? (
            <li className="px-3 py-2 text-[12px] text-ink-500 italic">{emptyHint}</li>
          ) : (
            filtered.map((item) => {
              const selected = item === value;
              return (
                <li key={item} role="option" aria-selected={selected}>
                  <button
                    type="button"
                    onClick={() => {
                      onChange(item);
                      dropdown.close();
                      inputRef.current?.focus();
                    }}
                    className={[
                      "flex w-full items-center gap-2 px-3 py-1.5 text-left transition-colors",
                      selected ? "bg-brand-500/14 text-white" : "text-ink-200 hover:bg-brand-500/12",
                    ].join(" ")}
                  >
                    <span className="min-w-0 flex-1 truncate font-mono">{item}</span>
                    {selected ? <span className="h-1.5 w-1.5 rounded-full bg-brand-300" /> : null}
                  </button>
                </li>
              );
            })
          )}
        </ul>
      </PortalDropdown>
    </div>
  );
}

function isSettingsTabKey(value: string): value is SettingsTabKey {
  return (SETTINGS_TABS as readonly string[]).includes(value);
}

function settingsTabId(tab: SettingsTabKey) {
  return `settings-tab-${tab}`;
}

function settingsPanelId(tab: SettingsTabKey) {
  return `settings-panel-${tab}`;
}

function SettingsModuleTabs({
  active,
  ariaLabel,
  items,
  onChange,
}: {
  active: SettingsTabKey;
  ariaLabel: string;
  items: SettingsTabItem[];
  onChange: (tab: SettingsTabKey) => void;
}) {
  return (
    <nav aria-label={ariaLabel} className="mb-5">
      <div role="tablist" className="grid grid-cols-1 gap-2 sm:grid-cols-2 xl:grid-cols-7">
        {items.map((item) => {
          const selected = item.key === active;
          return (
            <button
              key={item.key}
              id={settingsTabId(item.key)}
              type="button"
              role="tab"
              aria-selected={selected}
              aria-controls={settingsPanelId(item.key)}
              className={[
                "rounded-lg border px-3 py-3 text-left transition-colors",
                selected
                  ? "border-brand-400/50 bg-brand-500/10 text-white"
                  : "border-[color:var(--line)] text-ink-300 hover:border-brand-500/25 hover:text-ink-100",
              ].join(" ")}
              onClick={() => onChange(item.key)}
            >
              <span className="block text-[14px] font-medium">{item.label}</span>
              <span className="mt-1 block text-[12px] leading-5 text-ink-500">{item.description}</span>
              <span className="mt-2 inline-flex rounded-md border border-[color:var(--line)] px-2 py-0.5 text-[11px] text-ink-400 font-mono">
                {item.meta}
              </span>
            </button>
          );
        })}
      </div>
    </nav>
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
      routes: routesOf(row).map((route) => ({
        provider: route.provider || "",
        model: route.model || "",
        base_url: route.base_url || "",
        provider_key_ref: route.provider_key_ref || "",
        kind: route.kind || "",
      })),
      reasoning_effort: row.reasoning_effort || "",
    })),
  });
}

// Props let the page render in one of two modes:
//
//   - Default (no props): regular /settings page with the section nav
//     and the tabs that live there (Models / Access / Network & Env /
//     Interface). The Memory / Web search / Browsers tabs were
//     extracted into standalone top-bar "More" pages and are filtered
//     out of the section nav here.
//   - `forceSection`: render ONLY the matching panel content (no
//     section nav, no other tabs). Used by the standalone routes:
//        /memory     → forceSection="memory"
//        /web-search → forceSection="search"
//        /browsers   → forceSection="browsers"
//     Each route is a 4-line wrapper that mounts the same
//     SettingsWorkspace component so all those pages reuse the same
//     ~7000 lines of state hooks + JSX + helpers. The PageHeader text
//     for each standalone route is sourced from a dedicated
//     `<section>Page.{eyebrow,title,description}` i18n namespace.
//
// `forceMemoryOnly` (legacy) is preserved as a thin alias for
// `forceSection="memory"` so any external callers still work, but new
// pages should pass `forceSection` directly.
// The set of single-section render modes the SettingsWorkspace can be
// mounted into. Originally only the four standalone "More" pages
// (/memory, /web-search, /browsers, /env-vault) used this. The
// onboarding wizard (/setup) reuses the existing Models / Access /
// Runtime sections via the same prop so the wizard never re-implements
// password, LLM-tier, or gateway editing logic. Section-mode adds
// `(forceSection === "X")` to each tab's render gate (see below).
export type ForceSectionKey =
  | "memory"
  | "search"
  | "browsers"
  | "envvault"
  | "access"
  | "models"
  | "runtime"
  | "gateway"
  | "capabilityGates";

export interface SettingsPageProps {
  /** @deprecated Use `forceSection="memory"` instead. */
  forceMemoryOnly?: boolean;
  forceSection?: ForceSectionKey;
  /**
   * Optional content rendered immediately after the section-mode
   * PageHeader and before any other content. Used by `/browsers` to
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
}

export function SettingsWorkspace({
  forceMemoryOnly = false,
  forceSection: forceSectionProp,
  topBanner,
  compactLlm = false,
}: SettingsPageProps) {
  // Normalise the two equivalent prop shapes into a single value the
  // rest of the component reads. `forceSection` wins if both are set.
  const forceSection: ForceSectionKey | undefined =
    forceSectionProp ?? (forceMemoryOnly ? "memory" : undefined);
  const inSectionMode = forceSection !== undefined;
  const [uiSettings, patchUi] = useUiSettings();
  const t = useTranslations("settings");
  const tProvider = useTranslations("settings.providerCard");
  const tModel = useTranslations("settings.modelCard");
  const tMemory = useTranslations("settings.memoryCard");
  const tMemoryPage = useTranslations("memoryPage");
  const tWebSearchPage = useTranslations("webSearchPage");
  const tBrowsersPage = useTranslations("browsersPage");
  const tEnvVaultPage = useTranslations("envVaultPage");
  const tGateway = useTranslations("settings.gatewayCard");
  const tDisplay = useTranslations("settings.displayCard");
  const tChart = useTranslations("settings.chartCard");
  const tAuth = useTranslations("settings.authCard");
  const tTabs = useTranslations("settings.tabs");
  const tCommon = useTranslations("common");
  const tBrowserSession = useTranslations("browserSession");
  const tSearch = useTranslations("settingsSearch");
  const tBrowsers = useTranslations("settingsBrowsers");
  const tTunnel = useTranslations("settings.tunnelCard");
  const tFdApi = useTranslations("financialDatasets");
  const tProxy = useTranslations("networkProxy");
  const [venues, setVenues] = useState<{ name: string; label: string }[]>([]);
  const [providers, setProviders] = useState<ProviderOption[]>([]);
  const [providerProfiles, setProviderProfiles] = useState<LlmProviderProfile[]>([]);
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
  const [memoryStatus, setMemoryStatus] = useState<MemoryVectorStatus | null>(null);
  const [authStatus, setAuthStatus] = useState<AuthStatus | null>(null);
  const [authBusy, setAuthBusy] = useState(false);
  const [currentAdminPassword, setCurrentAdminPassword] = useState("");
  const [newAdminPassword, setNewAdminPassword] = useState("");
  const [confirmAdminPassword, setConfirmAdminPassword] = useState("");
  const [memoryBusy, setMemoryBusy] = useState("");
  const [memoryQuery, setMemoryQuery] = useState("");
  const [memoryResults, setMemoryResults] = useState<Array<Record<string, unknown>>>([]);
  // Memory activity feed + curated notebook state
  const [memoryActivityEvents, setMemoryActivityEvents] = useState<MemoryActivityEvent[]>([]);
  const [memoryActivityStats, setMemoryActivityStats] = useState<{
    write_ok: number;
    write_skipped: number;
    search: number;
    last_event_ts?: string;
  } | null>(null);
  const [memoryActivityFilter, setMemoryActivityFilter] = useState<"" | "write_ok" | "write_skipped" | "search">("");
  const [notebookAgent, setNotebookAgent] = useState<MemoryNotebookSnapshot | null>(null);
  const [notebookOperator, setNotebookOperator] = useState<MemoryNotebookSnapshot | null>(null);
  const [notebookDraft, setNotebookDraft] = useState<{ agent: string; operator: string }>({ agent: "", operator: "" });
  const [notebookBusy, setNotebookBusy] = useState<string>("");
  const [notebookMessage, setNotebookMessage] = useState<string>("");
  const [writeRules, setWriteRules] = useState<Record<string, MemoryWriteRuleConfig>>({});
  const [writeRuleCategories, setWriteRuleCategories] = useState<Array<{ id: string; name: string; description: string }>>([]);
  const [writeRuleDedupes, setWriteRuleDedupes] = useState<string[]>(["none", "by_hash", "by_key"]);
  const [writeRuleBusy, setWriteRuleBusy] = useState(false);
  // Memory sub-tab + provider directory state
  const [activeMemorySubTab, setActiveMemorySubTab] = useState<MemorySubTabKey>("notebook");
  const [memoryProvidersData, setMemoryProvidersData] = useState<{
    builtin: MemoryProviderView | null;
    external: MemoryProviderView | null;
    available_external: MemoryProviderView[];
  } | null>(null);
  const [memoryExternalConfig, setMemoryExternalConfig] = useState<MemoryExternalConfig | null>(null);
  const [agentmemoryDraft, setAgentmemoryDraft] = useState({
    base_url: "http://127.0.0.1:3111",
    secret_ref: "",
    secret_env: "AGENTMEMORY_SECRET",
    project: "",
    session_id: "",
    context_budget: "2000",
    timeout_s: "1.5",
  });
  const [agentmemoryInstall, setAgentmemoryInstall] = useState<{
    commands: string[];
    health_url: string;
    viewer_url: string;
    dependency_available: boolean;
    note: string;
  } | null>(null);
  // Last result of the "Install dependency" button on the Selected backend
  // settings card. Mirrors the shape of /memory/external/install/run for
  // agentmemory and /memory/vector/install for memsearch so we can render
  // a single status block regardless of backend.
  const [backendInstallResult, setBackendInstallResult] = useState<{
    backend: "memsearch" | "agentmemory";
    ok: boolean;
    cmd?: string[];
    returncode?: number;
    stdout_tail?: string;
    stderr_tail?: string;
    dependency_available?: boolean;
    note?: string;
    error?: string;
    detail?: string | null;
  } | null>(null);
  // Last result of the "Test recall" button — populated by /memory/test
  // which returns 1..N per-backend entries depending on what's enabled.
  const [backendTestResult, setBackendTestResult] = useState<{
    query: string;
    backends: Array<{
      backend: "builtin" | "memsearch" | "agentmemory";
      ok: boolean;
      agent_entries?: number;
      operator_entries?: number;
      matches?: number;
      available?: boolean;
      enabled?: boolean;
      base_url?: string;
      last_error?: string | null;
      note?: string;
      error?: string;
      detail?: string | null;
      preview?: Array<Record<string, unknown>>;
    }>;
  } | null>(null);
  // Editable query used by the "Test recall" button on the summary card.
  // Independent from `memoryQuery` (the memsearch sub-tab's query input)
  // so toggling between backends doesn't surprise the operator.
  const [backendTestQuery, setBackendTestQuery] = useState("");
  const [gatewayPlatforms, setGatewayPlatforms] = useState<GatewayPlatformSpec[]>([]);
  const [gatewayChannels, setGatewayChannels] = useState<GatewayChannelConfig[]>([]);
  const [gatewayDraft, setGatewayDraft] = useState<GatewayDraft>(() => emptyGatewayDraft());
  const [gatewayBusy, setGatewayBusy] = useState("");
  const [gatewayTestText, setGatewayTestText] = useState("Nerya gateway test message.");
  const [gatewayResult, setGatewayResult] = useState<string | null>(null);
  const [gatewayStatus, setGatewayStatus] = useState<Record<string, unknown> | null>(null);
  const [embProvider, setEmbProvider] = useState("openai");
  const [embModel, setEmbModel] = useState("text-embedding-3-small");
  const [embBaseUrl, setEmbBaseUrl] = useState("");
  const [embKeyRef, setEmbKeyRef] = useState("");
  // Operator can paste a plaintext API key here; the backend stashes
  // it in the SecretVault and points the embedding `api_key_ref` at
  // the new entry. Always cleared after a successful save so the
  // secret never sticks around in component state.
  const [embKeyPlain, setEmbKeyPlain] = useState("");
  const [milvusUri, setMilvusUri] = useState("~/.memsearch/milvus.db");
  const [milvusToken, setMilvusToken] = useState("");
  const [milvusCollection, setMilvusCollection] = useState("memsearch_chunks");
  const [error, setError] = useState<string | null>(null);
  const [info, setInfo] = useState<string | null>(null);
  const [activeSettingsTab, setActiveSettingsTab] = useState<SettingsTabKey>("models");
  const memoryBackendChoice: MemoryBackendChoice =
    memoryExternalConfig?.enabled && memoryExternalConfig.provider === "agentmemory"
      ? "agentmemory"
      : memoryStatus?.enabled
        ? "memsearch"
        : "builtin";

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

  // ---- Web search engines ----------------------------------------
  const [searchStatus, setSearchStatus] = useState<SearchEnginesStatus | null>(null);
  const [searchChainCsv, setSearchChainCsv] = useState<string>("");
  const [searchRegion, setSearchRegion] = useState<string>("wt-wt");
  const [searchSafesearch, setSearchSafesearch] = useState<string>("moderate");
  const [searchKeyDrafts, setSearchKeyDrafts] = useState<Record<string, string>>({});
  const [searchBaseUrlDrafts, setSearchBaseUrlDrafts] = useState<Record<string, string>>({});
  const [searchStore, setSearchStore] = useState<"vault" | "workspace">("vault");
  const [searchBusy, setSearchBusy] = useState<string>("");
  const [searchTestQuery, setSearchTestQuery] = useState<string>("Nvidia earnings");
  const [searchTestEngine, setSearchTestEngine] = useState<string>("");
  const [searchTestResult, setSearchTestResult] = useState<string | null>(null);
  const [searxngHostPort, setSearxngHostPort] = useState<string>("8888");
  const [searxngImage, setSearxngImage] = useState<string>("searxng/searxng:latest");
  const [searxngRebuild, setSearxngRebuild] = useState<boolean>(false);
  const [searchEngineRowResult, setSearchEngineRowResult] = useState<Record<string, string>>({});

  // ---- Headless browser engines ----------------------------------
  const [browsersStatus, setBrowsersStatus] = useState<BrowsersStatus | null>(null);
  const [browsersBusy, setBrowsersBusy] = useState<string>("");
  const [browserProbeUrl, setBrowserProbeUrl] = useState<string>("https://example.com");
  const [browserProbeName, setBrowserProbeName] = useState<string>("");
  const [browserProbeResult, setBrowserProbeResult] = useState<string | null>(null);
  const [browserRowResult, setBrowserRowResult] = useState<Record<string, string>>({});

  // ---- Financial Datasets API keys -------------------------------
  const [fdStatus, setFdStatus] = useState<FinancialDatasetsStatus | null>(null);
  const [fdKeysDraft, setFdKeysDraft] = useState<string>("");
  const [fdStore, setFdStore] = useState<"vault" | "workspace">("vault");
  const [fdBusy, setFdBusy] = useState<string>("");

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

  async function loadAuthStatus() {
    try {
      setAuthStatus(await clientApi.authStatus());
    } catch {
      setAuthStatus(null);
    }
  }

  function syncMemoryExternalDrafts(cfg: MemoryExternalConfig | null | undefined) {
    if (!cfg?.agentmemory) return;
    setMemoryExternalConfig(cfg);
    setAgentmemoryDraft({
      base_url: cfg.agentmemory.base_url || "http://127.0.0.1:3111",
      secret_ref: cfg.agentmemory.secret_ref || "",
      secret_env: cfg.agentmemory.secret_env || "AGENTMEMORY_SECRET",
      project: cfg.agentmemory.project || "",
      session_id: cfg.agentmemory.session_id || "",
      context_budget: String(cfg.agentmemory.context_budget || 2000),
      timeout_s: String(cfg.agentmemory.timeout_s || 1.5),
    });
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
      setError(e instanceof Error ? e.message : String(e));
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
      setError(e instanceof Error ? e.message : String(e));
      return null;
    }
  }

  async function saveDashboardEndpoint() {
    const port = Number(dashboardPortDraft.trim());
    if (!Number.isFinite(port) || port < 1 || port > 65535) {
      setError(tTunnel("dashboardPortInvalid"));
      return;
    }
    setDashboardBusy(true);
    setDashboardMessage(null);
    setError(null);
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
      setError(e instanceof Error ? e.message : String(e));
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
      setError(e instanceof Error ? e.message : String(e));
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
    setError(null);
    setInfo(null);
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
      setError(e instanceof Error ? e.message : String(e));
      return false;
    } finally {
      setTunnelBusy("");
    }
  }

  async function installTunnelProvider(provider: string) {
    setTunnelBusy(`install:${provider}`);
    setTunnelMessage(null);
    setError(null);
    setInfo(null);
    try {
      const res = await clientApi.networkTunnelInstall({ provider, approve: true });
      if (!res.ok) throw new Error(res.detail || res.error || "tunnel install failed");
      setTunnelMessage(res.already_installed ? tTunnel("alreadyInstalled") : tTunnel("installFinished"));
      await loadNetworkTunnels();
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setTunnelBusy("");
    }
  }

  async function startTunnelProvider(provider: string) {
    const saved = await saveTunnelConfig(provider);
    if (!saved) return;
    setTunnelBusy(`start:${provider}`);
    setTunnelMessage(null);
    setError(null);
    setInfo(null);
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
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setTunnelBusy("");
    }
  }

  async function stopTunnelProvider(provider: string) {
    setTunnelBusy(`stop:${provider}`);
    setTunnelMessage(null);
    setError(null);
    setInfo(null);
    try {
      const res = await clientApi.networkTunnelStop(provider);
      if (!res.ok) throw new Error(res.detail || res.error || "tunnel stop failed");
      setTunnelMessage(tTunnel("stopFinished"));
      await loadNetworkTunnels();
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
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
      setError(e instanceof Error ? e.message : String(e));
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
    setError(null);
    setInfo(null);
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
      setInfo("Network proxy settings saved and applied to the runtime.");
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setProxyBusy("");
    }
  }

  async function testNetworkProxy() {
    setProxyBusy("test");
    setError(null);
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
    setError(null);
    setInfo(null);
    try {
      const res = await clientApi.runtimeEnvPut({
        name: envNameDraft.trim(),
        value: envValueDraft,
      });
      if (!res.ok) throw new Error(res.detail || res.error || "env save failed");
      setEnvValueDraft("");
      setInfo(`${res.env?.name || envNameDraft.trim()} saved to encrypted runtime env.`);
      await loadSecurityRuntime();
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setSecurityBusy("");
    }
  }

  async function deleteRuntimeEnv(name: string) {
    setSecurityBusy(`env:delete:${name}`);
    setError(null);
    setInfo(null);
    try {
      const res = await clientApi.runtimeEnvDelete(name);
      if (!res.ok) throw new Error(res.detail || res.error || "env delete failed");
      setInfo(`${res.name || name} removed from runtime env.`);
      await loadSecurityRuntime();
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setSecurityBusy("");
    }
  }

  async function saveVaultSecret() {
    setSecurityBusy("vault:save");
    setError(null);
    setInfo(null);
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
      setInfo(`${res.ref?.ref || vaultNameDraft.trim()} saved to SecretVault.`);
      await loadSecurityRuntime();
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setSecurityBusy("");
    }
  }

  async function deleteVaultSecret(name: string) {
    setSecurityBusy(`vault:delete:${name}`);
    setError(null);
    setInfo(null);
    try {
      const res = await clientApi.secretsDelete(name);
      if (!res.ok) throw new Error(res.error || "vault delete failed");
      setInfo(`${res.name || name} removed from SecretVault.`);
      await loadSecurityRuntime();
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setSecurityBusy("");
    }
  }

  function applySearchStatus(status: SearchEnginesStatus | null | undefined) {
    if (!status) return;
    setSearchStatus(status);
    setSearchChainCsv((status.engines || []).join(", "));
    setSearchRegion(status.region || "wt-wt");
    setSearchSafesearch(status.safesearch || "moderate");
    setSearchKeyDrafts({});
    const nextBaseUrls: Record<string, string> = {};
    for (const row of status.engine_status || []) {
      if (!row.needs_base_url) continue;
      const ws = row.base_url?.workspace || "";
      nextBaseUrls[row.name] = ws;
    }
    setSearchBaseUrlDrafts(nextBaseUrls);
    if (status.searxng?.host_port) {
      setSearxngHostPort(String(status.searxng.host_port));
    }
    if (status.searxng?.image) setSearxngImage(status.searxng.image);
  }

  async function loadSearchStatus() {
    try {
      const res = await clientApi.searchEnginesStatus();
      applySearchStatus(res);
      return res;
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
      return null;
    }
  }

  function applyGatewayConfig(cfg: { channels?: GatewayChannelConfig[]; platforms?: GatewayPlatformSpec[]; status?: Record<string, unknown> } | null) {
    if (!cfg) return;
    const channels = cfg.channels || [];
    const platforms = cfg.platforms || [];
    setGatewayChannels(channels);
    setGatewayPlatforms(platforms);
    setGatewayStatus(cfg.status || null);
    const platformLookup = new Map<string, GatewayPlatformSpec>();
    for (const p of platforms) platformLookup.set(p.id, p);
    setGatewayDraft((current) => {
      const match = channels.find((row) => row.channel === current.channel);
      if (match) return gatewayDraftFromChannel(match, platformLookup.get(match.kind));
      if (channels[0] && (!current.channel || current.channel === "telegram")) {
        const head = channels[0];
        return gatewayDraftFromChannel(head, platformLookup.get(head.kind));
      }
      return current;
    });
  }

  async function loadGatewayConfig() {
    try {
      const cfg = await clientApi.gatewayConfig();
      if (!cfg.ok) throw new Error(cfg.error || "cannot load gateway config");
      applyGatewayConfig(cfg);
      return cfg;
    } catch (e) {
      setGatewayResult(e instanceof Error ? e.message : String(e));
      return null;
    }
  }

  async function loadModelConfig() {
    setLoading(true);
    try {
      const [cfg, providerRes, modelRes, venueRes, memoryRes, gatewayRes, authRes, searchRes, browsersRes, fdRes, catalogRes, oauthRes, runtimeEnvRes, secretsRes, proxyRes, dashboardRes, tunnelsRes] = await Promise.all([
        clientApi.llmConfig(),
        clientApi.llmProviders(),
        clientApi.llmModels(),
        clientApi.marketVenues().catch(() => ({ venues: [] })),
        clientApi.memoryVectorStatus().catch(() => null),
        clientApi.gatewayConfig().catch(() => null),
        clientApi.authStatus().catch(() => null),
        clientApi.searchEnginesStatus().catch(() => null),
        clientApi.browsersStatus().catch(() => null),
        clientApi.financialDatasetsStatus().catch(() => null),
        clientApi.llmCatalog().catch(() => null),
        clientApi.llmOauthProviders().catch(() => null),
        clientApi.runtimeEnvList().catch(() => null),
        clientApi.secretsList().catch(() => null),
        clientApi.networkProxy().catch(() => null),
        clientApi.networkDashboard().catch(() => null),
        clientApi.networkTunnels().catch(() => null),
      ]);
      if (!cfg.ok) throw new Error(cfg.error || "cannot load llm config");
      const loadedDefaultTier = cfg.default_tier || "medium";
      const loadedIntentTier = cfg.intent_tier || "light";
      const loadedTiers = ensureAssignmentTiers(cfg.tiers || []);
      const profiles = cfg.provider_profiles || [];
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
      setVenues((venueRes.venues || []).map((v) => ({ name: v.name, label: v.label })));
      setMemoryStatus(memoryRes);
      syncMemoryDrafts(memoryRes);
      applyGatewayConfig(gatewayRes);
      setAuthStatus(authRes);
      applySearchStatus(searchRes);
      setBrowsersStatus(browsersRes);
      setFdStatus(fdRes);
      if (runtimeEnvRes?.ok) setRuntimeEnv(runtimeEnvRes.env || []);
      if (secretsRes) setVaultRefs(secretsRes.refs || []);
      if (proxyRes?.ok) syncProxyDrafts(proxyRes);
      if (dashboardRes?.ok) syncDashboardDraft(dashboardRes);
      if (tunnelsRes?.ok) syncTunnelDrafts(tunnelsRes);
      setError(null);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setLoading(false);
    }
  }

  useEffect(() => {
    void loadModelConfig();
    // Memory activity / notebook / write rules load in the background
    // so the memory tab is populated even if the operator opens it
    // immediately after a refresh.
    void loadMemoryActivity();
    void loadNotebook();
    void loadWriteRules();
    void loadMemoryProviders();
  }, []);

  // Re-fetch the activity stream when the kind filter changes so the
  // dropdown response feels instant. Keep this separate from the
  // initial load so the bootstrap fires once.
  useEffect(() => {
    void loadMemoryActivity();
  }, [memoryActivityFilter]);

  useEffect(() => {
    if (typeof window === "undefined") return;
    if (inSectionMode) return;
    // Each moved section now has a dedicated top-level route. The map
    // catches anyone landing on `/settings#<section>` (old bookmarks,
    // in-doc deep-links, etc.) and rewrites them to the new page so
    // they don't end up on a tab that no longer exists in the section
    // nav. New tabs added here MUST also be removed from the
    // settingsTabs list below or the redirect just bounces back.
    const movedSections: Record<string, string> = {
      memory: "/memory",
      search: "/web-search",
      browsers: "/browsers",
      envvault: "/env-vault",
    };
    const syncHash = () => {
      const tab = window.location.hash.replace(/^#/, "");
      const target = movedSections[tab];
      if (target) {
        window.location.replace(target);
        return;
      }
      if (isSettingsTabKey(tab)) setActiveSettingsTab(tab);
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

  function providerArtifacts(rawProvider: string) {
    const trimmed = rawProvider.trim();
    const lower = trimmed.toLowerCase();
    const provider = providers.find(
      (row) => row.provider === trimmed || row.provider === lower,
    );
    const profile = providerProfileMap.get(trimmed) || providerProfileMap.get(lower);
    const catalog = catalogById.get(trimmed) || catalogById.get(lower);
    return {
      provider_id: lower,
      provider,
      profile,
      catalog,
      base_url:
        profile?.base_url ||
        provider?.base_url ||
        catalog?.base_url ||
        DEFAULT_PROVIDER_BASE_URLS[lower] ||
        "",
    };
  }

  function routeDefaultsForProvider(rawProvider: string) {
    const artifacts = providerArtifacts(rawProvider);
    return {
      base_url: artifacts.base_url,
      kind:
        artifacts.catalog?.api_mode === "anthropic_messages"
          ? "anthropic_messages"
          : "chat_completions",
    };
  }

  function routeHasCredential(route: LlmRouteConfig): boolean {
    if (!route.provider.trim()) return false;
    const artifacts = providerArtifacts(route.provider);
    return Boolean(
      route.provider_key ||
      route.provider_key_ref ||
      route.has_key_ref ||
      (route.provider_key_refs || []).length > 0 ||
      artifacts.profile?.provider_key_ref ||
      artifacts.profile?.has_key_ref ||
      artifacts.provider?.ready ||
      artifacts.provider_id === "ollama",
    );
  }

  const configuredTierCount = useMemo(
    () => tierRows.filter((row) =>
      routesOf(row).some((route) => route.provider && route.model),
    ).length,
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

  const gatewayPlatformMap = useMemo(() => {
    const map = new Map<string, GatewayPlatformSpec>();
    for (const platform of gatewayPlatforms) map.set(platform.id, platform);
    return map;
  }, [gatewayPlatforms]);

  const gatewayPlatformOptions = useMemo(() => {
    const rows = gatewayPlatforms.filter((platform) => platform.id !== "local");
    return rows.length ? rows : [
      {
        id: "telegram", title: "Telegram", alias_id: "telegram", status: "native",
        inbound: "polling", outbound: "bot_api", typing: "sendChatAction",
        menu: "setMyCommands", support_level: "tested",
      },
      {
        id: "discord", title: "Discord", alias_id: "discord", status: "webhook",
        inbound: "generic_inbound", outbound: "webhook", typing: "status_webhook",
        menu: "slash_commands_scaffold", support_level: "send_only",
      },
      {
        id: "webhook", title: "Generic Webhook", alias_id: "webhook", status: "native",
        inbound: "http", outbound: "json_webhook", typing: "status_webhook",
        menu: "none", support_level: "full_duplex",
      },
    ];
  }, [gatewayPlatforms]);

  const gatewayConfiguredCount = useMemo(
    () => gatewayChannels.filter((channel) => channel.configured && channel.enabled).length,
    [gatewayChannels],
  );

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

  const selectedGatewayPlatform = gatewayPlatformMap.get(gatewayDraft.kind);
  const gatewayIsTelegram = gatewayDraft.kind === "telegram";

  function setProviderDraftFromSelect(provider: string) {
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
    setProviderKeyDraft(profile?.provider_key_ref || "");
    // Switching to a catalogue provider clears the custom-kind picker —
    // the picker is only meaningful when the user is rolling their own
    // provider id outside the catalogue.
    if (catalogEntry) setCustomProviderKind("");
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

  function patchTierRoute(
    tierIndex: number,
    routeIndex: number,
    patch: Partial<LlmRouteConfig>,
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
          base_url: first.base_url || "",
          provider_key_ref: first.provider_key_ref || "",
          routes,
        };
      }),
    );
  }

  function addTierRoute(tierIndex: number) {
    setTierRows((rows) =>
      rows.map((row, i) => {
        if (i !== tierIndex) return row;
        return { ...row, routes: [...routesOf(row), emptyRoute()] };
      }),
    );
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
          base_url: first.base_url || "",
          provider_key_ref: first.provider_key_ref || "",
          routes: nextRoutes,
        };
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

  function setGatewayKind(kind: string) {
    setGatewayDraft((current) => {
      const next = emptyGatewayDraft(kind);
      return {
        ...next,
        channel: current.channel && current.channel !== "telegram" ? current.channel : next.channel,
      };
    });
    setGatewayResult(null);
  }

  function setGatewaySecret(key: string, value: string) {
    setGatewayDraft((cur) => ({
      ...cur,
      secrets: { ...cur.secrets, [key]: value },
    }));
  }

  function setGatewaySecretRef(key: string, value: string) {
    setGatewayDraft((cur) => ({
      ...cur,
      secretRefs: { ...cur.secretRefs, [key]: value },
    }));
  }

  function gatewayUpsertPayload(): GatewayUpsertRequest {
    const channel = gatewayDraft.channel.trim().toLowerCase();
    const body: GatewayUpsertRequest = {
      channel,
      kind: gatewayDraft.kind,
      enabled: gatewayDraft.enabled,
      mode: gatewayDraft.mode,
      trade_notifications: gatewayDraft.trade_notifications,
      approvals: gatewayDraft.approvals,
      auto_reply: gatewayDraft.auto_reply,
      allow_unknown_users: gatewayDraft.allow_unknown_users,
      group_sessions_per_user: gatewayDraft.group_sessions_per_user,
      thread_sessions_per_user: gatewayDraft.thread_sessions_per_user,
      topics: gatewayTopics(gatewayDraft.topicsCsv),
      allowed_chat_ids: gatewayCsvList(gatewayDraft.allowedChatIdsCsv),
      allowed_user_ids: gatewayCsvList(gatewayDraft.allowedUserIdsCsv),
      denied_user_ids: gatewayCsvList(gatewayDraft.deniedUserIdsCsv),
    };
    // Walk the platform's secret_fields catalog and emit only the keys
    // the operator has actually populated. ``secret``/``url`` fields go
    // through the vault (plaintext if typed, ref otherwise);
    // ``id``/``opaque`` fields stay in plaintext (chat_id, app_id, …).
    const spec = gatewayPlatformMap.get(gatewayDraft.kind);
    const fields = spec?.secret_fields || [];
    if (fields.length) {
      for (const field of fields) {
        const plain = (gatewayDraft.secrets[field.key] || "").trim();
        const ref = (gatewayDraft.secretRefs[field.key] || "").trim();
        const isVaulted = field.kind === "secret" || field.kind === "url";
        if (isVaulted) {
          if (plain) body[field.key] = plain;
          else if (ref) body[field.ref_key] = ref;
        } else if (plain) {
          body[field.key] = plain;
        }
      }
    } else {
      // Unknown platform (no spec yet): fall back to the legacy
      // bot_token / webhook / status_webhook keys so the form stays
      // usable until /gateway/config returns.
      const botToken = (gatewayDraft.secrets["bot_token"] || "").trim();
      const botRef = (gatewayDraft.secretRefs["bot_token"] || "").trim();
      if (botToken) body.bot_token = botToken;
      else if (botRef) body.bot_token_ref = botRef;
      const webhook = (gatewayDraft.secrets["webhook_url"] || "").trim();
      const webhookRef = (gatewayDraft.secretRefs["webhook_url"] || "").trim();
      if (gatewayDraft.kind === "webhook") {
        if (webhook) body.url = webhook;
        else if (webhookRef) body.url_ref = webhookRef;
      } else {
        if (webhook) body.webhook_url = webhook;
        else if (webhookRef) body.webhook_url_ref = webhookRef;
      }
      const statusUrl = (gatewayDraft.secrets["status_webhook_url"] || "").trim();
      const statusRef = (gatewayDraft.secretRefs["status_webhook_url"] || "").trim();
      if (statusUrl) body.status_webhook_url = statusUrl;
      else if (statusRef) body.status_webhook_url_ref = statusRef;
      const chatId = (gatewayDraft.secrets["chat_id"] || "").trim();
      if (chatId) body.chat_id = chatId;
    }
    // Telegram-specific extras live outside ``secret_fields`` because
    // they are runtime tunables, not credentials.
    if (gatewayIsTelegram) {
      body.polling = gatewayDraft.polling;
      if (gatewayDraft.parse_mode.trim()) body.parse_mode = gatewayDraft.parse_mode.trim();
      body.disable_web_page_preview = gatewayDraft.disable_web_page_preview;
    }
    if (gatewayDraft.username.trim()) body.username = gatewayDraft.username.trim();
    if (gatewayDraft.avatar_url.trim()) body.avatar_url = gatewayDraft.avatar_url.trim();
    const timeout = Number(gatewayDraft.timeout_s);
    if (Number.isFinite(timeout) && timeout > 0) body.timeout_s = timeout;
    return body;
  }

  async function saveGatewayConfig() {
    setGatewayBusy("save");
    setError(null);
    setInfo(null);
    setGatewayResult(null);
    try {
      const res = await clientApi.gatewayConfigUpsert(gatewayUpsertPayload());
      if (!res.ok) throw new Error(res.error || "gateway save failed");
      if (res.config) applyGatewayConfig(res.config);
      if (res.channel) {
        // After a successful upsert the backend returns the canonical
        // sanitized channel snapshot. Hydrate from it (using the spec
        // we already loaded) so the form clears any plaintext the
        // operator just typed and surfaces the freshly-stored vault://
        // refs instead.
        setGatewayDraft(gatewayDraftFromChannel(res.channel, gatewayPlatformMap.get(res.channel.kind)));
      } else {
        setGatewayDraft((current) => ({ ...current, secrets: {} }));
      }
      setInfo(tGateway("saved"));
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setGatewayBusy("");
    }
  }

  async function deleteGatewayConfig() {
    if (!gatewayDraft.channel.trim()) return;
    setGatewayBusy("delete");
    setError(null);
    setInfo(null);
    setGatewayResult(null);
    try {
      const res = await clientApi.gatewayConfigDelete(gatewayDraft.channel.trim().toLowerCase());
      if (!res.ok) throw new Error(res.error || "gateway delete failed");
      if (res.config) applyGatewayConfig(res.config);
      setGatewayDraft(emptyGatewayDraft(gatewayDraft.kind));
      setInfo(tGateway("deleted"));
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setGatewayBusy("");
    }
  }

  async function testGatewayConfig() {
    if (!gatewayDraft.channel.trim()) return;
    setGatewayBusy("test");
    setError(null);
    setInfo(null);
    setGatewayResult(null);
    try {
      const res = await clientApi.gatewayConfigTest({
        channel: gatewayDraft.channel.trim().toLowerCase(),
        text: gatewayTestText.trim() || "Nerya gateway test message.",
        mode: "agent",
      });
      const note = res.delivery?.delivery_note ? String(res.delivery.delivery_note) : "";
      if (!res.ok) throw new Error(res.detail || res.error || note || "gateway test failed");
      const turnId = res.agent?.turn_id || "";
      const reply = res.reply_text ? String(res.reply_text).slice(0, 220) : "";
      setGatewayResult(
        turnId
          ? `${tGateway("agentTestDelivered")} ${turnId}${reply ? ` · ${reply}` : ""}`
          : note || tGateway("testDelivered"),
      );
      await loadGatewayConfig();
    } catch (e) {
      setGatewayResult(e instanceof Error ? e.message : String(e));
    } finally {
      setGatewayBusy("");
    }
  }

  async function saveModelConfig() {
    setSaving(true);
    try {
      const intentRow = tierRows.find((row) => row.tier === INTENT_TIER);
      const intentReady = intentRow
        ? routesOf(intentRow).some((route) => route.provider && route.model)
        : false;
      const nextIntentTier = intentReady ? INTENT_TIER : intentTier || "light";
      const rowsForSave = tierRows
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
              return {
                provider: route.provider.trim().toLowerCase(),
                model: models.join(", "),
                models,
                base_url: (route.base_url || "").trim(),
                provider_key_ref: providerKeyRefs.join(", "),
                provider_key_refs: providerKeyRefs,
                provider_key: providerKeys.join(", "),
                provider_keys: providerKeys,
                kind: (route.kind || "").trim(),
              };
            });
          if (!row.tier.trim() || routes.length === 0) return null;
          const first = routes[0];
          return {
            tier: row.tier.trim(),
            provider: first.provider,
            model: first.model,
            base_url: first.base_url,
            provider_key_ref: first.provider_key_ref,
            reasoning_effort: (row.reasoning_effort || "").trim().toLowerCase(),
            routes,
          };
        })
        .filter(Boolean) as LlmTierConfig[];
      const res = await clientApi.llmConfigSet({
        default_tier: defaultTier,
        intent_tier: nextIntentTier,
        providers: profilesForSave(),
        tiers: rowsForSave,
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
    setDiscoveryError(null);
    setInfo(null);
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
      const msg = e instanceof Error ? e.message : String(e);
      // Mirror the failure both to the global banner (for visibility)
      // and to the inline ``discoveryError`` so the operator can react
      // without leaving the discover form.
      setError(msg);
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
    setError(null);
    const provider = providerDraft.trim().toLowerCase();
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
    setInfo(`Added ${id} to the import queue. Click "Import selected" to persist.`);
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

  async function loadMemoryActivity() {
    try {
      const res = await clientApi.memoryActivity({
        limit: 200,
        kinds: memoryActivityFilter ? [memoryActivityFilter] : undefined,
      });
      setMemoryActivityEvents(res.events || []);
      setMemoryActivityStats(res.stats || null);
    } catch (e) {
      // Activity stream is best-effort; don't surface a banner for a
      // transient backend hiccup. The empty list is the visible cue.
      console.warn("memory activity load failed", e);
    }
  }

  async function loadNotebook() {
    try {
      const res = await clientApi.memoryNotebookList();
      setNotebookAgent(res.agent || null);
      setNotebookOperator(res.operator || null);
    } catch (e) {
      console.warn("memory notebook load failed", e);
    }
  }

  async function loadMemoryProviders() {
    try {
      const [providersRes, configRes] = await Promise.all([
        clientApi.memoryProviders(),
        clientApi.memoryExternalConfig(),
      ]);
      setMemoryProvidersData(providersRes);
      syncMemoryExternalDrafts(configRes);
    } catch (e) {
      console.warn("memory providers load failed", e);
    }
  }

  async function setMemoryBackendChoice(next: MemoryBackendChoice) {
    setMemoryBusy(`backend:${next}`);
    setError(null);
    setInfo(null);
    try {
      if (next === "builtin") {
        await clientApi.memoryVectorConfig({ enabled: false, watch_enabled: false });
        const ext = await clientApi.memoryExternalConfigSet({
          enabled: false,
          provider: "",
        });
        if (ext.ok === false) throw new Error(ext.error || "memory backend config failed");
        syncMemoryExternalDrafts(ext);
      } else if (next === "memsearch") {
        const ext = await clientApi.memoryExternalConfigSet({
          enabled: false,
          provider: "",
        });
        if (ext.ok === false) throw new Error(ext.error || "memory backend config failed");
        const status = await clientApi.memoryVectorConfig({ enabled: true });
        setMemoryStatus(status);
        syncMemoryDrafts(status);
      } else {
        await clientApi.memoryVectorConfig({ enabled: false, watch_enabled: false });
        const ext = await clientApi.memoryExternalConfigSet({
          enabled: true,
          provider: "agentmemory",
          agentmemory: {
            base_url: agentmemoryDraft.base_url.trim() || "http://127.0.0.1:3111",
            secret_ref: agentmemoryDraft.secret_ref.trim(),
            secret_env: agentmemoryDraft.secret_env.trim() || "AGENTMEMORY_SECRET",
            project: agentmemoryDraft.project.trim(),
            session_id: agentmemoryDraft.session_id.trim(),
            context_budget: Number(agentmemoryDraft.context_budget) || 2000,
            timeout_s: Number(agentmemoryDraft.timeout_s) || 1.5,
          },
        });
        if (ext.ok === false) throw new Error(ext.error || "agentmemory config failed");
        syncMemoryExternalDrafts(ext);
      }
      await Promise.all([loadMemoryStatus(), loadMemoryProviders()]);
      setInfo(tMemory(`backendSaved_${next}`));
      // Auto-reveal the matching detail sub-tab so the operator sees
      // config for the backend they just selected without hunting.
      // builtin → notebook (curated content), agentmemory → providers
      // (external creds). memsearch no longer maps to a sub-tab because
      // its full configuration card is now inlined into the Selected
      // backend settings panel above — only visible when memsearch is
      // the active backend.
      if (next === "builtin") {
        setActiveMemorySubTab("notebook");
      } else if (next === "agentmemory") {
        setActiveMemorySubTab("providers");
      }
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setMemoryBusy("");
    }
  }

  async function saveAgentmemoryConfig(enable?: boolean) {
    setMemoryBusy("agentmemory:save");
    setError(null);
    setInfo(null);
    try {
      const enabled = enable ?? Boolean(memoryExternalConfig?.enabled);
      const res = await clientApi.memoryExternalConfigSet({
        enabled,
        provider: enabled ? "agentmemory" : "",
        agentmemory: {
          base_url: agentmemoryDraft.base_url.trim() || "http://127.0.0.1:3111",
          secret_ref: agentmemoryDraft.secret_ref.trim(),
          secret_env: agentmemoryDraft.secret_env.trim() || "AGENTMEMORY_SECRET",
          project: agentmemoryDraft.project.trim(),
          session_id: agentmemoryDraft.session_id.trim(),
          context_budget: Number(agentmemoryDraft.context_budget) || 2000,
          timeout_s: Number(agentmemoryDraft.timeout_s) || 1.5,
        },
      });
      if (res.ok === false) throw new Error(res.error || "agentmemory config failed");
      syncMemoryExternalDrafts(res);
      await Promise.all([loadMemoryStatus(), loadMemoryProviders()]);
      setInfo(enabled ? tMemory("agentmemoryEnabled") : tMemory("agentmemoryDisabled"));
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setMemoryBusy("");
    }
  }

  async function loadAgentmemoryInstall() {
    setMemoryBusy("agentmemory:install");
    setError(null);
    try {
      const res = await clientApi.memoryExternalInstall();
      setAgentmemoryInstall({
        commands: res.commands || [],
        health_url: res.health_url,
        viewer_url: res.viewer_url,
        dependency_available: Boolean(res.dependency_available),
        note: res.note,
      });
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setMemoryBusy("");
    }
  }

  // -- Selected backend install + test handlers ----------------------- //
  // Wire the "Install dependency" button on the summary card. Routes to
  // the matching backend installer (pip for memsearch, npm for
  // agentmemory) and stores the full stdout/stderr tail so the operator
  // can see why a real install failed without leaving the dashboard.
  async function runBackendInstall(backend: "memsearch" | "agentmemory") {
    setMemoryBusy(`${backend}:install:run`);
    setError(null);
    setInfo(null);
    setBackendInstallResult(null);
    try {
      let res: {
        ok?: boolean;
        cmd?: string[];
        returncode?: number;
        stdout_tail?: string;
        stderr_tail?: string;
        dependency_available?: boolean;
        note?: string;
        error?: string;
        detail?: string | null;
      };
      if (backend === "memsearch") {
        res = await clientApi.memoryVectorInstall();
      } else {
        res = await clientApi.memoryExternalInstallRun();
      }
      setBackendInstallResult({
        backend,
        ok: Boolean(res.ok),
        cmd: res.cmd,
        returncode: res.returncode,
        stdout_tail: res.stdout_tail,
        stderr_tail: res.stderr_tail,
        dependency_available: res.dependency_available,
        note: res.note,
        error: res.error,
        detail: res.detail ?? null,
      });
      if (res.ok) {
        setInfo(
          backend === "memsearch"
            ? "memsearch dependency installed."
            : "agentmemory dependency installed.",
        );
      } else if (res.error || res.detail) {
        setError(res.detail || res.error || "install failed");
      }
      // Refresh memsearch status so the "dependency_available" pill on the
      // summary card flips ok/warn without a manual reload. agentmemory's
      // health is probed by its own polling so we don't have to refresh it
      // here.
      if (backend === "memsearch") await loadMemoryStatus();
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setMemoryBusy("");
    }
  }

  // Wire the "Test recall" button. Calls the unified /memory/test endpoint
  // which probes all 3 backends in one shot and returns per-backend
  // diagnostics — much friendlier than asking the operator to switch
  // sub-tabs to run each backend's individual probe.
  async function runBackendTest(query?: string) {
    const q = (query ?? backendTestQuery).trim();
    setMemoryBusy("memory:test");
    setError(null);
    setInfo(null);
    try {
      const res = await clientApi.memoryTest(q ? { query: q } : {});
      setBackendTestResult({
        query: res.query,
        backends: res.backends || [],
      });
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setMemoryBusy("");
    }
  }

  // -- OAuth login-link helpers ---------------------------------------- //

  // Show the operator the right login affordance for ``providerId``:
  //  - flow=cli: copy-paste a CLI command.
  //  - flow=device_code: kick off GitHub's device-code flow (Copilot)
  //    and open the verification URL in a new tab.
  //  - flow=paste: nothing more to do, the existing paste field handles it.
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

  async function loadWriteRules() {
    try {
      const res = await clientApi.memoryWriteRulesGet();
      setWriteRules(res.rules || {});
      setWriteRuleCategories(
        (res.categories || []).map((c) => ({
          id: c.id,
          name: c.name,
          description: c.description,
        })),
      );
      if (Array.isArray(res.dedupe_strategies) && res.dedupe_strategies.length) {
        setWriteRuleDedupes(res.dedupe_strategies);
      }
    } catch (e) {
      console.warn("memory write rules load failed", e);
    }
  }

  async function notebookMutate(
    target: "agent" | "operator",
    action: "add" | "replace" | "remove",
    payload: { content?: string; old_text?: string },
  ) {
    setNotebookBusy(`${target}.${action}`);
    setNotebookMessage("");
    try {
      const res = await clientApi.memoryNotebookMutate({ target, action, ...payload });
      if (!res.ok) throw new Error(res.error || `notebook ${action} failed`);
      // Refresh the snapshot + activity feed so the operator sees the
      // update immediately on both panes.
      await Promise.all([loadNotebook(), loadMemoryActivity()]);
      setNotebookMessage(res.message || `${target} ${action} ok`);
      setNotebookDraft((prev) => ({ ...prev, [target]: "" }));
    } catch (e) {
      setNotebookMessage(e instanceof Error ? e.message : String(e));
    } finally {
      setNotebookBusy("");
    }
  }

  async function searchMemory() {
    const query = memoryQuery.trim();
    if (!query) return;
    const result = await runMemoryAction("search", () =>
      clientApi.memoryVectorSearch({ query, top_k: 5 }),
    );
    // Refresh the activity stream so the operator sees their search
    // appear in the trace alongside any writes.
    void loadMemoryActivity();
    if (result && typeof result === "object" && "results" in result) {
      setMemoryResults((result as { results?: Array<Record<string, unknown>> }).results || []);
    }
  }

  async function saveAdminPassword() {
    if (newAdminPassword.length < 8) {
      setError(tAuth("tooShort"));
      return;
    }
    if (newAdminPassword !== confirmAdminPassword) {
      setError(tAuth("mismatch"));
      return;
    }
    setAuthBusy(true);
    setError(null);
    setInfo(null);
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
      setInfo(tAuth("saved"));
      await loadAuthStatus();
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setAuthBusy(false);
    }
  }

  function logoutAdmin() {
    clearStoredAuthToken();
    setInfo(tAuth("loggedOut"));
    if (!isLocalDashboardHost()) {
      window.location.assign("/login");
    }
  }

  function parseSearchChain(csv: string): string[] {
    return csv
      .split(/[,\n]/)
      .map((part) => part.trim().toLowerCase())
      .filter(Boolean);
  }

  async function saveSearchEngines(opts: { keysOnly?: boolean } = {}) {
    setSearchBusy("save");
    setError(null);
    setInfo(null);
    try {
      const body: Parameters<typeof clientApi.searchEnginesConfig>[0] = {
        store: searchStore,
      };
      if (!opts.keysOnly) {
        const chain = parseSearchChain(searchChainCsv);
        if (chain.length) body.engines = chain;
        if (searchRegion.trim()) body.region = searchRegion.trim();
        if (searchSafesearch.trim()) body.safesearch = searchSafesearch.trim();
        const baseUrlBody: Record<string, string> = {};
        const previousByEngine = new Map(
          (searchStatus?.engine_status || [])
            .filter((row) => row.needs_base_url)
            .map((row) => [row.name, row.base_url?.workspace || ""]),
        );
        for (const [engine, draft] of Object.entries(searchBaseUrlDrafts)) {
          const next = (draft || "").trim();
          const previous = previousByEngine.get(engine) || "";
          if (next === previous) continue;
          baseUrlBody[engine] = next;
        }
        if (Object.keys(baseUrlBody).length) body.base_urls = baseUrlBody;
      }
      const drafts: Record<string, string> = {};
      for (const [engine, raw] of Object.entries(searchKeyDrafts)) {
        const trimmed = (raw || "").trim();
        if (trimmed === "") continue; // empty/untouched draft → don't overwrite vault
        drafts[engine] = trimmed;
      }
      if (Object.keys(drafts).length) body.keys = drafts;
      const res = await clientApi.searchEnginesConfig(body);
      applySearchStatus(res);
      setInfo(tSearch("savedAll"));
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setSearchBusy("");
    }
  }

  async function clearSearchKeys(engine: string) {
    setSearchBusy(`clear:${engine}`);
    setError(null);
    setInfo(null);
    try {
      const res = await clientApi.searchEnginesConfig({
        store: searchStore,
        keys: { [engine]: [] },
      });
      applySearchStatus(res);
      setInfo(`Cleared keys for ${engine}.`);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setSearchBusy("");
    }
  }

  async function saveSearchEngineRow(engine: string) {
    setSearchBusy(`save:${engine}`);
    setError(null);
    setInfo(null);
    try {
      const body: Parameters<typeof clientApi.searchEnginesConfig>[0] = {
        store: searchStore,
      };
      const draftKey = (searchKeyDrafts[engine] ?? "").trim();
      if (draftKey) {
        body.keys = { [engine]: draftKey };
      }
      const row = (searchStatus?.engine_status || []).find((r) => r.name === engine);
      if (row?.needs_base_url) {
        const previous = row.base_url?.workspace || "";
        const next = (searchBaseUrlDrafts[engine] ?? "").trim();
        if (next !== previous) {
          body.base_urls = { [engine]: next };
        }
      }
      if (!body.keys && !body.base_urls) {
        setSearchEngineRowResult((p) => ({
          ...p,
          [engine]: "no changes: type a key or change the base URL first",
        }));
        return;
      }
      const res = await clientApi.searchEnginesConfig(body);
      applySearchStatus(res);
      setSearchEngineRowResult((p) => ({
        ...p,
        [engine]: `saved (${searchStore})`,
      }));
    } catch (e) {
      setSearchEngineRowResult((p) => ({
        ...p,
        [engine]: e instanceof Error ? e.message : String(e),
      }));
    } finally {
      setSearchBusy("");
    }
  }

  async function testSearchEngineRow(engine: string) {
    setSearchBusy(`test:${engine}`);
    setSearchEngineRowResult((p) => ({ ...p, [engine]: "probing…" }));
    try {
      const res = await clientApi.searchEnginesTest({
        query: searchTestQuery.trim() || "Nerya engine probe",
        engine,
        max_results: 3,
      });
      if (!res.ok) {
        setSearchEngineRowResult((p) => ({
          ...p,
          [engine]: `error: ${res.error || "test failed"}${res.stderr_tail ? "\n" + res.stderr_tail : ""}`,
        }));
        return;
      }
      const result = res.result as Record<string, unknown> | null;
      const items = result && typeof result === "object"
        ? (result as { results?: unknown[] }).results
        : undefined;
      const count = Array.isArray(items) ? items.length : 0;
      const engineUsed = result && typeof result === "object"
        ? String((result as { engine?: unknown }).engine || "")
        : "";
      setSearchEngineRowResult((p) => ({
        ...p,
        [engine]: `ok: ${count} result(s) via ${engineUsed || engine} · ${res.elapsed_ms ?? "?"}ms`,
      }));
    } catch (e) {
      setSearchEngineRowResult((p) => ({
        ...p,
        [engine]: e instanceof Error ? e.message : String(e),
      }));
    } finally {
      setSearchBusy("");
    }
  }

  async function deploySearxng() {
    setSearchBusy("searxng-deploy");
    setError(null);
    setInfo(null);
    try {
      const res = await clientApi.searchSearxngDeploy({
        host_port: searxngHostPort.trim() ? Number(searxngHostPort.trim()) : undefined,
        image: searxngImage.trim() || undefined,
        rebuild: searxngRebuild,
      });
      if (!res.ok) {
        throw new Error(res.detail || res.error || "deploy failed");
      }
      setInfo(`SearXNG deployed at ${res.base_url || "http://127.0.0.1:" + searxngHostPort}.`);
      await loadSearchStatus();
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setSearchBusy("");
    }
  }

  async function teardownSearxng(opts: { remove?: boolean } = {}) {
    setSearchBusy(opts.remove === false ? "searxng-stop" : "searxng-teardown");
    setError(null);
    setInfo(null);
    try {
      const res = await clientApi.searchSearxngTeardown({
        remove: opts.remove !== false,
      });
      if (!res.ok) throw new Error(res.detail || res.error || "teardown failed");
      setInfo(opts.remove === false ? "SearXNG container stopped." : "SearXNG container removed.");
      await loadSearchStatus();
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setSearchBusy("");
    }
  }

  async function loadBrowsersStatus() {
    try {
      const next = await clientApi.browsersStatus();
      setBrowsersStatus(next);
    } catch (e) {
      // best effort — settings page should still render
      setBrowsersStatus(null);
    }
  }

  async function selectBrowser(name: string) {
    setBrowsersBusy(`select:${name}`);
    setError(null);
    setInfo(null);
    try {
      const res = await clientApi.browsersSelect(name);
      setBrowsersStatus(res);
      setInfo(`Selected ${name}.`);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setBrowsersBusy("");
    }
  }

  async function installBrowser(name: string) {
    setBrowsersBusy(`install:${name}`);
    setError(null);
    setInfo(null);
    try {
      const res = await clientApi.browsersInstall(name);
      if (!res.ok) {
        throw new Error(res.detail || res.error || "install failed");
      }
      if (res.status) setBrowsersStatus(res.status);
      else await loadBrowsersStatus();
      setInfo(`Installed ${name}${res.version ? ` (${res.version})` : ""}.`);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setBrowsersBusy("");
    }
  }

  async function uninstallBrowser(name: string) {
    setBrowsersBusy(`uninstall:${name}`);
    setError(null);
    setInfo(null);
    try {
      const res = await clientApi.browsersUninstall(name);
      if (!res.ok) throw new Error(res.detail || res.error || "uninstall failed");
      if (res.status) setBrowsersStatus(res.status);
      else await loadBrowsersStatus();
      setInfo(`Uninstalled ${name}.`);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setBrowsersBusy("");
    }
  }

  async function loadFinancialDatasetsStatus() {
    try {
      const next = await clientApi.financialDatasetsStatus();
      setFdStatus(next);
    } catch (e) {
      setFdStatus(null);
    }
  }

  async function saveFinancialDatasetsKeys() {
    setFdBusy("save");
    setError(null);
    setInfo(null);
    try {
      const text = fdKeysDraft.trim();
      const res = await clientApi.financialDatasetsSetKeys({
        keys: text,
        store: fdStore,
      });
      setFdStatus(res);
      setFdKeysDraft("");
      setInfo("Financial Datasets API keys saved.");
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setFdBusy("");
    }
  }

  async function clearFinancialDatasetsKeys() {
    setFdBusy("clear");
    setError(null);
    setInfo(null);
    try {
      const res = await clientApi.financialDatasetsSetKeys({
        keys: [],
        store: fdStore,
      });
      setFdStatus(res);
      setFdKeysDraft("");
      setInfo(`Cleared Financial Datasets keys (${fdStore}).`);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setFdBusy("");
    }
  }

  async function probeBrowser() {
    setBrowsersBusy("probe");
    setBrowserProbeResult(null);
    try {
      const res = await clientApi.browsersProbe({
        name: browserProbeName.trim().toLowerCase() || undefined,
        url: browserProbeUrl.trim() || "https://example.com",
      });
      if (!res.ok) {
        setBrowserProbeResult(
          [res.error, res.detail, res.stderr_tail].filter(Boolean).join("\n"),
        );
        return;
      }
      const preview = res.markdown_preview || res.text_preview || res.html_preview
        || res.markdown || res.text || res.html || "";
      setBrowserProbeResult(
        `via ${res.fetch_method || res.name} · ${res.elapsed_ms ?? "?"}ms · ${res.bytes ?? "?"}B`
          + (preview ? "\n\n" + preview : ""),
      );
    } catch (e) {
      setBrowserProbeResult(e instanceof Error ? e.message : String(e));
    } finally {
      setBrowsersBusy("");
    }
  }

  async function testBrowserRow(name: string) {
    setBrowsersBusy(`test:${name}`);
    setBrowserRowResult((p) => ({ ...p, [name]: "probing…" }));
    try {
      const res = await clientApi.browsersProbe({
        name,
        url: browserProbeUrl.trim() || "https://example.com",
      });
      if (!res.ok) {
        setBrowserRowResult((p) => ({
          ...p,
          [name]: `error: ${[res.error, res.detail, res.stderr_tail]
            .filter(Boolean)
            .join(" · ")}`,
        }));
        return;
      }
      setBrowserRowResult((p) => ({
        ...p,
        [name]: `ok: ${res.fetch_method || name} · ${res.elapsed_ms ?? "?"}ms · ${res.bytes ?? "?"}B`,
      }));
    } catch (e) {
      setBrowserRowResult((p) => ({
        ...p,
        [name]: e instanceof Error ? e.message : String(e),
      }));
    } finally {
      setBrowsersBusy("");
    }
  }

  async function runSearchEngineTest() {
    setSearchBusy("test");
    setSearchTestResult(null);
    try {
      const res = await clientApi.searchEnginesTest({
        query: searchTestQuery.trim() || "Nerya search engine probe",
        engine: searchTestEngine.trim().toLowerCase() || undefined,
        max_results: 3,
      });
      if (!res.ok) {
        setSearchTestResult(
          res.error
            ? `${res.error}${res.stderr_tail ? "\n" + res.stderr_tail : ""}`
            : "test failed",
        );
        return;
      }
      const result = res.result as Record<string, unknown> | null;
      const engineUsed = result && typeof result === "object"
        ? String((result as { engine?: unknown }).engine || "")
        : "";
      const items = result && typeof result === "object"
        ? (result as { results?: unknown[] }).results
        : undefined;
      const count = Array.isArray(items) ? items.length : 0;
      setSearchTestResult(
        `engine=${engineUsed || "?"} · results=${count} · ${res.elapsed_ms ?? "?"}ms`,
      );
    } catch (e) {
      setSearchTestResult(e instanceof Error ? e.message : String(e));
    } finally {
      setSearchBusy("");
    }
  }

  function selectSettingsTab(tab: SettingsTabKey) {
    setActiveSettingsTab(tab);
    if (typeof window !== "undefined") {
      const url = new URL(window.location.href);
      url.hash = tab;
      window.history.replaceState(null, "", url);
    }
  }

  // Memory / Access & Gateways / Web search / Browsers are intentionally
  // OMITTED from the settings section nav. Each lives on its own top-bar
  // "More" route (/memory, /access, /web-search, /browsers) and is
  // rendered by mounting this component with `forceSection=<key>`. The
  // section nav below only shows what's still managed inside /settings.
  const settingsTabs: SettingsTabItem[] = [
    {
      key: "models",
      label: tTabs("models"),
      description: tTabs("modelsDesc"),
      meta: tTabs("modelsMeta", {
        configured: configuredTierCount,
        total: tierRows.length || ASSIGNMENT_TIERS.length,
        providers: readyProviderCount,
        models: catalogModelCount,
      }),
    },
    {
      key: "access",
      label: tTabs("access"),
      description: tTabs("accessDesc"),
      meta: tTabs("accessMeta", {
        auth: authStatus?.password_configured ? tAuth("configured") : tAuth("notConfigured"),
      }),
    },
    {
      key: "runtime",
      label: tTabs("runtime"),
      description: tTabs("runtimeDesc"),
      meta: tTabs("runtimeMeta", {
        proxy: proxyEnabled ? tTabs("enabled") : tTabs("disabled"),
        tunnels: tunnelRunningCount,
      }),
    },
    {
      key: "capabilityGates",
      label: tTabs("capabilityGates"),
      description: tTabs("capabilityGatesDesc"),
      meta: tTabs("capabilityGatesMeta"),
    },
    {
      key: "interface",
      label: tTabs("interface"),
      description: tTabs("interfaceDesc"),
      meta: tTabs("interfaceMeta", {
        timezone: uiSettings.timezone,
        symbol: uiSettings.kline.symbol,
      }),
    },
  ];

  // In section mode (mounted by /memory, /access, /web-search,
  // /browsers) we pin the active tab to the forced section so the
  // matching panel renders even though it's no longer in the section
  // nav above.
  const effectiveSettingsTab: SettingsTabKey | ForceSectionKey = forceSection ?? activeSettingsTab;

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
    memory: tMemoryPage as unknown as SectionPageTranslator,
    search: tWebSearchPage as unknown as SectionPageTranslator,
    browsers: tBrowsersPage as unknown as SectionPageTranslator,
    envvault: tEnvVaultPage as unknown as SectionPageTranslator,
  };
  const tSectionPage: SectionPageTranslator | null = forceSection
    ? sectionPageTranslations[forceSection] ?? null
    : null;

  function renderSearchEngineRow(row: SearchEngineStatus) {
    const counts = row.key_counts || { workspace: 0, vault: 0, env: 0, total: 0 };
    const draftKey = searchKeyDrafts[row.name] ?? "";
    const inChain = (searchStatus?.engines || []).includes(row.name);
    const baseUrlInfo = row.base_url || {};
    const baseUrlDraft = searchBaseUrlDrafts[row.name] ?? (baseUrlInfo.workspace || "");
    return (
      <div
        key={row.name}
        className="rounded-lg border border-[color:var(--line)] p-3"
      >
        <div className="flex flex-wrap items-center justify-between gap-2">
          <div className="min-w-0">
            <div className="text-[13px] font-medium text-ink-100">{row.name}</div>
            <div
              className="mt-0.5 text-[11px] text-ink-500"
              title={
                row.needs_key
                  ? tSearch("keysCountsLocation", {
                      vault: counts.vault,
                      workspace: counts.workspace,
                      env: counts.env,
                    })
                  : undefined
              }
            >
              {row.needs_key
                ? tSearch("keysCounts", { total: counts.total })
                : tSearch("keyless")}
              {row.needs_base_url
                ? ` · ${tSearch("baseUrlInline", {
                    url: baseUrlInfo.effective || "–",
                  })}`
                : ""}
            </div>
          </div>
          <div className="flex flex-wrap justify-end gap-1.5">
            {inChain ? <Pill tone="brand">{tSearch("inChain")}</Pill> : null}
            <Pill tone={row.ready ? "ok" : "warn"}>
              {row.ready
                ? tSearch("ready")
                : row.needs_base_url && !baseUrlInfo.effective
                  ? tSearch("needsBaseUrl")
                  : tSearch("needsKey")}
            </Pill>
          </div>
        </div>
        {row.needs_key ? (
          <div className="mt-3 grid grid-cols-1 gap-2 md:grid-cols-[1fr_auto_auto]">
            <textarea
              className="input-dark font-mono text-xs"
              rows={Math.min(4, Math.max(1, draftKey.split(/[,\n]/).filter(Boolean).length || 1))}
              value={draftKey}
              onChange={(e) => setSearchKeyDrafts((p) => ({ ...p, [row.name]: e.target.value }))}
              placeholder={tSearch("keyPlaceholder")}
              spellCheck={false}
              autoCorrect="off"
              autoCapitalize="off"
            />
            <button
              type="button"
              className="btn btn-ghost"
              onClick={() => setSearchKeyDrafts((p) => {
                const next = { ...p };
                delete next[row.name];
                return next;
              })}
              disabled={!draftKey}
            >
              {tSearch("undo")}
            </button>
            <button
              type="button"
              className="btn btn-ghost"
              onClick={() => void clearSearchKeys(row.name)}
              disabled={Boolean(searchBusy) || counts.total === 0}
            >
              {searchBusy === `clear:${row.name}` ? tCommon("saving") : tSearch("clear")}
            </button>
          </div>
        ) : null}
        {row.needs_base_url ? (
          <div className="mt-2 grid grid-cols-1 gap-2 md:grid-cols-[1fr_auto]">
            <input
              className="input-dark font-mono text-xs"
              value={baseUrlDraft}
              onChange={(e) => setSearchBaseUrlDrafts((p) => ({ ...p, [row.name]: e.target.value }))}
              placeholder={baseUrlInfo.default || "https://example.com"}
              spellCheck={false}
            />
            <span className="self-center text-[11px] text-ink-500">
              {tSearch("envDefault", {
                env: baseUrlInfo.env || "–",
                def: baseUrlInfo.default || "–",
              })}
            </span>
          </div>
        ) : null}
        <div className="mt-2 flex flex-wrap items-center gap-2">
          {row.needs_key || row.needs_base_url ? (
            <button
              type="button"
              className="btn btn-primary"
              onClick={() => void saveSearchEngineRow(row.name)}
              disabled={Boolean(searchBusy)}
              title={tSearch("saveRowTitle")}
            >
              <CheckIcon size={12} />
              {searchBusy === `save:${row.name}` ? tCommon("saving") : tSearch("save")}
            </button>
          ) : null}
          <button
            type="button"
            className="btn btn-ghost"
            onClick={() => void testSearchEngineRow(row.name)}
            disabled={Boolean(searchBusy)}
            title={tSearch("testRowTitle")}
          >
            <SearchIcon size={12} />
            {searchBusy === `test:${row.name}` ? tSearch("testing") : tSearch("test")}
          </button>
          {row.key_preview && row.key_preview.length ? (
            <span className="font-mono text-[11px] text-ink-500">
              {tSearch("stored", { value: row.key_preview.join(" · ") })}
            </span>
          ) : null}
          {searchEngineRowResult[row.name] ? (
            <span
              className={`ml-auto font-mono text-[11px] ${
                /^(error|fail|missing|❌)/i.test(searchEngineRowResult[row.name] || "")
                  ? "text-rose-300"
                  : "text-emerald-300"
              }`}
            >
              {searchEngineRowResult[row.name]}
            </span>
          ) : null}
        </div>
      </div>
    );
  }

  return (
    <PageBody>
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
            onClick={() => void loadModelConfig()}
            disabled={loading}
          >
            <RefreshIcon size={14} />
            {loading ? tCommon("loading") : tCommon("refresh")}
          </button>
        }
      />

      {/* Optional banner injected by a host page (e.g. /browsers
          passes its Engines/Session tab strip here so the strip lives
          immediately under the section-page header instead of below
          the panel content). */}
      {inSectionMode && topBanner ? topBanner : null}

      {error ? <ErrorBanner error={error} /> : null}
      {info ? (
        <div className="rounded-lg border border-emerald-400/30 bg-emerald-500/10 px-3 py-2 text-[12px] text-emerald-200">
          {info}
        </div>
      ) : null}

      {/* Hide the section nav whenever this component is mounted in
          section mode (/memory, /access, /web-search, /browsers) —
          each standalone page owns its own top-level header and
          doesn't need the full Settings tab bar above it. */}
      {inSectionMode ? null : (
        <SettingsModuleTabs
          active={activeSettingsTab}
          ariaLabel={tTabs("ariaLabel")}
          items={settingsTabs}
          onChange={selectSettingsTab}
        />
      )}

      {effectiveSettingsTab === "models" && (!inSectionMode || forceSection === "models") ? (
        <div
          id={settingsPanelId("models")}
          role="tabpanel"
          aria-labelledby={settingsTabId("models")}
          className="space-y-5"
        >
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

            <div className="grid grid-cols-1 gap-3 lg:grid-cols-2">
              <Field label={tProvider("providerLabel")} hint={tProvider("providerHint")}>
                {/* Combo input: type any provider id (free-form) or
                    pick one from the catalogue via the datalist. The
                    free-text path is what unlocks "manually add a
                    provider" — anything not in the catalogue is treated
                    as a custom id and the operator picks the API shape
                    from the preset selector below. */}
                <input
                  className="input-dark font-mono"
                  list="provider-catalog-options"
                  value={providerDraft}
                  onChange={(e) => setProviderDraftFromSelect(e.target.value)}
                  placeholder={tProvider("providerPlaceholder")}
                  autoComplete="off"
                  spellCheck={false}
                />
                <datalist id="provider-catalog-options">
                  {providerOptions.map((p) => {
                    const entry = catalogById.get(p);
                    return (
                      <option key={p} value={p}>
                        {entry ? `${entry.api_mode}` : "custom"}
                      </option>
                    );
                  })}
                </datalist>
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
              <div className="lg:col-span-2 flex flex-wrap items-end gap-2">
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
                  {importing
                    ? tProvider("importing")
                    : tProvider("importSelectedCount", { count: selectedModelIds.size })}
                </button>
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
              title={tProvider("advancedTitle")}
              description={tProvider("advancedDesc")}
              storageKey="nerya.settings.provider.advanced"
            >
              <div className="grid grid-cols-1 gap-3 lg:grid-cols-2">
                <Field label={tProvider("baseUrlLabel")} hint={tProvider("baseUrlHint")}>
                  <input
                    className="input-dark font-mono"
                    value={providerBaseUrlDraft}
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
              <div className="embedded-list-scroll mt-4 rounded-lg border border-brand-500/10 bg-ink-950/35">
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
            ) : null}
          </Card>

          {compactLlm ? null : (
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

            <div className="mb-4 flex flex-wrap items-end gap-3 rounded-lg border border-[color:var(--line)] p-3">
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
                const routes = routesOf(row);
                const configuredRoutes = routes.filter((route) =>
                  route.provider.trim() && route.model.trim()
                ).length;
                const anyRouteConfigured = configuredRoutes > 0;
                const anyRouteReady = routes.some((route) => routeHasCredential(route));
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
                    <div className="mb-3 flex flex-wrap items-start justify-between gap-2">
                      <div>
                        <div className="text-[13px] font-medium text-ink-100">{tierLabel(row.tier, tModel)}</div>
                        <div className="mt-0.5 text-[11px] text-ink-500">
                          {laneKey ? tModel(laneKey) : `${row.tier} model lane`}
                        </div>
                      </div>
                      <div className="flex flex-wrap justify-end gap-1.5">
                        <Pill tone={anyRouteReady ? "ok" : "warn"}>
                          {anyRouteReady ? tModel("ready") : tModel("keyRefMissing")}
                        </Pill>
                        <Pill tone={anyRouteConfigured ? "neutral" : "warn"}>
                          {tModel("configuredRoutes", { count: configuredRoutes })}
                        </Pill>
                        {row.tier === defaultTier ? <Pill tone="brand">{tModel("default")}</Pill> : null}
                        {row.tier === intentTier ? <Pill tone="brand">{tModel("intent")}</Pill> : null}
                      </div>
                    </div>
                    <div className="space-y-2.5">
                      {routes.map((route, routeIndex) => {
                        const artifacts = providerArtifacts(route.provider);
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
                        const providerKeyValues = splitRouteValues(
                          route.provider_keys?.length ? route.provider_keys : route.provider_key,
                        );
                        const providerKeyRefValues = splitRouteValues(
                          route.provider_key_refs?.length
                            ? route.provider_key_refs
                            : route.provider_key_ref,
                        );
                        const keyValue = providerKeyValues.length
                          ? providerKeyValues.join(", ")
                          : providerKeyRefValues.join(", ");
                        const routeReady = routeHasCredential(route);
                        const canRemove = routes.length > 1;
                        return (
                          <div
                            key={`${row.tier}-${routeIndex}`}
                            className="rounded-lg border border-[color:var(--line)] bg-ink-950/20 p-3"
                          >
                            <div className="mb-2.5 flex items-center justify-between gap-2">
                              <div className="flex min-w-0 items-center gap-2">
                                <span className="text-[12px] font-medium text-ink-200">
                                  {tModel("routeLabel", { index: routeIndex + 1 })}
                                </span>
                                <Pill tone={routeReady ? "ok" : "warn"}>
                                  {routeReady ? tModel("ready") : tModel("keyRefMissing")}
                                </Pill>
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
                            <div className="grid grid-cols-1 gap-3 md:grid-cols-2 xl:grid-cols-5">
                              <Field
                                label={tProvider("providerLabel")}
                                hint={artifacts.base_url || tModel("selectProvider")}
                              >
                                <PortalSelect
                                  value={route.provider}
                                  onChange={(value) => {
                                    const defaults = routeDefaultsForProvider(value);
                                    patchTierRoute(index, routeIndex, {
                                      provider: value,
                                      base_url: defaults.base_url,
                                      kind: defaults.kind,
                                    });
                                  }}
                                  options={[
                                    { value: "", label: tModel("selectProvider") },
                                    ...providerOptions.map((p) => ({
                                      value: p,
                                      label: p,
                                    })),
                                  ]}
                                  size="sm"
                                  ariaLabel={tProvider("providerLabel")}
                                />
                              </Field>
                              <Field
                                label={tProvider("modelLabel")}
                                hint={models.length ? tModel("importedCount", { count: models.length }) : tModel("importModelsFirst")}
                              >
                                <ModelSelectInput
                                  value={modelInputValue}
                                  onChange={(value) => {
                                    patchTierRoute(index, routeIndex, {
                                      model: value,
                                      models: splitRouteValues(value),
                                    });
                                    if (row.tier === INTENT_TIER && value)
                                      setIntentTier(INTENT_TIER);
                                  }}
                                  disabled={!route.provider}
                                  options={modelOptions}
                                  placeholder={tModel("selectModel")}
                                  ariaLabel={tProvider("modelLabel")}
                                  className="font-mono"
                                  emptyHint={models.length ? tModel("selectModel") : tModel("importModelsFirst")}
                                />
                              </Field>
                              <Field label={tProvider("baseUrlLabel")} hint={tProvider("baseUrlHint")}>
                                <input
                                  className="input-dark font-mono text-xs"
                                  value={route.base_url || ""}
                                  onChange={(e) =>
                                    patchTierRoute(index, routeIndex, { base_url: e.target.value })
                                  }
                                  placeholder={artifacts.base_url || "https://api.example.com/v1"}
                                  autoComplete="off"
                                  spellCheck={false}
                                />
                              </Field>
                              <Field label={tProvider("apiKeyLabel")} hint={tProvider("apiKeyHint")}>
                                <input
                                  className="input-dark font-mono text-xs"
                                  value={keyValue}
                                  onChange={(e) => {
                                    const value = e.target.value;
                                    const values = splitRouteValues(value);
                                    patchTierRoute(index, routeIndex, value.trim().startsWith("vault://")
                                      ? {
                                          provider_key_ref: value,
                                          provider_key_refs: values,
                                          provider_key: "",
                                          provider_keys: [],
                                        }
                                      : {
                                          provider_key: value,
                                          provider_keys: values,
                                          provider_key_ref: "",
                                          provider_key_refs: [],
                                        });
                                  }}
                                  type={keyValue.startsWith("vault://") ? "text" : "password"}
                                  placeholder={tProvider("apiKeyPlaceholder")}
                                  autoComplete="off"
                                  spellCheck={false}
                                />
                              </Field>
                              <Field label={tModel("kindLabel")} hint={tModel("kindHint")}>
                                <PortalSelect
                                  value={route.kind || "chat_completions"}
                                  onChange={(value) =>
                                    patchTierRoute(index, routeIndex, { kind: value })
                                  }
                                  options={[
                                    {
                                      value: "chat_completions",
                                      label: tModel("kindChatCompletions"),
                                    },
                                    {
                                      value: "anthropic_messages",
                                      label: tModel("kindAnthropicMessages"),
                                    },
                                  ]}
                                  size="sm"
                                  ariaLabel={tModel("kindLabel")}
                                  className="font-mono"
                                />
                              </Field>
                            </div>
                          </div>
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
                    <div className="mt-3 grid grid-cols-1 gap-3 md:grid-cols-2 lg:grid-cols-3">
                      <Field
                        label={tModel("reasoningEffortLabel")}
                        hint={tModel("reasoningEffortHint")}
                      >
                        <PortalSelect
                          value={row.reasoning_effort || ""}
                          onChange={(value) =>
                            patchTier(index, { reasoning_effort: value })
                          }
                          disabled={!anyRouteConfigured}
                          options={[
                            {
                              value: "",
                              label: tModel("reasoningEffortDefault"),
                            },
                            ...reasoningLevels.map((level) => {
                              let label: string;
                              try {
                                label = tModel(
                                  `reasoningEffortLevel.${level}` as never,
                                );
                              } catch {
                                label = prettifyReasoningLevel(level);
                              }
                              return { value: level, label };
                            }),
                          ]}
                          size="sm"
                          ariaLabel={tModel("reasoningEffortLabel")}
                        />
                      </Field>
                    </div>
                  </div>
                );
              })}
            </div>
          </Card>
          )}
        </div>
      ) : null}

      {effectiveSettingsTab === "access" && (!inSectionMode || forceSection === "access") ? (
        <div
          id={settingsPanelId("access")}
          role="tabpanel"
          aria-labelledby={settingsTabId("access")}
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
              <Row label={tAuth("mode")} desc={tAuth("modeDesc")}>
                <span className="font-mono text-[11px] text-ink-200">
                  {authStatus?.mode || "local"}
                </span>
              </Row>
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

          <Advanced
            title={tFdApi("title")}
            description={tFdApi("description")}
            count={
              fdStatus?.ready
                ? fdStatus.total_keys === 1
                  ? tFdApi("keysReady", { count: fdStatus.total_keys })
                  : tFdApi("keysReadyPlural", { count: fdStatus.total_keys })
                : undefined
            }
            storageKey="nerya.settings.access.advanced.fdapi"
          >
            <div className="space-y-3">
              <div className="text-[11px] text-ink-500">
                Vault: <span className="font-mono">{fdStatus?.vault_count ?? 0}</span> ·
                Env: <span className="font-mono">{fdStatus?.env_count ?? 0}</span>
                {fdStatus?.env_sources?.length
                  ? ` (${fdStatus.env_sources.join(", ")})`
                  : ""}
                {fdStatus?.key_preview?.length ? (
                  <>
                    {" · "}
                    <span className="font-mono">{fdStatus.key_preview.join(" ")}</span>
                  </>
                ) : null}
              </div>
              <Field
                label="API key(s)"
                hint="comma-separated · leave blank to keep existing"
              >
                <input
                  className="input-dark font-mono text-xs"
                  type="password"
                  value={fdKeysDraft}
                  onChange={(e) => setFdKeysDraft(e.target.value)}
                  placeholder="k1,k2,k3"
                />
              </Field>
              <Field label="Storage" hint="vault is encrypted at rest">
                <Select
                  value={fdStore}
                  onChange={(v) => setFdStore(v === "workspace" ? "workspace" : "vault")}
                  options={[
                    { value: "vault", label: "SecretVault (encrypted)" },
                    { value: "workspace", label: "Workspace JSON (plaintext, dev only)" },
                  ]}
                />
              </Field>
              <div className="flex flex-wrap justify-end gap-2">
                <button
                  type="button"
                  className="btn btn-ghost"
                  onClick={() => void clearFinancialDatasetsKeys()}
                  disabled={Boolean(fdBusy) || (fdStatus?.total_keys ?? 0) === 0}
                >
                  {fdBusy === "clear" ? tCommon("saving") : "clear"}
                </button>
                <button
                  type="button"
                  className="btn btn-ghost"
                  onClick={() => void loadFinancialDatasetsStatus()}
                  disabled={Boolean(fdBusy)}
                >
                  <RefreshIcon size={14} />
                  {tCommon("refresh")}
                </button>
                <button
                  type="button"
                  className="btn btn-primary"
                  onClick={() => void saveFinancialDatasetsKeys()}
                  disabled={Boolean(fdBusy) || !fdKeysDraft.trim()}
                >
                  <CheckIcon size={14} />
                  {fdBusy === "save" ? tCommon("saving") : "save key(s)"}
                </button>
              </div>
              {fdStatus?.documentation ? (
                <a
                  className="text-[11px] text-brand-300 hover:underline"
                  href={fdStatus.documentation}
                  target="_blank"
                  rel="noreferrer"
                >
                  Financial Datasets API docs ↗
                </a>
              ) : null}
            </div>
          </Advanced>
          </div>
          {/* Gateway channels now live in the dedicated /gateway page
              (see components/GatewayChannelsPanel) — there is no
              right column here any more so the Admin password +
              Financial Datasets API cards span the full width. */}
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
          role="tabpanel"
          aria-labelledby={settingsTabId("runtime")}
          className="grid grid-cols-1 gap-5 xl:grid-cols-[380px_1fr]"
        >
          <div className="xl:col-span-2">
            <Card
              featured
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
                    <Row label="Enable proxy" desc="Updates runtime env and urllib immediately after save.">
                      <SwitchControl
                        checked={proxyEnabled}
                        label={proxyEnabled ? "Enabled" : "Disabled"}
                        onCheckedChange={(v) => setProxyEnabled(v)}
                      />
                    </Row>
                    <Field label="Mode">
                      <Select
                        value={proxyMode}
                        onChange={(v) => setProxyMode(v === "pool" ? "pool" : "direct")}
                        options={[
                          { value: "direct", label: "Direct proxy" },
                          { value: "pool", label: "Proxy pool" },
                        ]}
                      />
                    </Field>
                    <Field label="Preset" hint="Open-source/local pool defaults">
                      <Select
                        value={proxyPreset}
                        onChange={applyProxyPreset}
                        options={(proxyPresets.length ? proxyPresets : [{ id: "custom", label: "Custom proxy" }]).map((row) => ({
                          value: row.id,
                          label: row.label,
                        }))}
                      />
                    </Field>
                  </div>

                  {proxyMode === "direct" ? (
                    <div className="grid grid-cols-1 gap-3 md:grid-cols-3">
                      <Field
                        label="All proxy"
                        hint={proxyRefs.all_url_ref ? `stored ${proxyStatus?.config?.all_url_preview || proxyRefs.all_url_ref}` : "Used for HTTP and HTTPS when specific fields are blank"}
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
                        label="HTTP proxy"
                        hint={proxyRefs.http_url_ref ? `stored ${proxyStatus?.config?.http_url_preview || proxyRefs.http_url_ref}` : "Optional"}
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
                        label="HTTPS proxy"
                        hint={proxyRefs.https_url_ref ? `stored ${proxyStatus?.config?.https_url_preview || proxyRefs.https_url_ref}` : "Optional"}
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
                        label="Pool API URL"
                        hint={proxyRefs.pool_url_ref ? `stored ${proxyStatus?.config?.pool_url_preview || proxyRefs.pool_url_ref}` : "Endpoint returning one proxy"}
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
                      <Field label="Response format">
                        <Select
                          value={proxyPoolFormat}
                          onChange={setProxyPoolFormat}
                          options={[
                            { value: "auto", label: "Auto" },
                            { value: "jhao_json", label: "jhao JSON" },
                            { value: "smart_json", label: "Smart JSON" },
                            { value: "json", label: "Generic JSON" },
                            { value: "text", label: "Plain text" },
                          ]}
                        />
                      </Field>
                    </div>
                  )}

                  <Field label="NO_PROXY" hint="Keep local dashboard/API and loopback services off the proxy">
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
                      {proxyBusy === "save" ? tCommon("saving") : "Save proxy"}
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
                    <div className="text-[11px] text-ink-500 font-medium">Effective env</div>
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
                          No managed proxy env applied.
                        </div>
                      )}
                    </div>
                  </div>

                  <Field label="Probe URL">
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
                      {proxyBusy === "test" ? "Testing" : "Test proxy"}
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
              title={tTunnel("title")}
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
        </div>
      ) : null}

      {effectiveSettingsTab === "capabilityGates" && (!inSectionMode || forceSection === "capabilityGates") ? (
        <div
          id={settingsPanelId("capabilityGates")}
          role="tabpanel"
          aria-labelledby={settingsTabId("capabilityGates")}
          className="space-y-5"
        >
          <RuntimeFlagsPanel />
        </div>
      ) : null}

      {effectiveSettingsTab === "envvault" && forceSection === "envvault" ? (
        <div
          id={settingsPanelId("envvault")}
          role="tabpanel"
          aria-labelledby={settingsTabId("envvault")}
          className="grid grid-cols-1 gap-5 xl:grid-cols-2"
        >
          <Card
            featured
            title="Runtime environment"
            description="Encrypted variables injected into run_shell, skill script subprocesses, and stdio MCP servers."
            actions={<Pill tone={runtimeEnv.length ? "ok" : "warn"}>{runtimeEnv.length} configured</Pill>}
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
                    No runtime env variables configured.
                  </div>
                )}
              </div>

              <div className="grid grid-cols-1 gap-3">
                <Field label="Variable name" hint="Uppercase shell env name; saved value is encrypted">
                  <input
                    className="input-dark font-mono text-xs"
                    value={envNameDraft}
                    onChange={(e) => setEnvNameDraft(e.target.value)}
                    placeholder="OPENAI_API_KEY"
                    autoCapitalize="off"
                    autoCorrect="off"
                  />
                </Field>
                <Field label="Value" hint="Not shown again after save">
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
                  {securityBusy === "env:save" ? tCommon("saving") : "Save env"}
                </button>
              </div>

              <p className="text-[11px] leading-5 text-ink-500">
                These variables are resolved from SecretVault only at process-launch time. The agent sees the names and refs, not plaintext values.
              </p>
            </div>
          </Card>

          <Card
            title="SecretVault references"
            description="Manage reusable vault:// refs for providers, accounts, MCP config, gateway tokens, and runtime env."
            actions={<Pill tone={vaultRefs.length ? "brand" : "warn"}>{vaultRefs.length} refs</Pill>}
          >
            <div className="grid grid-cols-1 gap-4 xl:grid-cols-[1fr_360px]">
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
                        <span>preview {row.preview}</span>
                        <span>sha {row.fingerprint}</span>
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
                    SecretVault is empty.
                  </div>
                )}
              </div>

              <div className="space-y-3 rounded-lg border border-brand-500/10 bg-ink-950/30 p-3">
                <Field label="Vault name" hint="lowercase a-z0-9_-.">
                  <input
                    className="input-dark font-mono text-xs"
                    value={vaultNameDraft}
                    onChange={(e) => setVaultNameDraft(e.target.value)}
                    placeholder="mcp_fred_api_key"
                    autoCapitalize="off"
                    autoCorrect="off"
                  />
                </Field>
                <Field label="Kind">
                  <input
                    className="input-dark font-mono text-xs"
                    value={vaultKindDraft}
                    onChange={(e) => setVaultKindDraft(e.target.value)}
                    placeholder="bearer"
                  />
                </Field>
                <Field label="Scopes" hint="comma-separated">
                  <input
                    className="input-dark font-mono text-xs"
                    value={vaultScopeDraft}
                    onChange={(e) => setVaultScopeDraft(e.target.value)}
                    placeholder="mcp.read, env"
                  />
                </Field>
                <Field label="Value" hint="Encrypted; never revealed back">
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
                  {securityBusy === "vault:save" ? tCommon("saving") : "Save vault ref"}
                </button>
              </div>
            </div>
          </Card>
        </div>
      ) : null}

      {effectiveSettingsTab === "search" && (!inSectionMode || forceSection === "search") ? (
        <div
          id={settingsPanelId("search")}
          role="tabpanel"
          aria-labelledby={settingsTabId("search")}
          className="space-y-5"
        >
          <Card
            featured
            title={tSearch("cardTitle")}
            description={tSearch("cardDesc")}
            actions={
              searchStatus ? (
                <Pill tone={searchStatus.usable_in_chain > 0 ? "ok" : "warn"}>
                  {tSearch("enginesReady", {
                    ready: searchStatus.usable_in_chain,
                    total: searchStatus.engines.length,
                  })}
                </Pill>
              ) : null
            }
          >
            <div className="grid grid-cols-1 gap-3 lg:grid-cols-[1fr_auto_auto] lg:items-end">
              <Field label={tSearch("engineChain")} hint={tSearch("engineChainHint")}>
                <input
                  className="input-dark font-mono text-xs"
                  value={searchChainCsv}
                  onChange={(e) => setSearchChainCsv(e.target.value)}
                  placeholder={tSearch("engineChainPlaceholder")}
                />
              </Field>
              <button
                type="button"
                className="btn btn-primary"
                onClick={() => void saveSearchEngines()}
                disabled={Boolean(searchBusy)}
              >
                <CheckIcon size={14} />
                {searchBusy === "save" ? tCommon("saving") : tSearch("saveChain")}
              </button>
              <button
                type="button"
                className="btn btn-ghost"
                onClick={() => void loadSearchStatus()}
                disabled={Boolean(searchBusy)}
              >
                <RefreshIcon size={14} />
                {tCommon("refresh")}
              </button>
            </div>

            <Advanced
              title={tSearch("defaultsAdvancedTitle")}
              storageKey="nerya.settings.search.advanced.defaults"
            >
              <div className="grid grid-cols-1 gap-3 md:grid-cols-3">
                <Field label={tSearch("region")} hint={tSearch("regionHint")}>
                  <input
                    className="input-dark font-mono text-xs"
                    value={searchRegion}
                    onChange={(e) => setSearchRegion(e.target.value)}
                    placeholder="wt-wt"
                  />
                </Field>
                <Field label={tSearch("safesearch")}>
                  <Select
                    value={searchSafesearch}
                    onChange={setSearchSafesearch}
                    options={[
                      { value: "off", label: tSearch("safesearchOff") },
                      { value: "moderate", label: tSearch("safesearchModerate") },
                      { value: "strict", label: tSearch("safesearchStrict") },
                    ]}
                  />
                </Field>
                <Field label={tSearch("keyStorage")} hint={tSearch("keyStorageHint")}>
                  <Select
                    value={searchStore}
                    onChange={(v) => setSearchStore(v === "workspace" ? "workspace" : "vault")}
                    options={[
                      { value: "vault", label: tSearch("storeVault") },
                      { value: "workspace", label: tSearch("storeWorkspace") },
                    ]}
                  />
                </Field>
              </div>
            </Advanced>

            {(() => {
              const allRows = searchStatus?.engine_status || [];
              const chainSet = new Set(searchStatus?.engines || []);
              const inChainRows = allRows.filter((r) => chainSet.has(r.name));
              const otherRows = allRows.filter((r) => !chainSet.has(r.name));
              return (
                <>
                  <div className="mt-4 space-y-2">
                    {inChainRows.map((row) => renderSearchEngineRow(row))}
                  </div>
                  {otherRows.length ? (
                    <Advanced
                      title={tSearch("otherEnginesTitle", { count: otherRows.length })}
                      storageKey="nerya.settings.search.advanced.others"
                    >
                      <div className="space-y-2">
                        {otherRows.map((row) => renderSearchEngineRow(row))}
                      </div>
                    </Advanced>
                  ) : null}
                </>
              );
            })()}
          </Card>

          {(searchStatus?.engines || []).includes("searxng") ? (
            <Card
              title={tSearch("searxngTitle")}
              description={tSearch("searxngDesc")}
              actions={
                <Pill tone={searchStatus?.searxng?.container_running ? "ok" : "warn"}>
                  {searchStatus?.searxng?.docker_available
                    ? searchStatus?.searxng?.container_running
                      ? tSearch("searxngStateRunning")
                      : tSearch("searxngStateStopped")
                    : tSearch("searxngStateMissing")}
                </Pill>
              }
            >
              <div className="grid grid-cols-1 gap-3 md:grid-cols-3">
                <Field label={tSearch("hostPort")} hint={tSearch("hostPortHint")}>
                  <input
                    className="input-dark font-mono text-xs"
                    value={searxngHostPort}
                    onChange={(e) => setSearxngHostPort(e.target.value.replace(/[^0-9]/g, "").slice(0, 5))}
                    placeholder="8888"
                  />
                </Field>
                <Field label={tSearch("image")}>
                  <input
                    className="input-dark font-mono text-xs"
                    value={searxngImage}
                    onChange={(e) => setSearxngImage(e.target.value)}
                    placeholder="searxng/searxng:latest"
                  />
                </Field>
                <Row label={tSearch("rebuild")} desc={tSearch("rebuildDesc")}>
                  <SwitchControl
                    checked={searxngRebuild}
                    label={tSearch("rebuildSwitch")}
                    onCheckedChange={(v) => setSearxngRebuild(v)}
                  />
                </Row>
              </div>
              <div className="mt-3 space-y-1 text-[11px] text-ink-500">
                <div>
                  {tSearch("probe")}: <span className="font-mono">{searchStatus?.searxng?.probe?.ok ? "ok" : (searchStatus?.searxng?.probe?.error || "–")}</span>
                  {searchStatus?.searxng?.probe?.elapsed_ms != null
                    ? ` · ${searchStatus.searxng.probe.elapsed_ms}ms`
                    : ""}
                </div>
                <div>
                  {tSearch("baseUrl")}: <span className="font-mono">{searchStatus?.searxng?.base_url || "–"}</span>
                </div>
                <div>
                  {tSearch("config")}: <span className="font-mono">{searchStatus?.searxng?.config_dir || "–"}</span>
                </div>
              </div>
              <div className="mt-3 flex flex-wrap gap-2">
                <button
                  type="button"
                  className="btn btn-primary"
                  onClick={() => void deploySearxng()}
                  disabled={Boolean(searchBusy) || searchStatus?.searxng?.docker_available === false}
                >
                  <SparkIcon size={14} />
                  {searchBusy === "searxng-deploy"
                    ? tSearch("deploying")
                    : searchStatus?.searxng?.container_running
                      ? tSearch("redeploy")
                      : tSearch("deploy")}
                </button>
                <button
                  type="button"
                  className="btn btn-ghost"
                  onClick={() => void teardownSearxng({ remove: false })}
                  disabled={Boolean(searchBusy) || !searchStatus?.searxng?.container_running}
                >
                  {searchBusy === "searxng-stop" ? tSearch("stopping") : tSearch("stop")}
                </button>
                <button
                  type="button"
                  className="btn btn-ghost"
                  onClick={() => void teardownSearxng({ remove: true })}
                  disabled={Boolean(searchBusy) || searchStatus?.searxng?.deployed === false}
                >
                  {searchBusy === "searxng-teardown" ? tSearch("removing") : tSearch("remove")}
                </button>
                {searchStatus?.searxng?.docker_available === false ? (
                  <span className="self-center text-[11px] text-amber-300">
                    {tSearch("dockerMissingHint")}
                  </span>
                ) : null}
              </div>
            </Card>
          ) : null}

          <Advanced
            title={tSearch("probeTitle")}
            description={tSearch("probeDesc")}
            storageKey="nerya.settings.search.advanced.probe"
          >
            <div className="grid grid-cols-1 gap-3 md:grid-cols-[1fr_180px_auto]">
              <Field label={tSearch("query")}>
                <input
                  className="input-dark text-xs"
                  value={searchTestQuery}
                  onChange={(e) => setSearchTestQuery(e.target.value)}
                  placeholder={tSearch("queryPlaceholder")}
                />
              </Field>
              <Field label={tSearch("engineOverride")} hint={tSearch("engineOverrideHint")}>
                <input
                  className="input-dark font-mono text-xs"
                  value={searchTestEngine}
                  onChange={(e) => setSearchTestEngine(e.target.value)}
                  placeholder={tSearch("engineOverridePlaceholder")}
                />
              </Field>
              <div className="flex items-end">
                <button
                  type="button"
                  className="btn btn-primary"
                  onClick={() => void runSearchEngineTest()}
                  disabled={Boolean(searchBusy)}
                >
                  <SearchIcon size={14} />
                  {searchBusy === "test" ? tSearch("probing") : tSearch("probe")}
                </button>
              </div>
            </div>
            {searchTestResult ? (
              <div className="mt-3 rounded-md border border-brand-500/10 bg-ink-950/35 px-3 py-2 font-mono text-[11px] text-ink-300 whitespace-pre-wrap">
                {searchTestResult}
              </div>
            ) : null}
          </Advanced>
        </div>
      ) : null}

      {effectiveSettingsTab === "browsers" && (!inSectionMode || forceSection === "browsers") ? (
        <div
          id={settingsPanelId("browsers")}
          role="tabpanel"
          aria-labelledby={settingsTabId("browsers")}
          className="space-y-5"
        >
          <Card
            featured
            title={tBrowsers("cardTitle")}
            description={tBrowsers("cardDesc")}
            actions={
              <div className="flex flex-wrap items-center gap-2">
                {browsersStatus ? (
                  <Pill tone={browsersStatus.selected ? "ok" : "warn"}>
                    {browsersStatus.selected
                      ? tBrowsers("selectedPill", { name: browsersStatus.selected })
                      : tBrowsers("noEngineSelected")}
                  </Pill>
                ) : null}
              </div>
            }
          >
            <div className="mb-2 text-[11px] text-ink-500">
              {tBrowsers("platformInfo", {
                platform: browsersStatus?.platform || "?",
                dir: browsersStatus?.binaries_dir || "–",
              })}
            </div>
            <div className="space-y-3">
              {(() => {
                const engines = browsersStatus?.engines || [];
                const sorted = [...engines].sort((a, b) => {
                  const aSel = browsersStatus?.selected === a.name ? 0 : 1;
                  const bSel = browsersStatus?.selected === b.name ? 0 : 1;
                  if (aSel !== bSel) return aSel - bSel;
                  const aInst = a.installed ? 0 : 1;
                  const bInst = b.installed ? 0 : 1;
                  return aInst - bInst;
                });
                return sorted;
              })().map((row) => {
                const isSelected = browsersStatus?.selected === row.name;
                const cannotInstall = row.kind === "binary" && !row.platform_supported;
                const hasDetails = Boolean(
                  row.homepage || row.version || row.binary_path || row.checkout_path || row.service_url || row.module || row.notes,
                );
                return (
                  <div
                    key={row.name}
                    className={`rounded-xl border p-3 ${
                      isSelected
                        ? "border-brand-400/40 bg-brand-500/10"
                        : "border-brand-500/10 bg-ink-900/35"
                    }`}
                  >
                    <div className="flex flex-wrap items-start justify-between gap-2">
                      <div className="min-w-0">
                        <div className="flex flex-wrap items-center gap-2">
                          <span className="font-mono text-[13px] text-ink-100">{row.title}</span>
                          <Pill tone="brand">
                            {row.kind === "binary"
                              ? tBrowsers("kindBinary")
                              : row.kind === "node_service"
                                ? tBrowsers("kindService")
                                : tBrowsers("kindPython")}
                          </Pill>
                          <Pill tone={row.installed ? "ok" : "warn"}>
                            {row.installed ? tBrowsers("installed") : tBrowsers("notInstalled")}
                          </Pill>
                          {cannotInstall ? (
                            <Pill tone="warn">
                              {tBrowsers("noBinaryForPlatform", { platform: row.platform || "?" })}
                            </Pill>
                          ) : null}
                        </div>
                        <div className="mt-1 text-[11px] text-ink-400">{row.summary}</div>
                        {hasDetails ? (
                          <details className="mt-1.5 text-[11px]">
                            <summary className="cursor-pointer text-ink-500 hover:text-ink-300">
                              {tBrowsers("rowDetailsToggle")}
                            </summary>
                            <div className="mt-1.5 space-y-1 text-ink-500 font-medium">
                              {row.homepage ? (
                                <div>
                                  <a className="text-brand-300 hover:underline" href={row.homepage} target="_blank" rel="noreferrer">
                                    {row.homepage}
                                  </a>
                                </div>
                              ) : null}
                              {(row.version || row.binary_path || row.checkout_path || row.service_url || row.module) ? (
                                <div className="font-mono">
                                  {[
                                    row.version,
                                    row.binary_path,
                                    row.checkout_path,
                                    row.service_url,
                                    row.module ? tBrowsers("moduleSuffix", { module: row.module }) : "",
                                  ]
                                    .filter(Boolean)
                                    .join(" · ")}
                                </div>
                              ) : null}
                              {row.notes ? (
                                <div className="text-amber-200/80">{row.notes}</div>
                              ) : null}
                            </div>
                          </details>
                        ) : null}
                      </div>
                      <div className="flex flex-wrap justify-end gap-2">
                        <button
                          type="button"
                          className="btn btn-ghost"
                          onClick={() => void selectBrowser(row.name)}
                          disabled={Boolean(browsersBusy) || !row.installed || isSelected}
                        >
                          {browsersBusy === `select:${row.name}` ? tBrowsers("selecting") : tBrowsers("select")}
                        </button>
                        <button
                          type="button"
                          className="btn btn-ghost"
                          onClick={() => void testBrowserRow(row.name)}
                          disabled={Boolean(browsersBusy) || !row.installed}
                          title={
                            row.installed
                              ? tBrowsers("testTitleInstalled", {
                                  url: browserProbeUrl.trim() || "https://example.com",
                                  engine: row.name,
                                })
                              : tBrowsers("testTitleNotInstalled")
                          }
                        >
                          <SparkIcon size={12} />
                          {browsersBusy === `test:${row.name}` ? tBrowsers("testing") : tBrowsers("test")}
                        </button>
                        {row.installed ? (
                          <button
                            type="button"
                            className="btn btn-ghost"
                            onClick={() => void uninstallBrowser(row.name)}
                            disabled={Boolean(browsersBusy)}
                          >
                            {browsersBusy === `uninstall:${row.name}` ? tBrowsers("uninstalling") : tBrowsers("uninstall")}
                          </button>
                        ) : (
                          <button
                            type="button"
                            className="btn btn-primary"
                            onClick={() => void installBrowser(row.name)}
                            disabled={Boolean(browsersBusy) || cannotInstall}
                            title={
                              cannotInstall
                                ? tBrowsers("installTitleNoBinary", { platform: row.platform || "?" })
                                : row.kind === "binary"
                                  ? tBrowsers("installTitleBinary", { asset: row.asset || row.name })
                                  : row.kind === "node_service"
                                    ? tBrowsers("installTitleService")
                                    : tBrowsers("installTitlePython", { pkg: row.pip_package || "" })
                            }
                          >
                            {browsersBusy === `install:${row.name}` ? tBrowsers("installing") : tBrowsers("install")}
                          </button>
                        )}
                      </div>
                    </div>
                    {browserRowResult[row.name] ? (
                      <div
                        className={`mt-2 rounded-md border px-2 py-1 font-mono text-[11px] whitespace-pre-wrap ${
                          /^(error|fail|missing|❌)/i.test(browserRowResult[row.name] || "")
                            ? "border-rose-500/30 bg-rose-500/10 text-rose-200"
                            : "border-emerald-500/30 bg-emerald-500/10 text-emerald-200"
                        }`}
                      >
                        {browserRowResult[row.name]}
                      </div>
                    ) : null}
                  </div>
                );
              })}
              {(!browsersStatus?.engines || browsersStatus.engines.length === 0) ? (
                <div className="rounded-md border border-brand-500/10 bg-ink-950/30 p-3 text-[12px] text-ink-400">
                  {tBrowsers("registryNotLoaded")}
                </div>
              ) : null}
            </div>
            <div className="mt-3 flex flex-wrap gap-2">
              <button
                type="button"
                className="btn btn-ghost"
                onClick={() => void loadBrowsersStatus()}
                disabled={Boolean(browsersBusy)}
              >
                <RefreshIcon size={14} />
                {tCommon("refresh")}
              </button>
              {browsersStatus?.selected ? (
                <button
                  type="button"
                  className="btn btn-ghost"
                  onClick={() => void selectBrowser("")}
                  disabled={Boolean(browsersBusy)}
                >
                  {tBrowsers("clearSelection")}
                </button>
              ) : null}
            </div>
          </Card>

          <Advanced
            title={tBrowsers("probeTitle")}
            description={tBrowsers("probeDesc")}
            storageKey="nerya.settings.browsers.advanced.probe"
          >
            <div className="grid grid-cols-1 gap-3 md:grid-cols-[1fr_180px_auto]">
              <Field label={tBrowsers("url")}>
                <input
                  className="input-dark text-xs"
                  value={browserProbeUrl}
                  onChange={(e) => setBrowserProbeUrl(e.target.value)}
                  placeholder="https://example.com"
                />
              </Field>
              <Field label={tBrowsers("engineOverride")} hint={tBrowsers("engineOverrideHint")}>
                <input
                  className="input-dark font-mono text-xs"
                  value={browserProbeName}
                  onChange={(e) => setBrowserProbeName(e.target.value)}
                  placeholder={tBrowsers("engineOverridePlaceholder")}
                />
              </Field>
              <div className="flex items-end">
                <button
                  type="button"
                  className="btn btn-primary"
                  onClick={() => void probeBrowser()}
                  disabled={Boolean(browsersBusy)}
                >
                  <SearchIcon size={14} />
                  {browsersBusy === "probe" ? tBrowsers("probing") : tBrowsers("probe")}
                </button>
              </div>
            </div>
            {browserProbeResult ? (
              <div className="mt-3 max-h-72 overflow-auto rounded-md border border-brand-500/10 bg-ink-950/35 px-3 py-2 font-mono text-[11px] text-ink-300 whitespace-pre-wrap">
                {browserProbeResult}
              </div>
            ) : null}
          </Advanced>
        </div>
      ) : null}

      {/* Memory panel renders ONLY when this component is mounted by
          /memory/page.tsx (forceSection="memory"). The Memory tab is
          no longer reachable from the regular /settings page; the
          standalone /memory page owns this UI now. */}
      {effectiveSettingsTab === "memory" && forceSection === "memory" ? (
        <div
          id={settingsPanelId("memory")}
          role="tabpanel"
          aria-labelledby={settingsTabId("memory")}
          className="max-w-5xl space-y-5"
        >
          <Card title={tMemory("backendTitle")}>
            <div
              className="flex rounded-lg border border-brand-500/15 bg-ink-950/30 p-1"
              role="radiogroup"
              aria-label={tMemory("backendTitle")}
            >
              {(["builtin", "memsearch", "agentmemory"] as const).map((choice) => {
                const active = memoryBackendChoice === choice;
                const busy = memoryBusy === `backend:${choice}`;
                return (
                  <button
                    key={choice}
                    type="button"
                    role="radio"
                    aria-checked={active}
                    disabled={Boolean(memoryBusy)}
                    className={[
                      "flex-1 rounded-md px-3 py-2 text-center text-[12px] transition-colors",
                      active
                        ? "bg-brand-500/20 text-white"
                        : "text-ink-400 hover:bg-white/[0.04] hover:text-ink-200",
                    ].join(" ")}
                    onClick={() => void setMemoryBackendChoice(choice)}
                  >
                    <span className="font-mono">{tMemory(`backend_${choice}`)}</span>
                    {busy ? (
                      <span className="ml-2 text-[10px] text-ink-500">
                        {tMemory("backendSaving")}
                      </span>
                    ) : null}
                  </button>
                );
              })}
            </div>
            <div className="mt-2 text-[11px] text-ink-500">
              {tMemory(`backend_${memoryBackendChoice}_hint`)}
            </div>
          </Card>

          {/* Inline summary for the currently selected backend. Renders
              a small status card with 1-click CTAs that switch the
              sub-tab to the matching detail surface, so the operator
              never has to hunt for where a backend is configured. The
              full configuration UI lives in the targeted sub-tab. */}
          <Card title={tMemory("backendSummaryTitle")}>
            {memoryBackendChoice === "builtin" ? (
              <div className="space-y-3">
                <div>
                  <div className="text-[13px] font-medium text-ink-100">
                    {tMemory("backendSummary_builtin_title")}
                  </div>
                  <div className="mt-1 text-[11px] text-ink-500">
                    {tMemory("backendSummary_builtin_desc")}
                  </div>
                </div>
                <Row
                  label={tMemory("backendSummary_builtin_paths")}
                  desc={(memoryStatus?.paths || []).join(", ") || "memory, strategies"}
                >
                  <Pill tone="ok">{tMemory("backendActive")}</Pill>
                </Row>
                <div className="flex flex-wrap gap-2">
                  <button
                    type="button"
                    className="btn btn-ghost"
                    onClick={() => setActiveMemorySubTab("notebook")}
                  >
                    {tMemory("backendSummary_builtin_open_notebook")}
                  </button>
                  <button
                    type="button"
                    className="btn btn-ghost"
                    onClick={() => setActiveMemorySubTab("rules")}
                  >
                    {tMemory("backendSummary_builtin_open_rules")}
                  </button>
                  <button
                    type="button"
                    className="btn btn-ghost"
                    onClick={() => setActiveMemorySubTab("activity")}
                  >
                    {tMemory("backendSummary_builtin_open_activity")}
                  </button>
                </div>
              </div>
            ) : memoryBackendChoice === "memsearch" ? (
              <div className="space-y-3">
                <div>
                  <div className="text-[13px] font-medium text-ink-100">
                    {tMemory("backendSummary_memsearch_title")}
                  </div>
                  <div className="mt-1 text-[11px] text-ink-500">
                    {tMemory("backendSummary_memsearch_desc")}
                  </div>
                </div>
                <Row
                  label={tMemory("backendSummary_memsearch_dep")}
                  desc={memoryStatus?.install_package || "–"}
                >
                  <Pill tone={memoryStatus?.dependency_available ? "ok" : "warn"}>
                    {memoryStatus?.dependency_available
                      ? tMemory("dependencyAvailable")
                      : tMemory("dependencyMissing")}
                  </Pill>
                </Row>
                <Row
                  label={tMemory("backendSummary_memsearch_embedding")}
                  desc={memoryStatus?.embedding?.base_url || ""}
                >
                  <span className="font-mono text-[12px] text-ink-200">
                    {memoryStatus?.embedding?.model || "–"}
                  </span>
                </Row>
                <Row
                  label={tMemory("backendSummary_memsearch_milvus")}
                  desc={memoryStatus?.milvus?.collection || ""}
                >
                  <span className="font-mono text-[12px] text-ink-200">
                    {memoryStatus?.milvus?.uri || "–"}
                  </span>
                </Row>
                <Row label={tMemory("backendSummary_memsearch_watcher")}>
                  <Pill tone={memoryStatus?.watcher_running ? "ok" : "warn"}>
                    {memoryStatus?.watcher_running
                      ? tMemory("backendSummary_memsearch_watcher_running")
                      : tMemory("backendSummary_memsearch_watcher_idle")}
                  </Pill>
                </Row>
                <div className="flex flex-wrap gap-2">
                  {/* Install + Test recall live here; the full
                      embedding/Milvus form is rendered directly below
                      the Selected backend settings card when memsearch
                      is the active backend (was a separate sub-tab in
                      previous versions). */}
                  <button
                    type="button"
                    className="btn btn-ghost"
                    disabled={Boolean(memoryBusy)}
                    onClick={() => void runBackendInstall("memsearch")}
                  >
                    {memoryBusy === "memsearch:install:run"
                      ? tMemory("installRunning")
                      : tMemory("installDependency")}
                  </button>
                  <button
                    type="button"
                    className="btn btn-ghost"
                    disabled={Boolean(memoryBusy)}
                    onClick={() => void runBackendTest()}
                  >
                    {memoryBusy === "memory:test"
                      ? tMemory("testRunning")
                      : tMemory("testRecall")}
                  </button>
                </div>
              </div>
            ) : (
              <div className="space-y-3">
                <div>
                  <div className="text-[13px] font-medium text-ink-100">
                    {tMemory("backendSummary_agentmemory_title")}
                  </div>
                  <div className="mt-1 text-[11px] text-ink-500">
                    {tMemory("backendSummary_agentmemory_desc")}
                  </div>
                </div>
                <Row label={tMemory("backendSummary_agentmemory_base_url")}>
                  <span className="font-mono text-[12px] text-ink-200">
                    {agentmemoryDraft.base_url || "–"}
                  </span>
                </Row>
                <Row label={tMemory("backendSummary_agentmemory_project")}>
                  <span className="font-mono text-[12px] text-ink-200">
                    {agentmemoryDraft.project || "–"}
                  </span>
                </Row>
                <Row label={tMemory("backendSummary_agentmemory_secret")}>
                  <Pill tone={agentmemoryDraft.secret_ref ? "ok" : "warn"}>
                    {agentmemoryDraft.secret_ref
                      ? tMemory("backendSummary_agentmemory_secret_present")
                      : tMemory("backendSummary_agentmemory_secret_missing")}
                  </Pill>
                </Row>
                <div className="flex flex-wrap gap-2">
                  <button
                    type="button"
                    className="btn btn-primary"
                    onClick={() => setActiveMemorySubTab("providers")}
                  >
                    {tMemory("backendSummary_agentmemory_open")}
                  </button>
                  <button
                    type="button"
                    className="btn btn-ghost"
                    disabled={Boolean(memoryBusy)}
                    onClick={() => void runBackendInstall("agentmemory")}
                  >
                    {memoryBusy === "agentmemory:install:run"
                      ? tMemory("installRunning")
                      : tMemory("installDependency")}
                  </button>
                  <button
                    type="button"
                    className="btn btn-ghost"
                    disabled={Boolean(memoryBusy)}
                    onClick={() => void runBackendTest()}
                  >
                    {memoryBusy === "memory:test"
                      ? tMemory("testRunning")
                      : tMemory("testRecall")}
                  </button>
                </div>
              </div>
            )}
            {/* Inline test query input — lets the operator drive the
                "Test recall" button with a custom query without leaving
                the card. Empty query falls back to "memory test". */}
            {memoryBackendChoice !== "builtin" && (
              <div className="mt-3 flex flex-wrap items-center gap-2">
                <input
                  type="text"
                  className="input-dark text-xs flex-1 min-w-[200px]"
                  value={backendTestQuery}
                  onChange={(e) => setBackendTestQuery(e.target.value)}
                  placeholder={tMemory("testQueryPlaceholder")}
                />
              </div>
            )}
            {/* Install result block — shows command, exit code, and the
                full stdout/stderr tail returned by the backend so the
                operator can debug a failed install without opening a
                separate terminal. */}
            {backendInstallResult && (
              <div className="mt-3 rounded-md border border-line-700 bg-card-800 p-3 text-[12px]">
                <div className="flex flex-wrap items-center gap-2">
                  <Pill tone={backendInstallResult.ok ? "ok" : "danger"}>
                    {backendInstallResult.ok
                      ? tMemory("installSuccess")
                      : tMemory("installFailed")}
                  </Pill>
                  <span className="text-ink-400">
                    {backendInstallResult.backend}
                  </span>
                  {typeof backendInstallResult.returncode === "number" && (
                    <span className="font-mono text-ink-500">
                      exit={backendInstallResult.returncode}
                    </span>
                  )}
                  {backendInstallResult.dependency_available !== undefined && (
                    <Pill tone={backendInstallResult.dependency_available ? "ok" : "warn"}>
                      {backendInstallResult.dependency_available
                        ? tMemory("dependencyAvailable")
                        : tMemory("dependencyMissing")}
                    </Pill>
                  )}
                </div>
                {backendInstallResult.cmd && backendInstallResult.cmd.length > 0 && (
                  <pre className="mt-2 max-h-24 overflow-auto whitespace-pre-wrap break-all font-mono text-[11px] text-ink-300">
                    {backendInstallResult.cmd.join(" ")}
                  </pre>
                )}
                {backendInstallResult.stderr_tail && (
                  <details className="mt-2">
                    <summary className="cursor-pointer text-[11px] text-ink-400">
                      {tMemory("installStderr")}
                    </summary>
                    <pre className="mt-1 max-h-40 overflow-auto whitespace-pre-wrap break-all font-mono text-[11px] text-rose-300">
                      {backendInstallResult.stderr_tail}
                    </pre>
                  </details>
                )}
                {backendInstallResult.stdout_tail && (
                  <details className="mt-2">
                    <summary className="cursor-pointer text-[11px] text-ink-400">
                      {tMemory("installStdout")}
                    </summary>
                    <pre className="mt-1 max-h-40 overflow-auto whitespace-pre-wrap break-all font-mono text-[11px] text-ink-300">
                      {backendInstallResult.stdout_tail}
                    </pre>
                  </details>
                )}
                {backendInstallResult.note && (
                  <p className="mt-2 text-[11px] text-ink-400">
                    {backendInstallResult.note}
                  </p>
                )}
                {backendInstallResult.detail && !backendInstallResult.stderr_tail && (
                  <p className="mt-2 text-[11px] text-rose-300">
                    {backendInstallResult.detail}
                  </p>
                )}
              </div>
            )}
            {/* Test recall result block — renders one row per backend so
                the operator can compare reach (built-in entries,
                memsearch matches, agentmemory health) side-by-side. */}
            {backendTestResult && (
              <div className="mt-3 rounded-md border border-line-700 bg-card-800 p-3 text-[12px]">
                <div className="mb-2 flex flex-wrap items-center gap-2">
                  <span className="text-ink-300">{tMemory("testResultsTitle")}</span>
                  <span className="font-mono text-ink-500">
                    {tMemory("testResultsQuery")}: {backendTestResult.query}
                  </span>
                </div>
                <div className="space-y-2">
                  {backendTestResult.backends.map((b) => (
                    <div
                      key={b.backend}
                      className="flex flex-wrap items-center gap-2 rounded border border-line-800 bg-card-900 p-2"
                    >
                      <Pill tone={b.ok ? "ok" : b.enabled === false ? "neutral" : "warn"}>
                        {b.backend}
                      </Pill>
                      {b.backend === "builtin" && (
                        <span className="text-ink-300">
                          {tMemory("testBuiltin", {
                            agent: String(b.agent_entries ?? 0),
                            operator: String(b.operator_entries ?? 0),
                          })}
                        </span>
                      )}
                      {b.backend === "memsearch" && (
                        <span className="text-ink-300">
                          {b.ok
                            ? tMemory("testMatches", { n: String(b.matches ?? 0) })
                            : b.error || tMemory("testNotConfigured")}
                        </span>
                      )}
                      {b.backend === "agentmemory" && (
                        <span className="text-ink-300">
                          {b.enabled === false
                            ? tMemory("testNotEnabled")
                            : b.ok
                            ? tMemory("testMatches", { n: String(b.matches ?? 0) })
                            : b.last_error || b.error || tMemory("testNotReachable")}
                        </span>
                      )}
                    </div>
                  ))}
                </div>
              </div>
            )}
          </Card>

          {/* Memory sub-tab nav. Plain inline pills (not the larger
              ``SettingsModuleTabs`` cards used at the top of the page)
              because we're already inside a panel. */}
          <nav aria-label={tMemory("subtabsAriaLabel")} className="flex flex-wrap gap-1.5">
            {MEMORY_SUBTABS.map((key) => {
              const selected = key === activeMemorySubTab;
              // "vector" used to live in this map but the memsearch
              // panel is now rendered above the sub-tab nav (only when
              // memsearch is the active backend) — no per-backend
              // sub-tab needed anymore.
              const labelKey = (
                key === "notebook" ? "subtabNotebook" :
                key === "activity" ? "subtabActivity" :
                key === "rules" ? "subtabRules" :
                key === "evidence" ? "subtabEvidence" :
                key === "profile" ? "subtabProfile" :
                "subtabProviders"
              );
              return (
                <button
                  key={key}
                  type="button"
                  role="tab"
                  aria-selected={selected}
                  className={[
                    "rounded-full border px-3 py-1.5 text-[12px] transition-colors",
                    selected
                      ? "border-brand-300/60 bg-brand-500/15 text-white"
                      : "border-brand-500/15 bg-ink-950/30 text-ink-300 hover:border-brand-500/30 hover:text-ink-100",
                  ].join(" ")}
                  onClick={() => setActiveMemorySubTab(key)}
                >
                  {tMemory(labelKey)}
                </button>
              );
            })}
          </nav>

          {/* Full memsearch configuration — embedding model, Milvus,
              install/rebuild/start, search. Previously lived behind a
              dedicated "memsearch" sub-tab; now rendered inline
              directly under the Selected backend settings card, and
              only when memsearch is the active backend. Builtin /
              agentmemory deployments don't need this surface so it
              stays hidden, keeping the page focused. */}
          {memoryBackendChoice === "memsearch" ? (
          <Card title={tMemory("title")} description={tMemory("description")}>
            <Row
              label={tMemory("enableLabel")}
              desc={memoryStatus?.dependency_available ? tMemory("dependencyAvailable") : tMemory("dependencyMissing")}
            >
              <SwitchControl
                checked={Boolean(memoryStatus?.enabled)}
                disabled={Boolean(memoryBusy)}
                label={tMemory("enableLabel")}
                onCheckedChange={(v) => {
                  void setMemoryBackendChoice(v ? "memsearch" : "builtin");
                }}
              />
            </Row>
            <Row label={tMemory("backendLabel")} desc={memoryStatus?.paths?.join(", ") || "memory, strategies"}>
              <span className="font-mono text-xs text-ink-200">
                {memoryStatus?.backend || "memsearch"}
              </span>
            </Row>
            <div className="mt-3 space-y-3 rounded-lg border border-brand-500/15 bg-ink-950/30 p-3">
              {/* Make the independence obvious — embedding/vector model
                  is configured separately from the chat tiers, even
                  though the API key store is shared. */}
              <div className="flex flex-wrap items-start justify-between gap-2 border-b border-brand-500/10 pb-2">
                <div>
                  <div className="text-[13px] font-medium text-ink-100">
                    {tMemory("embeddingTitle")}
                  </div>
                  <div className="mt-0.5 text-[11px] text-ink-500">
                    {tMemory("embeddingIndependent")}
                  </div>
                </div>
                <Pill tone={memoryStatus?.embedding?.has_key ? "ok" : "warn"}>
                  {memoryStatus?.embedding?.has_key
                    ? tMemory("embeddingKeyResolved")
                    : tMemory("embeddingKeyMissing")}
                </Pill>
              </div>
              <div className="grid grid-cols-1 md:grid-cols-2 gap-3">
                <Field label={tMemory("embeddingProviderLabel")} hint={tMemory("embeddingProviderHint")}>
                  <PortalSelect
                    value={embProvider}
                    onChange={(next) => {
                      setEmbProvider(next);
                      const entry = catalogById.get(next);
                      if (entry && !embBaseUrl) setEmbBaseUrl(entry.base_url);
                    }}
                    options={Array.from(
                      new Set([
                        "openai",
                        "google",
                        "voyage",
                        "ollama",
                        "local",
                        ...providerCatalog
                          .filter((e) => e.api_mode === "chat_completions")
                          .map((e) => e.id),
                        embProvider,
                      ]),
                    )
                      .filter(Boolean)
                      .sort()
                      .map((id) => ({
                        value: id,
                        label:
                          id === "local"
                            ? `${id} (sentence-transformers)`
                            : id,
                      }))}
                    size="sm"
                    ariaLabel={tMemory("embeddingProviderLabel")}
                    className="font-mono"
                  />
                </Field>
                <Field label={tMemory("embeddingModelLabel")} hint={tMemory("embeddingModelHint")}>
                  <input
                    className="input-dark text-xs font-mono"
                    value={embModel}
                    onChange={(e) => setEmbModel(e.target.value)}
                    placeholder="text-embedding-3-small"
                  />
                </Field>
                <Field
                  label={tMemory("embeddingBaseUrlLabel")}
                  hint={
                    embProvider === "openai"
                      ? tMemory("embeddingBaseUrlOpenAIHint")
                      : tMemory("embeddingBaseUrlGenericHint")
                  }
                >
                  <input
                    className="input-dark text-xs font-mono"
                    value={embBaseUrl}
                    onChange={(e) => setEmbBaseUrl(e.target.value)}
                    placeholder={catalogById.get(embProvider)?.base_url || "https://api.openai.com/v1"}
                  />
                </Field>
                <Field label={tMemory("embeddingKeyRefLabel")} hint={tMemory("embeddingKeyRefHint")}>
                  <div className="flex flex-col gap-2">
                    <Select
                      value={embKeyRef}
                      onChange={setEmbKeyRef}
                      options={[
                        { value: "", label: tMemory("embeddingKeyRefNone") },
                        ...providerProfiles
                          .filter((p) => (p.provider_key_ref || "").startsWith("vault://"))
                          .map((p) => ({
                            value: String(p.provider_key_ref || ""),
                            label: `${p.provider} (${String(p.provider_key_ref || "").replace("vault://", "")})`,
                          })),
                      ]}
                    />
                    {/* Direct paste: when the operator types a key here,
                        we send it to the backend as ``api_key_plain``;
                        the server stashes it in the SecretVault and
                        rewrites ``api_key_ref`` automatically. The
                        field is cleared on successful save. */}
                    <input
                      className="input-dark font-mono"
                      type="password"
                      autoComplete="off"
                      value={embKeyPlain}
                      onChange={(e) => setEmbKeyPlain(e.target.value)}
                      placeholder={tMemory("embeddingKeyPlainPlaceholder")}
                    />
                    <div className="text-[11px] text-ink-500">
                      {tMemory("embeddingKeyPlainHint")}
                    </div>
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
                          // When the operator pasted a plaintext key,
                          // ship it as a separate field. The server
                          // stores it in the SecretVault and rewrites
                          // ``api_key_ref`` to ``vault://...`` so the
                          // secret is never persisted in plaintext.
                          ...(embKeyPlain.trim()
                            ? { api_key_plain: embKeyPlain.trim() }
                            : {}),
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
                        setEmbKeyPlain("");
                        // Reflect the freshly-minted vault ref in the
                        // dropdown so the operator sees what was saved.
                        if (res.embedding?.api_key_ref) {
                          setEmbKeyRef(res.embedding.api_key_ref);
                        }
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
          ) : null}

          {activeMemorySubTab === "notebook" ? (
          /* ---- Curated agent / operator notebook ---------------- */
          <Card
            title={tMemory("notebookTitle")}
            description={tMemory("notebookDescription")}
            actions={
              notebookMessage ? <Pill tone="brand">{notebookMessage}</Pill> : null
            }
          >
            <div className="grid grid-cols-1 gap-3 lg:grid-cols-2">
              {(["agent", "operator"] as const).map((target) => {
                const snap = target === "agent" ? notebookAgent : notebookOperator;
                const draftKey = target;
                const used = snap?.used_chars ?? 0;
                const limit = snap?.char_limit ?? (target === "agent" ? 2200 : 1375);
                const pct = limit > 0 ? Math.min(100, Math.round((used / limit) * 100)) : 0;
                return (
                  <div key={target} className="rounded-lg border border-[color:var(--line)] p-3">
                    <div className="mb-2 flex items-start justify-between gap-2">
                      <div>
                        <div className="text-[13px] font-medium text-ink-100">
                          {target === "agent" ? tMemory("notebookAgent") : tMemory("notebookOperator")}
                        </div>
                        <div className="mt-0.5 text-[11px] text-ink-500">
                          {tMemory("notebookFrozenHint")}
                        </div>
                      </div>
                      <Pill tone={pct > 90 ? "warn" : "brand"}>
                        {`${pct}% (${used}/${limit})`}
                      </Pill>
                    </div>
                    <div className="space-y-2">
                      {(snap?.entries || []).length === 0 ? (
                        <div className="rounded-md border border-dashed border-brand-500/15 px-3 py-2 text-[11px] text-ink-500">
                          {tMemory("notebookEmpty")}
                        </div>
                      ) : (
                        (snap?.entries || []).map((entry, idx) => (
                          <div
                            key={`${target}-${idx}`}
                            className="group flex items-start justify-between gap-2 rounded-md border border-brand-500/10 bg-ink-900/40 px-3 py-2 text-[12px] text-ink-100"
                          >
                            <div className="whitespace-pre-wrap">{entry}</div>
                            <button
                              type="button"
                              className="btn btn-ghost shrink-0 opacity-60 group-hover:opacity-100"
                              disabled={notebookBusy === `${target}.remove`}
                              onClick={() =>
                                void notebookMutate(target, "remove", { old_text: entry })
                              }
                            >
                              {tCommon("delete")}
                            </button>
                          </div>
                        ))
                      )}
                    </div>
                    <div className="mt-3 flex flex-col gap-2">
                      <textarea
                        className="input-dark text-xs"
                        rows={3}
                        value={notebookDraft[draftKey]}
                        placeholder={tMemory("notebookPlaceholder")}
                        onChange={(e) =>
                          setNotebookDraft((prev) => ({ ...prev, [draftKey]: e.target.value }))
                        }
                      />
                      <div className="flex justify-end">
                        <button
                          type="button"
                          className="btn btn-primary"
                          disabled={
                            notebookBusy === `${target}.add` ||
                            !(notebookDraft[draftKey] || "").trim()
                          }
                          onClick={() =>
                            void notebookMutate(target, "add", {
                              content: notebookDraft[draftKey],
                            })
                          }
                        >
                          <CheckIcon size={14} />
                          {tMemory("notebookAdd")}
                        </button>
                      </div>
                    </div>
                  </div>
                );
              })}
            </div>
          </Card>
          ) : null}

          {activeMemorySubTab === "activity" ? (
          /* ---- Activity stream (writes + searches) -------------- */
          <Card
            title={tMemory("activityTitle")}
            description={tMemory("activityDescription")}
            actions={
              memoryActivityStats ? (
                <span className="text-[11px] text-ink-500">
                  {tMemory("activityStats", {
                    writes: memoryActivityStats.write_ok,
                    skipped: memoryActivityStats.write_skipped,
                    searches: memoryActivityStats.search,
                  })}
                </span>
              ) : null
            }
          >
            <div className="mb-2 flex flex-wrap items-center gap-2">
              <Field label={tMemory("activityFilterLabel")}>
                <PortalSelect<typeof memoryActivityFilter>
                  value={memoryActivityFilter}
                  onChange={(value) => setMemoryActivityFilter(value)}
                  options={[
                    { value: "", label: tMemory("activityFilterAll") },
                    {
                      value: "write_ok",
                      label: tMemory("activityFilterWriteOk"),
                    },
                    {
                      value: "write_skipped",
                      label: tMemory("activityFilterWriteSkipped"),
                    },
                    {
                      value: "search",
                      label: tMemory("activityFilterSearch"),
                    },
                  ]}
                  size="sm"
                  ariaLabel={tMemory("activityFilterLabel")}
                />
              </Field>
              <button
                type="button"
                className="btn btn-ghost"
                onClick={() => void loadMemoryActivity()}
              >
                <RefreshIcon size={14} />
                {tCommon("refresh")}
              </button>
            </div>
            <div className="embedded-list-scroll max-h-96 rounded-lg border border-brand-500/10 bg-ink-950/35">
              {memoryActivityEvents.length === 0 ? (
                <div className="px-3 py-4 text-center text-[11px] text-ink-500">
                  {tMemory("activityEmpty")}
                </div>
              ) : (
                memoryActivityEvents.map((ev, idx) => {
                  const tone =
                    ev.kind === "write_ok" ? "ok" :
                    ev.kind === "write_skipped" ? "warn" : "brand";
                  return (
                    <div
                      key={`${ev.ts}-${idx}`}
                      className="border-b border-brand-500/10 px-3 py-2 text-[11px] last:border-b-0"
                    >
                      <div className="flex flex-wrap items-center justify-between gap-2">
                        <Pill tone={tone}>{ev.kind}</Pill>
                        <span className="font-mono text-ink-500">{ev.ts}</span>
                      </div>
                      <div className="mt-1 flex flex-wrap items-center gap-2 font-mono text-ink-300">
                        {ev.category ? <span className="text-ink-100">{ev.category}</span> : null}
                        {ev.skip_reason ? (
                          <span className="text-amber-300">skip:{ev.skip_reason}</span>
                        ) : null}
                        {ev.source ? <span>· {ev.source}</span> : null}
                        {typeof ev.result_count === "number" ? (
                          <span>· {ev.result_count} hits</span>
                        ) : null}
                        {typeof ev.latency_ms === "number" ? (
                          <span>· {ev.latency_ms}ms</span>
                        ) : null}
                      </div>
                      {ev.title ? (
                        <div className="mt-1 text-ink-100">{ev.title}</div>
                      ) : null}
                      {ev.preview ? (
                        <div className="mt-1 line-clamp-2 text-ink-200">{ev.preview}</div>
                      ) : null}
                      {ev.query ? (
                        <div className="mt-1 italic text-ink-200">"{ev.query}"</div>
                      ) : null}
                    </div>
                  );
                })
              )}
            </div>
          </Card>
          ) : null}

          {activeMemorySubTab === "rules" ? (
          /* ---- Write rules editor (per-category enable / retention) */
          writeRuleCategories.length ? (
            <Card
              title={tMemory("rulesTitle")}
              description={tMemory("rulesDescription")}
              actions={
                <button
                  type="button"
                  className="btn btn-primary"
                  disabled={writeRuleBusy}
                  onClick={async () => {
                    setWriteRuleBusy(true);
                    setError(null);
                    try {
                      const res = await clientApi.memoryWriteRulesSet(writeRules);
                      if (!res.ok && res.error) throw new Error(res.error);
                      setWriteRules(res.rules || writeRules);
                      setInfo(tMemory("rulesSaved"));
                    } catch (e) {
                      setError(e instanceof Error ? e.message : String(e));
                    } finally {
                      setWriteRuleBusy(false);
                    }
                  }}
                >
                  <CheckIcon size={14} />
                  {writeRuleBusy ? tCommon("saving") : tCommon("save")}
                </button>
              }
            >
              <div className="space-y-2">
                {writeRuleCategories.map((cat) => {
                  const rule = writeRules[cat.id];
                  if (!rule) return null;
                  return (
                    <div
                      key={cat.id}
                      className="rounded-lg border border-brand-500/10 bg-ink-950/40 p-3"
                    >
                      <div className="flex flex-wrap items-start justify-between gap-2">
                        <div>
                          <div className="font-mono text-[12px] text-ink-100">{cat.name}</div>
                          <div className="mt-0.5 text-[11px] text-ink-500">{cat.description}</div>
                        </div>
                        <SwitchControl
                          checked={rule.enabled}
                          onCheckedChange={(v) =>
                            setWriteRules((prev) => ({
                              ...prev,
                              [cat.id]: { ...prev[cat.id], enabled: v },
                            }))
                          }
                          label={tMemory("rulesEnabled")}
                        />
                      </div>
                      <div className="mt-3 grid grid-cols-1 gap-3 md:grid-cols-3">
                        <Field label={tMemory("rulesRetention")}>
                          <input
                            type="number"
                            min={0}
                            className="input-dark text-xs font-mono"
                            value={rule.retention_days}
                            onChange={(e) =>
                              setWriteRules((prev) => ({
                                ...prev,
                                [cat.id]: {
                                  ...prev[cat.id],
                                  retention_days: Number(e.target.value || 0),
                                },
                              }))
                            }
                          />
                        </Field>
                        <Field label={tMemory("rulesMaxEntries")}>
                          <input
                            type="number"
                            min={0}
                            className="input-dark text-xs font-mono"
                            value={rule.max_entries}
                            onChange={(e) =>
                              setWriteRules((prev) => ({
                                ...prev,
                                [cat.id]: {
                                  ...prev[cat.id],
                                  max_entries: Number(e.target.value || 0),
                                },
                              }))
                            }
                          />
                        </Field>
                        <Field label={tMemory("rulesDedupe")}>
                          <PortalSelect
                            value={rule.dedupe}
                            onChange={(value) =>
                              setWriteRules((prev) => ({
                                ...prev,
                                [cat.id]: {
                                  ...prev[cat.id],
                                  dedupe: value,
                                },
                              }))
                            }
                            options={writeRuleDedupes.map((s) => ({
                              value: s,
                              label: s,
                            }))}
                            size="sm"
                            ariaLabel={tMemory("rulesDedupe")}
                            className="font-mono"
                          />
                        </Field>
                      </div>
                    </div>
                  );
                })}
              </div>
            </Card>
          ) : null
          ) : null}

          {activeMemorySubTab === "providers" ? (
          /* ---- Memory provider directory (builtin + external) ----- */
          <Card
            title={tMemory("providersTitle")}
            description={tMemory("providersDescription")}
            actions={
              <button
                type="button"
                className="btn btn-ghost"
                onClick={() => void loadMemoryProviders()}
              >
                <RefreshIcon size={14} />
                {tCommon("refresh")}
              </button>
            }
          >
            <div className="space-y-3">
              {memoryProvidersData?.builtin ? (
                <div className="rounded-lg border border-emerald-400/30 bg-emerald-500/5 p-3">
                  <div className="flex flex-wrap items-start justify-between gap-2">
                    <div>
                      <div className="font-mono text-[12px] text-ink-100">
                        {memoryProvidersData.builtin.name}
                      </div>
                      <div className="mt-0.5 text-[11px] text-ink-500">
                        {memoryProvidersData.builtin.description}
                      </div>
                    </div>
                    <div className="flex items-center gap-1.5">
                      <Pill tone="ok">{tMemory("providersBuiltinAlwaysOn")}</Pill>
                      <Pill tone={memoryProvidersData.builtin.initialised ? "ok" : "warn"}>
                        {memoryProvidersData.builtin.initialised
                          ? tMemory("providersInitialised")
                          : tMemory("providersInitFailed")}
                      </Pill>
                    </div>
                  </div>
                  {memoryProvidersData.builtin.last_error ? (
                    <div className="mt-2 text-[11px] text-amber-300">
                      {memoryProvidersData.builtin.last_error}
                    </div>
                  ) : null}
                  <div className="mt-2 text-[11px] text-ink-500">
                    {memoryProvidersData.builtin.cost_hint}
                  </div>
                </div>
              ) : (
                <div className="rounded-lg border border-dashed border-brand-500/15 px-3 py-4 text-[11px] text-ink-500">
                  {tMemory("providersBuiltinMissing")}
                </div>
              )}

              <div className="rounded-lg border border-brand-500/15 bg-ink-950/40 p-3">
                <div className="flex flex-wrap items-start justify-between gap-2">
                  <div>
                    <div className="font-mono text-[12px] text-ink-100">
                      {tMemory("providersExternalTitle")}
                    </div>
                    <div className="mt-0.5 text-[11px] text-ink-500">
                      {tMemory("providersExternalHint")}
                    </div>
                  </div>
                  {memoryProvidersData?.external ? (
                    <Pill tone="brand">
                      {tMemory("providersActive", {
                        name: memoryProvidersData.external.name,
                      })}
                    </Pill>
                  ) : (
                    <Pill tone="warn">{tMemory("providersExternalNone")}</Pill>
                  )}
                </div>
                <div className="mt-3 rounded-md border border-brand-500/10 bg-ink-900/25 p-3">
                  <div className="flex flex-wrap items-start justify-between gap-2">
                    <div>
                      <div className="font-mono text-[11px] text-ink-100">
                        {tMemory("agentmemoryTitle")}
                      </div>
                      <div className="mt-0.5 text-[11px] text-ink-500">
                        {tMemory("agentmemoryHint")}
                      </div>
                    </div>
                    <Pill tone={memoryExternalConfig?.enabled ? "ok" : "warn"}>
                      {memoryExternalConfig?.enabled
                        ? tMemory("agentmemoryConfigured")
                        : tMemory("agentmemoryNotConfigured")}
                    </Pill>
                  </div>
                  <div className="mt-3 grid grid-cols-1 gap-3 lg:grid-cols-2">
                    <Field label={tMemory("agentmemoryBaseUrl")}>
                      <input
                        className="input"
                        value={agentmemoryDraft.base_url}
                        onChange={(e) =>
                          setAgentmemoryDraft((prev) => ({
                            ...prev,
                            base_url: e.target.value,
                          }))
                        }
                      />
                    </Field>
                    <Field label={tMemory("agentmemoryProject")}>
                      <input
                        className="input"
                        value={agentmemoryDraft.project}
                        onChange={(e) =>
                          setAgentmemoryDraft((prev) => ({
                            ...prev,
                            project: e.target.value,
                          }))
                        }
                      />
                    </Field>
                    <Field label={tMemory("agentmemorySecretRef")} hint={tMemory("agentmemorySecretHint")}>
                      <input
                        className="input"
                        value={agentmemoryDraft.secret_ref}
                        onChange={(e) =>
                          setAgentmemoryDraft((prev) => ({
                            ...prev,
                            secret_ref: e.target.value,
                          }))
                        }
                        placeholder="vault://agentmemory_secret"
                      />
                    </Field>
                    <Field label={tMemory("agentmemorySecretEnv")}>
                      <input
                        className="input"
                        value={agentmemoryDraft.secret_env}
                        onChange={(e) =>
                          setAgentmemoryDraft((prev) => ({
                            ...prev,
                            secret_env: e.target.value,
                          }))
                        }
                      />
                    </Field>
                    <Field label={tMemory("agentmemorySessionId")} hint={tMemory("agentmemoryOptional")}>
                      <input
                        className="input"
                        value={agentmemoryDraft.session_id}
                        onChange={(e) =>
                          setAgentmemoryDraft((prev) => ({
                            ...prev,
                            session_id: e.target.value,
                          }))
                        }
                      />
                    </Field>
                    <div className="grid grid-cols-2 gap-2">
                      <Field label={tMemory("agentmemoryBudget")}>
                        <input
                          className="input"
                          type="number"
                          min={1}
                          value={agentmemoryDraft.context_budget}
                          onChange={(e) =>
                            setAgentmemoryDraft((prev) => ({
                              ...prev,
                              context_budget: e.target.value,
                            }))
                          }
                        />
                      </Field>
                      <Field label={tMemory("agentmemoryTimeout")}>
                        <input
                          className="input"
                          type="number"
                          min={0.1}
                          step={0.1}
                          value={agentmemoryDraft.timeout_s}
                          onChange={(e) =>
                            setAgentmemoryDraft((prev) => ({
                              ...prev,
                              timeout_s: e.target.value,
                            }))
                          }
                        />
                      </Field>
                    </div>
                  </div>
                  <div className="mt-3 flex flex-wrap items-center gap-2">
                    <button
                      type="button"
                      className="btn btn-primary"
                      disabled={memoryBusy === "agentmemory:save"}
                      onClick={() => void saveAgentmemoryConfig(true)}
                    >
                      <CheckIcon size={14} />
                      {tMemory("agentmemorySaveEnable")}
                    </button>
                    <button
                      type="button"
                      className="btn btn-ghost"
                      disabled={memoryBusy === "agentmemory:save"}
                      onClick={() => void saveAgentmemoryConfig(false)}
                    >
                      {tMemory("agentmemoryDisable")}
                    </button>
                    <button
                      type="button"
                      className="btn btn-ghost"
                      disabled={memoryBusy === "agentmemory:install"}
                      onClick={() => void loadAgentmemoryInstall()}
                    >
                      {tMemory("agentmemoryInstall")}
                    </button>
                  </div>
                  {agentmemoryInstall ? (
                    <div className="mt-3 rounded-md border border-brand-500/10 bg-ink-950/50 p-2 text-[11px] text-ink-400">
                      <div className="flex flex-wrap items-center gap-2">
                        <Pill tone={agentmemoryInstall.dependency_available ? "ok" : "warn"}>
                          {agentmemoryInstall.dependency_available
                            ? tMemory("providersAvailable")
                            : tMemory("providersUnavailable")}
                        </Pill>
                        <span>{agentmemoryInstall.note}</span>
                      </div>
                      <pre className="mt-2 overflow-x-auto whitespace-pre-wrap rounded bg-ink-950 p-2 font-mono text-[11px] text-ink-100">
                        {agentmemoryInstall.commands.join("\n")}
                      </pre>
                      <div className="mt-1 font-mono text-[11px] text-ink-500">
                        {agentmemoryInstall.health_url} · {agentmemoryInstall.viewer_url}
                      </div>
                    </div>
                  ) : null}
                </div>
                {memoryProvidersData &&
                memoryProvidersData.available_external.length === 0 &&
                memoryProvidersData.external === null ? (
                  <div className="mt-2 text-[11px] text-ink-500">
                    {tMemory("providersAvailableEmpty")}
                  </div>
                ) : null}
                {memoryProvidersData?.external ? (
                  <div className="mt-3 rounded-md border border-brand-500/20 bg-ink-900/40 p-2">
                    <div className="font-mono text-[11px] text-ink-100">
                      {memoryProvidersData.external.name}
                    </div>
                    <div className="mt-0.5 text-[11px] text-ink-500">
                      {memoryProvidersData.external.description}
                    </div>
                    {memoryProvidersData.external.cost_hint ? (
                      <div className="mt-1 text-[11px] text-ink-500">
                        {memoryProvidersData.external.cost_hint}
                      </div>
                    ) : null}
                  </div>
                ) : null}
                {memoryProvidersData && memoryProvidersData.available_external.length > 0 ? (
                  <div className="mt-3 space-y-2">
                    {memoryProvidersData.available_external.map((ext) => (
                      <div
                        key={ext.id}
                        className="flex flex-wrap items-start justify-between gap-2 rounded-md border border-brand-500/10 bg-ink-900/30 p-2"
                      >
                        <div>
                          <div className="font-mono text-[11px] text-ink-100">{ext.name}</div>
                          <div className="mt-0.5 text-[11px] text-ink-500">{ext.description}</div>
                        </div>
                        <Pill tone={ext.available ? "ok" : "warn"}>
                          {ext.available ? tMemory("providersAvailable") : tMemory("providersUnavailable")}
                        </Pill>
                      </div>
                    ))}
                  </div>
                ) : null}
              </div>
            </div>
          </Card>
          ) : null}

          {activeMemorySubTab === "evidence" ? (
            <MemoryEvidencePanel />
          ) : null}

          {activeMemorySubTab === "profile" ? (
            <MemoryProfilePanel />
          ) : null}
        </div>
      ) : null}

      {effectiveSettingsTab === "interface" && !inSectionMode ? (
        <div
          id={settingsPanelId("interface")}
          role="tabpanel"
          aria-labelledby={settingsTabId("interface")}
          className="grid grid-cols-1 gap-5 xl:grid-cols-2"
        >
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
      ) : null}
    </PageBody>
  );
}

// SettingsWorkspace is mounted by FOUR thin wrapper routes:
//   /settings   → <SettingsWorkspace />                     (Models / Access / Network & Env / Interface)
//   /memory     → <SettingsWorkspace forceSection="memory" />
//   /web-search → <SettingsWorkspace forceSection="search" />
//   /browsers   → <SettingsWorkspace forceSection="browsers" />
// Each standalone route reuses every state hook / helper / JSX block
// in this file via the `forceSection` prop so we never duplicate
// ~7000 lines. The file lives under `components/` (not `app/.../page.tsx`)
// because Next's app-router page validation forbids extra named
// exports on `page.tsx`.
