"""Offline regressions for admitted rate windows and provider-visible repairs."""
from copy import deepcopy
from pathlib import Path
import json

import pytest

from nerya.core.config import Config
from nerya.data_api.defillama import defillama_specs
from nerya.data_api.registry import DataApiRegistry
from nerya.skills.builtin.backtest.scripts.replay_inputs import prepare_inputs
from nerya.strategies import request_scope as scope
from nerya.tools.types import ToolCall
from test_defillama_data_api import Fake, PID, POOL
from test_request_scope import (
    FakeGateway, admit, env, spans_output, assistant_env, parent_request, create_task, followup_task,
)

pytestmark = pytest.mark.smoke
ANCHOR = 1793173237.9919071
WINDOW = {'start_utc': '2026-09-28T00:00:00Z', 'end_utc': '2026-10-28T00:00:00Z'}


@pytest.fixture
def rate_scope(env, monkeypatch):
    text = 'Aave USDC yield, last 30 days; no trading.'
    admit(env, cid='rate-original', sid='rate-session', text=text, created=ANCHOR)
    choose = lambda docs: spans_output(docs[0], protocol='Aave', asset='USDC', window_days='30 days')
    contract = scope.ensure_contract(env, gateway=FakeGateway(choose))
    manifest = {'markets': ['AAVE_V3:ETH-USDC'], 'params': {'rate_replay': {
        'version': 1, 'dataset_id': 'rates', 'initial_cash_units': 1000,
        'unit_price_usd': 1, 'fee_bps': 10, 'max_gap_seconds': 90000}}}
    declaration = {'version': 1, 'datasets': [{'id': 'rates', 'provider': 'defillama',
        'action': 'yield_history', 'args': {'pool_id': PID, 'start': '2026-09-28', 'end': '2026-10-28'}}]}
    monkeypatch.setattr(scope, 'target_manifest', lambda *_: manifest)
    monkeypatch.setattr(scope, 'target_files', lambda *_: {'replay_inputs.json': json.dumps(declaration)})
    return env, contract, manifest, declaration, choose


@pytest.mark.parametrize('timeframe', [None, '1h', '4h'])
def test_rate_replay_uses_admitted_bounds_without_candidate_candle_grid(rate_scope, timeframe):
    config, contract, manifest, declaration, _ = rate_scope
    if timeframe:
        manifest['timeframe'] = timeframe
        manifest['backtest'] = {'tf': timeframe}
    original = deepcopy(contract)
    assert contract['constraints']['start_utc'] == 1790553600
    assert contract['constraints']['end_utc'] == 1793145600
    assert contract['constraints']['window_anchor'] == ANCHOR
    assert scope.compare(contract, manifest) == WINDOW
    assert scope._input_declaration(config, {}, WINDOW) == declaration
    assert scope.freeform_guard(config, ToolCall(name='strategy_backtest'))[0] is None
    assert contract == original


def test_rate_relative_scope_survives_reload_and_later_followup(assistant_env):
    task = assistant_env
    task.config.data['llm']['tiers'] = {'medium': {'provider': 'openai', 'model': 'fixture-model', 'allowed_tasks': ['agent.loop']}}
    task.config.data['agent']['native']['tier'] = 'medium'
    text = 'Aave USDC yield, last 30 days; no trading.'
    cfg, parent = parent_request(task, text=text, scopes=('execute:strategy',))
    receipt = create_task(task, cfg, parent, prompt=text)
    row = task.manager.store.claim(receipt['task_session_id'], 'rate-worker')
    config = Config(task.config.paths, deepcopy(task.config.data))
    config.data['runtime'].update(command_id=row['command_id'], command_session_id=row['session_id'],
                                 command_turn_id=row['turn_id'], financial_actor_id=row['actor_id'])
    choose = lambda docs: spans_output(docs[0], protocol='Aave', asset='USDC', window_days='30 days')
    original = scope.ensure_contract(config, gateway=FakeGateway(choose))
    manifest = {'markets': ['AAVE_V3:ETH-USDC'], 'params': {'rate_replay': {}}}
    pinned = scope.compare(original, manifest)
    restarted = Config(config.paths, deepcopy(config.data))
    assert scope.current_contract(restarted) == original
    assert scope.ensure_contract(restarted, gateway=FakeGateway()) == original
    with task.manager.store.transaction() as con:
        con.execute("UPDATE agent_commands SET state='interrupted' WHERE command_id=?", (row['command_id'],))
        con.execute("UPDATE agent_command_queues SET worker_owner='',lease_until=0 WHERE session_id=?", (row['session_id'],))
    text = 'Continue; no trading.'
    cfg, parent = parent_request(task, operation='task_follow_up', target=receipt['task_id'],
                                 cid='rate-parent-followup', text=text, scopes=('execute:strategy',))
    followup = followup_task(task, cfg, parent, text=text)
    row = task.manager.store.claim(followup['task_session_id'], 'rate-followup-worker')
    restarted.data['runtime'].update(command_id=row['command_id'], command_turn_id=row['turn_id'])
    follow = scope.ensure_contract(restarted, gateway=FakeGateway(choose))
    assert follow['constraints'] == original['constraints']
    assert scope.compare(follow, manifest) == pinned
    assert scope.current_contract(restarted, command_id=receipt['command_id']) == original


@pytest.mark.parametrize('field,value', [('start_utc', '2026-09-28T07:00:00Z'),
    ('end_utc', '2026-10-28T07:00:00Z'), ('window_days', 7)])
def test_rate_candidate_cannot_override_admitted_window(rate_scope, field, value):
    _, contract, manifest, _, _ = rate_scope
    with pytest.raises(scope.ScopeError) as failed:
        scope.compare(contract, {**manifest, 'backtest': {field: value}})
    assert failed.value.reason == 'request_scope_mismatch'
    assert failed.value.differences[0]['field'] == field


@pytest.mark.parametrize('field', ['start', 'end', 'both'])
def test_rate_declaration_shift_reaches_provider_as_actionable_error(rate_scope, field):
    from nerya.agent.loop import WorkspaceNativeAgentLoop
    from nerya.llm.messages import _openai_render_messages
    config, contract, _, declaration, _ = rate_scope
    before = deepcopy(contract)
    fields = ('start', 'end') if field == 'both' else (field,)
    for key in fields:
        declaration['datasets'][0]['args'][key] += 'T07:00:00Z'
    failure, callback = scope.freeform_guard(config, ToolCall(name='strategy_backtest'))
    assert failure.is_error and callback is None
    differences = [{'field': 'replay_inputs.' + key, 'expected': WINDOW[key + '_utc'],
                    'actual': declaration['datasets'][0]['args'][key]} for key in fields]
    assert failure.error.detail['differences'] == differences
    assert failure.error.recovery_hint['differences'] == differences
    assert failure.error.recovery_hint['request_scope_window'] == WINDOW
    assert failure.content[0].data['request_scope_window'] == WINDOW
    assert scope.current_contract(config) == before
    assert failure.content[0].data['next_required_action'] == failure.error.recovery_hint
    assert failure.error.retryable is False
    block = WorkspaceNativeAgentLoop._render_tool_result(object.__new__(WorkspaceNativeAgentLoop), failure)
    messages = _openai_render_messages(system='', messages=[{'role': 'user', 'content': [block]}])
    rendered = next(row['content'] for row in messages if row['role'] == 'tool')
    for expected in (differences[0]['field'], differences[0]['expected'], differences[0]['actual'],
                     'replay_inputs.json', 'retry only after correction', 'Do not change request scope'):
        assert expected in rendered
    assert all(value in rendered for value in WINDOW.values())


@pytest.mark.parametrize('damage', [None, 'resource', 'window', 'bytes'])
def test_prepared_rate_evidence_remains_exact_and_hash_bound(rate_scope, tmp_path, damage):
    config, contract, manifest, declaration, _ = rate_scope
    registry = DataApiRegistry()
    pool = {**POOL, 'symbol': 'USDT'} if damage == 'resource' else POOL
    for spec in defillama_specs(Fake(pools=[pool])):
        registry.register_action(spec)
    (tmp_path / 'replay_inputs.json').write_text(json.dumps(declaration))
    prepared = prepare_inputs(tmp_path, registry=registry)
    if damage == 'window':
        payload = json.loads(prepared['files']['rates.json'])
        payload['_envelope']['requested_window']['start'] = '2026-09-28T07:00:00Z'
        # Regenerate an internally consistent receipt; scope still must reject it.
        import hashlib
        blob = json.dumps(payload).encode()
        prepared['files']['rates.json'] = blob
        prepared['receipts'][0]['sha256'] = hashlib.sha256(blob).hexdigest()
        prepared['receipts'][0]['requested_window'] = payload['_envelope']['requested_window']
    elif damage == 'bytes':
        prepared['files']['rates.json'] += b' '
    failure, check = scope.freeform_guard(config, ToolCall(name='strategy_backtest'))
    assert failure is None
    if damage:
        with pytest.raises(scope.ScopeError):
            check(prepared)
    else:
        receipt = prepared['receipts'][0]
        assert check(prepared) == {receipt['snapshot_file']: receipt['sha256']}
        assert receipt['requested_window'] == {'start': WINDOW['start_utc'], 'end': WINDOW['end_utc'], 'inclusive': True}
        assert receipt['requested_window_complete'] is not True
        assert scope.current_contract(config) == contract


def test_reserve_index_prepared_input_preserves_pool_and_window(rate_scope):
    """Synthetic producer bytes exercise scope binding, not historical chain proof."""
    import hashlib

    config, contract, manifest, declaration, _ = rate_scope
    dataset = declaration['datasets'][0]
    dataset.update(provider='aave', action='reserve_index_history', args={
        'pool_id': PID, 'chain_id': 1, 'market': '0x' + '1' * 40,
        'underlying_token': '0x' + '2' * 40, 'rpc_provider': 'drpc',
        'start': WINDOW['start_utc'], 'end': WINDOW['end_utc'],
    })
    assert scope._input_declaration(config, {}, WINDOW) == declaration
    for damage in (None, 'pool', 'window'):
        pool_id = 'different-pool' if damage == 'pool' else PID
        window = {'start': WINDOW['start_utc'], 'end': WINDOW['end_utc']}
        if damage == 'window':
            window['start'] = '2026-09-29T00:00:00Z'
        envelope = {
            'mock': False, 'pool': {**POOL, 'pool_id': pool_id},
            'pool_identity_scope': 'current_catalog_metadata_checked_against_historical_atoken',
            'observation_kind': 'historical_reserve_index_observations',
            'replay_scope': 'historical_reserve_index_policy_simulation',
            'requested_window': window, 'sources': [{'sha256': 'a' * 64}],
        }
        blob = json.dumps({'_envelope': envelope, 'rows': [{'pool_id': pool_id}]}).encode()
        receipt = {
            'id': 'rates', 'provider': 'aave', 'action': 'reserve_index_history',
            'snapshot_file': 'prepared_inputs/rates.json', 'rows': 1,
            'sha256': hashlib.sha256(blob).hexdigest(),
            'sources': envelope['sources'], 'requested_window': window,
        }
        prepared = {'manifest': declaration, 'files': {'rates.json': blob}, 'receipts': [receipt]}
        if damage:
            with pytest.raises(scope.ScopeError):
                scope.validate_prepared(contract, manifest, declaration, prepared, config=config)
        else:
            assert scope.validate_prepared(contract, manifest, declaration, prepared, config=config) == {
                receipt['snapshot_file']: receipt['sha256']}


def test_rate_replay_result_cannot_claim_shifted_window(rate_scope):
    _, contract, manifest, _, _ = rate_scope
    replay = {'provenance': {'datasets': [{'market': manifest['markets'][0]}]},
              'requested_start_utc': WINDOW['start_utc'], 'requested_end_utc': WINDOW['end_utc']}
    assert scope.compare(contract, manifest, replay=replay) == WINDOW
    replay['requested_end_utc'] = '2026-10-28T07:00:00Z'
    with pytest.raises(scope.ScopeError) as failed:
        scope.compare(contract, manifest, replay=replay)
    assert failed.value.differences[0]['field'] == 'replay.end_utc'


def test_selected_pool_prepared_identity_and_producer_failure_details(rate_scope, tmp_path, monkeypatch):
    import hashlib
    from nerya.skills.builtin.backtest.scripts import rate_replay, replay_inputs
    from test_rate_replay import setup

    config, contract, manifest, declaration, _ = rate_scope
    contract = deepcopy(contract)
    contract['constraints']['resources'][0].update(
        pool=PID, chain='ethereum', protocol=scope._protocol('aave-v3'))
    manifest['params']['rate_source'] = {
        'pool_id': PID, 'project': 'aave-v3', 'chain': 'Ethereum', 'asset': 'USDC'}
    registry = DataApiRegistry()
    for spec in defillama_specs(Fake()):
        registry.register_action(spec)
    (tmp_path / 'replay_inputs.json').write_text(json.dumps(declaration))
    prepared = prepare_inputs(tmp_path, registry=registry)
    before = deepcopy(contract)
    receipt = prepared['receipts'][0]
    assert scope.validate_prepared(contract, manifest, declaration, prepared, config=config) == {
        receipt['snapshot_file']: receipt['sha256']}

    package, _ = setup(tmp_path)
    target = tmp_path / 'strategies' / 'rate_test'
    target.parent.mkdir()
    package.root.rename(target)
    monkeypatch.setattr(rate_replay, 'simulate_rates',
                        lambda *a, **k: pytest.fail('mismatched inputs reached replay'))
    # Rehash internally consistent provider bytes: identity/window still must fail.
    for field, value, expected_field in (
        ('pool_id', 'other-pool', 'prepared.pool'),
        ('chain', 'Arbitrum', 'prepared.chain'),
        ('symbol', 'USDT', 'prepared.asset'),
        ('project', 'aave', 'prepared.protocol'),
        ('end', '2026-10-28T07:00:00Z', 'prepared.end'),
        ('bytes', None, None),
    ):
        damaged = deepcopy(prepared)
        data = json.loads(damaged['files']['rates.json'])
        if field == 'end':
            data['_envelope']['requested_window']['end'] = value
            damaged['receipts'][0]['requested_window'] = data['_envelope']['requested_window']
        elif field != 'bytes':
            data['_envelope']['pool'][field] = value
            if field == 'pool_id':
                for row in data['rows']:
                    row['pool_id'] = value
                damaged['manifest']['datasets'][0]['args']['pool_id'] = value
        blob = json.dumps(data).encode()
        damaged['files']['rates.json'] = blob
        damaged['receipts'][0]['sha256'] = hashlib.sha256(blob).hexdigest()
        if field == 'bytes':
            damaged['files']['rates.json'] += b' '
        monkeypatch.setattr(replay_inputs, 'prepare_inputs', lambda *a, **k: damaged)
        validate = lambda inputs: scope.validate_prepared(
            contract, manifest, damaged['manifest'], inputs, config=config)
        with pytest.raises(scope.ScopeError) as failed:
            rate_replay.run_rate_backtest(
                workspace=tmp_path, strategy_id='rate_test', validate_prepared_inputs=validate)
        failure = json.loads(Path(failed.value.failure_path).read_text())
        assert failure['status'] == 'failed' and failure['phase'] == 'preparing_data'
        assert failure['details']['reason'] == failed.value.reason
        assert failure['details']['differences'] == scope._safe_differences(failed.value)
        assert failed.value.receipt == failure['details']
        if expected_field:
            assert failure['details']['differences'][0]['field'] == expected_field
        assert contract == before


def test_cex_candle_timeframe_preserves_admitted_window_and_explicit_dates(env):
    for timeframe in ('1h', '4h'):
        text = 'Binance ETH/USDT, last 30 days'
        admit(env, cid='cex-' + timeframe, sid='cex-' + timeframe, text=text, created=ANCHOR)
        contract = scope.ensure_contract(env, gateway=FakeGateway(lambda docs: spans_output(
            docs[0], venue='Binance', base='ETH', quote='USDT', window_days='30 days')))
        manifest = {'markets': ['BINANCE:ETHUSDT'], 'timeframe': timeframe}
        pinned = {key: scope._iso(contract['constraints'][key]) for key in ('start_utc', 'end_utc')}
        assert scope.compare(contract, manifest) == pinned
        assert scope.compare(contract, {**manifest, 'backtest': pinned}) == pinned
        shifted = {key: scope._iso(contract['constraints'][key] + 3600) for key in pinned}
        with pytest.raises(scope.ScopeError):
            scope.compare(contract, manifest, settings=shifted)
    text = 'Aave USDC from 2026-09-28T03:17:00Z to 2026-10-28T03:17:00Z'
    admit(env, cid='explicit', sid='explicit', text=text, created=ANCHOR)
    contract = scope.ensure_contract(env, gateway=FakeGateway(lambda docs: spans_output(
        docs[0], protocol='Aave', asset='USDC', start_utc='2026-09-28T03:17:00Z', end_utc='2026-10-28T03:17:00Z')))
    for params in ({}, {'rate_replay': {}}):
        manifest = {'markets': ['AAVE_V3:ETH-USDC'], 'timeframe': '1h', 'params': params}
        assert scope.compare(contract, manifest)['end_utc'] == '2026-10-28T03:17:00Z'
        with pytest.raises(scope.ScopeError):
            scope.compare(contract, {**manifest, 'backtest': {'end_utc': WINDOW['end_utc']}})


def test_freeform_error_bounds_redacts_and_omits_exception_text(rate_scope, monkeypatch):
    config, _, _, _, _ = rate_scope
    secret = 'sk-' + 's' * 48
    differences = [{'field': 'replay_inputs.start', 'expected': WINDOW['start_utc'],
                    'actual': secret + 'x' * 10000, 'private': secret}] * 20
    def reject(*args, **kwargs):
        raise scope.ScopeError('request_scope_mismatch', differences)
    monkeypatch.setattr(scope, '_input_declaration', reject)
    failure, _ = scope.freeform_guard(config, ToolCall(name='strategy_backtest'))
    assert secret not in failure.error.message
    assert secret not in json.dumps(failure.error.detail)
    assert len(failure.error.detail['differences']) == 4
    assert len(failure.error.message) < 2400
    assert 'private' not in failure.error.detail['differences'][0]
    def unknown(*args, **kwargs):
        raise RuntimeError(secret)
    monkeypatch.setattr(scope, '_input_declaration', unknown)
    failure, _ = scope.freeform_guard(config, ToolCall(name='strategy_backtest'))
    assert secret not in failure.error.message
    assert 'saved request contract' in failure.error.message
