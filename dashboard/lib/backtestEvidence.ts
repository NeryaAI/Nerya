/** Custom calculations, input simulation and event dispatch do not attest trading returns. */
export function backtestEvidenceKind(meta: {
  execution_mode?: unknown; evaluation_mode?: unknown; performance_evidence?: unknown; replay?: unknown;
  kind?: unknown; backtest_kind?: unknown; replay_kind?: unknown;
  decision_replay?: unknown; provenance?: unknown;
}): "trading" | "observation" | "script_simulated" | "retrospective_replay" | "pool_price_simulation" | "custom_research" | "rate_simulation" | "lp_counterfactual" {
  const replay = object(meta.replay);
  if (replay.scope === "isolated_custom_calculation") return "custom_research";
  if ((replay.scope === "sampled_fixed_range_counterfactual" || replay.scope === "cadence_fixed_range_counterfactual") &&
    replay.strategy_execution_verified === true && replay.accounting_verified === true) return "lp_counterfactual";
  if (meta.kind === "freeform_backtest" ||
    meta.backtest_kind === "freeform_backtest" || meta.replay_kind === "strategy_sdk_freeform") return "custom_research";
  if (meta.kind === "rate_policy_replay" || meta.backtest_kind === "rate_policy_replay" ||
    replay.scope === "historical_rate_policy_simulation") return "rate_simulation";
  if (replay.scope === "historical_pool_price_simulation" &&
    replay.strategy_execution_verified === true && meta.performance_evidence === false) return "pool_price_simulation";
  const provenance = object(meta.provenance);
  // chart.json retains the producer's verified tape summary in provenance.
  // Producer projections may split the proof between the explicit replay
  // summary and the signed tape summary. Preserve both before validating it.
  const proof = { ...object(provenance.decision_tape), ...object(meta.decision_replay) };
  const retrospective = "current_model_retrospective_replay";
  if (meta.performance_evidence === false &&
    (replay.scope === retrospective || replay.agent_execution === retrospective) &&
    proof.label === retrospective && proof.decision_receipts_verified === true && proof.model_decisions_verified !== false &&
    typeof proof.decision_count === "number" && Number.isSafeInteger(proof.decision_count) && proof.decision_count > 0) return "retrospective_replay";
  const stubbed = Array.isArray(replay.mock_surface_calls) && replay.mock_surface_calls.some(call => object(call).mode === "stub");
  if (meta.execution_mode === "script" && (meta.performance_evidence === false || replay.scope === "mocked_inputs_code_replay_only" || stubbed)) return "script_simulated";
  return meta.performance_evidence === false || meta.evaluation_mode === "observation" ? "observation" : "trading";
}

const object = (value: unknown): Record<string, unknown> =>
  value && typeof value === "object" && !Array.isArray(value) ? value as Record<string, unknown> : {};

export function rateSimulationLabelPrefix(meta: unknown): "rateReplay" | "rateReplayIndex" {
  return object(object(meta).replay).accrual_basis === "aave_normalized_income" ? "rateReplayIndex" : "rateReplay";
}

export function lpCounterfactualLabelPrefix(meta: unknown): "lpCounterfactual" | "lpCounterfactualCadence" {
  return object(object(meta).replay).scope === "cadence_fixed_range_counterfactual" ? "lpCounterfactualCadence" : "lpCounterfactual";
}

// Legacy display formatting supplies these event counters even for custom calculations.
const eventCounters = new Set(["decisions", "dispatches", "skipped", "errors"]);
export function customResearchDiagnostics(metrics: Record<string, unknown>): Record<string, unknown> {
  return Object.fromEntries(Object.entries(metrics).filter(([key, value]) => !eventCounters.has(key) && (
    value === null || typeof value === "number" && Number.isFinite(value) ||
    typeof value === "string" && value.trim() !== "" && Number.isFinite(Number(value.replace(/%$/, "")))
  )));
}
