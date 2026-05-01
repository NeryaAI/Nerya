"""StrategyReviewer — consumes a strategy/session and produces a review record.

It uses the LLMGateway for the textual review but always produces a
structured verdict dict independent of the model, so offline/mocked
environments still work.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from ..core.config import Config
from ..core.ids import review_id
from ..core.time import now_iso
from ..llm.gateway import LLMGateway
from . import store
from .session_writer import session_dir
from .attribution import (
    attribute_session,
    execution_quality,
    subagent_contribution,
)


@dataclass
class StrategyReviewer:
    config: Config
    llm: LLMGateway

    def review_trade(self, strategy_id: str, session_id: str, *, stage: str = "immediate",
                     tier: str | None = None,
                     big_loss_threshold_usd: float = 500.0) -> dict[str, Any]:
        paths = self.config.paths
        # aggregate quick summary
        triggers = [r for r in store.read_ledger(paths, strategy_id, "triggers")
                    if r.get("session_id") == session_id]
        intents = [r for r in store.read_ledger(paths, strategy_id, "intents")
                   if r.get("session_id") == session_id]
        risks = [r for r in store.read_ledger(paths, strategy_id, "risk")
                 if r.get("session_id") == session_id]
        decisions = [r for r in store.read_ledger(paths, strategy_id, "decisions")
                     if r.get("session_id") == session_id]
        orders = [r for r in store.read_ledger(paths, strategy_id, "orders")
                  if r.get("session_id") == session_id]
        fills = [r for r in store.read_ledger(paths, strategy_id, "fills")
                 if r.get("session_id") == session_id]
        messages = [r for r in store.read_ledger(paths, strategy_id, "messages")
                    if r.get("session_id") == session_id]

        pnl_usd = _session_pnl(paths, strategy_id, session_id)

        llm_task = "trade_explanation" if stage == "close" else "strategy_review"
        requested_tier = tier
        if stage == "close" and pnl_usd is not None and pnl_usd < -big_loss_threshold_usd:
            llm_task = "large_loss_postmortem"
            requested_tier = "high"

        prompt = (
            f"Review session {session_id} of strategy {strategy_id} stage={stage}.\n"
            f"Triggers: {len(triggers)}, Intents: {len(intents)}, Risks: {len(risks)}, "
            f"Decisions: {len(decisions)}, Orders: {len(orders)}, Fills: {len(fills)}, "
            f"Messages: {len(messages)}, PnL_usd: {pnl_usd}."
        )
        result = self.llm.call(
            task=llm_task, caller="skill:strategy_review.review_trade",
            tier=requested_tier, prompt=prompt,
        )
        parsed = result.parsed if isinstance(result.parsed, dict) else {"raw": result.raw}

        try:
            attribution = attribute_session(paths, strategy_id, session_id).as_dict()
        except Exception:
            attribution = {}
        try:
            exec_quality = execution_quality(paths, strategy_id, session_id)
        except Exception:
            exec_quality = {}
        try:
            subagent_report = subagent_contribution(paths, strategy_id, session_id)
        except Exception:
            subagent_report = {}

        record = {
            "review_id": review_id(),
            "stage": stage,
            "ts": now_iso(),
            "llm_tier": result.tier,
            "llm_task": result.task,
            "summary": parsed,
            "counts": {
                "triggers": len(triggers),
                "intents": len(intents),
                "risks": len(risks),
                "decisions": len(decisions),
                "orders": len(orders),
                "fills": len(fills),
                "messages": len(messages),
            },
            "pnl_usd": pnl_usd,
            "attribution": attribution,
            "execution_quality": exec_quality,
            "subagent_contribution": subagent_report,
        }
        store.record_review(paths, strategy_id=strategy_id,
                            session_id=session_id, review=record)
        # also write review.md into the session dir
        sd = session_dir(paths, strategy_id, session_id)
        sd.mkdir(parents=True, exist_ok=True)
        (sd / "review.md").write_text(_md(record), encoding="utf-8")
        return record


def _session_pnl(paths, strategy_id: str, session_id: str) -> float | None:
    """Sum realized PnL for a session from the pnl ledger, falling back to
    the last `realized_usd` or `realized_pnl_usd` entry if multiple exist."""
    rows = [r for r in store.read_ledger(paths, strategy_id, "pnl")
            if r.get("session_id") == session_id]
    if not rows:
        return None
    total = 0.0
    for r in rows:
        pnl = r.get("pnl", {})
        total += float(pnl.get("realized_usd", pnl.get("realized_pnl_usd", 0.0)) or 0.0)
    return total


def _md(record: dict[str, Any]) -> str:
    summary = record.get("summary") or {}
    return (
        f"# Review — {record['review_id']}\n"
        f"- Stage: {record['stage']}\n"
        f"- LLM tier: {record['llm_tier']} / task {record['llm_task']}\n\n"
        f"## Summary\n\n```json\n{summary}\n```\n"
    )
