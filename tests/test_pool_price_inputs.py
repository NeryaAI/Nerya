"""The native engine can consume attributed pool prices, never relabel their source."""
import json
from types import SimpleNamespace

import pytest

from test_request_scope import env as pool_env, admit, FakeGateway, spans_output
from test_geckoterminal_data_api import Fake, ARGS

from nerya.core.errors import TradingError
from nerya.skills.builtin.backtest.scripts import pool_price_inputs as inputs
from nerya.strategies import request_scope

env = pool_env


def test_pool_preparation_preserves_data_identity_and_quote_units(tmp_path, monkeypatch):
    package = SimpleNamespace(root=tmp_path, manifest=SimpleNamespace(
        extras={'params': {'price_simulation': {'dataset_id': 'pool'}}},
        asdict=lambda: {'markets': ['JUPITER:SOL/USDC']}))
    cfg = SimpleNamespace(markets=['JUPITER:SOL/USDC'], timeframes=['1h'], tf='1h',
        agent_replay=False, _window_start=7200, _window_end=14400, warmup_bars=1)
    rows = [{'ts': stamp, 'open': 100, 'high': 101, 'low': 99, 'close': 100, 'volume': 5}
            for stamp in (0, 3600, 7200, 10800, 14400)]
    pool = {'address': 'pool-identity', 'base': {'address': 'base-mint', 'symbol': 'SOL'},
            'quote': {'address': 'quote-mint', 'symbol': 'USDC'}}
    window = {'start': '1970-01-01T02:00:00Z', 'end': '1970-01-01T04:00:00Z'}
    payload = {'rows': rows, '_envelope': {'pool': pool, 'timeframe': '1h', 'quote_units': 'USDC',
                                        'requested_window': window}}
    prepared = {'files': {'pool.json': json.dumps(payload).encode()}, 'manifest': {'version': 1},
        'receipts': [{'id': 'pool', 'sha256': 'a'*64, 'sources': [{'sha256': 'b'*64}],
                      'requested_window': window}]}
    monkeypatch.setattr(inputs, 'prepare_inputs', lambda *a, **k: prepared)
    monkeypatch.setattr(request_scope, 'ensure_contract', lambda *a, **k: {'trusted': True})
    admitted = []
    monkeypatch.setattr(request_scope, 'validate_prepared', lambda *a, **k: admitted.append(a))
    result = inputs.prepare_pool_prices(None, package, cfg, tmp_path)
    assert admitted and result['proof']['pool'] == pool
    assert [row['ts'] for row in result['series'][cfg.markets[0]]['1h']] == [3600, 7200, 10800]
    assert (tmp_path/'prepared_inputs/pool.json').read_bytes() == prepared['files']['pool.json']
    metrics, provenance = {'performance_evidence': True, 'replay': {}}, {'data_kind': 'historical'}
    inputs.bind_pool_price_evidence(metrics, provenance, result)
    assert metrics['performance_evidence'] is False and metrics['accounting_currency'] == 'USDC'
    assert metrics['replay']['scope'] == inputs.SCOPE and metrics['replay']['funds_sent'] is False
    assert provenance['price_simulation']['pool'] == pool
    assert provenance['price_simulation']['execution_target'] == 'JUPITER:SOL/USDC'
    assert provenance['prepared_datasets'] == prepared['receipts']
    monkeypatch.setattr(request_scope, 'ensure_contract', lambda *a, **k: None)
    with pytest.raises(TradingError, match='admitted request scope'):
        inputs.prepare_pool_prices(None, package, cfg, tmp_path)


@pytest.mark.smoke
def test_real_pool_producer_through_native_worker_and_persisted_scope_receipt(env, monkeypatch):
    """Only HTTP and semantic extraction are fake; saved strategy runs in the real worker."""
    import hashlib
    from pathlib import Path
    from nerya.connectors.http import UrllibHttp
    from nerya.core import yaml_io
    from nerya.llm.gateway import LLMGateway
    from nerya.skills.builtin.backtest.scripts.backtest_run import run_strategy_backtest
    from copy import deepcopy
    from nerya.strategies.tool_verification import source_binding, attach_strategy_checks, delivery_source_versions
    from nerya.strategies.verification import replay_scope_receipt, completed_replay_receipt
    from nerya.task_delivery import start_delivery, read_delivery
    from nerya.tools.types import ToolCall, ToolResult

    text = '创建一个 Solana Jupiter SOL/USDC 趋势策略；可自行选择同资产池价格做模拟，不交易。'
    admit(env, cid='native-pool-integration', sid='native-pool-integration', text=text,
          created=1793577700)

    def choose(docs):
        value = spans_output(docs[0], chain='Solana', protocol='Jupiter', base='SOL', quote='USDC')
        evidence = spans_output(docs[0], price_simulation='可自行选择同资产池价格做模拟')['fields']['price_simulation']
        value['fields']['price_simulation'] = {'scope': 'same_asset_pool_price', 'evidence': evidence}
        return value

    gateway = FakeGateway(choose)
    monkeypatch.setattr(LLMGateway, 'call_messages', lambda self, **kw: gateway.call_messages(**kw))
    contract = request_scope.ensure_contract(env)
    assert contract['constraints']['price_simulation'] == 'same_asset_pool_price'
    assert contract['constraints']['resources'][0]['chain'] == 'solana'
    assert contract['constraints']['resources'][0]['protocol']['id'] == 'jupiter'
    assert 'start_utc' not in contract['constraints'] and 'end_utc' not in contract['constraints']
    fake = Fake()

    def public_http(self, method, url, **kw):
        assert ('/pools/' + ARGS['pool_address'] in url
                or url.startswith('https://lite-api.jup.ag/tokens/v2/search?'))
        return fake.request(method, url, **kw)

    monkeypatch.setattr(UrllibHttp, 'request', public_http)
    root = env.paths.root / 'strategies/native_pool_integration'
    root.mkdir(parents=True)
    yaml_io.dump(env.paths.root / 'nerya.yml', {'runtime': {'live_trading_enabled': False}})
    manifest = {
        'version': 1, 'strategy_id': root.name, 'title': 'Native pool integration fixture',
        'mode': 'paper', 'entrypoint': 'main.py:run', 'execution_mode': 'script',
        'markets': ['JUPITER:SOL/USDC'], 'accounts': ['paper_main'], 'timeframe': '1h',
        'chain': 'solana', 'protocol': 'jupiter',
        'schedule': {'type': 'none', 'enabled': False}, 'policy': {'allow_direct_order': True},
        'params': {'price_simulation': {'dataset_id': 'pool'}},
        'backtest': {'tf': '1h', 'window_days': 7, 'warmup_bars': 50,
                     'start_utc': ARGS['start'], 'end_utc': ARGS['end']},
    }
    yaml_io.dump(root / 'strategy.yml', manifest)
    (root / 'replay_inputs.json').write_text(json.dumps({'version': 1, 'datasets': [
        {'id': 'pool', 'provider': 'geckoterminal', 'action': 'pool_ohlcv', 'args': ARGS}]}))
    (root / 'main.py').write_text('''def run(ctx):
    market = ctx.config.markets[0]
    n = ctx.state.get('integration_calls', 0) + 1
    ctx.state.set('integration_calls', n)
    if n == 1:
        return ctx.trading.submit_intent(market=market, side='buy', size=100,
            size_unit='usd', order_type='market', reasoning='integration_entry')
    if n == 3:
        return ctx.trading.close_position(market=market, side='long', reasoning_ref='integration_exit')
    return ctx.result.hold(reason='integration_invocation_' + str(n))
''')
    binding = source_binding(env, ToolCall(name='strategy_backtest', arguments={'strategy_id': root.name}))
    result = run_strategy_backtest(strategy_id=root.name, workspace=env.paths.root, runtime_config=env)
    assert result['ok'] and result['engine'] == 'native'
    assert len(fake.calls) == 3 and len(gateway.calls) == 1
    assert result['accounting_currency'] == 'USDC' and result['performance_evidence'] is False
    assert result['requested_window_complete'] is True
    out_dir = Path(result['out_dir'])
    worker = json.loads((out_dir / 'worker_result.json').read_text())
    assert worker['ok'] is True
    execution = worker['result']
    assert len(execution['decisions']) == 168
    assert any('integration_invocation_' in str(row) for row in execution['decisions'])
    assert execution['order_attempts'] == 2
    assert [fill['side'] for fill in execution['trades']] == ['buy', 'sell']
    assert all(fill['market'] == 'JUPITER:SOL/USDC' for fill in execution['trades'])
    metrics = json.loads(Path(result['metrics_path']).read_text())
    assert metrics['performance_evidence'] is False and metrics['accounting_currency'] == 'USDC'
    provenance = metrics['provenance']
    proof = provenance['price_simulation']
    receipt = provenance['prepared_datasets'][0]
    blob = (out_dir / receipt['snapshot_file']).read_bytes()
    envelope = json.loads(blob)['_envelope']
    assert proof['scope'] == 'historical_pool_price_simulation'
    assert proof['execution_target'] == 'JUPITER:SOL/USDC'
    assert proof['pool'] == envelope['pool'] and proof['pool']['dex'] == 'orca'
    assert proof['quote_units'] == 'USDC'
    assert proof['price_units'] == envelope['quote_units'] == 'quote_token_per_base_token'
    assert envelope['volume_available'] is False
    assert len(envelope['token_identities']) == 2
    assert all(t['catalog_source_sha256'] == envelope['sources'][2]['sha256']
               and t['identity_scope'] == 'current_official_catalog'
               for t in envelope['token_identities'])
    assert proof['sources'] == receipt['sources'] == envelope['sources']
    assert proof['prepared_input_sha256'] == receipt['sha256'] == hashlib.sha256(blob).hexdigest()
    assert len(provenance['datasets']) == 1 and provenance['datasets'][0]['rows'] == 218
    assert json.loads((out_dir / 'run.json').read_text())['status'] == 'completed'
    # Re-read metrics and prepared snapshots, rather than trusting in-memory preparation.
    replay = {**result, 'provenance': provenance}
    assert request_scope.check_receipt(env, binding, replay=replay)['status'] == 'missing'
    original = deepcopy(contract)
    checked = replay_scope_receipt(env, binding, replay=replay)
    assert checked['status'] == 'passed', checked
    assert contract == original and checked['request_scope_hash'] == contract['request_scope_hash']
    assert checked['prepared_inputs'] == {receipt['snapshot_file']: receipt['sha256']}
    required = ['source_binding', 'request_scope_match', 'historical_run', 'report_output',
                'data_coverage', 'historical_data', 'replay_source', 'execution_integrity']
    start_delivery(env.paths, command_id='native-pool-integration', session_id='native-pool-integration',
        skill_id='backtest', workflow_id='strategy_replay', requested_scope={'summary': text},
        stages=[{'id': 'replay', 'state': 'completed', 'required_checks': required,
                 'required_artifacts': ['report', 'run_receipt']}],
        workflow_contract={'skill_id': 'backtest', 'workflow_id': 'strategy_replay',
                           'required_checks': required, 'required_artifacts': ['report', 'run_receipt']})
    def aggregate():
        produced = attach_strategy_checks(ToolResult.from_json(tool_use_id='receipt', name='strategy_backtest',
            data=deepcopy(replay)), before=binding, after=binding, kind='backtest', config=env)
        return {row['check_id']: row for row in produced.metadata['verification']['checks']}
    rows = aggregate()
    assert rows['request_scope_match']['status'] == rows['historical_data']['status'] == 'passed'
    assert rows['historical_performance']['required'] is False
    assert rows['historical_performance']['status'] == 'missing'
    delivery = read_delivery(env.paths, command_id='native-pool-integration', session_id='native-pool-integration')
    assert delivery['completion']['allowed'] and delivery['completion']['status'] == 'partial'
    assert (binding['artifact_id'], 'request_scope_match') not in delivery_source_versions(env, delivery)
    completed = completed_replay_receipt(root, root.name, None)
    assert completed['scope'] == 'historical_pool_price_simulation' and completed['performance_evidence'] is False
    from nerya.tools.native import strategy_runtime
    from nerya.tools.result_contracts import tool_json_data
    from nerya.skills.builtin.backtest.scripts import backtest_run
    def no_replay(*args, **kwargs):
        pytest.fail('receipt recheck must not execute replay or call a model/provider')
    monkeypatch.setattr(backtest_run, 'run_strategy_backtest', no_replay)
    monkeypatch.setattr(strategy_runtime, '_strategy_backtest_handler', no_replay)
    monkeypatch.setattr(UrllibHttp, 'request', no_replay)
    monkeypatch.setattr(LLMGateway, 'call_messages', no_replay)
    # Isolate validation execution only; the public wrapper, saved evidence
    # checks, merged metadata and delivery persistence all run normally.
    monkeypatch.setattr(strategy_runtime, '_strategy_validate_handler', lambda call, **kw:
        ToolResult.from_json(tool_use_id=call.id, name=call.name, data={
            'ok': True, 'structural': {'status': 'passed'},
            'behavioral': {'status': 'passed', 'exit_code': 0}}))
    def revalidate():
        return strategy_runtime.strategy_validate_handler(ToolCall(name='strategy_validate',
            arguments={'strategy_id': root.name}), config=env)
    rechecked = revalidate()
    assert rechecked.semantic_success is True
    merged = {row['check_id']: row for row in rechecked.metadata['verification']['checks']}
    assert all(merged[key]['status'] == 'passed' for key in
               ('strategy_contract', 'behavioral_tests', 'request_scope_match', 'historical_data'))
    assert merged['historical_performance']['status'] == 'missing'
    assert merged['historical_performance']['required'] is False
    assert tool_json_data(rechecked)['backtest_evidence']['replay_executed'] is False
    assert tool_json_data(rechecked)['delivery_completion']['allowed'] is True
    assert tool_json_data(rechecked)['delivery_completion']['status'] == 'partial'
    assert len(fake.calls) == 3 and len(gateway.calls) == 1
    assert request_scope.current_contract(env) == original
    denied = deepcopy(contract)
    del denied['constraints']['price_simulation']
    monkeypatch.setattr(request_scope, 'current_contract', lambda *a, **kw: denied)
    assert replay_scope_receipt(env, binding, replay=replay)['status'] != 'passed'
    denied = deepcopy(contract)
    denied['constraints']['start_utc'] = request_scope._timestamp(ARGS['start']) + 3600
    assert replay_scope_receipt(env, binding, replay=replay)['status'] == 'failed'
    denied = deepcopy(contract)
    denied['constraints']['resources'][0]['chain'] = 'ethereum'
    assert replay_scope_receipt(env, binding, replay=replay)['status'] == 'failed'
    monkeypatch.setattr(request_scope, 'current_contract', lambda *a, **kw: contract)
    wrong_quote = deepcopy(replay)
    wrong_quote['provenance']['price_simulation']['pool']['quote']['symbol'] = 'USDT'
    assert replay_scope_receipt(env, binding, replay=wrong_quote)['status'] != 'passed'
    wrong_chain = deepcopy(replay)
    wrong_chain['provenance']['price_simulation']['pool']['network'] = 'ethereum'
    assert replay_scope_receipt(env, binding, replay=wrong_chain)['status'] != 'passed'
    wrong_target = deepcopy(replay)
    wrong_target['provenance']['datasets'][0]['market'] = 'JUPITER:SOL/USDT'
    assert replay_scope_receipt(env, binding, replay=wrong_target)['status'] != 'passed'
    (out_dir / receipt['snapshot_file']).write_bytes(blob + b' ')
    rechecked = revalidate()
    assert rechecked.semantic_success is False
    rows = {row['check_id']: row for row in rechecked.metadata['verification']['checks']}
    assert rows['request_scope_match']['status'] != 'passed' and rows['historical_data']['status'] == 'missing'
    delivery = read_delivery(env.paths, command_id='native-pool-integration', session_id='native-pool-integration')
    assert delivery['completion']['allowed'] is False
    out_dir.joinpath('report.md').unlink()
    rechecked = revalidate()
    assert rechecked.semantic_success is False
    rows = {row['check_id']: row for row in rechecked.metadata['verification']['checks']}
    assert rows['report_output']['status'] == 'missing' and rows['historical_data']['status'] == 'missing'
