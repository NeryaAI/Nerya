"""Offline contracts for the copied rate policy and actual strategy entrypoint."""
from copy import deepcopy
from datetime import datetime
import json
from pathlib import Path
import socket
import sys
from types import MappingProxyType, ModuleType, SimpleNamespace

import pytest

from nerya.strategies import ResultBuilder, StrategyResult
from nerya.strategies.input_context import StrategyInputContext

pytestmark = pytest.mark.smoke
TEMPLATES = Path(__file__).parents[1] / 'nerya/skills/builtin/strategy_author/templates'
NOW = '2040-02-03T12:00:00Z'
SOURCE = dict(pool_id='synthetic-pool', chain='synthetic-chain',
              asset='SYN', project='synthetic-lending')
POLICY = dict(entry_apy_percent=5, exit_apy_percent=2,
              max_age_seconds=60, allocation_units=12)


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError('Network is outside this offline contract')
    monkeypatch.setattr(socket.socket, 'connect', forbidden)
    monkeypatch.setattr(socket, 'create_connection', forbidden)


@pytest.fixture
def assets(monkeypatch):
    modules = {}
    for name in ('aave_rate_policy', 'aave_supply', 'aave_rate_strategy'):
        path = TEMPLATES / (name + '.py.j2')
        module = ModuleType(name)
        monkeypatch.setitem(sys.modules, name, module)
        exec(compile(path.read_text(), str(path), 'exec'), module.__dict__)
        modules[name] = module
    return SimpleNamespace(policy=modules['aave_rate_policy'],
                           strategy=modules['aave_rate_strategy'])


class ForbiddenIO:
    def __getattr__(self, name):
        raise AssertionError(f'Unexpected I/O: {name}')


def context(*, mode='backtest', apy=6, supplied=0, cash=9):
    return SimpleNamespace(
        mode=mode, result=ResultBuilder(),
        config=SimpleNamespace(params={'rate_policy': dict(POLICY), 'rate_source': dict(SOURCE)}),
        clock=SimpleNamespace(now=lambda: datetime.fromisoformat(NOW.replace('Z', '+00:00'))),
        trigger={'rate_observation': {**SOURCE, 'timestamp': NOW, 'deposit_apy_percent': apy},
                 'lending_position': {'supplied_units': supplied, 'cash_units': cash}},
        financial=ForbiddenIO(), inputs=ForbiddenIO(), state=ForbiddenIO(),
    )


@pytest.mark.parametrize('mode', ['paper', 'backtest'])
def test_actual_run_exposes_cash_capped_unit_signal_without_fills_or_io(assets, mode):
    result = assets.strategy.run(context(mode=mode))
    assert isinstance(result, StrategyResult)
    assert result.status.value == 'hold'
    assert result.metadata['rate_decision'] == {
        'action': 'lend_supply', 'amount_units': 9,
        'observation_timestamp': NOW, 'apy_percent': 6,
    }
    assert result.metadata['financial_execution_allowed'] is False
    assert result.metadata['funds_sent'] is False
    assert not result.order and not result.intent


@pytest.mark.parametrize('apy,supplied,cash,action,units', [
    (5, 0, 100, 'lend_supply', 12),
    (9, 0, 3.125, 'lend_supply', 3.125),
    (9, 0, 0, 'hold', 0),
    (9, 4, 100, 'hold', 0),
    (3, 0, 100, 'hold', 0),
    (3, 20, 100, 'hold', 0),
    (2, 20.125, 0, 'lend_withdraw', 20.125),
    (0, 20, 0, 'lend_withdraw', 20),
    (0, 0, 100, 'hold', 0),
])
def test_pure_policy_and_actual_run_agree_on_hysteresis_and_per_entry_units(
    assets, apy, supplied, cash, action, units,
):
    ctx = context(apy=apy, supplied=supplied, cash=cash)
    params, trigger = deepcopy(ctx.config.params), deepcopy(ctx.trigger)
    expected = dict(action=action, amount_units=units,
                    observation_timestamp=NOW, apy_percent=apy)
    decision = assets.policy.decide_aave_rate(
        policy=MappingProxyType(params['rate_policy']),
        source=MappingProxyType(params['rate_source']),
        observation=trigger['rate_observation'], position=trigger['lending_position'],
        as_of=ctx.clock.now(),
    )
    assert decision == expected
    result = assets.strategy.run(ctx)
    assert result.metadata['rate_decision'] == expected
    json.dumps(result.asdict(), allow_nan=False)
    assert ctx.config.params == params and ctx.trigger == trigger


@pytest.mark.parametrize('section,field,value', [
    ('rate_policy', 'entry_apy_percent', 2),
    ('rate_policy', 'entry_apy_percent', 1),
    ('rate_policy', 'exit_apy_percent', -1),
    ('rate_policy', 'allocation_units', float('nan')),
    ('rate_policy', 'allocation_units', float('inf')),
    ('rate_policy', 'allocation_units', -1),
    ('rate_policy', 'allocation_units', True),
    ('rate_policy', 'allocation_units', '12'),
    ('rate_policy', 'max_age_seconds', -1),
    ('rate_source', 'asset', ''),
    ('rate_observation', 'deposit_apy_percent', None),
    ('rate_observation', 'deposit_apy_percent', float('nan')),
    ('rate_observation', 'deposit_apy_percent', float('inf')),
    ('rate_observation', 'deposit_apy_percent', -1),
    ('rate_observation', 'deposit_apy_percent', True),
    ('rate_observation', 'deposit_apy_percent', '6'),
    ('rate_observation', 'pool_id', 'other-pool'),
    ('rate_observation', 'chain', 'other-chain'),
    ('rate_observation', 'asset', 'syn'),
    ('rate_observation', 'project', 'other-project'),
    ('rate_observation', 'timestamp', '2040-02-03T12:00:01Z'),
    ('rate_observation', 'timestamp', '2040-02-03T11:58:59Z'),
    ('rate_observation', 'timestamp', '2040-02-03T12:00:00'),
    ('rate_observation', 'timestamp', '2040-02-03T13:00:00+01:00'),
    ('rate_observation', 'timestamp', None),
    ('lending_position', 'cash_units', -1),
    ('lending_position', 'supplied_units', float('inf')),
    ('lending_position', 'supplied_units', True),
    ('lending_position', 'supplied_units', '1'),
])
def test_invalid_inputs_fail_closed_at_both_public_seams(assets, section, field, value):
    ctx = context()
    (ctx.config.params if section in ctx.config.params else ctx.trigger)[section][field] = value
    with pytest.raises(ValueError):
        assets.policy.decide_aave_rate(
            policy=ctx.config.params['rate_policy'], source=ctx.config.params['rate_source'],
            observation=ctx.trigger['rate_observation'], position=ctx.trigger['lending_position'],
            as_of=ctx.clock.now(),
        )
    result = assets.strategy.run(ctx)
    assert result.status.value == 'error'
    assert result.error_kind in {'configuration_required', 'data_error'}
    assert result.metadata['financial_execution_allowed'] is False
    assert 'rate_decision' not in result.metadata
    json.dumps(result.asdict(), allow_nan=False)


@pytest.mark.parametrize('section', ['rate_policy', 'rate_source', 'rate_observation', 'lending_position'])
def test_missing_input_is_never_assumed_empty_position_or_zero_rate(assets, section):
    ctx = context()
    (ctx.config.params if section in ctx.config.params else ctx.trigger).pop(section)
    result = assets.strategy.run(ctx)
    assert result.status.value == 'error'
    assert 'rate_decision' not in result.metadata


def test_utc_cutoff_and_freshness_are_inclusive_and_normalized(assets):
    ctx = context()
    ctx.trigger['rate_observation']['timestamp'] = '2040-02-03T11:59:00+00:00'
    result = assets.strategy.run(ctx)
    assert result.metadata['rate_decision']['observation_timestamp'] == '2040-02-03T11:59:00Z'
    ctx.clock.now = lambda: datetime(2040, 2, 3, 12)
    assert assets.strategy.run(ctx).error_kind == 'data_error'


def snapshot(*rows):
    return {'rows': list(rows), '_envelope': {'pool': {
        'pool_id': SOURCE['pool_id'], 'chain': SOURCE['chain'],
        'symbol': SOURCE['asset'], 'project': SOURCE['project'],
    }}}


def row(timestamp=NOW, apy=6):
    return {'timestamp': timestamp, 'deposit_apy_percent': apy, 'pool_id': SOURCE['pool_id']}


def test_published_source_selects_latest_utc_row_and_binds_catalog_identity(assets):
    data = snapshot(row('2040-02-03T12:00:01Z', 99),
                    row('2040-02-03T11:59:00Z', 3), row(apy=6))
    observed = assets.policy.select_rate_observation(snapshot=data, source=SOURCE, as_of=NOW)
    assert observed == {**SOURCE, 'timestamp': NOW, 'deposit_apy_percent': 6}


@pytest.mark.parametrize('case', ['empty', 'future_only', 'wrong_envelope', 'wrong_row',
                                  'conflicting_time', 'null_latest', 'missing_pool', 'naive'])
def test_published_source_never_backfills_or_substitutes_identity(assets, case):
    data = snapshot(row('2040-02-03T11:59:00Z', 3), row())
    if case == 'empty':
        data['rows'] = []
    elif case == 'future_only':
        data['rows'] = [row('2040-02-03T12:00:01Z')]
    elif case == 'wrong_envelope':
        data['_envelope']['pool']['symbol'] = 'OTHER'
    elif case == 'wrong_row':
        data['rows'][-1]['pool_id'] = 'other-pool'
    elif case == 'conflicting_time':
        data['rows'].append(row(apy=7))
    elif case == 'null_latest':
        data['rows'][-1]['deposit_apy_percent'] = None
    elif case == 'missing_pool':
        data['_envelope'].pop('pool')
    elif case == 'naive':
        data['rows'][-1]['timestamp'] = '2040-02-03T12:00:00'
    with pytest.raises(ValueError):
        observed = assets.policy.select_rate_observation(snapshot=data, source=SOURCE, as_of=NOW)
        assets.policy.decide_aave_rate(policy=POLICY, source=SOURCE, observation=observed,
                                      position={'supplied_units': 0, 'cash_units': 10}, as_of=NOW)


def bound_request():
    # A synthetic token binding: source identity is catalog text, never this address.
    return dict(kind='lend_supply', component_id='builtin:aave', protocol='aave_v3',
                wallet_id='synthetic-wallet', chain='synthetic-chain',
                asset='0x' + '1' * 40, amount='12', parameters={})


def live_context():
    ctx = context(mode='live')
    ctx.trigger = ForbiddenIO()  # Even apparently valid trigger data is not a live input.
    ctx.config.params.update(source_id='configured-rates', financial_request=bound_request())
    ctx.inputs = StrategyInputContext(
        sources=[{'id': 'configured-rates', 'provider': 'data_api'}], market=ForbiddenIO(),
        news=ForbiddenIO(), markets=(), run_id='synthetic-run', clock=ctx.clock,
    )
    ctx.clock.now_iso = lambda: NOW
    ctx.inputs.publish('source:configured-rates', snapshot(row()))
    ctx.state = SimpleNamespace(get=lambda key: None)
    return ctx


class AccountOnly:
    def __init__(self, account):
        self.account, self.reads = account, []

    def account_state(self, request):
        self.reads.append(deepcopy(request))
        return deepcopy(self.account)

    def __getattr__(self, name):
        raise AssertionError(f'No live mutation allowed: {name}')


def test_live_reads_real_account_shape_but_never_treats_wallet_supply_as_owned(assets):
    ctx = live_context()
    ctx.financial = AccountOnly({'health': 'ok', 'source': 'aave_v3:synthetic-deployment',
        'as_of': ctx.clock.now().timestamp(),
        'state': {'asset': bound_request()['asset'], 'supply_base': '99125000',
                  'decimals': 6, 'supply_scaled': '99000000', 'income_index': '1000'}})
    result = assets.strategy.run(ctx)
    assert result.error_kind == 'configuration_required'
    assert result.metadata['live_ready'] is False
    assert result.metadata['financial_execution_allowed'] is False
    assert result.metadata['missing_attestations'] == ['strategy_owned_supplied_units', 'available_cash_units']
    assert ctx.financial.reads == [bound_request()]
    assert 'rate_decision' not in result.metadata


@pytest.mark.parametrize('case', ['request', 'amount_nan', 'amount_zero', 'source_id', 'source_mismatch'])
def test_live_requires_explicit_binding_and_valid_published_feed_before_account_read(assets, case):
    ctx = live_context()
    if case == 'request':
        ctx.config.params.pop('financial_request')
    elif case == 'amount_nan':
        ctx.config.params['financial_request']['amount'] = 'NaN'
    elif case == 'amount_zero':
        ctx.config.params['financial_request']['amount'] = '0'
    elif case == 'source_id':
        ctx.config.params.pop('source_id')
    elif case == 'source_mismatch':
        ctx.config.params['rate_source']['asset'] = 'OTHER'
    result = assets.strategy.run(ctx)
    assert result.error_kind in {'configuration_required', 'data_error'}
    assert 'rate_decision' not in result.metadata


class RecoveryOnly:
    def __init__(self, state, reconciled=None):
        self.receipt = {'action_id': 'original-action', 'state': state,
                        'quote_hash': 'original-quote', 'approval_id': 'original-approval'}
        self.reconciled = reconciled or state
        self.reads = []

    def get(self, action_id):
        assert action_id == 'original-action'
        self.reads.append(('get', action_id))
        return dict(self.receipt)

    def reconcile(self, action_id):
        assert action_id == 'original-action'
        self.reads.append(('reconcile', action_id))
        return {**self.receipt, 'state': self.reconciled}

    def __getattr__(self, name):
        raise AssertionError(f'Only original-action recovery allowed: {name}')


def recovery_context(state='submitted', reconciled=None):
    ctx = live_context()
    ctx.config.params.pop('source_id')  # Recovery cannot depend on fresh entry data.
    ctx.config.params.pop('rate_policy')
    ctx.inputs = ForbiddenIO()
    saved = {'request': bound_request(), 'operation_key': 'original-operation',
             'action_id': 'original-action'}
    ctx.state = SimpleNamespace(get=lambda key: saved)
    ctx.financial = RecoveryOnly(state, reconciled)
    return ctx, saved


@pytest.mark.parametrize('state,reconciled,status', [
    ('submitted', 'confirming', 'submitted'),
    ('confirming', 'confirmed', 'filled'),
    ('unconfirmed', 'unconfirmed', 'error'),
    ('needs_recovery', 'needs_recovery', 'error'),
    ('awaiting_approval', None, 'pending_approval'),
    ('prepared', None, 'hold'),
    ('future_unknown_state', None, 'error'),
])
def test_live_recovery_uses_unchanged_helper_without_new_execution_or_rate_inputs(
    assets, state, reconciled, status,
):
    ctx, saved = recovery_context(state, reconciled)
    before = deepcopy(saved)
    result = assets.strategy.run(ctx)
    assert isinstance(result, StrategyResult)
    assert result.status.value == status
    assert result.metadata['financial_action']['action_id'] == 'original-action'
    assert result.metadata['financial_execution_allowed'] is False
    assert result.metadata['live_ready'] is False
    assert 'rate_decision' not in result.metadata
    if status == 'filled':
        assert 'funds_sent' not in result.metadata  # Recovery does not assert no prior transaction.
    if status == 'pending_approval':
        assert result.approval_id == 'original-approval'
    expected = [('get', 'original-action')]
    if state in {'submitted', 'confirming', 'unconfirmed', 'needs_recovery'}:
        expected.append(('reconcile', 'original-action'))
    assert ctx.financial.reads == expected
    assert saved == before


def test_interrupted_prepare_and_changed_binding_cannot_mint_new_actions(assets):
    ctx, saved = recovery_context()
    saved['action_id'] = None
    result = assets.strategy.run(ctx)
    assert result.error_kind == 'execution_unknown'
    assert not ctx.financial.reads
    saved['action_id'] = 'original-action'
    ctx.config.params['financial_request']['amount'] = '13'
    result = assets.strategy.run(ctx)
    assert result.error_kind == 'configuration_required'
    assert not ctx.financial.reads


def test_zero_allocation_still_allows_full_exit_and_thresholds_are_caller_configured(assets):
    ctx = context(apy=40, supplied=0, cash=100)
    ctx.config.params['rate_policy'].update(entry_apy_percent=50, exit_apy_percent=30, allocation_units=0)
    assert assets.strategy.run(ctx).metadata['rate_decision']['action'] == 'hold'
    ctx.trigger['lending_position']['supplied_units'] = 17.75
    ctx.trigger['rate_observation']['deposit_apy_percent'] = 30
    assert assets.strategy.run(ctx).metadata['rate_decision'] == dict(
        action='lend_withdraw', amount_units=17.75, observation_timestamp=NOW, apy_percent=30)


def test_nonlive_cannot_recover_even_if_a_request_or_pending_action_exists(assets):
    ctx = context()
    ctx.config.params['financial_request'] = bound_request()
    assert assets.strategy.run(ctx).metadata['rate_decision']['action'] == 'lend_supply'


def test_backtest_fee_adapter_caps_entry_without_mutating_actual_cash_or_pure_policy(assets):
    ctx = context(cash=100)
    ctx.config.params['rate_policy']['allocation_units'] = 200
    ctx.config.params['rate_replay'] = MappingProxyType({'version': 1, 'fee_bps': 100})
    prior = deepcopy(ctx.trigger)
    pure = assets.policy.decide_aave_rate(
        policy=ctx.config.params['rate_policy'], source=SOURCE,
        observation=ctx.trigger['rate_observation'], position=ctx.trigger['lending_position'],
        as_of=ctx.clock.now(),
    )
    assert pure['amount_units'] == 100
    result = assets.strategy.run(ctx)
    decision = result.metadata['rate_decision']
    assert decision['action'] == 'lend_supply'
    assert decision['amount_units'] == pytest.approx(99.00990099009901)
    assert decision['amount_units'] * 1.01 <= 100 + 1e-12
    assert ctx.trigger == prior
    assert result.status.value == 'hold'
    assert result.metadata['financial_execution_allowed'] is False


@pytest.mark.parametrize('fee', [None, True, '100', -1, 10000, float('nan'), float('inf')])
def test_backtest_rejects_invalid_fee_assumptions(assets, fee):
    ctx = context()
    ctx.config.params['rate_replay'] = {'version': 1, 'fee_bps': fee}
    result = assets.strategy.run(ctx)
    assert result.error_kind == 'configuration_required'
    assert 'rate_decision' not in result.metadata
    assert result.metadata['financial_execution_allowed'] is False


@pytest.mark.parametrize('replay', [None, [], {}, {'version': 1}])
def test_present_replay_config_requires_explicit_fee_assumption(assets, replay):
    ctx = context()
    ctx.config.params['rate_replay'] = replay
    assert assets.strategy.run(ctx).error_kind == 'configuration_required'


def test_fee_adapter_never_caps_withdrawal_and_never_changes_paper_sizing(assets):
    ctx = context(apy=2, supplied=110, cash=0)
    ctx.config.params['rate_replay'] = {'version': 1, 'fee_bps': 100}
    assert assets.strategy.run(ctx).metadata['rate_decision']['amount_units'] == 110
    paper = context(mode='paper', cash=10)
    paper.config.params['rate_replay'] = {'version': 1, 'fee_bps': 100}
    assert assets.strategy.run(paper).metadata['rate_decision']['amount_units'] == 10


@pytest.mark.skipif(sys.platform != 'darwin', reason='Existing isolated worker requires macOS Seatbelt')
def test_copied_assets_run_in_real_isolated_host_with_injected_prior_positions(tmp_path):
    from nerya.strategies import candidate_validation as boundary
    from nerya.strategies.package import load_package_from_dir
    from nerya.skills.builtin.backtest.scripts.isolated_strategy import IsolatedStrategy

    # Only the known pre-start environment failure may skip this integration
    # check. Candidate failures below must fail; never relax the sandbox.
    try:
        boundary._runtime_libraries(boundary._python_executable())
    except ValueError as exc:
        if str(exc) != 'unresolved Python loader dependency':
            raise
        pytest.skip('Existing Seatbelt loader cannot resolve this interpreter dependency (@rpath)')

    root = tmp_path / 'synthetic-rate-package'
    root.mkdir()
    (root / 'strategy.yml').write_text(
        'id: synthetic_rates\nmode: paper\nmarkets: [SYN]\n'
        "schedule: {type: cron, cron: '0 * * * *'}\n"
    )
    for template, name in (('aave_rate_strategy', 'main'), ('aave_rate_policy', 'aave_rate_policy'),
                           ('aave_supply', 'aave_supply')):
        (root / (name + '.py')).write_text((TEMPLATES / (template + '.py.j2')).read_text())
    package = load_package_from_dir(root)
    params = {'rate_policy': POLICY, 'rate_source': SOURCE, 'rate_replay': {
        'version': 1, 'dataset_id': 'rates', 'initial_cash_units': 10,
        'unit_price_usd': 7, 'fee_bps': 0, 'max_gap_seconds': 60,
    }}

    def no_dispatch(*args):
        pytest.fail(f'Rate simulation must make no financial or host SDK calls: {args}')

    with IsolatedStrategy(package) as worker:
        for timestamp, apy, supplied, cash, action, units in (
            ('2040-02-03T12:00:00Z', 6, 0, 10, 'lend_supply', 10),
            ('2040-02-03T12:00:30Z', 4, 10.01, 0, 'hold', 0),
            ('2040-02-03T12:01:00Z', 2, 10.02, 0, 'lend_withdraw', 10.02),
        ):
            seconds = datetime.fromisoformat(timestamp.replace('Z', '+00:00')).timestamp()
            result = worker.run({
                'strategy_id': 'synthetic_rates', 'run_id': 'synthetic-replay',
                'clock': {'ts': seconds}, 'config': {'params': params},
                'trigger': {'rate_observation': {**SOURCE, 'timestamp': timestamp, 'deposit_apy_percent': apy},
                            'lending_position': {'supplied_units': supplied, 'cash_units': cash}},
            }, no_dispatch)
            assert result['status'] == 'hold'
            assert result['metadata']['financial_execution_allowed'] is False
            assert result['metadata']['rate_decision'] == dict(
                action=action, amount_units=units, observation_timestamp=timestamp, apy_percent=apy)
    assert worker.closed and worker.safety['process_exited']
