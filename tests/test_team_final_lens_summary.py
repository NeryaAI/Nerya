from __future__ import annotations

import pytest

from nerya.agent.loop import (
    _build_team_run_final_synthesis_prompt,
    _team_final_output_summary,
)


pytestmark = pytest.mark.smoke


def _lens_output(lens: str, diagnosis: str, decision: str, confidence: float):
    return {
        "lens": lens,
        "diagnosis": diagnosis,
        "facts_used": [
            {"claim": "BTC price 63,894 USDT", "source": "Binance", "as_of": "2026-07-29"},
        ],
        "framework_inferences": ["inference"],
        "decision_implication": decision,
        "invalidation": ["signal"],
        "failure_modes": ["mode"],
        "source_ids": ["B1"],
        "confidence": confidence,
    }


def _committee_payload():
    return {
        "team_run_id": "team-test",
        "status": "completed",
        "ok": True,
        "team_template": "ad_hoc_parallel_team",
        "task": "committee task",
        "roles_requested": ["buffett_lens", "marks_lens"],
        "roles_succeeded": ["buffett_lens", "marks_lens"],
        "roles_failed": [],
        "tokens_total": 100,
        "results": [
            {
                "subagent": "buffett_lens",
                "ok": True,
                "output": _lens_output(
                    "buffett", "outside the circle of competence", "do not participate", 0.92,
                ),
            },
            {
                "subagent": "marks_lens",
                "ok": True,
                "output": _lens_output(
                    "marks", "pendulum swung toward fear but not despair", "small probe position", 0.7,
                ),
            },
        ],
    }


def test_lens_summary_keeps_diagnosis_decision_and_confidence() -> None:
    summary = _team_final_output_summary(
        _lens_output("buffett", "narrative here", "conclusion here", 0.92),
    )
    assert "narrative here" in summary
    assert "conclusion here" in summary
    assert "0.92" in summary
    # The shared fact register must not displace the lens conclusion.
    assert "63,894" not in summary


def test_final_synthesis_prompt_carries_every_lane_distinctly() -> None:
    prompt = _build_team_run_final_synthesis_prompt(
        user_message="committee on BTC",
        team_results=[_committee_payload()],
    )
    assert "outside the circle of competence" in prompt
    assert "do not participate" in prompt
    assert "pendulum swung toward fear" in prompt
    assert "small probe position" in prompt
    assert "0.92" in prompt and "0.7" in prompt
