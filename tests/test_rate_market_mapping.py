"""Real provider shapes through discovery, documented bundle and draft scope guard."""
from copy import deepcopy
from pathlib import Path

import pytest

from nerya.core import yaml_io
from nerya.data_api.defillama import defillama_specs
from nerya.data_api.registry import DataApiRegistry
from nerya.llm.tool_compaction import compact_tool_result
from nerya.strategies import request_scope as scope
from nerya.tools.native.data_api import data_api_handler
from nerya.tools.native.strategy_runtime import strategy_draft_proposal_handler
from nerya.tools.types import ToolCall
from test_defillama_data_api import Fake, PID, POOL
from test_request_scope import (assistant_chain, env, admit, FakeGateway, install_fake,
                                spans_output, fake_execution)  # noqa: F401

pytestmark = pytest.mark.smoke
REFERENCE = (Path(__file__).parents[1]
             / 'nerya/skills/builtin/strategy_author/references/rate-policy.md')


def discover(action='yield_pools', *, pool=POOL, **options):
    registry = DataApiRegistry()
    for spec in defillama_specs(Fake(pools=[pool])):
        registry.register_action(spec)
    args = ({'project': pool['project'], 'chain': pool['chain'], 'symbol': pool['symbol']}
            if action == 'yield_pools' else
            {'pool_id': PID, 'start': '2026-10-20', 'end': '2026-10-22'})
    result = data_api_handler(ToolCall(name='data_api', arguments={
        'op': 'call', 'provider': 'defillama', 'action': action, 'args': args, **options,
    }), registry=registry)
    assert not result.is_error, result.text()
    return result.content[0].data


def bundle(pool):
    fence = chr(96) * 3
    example = REFERENCE.read_text().split(fence + 'python', 1)[1].split(fence, 1)[0]
    values = dict(requested_canonical_market=pool['canonical_identity']['market'],
        existing_paper_account='paper_main', discovered_pool_id=pool['pool_id'],
        catalog_project=pool['project'], catalog_chain=pool['chain'], catalog_symbol=pool['symbol'],
        requested_start_utc='2026-09-28T00:00:00Z', requested_end_utc='2026-10-28T00:00:00Z')
    for key, value in values.items():
        example = example.replace('<' + key + '>', value)
    calls = []
    exec(compile(example, str(REFERENCE), 'exec'),
         {'strategy_draft_proposal': lambda **args: calls.append(args)})
    return calls[0]


def test_discovered_and_historical_pool_share_chain_qualified_asset_identity():
    pool, = discover()['rows']
    identity = pool['canonical_identity']
    assert identity == {'market': 'AAVE_V3:ethereum-USDC', 'protocol': 'aave_v3',
        'component_id': 'builtin:aave', 'chain': 'ethereum', 'asset': 'USDC', 'pool_id': PID}
    historical = discover('yield_history', limit=1, columns=['timestamp'])
    assert historical['_envelope']['pool']['canonical_identity'] == identity
    assert historical['_envelope']['pool']['symbol'] == 'USDC'
    assert compact_tool_result('data_api', historical, size_threshold=0).kept[
        '_envelope']['pool']['canonical_identity'] == identity
    assert compact_tool_result('data_api', discover(), size_threshold=0).kept[
        'rows_sample'][0]['canonical_identity'] == identity
    assert scope._market(identity['market']) == {
        'protocol': scope._protocol('aave_v3'), 'chain': 'ethereum', 'asset': 'USDC'}


def test_aave_paper_prompt_does_not_turn_execution_mode_into_native_scope(env):
    admit(env, cid='aave-paper-scope', sid='aave-paper-scope',
          text='创建一个 Aave V3 Ethereum USDC 收益率策略，回测最近30天，做 paper 回测并生成报告，不交易。')
    contract = scope.ensure_contract(env, gateway=FakeGateway(lambda docs: spans_output(
        docs[0], protocol='Aave V3', chain='Ethereum', asset='USDC', window_days='30天')))
    assert contract['constraints']['resources'] == [{
        'protocol': scope._protocol('aave_v3'), 'chain': 'ethereum', 'asset': 'USDC'}]
    assert contract['constraints']['window_days'] == 30


def test_bound_rate_canonical_market_can_be_requoted_by_producer(env):
    original = assistant_chain(
        env,
        '生成 manifest：markets 使用 AAVE_V3:ethereum-USDC，保留原资产和30天窗口。',
        initial_text='创建一个 Aave V3 Ethereum USDC 收益率策略，回测最近30天，不交易。',
        initial_choose=lambda docs: spans_output(
            docs[0], protocol='Aave V3', chain='Ethereum', asset='USDC', window_days='30天'),
    )

    def choose(docs):
        result = deepcopy(original['evidence'])
        latest = docs[-1]
        for key, value in (('protocol', 'AAVE_V3'), ('chain', 'ethereum'), ('asset', 'USDC')):
            result['fields'][key] = spans_output(latest, **{key: value})['fields'][key]
        return result

    contract = scope.ensure_contract(env, gateway=FakeGateway(choose))
    assert contract['constraints'] == original['constraints']
    assert contract['normalized_evidence'] == original['normalized_evidence']


def test_confirmed_defillama_pool_uuid_is_bound_before_rate_draft(monkeypatch, tmp_path):
    from nerya.core.config import Config
    from nerya.core.paths import WorkspacePaths
    from nerya.tools.native import strategy_runtime

    config = Config(WorkspacePaths(tmp_path), {})
    pool = {**POOL, 'pool_id': PID,
            'canonical_identity': {'market': 'AAVE_V3:ethereum-USDC',
                'protocol': 'aave_v3', 'component_id': 'builtin:aave',
                'chain': 'ethereum', 'asset': 'USDC', 'pool_id': PID}}
    monkeypatch.setattr(strategy_runtime, '_rate_task_evidence', lambda _: (
        [pool], ['主池：USDC，无 pool_meta（推荐）']))
    source = {'pool_id': PID, 'project': 'aave-v3', 'chain': 'Ethereum', 'asset': 'USDC'}
    manifest = {'markets': ['AAVE_V3:ethereum-USDC']}
    assert strategy_runtime._rate_pool_binding_error(config, source, manifest) is None
    wrong = {**source, 'pool_id': '6f00d46b-8735-49ae-9ced-2a0fccc56ad0'}
    assert 'does not uniquely bind' in strategy_runtime._rate_pool_binding_error(config, wrong, manifest)


def test_catalog_and_history_pool_evidence_is_deduplicated(monkeypatch, tmp_path):
    from nerya.core.config import Config
    from nerya.core.paths import WorkspacePaths
    from nerya.tools.native import strategy_runtime

    config = Config(WorkspacePaths(tmp_path), {})
    pool = {**POOL, 'pool_id': PID, 'canonical_identity': {
        'market': 'AAVE_V3:ethereum-USDC', 'pool_id': PID}}
    monkeypatch.setattr(strategy_runtime, '_rate_task_evidence', lambda _: ([pool, dict(pool)], ['主池']))
    source = {'pool_id': PID, 'project': 'aave-v3', 'chain': 'Ethereum', 'asset': 'USDC'}
    assert strategy_runtime._rate_pool_binding_error(config, source, {'markets': ['AAVE_V3:ethereum-USDC']}) is None


@pytest.mark.parametrize('symbol', ['WETH', 'USDC.e', 'ETH-USDC', 'USDC/USDT'])
def test_catalog_asset_is_never_aliased_or_split_into_a_pair(symbol):
    pool, = discover(pool={**POOL, 'symbol': symbol})['rows']
    identity = pool['canonical_identity']
    if '-' in symbol or '/' in symbol:
        assert identity is None
    else:
        assert identity['asset'] == symbol
        assert identity['market'] == 'AAVE_V3:ethereum-' + symbol


@pytest.mark.parametrize('field,value', [('chain', 'Unreviewed'), ('project', 'unreviewed')])
def test_unknown_chain_or_protocol_has_no_guessed_market(field, value):
    pool, = discover(pool={**POOL, field: value})['rows']
    assert pool['canonical_identity'] is None


@pytest.mark.parametrize('damage', [None, 'top', 'manifest', 'both', 'asset', 'chain', 'scope'])
def test_real_shape_mapping_rejects_drift_and_correct_bundle_drafts(env, monkeypatch, fake_execution, damage):
    admit(env, cid='rate-mapping', sid='rate-mapping',
          text='帮我创建一个 Aave USDC 收益策略，回测最近30天，不启用交易。',
          created=1793145600)
    fake = FakeGateway(lambda docs: spans_output(docs[0],
        protocol='Aave', asset='USDC', window_days='30天'))
    install_fake(monkeypatch, fake)
    pool, = discover()['rows']
    args = bundle(pool)
    manifest = yaml_io.loads(args['files']['strategy.yml'])
    if damage in {'top', 'both'}:
        args['markets'] = ['AAVE_V3:ETH-USDC']
    if damage in {'manifest', 'both'}:
        manifest['markets'] = ['AAVE_V3:ETH-USDC']
    if damage in {'asset', 'chain'}:
        manifest['params']['rate_source'][damage] = 'WETH' if damage == 'asset' else 'Base'
    if damage == 'scope':
        manifest['params']['rate_source']['asset'] = 'WETH'
        args['markets'] = manifest['markets'] = ['AAVE_V3:ethereum-WETH']
    args['files']['strategy.yml'] = yaml_io.dumps(manifest)
    original = deepcopy(args)
    result = strategy_draft_proposal_handler(ToolCall(name='strategy_draft_proposal', arguments=args), config=env)
    assert args == original
    if damage:
        assert result.is_error
        if damage == 'scope':
            assert result.error.detail['reason'] == 'request_scope_mismatch'
            assert any(row['field'] == 'asset' for row in result.error.detail['differences'])
            assert len(fake.calls) == 1
            assert not env.paths.proposals.exists()
            return
        assert result.error.recovery_hint['field'] == ['markets']
        assert 'canonical_identity.market' in result.error.message
        assert not env.paths.proposals.exists()
        assert not fake.calls, 'bad mapping must fail before scope extraction and staging'
        return
    assert not result.is_error, result.text()
    data = result.content[0].data
    assert data['implementation_status'] == 'rate_policy_simulation'
    assert len(fake.calls) == 1
    contract = scope.current_contract(env)
    assert contract['constraints']['resources'][0]['asset'] == 'USDC'
    assert 'base' not in contract['constraints']['resources'][0]
    staged = scope.target_files(env, {'proposal_id': data['proposal_id']})
    saved = yaml_io.loads(staged['strategy.yml'])
    assert saved['markets'] == [pool['canonical_identity']['market']]
    assert saved['params']['rate_source'] == manifest['params']['rate_source']
    assert saved['schedule']['enabled'] is False
    assert scope.compare(contract, saved) == {'start_utc': '2026-09-28T00:00:00Z',
                                             'end_utc': '2026-10-28T00:00:00Z'}
