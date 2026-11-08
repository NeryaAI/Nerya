"""Budget rejection is producer evidence, never permission for another call."""

import json
import sqlite3
import time

import pytest

from nerya.skills.builtin.backtest.scripts.decision_producer import (
    DecisionProducer, DecisionProducerError,
)
from nerya.skills.builtin.backtest.scripts.retrospective_replay import VERDICT_SCHEMA
from nerya.skills.builtin.backtest.scripts.run_state import RunTracker
from nerya.tools.native.strategy_runtime import _backtest_error
from nerya.tools.types import ToolCall

pytestmark = pytest.mark.smoke


@pytest.mark.parametrize(
    "cause,calls,reserved,max_calls,max_tokens,expired",
    [("calls", 1, 20, 1, 100000, False),
     ("tokens", 12, 83686, 20, 100000, False),
     ("deadline", 0, 0, 20, 100000, True)],
)
def test_budget_rejection_persists_exact_limits_without_reset_or_wire_call(
    tmp_path, monkeypatch, cause, calls, reserved, max_calls, max_tokens, expired
):
    def forbidden(*args, **kwargs):
        pytest.fail("budget rejection must not call a model or the network")

    monkeypatch.setattr("socket.socket.connect", forbidden)
    producer = DecisionProducer(
        tmp_path / "ledger.db", authorization_id="fixture-authorization",
        backend=forbidden, provider="fixture", model="fixture-model",
        max_calls=max_calls, max_tokens=max_tokens,
        deadline=time.time() + (-1 if expired else 60),
    )
    # Represent durable history; the producer must preserve it byte for byte.
    with sqlite3.connect(producer.path) as db:
        db.execute("UPDATE budget SET calls=?,reserved=?", (calls, reserved))
    before = producer.path.read_bytes()
    inputs = dict(binding={"timestamp": 1}, role_prompt="Confirm entry.",
                  payload={"prefix": "x" * 17000}, output_schema=VERDICT_SCHEMA,
                  max_output_tokens=4096)
    required = producer.request_identity(**inputs)["reserve"]
    tracker = RunTracker()
    tracker.bind(tmp_path / "run", strategy_id="fixture", proposal_id="prp_fixture")
    tracker.phase("replaying")
    with pytest.raises(DecisionProducerError, match="^decision_budget_exhausted$") as caught:
        producer.produce(**inputs)
    diagnostic = caught.value.receipt
    assert diagnostic["reason"] == "decision_budget_exhausted"
    assert diagnostic["exhausted_limits"] == [cause]
    assert diagnostic["authorization_id"] == "fixture-authorization"
    assert diagnostic["calls"] == calls and diagnostic["max_calls"] == max_calls
    assert diagnostic["reserved_tokens"] == reserved
    assert diagnostic["max_tokens"] == max_tokens
    assert diagnostic["remaining_calls"] == max_calls - calls
    assert diagnostic["remaining_tokens"] == max_tokens - reserved
    assert diagnostic["required_reservation_tokens"] == required
    assert diagnostic["deadline"] == producer.deadline
    assert diagnostic["performance_evidence"] is False
    assert diagnostic["label"] == "current_model_retrospective_replay"
    assert diagnostic["request_sent"] is False
    assert inputs["payload"]["prefix"] not in json.dumps(diagnostic)
    tracker.fail(caught.value)
    failure = json.loads((tmp_path / "run/failure.json").read_text())
    assert failure["details"] == diagnostic and failure["status"] == "failed"
    caught.value.receipt.update({
        "provider": "uniswap", "action": "pool_history",
        "failure_stage": "http_status", "rpc_provider": "drpc",
        "rpc_method": "eth_call", "http_status": 124,
        "authorization_id": "PRIVATE_SHOULD_NOT_PROJECT",
    })
    result = _backtest_error(ToolCall(name="strategy_backtest"), caught.value,
                             reason="backtest_invalid_input",
                             strategy_id="fixture", proposal_id="prp_fixture",
                             workspace_root=tmp_path)
    body = result.content[0].data
    assert body["diagnostics"] == diagnostic
    assert result.error.recovery_hint["artifacts"] == {
        "run_receipt": "run/run.json", "failure_path": "run/failure.json"}
    assert result.error.recovery_hint["diagnostics"] == {
        "provider": "uniswap", "action": "pool_history",
        "failure_stage": "http_status", "rpc_provider": "drpc",
        "rpc_method": "eth_call", "http_status": 124,
    }
    assert "authorization_id" not in result.error.recovery_hint["diagnostics"]
    assert body["backtest_status"] == "blocked" and body["ok"] is False
    assert body["next_required_action"]["automatic"] is False
    assert result.error.retryable is False
    assert producer.path.read_bytes() == before
    with sqlite3.connect(producer.path) as db:
        assert db.execute("SELECT count(*) FROM requests").fetchone()[0] == 0


@pytest.mark.parametrize("cause", [
    "model_request", "user_cancel", "user_deadline_reason",
    "token_deadline", "authorization_deadline", "late_global_deadline",
    "early_deadline_error",
])
def test_reserved_request_deadline_preserves_ledger_and_native_failure_receipts(
    tmp_path, monkeypatch, cause
):
    from nerya.core.config import Config
    from nerya.core.paths import WorkspacePaths
    from nerya.harness.cancellation import CancelToken, CancelledError
    from nerya.llm.adapters._base import io_control, _managed_urlopen
    from nerya.llm.attempt_budget import current_attempt_budget
    from nerya.skills.builtin.backtest.scripts import backtest_run
    from nerya.tools.native.strategy_runtime import strategy_backtest_handler

    def forbidden(*args, **kwargs):
        pytest.fail("no model, network or automatic retry is allowed")

    monkeypatch.setattr("socket.socket.connect", forbidden)
    clock = [1000.0]
    monkeypatch.setattr(time, "time", lambda: clock[0])
    token = CancelToken()
    calls, original = [], []
    deadline = 1060.0 if cause == "authorization_deadline" else 1197.0

    def backend(request):
        calls.append(request)
        assert len(calls) == 1 and current_attempt_budget().limit == 0
        assert request.max_tokens == 16384
        assert request.deadline == min(deadline, 1090.0)
        with sqlite3.connect(producer.path) as db:
            assert db.execute("SELECT calls,reserved FROM budget").fetchone() == (3, 50286)
            assert db.execute("SELECT status,result FROM requests WHERE digest=?",
                              (prepared["digest"],)).fetchone() == ("unconfirmed", None)
        try:
            with io_control(request.cancel_token, request.deadline):
                clock[0] = request.deadline
                if cause in {"user_cancel", "user_deadline_reason"}:
                    token.cancel("operator_interrupt" if cause == "user_cancel" else "deadline_exceeded")
                elif cause == "token_deadline":
                    token.deadline_s = clock[0]
                elif cause == "late_global_deadline":
                    clock[0] = deadline
                elif cause == "early_deadline_error":
                    clock[0] -= 1
                    raise CancelledError("deadline_exceeded")
                # Exercise the transport's real deadline check before any IO.
                with _managed_urlopen(None, 90):
                    forbidden()
        except CancelledError as exc:
            original.append(exc)
            raise

    producer = DecisionProducer(
        tmp_path / "ledger.db", authorization_id="PRIVATE_AUTHORIZATION",
        backend=backend, provider="fixture", model="fixture-model",
        max_calls=20, max_tokens=100000, deadline=deadline, cancel_token=token,
    )
    inputs = dict(binding={"candidate_id": "fixture", "timestamp": 1},
                  role_prompt="PRIVATE_PROMPT", payload={"input": "PRIVATE_PAYLOAD"},
                  output_schema=VERDICT_SCHEMA, max_output_tokens=16384)
    prepared = producer.request_identity(**inputs)
    with sqlite3.connect(producer.path) as db:
        db.execute("UPDATE budget SET calls=2,reserved=?", (50286 - prepared["reserve"],))
        db.executemany("INSERT INTO requests VALUES (?,?,?,?)", [
            (producer.authorization_id, "prior-complete", "complete", "{}"),
            (producer.authorization_id, "prior-failed", "failed_response", None),
        ])

    def replay(*, tracker, proposal_id, **kwargs):
        tracker.bind(tmp_path / "run", strategy_id="fixture", proposal_id=proposal_id)
        tracker.phase("replaying")
        return producer.produce(**inputs)

    monkeypatch.setattr(backtest_run, "_run_strategy_backtest", replay)
    call = ToolCall(name="strategy_backtest",
                    arguments={"proposal_id": "prp_fixture", "engine": "native"},
                    metadata={"cancel_token": token})
    config = Config(paths=WorkspacePaths(tmp_path))
    if cause == "model_request":
        result = strategy_backtest_handler(call, config=config)
        body = result.content[0].data
        assert body["reason"] == "decision_request_unconfirmed"
        assert body["proposal_id"] == "prp_fixture" and body["backtest_status"] == "blocked"
        assert result.error.retryable is False
        recovery = result.error.recovery_hint
        assert recovery["action"] == "reconcile_retrospective_request"
        assert recovery["automatic"] is False and recovery["requires_operator_reconciliation"] is True
        assert recovery["artifacts"] == {
            "run_receipt": "run/run.json", "failure_path": "run/failure.json"}
        assert body["diagnostics"]["safe_to_retry"] is False
    else:
        with pytest.raises(CancelledError) as caught:
            strategy_backtest_handler(call, config=config)
        assert caught.value is original[0]

    failure = json.loads((tmp_path / "run/failure.json").read_text())
    run = json.loads((tmp_path / "run/run.json").read_text())
    for receipt in (failure, run):
        assert receipt["status"] == ("failed" if cause == "model_request" else "cancelled")
        assert receipt["phase"] == "replaying"
        assert receipt["strategy_id"] == "fixture" and receipt["proposal_id"] == "prp_fixture"
    if cause == "model_request":
        assert failure["details"] == {
            "reason": "decision_request_unconfirmed",
            "label": "current_model_retrospective_replay", "performance_evidence": False,
            "deadline_source": "model_request", "failure_stage": "model_request_deadline",
            "request_sent": True, "requires_receipt_reconciliation": True,
            "safe_to_retry": False, "request_digest": prepared["digest"],
        }
        public = json.dumps(failure) + result.text() + result.error.message
        assert all(secret not in public for secret in ("PRIVATE_AUTHORIZATION", "PRIVATE_PROMPT", "PRIVATE_PAYLOAD"))
        assert clock[0] == deadline - 107
    with sqlite3.connect(producer.path) as db:
        assert db.execute("SELECT calls,reserved FROM budget").fetchone() == (3, 50286)
        assert db.execute("SELECT digest,status,result FROM requests ORDER BY digest").fetchall() == sorted([
            ("prior-complete", "complete", "{}"), ("prior-failed", "failed_response", None),
            (prepared["digest"], "unconfirmed", None),
        ])
        assert db.execute("SELECT count(*) FROM response_diagnostics").fetchone()[0] == 0
    assert len(calls) == 1
