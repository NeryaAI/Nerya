"""Task-class normalisation for LLM tier routing.

Task routing historically used exact string membership against
``tier.allowed_tasks``. That made the system brittle to naming drift and
forced operators to update every tier whenever a new task intent was
introduced.

This module adds a lightweight normaliser from freeform task names to
stable *task classes* (capability families) so tiers can declare:

    allowed_classes:
      - classification
      - structured_extraction
      - subagent_reasoning

and the runtime resolves ``news_filtering``, ``trigger_triage`` etc.
against the ``classification`` class without needing a bespoke entry per
task name.

Tiers can still use the original ``allowed_tasks`` for exact-match
safety. The two mechanisms are additive — task class matching is a
*relaxation* of exact matching, not a replacement.
"""

from __future__ import annotations

from typing import Iterable


# Canonical task-class identifiers.
CLASSIFICATION = "classification"
STRUCTURED_EXTRACTION = "structured_extraction"
SUBAGENT_REASONING = "subagent_reasoning"
STRATEGY_REVIEW = "strategy_review"
PROPOSAL_GENERATION = "proposal_generation"
COMPLEX_REASONING = "complex_reasoning"
CONTENT_COMPRESSION = "content_compression"
AGENT_LOOP = "agent_loop"


# Built-in normaliser: task id -> class id. Operators can extend this via
# ``llm.task_class_map`` in workspace config.
_BUILTIN_TASK_CLASS_MAP: dict[str, str] = {
    # classification-family
    "classify": CLASSIFICATION,
    "news_filtering": CLASSIFICATION,
    "trigger_triage": CLASSIFICATION,
    "intent_classification": CLASSIFICATION,
    # structured extraction
    "extract_json": STRUCTURED_EXTRACTION,
    "extract_candle_data": STRUCTURED_EXTRACTION,
    "schema_extract": STRUCTURED_EXTRACTION,
    "json_mode": STRUCTURED_EXTRACTION,
    "nl_schedule_parse": STRUCTURED_EXTRACTION,
    # content compression
    "compress": CONTENT_COMPRESSION,
    "summarise": CONTENT_COMPRESSION,
    "summarize": CONTENT_COMPRESSION,
    "context_compress": CONTENT_COMPRESSION,
    "summary": CONTENT_COMPRESSION,
    "brief_summary": CONTENT_COMPRESSION,
    # subagent reasoning
    "subagent_analysis": SUBAGENT_REASONING,
    "market_analyst_summary": SUBAGENT_REASONING,
    "risk_critic_review": SUBAGENT_REASONING,
    "news_interpreter": SUBAGENT_REASONING,
    # main agent loop
    "agent.loop": AGENT_LOOP,
    "normal_agent_loop": AGENT_LOOP,
    "agent_step": AGENT_LOOP,
    "agent_decision": AGENT_LOOP,
    # strategy review
    "strategy_review": STRATEGY_REVIEW,
    "trade_explanation": STRATEGY_REVIEW,
    "reflection": STRATEGY_REVIEW,
    # proposal generation
    "script_generation": PROPOSAL_GENERATION,
    "skill_generation": PROPOSAL_GENERATION,
    "strategy_evolution": PROPOSAL_GENERATION,
    "self_improvement": PROPOSAL_GENERATION,
    "prompt_patch": PROPOSAL_GENERATION,
    # complex reasoning
    "complex_signal_analysis": COMPLEX_REASONING,
    "large_loss_postmortem": COMPLEX_REASONING,
    "deep_research": COMPLEX_REASONING,
    "analysis": COMPLEX_REASONING,
    "research": COMPLEX_REASONING,
    "market_analysis": COMPLEX_REASONING,
    "market_research": COMPLEX_REASONING,
    "investment_analysis": COMPLEX_REASONING,
    "investment_guide": COMPLEX_REASONING,
    "stock_investment_guide": COMPLEX_REASONING,
    "a_share_investment_guide": COMPLEX_REASONING,
    "china_a_share_investment_guide": COMPLEX_REASONING,
}


ALL_CLASSES: tuple[str, ...] = (
    CLASSIFICATION,
    STRUCTURED_EXTRACTION,
    SUBAGENT_REASONING,
    STRATEGY_REVIEW,
    PROPOSAL_GENERATION,
    COMPLEX_REASONING,
    CONTENT_COMPRESSION,
    AGENT_LOOP,
)


def normalise_task_class(task: str,
                         *, extra_map: dict[str, str] | None = None
                         ) -> str | None:
    """Return the canonical task class for ``task`` or ``None`` if unknown.

    ``extra_map`` merges into the built-in map so operators can extend
    the normaliser through workspace config without editing Python.
    """
    if not isinstance(task, str) or not task:
        return None
    low = task.strip().lower()
    if extra_map and low in extra_map:
        return extra_map[low]
    return _BUILTIN_TASK_CLASS_MAP.get(low)


def tier_advertises_class(tier_cfg: dict,
                          classes: Iterable[str]) -> bool:
    """Return True if ``tier_cfg`` lists any of ``classes`` in
    ``allowed_classes``."""
    advertised = set(tier_cfg.get("allowed_classes") or [])
    return bool(advertised.intersection(classes))
