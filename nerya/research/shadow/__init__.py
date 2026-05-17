"""Shadow runtime.

The shadow runtime replays a strategy candidate's signals against
fixture (or paper-snapshot) market data, **without** writing to the
paper or live ledgers. It produces a structured ``ShadowReport`` that
the promotion gate consumes to decide whether ``canary -> live`` is
allowed.

Design rules:

* Shadow code never touches connectors. It reads market data via the
  research dataset router, just like the backtester.
* Shadow signals run through ``IntentCandidate`` and the Risk Gate
  *compatibility* checks (no actual orders, no ledger mutations).
* All artifacts land under
  ``workspace/strategies/<strategy_id>/shadow/runs/<run_id>/`` and are
  fully isolated from paper/live ledgers.
* Tests use fixture data only.
"""
from __future__ import annotations

from .models import ShadowEvent, ShadowFill, ShadowReport, ShadowRun
from .runtime import ShadowRuntime, run_shadow
from .store import ShadowStore

__all__ = [
    "ShadowEvent",
    "ShadowFill",
    "ShadowReport",
    "ShadowRun",
    "ShadowRuntime",
    "ShadowStore",
    "run_shadow",
]
