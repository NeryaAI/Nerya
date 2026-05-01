"""Executor stack — durable, recoverable order/position state machines.

Plan 2026-04-29 §3.2 / §6 — every order Nerya places is owned by an
executor. Executors are persistent (the run row in
``executor_runs``) and recoverable: an unexpected restart resumes any
non-terminal executor and re-attaches it to its outstanding orders.

The first slice of executors:

* :class:`MarketOrderExecutor` — single market or limit-equivalent
  order, used by the new TradePlan path.
* :class:`PositionProtectionExecutor` — soft-runtime TP/SL/trailing
  / time-limit / partial-exit watcher attached to a position.

Both inherit from :class:`Executor` (in :mod:`.base`) and run through
:class:`ExecutorOrchestrator` (in :mod:`.orchestrator`).
"""

from .base import (
    CloseType,
    Executor,
    ExecutorConfig,
    ExecutorKind,
    ExecutorRun,
    ExecutorState,
    TERMINAL_EXECUTOR_STATES,
)
from .orchestrator import ExecutorOrchestrator
from .market_order import MarketOrderExecutor, MarketOrderConfig
from .position_protection import PositionProtectionExecutor, ProtectionExecutorConfig

__all__ = [
    "CloseType",
    "Executor",
    "ExecutorConfig",
    "ExecutorKind",
    "ExecutorRun",
    "ExecutorState",
    "ExecutorOrchestrator",
    "MarketOrderExecutor",
    "MarketOrderConfig",
    "PositionProtectionExecutor",
    "ProtectionExecutorConfig",
    "TERMINAL_EXECUTOR_STATES",
]
