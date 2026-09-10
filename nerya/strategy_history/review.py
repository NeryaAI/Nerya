"""Evidence-first session review; no inferred workflow or implicit tier escalation."""
from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from ..core.atomic_write import atomic_write_text
from ..core.config import Config
from ..core.ids import review_id
from ..core.redaction import redact_display_dict
from ..core.time import now_iso
from ..llm.gateway import LLMGateway
from ..security.prompt_injection import wrap_untrusted
from . import store
from .session_writer import session_dir
from .attribution import attribute_session, execution_quality, subagent_contribution


@dataclass
class StrategyReviewer:
    config: Config
    llm: LLMGateway

    def review_trade(self, strategy_id: str, session_id: str, *,
                     stage: str = "immediate", tier: str | None = None) -> dict[str, Any]:
        paths = self.config.paths
        bundle = attribute_session(paths, strategy_id, session_id)
        if not bundle.evidence_refs:
            raise ValueError("cannot review a session with no attributable ledger evidence")
        # Derive every diagnostic from the exact snapshot supplied to the model.
        diagnostics = {
            "attribution": bundle.as_dict(),
            "execution_quality": execution_quality(paths, strategy_id, session_id, ledgers=bundle.evidence),
            "subagent_contribution": subagent_contribution(paths, strategy_id, session_id, ledgers=bundle.evidence),
        }
        evidence = redact_display_dict(diagnostics)
        rid = review_id()
        sd = session_dir(paths, strategy_id, session_id)
        evidence_path = sd / f"{rid}.evidence.json"
        atomic_write_text(evidence_path, json.dumps(evidence, ensure_ascii=False, indent=2, default=str))
        prompt = (
            f"Review strategy={strategy_id}, session={session_id}, stage={stage}.\n"
            "Use the frozen ledger evidence below. Distinguish observations, hypotheses, "
            "and unobserved outcomes. Not trading or a risk rejection is not itself a defect. "
            "Explain plausible alternatives and evidence needed to distinguish them. "
            "Do not infer causality from raw PnL, tool counts, or unmatched paper/live samples. "
            "Return one JSON object with summary, evidence references, uncertainties, and "
            "testable follow-up actions; no change is a valid recommendation. "
            "Never propose bypassing configured risk, approval or live-mode boundaries.\n"
            + wrap_untrusted("session_evidence", json.dumps(evidence, ensure_ascii=False, default=str))
        )
        result = self.llm.call(
            task="trade_explanation" if stage == "close" else "strategy_review",
            caller="skill:strategy_review.review_trade", tier=tier, prompt=prompt,
        )
        parsed = result.parsed
        if not isinstance(parsed, dict) or not str(parsed.get("summary") or "").strip():
            raise ValueError("strategy review must return a structured, non-empty summary")
        record = {
            "review_id": rid, "stage": stage, "ts": now_iso(),
            "llm_tier": result.tier, "llm_task": result.task,
            "summary": redact_display_dict(parsed), "counts": bundle.counts,
            "pnl_usd": bundle.pnl_usd, **evidence,
            "evidence_snapshot": str(evidence_path.relative_to(paths.root)),
        }
        store.record_review(paths, strategy_id=strategy_id, session_id=session_id, review=record)
        atomic_write_text(sd / "review.md", _md(record))
        return record


def _md(record: dict[str, Any]) -> str:
    return (
        f"# Review — {record['review_id']}\n"
        f"- Stage: {record['stage']}\n"
        f"- LLM tier: {record['llm_tier']} / task {record['llm_task']}\n"
        f"- Evidence: {record['evidence_snapshot']}\n\n"
        f"## Summary\n\n```json\n{json.dumps(record['summary'], ensure_ascii=False, indent=2)}\n```\n"
    )
