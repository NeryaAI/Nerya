"use client";

/**
 * Browser-side thin client for the Nerya API.
 *
 * The dashboard proxies every call through `/api/proxy/...` so we never
 * leak credentials and CORS never becomes a problem. Method signatures
 * mirror `lib/api.ts` where useful but this module runs in the browser.
 */

import type {
  Candle,
  EquityPoint,
  PortfolioPnl,
  PortfolioPosition,
  PortfolioSummary,
  RecentTrade,
  StrategyCard,
} from "./api";
import type {
  StrategyGenerationRequest,
  StrategyGenerationResponse,
  StrategyHistoryEnvelope,
  StrategyPackageDetail,
  StrategyPackageSummary,
  StrategyRunRecord,
  StrategyScheduleStatus,
  StrategyStatusEnvelope,
  StrategyTuningGenerationRequest,
  StrategyTuningGenerationResponse,
  StrategyTuningRunResult,
  StrategyTuningStatusEnvelope,
  StrategyValidationReport,
  StrategyWorkspaceEnvelope,
  KillSwitchState,
  StrategyPerformanceSnapshot,
} from "./strategyTypes";
import type {
  AgentTaskArtifactsEnvelope,
  AgentTaskTimelineEnvelope,
  AgentTasksEnvelope,
  CapabilityCatalogEnvelope,
  CapabilityEntry,
  CapabilityReadinessEnvelope,
  DataSourceEventsEnvelope,
  DataSourceStatusEnvelope,
  E2eRunEnvelope,
  E2eRunsEnvelope,
  EvidenceDoc,
  EvidenceSearchEnvelope,
  EvidenceSourcesEnvelope,
  EvidenceTopicsEnvelope,
  InboxItemsEnvelope,
  InboxResolveEnvelope,
  InboxResolveRequest,
  OperatorEnvelope,
  OperatorNavEnvelope,
  OperatorOverviewEnvelope,
  ProfileEnvelope,
  ProfileFact,
  PromptGuardClassifyEnvelope,
  PromptGuardItem,
  PromptGuardListEnvelope,
  RuntimeFlagsEnvelope,
  SetupReadinessEnvelope,
} from "./operatorTypes";
import type {
  EvolutionAssetsEnvelope,
  EvolutionEvidenceResolveEnvelope,
  EvolutionEventsEnvelope,
  EvolutionPeriodicReflectionSchedule,
  EvolutionProposalDetail,
  EvolutionSignalsEnvelope,
  EvolutionTimelineEnvelope,
} from "./evolutionTypes";
import { authHeaders, handleAuthFailure } from "./auth";

const BASE = "/api/proxy";
// Bumped from 1.5s -> 4s. Polled endpoints like
// `/operator/overview`, `/operator/nav`, `/health`, `/inbox/items`,
// `/setup/readiness`, `/accounts/list` are touched by multiple
// long-lived components (TopNav + AccountSelector + page-level
// `loadCore`). With the old TTL, a fresh page navigation (~300-800ms
// after a polled hit) blew past the window and refetched, doubling
// round-trip cost for the same data. 4s safely covers cross-page nav
// + concurrent page mounts while staying well under the 15/30/60s
// poll intervals so refresh feel doesn't change.
const READ_CACHE_TTL_MS = 4_000;

type ReadCacheEntry = {
  expiresAt: number;
  promise?: Promise<unknown>;
  value?: unknown;
  settled?: boolean;
};

const readCache = new Map<string, ReadCacheEntry>();

const READONLY_POST_PATHS = new Set([
  "/accounts/list",
  "/control/reconciliation/reports",
  "/executors/list",
  "/evolution/proposals",
  "/market/candles",
  "/messages/list",
  "/orders/list",
  "/portfolio/equity_curve",
  "/accounts/equity_curve",
  "/portfolio/health",
  "/portfolio/pnl",
  "/portfolio/positions",
  "/portfolio/summary",
  "/reconciliation/reports",
  "/strategy/list",
  "/strategy/list_all",
  "/trading/history",
  "/trading/recent_trades",
  "/wallet/portfolio",
]);

function canUseReadCache(method: "GET" | "POST", path: string): boolean {
  if (typeof window === "undefined") return false;
  if (method === "GET") return true;
  return READONLY_POST_PATHS.has(path);
}

function stableBody(body: unknown): string {
  if (body === undefined) return "";
  try {
    return JSON.stringify(body);
  } catch {
    return String(body);
  }
}

async function cachedRead<T>(
  method: "GET" | "POST",
  path: string,
  body: unknown,
  load: () => Promise<T>,
): Promise<T> {
  if (!canUseReadCache(method, path)) {
    return load();
  }

  const key = `${method}:${path}:${stableBody(body)}`;
  const now = Date.now();
  const existing = readCache.get(key);
  if (existing && existing.expiresAt > now) {
    if (existing.promise) return existing.promise as Promise<T>;
    if (existing.settled) return existing.value as T;
  }

  const promise = load()
    .then((value) => {
      readCache.set(key, {
        expiresAt: Date.now() + READ_CACHE_TTL_MS,
        value,
        settled: true,
      });
      return value;
    })
    .catch((error) => {
      readCache.delete(key);
      throw error;
    });
  readCache.set(key, {
    expiresAt: now + READ_CACHE_TTL_MS,
    promise,
  });
  return promise;
}

async function post<T>(path: string, body: unknown = {}): Promise<T> {
  const res = await fetch(`${BASE}${path}`, {
    method: "POST",
    headers: authHeaders({ "content-type": "application/json" }),
    body: JSON.stringify(body),
    cache: "no-store",
  });
  if (!res.ok) {
    const text = await res.text().catch(() => "");
    handleAuthFailure(res.status);
    throw new Error(`HTTP ${res.status}: ${text || res.statusText}`);
  }
  return (await res.json()) as T;
}

async function get<T>(path: string): Promise<T> {
  return cachedRead("GET", path, undefined, async () => {
    const res = await fetch(`${BASE}${path}`, {
      headers: authHeaders(),
      cache: "no-store",
    });
    if (!res.ok) {
      const text = await res.text().catch(() => "");
      handleAuthFailure(res.status);
      throw new Error(`HTTP ${res.status}: ${text || res.statusText}`);
    }
    return (await res.json()) as T;
  });
}

/** Generic helper for callers that need method + body flexibility
 *  (the chat surface, ad-hoc pages that talk to /agent/run_turn etc.).
 *
 *  Superseded all historical imports from ``lib/client.ts`` — keep the
 *  signature stable so existing call sites keep compiling.
 */
export async function callApi<T = unknown>(
  path: string,
  init?: { method?: string; body?: unknown; signal?: AbortSignal }
): Promise<T> {
  const normalizedPath = path.startsWith("/") ? path : "/" + path;
  const method = (init?.method || "GET").toUpperCase();
  const load = async () => {
    const url = `${BASE}${normalizedPath}`;
    const res = await fetch(url, {
      method,
      headers: authHeaders({ "content-type": "application/json" }),
      body: init?.body !== undefined ? JSON.stringify(init.body) : undefined,
      signal: init?.signal,
    });
    const text = await res.text();
    let body: unknown = null;
    try { body = text ? JSON.parse(text) : null; } catch { body = text; }
    if (!res.ok) {
      handleAuthFailure(res.status);
      // Surface as much upstream context as the proxy / backend gave us so
      // the chat error card can show real failure causes (e.g. "ECONNREFUSED
      // 127.0.0.1:18317") instead of opaque labels like ``upstream_unreachable``.
      // Format: ``HTTP <status> <error>: <detail> | trace: <stack>``.
      const parts: string[] = [`HTTP ${res.status}`];
      if (body && typeof body === "object") {
        const obj = body as Record<string, unknown>;
        const errLabel = "error" in obj ? String(obj.error) : "";
        const detail = "detail" in obj ? String(obj.detail) : "";
        const trace = "trace" in obj ? String(obj.trace) : "";
        const message =
          "message" in obj && typeof obj.message === "string"
            ? String(obj.message)
            : "";
        if (errLabel) parts.push(errLabel);
        if (message && message !== detail) parts.push(message);
        if (detail) parts.push(`detail: ${detail}`);
        if (trace) parts.push(`trace: ${trace}`);
      } else if (typeof body === "string" && body.trim()) {
        parts.push(body.trim());
      }
      throw new Error(parts.join(" | "));
    }
    return body as T;
  };
  if (method === "GET") {
    return cachedRead(method, normalizedPath, init?.body, load);
  }
  return load();
}

export interface BacktestRunSummary {
  ts: string;
  days?: number;
  total_return_pct?: number;
  max_dd_pct?: number;
  sharpe_ratio?: number | null;
  verdict?: string;
  start_utc?: string;
  end_utc?: string;
}

export interface BacktestChartEnvelope {
  ok: boolean;
  strategy_id: string;
  proposal_id?: string | null;
  ts: string;
  chart: BacktestChartData;
  error?: string;
}

export interface BacktestChartData {
  schema_version: string;
  meta: Record<string, unknown>;
  panels: BacktestPanel[];
  summary_cards: Array<Record<string, unknown>>;
  tables: Array<{ id: string; columns: string[]; rows: unknown[][] }>;
}

export interface BacktestPanel {
  id: string;
  type: string;
  title: string;
  series: Array<{ kind: string; name?: string; data: Array<Record<string, unknown>> }>;
  annotations?: Array<Record<string, unknown>>;
  guides?: Array<Record<string, unknown>>;
}

export type SecretRef = {
  name: string;
  kind: string;
  scope: string[];
  preview: string;
  fingerprint: string;
  ref: string;
};

export type RuntimeEnvVar = {
  name: string;
  secret_name: string;
  kind: string;
  scope: string[];
  owner: string;
  created_at: string;
  preview: string;
  fingerprint: string;
  ref: string;
};

export type NetworkProxyConfig = {
  enabled: boolean;
  mode: "direct" | "pool" | string;
  preset: string;
  no_proxy: string;
  pool_format: string;
  http_url?: string;
  http_url_ref?: string;
  http_url_preview?: string;
  https_url?: string;
  https_url_ref?: string;
  https_url_preview?: string;
  all_url?: string;
  all_url_ref?: string;
  all_url_preview?: string;
  pool_url?: string;
  pool_url_ref?: string;
  pool_url_preview?: string;
};

export type NetworkProxyPreset = {
  id: string;
  label: string;
  mode: "direct" | "pool" | string;
  description: string;
  all_url?: string;
  pool_url?: string;
  pool_format?: string;
  docs_url?: string;
};

export type NetworkProxyStatus = {
  ok: boolean;
  config: NetworkProxyConfig;
  presets: NetworkProxyPreset[];
  applied?: {
    enabled?: boolean;
    mode?: string;
    preset?: string;
    workspace?: string;
    env?: Record<string, string>;
    error?: string;
    applied_at?: number;
    selected_proxy?: string;
    pool_url?: string;
  };
  error?: string;
  detail?: string;
};

export type TunnelProviderSpec = {
  id: string;
  label: string;
  executable: string;
  description: string;
  docs_url: string;
  free_tier: string;
  install_hint: string;
  token_label?: string;
  token_required_for_start?: boolean;
  supports_workspace_install?: boolean;
  supports_process?: boolean;
  modes: string[];
};

export type TunnelProviderConfig = {
  enabled: boolean;
  target: "dashboard" | "api" | "custom" | string;
  target_url?: string;
  mode: string;
  token_ref?: string;
  token_configured?: boolean;
  public_hostname?: string;
  region?: string;
  cloudflare_mode?: "quick" | "token" | string;
  desired_running?: boolean;
};

export type TunnelProviderStatus = {
  spec: TunnelProviderSpec;
  config: TunnelProviderConfig;
  installed: boolean;
  executable_path?: string;
  version?: string;
  running: boolean;
  state?: {
    pid?: number | null;
    started_at?: number | null;
    target_url?: string;
    log_path?: string;
    command?: string[];
    external_urls?: string[];
    tailscale?: Record<string, unknown>;
  };
};

export type NetworkTunnelsStatus = {
  ok: boolean;
  providers: TunnelProviderStatus[];
  auth: {
    admin_password_configured: boolean;
    auth_mode: string;
    dashboard_target: string;
    api_target: string;
    direct_api_requires_token_mode: boolean;
  };
  error?: string;
  detail?: string;
};

export type NetworkDashboardStatus = {
  ok: boolean;
  config: {
    host: string;
    port: number;
    url: string;
  };
  error?: string;
  detail?: string;
};

export type TunnelConfigPayload = {
  provider: string;
  enabled: boolean;
  target: "dashboard" | "api" | "custom" | string;
  target_url?: string;
  mode?: string;
  token?: string;
  token_ref?: string;
  public_hostname?: string;
  region?: string;
  cloudflare_mode?: "quick" | "token" | string;
  desired_running?: boolean;
};

export type GatewaySecretFieldSpec = {
  key: string;
  ref_key: string;
  label: string;
  kind: "secret" | "url" | "id" | "opaque" | string;
  required: boolean;
  placeholder?: string;
  description?: string;
};

export type GatewayPlatformSpec = {
  id: string;
  title: string;
  alias_id: string;
  status: string;
  inbound: string;
  outbound: string;
  typing: string;
  menu: string;
  attachments?: boolean;
  voice?: boolean;
  notes?: string;
  config_refs?: string[];
  support_level: string;
  docs_url?: string;
  secret_fields?: GatewaySecretFieldSpec[];
  setup_steps?: string[];
};

export type GatewayLiveEvent = {
  seq: number;
  ts: string;
  ts_ms: number;
  kind:
    | "inbound"
    | "outbound"
    | "phase"
    | "error"
    | "heartbeat"
    | "info"
    | string;
  platform: string;
  channel: string;
  chat_id?: string;
  user_id?: string;
  actor_id?: string;
  session_id?: string | null;
  session_key?: string | null;
  text?: string;
  phase?: string;
  iteration?: number;
  command?: string | boolean;
  delivered?: boolean;
  turn_id?: string;
  update_id?: number;
  // ``error`` events carry an operator-actionable hint so the dashboard
  // can render "we dropped this update because <reason>; do <hint>".
  reason?: string;
  detail?: string;
  hint?: string;
  // ``heartbeat`` events confirm the poller is alive.
  status?: string;
  note?: string;
  // ``info`` events with reason=backlog_drained carry the count of
  // stale messages that were dropped on poller startup.
  drained_count?: number;
};

export type GatewayEventsResponse = {
  ok: boolean;
  events: GatewayLiveEvent[];
  cursor: number;
  head: number;
};

export type GatewaySecretRef = {
  configured: boolean;
  ref?: string | null;
  legacy_plaintext?: boolean;
};

export type GatewayChannelConfig = {
  channel: string;
  kind: string;
  platform: string;
  title: string;
  support_level: string;
  configured: boolean;
  enabled: boolean;
  mode: string;
  outbound_ready: boolean;
  config: Record<string, unknown>;
  secret_refs: Record<string, GatewaySecretRef>;
};

export type GatewayConfigEnvelope = {
  ok: boolean;
  channels_file_exists: boolean;
  channels: GatewayChannelConfig[];
  platforms: GatewayPlatformSpec[];
  status?: Record<string, unknown>;
  error?: string;
};

export type GatewayUpsertRequest = {
  channel: string;
  kind: string;
  platform?: string;
  enabled?: boolean;
  chat_id?: string;
  mode?: string;
  polling?: boolean;
  trade_notifications?: boolean;
  approvals?: boolean;
  auto_reply?: boolean;
  allow_unknown_users?: boolean;
  allowed_chat_ids?: string[];
  allowed_user_ids?: string[];
  denied_user_ids?: string[];
  group_sessions_per_user?: boolean;
  thread_sessions_per_user?: boolean;
  topics?: string[];
  username?: string;
  avatar_url?: string;
  parse_mode?: string;
  disable_web_page_preview?: boolean;
  timeout_s?: number;
  // Per-platform secret/url/id fields (bot_token, webhook_url, app_id,
  // app_secret, signing_secret, verification_token, corp_id, agent_id,
  // phone_number_id, smtp_url, imap_url, …) plus the matching ``*_ref``
  // vault pointers. The catalog drives which keys the dashboard renders;
  // unknown keys are accepted by the backend as long as they appear in
  // the platform's ``secret_fields`` spec.
  [key: string]: unknown;
};

export type AccountCredentialField = {
  name: string;
  label: string;
  kind: "secret" | "public" | "url" | string;
  required: boolean;
  description?: string;
  placeholder?: string;
  sensitive: boolean;
  vault_scope?: string;
};

export type WalletCapabilityStatus =
  | "real"
  | "partial"
  | "experimental"
  | "stub";

export type WalletExecutionProfile =
  | "production"
  | "partial"
  | "experimental"
  | "stub";

export type WalletCapabilityCell = {
  supported: boolean;
  status: WalletCapabilityStatus;
  note: string;
};

export type WalletCapabilities = {
  balance: WalletCapabilityCell;
  quote: WalletCapabilityCell;
  swap: WalletCapabilityCell;
  market_data?: WalletCapabilityCell;
  execution_profile: WalletExecutionProfile;
  chains: string[];
  notes: string;
};

export type WalletMarketDataSource = {
  venue: string;
  canonical: string;
  label: string;
  market_format: string;
  fetch_method: string;
  description?: string;
};

export type WalletAuthFlow = {
  id: string;
  kind: string;
  label: string;
  description: string;
  docs_url?: string;
  commands?: string[];
  stores?: string[];
  notes?: string[];
};

export type WalletAuthCliResult = {
  ok: boolean;
  provider?: string;
  return_code?: number;
  json?: Record<string, unknown> | Array<unknown> | null;
  stdout_tail?: string;
  stderr_tail?: string;
  error?: string;
  install_command?: string;
  detail?: string;
};

export type WalletAuthInstallState = {
  installed: boolean;
  kind?: string;
  target?: string;
  install_command?: string;
  install_path?: string;
  command?: string[];
  binary?: string;
  package?: string;
  bin?: string;
};

export type WalletAutoCreateAccountResult = {
  account_id: string;
  mode: string;
  created: boolean;
  wallet_id?: string;
  kind?: string;
} | Record<string, unknown>;

export type WalletAutoCreateAccountWarning = {
  error: string;
  detail?: string;
  attempted_mode?: string;
};

export type WalletAuthStartResponse = {
  ok: boolean;
  provider?: string;
  next_action?: "otp" | "qr_approval" | "device_approval" | string;
  required_inputs?: string[];
  auth?: WalletAuthCliResult;
  install?: Record<string, unknown> | null;
  binding?: Record<string, unknown>;
  bindings?: WalletBinding[];
  account?: WalletAutoCreateAccountResult | null;
  account_warning?: WalletAutoCreateAccountWarning | null;
  error?: string;
  detail?: string;
};

export type WalletAuthVerifyResponse = {
  ok: boolean;
  provider?: string;
  auth?: WalletAuthCliResult;
  account?: WalletAutoCreateAccountResult | null;
  account_warning?: WalletAutoCreateAccountWarning | null;
  binding?: Record<string, unknown>;
  bindings?: WalletBinding[];
  error?: string;
  detail?: string;
};

export type WalletProviderInfo = {
  id: string;
  label: string;
  description: string;
  install_hint: string;
  install_command?: string;
  install_alternatives?: Array<Record<string, unknown>>;
  runtime: string;
  links: Record<string, string>;
  credential_fields?: AccountCredentialField[];
  advanced_credential_fields?: AccountCredentialField[];
  auth_flows?: WalletAuthFlow[];
  auth_install_state?: WalletAuthInstallState;
  market_data_sources?: WalletMarketDataSource[];
  installed?: boolean;
  stability?: WalletExecutionProfile;
  capabilities?: WalletCapabilities | null;
  readiness: {
    provider: string;
    ready: boolean;
    installed?: boolean;
    reason?: string;
    missing?: string[];
    install_hint?: string;
  };
};

export type DiscoveryAccount = {
  id: string;
  exchange: string;
  venue: string;
  kind: string;
  mode: string;
  status: string;
  live_trading_enabled: boolean;
  initial_balance_usd: number;
};

export type DiscoveryWalletProvider = {
  id: string;
  label: string;
  runtime: string;
  ready: boolean;
  reason: string;
  active: boolean;
};

export type DiscoveryVenue = {
  id: string;
  kind: string | null;
  venue: string;
  instrument_types: string[];
};

export type DiscoverySnapshot = {
  accounts: DiscoveryAccount[];
  wallets: { providers: DiscoveryWalletProvider[]; active: string | null };
  venues: DiscoveryVenue[];
  markets: string[];
  strategy_statuses: string[];
  strategy_drivers: string[];
};

export type StrategyAccountShareWarning = {
  code: "account_already_bound";
  account_id: string;
  strategies: Array<{
    strategy_id: string;
    title?: string;
    status?: string;
  }>;
  recommendation: string;
};

export type StrategyRecord = {
  id: string;
  title: string;
  status: string;
  mode: string;
  enabled: boolean;
  account_id: string;
  wallet_id: string | null;
  markets: string[];
  subagents: string[];
  trigger_kinds: string[];
  path: string;
};

export type StrategyDetail = {
  strategy: StrategyRecord;
  strategy_yml: Record<string, unknown>;
  config: Record<string, unknown>;
  limits: Record<string, unknown>;
  prompts: Record<string, string>;
  learnings: string;
};

// ---- Strategy performance envelope ----------------------------------
//
// Operator-grade per-strategy view returned by ``/strategy/performance``.
// Every dollar figure is in USD; every ts is a unix-epoch seconds float.
// Sized so the dashboard can render positions + recent orders + recent
// fills + a downsampled equity curve from a single round trip.

export type StrategyPerformanceShareMerged = {
  position_id: string;
  size_base: number;
  avg_entry_price: number;
  mark_price: number;
  unrealized_pnl_usd: number;
  /** strategy_ids that share the merged ``(account, market)`` row with
   *  this strategy. Useful so the operator can see co-tenants without a
   *  second round trip. */
  co_strategies: string[];
};

export type StrategyPerformancePosition = {
  share_id: string;
  market: string;
  venue: string;
  account_id: string;
  side: "long" | "short";
  size_share_base: number;
  avg_entry_price: number;
  mark_price: number;
  unrealized_pnl_usd: number;
  realized_pnl_usd: number;
  fees_usd: number;
  funding_usd: number;
  notional_usd: number;
  opened_at: number;
  updated_at: number;
  merged: StrategyPerformanceShareMerged | null;
};

export type StrategyPerformanceOrder = {
  order_id: string | null;
  venue_order_id: string | null;
  client_order_id: string | null;
  account_id: string | null;
  strategy_id: string | null;
  market: string | null;
  side: string | null;
  size_base: number;
  price: number | null;
  order_type: string | null;
  state: string | null;
  filled_size: number;
  avg_price: number | null;
  fee_usd: number | null;
  created_at: number;
  updated_at: number;
};

export type StrategyPerformanceFill = {
  fill_id: string;
  order_id: string;
  account_id: string;
  market: string;
  side: string;
  price: number;
  size_base: number;
  notional_usd: number;
  fee_usd: number;
  funding_usd: number;
  ts: number;
};

export type StrategyPerformanceEquityPoint = {
  ts: number;
  realized_pnl_usd: number;
  fees_paid_usd: number;
};

export type StrategyPerformanceKpis = {
  open_positions: number;
  closed_shares: number;
  trades_count: number;
  wins: number;
  losses: number;
  total_realized_usd: number;
  total_unrealized_usd: number;
  fees_usd: number;
  funding_usd: number;
  last_trade_at: number | null;
};

export type StrategyPerformanceEnvelope = {
  ok: boolean;
  strategy_id: string;
  kpis: StrategyPerformanceKpis;
  positions: StrategyPerformancePosition[];
  orders: StrategyPerformanceOrder[];
  fills: StrategyPerformanceFill[];
  equity_curve: StrategyPerformanceEquityPoint[];
  error?: string;
};

export type SubagentRecord = {
  name: string;
  path: string;
  strategies?: string[];
};

export type SubagentDetail = SubagentRecord & {
  body: string;
};

export type ScriptRecord = {
  state: "pending" | "approved" | "rejected" | "examples" | string;
  script_id: string;
  title?: string;
  description?: string;
  path: string;
  bytes: number;
  mtime: number;
};

export type ScriptDetail = {
  script_id: string;
  state: string;
  path: string;
  manifest: Record<string, unknown>;
  source: string;
  truncated: boolean;
};

export type ScriptAnalyzeFinding = {
  severity: "error" | "warning" | "info" | string;
  code: string;
  message: string;
  line?: number;
};

export type ScriptAnalyzeResult = {
  ok?: boolean;
  has_errors?: boolean;
  issues?: ScriptAnalyzeFinding[];
  findings?: ScriptAnalyzeFinding[];
  [key: string]: unknown;
};

export type MessageRecord = {
  message_id?: string;
  ts?: string | number;
  channel?: string;
  severity?: string;
  priority?: string;
  state?: string;
  text?: string;
  kind?: string;
  strategy_id?: string;
  session_id?: string;
  [key: string]: unknown;
};

export type MemoryFileInfo = {
  name: string;
  exists: boolean;
  size: number;
};

export type MemoryRecallResult = {
  ok: boolean;
  scope: "global" | "strategy";
  file?: string;
  strategy_id?: string;
  text?: string;
  truncated?: boolean;
  available_files?: MemoryFileInfo[];
  error?: string;
};

export type EvolutionProposal = {
  id: string;
  kind?: string;
  state?: string;
  scope?: string;
  summary?: string;
  ts?: string;
  created_at?: string;
  target?: string | null;
  rationale?: string;
  metadata?: Record<string, unknown> | null;
  [key: string]: unknown;
};

export type StrategyRuntimePromotionResult = {
  ok: boolean;
  proposal_id: string;
  strategy_id?: string | null;
  state?: string;
  note?: string;
  reason?: string;
  error?: string;
  validation?: StrategyValidationReport;
  promotion?: Record<string, unknown>;
  schedules?: Record<string, unknown>;
  [key: string]: unknown;
};

export type TradingHistoryResult = {
  orders?: Array<Record<string, unknown>>;
  fills?: Array<Record<string, unknown>>;
  [key: string]: unknown;
};

// ---------------------------------------------------------------------------
// Control-plane types.
// ---------------------------------------------------------------------------

export type AccountSnapshotData = {
  snapshot_id?: string;
  account_id: string;
  ts: number;
  health: "ok" | "degraded" | "auth_error" | "rate_limited" | string;
  total_usd?: number;
  free_usd?: number;
  available_usd?: number;
  positions_value_usd?: number;
  latency_ms?: number;
  source?: string;
  [key: string]: unknown;
};

export type ControlPlaneExecutor = {
  executor_id: string;
  kind: string;
  state: string;
  account_id: string;
  strategy_id: string;
  market: string;
  created_at?: number;
  last_heartbeat?: number | null;
  terminal_at?: number | null;
  close_type?: string;
  result_json?: Record<string, unknown>;
  order_ids?: string[];
  reservation_ids?: string[];
  position_id?: string | null;
  intent_id?: string | null;
  plan_id?: string | null;
};

export type ControlPlaneAccountHealth = {
  account_id: string;
  mode: string;
  venue: string;
  kind: string;
  live_trading_enabled: boolean;
  snapshot: AccountSnapshotData | null;
  reserved_usd: number;
  open_position_count: number;
  open_positions: Array<Record<string, unknown>>;
  protection_count: number;
  protections: Array<Record<string, unknown>>;
  active_executors: Array<Pick<
    ControlPlaneExecutor,
    "executor_id" | "kind" | "state" | "market" | "strategy_id" | "created_at" | "last_heartbeat"
  >>;
};

export type ControlPlanePortfolioHealth = {
  accounts: ControlPlaneAccountHealth[];
  totals: {
    accounts: number;
    live_accounts: number;
    open_positions: number;
    active_protections: number;
    active_executors: number;
    reserved_usd: number;
  };
  ts: string;
};

export type ControlPlaneOrder = {
  order_id: string;
  client_order_id?: string;
  exchange_order_id?: string;
  account_id: string;
  strategy_id?: string;
  market: string;
  side: string;
  order_type: string;
  size_base?: number;
  filled_size?: number;
  avg_price?: number | null;
  state: string;
  created_at?: number;
  submitted_at?: number | null;
  last_seen_at?: number | null;
  terminal_at?: number | null;
  executor_id?: string | null;
  intent_id?: string | null;
  plan_id?: string | null;
};

export type ReconciliationReport = {
  report_id: string;
  ts: number;
  scope: "strategy" | "local" | "account" | "active_orders" | "global";
  severity: "info" | "warning" | "action_required" | "trading_halted";
  account_id?: string | null;
  strategy_id?: string | null;
  summary: Record<string, unknown>;
  issues: Array<Record<string, unknown>>;
};

export type Incident = {
  kind: string;
  severity: string;
  ts: number | string;
  [key: string]: unknown;
};

export type KillSwitchView = {
  kill_switch: boolean;
  live_trading_enabled: boolean;
  ts: string;
};

// ---------------------------------------------------------------------------
// Account control plane.
// ---------------------------------------------------------------------------

export type AccountProfileView = {
  id: string;
  mode: "paper" | "shadow" | "canary" | "live" | string;
  venue: string;
  kind: string;
  provider_spec: string;
  base_currency: string;
  subaccount: string;
  status: "active" | "read_only" | "disabled" | "quarantined" | string;
  live_trading_enabled: boolean;
  initial_balance_usd: number;
  permissions: {
    read_balances: boolean;
    place_order: boolean;
    cancel_order: boolean;
    withdraw: boolean;
  };
  limits: Record<string, number>;
  credentials: Record<string, string>;
  wallet_id: string;
};

export type AccountBoundStrategy = {
  strategy_id: string;
  title?: string;
  status?: string;
};

export type AccountSummary = {
  profile: AccountProfileView;
  snapshot: AccountSnapshotData | null;
  reserved_usd: number;
  open_positions: Array<Record<string, unknown>>;
  open_position_count: number;
  protections: Array<Record<string, unknown>>;
  protection_count: number;
  active_executors: Array<Pick<
    ControlPlaneExecutor,
    "executor_id" | "kind" | "state" | "market" | "strategy_id" | "created_at" | "last_heartbeat"
  >>;
  bound_strategies?: AccountBoundStrategy[];
  bound_strategy_count?: number;
  next_refresh_ts?: number | null;
};

export type AccountEquityPoint = {
  ts: number;
  nav_usd: number;
  unrealized_pnl_usd?: number | null;
  source?: string | null;
  health?: string | null;
};

export type WalletBinding = {
  wallet_id: string;
  provider: string;
  label: string;
  source: "providers" | "legacy" | string;
  config: Record<string, unknown>;
};

export type AccountProposalView = {
  id: string;
  kind: "account_roster_patch" | string;
  state: "pending_review" | "approved" | "rejected" | "applied" | string;
  operator: string;
  summary: string;
  target_id: string;
  operation: "create" | "update" | string;
  ts: string;
  payload: Record<string, unknown>;
  diff: Record<string, { before: unknown; after: unknown }>;
  state_ts: string;
  state_note: string;
  applied_ts: string;
};

export type RiskFixHint = {
  title: string;
  detail: string;
  reason: string;
  match: string;
  action?: string;
  href?: string;
};

export type RiskEvaluationRow = {
  risk_evaluation_id: string;
  intent_id: string | null;
  plan_id: string | null;
  strategy_id: string;
  account_id: string;
  decision: "allow" | "reject" | "escalate" | string;
  notional_usd: number;
  reasons: string[];
  fix_hints: RiskFixHint[];
  snapshot: Record<string, unknown>;
  ts: number;
};

export type StrategyHistoryEvent = {
  ts?: string | number;
  kind?: string;
  strategy_id?: string;
  session_id?: string;
  order_id?: string;
  event?: Record<string, unknown>;
  [key: string]: unknown;
};

export type SkillActionSummary =
  | string
  | {
      name: string;
      status?: string;
      tags?: string[];
      risk_gate?: string;
      approval_gate?: string;
      context_policy?: string;
    };

export type SkillSummary = {
  id: string;
  title?: string;
  description?: string;
  version?: string;
  status?: string;
  style?: string;
  source?: string;
  path?: string;
  has_playbook?: boolean;
  metadata?: Record<string, unknown>;
  permissions?: string[];
  actions?: SkillActionSummary[];
  tags?: string[];
};

export type SkillFileSummary = {
  path: string;
  kind: "playbook" | "script" | "reference" | "template" | "file" | string;
  size: number;
  mtime: number;
};

export type SkillDetail = SkillSummary & {
  instructions?: string;
  skill_md?: string;
  relative_path?: string;
  editable?: boolean;
  editable_reason?: string;
  files?: SkillFileSummary[];
};

export type MemoryWriteRuleConfig = {
  category: string;
  enabled: boolean;
  retention_days: number;
  max_entries: number;
  dedupe: string;
  target_files: string[];
};

export type MemoryActivityEvent = {
  ts: string;
  kind: "write_ok" | "write_skipped" | "search";
  category?: string;
  key?: string;
  title?: string;
  preview?: string;
  hash?: string;
  source?: string;
  actor_id?: string;
  skip_reason?: string;
  query?: string;
  result_count?: number;
  latency_ms?: number;
  extra?: Record<string, unknown>;
};

export type MemoryProviderView = {
  id: string;
  name: string;
  family: "builtin" | "external" | string;
  description: string;
  requires_api_key: boolean;
  env_key: string | null;
  cost_hint: string;
  install_command?: string;
  install_alternatives?: string[];
  docs_url?: string;
  available: boolean;
  initialised: boolean;
  last_error: string;
  last_initialised_at: number | null;
};

export type MemoryExternalConfig = {
  enabled: boolean;
  provider: string;
  available_providers: string[];
  agentmemory: {
    base_url: string;
    secret_ref: string;
    secret_env: string;
    project: string;
    session_id: string;
    context_budget: number;
    timeout_s: number;
    install_command: string;
    mcp_command: string;
    viewer_url: string;
    docs_url?: string;
  };
};


export type MemoryNotebookSnapshot = {
  entries: string[];
  used_chars: number;
  char_limit: number;
  snapshot: string;
};


export type OAuthProviderStatus = {
  provider: string;
  has_token: boolean;
  source?: string;          // "cli_file" | "paste" | "env" | ""
  cli_path?: string;        // detected CLI credential path, if any
  cli_present?: boolean;    // whether the CLI credential file exists on disk
  env_present?: boolean;    // whether the fallback env var is set
  updated_at?: string;
};


export type LlmRouteConfig = {
  provider: string;
  model: string;
  models?: string[];
  base_url?: string;
  provider_key_ref?: string;
  provider_key_refs?: string[];
  provider_key?: string;
  provider_keys?: string[];
  has_key_ref?: boolean;
  kind?: string;
  provider_native_web_search?: Record<string, unknown>;
};

export type LlmTierConfig = {
  tier: string;
  provider: string;
  model: string;
  models?: string[];
  base_url?: string;
  provider_key_ref?: string;
  provider_key?: string;
  has_key_ref?: boolean;
  provider_native_web_search?: Record<string, unknown>;
  // Reasoning intensity hint forwarded to capable models (OpenAI o-series,
  // Codex Responses, Anthropic extended thinking, Gemini). Empty string
  // means "do not pass" — the model uses its built-in default. Allowed
  // values are the strings returned in `LlmConfigResponse.reasoning_levels`.
  reasoning_effort?: string;
  routes?: LlmRouteConfig[];
};

export type LlmProviderProfile = {
  provider: string;
  base_url?: string;
  provider_key_ref?: string;
  provider_key?: string;
  has_key_ref?: boolean;
};

export type MemoryVectorEmbedding = {
  provider: string;
  model: string;
  base_url: string;
  api_key_ref: string;
  has_key: boolean;
};

export type MemoryVectorMilvus = {
  uri: string;
  collection: string;
  has_token: boolean;
};

export type MemoryVectorStatus = {
  ok: boolean;
  enabled: boolean;
  backend: string;
  dependency_available: boolean;
  install_package: string;
  watch_enabled: boolean;
  watcher_running: boolean;
  paths: string[];
  embedding?: MemoryVectorEmbedding;
  milvus?: MemoryVectorMilvus;
  error?: string;
  detail?: string;
};

export type SearchEngineKeyCounts = {
  workspace: number;
  vault: number;
  env: number;
  total: number;
};

export type SearchEngineBaseUrlInfo = {
  workspace?: string;
  env?: string;
  default?: string;
  effective?: string;
};

export type SearchEngineStatus = {
  name: string;
  needs_key: boolean;
  needs_base_url: boolean;
  ready: boolean;
  key_counts: SearchEngineKeyCounts;
  vault_ref: string | null;
  key_preview: string[];
  base_url?: SearchEngineBaseUrlInfo;
};

export type SearchSearxngProbe = {
  ok: boolean;
  status?: number;
  content_type?: string;
  elapsed_ms?: number;
  body_preview?: string;
  error?: string;
};

export type SearchSearxngStatus = {
  ok: boolean;
  docker_available?: boolean;
  container_name?: string;
  container_running?: boolean;
  image?: string;
  host_port?: number;
  base_url?: string;
  config_dir?: string;
  state_file?: string;
  deployed?: boolean;
  probe?: SearchSearxngProbe;
  error?: string;
};

export type SearchEnginesStatus = {
  ok: boolean;
  engines: string[];
  region: string;
  safesearch: string;
  supported: string[];
  keyless?: string[];
  base_url_engines?: string[];
  default_base_urls?: Record<string, string>;
  engine_status: SearchEngineStatus[];
  usable_in_chain: number;
  workspace_path: string;
  searxng?: SearchSearxngStatus | null;
};

export type SearchEnginesConfigRequest = {
  engines?: string[];
  region?: string;
  safesearch?: string;
  /**
   * Per-engine key list. Values may be either a comma-separated string or
   * an array of strings. Send an empty array (or empty string) to wipe a
   * given engine's stored keys.
   */
  keys?: Record<string, string | string[]>;
  /**
   * Per-engine base URL override. Used by ``searxng`` and ``firecrawl``.
   * Pass an empty string to clear an override (falls back to env / default).
   */
  base_urls?: Record<string, string>;
  /**
   * `vault` (default) stores keys encrypted in the workspace SecretVault
   * under ``vault://search.<engine>.keys``. `workspace` writes them
   * plaintext into ``workspace/search_engines.json`` — only use for local
   * dev / experimentation.
   */
  store?: "vault" | "workspace";
};

export type SearchSearxngDeployRequest = {
  host_port?: number;
  image?: string;
  container_name?: string;
  rebuild?: boolean;
};

export type SearchSearxngDeployResponse = SearchSearxngStatus & {
  ok: boolean;
  detail?: string;
  settings_yml?: string;
};

export type BrowserKind = "binary" | "python_pkg" | "node_service";

export type BrowserSpec = {
  name: string;
  title: string;
  kind: BrowserKind;
  recommended_rank?: number;
  summary: string;
  homepage: string;
  license: string;
  supported_platforms?: string[] | null;
  pip_package?: string | null;
  repo_url?: string | null;
  service_url?: string | null;
  notes?: string;
};

export type BrowserStatusRow = {
  name: string;
  title: string;
  kind: BrowserKind;
  recommended_rank?: number;
  summary: string;
  homepage: string;
  installed: boolean;
  ready?: boolean;
  managed: boolean;
  enabled: boolean;
  binary_path?: string;
  checkout_path?: string;
  module?: string;
  version?: string;
  platform: string;
  platform_supported: boolean;
  asset?: string;
  pip_package?: string | null;
  repo_url?: string | null;
  service_url?: string | null;
  service_ready?: boolean;
  service_error?: string;
  notes?: string;
};

export type BrowsersStatus = {
  ok: boolean;
  engines: BrowserStatusRow[];
  selected: string | null;
  platform: string;
  state_file: string;
  binaries_dir: string;
};

export type BrowsersConfigureRequest = {
  selected?: string;
  enabled?: Record<string, boolean>;
};

export type BrowserActionResponse = {
  ok: boolean;
  name?: string;
  error?: string;
  detail?: string;
  binary?: string;
  version?: string;
  asset?: string;
  platform?: string;
  package?: string;
  elapsed_ms?: number;
  status?: BrowsersStatus;
  stderr_tail?: string;
  stdout_tail?: string;
};

export type FinancialDatasetsStatus = {
  ok: boolean;
  name: string;
  ready: boolean;
  total_keys: number;
  vault_count: number;
  env_count: number;
  env_sources: string[];
  vault_ref: string;
  key_preview: string[];
  documentation: string;
  error?: string;
};

export type FinancialDatasetsKeysRequest = {
  keys?: string | string[];
  store?: "vault" | "workspace";
};

export type BrowserProbeResponse = {
  ok: boolean;
  name?: string;
  error?: string;
  detail?: string;
  url?: string;
  fetch_method?: string;
  elapsed_ms?: number;
  bytes?: number;
  markdown?: string;
  text?: string;
  html?: string;
  markdown_preview?: string;
  text_preview?: string;
  html_preview?: string;
  returncode?: number;
  stderr_tail?: string;
};

// ---- Live browser sessions ------------------------------------------

export type BrowserSessionFetchResult = {
  ok?: boolean;
  name?: string;
  error?: string;
  detail?: string;
  url?: string;
  fetch_method?: string;
  elapsed_ms?: number;
  bytes?: number;
  markdown?: string;
  text?: string;
  html?: string;
  returncode?: number;
  stderr_tail?: string;
};

export type BrowserSessionHistoryEntry = {
  ts: string;
  url: string;
  ok: boolean;
  fetch_method?: string;
  bytes?: number;
  elapsed_ms?: number;
};

export type BrowserSessionSummary = {
  session_id: string;
  engine?: string;
  current_url?: string;
  created_at?: string;
  updated_at?: string;
  history_count?: number;
  last_ok?: boolean;
  last_fetch_method?: string;
  last_bytes?: number;
  last_elapsed_ms?: number;
  cdp?: boolean;
};

export type BrowserSessionEnvelope = BrowserSessionSummary & {
  ok: boolean;
  error?: string;
  engine?: string;
  result?: BrowserSessionFetchResult;
};

export type BrowserSessionScreenshot = {
  ts: string;
  url: string;
  ok: boolean;
  path?: string;
  bytes?: number;
  elapsed_ms?: number;
  fetch_method?: string;
  error?: string;
  stderr_tail?: string;
  data_uri?: string;
};

export type BrowserSessionRecord = BrowserSessionSummary & {
  ok: boolean;
  history: BrowserSessionHistoryEntry[];
  last?: BrowserSessionFetchResult;
  screenshots?: BrowserSessionScreenshot[];
  last_screenshot?: BrowserSessionScreenshot;
};

export type BrowserSessionScreenshotResponse = {
  ok: boolean;
  session_id?: string;
  engine?: string;
  url?: string;
  path?: string;
  bytes?: number;
  elapsed_ms?: number;
  fetch_method?: string;
  data_uri?: string;
  error?: string;
  detail?: string;
  stderr_tail?: string;
  data_uri_error?: string;
};

export type BrowserSessionListResponse = {
  ok: boolean;
  count: number;
  sessions: BrowserSessionSummary[];
};

export type BrowserSessionStartRequest = {
  url: string;
  engine?: string;
  session_id?: string;
  timeout_s?: number;
};

export type BrowserSessionNavigateRequest = {
  session_id: string;
  url: string;
  timeout_s?: number;
};

export type BrowserCdpAction =
  | "click_xy"
  | "click_selector"
  | "type"
  | "press"
  | "scroll"
  | "scroll_to"
  | "goto"
  | "go_back"
  | "go_forward"
  | "reload"
  | "eval"
  | "title"
  | "get_console"
  | "get_network"
  | "get_api_requests"
  | "clear_events";

export type BrowserCdpActionRequest = {
  session_id: string;
  action: BrowserCdpAction;
  payload?: Record<string, unknown>;
};

export type BrowserConsoleEvent = {
  ts?: string;
  kind?: string;
  level?: string;
  text?: string;
  url?: string;
  line?: number;
  column?: number;
  source?: string;
};

export type BrowserNetworkEvent = {
  ts?: string;
  kind?: string;
  method?: string;
  url?: string;
  status?: number | string;
  status_text?: string;
  resource_type?: string;
  request_id?: string;
  elapsed_ms?: number;
  api?: boolean;
  ok?: boolean;
  failure?: string;
};

export type BrowserCdpActionResponse = {
  ok: boolean;
  action?: string;
  current_url?: string;
  elapsed_ms?: number;
  error?: string;
  detail?: string;
  hint?: string;
  click?: { x: number; y: number };
  selector?: string;
  url?: string;
  typed?: number;
  key?: string;
  delta?: { dx: number; dy: number };
  value?: string;
  title?: string;
  console?: BrowserConsoleEvent[];
  events?: BrowserNetworkEvent[];
  count?: number;
  total?: number;
  cleared?: { console?: number; network?: number };
};

export type AuthStatus = {
  ok: boolean;
  mode: string;
  password_configured: boolean;
  jwt_configured: boolean;
  jwt_ttl_seconds: number;
  static_token_configured: boolean;
};

export type AuthLoginResponse = {
  ok: boolean;
  error?: string;
  detail?: string;
  token?: string;
  token_type?: string;
  expires_at?: number;
  expires_in?: number;
  actor?: string;
  scope?: string;
  password_configured?: boolean;
};

export interface WorkspaceFileEntry {
  name: string;
  kind: "file" | "dir";
  size: number | null;
  mtime_ms: number;
  path: string;
  is_symlink?: boolean;
}

export interface WorkspaceSyncConfig {
  enabled: boolean;
  provider: "git" | "webdav";
  remote: string;
  branch: string;
  git_path: string;
  remote_path: string;
  username_ref: string;
  password_ref: string;
  includes: string[];
  excludes: string[];
}

export interface WorkspaceSyncStatus {
  ok: boolean;
  error?: string;
  detail?: string;
  config: WorkspaceSyncConfig;
  configured: boolean;
  credential_ready: boolean | null;
  git_available: boolean;
  last_sync?: {
    ok: boolean;
    action: "pull" | "push" | "sync";
    provider: "git" | "webdav";
    finished_at: string;
    results: Array<Record<string, unknown>>;
  } | null;
  config_path: string;
  safety: { hard_excludes: string[]; credentials: string };
}

export interface WorkspaceSyncRunResult {
  ok: boolean;
  error?: string;
  detail?: string;
  conflicts?: string[];
  action?: "pull" | "push" | "sync";
  provider?: "git" | "webdav";
  finished_at?: string;
  results?: Array<Record<string, unknown>>;
}

export const clientApi = {
  authStatus: () => get<AuthStatus>("/auth/status"),
  authLogin: (body: { password: string }) =>
    post<AuthLoginResponse>("/auth/login", body),
  authSetPassword: (body: { current_password?: string; new_password: string }) =>
    post<AuthLoginResponse>("/auth/admin/password", body),
  authLogout: () => post<{ ok: boolean }>("/auth/logout", {}),
  health: () => get<{ status: string }>("/health"),
  workspace: () => get<{ root: string; live_trading_enabled: boolean; kill_switch: boolean }>("/workspace"),
  workspaceSyncStatus: () => get<WorkspaceSyncStatus>("/workspace/sync"),
  workspaceSyncConfig: (body: Partial<WorkspaceSyncConfig>) =>
    post<WorkspaceSyncStatus>("/workspace/sync/config", body),
  workspaceSyncRun: (body: {
    action: "pull" | "push" | "sync";
    force?: boolean;
  }) => post<WorkspaceSyncRunResult>("/workspace/sync/run", body),
  workspaceFilesList: (
    path: string = ".",
    show_hidden: boolean = false,
    limit: number = 500,
  ) =>
    get<{
      ok: boolean;
      error?: string;
      detail?: string;
      path?: string;
      absolute?: string;
      root?: string;
      entries?: WorkspaceFileEntry[];
      truncated?: boolean;
      show_hidden?: boolean;
      breadcrumbs?: Array<{ name: string; path: string }>;
    }>(
      `/workspace/files?path=${encodeURIComponent(path)}&show_hidden=${
        show_hidden ? "1" : "0"
      }&limit=${limit}`,
    ),
  workspaceFileRead: (path: string) =>
    get<{
      ok: boolean;
      error?: string;
      detail?: string;
      path?: string;
      binary?: boolean;
      size?: number;
      content?: string;
      truncated?: boolean;
    }>(`/workspace/file?path=${encodeURIComponent(path)}`),
  workspaceFileSave: (body: { path: string; content: string }) =>
    post<{
      ok: boolean;
      error?: string;
      detail?: string;
      path?: string;
      size?: number;
      mtime_ms?: number;
    }>("/workspace/file/save", body),
  workspaceFileDelete: (body: { path: string; recursive?: boolean }) =>
    post<{ ok: boolean; error?: string; detail?: string; path?: string }>(
      "/workspace/file/delete",
      body,
    ),
  workspaceFileCreate: (body: { path: string; kind: "file" | "dir" }) =>
    post<{ ok: boolean; error?: string; detail?: string; path?: string; kind?: string }>(
      "/workspace/file/create",
      body,
    ),
  workspaceFileRename: (body: { from: string; to: string }) =>
    post<{ ok: boolean; error?: string; detail?: string; from?: string; to?: string }>(
      "/workspace/file/rename",
      body,
    ),
  callSkill: <T = unknown>(skill_id: string, action: string, payload: unknown = {}) =>
    post<T>("/skills/call", { skill_id, action, payload, caller: "dashboard" }),
  skills: () =>
    get<{
      skills: SkillSummary[];
    }>("/skills"),
  skillDetail: (skill_id: string) =>
    get<{
      ok: boolean;
      error?: string;
      skill?: SkillDetail;
    }>(`/skills/detail?skill_id=${encodeURIComponent(skill_id)}`),
  skillUpdate: (body: { skill_id: string; skill_md: string; reason?: string }) =>
    post<{
      ok: boolean;
      error?: string;
      detail?: string;
      skill?: SkillDetail;
      updated_at?: string;
      reloaded?: number;
    }>("/skills/update", body),
  skillCreate: (body: {
    name: string;
    description?: string;
    body?: string;
    skill_md?: string;
    overwrite?: boolean;
  }) =>
    post<{
      ok: boolean;
      error?: string;
      detail?: string;
      skill?: SkillDetail;
      created_at?: string;
      reloaded?: number;
    }>("/skills/create", body),
  skillsInstalled: () =>
    get<{
      installed: Array<Record<string, unknown>>;
    }>("/skills/installed"),
  skillsInstall: (body: {
    source: string;
    kind?: "auto" | "git" | "dir" | "tar";
    subdir?: string;
    git_ref?: string;
  }) => post<Record<string, unknown>>("/skills/install", body),
  skillsPromote: (skill_id: string) =>
    post<{ ok: boolean; skill_id: string; installed_at: string }>(
      "/skills/promote",
      { skill_id },
    ),
  skillsLockStatus: () =>
    get<{
      ok: boolean;
      lock: Record<string, unknown>;
      drift: Record<string, unknown>;
      signature?: Record<string, unknown> | null;
    }>("/skills/lock/status"),
  skillsLockInspect: () =>
    get<{
      ok: boolean;
      entries: Array<Record<string, unknown>>;
    }>("/skills/lock/inspect"),

  portfolioSummary: () => post<PortfolioSummary>("/portfolio/summary"),
  portfolioPositions: () => post<{ positions: PortfolioPosition[] }>("/portfolio/positions"),
  portfolioPnl: () => post<PortfolioPnl>("/portfolio/pnl"),
  portfolioEquityCurve: (limit = 120) =>
    post<{ points: EquityPoint[]; equity_usd: number }>("/portfolio/equity_curve", { limit }),

  discoverySnapshot: () => get<DiscoverySnapshot>("/discovery"),
  discoveryAccounts: () =>
    get<{ accounts: DiscoveryAccount[] }>("/discovery/accounts"),
  discoveryWallets: () =>
    get<{ providers: DiscoveryWalletProvider[]; active: string | null }>(
      "/discovery/wallets",
    ),
  discoveryVenues: () => get<{ venues: DiscoveryVenue[] }>("/discovery/venues"),
  discoveryMarkets: () => get<{ markets: string[] }>("/discovery/markets"),
  discoveryLifecycle: () =>
    get<{ statuses: string[]; drivers: string[] }>("/discovery/lifecycle"),

  strategyList: () => post<{ strategies: StrategyCard[] }>("/strategy/list"),
  strategiesAll: (includeArchived = false) =>
    post<{ strategies: StrategyRecord[] }>("/strategy/list_all", {
      include_archived: includeArchived,
    }),
  strategyGet: (strategyId: string) =>
    post<StrategyDetail>("/strategy/get", { strategy_id: strategyId }),
  strategyPerformance: (
    strategyId: string,
    opts?: {
      limit_orders?: number;
      limit_fills?: number;
      equity_points?: number;
    },
  ) =>
    post<StrategyPerformanceEnvelope>("/strategy/performance", {
      strategy_id: strategyId,
      ...(opts ?? {}),
    }),
  strategyCreate: (body: {
    strategy_id: string;
    title: string;
    account_id: string;
    markets: string[];
    trigger_kinds?: string[];
    subagents?: string[];
    driver?: "prompt" | "script";
    status?: "draft" | "paper" | "canary" | "live" | "paused" | "archived";
    main_prompt?: string;
    subagent_prompts?: Record<string, string>;
    limits?: Record<string, number | boolean>;
    wallet_id?: string;
    description?: string;
  }) =>
    post<{
      ok: boolean;
      strategy_id: string;
      state: string;
      path: string;
      // strategy_crud surfaces a soft warning when
      // the account is already referenced by other non-archived
      // strategies. ``kind=account_shared``, plus the suggested
      // sub-account remediation.
      warning?: StrategyAccountShareWarning | null;
    }>("/strategy/create", body),
  strategyUpdate: (strategyId: string, patch: {
    title?: string;
    description?: string;
    account_id?: string;
    wallet_id?: string | null;
    markets?: string[];
    trigger_kinds?: string[];
    subagents?: string[];
    driver?: "prompt" | "script" | "manual";
    limits?: Record<string, unknown>;
    config?: Record<string, unknown>;
    prompts?: Record<string, string>;
    reason?: string;
  }) =>
    post<{
      ok: boolean;
      strategy_id: string;
      state: string;
      changed: string[];
      version_id?: string;
      warning?: StrategyAccountShareWarning | null;
    }>("/strategy/update", { strategy_id: strategyId, ...patch }),
  strategyDelete: (body: { strategy_id: string; force?: boolean }) =>
    post<{
      ok: boolean;
      strategy_id: string;
      deleted?: boolean;
      path?: string;
      removed_schedules?: string[];
      error?: string;
      state?: {
        open_positions: number;
        active_executors: number;
        active_orders: number;
      };
    }>("/strategy/delete", body),
  strategyClosePositions: (body: { strategy_id: string; dry_run?: boolean; operator?: string; reason?: string }) =>
    post<{
      ok: boolean;
      strategy_id: string;
      dry_run: boolean;
      count: number;
      positions: Array<{
        position_id: string;
        account_id: string;
        market: string;
        side: "long" | "short";
        size_base: number;
        mark_price: number;
        notional_usd: number;
        unrealized_pnl_usd: number;
      }>;
      submitted?: Array<{
        position_id: string;
        market?: string;
        side?: "long" | "short";
        size_base?: number;
        status: string;
        error?: string;
        result?: unknown;
      }>;
      remaining_state?: {
        open_positions: number;
        active_executors: number;
        active_orders: number;
      };
      error?: string;
    }>("/strategy/close_positions", body),
  strategySetStatus: (strategyId: string, status: string, reason?: string) =>
    post<{ ok: boolean; strategy_id: string; status: string }>(
      "/strategy/set_status",
      { strategy_id: strategyId, status, reason },
    ),
  strategyBindWallet: (strategyId: string, walletId: string | null) =>
    post<{ ok: boolean; strategy_id: string; wallet_id: string | null }>(
      "/strategy/bind_wallet",
      { strategy_id: strategyId, wallet_id: walletId },
    ),
  strategyBindAccount: (strategyId: string, accountId: string) =>
    post<{
      ok: boolean;
      strategy_id: string;
      account_id: string;
      warning?: StrategyAccountShareWarning | null;
    }>("/strategy/bind_account", {
      strategy_id: strategyId,
      account_id: accountId,
    }),
  strategyResolveRuntime: (strategyId: string) =>
    post<{
      ok: boolean;
      strategy_id: string;
      effective_account: string;
      account_source: string;
      effective_wallet: string | null;
      wallet_source: string | null;
    }>("/strategy/resolve_runtime", { strategy_id: strategyId }),
  strategyVersions: (strategyId: string) =>
    post<{
      strategy_id: string;
      versions: Array<{
        version_id: string;
        ts: string;
        reason: string;
        title?: string;
        status?: string;
      }>;
    }>("/strategy/versions", { strategy_id: strategyId }),
  strategyFilesList: (strategyId: string) =>
    post<{
      strategy_id: string;
      root: string;
      files: Array<{
        rel_path: string;
        size: number;
        kind: "python" | "yaml" | "markdown" | "json" | "text";
        content: string | null;
        error?: "decode_failed" | "too_large";
      }>;
    }>("/strategy/files_list", { strategy_id: strategyId }),
  strategyFilesWrite: (
    strategyId: string,
    rel_path: string,
    content: string,
    reason?: string,
  ) =>
    post<{
      ok: boolean;
      strategy_id: string;
      rel_path: string;
      size: number;
      version_id?: string | null;
      error?: string;
    }>("/strategy/files_write", {
      strategy_id: strategyId,
      rel_path,
      content,
      reason: reason || "dashboard_write_file",
    }),
  strategyBacktests: (strategyId: string) =>
    post<{ ok: boolean; strategy_id: string; backtests: BacktestRunSummary[] }>(
      "/strategy/backtests",
      { strategy_id: strategyId },
    ),
  strategyBacktestChart: (strategyId: string, ts: string, proposalId?: string | null) =>
    post<BacktestChartEnvelope>("/strategy/backtests/chart", {
      strategy_id: strategyId,
      ts,
      ...(proposalId ? { proposal_id: proposalId } : {}),
    }),
  strategyBacktestFile: (strategyId: string, ts: string, name: string, proposalId?: string | null) =>
    post<{ ok: boolean; strategy_id: string; proposal_id?: string | null; ts: string; name: string; content: string }>(
      "/strategy/backtests/file",
      { strategy_id: strategyId, ts, name, ...(proposalId ? { proposal_id: proposalId } : {}) },
    ),

  subagentList: () =>
    post<{ subagents: SubagentRecord[] }>("/skills/call", {
      skill_id: "subagent", action: "list_subagents",
      payload: {}, caller: "dashboard",
    }),
  subagentGet: (name: string) =>
    post<SubagentDetail>("/skills/call", {
      skill_id: "subagent", action: "get_subagent",
      payload: { name }, caller: "dashboard",
    }),
  subagentCreate: (body: {
    name: string;
    prompt: string;
    description?: string;
    overwrite?: boolean;
  }) =>
    post<{ name: string; path: string; state: string; notice?: string }>(
      "/skills/call",
      { skill_id: "subagent", action: "create_subagent", payload: body, caller: "dashboard" },
    ),
  subagentDelete: (name: string, force = false) =>
    post<{ name: string; state: string; path: string; strategies?: string[] }>(
      "/skills/call",
      {
        skill_id: "subagent", action: "delete_subagent",
        payload: { name, force }, caller: "dashboard",
      },
    ),

  // Agents library — workspace-level personas (workspace + defaults).
  // Backed by the persistent role registry (see routes_teams.py).
  agentsList: () =>
    post<{
      ok: boolean;
      roles: Array<{
        name: string;
        tier: string;
        allowed_skills: string[];
        source: "workspace" | "default";
        prompt_path?: string;
        description?: string;
      }>;
    }>("/teams/roles", {}),
  agentsGet: (name: string) =>
    post<{
      ok: boolean;
      role?: {
        name: string;
        tier: string;
        allowed_skills: string[];
        prompt: string;
        prompt_path?: string;
        source: "workspace" | "default";
        persistent: boolean;
      };
      error?: string;
    }>("/teams/role/get", { name }),
  agentsSave: (body: {
    name: string;
    prompt: string;
    allowed_skills?: string[];
    tier?: "light" | "medium" | "high";
  }) =>
    post<{
      ok: boolean;
      role?: {
        name: string;
        tier: string;
        allowed_skills: string[];
        prompt: string;
        prompt_path?: string;
        source: "workspace";
        persistent: boolean;
      };
      error?: string;
    }>("/teams/role/save", body),
  agentsDelete: (name: string) =>
    post<{ ok: boolean; deleted: boolean; name: string; error?: string }>(
      "/teams/role/delete",
      { name },
    ),
  subagentRename: (
    name: string,
    newName: string,
    options?: { update_strategy_refs?: boolean; overwrite?: boolean },
  ) =>
    post<{
      name: string;
      new_name: string;
      path: string;
      state: string;
      updated_strategies?: string[];
    }>("/skills/call", {
      skill_id: "subagent", action: "rename_subagent",
      payload: { name, new_name: newName, ...(options ?? {}) }, caller: "dashboard",
    }),
  subagentDuplicate: (name: string, newName: string, overwrite = false) =>
    post<{ name: string; new_name: string; path: string; state: string }>(
      "/skills/call",
      {
        skill_id: "subagent", action: "duplicate_subagent",
        payload: { name, new_name: newName, overwrite }, caller: "dashboard",
      },
    ),

  scriptList: (state: "all" | "pending" | "approved" | "rejected" | "examples" = "all", maxEntries = 200) =>
    post<{ scripts: ScriptRecord[]; truncated: boolean; count: number }>("/skills/call", {
      skill_id: "script", action: "list_scripts",
      payload: { state, max_entries: maxEntries }, caller: "dashboard",
    }),
  scriptGet: (scriptId: string, state?: "pending" | "approved" | "rejected" | "examples") =>
    post<ScriptDetail>("/skills/call", {
      skill_id: "script", action: "read_script",
      payload: { script_id: scriptId, state, max_bytes: 200_000 }, caller: "dashboard",
    }),
  scriptCreate: (body: {
    script_id: string;
    summary: string;
    description?: string;
    script?: string;
  }) =>
    post<{ script_id: string; path: string; state: string }>("/skills/call", {
      skill_id: "script", action: "generate_script_proposal",
      payload: body, caller: "dashboard",
    }),
  scriptDelete: (scriptId: string, state?: "pending" | "approved" | "rejected") =>
    post<{ script_id: string; deleted: boolean; state: string; path: string }>("/skills/call", {
      skill_id: "script", action: "delete_script",
      payload: { script_id: scriptId, state }, caller: "dashboard",
    }),
  scriptApprove: (scriptId: string) =>
    post<{ script_id: string; path: string; state: string }>("/skills/call", {
      skill_id: "script", action: "approve_script",
      payload: { script_id: scriptId }, caller: "dashboard",
    }),
  scriptRun: (scriptId: string, args: Record<string, unknown> = {}) =>
    post<Record<string, unknown>>("/skills/call", {
      skill_id: "script", action: "run_script",
      payload: { script_id: scriptId, args }, caller: "dashboard",
    }),
  scriptAnalyzeSource: (source: string) =>
    callApi<ScriptAnalyzeResult>("/scripts/analyze", { method: "POST", body: { source } }),

  recentTrades: (limit = 25) =>
    post<{ trades: RecentTrade[] }>("/trading/recent_trades", { limit }),
  tradingHistory: (strategy_id: string, limit = 20) =>
    post<TradingHistoryResult>("/trading/history", { strategy_id, limit }),
  tradingCancelOrder: (strategy_id: string, order_id: string) =>
    post<Record<string, unknown>>("/trading/cancel", { strategy_id, order_id }),

  // ---- Control plane ----
  portfolioHealth: (account_id?: string) =>
    post<ControlPlanePortfolioHealth>("/portfolio/health", { account_id }),
  controlOrdersList: (filter?: {
    account_id?: string;
    state?: "active" | "cached" | "lost" | "recent";
    limit?: number;
  }) =>
    post<{ orders: ControlPlaneOrder[]; filter: Record<string, unknown> }>(
      "/orders/list",
      filter ?? {}
    ),
  controlOrderCancel: (body: { order_id: string; reason?: string; operator?: string }) =>
    post<{ ok: boolean; order_id: string; state: string }>("/orders/cancel", body),
  controlExecutorsList: (filter?: {
    account_id?: string;
    state?: "active" | "recent";
    limit?: number;
  }) =>
    post<{ executors: ControlPlaneExecutor[]; filter: Record<string, unknown> }>(
      "/executors/list",
      filter ?? {}
    ),
  controlExecutorCancel: (body: {
    executor_id: string;
    reason?: string;
    operator?: string;
  }) =>
    post<{ ok: boolean; executor_id: string; state: string }>("/executors/cancel", body),
  controlReconciliationReports: (params?: {
    account_id?: string;
    scope?: string;
    limit?: number;
    worst_window_s?: number;
  }) =>
    post<{
      reports: ReconciliationReport[];
      worst_recent: ReconciliationReport | null;
      filter: Record<string, unknown>;
    }>("/reconciliation/reports", params ?? {}),
  controlReconciliationRun: (body?: { account_id?: string; operator?: string }) =>
    post<{ report: ReconciliationReport; ok: boolean }>(
      "/reconciliation/run",
      body ?? {}
    ),
  controlProtectionsList: (account_id?: string) =>
    post<{ protections: Array<Record<string, unknown>> }>("/protections/list", {
      account_id,
    }),
  controlIncidents: (window_s = 3600) =>
    post<{ incidents: Incident[]; ts: string }>("/incidents", { window_s }),
  controlKillSwitchGet: () => get<KillSwitchView>("/kill_switch/get"),
  controlKillSwitchSet: (enabled: boolean, operator?: string) =>
    post<{ ok: boolean; kill_switch: boolean }>("/kill_switch/set", {
      enabled,
      operator,
    }),
  controlPromotionsList: (strategy_id: string, limit = 50) =>
    post<{ promotions: Array<Record<string, unknown>> }>(
      "/strategy/promotions/list",
      { strategy_id, limit }
    ),
  controlPromotionRequest: (body: {
    strategy_id: string;
    target?: string;
    operator?: string;
    notes?: string;
  }) =>
    post<{ ok: boolean; promotion: Record<string, unknown> }>(
      "/strategy/promotions/request",
      body
    ),
  controlPromotionApply: (promotion_id: string) =>
    post<{ ok: boolean; promotion: Record<string, unknown> }>(
      "/strategy/promotions/apply",
      { promotion_id }
    ),
  controlEvidenceRecord: (body: {
    strategy_id: string;
    kind: string;
    passed?: boolean;
    payload?: Record<string, unknown>;
    artifact_ref?: string;
    operator?: string;
    ttl_seconds?: number;
  }) =>
    post<{ ok: boolean; evidence: Record<string, unknown> }>(
      "/strategy/evidence/record",
      body
    ),

  // ---- Account roster CRUD ----
  accountsList: () =>
    get<{ accounts: AccountSummary[]; ts: number }>("/accounts/list"),
  accountsGet: (account_id: string) =>
    post<{ ok: boolean; account?: AccountSummary; error?: string }>(
      "/accounts/get",
      { account_id }
    ),
  accountsEquityCurve: (body: {
    account_id: string;
    since_ts?: number;
    limit?: number;
    bucket_seconds?: number;
  }) =>
    post<{
      ok: boolean;
      account_id?: string;
      points?: AccountEquityPoint[];
      count?: number;
      error?: string;
      detail?: string;
    }>("/accounts/equity_curve", body),
  accountsUpsert: (body: {
    id: string;
    venue: string;
    kind?: "cex" | "dex" | "chain" | "perp" | "futures" | string;
    mode: "paper" | "shadow" | "canary" | "live";
    status?: "active" | "read_only" | "disabled" | "quarantined";
    live_trading_enabled?: boolean;
    base_currency?: string;
    subaccount?: string;
    initial_balance_usd?: number;
    wallet_id?: string;
    provider_spec?: string;
    provider_config?: Record<string, unknown>;
    permissions?: Partial<AccountProfileView["permissions"]>;
    limits?: Record<string, number>;
    credentials?: Record<string, string>;
    operator?: string;
    // When ``apply: false`` the upsert is
    // staged as an account_roster_patch proposal that needs operator
    // approval. Default ``true`` keeps the legacy direct write path
    // for backwards compat.
    apply?: boolean;
    // Bypass the same venue+credential dedup
    // guard. Dashboard sets ``force=true`` (or
    // ``acknowledge_duplicate=true``) after the operator confirms
    // the duplicate_candidate warning surfaced by the upsert route.
    force?: boolean;
    acknowledge_duplicate?: boolean;
  }) =>
    post<{
      ok: boolean;
      applied?: boolean;
      account?: AccountSummary;
      proposal?: AccountProposalView;
      error?: string;
      detail?: string;
      duplicate_candidate?: {
        account_id: string;
        venue: string;
        mode: string;
        status: string;
        wallet_id?: string;
      } | null;
    }>("/accounts/upsert", body),
  accountsTestBalance: (body: {
    account_id?: string;
    id?: string;
    venue?: string;
    kind?: "cex" | "dex" | "chain" | "perp" | "futures" | string;
    mode?: "paper" | "shadow" | "canary" | "live";
    live_trading_enabled?: boolean;
    base_currency?: string;
    wallet_id?: string;
    provider_spec?: string;
    provider_config?: Record<string, unknown>;
    permissions?: Partial<AccountProfileView["permissions"]>;
    limits?: Record<string, number>;
    credentials?: Record<string, string>;
    initial_balance_usd?: number;
  }) =>
    post<{
      ok: boolean;
      account_id?: string;
      mode?: string;
      venue?: string;
      kind?: string;
      wallet_id?: string;
      snapshot?: AccountSnapshotData;
      error?: string;
      detail?: string;
    }>("/accounts/test_balance", body),
  accountsIntakeSchema: (body: {
    venue: string;
    account_kind?: string;
  }) =>
    post<{
      ok: boolean;
      venue?: string;
      account_kind?: string;
      provider_label?: string;
      credential_fields?: AccountCredentialField[];
      error?: string;
      detail?: string;
    }>("/accounts/intake/schema", body),
  accountsProposalsList: (state?: string) =>
    post<{
      ok: boolean;
      proposals: AccountProposalView[];
      count: number;
    }>("/accounts/proposals/list", state ? { state } : {}),
  accountsProposalGet: (proposal_id: string) =>
    post<{ ok: boolean; proposal?: AccountProposalView; error?: string; detail?: string }>(
      "/accounts/proposals/get",
      { proposal_id }
    ),
  accountsProposalApply: (body: {
    proposal_id: string;
    operator?: string;
    note?: string;
  }) =>
    post<{
      ok: boolean;
      proposal?: AccountProposalView;
      account?: AccountSummary;
      error?: string;
      detail?: string;
    }>("/accounts/proposals/apply", body),
  accountsProposalReject: (body: {
    proposal_id: string;
    operator?: string;
    note?: string;
  }) =>
    post<{ ok: boolean; proposal?: AccountProposalView; error?: string; detail?: string }>(
      "/accounts/proposals/reject",
      body
    ),
  accountsDelete: (body: { account_id: string; force?: boolean; operator?: string }) =>
    post<{
      ok: boolean;
      account_id?: string;
      error?: string;
      detail?: string;
      state?: { open_positions: number; active_executors: number; active_orders: number };
    }>("/accounts/delete", body),
  accountsQuarantine: (body: {
    account_id: string;
    status: "active" | "read_only" | "disabled" | "quarantined";
    reason?: string;
    operator?: string;
  }) =>
    post<{ ok: boolean; account?: AccountSummary; error?: string; detail?: string }>(
      "/accounts/quarantine",
      body
    ),
  accountsResetPaper: (body: {
    account_id: string;
    initial_balance_usd?: number;
    force?: boolean;
    operator?: string;
  }) =>
    post<{
      ok: boolean;
      account?: AccountSummary;
      error?: string;
      detail?: string;
      state?: { open_positions: number; active_executors: number; active_orders: number };
    }>("/accounts/reset_paper", body),
  walletConfigured: () =>
    get<{ bindings: WalletBinding[]; count: number }>("/wallet/configured"),
  // Manage HTTP auth headers on data-source
  // accounts. ``accountsHeadersList`` returns a masked metadata view
  // safe to render in the dashboard; ``accountsHeadersPatch`` merges
  // / removes headers and refuses any plaintext value that looks like
  // a secret (operator must store it via /security/secrets/put first
  // and reference it as ``vault://<name>``).
  accountsHeadersList: (body: { account_id: string }) =>
    post<{
      ok: boolean;
      account_id?: string;
      headers?: Array<{ key: string; value: string; kind: string }>;
      error?: string;
      detail?: string;
    }>("/accounts/headers/list", body),
  accountsHeadersPatch: (body: {
    account_id: string;
    headers: Record<string, string | null>;
    operator?: string;
    // When true the backend stashes any plaintext value into the
    // SecretVault and rewrites it to ``vault://<auto_name>`` before
    // persisting. ``Bearer <plaintext>`` becomes ``Bearer vault://...``.
    // Generated refs are echoed back as ``vaulted_refs``.
    auto_vault?: boolean;
  }) =>
    post<{
      ok: boolean;
      account?: AccountSummary;
      headers?: Array<{ key: string; value: string; kind: string }>;
      vaulted_refs?: Record<string, string>;
      error?: string;
      detail?: string;
    }>("/accounts/headers/patch", body),
  // Aggregated balances for every account bound to a wallet provider.
  walletPortfolio: (body: { account_id?: string } = {}) =>
    post<{
      ok: boolean;
      accounts?: Array<{
        account_id: string;
        wallet_id: string;
        venue: string;
        mode: string;
        ts: number;
        source: string;
        health: string;
        nav_usd: number;
        free_by_asset: Record<string, number>;
        cash_by_asset: Record<string, number>;
        meta: Record<string, unknown>;
      }>;
      count?: number;
      error?: string;
      detail?: string;
    }>("/wallet/portfolio", body),
  // Recent risk-gate decisions with fix
  // hints. Used by the strategy detail page to render a "why this
  // got rejected" panel that links to the right account / settings.
  riskEvaluations: (body: {
    strategy_id?: string;
    account_id?: string;
    decisions?: string[];
    limit?: number;
    since_seconds?: number;
  } = {}) =>
    post<{
      evaluations: RiskEvaluationRow[];
      ts: number;
      count?: number;
      error?: string;
    }>("/risk/evaluations", body),
  messagesList: (limit = 100) =>
    post<{ messages: MessageRecord[] }>("/messages/list", { limit }),
  messagesSend: (body: { channel: string; text: string; strategy_id?: string; session_id?: string }) =>
    post<Record<string, unknown>>("/messages/send", body),
  proposalsList: () =>
    post<{ proposals: EvolutionProposal[] }>("/evolution/proposals", {}),
  proposalDetail: (proposal_id: string) =>
    post<EvolutionProposalDetail>(
      `/evolution/proposals/${encodeURIComponent(proposal_id)}`,
      { proposal_id },
    ),
  proposalApprove: (proposal_id: string) =>
    post<EvolutionProposalDetail>(
      `/evolution/proposals/${encodeURIComponent(proposal_id)}/approve`,
      { proposal_id },
    ),
  proposalReject: (proposal_id: string, note?: string) =>
    post<EvolutionProposalDetail>(
      `/evolution/proposals/${encodeURIComponent(proposal_id)}/reject`,
      { proposal_id, note },
    ),
  proposalApply: (proposal_id: string) =>
    post<Record<string, unknown>>("/evolution/apply", { proposal_id }),
  proposalRollback: (proposal_id: string) =>
    post<Record<string, unknown>>("/evolution/rollback", { proposal_id }),
  proposalPostApplyObservation: (body: {
    proposal_id: string;
    status?: string;
    summary?: string;
    source?: string;
    evidence_refs?: string[];
    metrics?: Record<string, unknown>;
    backtest_result?: Record<string, unknown>;
    run_id?: string;
    operator?: string;
    metadata?: Record<string, unknown>;
  }) =>
    post<Record<string, unknown>>("/evolution/post_apply_observation", body),
  proposalDelete: (proposal_id: string, force = false) =>
    post<{
      ok?: boolean;
      deleted?: boolean;
      proposal_id?: string;
      error?: string;
      reason?: string;
      state?: string;
    }>("/evolution/proposals/delete", { proposal_id, force }),
  evolutionReflect: () =>
    post<Record<string, unknown>>("/evolution/reflect", {}),
  evolutionReflectionSchedule: () =>
    get<{ ok: boolean; schedule: EvolutionPeriodicReflectionSchedule }>(
      "/evolution/reflection_schedule",
    ),
  evolutionReflectionScheduleUpdate: (body: {
    enabled: boolean;
    time?: string;
    cron?: string;
    timezone?: string;
  }) =>
    post<{ ok: boolean; schedule: EvolutionPeriodicReflectionSchedule }>(
      "/evolution/reflection_schedule",
      body,
    ),
  evolutionReflectionRunNow: () =>
    post<Record<string, unknown>>(
      "/evolution/reflection_schedule/run_now",
      {},
    ),
  evolutionRank: (body: { strategy_id?: string; states?: string[]; persist?: boolean } = {}) =>
    post<Record<string, unknown>>("/evolution/rank", body),
  evolutionEvidence: (strategy_id: string) =>
    post<Record<string, unknown>>("/evolution/evidence", { strategy_id }),
  evolutionEvidenceResolve: (body: { ref?: string; refs?: string[] }) =>
    post<EvolutionEvidenceResolveEnvelope>("/evolution/evidence/resolve", body),
  evolutionSignals: (body: {
    source?: string;
    strategy_id?: string;
    severity?: string;
    kind?: string;
    limit?: number;
    refresh?: boolean;
  } = {}) =>
    post<EvolutionSignalsEnvelope>("/evolution/signals", body),
  evolutionEvents: (body: {
    strategy_id?: string;
    proposal_id?: string;
    outcome?: string;
    limit?: number;
  } = {}) =>
    post<EvolutionEventsEnvelope>("/evolution/events", body),
  evolutionTimeline: (body: {
    strategy_id?: string;
    query?: string;
    limit?: number;
  } = {}) =>
    post<EvolutionTimelineEnvelope>("/evolution/timeline", body),
  evolutionAssets: (body: {
    kind?: "gene" | "capsule";
    query?: string;
    strategy_id?: string;
    limit?: number;
    candidate_limit?: number;
  } = {}) =>
    post<EvolutionAssetsEnvelope>("/evolution/assets", body),
  evolutionAssetPromote: (candidate_id: string, operator = "dashboard") =>
    post<Record<string, unknown>>("/evolution/assets/promote", { candidate_id, operator }),
  evolutionAssetReject: (candidate_id: string, reason = "", operator = "dashboard") =>
    post<Record<string, unknown>>("/evolution/assets/reject", { candidate_id, reason, operator }),
  evolutionValidationRun: (body: { plan_id?: string; proposal_id?: string; dry_run?: boolean }) =>
    post<Record<string, unknown>>("/evolution/validation/run", body),

  memoryFiles: () =>
    post<{ ok: boolean; files: MemoryFileInfo[] }>("/skills/call", {
      skill_id: "memory", action: "list_memory_files",
      payload: {}, caller: "dashboard",
    }),
  memoryRecall: (payload: {
    scope: "global" | "strategy";
    file?: string;
    strategy_id?: string;
    max_chars?: number;
  }) =>
    post<MemoryRecallResult>("/skills/call", {
      skill_id: "memory", action: "recall",
      payload, caller: "dashboard",
    }),
  memoryRemember: (payload: {
    scope: "global" | "strategy";
    note: string;
    file?: string;
    strategy_id?: string;
  }) =>
    post<Record<string, unknown>>("/skills/call", {
      skill_id: "memory", action: "remember",
      payload, caller: "dashboard",
    }),

  marketVenues: () =>
    get<{ venues: { name: string; label: string; public: boolean }[] }>("/market/venues"),
  marketCandles: (body: { venue: string; market: string; interval: string; count?: number }) =>
    post<{
      venue: string; market: string; interval: string;
      count: number; candles: Candle[]; error?: string;
    }>("/market/candles", body),
  marketTicker: (body: { venue: string; market: string }) =>
    post<{
      venue: string; market: string;
      bid?: number; ask?: number; mid?: number; last?: number; error?: string;
    }>("/market/ticker", body),

  secretsList: () => post<{ refs: SecretRef[] }>("/security/secrets/list"),
  secretsPut: (body: { name: string; value: string; kind?: string; scope?: string[] }) =>
    post<{ ok: boolean; ref?: SecretRef; error?: string; detail?: string }>(
      "/security/secrets/put",
      body,
    ),
  secretsDelete: (name: string) =>
    post<{ ok: boolean; name?: string; error?: string }>("/security/secrets/delete", { name }),
  runtimeEnvList: () =>
    post<{ ok: boolean; env: RuntimeEnvVar[]; count: number; error?: string }>("/security/env/list"),
  runtimeEnvPut: (body: { name: string; value: string }) =>
    post<{ ok: boolean; env?: RuntimeEnvVar; error?: string; detail?: string }>(
      "/security/env/put",
      body,
    ),
  runtimeEnvDelete: (name: string) =>
    post<{ ok: boolean; name?: string; error?: string; detail?: string }>(
      "/security/env/delete",
      { name },
    ),
  networkProxy: () => get<NetworkProxyStatus>("/network/proxy"),
  networkProxySet: (body: Partial<NetworkProxyConfig>) =>
    post<NetworkProxyStatus>("/network/proxy", body),
  networkProxyTest: (body: { url?: string }) =>
    post<{
      ok: boolean;
      status?: number;
      elapsed_ms?: number;
      proxy?: Record<string, unknown>;
      body_preview?: string;
      error?: string;
    }>("/network/proxy/test", body),
  networkDashboard: () => get<NetworkDashboardStatus>("/network/dashboard"),
  networkDashboardSet: (body: { host?: string; port?: number | string }) =>
    post<NetworkDashboardStatus>("/network/dashboard", body),
  networkTunnels: () => get<NetworkTunnelsStatus>("/network/tunnels"),
  networkTunnelConfig: (body: TunnelConfigPayload) =>
    post<NetworkTunnelsStatus>("/network/tunnels/config", body),
  networkTunnelInstall: (body: { provider: string; approve: boolean }) =>
    post<{
      ok: boolean;
      provider?: string;
      already_installed?: boolean;
      command?: string[];
      returncode?: number;
      output_preview?: string;
      path?: string;
      error?: string;
      detail?: string;
    }>("/network/tunnels/install", body),
  networkTunnelStart: (provider: string) =>
    post<{
      ok: boolean;
      provider?: string;
      state?: Record<string, unknown>;
      external_urls?: string[];
      output_preview?: string;
      warning?: string;
      tailscale?: Record<string, unknown>;
      error?: string;
      detail?: string;
    }>("/network/tunnels/start", { provider }),
  networkTunnelStop: (provider: string) =>
    post<{ ok: boolean; provider?: string; error?: string; detail?: string }>(
      "/network/tunnels/stop",
      { provider },
    ),

  gatewayPlatforms: () =>
    get<{ platforms: GatewayPlatformSpec[] }>("/gateway/platforms"),
  gatewayStatus: () =>
    get<Record<string, unknown>>("/gateway/status"),
  gatewayConfig: () =>
    get<GatewayConfigEnvelope>("/gateway/config"),
  gatewayConfigUpsert: (body: GatewayUpsertRequest) =>
    post<{
      ok: boolean;
      channel?: GatewayChannelConfig;
      config?: GatewayConfigEnvelope;
      startup?: Record<string, unknown> | null;
      error?: string;
    }>("/gateway/config/upsert", body),
  gatewayConfigDelete: (channel: string) =>
    post<{
      ok: boolean;
      channel?: string;
      deleted?: boolean;
      config?: GatewayConfigEnvelope;
      error?: string;
    }>("/gateway/config/delete", { channel }),
  gatewayEvents: (params: { since?: number; channel?: string; platform?: string; limit?: number } = {}) => {
    const query = new URLSearchParams();
    if (params.since) query.set("since", String(params.since));
    if (params.channel) query.set("channel", params.channel);
    if (params.platform) query.set("platform", params.platform);
    if (params.limit) query.set("limit", String(params.limit));
    const suffix = query.toString();
    const path = suffix ? `/gateway/events?${suffix}` : "/gateway/events";
    return get<GatewayEventsResponse>(path);
  },
  gatewayConfigTest: (body: {
    channel: string;
    text?: string;
    mode?: "agent" | "send_only" | "outbound";
    chat_id?: string;
    user_id?: string;
  }) =>
    post<{
      ok: boolean;
      channel?: string;
      mode?: "agent" | "send_only" | "outbound" | string;
      agent?: { turn_id?: string; session_id?: string; session_key?: string };
      reply_text?: string;
      delivery?: Record<string, unknown>;
      error?: string;
      detail?: string;
    }>("/gateway/config/test", body),
  gatewayTelegramDiagnose: (body: { channel?: string; chat_id?: string } = {}) =>
    post<{
      ok: boolean;
      channel?: string;
      error?: string;
      hint?: string;
      configured?: { bot_token_ref?: boolean; chat_id?: string | null };
      bot?: { ok?: boolean; status?: number; error?: string;
              bot?: Record<string, unknown> };
      chat?: { ok?: boolean; status?: number; error?: string;
              chat?: Record<string, unknown> };
      polling?: { alive?: boolean; disabled_by_env?: boolean; offset?: unknown };
    }>("/gateway/telegram/diagnose", body),

  walletProviders: () =>
    post<{ providers: WalletProviderInfo[]; active: string | null }>("/wallet/providers"),
  walletStatus: (provider?: string) =>
    post<{ provider: string; ready: boolean; reason?: string; missing?: string[];
            active?: boolean; install_hint?: string }>(
      "/wallet/status",
      { provider },
    ),
  walletUse: (provider: string) =>
    post<{ ok: boolean; provider: string | null }>("/wallet/configure", { provider }),
  walletConfigure: (provider: string, config: Record<string, unknown>) =>
    post<{ ok: boolean; provider: string; config: Record<string, unknown>; bindings?: WalletBinding[] }>(
      "/wallet/configure",
      { provider, config },
    ),
  walletConfigureBinding: (body: {
    provider: string;
    wallet_id: string;
    label?: string;
    config: Record<string, unknown>;
    activate?: boolean;
    operator?: string;
    // When ``auto_create_account`` is true the
    // backend also writes an accounts.yml row of ``kind=chain`` tied
    // to this wallet. ``account_mode`` controls paper/shadow/canary/
    // live; the new account is created with ``place_order=false`` so
    // the operator still has to opt in to live trading explicitly.
    auto_create_account?: boolean;
    account_mode?: "paper" | "shadow" | "canary" | "live";
    account_id_hint?: string;
    initial_balance_usd?: number;
    balances?: Array<{
      chain?: string;
      address?: string;
      token?: string;
      symbol?: string;
      decimals?: number;
    }>;
  }) =>
    post<{
      ok: boolean;
      provider?: string;
      wallet_id?: string;
      label?: string;
      config?: Record<string, unknown>;
      bindings?: WalletBinding[];
      stored_refs?: SecretRef[];
      account?: {
        account_id: string;
        mode: string;
        created: boolean;
        wallet_id?: string;
        kind?: string;
      } | null;
      account_warning?: {
        error: string;
        detail?: string;
        attempted_mode?: string;
      } | null;
      error?: string;
      detail?: string;
    }>("/wallet/configure", body),
  walletInstallHint: (provider: string) =>
    post<{ provider: string; install_hint: string; runtime: string; links: Record<string, string> }>(
      "/wallet/install_hint",
      { provider },
    ),
  walletCredentialSchema: (provider: string) =>
    post<{
      ok: boolean;
      provider: string;
      label: string;
      runtime: string;
      install_command: string;
      install_alternatives: Array<Record<string, unknown>>;
      install_hint: string;
      auth_flows?: WalletAuthFlow[];
      auth_install_state?: WalletAuthInstallState;
      credential_fields: AccountCredentialField[];
      advanced_credential_fields?: AccountCredentialField[];
      error?: string;
      known?: string[];
    }>("/wallet/credential_schema", { provider }),
  walletInstall: (body: { provider: string; approve?: boolean; command?: string }) =>
    post<{
      ok: boolean;
      provider?: string;
      skipped?: boolean;
      reason?: string;
      error?: string;
      detail?: string;
      install_command?: string;
      install_hint?: string;
      result?: Record<string, unknown>;
      configure_patch?: Record<string, unknown>;
    }>("/wallet/install", body),
  walletAuthStart: (body: {
    provider: string;
    approve?: boolean;
    install?: boolean;
    email?: string;
    locale?: string;
    wallet_id?: string;
    label?: string;
    config?: Record<string, unknown>;
    activate?: boolean;
    operator?: string;
    create_binding?: boolean;
    // Same auto-create knobs as
    // /wallet/configure; the auth flow can drop a chain account
    // on first successful login.
    auto_create_account?: boolean;
    account_mode?: "paper" | "shadow" | "canary" | "live";
    account_id_hint?: string;
    initial_balance_usd?: number;
    balances?: Array<Record<string, unknown>>;
  }) => post<WalletAuthStartResponse>("/wallet/auth/start", body),
  walletAuthVerify: (body: {
    provider: string;
    otp?: string;
    code?: string;
    deviceCode?: string;
    device_code?: string;
    qrCodeId?: string;
    qr_code_id?: string;
    wallet_id?: string;
    label?: string;
    config?: Record<string, unknown>;
    activate?: boolean;
    operator?: string;
    create_binding?: boolean;
    auto_create_account?: boolean;
    account_mode?: "paper" | "shadow" | "canary" | "live";
    account_id_hint?: string;
    initial_balance_usd?: number;
    balances?: Array<Record<string, unknown>>;
  }) => post<WalletAuthVerifyResponse>("/wallet/auth/verify", body),
  walletAuthStatus: (
    provider: string,
    body: {
      wallet_id?: string;
      label?: string;
      config?: Record<string, unknown>;
      activate?: boolean;
      operator?: string;
      create_binding?: boolean;
    } = {},
  ) =>
    post<WalletAuthVerifyResponse>("/wallet/auth/status", { provider, ...body }),

  exchangeProviders: () =>
    post<{
      providers: Array<{
        id: string;
        label: string;
        kind: string;
        runtime: string;
        aliases: string[];
        install_hint: string;
        links: Record<string, string>;
        description: string;
        supports: Record<string, boolean>;
      }>;
      ccxt_supported: string[];
      count: number;
    }>("/exchanges/providers"),

  exchangeAuthorScaffoldCcxt: (body: {
    venue_id: string;
    ccxt_id: string;
    label?: string;
    notes?: string;
  }) =>
    post<{
      proposal_id: string;
      venue_id: string;
      path: string;
      state: string;
    }>("/skills/call", {
      skill_id: "exchange_author",
      action: "scaffold_ccxt",
      payload: body,
    }),

  exchangeAuthorScaffoldHttp: (body: {
    venue_id: string;
    kind: "cex" | "dex" | "prediction_market" | "chain";
    base_url?: string;
    docs_url?: string;
    label?: string;
    install_hint?: string;
    endpoints?: Record<string, Record<string, unknown>>;
    notes?: string;
  }) =>
    post<{
      proposal_id: string;
      venue_id: string;
      path: string;
      state: string;
    }>("/skills/call", {
      skill_id: "exchange_author",
      action: "scaffold_http",
      payload: body,
    }),

  exchangeAuthorApprove: (venue_id: string) =>
    post<{
      venue_id: string;
      proposal_id: string;
      path: string;
      state: string;
    }>("/skills/call", {
      skill_id: "exchange_author",
      action: "approve_pending",
      payload: { venue_id },
    }),

  // LLM operator control plane
  llmProviders: () =>
    get<{
      count: number;
      providers: Array<{
        provider: string;
        adapter_present: boolean;
        base_url?: string | null;
        configured_tiers: string[];
        has_key_ref: boolean;
        ready: boolean;
      }>;
    }>("/llm/providers"),
  llmTiers: () =>
    get<{
      count: number;
      tiers: Array<{
        tier: string;
        provider?: string | null;
        model?: string | null;
        base_url?: string | null;
        has_key_ref: boolean;
      }>;
    }>("/llm/tiers"),
  llmConfig: () =>
    get<{
      ok: boolean;
      default_tier: string;
      intent_tier?: string;
      provider_profiles?: LlmProviderProfile[];
      tiers: LlmTierConfig[];
      // Canonical list of allowed reasoning_effort values, in order
      // from "off" → "lightest think" → … → "extra high". Tier rows
      // should render their dropdown from this list so the UI stays
      // in sync with backend changes.
      reasoning_levels?: string[];
      error?: string;
    }>("/llm/config"),
  // ---- Memory write rules + activity stream ------------------
  memoryWriteRulesGet: () =>
    get<{
      categories: Array<{
        id: string;
        name: string;
        description: string;
        default_target_files: string[];
        default_retention_days: number;
        default_max_entries: number;
        default_dedupe: string;
        default_enabled: boolean;
      }>;
      dedupe_strategies: string[];
      rules: Record<string, MemoryWriteRuleConfig>;
      warnings: string[];
    }>("/memory/write_rules"),
  memoryWriteRulesSet: (rules: Record<string, Partial<MemoryWriteRuleConfig>>) =>
    post<{
      ok: boolean;
      categories: Array<{ id: string; name: string }>;
      dedupe_strategies: string[];
      rules: Record<string, MemoryWriteRuleConfig>;
      warnings: string[];
      error?: string;
    }>("/memory/write_rules", { rules }),
  memoryActivity: (body?: { limit?: number; kinds?: string[]; category?: string }) =>
    post<{
      events: MemoryActivityEvent[];
      stats: {
        write_ok: number;
        write_skipped: number;
        search: number;
        last_event_ts?: string;
      };
    }>("/memory/activity", body || {}),
  memoryProviders: () =>
    get<{
      builtin: MemoryProviderView | null;
      external: MemoryProviderView | null;
      available_external: MemoryProviderView[];
    }>("/memory/providers"),
  memoryExternalConfig: () =>
    get<MemoryExternalConfig>("/memory/external/config"),
  memoryExternalConfigSet: (body: {
    enabled?: boolean;
    provider?: string;
    agentmemory?: Partial<MemoryExternalConfig["agentmemory"]>;
  }) =>
    post<MemoryExternalConfig & { ok?: boolean; error?: string }>(
      "/memory/external/config",
      body,
    ),
  memoryExternalInstall: () =>
    post<{
      ok: boolean;
      manual: boolean;
      provider: string;
      dependency_available: boolean;
      commands: string[];
      health_url: string;
      viewer_url: string;
      docs_url: string;
      note: string;
    }>("/memory/external/install", {}),
  // Actually run `npm install -g @agentmemory/agentmemory` (or whatever the
  // configured install_command targets). Keeps the same install surface as
  // parity between the two backends.
  memoryExternalInstallRun: () =>
    post<{
      ok: boolean;
      manual: boolean;
      cmd?: string[];
      package?: string;
      returncode?: number;
      stdout_tail?: string;
      stderr_tail?: string;
      dependency_available?: boolean;
      note?: string;
      error?: string;
      detail?: string;
    }>("/memory/external/install/run", {}),
  // Unified recall probe — runs across builtin / memsearch / agentmemory
  // backends and returns per-backend availability + previews. Powers the
  // "Test recall" button on the Selected backend settings card.
  memoryTest: (body: { query?: string; limit?: number } = {}) =>
    post<{
      ok: boolean;
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
    }>("/memory/test", body),
  memoryNotebookList: () =>
    get<{
      targets: string[];
      agent: MemoryNotebookSnapshot;
      operator: MemoryNotebookSnapshot;
    }>("/memory/notebook"),
  memoryNotebookMutate: (body: {
    action: "add" | "replace" | "remove";
    target: "agent" | "operator";
    content?: string;
    old_text?: string;
  }) =>
    post<{
      ok: boolean;
      target: string;
      entries: string[];
      used_chars: number;
      char_limit: number;
      usage_pct: number;
      message?: string;
      error?: string;
      extra?: Record<string, unknown>;
    }>("/memory/notebook", body),
  llmConfigSet: (body: {
    default_tier?: string;
    intent_tier?: string;
    providers?: LlmProviderProfile[];
    tiers?: LlmTierConfig[];
  }) =>
    post<{
      ok: boolean;
      default_tier: string;
      intent_tier?: string;
      provider_profiles?: LlmProviderProfile[];
      tiers: LlmTierConfig[];
      reasoning_levels?: string[];
      error?: string;
    }>("/llm/config", body),
  // OAuth-login provider directory + per-provider status. The
  // backend's ``OAUTH_PROVIDERS`` map drives the response so adding a
  // new OAuth provider is a backend-only change.
  llmOauthProviders: () =>
    get<{
      ok: boolean;
      providers: Array<{
        id: string;
        display_name: string;
        cli_name: string;
        cli_paths: string[];
        env_keys: string[];
        description: string;
      }>;
      statuses: Record<string, OAuthProviderStatus>;
    }>("/llm/oauth/providers"),
  llmOauthStatus: (provider?: string) =>
    post<{
      ok: boolean;
      statuses: Record<string, OAuthProviderStatus>;
      status?: OAuthProviderStatus;
    }>("/llm/oauth/status", provider ? { provider } : {}),
  llmOauthImport: (provider: string) =>
    post<{
      ok: boolean;
      provider: string;
      imported?: boolean;
      source?: string;
      status?: OAuthProviderStatus;
      error?: string;
    }>("/llm/oauth/import", { provider }),
  llmOauthPaste: (body: { provider: string; token: string }) =>
    post<{
      ok: boolean;
      status?: OAuthProviderStatus;
      error?: string;
    }>("/llm/oauth/paste", body),
  llmOauthRevoke: (provider: string) =>
    post<{
      ok: boolean;
      provider: string;
      status?: OAuthProviderStatus;
      error?: string;
    }>("/llm/oauth/revoke", { provider }),
  // Per-provider login directive — tells the dashboard which UI affordance
  // to render (CLI command vs Start-device-code button vs paste-only).
  // Returns the backend-defined OAuth login directive.
  llmOauthLoginDirective: (provider: string) =>
    post<{
      ok: boolean;
      directive?: {
        provider: string;
        flow: "cli" | "device_code" | "paste";
        command?: string;
        verification_uri?: string;
        instruction: string;
      };
      error?: string;
    }>("/llm/oauth/login_directive", { provider }),
  // Device-code flow (today: Copilot). The dashboard calls
  // ``start`` once, shows the user_code + verification_uri to the
  // operator, then polls ``poll`` every ``interval`` seconds.
  llmOauthDeviceCodeStart: (provider: string) =>
    post<{
      ok: boolean;
      provider?: string;
      device_code?: string;
      user_code?: string;
      verification_uri?: string;
      verification_uri_complete?: string;
      interval?: number;
      expires_in?: number;
      expires_at?: number;
      error?: string;
    }>("/llm/oauth/device_code/start", { provider }),
  llmOauthDeviceCodePoll: (body: { provider: string; device_code: string }) =>
    post<{
      ok: boolean;
      status?: "pending" | "slow_down" | "ok" | "error";
      interval?: number;
      provider?: string;
      record?: Record<string, unknown>;
      error?: string;
    }>("/llm/oauth/device_code/poll", body),
  // Provider metadata catalogue. Returned by
  // ``nerya.llm.provider_catalog.PROVIDER_CATALOG`` and is the canonical
  // source of truth for provider id → API mode → default base URL.
  // The dashboard hydrates the Add-Provider form from this so a new
  // backend provider becomes available without a frontend rebuild.
  llmCatalog: () =>
    get<{
      ok: boolean;
      reasoning_levels?: string[];
      providers: Array<{
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
      }>;
      error?: string;
    }>("/llm/catalog"),
  llmModels: () =>
    get<{
      updated_at?: string | null;
      providers: Record<string, Array<Record<string, unknown>>>;
      errors: Record<string, string>;
      counts: Record<string, number>;
    }>("/llm/models"),
  llmModelsRefresh: (body?: { vault_passphrase?: string }) =>
    post<{
      updated_at?: string | null;
      providers: Record<string, Array<Record<string, unknown>>>;
      errors: Record<string, string>;
      counts?: Record<string, number>;
    }>("/llm/models/refresh", body ?? {}),
  llmModelsDiscover: (body: {
    provider: string;
    base_url?: string;
    provider_key?: string;
    provider_key_ref?: string;
    vault_passphrase?: string;
    api_mode?: string;
  }) =>
    post<{
      ok: boolean;
      provider?: string;
      base_url?: string;
      provider_key_ref?: string;
      models?: Array<Record<string, unknown>>;
      count?: number;
      error?: string;
      detail?: string;
    }>("/llm/models/discover", body),
  llmModelsImport: (body: {
    provider: string;
    base_url?: string;
    models: Array<Record<string, unknown> | string>;
  }) =>
    post<{
      ok: boolean;
      provider?: string;
      imported?: number;
      updated_at?: string | null;
      providers: Record<string, Array<Record<string, unknown>>>;
      errors: Record<string, string>;
      counts?: Record<string, number>;
      error?: string;
    }>("/llm/models/import", body),
  llmValidateAssignment: (body: { provider: string; model: string }) =>
    post<{
      provider: string;
      model: string;
      known: boolean;
      sample_models: Array<Record<string, unknown>>;
    }>("/llm/models/validate", body),
  llmRoutingGet: () =>
    get<{
      default: Record<string, unknown>;
      per_provider: Record<string, Record<string, unknown>>;
    }>("/llm/provider_routing"),
  llmRoutingSet: (body: {
    default?: Record<string, unknown>;
    per_provider?: Record<string, Record<string, unknown>>;
  }) =>
    post<{
      default: Record<string, unknown>;
      per_provider: Record<string, Record<string, unknown>>;
    }>("/llm/provider_routing", body),

  memoryVectorStatus: () =>
    get<MemoryVectorStatus>("/memory/vector/status"),
  memoryVectorConfig: (body: {
    enabled?: boolean;
    watch_enabled?: boolean;
    paths?: string[];
    install_package?: string;
    embedding?: Partial<{
      provider: string;
      model: string;
      base_url: string;
      api_key_ref: string;
      // When set, the server stores the plaintext value in the
      // SecretVault and rewrites ``api_key_ref`` to a fresh
      // ``vault://...`` reference. Lets operators paste a key
      // directly without having to register a vault entry first.
      api_key_plain: string;
    }>;
    milvus?: Partial<{
      uri: string;
      token: string;
      collection: string;
    }>;
  }) => post<MemoryVectorStatus>("/memory/vector/config", body),
  memoryVectorInstall: () =>
    post<MemoryVectorStatus & {
      returncode?: number;
      stdout_tail?: string;
      stderr_tail?: string;
    }>("/memory/vector/install", {}),
  memoryVectorReindex: (body: { force?: boolean } = {}) =>
    post<Record<string, unknown> & { ok: boolean; error?: string }>(
      "/memory/vector/reindex",
      body,
    ),
  memoryVectorSearch: (body: { query: string; top_k?: number }) =>
    post<{
      ok: boolean;
      query?: string;
      results?: Array<Record<string, unknown>>;
      count?: number;
      error?: string;
    }>("/memory/vector/search", body),
  memoryVectorStart: () =>
    post<MemoryVectorStatus & { started?: boolean; pid?: number; log_path?: string }>(
      "/memory/vector/start",
      {},
    ),
  memoryVectorStop: () =>
    post<MemoryVectorStatus & { stopped?: boolean }>("/memory/vector/stop", {}),

  // ---------------------------------------------------------------
  // Web search engines (multi-engine, multi-key rotation)
  //
  // Backend: ``nerya.api.routes_search``. The dashboard manages the
  // engine *chain* + per-engine *key list*; per-engine keys are
  // persisted to the SecretVault under ``vault://search.<engine>.keys``
  // by default.
  // ---------------------------------------------------------------
  searchEnginesStatus: () =>
    get<SearchEnginesStatus>("/search/engines/status"),
  searchEnginesConfig: (body: SearchEnginesConfigRequest) =>
    post<SearchEnginesStatus>("/search/engines/config", body),
  searchEnginesTest: (body: { query?: string; engine?: string; max_results?: number } = {}) =>
    post<{
      ok: boolean;
      engine?: string | null;
      elapsed_ms?: number;
      result?: unknown;
      error?: string;
      stderr_tail?: string;
      returncode?: number;
    }>("/search/engines/test", body),

  searchSearxngStatus: () =>
    get<SearchSearxngStatus>("/search/engines/searxng/status"),
  searchSearxngDeploy: (body: SearchSearxngDeployRequest = {}) =>
    post<SearchSearxngDeployResponse>("/search/engines/searxng/deploy", body),
  searchSearxngTeardown: (body: { remove?: boolean } = {}) =>
    post<SearchSearxngDeployResponse>("/search/engines/searxng/teardown", body),

  // ---------------------------------------------------------------
  // Headless browser engines (camofox / cloakbrowser / lightpanda / obscura)
  // ---------------------------------------------------------------
  browsersStatus: () => get<BrowsersStatus>("/browsers/status"),
  browsersRegistry: () =>
    get<{ ok: boolean; engines: BrowserSpec[] }>("/browsers/registry"),
  browsersSelect: (name: string) =>
    post<BrowsersStatus>("/browsers/select", { name }),
  browsersConfigure: (body: BrowsersConfigureRequest) =>
    post<BrowsersStatus>("/browsers/configure", body),
  browsersInstall: (name: string) =>
    post<BrowserActionResponse>("/browsers/install", { name }),
  browsersUninstall: (name: string) =>
    post<BrowserActionResponse>("/browsers/uninstall", { name }),
  browsersProbe: (body: { name?: string; url?: string; timeout_s?: number }) =>
    post<BrowserProbeResponse>("/browsers/probe", body),

  // ---------------------------------------------------------------
  // Live browser sessions (dashboard-driven navigation)
  // ---------------------------------------------------------------
  browserSessionStart: (body: BrowserSessionStartRequest) =>
    post<BrowserSessionEnvelope>("/browsers/session/start", body),
  browserSessionNavigate: (body: BrowserSessionNavigateRequest) =>
    post<BrowserSessionEnvelope>("/browsers/session/navigate", body),
  browserSessionSnapshot: (body: { session_id: string; timeout_s?: number }) =>
    post<BrowserSessionEnvelope>("/browsers/session/snapshot", body),
  browserSessionScreenshot: (body: {
    session_id: string;
    url?: string;
    full_page?: boolean;
    timeout_s?: number;
  }) =>
    post<BrowserSessionScreenshotResponse>(
      "/browsers/session/screenshot",
      body,
    ),
  browserSessionClose: (session_id: string) =>
    post<{ ok: boolean; removed: boolean; session_id: string }>(
      "/browsers/session/close",
      { session_id },
    ),
  browserSessionList: () =>
    get<BrowserSessionListResponse>("/browsers/session/list"),
  browserSessionGet: (session_id: string) =>
    get<BrowserSessionRecord>(
      `/browsers/session/get?session_id=${encodeURIComponent(session_id)}`,
    ),
  browserSessionCdpOpen: (body: BrowserSessionStartRequest) =>
    post<BrowserSessionEnvelope & { current_url?: string }>(
      "/browsers/session/cdp_open",
      body,
    ),
  browserSessionCdpAction: (body: BrowserCdpActionRequest) =>
    post<BrowserCdpActionResponse>("/browsers/session/cdp_action", body),
  browserSessionCdpScreenshot: (body: {
    session_id: string;
    full_page?: boolean;
    timeout_s?: number;
  }) =>
    post<BrowserSessionScreenshotResponse>(
      "/browsers/session/cdp_screenshot",
      body,
    ),
  browserSessionCdpClose: (session_id: string) =>
    post<{ ok: boolean; closed: boolean; session_id: string; error?: string }>(
      "/browsers/session/cdp_close",
      { session_id },
    ),

  // ---------------------------------------------------------------
  // Data-source API keys (Financial Datasets, equities backbone)
  // ---------------------------------------------------------------
  financialDatasetsStatus: () =>
    get<FinancialDatasetsStatus>("/data/financial_datasets/status"),
  financialDatasetsSetKeys: (body: FinancialDatasetsKeysRequest) =>
    post<FinancialDatasetsStatus>("/data/financial_datasets/keys", body),

  // Triggers & schedules operator plane
  triggerRoutes: () =>
    get<{ routes: TriggerRoute[] } | TriggerRoute[]>("/triggers/routes"),
  triggerRouteAdd: (route: TriggerRoute) =>
    post<{ ok: boolean; route: TriggerRoute }>("/triggers/routes/add", route),
  triggerRouteUpdate: (id: string, patch: Partial<TriggerRoute>) =>
    post<{ ok: boolean; route: TriggerRoute }>(
      "/triggers/routes/update",
      { id, ...patch },
    ),
  triggerRoutePause: (id: string, paused: boolean) =>
    post<{ ok: boolean }>("/triggers/routes/pause", { id, paused }),
  triggerRouteRemove: (id: string) =>
    post<{ ok: boolean }>("/triggers/routes/remove", { id }),
  triggerDryRun: (body: {
    source: string;
    kind: string;
    payload?: unknown;
    target?: string;
    strategy_id?: string;
  }) => post<Record<string, unknown>>("/triggers/dry_run", body),

  scheduleList: () =>
    get<{ schedules: TriggerSchedule[] }>("/triggers/schedules"),
  triggerSchedules: () =>
    get<{ schedules: TriggerSchedule[] }>("/triggers/schedules"),
  scheduleAdd: (entry: TriggerSchedule) =>
    post<{ ok: boolean; schedule: TriggerSchedule }>(
      "/triggers/schedules/add",
      entry,
    ),
  scheduleAddFromText: (
    text: string,
    defaults?: Partial<TriggerSchedule>,
  ) =>
    post<{
      ok: boolean;
      schedule: TriggerSchedule;
      parser?: "deterministic" | "llm";
      parsed_from_text?: string;
    }>("/triggers/schedules/add_from_text", { text, defaults }),
  scheduleUpdate: (id: string, patch: Partial<TriggerSchedule>) =>
    post<{ ok: boolean; schedule: TriggerSchedule }>(
      "/triggers/schedules/update",
      { id, ...patch },
    ),
  schedulePause: (id: string) =>
    post<{ ok: boolean; schedule: TriggerSchedule }>(
      "/triggers/schedules/pause",
      { id },
    ),
  scheduleResume: (id: string) =>
    post<{ ok: boolean; schedule: TriggerSchedule }>(
      "/triggers/schedules/resume",
      { id },
    ),
  scheduleRunNow: (id: string) =>
    post<{
      ok: boolean;
      schedule_id?: string;
      fired?: boolean;
      event_id?: string;
      session?: Record<string, unknown>;
      sessions?: Array<Record<string, unknown>>;
      script?: Record<string, unknown>;
      result?: Record<string, unknown>;
      error?: string;
    }>(
      "/triggers/schedules/run_now",
      { id },
    ),
  scheduleRemove: (id: string) =>
    post<{ ok: boolean }>("/triggers/schedules/remove", { id }),
  scheduleStatus: (id: string) =>
    get<{ ok: boolean; schedules: TriggerSchedule[] }>(
      `/triggers/schedules/status?id=${encodeURIComponent(id)}`,
    ),
  scheduleStatuses: () =>
    get<{ ok: boolean; schedules: TriggerSchedule[] }>(
      "/triggers/schedules/status",
    ),
  scheduleTick: () =>
    post<{ ok: boolean; fired: Array<Record<string, unknown>> }>("/triggers/schedules/tick"),

  // Agent / session operator plane
  // ---------------------------------------------------------------
  // Strategy runtime control plane
  //
  // These wrap ``/strategies/runtime/*`` exposed by
  // ``nerya.api.routes_strategies_runtime``. The legacy
  // ``strategiesAll`` / ``strategyGet`` calls above still target the
  // older ``trading.strategies.Strategy`` rows so the existing
  // dashboard surfaces keep working during the migration.
  // ---------------------------------------------------------------
  strategyRuntimeList: () =>
    get<{ ok: boolean; strategies: StrategyPackageSummary[] }>(
      "/strategies/runtime/list",
    ),
  strategyRuntimeGet: (strategy_id: string) =>
    get<{ ok: boolean } & StrategyPackageDetail>(
      `/strategies/runtime/get?strategy_id=${encodeURIComponent(strategy_id)}`,
    ),
  strategyRuntimeGenerate: (req: StrategyGenerationRequest, validate = true) =>
    post<StrategyGenerationResponse>("/strategies/runtime/generate", {
      ...req,
      validate,
    }),
  strategyRuntimeValidate: (body: {
    strategy_id?: string;
    proposal_id?: string;
  }) =>
    post<{ ok: boolean } & StrategyValidationReport>(
      "/strategies/runtime/validate",
      body,
    ),
  strategyRuntimePromote: (proposal_id: string, note?: string) =>
    post<StrategyRuntimePromotionResult>(
      "/strategies/runtime/promote",
      { proposal_id, note },
    ),
  strategyRuntimeRunTick: (body: {
    strategy_id: string;
    trigger_payload?: Record<string, unknown>;
    trigger_event_id?: string;
    operator?: string;
    note?: string;
    mode_override?: "paper" | "shadow" | "live";
  }) =>
    post<{ ok: boolean } & StrategyRunRecord>(
      "/strategies/runtime/run_tick",
      body,
    ),
  strategyRuntimeSchedule: (strategy_id: string) =>
    post<{ ok: boolean } & StrategyScheduleStatus>(
      "/strategies/runtime/schedule",
      { strategy_id },
    ),
  strategyRuntimeScheduleStatus: (strategy_id: string) =>
    get<{ ok: boolean } & StrategyScheduleStatus>(
      `/strategies/runtime/schedule_status?strategy_id=${encodeURIComponent(
        strategy_id,
      )}`,
    ),
  strategyRuntimePause: (strategy_id: string) =>
    post<{ ok: boolean } & StrategyScheduleStatus>(
      "/strategies/runtime/pause",
      { strategy_id },
    ),
  strategyRuntimeResume: (strategy_id: string) =>
    post<{ ok: boolean } & StrategyScheduleStatus>(
      "/strategies/runtime/resume",
      { strategy_id },
    ),
  strategyRuntimeKillSwitch: (body: {
    strategy_id: string;
    action: "set" | "clear" | "get";
    reason?: string;
    by?: string;
  }) =>
    post<{ ok: boolean; strategy_id: string; state: KillSwitchState }>(
      "/strategies/runtime/kill_switch",
      body,
    ),
  strategyRuntimeRuns: (strategy_id: string, limit = 50) =>
    get<{ ok: boolean; strategy_id: string; count: number; runs: StrategyRunRecord[] }>(
      `/strategies/runtime/runs?strategy_id=${encodeURIComponent(
        strategy_id,
      )}&limit=${encodeURIComponent(String(limit))}`,
    ),
  strategyRuntimeStatus: (strategy_id: string) =>
    get<StrategyStatusEnvelope>(
      `/strategies/runtime/status?strategy_id=${encodeURIComponent(
        strategy_id,
      )}`,
    ),
  strategyRuntimeWorkspace: (strategy_id: string, runs_limit = 50) =>
    get<StrategyWorkspaceEnvelope>(
      `/strategies/runtime/workspace?strategy_id=${encodeURIComponent(
        strategy_id,
      )}&runs_limit=${encodeURIComponent(String(runs_limit))}`,
    ),
  strategyRuntimeTuningGenerate: (req: StrategyTuningGenerationRequest) =>
    post<StrategyTuningGenerationResponse>(
      "/strategies/runtime/tuning/generate",
      req,
    ),
  strategyRuntimeTuningSchedule: (strategy_id: string) =>
    post<{ ok: boolean } & StrategyScheduleStatus>(
      "/strategies/runtime/tuning/schedule",
      { strategy_id },
    ),
  strategyRuntimeTuningPause: (strategy_id: string) =>
    post<{ ok: boolean } & StrategyScheduleStatus>(
      "/strategies/runtime/tuning/pause",
      { strategy_id },
    ),
  strategyRuntimeTuningResume: (strategy_id: string) =>
    post<{ ok: boolean } & StrategyScheduleStatus>(
      "/strategies/runtime/tuning/resume",
      { strategy_id },
    ),
  strategyRuntimeTuningRun: (body: {
    strategy_id: string;
    dry_run?: boolean;
    operator?: string;
    note?: string;
    trigger_event_id?: string;
  }) =>
    post<{ ok: boolean } & StrategyTuningRunResult>(
      "/strategies/runtime/tuning/run",
      body,
    ),
  strategyRuntimeTuningStatus: (strategy_id: string, lookback_runs = 200) =>
    get<StrategyTuningStatusEnvelope>(
      `/strategies/runtime/tuning/status?strategy_id=${encodeURIComponent(
        strategy_id,
      )}&lookback_runs=${encodeURIComponent(String(lookback_runs))}`,
    ),
  strategyRuntimeTuningSnapshot: (strategy_id: string, lookback_runs = 200) =>
    get<{ ok: boolean; strategy_id: string; snapshot: StrategyPerformanceSnapshot }>(
      `/strategies/runtime/tuning/snapshot?strategy_id=${encodeURIComponent(
        strategy_id,
      )}&lookback_runs=${encodeURIComponent(String(lookback_runs))}`,
    ),

  // ---------------------------------------------------------------------
  // Operator BFF (27 of dashboard BFF surface)
  //
  // These endpoints aggregate raw runtime data into product-shaped
  // envelopes the dashboard can render directly. Every response wears
  // ``OperatorEnvelope`` so callers handle status / severity uniformly.
  // ---------------------------------------------------------------------

  operatorNav: () => get<OperatorNavEnvelope>("/operator/nav"),
  operatorOverview: () => get<OperatorOverviewEnvelope>("/operator/overview"),
  setupReadiness: () => get<SetupReadinessEnvelope>("/setup/readiness"),

  inboxItems: (opts?: {
    type?: string | string[];
    severity?: string | string[];
    status?: string | string[];
    requires_action?: boolean;
    limit?: number;
  }) => {
    const qs = new URLSearchParams();
    const join = (v: string | string[] | undefined) =>
      Array.isArray(v) ? v.filter(Boolean).join(",") : v ?? "";
    const type = join(opts?.type);
    const sev = join(opts?.severity);
    const status = join(opts?.status);
    if (type) qs.set("type", type);
    if (sev) qs.set("severity", sev);
    if (status) qs.set("status", status);
    if (typeof opts?.requires_action === "boolean")
      qs.set("requires_action", opts.requires_action ? "1" : "0");
    if (typeof opts?.limit === "number") qs.set("limit", String(opts.limit));
    const suffix = qs.toString();
    return get<InboxItemsEnvelope>(
      `/inbox/items${suffix ? `?${suffix}` : ""}`,
    );
  },
  inboxResolve: (body: InboxResolveRequest) =>
    post<InboxResolveEnvelope>("/inbox/resolve", body),

  agentTasks: (opts?: { strategy_id?: string; status?: string; limit?: number }) => {
    const qs = new URLSearchParams();
    if (opts?.strategy_id) qs.set("strategy_id", opts.strategy_id);
    if (opts?.status) qs.set("status", opts.status);
    if (typeof opts?.limit === "number") qs.set("limit", String(opts.limit));
    const suffix = qs.toString();
    return get<AgentTasksEnvelope>(
      `/agent/tasks${suffix ? `?${suffix}` : ""}`,
    );
  },
  agentTaskTimeline: (id: string) =>
    get<AgentTaskTimelineEnvelope>(
      `/agent/tasks/timeline?id=${encodeURIComponent(id)}`,
    ),
  agentTaskArtifacts: (id: string) =>
    get<AgentTaskArtifactsEnvelope>(
      `/agent/tasks/artifacts?id=${encodeURIComponent(id)}`,
    ),
  agentTaskCancel: (id: string, reason?: string) =>
    post<OperatorEnvelope<{ task_id: string; cancelled: boolean }>>(
      "/agent/tasks/cancel",
      { id, reason },
    ),
  agentTaskResume: (id: string) =>
    post<OperatorEnvelope<{
      task_id: string;
      open_turn_id: string | null;
      turn_state?: Record<string, unknown>;
    }>>("/agent/tasks/resume", { id }),

  // ---------------------------------------------------------------------
  // Runtime feature flags, capability catalog, data sources, evidence
  // vault, prompt guard review queue,
  // operator preference profile, E2E artifact capture.
  // ---------------------------------------------------------------------

  runtimeFlags: () => get<RuntimeFlagsEnvelope>("/runtime/flags"),
  runtimeFlagSet: (key: string, enabled: boolean | null) =>
    post<OperatorEnvelope<{ ok: boolean; key: string; enabled: boolean | null; path: string }>>(
      "/runtime/flags/set",
      { key, enabled },
    ),
  runtimeFlagRefresh: () =>
    post<OperatorEnvelope<{ cleared: boolean }>>("/runtime/flags/refresh", {}),

  capabilityCatalog: () => get<CapabilityCatalogEnvelope>("/capabilities/catalog"),
  capabilityReadiness: () => get<CapabilityReadinessEnvelope>("/capabilities/readiness"),
  capabilityEntry: (id: string) =>
    get<OperatorEnvelope<{ entry: CapabilityEntry }>>(
      `/capabilities/entry?id=${encodeURIComponent(id)}`,
    ),

  dataSourcesStatus: () => get<DataSourceStatusEnvelope>("/data-sources/status"),
  dataSourcesEvents: (limit = 64) =>
    get<DataSourceEventsEnvelope>(
      `/data-sources/events?limit=${encodeURIComponent(String(limit))}`,
    ),
  dataSourcesSyncNow: (source_id: string) =>
    post<OperatorEnvelope<{ result: Record<string, unknown> }>>(
      "/data-sources/sync-now",
      { source_id },
    ),

  evidenceSources: () => get<EvidenceSourcesEnvelope>("/evidence/sources"),
  evidenceTopics: () => get<EvidenceTopicsEnvelope>("/evidence/topics"),
  evidenceSearch: (opts?: {
    q?: string;
    source_type?: string;
    topic?: string;
    scope?: string;
    strategy_id?: string;
    session_id?: string;
    limit?: number;
  }) => {
    const qs = new URLSearchParams();
    if (opts?.q) qs.set("q", opts.q);
    if (opts?.source_type) qs.set("source_type", opts.source_type);
    if (opts?.topic) qs.set("topic", opts.topic);
    if (opts?.scope) qs.set("scope", opts.scope);
    if (opts?.strategy_id) qs.set("strategy_id", opts.strategy_id);
    if (opts?.session_id) qs.set("session_id", opts.session_id);
    if (typeof opts?.limit === "number") qs.set("limit", String(opts.limit));
    const suffix = qs.toString();
    return get<EvidenceSearchEnvelope>(
      `/evidence/search${suffix ? `?${suffix}` : ""}`,
    );
  },
  evidenceGet: (id: string) =>
    get<OperatorEnvelope<{ evidence: EvidenceDoc }>>(
      `/evidence/get?id=${encodeURIComponent(id)}`,
    ),
  evidenceIngestRun: (body?: { kind?: string } & Record<string, unknown>) =>
    post<OperatorEnvelope<{ docs?: EvidenceDoc[]; doc?: EvidenceDoc }>>(
      "/evidence/ingest/run",
      body ?? { kind: "demo" },
    ),

  profileList: (opts?: { facet?: string; scope?: string; include_forgotten?: boolean }) => {
    const qs = new URLSearchParams();
    if (opts?.facet) qs.set("facet", opts.facet);
    if (opts?.scope) qs.set("scope", opts.scope);
    if (opts?.include_forgotten) qs.set("include_forgotten", "1");
    const suffix = qs.toString();
    return get<ProfileEnvelope>(
      `/memory/profile${suffix ? `?${suffix}` : ""}`,
    );
  },
  profileSet: (body: {
    facet: string;
    key: string;
    value: unknown;
    scope?: string;
    pinned?: boolean;
    source?: string;
    operator_id?: string;
  }) => post<{ ok: boolean; fact?: ProfileFact; error?: string }>("/memory/profile/set", body),
  profilePin: (id: string) =>
    post<{ ok: boolean; fact?: ProfileFact; error?: string }>(
      "/memory/profile/pin",
      { fact_id: id },
    ),
  profileForget: (id: string) =>
    post<{ ok: boolean; fact?: ProfileFact; error?: string }>(
      "/memory/profile/forget",
      { fact_id: id },
    ),
  profileRebuild: () =>
    post<{ ok: boolean; cache?: Record<string, unknown> }>(
      "/memory/profile/rebuild",
      {},
    ),

  promptGuardList: (state?: string) => {
    const qs = state ? `?state=${encodeURIComponent(state)}` : "";
    return get<PromptGuardListEnvelope>(`/security/prompt_guard/items${qs}`);
  },
  promptGuardResolve: (body: {
    id: string;
    decision: string;
    operator_id?: string;
    note?: string;
  }) =>
    post<{ ok: boolean; item?: PromptGuardItem; error?: string }>(
      "/security/prompt_guard/resolve",
      body,
    ),
  promptGuardClassify: (body: {
    content: string;
    source_route?: string;
    source_channel?: string;
    enqueue?: boolean;
  }) =>
    post<PromptGuardClassifyEnvelope>("/security/prompt_guard/classify", body),

  e2eRuns: () => get<E2eRunsEnvelope>("/ops/e2e/runs"),
  e2eRun: (id: string) =>
    get<E2eRunEnvelope>(`/ops/e2e/run?id=${encodeURIComponent(id)}`),

  agentRunTurn: (body: {
    trigger?: Record<string, unknown>;
    source?: string;
    kind?: string;
    target?: string;
    payload?: Record<string, unknown>;
    strategy_id?: string;
    session_id?: string;
    [key: string]: unknown;
  }) => post<AgentRunTurnResult>("/agent/run_turn", body),
  strategyHistory: (strategy_id: string, limit = 50) =>
    post<{ events: StrategyHistoryEvent[] }>("/strategy/history", { strategy_id, limit }),
  strategyReview: (body: { strategy_id: string; session_id: string; stage?: string }) =>
    post<Record<string, unknown>>("/strategy/review", body),
  strategyAttribution: (strategy_id: string, session_id: string) =>
    post<Record<string, unknown>>("/strategy/attribution", { strategy_id, session_id }),
  strategyDivergence: (strategy_id: string, window_sessions = 25) =>
    post<Record<string, unknown>>("/strategy/divergence", { strategy_id, window_sessions }),
  strategyScenarioReplay: (body: Record<string, unknown> & { strategy_id: string; session_id: string }) =>
    post<Record<string, unknown>>("/strategy/scenario_replay", body),
  agentOpenTurns: () =>
    get<{ open_turns: AgentOpenTurn[] }>("/agent/open_turns"),
  agentTurnState: (turn_id: string) =>
    post<AgentTurnState>("/agent/turn_state", { turn_id }),
  /**
   * Poll the in-process streaming bus
   * (``nerya.agent.streaming.bus``). ``after_seq`` is the cursor —
   * pass the highest ``seq`` you've seen and the server returns only
   * events strictly after that, plus the new high-water mark.
   * ``limit`` caps the response so a long-running agent backlog
   * doesn't blow the wire on first connect (server-side default 200).
   * Used by ``ChatView`` to drive a live activity timeline while a
   * turn is in flight.
   */
  streamEvents: (
    after_seq?: number,
    opts?: { limit?: number; session_id?: string },
  ) => {
    const qs = new URLSearchParams();
    if (typeof after_seq === "number" && Number.isFinite(after_seq)) {
      qs.set("after_seq", String(Math.max(0, Math.floor(after_seq))));
    }
    if (typeof opts?.limit === "number" && Number.isFinite(opts.limit)) {
      qs.set("limit", String(Math.max(1, Math.floor(opts.limit))));
    }
    if (opts?.session_id) qs.set("session_id", opts.session_id);
    const suffix = qs.toString();
    return get<{
      events: Array<Record<string, unknown>>;
      latest_seq: number;
      count: number;
      cursor: number;
    }>(`/agent/stream/events${suffix ? `?${suffix}` : ""}`);
  },
  agentTrace: (body: {
    trigger_id?: string;
    turn_id?: string;
    session_id?: string;
    strategy_id?: string;
  }) => post<Record<string, unknown>>("/agent/trace", body),
  agentExplain: (body: {
    trigger_id?: string;
    turn_id?: string;
    session_id?: string;
    strategy_id?: string;
  }) => post<Record<string, unknown>>("/agent/explain", body),
  sessionList: (
    strategy_id?: string,
    limit = 50,
    opts?: { offset?: number; include?: "all" | "chat" | string },
  ) => {
    const qs = new URLSearchParams();
    if (strategy_id) qs.set("strategy_id", strategy_id);
    qs.set("limit", String(limit));
    if (typeof opts?.offset === "number" && Number.isFinite(opts.offset)) {
      qs.set("offset", String(Math.max(0, Math.floor(opts.offset))));
    }
    if (opts?.include && opts.include !== "chat") qs.set("include", opts.include);
    return get<{
      sessions: AgentSession[];
      limit?: number;
      offset?: number;
      next_offset?: number;
      has_more?: boolean;
    }>(
      `/agent/sessions?${qs.toString()}`,
    );
  },
  sessionGet: (session_id: string) =>
    get<AgentSession | { error: string }>(
      `/agent/session?session_id=${encodeURIComponent(session_id)}`,
    ),
  sessionDelete: (session_id: string) =>
    post<{ ok: boolean }>("/agent/session/delete", { session_id }),
  sessionRename: (session_id: string, title: string) =>
    post<{ ok: boolean; session?: AgentSession; error?: string }>(
      "/agent/session/rename",
      { session_id, title },
    ),
  sessionMessageEdit: (body: {
    session_id: string;
    message_id: string;
    content: string;
  }) =>
    post<{ ok: boolean; error?: string; session_id?: string; message_id?: string }>(
      "/agent/session/message/edit",
      body,
    ),
  sessionMessageDelete: (body: { session_id: string; message_id: string }) =>
    post<{ ok: boolean; error?: string; session_id?: string; message_id?: string }>(
      "/agent/session/message/delete",
      body,
    ),
  sessionRecordSkillState: (session_id: string, skill_id: string, state: unknown) =>
    post<{ ok: boolean; session: AgentSession }>(
      "/agent/session/skill_state",
      { session_id, skill_id, state },
    ),
  sessionTranscript: (
    session_id: string,
    opts?: { full?: boolean; max_pairs?: number; per_msg_cap?: number },
  ) => {
    const qs = new URLSearchParams();
    qs.set("session_id", session_id);
    if (opts?.full) qs.set("full", "1");
    if (typeof opts?.max_pairs === "number") {
      qs.set("max_pairs", String(opts.max_pairs));
    }
    if (typeof opts?.per_msg_cap === "number") {
      qs.set("per_msg_cap", String(opts.per_msg_cap));
    }
    return get<{
      ok: boolean;
      session_id: string;
      strategy_id?: string | null;
      title?: string;
      created_at?: string;
      updated_at?: string;
      messages: Array<{
        message_id?: string;
        role: "user" | "assistant";
        content: string;
        turn_id?: string;
        ts?: string;
        meta?: Record<string, unknown>;
        turn?: Record<string, unknown> | null;
      }>;
      count: number;
      error?: string;
    }>(`/agent/session/transcript?${qs.toString()}`);
  },
  approvalsPending: () =>
    get<{
      ok: boolean;
      count: number;
      approvals: ApprovalCard[];
    }>("/approvals/pending"),
  approvalCallback: (body: { callback_data: string; actor_id?: string }) =>
    post<{
      ok: boolean;
      approval_id?: string;
      approval_ids?: string[];
      action?: string;
      state?: string;
      batch?: boolean;
      item_count?: number;
      error?: string;
      note?: string;
    }>("/approvals/callback", body),
};

export type AgentOpenTurn = {
  turn_id: string;
  strategy_id?: string | null;
  session_id?: string | null;
  opened_at?: string | null;
  last_step?: string | null;
  step_count?: number;
  [key: string]: unknown;
};

export type AgentTurnState = {
  turn_id?: string;
  strategy_id?: string | null;
  session_id?: string | null;
  steps?: Array<Record<string, unknown>>;
  status?: string;
  [key: string]: unknown;
};

export type AgentRunTurnResult = {
  trigger_event_id?: string;
  turn_id?: string;
  stopped_reason?: string | null;
  transition_reason?: string | null;
  final_text?: string;
  artifact_index?: Record<string, unknown>;
  final_report?: Record<string, unknown>;
  decision?: Record<string, unknown>;
  actions?: Array<Record<string, unknown>>;
  subagents?: Array<Record<string, unknown>>;
  plan?: { kind?: string; tier?: string };
  tool_trace?: Array<Record<string, unknown>>;
  budget?: Record<string, unknown>;
  [key: string]: unknown;
};

export type AgentSession = {
  session_id: string;
  strategy_id?: string | null;
  created_at?: string;
  updated_at?: string;
  turn_ids?: string[];
  invoked_skills?: string[];
  skill_state?: Record<string, unknown>;
  last_action?: string | null;
  meta?: Record<string, unknown>;
  source?: string;
  message_count?: number;
};

export type ApprovalCard = {
  record?: Record<string, unknown>;
  prompt?: {
    approval_id: string;
    actor_id?: string;
    text: string;
    buttons?: Array<{
      label: string;
      callback_data: string;
      style?: string;
    }>;
    metadata?: Record<string, unknown>;
  };
  telegram?: Record<string, unknown>;
};

export type TriggerRoute = {
  id: string;
  title?: string;
  match?: Record<string, unknown>;
  action?: { skill_id?: string; [key: string]: unknown };
  kind?: string;
  target?: string;
  enabled?: boolean;
  paused?: boolean;
  max_per_minute?: number;
  rate_limit_per_min?: number;
  cooldown_seconds?: number;
  max_payload_bytes?: number;
  priority?: number;
  source_allow?: string[];
  strategy_id?: string;
  description?: string;
  extra?: Record<string, unknown>;
  [key: string]: unknown;
};

export type TriggerDeliveryTarget = {
  kind:
    | "messages"
    | "webhook"
    | "gateway"
    | "platform"
    | "dashboard"
    | "local"
    | "telegram"
    | "discord"
    | "slack"
    | "feishu"
    | "wecom"
    | "dingtalk"
    | "matrix"
    | "whatsapp"
    | string;
  channel?: string;
  platform?: string;
  url?: string;
  headers?: Record<string, string>;
  [key: string]: unknown;
};

export type TriggerSchedule = {
  id: string;
  enabled?: boolean;
  paused?: boolean;
  title?: string;
  source?: string;
  kind?: string;
  target?: string;
  cron?: string;
  interval?: string | number;
  every_seconds?: number;
  starts_at?: string | null;
  ends_at?: string | null;
  payload?: Record<string, unknown>;
  strategy_id?: string;
  description?: string;
  timezone?: string;
  last_tick_at?: string | null;
  next_due_at?: string | null;
  // compatibility cron/session extension
  last_fired_ts?: number | null;
  session_kind?: "trigger" | "agent" | "script";
  attached_skills?: string[];
  delivery_targets?: TriggerDeliveryTarget[];
  session_ttl_seconds?: number | null;
  session_mode?: "ephemeral" | "reuse" | "fanout";
  session_id?: string | null;
  session_ids?: string[];
};

export type ClientApi = typeof clientApi;
