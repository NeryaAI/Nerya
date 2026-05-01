"""Executor base class (04-29 §3.2).

The state machine is shared by every executor kind:

```
created -> reserving -> ready -> submitted -> working -> closing -> done
                               |          |         |
                               |          |         -> failed
                               |          -> canceling -> canceled
                               -> rejected
```

Subclasses override :meth:`prepare` (called once before transitioning
out of ``ready``), :meth:`step` (the work-tick) and optionally
:meth:`on_cancel`. The orchestrator (see :mod:`.orchestrator`) drives
the lifecycle and persists every transition to the ``executor_runs``
table.
"""

from __future__ import annotations

import logging
import time
from abc import ABC, abstractmethod
from dataclasses import asdict, dataclass, field
from typing import Any, Literal

from ...core.ids import executor_id as _new_executor_id
from ...core.paths import WorkspacePaths

log = logging.getLogger(__name__)


ExecutorKind = Literal[
    "market_order",
    "limit_order",
    "limit_chaser",
    "twap",
    "position_protection",
    "rebalance",
]

ExecutorState = Literal[
    "created",
    "reserving",
    "ready",
    "submitted",
    "working",
    "closing",
    "canceling",
    "canceled",
    "done",
    "failed",
    "rejected",
]

CloseType = Literal[
    "take_profit",
    "stop_loss",
    "trailing_stop",
    "time_limit",
    "manual_cancel",
    "operator_flatten",
    "risk_kill_switch",
    "insufficient_balance",
    "lost_order_recovered",
    "external_position_change",
    "filled",
    "failed",
    "",  # not yet terminal
]

TERMINAL_EXECUTOR_STATES: tuple[ExecutorState, ...] = (
    "canceled", "done", "failed", "rejected",
)


@dataclass
class ExecutorConfig:
    """Base config every executor stores in its ``config_json`` row."""

    kind: ExecutorKind
    account_id: str
    strategy_id: str
    market: str
    extra: dict[str, Any] = field(default_factory=dict)

    def asdict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class ExecutorRun:
    """Persistent state-machine row for a single executor invocation."""

    executor_id: str
    kind: ExecutorKind
    account_id: str
    strategy_id: str
    market: str
    state: ExecutorState
    close_type: CloseType = ""
    retries: int = 0
    last_heartbeat: float | None = None
    plan_json: dict[str, Any] = field(default_factory=dict)
    config_json: dict[str, Any] = field(default_factory=dict)
    result_json: dict[str, Any] = field(default_factory=dict)
    order_ids: list[str] = field(default_factory=list)
    reservation_ids: list[str] = field(default_factory=list)
    position_id: str | None = None
    protection_id: str | None = None
    intent_id: str | None = None
    plan_id: str | None = None
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)
    terminal_at: float | None = None

    @property
    def is_terminal(self) -> bool:
        return self.state in TERMINAL_EXECUTOR_STATES

    def asdict(self) -> dict[str, Any]:
        return asdict(self)


# ---------------------------------------------------------------------------
# Executor base
# ---------------------------------------------------------------------------


class Executor(ABC):
    """Lifecycle owner of one or more orders.

    Subclasses implement :meth:`prepare`, :meth:`step`, and optionally
    :meth:`on_cancel`. They never call SQLite directly; the
    orchestrator persists transitions on their behalf.
    """

    kind: ExecutorKind = "market_order"

    def __init__(self, run: ExecutorRun, paths: WorkspacePaths):
        self.run = run
        self.paths = paths

    @classmethod
    def new(
        cls,
        *,
        account_id: str,
        strategy_id: str,
        market: str,
        config: ExecutorConfig,
        plan: dict[str, Any] | None = None,
        intent_id: str | None = None,
        plan_id: str | None = None,
        position_id: str | None = None,
        protection_id: str | None = None,
        paths: WorkspacePaths,
    ) -> "Executor":
        run = ExecutorRun(
            executor_id=_new_executor_id(),
            kind=cls.kind,
            account_id=account_id,
            strategy_id=strategy_id,
            market=market,
            state="created",
            config_json=config.asdict(),
            plan_json=dict,
            intent_id=intent_id,
            plan_id=plan_id,
            position_id=position_id,
            protection_id=protection_id,
        )
        return cls(run, paths)

    # -- subclass hooks ---------------------------------------------------------
    @abstractmethod
    def prepare(self) -> None:
        """Allocate reservations, build OrderCandidate, etc."""

    @abstractmethod
    def step(self) -> bool:
        """Drive one work tick. Return ``True`` when the executor is
        terminal (``state in TERMINAL_EXECUTOR_STATES``)."""

    def on_cancel(self) -> None:
        """Called when the orchestrator transitions us into
        ``canceling``. Subclasses should cancel any active orders and
        release reservations."""

    # -- helpers (called by orchestrator/subclasses) ---------------------------
    def transition(self, new_state: ExecutorState, *, close_type: CloseType = "") -> None:
        self.run.state = new_state
        self.run.updated_at = time.time()
        if new_state in TERMINAL_EXECUTOR_STATES:
            self.run.terminal_at = self.run.updated_at
        if close_type:
            self.run.close_type = close_type

    def attach_order(self, order_id: str) -> None:
        if order_id and order_id not in self.run.order_ids:
            self.run.order_ids.append(order_id)

    def attach_reservation(self, reservation_id: str) -> None:
        if reservation_id and reservation_id not in self.run.reservation_ids:
            self.run.reservation_ids.append(reservation_id)

    def heartbeat(self) -> None:
        self.run.last_heartbeat = time.time()

    def store_result(self, payload: dict[str, Any]) -> None:
        merged = dict(self.run.result_json or {})
        merged.update(payload)
        self.run.result_json = merged


__all__ = [
    "CloseType",
    "Executor",
    "ExecutorConfig",
    "ExecutorKind",
    "ExecutorRun",
    "ExecutorState",
    "TERMINAL_EXECUTOR_STATES",
]
