"""Small shared runtime seam for root and child agent loops.

The provider/tool engines are still owned by their callers while the
completion decision is deliberately supplied by the caller.  This module is
the compatibility seam used during the root/child convergence work; it does
not know about strategies, teams, wallets, or trading.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field, replace
from enum import Enum
from typing import Any, Callable, Generic, Mapping, Protocol, TypeVar


class GateStatus(str, Enum):
    """The only lifecycle decisions a completion gate may make."""

    COMPLETE = "complete"
    CONTINUE = "continue"
    BLOCKED = "blocked"


MAX_GATE_FEEDBACK_CHARS = 8_000


@dataclass(frozen=True, slots=True)
class GateDecision:
    """Normalized completion-gate result.

    ``feedback`` is data for the next model round; it is never interpreted by
    the shared runtime as a workflow instruction.  That keeps domain policy in
    the caller-owned gate.
    """

    status: str = GateStatus.COMPLETE.value
    feedback: str = ""
    reason: str = ""

    def __post_init__(self) -> None:
        raw_status = self.status.value if isinstance(self.status, GateStatus) else self.status
        status = str(raw_status or GateStatus.COMPLETE.value).strip().lower()
        if status not in {item.value for item in GateStatus}:
            raise ValueError(f"unknown completion-gate status: {status!r}")
        object.__setattr__(self, "status", status)
        feedback = str(self.feedback or "").strip()
        reason = str(self.reason or "").strip()
        object.__setattr__(
            self,
            "feedback",
            feedback[:MAX_GATE_FEEDBACK_CHARS],
        )
        object.__setattr__(self, "reason", reason[:1_000])

    @classmethod
    def complete(cls, *, reason: str = "") -> "GateDecision":
        return cls(GateStatus.COMPLETE.value, reason=reason)

    @classmethod
    def continue_(cls, feedback: str, *, reason: str = "") -> "GateDecision":
        return cls(GateStatus.CONTINUE.value, feedback=feedback, reason=reason)

    @classmethod
    def blocked(cls, reason: str, *, feedback: str = "") -> "GateDecision":
        return cls(GateStatus.BLOCKED.value, feedback=feedback, reason=reason)

    @property
    def terminal(self) -> bool:
        return self.status in {
            GateStatus.COMPLETE.value,
            GateStatus.BLOCKED.value,
        }

    @property
    def is_complete(self) -> bool:
        return self.status == GateStatus.COMPLETE.value

    def asdict(self) -> dict[str, str]:
        return {
            "status": self.status,
            "feedback": self.feedback,
            "reason": self.reason,
        }


@dataclass(frozen=True, slots=True)
class TurnSnapshot:
    """Minimal, provider-neutral view exposed to a completion gate."""

    iteration: int = 0
    transcript: tuple[Any, ...] = ()
    tool_results: tuple[Any, ...] = ()
    output: Any = None
    stop_reason: str = ""
    usage: Mapping[str, Any] = field(default_factory=dict)
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "iteration", max(0, int(self.iteration or 0)))
        object.__setattr__(self, "transcript", tuple(self.transcript or ()))
        object.__setattr__(self, "tool_results", tuple(self.tool_results or ()))
        object.__setattr__(self, "stop_reason", str(self.stop_reason or ""))
        object.__setattr__(self, "usage", dict(self.usage or {}))
        object.__setattr__(self, "metadata", dict(self.metadata or {}))

    def asdict(self) -> dict[str, Any]:
        return {
            "iteration": self.iteration,
            "transcript": list(self.transcript),
            "tool_results": list(self.tool_results),
            "output": self.output,
            "stop_reason": self.stop_reason,
            "usage": dict(self.usage),
            "metadata": dict(self.metadata),
        }


class CompletionGate(Protocol):
    """Caller-owned acceptance policy for a runtime snapshot."""

    def evaluate(self, snapshot: TurnSnapshot) -> GateDecision | Any:
        ...


CompletionGateLike = CompletionGate | Callable[[TurnSnapshot], Any]


class ContinuationUnavailable(RuntimeError):
    """A caller-owned checkpoint cannot safely continue this runtime value."""

    def __init__(self, reason: str, *, feedback: str = "") -> None:
        self.reason = str(reason or "stateful_continuation_required")[:1_000]
        self.feedback = str(feedback or "")[:MAX_GATE_FEEDBACK_CHARS]
        super().__init__(self.reason)


@dataclass(frozen=True, slots=True)
class RuntimeRequest:
    """Budget and cancellation knobs for the shared adapter."""

    max_rounds: int = 2
    max_wall_seconds: float | None = None
    cancel: Any = None
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "max_rounds", max(1, int(self.max_rounds or 1)))
        if self.max_wall_seconds is not None:
            try:
                object.__setattr__(
                    self,
                    "max_wall_seconds",
                    max(0.0, float(self.max_wall_seconds)),
                )
            except (TypeError, ValueError):
                object.__setattr__(self, "max_wall_seconds", None)
        object.__setattr__(self, "metadata", dict(self.metadata or {}))


T = TypeVar("T")


@dataclass(frozen=True, slots=True)
class RuntimeResult(Generic[T]):
    """Result of the adapter, preserving the caller's native value."""

    value: T | None
    decision: GateDecision
    rounds: int
    snapshots: tuple[TurnSnapshot, ...] = ()

    @property
    def complete(self) -> bool:
        return self.decision.is_complete


def normalize_gate_decision(value: Any) -> GateDecision:
    """Accept tiny predicate adapters without spreading branching to callers."""

    if isinstance(value, GateDecision):
        return value
    if value is None or value is True:
        return GateDecision.complete()
    if value is False:
        return GateDecision.blocked("completion_gate_rejected")
    if isinstance(value, Mapping):
        status = value.get("status") or value.get("decision") or value.get("action")
        if status is None and value.get("complete") is True:
            status = GateStatus.COMPLETE.value
        if status is None:
            return GateDecision.blocked(
                "completion_gate_invalid",
                feedback=str(value.get("feedback") or value.get("message") or value.get("error") or ""),
            )
        if isinstance(status, GateStatus):
            status = status.value
        return GateDecision(
            status=str(status or GateStatus.COMPLETE.value),
            feedback=str(value.get("feedback") or value.get("message") or ""),
            reason=str(value.get("reason") or value.get("error") or ""),
        )
    if isinstance(value, str):
        status = value.strip().lower()
        if status in {item.value for item in GateStatus}:
            return GateDecision(status)
        return GateDecision.blocked("completion_gate_invalid", feedback=value)
    raise TypeError(
        "completion gate must return GateDecision, bool, mapping, or status string"
    )


def evaluate_completion_gate(
    gate: CompletionGateLike | None,
    snapshot: TurnSnapshot,
) -> GateDecision:
    """Evaluate a gate, failing closed if its implementation raises."""

    if gate is None:
        return GateDecision.complete()
    try:
        evaluator = getattr(gate, "evaluate", None)
        raw = evaluator(snapshot) if callable(evaluator) else gate(snapshot)  # type: ignore[misc]
        return normalize_gate_decision(raw)
    except Exception as exc:  # caller policy must not silently mark success
        return GateDecision.blocked(
            "completion_gate_error",
            feedback=f"{type(exc).__name__}: {exc}",
        )


def _cancelled(cancel: Any) -> bool:
    if cancel is None:
        return False
    try:
        value = getattr(cancel, "is_set", False)
        return bool(value() if callable(value) else value)
    except Exception:
        return True


class AgentRuntime(Generic[T]):
    """Shared round adapter used by root and child compatibility wrappers.

    ``execute`` owns the first provider/tool round. A caller may additionally
    provide ``continue_from`` to resume from its own checkpointed value; without
    that explicit capability a CONTINUE decision remains fail-closed. The
    adapter owns bounded continuation, cancellation, and decision normalization.
    """

    def __init__(self, *, clock: Callable[[], float] = time.monotonic) -> None:
        self._clock = clock

    def run(
        self,
        request: RuntimeRequest | None = None,
        gate: CompletionGateLike | None = None,
        *,
        execute: Callable[[str], T],
        snapshot: Callable[[T, int], TurnSnapshot] | None = None,
        continue_from: Callable[[T, str], T] | None = None,
    ) -> RuntimeResult[T]:
        request = request or RuntimeRequest()
        started = self._clock()
        snapshots: list[TurnSnapshot] = []
        feedback = ""
        value: T | None = None
        for round_index in range(request.max_rounds):
            if _cancelled(request.cancel):
                return RuntimeResult(
                    value=value,
                    decision=GateDecision.blocked("cancelled"),
                    rounds=round_index,
                    snapshots=tuple(snapshots),
                )
            if (
                request.max_wall_seconds is not None
                and self._clock() - started >= request.max_wall_seconds
            ):
                return RuntimeResult(
                    value=value,
                    decision=GateDecision.blocked("runtime_wall_time_exceeded"),
                    rounds=round_index,
                    snapshots=tuple(snapshots),
                )
            if round_index == 0:
                value = execute(feedback)
            else:
                if value is None or continue_from is None:
                    return RuntimeResult(
                        value=value,
                        decision=GateDecision.blocked(
                            "stateful_continuation_required",
                            feedback=feedback,
                        ),
                        rounds=round_index,
                        snapshots=tuple(snapshots),
                    )
                try:
                    value = continue_from(value, feedback)
                except ContinuationUnavailable as exc:
                    return RuntimeResult(
                        value=value,
                        decision=GateDecision.blocked(
                            exc.reason,
                            feedback=exc.feedback or feedback,
                        ),
                        rounds=round_index,
                        snapshots=tuple(snapshots),
                    )
                except Exception as exc:
                    return RuntimeResult(
                        value=value,
                        decision=GateDecision.blocked(
                            "continuation_error",
                            feedback=f"{type(exc).__name__}: {exc}",
                        ),
                        rounds=round_index,
                        snapshots=tuple(snapshots),
                    )
            try:
                current = (
                    snapshot(value, round_index)
                    if snapshot is not None
                    else TurnSnapshot(iteration=round_index, output=value)
                )
            except Exception as exc:
                return RuntimeResult(
                    value=value,
                    decision=GateDecision.blocked(
                        "snapshot_error",
                        feedback=f"{type(exc).__name__}: {exc}",
                    ),
                    rounds=round_index + 1,
                    snapshots=tuple(snapshots),
                )
            if current.iteration != round_index:
                current = replace(current, iteration=round_index)
            snapshots.append(current)
            # Host-owned cancellation and time limits outrank a late model
            # completion decision from the gate.
            if _cancelled(request.cancel):
                return RuntimeResult(
                    value=value,
                    decision=GateDecision.blocked("cancelled"),
                    rounds=round_index + 1,
                    snapshots=tuple(snapshots),
                )
            if (
                request.max_wall_seconds is not None
                and self._clock() - started >= request.max_wall_seconds
            ):
                return RuntimeResult(
                    value=value,
                    decision=GateDecision.blocked("runtime_wall_time_exceeded"),
                    rounds=round_index + 1,
                    snapshots=tuple(snapshots),
                )
            decision = evaluate_completion_gate(gate, current)
            if _cancelled(request.cancel):
                return RuntimeResult(
                    value=value,
                    decision=GateDecision.blocked("cancelled"),
                    rounds=round_index + 1,
                    snapshots=tuple(snapshots),
                )
            if (
                request.max_wall_seconds is not None
                and self._clock() - started >= request.max_wall_seconds
            ):
                return RuntimeResult(
                    value=value,
                    decision=GateDecision.blocked("runtime_wall_time_exceeded"),
                    rounds=round_index + 1,
                    snapshots=tuple(snapshots),
                )
            if decision.status != GateStatus.CONTINUE.value:
                return RuntimeResult(
                    value=value,
                    decision=decision,
                    rounds=round_index + 1,
                    snapshots=tuple(snapshots),
                )
            if continue_from is None:
                # Never restart a legacy engine from its original input. A
                # continuation callback is the explicit proof that the caller
                # owns a checkpoint and side-effect journal.
                return RuntimeResult(
                    value=value,
                    decision=GateDecision.blocked(
                        "stateful_continuation_required",
                        feedback=decision.feedback,
                    ),
                    rounds=round_index + 1,
                    snapshots=tuple(snapshots),
                )
            feedback = decision.feedback
        return RuntimeResult(
            value=value,
            decision=GateDecision.blocked("completion_gate_round_budget_exhausted"),
            rounds=len(snapshots),
            snapshots=tuple(snapshots),
        )

    # Explicit name for legacy wrappers; keeps migration call sites readable.
    run_legacy = run


# Names used by the architecture note and by early migration callers.
RunRequest = RuntimeRequest
TurnOutcome = RuntimeResult
SharedAgentRuntime = AgentRuntime


__all__ = [
    "AgentRuntime",
    "SharedAgentRuntime",
    "CompletionGate",
    "CompletionGateLike",
    "ContinuationUnavailable",
    "GateDecision",
    "GateStatus",
    "TurnSnapshot",
    "RuntimeRequest",
    "RunRequest",
    "RuntimeResult",
    "TurnOutcome",
    "evaluate_completion_gate",
    "normalize_gate_decision",
]
