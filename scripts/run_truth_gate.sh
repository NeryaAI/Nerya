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

if [[ "$MODE" == "--quick" || "$MODE" == "quick" ]]; then
  echo "== Nerya truth gate (quick) =="
  "${PYTHON:-python}" -m pytest tests/test_production_gate_phase6.py -q
else
  echo "== Nerya truth gate (full regression) =="
  "${PYTHON:-python}" -m pytest -m "" tests -q
fi
