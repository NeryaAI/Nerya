"""Offline LP authoring contracts. Synthetic fixtures are not historical evidence."""
from pathlib import Path
from copy import deepcopy
from dataclasses import asdict, replace
from datetime import datetime, timezone
import json
import socket
import sys
import time
from types import ModuleType, SimpleNamespace

import pytest

from nerya.core import yaml_io
from nerya.core.paths import WorkspacePaths
from nerya.evolution.strategy_code_generator import StrategyCodeGenerator, StrategyGenerationRequest
from nerya.financial.gateway import FinancialGateway
from nerya.financial.store import FinancialStore
from nerya.sdk.financial_api import FinancialAPI
from nerya.strategies import ResultBuilder, StrategyResult
from nerya.strategies.context import StrategyClock, StrategyState
from nerya.strategies.input_context import StrategyInputContext
from nerya.trading.components import trading_components
from nerya.workspace.state_store import StateStore
import test_uniswap_execution as lp
import test_lp_rebalance as rebalance

pytestmark = pytest.mark.smoke
TEMPLATES = Path(__file__).parents[1] / 'nerya/skills/builtin/strategy_author/templates'


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    def denied(*args, **kwargs):
        raise AssertionError('NETWORK_DISABLED')
    monkeypatch.setattr(socket.socket, 'connect', denied)
    monkeypatch.setattr(socket, 'create_connection', denied)


@pytest.fixture
def assets(tmp_path, monkeypatch):
    generated = generate(tmp_path, {'lp_policy': {}, 'lp_source': {}, 'lp_execution': {}}).files
    modules = {}
    for name in ('risk_rules', 'lp_range_policy', 'lp_rebalance', 'lp_range_strategy'):
        module = ModuleType(name)
        monkeypatch.setitem(sys.modules, name, module)
        path = 'main.py' if name == 'lp_range_strategy' else name + '.py'
        exec(compile(generated[path], path, 'exec'), module.__dict__)
        modules[name] = module
    return SimpleNamespace(run=modules['lp_range_strategy'].run, policy=modules['lp_range_policy'])


def fixture_context(tmp_path, *, token=lp.NFT_ID):
    now = int(time.time())
    pool = dict(pool_id='pair', tick=0, token0=lp.ASSET, token1=lp.TOKEN1,
                fee=500, tick_spacing=10, decimals=[6, 6])
    position = dict(token_id=token, owner=lp.SENDER, pool=pool, tick_lower=-100, tick_upper=100,
                    liquidity='2000000000', principal_value_usd='20', owed_value_usd='0',
                    amounts_base=['10000000', '10000000'], tokens_owed_base=['0', '0'])
    binding = dict(id='builtin:uniswap', revision='fixture-only', abi_version=1)
    common = dict(health='ok', source='synthetic-offline-fixture', as_of=now)
    account = dict(**common, component_binding=binding,
                   coverage=['lp_nft_inventory', 'liquidity', 'ranges', 'principal'],
                   fees_coverage='fee_growth_inside', positions=[position] if token is not None else [])
    snapshot = dict(**common, schema='lp.snapshot.v1', wallet_id='wallet', chain='base',
                    protocol='uniswap_v3', pool_id='pair', account=account, pool=pool,
                    economics=dict(**common, pool_id='pair', currency='USD',
                                   expected_incremental_fees_usd='2', total_execution_cost_usd='.1'))
    request = {**lp.request(), 'component_id': 'builtin:uniswap'}
    source = dict(source_id='lp_fixture', wallet_id='wallet', chain='base', protocol='uniswap_v3',
                  pool_id='pair', token_id=token, component_binding=binding, token0=lp.ASSET,
                  token1=lp.TOKEN1, fee=500, tick_spacing=10, decimals=[6, 6], account_request=request)
    removal = {**request, 'kind':'lp_remove', 'position_id':str(lp.NFT_ID),
               'parameters': {'pool_id':'pair', 'token_id':lp.NFT_ID, 'liquidity':2000000000,
                              'amount0_min':'9.9', 'amount1_min':'9.9',
                              'collect0_max':'10', 'collect1_max':'10'}}
    params = dict(lp_source=source, lp_policy=dict(max_age_seconds=60, persistence_seconds=10,
                  cooldown_seconds=100, cost_multiple=2, enter_when_empty=True,
                  exit_below_principal_usd=5, collect_min_fees_usd=1),
                  lp_execution=dict(operation_key='reviewed-cycle-1',
                      requests={'lp_add':request, 'lp_remove':removal,
                                'lp_collect':{**removal, 'kind':'lp_collect'}},
                      rebalance_plan={**rebalance.plan(), 'exit':removal, 'entry':request}))
    clock = StrategyClock()
    clock.freeze(iso=datetime.fromtimestamp(now, timezone.utc).isoformat())
    inputs = StrategyInputContext(sources=[], market=None, news=None, markets=(), run_id='fixture', clock=clock)
    ctx = SimpleNamespace(mode='paper', result=ResultBuilder(), config=SimpleNamespace(params=params),
        clock=clock, inputs=inputs, state=StrategyState(StateStore(tmp_path/'state.json')),
        snapshot=snapshot, position=position, pool=pool, financial=None)
    publish(ctx)
    return ctx


def publish(ctx):
    snapshot = deepcopy(ctx.snapshot)
    for pool in [snapshot['pool'], *[p['pool'] for p in snapshot['account']['positions']]]:
        pool['underlying_tokens'] = [pool['token0'], pool['token1']]
    binding = snapshot['account']['component_binding']
    if isinstance(binding.get('revision'), str) and len(binding['revision']) == 64:
        binding['revision'] = {'sha256':binding['revision']}
    ctx.inputs.publish('source:lp_fixture', snapshot)


def move_clock(ctx, seconds):
    timestamp = ctx.clock.now().timestamp() + seconds
    ctx.clock.freeze(iso=datetime.fromtimestamp(timestamp, timezone.utc).isoformat())
    for value in (ctx.snapshot, ctx.snapshot['account'], ctx.snapshot['economics']):
        value['as_of'] = timestamp
    publish(ctx)


def generate(tmp_path, params=None, **overrides):
    request = dict(strategy_id='lp_fixture', markets=('UNISWAP_V3:ETH/USDC',),
                   accounts=('fixture_paper',), create_tuning=False,
                   files={'strategy.yml': yaml_io.dumps({'params': params})} if params else {})
    request.update(overrides)
    return StrategyCodeGenerator(WorkspacePaths(tmp_path)).generate(
        StrategyGenerationRequest(**request), validate=False, create_proposal_record=False)


def test_explicit_lp_contract_selects_executable_assets(tmp_path):
    result = generate(tmp_path, {'lp_policy': {}, 'lp_source': {}, 'lp_execution': {}})
    assert {'lp_range_policy.py', 'lp_rebalance.py', 'risk_rules.py'} <= result.files.keys()
    assert 'ctx.trading.' not in result.files['main.py']
    assert 'def run(ctx)' in result.files['main.py']
    templates = Path(__file__).parents[1] / 'nerya/skills/builtin/strategy_author/templates'
    assert result.files['main.py'] == (templates / 'lp_range_strategy.py.j2').read_text()
    assert 'LP contract behavior' in result.files['tests/test_contract.py']


def test_prose_alone_does_not_select_lp_assets(tmp_path):
    result = generate(tmp_path, prompt='Uniswap LP rebalance', title='LP range')
    assert 'lp_range_policy.py' not in result.files


def test_explicit_lp_test_and_entrypoint_overrides_remain_authoritative(tmp_path):
    params = {'lp_policy': {}, 'lp_source': {}, 'lp_execution': {}}
    files = {'strategy.yml': yaml_io.dumps({'params': params}),
             'tests/test_contract.py': 'CUSTOM_TEST = True'}
    result = generate(tmp_path, files=files)
    assert result.files['tests/test_contract.py'] == files['tests/test_contract.py']
    files['main.py'] = 'def run(ctx): return ctx.result.hold(reason="authored")'
    result = generate(tmp_path/'authored', files=files)
    assert 'lp_range_policy.py' not in result.files
    assert 'authored' in result.files['main.py']


@pytest.mark.parametrize('mode', ['paper', 'shadow', 'backtest'])
def test_entry_hold_exit_signals_never_call_funds_in_simulation(tmp_path, assets, mode):
    ctx = fixture_context(tmp_path, token=None)
    ctx.mode = mode
    result = assets.run(ctx)
    assert isinstance(result, StrategyResult) and result.status.value == 'hold'
    assert result.metadata['lp_decision']['action'] == 'lp_add'
    assert result.metadata['funds_sent'] is False
    ctx = fixture_context(tmp_path/'owned')
    assert assets.run(ctx).metadata['lp_decision']['reason'] == 'inside_range'
    ctx.position['principal_value_usd'] = '4'
    publish(ctx)
    assert assets.run(ctx).metadata['lp_decision']['action'] == 'lp_remove'
    ctx.position.update(principal_value_usd='20', owed_value_usd='2')
    publish(ctx)
    assert assets.run(ctx).metadata['lp_decision']['action'] == 'lp_collect'
    assert ctx.state.get('lp_operation') is None


def test_actual_pool_tick_persistence_cooldown_and_economics(tmp_path, assets):
    ctx = fixture_context(tmp_path)
    ctx.pool['tick'] = 100  # upper bound is exclusive
    publish(ctx)
    assert assets.run(ctx).metadata['lp_decision']['reason'] == 'range_exit_first_observed'
    move_clock(ctx, 9)
    assert assets.run(ctx).metadata['lp_decision']['reason'] == 'range_exit_not_persistent'
    move_clock(ctx, 1)
    assert assets.run(ctx).metadata['lp_decision']['action'] == 'lp_rebalance'
    timing = ctx.state.get('lp_policy_state:paper')
    ctx.state.set('lp_policy_state:paper', {**timing, 'last_rebalance':ctx.clock.now().timestamp()})
    assert assets.run(ctx).metadata['lp_decision']['reason'] == 'rebalance_cooldown'
    move_clock(ctx, 100)
    ctx.snapshot['economics']['expected_incremental_fees_usd'] = None
    publish(ctx)
    assert assets.run(ctx).metadata['lp_decision']['reason'] == 'fee_economics_unknown'
    ctx.snapshot['economics']['expected_incremental_fees_usd'] = '.2'
    publish(ctx)
    assert assets.run(ctx).metadata['lp_decision']['reason'] == 'fees_do_not_cover_execution'
    ctx.pool['tick'] = 0
    publish(ctx)
    assert assets.run(ctx).metadata['lp_decision']['reason'] == 'inside_range'
    assert ctx.state.get('lp_policy_state:paper')['outside_since'] is None


@pytest.mark.parametrize('mutation', ['stale', 'future', 'missing_fees', 'foreign_pool',
    'orientation', 'no_inventory', 'missing_nft', 'nan_gas', 'boolean_principal', 'bad_account_request'])
def test_attested_input_failures_close_execution(tmp_path, assets, mutation):
    ctx = fixture_context(tmp_path)
    if mutation == 'stale': ctx.snapshot['as_of'] -= 61
    elif mutation == 'future': ctx.snapshot['economics']['as_of'] += 1
    elif mutation == 'missing_fees': ctx.position.pop('owed_value_usd')
    elif mutation == 'foreign_pool': ctx.pool['pool_id'] = 'foreign'
    elif mutation == 'orientation': ctx.pool['token0'] = lp.TOKEN1
    elif mutation == 'no_inventory': ctx.snapshot['account']['coverage'] = []
    elif mutation == 'missing_nft': ctx.snapshot['account']['positions'] = []
    elif mutation == 'nan_gas': ctx.snapshot['economics']['total_execution_cost_usd'] = 'NaN'
    elif mutation == 'boolean_principal': ctx.position['principal_value_usd'] = True
    else: ctx.config.params['lp_source']['account_request']['wallet_id'] = 'foreign'
    publish(ctx)
    result = assets.run(ctx)
    assert result.status.value == 'error' and result.metadata['financial_execution_allowed'] is False
    assert ctx.state.get('lp_operation') is None
    json.dumps(result.asdict(), allow_nan=False)


def real_context(tmp_path, monkeypatch, *, token=None, bind_validation=True):
    from nerya.financial.strategy_runtime import bind_run
    cfg, rpc, _, _ = lp.setup(tmp_path, monkeypatch)
    cfg.data['financial']['account_limits'] = {'wallet:wallet': {'single_usd':'100','rolling_24h_usd':'1000'}}
    yaml_io.dump(cfg.paths.accounts_file, {'accounts':[{'id':'fixture-account','kind':'chain','wallet_id':'wallet'}]})
    yaml_io.dump(cfg.paths.strategy('lp_fixture')/'strategy.yml',
                 {'id':'lp_fixture','accounts':['fixture-account'],'markets':['UNISWAP_V3:ETH/USDC']})
    cfg, identity = bind_run(cfg, strategy_id='lp_fixture', run_id='fixture-run', mode='live')
    if bind_validation:
        # Explicit fixture precondition: execute.validate currently needs this
        # binding outside its quote call. The production gap is tested below;
        # no gateway/SDK method is replaced by this fixture.
        cfg.data.setdefault('runtime', {})['financial_context'] = {
            **asdict(identity), 'scopes':sorted(identity.scopes)}
    ctx = fixture_context(tmp_path, token=token)
    ctx.mode = 'live'
    ctx.clock = StrategyClock()
    ctx.financial = FinancialAPI(cfg, context_provider=lambda write:ctx.identity)
    ctx.identity, ctx.runtime_config, ctx.rpc = identity, cfg, rpc
    ctx.config.params['lp_source']['component_binding'] = trading_components(cfg).resolve(lp.request()).binding()
    actual_pool = FinancialGateway(cfg).adapter(lp.request())._pool('pair')
    ctx.config.params['lp_source']['decimals'] = actual_pool['decimals']
    ctx.snapshot['pool'] = actual_pool
    publish(ctx)
    return ctx


def test_real_sdk_initial_entry_approval_and_original_action_recovery(tmp_path, monkeypatch, assets):
    ctx = real_context(tmp_path, monkeypatch)
    result = assets.run(ctx)
    assert result.status.value == 'hold', result.asdict()
    action = result.metadata['financial_action']
    assert action['status'] == 'approval_required' and ctx.rpc.sends == 0
    action = ctx.financial.get(action['action_id'])
    assert action['state'] == 'awaiting_approval'
    assert action['request'] == ctx.config.params['lp_execution']['requests']['lp_add']
    assert ctx.state.get('lp_operation')['action_id'] == action['action_id']
    # A new producer, broken inputs and changed params cannot restart the mint.
    ctx.identity = replace(ctx.identity, strategy_run_id='next-tick')
    ctx.config.params = {}
    ctx.inputs = None
    again = assets.run(ctx)
    assert again.metadata['financial_action']['action_id'] == action['action_id']
    assert len(FinancialStore(ctx.runtime_config).list_actions(ctx.identity)) == 1
    assert ctx.rpc.sends == 0


def seed_owned(ctx):
    gateway = FinancialGateway(ctx.runtime_config)
    action = ctx.financial.prepare(lp.request(), client_request_id='offline-seed')
    quote = gateway.store.get_action(action['action_id'], ctx.identity, internal=True)['quote']
    ctx.rpc.minted = True
    ctx.rpc.liquidity = 2000000000
    ctx.rpc.receipt = lp.receipt(quote)
    ctx.rpc.transaction = dict(hash=lp.TX_HASH, **{'from':lp.SENDER}, to=lp.MANAGER,
                               input=quote['transaction']['data'], value='0x0')
    gateway.store.mark(action['action_id'], state='submitted',
                       submission={'transaction_hash':lp.TX_HASH,'action_id':action['action_id']})
    assert ctx.financial.reconcile(action['action_id'])['state'] == 'confirmed'


def test_real_sdk_owned_exit_and_unowned_exit_blocked(tmp_path, monkeypatch, assets):
    ctx = real_context(tmp_path, monkeypatch, token=lp.NFT_ID)
    # Wallet observation alone does not grant an exit.
    ctx.rpc.minted, ctx.rpc.liquidity = True, 2000000000
    ctx.runtime_config.data['financial']['defi']['deployments']['base']['uniswap_v3']['start_block'] = 0
    # Inventory reconciliation uses logs; seed the observed wallet position in
    # the protocol inventory table without a strategy claim.
    with FinancialStore(ctx.runtime_config).transaction() as con:
        con.execute('INSERT INTO protocol_positions VALUES (?,?,?,?,?,?,?,?,?,?)',
                    ('fixture-position', 'wallet', 'base', 'uniswap_v3', 'lp', lp.SENDER,
                     json.dumps({'token_id':lp.NFT_ID}), 100, lp.BLOCK_HASH, time.time()))
    ctx.config.params['lp_policy']['exit_below_principal_usd'] = 1000
    blocked = assets.run(ctx)
    assert blocked.status.value == 'error' and ctx.rpc.sends == 0
    assert len(FinancialStore(ctx.runtime_config).list_actions(ctx.identity)) == 0
    # Independently seed a receipt-backed claim, then use a new reviewed cycle.
    ctx.state.set('lp_operation', None)
    seed_owned(ctx)
    result = assets.run(ctx)
    assert result.status.value == 'hold', result.asdict()
    action = ctx.financial.get(result.metadata['financial_action']['action_id'])
    assert action['request']['kind'] == 'lp_remove'
    assert action['state'] == 'awaiting_approval'
    assert ctx.rpc.sends == 0


def test_unbound_execute_validation_fails_closed_preserving_real_prepared_action(tmp_path, monkeypatch, assets):
    ctx = real_context(tmp_path, monkeypatch, bind_validation=False)
    result = assets.run(ctx)
    assert result.status.value == 'hold', result.asdict()
    assert result.metadata['financial_action']['status'] == 'approval_required'
    saved = ctx.state.get('lp_operation')
    action = ctx.financial.get(saved['action_id'])
    assert action['state'] == 'awaiting_approval' and ctx.rpc.sends == 0
    assert 'financial_context' not in ctx.runtime_config.data['runtime']
    # Recovery observes the original action awaiting approval without trying to execute
    # with a later producer's authority or requiring the new input/config.
    ctx.config.params = {}
    again = assets.run(ctx)
    assert again.metadata['financial_action']['action_id'] == saved['action_id']
    assert len(FinancialStore(ctx.runtime_config).list_actions(ctx.identity)) == 1


def test_partial_rebalance_recovers_real_coordinator_before_new_inputs(tmp_path, monkeypatch, assets):
    from nerya.financial.rebalance import RebalanceService
    import nerya.financial.rebalance as coordinator
    cfg, service, identity, transport = rebalance.setup(tmp_path, monkeypatch)
    payload = rebalance.plan()
    ctx = fixture_context(tmp_path)
    ctx.mode = 'live'
    ctx.financial = FinancialAPI(cfg, context_provider=lambda write:identity)
    # Inject only the existing deterministic transport fixture. The SDK and
    # coordinator methods, persistence, proceeds and revision checks are real.
    monkeypatch.setattr(coordinator, 'RebalanceService', lambda config: service)
    saved = dict(key='lp:partial-cycle', cycle='partial-cycle', kind='lp_rebalance', payload=payload)
    ctx.state.set('lp_operation', saved)
    ctx.config.params = {}
    ctx.inputs = None
    first = assets.run(ctx)
    assert first.metadata['state'] == 'confirming' and transport.sent == ['lp_remove']
    rid = first.metadata['rebalance_id']
    frozen = ctx.state.get('lp_operation')
    # Restart the coordinator, keeping its original stored plan and key.
    service = RebalanceService(cfg, gateway=service.gateway)
    again = assets.run(ctx)
    assert again.metadata['rebalance_id'] == rid and transport.sent == ['lp_remove']
    transport.credit = '9000000'
    transport.settled.add('lp_remove')
    assert assets.run(ctx).metadata['step'] == 1
    blocked = assets.run(ctx)
    assert blocked.metadata['state'] == 'needs_recovery'
    assert blocked.metadata['reason'] == 'rebalance_proceeds_below_entry_bounds'
    assert blocked.metadata['actions'][0]['receipt']['cashflows'][0]['credit_base'] == '9000000'
    assert transport.sent == ['lp_remove']
    assert ctx.state.get('lp_operation') == frozen


def test_live_missing_resource_and_prerequisite_close_without_funds(tmp_path, monkeypatch, assets):
    ctx = real_context(tmp_path, monkeypatch)
    ctx.rpc.allowance = 0
    result = assets.run(ctx)
    assert result.metadata['financial_action']['status'] == 'prerequisite_required', result.asdict()
    original = ctx.state.get('lp_operation')['action_id']
    assert ctx.financial.get(original)['state'] == 'awaiting_prerequisite' and ctx.rpc.sends == 0
    assert assets.run(ctx).metadata['financial_action']['action_id'] == original
    # A fresh strategy with a foreign resource cannot even read the account.
    ctx = real_context(tmp_path/'foreign', monkeypatch)
    ctx.config.params['lp_source']['wallet_id'] = 'foreign'
    ctx.config.params['lp_source']['account_request']['wallet_id'] = 'foreign'
    blocked = assets.run(ctx)
    assert blocked.status.value == 'error' and ctx.state.get('lp_operation') is None


def test_generated_saved_package_passes_static_contract(tmp_path):
    from nerya.strategies.validator import validate_proposal_files
    params = fixture_context(tmp_path).config.params
    result = generate(tmp_path, params)
    report = validate_proposal_files(strategy_id='lp_fixture', files=result.files, smoke_test=False)
    assert report.ok, report.asdict()


def test_actual_run_new_rebalance_uses_coordinator_and_fences_changed_producer(tmp_path, monkeypatch, assets):
    ctx = real_context(tmp_path, monkeypatch, token=lp.NFT_ID)
    seed_owned(ctx)
    # A real adapter observes the pool tick; only its deterministic RPC
    # boundary changes. No candle price is consulted by the strategy.
    original_rpc = ctx.rpc._rpc
    def outside(method, args):
        from eth_abi import encode
        from eth_utils import keccak
        if method == 'eth_call' and args[0]['data'][:10] == '0x' + keccak(text='slot0()')[:4].hex():
            return '0x' + encode(['uint160','int24','uint16','uint16','uint16','uint8','bool'],
                                  [2**96,100,0,0,0,0,True]).hex()
        return original_rpc(method, args)
    ctx.rpc._rpc = outside
    # First tick records the observed range exit.
    assert assets.run(ctx).metadata['lp_decision']['reason'] == 'range_exit_first_observed'
    timing = ctx.state.get('lp_policy_state:live')
    ctx.state.set('lp_policy_state:live', {**timing, 'outside_since':ctx.clock.now().timestamp()-10})
    first = assets.run(ctx)
    assert first.metadata['lp_decision']['action'] == 'lp_rebalance', first.asdict()
    assert first.metadata['state'] == 'awaiting_approval' and ctx.rpc.sends == 0
    saved = ctx.state.get('lp_operation')
    rid = first.metadata['rebalance_id']
    ctx.identity = replace(ctx.identity, strategy_run_id='later-producer')
    ctx.config.params = {}
    ctx.inputs = None
    again = assets.run(ctx)
    assert again.metadata['rebalance_id'] == rid
    assert again.metadata['reason'] == 'rebalance_requires_original_producer_approval'
    assert ctx.state.get('lp_operation') == saved and ctx.rpc.sends == 0


def test_real_sdk_collection_requires_separate_approval(tmp_path, monkeypatch, assets):
    ctx = real_context(tmp_path, monkeypatch, token=lp.NFT_ID)
    seed_owned(ctx)
    ctx.rpc.owed0 = 2_000_000
    result = assets.run(ctx)
    assert result.metadata['lp_decision']['action'] == 'lp_collect', result.asdict()
    action = ctx.financial.get(result.metadata['financial_action']['action_id'])
    assert action['request']['kind'] == 'lp_collect' and action['state'] == 'awaiting_approval'
    assert ctx.rpc.sends == 0


def test_live_hold_and_empty_inventory_without_pool_never_prepare(tmp_path, monkeypatch, assets):
    ctx = real_context(tmp_path, monkeypatch, token=lp.NFT_ID)
    seed_owned(ctx)
    result = assets.run(ctx)
    assert result.metadata['lp_decision']['reason'] == 'inside_range'
    assert ctx.state.get('lp_operation') is None and ctx.rpc.sends == 0
    ctx = real_context(tmp_path/'empty', monkeypatch)
    ctx.snapshot.pop('pool')
    # Publish without a pool; account_state cannot invent one from empty inventory.
    ctx.inputs.publish('source:lp_fixture', ctx.snapshot)
    result = assets.run(ctx)
    assert result.status.value == 'error' and ctx.state.get('lp_operation') is None
    assert FinancialStore(ctx.runtime_config).list_actions(ctx.identity) == []


def test_simulation_cannot_seed_live_persistence(tmp_path, monkeypatch, assets):
    ctx = real_context(tmp_path, monkeypatch, token=lp.NFT_ID)
    seed_owned(ctx)
    ctx.mode = 'paper'
    ctx.config.params['lp_source']['component_binding'] = ctx.snapshot['account']['component_binding']
    ctx.config.params['lp_source']['decimals'] = [6, 6]
    ctx.position['pool']['tick'] = 100
    publish(ctx)
    assert assets.run(ctx).metadata['lp_decision']['reason'] == 'range_exit_first_observed'
    assert ctx.state.get('lp_policy_state:live') is None
    assert ctx.state.get('lp_policy_state:paper')['outside_since'] is not None


def test_confirmed_cycle_stays_latched_when_inputs_or_config_disappear(tmp_path, monkeypatch, assets):
    ctx = real_context(tmp_path, monkeypatch)
    first = assets.run(ctx)
    action_id = first.metadata['financial_action']['action_id']
    gateway = FinancialGateway(ctx.runtime_config)
    quote = gateway.store.get_action(action_id, ctx.identity, internal=True)['quote']
    ctx.rpc.minted, ctx.rpc.liquidity = True, 2000000000
    ctx.rpc.receipt = lp.receipt(quote)
    ctx.rpc.transaction = dict(hash=lp.TX_HASH, **{'from':lp.SENDER}, to=lp.MANAGER,
                               input=quote['transaction']['data'], value='0x0')
    gateway.store.mark(action_id, state='submitted',
                       submission={'transaction_hash':lp.TX_HASH,'action_id':action_id})
    ctx.config.params = {}
    ctx.inputs = None
    assert assets.run(ctx).metadata['financial_action']['state'] == 'confirmed'
    assert assets.run(ctx).metadata['financial_action']['action_id'] == action_id
    assert len(FinancialStore(ctx.runtime_config).list_actions(ctx.identity)) == 1
    assert ctx.state.get('lp_policy_state:live')['last_rebalance'] <= ctx.clock.now().timestamp()
    assert ctx.rpc.sends == 0
