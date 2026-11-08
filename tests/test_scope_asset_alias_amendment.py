"""Superseded asset roles remain bound to their original admitted evidence."""
from contextlib import closing
from copy import deepcopy
import json

import pytest

from nerya.core.config import Config
from nerya.db.sqlite import connect, connect_readonly
from nerya.strategies import request_scope as scope
from test_request_scope import FakeGateway, admit, assistant_chain, spans_output
from test_request_scope import env  # noqa: F401


INITIAL = '新建 Uniswap v3 ETH/USDC LP策略，回测最近30天，不启用交易。'
FOLLOWUP = ('本次仅在 LP 池资产绑定中使用 WETH 作为 ETH 的包装资产，报告明确标注；'
            '保留原协议、原区间与策略目标，不执行包装、授权或交易。')


def initial_output(docs):
    return spans_output(docs[0], protocol='Uniswap v3', base='ETH', quote='USDC', window_days='30天')


def amended_output(docs):
    result = initial_output(docs)
    result['fields']['base'] = spans_output(docs[-1], base='WETH')['fields']['base']
    text = docs[-1]['text']
    result['amendments'] = [{'command_id': docs[-1]['command_id'], 'start': 0,
                             'end': len(text), 'text': text}]
    return result


def assert_rejected(config, original, choose, diagnostic=None):
    with pytest.raises(scope.ScopeError) as raised:
        scope.ensure_contract(config, gateway=FakeGateway(choose))
    if diagnostic:
        assert raised.value.diagnostic_code == diagnostic
    assert scope.current_contract(config, command_id='child-create') == original
    with closing(connect_readonly(config.paths.db)) as con:
        context = json.loads(con.execute(
            "SELECT context_json FROM agent_commands WHERE command_id='child-follow'").fetchone()[0])
    assert 'request_scope' not in context


def test_exact_descriptive_asset_amendment_is_durable_and_keeps_original_scope(env):
    original = assistant_chain(env, FOLLOWUP, initial_text=INITIAL, initial_choose=initial_output)
    changed = scope.ensure_contract(env, gateway=FakeGateway(amended_output))
    expected = deepcopy(original['constraints'])
    expected['base'] = expected['resources'][0]['base'] = 'WETH'
    assert changed['constraints'] == expected
    assert changed['revision'] == 2
    assert changed['request_scope_hash'] != original['request_scope_hash']
    assert changed['evidence']['amendments'][0]['text'] == FOLLOWUP
    assert scope.current_contract(env, command_id='child-create') == original
    assert scope.current_contract(Config(env.paths, deepcopy(env.data))) == changed
    cached = FakeGateway()
    assert scope.ensure_contract(env, gateway=cached) == changed
    assert not cached.calls
    candidate = {'markets': ['UNISWAP_V3:WETH/USDC'], 'backtest': {'window_days': 30}}
    assert scope.compare(changed, candidate)
    with pytest.raises(scope.ScopeError, match='request_scope_mismatch'):
        scope.compare(original, candidate)


@pytest.mark.parametrize('asset,wrapped', [('BNB', 'WBNB'), ('SOL', 'WSOL')])
def test_original_asset_role_is_generic_across_provider_aliases(env, asset, wrapped):
    initial = INITIAL.replace('ETH/', asset + '/')
    text = FOLLOWUP.replace('WETH', wrapped).replace('ETH 的', asset + ' 的')
    def choose_initial(docs):
        return spans_output(docs[0], protocol='Uniswap v3', base=asset, quote='USDC', window_days='30天')
    original = assistant_chain(env, text, initial_text=initial, initial_choose=choose_initial)
    def choose(docs):
        result = choose_initial(docs)
        result['fields']['base'] = spans_output(docs[-1], base=wrapped)['fields']['base']
        result['amendments'] = [{'command_id': docs[-1]['command_id'], 'start': 0,
                                 'end': len(text), 'text': text}]
        return result
    changed = scope.ensure_contract(env, gateway=FakeGateway(choose))
    expected = deepcopy(original['constraints'])
    expected['base'] = expected['resources'][0]['base'] = wrapped
    assert changed['constraints'] == expected
    assert scope.current_contract(env) == changed


def test_later_continuation_revalidates_the_asset_amendment_chain(env):
    assistant_chain(env, FOLLOWUP, initial_text=INITIAL, initial_choose=initial_output)
    changed = scope.ensure_contract(env, gateway=FakeGateway(amended_output))
    with closing(connect(env.paths.db)) as con, con:
        con.execute("UPDATE agent_commands SET state='succeeded' WHERE command_id='child-follow'")
    admit(env, cid='child-next', sid='child', text='继续原任务。', created=1792900700,
          position=3, source='user_chat')
    continued = scope.ensure_contract(env, gateway=FakeGateway(lambda docs: deepcopy(changed['evidence'])))
    assert continued['constraints'] == changed['constraints']
    assert continued['revision'] == 2
    assert scope.current_contract(env) == continued


@pytest.mark.parametrize('name', ['Binance', 'Aave'])
def test_asset_reference_does_not_cover_omitted_new_venue_or_protocol(env, name):
    text = FOLLOWUP + f' 改用 {name}。'
    original = assistant_chain(env, text, initial_text=INITIAL, initial_choose=initial_output)
    assert_rejected(env, original, amended_output, 'incomplete_scope_evidence')


def test_explicit_new_venue_still_requires_and_accepts_its_own_span(env):
    text = FOLLOWUP + ' 改用 Binance。'
    original = assistant_chain(env, text, initial_text=INITIAL, initial_choose=initial_output)
    def choose(docs):
        result = amended_output(docs)
        result['fields']['venue'] = spans_output(docs[-1], venue='Binance')['fields']['venue']
        return result
    changed = scope.ensure_contract(env, gateway=FakeGateway(choose))
    assert changed['constraints']['venue'] == 'binance'
    assert changed['constraints']['base'] == 'WETH'
    assert scope.current_contract(env, command_id='child-create') == original


@pytest.mark.parametrize('damage', ['missing', 'wrong_command', 'tampered_text', 'outside_asset', 'outside_reference'])
def test_asset_reference_requires_exact_bound_amendment(env, damage):
    original = assistant_chain(env, FOLLOWUP, initial_text=INITIAL, initial_choose=initial_output)
    def choose(docs):
        result = amended_output(docs)
        amendment = result['amendments'][0]
        if damage == 'missing':
            result['amendments'] = []
        elif damage == 'wrong_command':
            amendment['command_id'] = docs[0]['command_id']
        elif damage == 'tampered_text':
            amendment['text'] += '伪造'
        else:
            start = FOLLOWUP.index('ETH 的') if damage == 'outside_asset' else 0
            end = len(FOLLOWUP) if damage == 'outside_asset' else FOLLOWUP.index('ETH 的')
            amendment.update(start=start, end=end, text=FOLLOWUP[start:end])
        return result
    assert_rejected(env, original, choose)


def test_described_name_without_original_asset_role_is_not_exempt(env):
    initial = INITIAL.replace('ETH/', 'BTC/')
    def choose_initial(docs):
        return spans_output(docs[0], protocol='Uniswap v3', base='BTC', quote='USDC', window_days='30天')
    original = assistant_chain(env, FOLLOWUP, initial_text=initial, initial_choose=choose_initial)
    def choose(docs):
        result = choose_initial(docs)
        result['fields']['base'] = spans_output(docs[-1], base='WETH')['fields']['base']
        result['amendments'] = [{'command_id': docs[-1]['command_id'], 'start': 0,
                                 'end': len(FOLLOWUP), 'text': FOLLOWUP}]
        return result
    # Keep the original extraction valid while changing only the followup base.
    assert_rejected(env, original, choose, 'incomplete_scope_evidence')


def test_tampered_original_asset_evidence_cannot_authorize_followup(env):
    assistant_chain(env, FOLLOWUP, initial_text=INITIAL, initial_choose=initial_output)
    with closing(connect(env.paths.db)) as con, con:
        context = json.loads(con.execute(
            "SELECT context_json FROM agent_commands WHERE command_id='child-create'").fetchone()[0])
        context['request_scope']['extractions'][0]['fields']['base']['text'] = 'Binance'
        con.execute("UPDATE agent_commands SET context_json=? WHERE command_id='child-create'",
                    (json.dumps(context),))
    gateway = FakeGateway(amended_output)
    with pytest.raises(scope.ScopeError):
        scope.ensure_contract(env, gateway=gateway)
    assert not gateway.calls
