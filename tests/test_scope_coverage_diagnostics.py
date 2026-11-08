"""Offline coverage failures retain only safe locations through model projection."""
from contextlib import closing
import json
import logging

import pytest

from nerya.agent.loop import WorkspaceNativeAgentLoop
from nerya.agent.tool_projection import project_tool_results
from nerya.db.sqlite import connect, connect_readonly
from nerya.strategies import request_scope as scope
from nerya.tools.types import ToolCall
import test_request_scope as request_scope_fixtures
from test_request_scope import FakeGateway, assistant_chain, install_fake, spans_output

env = request_scope_fixtures.env

pytestmark = pytest.mark.smoke
CANARY = 'PRIVATE_COVERAGE_CANARY_4c19'
CASES = [
    ('Binance ETH/USDT 1h 30 days', 'Binance', 'uncovered_native_name', None,
     {'base': 'ETH', 'quote': 'USDT', 'timeframe': '1h', 'window_days': '30 days'}),
    ('Binance ETH/USDT', 'ETH', 'uncovered_native_name', 'base',
     {'venue': 'Binance', 'quote': 'USDT'}),
    ('Binance ETH/USDT', 'USDT', 'uncovered_pair_asset', 'quote',
     {'venue': 'Binance', 'base': 'ETH'}),
    ('Binance ETH/USDT 1h 30 days', '1h', 'uncovered_duration', None,
     {'venue': 'Binance', 'base': 'ETH', 'quote': 'USDT', 'window_days': '30 days'}),
    ('Binance ETH/USDT 1h 30 days', '30 days', 'uncovered_duration', None,
     {'venue': 'Binance', 'base': 'ETH', 'quote': 'USDT', 'timeframe': '1h'}),
    ('Uniswap v3 ETH/USDC 最近30天', 'Uniswap', 'uncovered_native_name', None,
     {'base': 'ETH', 'quote': 'USDC', 'window_days': '30天'}),
    ('Binance ETH/USDT and BTC/USDC', 'BTC', 'uncovered_pair_asset', 'base',
     {'venue': 'Binance', 'base': 'ETH', 'quote': 'USDT'}),
]


def _contexts(config):
    with closing(connect_readonly(config.paths.db)) as con:
        return list(con.execute('SELECT command_id,context_json,revision FROM agent_commands'))


def _case(config, case):
    text, token, reason, field, values = case
    # Unicode prefix proves offsets are Python character indices, not bytes.
    text = '请求：' + text + '; ' + CANARY + ' provider-body token=private-fixture'
    with closing(connect(config.paths.db)) as con, con:
        request = json.loads(con.execute('SELECT request_json FROM agent_commands').fetchone()[0])
        request['payload']['text'] = text
        con.execute('UPDATE agent_commands SET request_json=?', (json.dumps(request),))
    expected = {'reason': reason, 'source_document_index': 0,
                'token_start': text.index(token), 'token_end': text.index(token) + len(token)}
    if field:
        expected['field'] = field
    return FakeGateway(lambda docs: spans_output(docs[0], **values)), expected


def _assert_safe(value):
    serialized = json.dumps(value, default=str)
    for private in (CANARY, 'Binance', 'Uniswap', 'ETH', 'USDT', 'BTC', 'USDC', '30 days',
                    '请求', 'provider-body', 'private-fixture', 'normalized_evidence', 'request_hash'):
        assert private not in serialized


def _assert_log(caplog, expected):
    record, = [r for r in caplog.records if r.name == scope.__name__]
    assert record.exc_info is None and record.stack_info is None
    message = record.getMessage()
    assert 'diagnostic_code=incomplete_scope_evidence' in message
    assert 'function=_canonical' in message
    assert 'diagnostic_metadata=' + scope._json(expected) in message
    _assert_safe(message)


@pytest.mark.parametrize('case', CASES)
def test_real_ensure_coverage_branch_retains_safe_metadata(env, caplog, case):
    fake, expected = _case(env, case)
    before = _contexts(env)
    with pytest.raises(scope.ScopeError) as raised:
        scope.ensure_contract(env, gateway=fake)
    assert raised.value.diagnostic_code == 'incomplete_scope_evidence'
    assert raised.value.diagnostic_metadata == expected
    assert str(raised.value) == 'request_scope_unverified'
    _assert_safe(vars(raised.value))
    _assert_log(caplog, expected)
    assert len(fake.calls) == 1 and _contexts(env) == before
    assert not env.paths.proposals.exists()


@pytest.mark.parametrize('case', CASES)
def test_guard_metadata_survives_real_model_and_event_projection(env, monkeypatch, caplog, case):
    fake, expected = _case(env, case)
    install_fake(monkeypatch, fake)
    before = _contexts(env)
    result, pinned = scope.guard(env, ToolCall(name='strategy_draft_proposal', arguments={}))
    assert pinned == {} and result.is_error and result.semantic_success is False
    data = result.content[0].data
    for envelope in (data, data['next_required_action'], result.error.detail, result.error.recovery_hint):
        assert envelope['diagnostic_metadata'] == expected
        assert envelope['diagnostic_code'] == 'incomplete_scope_evidence'
    assert result.error.retryable is False
    assert result.error.recovery_hint['safe_to_retry'] is False
    assert result.error.recovery_hint['preserve_candidate'] is True
    assert result.error.recovery_hint['action'] == 'inspect_scope_extraction'
    label = 'diagnostic_metadata=' + scope._json(expected)
    assert label in result.error.message
    # No loop initialization or execution: use the real pure render/projection path.
    loop = object.__new__(WorkspaceNativeAgentLoop)
    projection = project_tool_results([result], render_tool_result=loop._render_tool_result,
                                     rendered_tool_result_text=loop._rendered_tool_result_text)
    block, = projection.transcript_blocks
    assert block['is_error'] is True
    assert label in block['content'][0]['text']
    assert '<tool_use_error>' in block['content'][0]['text']
    event, = projection.event_blocks
    assert label in event['error']
    assert event['recovery']['diagnostic_metadata'] == expected
    _assert_safe(result.asdict())
    _assert_safe(projection.transcript_blocks)
    _assert_safe(projection.event_blocks)
    _assert_log(caplog, expected)
    assert len(fake.calls) == 1 and _contexts(env) == before
    assert not env.paths.proposals.exists()


def test_amended_answer_restat_of_already_bound_chain_is_covered(env):
    docs = [
        {'command_id': 'c0', 'text': 'Aave V3 Ethereum USDC, pool aa70268e-4b52-42bf-a116-608b370f9501, last 30 days'},
        {'command_id': 'c1', 'text': '确认 Ethereum 池 aa70268e-4b52-42bf-a116-608b370f9501'},
    ]
    def bind(doc, text):
        start = doc['text'].index(text)
        return {'command_id': doc['command_id'], 'start': start, 'end': start + len(text), 'text': text}
    fields = dict.fromkeys(scope.FIELDS)
    fields.update(protocol=bind(docs[0], 'Aave V3'), chain=bind(docs[0], 'Ethereum'),
                  asset=bind(docs[0], 'USDC'), pool=bind(docs[0], 'aa70268e-4b52-42bf-a116-608b370f9501'),
                  window_days=bind(docs[0], '30 days'))
    output = {'fields': fields, 'additional_resources': [],
              'amendments': [bind(docs[1], docs[1]['text'])], 'excluded_spans': []}
    scope._named_resources_covered(output, docs)
    docs[1] = {'command_id': 'c1', 'text': '确认 Solana 池 aa70268e-4b52-42bf-a116-608b370f9501'}
    output['amendments'] = [bind(docs[1], docs[1]['text'])]
    with pytest.raises(scope.ScopeError) as raised:
        scope._named_resources_covered(output, docs)
    assert raised.value.diagnostic_metadata['reason'] == 'uncovered_native_name'
    assert raised.value.diagnostic_metadata['source_document_index'] == 1


def test_exact_repeated_multiword_protocol_is_covered(env):
    docs = [
        {'command_id': 'c0', 'text': 'Uniswap V3 Ethereum WETH/USDC 最近30天'},
        {'command_id': 'c1', 'text': '继续 Uniswap V3 Ethereum WETH/USDC 最近30天'},
    ]
    def bind(doc, text):
        start = doc['text'].index(text)
        return {'command_id': doc['command_id'], 'start': start, 'end': start + len(text), 'text': text}
    fields = dict.fromkeys(scope.FIELDS)
    fields.update(protocol=bind(docs[0], 'Uniswap V3'), chain=bind(docs[0], 'Ethereum'),
                   base=bind(docs[0], 'WETH'), quote=bind(docs[0], 'USDC'),
                   window_days=bind(docs[0], '最近30天'))
    output = {'fields': fields, 'additional_resources': [],
              'amendments': [bind(docs[1], docs[1]['text'])], 'excluded_spans': []}
    scope._named_resources_covered(output, docs)

def test_amendment_gap_reports_later_document_index(env, caplog):
    text = 'Change venue to OKX; ' + CANARY
    original = assistant_chain(env, text)

    def choose(docs):
        result = spans_output(docs[0], venue='Binance', base='ETH', quote='USDT',
                              timeframe='1h', window_days='30 days')
        result['amendments'] = [spans_output(docs[-1], asset=text)['fields']['asset']]
        return result

    fake = FakeGateway(choose)
    before = _contexts(env)
    caplog.clear()
    with pytest.raises(scope.ScopeError) as raised:
        scope.ensure_contract(env, gateway=fake)
    expected = {'reason': 'uncovered_native_name', 'source_document_index': 1,
                'token_start': text.index('OKX'), 'token_end': text.index('OKX') + 3}
    assert raised.value.diagnostic_metadata == expected
    _assert_log(caplog, expected)
    assert len(fake.calls) == 1 and _contexts(env) == before
    assert scope.current_contract(env, command_id='child-create') == original


class _Int(int):
    pass


class _Str(str):
    pass


@pytest.mark.parametrize('reason', ['uncovered_native_name', 'uncovered_pair_asset', 'uncovered_duration'])
def test_coverage_metadata_allowlist(reason):
    metadata = {'reason': reason, 'source_document_index': 0, 'token_start': 0, 'token_end': 16000}
    assert scope.ScopeError(diagnostic_metadata={**metadata, 'token': CANARY, 'value': CANARY,
        'body': CANARY, 'text': CANARY}).diagnostic_metadata == metadata
    assert scope.ScopeError(diagnostic_metadata={'reason': _Str(reason)}).diagnostic_metadata == {}


@pytest.mark.parametrize('start,end', [
    (True, 2), (0, False), (-1, 2), (0, 16001), (1, 1), (2, 1),
    (1.0, 2), (0, 2.0), ('0', 2), (0, '2'), (_Int(0), 2), (0, _Int(2)),
    (None, 2), (0, None), ([], 2), (0, {}),
])
def test_invalid_offsets_are_not_projected(start, end):
    assert scope.ScopeError(diagnostic_metadata={'token_start': start, 'token_end': end}).diagnostic_metadata == {}


@pytest.mark.parametrize('key', ['token_start', 'token_end'])
def test_incomplete_offset_pair_is_omitted(key):
    assert scope.ScopeError(diagnostic_metadata={key: 1}).diagnostic_metadata == {}


def test_empty_metadata_does_not_append_message_label(env, monkeypatch):
    fake = FakeGateway(lambda docs: {**spans_output(docs[0]), 'status': 'ambiguous'})
    install_fake(monkeypatch, fake)
    result, _ = scope.guard(env, ToolCall(name='strategy_draft_proposal', arguments={}))
    assert result.error.detail['diagnostic_metadata'] == {}
    assert 'diagnostic_metadata=' not in result.error.message
    assert result.error.retryable is False and len(fake.calls) == 1


def test_mutated_metadata_is_sanitized_again_for_log_message_and_projection(env, monkeypatch, caplog):
    expected = {'reason': 'uncovered_duration', 'source_document_index': 0,
                'token_start': 3, 'token_end': 5}
    error = scope.ScopeError(diagnostic_code='incomplete_scope_evidence', diagnostic_metadata=expected)
    error.diagnostic_metadata.update(token=CANARY, body=CANARY, field=CANARY)
    calls = []

    class Response:
        provider = 'openai'

        @property
        def model(self):
            raise error

    def respond(self, **kwargs):
        calls.append(kwargs)
        return Response()

    class Mutate(logging.Handler):
        def emit(self, record):
            if record.name == scope.__name__:
                error.diagnostic_metadata.update(token_start=True, token_end=16001, body=CANARY, reason=CANARY)

    monkeypatch.setattr('nerya.llm.gateway.LLMGateway.call_messages', respond)
    handler = Mutate()
    logger = logging.getLogger(scope.__name__)
    logger.addHandler(handler)
    before = _contexts(env)
    try:
        result, _ = scope.guard(env, ToolCall(name='strategy_draft_proposal', arguments={}))
    finally:
        logger.removeHandler(handler)
    record, = [r for r in caplog.records if r.name == scope.__name__]
    assert 'diagnostic_metadata=' + scope._json(expected) in record.getMessage()
    safe = {'source_document_index': 0}
    assert 'diagnostic_metadata=' + scope._json(safe) in result.error.message
    for envelope in (result.content[0].data, result.error.detail, result.error.recovery_hint):
        assert envelope['diagnostic_metadata'] == safe
    loop = object.__new__(WorkspaceNativeAgentLoop)
    projection = project_tool_results([result], render_tool_result=loop._render_tool_result,
                                     rendered_tool_result_text=loop._rendered_tool_result_text)
    _assert_safe(result.asdict())
    _assert_safe(projection.transcript_blocks)
    _assert_safe(projection.event_blocks)
    _assert_safe(caplog.text)
    assert len(calls) == 1 and _contexts(env) == before
