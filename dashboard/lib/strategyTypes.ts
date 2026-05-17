/**
 * Strategy runtime types — of the agent-generated strategy
 * runtime refactor.
 *
 * These mirror the backend's
 * :mod:`nerya.api.routes_strategies_runtime` envelope shapes. Keep the
 * field names in lockstep with the Python dataclasses; if you change
 * one side without the other you'll see hard runtime errors in the
 * StrategyWorkspace panels.
 */

export type StrategyMode = "paper" | "shadow" | "live";
export type StrategyClass = "scalping" | "trend" | "news" | "agent" | "agent_team";
export type StrategyExecutionMode = "script" | "agent" | "agent_task" | "agent_team" | "team";

export interface StrategyScheduleManifest {
  type: "cron" | "interval";
  cron?: string;
  every_seconds?: number;
  enabled?: boolean;
  starts_at?: string;
  ends_at?: string;
}

export interface StrategyPolicyManifest {
  max_single_order_usd?: number;
  max_daily_notional_usd?: number;
  max_open_positions?: number;
  min_confidence?: number;
  allow_direct_order?: boolean;
  require_subagent_before_order?: boolean;
  default_order_usd?: number;
  max_run_seconds?: number;
  max_sdk_calls_per_run?: number;
  max_subagent_calls_per_run?: number;
}

export interface StrategyLLMPolicyManifest {
  default_tier: "light" | "medium" | "high";
  allowed_tiers: ("light" | "medium" | "high")[];
  max_calls_per_run: number;
}

export interface StrategyTuningManifest {
  enabled: boolean;
  schedule?: StrategyScheduleManifest;
  lookback?: {
    runs?: number;
    min_closed_trades?: number;
    max_age_hours?: number;
  };
  subagent?: {
    name: string;
    prompt_file: string;
    tier: string;
  };
  objectives?: string[];
  guardrails?: {
    max_patch_files?: number;
    max_position_size_change_pct?: number;
    require_backtest?: boolean;
    require_shadow_run?: boolean;
    require_operator_approval?: boolean;
  };
  proposal_policy?: {
    allowed_targets?: string[];
    forbidden_targets?: string[];
  };
  tuning_prompt?: string;
}

export interface StrategyManifest {
  version: number;
  strategy_id: string;
  title: string;
  description: string;
  mode: StrategyMode;
  entrypoint: string;
  markets: string[];
  accounts: string[];
  schedule: StrategyScheduleManifest;
  policy: StrategyPolicyManifest;
  llm_policy: StrategyLLMPolicyManifest;
  subagents: string[];
  news_sources: string[];
  tuning: StrategyTuningManifest;
  extras?: Record<string, unknown>;
}

export interface StrategyPackageSummary {
  strategy_id: string;
  title: string;
  mode: StrategyMode;
  package_hash: string;
  markets: string[];
  accounts: string[];
  subagents: string[];
}

export interface StrategyPackageDetail {
  strategy_id: string;
  manifest: StrategyManifest;
  package_hash: string;
  files: string[];
}

export interface StrategyValidationReport {
  ok: boolean;
  strategy_id: string;
  blockers: { code: string; message: string }[];
  warnings: { code: string; message: string }[];
  ts: string;
}

export interface StrategyScheduleEntry {
  id: string;
  kind: string;
  target: string;
  enabled: boolean;
  every_seconds?: number;
  cron?: string;
  starts_at?: string;
  ends_at?: string;
  strategy_id?: string;
  payload?: Record<string, unknown>;
}

export interface StrategyScheduleStatus {
  strategy_id: string;
  trading: StrategyScheduleEntry | null;
  tuning: StrategyScheduleEntry | null;
}

export interface StrategyRunRecord {
  run_id: string;
  strategy_id: string;
  package_hash: string;
  started_at: string;
  finished_at: string;
  duration_ms: number;
  status: "ok" | "hold" | "submitted" | "error";
  mode: StrategyMode;
  reason?: string;
  session_id?: string | null;
  trigger_event_id?: string | null;
  inputs?: Record<string, unknown>;
  outputs?: Record<string, unknown>;
  audit?: Array<Record<string, unknown>>;
  error?: { kind?: string; message?: string } | null;
}

export interface KillSwitchState {
  asserted: boolean;
  reason: string;
  by: string;
  at: string;
}

export interface StrategyStatusEnvelope {
  ok: boolean;
  strategy_id: string;
  manifest?: StrategyManifest;
  package_hash?: string;
  schedules?: StrategyScheduleStatus;
  kill_switch?: KillSwitchState;
  last_run?: StrategyRunRecord | null;
  error?: string;
}

export interface StrategyPerformanceSnapshot {
  strategy_id: string;
  package_hash: string;
  generated_at: string;
  lookback_runs: number;
  runs_considered: number;
  run_metrics: Record<string, unknown>;
  trade_metrics: Record<string, unknown>;
  cost_metrics: Record<string, unknown>;
  risk_metrics: Record<string, unknown>;
  last_run_at?: string | null;
  last_review_at?: string | null;
  notes?: string[];
}

export interface PendingTuningProposal {
  id: string;
  summary: string;
  state: string;
  ts: string;
}

export interface StrategyTuningStatusEnvelope {
  ok: boolean;
  strategy_id: string;
  tuning?: StrategyTuningManifest;
  schedule?: StrategyScheduleEntry | null;
  snapshot?: StrategyPerformanceSnapshot;
  pending_proposals?: PendingTuningProposal[];
  error?: string;
}

export interface StrategyTuningRunResult {
  run_id: string;
  strategy_id: string;
  started_at: string;
  finished_at: string;
  duration_ms: number;
  status: "ok" | "skipped" | "hold" | "error";
  reason?: string;
  snapshot?: StrategyPerformanceSnapshot;
  subagent_output?: Record<string, unknown>;
  proposal_id?: string | null;
  review_path?: string | null;
  dropped_changes?: Array<{ entry: Record<string, unknown>; reason: string }>;
  warnings?: string[];
  error?: { kind?: string; message?: string } | null;
}

export interface StrategyHistoryEnvelope {
  strategy_id: string;
  ledgers: Record<
    string,
    {
      count: number;
      tail: Array<Record<string, unknown>>;
    }
  >;
}

export interface StrategyWorkspaceEnvelope extends StrategyStatusEnvelope {
  runs?: { strategy_id: string; count: number; runs: StrategyRunRecord[] };
  history?: StrategyHistoryEnvelope;
}

export interface StrategyGenerationRequest {
  strategy_id: string;
  title?: string;
  description?: string;
  prompt?: string;
  strategy_class?: StrategyClass;
  execution_mode?: StrategyExecutionMode;
  mode?: StrategyMode;
  markets: string[];
  accounts: string[];
  schedule_cron?: string;
  schedule_every_seconds?: number;
  news_sources?: string[];
  subagents?: string[];
  policy_overrides?: Record<string, unknown>;
  llm_policy_overrides?: Record<string, unknown>;
  create_tuning?: boolean;
  tuning_prompt?: string;
  tuning_cron?: string;
  tuning_objectives?: string[];
  extra_subagent_prompts?: Record<string, string>;
  files?: Record<string, string>;
  validate?: boolean;
}

export interface StrategyGenerationResponse {
  ok: boolean;
  strategy_id: string;
  proposal_id: string | null;
  validation: StrategyValidationReport | null;
  files: string[];
}

export interface StrategyTuningGenerationRequest {
  strategy_id: string;
  prompt?: string;
  cron?: string;
  every_seconds?: number;
  objectives?: string[];
  require_backtest?: boolean;
  require_shadow_run?: boolean;
}

export interface StrategyTuningGenerationResponse {
  ok: boolean;
  strategy_id: string;
  proposal_id: string | null;
  files: string[];
}
