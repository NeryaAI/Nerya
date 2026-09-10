"""Lifecycle contracts are independently testable without a provider or executor."""
from __future__ import annotations

import subprocess
import sys

import pytest

from nerya.agent.loop_contracts import LoopConfig, LoopOutcome
from nerya.agent.loop_state import LoopRunState
from nerya.agent.runtime import ContinuationUnavailable

pytestmark = pytest.mark.smoke


def _begin(config=None, **kwargs):
    return LoopRunState.begin(
        config=config or LoopConfig(), user_message="task", original_user_text="task",
        now=100.0, **kwargs,
    )


@pytest.mark.parametrize(("original", "new", "expected"), [
    (20.0, 5.0, 105.0), (5.0, 20.0, 105.0),
    (None, 5.0, 105.0), (5.0, None, 105.0), (None, 0.0, 100.0),
])
def test_resume_can_only_tighten_deadline(original, new, expected):
    state = _begin(LoopConfig(max_wall_seconds=original))
    state.attempt_budget.claim("previous_retry")
    checkpoint = state.to_checkpoint(resumable=True)
    resumed = _begin(
        LoopConfig(max_wall_seconds=new, max_extra_llm_attempts_per_turn=1),
        checkpoint=checkpoint, continuation_feedback="verify once",
    )
    assert resumed.deadline_epoch == expected
    assert resumed.turn_id == state.turn_id
    assert resumed.attempt_budget.used == 1
    assert resumed.attempt_budget.limit == 1
    assert resumed.resume_count == 1
    assert len(resumed.transcript) == 2
    assert len(checkpoint.transcript) == 1


@pytest.mark.parametrize("budget", [-1.0, float("nan"), float("inf")])
def test_invalid_deadline_fails_before_provider_dispatch(budget):
    with pytest.raises(ValueError, match="max_wall_seconds"):
        _begin(LoopConfig(max_wall_seconds=budget))


def test_zero_and_unlimited_budgets_are_distinct():
    assert _begin(LoopConfig(max_wall_seconds=0)).deadline_epoch == 100.0
    assert _begin().deadline_epoch is None


def test_begin_does_not_mutate_caller_history_or_inputs():
    prior = [{"role": "assistant", "content": [{"type": "text", "text": "original"}]}]
    user = [{"type": "text", "text": "request"}]
    state = LoopRunState.begin(
        config=LoopConfig(), user_message=user, original_user_text="request",
        now=100.0, prior_messages=prior,
    )
    state.transcript[0]["content"][0]["text"] = "changed"
    state.transcript[1]["content"][0]["text"] = "changed"
    assert prior[0]["content"][0]["text"] == "original"
    assert user[0]["text"] == "request"


def test_events_survive_sink_failure_and_continue_with_monotonic_sequence():
    state = _begin(turn_id="stable-turn")

    def broken_sink(_event):
        raise RuntimeError("client disconnected")

    state.emit("assistant", {"kind": "text", "text": "first"}, sink=broken_sink)
    resumed = _begin(checkpoint=state.to_checkpoint(resumable=True))
    resumed.emit("assistant", {"kind": "text", "text": "second"})
    resumed.stop_reason = "end_turn"
    outcome = resumed.outcome(config=LoopConfig(), aborted=False, now=100.0)
    assert [event.seq for event in outcome.blocks] == [1, 2]
    assert outcome.snapshot(1).metadata["turn_id"] == "stable-turn"
    assert outcome.checkpoint.seq == 2


def test_nonresumable_checkpoint_cannot_reenter():
    checkpoint = _begin().to_checkpoint(resumable=False, resume_block_reason="approval_pending")
    with pytest.raises(ContinuationUnavailable, match="approval_pending"):
        _begin(checkpoint=checkpoint)


def test_public_contract_import_does_not_load_engine_and_aliases_match():
    subprocess.run([
        sys.executable, "-c",
        "import sys; from nerya.agent import LoopConfig, LoopOutcome; "
        "assert 'nerya.agent.loop' not in sys.modules; "
        "assert 'nerya.agent.kernel' not in sys.modules",
    ], check=True)
    from nerya.agent.loop import LoopConfig as OldConfig, LoopOutcome as OldOutcome
    assert OldConfig is LoopConfig
    assert OldOutcome is LoopOutcome


def test_cancel_wait_is_immediate_when_cancelled_and_respects_deadline(monkeypatch):
    from types import SimpleNamespace
    from nerya.harness import cancellation as module

    token = module.CancelToken()
    token.cancel("operator_stop")
    assert token.wait(3600.0) is True
    waits = []
    flag = SimpleNamespace(is_set=lambda: False, wait=lambda timeout: waits.append(timeout))
    token = module.CancelToken(deadline_s=110.0, _flag=flag)
    monkeypatch.setattr(module, "time", SimpleNamespace(time=lambda: 100.0))
    assert token.wait(60.0) is False
    assert waits == [10.0]


def test_blank_explicit_turn_id_uses_configured_id():
    assert _begin(LoopConfig(turn_id="configured-turn"), turn_id=" ").turn_id == "configured-turn"

