from __future__ import annotations

import pytest

from nerya.agent.verifier import compute_verifier_outcome


pytestmark = pytest.mark.smoke


def test_model_only_turn_is_lazy_not_verified() -> None:
    outcome = compute_verifier_outcome(
        blocks=[
            {
                "block": {
                    "kind": "text",
                    "text": "我已经处理好了。",
                },
            }
        ],
    )

    assert outcome.transition_label == "model_done"
    assert outcome.hard_passed is False
    assert outcome.hard_status == "missing"
    assert outcome.has_hard_evidence is False
    assert outcome.has_validation_attempt is False
    assert outcome.trusted is False


def test_successful_validation_turn_is_verified_and_trusted() -> None:
    outcome = compute_verifier_outcome(
        blocks=[
            {
                "block": {
                    "kind": "tool_use",
                    "call_id": "call_test",
                    "action": "run_shell",
                    "payload": {"command": "python -m pytest tests/test_example.py -q"},
                },
            },
            {
                "block": {
                    "kind": "tool_result",
                    "call_id": "call_test",
                    "action": "run_shell",
                    "ok": True,
                    "result": "1 passed",
                },
            },
        ],
    )

    assert outcome.transition_label == "verified"
    assert outcome.hard_passed is True
    assert outcome.hard_status == "passed"
    assert outcome.has_hard_evidence is True
    assert outcome.has_validation_attempt is True
    assert outcome.trusted is True

