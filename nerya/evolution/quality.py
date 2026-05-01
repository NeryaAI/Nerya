"""Quality gates for memory and evolution asset candidates."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any


_SECRET_PATTERNS = (
    re.compile(r"(?i)(api[_-]?key|secret|token|password)\s*[:=]\s*\S+"),
    re.compile(r"vault://[A-Za-z0-9_.:/-]+"),
)


@dataclass(frozen=True)
class QualityResult:
    ok: bool
    score: float
    reasons: list[str]

    def asdict(self) -> dict[str, Any]:
        return {"ok": self.ok, "score": self.score, "reasons": list(self.reasons)}


def evaluate_learning_candidate(
    text: str,
    *,
    evidence_refs: list[str] | None = None,
    existing_summaries: list[str] | None = None,
) -> QualityResult:
    reasons: list[str] = []
    body = (text or "").strip()
    if len(body) < 20:
        reasons.append("too_short")
    if not evidence_refs:
        reasons.append("missing_evidence_refs")
    if _contains_secret(body):
        reasons.append("possible_secret")
    lowered = body.lower()
    low_value_phrases = (
        "done",
        "completed",
        "finished",
        "all set",
        "works now",
    )
    if lowered in low_value_phrases or len(set(lowered.split())) < 4:
        reasons.append("low_information_content")
    for existing in existing_summaries or []:
        if existing and _rough_overlap(body, existing) > 0.9:
            reasons.append("near_duplicate")
            break
    score = 1.0
    penalty = {
        "too_short": 0.35,
        "missing_evidence_refs": 0.3,
        "possible_secret": 1.0,
        "low_information_content": 0.35,
        "near_duplicate": 0.5,
    }
    for reason in reasons:
        score -= penalty.get(reason, 0.2)
    score = max(0.0, round(score, 4))
    return QualityResult(ok=score >= 0.55 and "possible_secret" not in reasons, score=score, reasons=reasons)


def _contains_secret(text: str) -> bool:
    return any(p.search(text or "") for p in _SECRET_PATTERNS)


def _rough_overlap(a: str, b: str) -> float:
    aw = {x for x in re.split(r"\W+", a.lower()) if x}
    bw = {x for x in re.split(r"\W+", b.lower()) if x}
    if not aw or not bw:
        return 0.0
    return len(aw & bw) / max(1, len(aw | bw))


__all__ = ["QualityResult", "evaluate_learning_candidate"]
