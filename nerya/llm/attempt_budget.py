"""Turn-scoped budget for extra LLM/provider attempts.

A normal first provider call for each semantic loop iteration is not charged.
Every additional attempt caused by transport retry, context recovery, safety
retry, or transient final synthesis consumes the same budget.  The budget is
bound through a context variable so provider adapters and the agent loop share
one authority without introducing an agent-layer dependency into LLM plumbing.
"""

from __future__ import annotations

import contextlib
import contextvars
from dataclasses import dataclass, field
from typing import Any, Iterator, Mapping


DEFAULT_EXTRA_ATTEMPT_LIMIT = 8


@dataclass(slots=True)
class AttemptBudget:
    """Mutable bounded ledger for extra attempts within one logical turn."""

    limit: int = DEFAULT_EXTRA_ATTEMPT_LIMIT
    used: int = 0
    by_reason: dict[str, int] = field(default_factory=dict)
    denied: int = 0

    def __post_init__(self) -> None:
        self.limit = max(0, int(self.limit or 0))
        self.used = max(0, min(int(self.used or 0), self.limit))
        self.by_reason = {
            str(key): max(0, int(value or 0))
            for key, value in dict(self.by_reason or {}).items()
            if str(key)
        }
        self.denied = max(0, int(self.denied or 0))

    @property
    def remaining(self) -> int:
        return max(0, self.limit - self.used)

    @property
    def exhausted(self) -> bool:
        return self.remaining <= 0

    def claim(self, reason: str) -> bool:
        """Consume one extra attempt, returning ``False`` when exhausted."""

        key = str(reason or "extra_attempt").strip() or "extra_attempt"
        if self.used >= self.limit:
            self.denied += 1
            return False
        self.used += 1
        self.by_reason[key] = self.by_reason.get(key, 0) + 1
        return True

    def constrain(self, limit: int) -> None:
        """Tighten, but never expand, a restored checkpoint budget."""

        self.limit = min(self.limit, max(0, int(limit or 0)))

    def asdict(self) -> dict[str, Any]:
        return {
            "limit": self.limit,
            "used": self.used,
            "remaining": self.remaining,
            "by_reason": dict(sorted(self.by_reason.items())),
            "denied": self.denied,
        }

    @classmethod
    def from_dict(
        cls,
        value: Mapping[str, Any] | None,
        *,
        default_limit: int = DEFAULT_EXTRA_ATTEMPT_LIMIT,
    ) -> "AttemptBudget":
        data = dict(value or {})
        return cls(
            limit=int(data.get("limit", default_limit) or 0),
            used=int(data.get("used") or 0),
            by_reason={
                str(key): int(count or 0)
                for key, count in dict(data.get("by_reason") or {}).items()
            },
            denied=int(data.get("denied") or 0),
        )


_CURRENT_ATTEMPT_BUDGET: contextvars.ContextVar[AttemptBudget | None] = (
    contextvars.ContextVar("nerya_llm_attempt_budget", default=None)
)


@contextlib.contextmanager
def attempt_budget_scope(budget: AttemptBudget | None) -> Iterator[None]:
    """Bind ``budget`` for nested provider adapter calls in this context."""

    token = _CURRENT_ATTEMPT_BUDGET.set(budget)
    try:
        yield
    finally:
        _CURRENT_ATTEMPT_BUDGET.reset(token)


def current_attempt_budget() -> AttemptBudget | None:
    return _CURRENT_ATTEMPT_BUDGET.get()


def claim_current_extra_attempt(reason: str) -> bool:
    """Claim from the bound budget; unbound callers retain legacy behavior."""

    budget = current_attempt_budget()
    return True if budget is None else budget.claim(reason)


__all__ = [
    "AttemptBudget",
    "DEFAULT_EXTRA_ATTEMPT_LIMIT",
    "attempt_budget_scope",
    "claim_current_extra_attempt",
    "current_attempt_budget",
]
