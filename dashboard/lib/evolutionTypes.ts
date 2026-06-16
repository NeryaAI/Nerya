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

export type EvolutionPostApplyWeightedSummary = {
  status?: string;
  observed_at?: string;
  count?: number;
  by_status?: Record<string, number>;
  by_source?: Record<string, number>;
  weighted_by_status?: Record<string, number>;
  weighted_by_source?: Record<string, number>;
  weighted_negative_count?: number;
  weighted_healthy_count?: number;
  weighted_observing_count?: number;
  decay?: {
    half_life_days?: number;
    source_weight_cap?: number;
    anchor_observed_at?: string | null;
    [key: string]: unknown;
  };
  [key: string]: unknown;
};

export type EvolutionGdiBreakdown = {
  version?: string;
  score?: number;
  polarity?: "positive" | "negative" | string;
  components?: Record<string, number>;
  matched_signals?: string[];
  relevance?: {
    version?: string;
    score?: number;
    matched_signals?: string[];
    trigger_signal_kinds?: string[];
    source?: string;
    gene_id?: string | null;
    matched_context?: Record<string, string[]>;
    [key: string]: unknown;
  };
  usage_count?: number;
  post_apply_status?: string;
  post_apply_weighted?: EvolutionPostApplyWeightedSummary | null;
  rationale?: string;
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
  gdi?: EvolutionGdiBreakdown;
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
  promotion_gates?: {
    version?: string;
    can_promote?: boolean;
    review_only_until_promoted?: boolean;
    selector_eligible?: boolean;
    checks?: Array<{
      id?: string;
      status?: string;
      summary?: string;
      reasons?: string[];
      [key: string]: unknown;
    }>;
    blockers?: string[];
    warnings?: string[];
    [key: string]: unknown;
  };
  ts: string;
  [key: string]: unknown;
};

export type EvolutionOptimizerFeedbackFeature = {
  feature?: string;
  positive?: number;
  negative?: number;
  net?: number;
  samples?: number;
  sources?: Record<string, number>;
  examples?: Array<Record<string, unknown>>;
  [key: string]: unknown;
};

export type EvolutionOptimizerFeedbackExample = {
  proposal_id?: string;
  run_id?: string;
  strategy_id?: string | null;
  state?: string;
  selected_candidate_id?: string | null;
  selected_score?: number;
  candidate_status?: string | null;
  feedback_sample_count?: number;
  [key: string]: unknown;
};

export type EvolutionOptimizerCandidateDecision = {
  candidate_id?: string;
  asset_kind?: string;
  state?: string;
  decision?: string;
  operator?: string | null;
  decided_at?: string;
  strategy_id?: string | null;
  summary?: string;
  promoted_ref?: string | null;
  rejected_reason?: string | null;
  optimizer_run_id?: string | null;
  optimizer_candidate_id?: string | null;
  preview_type?: string | null;
  preview_status?: string | null;
  selected_by_optimizer?: boolean;
  outcome_score?: number;
  evidence_refs?: string[];
  [key: string]: unknown;
};

export type EvolutionOptimizerCandidateDecisionSummary = {
  version?: string;
  total?: number;
  promoted?: number;
  rejected?: number;
  recent?: EvolutionOptimizerCandidateDecision[];
  evidence_refs?: string[];
  [key: string]: unknown;
};

export type EvolutionOptimizerCalibration = {
  version?: string;
  status?: string;
  confidence?: string;
  warnings?: string[];
  run_count?: number;
  sample_count?: number;
  source_mix?: {
    proposal_samples?: number;
    candidate_decision_samples?: number;
    proposal_ratio?: number;
    candidate_decision_ratio?: number;
    [key: string]: unknown;
  };
  polarity_mix?: {
    positive_samples?: number;
    negative_samples?: number;
    neutral_samples?: number;
    positive_ratio?: number;
    negative_ratio?: number;
    neutral_ratio?: number;
    [key: string]: unknown;
  };
  candidate_decision_mix?: {
    total?: number;
    promoted?: number;
    rejected?: number;
    promoted_ratio?: number;
    rejected_ratio?: number;
    [key: string]: unknown;
  };
  feature_concentration?: {
    top_abs_net?: number;
    total_abs_net?: number;
    top_feature_ratio?: number;
    feature_count?: number;
    [key: string]: unknown;
  };
  [key: string]: unknown;
};

export type EvolutionOptimizerFeedbackSummary = {
  version?: string;
  strategy_id?: string | null;
  run_count?: number;
  sample_count?: number;
  positive_samples?: number;
  negative_samples?: number;
  neutral_samples?: number;
  top_positive_features?: EvolutionOptimizerFeedbackFeature[];
  top_negative_features?: EvolutionOptimizerFeedbackFeature[];
  recent_examples?: EvolutionOptimizerFeedbackExample[];
  candidate_decisions?: EvolutionOptimizerCandidateDecisionSummary;
  calibration?: EvolutionOptimizerCalibration;
  evidence_refs?: string[];
  [key: string]: unknown;
};

export type EvolutionCandidateValidationPreview = {
  version?: string;
  status?: string;
  reason?: string;
  score_delta?: number;
  requested_step_types?: string[];
  executed_step_types?: string[];
  deferred_step_types?: string[];
  blocked_reasons?: string[];
  warning_count?: number;
  blocker_count?: number;
  evidence_refs?: string[];
  validation?: {
    ok?: boolean;
    blockers?: Array<Record<string, unknown>>;
    warnings?: Array<Record<string, unknown>>;
    [key: string]: unknown;
  };
  preview_policy?: Record<string, unknown>;
  [key: string]: unknown;
};

export type EvolutionCandidateBacktestPreview = {
  version?: string;
  status?: string;
  reason?: string;
  score_delta?: number;
  preset?: string;
  allow_mock?: boolean;
  blocked_reasons?: string[];
  evidence_refs?: string[];
  backtest_result?: Record<string, unknown>;
  baseline_comparison?: (EvolutionBacktestComparison & {
    overall_direction?: string;
    score_delta?: number;
    critical_regressed?: string[];
    candidate?: Record<string, unknown>;
  });
  artifacts?: Array<Record<string, unknown>>;
  preview_policy?: Record<string, unknown>;
  [key: string]: unknown;
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
  optimizer_feedback?: EvolutionOptimizerFeedbackSummary | null;
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
  fitness_vector?: EvolutionFitnessVector;
  post_apply_monitor?: EvolutionPostApplyMonitor | null;
  why_reused?: EvolutionWhyReused | null;
  action_gates?: EvolutionActionGates | null;
  lineage_graph?: EvolutionLineageGraph | null;
  optimizer_report?: EvolutionOptimizerReport | null;
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
  run?: EvolutionProcessRun | null;
  has_prompt?: boolean;
  has_inputs?: boolean;
  has_outputs?: boolean;
  has_generated_docs?: boolean;
  has_file_changes?: boolean;
  has_validation?: boolean;
  sections: EvolutionProcessSection[];
  artifacts?: EvolutionProcessArtifact[];
};

export type EvolutionProcessRun = {
  subagent?: string | null;
  tier?: string | null;
  provider?: string | null;
  model?: string | null;
  ok?: boolean | null;
  tokens?: number | null;
  usd?: number | null;
  wall_ms?: number | null;
  model_calls?: Array<Record<string, unknown>>;
  redacted?: boolean;
};

export type EvolutionOptimizerCandidate = {
  candidate_id?: string | null;
  index?: number;
  score?: number;
  status?: string;
  summary?: string;
  accepted_count?: number;
  dropped_count?: number;
  materialized_count?: number;
  unmaterialized_count?: number;
  accepted_targets?: string[];
  materialized_files?: string[];
  validation_status?: string;
  validation_types?: string[];
  blocked_reasons?: string[];
  risk_flags?: string[];
  reasons?: string[];
  warnings?: string[];
  outcome_feedback?: {
    version?: string;
    score_delta?: number;
    raw_score_delta?: number;
    calibration_scale?: number;
    calibration_status?: string;
    calibration_confidence?: string;
    calibration_warnings?: string[];
    sample_count?: number;
    matched_features?: EvolutionOptimizerFeedbackFeature[];
    [key: string]: unknown;
  };
  asset_candidate?: {
    id?: string;
    kind?: string;
    safe_to_promote?: boolean;
    blocked_reasons?: string[];
    evidence_refs?: string[];
    preview_type?: string;
    preview_status?: string;
    selected_by_optimizer?: boolean;
    outcome_score?: number;
    promotion_gates?: EvolutionAssetCandidate["promotion_gates"];
    [key: string]: unknown;
  };
  validation_preview?: EvolutionCandidateValidationPreview;
  backtest_preview?: EvolutionCandidateBacktestPreview;
  [key: string]: unknown;
};

export type EvolutionOptimizerReport = {
  version?: string;
  candidate_count?: number;
  evaluated_count?: number;
  truncated?: boolean;
  selected_candidate_id?: string | null;
  selected_index?: number;
  selected_score?: number;
  selection_reason?: string;
  outcome_feedback?: {
    version?: string;
    sample_count?: number;
    positive_samples?: number;
    negative_samples?: number;
    neutral_samples?: number;
    calibration?: EvolutionOptimizerCalibration;
    top_features?: EvolutionOptimizerFeedbackFeature[];
    examples?: Array<Record<string, unknown>>;
    [key: string]: unknown;
  };
  validation_preview?: {
    version?: string;
    top_k?: number;
    previewed_count?: number;
    passed_count?: number;
    failed_count?: number;
    skipped_count?: number;
    executed_step_types?: string[];
    policy?: Record<string, unknown>;
    [key: string]: unknown;
  };
  backtest_preview?: {
    version?: string;
    top_k?: number;
    previewed_count?: number;
    passed_count?: number;
    failed_count?: number;
    no_data_count?: number;
    skipped_count?: number;
    policy?: Record<string, unknown>;
    [key: string]: unknown;
  };
  candidates?: EvolutionOptimizerCandidate[];
  [key: string]: unknown;
};

export type EvolutionEvidenceArtifact = {
  id?: string;
  title: string;
  kind?: string;
  path?: string;
  language?: string;
  size?: number;
  preview?: string;
  truncated?: boolean;
  redacted?: boolean;
  metadata?: Record<string, unknown>;
};

export type EvolutionEvidenceItem = {
  ref: string;
  type: string;
  resolved: boolean;
  title: string;
  summary?: string;
  reason?: string;
  path?: string;
  record?: unknown;
  artifacts?: EvolutionEvidenceArtifact[];
  metadata?: Record<string, unknown>;
};

export type EvolutionEvidenceResolveEnvelope = {
  ok: boolean;
  count: number;
  items: EvolutionEvidenceItem[];
};

export type EvolutionProposalFileChange = {
  path: string;
  before_path?: string;
  after_path?: string;
  before_exists?: boolean;
  before?: string;
  after?: string;
  diff?: string;
  before_truncated?: boolean;
  after_truncated?: boolean;
};

export type EvolutionBacktestRunSummary = {
  backtest_id?: string;
  metrics_path?: string;
  report_path?: string | null;
  chart_path?: string | null;
  metrics?: Record<string, unknown>;
};

export type EvolutionBacktestMetricDelta = {
  key: string;
  before: number;
  after: number;
  delta: number;
  direction: "improved" | "regressed" | "flat" | string;
};

export type EvolutionBacktestComparison = {
  strategy_id?: string;
  status: "complete" | "missing_before" | "missing_after" | "missing_both" | string;
  summary?: string;
  before?: EvolutionBacktestRunSummary | null;
  after?: EvolutionBacktestRunSummary | null;
  metrics_delta?: EvolutionBacktestMetricDelta[];
  evidence_refs?: string[];
};

export type EvolutionFitnessDimension = {
  id: string;
  label?: string;
  status: string;
  summary?: string;
  score?: number;
  blockers?: string[];
  warnings?: string[];
  evidence_refs?: string[];
  details?: Record<string, unknown>;
};

export type EvolutionFitnessVector = {
  version: string;
  status: string;
  summary?: string;
  dimensions: EvolutionFitnessDimension[];
  blockers?: string[];
  warnings?: string[];
  evidence_refs?: string[];
  ready_for_approval?: boolean;
};

export type EvolutionPostApplyObservation = {
  id?: string;
  proposal_id?: string;
  status?: string;
  summary?: string;
  source?: string;
  observed_at?: string;
  evidence_refs?: string[];
  metrics?: Record<string, unknown>;
  backtest_result?: Record<string, unknown>;
  run_id?: string;
  operator?: string;
  metadata?: Record<string, unknown>;
  journal_ref?: string;
  [key: string]: unknown;
};

export type EvolutionPostApplyMonitor = {
  status: string;
  summary?: string;
  observed_at?: string;
  evidence_refs?: string[];
  latest?: EvolutionPostApplyObservation;
  observations?: EvolutionPostApplyObservation[];
  weighted_summary?: EvolutionPostApplyWeightedSummary | null;
};

export type EvolutionActionGates = {
  version?: string;
  can_apply?: boolean;
  blockers?: string[];
  warnings?: string[];
  state?: string;
  kind?: string;
  materialization?: {
    required?: boolean;
    after_file_count?: number;
    paths?: string[];
    advisory_only?: boolean;
    [key: string]: unknown;
  };
  evidence?: {
    required?: boolean;
    count?: number;
    refs?: string[];
    [key: string]: unknown;
  };
  validation?: {
    ok?: boolean;
    required?: boolean;
    source?: string;
    plan_id?: string;
    status?: string;
    reason?: string | null;
    blocked_reasons?: string[];
    failed_required_steps?: string[];
    missing_evidence_steps?: string[];
    evidence_refs?: string[];
    report?: Record<string, unknown>;
    [key: string]: unknown;
  };
};

export type EvolutionLineageGraphNode = {
  id: string;
  type: string;
  label: string;
  status?: string;
  summary?: string;
  ts?: string;
  evidence_refs?: string[];
  metadata?: Record<string, unknown>;
};

export type EvolutionLineageGraphEdge = {
  id: string;
  source: string;
  target: string;
  type: string;
  label?: string;
  status?: string;
  evidence_refs?: string[];
  metadata?: Record<string, unknown>;
};

export type EvolutionLineageGraph = {
  version: string;
  root_id: string;
  nodes: EvolutionLineageGraphNode[];
  edges: EvolutionLineageGraphEdge[];
  evidence_refs?: string[];
  warnings?: string[];
  truncated?: boolean;
};

export type EvolutionWhyReusedAsset = {
  kind?: string;
  id?: string;
  summary?: string;
  signals_match?: string[];
  outcome_score?: number;
  evidence_refs?: string[];
  gdi_score?: number;
  polarity?: string;
  relevance_score?: number;
  relevance_source?: string;
  relevance_gene_id?: string | null;
  matched_signals?: string[];
  matched_context?: Record<string, string[]>;
  rationale?: string;
};

export type EvolutionWhyReusedSignal = {
  id?: string;
  kind?: string;
  severity?: string;
  summary?: string;
  confidence?: number;
  evidence_refs?: string[];
  metadata?: Record<string, unknown>;
};

export type EvolutionWhyReused = {
  version?: string;
  summary?: string;
  counts?: Record<string, number>;
  trigger_context?: Record<string, unknown>;
  selection_signals?: EvolutionWhyReusedSignal[];
  genes?: EvolutionWhyReusedAsset[];
  capsules?: EvolutionWhyReusedAsset[];
  negative_capsules?: EvolutionWhyReusedAsset[];
  proposal_diff?: {
    change_count?: number;
    paths?: string[];
    materialized?: boolean;
    advisory_only?: boolean;
    [key: string]: unknown;
  };
  validation?: {
    plan_id?: string;
    status?: string;
    summary?: string;
    backtest_status?: string;
    backtest_summary?: string;
    evidence_refs?: string[];
    [key: string]: unknown;
  } | null;
  post_apply?: {
    status?: string;
    summary?: string;
    observed_at?: string;
    observation_count?: number;
    weighted_negative_count?: number;
    weighted_healthy_count?: number;
    weighted_observing_count?: number;
    evidence_refs?: string[];
    [key: string]: unknown;
  } | null;
  evidence_refs?: string[];
};

export type EvolutionProposalDetail = {
  id: string;
  kind?: string;
  state?: string;
  summary?: string;
  ts?: string;
  path?: string;
  target?: string | null;
  evidence_refs?: string[];
  source_event_id?: string | null;
  validation_plan_id?: string | null;
  metadata?: Record<string, unknown>;
  rationale_md?: string;
  diff_patch?: string;
  test_plan_md?: string;
  rollback_md?: string;
  files?: Record<string, string>;
  file_changes?: EvolutionProposalFileChange[];
  backtest_comparison?: EvolutionBacktestComparison | null;
  fitness_vector?: EvolutionFitnessVector;
  post_apply_monitor?: EvolutionPostApplyMonitor | null;
  why_reused?: EvolutionWhyReused | null;
  action_gates?: EvolutionActionGates | null;
  lineage_graph?: EvolutionLineageGraph | null;
  optimizer_report?: EvolutionOptimizerReport | null;
  process?: EvolutionProcessTrace;
  error?: string;
  [key: string]: unknown;
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

export type EvolutionInboxEntry = {
  id: string;
  item_id?: string;
  record_id?: string;
  type?: string;
  stage?: string;
  status?: string;
  title: string;
  summary?: string;
  ts?: string;
  strategy_id?: string | null;
  proposal_id?: string | null;
  validation_plan_id?: string | null;
  evidence_refs?: string[];
  reasons?: string[];
  next_step?: string;
};

export type EvolutionInboxGroup = {
  id: string;
  tone: "neutral" | "ok" | "warn" | "danger" | "brand" | string;
  stage?: string;
  action?: string;
  count: number;
  items: EvolutionInboxEntry[];
};

export type EvolutionInboxEnvelope = {
  total: number;
  groups: EvolutionInboxGroup[];
};

export type EvolutionConfigSnapshot = {
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
  inbox?: EvolutionInboxEnvelope;
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
    optimizer_feedback?: EvolutionOptimizerFeedbackSummary | null;
  };
};
