"""Custom token-denominated values must not become USD performance."""
from pathlib import Path

from nerya.skills.builtin.backtest.scripts.freeform_run import _build_metrics, _render_chart


def test_token_denomination_never_infers_usd_or_performance():
    result = _build_metrics(payload={'accounting_currency':'token1:0xfixture',
        'initial_capital_usd':999, 'final_equity_usd':1000, 'total_return_pct':99},
        equity_rows=[{'ts':1,'equity':'2'},{'ts':2,'equity':'3'}], trade_rows=[],
        strategy_id='lp',proposal_id=None,script_path=Path('scripts/custom_replay.py'))
    assert result['accounting_currency'] == 'token1:0xfixture'
    assert result['modeled_initial_value'] == 2
    assert result['modeled_final_value'] == 3
    for key in ('initial_capital_usd','final_equity_usd','total_return_usd','total_return_pct',
                'max_drawdown_pct','sharpe_ratio','profit_factor','win_rate_pct'):
        assert result[key] is None
    assert result['performance_evidence'] is False


def test_legacy_usd_custom_artifacts_remain_readable():
    result = _build_metrics(payload={},
        equity_rows=[{'ts':1,'equity':2},{'ts':2,'equity':3}], trade_rows=[],
        strategy_id='legacy',proposal_id=None,script_path=Path('scripts/custom_replay.py'))
    assert result['initial_capital_usd'] == 2
    assert result['final_equity_usd'] == 3
    assert result['performance_evidence'] is False


def test_unit_bearing_curve_does_not_create_legacy_dollar_chart_block(tmp_path):
    rows = [{'ts':1,'equity':2},{'ts':2,'equity':3}]
    metrics = _build_metrics(payload={'accounting_currency':'token1:fixture'},
        equity_rows=rows,trade_rows=[],strategy_id='lp',proposal_id=None,
        script_path=Path('scripts/custom_replay.py'))
    chart = _render_chart(out_dir=tmp_path,strategy_id='lp',backtest_ts='freeform_20261029_120000',
        equity_rows=rows,trade_rows=[],metrics=metrics,workspace_root=tmp_path)
    assert chart['meta']['accounting_currency'] == 'token1:fixture'
    assert chart['meta']['initial_capital_usd'] is None
    assert chart['panels'][0]['title'] == 'Modeled value (token1:fixture)'
    assert 'chart_blocks' not in chart


def test_only_host_bound_public_pool_denomination_survives_artifact_redaction():
    import hashlib
    import json
    from copy import deepcopy
    from nerya.skills.builtin.backtest.scripts.freeform_run import _redact_replay_payload

    token0, token1 = '0x' + '11' * 20, '0x' + '22' * 20
    envelope = {'provider': 'uniswap', 'action': 'pool_history',
                'row_contract': 'uniswap_v3_pool_history.v1', 'mock': False, 'chain_id': 1,
                'pool': {'chain_id': 1, 'token0': token0, 'token1': token1}}
    def prepared(env):
        blob = json.dumps({'_envelope': env, 'rows': []}).encode()
        return {'files': {'pool.json': blob}, 'receipts': [{'id': 'pool', 'provider': 'uniswap',
            'action': 'pool_history', 'sha256': hashlib.sha256(blob).hexdigest()}]}
    secret = '0x' + 'ab' * 32
    payload = {'accounting_currency': 'token1:' + token1, 'quote_token': token1,
               'private_key': secret, 'note': 'Bearer ' + 'secret' * 12,
               'candidate_hex': secret, 'nested': {'quote_token': token1}}
    clean = _redact_replay_payload(payload, prepared(envelope))
    assert clean['accounting_currency'] == payload['accounting_currency']
    assert clean['quote_token'] == token1
    assert secret not in json.dumps(clean) and payload['note'] not in json.dumps(clean)
    assert clean['nested']['quote_token'] != token1
    metrics = _build_metrics(payload=clean, equity_rows=[{'ts': 1, 'equity': 2}],
        trade_rows=[], strategy_id='lp', proposal_id=None, script_path=Path('custom.py'))
    assert metrics['accounting_currency'] == 'token1:' + token1
    assert metrics['initial_capital_usd'] is None and metrics['performance_evidence'] is False
    for changed in ({'quote_token': token0}, {'accounting_currency': 'token0:' + token1}):
        result = _redact_replay_payload({**payload, **changed}, prepared(envelope))
        assert result['accounting_currency'] != {**payload, **changed}['accounting_currency']
    for field, value in (('chain_id', 10), ('mock', True), ('row_contract', 'candidate_claim')):
        invalid = deepcopy(envelope)
        invalid[field] = value
        assert _redact_replay_payload(payload, prepared(invalid))['quote_token'] != token1
    assert _redact_replay_payload(payload, {'files': {}, 'receipts': []})['quote_token'] != token1


def test_equity_artifact_long_decimals_are_numeric_but_other_columns_stay_redacted(tmp_path):
    import json
    from nerya.skills.builtin.backtest.scripts.freeform_run import (
        _redact_artifact, _read_csv, _payload_rows,
    )

    decimal = '36.12345678901234567890123456789012345678901234567890'
    secret = '0x' + 'ab' * 32
    prepared = {'files': {}, 'receipts': []}
    raw = '\n'.join(['ts,equity,private_key,note',
        '1793300000,' + decimal + ',' + secret + ',Bearer ' + 'secret' * 12, ''])
    cleaned = _redact_artifact('equity.csv', raw, prepared)
    path = tmp_path / 'equity.csv'
    path.write_text(cleaned)
    rows = _read_csv(path)
    assert float(rows[0]['equity']) == float(decimal)
    assert 'REDACTED' not in rows[0]['equity']
    assert secret not in cleaned and 'Bearer ' + 'secret' * 12 not in cleaned
    for values in ([{'ts': 1793300000, 'value': decimal, 'private_key': secret}], [decimal]):
        payload = json.loads(_redact_artifact('result.json', json.dumps({
            'equity_curve': values, 'note': secret}), prepared))
        point = _payload_rows(payload, ('equity_curve',))[0]
        assert point.get('value', point.get('equity')) == float(decimal)
        assert secret not in json.dumps(payload)
    payload = json.loads(_redact_artifact('result.json', json.dumps({
        'equity_curve': [{'ts': 1, 'value': 'NaN'}, {'ts': 2, 'value': secret}]}), prepared))
    assert all(row['value'] is None for row in payload['equity_curve'])
    metrics = _build_metrics(payload={'accounting_currency': 'token1:fixture'},
        equity_rows=rows, trade_rows=[], strategy_id='lp', proposal_id=None, script_path=Path('custom.py'))
    chart = _render_chart(out_dir=tmp_path, strategy_id='lp', backtest_ts='fixture',
        equity_rows=rows, trade_rows=[], metrics=metrics, workspace_root=tmp_path)
    assert chart['panels'][0]['series'][0]['data'][0]['value'] == float(decimal)
