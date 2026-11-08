"""Resource-shape failures expose roles and indices, never admitted text."""
from contextlib import closing
import json

import pytest

from nerya.db.sqlite import connect_readonly
from nerya.strategies import request_scope as scope
from nerya.tools.types import ToolCall
import test_request_scope as request_scope_fixtures
from test_request_scope import admit, FakeGateway, install_fake, spans_output

env = request_scope_fixtures.env

pytestmark = pytest.mark.smoke
CANARY = 'PRIVATE_RESOURCE_CANARY_8bf19'


@pytest.mark.parametrize('values,reason', [
    ({'protocol': 'Aave'}, 'missing_asset_or_pair'),
    ({'quote': 'USDC'}, 'missing_asset_or_pair'),
    ({'asset': 'USDC', 'quote': 'USDC'}, 'mixed_asset_and_pair'),
])
@pytest.mark.parametrize('additional', [False, True])
def test_shape_failure_is_safe_locatable_bounded_and_not_persisted(env, monkeypatch, caplog, values, reason, additional):
    text = 'Aave' if 'protocol' in values else 'USDC'
    if additional:
        text = 'DAI; ' + text
    admit(env, cid='invalid-shape', sid='invalid-shape-session', text=text + '; ' + CANARY)

    def choose(docs):
        result = spans_output(docs[0], **({'asset': 'DAI'} if additional else values))
        if additional:
            result['additional_resources'] = [{key: value for key, value in
                spans_output(docs[0], **values)['fields'].items() if key in scope.RESOURCE_FIELDS}]
        return result

    fake = FakeGateway(choose)
    install_fake(monkeypatch, fake)
    result, pinned = scope.guard(env, ToolCall(name='strategy_draft_proposal'))
    assert result.is_error and pinned == {}
    index = int(additional)
    data = result.content[0].data
    for envelope in (data, data['next_required_action'], result.error.detail, result.error.recovery_hint):
        assert envelope['reason'] == 'request_scope_unverified'
        assert envelope['diagnostic_code'] == 'invalid_resource_shape'
        assert envelope['diagnostic_metadata'] == {'reason': reason, 'resource_index': index}
        assert envelope['differences'] == [{
            'field': f'resources[{index}]', 'reason': reason,
            'expected': 'asset OR base+quote', 'actual': ','.join(k for k in scope.RESOURCE_FIELDS if k in values)}]
    assert result.error.retryable is False
    assert result.error.recovery_hint['safe_to_retry'] is False
    assert result.error.recovery_hint['preserve_candidate'] is True
    assert CANARY not in json.dumps(result.asdict()) + caplog.text
    assert 'function=_canonical' in caplog.text and 'diagnostic_code=invalid_resource_shape' in caplog.text
    assert len(fake.calls) == 2
    assert fake.calls[0]['messages'] == fake.calls[1]['messages']
    assert 'extraction_repair' in fake.calls[1]['system']
    with closing(connect_readonly(env.paths.db)) as con:
        context = con.execute("SELECT context_json FROM agent_commands WHERE command_id='invalid-shape'").fetchone()[0]
    assert 'request_scope' not in json.loads(context)
    assert not env.paths.proposals.exists()


@pytest.mark.parametrize('corrected', [{'protocol': 'Aave', 'asset': 'USDC'},
                                      {'protocol': 'Aave', 'asset': 'DAI'}])
def test_one_shape_repair_requires_exact_admitted_asset(env, corrected):
    admit(env, cid='repair-shape', sid='repair-shape-session', text='Aave USDC; ' + CANARY)

    def choose(docs):
        values = corrected if len(fake.calls) == 2 else {'protocol': 'Aave', 'quote': 'USDC'}
        result = spans_output(docs[0], **{k: v for k, v in values.items() if v != 'DAI'})
        if values.get('asset') == 'DAI':
            result['fields']['asset'] = dict(result['fields']['protocol'], text='DAI')
        return result

    fake = FakeGateway(choose)
    if corrected['asset'] == 'USDC':
        contract = scope.ensure_contract(env, gateway=fake)
        resource, = contract['constraints']['resources']
        assert resource['asset'] == 'USDC' and 'quote' not in resource
    else:
        with pytest.raises(scope.ScopeError) as raised:
            scope.ensure_contract(env, gateway=fake)
        assert raised.value.diagnostic_code == 'invalid_span'
    assert len(fake.calls) == 2


@pytest.mark.parametrize('index', [True, -1, 16, '0', 0.5])
def test_resource_location_metadata_rejects_nonindices(index):
    assert scope.ScopeError(diagnostic_metadata={'resource_index': index}).diagnostic_metadata == {}
