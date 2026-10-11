from copy import deepcopy
from dataclasses import replace
import time

import pytest

from nerya.financial.contracts import FinancialContext, FinancialError
from nerya.financial.gateway import FinancialGateway
from nerya.financial.rebalance import RebalanceService
from test_uniswap_execution import setup as lp_setup, ASSET, TOKEN1, NFT_ID

pytestmark = pytest.mark.smoke


class Transport:
    def __init__(self):
        self.sent = []
        self.settled = set()
        self.credit = '10000000'

    def quote(self, request):
        spending = {ASSET: '10', TOKEN1: '10'} if request['kind'] == 'lp_add' else {}
        if request['kind'] == 'lp_add':
            spending = {token: request['parameters'][f'amount{index}_max'] for index, token in enumerate((ASSET, TOKEN1))}
        elif request['kind'] == 'swap':
            spending = {request['asset']: request['amount']}
        return {'risk_usd': '20', 'spend_usd': '20' if spending else '0', 'fee_usd': '.1',
                'asset_amounts': spending, 'available_asset_amounts': {ASSET:'100', TOKEN1:'100'},
                'expires_at': time.time() + 30,
                'pool': {'token0': ASSET, 'token1': TOKEN1, 'decimals': [6, 6]}}

    def validate(self, request, quote):
        pass

    def execute(self, request, quote, submitted):
        self.sent.append(request['kind'])
        reference = {'transaction_hash': 'tx-' + request['kind']}
        submitted(reference)
        return {'state': 'submitted', 'submission': reference}

    def status(self, request, quote, submission):
        if request['kind'] not in self.settled:
            return {'state': 'confirming'}
        if request['kind'] == 'swap':
            return {'state': 'confirmed', 'settled_fee_usd': '.1', 'settled_notional_usd': '1',
                    'result': {'result': {'amount_in': request['amount'], 'amount_out': '1',
                        'extra': {'confirmed': True, 'amount_out_source': 'receipt'}}}}
        return {'state': 'confirmed', 'settled_fee_usd': '.1', 'settled_notional_usd':'20',
                'position_id': '124' if request['kind'] == 'lp_add' else str(NFT_ID),
                'cashflows': [{'debit_base': '0', 'credit_base': self.credit}] * 2}


def setup(tmp_path, monkeypatch):
    cfg, _, _, _ = lp_setup(tmp_path, monkeypatch)
    context = FinancialContext('operator', frozenset({'api:all'}), task_kind='scheduled_agent',
                               task_id='lp-cycle', security_revision='reviewed-plan')
    transport = Transport()
    gateway = FinancialGateway(cfg, adapters={'lp_add': transport, 'lp_remove': transport})
    policy = {'actions':['lp_add','lp_remove'],
              'resources': {'wallets':['wallet'], 'assets':[ASSET,TOKEN1], 'chains':['base'],
                            'components':['builtin:uniswap'], 'protocols':['uniswap_v3'], 'positions':[str(NFT_ID)]},
              'limits': {'single_usd':'100', 'rolling_24h_usd':'1000', 'total_usd':'1000', 'fee_usd':'1',
                         'asset_amounts':{ASSET:'100',TOKEN1:'100'}}}
    grant = gateway.store.create_grant(context, task_kind=context.task_kind, task_id=context.task_id,
        security_revision=context.security_revision, policy=policy)
    gateway.store.approve_grant(grant['grant_id'], context, expected_revision=grant['revision'])
    service = RebalanceService(cfg, gateway=gateway)
    return cfg, service, context, transport


def plan():
    common = {'wallet_id':'wallet', 'chain':'base', 'protocol':'uniswap_v3'}
    return {'expires_at':time.time()+600, 'max_total_fee_usd':'1',
            'exit': {**common, 'kind':'lp_remove', 'position_id':str(NFT_ID),
                     'parameters':{'pool_id':'pair','token_id':NFT_ID,'liquidity':2000000000,
                                   'amount0_min':'9.9','amount1_min':'9.9','collect0_max':'10','collect1_max':'10'}},
            'entry': {**common, 'kind':'lp_add', 'parameters':{'pool_id':'pair','tick_lower':-200,'tick_upper':200,
                       'amount0_max':'10','amount1_max':'10','amount0_min':'9.9','amount1_min':'9.9','minimum_liquidity':1}}}


def advance(service, context, row):
    return service.advance(context, row['rebalance_id'], expected_revision=row['revision'])


def test_remove_confirm_then_add_never_reposts_across_service_restart(tmp_path, monkeypatch):
    cfg, service, ctx, transport = setup(tmp_path, monkeypatch)
    row = service.create(ctx, plan(), client_key='range-one')
    assert transport.sent == []
    row = advance(service, ctx, row)
    assert transport.sent == ['lp_remove'] and row['cursor'] == 0
    service = RebalanceService(cfg, gateway=service.gateway)
    row = advance(service, ctx, row)
    assert transport.sent == ['lp_remove']
    transport.settled.add('lp_remove')
    row = advance(service, ctx, row)
    assert row['cursor'] == 1
    row = advance(service, ctx, row)
    assert transport.sent == ['lp_remove', 'lp_add']
    transport.settled.add('lp_add')
    row = advance(service, ctx, row)
    assert row['state'] == 'completed'
    assert advance(service, ctx, row)['state'] == 'completed' and len(transport.sent) == 2


def test_one_owned_nft_cannot_have_two_active_rebalances(tmp_path, monkeypatch):
    _, service, ctx, _ = setup(tmp_path, monkeypatch)
    payload = plan()
    row = service.create(ctx, payload, client_key='same')
    assert service.create(ctx, payload, client_key='same')['rebalance_id'] == row['rebalance_id']
    with pytest.raises(FinancialError, match='already_rebalancing'):
        service.create(ctx, payload, client_key='different')
    changed = deepcopy(payload)
    changed['entry']['parameters']['tick_lower'] = -300
    with pytest.raises(FinancialError, match='idempotency_conflict'):
        service.create(ctx, changed, client_key='same')


def test_entry_cannot_spend_other_strategy_wallet_funds(tmp_path, monkeypatch):
    _, service, ctx, transport = setup(tmp_path, monkeypatch)
    row = advance(service, ctx, service.create(ctx, plan(), client_key='insufficient'))
    transport.credit = '9000000'
    transport.settled.add('lp_remove')
    row = advance(service, ctx, row)
    row = advance(service, ctx, row)
    assert row['state'] == 'needs_recovery' and row['reason'] == 'rebalance_proceeds_below_entry_bounds'
    assert transport.sent == ['lp_remove']


def test_fee_budget_and_uncertain_actions_cannot_be_bypassed(tmp_path, monkeypatch):
    _, service, ctx, transport = setup(tmp_path, monkeypatch)
    payload = plan()
    payload['max_total_fee_usd'] = '.05'
    row = advance(service, ctx, service.create(ctx, payload, client_key='fee'))
    assert row['state'] == 'needs_recovery' and transport.sent == []
    row = advance(service, ctx, row)
    assert transport.sent == []


def test_readonly_foreign_and_changed_strategy_cannot_advance(tmp_path, monkeypatch):
    _, service, ctx, transport = setup(tmp_path, monkeypatch)
    row = service.create(ctx, plan(), client_key='identity')
    for candidate in (replace(ctx, actor_id='other'), replace(ctx, security_revision='changed'),
                      replace(ctx, plan_only=True), replace(ctx, scopes=frozenset({'read:funds'}))):
        with pytest.raises(FinancialError):
            advance(service, candidate, row)
    assert transport.sent == []


def test_unconfirmed_child_cannot_be_abandoned_to_restart_same_position(tmp_path, monkeypatch):
    _, service, ctx, _ = setup(tmp_path, monkeypatch)
    row = advance(service, ctx, service.create(ctx, plan(), client_key='pending'))
    with pytest.raises(FinancialError, match='resolve_or_discard'):
        service.stop(ctx, row['rebalance_id'], expected_revision=row['revision'])


@pytest.mark.parametrize('mutation', ['wallet', 'protocol', 'nft', 'recipient'])
def test_invalid_rebalance_plan_rejected_before_any_side_effect(tmp_path, monkeypatch, mutation):
    _, service, ctx, transport = setup(tmp_path, monkeypatch)
    payload = plan()
    if mutation == 'wallet': payload['entry']['wallet_id'] = 'foreign'
    elif mutation == 'protocol': payload['entry']['protocol'] = 'byreal'
    elif mutation == 'nft': payload['entry']['parameters']['token_id'] = NFT_ID
    else: payload['swap'] = {'kind':'swap','wallet_id':'wallet','chain':'base','asset':ASSET,'to_asset':TOKEN1,
                             'amount':'1','recipient':'foreign'}
    with pytest.raises(FinancialError):
        service.create(ctx, payload, client_key='invalid')
    assert transport.sent == []


def test_exit_ratio_swap_entry_reconciles_three_independent_transactions(tmp_path, monkeypatch):
    cfg, service, ctx, transport = setup(tmp_path, monkeypatch)
    cfg.data['financial']['wallet_permissions']['wallet']['swap'] = True
    service.gateway.adapters['swap'] = transport
    policy = {'actions': ['swap'],
        'resources': {'wallets': ['wallet'], 'assets': [ASSET, TOKEN1], 'chains': ['base'],
                      'components': ['builtin:trading']},
        'limits': {'single_usd': '100', 'rolling_24h_usd': '100', 'total_usd': '100',
                   'fee_usd': '1', 'slippage_bps': 50, 'asset_amounts': {ASSET: '100', TOKEN1: '100'}}}
    grant = service.gateway.store.create_grant(ctx, task_kind=ctx.task_kind, task_id=ctx.task_id,
        security_revision=ctx.security_revision, policy=policy)
    service.gateway.store.approve_grant(grant['grant_id'], ctx, expected_revision=grant['revision'])
    payload = plan()
    payload['swap'] = {'kind': 'swap', 'wallet_id': 'wallet', 'chain': 'base', 'asset': ASSET,
                       'to_asset': TOKEN1, 'amount': '1', 'slippage_bps': 50}
    payload['entry']['parameters'].update(amount0_max='9', amount0_min='8.9', amount1_max='11', amount1_min='10.9')
    row = service.create(ctx, payload, client_key='ratio-cycle')
    for index, kind in enumerate(('lp_remove', 'swap', 'lp_add')):
        row = advance(service, ctx, row)
        assert transport.sent == ['lp_remove', 'swap', 'lp_add'][:index+1]
        assert row['cursor'] == index
        # Polling and a new coordinator instance must not send the same step.
        service = RebalanceService(cfg, gateway=service.gateway)
        row = advance(service, ctx, row)
        assert len(transport.sent) == index+1
        transport.settled.add(kind)
        row = advance(service, ctx, row)
        assert row['cursor'] == index+1
    assert row['state'] == 'completed' and len(set(row['action_ids'])) == 3
