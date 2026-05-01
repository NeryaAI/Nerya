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
  id: string;
  decision?: "approve" | "reject" | "apply" | "rollback" | "dismiss";
  actor_id?: string;
};

export type InboxResolveEnvelope = OperatorEnvelope<{
  id: string;
  outcome?: Record<string, unknown>;
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
