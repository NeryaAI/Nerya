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
  InboxItemsEnvelope,
  InboxResolveEnvelope,
  InboxResolveRequest,
  OperatorEnvelope,
  OperatorNavEnvelope,
  OperatorOverviewEnvelope,
  SetupReadinessEnvelope,
} from "./operatorTypes";
import type {
  EvolutionAssetsEnvelope,
  EvolutionEventsEnvelope,
  EvolutionSignalsEnvelope,
  EvolutionTimelineEnvelope,
} from "./evolutionTypes";

const BASE = "/api/proxy";

async function post<T>(path: string, body: unknown = {}): Promise<T> {
  const res = await fetch(`${BASE}${path}`, {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify(body),
    cache: "no-store",
  });
  if (!res.ok) {
    const text = await res.text().catch(() => "");
    throw new Error(`HTTP ${res.status}: ${text || res.statusText}`);
  }
  return (await res.json()) as T;
}

async function get<T>(path: string): Promise<T> {
  const res = await fetch(`${BASE}${path}`, { cache: "no-store" });
  if (!res.ok) {
    const text = await res.text().catch(() => "");
    throw new Error(`HTTP ${res.status}: ${text || res.statusText}`);
  }
  return (await res.json()) as T;
}

/** Generic helper for callers that need method + body flexibility
 *  (the chat surface, ad-hoc pages that talk to /agent/run_turn etc.).
 *
 *  Superseded all historical imports from ``lib/client.ts`` — keep the
 *  signature stable so existing call sites keep compiling.
 */
export async function callApi<T = unknown>(
  path: string,
  init?: { method?: string; body?: unknown }
): Promise<T> {
  const url = `${BASE}${path.startsWith("/") ? path : "/" + path}`;
  const res = await fetch(url, {
    method: init?.method || "GET",
    headers: { "content-type": "application/json" },
    body: init?.body !== undefined ? JSON.stringify(init.body) : undefined,
  });
  const text = await res.text();
  let body: unknown = null;
  try { body = text ? JSON.parse(text) : null; } catch { body = text; }
  if (!res.ok) {
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
  ts: string;
  chart: BacktestChartData;
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
  execution_profile: WalletExecutionProfile;
  chains: string[];
  notes: string;
};

export type WalletProviderInfo = {
  id: string;
  label: string;
  description: string;
  install_hint: string;
  runtime: string;
  links: Record<string, string>;
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
// Control-plane types (04-29 §11 P7).
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
// Account control plane (04-29 §11 P8).
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

export type LlmTierConfig = {
  tier: string;
  provider: string;
  model: string;
  base_url?: string;
  provider_key_ref?: string;
  provider_key?: string;
  has_key_ref?: boolean;
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

export const clientApi = {
  health: () => get<{ status: string }>("/health"),
  workspace: () => get<{ root: string; live_trading_enabled: boolean; kill_switch: boolean }>("/workspace"),
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
  portfolioPnl: () => post<{ realized_usd: number; equity_usd: number }>("/portfolio/pnl"),
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
    post<{ ok: boolean; strategy_id: string; state: string; path: string }>(
      "/strategy/create",
      body,
    ),
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
    }>("/strategy/update", { strategy_id: strategyId, ...patch }),
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
    post<{ ok: boolean; strategy_id: string; account_id: string }>(
      "/strategy/bind_account",
      { strategy_id: strategyId, account_id: accountId },
    ),
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
  strategyBacktestChart: (strategyId: string, ts: string) =>
    post<BacktestChartEnvelope>("/strategy/backtests/chart", {
      strategy_id: strategyId,
      ts,
    }),
  strategyBacktestFile: (strategyId: string, ts: string, name: string) =>
    post<{ ok: boolean; strategy_id: string; ts: string; name: string; content: string }>(
      "/strategy/backtests/file",
      { strategy_id: strategyId, ts, name },
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

  // ---- Control plane (04-29 §11 P7) ----
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

  // ---- Account roster CRUD (04-29 §11 P8) ----
  accountsList: () =>
    get<{ accounts: AccountSummary[]; ts: number }>("/accounts/list"),
  accountsGet: (account_id: string) =>
    post<{ ok: boolean; account?: AccountSummary; error?: string }>(
      "/accounts/get",
      { account_id }
    ),
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
    // 04-29 §11 P9 — when ``apply: false`` the upsert is
    // staged as an account_roster_patch proposal that needs operator
    // approval. Default ``true`` keeps the legacy direct write path
    // for backwards compat.
    apply?: boolean;
  }) =>
    post<{
      ok: boolean;
      applied?: boolean;
      account?: AccountSummary;
      proposal?: AccountProposalView;
      error?: string;
      detail?: string;
    }>("/accounts/upsert", body),
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
  // 04-29 §11 P10 — manage HTTP auth headers on data-source
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
  }) =>
    post<{
      ok: boolean;
      account?: AccountSummary;
      headers?: Array<{ key: string; value: string; kind: string }>;
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
  // 04-29 §11 P9 — recent risk-gate decisions with fix
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
  proposalApply: (proposal_id: string) =>
    post<Record<string, unknown>>("/evolution/apply", { proposal_id }),
  proposalRollback: (proposal_id: string) =>
    post<Record<string, unknown>>("/evolution/rollback", { proposal_id }),
  evolutionReflect: () =>
    post<Record<string, unknown>>("/evolution/reflect", {}),
  evolutionRank: (body: { strategy_id?: string; states?: string[]; persist?: boolean } = {}) =>
    post<Record<string, unknown>>("/evolution/rank", body),
  evolutionEvidence: (strategy_id: string) =>
    post<Record<string, unknown>>("/evolution/evidence", { strategy_id }),
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
    post<{ ok: boolean; provider: string; config: Record<string, unknown> }>(
      "/wallet/configure",
      { provider, config },
    ),
  walletInstallHint: (provider: string) =>
    post<{ provider: string; install_hint: string; runtime: string; links: Record<string, string> }>(
      "/wallet/install_hint",
      { provider },
    ),

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
      error?: string;
    }>("/llm/config"),
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
      error?: string;
    }>("/llm/config", body),
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
    post<{ ok: boolean; fired: boolean; event_id?: string }>(
      "/triggers/schedules/run_now",
      { id },
    ),
  scheduleRemove: (id: string) =>
    post<{ ok: boolean }>("/triggers/schedules/remove", { id }),
  scheduleStatus: (id: string) =>
    get<{
      id: string;
      enabled: boolean;
      cron?: string;
      every_s?: number;
      last_tick_at?: string | null;
      next_due_at?: string | null;
      is_due: boolean;
    }>(`/triggers/schedules/status?id=${encodeURIComponent(id)}`),
  scheduleTick: () =>
    post<{ fired: number; events: string[] }>("/triggers/schedules/tick"),

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
  sessionList: (strategy_id?: string, limit = 50) => {
    const qs = new URLSearchParams();
    if (strategy_id) qs.set("strategy_id", strategy_id);
    qs.set("limit", String(limit));
    return get<{ sessions: AgentSession[] }>(
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
  kind: "messages" | "webhook";
  channel?: string;
  url?: string;
  headers?: Record<string, string>;
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
  last_tick_at?: string | null;
  next_due_at?: string | null;
  // compatibility cron/session extension
  session_kind?: "trigger" | "agent";
  attached_skills?: string[];
  delivery_targets?: TriggerDeliveryTarget[];
  session_ttl_seconds?: number | null;
};

export type ClientApi = typeof clientApi;
