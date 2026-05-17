export type EvolutionSignal = {
  id: string;
  ts: string;
  source: string;
  kind: string;
  severity: "info" | "warn" | "critical" | string;
  strategy_id?: string | null;
  evidence_refs: string[];
  summary: string;
  dedupe_key: string;
  confidence: number;
  metadata?: Record<string, unknown>;
};

export type EvolutionEvent = {
  id: string;
  ts: string;
  parent_id?: string | null;
  signals: string[];
  genes_used: string[];
  proposal_id?: string | null;
  mutation_scope: string[];
  validation_status: string;
  outcome: string;
  outcome_score: number;
  strategy_id?: string | null;
  summary: string;
  evidence_refs: string[];
  metadata?: Record<string, unknown>;
};

export type EvolutionAsset = {
  kind: "gene" | "capsule" | string;
  id: string;
  summary?: string;
  category?: string;
  signals_match?: string[];
  strategy_id?: string | null;
  confidence?: number;
  outcome_score?: number;
  evidence_refs?: string[];
  [key: string]: unknown;
};

export type EvolutionAssetCandidate = {
  id: string;
  kind: "gene" | "capsule" | string;
  summary: string;
  payload: Record<string, unknown>;
  evidence_refs: string[];
  source_event_id?: string | null;
  strategy_id?: string | null;
  state: string;
  safe_to_promote: boolean;
  blocked_reasons: string[];
  ts: string;
};

export type EvolutionSignalsEnvelope = {
  signals: EvolutionSignal[];
  count: number;
  collected?: EvolutionSignal[];
};

export type EvolutionEventsEnvelope = {
  events: EvolutionEvent[];
  count: number;
};

export type EvolutionAssetsEnvelope = {
  assets: EvolutionAsset[];
  candidates: EvolutionAssetCandidate[];
  count: number;
  candidate_count: number;
};

export type EvolutionTimelineItem = {
  id: string;
  record_id: string;
  type: "signal" | "event" | "proposal" | "validation" | "asset_candidate" | "asset" | string;
  stage: "signal" | "reflection" | "proposal" | "validation" | "outcome" | "asset" | string;
  ts: string;
  title: string;
  summary: string;
  status: string;
  severity?: string;
  source?: string | null;
  outcome?: string | null;
  strategy_id?: string | null;
  proposal_id?: string | null;
  validation_plan_id?: string | null;
  validation_status?: string | null;
  source_event_id?: string | null;
  signal_ids?: string[];
  asset_ids?: string[];
  evidence_refs?: string[];
  blocked_reasons?: string[];
  why?: string;
  next_step?: string;
  outcome_score?: number;
  process?: EvolutionProcessTrace;
  raw?: Record<string, unknown>;
  [key: string]: unknown;
};

export type EvolutionProcessArtifact = {
  id: string;
  title: string;
  kind: "prompt" | "input" | "output" | "document" | "proposal" | "validation" | string;
  path?: string;
  language?: string;
  size?: number;
  preview?: string;
  truncated?: boolean;
  redacted?: boolean;
  metadata?: Record<string, unknown>;
};

export type EvolutionProcessSection = {
  id: string;
  title: string;
  summary?: string;
  artifacts: EvolutionProcessArtifact[];
};

export type EvolutionProcessTrace = {
  has_prompt?: boolean;
  has_inputs?: boolean;
  has_outputs?: boolean;
  has_generated_docs?: boolean;
  has_file_changes?: boolean;
  has_validation?: boolean;
  sections: EvolutionProcessSection[];
  artifacts?: EvolutionProcessArtifact[];
};

export type EvolutionTimelineSummary = {
  signals: number;
  events: number;
  assets: number;
  capsules: number;
  candidates: number;
  blocked_candidates: number;
  proposals: number;
  open_proposals: number;
  validation_plans: number;
  blocked_validation_plans: number;
  terminal_outcomes: number;
  timeline_items: number;
  last_activity_ts?: string | null;
};

export type EvolutionConfigSnapshot = {
  hooks: {
    enabled: boolean;
    sources: string[];
  };
  signal_collection: {
    manual_refresh_endpoint: string;
    reflection_endpoint: string;
    dedupe_window: number;
  };
  memory_quality_gate: {
    enabled: boolean;
    minimum_score: number;
    requires_evidence_refs: boolean;
    blocks_possible_secrets: boolean;
  };
  validation: {
    dry_run_only: boolean;
    execution_enabled: boolean;
    allowed_step_types: string[];
  };
  strategy_tuning: {
    total_strategies: number;
    enabled_strategies: number;
    strategies: Array<Record<string, unknown> & { strategy_id: string; enabled: boolean }>;
  };
  periodic_reflection: EvolutionPeriodicReflectionSchedule;
};

export type EvolutionPeriodicReflectionSchedule = {
  id: string;
  kind: string;
  target: string;
  enabled: boolean;
  configured: boolean;
  cron?: string | null;
  time?: string | null;
  timezone?: string | null;
  payload?: Record<string, unknown>;
};

export type EvolutionTimelineEnvelope = {
  ok: boolean;
  timeline: EvolutionTimelineItem[];
  summary: EvolutionTimelineSummary;
  config: EvolutionConfigSnapshot;
  raw: {
    signals: EvolutionSignal[];
    events: EvolutionEvent[];
    proposals: Array<Record<string, unknown>>;
    assets: EvolutionAsset[];
    candidates: EvolutionAssetCandidate[];
    validation_plans: Array<Record<string, unknown>>;
    strategy_audits?: Array<Record<string, unknown>>;
  };
};
