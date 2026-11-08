"""Offline recovery at the native error seam; all artifacts stay in tmp_path."""

from copy import deepcopy
import json
import time

import pytest

from nerya.core.errors import TradingError
from nerya.llm.messages import MessagesResponse
from nerya.skills.builtin.backtest.scripts.decision_producer import (
    DecisionProducer,
    DecisionProducerError,
    ResponseFailure,
)
from nerya.skills.builtin.backtest.scripts.decision_tape import (
    DecisionRequired,
    DecisionTape,
    DecisionTapeError,
)
from nerya.skills.builtin.backtest.scripts.retrospective_replay import VERDICT_SCHEMA
from nerya.skills.builtin.backtest.scripts.run_state import RunTracker
from nerya.strategies.tool_verification import attach_strategy_checks
from nerya.tools.native.strategy_runtime import _backtest_error
from nerya.tools.types import ToolCall

pytestmark = pytest.mark.smoke


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    def denied(*args, **kwargs):
        pytest.fail("recovery tests must not access the network")

    monkeypatch.setattr("socket.socket.connect", denied)
    monkeypatch.setattr("socket.create_connection", denied)


def classify(exc):
    return _backtest_error(
        ToolCall(name="strategy_backtest"), exc,
        reason="backtest_invalid_input",
        strategy_id="fixture", proposal_id="prp_fixture",
    )


def test_exhaustion_after_signed_decision_keeps_partial_evidence_incomplete(tmp_path):
    calls = []

    def backend(request):
        calls.append(request)
        return MessagesResponse(
            content=[{"type": "text", "text": '{"decision":"HOLD","confidence":0.6}'}],
            provider="fixture", model="fixture-model",
            usage={"input_tokens": 12, "output_tokens": 8},
        )

    producer = DecisionProducer(
        tmp_path / "ledger.db", authorization_id="fixture-authorization",
        backend=backend, provider="fixture", model="fixture-model",
        max_calls=1, max_tokens=100000, deadline=time.time() + 60,
    )
    binding = dict(
        candidate_id="fixture", source_revision="a" * 64,
        config_hash="b" * 64, data_hash="c" * 64,
        role_prompt_hash="d" * 64, model_settings_hash="e" * 64,
        runtime_build_id="fixture-runtime", sdk_build_id="fixture-sdk",
        provider="fixture", model="fixture-model", model_version="fixture-version",
    )
    key = b"offline-fixture-signing-key-00001"
    tape = DecisionTape(binding, host_key=key, max_receipts=1)
    run_dir = tmp_path / "run"
    tracker = RunTracker()
    tracker.bind(run_dir, strategy_id="fixture", proposal_id="prp_fixture")
    tracker.phase("replaying")

    def decision(timestamp):
        request = tape.bind(timestamp_ms=timestamp, market="FIXTURE:ETHUSDT",
                            timeframe="1h", role="confirmation", payload={"close": 100})
        with pytest.raises(DecisionRequired):
            tape.consume(request)
        return request, dict(binding=request, role_prompt="Confirm entry.",
                             payload={"timestamp": timestamp}, output_schema=VERDICT_SCHEMA)

    first, inputs = decision(1700000000000)
    answer = producer.produce(**inputs)
    tape.append_produced(first, result=answer,
                         expected_request_digest=producer.request_identity(**inputs)["digest"])
    tape_path = run_dir / "decision_tape.json"
    tape_path.write_bytes(tape.dumps())
    tape.consume(first)
    _, inputs = decision(1700003600000)
    with pytest.raises(DecisionProducerError, match="^decision_budget_exhausted$") as caught:
        producer.produce(**inputs)
    tracker.fail(caught.value)
    before = {path: path.read_bytes() for path in tmp_path.rglob("*") if path.is_file()}
    args = {"proposal_id": "prp_fixture", "settings": {
        "agent_replay": True, "start_utc": "2026-09-01", "end_utc": "2026-10-01"}}
    saved_args = deepcopy(args)
    result = _backtest_error(ToolCall(name="strategy_backtest", arguments=args),
                             caught.value, reason="backtest_invalid_input",
                             strategy_id="fixture", proposal_id="prp_fixture")
    body = result.content[0].data
    assert body["reason"] == "decision_budget_exhausted"
    assert body["backtest_status"] == "blocked"
    action = body["next_required_action"]
    assert action["type"] == "review_retrospective_authorization"
    assert action["requires_operator_authorization"] is True
    assert action["automatic"] is False
    assert action["preserve_requested_constraints"] is True
    assert action["preserve_partial_artifacts"] is True
    assert "deadline" in action["message"]
    assert "conditional_replay" not in action and "tool" not in action
    assert result.error.retryable is False
    assert result.error.recovery_hint["next_required_action"] == action
    assert result.is_error and result.semantic_success is False and body["ok"] is False
    assert body["run_receipt"] == str(run_dir / "run.json")
    assert body["failure_path"] == str(run_dir / "failure.json")
    assert "DecisionProducerError" not in result.error.message
    assert "回放预算不可用" in result.error.message
    assert body["phase"] == "replaying"
    assert body["strategy_id"] == "fixture" and body["proposal_id"] == "prp_fixture"
    assert args == saved_args and len(calls) == 1
    assert {path: path.read_bytes() for path in tmp_path.rglob("*") if path.is_file()} == before
    verified = DecisionTape.verify(tape_path.read_bytes(), binding=binding, host_key=key)
    restored = verified.bind(timestamp_ms=1700000000000, market="FIXTURE:ETHUSDT",
                             timeframe="1h", role="confirmation", payload={"close": 100})
    assert restored == first and verified.consume(restored)["output"]["decision"] == "HOLD"
    assert json.loads((run_dir / "run.json").read_text())["status"] == "failed"

    identity = {"artifact_id": "strategy:fixture:proposal:prp_fixture", "revision": "a" * 64}
    checked = attach_strategy_checks(result, before=identity, after=identity, kind="backtest")
    checks = {row["check_id"]: row["status"] for row in checked.metadata["verification"]["checks"]}
    assert checks["historical_run"] == "failed"
    assert checks["report_output"] == checks["historical_performance"] == "missing"
    assert checked.is_error and checked.semantic_success is False


@pytest.mark.parametrize("error,reason", [
    (DecisionTapeError("retrospective_deadline"), "retrospective_deadline"),
    (TradingError("retrospective_replay_requires_operator_authorization"),
     "retrospective_replay_requires_operator_authorization"),
    (TradingError("invalid_or_expired_retrospective_authorization"),
     "invalid_or_expired_retrospective_authorization"),
    (TradingError("retrospective_model_route_mismatch"), "retrospective_model_route_mismatch"),
    (TradingError("retrospective_backend_not_bounded"), "retrospective_backend_not_bounded"),
    (DecisionProducerError("invalid retrospective replay authorization"),
     "retrospective_authorization_invalid"),
    (DecisionProducerError("authorization policy changed"),
     "retrospective_authorization_policy_changed"),
    (DecisionProducerError("decision input exceeds per-call budget"),
     "decision_input_budget_exceeded"),
    (DecisionProducerError("decision_provider_exceeded_budget"), "decision_provider_exceeded_budget"),
    (DecisionProducerError("invalid decision output budget"), "decision_output_budget_invalid"),
    (DecisionProducerError("invalid decision reasoning policy"), "decision_reasoning_policy_invalid"),
])
def test_authorization_and_deadline_failures_require_operator_review(tmp_path, error, reason):
    tracker = RunTracker()
    tracker.bind(tmp_path / "run", strategy_id="fixture", proposal_id="prp_fixture")
    tracker.fail(error)
    before = {path: path.read_bytes() for path in tmp_path.rglob("*") if path.is_file()}
    result = classify(error)
    body = result.content[0].data
    assert body["reason"] == reason and body["backtest_status"] == "blocked"
    action = body["next_required_action"]
    assert action["type"] == "review_retrospective_authorization"
    assert action["requires_operator_authorization"] is True
    assert action["automatic"] is False
    assert action["preserve_partial_artifacts"] and action["preserve_requested_constraints"]
    assert "Do not retry automatically" in action["message"]
    assert "reset budgets" in action["message"] and "create a new authorization" in action["message"]
    assert "conditional_replay" not in action and "tool" not in action
    assert result.error.retryable is False and result.error.recovery_hint["next_required_action"] == action
    assert result.is_error and result.semantic_success is False and body["ok"] is False
    assert body["run_receipt"] == str(tmp_path / "run/run.json")
    assert body["failure_path"] == str(tmp_path / "run/failure.json")
    assert {path: path.read_bytes() for path in tmp_path.rglob("*") if path.is_file()} == before


def test_unconfirmed_request_requires_reconciliation_without_another_model_call(tmp_path):
    calls = []

    def backend(request):
        calls.append(request)
        raise TimeoutError("offline uncertain response")

    producer = DecisionProducer(
        tmp_path / "ledger.db", authorization_id="fixture-authorization",
        backend=backend, provider="fixture", model="fixture-model",
        max_calls=2, max_tokens=100000, deadline=time.time() + 60,
    )
    inputs = dict(binding={"timestamp": 1}, role_prompt="Confirm entry.",
                  payload={"close": 100}, output_schema=VERDICT_SCHEMA)
    with pytest.raises(TimeoutError):
        producer.produce(**inputs)
    with pytest.raises(DecisionProducerError, match="unconfirmed") as caught:
        producer.produce(**inputs)
    ledger = producer.path.read_bytes()
    result = classify(caught.value)
    body = result.content[0].data
    assert body["reason"] == "decision_request_unconfirmed"
    assert body["backtest_status"] == "blocked"
    action = body["next_required_action"]
    assert action["type"] == "reconcile_retrospective_request"
    assert action["requires_operator_reconciliation"] is True
    assert action["automatic"] is False
    assert "requires_operator_authorization" not in action
    assert "Reconcile" in action["message"]
    assert result.error.retryable is False and result.error.recovery_hint["next_required_action"] == action
    assert result.is_error and result.semantic_success is False and body["ok"] is False
    assert len(calls) == 1 and producer.path.read_bytes() == ledger


@pytest.mark.parametrize("message,reason,status,action_type", [
    ("unsafe decision ledger", "decision_ledger_unsafe", "blocked", "reconcile_retrospective_request"),
    ("decision_response_ledger_conflict", "decision_response_ledger_conflict",
     "blocked", "reconcile_retrospective_request"),
    ("decision_request_failed_response; no automatic retry", "decision_request_failed_response",
     "failed", "inspect_retrospective_response"),
    *[(reason.value, reason.value, "failed", "inspect_retrospective_response")
      for reason in ResponseFailure if reason is not ResponseFailure.OVERRUN],
])
def test_response_and_ledger_errors_do_not_become_strategy_repair(message, reason, status, action_type):
    error = DecisionProducerError(message)
    error.receipt = {"stop_reason": "max_tokens", "response_sha256": "a" * 64}
    result = classify(error)
    body = result.content[0].data
    assert body["reason"] == reason and body["backtest_status"] == status
    assert body["diagnostics"] == error.receipt
    action = body["next_required_action"]
    assert action["type"] == action_type
    assert action["automatic"] is False and action["preserve_partial_artifacts"] is True
    assert action["preserve_requested_constraints"] is True
    assert "requires_operator_authorization" not in action
    assert "conditional_replay" not in action and "tool" not in action
    assert "Do not retry automatically" in action["message"]
    assert result.error.retryable is False and result.error.recovery_hint["next_required_action"] == action
    assert result.is_error and result.semantic_success is False and body["ok"] is False


@pytest.mark.parametrize("error", [
    ValueError("decision_budget_exhausted"),
    ValueError("retrospective_deadline"),
    ValueError("retrospective_replay_requires_operator_authorization"),
    TradingError("retrospective_confirmation_role_required"),
    TradingError("retrospective_role_prompt_missing_or_unsafe"),
    DecisionTapeError("source_changed"),
])
def test_strategy_contract_errors_keep_existing_repair_path(error):
    result = classify(error)
    body = result.content[0].data
    assert body["reason"] == "backtest_invalid_input"
    assert body["next_required_action"]["type"] == "inspect_backtest_error"
    assert result.is_error and result.semantic_success is False
