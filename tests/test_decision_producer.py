import time
import hashlib
import json
import sqlite3
import pytest

from nerya.llm.messages import MessagesResponse
from nerya.skills.builtin.backtest.scripts.decision_producer import (
    DecisionProducer,
    DecisionProducerError,
)

SCHEMA = {
    "type": "object",
    "properties": {"decision": {"enum": ["ENTRY", "HOLD"]}},
    "required": ["decision"],
    "additionalProperties": False,
}


def make(tmp_path, backend, **limits):
    return DecisionProducer(
        tmp_path / "ledger.db",
        authorization_id="authorized-test",
        backend=backend,
        provider="fixture",
        model="fixture-model",
        max_calls=limits.get("max_calls", 2),
        max_tokens=limits.get("max_tokens", 10000),
        deadline=limits.get("deadline", time.time() + 60),
        cancel_token=limits.get("cancel_token"),
    )


def request(producer, stamp=1):
    return producer.produce(
        binding={"timestamp": stamp},
        role_prompt="Confirm entry.",
        payload={"timestamp": stamp},
        output_schema=SCHEMA,
    )


def response(text='{"decision":"HOLD"}'):
    return MessagesResponse(
        content=[{"type": "text", "text": text}],
        provider="fixture",
        model="fixture-model",
        usage={"input_tokens": 12, "output_tokens": 5},
    )


def tool_response(output=None):
    value = response()
    value.content = [{
        "type": "tool_use",
        "id": "fixture-call",
        "name": "record_replay_decision",
        "input": {"decision": "HOLD"} if output is None else output,
    }]
    value.stop_reason = "tool_use"
    return value


def test_forced_data_only_tool_records_decision_once_without_execution(tmp_path):
    from nerya.llm.attempt_budget import current_attempt_budget

    calls = []

    def backend(r):
        calls.append(r)
        assert current_attempt_budget().limit == 0
        assert len(r.tools) == 1
        assert r.tools[0]["name"] == "record_replay_decision"
        assert r.tools[0]["input_schema"] == SCHEMA
        assert r.tool_choice == {"type": "tool", "name": "record_replay_decision"}
        assert r.max_tokens == 1024 and r.temperature == 0 and r.stream is False
        value = tool_response()
        # The decision is the tool input, never parsed from accompanying prose.
        value.content.insert(0, {"type": "text", "text": "private unstructured explanation"})
        return value

    producer = make(tmp_path, backend, max_calls=1)
    result = request(producer)
    assert result["output"] == {"decision": "HOLD"}
    assert set(result) == {
        "request_digest", "output", "provider", "model", "generated_at",
        "response_sha256", "reserved_tokens", "usage", "evidence_level",
    }
    assert request(producer) == result and len(calls) == 1
    assert charge(producer) == (1, 17)
    assert b"private unstructured explanation" not in producer.path.read_bytes()


@pytest.mark.parametrize("kind,code", [
    ("multiple", "decision_response_tool_calls_forbidden"),
    ("arbitrary", "decision_response_tool_calls_forbidden"),
    ("wrong_stop", "decision_response_completion_invalid"),
    ("no_tool", "decision_response_completion_invalid"),
    ("truncated", "decision_response_truncated"),
    ("string_input", "decision_response_not_object"),
    ("missing_input", "decision_response_not_object"),
    ("array_input", "decision_response_not_object"),
    ("wrong_enum", "decision_response_schema_invalid"),
    ("extra_field", "decision_response_schema_invalid"),
    ("missing_field", "decision_response_schema_invalid"),
    ("nonfinite", "decision_response_json_invalid"),
    ("oversize", "decision_response_too_large"),
])
def test_invalid_tool_response_is_charged_and_never_retried_or_parsed_as_text(tmp_path, kind, code):
    calls = []
    secret = "private-tool-argument"

    def backend(r):
        calls.append(r)
        value = tool_response()
        # Even valid fallback text must not rescue an invalid tool response.
        value.content.insert(0, {"type": "text", "text": '{"decision":"HOLD"}'})
        tool = value.content[-1]
        if kind == "multiple":
            value.content.append(dict(tool))
        elif kind == "arbitrary":
            tool["name"] = "execute_trade"
        elif kind == "wrong_stop":
            value.stop_reason = "end_turn"
        elif kind == "no_tool":
            value.content.pop()
        elif kind == "truncated":
            value.stop_reason = "max_tokens"
        elif kind == "missing_input":
            del tool["input"]
        else:
            tool["input"] = {
                "string_input": '{"decision":"HOLD"}',
                "array_input": [],
                "wrong_enum": {"decision": "BUY"},
                "extra_field": {"decision": "HOLD", "private": secret},
                "missing_field": {},
                "nonfinite": {"decision": float("nan")},
                "oversize": {"decision": secret * 4000},
            }[kind]
        return value

    producer = make(tmp_path, backend)
    reserve = reserve_for(producer)
    with pytest.raises(DecisionProducerError, match=code):
        request(producer)
    assert charge(producer) == (1, reserve)
    resumed = make(tmp_path, backend, deadline=producer.deadline)
    with pytest.raises(DecisionProducerError, match="failed_response"):
        request(resumed)
    assert len(calls) == 1
    with sqlite3.connect(producer.path) as db:
        assert db.execute("SELECT status,result FROM requests").fetchone() == ("failed_response", None)
        reason, diagnostic = db.execute("SELECT reason_code,diagnostics FROM response_diagnostics").fetchone()
    assert reason == code and len(diagnostic) < 1000
    assert secret.encode() not in producer.path.read_bytes()


def test_tool_diagnostic_hash_covers_arguments_without_persisting_them(tmp_path):
    secret = "private-invalid-tool-argument"
    value = tool_response({"decision": secret})
    producer = make(tmp_path, lambda r: value)
    with pytest.raises(DecisionProducerError, match="schema_invalid"):
        request(producer)
    with sqlite3.connect(producer.path) as db:
        diagnostic = json.loads(db.execute("SELECT diagnostics FROM response_diagnostics").fetchone()[0])
    body = json.dumps({"text": "", "tool_uses": value.content},
                      sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)
    assert diagnostic["response_sha256"] == hashlib.sha256(body.encode()).hexdigest()
    assert diagnostic["stop_reason"] == "tool_use" and diagnostic["tool_call_count"] == 1
    assert diagnostic["usage"] == {"input_tokens": 12, "output_tokens": 5}
    assert secret.encode() not in producer.path.read_bytes()


def test_output_policy_binds_version_and_tool_wire_into_identity_and_reservation(tmp_path):
    producer = make(tmp_path, lambda r: pytest.fail("identity must not call backend"))
    args = dict(binding={"timestamp": 1}, role_prompt="Confirm entry.",
                payload={"timestamp": 1}, output_schema=SCHEMA)
    prepared = producer.request_identity(**args)
    policy = producer.output_policy(SCHEMA)
    assert producer.policy_version == policy["policy_version"] == 2
    assert policy["plain_json_fallback"] == "strict_object_end_turn_no_tools"
    wire = json.dumps({key: policy[key] for key in ("tools", "tool_choice")},
                      sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)
    assert prepared["reserve"] >= (len(prepared["system"].encode()) +
                                    len(prepared["text"].encode()) + len(wire.encode()) + 1024)
    producer.policy_version = 3
    assert producer.request_identity(**args)["digest"] != prepared["digest"]
    producer.policy_version = 2
    changed = {**SCHEMA, "description": "Historical confirmation"}
    assert producer.request_identity(**{**args, "output_schema": changed})["digest"] != prepared["digest"]
    assert producer.output_policy(changed) != policy
    original = producer.output_policy
    def changed_choice(schema):
        return {**original(schema), "tool_choice": {"type": "any"}}
    producer.output_policy = changed_choice
    assert producer.request_identity(**args)["digest"] != prepared["digest"]
    producer.output_policy = original
    assert producer.request_identity(
        **{**args, "output_schema": {**SCHEMA, "description": "x" * 32000}}
    )["reserve"] > 32000
    assert charge(producer) == (0, 0)


@pytest.mark.parametrize("max_tokens", [32000, 100000])
def test_large_request_uses_granted_total_reservation_without_per_call_ceiling(tmp_path, max_tokens):
    calls = []
    args = dict(binding={"timestamp": 1}, role_prompt="Confirm entry.",
                payload={"history": "x" * 33000}, output_schema=SCHEMA)

    def backend(r):
        calls.append(r)
        assert charge(producer) == (1, reserve)
        return tool_response()

    producer = make(tmp_path, backend, max_tokens=max_tokens)
    reserve = producer.request_identity(**args)["reserve"]
    assert 32000 < reserve < 100000
    if reserve <= max_tokens:
        result = producer.produce(**args)
        assert producer.produce(**args) == result
        assert len(calls) == 1 and charge(producer) == (1, 17)
    else:
        with pytest.raises(DecisionProducerError, match="budget_exhausted") as caught:
            producer.produce(**args)
        assert caught.value.receipt["exhausted_limits"] == ["tokens"]
        assert caught.value.receipt["required_reservation_tokens"] == reserve
        assert calls == [] and charge(producer) == (0, 0)


@pytest.mark.parametrize("outcome", ["success", "operator_cancel", "authorization_deadline", "transport_timeout"])
def test_request_inherits_authorization_deadline_and_preserves_unknown_effects(tmp_path, monkeypatch, outcome):
    from nerya.harness.cancellation import CancelToken, CancelledError, raise_if_cancelled

    clock = [time.time()]
    monkeypatch.setattr("nerya.skills.builtin.backtest.scripts.decision_producer.time.time", lambda: clock[0])
    token = CancelToken()
    calls = []
    failure = TimeoutError("transport timeout") if outcome == "transport_timeout" else CancelledError(
        "operator_interrupt" if outcome == "operator_cancel" else "deadline_exceeded"
    )

    def backend(r):
        calls.append(r)
        assert r.deadline == producer.deadline and r.cancel_token is token
        clock[0] += 91
        raise_if_cancelled(r.cancel_token, r.deadline)
        if outcome == "success":
            return tool_response()
        if outcome == "authorization_deadline":
            clock[0] = r.deadline
        elif outcome == "operator_cancel":
            token.cancel("operator_interrupt")
        raise failure

    producer = make(tmp_path, backend, deadline=clock[0] + 1800, cancel_token=token)
    reserve = reserve_for(producer)
    if outcome == "success":
        result = request(producer)
        assert request(producer) == result and charge(producer) == (1, 17)
    else:
        with pytest.raises(type(failure)) as caught:
            request(producer)
        assert caught.value is failure
        assert charge(producer) == (1, reserve)
        with sqlite3.connect(producer.path) as db:
            assert db.execute("SELECT status,result FROM requests").fetchone() == ("unconfirmed", None)
            assert db.execute("SELECT count(*) FROM response_diagnostics").fetchone() == (0,)
        resumed = make(tmp_path, backend, deadline=producer.deadline)
        with pytest.raises(DecisionProducerError, match="unconfirmed"):
            request(resumed)
    assert len(calls) == 1


def test_cancellation_before_request_does_not_reserve(tmp_path):
    from nerya.harness.cancellation import CancelToken, CancelledError

    token = CancelToken()
    token.cancel("operator_interrupt")
    producer = make(tmp_path, lambda r: pytest.fail("cancelled request sent"), cancel_token=token)
    with pytest.raises(CancelledError, match="operator_interrupt"):
        request(producer)
    assert charge(producer) == (0, 0)


@pytest.mark.parametrize("structured", [True, False])
def test_real_adapter_forces_tool_and_accepts_strict_plain_json_compatibility(tmp_path, structured):
    from nerya.llm.messages import OpenAIMessagesBackend
    calls = []

    class Transport:
        def post_json(self, url, **kwargs):
            calls.append(kwargs)
            body = kwargs["body"]
            assert body["tool_choice"] == "required"
            assert len(body["tools"]) == 1
            assert body["tools"][0]["function"]["name"] == "record_replay_decision"
            assert body["tools"][0]["function"]["parameters"] == SCHEMA
            message = {"role": "assistant", "content": '{"decision":"HOLD"}'}
            if structured:
                message = {"role": "assistant", "content": None, "tool_calls": [{
                    "id": "fixture-call", "type": "function",
                    "function": {"name": "record_replay_decision", "arguments": '{"decision":"HOLD"}'},
                }]}
            return 200, {
                "choices": [{"message": message, "finish_reason": "tool_calls" if structured else "stop"}],
                "usage": {"prompt_tokens": 12, "completion_tokens": 5},
            }

    backend = OpenAIMessagesBackend(api_key="fixture-key", model="fixture-model",
                                    provider_name="fixture", transport=Transport(), max_attempts=1)
    producer = make(tmp_path, backend, max_calls=1)
    result = request(producer)
    assert result["output"] == {"decision": "HOLD"}
    assert request(producer) == result and len(calls) == 1 and charge(producer) == (1, 17)


def test_success_is_cached_and_budget_cannot_be_reset(tmp_path):
    calls = []

    def backend(r):
        calls.append(r)
        return tool_response()

    p = make(tmp_path, backend, max_calls=1)
    first = request(p)
    assert request(p) == first and len(calls) == 1
    assert calls[0].tools[0]["input_schema"] == SCHEMA and calls[0].deadline <= p.deadline
    with pytest.raises(DecisionProducerError, match="budget_exhausted"):
        request(p, 2)
    with pytest.raises(DecisionProducerError, match="policy changed"):
        make(tmp_path, backend, max_calls=2, deadline=p.deadline)
    resumed = make(tmp_path, backend, max_calls=1, deadline=p.deadline)
    assert request(resumed) == first and len(calls) == 1


def test_local_reasoning_rejection_does_not_reserve_or_leave_unconfirmed(tmp_path):
    from types import SimpleNamespace
    from nerya.core.errors import LLMError
    from nerya.llm.messages import OpenAIMessagesBackend

    backend = OpenAIMessagesBackend(api_key="fixture-key", provider_name="zhipu", model="glm-5.3",
        transport=SimpleNamespace(post=lambda *args, **kwargs: pytest.fail("must not send network request")))
    producer = DecisionProducer(tmp_path / "ledger.db", authorization_id="authorized-test",
        backend=backend, provider="zhipu", model="glm-5.3", max_calls=2, max_tokens=10000,
        deadline=time.time() + 60, reasoning_effort="none")
    for _ in range(2):
        with pytest.raises(LLMError, match="reasoning_off_unsupported"):
            request(producer)
    with sqlite3.connect(producer.path) as db:
        assert db.execute("SELECT calls,reserved FROM budget").fetchone() == (0, 0)
        assert db.execute("SELECT count(*) FROM requests").fetchone()[0] == 0


def test_uncertain_failure_is_not_replayed(tmp_path):
    calls = []

    def fail(r):
        calls.append(r)
        raise TimeoutError("uncertain provider receipt")

    p = make(tmp_path, fail)
    with pytest.raises(TimeoutError):
        request(p)
    with pytest.raises(DecisionProducerError, match="unconfirmed"):
        request(p)
    assert len(calls) == 1


@pytest.mark.parametrize(
    "text",
    [
        '{"decision":"BUY"}',
        '{"decision":"HOLD","decision":"ENTRY"}',
        '{"decision":NaN}',
        "[]",
        '```json\n{"decision":"HOLD"}\n```',
        'The decision is {"decision":"HOLD"}',
        '{"decision":"HOLD"} trailing prose',
    ],
)
def test_invalid_model_body_never_becomes_complete_receipt(tmp_path, text):
    p = make(tmp_path, lambda r: response(text))
    with pytest.raises(Exception):
        request(p)
    with pytest.raises(DecisionProducerError, match="failed_response"):
        request(p)


def test_token_cap_blocks_before_call(tmp_path):
    p = make(tmp_path, lambda r: pytest.fail("budget bypass"), max_tokens=1)
    with pytest.raises(DecisionProducerError, match="budget_exhausted"):
        request(p)


@pytest.mark.parametrize("status", [400, 401, 403, 408, 429, 500, 503])
def test_real_adapter_cannot_retry_inside_one_authorized_call(tmp_path, status):
    from nerya.llm.messages import OpenAIMessagesBackend

    calls = []

    class Transport:
        def post_json_with_headers(self, url, **kwargs):
            calls.append(kwargs)
            return status, {"error": {"message": "private-provider-rejection"}}, {
                "x-request-id": "fixture-rejection-id", "retry-after": "0",
                "private-header": "private-provider-rejection",
            }

    backend = OpenAIMessagesBackend(
        api_key="fixture-key",
        model="fixture-model",
        provider_name="fixture",
        transport=Transport(),
        max_attempts=5,
    )
    p = make(tmp_path, backend, max_calls=1)
    reserve = reserve_for(p)
    with pytest.raises(DecisionProducerError, match="^decision_request_failed_response$") as caught:
        request(p)
    receipt = caught.value.receipt
    assert receipt == {
        "max_output_tokens": 1024, "reserved_tokens": reserve,
        "status_code": status, "request_id": "fixture-rejection-id",
        "reason": "decision_request_failed_response",
        "label": "current_model_retrospective_replay", "performance_evidence": False,
        "authorization_id": p.authorization_id,
        "request_digest": p.request_identity(binding={"timestamp": 1}, role_prompt="Confirm entry.",
            payload={"timestamp": 1}, output_schema=SCHEMA)["digest"],
        "status": "failed_response", "failure_stage": "model_response_validation",
        "request_sent": True, "reservation_retained": True,
        "safe_to_retry": False, "recovery_disposition": "no_automatic_retry",
    }
    assert charge(p) == (1, reserve)
    with sqlite3.connect(p.path) as db:
        assert db.execute("SELECT status,result FROM requests").fetchone() == ("failed_response", None)
        reason, diagnostic = db.execute("SELECT reason_code,diagnostics FROM response_diagnostics").fetchone()
    assert reason == receipt["reason"]
    assert json.loads(diagnostic) == {key: receipt[key] for key in (
        "max_output_tokens", "reserved_tokens", "status_code", "request_id",
    )}
    assert "private-provider-rejection".encode() not in p.path.read_bytes()
    from nerya.skills.builtin.backtest.scripts.run_state import RunTracker
    tracker = RunTracker()
    tracker.bind(tmp_path / "run", strategy_id="fixture", proposal_id=None)
    tracker.fail(caught.value)
    failure = json.loads((tmp_path / "run" / "failure.json").read_text())
    assert failure["details"] == receipt and failure["status"] == "failed"
    assert failure["error"]["message"] == receipt["reason"]
    assert "private-provider-rejection" not in json.dumps(failure)
    assert len(calls) == 1
    resumed = make(tmp_path, backend, max_calls=1, deadline=p.deadline)
    for producer in (p, resumed):
        with pytest.raises(DecisionProducerError, match="failed_response"):
            request(producer)
    assert len(calls) == 1 and charge(resumed) == (1, reserve)


def test_provider_overrun_exhausts_budget_and_does_not_publish_receipt(tmp_path):
    def backend(r):
        value = tool_response()
        value.usage = {"input_tokens": 100001, "output_tokens": 1}
        return value

    p = make(tmp_path, backend)
    with pytest.raises(DecisionProducerError, match="exceeded_budget"):
        request(p)
    with pytest.raises(DecisionProducerError, match="budget_exhausted"):
        request(p, 2)
    with pytest.raises(DecisionProducerError, match="failed_response"):
        request(p)


def test_truncated_response_persists_bounded_diagnostics_before_failure_and_never_retries(tmp_path):
    secret = 'private-provider-body-never-persist'
    calls = []
    def backend(r):
        calls.append(r)
        value = response(secret)
        value.stop_reason = 'max_tokens'
        return value
    producer = make(tmp_path, backend)
    args = dict(binding={"timestamp": 1}, role_prompt="Confirm entry.",
                payload={"timestamp": 1}, output_schema=SCHEMA, max_output_tokens=2048)
    prepared = producer.request_identity(**args)
    with pytest.raises(DecisionProducerError, match='decision_response_truncated') as caught:
        producer.produce(**args)
    with sqlite3.connect(producer.path) as db:
        status, result = db.execute('SELECT status,result FROM requests').fetchone()
        code, raw = db.execute('SELECT reason_code,diagnostics FROM response_diagnostics').fetchone()
    diagnostic = json.loads(raw)
    assert status == 'failed_response' and result is None
    assert code == 'decision_response_truncated'
    assert diagnostic['stop_reason'] == 'max_tokens'
    assert diagnostic['provider_match'] is diagnostic['model_match'] is True
    assert diagnostic['tool_call_count'] == 0
    assert diagnostic['usage'] == {'input_tokens': 12, 'output_tokens': 5}
    assert diagnostic['response_sha256'] == hashlib.sha256(secret.encode()).hexdigest()
    assert len(raw) < 1000 and secret not in raw
    receipt = caught.value.receipt
    assert receipt == {
        **diagnostic,
        'reason': code, 'label': 'current_model_retrospective_replay', 'performance_evidence': False,
        'authorization_id': producer.authorization_id, 'request_digest': prepared['digest'],
        'status': 'failed_response', 'failure_stage': 'model_response_validation',
        'request_sent': True, 'reservation_retained': True,
        'safe_to_retry': False, 'recovery_disposition': 'no_automatic_retry',
    }
    assert receipt['max_output_tokens'] == calls[0].max_tokens == 2048
    assert receipt['reserved_tokens'] == prepared['reserve'] == charge(producer)[1]
    assert len(json.dumps(receipt)) < 1500 and secret not in json.dumps(receipt)
    from nerya.skills.builtin.backtest.scripts.run_state import RunTracker
    tracker = RunTracker()
    tracker.bind(tmp_path / 'run', strategy_id='fixture', proposal_id=None)
    tracker.fail(caught.value)
    failure = json.loads((tmp_path / 'run' / 'failure.json').read_text())
    assert failure['details'] == receipt and failure['status'] == 'failed'
    assert failure['error']['message'] == code and secret not in json.dumps(failure)
    resumed = make(tmp_path, backend, deadline=producer.deadline)
    with pytest.raises(DecisionProducerError, match='failed_response'):
        resumed.produce(**args)
    assert len(calls) == 1
    assert secret.encode() not in producer.path.read_bytes()


@pytest.mark.parametrize("kind", [
    "timeout", "timeout_with_status", "untyped_http_text", "missing_status", "none_status",
    "zero_status", "success_status", "redirect_status", "invalid_status", "string_status",
    "float_status", "bool_status",
])
def test_network_failure_has_no_response_diagnostic_and_remains_unconfirmed(tmp_path, kind):
    from nerya.core.errors import LLMError

    failure = TimeoutError("private network error") if kind.startswith("timeout") else LLMError(
        "private network error HTTP 429 request_id=ambiguous"
    )
    statuses = {
        "timeout_with_status": 429, "none_status": None, "zero_status": 0,
        "success_status": 200, "redirect_status": 302, "invalid_status": 600,
        "string_status": "429", "float_status": 429.0, "bool_status": True,
    }
    if kind in statuses:
        failure.status_code = statuses[kind]
    calls = []
    def backend(r):
        calls.append(r)
        raise failure
    producer = make(tmp_path, backend)
    reserve = reserve_for(producer)
    with pytest.raises(type(failure)) as caught:
        request(producer)
    assert caught.value is failure
    assert not hasattr(failure, "receipt")
    with sqlite3.connect(producer.path) as db:
        assert db.execute('SELECT status,result FROM requests').fetchone() == ('unconfirmed', None)
        assert db.execute('SELECT count(*) FROM response_diagnostics').fetchone() == (0,)
    resumed = make(tmp_path, backend, deadline=producer.deadline)
    with pytest.raises(DecisionProducerError, match="unconfirmed"):
        request(resumed)
    assert len(calls) == 1 and charge(resumed) == (1, reserve)


def test_overrun_with_bad_json_still_exhausts_budget(tmp_path):
    def backend(r):
        value = response('not json')
        value.usage = {'input_tokens': 100001, 'output_tokens': 1}
        return value
    producer = make(tmp_path, backend)
    with pytest.raises(DecisionProducerError, match='decision_provider_exceeded_budget'):
        request(producer)
    with pytest.raises(DecisionProducerError, match='budget_exhausted'):
        request(producer, 2)


def reserve_for(producer, stamp=1):
    return producer.request_identity(binding={'timestamp': stamp}, role_prompt='Confirm entry.',
        payload={'timestamp': stamp}, output_schema=SCHEMA)['reserve']


def charge(producer):
    with sqlite3.connect(producer.path) as db:
        return db.execute('SELECT calls,reserved FROM budget').fetchone()


def test_success_releases_unused_reservation_allowing_second_call_without_policy_reset(tmp_path):
    calls = []
    probe = make(tmp_path / 'probe', lambda r: tool_response())
    reserve = reserve_for(probe)
    def backend(r):
        calls.append(r)
        return tool_response()
    producer = make(tmp_path / 'actual', backend, max_tokens=reserve + 17)
    assert reserve * 2 > producer.max_tokens
    request(producer)
    assert charge(producer) == (1, 17)
    resumed = make(tmp_path / 'actual', backend, max_tokens=reserve + 17, deadline=producer.deadline)
    request(resumed, 2)
    assert len(calls) == 2 and charge(resumed) == (2, 34)
    request(resumed, 2)
    assert len(calls) == 2 and charge(resumed) == (2, 34)


@pytest.mark.parametrize('kind', ['unknown', 'invalid', 'zero_usage', 'missing_usage'])
def test_uncertain_or_invalid_usage_never_releases_unused_reservation(tmp_path, kind):
    def backend(r):
        if kind == 'unknown':
            raise TimeoutError('uncertain')
        value = response('not json') if kind == 'invalid' else tool_response()
        if kind == 'zero_usage':
            value.usage = {'input_tokens': 0, 'output_tokens': 0}
        elif kind == 'missing_usage':
            value.usage = {}
        return value
    producer = make(tmp_path, backend)
    reserve = reserve_for(producer)
    if kind in {'unknown', 'invalid'}:
        with pytest.raises((TimeoutError, DecisionProducerError)):
            request(producer)
    else:
        request(producer)
    assert charge(producer) == (1, reserve)


def test_settlement_preserves_concurrent_reservation_and_prevents_oversubscription(tmp_path):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event
    started, release = Event(), Event()
    calls = []
    def backend(r):
        stamp = json.loads(r.messages[0]['content'])['timestamp']
        calls.append(stamp)
        if stamp == 1:
            started.set()
            assert release.wait(5)
        return tool_response()
    probe = make(tmp_path / 'probe', backend)
    reserve = reserve_for(probe)
    producer = make(tmp_path / 'actual', backend, max_calls=3, max_tokens=reserve * 2)
    with ThreadPoolExecutor(max_workers=1) as pool:
        first = pool.submit(request, producer, 1)
        try:
            assert started.wait(5)
            request(producer, 2)
            assert charge(producer) == (2, reserve + 17)
            with pytest.raises(DecisionProducerError, match='budget_exhausted'):
                request(producer, 3)
        finally:
            release.set()
        first.result(timeout=5)
    assert charge(producer) == (2, 34)
    request(producer, 3)
    assert calls == [1, 2, 3] and charge(producer) == (3, 51)


@pytest.mark.parametrize('kind,code', [
    ('provider', 'decision_response_identity_mismatch'),
    ('model', 'decision_response_identity_mismatch'),
    ('tool', 'decision_response_tool_calls_forbidden'),
    ('stop', 'decision_response_completion_invalid'),
    ('length', 'decision_response_truncated'),
    ('usage', 'decision_response_usage_invalid'),
    ('json', 'decision_response_json_invalid'),
    ('schema', 'decision_response_schema_invalid'),
])
def test_failure_reason_codes_are_specific_and_diagnostics_contain_no_provider_body(tmp_path, kind, code):
    secret = 'provider-private-blob'
    def backend(r):
        value = response()
        value.raw = {'private': secret}
        if kind in {'provider', 'model'}:
            setattr(value, kind, secret)
        elif kind == 'tool':
            value.content.append({'type': 'tool_use', 'id': secret, 'input': {'private': secret}})
        elif kind in {'stop', 'length'}:
            value.stop_reason = secret if kind == 'stop' else 'length'
        elif kind == 'usage':
            value.usage = {'input_tokens': secret}
        elif kind == 'json':
            value.content = [{'type': 'text', 'text': secret}]
        else:
            value.content = [{'type': 'text', 'text': '{"decision":"BUY"}'}]
        return value
    producer = make(tmp_path, backend)
    with pytest.raises(DecisionProducerError, match=code) as caught:
        request(producer)
    with sqlite3.connect(producer.path) as db:
        reason, diagnostic = db.execute('SELECT reason_code,diagnostics FROM response_diagnostics').fetchone()
        assert db.execute('SELECT status FROM requests').fetchone() == ('failed_response',)
    assert reason == code and len(diagnostic) < 1000 and secret not in diagnostic
    assert caught.value.receipt['reason'] == code
    assert secret not in json.dumps(caught.value.receipt)
    assert secret.encode() not in producer.path.read_bytes()
