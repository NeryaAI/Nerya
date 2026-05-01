"""Nerya research package.

Implements the strategy research, validation, backtest, shadow runtime and
swarm primitives described in
``docs/plans/2026-04-25-nerya-vibetrading-deep-optimization-plan.md``.

The research layer is intentionally network-free at test time. All entry
points convert agent-authored signal engines into ``IntentCandidate``
objects, run deterministic fixture backtests and emit ``ValidationReport``
artifacts under ``workspace/strategies/<strategy_id>/candidates/<candidate_id>/``.

It MUST NOT import from ``../Vibe-Trading``. Vibe-Trading remains a
reference repository only — its contracts are reimplemented inside Nerya
so the runtime keeps a single source of truth.
"""
from __future__ import annotations

from .promotion_gate import (
    Blocker,
    evaluate_promotion,
    required_for_transition,
)
from .schemas import BacktestConfig, BacktestConfigError
from .validation_report import ValidationReport, ValidationStatus

__all__ = [
    "BacktestConfig",
    "BacktestConfigError",
    "Blocker",
    "ValidationReport",
    "ValidationStatus",
    "evaluate_promotion",
    "required_for_transition",
]
