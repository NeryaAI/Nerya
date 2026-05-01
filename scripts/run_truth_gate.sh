#!/usr/bin/env bash
# Nerya release truth gate.
#
# Runs the regression surfaces the 2026-04-23 production-readiness audit
# requires to stay green before a release can honestly claim that all
# outward-facing surfaces match the runtime. Used by CI and by operators
# who want to reproduce the release gate locally.
#
# Usage:
#   bash scripts/run_truth_gate.sh          # run the canonical gate
#   bash scripts/run_truth_gate.sh --quick  # subset: only the truth gates
#
# Exit code mirrors pytest's: 0 on success, non-zero on any failure.

set -euo pipefail

cd "$(dirname "$0")/.."

MODE="${1:-full}"

# ------------------------------------------------------------ truth gates
# These scan repo artifacts for outward-facing honesty rules (Phase 6).
TRUTH_GATES=(
  tests/test_release_truth_gate.py
  tests/test_runtime_truth_gate.py
  tests/test_production_gate_phase6.py
  tests/test_no_placeholder_runtime_paths.py
)

# --------------------------------------------- per-phase regression suites
# Locked to the audit's Section 8 list so a drift in this file also means
# a drift in the documented release-gate surface.
PHASE_REGRESSION=(
  # Phase 1 — external SDK surface
  tests/test_sdk_smoke.py
  # Phase 2 — script runtime truth
  tests/test_script_context.py
  tests/test_script_sandbox.py
  # Phase 3 — self-evolution closure
  tests/test_evolution_scaffold_phase3.py
  # Phase 4 — provider / wallet capability honesty
  tests/test_wallet_capabilities_phase4.py
  tests/test_provider_capability_matrix.py
  # Core agent loop & subagents
  tests/test_agent_loop.py
  tests/test_subagent_runtime_phase3.py
  tests/test_subagent_routing.py
  # Trigger / schedule control plane
  tests/test_trigger_router.py
  tests/test_trigger_sdk.py
  tests/test_trigger_route_crud.py
  tests/test_trigger_schedule_lifecycle.py
  # Hermes parity — scheduled agent sessions (2026-04-24 plan)
  tests/test_schedule_schema_extension.py
  tests/test_scheduled_session_runner.py
  tests/test_scheduled_session_delivery.py
  tests/test_schedule_nl_parse.py
  tests/test_hermes_parity_cron_session.py
  tests/test_hermes_parity_e2e.py
  # Operator-facing prompt-driven end-to-end (skill scaffold -> route ->
  # NL schedule -> live agent turn -> self-managed schedule lifecycle)
  tests/test_agent_prompt_driven_e2e.py
  # Trading SDK + risk gate
  tests/test_trading_sdk.py
  tests/test_direct_order_sdk_risk_gate.py
  # Attribution / replay
  tests/test_attribution_phase8.py
  tests/test_scenario_replay.py
  # Indicator / feature path
  tests/test_indicators_talib.py
  tests/test_features_indicator_fusion.py
)

if [[ "$MODE" == "--quick" || "$MODE" == "quick" ]]; then
  TARGETS=("${TRUTH_GATES[@]}")
  echo "== Nerya truth gate (quick: truth-gates only) =="
else
  TARGETS=("${TRUTH_GATES[@]}" "${PHASE_REGRESSION[@]}")
  echo "== Nerya truth gate (full: truth-gates + phase regression) =="
fi

echo "target count: ${#TARGETS[@]}"
python -m pytest "${TARGETS[@]}" -q
