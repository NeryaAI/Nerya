/**
 * Operator-facing API envelope and BFF data types.
 *
 * Mirrors ``nerya/api/_envelope.py`` and the ``routes_operator.py`` /
 * ``routes_inbox.py`` / ``routes_agent_tasks.py`` modules. The envelope
 * is intentionally chunky so the dashboard can render status, severity,
 * primary action, and source/debug references uniformly without having
 * to special-case each endpoint.
 */

export type EnvelopeStatus = "ok" | "warn" | "error" | "blocked";
export type EnvelopeSeverity = "info" | "warn" | "danger";

export type OperatorAction = {
  id: string;
  label: string;
  href?: string;
  method?: string;
  body?: Record<string, unknown>;
  requires_scope?: string;
  disabled_reason?: string;
  severity?: EnvelopeSeverity;
};

export type OperatorRef = {
  kind: string;
  id: string;
  href?: string;
  label?: string;
};

export type OperatorEnvelope<T = unknown> = {
  ok: boolean;
  status: EnvelopeStatus;
  severity: EnvelopeSeverity;
  summary: string;
  primary_action: OperatorAction | null;
  next_actions: OperatorAction[];
  source_refs: OperatorRef[];
  debug_refs: OperatorRef[];
  data: T;
};

// ---------------------------------------------------------------------------
// /operator/nav
// ---------------------------------------------------------------------------

export type NavEntry = {
  id: string;
  label: string;
  href: string;
  match_hrefs?: string[];
  icon?: string;
  tagline?: string;
  always_visible?: boolean;
  requires_capability?: string;
  badge?: { count?: number; severity?: EnvelopeSeverity } | null;
};

export type HiddenNavEntry = NavEntry & {
  reason: string;
  fix_action?: OperatorAction | null;
};

export type OperatorNavData = {
  primary: NavEntry[];
  advanced: NavEntry[];
  hidden: HiddenNavEntry[];
  capabilities: Record<string, boolean>;
};

export type OperatorNavEnvelope = OperatorEnvelope<OperatorNavData>;

// ---------------------------------------------------------------------------
// /operator/overview
// ---------------------------------------------------------------------------

export type AttentionItem = {
  id: string;
  type: string;
  severity: EnvelopeSeverity;
  title: string;
  summary: string;
  href?: string;
  requires_action: boolean;
  source_refs?: OperatorRef[];
};

export type OverviewHealth = {
  live_trading: boolean;
  kill_switch: boolean;
  llm_ready: boolean;
  trading_account: boolean;
  strategies: boolean;
};

export type OverviewPortfolio = {
  equity_usd?: number;
  realized_usd?: number;
  unrealized_usd?: number;
  exposure_usd?: number;
  drawdown_pct?: number;
  positions?: number;
  [key: string]: unknown;
};

export type OverviewEquityPoint = {
  ts: string;
  equity_usd: number;
};

export type OperatorOverviewData = {
  health: OverviewHealth;
  portfolio: OverviewPortfolio;
  equity_curve: OverviewEquityPoint[];
  attention: AttentionItem[];
  llm: {
    ready_tiers: number;
    total_tiers: number;
    not_ready: Array<{ tier: string; provider: string }>;
  };
  counts: {
    strategy_packages: number;
    trading_strategies: number;
    accounts: number;
    open_turns: number;
    pending_approvals: number;
    pending_proposals: number;
  };
};

export type OperatorOverviewEnvelope = OperatorEnvelope<OperatorOverviewData>;

// ---------------------------------------------------------------------------
// /setup/readiness
// ---------------------------------------------------------------------------

export type ReadinessCheck = {
  name: string;
  status: "ok" | "warn" | "blocked";
  summary: string;
  fix?: OperatorAction | null;
  sources?: OperatorRef[];
};

export type SetupReadinessData = {
  checks: ReadinessCheck[];
  blocking: string[];
};

export type SetupReadinessEnvelope = OperatorEnvelope<SetupReadinessData>;

// ---------------------------------------------------------------------------
// /inbox/items
// ---------------------------------------------------------------------------

export type InboxItemType =
  | "approval"
  | "proposal"
  | "failed_task"
  | "notification"
  | "provider_error";

export type InboxItem = {
  id: string;
  raw_id: string;
  type: InboxItemType;
  severity: EnvelopeSeverity;
  status: string;
  title: string;
  summary: string;
  requires_action: boolean;
  created_at: string;
  source_refs: OperatorRef[];
  actions: OperatorAction[];
  data: Record<string, unknown>;
};

export type InboxItemsData = {
  items: InboxItem[];
  count: number;
  needs_action: number;
};

export type InboxItemsEnvelope = OperatorEnvelope<InboxItemsData>;

export type InboxResolveRequest = {
  id?: string;
  ids?: string[];
  decision?: "approve" | "reject" | "apply" | "rollback" | "dismiss";
  actor_id?: string;
};

export type InboxResolveEnvelope = OperatorEnvelope<{
  id?: string;
  ids?: string[];
  outcome?: Record<string, unknown>;
  results?: Array<{
    id: string;
    ok: boolean;
    status?: string;
    severity?: EnvelopeSeverity;
    summary?: string;
    outcome?: Record<string, unknown>;
  }>;
  resolved_count?: number;
  failed_count?: number;
}>;

// ---------------------------------------------------------------------------
// /agent/tasks
// ---------------------------------------------------------------------------

export type AgentTaskStatus = "in_progress" | "failed" | "done" | "empty";

export type AgentTaskRow = {
  id: string;
  status: AgentTaskStatus;
  severity: EnvelopeSeverity;
  title: string;
  last_action: string;
  strategy_id: string | null;
  turn_count: number;
  skills_invoked: string[];
  created_at: string;
  updated_at: string;
  meta: Record<string, unknown>;
  active_turn_ids: string[];
  failed_turn_ids: string[];
};

export type AgentTasksData = {
  tasks: AgentTaskRow[];
  counts: Record<AgentTaskStatus, number>;
  count: number;
};

export type AgentTasksEnvelope = OperatorEnvelope<AgentTasksData>;

export type AgentTaskTimelineEvent = {
  surface: string;
  ts: string | null;
  record: Record<string, unknown>;
};

export type AgentTaskTimelineData = {
  task_id: string;
  correlator: Record<string, string | null>;
  events: AgentTaskTimelineEvent[];
  surfaces: string[];
};

export type AgentTaskTimelineEnvelope = OperatorEnvelope<AgentTaskTimelineData>;

export type AgentTaskArtifacts = {
  files: Array<{ ts: string | null; action: string; path: string }>;
  messages: Array<{ ts: string | null; channel?: string; text: string }>;
  memory: Array<{ ts: string | null; key?: string; summary: string }>;
  orders: Array<{
    ts: string | null;
    action: string;
    symbol?: string;
    side?: string;
    quantity?: number;
    result?: Record<string, unknown>;
  }>;
  created: Array<{ ts: string | null; action: string; result?: Record<string, unknown> }>;
};

export type AgentTaskArtifactsData = {
  task_id: string;
  artifacts: AgentTaskArtifacts;
  counts: Record<keyof AgentTaskArtifacts, number>;
};

export type AgentTaskArtifactsEnvelope = OperatorEnvelope<AgentTaskArtifactsData>;

// ---------------------------------------------------------------------------
// Runtime feature flags, capability catalog, data sources, evidence vault,
// prompt guard review queue, operator profile,
// E2E artifact capture.
// ---------------------------------------------------------------------------

export type RuntimeFlag = {
  key: string;
  phase: string;
  summary: string;
  default: boolean;
  enabled: boolean;
  env_override: string;
};

export type RuntimeFlagsData = {
  flags: RuntimeFlag[];
  counts: { total: number; enabled: number; disabled: number };
  overrides_path: string;
};

export type RuntimeFlagsEnvelope = OperatorEnvelope<RuntimeFlagsData>;

export type CapabilityStatus = "ready" | "degraded" | "blocked" | "unavailable";

export type CapabilityEntry = {
  id: string;
  name: string;
  domain: string;
  kind: string;
  status: CapabilityStatus;
  source: string;
  entrypoint?: string;
  dashboard_path?: string;
  required_config?: string[];
  required_secrets?: string[];
  permissions?: string[];
  approval?: string;
  live_trading_impact?: string;
  data_boundary?: {
    secrets_visible_to_agent?: boolean;
    external_network?: boolean;
    data_leaves_device?: boolean;
  };
  last_verified_at?: string | null;
  last_error?: string | null;
  operator_hint?: string;
  tags?: string[];
};

export type CapabilityCatalogData = {
  entries: CapabilityEntry[];
  count: number;
};

export type CapabilityCatalogEnvelope = OperatorEnvelope<CapabilityCatalogData>;

export type CapabilityReadinessData = {
  total: number;
  counts: Record<CapabilityStatus, number>;
  blocked: CapabilityEntry[];
  degraded: CapabilityEntry[];
};

export type CapabilityReadinessEnvelope = OperatorEnvelope<CapabilityReadinessData>;

export type DataSourceRow = {
  source_id: string;
  kind: string;
  provider?: string;
  account_id?: string | null;
  enabled: boolean;
  last_success_at?: string | null;
  last_attempt_at?: string | null;
  next_due_at?: string | null;
  cursor?: string | null;
  freshness_sla_seconds?: number;
  budget?: { daily_limit?: number; used_today?: number };
  last_error?: string | null;
  stale: boolean;
};

export type DataSourceStatusData = {
  sources: DataSourceRow[];
  total: number;
  stale_count: number;
  enabled_count: number;
  generated_at: string;
};

export type DataSourceStatusEnvelope = OperatorEnvelope<DataSourceStatusData>;

export type DataSourceEvent = {
  ts: string;
  source_id: string;
  event: string;
  detail?: string;
};

export type DataSourceEventsEnvelope = OperatorEnvelope<{
  events: DataSourceEvent[];
  count: number;
}>;

export type EvidenceDoc = {
  evidence_id: string;
  source_type: string;
  source_id: string;
  title: string;
  summary: string;
  workspace_path?: string;
  tags?: string[];
  scope: "shared" | "strategy" | "session";
  strategy_id?: string | null;
  session_id?: string | null;
  created_at: string;
  provenance?: Record<string, unknown>;
  security?: { contains_secret?: boolean; redaction_applied?: boolean };
};

export type EvidenceSearchEnvelope = OperatorEnvelope<{
  results: EvidenceDoc[];
  count: number;
  query: string;
}>;

export type EvidenceSourcesEnvelope = OperatorEnvelope<{
  sources: Array<{ source_type: string; source_id: string; count: number; last_at?: string }>;
  count: number;
}>;

export type EvidenceTopicsEnvelope = OperatorEnvelope<{
  topics: Array<{ topic: string; count: number }>;
  count: number;
}>;

export type ProfileFact = {
  id: string;
  ts: string;
  facet: string;
  key: string;
  value: unknown;
  scope: string;
  pinned: boolean;
  forgotten: boolean;
  source: string;
  operator_id?: string;
};

export type ProfileEnvelope = {
  ok: boolean;
  facts: ProfileFact[];
  stats: {
    total: number;
    forgotten: number;
    pinned: number;
    by_facet: Record<string, number>;
    by_scope: Record<string, number>;
  };
  error?: string;
  flag?: string;
  detail?: string;
};

export type PromptGuardItem = {
  id: string;
  ts: string;
  verdict: "review" | "block";
  policy: string;
  matched: string[];
  excerpt: string;
  source_route?: string;
  source_channel?: string;
  affected_action?: string;
  state: "pending" | "approved" | "rejected" | "escalated";
  decision?: string;
  decided_by?: string;
  decided_at?: string;
  note?: string;
};

export type PromptGuardListEnvelope = {
  ok: boolean;
  items: PromptGuardItem[];
  count: number;
  stats: {
    total: number;
    by_state: Record<string, number>;
    by_verdict: Record<string, number>;
  };
  error?: string;
  flag?: string;
  detail?: string;
};

export type PromptGuardClassifyEnvelope = {
  ok: boolean;
  verdict: "allow" | "review" | "block";
  policy: string;
  matched: string[];
  enqueued?: PromptGuardItem | null;
  error?: string;
  flag?: string;
};

export type E2eRunMeta = {
  run_id: string;
  started_at: string;
  ended_at?: string | null;
  label: string;
  base_url?: string;
  env?: Record<string, unknown>;
  status?: string;
  artifacts?: Array<{ name: string; path: string; size: number }>;
};

export type E2eRunsEnvelope = OperatorEnvelope<{ runs: E2eRunMeta[]; count: number }>;
export type E2eRunEnvelope = OperatorEnvelope<{ run: E2eRunMeta }>;
