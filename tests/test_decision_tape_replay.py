"""Offline host-tape contract, with real EMA gates and synthetic producer verdicts."""
import hashlib
import hmac
import json
from pathlib import Path
import sys
from types import ModuleType, SimpleNamespace as NS

import pytest

from nerya.skills.builtin.backtest.scripts.decision_tape import (
    DecisionRequired, DecisionTape, DecisionTapeError, GENESIS, LABEL,
    MAX_JSON_BYTES, MAX_TAPE_BYTES, canonical_hash, canonical_json,
)
from nerya.strategies import ResultBuilder
from nerya.strategies.context import StrategyClock, StrategyConfig, StrategyPosition

pytestmark = pytest.mark.smoke
KEY = b'synthetic-host-only-tape-key-00001'
NOW = 1_800_000_000_000
TEMPLATES = Path(__file__).parents[1] / 'nerya/skills/builtin/strategy_author/templates'


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    def denied(*args, **kwargs):
        pytest.fail('decision tape tests must not access network or DB')
    monkeypatch.setattr('socket.socket.connect', denied)
    monkeypatch.setattr('socket.create_connection', denied)
    monkeypatch.setattr('sqlite3.connect', denied)


def binding():
    return dict(candidate_id='fixture-candidate', source_revision=canonical_hash({'main.py': 'fixture'}),
                config_hash=canonical_hash({'timeframe': '1h'}), data_hash=canonical_hash([100, 120, 80]),
                role_prompt_hash=canonical_hash('Return ENTRY or HOLD'),
                model_settings_hash=canonical_hash({'temperature': 0}), runtime_build_id='runtime-fixture',
                sdk_build_id='sdk-fixture', provider='synthetic', model='fixture-model', model_version='v1')


def request(tape, **overrides):
    kwargs = dict(timestamp_ms=1_700_000_000_000, market='MOCK:ETHUSDT',
                  timeframe='1h', role='market_analyst', payload={'close': 100})
    kwargs.update(overrides)
    return tape.bind(**kwargs)


def answer(decision='ENTRY'):
    return {'ok': True, 'output': {'decision': decision, 'confidence': .8}}


def record(tape, req, decision='ENTRY', **overrides):
    kwargs = dict(response=answer(decision), producer_kind='synthetic', produced_at_ms=NOW,
                  usage={'input_tokens': 30, 'output_tokens': 10})
    kwargs.update(overrides)
    return tape.append(req, **kwargs)


def populated(count=2):
    tape = DecisionTape(binding(), host_key=KEY)
    requests = []
    for i in range(count):
        req = request(tape, timestamp_ms=1_700_000_000_000 + i * 3600000)
        record(tape, req)
        assert tape.consume(req) == answer()
        requests.append(req)
    return tape, requests


def test_canonical_hash_is_order_invariant_and_json_is_strict():
    assert canonical_json({'b': '均线', 'a': [1, 2.]}) == b'{"a":[1,2.0],"b":"\xe5\x9d\x87\xe7\xba\xbf"}'
    assert canonical_hash({'a': 1, 'b': 2}) == canonical_hash({'b': 2, 'a': 1})
    assert canonical_hash({'a': 1}) != canonical_hash({'a': 1.})


@pytest.mark.parametrize('value', [float('nan'), float('inf'), 2**53, {1: 'key'}, (1, 2),
                                  {'secret': object()}, '\ud800', 'x' * (MAX_JSON_BYTES + 1)])
def test_invalid_json_cannot_be_signed(value):
    with pytest.raises(DecisionTapeError):
        canonical_json(value)


def test_depth_cycles_and_duplicate_wire_keys_are_rejected():
    cyclic = []
    cyclic.append(cyclic)
    with pytest.raises(DecisionTapeError, match='complexity'):
        canonical_hash(cyclic)
    with pytest.raises(DecisionTapeError, match='duplicate_json_key'):
        DecisionTape.verify(b'{"version":1,"version":1}', binding=binding(), host_key=KEY)
    with pytest.raises(DecisionTapeError, match='invalid_tape_bytes'):
        DecisionTape.verify(b' ' * (MAX_TAPE_BYTES + 1), binding=binding(), host_key=KEY)


@pytest.mark.parametrize('key', [None, '', b'', b'too-short', 'x' * 32])
def test_host_key_must_be_explicit_bytes(key):
    with pytest.raises(DecisionTapeError, match='host_key_required'):
        DecisionTape(binding(), host_key=key)


def test_missing_decision_halts_without_hold_and_cannot_finish():
    tape = DecisionTape(binding(), host_key=KEY)
    req = request(tape)
    assert req['prior_receipt_hash'] == GENESIS
    with pytest.raises(DecisionRequired) as missing:
        tape.consume(req)
    assert missing.value.request == req
    assert missing.value.performance_evidence is False
    with pytest.raises(DecisionTapeError, match='unresolved_request'):
        tape.finish()
    receipt = record(tape, req)
    assert receipt['label'] == LABEL and receipt['request_hash'] == canonical_hash(req)
    assert tape.consume(req) == answer()
    summary = tape.finish()
    assert summary == dict(label=LABEL, decision_receipts_verified=True, decision_count=1,
                           model_decisions_verified=False, performance_evidence=False)
    assert KEY not in tape.dumps()


def test_authenticated_roundtrip_and_idempotent_cache_do_not_invoke_producer():
    tape, requests = populated()
    raw = tape.dumps()
    replay = DecisionTape.verify(raw, binding=binding(), host_key=KEY)
    assert replay.dumps() == raw
    for index, saved in enumerate(requests):
        req = request(replay, timestamp_ms=saved['timestamp_ms'])
        assert req == saved
        assert replay.consume(req) == answer()
        # Exact retry preserves original receipt; no duplicate append or model call.
        receipt = record(replay, req)
        assert receipt['request']['ordinal'] == index
        if index:
            assert req['prior_receipt_hash'] == canonical_hash(record(replay, requests[index - 1]))
    assert replay.dumps() == raw
    assert replay.finish()['decision_count'] == 2
    with pytest.raises(DecisionTapeError, match='idempotency_conflict'):
        record(replay, requests[0], decision='HOLD')


@pytest.mark.parametrize('field', list(binding()))
def test_current_host_binding_rejects_stale_source_config_data_prompt_and_model(field):
    tape, _ = populated(1)
    stale = binding()
    stale[field] = canonical_hash('changed') if len(stale[field]) == 64 else 'changed'
    with pytest.raises(DecisionTapeError, match='stale_binding'):
        DecisionTape.verify(tape.dumps(), binding=stale, host_key=KEY)


@pytest.mark.parametrize('field,value', [
    ('timestamp_ms', 1_700_000_001_000), ('market', 'OTHER:ETHUSDT'), ('timeframe', '15m'),
    ('role', 'other'), ('payload', {'close': 101}),
])
def test_exact_request_match_not_merely_timestamp(field, value):
    tape, _ = populated(1)
    replay = DecisionTape.verify(tape.dumps(), binding=binding(), host_key=KEY)
    req = request(replay, **{field: value})
    with pytest.raises(DecisionTapeError, match='request_mismatch'):
        replay.consume(req)
    with pytest.raises(DecisionTapeError, match='tape_failed'):
        replay.finish()


def test_chronology_global_ordinal_same_timestamp_and_no_unresolved_skip():
    tape = DecisionTape(binding(), host_key=KEY)
    first = request(tape)
    record(tape, first)
    tape.consume(first)
    second = request(tape, market='MOCK:BTCUSDT')
    assert second['ordinal'] == 1 and second['timestamp_ms'] == first['timestamp_ms']
    record(tape, second)
    tape.consume(second)
    with pytest.raises(DecisionTapeError, match='chronology_mismatch'):
        request(tape, timestamp_ms=first['timestamp_ms'] - 1)
    pending = DecisionTape(binding(), host_key=KEY)
    request(pending)
    with pytest.raises(DecisionTapeError, match='unresolved_request_changed'):
        request(pending, market='MOCK:BTCUSDT')


@pytest.mark.parametrize('field,value', [('ordinal', 1), ('prior_receipt_hash', 'f' * 64),
                                       ('payload_hash', 'not-a-digest'), ('timestamp_ms', True)])
def test_modified_request_is_rejected_before_record(field, value):
    tape = DecisionTape(binding(), host_key=KEY)
    req = request(tape)
    req[field] = value
    with pytest.raises(DecisionTapeError):
        record(tape, req)


def test_receipt_limit_and_unused_receipts_fail_closed():
    tape = DecisionTape(binding(), host_key=KEY, max_receipts=1)
    req = request(tape)
    record(tape, req)
    with pytest.raises(DecisionTapeError, match='unresolved_request'):
        tape.finish()
    tape.consume(req)
    req = request(tape, timestamp_ms=req['timestamp_ms'] + 1)
    with pytest.raises(DecisionTapeError, match='receipt_limit'):
        record(tape, req)
    complete, _ = populated(2)
    replay = DecisionTape.verify(complete.dumps(), binding=binding(), host_key=KEY)
    replay.consume(request(replay))
    with pytest.raises(DecisionTapeError, match='unconsumed_receipts'):
        replay.finish()


def test_tape_and_return_values_are_detached_and_key_is_never_exported():
    expected = binding()
    tape = DecisionTape(expected, host_key=KEY)
    expected['candidate_id'] = 'changed'
    req = request(tape)
    receipt = record(tape, req)
    receipt['response']['output']['decision'] = 'HOLD'
    result = tape.consume(req)
    result['output']['decision'] = 'HOLD'
    raw = tape.dumps()
    assert KEY not in raw and b'host_key' not in raw
    replay = DecisionTape.verify(raw, binding=binding(), host_key=KEY)
    assert replay.consume(request(replay)) == answer()


@pytest.mark.parametrize('edit', ['response', 'request', 'remove', 'extra', 'reorder', 'label', 'key'])
def test_tampering_truncation_extra_rows_and_old_claims_fail_authentication(edit):
    tape, _ = populated(2)
    value = json.loads(tape.dumps())
    key = KEY
    if edit == 'response':
        value['receipts'][0]['response']['output']['decision'] = 'HOLD'
    elif edit == 'request':
        value['receipts'][0]['request']['timestamp_ms'] += 1
    elif edit == 'remove':
        value['receipts'].pop()
    elif edit == 'extra':
        value['receipts'].append(value['receipts'][-1])
    elif edit == 'reorder':
        value['receipts'].reverse()
    elif edit == 'label':
        value['label'] = 'contemporaneous_real'
    else:
        key = b'different-host-only-tape-key-0001'
    with pytest.raises(DecisionTapeError):
        DecisionTape.verify(canonical_json(value), binding=binding(), host_key=key)


def _host_resign(kind, value):
    """Test fixture simulates a buggy host encoder; never used by a candidate."""
    body = {k: v for k, v in value.items() if k != 'signature'}
    value['signature'] = hmac.new(KEY, b'nerya.decision_tape.v1/' + kind.encode() + b'\0'
                                  + canonical_json(body), hashlib.sha256).hexdigest()


@pytest.mark.parametrize('defect,expected', [
    ('digest', 'digest_mismatch'), ('ordinal', 'ordinal_mismatch'), ('chain', 'chain_mismatch'),
    ('old_claim', 'invalid_version_or_label'), ('extra_field', 'invalid_schema'),
])
def test_valid_signature_does_not_waive_receipt_schema_digest_or_chain(defect, expected):
    tape, _ = populated(2)
    value = json.loads(tape.dumps())
    receipt = value['receipts'][1]
    if defect == 'digest':
        receipt['response_hash'] = 'f' * 64
    elif defect == 'ordinal':
        receipt['request']['ordinal'] = 0
    elif defect == 'chain':
        receipt['request']['prior_receipt_hash'] = GENESIS
    elif defect == 'old_claim':
        receipt['label'] = 'contemporaneous_real'
    else:
        receipt['performance_evidence'] = True
    _host_resign('receipt', receipt)
    _host_resign('tape', value)
    with pytest.raises(DecisionTapeError, match=expected):
        DecisionTape.verify(canonical_json(value), binding=binding(), host_key=KEY)


def test_authenticated_model_receipt_only_proves_decisions_not_historical_performance():
    tape = DecisionTape(binding(), host_key=KEY)
    req = request(tape)
    # This is a local encoder fixture, not a model invocation.
    record(tape, req, producer_kind='model')
    tape.consume(req)
    result = tape.finish()
    assert result['label'] == LABEL and result['model_decisions_verified'] is True
    assert result['performance_evidence'] is False


def producer_result(req):
    """Synthetic trusted-host result with a separately constructed request identity."""
    identity = {'binding': req, 'system': 'fixture pinned policy and role',
                'payload': {'close': 100}, 'provider': binding()['provider'],
                'model': binding()['model'], 'max_output_tokens': 128, 'temperature': 0}
    expected = canonical_hash(identity)
    raw = '{ "decision": "ENTRY", "confidence": 0.8 }'
    return expected, {
        'request_digest': expected, 'output': json.loads(raw), 'provider': binding()['provider'],
        'model': binding()['model'], 'generated_at': NOW / 1000 + .123,
        'response_sha256': hashlib.sha256(raw.encode()).hexdigest(),
        'reserved_tokens': 512, 'usage': {'input_tokens': 30, 'output_tokens': 10},
        'evidence_level': LABEL,
    }


def test_produced_result_preserves_raw_evidence_and_roundtrips_without_producer():
    tape = DecisionTape(binding(), host_key=KEY)
    req = request(tape)
    expected, result = producer_result(req)
    receipt = tape.append_produced(req, result=result, expected_request_digest=expected)
    assert receipt['response'] == answer()
    assert receipt['producer_receipt'] == {key: value for key, value in result.items() if key != 'output'}
    assert receipt['response_hash'] == canonical_hash(answer())
    assert receipt['response_hash'] != result['response_sha256']
    assert receipt['produced_at_ms'] == int(result['generated_at'] * 1000)
    assert tape.append_produced(req, result=result, expected_request_digest=expected) == receipt
    assert tape.consume(req) == answer()
    replay = DecisionTape.verify(tape.dumps(), binding=binding(), host_key=KEY)
    assert replay.consume(request(replay)) == answer()
    assert replay.dumps() == tape.dumps()
    assert replay.finish()['model_decisions_verified'] is True
    assert replay.finish()['performance_evidence'] is False


@pytest.mark.parametrize('field,value,code', [
    ('request_digest', 'f' * 64, 'producer_request_digest_mismatch'),
    ('provider', 'other', 'producer_identity_mismatch'),
    ('model', 'other', 'producer_identity_mismatch'),
    ('evidence_level', 'contemporaneous_real', 'invalid_producer_evidence'),
    ('generated_at', True, 'invalid_producer_timestamp'),
    ('generated_at', 1, 'nonretrospective_receipt'),
    ('response_sha256', 'invalid', 'invalid_digest'),
    ('reserved_tokens', False, 'invalid_integer'),
    ('reserved_tokens', 1, 'producer_usage_exceeds_reservation'),
    ('usage', {'input_tokens': -1, 'output_tokens': 10}, 'invalid_integer'),
    ('output', {'decision': 'ENTRY', 'confidence': .8, 'tools': []}, 'invalid_response_schema'),
])
def test_invalid_producer_results_never_append(field, value, code):
    tape = DecisionTape(binding(), host_key=KEY)
    req = request(tape)
    expected, result = producer_result(req)
    result[field] = value
    before = tape.dumps()
    with pytest.raises(DecisionTapeError, match=code):
        tape.append_produced(req, result=result, expected_request_digest=expected)
    assert tape.dumps() == before
    with pytest.raises(DecisionRequired):
        tape.consume(req)


def test_producer_digest_cannot_be_reused_across_timestamps_or_priors():
    tape = DecisionTape(binding(), host_key=KEY)
    first = request(tape)
    first_digest, result = producer_result(first)
    tape.append_produced(first, result=result, expected_request_digest=first_digest)
    tape.consume(first)
    second = request(tape, timestamp_ms=first['timestamp_ms'] + 3600000)
    second_digest, _ = producer_result(second)
    assert second_digest != first_digest
    with pytest.raises(DecisionTapeError, match='producer_request_digest_mismatch'):
        tape.append_produced(second, result=result, expected_request_digest=second_digest)


def test_producer_metadata_is_signed_and_usage_must_match_receipt():
    tape = DecisionTape(binding(), host_key=KEY)
    req = request(tape)
    expected, result = producer_result(req)
    tape.append_produced(req, result=result, expected_request_digest=expected)
    value = json.loads(tape.dumps())
    receipt = value['receipts'][0]
    receipt['producer_receipt']['reserved_tokens'] += 1
    with pytest.raises(DecisionTapeError, match='invalid_signature'):
        DecisionTape.verify(canonical_json(value), binding=binding(), host_key=KEY)
    receipt['usage']['input_tokens'] += 1
    _host_resign('receipt', receipt)
    _host_resign('tape', value)
    with pytest.raises(DecisionTapeError, match='producer_receipt_mismatch'):
        DecisionTape.verify(canonical_json(value), binding=binding(), host_key=KEY)
    receipt['producer_receipt'] = None
    _host_resign('receipt', receipt)
    _host_resign('tape', value)
    with pytest.raises(DecisionTapeError, match='invalid_schema'):
        DecisionTape.verify(canonical_json(value), binding=binding(), host_key=KEY)


def test_invalid_schema_and_oversize_append_do_not_change_tape():
    tape = DecisionTape(binding(), host_key=KEY)
    req = request(tape)
    before = tape.dumps()
    for invalid in ([], {}, {'ordinal': 0}):
        with pytest.raises(DecisionTapeError, match='invalid_schema'):
            record(tape, invalid)
    oversized = answer()
    oversized['output']['veto_reason'] = 'x' * MAX_JSON_BYTES
    with pytest.raises(DecisionTapeError, match='json_size_limit'):
        record(tape, req, response=oversized)
    assert tape.dumps() == before
    record(tape, req)
    assert tape.consume(req) == answer()


def test_twenty_receipt_bound_and_restart_without_duplicate_production():
    tape, requests = populated(20)
    replay = DecisionTape.verify(tape.dumps(), binding=binding(), host_key=KEY)
    for saved in requests:
        assert replay.consume(request(replay, timestamp_ms=saved['timestamp_ms'])) == answer()
    assert replay.finish()['decision_count'] == 20
    next_request = request(replay, timestamp_ms=requests[-1]['timestamp_ms'] + 1)
    with pytest.raises(DecisionTapeError, match='receipt_limit'):
        record(replay, next_request)


def test_exact_next_receipt_required_on_same_timestamp():
    tape = DecisionTape(binding(), host_key=KEY)
    for market in ('MOCK:ETHUSDT', 'MOCK:BTCUSDT'):
        req = request(tape, market=market)
        record(tape, req)
        tape.consume(req)
    replay = DecisionTape.verify(tape.dumps(), binding=binding(), host_key=KEY)
    wrong_order = request(replay, market='MOCK:BTCUSDT')
    with pytest.raises(DecisionTapeError, match='request_mismatch'):
        replay.consume(wrong_order)


@pytest.mark.parametrize('response', [
    {'ok': False, 'output': {}}, {'ok': True, 'output': {'decision': 'BUY', 'confidence': .8}},
    {'ok': True, 'output': {'decision': 'ENTRY', 'confidence': True}},
    {'ok': True, 'output': {'decision': 'ENTRY', 'confidence': 2}},
    {'ok': True, 'output': {'decision': 'ENTRY', 'confidence': .8, 'performance_evidence': True}},
])
def test_candidate_verdict_cannot_smuggle_provenance_or_invalid_schema(response):
    tape = DecisionTape(binding(), host_key=KEY)
    req = request(tape)
    with pytest.raises(DecisionTapeError):
        record(tape, req, response=response)
    with pytest.raises(DecisionRequired):
        tape.consume(req)


@pytest.fixture
def gate(monkeypatch):
    for name in ('strategy_execution', 'ema_cross', 'ema_cross_confirm'):
        module = ModuleType(name)
        monkeypatch.setitem(sys.modules, name, module)
        asset = TEMPLATES / (name + '.py.j2')
        exec(compile(asset.read_text(), str(asset), 'exec'), module.__dict__)
    return module


def gated_run(gate, tape, *, produce=False):
    """Synthetic account; actual EMA/closed-signal/confirmation code and ResultBuilder."""
    rows, state, dispatches, trades = [], {}, [], []
    quantity = 0
    params = dict(indicator={'fast_period': 2, 'slow_period': 5}, lookback=160,
                  confidence=.8, confirmation_agent='market_analyst', confirmation_min_confidence=.6,
                  sizing={'method': 'pct_nav', 'pct_nav': .1})
    def confirm(role, *, payload):
        req = tape.bind(timestamp_ms=ctx.clock.now_ms(), market='MOCK:ETHUSDT',
                           timeframe='1h', role=role, payload=payload)
        try:
            return tape.consume(req)
        except DecisionRequired:
            if not produce:
                raise
            dispatches.append(req)
            record(tape, req)
            return tape.consume(req)
    def enter(*, market, side, sizing, protection, confidence, reasoning_ref):
        nonlocal quantity
        quantity = 1
        trades.append(('open', rows[-1]['ts']))
        return {'status': 'filled', 'order_id': 'synthetic-entry'}
    def close(*, market, side, confidence, reasoning_ref):
        nonlocal quantity
        quantity = 0
        trades.append(('close', rows[-1]['ts']))
        return {'status': 'filled', 'order_id': 'synthetic-exit'}
    ctx = NS(config=StrategyConfig(strategy_id='fixture', markets=('MOCK:ETHUSDT',),
                                  extras={'params': params, 'timeframe': '1h'}),
             trigger={}, result=ResultBuilder(),
             market=NS(candles=lambda market, *, timeframe, limit: rows[-limit:]),
             clock=StrategyClock(_now_ms_fn=lambda: (rows[-1]['ts'] + 3600) * 1000),
             state=NS(get=state.get, set=state.__setitem__),
             subagents=NS(run=confirm),
             portfolio=NS(positions=lambda market: [StrategyPosition(size=quantity)] if quantity else []),
             trading=NS(open_position=enter, close_position=close, execution_status=lambda receipt: {
                 'status': 'filled', 'terminal': True, 'reconciled': True, 'has_open_orders': False}))
    for index, price in enumerate([100.] * 8 + [120., 120., 80., 80., 120.]):
        # Explicit synthetic flat OHLCV, not observed historical market data.
        rows.append({'ts': 1700006400 + index * 3600, 'open': price, 'high': price,
                     'low': price, 'close': price, 'volume': 100.})
        before = len(dispatches)
        gate.run(ctx)
        if index < 8 or index in (9, 10, 11):
            assert len(dispatches) == before  # Warmup/flat/holding/exit never calls Agent.
        gate.run(ctx)  # Duplicate candle must not consume another tape row.
    return dispatches, trades


def test_real_gates_capture_then_replay_exact_sequence_with_synthetic_producer(gate):
    tape = DecisionTape(binding(), host_key=KEY)
    requests, trades = gated_run(gate, tape, produce=True)
    assert len(requests) == 2
    assert [kind for kind, _ in trades] == ['open', 'close', 'open']
    assert tape.finish()['model_decisions_verified'] is False
    replay = DecisionTape.verify(tape.dumps(), binding=binding(), host_key=KEY)
    dispatched, replay_trades = gated_run(gate, replay)
    assert dispatched == [] and replay_trades == trades
    assert replay.finish()['decision_count'] == 2
    assert replay.finish()['performance_evidence'] is False


def test_real_gate_missing_receipt_never_reaches_synthetic_trade(gate):
    tape = DecisionTape(binding(), host_key=KEY)
    with pytest.raises(DecisionRequired) as missing:
        gated_run(gate, tape)
    assert missing.value.request['ordinal'] == 0
    assert missing.value.request['timestamp_ms'] == (1700006400 + 9 * 3600) * 1000
    assert json.loads(tape.dumps())['receipts'] == []
