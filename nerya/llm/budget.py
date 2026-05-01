"""Daily budget enforcement, per tier."""

from __future__ import annotations

from dataclasses import dataclass

from ..core.errors import LLMBudgetExceeded


@dataclass
class BudgetPolicy:
    daily_budget_usd: dict[str, float]

    def check(self, tier: str, current_spend: float, expected_cost: float) -> None:
        cap = float(self.daily_budget_usd.get(tier, 0) or 0)
        if cap <= 0:
            return  # unmetered
        if current_spend + expected_cost > cap:
            raise LLMBudgetExceeded(
                f"{tier} budget exceeded: {current_spend:.3f} + {expected_cost:.3f} > {cap:.3f}"
            )
