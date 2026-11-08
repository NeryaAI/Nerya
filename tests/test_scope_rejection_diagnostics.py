"""Offline rejection evidence: real admission, inventory and exact-span checks."""
from contextlib import closing
import json
import logging

import pytest

from nerya.db.sqlite import connect_readonly
from nerya.strategies import request_scope as scope
from nerya.tools.types import ToolCall
import test_request_scope as request_scope_fixtures
from test_request_scope import FakeGateway, assistant_chain, install_fake, spans_output

env = request_scope_fixtures.env

pytestmark = pytest.mark.smoke
CANARY = 'PRIVATE_SCOPE_CANARY_8bf19'
REASONS = ('unbound_followup_span', 'outside_amendment_span')
CHANGES = {
    'venue': ('Binance', 'OKX'), 'protocol': ('Uniswap v3', 'Aave'),
    'base': ('ETH', 'BTC'), 'quote': ('USDT', 'USDC'), 'asset': ('USDC', 'DAI'),
    'chain': ('Ethereum', 'Solana'), 'timeframe': ('1h', '2h'),
    'window_days': ('30 days', '7 days'),
    'start_utc': ('2026-01-01', '2026-01-02'),
    'end_utc': ('2026-02-01', '2026-02-02'),
}


def _contexts(config):
    with closing(connect_readonly(config.paths.db)) as con:
        return {row['command_id']: row['context_json'] for row in
                con.execute('SELECT command_id,context_json FROM agent_commands')}


def _case(config, field, reason, *, additional=False, prior_field=True):
    old, new = CHANGES[field]
    values = {'asset': 'USDC'}
    if field in {'base', 'quote'}:
        values = {'base': 'ETH', 'quote': 'USDT'}
    if prior_field:
        values[field] = old
    if field in {'start_utc', 'end_utc'}:
        values.update(start_utc=CHANGES['start_utc'][0], end_utc=CHANGES['end_utc'][0])
    text = values.get('base', '') + '/' + values.get('quote', '') if 'base' in values else ''
    text += ' ' + ' '.join(v for k, v in values.items() if k not in {'base', 'quote'})
    if additional:
        text = 'DAI; ' + text
    text += '; ' + CANARY + ' provider-body token=private-fixture'

    def initial(docs):
        result = spans_output(docs[0], **({'asset': 'DAI'} if additional else values))
        if additional:
            result['additional_resources'] = [{k: v for k, v in
                spans_output(docs[0], **values)['fields'].items() if k in scope.RESOURCE_FIELDS}]
        return result

    original = assistant_chain(config, f'Continue {new}; confirm; {CANARY}',
        initial_text=text, initial_choose=initial)

    def rejected(docs):
        result = initial(docs)
        fields = result['additional_resources'][0] if additional else result['fields']
        fields[field] = spans_output(docs[-1], **{field: new})['fields'][field]
        if reason == 'outside_amendment_span':
            result['amendments'] = [spans_output(docs[-1], asset='confirm')['fields']['asset']]
        else:
            # Retain original lexical coverage so the real inventory check
            # reaches value_for, rather than rejecting omitted evidence first.
            result['additional_resources'].append({k: v for k, v in
                spans_output(docs[0], **values)['fields'].items() if k in scope.RESOURCE_FIELDS})
            if field in {'timeframe', 'window_days'}:
                other = 'window_days' if field == 'timeframe' else 'timeframe'
                result['fields'][other] = spans_output(docs[0], **{field: old})['fields'][field]
        return result

    expected = {'field': field, 'reason': reason, 'source_document_index': 1}
    if prior_field:
        expected['authority_document_index'] = 0
    return original, FakeGateway(rejected), expected


def _assert_safe(value):
    serialized = json.dumps(value, default=str)
    for private in (CANARY, '/private/', 'provider-body', 'private-fixture', 'request_hash',
                    'input_hash', 'normalized_evidence', 'command_id', 'start_utc_value'):
        assert private not in serialized


def _assert_log(caplog, expected):
    record, = [r for r in caplog.records if r.name == scope.__name__]
    assert record.exc_info is None and record.stack_info is None
    message = record.getMessage()
    assert 'diagnostic_code=invalid_semantics' in message
    assert 'command_id=child-follow' in message and 'function=value_for' in message
    assert 'diagnostic_metadata=' + scope._json(expected) in message
    assert CANARY not in message and '/private/' not in message


@pytest.mark.parametrize('field', scope.FIELDS)
@pytest.mark.parametrize('reason', REASONS)
def test_ensure_contract_exact_rejection_branch(env, caplog, field, reason):
    original, fake, expected = _case(env, field, reason)
    before = _contexts(env)
    caplog.clear()
    with pytest.raises(scope.ScopeError) as raised:
        scope.ensure_contract(env, gateway=fake)
    error = raised.value
    assert error.diagnostic_code == 'invalid_semantics'
    assert error.diagnostic_metadata == expected
    assert str(error) == 'request_scope_unverified'
    _assert_safe(vars(error))
    _assert_log(caplog, expected)
    assert len(fake.calls) == (2 if reason == 'outside_amendment_span' else 1) and _contexts(env) == before
    assert scope.current_contract(env, command_id='child-create') == original
    assert 'request_scope' not in json.loads(before['child-follow'])


@pytest.mark.parametrize('field', scope.FIELDS)
@pytest.mark.parametrize('reason', REASONS)
def test_guard_serializes_safe_rejection_without_operation_retry(env, monkeypatch, caplog, field, reason):
    _, fake, expected = _case(env, field, reason)
    before = _contexts(env)
    install_fake(monkeypatch, fake)
    caplog.clear()
    result, pinned = scope.guard(env, ToolCall(name='strategy_draft_proposal', arguments={}))
    assert pinned == {} and result.is_error and result.semantic_success is False
    data = result.content[0].data
    for envelope in (data, data['next_required_action'], result.error.detail, result.error.recovery_hint):
        assert envelope['diagnostic_metadata'] == expected
        assert envelope['diagnostic_code'] == 'invalid_semantics'
    assert result.error.retryable is False
    assert result.error.recovery_hint['safe_to_retry'] is False
    assert result.error.recovery_hint['preserve_candidate'] is True
    _assert_safe(result.asdict())
    _assert_safe(result.text() + result.error.message)
    _assert_log(caplog, expected)
    assert len(fake.calls) == (2 if reason == 'outside_amendment_span' else 1) and _contexts(env) == before
    assert not env.paths.proposals.exists()


@pytest.mark.parametrize('field', scope.RESOURCE_FIELDS)
@pytest.mark.parametrize('reason', REASONS)
def test_additional_resource_uses_same_role_authority(env, field, reason):
    _, fake, expected = _case(env, field, reason, additional=True)
    before = _contexts(env)
    with pytest.raises(scope.ScopeError) as raised:
        scope.ensure_contract(env, gateway=fake)
    assert raised.value.diagnostic_metadata == expected
    assert len(fake.calls) == (2 if reason == 'outside_amendment_span' else 1) and _contexts(env) == before


def test_unknown_prior_role_omits_authority(env):
    _, fake, expected = _case(env, 'venue', REASONS[0], prior_field=False)
    with pytest.raises(scope.ScopeError) as raised:
        scope.ensure_contract(env, gateway=fake)
    assert raised.value.diagnostic_metadata == expected


class _Str(str):
    pass


class _Int(int):
    pass


@pytest.mark.parametrize('metadata,expected', [
    ({'field': 'asset', 'reason': REASONS[0], 'source_document_index': 1,
      'authority_document_index': 0, 'rawtext': CANARY, 'hash': CANARY, 'path': '/private/'},
     {'field': 'asset', 'reason': REASONS[0], 'source_document_index': 1, 'authority_document_index': 0}),
    *[({'field': value}, {}) for value in (CANARY, 'resources[0].asset', _Str('asset'), [], {}, None, 1)],
    *[({'reason': value}, {}) for value in (CANARY, 'invalid_span', _Str(REASONS[0]), [], None, 1)],
    *[({key: value}, {}) for key in ('source_document_index', 'authority_document_index')
      for value in (True, False, -1, 1.0, '1', _Int(1), [], {}, None)],
    *[({'unchanged_text': value}, {}) for value in (True, False, CANARY, 1, None)],
    (None, {}), ([], {}), (CANARY, {}),
])
def test_metadata_is_strictly_allowlisted(metadata, expected):
    error = scope.ScopeError(diagnostic_metadata=metadata)
    assert error.diagnostic_metadata == expected
    _assert_safe(vars(error))


def test_diagnostic_code_compatibility():
    for code in ('invalid_semantics', _Str('invalid_semantics')):
        assert scope.ScopeError(diagnostic_code=code).diagnostic_code is code
    assert scope.ScopeError(diagnostic_code=CANARY).diagnostic_code == 'unverified_scope'


def test_metadata_is_revalidated_on_log_and_recovery(env, monkeypatch, caplog):
    error = scope.ScopeError(diagnostic_metadata={'field': 'asset'})
    error.diagnostic_metadata.update(field=CANARY, reason=CANARY,
        source_document_index=True, authority_document_index=1.5, rawtext=CANARY)
    calls = []

    class Response:
        provider = 'openai'

        @property
        def model(self):
            raise error

    def respond(self, **kwargs):
        calls.append(kwargs)
        return Response()

    # Mutate after the host logs, before guard serializes recovery. The log
    # must already hold a safe serialized copy; recovery must sanitize again.
    class Mutate(logging.Handler):
        def emit(self, record):
            if record.name == scope.__name__:
                error.diagnostic_metadata.update(field=CANARY, rawtext=CANARY)

    monkeypatch.setattr('nerya.llm.gateway.LLMGateway.call_messages', respond)
    handler = Mutate()
    logger = logging.getLogger(scope.__name__)
    logger.addHandler(handler)
    try:
        result, _ = scope.guard(env, ToolCall(name='strategy_draft_proposal', arguments={}))
    finally:
        logger.removeHandler(handler)
    for envelope in (result.content[0].data, result.error.detail, result.error.recovery_hint):
        assert envelope['diagnostic_metadata'] == {}
    _assert_safe(result.asdict())
    assert CANARY not in caplog.text and len(calls) == 1
